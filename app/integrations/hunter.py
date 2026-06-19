"""Hunter.io email verifier wrapper (Contact verification).

Free tier: 50 verifications/month. Usage is tracked per calendar month in the
settings table. In DRY_RUN no network call is made and no real quota is consumed —
a deterministic verdict is derived from the local-part so the verify-before-send
gate is testable end-to-end (some addresses come back verified, some not).
"""
from __future__ import annotations

import time
from datetime import datetime

import requests
from sqlmodel import Session

from ..config import HUNTER_MONTHLY_LIMIT, settings
from ..db import get_setting, set_setting
from ..logging_setup import log_event

HUNTER_ENDPOINT = "https://api.hunter.io/v2/email-verifier"
HUNTER_DOMAIN_ENDPOINT = "https://api.hunter.io/v2/domain-search"
MAX_RETRIES = 3

# Position/department keywords that mark an HR / recruiting / talent contact.
_HR_KEYWORDS = (
    "recruit", "talent", "hr", "human resource", "people", "hiring",
    "staffing", "ta ", "acquisition",
)

# Hunter score (0-100) at/above which we treat an address as verified.
VERIFY_SCORE_THRESHOLD = 0.70
# Hunter statuses we accept as deliverable.
ACCEPTED_STATUSES = {"valid", "accept_all"}


def _api_keys() -> list[str]:
    """Configured Hunter keys, primary first. Each free account = 50 lookups/month."""
    keys = [settings.hunter_api_key, settings.hunter_api_key_backup]
    return [k for k in keys if k]


def _usage_key(idx: int = 0, month: str | None = None) -> str:
    month = month or datetime.now().strftime("%Y-%m")
    # Key 0 keeps the legacy setting name so existing usage carries over unchanged.
    return f"hunter_used_{month}" if idx == 0 else f"hunter_used_{month}_{idx}"


def _used(session: Session, idx: int) -> int:
    raw = get_setting(session, _usage_key(idx))
    try:
        return int(raw) if raw else 0
    except ValueError:
        return 0


def used_this_month(session: Session) -> int:
    """Total lookups used this month across all configured keys."""
    return sum(_used(session, i) for i in range(max(1, len(_api_keys()))))


def remaining_this_month(session: Session) -> int:
    """Total lookups remaining this month, summed across all keys."""
    n = len(_api_keys())
    if n == 0:
        return 0
    return sum(max(0, HUNTER_MONTHLY_LIMIT - _used(session, i)) for i in range(n))


def _active_key(session: Session):
    """First key (index, value) that still has quota this month, or (None, None)."""
    for i, key in enumerate(_api_keys()):
        if HUNTER_MONTHLY_LIMIT - _used(session, i) > 0:
            return i, key
    return None, None


def _increment_usage(session: Session, idx: int, n: int = 1) -> None:
    set_setting(session, _usage_key(idx), str(_used(session, idx) + n))


def _simulate(email: str) -> dict:
    """Deterministic DRY_RUN verdict based on the local part."""
    local = email.split("@", 1)[0].lower()
    if "." in local or local in {"careers", "jobs"}:
        # personal pattern (first.last) or a primary careers/jobs inbox -> verified
        return {"verified": True, "confidence": 0.85, "status": "valid", "simulated": True}
    if local in {"hr", "talent"}:
        # secondary role inboxes -> unknown / unverified (exercises the gate)
        return {"verified": False, "confidence": 0.30, "status": "unknown", "simulated": True}
    return {"verified": True, "confidence": 0.72, "status": "accept_all", "simulated": True}


def _is_hr(position: str | None, department: str | None) -> bool:
    blob = f"{position or ''} {department or ''}".lower()
    return any(k in blob for k in _HR_KEYWORDS)


def _simulate_hr(company: str, domain: str | None) -> dict:
    dom = domain or (company.lower().replace(" ", "") + ".com")
    contacts = [
        {"email": f"hr@{dom}", "first_name": "", "last_name": "", "position": "HR",
         "department": "hr", "confidence": 0.6, "domain": dom, "is_hr": True,
         "simulated": True},
        {"email": f"careers@{dom}", "first_name": "", "last_name": "", "position": "Recruiting",
         "department": "hr", "confidence": 0.55, "domain": dom, "is_hr": True,
         "simulated": True},
    ]
    return {"domain": dom, "headcount": "11-50", "contacts": contacts}


def find_hr_emails(session: Session, company: str, domain: str | None = None,
                   limit: int = 2) -> dict:
    """Find HR / recruiter emails + company headcount via Hunter domain-search.

    Pass `company` (Hunter resolves the domain) and/or an explicit `domain`.
    Returns {domain, headcount, contacts}: up to `limit` contacts, preferring
    HR/recruiting roles then highest confidence; `headcount` is Hunter's employee
    range (e.g. "11-50") used as a startup-size signal. One domain-search costs ONE
    Hunter request; returns empty contacts (logged) on quota/error — never crashes.
    """
    empty = {"domain": domain, "headcount": None, "contacts": []}
    if not (company or domain):
        return empty

    if settings.dry_run:
        log_event("contacts", company or domain, "hr_search_dry_run", "")
        res = _simulate_hr(company, domain)
        res["contacts"] = res["contacts"][:limit]
        return res

    key_idx, api_key = _active_key(session)
    if api_key is None:
        log_event("contacts", company or domain, "hr_search_quota_exhausted", "")
        return empty

    params = {"api_key": api_key, "limit": 10, "type": "personal"}
    if domain:
        params["domain"] = domain
    if company:
        params["company"] = company

    last_err = ""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(HUNTER_DOMAIN_ENDPOINT, params=params, timeout=20)
            if resp.status_code == 429:
                raise requests.HTTPError("429 rate limited")
            resp.raise_for_status()
            _increment_usage(session, key_idx)
            data = (resp.json() or {}).get("data", {})
            resolved_domain = data.get("domain") or domain
            headcount = data.get("headcount")
            people = data.get("emails", []) or []
            contacts = []
            for p in people:
                email = (p.get("value") or "").lower()
                if not email:
                    continue
                contacts.append({
                    "email": email,
                    "first_name": p.get("first_name") or "",
                    "last_name": p.get("last_name") or "",
                    "position": p.get("position") or "",
                    "department": p.get("department") or "",
                    "confidence": float(p.get("confidence") or 0) / 100.0,
                    "domain": resolved_domain,
                    "is_hr": _is_hr(p.get("position"), p.get("department")),
                })
            # HR/recruiting first, then by Hunter confidence.
            contacts.sort(key=lambda c: (not c["is_hr"], -c["confidence"]))
            log_event("contacts", company or resolved_domain, "hr_search",
                      f"{len(contacts)} email(s), {sum(c['is_hr'] for c in contacts)} HR, hc={headcount}")
            return {"domain": resolved_domain, "headcount": headcount, "contacts": contacts[:limit]}
        except (requests.RequestException, ValueError) as e:
            last_err = str(e)
            if attempt < MAX_RETRIES:
                time.sleep(2 ** attempt)

    log_event("contacts", company or domain, "hr_search_error", last_err)
    return empty


def verify(session: Session, email: str) -> dict:
    """Verify one email. Returns {verified, confidence, status}.

    On quota exhaustion or repeated failure, returns an unverified verdict so the
    caller can store the contact as unverified (FR-10) without crashing.
    """
    if not email:
        return {"verified": False, "confidence": 0.0, "status": "no_email"}

    if settings.dry_run:
        verdict = _simulate(email)
        log_event("contacts", email, "verify_dry_run", verdict["status"])
        return verdict

    key_idx, api_key = _active_key(session)
    if api_key is None:
        log_event("contacts", email, "verify_quota_exhausted", "")
        return {"verified": False, "confidence": 0.0, "status": "quota_exhausted"}

    params = {"email": email, "api_key": api_key}
    last_err = ""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(HUNTER_ENDPOINT, params=params, timeout=20)
            if resp.status_code == 429:
                raise requests.HTTPError("429 rate limited")
            resp.raise_for_status()
            _increment_usage(session, key_idx)
            data = (resp.json() or {}).get("data", {})
            status = (data.get("status") or "unknown").lower()
            score = float(data.get("score") or 0) / 100.0
            verified = status in ACCEPTED_STATUSES and score >= VERIFY_SCORE_THRESHOLD
            log_event("contacts", email, "verify", f"{status} score={score:.2f}")
            return {"verified": verified, "confidence": score, "status": status}
        except (requests.RequestException, ValueError) as e:
            last_err = str(e)
            if attempt < MAX_RETRIES:
                time.sleep(2 ** attempt)

    log_event("contacts", email, "verify_error", last_err)
    return {"verified": False, "confidence": 0.0, "status": "error"}
