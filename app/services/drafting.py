"""Drafting stage (FR-11..FR-14, Technical-Spec §5.3 / §7.1).

One tailored email per posting via Groq, referencing the exact role and one
company-specific detail. Enforces 90-130 words, plain text, no links. Rejects and
regenerates a body identical to a prior one (hash check). Stores the draft with
status 'drafted' — never sends. In DRY_RUN a deterministic in-range body is built
locally so no Groq quota is used.
"""
from __future__ import annotations

import hashlib
import re

from ..config import settings
from ..db import (
    applications_for_drafting,
    existing_bodies,
    get_session,
    update_draft,
)
from ..profile import load_profile
from ..integrations import groq_client
from ..logging_setup import log_event
from ..prompts import (
    DRAFT_SYSTEM,
    DRAFT_SYSTEM_INTL,
    draft_user_prompt,
    draft_user_prompt_intl,
    subject_for,
)

WORD_MIN, WORD_MAX = 90, 130

# --- International detection ------------------------------------------------------
# An international row gets the remote-from-India value-prop pitch instead of the
# standard India application. Decided from the company's location string.

_INDIA_MARKERS = (
    "india", "delhi", "ncr", "gurgaon", "gurugram", "noida", "bangalore",
    "bengaluru", "mumbai", "hyderabad", "pune", "chennai", "kolkata",
    "ahmedabad", "remote-india", "remote india", "remote (india",
)
_INTL_MARKERS = (
    "united states", "u.s.", "america", "us-based", "san francisco", "bay area",
    "new york", "nyc", "seattle", "austin", "boston", "los angeles", "palo alto",
    "mountain view", "germany", "german", "berlin", "munich", "münchen", "hamburg",
    "europe", "united kingdom", "london", "ireland", "dublin", "canada", "toronto",
    "amsterdam", "netherlands", "paris", "france", "singapore", "australia",
    "remote (us", "remote - us", "remote us", "remote (eu", "remote - eu",
    "remote eu", "remote (global", "global", "remote (worldwide", "worldwide",
)
_INTL_TOKENS = {"us", "usa", "uk", "eu"}


def _is_international(company) -> bool:
    loc = (company.location or "").strip().lower()
    if not loc:
        return False
    if any(m in loc for m in _INDIA_MARKERS):
        return False
    if any(m in loc for m in _INTL_MARKERS):
        return True
    tokens = set(re.split(r"[\s,/()\-]+", loc))
    return bool(tokens & _INTL_TOKENS)


def count_words(text: str) -> int:
    return len(text.split())


def _hash(body: str) -> str:
    return hashlib.md5(" ".join(body.split()).lower().encode()).hexdigest()


def _detail(company) -> str:
    """One concrete company-specific detail for the prompt."""
    bits = []
    if company.role_title:
        bits.append(f"the {company.role_title} opening")
    if company.location:
        bits.append(f"your team in {company.location}")
    if company.remote:
        bits.append("remote-friendly engineering")
    return ", ".join(bits) if bits else "your engineering team"


# --- DRY_RUN local draft ---------------------------------------------------------

_FILLERS = [
    "I care about writing clean, well-tested code and shipping features that hold up "
    "in production.",
    "I learn quickly, ask good questions, and enjoy collaborating closely with a team.",
    "I have hands-on experience taking projects from an idea to a working, deployed "
    "service.",
    "I am comfortable across the stack and happy to go deep wherever the team needs help.",
]


def _dry_draft(role: str, company: str, detail: str, signature: str, variant: int = 0) -> str:
    body = (
        f"Dear {company} Hiring Team,\n"
        f"I am writing to apply for the {role} role at {company}. "
        f"I was drawn to {detail}, and the chance to contribute there genuinely "
        f"excites me. "
    )
    # Pad with deterministic filler sentences until we clear the lower bound.
    i = variant
    while count_words(body + signature) < WORD_MIN + 4:
        body += _FILLERS[i % len(_FILLERS)] + " "
        i += 1
    body += (
        "I have attached my CV and would welcome a short conversation about how I can "
        "help. Thank you for your time and consideration.\n"
    )
    full = body + signature
    # Trim from the filler region if we overshoot the upper bound.
    words = full.split()
    if len(words) > WORD_MAX:
        words = words[:WORD_MAX]
        full = " ".join(words)
    return full


_INTL_FILLERS = [
    "I work across Python, FastAPI, TypeScript and Next.js, with hands-on LLM, "
    "agent and RAG experience.",
    "I have taken projects from idea to a deployed service and like owning features "
    "end to end.",
    "I move fast, communicate clearly in writing, and am comfortable in an async "
    "remote team.",
    "Hiring me remotely gives you senior-quality engineering on a startup budget.",
]


def _dry_draft_intl(role: str, company: str, detail: str, signature: str, variant: int = 0) -> str:
    body = (
        f"Hi {company} team,\n"
        f"I am reaching out about the remote {role} role - {detail} really caught "
        f"my attention. I am an AI/backend engineer based in India who ships "
        f"senior-quality work, and I can join remotely at a fraction of the cost of "
        f"a comparable US or EU hire, which is a real edge for a lean team. "
    )
    i = variant
    while count_words(body + signature) < WORD_MIN + 4:
        body += _INTL_FILLERS[i % len(_INTL_FILLERS)] + " "
        i += 1
    body += (
        "I am glad to overlap with your timezone, and I have attached my CV. "
        "I would love a short call to explore the fit.\n"
    )
    full = body + signature
    words = full.split()
    if len(words) > WORD_MAX:
        full = " ".join(words[:WORD_MAX])
    return full


def _enforce_range(body: str) -> str:
    """Final guard for real Groq output: cap at WORD_MAX words."""
    words = body.split()
    if len(words) > WORD_MAX:
        return " ".join(words[:WORD_MAX])
    return body


def _generate_body(
    role, company_name, detail, signature, seen: set[str],
    international: bool = False, location: str | None = None,
) -> tuple[str, bool]:
    """Return (body, ok). ok=False if we couldn't get an in-range, non-duplicate body.

    International rows use the remote-from-India value-prop pitch.
    """
    if settings.dry_run:
        maker = _dry_draft_intl if international else _dry_draft
        fillers = _INTL_FILLERS if international else _FILLERS
        for variant in range(len(fillers) + 1):
            body = maker(role, company_name, detail, signature, variant)
            if WORD_MIN <= count_words(body) <= WORD_MAX and _hash(body) not in seen:
                return body, True
        return body, (WORD_MIN <= count_words(body) <= WORD_MAX)

    if international:
        system = DRAFT_SYSTEM_INTL
        user = draft_user_prompt_intl(
            role, company_name, detail, location or "Remote", settings.cv_summary, signature
        )
    else:
        system = DRAFT_SYSTEM
        user = draft_user_prompt(role, company_name, detail, settings.cv_summary, signature)

    for _ in range(3):
        body = _enforce_range(groq_client.chat(system, user, temperature=0.8))
        if WORD_MIN <= count_words(body) <= WORD_MAX and _hash(body) not in seen:
            return body, True
    return body, (WORD_MIN <= count_words(body) <= WORD_MAX)


def _links_footer() -> str:
    """A compact 'links' block (GitHub/LeetCode/etc.) from the profile, for the email
    body. Empty if no links are set."""
    p = load_profile()
    labels = [("GitHub", "github"), ("LeetCode", "leetcode"),
              ("GeeksforGeeks", "geeksforgeeks"), ("LinkedIn", "linkedin"),
              ("Portfolio", "portfolio")]
    lines = [f"{label}: {p[key]}" for label, key in labels if p.get(key)]
    return ("\n\nLinks:\n" + "\n".join(lines)) if lines else ""


def generate_drafts() -> dict:
    summary = {"drafted": 0, "skipped": 0, "dry_run": settings.dry_run, "message": ""}

    with get_session() as session:
        pending = applications_for_drafting(session)
        seen = {_hash(b) for b in existing_bodies(session)}

        for application, company in pending:
            role = company.role_title or "Software Engineer"
            signature = settings.signature
            international = _is_international(company)
            body, ok = _generate_body(
                role, company.name, _detail(company), signature, seen,
                international=international, location=company.location,
            )
            if not ok or not body.strip():
                summary["skipped"] += 1
                log_event("draft", company.name, "skipped", "no valid body")
                continue

            # Keep a referral digest's explicit subject (e.g. "SE Intern application");
            # otherwise use the standard templated subject.
            subject = (
                application.email_subject
                if application.apply_kind == "email" and application.email_subject
                else subject_for(role, settings.sender_name)
            )
            seen.add(_hash(body))  # dedupe on the prose, before adding the links footer
            # Referral application emails list the coding-profile links in the body.
            if application.apply_kind == "email":
                body = body.rstrip() + _links_footer()
            update_draft(session, application, subject, body)
            summary["drafted"] += 1
            kind = "intl" if international else "india"
            log_event("draft", company.name, "drafted", f"{count_words(body)} words ({kind})")

    note = " (DRY RUN - local draft, no Groq quota used)" if settings.dry_run else ""
    summary["message"] = (
        f"Drafted {summary['drafted']} email(s), skipped {summary['skipped']}.{note}"
    )
    log_event("draft", "batch", "ok", summary["message"])
    return summary
