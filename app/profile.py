"""Applicant profile: the answers used to auto-fill job-application forms.

Loaded from profile.json at the project root (git-ignored, like secrets). These
are the ~15 facts referral Google Forms ask for again and again - name, email,
phone, college, batch, CV link, coding profiles, expected stipend, etc. The form
filler maps each form question to one of these fields; anything free-text (e.g.
"why should we hire you") is answered by Groq using `context_block()`.

Edit profile.json once; nothing here is secret-sensitive except that it is
personal, so it stays out of git.
"""
from __future__ import annotations

import json
from pathlib import Path

from .config import ROOT

PROFILE_PATH = ROOT / "profile.json"

# Canonical fields with safe defaults. profile.json overrides any of these.
DEFAULTS: dict[str, str] = {
    "full_name": "",
    "email": "",
    "phone": "",
    "city": "",
    "college": "",
    "degree": "",
    "graduation_year": "",
    "batch": "",
    "cv_url": "",            # the public resume link pasted into "resume link" fields
    "linkedin": "",
    "github": "",
    "leetcode": "",
    "geeksforgeeks": "",
    "portfolio": "",
    "years_experience": "0",
    "default_skill_years": "1",   # answer for "years with <skill>" when not specified
    "expected_stipend": "",
    "expected_ctc": "",
    "expected_ctc_number": "",     # numeric salary expectation (for number-only fields)
    "current_ctc": "",
    "notice_period": "",
    "currently_working": "Yes",   # answer to "are you currently working?" (interning counts)
    "employment_status": "Not working currently",  # Cutshort: Not working / On notice period / Not resigned
                                                    # ("On notice period" demands a notice-end date)
    "willing_to_relocate": "Yes",
    # Answer bank for autonomous LinkedIn apply (India scope)
    "work_authorized_in": "India",
    "needs_sponsorship": "No",
    "address_line1": "",
    "address_line2": "",
    "pincode": "",
    "country": "India",
    "gender": "",
    "extra_notes": "",       # free-form: anything extra to feed the LLM for open questions
}


def load_profile() -> dict[str, str]:
    """Return the merged profile (defaults <- profile.json). Missing file => defaults."""
    data = dict(DEFAULTS)
    if PROFILE_PATH.exists():
        try:
            loaded = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
            for k, v in loaded.items():
                if k in DEFAULTS and v is not None:
                    data[k] = str(v).strip()
        except (ValueError, OSError):
            pass
    return data


def missing_required(profile: dict[str, str] | None = None) -> list[str]:
    """Fields that really should be filled before form-filling is useful."""
    p = profile or load_profile()
    required = ["full_name", "email", "phone", "college", "graduation_year", "cv_url"]
    return [f for f in required if not p.get(f)]


def context_block(profile: dict[str, str] | None = None) -> str:
    """A compact human-readable profile block to give the LLM for open questions."""
    p = profile or load_profile()
    lines = []
    label = {
        "full_name": "Name", "email": "Email", "phone": "Phone", "city": "City",
        "college": "College", "degree": "Degree", "graduation_year": "Graduation year",
        "batch": "Batch", "cv_url": "Resume link", "linkedin": "LinkedIn",
        "github": "GitHub", "leetcode": "LeetCode", "geeksforgeeks": "GeeksforGeeks",
        "portfolio": "Portfolio",
        "years_experience": "Years of experience", "expected_stipend": "Expected stipend",
        "current_ctc": "Current CTC", "notice_period": "Availability/notice",
        "willing_to_relocate": "Willing to relocate", "extra_notes": "Notes",
    }
    for key, lab in label.items():
        if p.get(key):
            lines.append(f"{lab}: {p[key]}")
    return "\n".join(lines)
