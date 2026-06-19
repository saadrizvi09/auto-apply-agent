"""Orchestrates Google-Forms auto-apply: digest -> fill -> review -> submit.

Pipeline:
  ingest_digest(text)  parse the referral email, store jobs (forms + emails).
  fill_pending()       for each form job: read questions, plan answers, fill the
                       live form, screenshot it. NEVER submits. -> status form_filled
  submit_form(id)      operator-approved single submit. DRY_RUN simulates (no real
                       submission); real mode clicks Submit in the logged-in browser.

Filling needs the one-time Google login in .browser_profile (see formtool.py login),
because the referral forms require sign-in. Open-text answers use Groq in real mode
and a deterministic stub in DRY_RUN, so a dry preview costs no quota and submits
nothing.
"""
from __future__ import annotations

import json
import time

from ..config import settings
from ..db import (
    archive_application,
    delete_application_cascade,
    form_application,
    form_applications_pending,
    form_applications_unsubmitted,
    get_session,
    ingest_referral_jobs,
    list_form_jobs,
    mark_form_submitted,
    save_form_fill,
    save_prefill,
)
from ..integrations import browser
from ..logging_setup import log_event
from ..models import Application, Company
from ..profile import missing_required
from . import referrals
from .formfiller import fill_open_answers, plan_answers


def ingest_digest(text: str) -> dict:
    """Parse a referral digest and store its jobs. Returns a summary."""
    jobs = referrals.parse_digest(text)
    with get_session() as session:
        summary = ingest_referral_jobs(session, jobs)
    summary["parsed"] = len(jobs)
    summary["message"] = (
        f"Parsed {len(jobs)} job(s): added {summary['added']} "
        f"({summary['forms']} form, {summary['emails']} email, {summary['manual']} manual); "
        f"skipped {summary['skipped']} already-seen."
    )
    log_event("forms", "ingest", "ok", summary["message"])
    return summary


def _shot_name(app_id: int) -> str:
    return f"form_{app_id}.png"


def _save_fill(app_id: int, answers_json: str, screenshot: str | None, status: str, note: str) -> None:
    with get_session() as s:
        a = s.get(Application, app_id)
        if a:
            save_form_fill(s, a, answers_json, screenshot, status, note)


def fill_pending(limit: int | None = None) -> dict:
    """Fill every pending form job and screenshot it for review. Never submits."""
    summary = {"filled": 0, "errors": 0, "needs_login": 0, "results": [], "dry_run": settings.dry_run}
    # Capture plain values while the session is open (avoid detached-instance access).
    with get_session() as session:
        pending = [
            {"id": app.id, "company": company.name, "role": company.role_title,
             "url": app.form_url or (contact.apply_url if contact else None)}
            for app, company, contact in form_applications_pending(session)
        ]

    for job in pending:
        if not job["url"]:
            continue
        info = browser.read_form(job["url"])
        if info["signin_required"]:
            _save_fill(job["id"], "[]", None, "form_error",
                       "Sign-in required - run: py -3.11 formtool.py login")
            summary["needs_login"] += 1
            continue
        if info["error"]:
            _save_fill(job["id"], "[]", None, "form_error", info["error"][:200])
            summary["errors"] += 1
            continue

        # Enrich a bare/standalone form job with the form's own title.
        if job["company"] in ("Form application", "Unknown") and info.get("title"):
            with get_session() as s:
                a = s.get(Application, job["id"])
                comp = s.get(Company, a.company_id) if a else None
                if comp:
                    comp.name = info["title"][:80]
                    s.add(comp)
            job["company"] = info["title"][:80]

        planned = fill_open_answers(
            plan_answers(info["questions"]), company=job["company"], role=job["role"]
        )
        shot = str(browser.SHOTS_DIR / _shot_name(job["id"]))
        res = browser.fill_form(job["url"], planned, shot, submit=False)
        if not res["error"] and res["total"] == 0:
            # Form loaded but no questions were read -> almost always not signed in.
            _save_fill(job["id"], "[]", None, "form_error",
                       "Couldn't read the form's questions - is the browser logged into "
                       "Google? Run: py -3.11 formtool.py login")
            summary["errors"] += 1
            summary["results"].append({"id": job["id"], "company": job["company"],
                                       "status": "form_error", "note": "no questions read"})
            continue
        note = res["error"] or (
            f"filled {res['filled']}/{res['total']} fields"
            + ("; complete the upload/missing fields before submitting"
               if any(p.get("blocked") or p["source"] == "missing" for p in planned) else "")
        )
        status = "form_error" if res["error"] else "form_filled"
        _save_fill(job["id"], json.dumps(planned),
                   _shot_name(job["id"]) if res["screenshot"] else None, status, note)
        if res["error"]:
            summary["errors"] += 1
        else:
            summary["filled"] += 1
        summary["results"].append({"id": job["id"], "company": job["company"],
                                   "status": status, "note": note})

    miss = missing_required()
    summary["message"] = (
        f"Filled {summary['filled']} form(s); {summary['errors']} error(s), "
        f"{summary['needs_login']} need login."
        + (f" Profile gaps: {', '.join(miss)}." if miss else "")
    )
    log_event("forms", "fill", "ok", summary["message"])
    return summary


def submit_form(app_id: int) -> dict:
    """Operator-approved submit of one filled form. DRY_RUN simulates only."""
    with get_session() as session:
        row = form_application(session, app_id)
        if not row:
            return {"ok": False, "message": "Form job not found."}
        app, company = row
        url, answers_json, status = app.form_url, app.form_answers, app.status
        company_name, role_title = company.name, company.role_title

    if status not in ("form_filled", "form_error"):
        return {"ok": False, "message": f"Not ready to submit (status: {status}). Fill the form first."}
    planned = json.loads(answers_json) if answers_json else []

    if settings.dry_run:
        with get_session() as s:
            mark_form_submitted(s, s.get(Application, app_id), "DRY_RUN - simulated submit, nothing sent")
        log_event("forms", company_name, "dry_run", "would submit form")
        return {"ok": True, "submitted": False, "dry_run": True,
                "message": f"DRY RUN: would submit {company_name}. Set DRY_RUN=false to really submit."}

    shot = str(browser.SHOTS_DIR / _shot_name(app_id))
    res = browser.fill_form(url, planned, shot, submit=True)
    if res["error"] or not res["submitted"]:
        return {"ok": False, "message": res["error"] or "Submit did not confirm.",
                "submitted": False}
    with get_session() as s:
        mark_form_submitted(s, s.get(Application, app_id),
                            f"submitted ({res['filled']}/{res['total']} fields)")
    log_event("forms", company_name, "submitted", "form submitted")
    return {"ok": True, "submitted": True,
            "message": f"Submitted {company_name} - {role_title}."}


def scan_inbox(query: str = '("Referral Alert" OR "How to Apply") newer_than:30d',
               max_msgs: int = 20) -> dict:
    """Find referral-digest emails in Gmail and ingest their jobs. Real mode only."""
    if settings.dry_run:
        return {"ok": False, "added": 0,
                "message": "DRY_RUN: inbox scan is skipped. Paste the email text, "
                           "or set DRY_RUN=false to scan Gmail."}
    from ..integrations import gmail
    total = {"ok": True, "emails_scanned": 0, "parsed": 0, "added": 0, "skipped": 0,
             "forms": 0, "emails": 0, "manual": 0}
    try:
        msgs = gmail.list_messages(query)[:max_msgs]
    except Exception as e:
        return {"ok": False, "added": 0, "message": f"Gmail scan failed: {e}"}
    with get_session() as session:
        for m in msgs:
            full = gmail.get_message(m["id"])
            jobs = referrals.parse_digest(full.get("text", ""))
            if not jobs:
                continue
            s = ingest_referral_jobs(session, jobs)
            for k in ("added", "skipped", "forms", "emails", "manual"):
                total[k] += s[k]
            total["parsed"] += len(jobs)
            total["emails_scanned"] += 1
    total["message"] = (
        f"Scanned {total['emails_scanned']} digest email(s); added {total['added']} "
        f"new job(s) ({total['forms']} form, {total['emails']} email)."
    )
    log_event("forms", "scan_inbox", "ok", total["message"])
    return total


def _build_prefill_url(base_url: str, fields: list[dict], planned: list[dict]) -> str:
    """Compose a Google Forms pre-filled link from planned answers + entry IDs."""
    from urllib.parse import quote
    params = ["usp=pp_url"]
    for f, p in zip(fields, planned):
        ans = p.get("answer", "")
        if ans and not p.get("blocked") and f.get("entry_id"):
            params.append(f"entry.{f['entry_id']}={quote(ans)}")
    return base_url.split("?")[0] + "?" + "&".join(params)


def build_prefill_links(limit: int | None = None) -> dict:
    """For each pending form, read its entry IDs and build a pre-filled link.

    The operator opens the link in their own (logged-in) browser → the form is
    already filled → they attach the CV + submit. No automation window needed.
    """
    summary = {"built": 0, "errors": 0, "needs_login": 0, "results": []}
    with get_session() as session:
        pending = [
            {"id": app.id, "company": company.name, "role": company.role_title,
             "url": app.form_url or (contact.apply_url if contact else None)}
            for app, company, contact in form_applications_unsubmitted(session)
        ]

    for idx, job in enumerate(pending):
        if not job["url"]:
            continue
        if idx:
            time.sleep(1.2)  # let the previous form's Chrome release the profile lock
        info = browser.read_form_full(job["url"])
        if info["signin_required"]:
            with get_session() as s:
                save_form_fill(s, s.get(Application, job["id"]), "[]", None, "form_error",
                               "Sign-in required - run: py -3.11 formtool.py login")
            summary["needs_login"] += 1
            continue
        if info["error"] or not info["fields"]:
            with get_session() as s:
                save_form_fill(s, s.get(Application, job["id"]), "[]", None, "form_error",
                               info["error"] or "no fields read")
            summary["errors"] += 1
            continue

        planned = fill_open_answers(plan_answers(info["fields"]),
                                    company=job["company"], role=job["role"])
        prefill = _build_prefill_url(job["url"], info["fields"], planned)
        uploads = [p["title"] for p in planned if p.get("blocked")]
        note = ("Open the pre-filled link, attach your CV"
                + (f" ({uploads[0]})" if uploads else "") + ", then Submit.")
        with get_session() as s:
            save_prefill(s, job["id"], prefill, json.dumps(planned), note)
        summary["built"] += 1
        summary["results"].append({"id": job["id"], "company": job["company"]})

    miss = missing_required()
    summary["message"] = (
        f"Built {summary['built']} pre-filled link(s); {summary['errors']} error(s), "
        f"{summary['needs_login']} need login."
        + (f" Profile gaps: {', '.join(miss)}." if miss else "")
    )
    log_event("forms", "prefill", "ok", summary["message"])
    return summary


def mark_submitted_manual(app_id: int) -> dict:
    """Operator submitted the form by hand in the browser; just record it as done."""
    with get_session() as session:
        row = form_application(session, app_id)
        if not row:
            return {"ok": False, "message": "Form job not found."}
        app, company = row
        name = company.name
        mark_form_submitted(session, app, "marked submitted manually by operator")
    log_event("forms", name, "submitted", "marked submitted manually")
    return {"ok": True, "submitted": True, "message": f"Marked {name} as submitted."}


def delete_job(app_id: int) -> dict:
    """Archive a form job: hide it from the dashboard but KEEP it in history.
    Non-destructive — the application stays in the Applications tracker."""
    with get_session() as session:
        name = archive_application(session, app_id)
    if not name:
        return {"ok": False, "message": "Already gone."}
    log_event("forms", name, "archived", "hidden from dashboard (kept in history)")
    return {"ok": True, "message": f"Archived {name} (still in Applications history)."}


def delete_job_permanent(app_id: int) -> dict:
    """Permanently delete a form job (company + contact + application). Destructive."""
    with get_session() as session:
        name = delete_application_cascade(session, app_id)
    if not name:
        return {"ok": False, "message": "Already gone."}
    log_event("forms", name, "deleted", "permanently removed")
    return {"ok": True, "message": f"Permanently deleted {name}."}


def list_jobs() -> dict:
    with get_session() as session:
        return {"forms": list_form_jobs(session)}
