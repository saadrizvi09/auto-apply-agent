"""Discovery stage: Custom Search (ATS-restricted) -> parse -> dedupe -> store.

Implements FR-1..FR-5. Builds a query from operator filters, restricts to ATS
domains, parses results into company/posting records, deduplicates within the batch
and against the DB (by source_url and by (domain, role_title)), and creates an
`applications` row with status `discovered` for each new posting.
"""
from __future__ import annotations

import re
from datetime import datetime
from urllib.parse import urlparse

from sqlmodel import select

from ..config import settings
from ..models import Application
from ..db import (
    create_application,
    create_company,
    create_contact,
    delete_application_cascade,
    find_company_by_domain_role,
    find_company_by_source_url,
    get_session,
    link_contact,
)
from ..integrations import cse, hunter, linkedin
from ..logic import dedupe_postings
from ..logging_setup import log_event

# Default LinkedIn role queries when the operator leaves the role box blank.
# Agent-focused: surfaces startups building AI agents/LLM products (many YC-backed),
# plus backend to catch general early-stage startup hiring.
DEFAULT_ROLE_QUERIES = ["AI Agents Engineer", "LLM Engineer", "Backend Engineer"]
# Default geos to search for REMOTE roles when the location box is blank — home
# market plus US/EU (the remote-from-India cold-outreach targets). 3 roles × 3 geos
# keeps the per-run LinkedIn request count bounded against rate-limiting.
DEFAULT_LOCATIONS = ["India", "United States", "Germany"]
# Only consider a job "8+ LPA" worthy if a parsed salary is at least this (lakhs).
DEFAULT_MIN_LPA = 8.0
# Bias toward small startups: drop companies whose Hunter headcount upper bound
# exceeds this many people (None on a company = unknown, kept).
DEFAULT_MAX_HEADCOUNT = 500
# Per-run safety caps so one Find Jobs click can't blow the Hunter free quota.
MAX_JOBS_PER_RUN = 40
MAX_HUNTER_LOOKUPS_PER_RUN = 12
# Per LinkedIn query, cap cards so multi-geo×multi-role stays bounded + gentle.
PER_QUERY_LIMIT = 15
# Hunter confidence at/above which an HR email is treated as verified.
HR_VERIFY_THRESHOLD = 0.70
_INTERN_MARKERS = ("intern", "internship", "trainee")
# Roles a new grad has ~no chance of an interview call for — skip them so the list
# only holds realistically-reachable openings. Word-boundary/substring matched.
_SENIOR_MARKERS = (
    "staff ", "principal", "director", "vp ", "vice president", "head of",
    "manager", "architect", "lead engineer", "engineering lead", "distinguished",
)

# Large enterprises, IT-services/consultancies and staffing agencies dominate
# LinkedIn results — exclude them so what remains skews to small startups.
# Single-word entries match on a word boundary (so "ola" ≠ "Motorola"); entries
# containing a space match as a substring.
_BIG_OR_STAFFING = {
    # Indian IT services / consultancies
    "infosys", "tcs", "tata consultancy", "wipro", "hcl", "hcltech", "tech mahindra",
    "cognizant", "capgemini", "accenture", "ltimindtree", "mindtree", "mphasis",
    "persistent systems", "hexaware", "genpact", "coforge", "birlasoft", "zensar",
    "cybage", "virtusa", "nagarro", "dxc", "mastech", "deloitte", "kpmg", "pwc",
    "pricewaterhouse", "ernst & young", "ey ", "infogain",
    # Big tech / large unicorns (high headcount)
    "google", "amazon", "microsoft", "meta", "facebook", "apple", "netflix",
    "adobe", "salesforce", "uber", "walmart", "flipkart", "paytm", "swiggy",
    "zomato", "byju", "ola", "phonepe", "oracle", "sap", "ibm", "intel", "nvidia",
    "qualcomm", "cisco", "vmware", "dell", "samsung", "sony", "wipro",
    # Staffing / recruiting agencies (multi-word → substring match)
    "staffing", "recruitment", "recruiters", "talent solutions", "outsourc",
    "manpower", "randstad", "teamlease", "quess", "adecco", "hays", "consultancy",
}


def _is_blocked_company(name: str | None) -> bool:
    """True if the company looks like a big enterprise / IT-services / staffing firm."""
    if not name:
        return False
    low = name.lower()
    tokens = set(re.split(r"[^a-z0-9&]+", low))
    for entry in _BIG_OR_STAFFING:
        if " " in entry:
            if entry in low:
                return True
        elif entry in tokens:
            return True
    return False


def _headcount_upper(headcount: str | None) -> int | None:
    """Upper bound of a Hunter headcount range ('11-50'->50, '5001+'->5001). None if unknown."""
    if not headcount:
        return None
    nums = [int(n) for n in re.findall(r"\d+", headcount)]
    return max(nums) if nums else None

# Indian-NCR location hints used to tag postings.
_CITY_HINTS = ["delhi", "gurgaon", "gurugram", "noida", "ncr", "bengaluru", "bangalore"]
_SEPARATORS = [" - ", " – ", " — ", " | ", " at ", ", "]


def build_query(filters: dict) -> str:
    """Compose an ATS-restricted Custom Search query from operator filters."""
    role = (filters.get("role") or "software engineer").strip()
    location = (filters.get("location") or "").strip()
    remote = bool(filters.get("remote"))
    keywords = (filters.get("keywords") or "").strip()

    sites = " OR ".join(f"site:{d}" for d in cse.ATS_DOMAINS)

    loc_terms: list[str] = []
    if location:
        loc_terms.append(location)
    if remote:
        loc_terms.append("remote")
    if not loc_terms:
        loc_terms = ["Delhi", "Gurgaon", "Noida", "remote"]
    loc = " OR ".join(loc_terms)

    parts = [f"({sites})", f'"{role}"', '(intern OR fresher OR "SDE 1")', f"({loc})"]
    if keywords:
        parts.append(keywords)
    return " ".join(parts)


def _slug_from_link(link: str) -> str | None:
    host = urlparse(link).netloc.lower()
    path_parts = [p for p in urlparse(link).path.split("/") if p]
    for d in cse.ATS_DOMAINS:
        if d in host and path_parts:
            return path_parts[0]
    return None


def _titleize(slug: str) -> str:
    return " ".join(w.capitalize() for w in slug.replace("-", " ").replace("_", " ").split())


def _split_title(title: str) -> tuple[str, str | None]:
    """Return (role_part, company_part) by splitting on the first known separator."""
    for sep in _SEPARATORS:
        if sep in title:
            left, right = title.split(sep, 1)
            return left.strip(), right.strip()
    return title.strip(), None


def _location_and_remote(text: str) -> tuple[str | None, int]:
    low = text.lower()
    remote = 1 if "remote" in low else 0
    found = [c for c in _CITY_HINTS if c in low]
    location = None
    if found:
        # Prefer a recognizable label.
        label = found[0]
        location = "Delhi NCR" if label in {"delhi", "gurgaon", "gurugram", "noida", "ncr"} else label.capitalize()
    elif remote:
        location = "Remote"
    return location, remote


def parse_item(item: dict) -> dict:
    """Turn one CSE result into a posting dict."""
    link = item.get("link", "") or ""
    title = (item.get("title", "") or "").strip()
    snippet = item.get("snippet", "") or ""

    slug = _slug_from_link(link)
    role_part, company_part = _split_title(title)

    company = _titleize(slug) if slug else (company_part or "Unknown")
    domain = f"{slug}.com" if slug else None
    role_title = role_part or title
    location, remote = _location_and_remote(f"{title} {snippet}")

    return {
        "name": company,
        "domain": domain,
        "source_url": link,
        "role_title": role_title,
        "location": location,
        "remote": remote,
    }


def _store(session, posting: dict) -> bool:
    """Create company + application if new. Returns True when created, False if dup."""
    if find_company_by_source_url(session, posting["source_url"]):
        return False
    if find_company_by_domain_role(session, posting.get("domain"), posting.get("role_title")):
        return False

    company = create_company(
        session,
        name=posting["name"],
        domain=posting.get("domain"),
        source_url=posting["source_url"],
        role_title=posting.get("role_title"),
        location=posting.get("location"),
        remote=int(posting.get("remote") or 0),
        discovered_at=datetime.now().isoformat(timespec="seconds"),
    )
    create_application(session, company_id=company.id, status="discovered")
    return True


def discover_ats(filters: dict) -> dict:
    """Legacy ATS discovery via Google Custom Search (kept as a fallback).

    Disabled in practice because the project's CSE key returns 403; superseded by
    `discover` (LinkedIn guest jobs + Hunter HR emails).
    """
    query = build_query(filters)
    summary = {
        "query": query,
        "new": 0,
        "duplicates": 0,
        "fetched": 0,
        "cse_remaining": None,
        "dry_run": settings.dry_run,
        "message": "",
    }

    with get_session() as session:
        if not settings.dry_run and cse.remaining_today(session) <= 0:
            summary["cse_remaining"] = 0
            summary["message"] = "Custom Search daily quota exhausted. Try again tomorrow."
            log_event("discover", "cse", "quota_exhausted", "")
            return summary

        items = cse.search(session, query)
        postings = [p for p in (parse_item(it) for it in items) if p.get("source_url")]
        summary["fetched"] = len(postings)

        deduped = dedupe_postings(postings)
        within_batch_dupes = len(postings) - len(deduped)

        for p in deduped:
            if _store(session, p):
                summary["new"] += 1
            else:
                summary["duplicates"] += 1
        summary["duplicates"] += within_batch_dupes

        summary["cse_remaining"] = cse.remaining_today(session)

    note = " (DRY RUN - fixture data, no quota used)" if settings.dry_run else ""
    summary["message"] = (
        f"Found {summary['new']} new posting(s), skipped {summary['duplicates']} duplicate(s).{note}"
    )
    log_event("discover", "batch", "ok", summary["message"])
    return summary


# --- LinkedIn discovery (current Find Jobs) --------------------------------------

def _role_queries(filters: dict) -> list[str]:
    role = (filters.get("role") or "").strip()
    keywords = (filters.get("keywords") or "").strip()
    bases = [role] if role else list(DEFAULT_ROLE_QUERIES)
    if keywords:
        bases = [f"{b} {keywords}".strip() for b in bases]
    return bases


def _job_to_posting(job: dict) -> dict:
    return {
        "name": job.get("company") or "Unknown",
        "domain": None,                       # resolved by Hunter from the name
        "source_url": job.get("url"),
        "role_title": job.get("title"),
        "location": job.get("location") or ("Remote" if job.get("remote") else None),
        "remote": int(job.get("remote") or 0),
        "salary": job.get("salary"),
        "salary_lpa": job.get("salary_lpa"),
        "urgent": int(job.get("urgent") or 0),
    }


def _store_linkedin(session, posting: dict):
    """Create company + discovered application if new. Returns the Company or None (dup)."""
    if find_company_by_source_url(session, posting["source_url"]):
        return None
    company = create_company(
        session,
        name=posting["name"],
        domain=posting.get("domain"),
        source_url=posting["source_url"],
        role_title=posting.get("role_title"),
        location=posting.get("location"),
        salary=posting.get("salary"),
        remote=int(posting.get("remote") or 0),
        urgent=int(posting.get("urgent") or 0),
        discovered_at=datetime.now().isoformat(timespec="seconds"),
    )
    create_application(session, company_id=company.id, status="discovered")
    return company


def _attach_hr_email(session, company) -> dict:
    """Resolve + attach an HR email + headcount for a company via Hunter.

    Returns {email, headcount}: `email` is the chosen HR address or None; `headcount`
    is Hunter's employee range (or None). Also records the headcount on the company.
    """
    res = hunter.find_hr_emails(session, company.name, domain=company.domain, limit=1)
    headcount = res.get("headcount")
    if headcount:
        company.headcount = headcount
        session.add(company)
    if res.get("domain") and not company.domain:
        company.domain = res["domain"]
        session.add(company)

    contacts = res.get("contacts") or []
    if not contacts:
        return {"email": None, "headcount": headcount}
    top = contacts[0]
    contact = create_contact(
        session,
        company_id=company.id,
        email=top["email"],
        source="hunter-hr",
        verified=1 if top["confidence"] >= HR_VERIFY_THRESHOLD else 0,
        confidence=top["confidence"],
        created_at=datetime.now().isoformat(timespec="seconds"),
    )
    app = session.exec(
        select(Application).where(Application.company_id == company.id)
    ).first()
    if app:
        link_contact(session, app, contact.id, status="email_found")
    return {"email": top["email"], "headcount": headcount}


def _app_id_for(session, company_id: int) -> int | None:
    app = session.exec(
        select(Application).where(Application.company_id == company_id)
    ).first()
    return app.id if app else None


def _locations(filters: dict) -> list[str]:
    loc = (filters.get("location") or "").strip()
    return [loc] if loc else list(DEFAULT_LOCATIONS)


def discover(filters: dict) -> dict:
    """Find Jobs: LinkedIn guest jobs across geos (remote, salary-filtered, startup-biased)
    + Hunter HR emails. Skips big enterprises/staffing and large-headcount companies."""
    remote = bool(filters.get("remote", True))
    locations = _locations(filters)
    min_lpa = float(filters.get("min_lpa") or DEFAULT_MIN_LPA)
    max_headcount = int(filters.get("max_headcount") or DEFAULT_MAX_HEADCOUNT)
    roles = _role_queries(filters)

    summary = {
        "roles": roles, "locations": locations, "remote": remote, "min_lpa": min_lpa,
        "max_headcount": max_headcount,
        "fetched": 0, "new": 0, "duplicates": 0,
        "below_salary": 0, "interns_skipped": 0, "senior_skipped": 0,
        "big_co_skipped": 0, "too_big_dropped": 0, "urgent_found": 0,
        "hr_emails": 0, "hunter_used": 0, "hunter_remaining": None,
        "dry_run": settings.dry_run, "message": "",
    }

    # 1) Scrape LinkedIn for each role × location, merge + dedupe by URL.
    raw: list[dict] = []
    for role in roles:
        for loc in locations:
            raw.extend(linkedin.search_jobs(role, location=loc, remote=remote, limit=PER_QUERY_LIMIT))
    seen_urls, jobs = set(), []
    for j in raw:
        u = j.get("url")
        if u and u not in seen_urls:
            seen_urls.add(u)
            jobs.append(j)
    summary["fetched"] = len(jobs)

    # 2) Filter: drop internships, big enterprises/staffing, below-threshold salaries.
    kept = []
    for j in jobs:
        title = (j.get("title") or "").lower()
        if any(m in title for m in _INTERN_MARKERS):
            summary["interns_skipped"] += 1
            continue
        if any(m in f"{title} " for m in _SENIOR_MARKERS):
            summary["senior_skipped"] += 1
            continue
        if _is_blocked_company(j.get("company")):
            summary["big_co_skipped"] += 1
            continue
        lpa = j.get("salary_lpa")
        if lpa is not None and lpa < min_lpa:
            summary["below_salary"] += 1
            continue
        kept.append(j)
    kept = kept[:MAX_JOBS_PER_RUN]

    # 3) Store new jobs, resolve HR emails (capped), drop large-headcount companies.
    with get_session() as session:
        hunter_budget = min(MAX_HUNTER_LOOKUPS_PER_RUN, hunter.remaining_this_month(session))
        for j in kept:
            posting = _job_to_posting(j)
            if not posting["source_url"]:
                continue
            company = _store_linkedin(session, posting)
            if company is None:
                summary["duplicates"] += 1
                continue
            summary["new"] += 1
            if int(posting.get("urgent") or 0) > 0:
                summary["urgent_found"] += 1
            if hunter_budget > 0:
                before = hunter.used_this_month(session)
                res = _attach_hr_email(session, company)
                spent = hunter.used_this_month(session) - before
                summary["hunter_used"] += spent
                hunter_budget -= spent
                # Startup bias: if Hunter says this company is large, drop the row.
                upper = _headcount_upper(res.get("headcount"))
                if upper is not None and upper > max_headcount:
                    delete_application_cascade(session, _app_id_for(session, company.id))
                    summary["new"] -= 1
                    summary["too_big_dropped"] += 1
                    continue
                if res.get("email"):
                    summary["hr_emails"] += 1
        summary["hunter_remaining"] = hunter.remaining_this_month(session)

    note = " (DRY RUN - fixture data, no network/quota used)" if settings.dry_run else ""
    if summary["fetched"] == 0 and not settings.dry_run:
        summary["message"] = (
            "LinkedIn returned no cards — it may be rate-limiting this IP. "
            "Wait a few minutes and retry, or narrow the role/location."
        )
    else:
        summary["message"] = (
            f"Found {summary['new']} new startup job(s) "
            f"({summary['urgent_found']} flagged urgent/hiring — applied first; "
            f"{summary['hr_emails']} with an HR email); skipped "
            f"{summary['big_co_skipped']} big-co, {summary['too_big_dropped']} too-large, "
            f"{summary['senior_skipped']} too-senior, {summary['duplicates']} dup, "
            f"{summary['interns_skipped']} intern, {summary['below_salary']} below {min_lpa:.0f} LPA. "
            f"Hunter left this month: {summary['hunter_remaining']}.{note}"
        )
    log_event("discover", "batch", "ok", summary["message"])
    return summary
