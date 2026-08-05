"""SQLite storage for jobhunt."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS companies (
    id           INTEGER PRIMARY KEY,
    name         TEXT NOT NULL,
    ats          TEXT NOT NULL,
    slug         TEXT NOT NULL,
    is_target    INTEGER NOT NULL DEFAULT 0,
    active       INTEGER NOT NULL DEFAULT 1,
    last_scraped TEXT,
    last_status  TEXT,
    UNIQUE(ats, slug)
);

CREATE TABLE IF NOT EXISTS jobs (
    id                INTEGER PRIMARY KEY,
    source            TEXT NOT NULL,
    source_job_id     TEXT NOT NULL,
    company           TEXT NOT NULL,
    company_id        INTEGER REFERENCES companies(id),
    title             TEXT NOT NULL,
    location          TEXT,
    remote            INTEGER NOT NULL DEFAULT 0,
    url               TEXT NOT NULL,
    description       TEXT,
    department        TEXT,
    compensation      TEXT,
    posted_at         TEXT,
    first_seen        TEXT NOT NULL,
    last_seen         TEXT NOT NULL,
    is_open           INTEGER NOT NULL DEFAULT 1,
    score             REAL,
    resume_similarity REAL,
    keyword_score     REAL,
    title_score       REAL,
    matched_keywords  TEXT,
    missing_keywords  TEXT,
    screen_status     TEXT,
    screen_reason     TEXT,
    is_target         INTEGER NOT NULL DEFAULT 0,
    content_hash      TEXT,
    best_variant      TEXT,
    variant_scores    TEXT,
    UNIQUE(source, source_job_id)
);

CREATE TABLE IF NOT EXISTS notifications (
    id      INTEGER PRIMARY KEY,
    job_id  INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    kind    TEXT NOT NULL,
    sent_at TEXT NOT NULL,
    UNIQUE(job_id, kind)
);

CREATE TABLE IF NOT EXISTS applications (
    id               INTEGER PRIMARY KEY,
    job_id           INTEGER NOT NULL UNIQUE REFERENCES jobs(id) ON DELETE CASCADE,
    status           TEXT NOT NULL DEFAULT 'interested',
    applied_at       TEXT,
    notes            TEXT,
    next_action      TEXT,
    next_action_date TEXT,
    updated_at       TEXT NOT NULL
);

-- One row per stage change, so the dashboard can show a timeline and work out
-- how long something has been sitting without a reply.
CREATE TABLE IF NOT EXISTS application_events (
    id         INTEGER PRIMARY KEY,
    job_id     INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    status     TEXT NOT NULL,
    note       TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS resume (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    filename    TEXT,
    text        TEXT NOT NULL,
    uploaded_at TEXT NOT NULL
);

-- Tailored versions carved out of that one document. `resume.text` stays the
-- whole thing (screening asks "could I apply at all", which any variant can
-- answer); these are what postings get matched against individually.
CREATE TABLE IF NOT EXISTS resume_variants (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL UNIQUE,
    text       TEXT NOT NULL,
    position   INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY,
    source      TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    found       INTEGER DEFAULT 0,
    new_jobs    INTEGER DEFAULT 0,
    errors      INTEGER DEFAULT 0,
    note        TEXT
);
"""

# Applied after _migrate(), so an index may safely reference a column that an
# older database is only acquiring in this same connect() call.
INDEXES = """
CREATE INDEX IF NOT EXISTS idx_jobs_first_seen ON jobs(first_seen);
CREATE INDEX IF NOT EXISTS idx_jobs_score      ON jobs(score DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_open       ON jobs(is_open, screen_status);
CREATE INDEX IF NOT EXISTS idx_jobs_company    ON jobs(company);
CREATE INDEX IF NOT EXISTS idx_events_job      ON application_events(job_id, id);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: str | Path) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.executescript(INDEXES)
    return conn


def _migrate(conn) -> None:
    """Add columns introduced after a database was first created.

    CREATE TABLE IF NOT EXISTS won't alter a table that already exists, so a
    database from before resume variants needs the two columns added by hand.
    """
    have = {r["name"] for r in conn.execute("PRAGMA table_info(jobs)")}
    for column, decl in (("best_variant", "TEXT"), ("variant_scores", "TEXT")):
        if column not in have:
            conn.execute(f"ALTER TABLE jobs ADD COLUMN {column} {decl}")

    have = {r["name"] for r in conn.execute("PRAGMA table_info(applications)")}
    for column, decl in (("next_action", "TEXT"), ("next_action_date", "TEXT")):
        if column not in have:
            conn.execute(f"ALTER TABLE applications ADD COLUMN {column} {decl}")
    conn.commit()


# --------------------------------------------------------------------------
# companies
# --------------------------------------------------------------------------

def upsert_company(conn, name: str, ats: str, slug: str, is_target: bool) -> int:
    conn.execute(
        """INSERT INTO companies (name, ats, slug, is_target) VALUES (?,?,?,?)
           ON CONFLICT(ats, slug) DO UPDATE SET name=excluded.name,
                                                is_target=excluded.is_target""",
        (name, ats, slug, int(is_target)),
    )
    row = conn.execute(
        "SELECT id FROM companies WHERE ats=? AND slug=?", (ats, slug)
    ).fetchone()
    return row["id"]


def active_companies(conn, ats: str | None = None) -> list[sqlite3.Row]:
    q = "SELECT * FROM companies WHERE active=1"
    args: tuple = ()
    if ats:
        q += " AND ats=?"
        args = (ats,)
    return conn.execute(q + " ORDER BY is_target DESC, name", args).fetchall()


def mark_company_status(conn, company_id: int, status: str) -> None:
    conn.execute(
        "UPDATE companies SET last_scraped=?, last_status=? WHERE id=?",
        (utcnow(), status, company_id),
    )


# --------------------------------------------------------------------------
# jobs
# --------------------------------------------------------------------------

def upsert_job(conn, job: dict) -> tuple[int, bool]:
    """Insert or refresh a job. Returns (job_id, is_new)."""
    now = utcnow()
    existing = conn.execute(
        "SELECT id, content_hash FROM jobs WHERE source=? AND source_job_id=?",
        (job["source"], job["source_job_id"]),
    ).fetchone()

    if existing:
        conn.execute(
            """UPDATE jobs SET last_seen=?, is_open=1, title=?, location=?,
                   url=?, description=?, compensation=?, content_hash=?
               WHERE id=?""",
            (now, job["title"], job.get("location"), job["url"],
             job.get("description"), job.get("compensation"),
             job.get("content_hash"), existing["id"]),
        )
        return existing["id"], False

    cur = conn.execute(
        """INSERT INTO jobs (source, source_job_id, company, company_id, title,
                             location, remote, url, description, department,
                             compensation, posted_at, first_seen, last_seen,
                             is_target, content_hash)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (job["source"], job["source_job_id"], job["company"], job.get("company_id"),
         job["title"], job.get("location"), int(job.get("remote", 0)), job["url"],
         job.get("description"), job.get("department"), job.get("compensation"),
         job.get("posted_at"), now, now, int(job.get("is_target", 0)),
         job.get("content_hash")),
    )
    return cur.lastrowid, True


def close_stale_jobs(conn, source: str, cutoff_iso: str) -> int:
    cur = conn.execute(
        "UPDATE jobs SET is_open=0 WHERE source=? AND last_seen < ? AND is_open=1",
        (source, cutoff_iso),
    )
    return cur.rowcount


def save_scoring(conn, job_id: int, result: dict) -> None:
    conn.execute(
        """UPDATE jobs SET score=?, resume_similarity=?, keyword_score=?,
               title_score=?, matched_keywords=?, missing_keywords=?,
               screen_status=?, screen_reason=?, best_variant=?,
               variant_scores=? WHERE id=?""",
        (result["score"], result["resume_similarity"], result["keyword_score"],
         result["title_score"], json.dumps(result["matched"]),
         json.dumps(result["missing"]), result["screen_status"],
         result["screen_reason"], result.get("best_variant"),
         json.dumps(result.get("variant_scores") or {}), job_id),
    )


def unscored_jobs(conn) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM jobs WHERE score IS NULL AND is_open=1"
    ).fetchall()


def corpus_documents(conn, limit: int = 3000) -> list[str]:
    rows = conn.execute(
        "SELECT title || ' ' || COALESCE(description,'') AS doc FROM jobs "
        "ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [r["doc"] for r in rows]


def digest_jobs(conn, since_iso: str, min_score: float) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT * FROM jobs
           WHERE first_seen >= ? AND is_open=1 AND screen_status='pass'
             AND score >= ?
           ORDER BY is_target DESC, score DESC""",
        (since_iso, min_score),
    ).fetchall()


def alertable_jobs(conn, min_score: float, role_matched_ids: list[int]) -> list[sqlite3.Row]:
    placeholder = ",".join("?" * len(role_matched_ids)) or "NULL"
    return conn.execute(
        f"""SELECT j.* FROM jobs j
            LEFT JOIN notifications n ON n.job_id=j.id AND n.kind='instant'
            WHERE n.id IS NULL AND j.is_open=1 AND j.screen_status='pass'
              AND j.score >= ?
              AND (j.is_target=1 OR j.id IN ({placeholder}))
            ORDER BY j.score DESC""",
        (min_score, *role_matched_ids),
    ).fetchall()


def record_notification(conn, job_id: int, kind: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO notifications (job_id, kind, sent_at) VALUES (?,?,?)",
        (job_id, kind, utcnow()),
    )


def start_run(conn, source: str) -> int:
    cur = conn.execute(
        "INSERT INTO runs (source, started_at) VALUES (?,?)", (source, utcnow())
    )
    return cur.lastrowid


def finish_run(conn, run_id: int, found: int, new: int, errors: int, note: str = "") -> None:
    conn.execute(
        "UPDATE runs SET finished_at=?, found=?, new_jobs=?, errors=?, note=? WHERE id=?",
        (utcnow(), found, new, errors, note, run_id),
    )


# The pipeline, in order. `interested` is a bookmark; everything from `applied`
# onward is a real application. Splitting live from finished lets the dashboard
# answer "what still needs me" without hardcoding the list in three places.
STAGES = ["interested", "applied", "heard_back", "interview",
          "offer", "rejected", "withdrawn"]
LIVE_STAGES = ["applied", "heard_back", "interview"]
CLOSED_STAGES = ["offer", "rejected", "withdrawn"]
STAGE_LABELS = {
    "interested": "Interested", "applied": "Applied",
    "heard_back": "Heard back", "interview": "Interviewing",
    "offer": "Offer", "rejected": "Rejected", "withdrawn": "Withdrawn",
}
# Anything that means a human replied — the numerator of the response rate.
RESPONDED = ("heard_back", "interview", "offer")


def set_application(conn, job_id: int, status: str, notes: str | None = None,
                    next_action: str | None = None,
                    next_action_date: str | None = None) -> None:
    now = utcnow()
    # Stamp the application date the first time it reaches a real stage, so
    # "applied 12 days ago" survives a later move to interview.
    applied = now if status != "interested" else None
    prev = conn.execute(
        "SELECT status FROM applications WHERE job_id=?", (job_id,)).fetchone()
    conn.execute(
        """INSERT INTO applications (job_id, status, applied_at, notes,
                                     next_action, next_action_date, updated_at)
           VALUES (?,?,?,?,?,?,?)
           ON CONFLICT(job_id) DO UPDATE SET status=excluded.status,
               applied_at=COALESCE(applications.applied_at, excluded.applied_at),
               notes=COALESCE(excluded.notes, applications.notes),
               next_action=excluded.next_action,
               next_action_date=excluded.next_action_date,
               updated_at=excluded.updated_at""",
        (job_id, status, applied, notes, next_action, next_action_date, now),
    )
    if prev is None or prev["status"] != status:
        conn.execute(
            """INSERT INTO application_events (job_id, status, note, created_at)
               VALUES (?,?,?,?)""",
            (job_id, status, notes, now),
        )


def applications(conn, stages: list[str] | None = None) -> list[sqlite3.Row]:
    """Tracked jobs joined to their posting, newest activity first."""
    where, args = "", []
    if stages:
        where = f"WHERE a.status IN ({','.join('?' * len(stages))})"
        args = list(stages)
    return conn.execute(
        f"""SELECT a.*, j.title, j.company, j.location, j.url, j.score,
                   j.best_variant, j.is_target, j.source,
                   (SELECT COUNT(*) FROM application_events e
                     WHERE e.job_id = a.job_id) AS event_count
            FROM applications a JOIN jobs j ON j.id = a.job_id
            {where}
            ORDER BY a.updated_at DESC""",
        args,
    ).fetchall()


def application_events(conn, job_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM application_events WHERE job_id=? ORDER BY id", (job_id,)
    ).fetchall()


def application_counts(conn) -> dict[str, int]:
    rows = conn.execute(
        "SELECT status, COUNT(*) c FROM applications GROUP BY status").fetchall()
    return {r["status"]: r["c"] for r in rows}


def delete_application(conn, job_id: int) -> None:
    conn.execute("DELETE FROM application_events WHERE job_id=?", (job_id,))
    conn.execute("DELETE FROM applications WHERE job_id=?", (job_id,))


# --------------------------------------------------------------------------
# settings + resume (the web UI edits these; CLI reads them)
# --------------------------------------------------------------------------

def get_setting(conn, key: str, default=None):
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default


def set_setting(conn, key: str, value) -> None:
    conn.execute(
        """INSERT INTO settings (key, value, updated_at) VALUES (?,?,?)
           ON CONFLICT(key) DO UPDATE SET value=excluded.value,
                                          updated_at=excluded.updated_at""",
        (key, json.dumps(value), utcnow()),
    )


def get_resume(conn):
    return conn.execute("SELECT * FROM resume WHERE id=1").fetchone()


def set_resume(conn, filename: str, text: str) -> None:
    conn.execute(
        """INSERT INTO resume (id, filename, text, uploaded_at) VALUES (1,?,?,?)
           ON CONFLICT(id) DO UPDATE SET filename=excluded.filename,
                                         text=excluded.text,
                                         uploaded_at=excluded.uploaded_at""",
        (filename, text, utcnow()),
    )


def get_resume_variants(conn) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM resume_variants ORDER BY position, id").fetchall()


def set_resume_variants(conn, variants: list[tuple[str, str]]) -> None:
    """Replace the stored variants wholesale — the .tex upload is the truth."""
    conn.execute("DELETE FROM resume_variants")
    now = utcnow()
    for i, (name, text) in enumerate(variants):
        conn.execute(
            """INSERT INTO resume_variants (name, text, position, updated_at)
               VALUES (?,?,?,?)
               ON CONFLICT(name) DO UPDATE SET text=excluded.text,
                   position=excluded.position, updated_at=excluded.updated_at""",
            (name, text, i, now),
        )


def recent_runs(conn, limit: int = 40):
    return conn.execute(
        "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()


def delete_company(conn, company_id: int) -> None:
    conn.execute("DELETE FROM companies WHERE id=?", (company_id,))


def set_company_flags(conn, company_id: int, is_target=None, active=None) -> None:
    if is_target is not None:
        conn.execute("UPDATE companies SET is_target=? WHERE id=?", (int(is_target), company_id))
        conn.execute(
            "UPDATE jobs SET is_target=? WHERE company_id=?", (int(is_target), company_id)
        )
    if active is not None:
        conn.execute("UPDATE companies SET active=? WHERE id=?", (int(active), company_id))
