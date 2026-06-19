"""Google Custom Search JSON API wrapper (Discovery).

Free tier: 100 queries/day (each result page = 1 query). Usage is tracked per
calendar day in the settings table so the dashboard can report remaining budget
(FR-5). In DRY_RUN no network call is made and no real quota is consumed — a small
canned fixture of ATS postings is returned instead so the whole Find-Jobs slice is
testable without burning the daily budget.
"""
from __future__ import annotations

import time
from datetime import datetime

import requests
from sqlmodel import Session

from ..config import CSE_DAILY_LIMIT, settings
from ..db import get_setting, set_setting
from ..logging_setup import log_event

CSE_ENDPOINT = "https://www.googleapis.com/customsearch/v1"
MAX_RETRIES = 3

# ATS / job-board domains the search engine is restricted to (Runbook §1.3).
ATS_DOMAINS = ["lever.co", "greenhouse.io", "ashbyhq.com", "wellfound.com", "instahyre.com"]


def _usage_key(day: str | None = None) -> str:
    day = day or datetime.now().date().isoformat()
    return f"cse_used_{day}"


def used_today(session: Session) -> int:
    raw = get_setting(session, _usage_key())
    try:
        return int(raw) if raw else 0
    except ValueError:
        return 0


def remaining_today(session: Session) -> int:
    return max(0, CSE_DAILY_LIMIT - used_today(session))


def _increment_usage(session: Session, n: int = 1) -> None:
    key = _usage_key()
    set_setting(session, key, str(used_today(session) + n))


# --- DRY_RUN fixture -------------------------------------------------------------
# A realistic mix of ATS postings, including one exact-URL duplicate and one
# (domain, role) duplicate, so dedupe is demonstrable end-to-end.
_FIXTURE_ITEMS = [
    {
        "title": "Software Engineer, Backend (SDE 1) - Acme",
        "link": "https://jobs.lever.co/acme/1111-sde-1-backend",
        "snippet": "Acme is hiring an SDE 1 in Gurgaon. Work on our payments platform.",
        "displayLink": "jobs.lever.co",
    },
    {
        "title": "Software Engineering Intern - Bolt",
        "link": "https://boards.greenhouse.io/bolt/jobs/2222",
        "snippet": "Bolt seeks a software engineering intern (remote, India).",
        "displayLink": "boards.greenhouse.io",
    },
    {
        "title": "Frontend Engineer (Fresher) - Cobalt",
        "link": "https://jobs.ashbyhq.com/cobalt/3333",
        "snippet": "Cobalt is building developer tools. Hiring freshers in Noida.",
        "displayLink": "jobs.ashbyhq.com",
    },
    # Exact-URL duplicate of the Acme posting (dedupe by source_url).
    {
        "title": "Software Engineer, Backend (SDE 1) - Acme",
        "link": "https://jobs.lever.co/acme/1111-sde-1-backend",
        "snippet": "Acme is hiring an SDE 1 in Gurgaon.",
        "displayLink": "jobs.lever.co",
    },
    # Same company+role as Acme via a different URL (dedupe by domain+role).
    {
        "title": "Software Engineer, Backend (SDE 1) - Acme",
        "link": "https://jobs.lever.co/acme/4444-sde-1-backend-2",
        "snippet": "Another listing for the same Acme SDE 1 backend role.",
        "displayLink": "jobs.lever.co",
    },
    {
        "title": "Backend Developer Intern - Delta Labs",
        "link": "https://jobs.lever.co/deltalabs/5555",
        "snippet": "Delta Labs internship, Delhi NCR, working on data infra.",
        "displayLink": "jobs.lever.co",
    },
]


def search(session: Session, query: str, max_results: int = 10) -> list[dict]:
    """Return raw CSE result items for a query (one page, up to 10 results).

    Honors the daily quota and DRY_RUN. Increments real usage only on real calls.
    """
    if settings.dry_run:
        log_event("discover", "cse", "dry_run", f"would query: {query[:120]}")
        return _FIXTURE_ITEMS

    if remaining_today(session) <= 0:
        log_event("discover", "cse", "quota_exhausted", "0 queries left today")
        return []

    params = {
        "key": settings.google_cse_api_key,
        "cx": settings.google_cse_id,
        "q": query,
        "num": min(max_results, 10),
    }

    last_err = ""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(CSE_ENDPOINT, params=params, timeout=20)
            if resp.status_code == 429:
                raise requests.HTTPError("429 rate limited")
            resp.raise_for_status()
            _increment_usage(session, 1)
            data = resp.json()
            items = data.get("items", []) or []
            log_event("discover", "cse", "ok", f"{len(items)} items")
            return items
        except (requests.RequestException, ValueError) as e:
            last_err = str(e)
            if attempt < MAX_RETRIES:
                time.sleep(2 ** attempt)  # exponential backoff

    log_event("discover", "cse", "error", last_err)
    return []
