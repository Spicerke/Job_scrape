"""Configuration lives in the database, not in config.yaml.

config.yaml is only a seed: on first `init` it's copied into the settings
table, and after that the database is the single source of truth so the web
UI and the cron jobs can never disagree. `jobhunt config export` writes the
live config back out to YAML if you want to version it.
"""
from __future__ import annotations

import copy
from pathlib import Path

import yaml

from . import db

CONFIG_KEY = "config"

DEFAULTS: dict = {
    "profile": {
        "name": "",
        "graduation": "",
        "degree_level": "bachelors",  # bachelors | masters | phd
    },
    "search": {
        "role_patterns": [],
        "min_score": 45,
        "instant_min_score": 60,
        "keyword_saturation": 8,
        "weights": {"keyword": 0.40, "resume": 0.35, "title": 0.25},
    },
    "keywords": {"skills": {}, "bonus": {}},
    "screening": {
        "max_years_experience": 2,
        "reject_title_terms": [],
        "reject_body_terms": [],
        "require_any_terms": [],
        "locations": [],
        "allow_remote": True,
        "min_description_chars": 200,
        "min_requirement_coverage": 0.35,
        "enforce_degree_level": True,
        "enforce_graduation_year": False,
    },
    "targets": {"roles": []},
    "email": {
        "smtp_host": "smtp.gmail.com",
        "smtp_port": 587,
        "username": "",
        "from_addr": "",
        "to_addr": "",
        "password_env": "JOBHUNT_SMTP_PASSWORD",
        "digest_day": "friday",
        "digest_time": "17:30",
        "lookback_days": 7,
        "max_jobs": 40,
    },
    "schedule": {
        # 24h local times. Two pulls a day catches postings that go up in the
        # morning and get filled before the next one.
        "scrape_times": ["09:00", "21:00"],
        "enabled": True,
    },
}


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load(conn) -> dict:
    """Live config: defaults <- database."""
    return _merge(DEFAULTS, db.get_setting(conn, CONFIG_KEY, {}) or {})


def save(conn, cfg: dict) -> None:
    db.set_setting(conn, CONFIG_KEY, cfg)
    conn.commit()


def seed_from_yaml(conn, path: str | Path, force: bool = False) -> bool:
    """Copy config.yaml into the database. Returns True if it wrote."""
    existing = db.get_setting(conn, CONFIG_KEY)
    if existing and not force:
        return False
    path = Path(path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
    save(conn, _merge(DEFAULTS, raw or {}))
    return True


def export_yaml(conn, path: str | Path) -> None:
    Path(path).write_text(
        yaml.safe_dump(load(conn), sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
