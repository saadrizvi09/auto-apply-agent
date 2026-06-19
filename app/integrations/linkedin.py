"""LinkedIn public *guest* jobs search (no login, lower ban risk).

LinkedIn exposes an unauthenticated endpoint that powers the "see more jobs"
infinite scroll on its public job-search pages. It returns plain HTML job cards
with title / company / location / posting URL / posted-date and, when LinkedIn has
it, a salary string. No account, no cookies — so this does not touch the operator's
logged-in profile and carries far less risk than scraping the authenticated site.

Limitations (be honest about them upstream):
  - No contact emails (LinkedIn hides them — emails come from Hunter separately).
  - Salary is present on only a minority of cards; we parse it when shown.
  - The endpoint is rate-limited by IP; on 429/403 we back off and return what we
    have rather than crashing.

In DRY_RUN a tiny deterministic fixture is returned so the pipeline is testable
offline with no network call.
"""
from __future__ import annotations

import time
from urllib.parse import quote_plus

import requests
from bs4 import BeautifulSoup

from ..config import settings
from ..logging_setup import log_event

GUEST_ENDPOINT = (
    "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
)
# A normal desktop browser UA — the guest endpoint rejects obvious bot UAs.
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
}
_PAGE_SIZE = 25          # LinkedIn returns 25 cards per "start" offset
_MAX_RETRIES = 3
# Work-type filter: 2 = Remote (LinkedIn's f_WT code).
_WT_REMOTE = "2"

# Urgency signals small companies put in the posting title. Tier 2 = explicitly
# urgent/immediate (apply first); tier 1 = a plain "hiring" shout (apply before
# unmarked roles). Matched case-insensitively as substrings. The user asked to
# prioritise "#urgent hiring" and "hiring" posts in the LinkedIn apply queue.
_URGENT_MARKERS = (
    "urgent", "urgently", "immediate", "immediately", "asap", "quick joiner",
    "quick joining", "immediate joiner", "immediate joining", "join immediately",
)
_HIRING_MARKERS = (
    "#hiring", "hiring now", "now hiring", "we are hiring", "we're hiring",
    "actively hiring", "hiring", "open position", "open role", "apply now",
)


def _urgency_score(*texts: str | None) -> int:
    """2 if the text shouts urgent/immediate hiring, 1 if it just says 'hiring', else 0."""
    blob = " ".join(t for t in texts if t).lower()
    if any(m in blob for m in _URGENT_MARKERS):
        return 2
    if any(m in blob for m in _HIRING_MARKERS):
        return 1
    return 0


def _fetch(keywords: str, location: str, remote: bool, start: int) -> str | None:
    params = {
        "keywords": keywords,
        "location": location or "India",
        "start": str(start),
    }
    if remote:
        params["f_WT"] = _WT_REMOTE
    qs = "&".join(f"{k}={quote_plus(v)}" for k, v in params.items())
    url = f"{GUEST_ENDPOINT}?{qs}"

    last_err = ""
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            resp = requests.get(url, headers=_HEADERS, timeout=20)
            if resp.status_code in (429, 403):
                last_err = f"{resp.status_code} throttled"
                time.sleep(2 ** attempt)
                continue
            if resp.status_code == 400:
                return ""  # past the last page — no more results
            resp.raise_for_status()
            return resp.text
        except requests.RequestException as e:
            last_err = str(e)
            time.sleep(2 ** attempt)
    log_event("discover", "linkedin", "fetch_error", last_err)
    return None


def _parse_salary_lpa(text: str | None) -> float | None:
    """Best-effort INR-LPA from a LinkedIn salary string. None if not parseable.

    Handles '₹8L - ₹12L', '₹8,00,000', '8-12 LPA'. Ignores non-INR ($, €, £).
    Returns the LOWER bound (conservative) in lakhs-per-annum.
    """
    if not text:
        return None
    t = text.replace(",", " ").lower()
    if any(sym in t for sym in ("$", "€", "£", "usd", "eur", "gbp")):
        return None
    import re

    # "8 lpa", "8-12 lpa", "8 lakh"
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:-\s*\d+(?:\.\d+)?\s*)?(?:l\b|lpa|lakh)", t)
    if m:
        return float(m.group(1))
    # bare large rupee number, e.g. "₹ 800000"
    m = re.search(r"₹\s*(\d{6,})", t)
    if m:
        return round(int(m.group(1)) / 100000.0, 1)
    return None


def _parse_cards(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    out: list[dict] = []
    for li in soup.select("li"):
        title_el = li.select_one("h3.base-search-card__title")
        company_el = li.select_one("h4.base-search-card__subtitle")
        loc_el = li.select_one(".job-search-card__location")
        link_el = li.select_one("a.base-card__full-link") or li.select_one("a[href*='/jobs/view/']")
        if not (title_el and link_el):
            continue
        salary_el = li.select_one(".job-search-card__salary-info")
        url = (link_el.get("href") or "").split("?")[0]
        loc_text = loc_el.get_text(strip=True) if loc_el else ""
        title_text = title_el.get_text(strip=True)
        company_text = company_el.get_text(strip=True) if company_el else "Unknown"
        out.append({
            "title": title_text,
            "company": company_text,
            "location": loc_text,
            "url": url,
            "remote": 1 if "remote" in loc_text.lower() else 0,
            "salary": salary_el.get_text(strip=True) if salary_el else None,
            "salary_lpa": _parse_salary_lpa(salary_el.get_text(strip=True) if salary_el else None),
            "urgent": _urgency_score(title_text, company_text),
        })
    return out


def _fixture(keywords: str, remote: bool) -> list[dict]:
    return [
        {"title": f"Urgent Hiring: {keywords} Engineer", "company": "Nimbus AI",
         "location": "Remote (India)", "url": "https://www.linkedin.com/jobs/view/dryrun-1",
         "remote": 1, "salary": "₹12L - ₹18L", "salary_lpa": 12.0, "urgent": 2},
        {"title": f"Senior {keywords}", "company": "Forge Labs", "location": "Bengaluru, India",
         "url": "https://www.linkedin.com/jobs/view/dryrun-2", "remote": 0,
         "salary": None, "salary_lpa": None, "urgent": 0},
    ]


def search_jobs(keywords: str, location: str = "India", remote: bool = True,
                limit: int = 25) -> list[dict]:
    """Return up to `limit` LinkedIn job cards for one keyword query.

    Each card: {title, company, location, url, remote, salary, salary_lpa}.
    Network failures degrade to whatever was collected (possibly empty).
    """
    if settings.dry_run:
        log_event("discover", "linkedin", "dry_run", f"would search '{keywords}' remote={remote}")
        return _fixture(keywords, remote)[:limit]

    collected: list[dict] = []
    seen: set[str] = set()
    start = 0
    while len(collected) < limit:
        html = _fetch(keywords, location, remote, start)
        if html is None:          # hard failure already logged
            break
        if not html.strip():      # no more pages
            break
        cards = _parse_cards(html)
        if not cards:
            break
        for c in cards:
            if c["url"] and c["url"] not in seen:
                seen.add(c["url"])
                # When the remote filter (f_WT=2) is applied, every card is remote
                # even though LinkedIn prints the country as the location.
                if remote:
                    c["remote"] = 1
                collected.append(c)
        start += _PAGE_SIZE
        time.sleep(1.5)           # be gentle with the endpoint
    log_event("discover", "linkedin", "ok", f"'{keywords}' remote={remote} -> {len(collected)} card(s)")
    return collected[:limit]
