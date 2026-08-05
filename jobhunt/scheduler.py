"""A small always-on scheduler, for when cron isn't available.

On a laptop or a normal VM, cron is the right answer and this is unnecessary.
This exists for containers — ECS/Fargate, Fly, a bare `docker run` — where
there is no cron daemon and you want one process that keeps itself on schedule.

Last-run timestamps are persisted in the settings table, so restarting the
container doesn't cause a duplicate digest or skip a day.
"""
from __future__ import annotations

import time
from datetime import datetime

from . import config_store, db, tasks

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday",
            "saturday", "sunday"]
CHECK_INTERVAL = 300  # seconds


def _due(conn, key: str, today: str) -> bool:
    return db.get_setting(conn, f"last_run:{key}") != today


def _mark(conn, key: str, today: str) -> None:
    db.set_setting(conn, f"last_run:{key}", today)
    conn.commit()


def parse_time(value, default: tuple[int, int]) -> tuple[int, int]:
    """'21:30' or 21 -> (21, 30). Falls back to `default` on anything else."""
    if isinstance(value, (int, float)):
        return int(value), 0
    try:
        hh, _, mm = str(value).partition(":")
        h, m = int(hh), int(mm or 0)
    except (TypeError, ValueError):
        return default
    if not (0 <= h < 24 and 0 <= m < 60):
        return default
    return h, m


def scrape_slots(sched: dict) -> list[tuple[int, int]]:
    """Configured scrape times, oldest first.

    Accepts the current `scrape_times: ["09:00", "21:00"]` and still honours a
    legacy single `scrape_hour` so an existing database keeps its schedule.
    """
    raw = sched.get("scrape_times")
    if not raw:
        legacy = sched.get("scrape_hour")
        raw = [legacy] if legacy is not None else ["09:00", "21:00"]
    if isinstance(raw, (str, int, float)):
        raw = [raw]
    return sorted({parse_time(v, (9, 0)) for v in raw})


def tick(conn, now: datetime | None = None, log=print) -> list[str]:
    """Run whatever is due right now. Returns the names of tasks fired."""
    now = now or datetime.now()
    cfg = config_store.load(conn)
    sched = cfg.get("schedule", {}) or {}
    if not sched.get("enabled", True):
        return []

    today = now.strftime("%Y-%m-%d")
    fired = []

    # Every slot whose time has passed and that hasn't run yet today. If the
    # process was down all day, several are due at once — run the cycle once
    # and retire all of them rather than scraping back-to-back.
    due = [s for s in scrape_slots(sched)
           if (now.hour, now.minute) >= s and _due(conn, f"scrape@{s[0]:02d}:{s[1]:02d}", today)]
    if due:
        label = ", ".join(f"{h:02d}:{m:02d}" for h, m in due)
        log(f"[{now:%Y-%m-%d %H:%M}] scrape cycle starting (slot {label})")
        tasks.daily_cycle(conn, log=log)
        for h, m in due:
            _mark(conn, f"scrape@{h:02d}:{m:02d}", today)
        fired.append("scrape")

    ecfg = cfg.get("email", {}) or {}
    digest_day = str(ecfg.get("digest_day", "friday")).lower()
    digest_at = parse_time(ecfg.get("digest_time", ecfg.get("digest_hour")), (17, 30))
    if (WEEKDAYS[now.weekday()] == digest_day
            and (now.hour, now.minute) >= digest_at
            and _due(conn, "digest", today)):
        log(f"[{now:%Y-%m-%d %H:%M}] weekly digest starting")
        try:
            tasks.digest(conn, log=log)
        except Exception as exc:  # noqa: BLE001
            log(f"digest failed: {exc}")
        _mark(conn, "digest", today)
        fired.append("digest")

    return fired


def run_forever(db_path: str, log=print) -> None:
    log(f"scheduler up, checking every {CHECK_INTERVAL // 60} min "
        f"(db: {db_path})")
    while True:
        try:
            conn = db.connect(db_path)
            try:
                tick(conn, log=log)
            finally:
                conn.close()
        except Exception as exc:  # noqa: BLE001 - never let the loop die
            log(f"scheduler error: {exc}")
        time.sleep(CHECK_INTERVAL)
