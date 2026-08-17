"""Web console: browse ranked jobs, edit every setting, upload your resume.

Runs anywhere Flask runs. Behind `JOBHUNT_WEB_PASSWORD` if that variable is
set; if it isn't, the app binds to localhost only and says so loudly, because
an unauthenticated settings page on a public IP is a bad afternoon.
"""
from __future__ import annotations

import io
import json
import os
import secrets
import tempfile
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path

from flask import (Flask, abort, flash, get_flashed_messages, redirect,
                   render_template, request, send_file, session, url_for)

from . import config_store, db, scheduler, tasks
from .sources.ats import ADAPTERS

DB_PATH = os.environ.get("JOBHUNT_DB", str(Path(__file__).resolve().parent.parent / "jobs.db"))
PASSWORD = os.environ.get("JOBHUNT_WEB_PASSWORD", "")
# An application sitting at "applied" this long with no reply gets nudged.
STALE_DAYS = 14

app = Flask(__name__)
app.secret_key = os.environ.get("JOBHUNT_SECRET_KEY") or secrets.token_hex(32)


def conn():
    return db.connect(DB_PATH)


def protected(fn):
    @wraps(fn)
    def inner(*a, **kw):
        if PASSWORD and not session.get("auth"):
            return redirect(url_for("login", next=request.path))
        return fn(*a, **kw)
    return inner


# --------------------------------------------------------------- text <-> list

def lines_to_list(raw: str) -> list[str]:
    return [l.strip() for l in (raw or "").splitlines() if l.strip()]


def _fmt_time(raw, default: tuple[int, int] | None) -> str | None:
    """Normalise a form value to 'HH:MM'. Returns None if it won't parse."""
    if raw is None or not str(raw).strip():
        return f"{default[0]:02d}:{default[1]:02d}" if default else None
    parsed = scheduler.parse_time(str(raw).strip(), default or (-1, -1))
    if parsed == (-1, -1):
        return None
    return f"{parsed[0]:02d}:{parsed[1]:02d}"


def lines_to_weights(raw: str) -> dict[str, float]:
    """Parse 'python: 3' per line. A bare term defaults to weight 1."""
    out: dict[str, float] = {}
    for line in lines_to_list(raw):
        term, _, weight = line.partition(":")
        term = term.strip().lower()
        if not term:
            continue
        try:
            out[term] = float(weight.strip()) if weight.strip() else 1.0
        except ValueError:
            out[term] = 1.0
    return out


def weights_to_lines(d: dict) -> str:
    return "\n".join(f"{k}: {v:g}" for k, v in (d or {}).items())


def days_since(iso: str | None) -> int | None:
    """Whole days between an ISO timestamp and now. None if unparseable."""
    if not iso:
        return None
    try:
        then = datetime.fromisoformat(iso)
    except ValueError:
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - then).days


app.jinja_env.filters["fromjson"] = lambda v: json.loads(v or "[]")
app.jinja_env.filters["lines"] = lambda v: "\n".join(v or [])
app.jinja_env.filters["weightlines"] = weights_to_lines
app.jinja_env.filters["days_since"] = days_since
app.jinja_env.filters["stagelabel"] = lambda s: db.STAGE_LABELS.get(s, s or "—")
app.jinja_env.filters["date"] = lambda v: (v or "")[:10]


# ------------------------------------------------------------------- routes

@app.route("/login", methods=["GET", "POST"])
def login():
    if not PASSWORD:
        return redirect(url_for("jobs"))
    if request.method == "POST":
        if secrets.compare_digest(request.form.get("password", ""), PASSWORD):
            session["auth"] = True
            return redirect(request.args.get("next") or url_for("jobs"))
        flash("That password didn't match.", "err")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@protected
def jobs():
    c = conn()
    try:
        cfg = config_store.load(c)
        min_score = float(request.args.get("min_score", cfg["search"]["min_score"]))
        show = request.args.get("show", "eligible")
        q = (request.args.get("q") or "").strip()
        days = int(request.args.get("days", 30))

        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        sql = ["SELECT j.*, a.status AS app_status FROM jobs j "
               "LEFT JOIN applications a ON a.job_id=j.id "
               "WHERE j.is_open=1 AND j.first_seen>=? AND j.score>=?"]
        args: list = [since, min_score]
        if show == "eligible":
            sql.append("AND j.screen_status='pass'")
        elif show == "filtered":
            sql.append("AND j.screen_status='reject'")
        elif show == "target":
            sql.append("AND j.is_target=1 AND j.screen_status='pass'")
        if q:
            sql.append("AND (j.title LIKE ? OR j.company LIKE ? OR j.location LIKE ?)")
            args += [f"%{q}%"] * 3
        sql.append("ORDER BY j.is_target DESC, j.score DESC LIMIT 300")
        rows = c.execute(" ".join(sql), args).fetchall()

        week = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        stats = {
            "open": c.execute("SELECT COUNT(*) n FROM jobs WHERE is_open=1").fetchone()["n"],
            "eligible": c.execute("SELECT COUNT(*) n FROM jobs WHERE is_open=1 AND screen_status='pass'").fetchone()["n"],
            "week": c.execute("SELECT COUNT(*) n FROM jobs WHERE first_seen>=?", (week,)).fetchone()["n"],
            "targets": c.execute("SELECT COUNT(*) n FROM companies WHERE is_target=1 AND active=1").fetchone()["n"],
        }
        resume = db.get_resume(c)
        return render_template("jobs.html", jobs=rows, stats=stats, cfg=cfg,
                               min_score=min_score, show=show, q=q, days=days,
                               resume=resume, status=tasks.STATUS)
    finally:
        c.close()


@app.route("/job/<int:job_id>")
@protected
def job_detail(job_id: int):
    c = conn()
    try:
        j = c.execute(
            "SELECT j.*, a.status AS app_status, a.notes FROM jobs j "
            "LEFT JOIN applications a ON a.job_id=j.id WHERE j.id=?", (job_id,)
        ).fetchone()
        if not j:
            return "No job with that id.", 404
        cfg = config_store.load(c)
        from .screening import Screener
        sc = Screener(cfg, tasks.resume_text(c))
        coverage, have, missing = sc.requirement_gap(j["description"] or "")
        return render_template("job_detail.html", j=j, coverage=coverage,
                               have=have, missing=missing,
                               matched=json.loads(j["matched_keywords"] or "[]"),
                               gaps=json.loads(j["missing_keywords"] or "[]"),
                               variant_scores=json.loads(j["variant_scores"] or "{}"),
                               stages=db.STAGES, labels=db.STAGE_LABELS)
    finally:
        c.close()


@app.route("/job/<int:job_id>/track", methods=["POST"])
@protected
def track(job_id: int):
    f = request.form
    status = f.get("status", "interested")
    c = conn()
    try:
        if status == "__delete__":
            db.delete_application(c, job_id)
            flash("Removed from the pipeline.", "ok")
        else:
            db.set_application(c, job_id, status, f.get("notes"),
                               f.get("next_action"), f.get("next_action_date"))
            flash(f"Marked {db.STAGE_LABELS.get(status, status)}.", "ok")
        c.commit()
    finally:
        c.close()
    return redirect(request.referrer or url_for("applications"))


# -------------------------------------------------------------- applications

@app.route("/applications")
@protected
def applications():
    show = request.args.get("show", "live")
    stages = {"live": db.LIVE_STAGES, "closed": db.CLOSED_STAGES,
              "interested": ["interested"]}.get(show)
    c = conn()
    try:
        rows = db.applications(c, stages)
        counts = db.application_counts(c)
        applied_total = sum(counts.get(s, 0) for s in db.STAGES if s != "interested")
        responded = sum(counts.get(s, 0) for s in db.RESPONDED)
        stats = {
            "applied": applied_total,
            "live": sum(counts.get(s, 0) for s in db.LIVE_STAGES),
            "responded": responded,
            "offers": counts.get("offer", 0),
            "rejected": counts.get("rejected", 0),
            "rate": (responded / applied_total * 100) if applied_total else 0.0,
        }
        # Things that have gone quiet or have a follow-up date that's passed.
        today = datetime.now(timezone.utc).date().isoformat()
        nudges = [r for r in db.applications(c, db.LIVE_STAGES)
                  if (r["next_action_date"] and r["next_action_date"] <= today)
                  or (r["status"] == "applied"
                      and (days_since(r["applied_at"]) or 0) >= STALE_DAYS)]
        return render_template("applications.html", rows=rows, stats=stats,
                               counts=counts, nudges=nudges, show=show,
                               stages=db.STAGES, labels=db.STAGE_LABELS,
                               stale_days=STALE_DAYS, status=tasks.STATUS,
                               cfg=config_store.load(c))
    finally:
        c.close()


@app.route("/application/<int:job_id>")
@protected
def application_detail(job_id: int):
    c = conn()
    try:
        rows = [r for r in db.applications(c) if r["job_id"] == job_id]
        if not rows:
            return "Not tracked.", 404
        return render_template("application_detail.html", a=rows[0],
                               events=db.application_events(c, job_id),
                               stages=db.STAGES, labels=db.STAGE_LABELS,
                               status=tasks.STATUS, cfg=config_store.load(c))
    finally:
        c.close()


# ------------------------------------------------------------------ settings

@app.route("/settings", methods=["GET", "POST"])
@protected
def settings():
    c = conn()
    try:
        cfg = config_store.load(c)
        if request.method == "POST":
            f = request.form
            # Every field here is read with a fallback, so a POST that isn't
            # actually the settings form reads as "user cleared everything"
            # and silently wipes months of tuning. The form declares itself.
            if f.get("form") != "settings":
                flash("That POST didn't look like the settings form — nothing saved.", "err")
                return redirect(url_for("settings"))
            cfg["profile"].update({
                "name": f.get("name", ""),
                "graduation": f.get("graduation", ""),
                "degree_level": f.get("degree_level", "bachelors"),
            })
            cfg["search"].update({
                "role_patterns": [r.lower() for r in lines_to_list(f.get("role_patterns"))],
                "min_score": float(f.get("min_score") or 45),
                "instant_min_score": float(f.get("instant_min_score") or 60),
                "keyword_saturation": int(f.get("keyword_saturation") or 8),
                "weights": {
                    "keyword": float(f.get("w_keyword") or .4),
                    "resume": float(f.get("w_resume") or .35),
                    "title": float(f.get("w_title") or .25),
                },
            })
            cfg["keywords"] = {
                "skills": lines_to_weights(f.get("skills")),
                "bonus": lines_to_weights(f.get("bonus")),
            }
            years = f.get("max_years_experience", "").strip()
            cfg["screening"].update({
                "max_years_experience": int(years) if years else None,
                "reject_title_terms": lines_to_list(f.get("reject_title_terms")),
                "reject_body_terms": lines_to_list(f.get("reject_body_terms")),
                "require_any_terms": lines_to_list(f.get("require_any_terms")),
                "locations": [l.lower() for l in lines_to_list(f.get("locations"))],
                "allow_remote": bool(f.get("allow_remote")),
                "min_requirement_coverage": float(f.get("min_requirement_coverage") or 0),
                "enforce_degree_level": bool(f.get("enforce_degree_level")),
                "enforce_graduation_year": bool(f.get("enforce_graduation_year")),
            })
            cfg["targets"]["roles"] = [r.lower() for r in lines_to_list(f.get("target_roles"))]
            cfg["email"].update({
                "smtp_host": f.get("smtp_host", ""),
                "smtp_port": int(f.get("smtp_port") or 587),
                "username": f.get("username", ""),
                "from_addr": f.get("from_addr", ""),
                "to_addr": f.get("to_addr", ""),
                "digest_day": f.get("digest_day", "friday"),
                "digest_time": _fmt_time(f.get("digest_time"), (17, 30)),
                "lookback_days": int(f.get("lookback_days") or 7),
                "max_jobs": int(f.get("max_jobs") or 40),
            })
            # Drop the superseded single-hour keys rather than leaving dead
            # values in the stored config for someone to misread later.
            cfg["email"].pop("digest_hour", None)
            times = [_fmt_time(t, None) for t in lines_to_list(f.get("scrape_times"))]
            cfg["schedule"].update({
                "scrape_times": [t for t in times if t] or ["09:00", "21:00"],
                "enabled": bool(f.get("schedule_enabled")),
            })
            cfg["schedule"].pop("scrape_hour", None)
            config_store.save(c, cfg)
            if f.get("rescore"):
                tasks.run_in_background("rescoring", _bg_score)
                flash("Settings saved. Re-ranking every posting now.", "ok")
            else:
                flash("Settings saved. Re-rank to apply them to existing jobs.", "ok")
            return redirect(url_for("settings"))

        return render_template("settings.html", cfg=cfg, resume=db.get_resume(c),
                               variants=db.get_resume_variants(c),
                               status=tasks.STATUS)
    finally:
        c.close()


ALLOWED_RESUME = (".tex", ".txt", ".md", ".pdf")


@app.route("/resume", methods=["POST"])
@protected
def upload_resume():
    from werkzeug.utils import secure_filename

    from .matching import split_input_variants

    files = [f for f in request.files.getlist("resume") if f and f.filename]
    if not files:
        flash("Choose a file first.", "err")
        return redirect(url_for("settings"))
    bad = [f.filename for f in files
           if Path(f.filename).suffix.lower() not in ALLOWED_RESUME]
    if bad:
        flash(f"Unsupported: {', '.join(bad)}. Use .tex, .txt, .md, or .pdf.", "err")
        return redirect(url_for("settings"))

    # Everything lands in one directory under its own name, so a main.tex that
    # \inputs variant-swe.tex still resolves when you select the whole set.
    with tempfile.TemporaryDirectory() as tmpdir:
        saved = []
        for f in files:
            dest = Path(tmpdir) / (secure_filename(Path(f.filename).name) or "resume")
            f.save(dest)
            saved.append(dest)

        included: set[str] = set()
        for path in saved:
            if path.suffix.lower() == ".tex":
                try:
                    included.update(d["source"] for d in split_input_variants(path))
                except Exception:  # noqa: BLE001 — fall through to the real read below
                    pass
        # A file pulled in by another upload is a fragment, not a resume of
        # its own; adding it separately would store a variant with no heading.
        primary = [p for p in saved if p.name not in included]

        c = conn()
        try:
            added = tasks.add_resume_documents(c, primary, log=lambda m: None)
        except Exception as exc:  # noqa: BLE001
            flash(f"Couldn't read that: {exc}", "err")
            return redirect(url_for("settings"))
        finally:
            c.close()

    thin = [d["name"] for d in added if len(d["text"].split()) < 40]
    if thin:
        flash(f"{', '.join(thin)} produced almost no text — a scanned image?", "err")

    tasks.run_in_background("rescoring", _bg_score)
    names = ", ".join(d["name"] for d in added)
    flash(f"{len(added)} variant(s) stored: {names}. Re-ranking now.", "ok")
    return redirect(url_for("settings"))


@app.route("/resume/<int:vid>/file")
@protected
def resume_file(vid: int):
    c = conn()
    try:
        row = db.get_resume_variant(c, vid)
        if not row or not row["file_data"]:
            abort(404)
        name = row["file_name"] or f"{row['name']}.pdf"
        data = bytes(row["file_data"])
    finally:
        c.close()
    return send_file(io.BytesIO(data), download_name=name, as_attachment=True)


@app.route("/resume/<int:vid>/delete", methods=["POST"])
@protected
def delete_variant(vid: int):
    c = conn()
    try:
        row = db.get_resume_variant(c, vid)
        if not row:
            abort(404)
        db.delete_resume_variant(c, vid)
        db.rebuild_resume(c)
        c.commit()
    finally:
        c.close()
    tasks.run_in_background("rescoring", _bg_score)
    flash(f"Removed '{row['name']}'. Re-ranking now.", "ok")
    return redirect(url_for("settings"))


# ----------------------------------------------------------------- companies

@app.route("/companies", methods=["GET", "POST"])
@protected
def companies():
    c = conn()
    try:
        if request.method == "POST":
            action = request.form.get("action")
            if action == "add":
                name = request.form.get("name", "").strip()
                ats = request.form.get("ats", "")
                slug = request.form.get("slug", "").strip()
                if not (name and slug and ats in ADAPTERS):
                    flash("Name, ATS, and slug are all required.", "err")
                else:
                    db.upsert_company(c, name, ats, slug, bool(request.form.get("is_target")))
                    c.commit()
                    flash(f"Added {name}. Run a scrape to pull its postings.", "ok")
            elif action == "target":
                db.set_company_flags(c, int(request.form["id"]),
                                     is_target=request.form.get("value") == "1")
                c.commit()
            elif action == "active":
                db.set_company_flags(c, int(request.form["id"]),
                                     active=request.form.get("value") == "1")
                c.commit()
            elif action == "delete":
                db.delete_company(c, int(request.form["id"]))
                c.commit()
                flash("Company removed. Its postings stay in the database.", "ok")
            return redirect(url_for("companies"))

        rows = c.execute(
            """SELECT c.*, (SELECT COUNT(*) FROM jobs j
                            WHERE j.company_id=c.id AND j.is_open=1) AS n_open
               FROM companies c ORDER BY c.is_target DESC, c.name"""
        ).fetchall()
        return render_template("companies.html", companies=rows,
                               adapters=sorted(ADAPTERS), status=tasks.STATUS)
    finally:
        c.close()


# ----------------------------------------------------------------- activity

def _with_conn(fn, *a, **kw):
    c = conn()
    try:
        return fn(c, *a, log=tasks._log, **kw)
    finally:
        c.close()


def _bg_scrape():
    _with_conn(tasks.scrape)
    _with_conn(tasks.score, rescore=True)


def _bg_score():
    _with_conn(tasks.score, rescore=True)


def _bg_digest():
    _with_conn(tasks.digest)


def _bg_alerts():
    _with_conn(tasks.alerts)


@app.route("/run/<what>", methods=["POST"])
@protected
def run(what: str):
    jobs_map = {
        "scrape": ("scraping boards", _bg_scrape),
        "score": ("rescoring", _bg_score),
        "digest": ("sending digest", _bg_digest),
        "alerts": ("sending alerts", _bg_alerts),
    }
    if what not in jobs_map:
        return "Unknown task.", 404
    label, fn = jobs_map[what]
    if tasks.run_in_background(label, fn):
        flash(f"Started: {label}.", "ok")
    else:
        flash(f"Already running: {tasks.busy()}. Wait for it to finish.", "err")
    return redirect(request.referrer or url_for("jobs"))


@app.route("/activity")
@protected
def activity():
    c = conn()
    try:
        return render_template("activity.html", runs=db.recent_runs(c),
                               status=tasks.STATUS)
    finally:
        c.close()


@app.route("/api/status")
@protected
def api_status():
    return {"running": tasks.STATUS["running"], "log": tasks.STATUS["log"][-12:],
            "last": tasks.STATUS["last"]}


def serve(host: str, port: int, db_path: str, debug: bool = False):
    global DB_PATH
    DB_PATH = db_path
    if not PASSWORD and host not in ("127.0.0.1", "localhost"):
        raise SystemExit(
            "Refusing to bind to a public interface without a password.\n"
            "Set JOBHUNT_WEB_PASSWORD, or use --host 127.0.0.1."
        )
    if not PASSWORD:
        print("no JOBHUNT_WEB_PASSWORD set — localhost only, no login required")
    app.run(host=host, port=port, debug=debug, threaded=True)
