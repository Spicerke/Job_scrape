"""Shared plumbing for job board sources."""
from __future__ import annotations

import hashlib
import html
import re
import time

import requests

USER_AGENT = "jobhunt/1.0 (personal job search tool; +contact in config)"
TIMEOUT = 25
_LAST_CALL: dict[str, float] = {}
MIN_INTERVAL = 0.6  # seconds between calls to the same host


class SourceError(Exception):
    pass


def _throttle(url: str) -> None:
    host = url.split("/")[2] if "//" in url else url
    last = _LAST_CALL.get(host, 0.0)
    wait = MIN_INTERVAL - (time.time() - last)
    if wait > 0:
        time.sleep(wait)
    _LAST_CALL[host] = time.time()


def get_json(url: str, params: dict | None = None, retries: int = 3):
    last_err = None
    for attempt in range(retries):
        _throttle(url)
        try:
            resp = requests.get(
                url, params=params, timeout=TIMEOUT,
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            )
            if resp.status_code == 404:
                raise SourceError("board not found (404) - check the slug")
            if resp.status_code == 429:
                time.sleep(3 * (attempt + 1))
                last_err = SourceError("rate limited (429)")
                continue
            resp.raise_for_status()
            return resp.json()
        except SourceError:
            raise
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            time.sleep(1.5 * (attempt + 1))
    raise SourceError(f"request failed after {retries} tries: {last_err}")


_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\r\f\v]+")
_NL_RE = re.compile(r"\n{3,}")


def strip_html(raw: str | None) -> str:
    """Convert an HTML job description to readable plain text."""
    if not raw:
        return ""
    text = html.unescape(raw)
    text = re.sub(r"<\s*br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</\s*(p|div|li|h\d|tr)\s*>", "\n", text, flags=re.I)
    text = re.sub(r"<\s*li[^>]*>", "- ", text, flags=re.I)
    text = _TAG_RE.sub(" ", text)
    text = html.unescape(text)
    text = _WS_RE.sub(" ", text)
    text = _NL_RE.sub("\n\n", text)
    return "\n".join(line.strip() for line in text.split("\n")).strip()


def content_hash(title: str, description: str) -> str:
    return hashlib.sha256(f"{title}\n{description}".encode()).hexdigest()[:32]


REMOTE_RE = re.compile(r"\b(remote|distributed|work from home|anywhere)\b", re.I)


def looks_remote(location: str | None, description: str = "") -> bool:
    return bool(REMOTE_RE.search(location or "")) or bool(
        REMOTE_RE.search(description[:1500])
    )
