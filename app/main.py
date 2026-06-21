"""FastAPI app: routes, static dashboard mount, scheduler lifecycle.

Bound to 127.0.0.1 only (see __main__ and the README run command). No inbound
exposure (Architecture §8).
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import BackgroundTasks, FastAPI, File, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .config import CSE_DAILY_LIMIT, HUNTER_MONTHLY_LIMIT, STATIC_DIR, settings
from .db import (
    drafts_for_review,
    get_applications_full,
    get_session,
    get_state,
    init_db,
    is_paused,
    send_outcomes,
    set_paused,
    set_setting,
)
from .integrations import browser, cse, gmail, hunter
from .logic import rolling_bounce_rate
from .logging_setup import log_event, setup_logging
from .profile import load_profile, missing_required
from .services import (
    contacts,
    discovery,
    drafting,
    forms,
    importer,
    linkedin_apply,
    platform_apply,
    replies,
    resume,
    sender,
)

# Tracks the most recent background send run for the dashboard to poll.
LAST_SEND: dict = {"running": False, "result": None}
LAST_LI_APPLY: dict = {"running": False, "result": None}
# Tracks the most recent background form-fill run.
LAST_FILL: dict = {"running": False, "result": None}
# Tracks the most recent background platform (YC/Cutshort/ZipRecruiter) apply run.
LAST_PLATFORM: dict = {"running": False, "result": None, "platform": None}

scheduler = BackgroundScheduler()


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    init_db()
    # Hourly automated reply scan (FR-25). Interval from SCAN_INTERVAL_MINUTES.
    scheduler.add_job(
        replies.scan_job,
        "interval",
        minutes=max(1, settings.scan_interval_minutes),
        id="reply_scan",
        replace_existing=True,
    )
    if not scheduler.running:
        scheduler.start()
    log_event("startup", "app", "ready", f"dry_run={settings.dry_run}")
    yield
    if scheduler.running:
        scheduler.shutdown(wait=False)


app = FastAPI(title="AutoApply", lifespan=lifespan)


@app.exception_handler(Exception)
async def _safety_net(request: Request, exc: Exception) -> JSONResponse:
    """Global catch-all: any unhandled error is logged and returned as a clean,
    readable message instead of a 500/stack trace. The server never crashes; the
    dashboard shows the message and keeps working. Re-auth needs are spelled out."""
    msg = str(exc)
    if any(s in msg.lower() for s in ("invalid_grant", "token has been expired", "revoked")):
        friendly = "Google sign-in expired — it will re-prompt automatically on the next action."
    elif "rate limit" in msg.lower() or "429" in msg:
        friendly = "A service hit its rate limit — it auto-falls-back/retries; try again shortly."
    else:
        friendly = f"Something went wrong (handled, not a crash): {msg[:160]}"
    log_event("api", str(request.url.path), "error", msg[:300])
    return JSONResponse(status_code=200, content={"ok": False, "error": True, "message": friendly})


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/applications")
def applications_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "applications.html")


@app.get("/api/applications")
def api_applications() -> dict:
    """Full application detail + status summary for the tracker page."""
    with get_session() as session:
        return get_applications_full(session)


@app.get("/api/state")
def api_state() -> dict:
    with get_session() as session:
        state = get_state(session)
        state["paused"] = is_paused(session)
        state["bounce_rate"] = round(rolling_bounce_rate(send_outcomes(session)), 4)
    state["dry_run"] = settings.dry_run
    state["send_status"] = LAST_SEND
    state["li_apply_status"] = LAST_LI_APPLY
    state["banner"] = (
        "Supporting channel. Run alongside referrals and LinkedIn. "
        "Volume is capped for deliverability safety."
    )
    return state


@app.get("/api/health")
def api_health() -> dict:
    """Connection health so a needed re-login is shown proactively, not as an error."""
    from .config import ROOT
    return {
        "gmail": gmail.auth_status(),                       # ok | needs_login | unconfigured
        "linkedin_session": (ROOT / ".browser_profile").exists(),
        "groq": "ok" if settings.groq_api_key else "unconfigured",
        "groq_backup": bool(settings.groq_api_key_backup),
        "hunter_keys": len([k for k in (settings.hunter_api_key,
                                        settings.hunter_api_key_backup) if k]),
    }


@app.get("/api/quota")
def api_quota() -> dict:
    """Remaining free-tier budgets. Hunter counter arrives with Slice 2."""
    with get_session() as session:
        budget = get_state(session)["send_budget"]
        cse_used = cse.used_today(session)
        hunter_used = hunter.used_this_month(session)
    return {
        "send": budget,
        "cse": {"limit_per_day": CSE_DAILY_LIMIT, "used_today": cse_used},
        "hunter": {"limit_per_month": HUNTER_MONTHLY_LIMIT, "used_this_month": hunter_used},
    }


class DiscoverFilters(BaseModel):
    role: str | None = None
    location: str | None = None
    remote: bool = True            # LinkedIn discovery prefers remote by default
    keywords: str | None = None
    min_lpa: float | None = None   # salary floor in LPA (defaults to 8 in discovery)
    max_headcount: int | None = None  # startup bias: drop companies bigger than this


@app.post("/api/discover")
def api_discover(filters: DiscoverFilters) -> dict:
    """Run discovery for the given filters and return a summary (FR-1..FR-5)."""
    return discovery.discover(filters.model_dump())


@app.post("/api/contacts")
def api_contacts() -> dict:
    """Resolve + verify a contact for every posting that lacks one (FR-6..FR-10)."""
    return contacts.resolve_contacts()


@app.get("/api/import/template")
def api_import_template() -> Response:
    """Download a CSV template for bulk import."""
    return Response(
        content=importer.template_csv(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=autoapply_template.csv"},
    )


@app.post("/api/import")
async def api_import(file: UploadFile = File(...)) -> dict:
    """Bulk-import companies + contacts from an uploaded CSV/Excel file."""
    name = (file.filename or "").lower()
    data = await file.read()
    try:
        if name.endswith(".csv"):
            rows = importer.parse_csv(data)
        elif name.endswith(".xlsx"):
            rows = importer.parse_xlsx(data)
        else:
            return {"imported": 0, "message": "Unsupported file type — upload a .csv or .xlsx file."}
    except Exception as e:  # noqa: BLE001
        return {"imported": 0, "message": f"Could not read file: {e}"}
    if not rows:
        return {"imported": 0, "message": "No data rows found in the file."}
    return importer.import_rows(rows)


@app.post("/api/draft")
def api_draft() -> dict:
    """Generate a tailored draft for every posting in 'email_found' (FR-11..FR-14)."""
    return drafting.generate_drafts()


@app.get("/api/drafts")
def api_drafts() -> dict:
    """List drafts awaiting operator approval (FR-15)."""
    with get_session() as session:
        return {"drafts": drafts_for_review(session)}


class SendRequest(BaseModel):
    application_ids: list[int] = []
    allow_unverified: bool = False   # operator explicitly approved unverified recipients


def _run_send(ids: list[int], allow_unverified: bool = False) -> None:
    LAST_SEND["running"] = True
    try:
        LAST_SEND["result"] = sender.send_approved(ids, allow_unverified=allow_unverified)
    finally:
        LAST_SEND["running"] = False


@app.post("/api/send")
def api_send(req: SendRequest, background: BackgroundTasks) -> dict:
    """Send the operator-approved emails in the background (FR-16; anti-ban §6).

    Nothing sends without explicit approval: only the supplied application_ids are
    processed, and every anti-ban rule is enforced inside sender.send_approved.
    """
    if LAST_SEND["running"]:
        return {"started": False, "message": "A send run is already in progress."}
    if not req.application_ids:
        return {"started": False, "message": "No emails approved — nothing to send."}
    background.add_task(_run_send, req.application_ids, req.allow_unverified)
    return {
        "started": True,
        "message": f"Sending {len(req.application_ids)} approved email(s) in the background…",
    }


class SettingsUpdate(BaseModel):
    sending_paused: bool | None = None
    values: dict[str, str] | None = None


@app.post("/api/scan")
def api_scan() -> dict:
    """Run a reply scan now (FR-22..FR-26). Also runs hourly via the scheduler."""
    return replies.scan()


# --- Google-Forms auto-apply (referral digests) ---------------------------------

class DigestText(BaseModel):
    text: str = ""


@app.post("/api/referrals/ingest")
def api_referrals_ingest(payload: DigestText) -> dict:
    """Parse a pasted referral digest and store its jobs (forms + emails)."""
    if not payload.text.strip():
        return {"added": 0, "message": "Paste a referral email first."}
    return forms.ingest_digest(payload.text)


@app.post("/api/referrals/scan")
def api_referrals_scan() -> dict:
    """Scan Gmail for referral-digest emails and ingest them (real mode only)."""
    return forms.scan_inbox()


@app.get("/api/forms")
def api_forms() -> dict:
    """All form-apply jobs with fill/review state."""
    out = forms.list_jobs()
    out["fill_status"] = LAST_FILL
    out["dry_run"] = settings.dry_run
    return out


def _run_fill() -> None:
    LAST_FILL["running"] = True
    try:
        LAST_FILL["result"] = forms.fill_pending()
    finally:
        LAST_FILL["running"] = False


@app.post("/api/forms/fill")
def api_forms_fill(background: BackgroundTasks) -> dict:
    """Fill all pending forms + screenshot them for review (never submits)."""
    if LAST_FILL["running"]:
        return {"started": False, "message": "A fill run is already in progress."}
    background.add_task(_run_fill)
    return {"started": True,
            "message": "Filling forms in a browser - watch the cards update below."}


def _run_prefill() -> None:
    LAST_FILL["running"] = True
    try:
        LAST_FILL["result"] = forms.build_prefill_links()
    finally:
        LAST_FILL["running"] = False


@app.post("/api/forms/prefill")
def api_forms_prefill(background: BackgroundTasks) -> dict:
    """Build pre-filled form links you open in your own browser (no automation window)."""
    if LAST_FILL["running"]:
        return {"started": False, "message": "Already building links."}
    background.add_task(_run_prefill)
    return {"started": True,
            "message": "Building pre-filled links - the cards will show 'Open pre-filled form' shortly."}


class SubmitForm(BaseModel):
    application_id: int


@app.post("/api/forms/submit")
def api_forms_submit(req: SubmitForm) -> dict:
    """Operator-approved submit of one reviewed form (DRY_RUN simulates)."""
    return forms.submit_form(req.application_id)


@app.post("/api/forms/mark-submitted")
def api_forms_mark_submitted(req: SubmitForm) -> dict:
    """Record a form the operator submitted by hand (these forms need a manual CV upload)."""
    return forms.mark_submitted_manual(req.application_id)


@app.post("/api/forms/delete")
def api_forms_delete(req: SubmitForm) -> dict:
    """Archive a form job (hide from dashboard, keep in history)."""
    return forms.delete_job(req.application_id)


@app.post("/api/forms/delete-permanent")
def api_forms_delete_permanent(req: SubmitForm) -> dict:
    """Permanently delete a form job (destructive)."""
    return forms.delete_job_permanent(req.application_id)


# --- Assisted LinkedIn Easy Apply (pre-fill, operator submits) -------------------

class LinkedInApply(BaseModel):
    limit: int = 10


@app.get("/api/linkedin/targets")
def api_linkedin_targets() -> dict:
    """How many discovered LinkedIn jobs are queued for assisted Easy Apply."""
    return linkedin_apply.list_targets(limit=50)


def _run_li_apply(limit: int) -> None:
    LAST_LI_APPLY["running"] = True
    try:
        LAST_LI_APPLY["result"] = linkedin_apply.apply_assisted(limit=limit)
    finally:
        LAST_LI_APPLY["running"] = False


@app.post("/api/linkedin/apply")
def api_linkedin_apply(req: LinkedInApply, background: BackgroundTasks) -> dict:
    """Open + pre-fill matching LinkedIn Easy-Apply jobs in your logged-in Chrome.
    Holds the window for you to review screening questions and Submit. Never submits."""
    if LAST_LI_APPLY["running"]:
        return {"started": False, "message": "A LinkedIn apply run is already in progress."}
    background.add_task(_run_li_apply, max(1, min(req.limit, 25)))
    return {"started": True,
            "message": "Opening LinkedIn Easy Apply in your browser — review each and Submit."}


def _run_li_autoapply() -> None:
    LAST_LI_APPLY["running"] = True
    try:
        LAST_LI_APPLY["result"] = linkedin_apply.autoapply()
    finally:
        LAST_LI_APPLY["running"] = False


@app.post("/api/linkedin/autoapply")
def api_linkedin_autoapply(background: BackgroundTasks) -> dict:
    """AUTONOMOUS: auto-submit to India-scope Easy-Apply jobs up to the daily ramp cap.
    Skips (discards) jobs with unanswerable required questions. Stops on a captcha."""
    if LAST_LI_APPLY["running"]:
        return {"started": False, "message": "A LinkedIn run is already in progress."}
    background.add_task(_run_li_autoapply)
    return {"started": True,
            "message": "Auto-applying on LinkedIn in the background — watch the window; it stops on a security check."}


@app.post("/api/linkedin/stop")
def api_linkedin_stop() -> dict:
    """Ask a running auto-apply to stop after the current job (cooperative)."""
    browser.request_autoapply_stop()
    return {"ok": True, "message": "Stop requested — the agent will halt after the current job."}


# --- Other platforms: YC / Cutshort / ZipRecruiter (autonomous, fragile) ---------

@app.get("/api/platforms/status")
def api_platforms_status() -> dict:
    """Login-session presence + today's per-platform apply counts."""
    out = platform_apply.status()
    out["run"] = LAST_PLATFORM
    out["dry_run"] = settings.dry_run
    return out


class PlatformApply(BaseModel):
    platform: str                 # yc | cutshort | ziprecruiter
    query: str = ""               # role / skill / search keywords
    location: str = ""            # ZipRecruiter location (e.g. "Remote")
    remote: bool = True
    limit: int | None = None      # cap this run (None = up to the daily cap)


def _run_platform_apply(platform: str, query: str, location: str, remote: bool,
                        limit: int | None) -> None:
    LAST_PLATFORM["running"] = True
    LAST_PLATFORM["platform"] = platform
    try:
        LAST_PLATFORM["result"] = platform_apply.autoapply(platform, query, location, remote, limit)
    finally:
        LAST_PLATFORM["running"] = False


@app.post("/api/platforms/autoapply")
def api_platforms_autoapply(req: PlatformApply, background: BackgroundTasks) -> dict:
    """AUTONOMOUS apply on YC / Cutshort / ZipRecruiter (ToS-restricted; stops on any
    bot-wall). Needs a one-time login via  formtool.py platlogin <platform>."""
    if LAST_PLATFORM["running"]:
        return {"started": False, "message": "A platform apply run is already in progress."}
    if req.platform not in ("yc", "cutshort", "ziprecruiter"):
        return {"started": False, "message": f"Unknown platform '{req.platform}'."}
    limit = req.limit if (req.limit and req.limit > 0) else None
    background.add_task(_run_platform_apply, req.platform, req.query, req.location,
                        req.remote, limit)
    cap_note = f"up to {limit}" if limit else "up to the daily cap"
    return {"started": True,
            "message": f"Auto-applying on {req.platform} ({cap_note}) in the background — "
                       "watch the window; it stops on any security check."}


@app.get("/api/profile")
def api_profile() -> dict:
    """Current applicant profile + which required fields are still blank."""
    return {"profile": load_profile(), "missing": missing_required()}


class ResumeRead(BaseModel):
    overwrite: bool = False


@app.post("/api/profile/read-resume")
def api_read_resume(req: ResumeRead) -> dict:
    """Read cv.pdf and auto-fill the profile (blank fields only, unless overwrite)."""
    return resume.parse_resume(apply=True, overwrite=req.overwrite)


@app.get("/form_shots/{filename}")
def api_form_shot(filename: str) -> Response:
    """Serve a form-fill screenshot for the review cards."""
    safe = (browser.SHOTS_DIR / filename).resolve()
    if browser.SHOTS_DIR.resolve() not in safe.parents or not safe.exists():
        return Response(status_code=404)
    return FileResponse(safe)


@app.post("/api/settings")
def api_settings(update: SettingsUpdate) -> dict:
    """Update behavior settings and/or pause/resume sending (FR-20 resume path)."""
    with get_session() as session:
        if update.sending_paused is not None:
            set_paused(session, update.sending_paused)
            # Resuming = operator acknowledged; start a fresh bounce window so prior
            # bounces stop blocking new (now-verified) sends.
            if update.sending_paused is False:
                from datetime import datetime
                set_setting(session, "bounce_window_start",
                            datetime.now().isoformat(timespec="seconds"))
        for key, value in (update.values or {}).items():
            set_setting(session, key, str(value))
        paused = is_paused(session)
    return {"paused": paused, "message": "Settings updated."}


# Static assets (app.js, styles.css) served under /static.
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


if __name__ == "__main__":
    import uvicorn

    # Local-only bind — never expose publicly.
    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, reload=False)
