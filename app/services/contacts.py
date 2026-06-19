"""Contact resolution stage (FR-6..FR-10, Technical-Spec §5.2).

Waterfall, stop at first success:
  1. Parse the posting page for a mailto:/apply email or an apply URL.
  2. Scrape /careers, /contact, /about on the company domain for published emails.
  3. Generate common email patterns against the domain.
Then verify the chosen email via Hunter and store verified/confidence.

If only an apply URL exists, it is stored and the posting is flagged "portal apply"
(no email send). In DRY_RUN the whole waterfall is simulated deterministically — no
company-site fetches, no Hunter quota used.
"""
from __future__ import annotations

import re
import time
from datetime import datetime
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from ..config import settings
from ..db import (
    applications_needing_contact,
    applications_needing_email,
    applications_unverified_pending,
    create_contact,
    get_session,
    link_contact,
)
from ..integrations import hunter
from ..logic import generate_email_patterns
from ..logging_setup import log_event

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_HEADERS = {"User-Agent": "Mozilla/5.0 (AutoApply contact resolver)"}
_SITE_PATHS = ["/careers", "/contact", "/about", ""]
# Hunter confidence at/above which an HR email is treated as verified.
HR_VERIFY_THRESHOLD = 0.70
_MAX_RETRIES = 3


# --- HTTP helper -----------------------------------------------------------------

def _fetch(url: str) -> str | None:
    last_err = ""
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            resp = requests.get(url, headers=_HEADERS, timeout=15)
            if resp.status_code >= 400:
                return None
            return resp.text
        except requests.RequestException as e:
            last_err = str(e)
            if attempt < _MAX_RETRIES:
                time.sleep(2 ** attempt)
    log_event("contacts", url, "fetch_error", last_err)
    return None


def _emails_on_domain(text: str, domain: str | None) -> list[str]:
    found = _EMAIL_RE.findall(text or "")
    seen, out = set(), []
    for e in found:
        e = e.lower()
        if e in seen:
            continue
        if domain and not e.endswith("@" + domain.lower()):
            continue
        seen.add(e)
        out.append(e)
    return out


# --- Waterfall steps (real path) -------------------------------------------------

def _from_posting(source_url: str, domain: str | None) -> tuple[str | None, str | None]:
    """Return (email, apply_url) extracted from the posting page."""
    if not source_url:
        return None, None
    html = _fetch(source_url)
    if not html:
        return None, None
    soup = BeautifulSoup(html, "html.parser")

    # mailto: links first
    for a in soup.select("a[href^=mailto]"):
        addr = a.get("href", "")[len("mailto:"):].split("?")[0].strip().lower()
        if addr:
            return addr, None

    # any email in the page text on the company domain
    emails = _emails_on_domain(soup.get_text(" "), domain)
    if emails:
        return emails[0], None

    # an apply link (portal apply) — keep the posting URL itself as the apply route
    for a in soup.find_all("a"):
        label = (a.get_text() or "").strip().lower()
        if "apply" in label and a.get("href"):
            return None, a["href"]
    return None, source_url  # posting exists but no email -> treat as portal apply


def _from_site(domain: str | None) -> str | None:
    if not domain:
        return None
    for path in _SITE_PATHS:
        html = _fetch(f"https://{domain}{path}")
        if not html:
            continue
        emails = _emails_on_domain(BeautifulSoup(html, "html.parser").get_text(" "), domain)
        if emails:
            return emails[0]
    return None


def _from_pattern(domain: str | None) -> str | None:
    # No person name in cold-apply discovery -> role inboxes (careers@, jobs@, ...).
    candidates = generate_email_patterns(None, None, domain or "")
    return candidates[0] if candidates else None


# --- DRY_RUN simulator -----------------------------------------------------------

def _simulate_waterfall(domain: str | None, source_url: str) -> dict:
    """Deterministic resolution for fixture companies so the slice is testable.

    Produces a spread of outcomes: posting-email, scraped-email, an unverified
    pattern email, and a portal-apply-only posting.
    """
    d = (domain or "").lower()
    mapping = {
        "acme.com": {"email": "careers@acme.com", "apply_url": None, "source": "posting"},
        "bolt.com": {"email": "jobs@bolt.com", "apply_url": None, "source": "scraped"},
        "cobalt.com": {"email": "hr@cobalt.com", "apply_url": None, "source": "pattern"},
        "deltalabs.com": {"email": None, "apply_url": source_url, "source": "posting"},
    }
    if d in mapping:
        return mapping[d]
    # Unknown domains fall back to a generated pattern address.
    return {"email": _from_pattern(domain), "apply_url": None, "source": "pattern"}


# --- Orchestration ---------------------------------------------------------------

def _resolve_one(domain: str | None, source_url: str) -> dict:
    """Run the waterfall (or simulate it) -> {email, apply_url, source}."""
    if settings.dry_run:
        return _simulate_waterfall(domain, source_url)

    email, apply_url = _from_posting(source_url, domain)
    if email:
        return {"email": email, "apply_url": None, "source": "posting"}

    scraped = _from_site(domain)
    if scraped:
        return {"email": scraped, "apply_url": None, "source": "scraped"}

    pattern = _from_pattern(domain)
    if pattern:
        return {"email": pattern, "apply_url": None, "source": "pattern"}

    # No email anywhere; if the posting gave an apply URL, it's portal apply.
    return {"email": None, "apply_url": apply_url, "source": "posting"}


def _hunter_resolve(session, application, company, contact, summary) -> bool:
    """Resolve an HR email for a no-domain (LinkedIn) company via Hunter by name.

    Updates the existing email-less contact, or creates one. Records company
    headcount/domain. Returns True if an email was set. Quota-bounded.
    """
    if hunter.remaining_this_month(session) <= 0:
        summary["quota_skipped"] += 1
        return False
    hres = hunter.find_hr_emails(session, company.name, domain=None, limit=1)
    if hres.get("headcount"):
        company.headcount = hres["headcount"]
    if hres.get("domain"):
        company.domain = hres["domain"]
    session.add(company)

    cts = hres.get("contacts") or []
    if not cts:
        summary["no_email"] += 1
        log_event("contacts", company.name, "no_email", "Hunter found none")
        return False

    top = cts[0]
    verified = 1 if top["confidence"] >= HR_VERIFY_THRESHOLD else 0
    conf = round(top["confidence"], 3)
    if contact is not None:
        # Replace the bogus email-less / portal-apply contact in place.
        contact.email = top["email"]
        contact.apply_url = None
        contact.source = "hunter-hr"
        contact.verified = verified
        contact.confidence = conf
        session.add(contact)
        application.status = "email_found"
        session.add(application)
    else:
        new_contact = create_contact(
            session, company_id=company.id, email=top["email"], source="hunter-hr",
            verified=verified, confidence=conf,
            created_at=datetime.now().isoformat(timespec="seconds"),
        )
        link_contact(session, application, new_contact.id, "email_found")
    summary["emails_found"] += 1
    summary["verified" if verified else "unverified"] += 1
    log_event("contacts", top["email"], "resolved", f"source=hunter-hr verified={verified}")
    return True


def resolve_contacts() -> dict:
    """Resolve+verify a contact for every application that lacks a usable email."""
    summary = {
        "processed": 0,
        "emails_found": 0,
        "verified": 0,
        "unverified": 0,
        "portal_apply": 0,
        "no_email": 0,
        "quota_skipped": 0,
        "dry_run": settings.dry_run,
        "hunter_remaining": None,
        "message": "",
    }

    with get_session() as session:
        # Pass A — LinkedIn rows (no company domain) missing an email: resolve via
        # Hunter by company name. Covers BOTH never-contacted rows and the bogus
        # email-less "portal-apply" contacts created before this fix. Quota-bounded.
        if not settings.dry_run:
            for application, company, contact in applications_needing_email(session):
                if company.domain:
                    continue  # has a domain -> Pass B waterfall handles it
                summary["processed"] += 1
                _hunter_resolve(session, application, company, contact, summary)

        # Pass B — rows with no contact yet, resolved by the page/site/pattern
        # waterfall (needs a domain; in DRY_RUN it's simulated for all rows).
        pending = applications_needing_contact(session)
        for application, company in pending:
            if not settings.dry_run and not company.domain:
                continue  # handled in Pass A
            summary["processed"] += 1
            res = _resolve_one(company.domain, company.source_url or "")
            email = res.get("email")
            apply_url = res.get("apply_url")

            verified, confidence = 0, None
            new_status = None

            if email:
                verdict = hunter.verify(session, email)
                verified = 1 if verdict["verified"] else 0
                confidence = round(verdict["confidence"], 3)
                summary["emails_found"] += 1
                summary["verified" if verified else "unverified"] += 1
                # Posting moves forward to drafting regardless of verified flag;
                # the verify gate is enforced at send time (FR-10).
                new_status = "email_found"
            elif apply_url:
                summary["portal_apply"] += 1
                # No email -> stays 'discovered'; sender will skip ("portal apply").

            contact = create_contact(
                session,
                company_id=company.id,
                email=email,
                apply_url=apply_url,
                source=res.get("source"),
                verified=verified,
                confidence=confidence,
                created_at=datetime.now().isoformat(timespec="seconds"),
            )
            link_contact(session, application, contact.id, new_status)
            log_event(
                "contacts",
                email or apply_url or company.name,
                "resolved",
                f"source={res.get('source')} verified={verified}",
            )

        # Pass 2: verify imported emails that already have a contact but were never
        # run through verification (verified=0, confidence NULL). Idempotent — once
        # verified/attempted, confidence is set and they're skipped on re-run.
        for application, company, contact in applications_unverified_pending(session):
            verdict = hunter.verify(session, contact.email)
            contact.verified = 1 if verdict["verified"] else 0
            contact.confidence = round(verdict["confidence"], 3)
            session.add(contact)
            summary["processed"] += 1
            summary["emails_found"] += 1
            summary["verified" if contact.verified else "unverified"] += 1
            log_event(
                "contacts", contact.email, "verified_import", f"verified={contact.verified}"
            )

        summary["hunter_remaining"] = hunter.remaining_this_month(session)

    note = " (DRY RUN - simulated, no Hunter quota used)" if settings.dry_run else ""
    quota_msg = ""
    if summary["quota_skipped"]:
        quota_msg = (
            f" Hunter quota ran out — {summary['quota_skipped']} still need emails "
            f"(resets next month, or add Hunter credits)."
        )
    summary["message"] = (
        f"Resolved {summary['processed']}: {summary['verified']} verified, "
        f"{summary['unverified']} unverified, {summary['no_email']} no-email-found, "
        f"{summary['portal_apply']} portal-apply. Hunter left: {summary['hunter_remaining']}."
        f"{quota_msg}{note}"
    )
    log_event("contacts", "batch", "ok", summary["message"])
    return summary
