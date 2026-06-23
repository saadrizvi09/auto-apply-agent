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
from . import answer_bank

# Location markers that count as India.
_INDIA = (
    "india", "delhi", "ncr", "gurgaon", "gurugram", "noida", "bengaluru", "bangalore",
    "mumbai", "hyderabad", "pune", "chennai", "kolkata", "ahmedabad",
)
# Remote / location-agnostic markers — doable from India, so in scope regardless of country.
_REMOTE = ("remote", "anywhere", "work from home", "wfh", "distributed")


def _in_apply_scope(job: dict) -> bool:
    """Whether the agent should attempt this job. India and remote/global are obviously in
    scope. Foreign-located jobs are ALSO attempted — because LinkedIn's guest scraper labels
    REMOTE roles by country (e.g. "United States"), not "Remote", so a remote-US role and an
    on-site-Tampa role are indistinguishable here. The work-authorisation screening answer
    ("No" for a country the operator can't work in) then truthfully discards the genuinely
    on-site-only ones inside the Easy-Apply flow — so the honest filter happens there, not on
    the location string. Net: attempt everything; let work-auth do the real filtering."""
    return True


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
    """AUTONOMOUS LinkedIn Easy Apply: submit to in-scope Easy-Apply jobs up to the daily
    ramp cap. Skips (discards) any job with an unanswerable required field, and foreign
    on-site jobs self-discard via the work-authorisation answer."""
    profile = load_profile()
    with get_session() as session:
        cap = _li_cap_today(session)
        used = _li_used_today(session)
        remaining = max(0, cap - used)
        jobs = [j for j in linkedin_jobs(session, 300) if _in_apply_scope(j)]
        urgent_queued = sum(1 for j in jobs[:remaining] if j.get("urgent"))

    if remaining <= 0:
        return {"ok": True, "submitted": 0,
                "message": f"Daily LinkedIn cap reached ({used}/{cap}). Resumes tomorrow."}
    if not jobs:
        return {"ok": True, "submitted": 0,
                "message": "No LinkedIn jobs queued for apply. Run ① Find Jobs first."}

    jobs = jobs[:remaining]
    if settings.dry_run:
        log_event("li_autoapply", "batch", "dry_run", f"{len(jobs)} job(s), cap {cap}")
        return {"ok": True, "submitted": 0, "dry_run": True,
                "message": f"DRY RUN — would auto-apply to up to {len(jobs)} job(s) (cap {cap}/day)."}

    bank_before = answer_bank.count()
    # Easy-Apply ONLY: non-Easy-Apply jobs are not auto-filled on the company site — their
    # link is captured and handed back to the operator.
    results = browser.linkedin_autoapply_session(jobs, profile, max_apply=remaining,
                                                 external_submit=False)
    learned = max(0, answer_bank.count() - bank_before)

    submitted = [r for r in results if r.get("outcome") == "submitted"]
    externals = [r for r in results if r.get("outcome") == "external" and r.get("url")]
    skipped = [r for r in results if str(r.get("outcome", "")).startswith("skipped")]
    captcha = any(r.get("outcome") == "captcha_stop" for r in results)
    needs_login = any(r.get("outcome") == "needs_login" for r in results)
    closed = any(r.get("outcome") == "window_closed" for r in results)

    with get_session() as session:
        if submitted and not get_setting(session, "li_first_apply_date"):
            set_setting(session, "li_first_apply_date", date.today().isoformat())
        _li_add_used(session, len(submitted))
        for r in results:
            oc = r.get("outcome", "")
            if oc == "submitted":
                how = "company ATS site" if r.get("external") else "LinkedIn Easy Apply"
                set_li_status(session, r["id"], "li_applied", f"auto-applied via {how}")
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
        learned_note = (f" Learned {learned} new answer(s) for next time." if learned else "")
        ext_note = (f" {len(externals)} job(s) are NOT Easy Apply — links below."
                    if externals else "")
        closed_note = " You closed the browser window, so the run stopped early." if closed else ""
        msg = (f"Auto-applied to {len(submitted)} Easy-Apply job(s); skipped {len(skipped)} "
               f"(unanswerable/incomplete). Daily cap {cap}."
               f"{ext_note}{closed_note}{urgent_note}{learned_note}")
        if externals:
            msg += "\n\nNot Easy Apply — apply on the company site:\n" + "\n".join(
                f"  • {r.get('company') or 'Job'} ({r.get('role') or ''}): {r.get('url')}"
                for r in externals)
    log_event("li_autoapply", "batch", "ok", msg)
    return {"ok": True, "submitted": len(submitted), "skipped": len(skipped),
            "learned": learned, "captcha_stop": captcha,
            "external_links": [{"company": r.get("company"), "role": r.get("role"),
                                "url": r.get("url")} for r in externals],
            "message": msg}


def hard_apply_assisted(limit: int = 12) -> dict:
    """HARD-APPLY ASSIST: open the NON-Easy-Apply LinkedIn jobs, walk into each company ATS,
    AI-fill everything known, and HOLD the windows for the operator to review + Submit. Never
    submits. Targets jobs flagged li_external (by the Easy-Apply run) plus the unapplied queue."""
    profile = load_profile()
    with get_session() as session:
        jobs = linkedin_jobs(session, limit,
                             statuses=["li_external", "discovered", "email_found"])
    if not jobs:
        return {"ok": True, "count": 0,
                "message": "No LinkedIn jobs queued. Run ① Find Jobs (and Auto-apply) first."}

    if settings.dry_run:
        log_event("li_hardapply", "batch", "dry_run", f"{len(jobs)} job(s)")
        return {"ok": True, "count": len(jobs), "dry_run": True,
                "message": f"DRY RUN — would open up to {len(jobs)} company application(s) and AI-fill them."}

    results = browser.linkedin_hardapply_session(jobs, profile, max_open=limit)
    opened = [r for r in results if r.get("opened")]
    easy = [r for r in results if r.get("easy_apply")]
    needs_login = any(r.get("needs_login") for r in results)

    with get_session() as session:
        for r in results:
            if r.get("opened"):
                set_li_status(session, r["id"], "li_hard_prefilled",
                              f"company ATS AI-filled {r.get('filled')}/{r.get('total')} — review + submit")

    if needs_login:
        msg = ("Not logged into LinkedIn. Run:  py -3.11 formtool.py lilogin  "
               "(log in, close the window), then try again.")
    else:
        easy_note = (f" {len(easy)} were Easy Apply (use Auto-apply for those)." if easy else "")
        msg = (f"Opened {len(opened)} company-site application(s), AI-filled — review + Submit "
               f"each in the browser.{easy_note}")
    log_event("li_hardapply", "batch", "ok", msg)
    return {"ok": True, "count": len(opened), "easy_apply": len(easy),
            "needs_login": needs_login, "message": msg}


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

    bank_before = answer_bank.count()
    results = browser.linkedin_apply_session(jobs, profile)
    learned = max(0, answer_bank.count() - bank_before)

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
        learned_note = (f" Learned {learned} answer(s) from your inputs for future auto-apply."
                        if learned else "")
        msg = (f"Opened {easy} Easy-Apply job(s) pre-filled — review + Submit in the browser. "
               f"{external} are external-apply (do those on the company site).{learned_note}")
    log_event("li_apply", "batch", "ok", msg)
    return {"ok": True, "count": len(jobs), "easy_apply": easy, "external": external,
            "learned": learned, "needs_login": needs_login, "message": msg}
