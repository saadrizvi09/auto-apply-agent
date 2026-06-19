"""Referral-digest ingestion: turn a "Referral Alert" email into structured jobs.

These digests (forwarded from Telegram/WhatsApp job channels) bundle N postings,
each ending in a "How to Apply:" block that is either a Google Form link or an
email-to-apply instruction. We split the digest into jobs and route each one:

  apply_kind = "form"   -> a Google Form URL  -> browser form-filler
  apply_kind = "email"  -> an email + subject -> existing email pipeline
  apply_kind = "manual" -> anything we can't auto-handle (flagged for the operator)

Offline (DRY_RUN) a deterministic regex parser handles the common numbered format;
in real mode Groq extracts jobs robustly from messier text. The two return the same
shape so everything downstream is identical.
"""
from __future__ import annotations

import json
import re

from ..config import settings
from ..integrations import groq_client
from ..logging_setup import log_event

# A Google Form link in any of its shapes.
FORM_RE = re.compile(r"https://docs\.google\.com/forms/\S+", re.I)
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
SUBJECT_RE = re.compile(r'subject[:\s]*[\'"“]([^\'"”]+)[\'"”]', re.I)
# Lines that mark the promotional tail we should ignore.
TAIL_RE = re.compile(
    r"(for free hiring updates|if you are a serious|premium referral|topmate\.io"
    r"|whatsapp\.com/channel|only for serious)",
    re.I,
)
FIELD_RE = {
    "company": re.compile(r"company\s*[-:]\s*(.+)", re.I),
    "role": re.compile(r"role\s*[-:]\s*(.+)", re.I),
    "batch": re.compile(r"batch\s*[-:]\s*(.+)", re.I),
    "stipend": re.compile(r"stipend\s*[-:]\s*(.+)", re.I),
    "location": re.compile(r"location\s*[-:]\s*(.+)", re.I),
}


def _clean_url(url: str) -> str:
    return url.rstrip(").,;'\"” ").strip()


def _strip_tail(text: str) -> str:
    """Remove promotional lines (they repeat between jobs, not just at the end)."""
    return "\n".join(ln for ln in text.splitlines() if not TAIL_RE.search(ln))


def _split_blocks(text: str) -> list[str]:
    """Split a digest into per-job blocks, breaking before each '1)' '2)' marker."""
    parts = re.split(r"(?m)^(?=\s*\d+\)\s)", text)
    parts = [p.strip() for p in parts if p.strip()]
    # No numbered markers => treat the whole thing as one block.
    return parts if len(parts) > 1 else [text.strip()]


def _apply_block(block: str) -> str:
    """The text after 'How to Apply:' within a job block (or '' if absent)."""
    m = re.search(r"how to apply\s*[:\-]?\s*(.+)", block, re.I | re.S)
    return m.group(1).strip() if m else ""


def _route(apply_text: str, whole_block: str) -> dict:
    """Classify how to apply and pull out the target details."""
    search_space = apply_text or whole_block
    form = FORM_RE.search(search_space)
    if form:
        return {"apply_kind": "form", "apply_url": _clean_url(form.group(0)),
                "apply_email": "", "apply_cc": "", "apply_subject": ""}

    emails = EMAIL_RE.findall(search_space)
    if emails:
        to = emails[0]
        cc = ""
        cc_m = re.search(r"cc[:\s]+(" + EMAIL_RE.pattern + ")", search_space, re.I)
        if cc_m:
            cc = cc_m.group(1)
        elif len(emails) > 1:
            cc = emails[1]
        subj = SUBJECT_RE.search(search_space)
        return {"apply_kind": "email", "apply_url": "", "apply_email": to,
                "apply_cc": cc, "apply_subject": subj.group(1).strip() if subj else ""}

    return {"apply_kind": "manual", "apply_url": "", "apply_email": "",
            "apply_cc": "", "apply_subject": ""}


def _field(block: str, key: str) -> str:
    m = FIELD_RE[key].search(block)
    return m.group(1).strip() if m else ""


def parse_heuristic(text: str) -> list[dict]:
    """Regex parser for the common numbered digest format. No network."""
    text = _strip_tail(text)
    jobs = []
    for block in _split_blocks(text):
        company = _field(block, "company")
        role = _field(block, "role")
        if not company and not role:
            continue  # not a job block (intro/footer)
        route = _route(_apply_block(block), block)
        jobs.append({
            "company": company or "Unknown",
            "role": role or "Software Engineering Intern",
            "batch": _field(block, "batch"),
            "stipend": _field(block, "stipend"),
            "location": _field(block, "location"),
            **route,
        })
    return jobs


# --- Groq path (real mode, messy emails) ----------------------------------------

_EXTRACT_SYSTEM = (
    "You extract job postings from a referral-digest email. Return ONLY a JSON array. "
    "Each element: {\"company\",\"role\",\"batch\",\"stipend\",\"location\","
    "\"apply_kind\",\"apply_url\",\"apply_email\",\"apply_cc\",\"apply_subject\"}. "
    "apply_kind is 'form' if applying via a Google Form URL (put it in apply_url), "
    "'email' if applying by sending an email (fill apply_email, apply_cc, "
    "apply_subject), else 'manual'. Use empty strings for unknown fields. No prose."
)


def parse_with_groq(text: str) -> list[dict]:
    raw = groq_client.chat(_EXTRACT_SYSTEM, text, temperature=0.0, max_tokens=1500).strip()
    # Tolerate a fenced code block.
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-z]*\n?|\n?```$", "", raw).strip()
    try:
        data = json.loads(raw)
    except ValueError:
        return parse_heuristic(text)
    out = []
    for j in data if isinstance(data, list) else []:
        if not isinstance(j, dict):
            continue
        out.append({
            "company": str(j.get("company", "")).strip() or "Unknown",
            "role": str(j.get("role", "")).strip() or "Software Engineering Intern",
            "batch": str(j.get("batch", "")).strip(),
            "stipend": str(j.get("stipend", "")).strip(),
            "location": str(j.get("location", "")).strip(),
            "apply_kind": (str(j.get("apply_kind", "")).strip().lower() or "manual"),
            "apply_url": _clean_url(str(j.get("apply_url", ""))),
            "apply_email": str(j.get("apply_email", "")).strip(),
            "apply_cc": str(j.get("apply_cc", "")).strip(),
            "apply_subject": str(j.get("apply_subject", "")).strip(),
        })
    return out or parse_heuristic(text)


def _bare_form_jobs(text: str, already: list[dict]) -> list[dict]:
    """Catch standalone Google Form links pasted without the numbered structure.

    The company/role are unknown here - they get enriched from the form's own title
    when it is filled.
    """
    seen = {j.get("apply_url", "").split("?")[0] for j in already if j.get("apply_url")}
    extra = []
    for m in FORM_RE.finditer(text):
        url = _clean_url(m.group(0))
        if url.split("?")[0] in seen:
            continue
        seen.add(url.split("?")[0])
        extra.append({"company": "Form application", "role": "(from form)",
                      "batch": "", "stipend": "", "location": "",
                      "apply_kind": "form", "apply_url": url,
                      "apply_email": "", "apply_cc": "", "apply_subject": ""})
    return extra


def parse_digest(text: str) -> list[dict]:
    """Parse a dump (referral emails, form links, anything) into routed jobs.

    Heuristic-first (free + reliable on the numbered format); Groq fallback for
    messy emails in real mode; then sweep up any standalone Google Form links.
    """
    if not text or not text.strip():
        return []
    jobs = parse_heuristic(text)
    jobs += _bare_form_jobs(text, jobs)  # standalone form links the structure missed (free)
    if not jobs and not settings.dry_run:
        jobs = parse_with_groq(text)     # only pay for Groq if we found nothing
    log_event("referral", "parse", "ok",
              f"{len(jobs)} job(s): "
              + ", ".join(f"{j['company']}/{j['apply_kind']}" for j in jobs))
    return jobs
