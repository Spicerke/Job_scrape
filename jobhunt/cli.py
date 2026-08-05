"""jobhunt command line interface."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from . import config_store, db, emailer, scheduler, tasks
from .sources.ats import ADAPTERS

ROOT = Path(__file__).resolve().parent.parent


def resolve(p: str) -> Path:
    q = Path(p)
    return q if q.is_absolute() else ROOT / q


# --------------------------------------------------------------------------

def cmd_init(args, conn):
    wrote = config_store.seed_from_yaml(conn, resolve(args.config), force=args.reset)
    print(f"database ready at {args.db}")
    print("settings seeded from config.yaml" if wrote
          else "settings already in database (use --reset to overwrite from config.yaml)")

    path = resolve(args.companies)
    if path.exists():
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        rows = data.get("companies", [])
        for c in rows:
            db.upsert_company(conn, c["name"], c["ats"], c["slug"], c.get("target", False))
        conn.commit()
        print(f"loaded {len(rows)} company boards "
              f"({sum(1 for c in rows if c.get('target'))} on the watchlist)")

    if args.resume:
        from .matching import DEFAULT_VARIANT, load_resume, load_resume_variants
        path = resolve(args.resume)
        text = load_resume(path)
        variants = load_resume_variants(path)
        db.set_resume(conn, Path(args.resume).name, text)
        db.set_resume_variants(conn, variants)
        conn.commit()
        print(f"resume loaded ({len(text.split())} words)")
        named = [n for n, _ in variants if n != DEFAULT_VARIANT]
        if named:
            print(f"  {len(named)} variants: {', '.join(named)}")
    elif not db.get_resume(conn):
        print("no resume yet — upload one in the web UI or pass --resume")

    print("next: `jobhunt check-boards`, then `jobhunt scrape`")


def cmd_check_boards(args, conn):
    ok, bad = 0, 0
    for c in db.active_companies(conn):
        fn = ADAPTERS.get(c["ats"])
        if not fn:
            print(f"  FAIL  {c['name']:<26} unknown board type '{c['ats']}'")
            bad += 1
            continue
        try:
            n = sum(1 for _ in fn(c["slug"], c["name"]))
            db.mark_company_status(conn, c["id"], f"ok ({n} jobs)")
            print(f"  ok    {c['name']:<26} {c['ats']}/{c['slug']}  {n} jobs")
            ok += 1
        except Exception as exc:  # noqa: BLE001
            db.mark_company_status(conn, c["id"], f"error: {exc}")
            print(f"  FAIL  {c['name']:<26} {c['ats']}/{c['slug']}  {exc}")
            bad += 1
    conn.commit()
    print(f"\n{ok} boards ok, {bad} failed")
    if bad:
        print("Fix a slug on the Companies page, or pause the board there.")


def cmd_scrape(args, conn):
    tasks.scrape(conn, ats=args.ats, company=args.company,
                 stale_days=args.stale_days, log=print)
    tasks.score(conn, rescore=True, log=print)


def cmd_score(args, conn):
    tasks.score(conn, rescore=True, log=print)


def cmd_digest(args, conn):
    cfg = config_store.load(conn)
    day = str(cfg["email"].get("digest_day", "friday")).lower()
    if not args.force and datetime.now().strftime("%A").lower() != day:
        print(f"today isn't {day} — skipping (use --force to send anyway)")
        return
    result = tasks.digest(conn, dry_run=args.dry_run, log=print)
    if args.dry_run:
        out = ROOT / "digest-preview.html"
        out.write_text(result["html"], encoding="utf-8")
        print(f"preview written to {out}")


def cmd_alerts(args, conn):
    result = tasks.alerts(conn, dry_run=args.dry_run, log=print)
    if args.dry_run and result.get("html"):
        out = ROOT / "alert-preview.html"
        out.write_text(result["html"], encoding="utf-8")
        print(f"preview written to {out}")


def cmd_run(args, conn):
    """One full cycle: scrape, score, alert. What cron calls each morning."""
    tasks.daily_cycle(conn, log=print)


def cmd_daemon(args, conn):
    conn.close()
    if args.with_web:
        import threading
        from . import web
        threading.Thread(
            target=scheduler.run_forever, args=(args.db,), daemon=True).start()
        web.serve(args.host, args.port, args.db)
    else:
        scheduler.run_forever(args.db)


def cmd_web(args, conn):
    conn.close()
    from . import web
    web.serve(args.host, args.port, args.db, debug=args.debug)


def cmd_list(args, conn):
    rows = conn.execute(
        """SELECT id, title, company, location, score, screen_status, screen_reason, url
           FROM jobs WHERE is_open=1 AND score >= ?
             AND (? = 0 OR screen_status='pass')
           ORDER BY is_target DESC, score DESC LIMIT ?""",
        (args.min_score, 0 if args.include_rejected else 1, args.limit),
    ).fetchall()
    if not rows:
        print("nothing matches — lower --min-score or run `jobhunt scrape`")
        return
    for r in rows:
        flag = "" if r["screen_status"] == "pass" else f"  [filtered: {r['screen_reason']}]"
        print(f"{r['id']:>5}  {r['score']:>5.1f}  {r['title'][:50]:<50} "
              f"{r['company'][:20]:<20} {(r['location'] or '')[:20]:<20}{flag}")
        if args.urls:
            print(f"        {r['url']}")


def cmd_show(args, conn):
    j = conn.execute("SELECT * FROM jobs WHERE id=?", (args.job_id,)).fetchone()
    if not j:
        sys.exit(f"no job with id {args.job_id}")
    from .screening import Screener
    sc = Screener(config_store.load(conn), tasks.resume_text(conn))
    coverage, have, missing = sc.requirement_gap(j["description"] or "")
    print(f"{j['title']} — {j['company']}")
    print(f"{j['location']}  |  {j['url']}")
    print(f"score {j['score']} (keywords {j['keyword_score']}, resume {j['resume_similarity']}, "
          f"title {j['title_score']})  screen: {j['screen_status']} {j['screen_reason']}")
    variant_scores = json.loads(j["variant_scores"] or "{}")
    if len(variant_scores) > 1:
        ranked = sorted(variant_scores.items(), key=lambda kv: -kv[1])
        print(f"send the '{j['best_variant']}' resume — "
              + ", ".join(f"{n} {s:.0f}" for n, s in ranked))
    print(f"requirement coverage: {coverage:.0%}")
    print(f"  you have:  {', '.join(have) or '—'}")
    print(f"  missing:   {', '.join(missing) or '—'}")
    print("-" * 70)
    print((j["description"] or "")[: args.chars])


def cmd_track(args, conn):
    db.set_application(conn, args.job_id, args.status, args.notes,
                       args.next, args.by)
    conn.commit()
    print(f"job {args.job_id} marked '{args.status}'")


def cmd_apps(args, conn):
    stages = {"live": db.LIVE_STAGES, "closed": db.CLOSED_STAGES}.get(args.show)
    rows = db.applications(conn, stages)
    if not rows:
        print("nothing tracked yet — `jobhunt track <id> applied`")
        return

    counts = db.application_counts(conn)
    applied = sum(c for s, c in counts.items() if s != "interested")
    replied = sum(counts.get(s, 0) for s in db.RESPONDED)
    rate = f"{replied / applied * 100:.0f}%" if applied else "—"
    print(f"applied {applied}   live {sum(counts.get(s, 0) for s in db.LIVE_STAGES)}   "
          f"heard back {replied}   offers {counts.get('offer', 0)}   "
          f"rejected {counts.get('rejected', 0)}   response rate {rate}")
    print("-" * 88)

    now = datetime.now(timezone.utc)
    for r in rows:
        age = ""
        if r["applied_at"]:
            try:
                then = datetime.fromisoformat(r["applied_at"])
                if then.tzinfo is None:
                    then = then.replace(tzinfo=timezone.utc)
                age = f"{(now - then).days}d"
            except ValueError:
                pass
        print(f"{r['job_id']:>5}  {db.STAGE_LABELS.get(r['status'], r['status']):<12} "
              f"{age:>4}  {r['title'][:38]:<40} {r['company'][:18]:<20}")
        if r["next_action"]:
            due = f" (by {r['next_action_date']})" if r["next_action_date"] else ""
            print(f"       next: {r['next_action']}{due}")


def cmd_stats(args, conn):
    q = conn.execute
    total = q("SELECT COUNT(*) c FROM jobs").fetchone()["c"]
    open_ = q("SELECT COUNT(*) c FROM jobs WHERE is_open=1").fetchone()["c"]
    passing = q("SELECT COUNT(*) c FROM jobs WHERE is_open=1 AND screen_status='pass'").fetchone()["c"]
    week = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    fresh = q("SELECT COUNT(*) c FROM jobs WHERE first_seen>=?", (week,)).fetchone()["c"]
    print(f"jobs: {total}   open: {open_}   eligible: {passing}   new this week: {fresh}")
    r = db.get_resume(conn)
    print(f"resume: {r['filename']} ({len(r['text'].split())} words)" if r else "resume: none")
    variants = db.get_resume_variants(conn)
    if len(variants) > 1:
        picks = q("""SELECT best_variant, COUNT(*) c FROM jobs
                     WHERE is_open=1 AND screen_status='pass' AND best_variant IS NOT NULL
                     GROUP BY best_variant ORDER BY c DESC""").fetchall()
        summary = ", ".join(f"{p['best_variant']} {p['c']}" for p in picks) or "nothing scored yet"
        print(f"variants: {len(variants)} — best fit by job: {summary}")
    reasons = q("""SELECT screen_reason, COUNT(*) c FROM jobs
                   WHERE screen_status='reject' AND screen_reason!=''
                   GROUP BY screen_reason ORDER BY c DESC LIMIT 8""").fetchall()
    if reasons:
        print("top filter reasons:")
        for row in reasons:
            print(f"   {row['c']:>4}  {row['screen_reason']}")


def cmd_config(args, conn):
    if args.action == "show":
        print(yaml.safe_dump(config_store.load(conn), sort_keys=False))
    elif args.action == "export":
        config_store.export_yaml(conn, resolve(args.file))
        print(f"wrote {args.file}")
    elif args.action == "import":
        config_store.seed_from_yaml(conn, resolve(args.file), force=True)
        print(f"loaded {args.file} into the database")


def cmd_export(args, conn):
    since = (datetime.now(timezone.utc) - timedelta(days=args.days)).isoformat()
    jobs = db.digest_jobs(conn, since, args.min_score)
    html = emailer.render_digest(jobs, "jobhunt board",
                                 f"{len(jobs)} open postings from the last {args.days} days")
    resolve(args.out).write_text(html, encoding="utf-8")
    print(f"wrote {len(jobs)} jobs to {args.out}")


# --------------------------------------------------------------------------

def main(argv=None):
    p = argparse.ArgumentParser(prog="jobhunt", description="Automated job collection and matching")
    p.add_argument("--config", default="config.yaml", help="seed file, used by init only")
    p.add_argument("--db", default=str(ROOT / "jobs.db"))
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="create the database, seed settings and boards")
    s.add_argument("--companies", default="companies.yaml")
    s.add_argument("--resume", help="path to your resume (.tex/.txt/.pdf)")
    s.add_argument("--reset", action="store_true", help="overwrite settings from config.yaml")
    s.set_defaults(fn=cmd_init)

    s = sub.add_parser("check-boards", help="verify every company slug resolves")
    s.set_defaults(fn=cmd_check_boards)

    s = sub.add_parser("scrape", help="pull boards and score new postings")
    s.add_argument("--ats"); s.add_argument("--company")
    s.add_argument("--stale-days", type=int, default=10)
    s.set_defaults(fn=cmd_scrape)

    s = sub.add_parser("score", help="re-rank everything against current settings")
    s.set_defaults(fn=cmd_score)

    s = sub.add_parser("run", help="one full cycle: scrape, score, alert (for cron)")
    s.set_defaults(fn=cmd_run)

    s = sub.add_parser("daemon", help="built-in scheduler, for containers without cron")
    s.add_argument("--with-web", action="store_true", help="also serve the web UI")
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=8000)
    s.set_defaults(fn=cmd_daemon)

    s = sub.add_parser("web", help="serve the web console")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--debug", action="store_true")
    s.set_defaults(fn=cmd_web)

    s = sub.add_parser("digest", help="send the weekly email")
    s.add_argument("--force", action="store_true")
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(fn=cmd_digest)

    s = sub.add_parser("alerts", help="send watchlist alerts")
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(fn=cmd_alerts)

    s = sub.add_parser("list", help="ranked jobs in the terminal")
    s.add_argument("--min-score", type=float, default=45)
    s.add_argument("--limit", type=int, default=40)
    s.add_argument("--include-rejected", action="store_true")
    s.add_argument("--urls", action="store_true")
    s.set_defaults(fn=cmd_list)

    s = sub.add_parser("show", help="one posting with its score breakdown")
    s.add_argument("job_id", type=int)
    s.add_argument("--chars", type=int, default=2000)
    s.set_defaults(fn=cmd_show)

    s = sub.add_parser("track", help="record application status")
    s.add_argument("job_id", type=int)
    s.add_argument("status", choices=db.STAGES)
    s.add_argument("--notes")
    s.add_argument("--next", help="next step, e.g. 'follow up with recruiter'")
    s.add_argument("--by", help="date that next step is due (YYYY-MM-DD)")
    s.set_defaults(fn=cmd_track)

    s = sub.add_parser("apps", help="the application pipeline")
    s.add_argument("--show", choices=["live", "closed", "all"], default="live")
    s.set_defaults(fn=cmd_apps)

    s = sub.add_parser("stats", help="database summary")
    s.set_defaults(fn=cmd_stats)

    s = sub.add_parser("config", help="show/export/import settings as YAML")
    s.add_argument("action", choices=["show", "export", "import"])
    s.add_argument("--file", default="config.yaml")
    s.set_defaults(fn=cmd_config)

    s = sub.add_parser("export", help="standalone HTML board")
    s.add_argument("--days", type=int, default=14)
    s.add_argument("--min-score", type=float, default=45)
    s.add_argument("--out", default="board.html")
    s.set_defaults(fn=cmd_export)

    args = p.parse_args(argv)
    conn = db.connect(args.db)
    try:
        args.fn(args, conn)
    finally:
        try:
            conn.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
