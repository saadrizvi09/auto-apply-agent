"""Read the applicant's CV (cv.pdf) and extract contact details into the profile.

The form-filler only ever uses profile.json - it never reads the PDF itself. This
module bridges that: it pulls text from the resume once and fills in the obvious
fields (email, phone, LinkedIn/GitHub/LeetCode/GeeksforGeeks links) so the operator
doesn't hand-type them (and can't get a stray dummy value).

By default it only fills BLANK profile fields - it won't clobber values you set by
hand unless overwrite=True.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from ..config import ROOT, settings
from ..logging_setup import log_event
from ..profile import DEFAULTS, PROFILE_PATH

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
# Indian mobile: optional +91/0 prefix, then 6-9 followed by 9 digits.
PHONE_RE = re.compile(r"(?:\+?91[\s\-]?|\b0)?([6-9]\d{9})\b")
URL_RES = {
    "linkedin": re.compile(r"(?:https?://)?(?:www\.)?linkedin\.com/in/[A-Za-z0-9\-_%]+", re.I),
    "github": re.compile(r"(?:https?://)?(?:www\.)?github\.com/[A-Za-z0-9\-_]+", re.I),
    "leetcode": re.compile(r"(?:https?://)?(?:www\.)?leetcode\.com/(?:u/)?[A-Za-z0-9\-_]+", re.I),
    "geeksforgeeks": re.compile(
        r"(?:https?://)?(?:www\.|auth\.)?geeksforgeeks\.org/(?:user|profile)/[A-Za-z0-9\-_]+", re.I),
}
# Don't capture obvious placeholder numbers (e.g. 9876543210, all-same digits).
_DUMMY_PHONES = {"9876543210", "1234567890", "0000000000"}


def _cv_path() -> Path:
    p = Path(settings.cv_path)
    return p if p.is_absolute() else (ROOT / p)


def _annotation_uris(page) -> list[str]:
    """Clickable hyperlink targets on a PDF page (LinkedIn/GitHub/etc. live here)."""
    uris = []
    try:
        for annot in page.get("/Annots") or []:
            obj = annot.get_object()
            uri = (obj.get("/A") or {}).get("/URI")
            if uri:
                uris.append(str(uri))
    except Exception:  # noqa: BLE001
        pass
    return uris


def read_cv_text() -> str:
    """Extract all text + hyperlink targets from the CV PDF (empty on any failure)."""
    from pypdf import PdfReader

    path = _cv_path()
    if not path.exists():
        return ""
    try:
        reader = PdfReader(str(path))
        parts = []
        for page in reader.pages:
            parts.append(page.extract_text() or "")
            parts.extend(_annotation_uris(page))
        return "\n".join(parts)
    except Exception as e:  # noqa: BLE001
        log_event("resume", "read", "error", str(e))
        return ""


def _norm_url(u: str) -> str:
    u = u.strip().rstrip("/.,)")
    return u if u.startswith("http") else "https://" + u


def extract_fields(text: str) -> dict[str, str]:
    """Pull contact fields out of resume text."""
    out: dict[str, str] = {}
    if not text:
        return out
    m = EMAIL_RE.search(text)
    if m:
        out["email"] = m.group(0)
    for ph in PHONE_RE.findall(text):
        if ph not in _DUMMY_PHONES and len(set(ph)) > 2:
            out["phone"] = ph
            break
    for field, rx in URL_RES.items():
        m = rx.search(text)
        if m and not re.search(r"change-?me|your-?handle|example|xxxx", m.group(0), re.I):
            out[field] = _norm_url(m.group(0))
    return out


def _load_raw_profile() -> dict:
    if PROFILE_PATH.exists():
        try:
            return json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            pass
    return dict(DEFAULTS)


def parse_resume(apply: bool = True, overwrite: bool = False) -> dict:
    """Read the CV, extract fields, and (optionally) write them into profile.json.

    Returns {found, applied, skipped, message}.
    """
    text = read_cv_text()
    found = extract_fields(text)
    result = {"found": found, "applied": {}, "skipped": {}, "message": ""}
    if not text:
        result["message"] = ("Couldn't read cv.pdf (missing or unreadable). "
                              "Put your resume at cv.pdf and try again.")
        return result
    if not found:
        result["message"] = "Read the resume but found no email/phone/profile links to extract."
        return result

    if apply:
        profile = _load_raw_profile()
        for k, v in found.items():
            current = str(profile.get(k, "")).strip()
            if overwrite or not current:
                profile[k] = v
                result["applied"][k] = v
            else:
                result["skipped"][k] = current
        PROFILE_PATH.write_text(json.dumps(profile, indent=2) + "\n", encoding="utf-8")

    applied = ", ".join(f"{k}={v}" for k, v in result["applied"].items()) or "nothing new"
    skipped = (f" Kept existing: {', '.join(result['skipped'])}." if result["skipped"] else "")
    result["message"] = f"From your resume - filled: {applied}.{skipped}"
    log_event("resume", "parse", "ok", result["message"])
    return result
