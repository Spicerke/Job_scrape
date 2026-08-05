"""Adapters for the public JSON job-board APIs of the major ATS vendors.

Each adapter takes a company board slug and yields normalized job dicts.
All of these endpoints are the same ones the companies' own careers pages
call from the browser, so no HTML scraping is involved.
"""
from __future__ import annotations

from .base import SourceError, content_hash, get_json, looks_remote, strip_html


def _job(source, sid, company, title, url, **kw):
    desc = kw.get("description", "") or ""
    return {
        "source": source,
        "source_job_id": f"{company}:{sid}",
        "company": company,
        "title": (title or "").strip(),
        "url": url,
        "description": desc,
        "location": kw.get("location"),
        "department": kw.get("department"),
        "compensation": kw.get("compensation"),
        "posted_at": kw.get("posted_at"),
        "remote": looks_remote(kw.get("location"), desc),
        "content_hash": content_hash(title or "", desc),
    }


# ---------------------------------------------------------------- greenhouse
def greenhouse(slug: str, company_name: str):
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
    data = get_json(url, params={"content": "true"})
    for j in data.get("jobs", []):
        offices = j.get("offices") or []
        loc = (j.get("location") or {}).get("name") or ", ".join(
            o.get("name", "") for o in offices
        )
        yield _job(
            "greenhouse", j["id"], company_name, j.get("title"),
            j.get("absolute_url", ""),
            description=strip_html(j.get("content")),
            location=loc,
            department=", ".join(
                d.get("name", "") for d in (j.get("departments") or [])
            ),
            posted_at=(j.get("first_published") or j.get("updated_at")),
        )


# --------------------------------------------------------------------- lever
def lever(slug: str, company_name: str):
    url = f"https://api.lever.co/v0/postings/{slug}"
    data = get_json(url, params={"mode": "json"})
    if not isinstance(data, list):
        raise SourceError("unexpected Lever payload")
    for j in data:
        cats = j.get("categories") or {}
        body = strip_html(j.get("descriptionPlain") or j.get("description"))
        extra = "\n".join(
            strip_html(l.get("text", "") + " " + str(l.get("content", "")))
            for l in (j.get("lists") or [])
        )
        posted = j.get("createdAt")
        yield _job(
            "lever", j.get("id"), company_name, j.get("text"),
            j.get("hostedUrl", ""),
            description=(body + "\n" + extra).strip(),
            location=cats.get("location"),
            department=cats.get("team") or cats.get("department"),
            compensation=cats.get("commitment"),
            posted_at=(str(posted) if posted else None),
        )


# --------------------------------------------------------------------- ashby
def ashby(slug: str, company_name: str):
    url = "https://api.ashbyhq.com/posting-api/job-board/" + slug
    data = get_json(url, params={"includeCompensation": "true"})
    for j in data.get("jobs", []):
        comp = j.get("compensation") or {}
        summary = comp.get("compensationTierSummary") if isinstance(comp, dict) else None
        yield _job(
            "ashby", j.get("id"), company_name, j.get("title"),
            j.get("jobUrl") or j.get("applyUrl", ""),
            description=strip_html(j.get("descriptionHtml") or j.get("descriptionPlain")),
            location=j.get("location"),
            department=j.get("department") or j.get("team"),
            compensation=summary,
            posted_at=j.get("publishedAt"),
        )


# ----------------------------------------------------------- smartrecruiters
def smartrecruiters(slug: str, company_name: str, max_jobs: int = 200):
    listing = f"https://api.smartrecruiters.com/v1/companies/{slug}/postings"
    offset, seen = 0, 0
    while seen < max_jobs:
        data = get_json(listing, params={"limit": 100, "offset": offset})
        postings = data.get("content", [])
        if not postings:
            break
        for p in postings:
            seen += 1
            loc = p.get("location") or {}
            loc_str = ", ".join(
                x for x in [loc.get("city"), loc.get("region"), loc.get("country")] if x
            )
            detail_txt = ""
            try:
                detail = get_json(f"{listing}/{p['id']}")
                sections = (detail.get("jobAd", {}).get("sections", {}) or {})
                detail_txt = "\n".join(
                    strip_html((sections.get(k) or {}).get("text", ""))
                    for k in ("companyDescription", "jobDescription", "qualifications")
                )
            except Exception:  # noqa: BLE001 - detail is best-effort
                pass
            yield _job(
                "smartrecruiters", p["id"], company_name, p.get("name"),
                f"https://jobs.smartrecruiters.com/{slug}/{p['id']}",
                description=detail_txt,
                location=loc_str,
                department=(p.get("department") or {}).get("label"),
                posted_at=p.get("releasedDate"),
            )
        offset += len(postings)
        if offset >= data.get("totalFound", 0):
            break


# ------------------------------------------------------------------ workable
def workable(slug: str, company_name: str):
    url = f"https://apply.workable.com/api/v1/widget/accounts/{slug}"
    data = get_json(url, params={"details": "true"})
    for j in data.get("jobs", []):
        loc = ", ".join(
            x for x in [j.get("city"), j.get("state"), j.get("country")] if x
        )
        yield _job(
            "workable", j.get("shortcode") or j.get("id"), company_name,
            j.get("title"), j.get("url") or j.get("application_url", ""),
            description=strip_html(
                (j.get("description") or "") + " " + (j.get("requirements") or "")
            ),
            location=loc,
            department=j.get("department"),
            posted_at=j.get("published_on"),
        )


ADAPTERS = {
    "greenhouse": greenhouse,
    "lever": lever,
    "ashby": ashby,
    "smartrecruiters": smartrecruiters,
    "workable": workable,
}
