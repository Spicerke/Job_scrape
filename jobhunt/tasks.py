"""The actual work, callable from the CLI, the web UI, or the scheduler.

Every entry point goes through here so there is exactly one implementation of
"scrape", "score", and "send the digest" regardless of what triggered it.
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

from . import config_store, db, emailer
from .matching import DEFAULT_VARIANT, Scorer, TfidfIndex, _phrase_present
from .screening import Screener
from .sources.ats import ADAPTERS
from .sources.base import SourceError

# Live status for the web UI's activity strip.
STATUS: dict = {"running": None, "started": None, "last": None, "log": []}
_LOCK = threading.Lock()


def _log(msg: str) -> None:
    stamp = datetime.now().strftime("%H:%M:%S")
    STATUS["log"] = (STATUS["log"] + [f"{stamp}  {msg}"])[-60:]


def busy() -> str | None:
    return STATUS["running"]


def run_in_background(name: str, fn, *args, **kwargs) -> bool:
    """Start a task unless one is already running. Returns True if started."""
    with _LOCK:
        if STATUS["running"]:
            return False
        STATUS["running"] = name
        STATUS["started"] = datetime.now().isoformat(timespec="seconds")
        STATUS["log"] = []

    def wrapper():
        try:
            fn(*args, **kwargs)
            STATUS["last"] = f"{name} finished"
        except Exception as exc:  # noqa: BLE001
            _log(f"error: {exc}")
            STATUS["last"] = f"{name} failed: {exc}"
        finally:
            STATUS["running"] = None

    threading.Thread(target=wrapper, daemon=True).start()
    return True


# --------------------------------------------------------------------------

def resume_text(conn) -> str:
    row = db.get_resume(conn)
    return row["text"] if row else ""


def resume_variants(conn) -> list[tuple[str, str]]:
    """Stored variants, or the whole resume as a single unnamed one."""
    rows = db.get_resume_variants(conn)
    if rows:
        return [(r["name"], r["text"]) for r in rows]
    text = resume_text(conn)
    return [(DEFAULT_VARIANT, text)] if text else []


def scrape(conn, ats: str | None = None, company: str | None = None,
           stale_days: int = 10, log=_log) -> dict:
    companies = db.active_companies(conn, ats)
    if company:
        companies = [c for c in companies if company.lower() in c["name"].lower()]

    found = new = errors = 0
    for c in companies:
        fn = ADAPTERS.get(c["ats"])
        if not fn:
            continue
        run_id = db.start_run(conn, f"{c['ats']}:{c['slug']}")
        cf = cn = 0
        note = "ok"
        try:
            for job in fn(c["slug"], c["name"]):
                if not job["url"] or not job["title"]:
                    continue
                job["company_id"] = c["id"]
                job["is_target"] = c["is_target"]
                _, is_new = db.upsert_job(conn, job)
                cf += 1
                cn += int(is_new)
            db.mark_company_status(conn, c["id"], f"ok ({cf})")
        except SourceError as exc:
            note, errors = str(exc), errors + 1
            db.mark_company_status(conn, c["id"], f"error: {exc}")
        except Exception as exc:  # noqa: BLE001
            note, errors = f"unexpected: {exc}", errors + 1
            db.mark_company_status(conn, c["id"], f"error: {exc}")
        db.finish_run(conn, run_id, cf, cn, 0 if note == "ok" else 1, note)
        conn.commit()
        found += cf
        new += cn
        log(f"{c['name']}: {cf} postings, {cn} new" + ("" if note == "ok" else f" — {note}"))

    cutoff = (datetime.now(timezone.utc) - timedelta(days=stale_days)).isoformat()
    for source in ("greenhouse", "lever", "ashby", "smartrecruiters", "workable"):
        db.close_stale_jobs(conn, source, cutoff)
    conn.commit()

    log(f"done: {found} postings, {new} new, {errors} board errors")
    return {"found": found, "new": new, "errors": errors}


def score(conn, rescore: bool = True, log=_log) -> dict:
    cfg = config_store.load(conn)
    resume = resume_text(conn)
    if not resume:
        log("no resume uploaded — resume similarity and gap screening are off")
    variants = resume_variants(conn)
    if len(variants) > 1:
        log(f"matching against {len(variants)} resume variants: "
            + ", ".join(n for n, _ in variants))
    index = TfidfIndex(db.corpus_documents(conn))
    scorer = Scorer(cfg, resume, index, variants=variants or None)
    # Screening asks "can I apply at all", so it reads the whole document —
    # a requirement covered by any one variant still counts as covered.
    screener = Screener(cfg, resume)

    rows = (conn.execute("SELECT * FROM jobs WHERE is_open=1").fetchall()
            if rescore else db.unscored_jobs(conn))
    passed = 0
    for job in rows:
        status, reason = screener.screen(
            job["title"], job["description"] or "", job["location"], bool(job["remote"]))
        result = scorer.score(job["title"], job["description"] or "", job["location"] or "")
        result["screen_status"] = status
        result["screen_reason"] = reason
        db.save_scoring(conn, job["id"], result)
        passed += int(status == "pass")
    conn.commit()
    log(f"scored {len(rows)} postings — {passed} eligible, {len(rows)-passed} filtered")
    return {"scored": len(rows), "passed": passed}


def _role_matched_ids(conn, cfg) -> list[int]:
    patterns = [p.lower() for p in (cfg.get("targets", {}).get("roles") or [])]
    if not patterns:
        return []
    rows = conn.execute(
        "SELECT id, title FROM jobs WHERE is_open=1 AND screen_status='pass'").fetchall()
    out = []
    for row in rows:
        t = row["title"].lower()
        if any(all(_phrase_present(w, t) for w in p.split()) for p in patterns):
            out.append(row["id"])
    return out


def alerts(conn, dry_run: bool = False, log=_log) -> dict:
    cfg = config_store.load(conn)
    jobs = db.alertable_jobs(
        conn, float(cfg["search"].get("instant_min_score", 60)),
        _role_matched_ids(conn, cfg))
    if not jobs:
        log("no new alert-worthy postings")
        return {"sent": 0}

    html = emailer.render_digest(
        jobs, "New posting on your watchlist",
        f"{len(jobs)} posting(s) matched a target company or role", tiers=False)
    text = emailer.render_text(jobs)
    subject = (f"[jobhunt] {jobs[0]['title']} at {jobs[0]['company']}"
               if len(jobs) == 1 else f"[jobhunt] {len(jobs)} new target postings")
    if dry_run:
        log(f"dry run — {len(jobs)} alert(s) would be sent")
        return {"sent": 0, "would_send": len(jobs), "html": html}

    emailer.send(cfg, subject, html, text)
    for j in jobs:
        db.record_notification(conn, j["id"], "instant")
    conn.commit()
    log(f"sent {len(jobs)} alert(s)")
    return {"sent": len(jobs)}


def digest(conn, dry_run: bool = False, log=_log) -> dict:
    cfg = config_store.load(conn)
    ecfg = cfg.get("email", {})
    days = int(ecfg.get("lookback_days", 7))
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    jobs = db.digest_jobs(conn, since, float(cfg["search"].get("min_score", 45)))
    jobs = jobs[: int(ecfg.get("max_jobs", 40))]

    html = emailer.render_digest(
        jobs, "Your weekly job digest",
        f"{len(jobs)} postings from the last {days} days, ranked against your resume")
    text = emailer.render_text(jobs)
    if dry_run:
        log(f"dry run — digest would contain {len(jobs)} jobs")
        return {"sent": 0, "count": len(jobs), "html": html}

    emailer.send(cfg, f"[jobhunt] {len(jobs)} jobs to apply to this week", html, text)
    for j in jobs:
        db.record_notification(conn, j["id"], "weekly")
    conn.commit()
    log(f"digest sent to {ecfg.get('to_addr')} ({len(jobs)} jobs)")
    return {"sent": len(jobs)}


def daily_cycle(conn, log=_log) -> None:
    """What the scheduler runs each morning."""
    scrape(conn, log=log)
    score(conn, rescore=True, log=log)
    try:
        alerts(conn, log=log)
    except Exception as exc:  # noqa: BLE001 - email failure shouldn't lose the scrape
        log(f"alerts failed: {exc}")
