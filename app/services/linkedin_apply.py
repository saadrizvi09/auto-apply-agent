"""Assisted LinkedIn Easy-Apply (pre-fill, the operator submits).

Opens the matching discovered LinkedIn jobs in the operator's own logged-in Chrome,
clicks Easy Apply, and pre-fills the safe fields from profile.json. It NEVER submits
— the operator reviews any screening questions and clicks Submit. This mirrors the
Google-Forms pre-fill discipline and keeps LinkedIn-ban risk low (a human is driving
the final action). External-apply (non-Easy-Apply) jobs are flagged for manual apply.

In DRY_RUN nothing opens — it just reports what it would do.
"""
from __future__ import annotations

from datetime import date

from ..config import settings
from ..db import (
    get_session,
    get_setting,
    linkedin_jobs,
    set_li_status,
    set_setting,
)
from ..integrations import browser
from ..logic import li_ramp_cap
from ..logging_setup import log_event
from ..profile import load_profile

# Location markers that count as India / remote-India (the only scope we auto-apply to).
_INDIA = (
    "india", "delhi", "ncr", "gurgaon", "gurugram", "noida", "bengaluru", "bangalore",
    "mumbai", "hyderabad", "pune", "chennai", "kolkata", "ahmedabad", "remote",
)


def _is_india_scope(job: dict) -> bool:
    loc = (job.get("location") or "").lower()
    if not loc:
        return True  # unknown location — let the agent's work-auth answer decide
    if any(c in loc for c in ("united states", "u.s.", "usa", "united kingdom", " uk ",
                              "germany", "europe", "canada", "australia", "singapore")):
        # explicitly foreign and not remote-India
        return "india" in loc
    return any(m in loc for m in _INDIA)


def _li_used_today(session) -> int:
    raw = get_setting(session, f"li_applied_{date.today().isoformat()}")
    try:
        return int(raw) if raw else 0
    except ValueError:
        return 0


def _li_add_used(session, n: int) -> None:
    key = f"li_applied_{date.today().isoformat()}"
    set_setting(session, key, str(_li_used_today(session) + n))


def _li_cap_today(session) -> int:
    cap = settings.li_daily_cap
    if not settings.li_warmup_ramp:
        return cap
    raw = get_setting(session, "li_first_apply_date")
    first = date.fromisoformat(raw) if raw else None
    return li_ramp_cap(first, date.today(), cap)


def autoapply() -> dict:
    """AUTONOMOUS LinkedIn Easy Apply: submit to India-scope Easy-Apply jobs up to the
    daily ramp cap. Skips (discards) any job with an unanswerable required field."""
    profile = load_profile()
    with get_session() as session:
        cap = _li_cap_today(session)
        used = _li_used_today(session)
        remaining = max(0, cap - used)
        jobs = [j for j in linkedin_jobs(session, 300) if _is_india_scope(j)]
        urgent_queued = sum(1 for j in jobs[:remaining] if j.get("urgent"))

    if remaining <= 0:
        return {"ok": True, "submitted": 0,
                "message": f"Daily LinkedIn cap reached ({used}/{cap}). Resumes tomorrow."}
    if not jobs:
        return {"ok": True, "submitted": 0,
                "message": "No India-scope LinkedIn jobs queued. Run ① Find Jobs first."}

    jobs = jobs[:remaining]
    if settings.dry_run:
        log_event("li_autoapply", "batch", "dry_run", f"{len(jobs)} job(s), cap {cap}")
        return {"ok": True, "submitted": 0, "dry_run": True,
                "message": f"DRY RUN — would auto-apply to up to {len(jobs)} job(s) (cap {cap}/day)."}

    results = browser.linkedin_autoapply_session(jobs, profile, max_apply=remaining)

    submitted = [r for r in results if r.get("outcome") == "submitted"]
    skipped = [r for r in results if str(r.get("outcome", "")).startswith("skipped")]
    captcha = any(r.get("outcome") == "captcha_stop" for r in results)
    needs_login = any(r.get("outcome") == "needs_login" for r in results)

    with get_session() as session:
        if submitted and not get_setting(session, "li_first_apply_date"):
            set_setting(session, "li_first_apply_date", date.today().isoformat())
        _li_add_used(session, len(submitted))
        for r in results:
            oc = r.get("outcome", "")
            if oc == "submitted":
                set_li_status(session, r["id"], "li_applied", "auto-applied via LinkedIn Easy Apply")
            elif oc == "external":
                set_li_status(session, r["id"], "li_external", "external apply — do on company site")
            elif str(oc).startswith("skipped"):
                set_li_status(session, r["id"], "li_skipped", f"auto-apply {oc} — review by hand")

    if needs_login:
        msg = "Not logged into LinkedIn. Run:  py -3.11 formtool.py lilogin, then retry."
    elif captcha:
        msg = (f"Stopped: LinkedIn showed a security check. Submitted {len(submitted)} before "
               f"stopping — wait a while before running again (ban-risk).")
    else:
        urgent_note = (f" Prioritised {urgent_queued} urgent/hiring post(s) first."
                       if urgent_queued else "")
        msg = (f"Auto-applied to {len(submitted)} job(s); skipped {len(skipped)} "
               f"(unanswerable questions — listed for manual). Daily cap {cap}.{urgent_note}")
    log_event("li_autoapply", "batch", "ok", msg)
    return {"ok": True, "submitted": len(submitted), "skipped": len(skipped),
            "captcha_stop": captcha, "message": msg}


def list_targets(limit: int = 10) -> dict:
    """How many LinkedIn jobs are queued for assisted apply."""
    with get_session() as session:
        jobs = linkedin_jobs(session, limit)
    return {"count": len(jobs), "jobs": jobs}


def apply_assisted(limit: int = 10) -> dict:
    """Open + pre-fill up to `limit` Easy-Apply jobs; hold the window for review+submit."""
    profile = load_profile()
    with get_session() as session:
        jobs = linkedin_jobs(session, limit)

    if not jobs:
        return {"ok": True, "count": 0,
                "message": "No LinkedIn jobs to apply to. Run ① Find Jobs first."}

    if settings.dry_run:
        log_event("li_apply", "batch", "dry_run", f"{len(jobs)} job(s)")
        return {"ok": True, "count": len(jobs), "dry_run": True,
                "message": f"DRY RUN — would open {len(jobs)} Easy-Apply job(s) and pre-fill them."}

    results = browser.linkedin_apply_session(jobs, profile)

    easy = sum(1 for r in results if r.get("easy_apply"))
    external = sum(1 for r in results if r.get("external"))
    needs_login = any(r.get("needs_login") for r in results)

    with get_session() as session:
        for j, r in zip(jobs, results):
            if r.get("needs_login"):
                continue  # leave status untouched so a retry picks it up
            if r.get("external"):
                set_li_status(session, j["id"], "li_external",
                              "external apply — do it on the company site")
            elif r.get("easy_apply"):
                set_li_status(session, j["id"], "li_prefilled",
                              f"Easy Apply pre-filled {r.get('filled')}/{r.get('total')} — review + submit")
            elif r.get("error"):
                set_li_status(session, j["id"], "li_error", r["error"][:200])

    if needs_login:
        msg = ("Not logged into LinkedIn. Run:  py -3.11 formtool.py lilogin  "
               "(log in, close the window), then try again.")
    else:
        msg = (f"Opened {easy} Easy-Apply job(s) pre-filled — review + Submit in the browser. "
               f"{external} are external-apply (do those on the company site).")
    log_event("li_apply", "batch", "ok", msg)
    return {"ok": True, "count": len(jobs), "easy_apply": easy,
            "external": external, "needs_login": needs_login, "message": msg}
