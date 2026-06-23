"""SQLite engine, schema init, and repository helpers (all reads/writes go here).

Writes are transactional via short-lived sessions (NFR-4). The schema is created
from the SQLModel models, which mirror Technical-Spec §2.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timedelta
from typing import Iterator, Optional

from sqlalchemy import event, func
from sqlmodel import Session, SQLModel, create_engine, select

from .config import DB_PATH, settings
from .logic import ramp_cap_for_today
from .models import Application, Company, Contact, SendLog, Setting

engine = create_engine(
    f"sqlite:///{DB_PATH}",
    echo=False,
    # timeout: how long the driver waits for a locked DB before raising (seconds).
    connect_args={"check_same_thread": False, "timeout": 30},
)


@event.listens_for(engine, "connect")
def _sqlite_pragmas(dbapi_conn, _record):
    """Make SQLite tolerate concurrent access (scheduler thread + request handlers).

    WAL lets readers and a single writer coexist without blocking each other, and
    busy_timeout makes a would-be second writer wait its turn (up to 30s) instead of
    failing immediately with 'database is locked'. synchronous=NORMAL is the safe,
    fast pairing for WAL. Set on every new connection via the connect event.
    """
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA busy_timeout=30000")
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.close()


def init_db() -> None:
    """Create tables if they don't exist, and apply lightweight column migrations."""
    SQLModel.metadata.create_all(engine)
    # Self-healing migration: add columns introduced after a DB was first created.
    with engine.begin() as conn:
        cols = {r[1] for r in conn.exec_driver_sql("PRAGMA table_info(companies)").fetchall()}
        if "salary" not in cols:
            conn.exec_driver_sql("ALTER TABLE companies ADD COLUMN salary TEXT")
        if "headcount" not in cols:
            conn.exec_driver_sql("ALTER TABLE companies ADD COLUMN headcount TEXT")
        if "urgent" not in cols:
            conn.exec_driver_sql(
                "ALTER TABLE companies ADD COLUMN urgent INTEGER NOT NULL DEFAULT 0"
            )
        app_cols = {r[1] for r in conn.exec_driver_sql("PRAGMA table_info(applications)").fetchall()}
        for col in ("apply_kind", "form_url", "form_answers", "form_screenshot",
                    "form_note", "form_prefill_url", "email_cc"):
            if col not in app_cols:
                conn.exec_driver_sql(f"ALTER TABLE applications ADD COLUMN {col} TEXT")
        if "archived" not in app_cols:
            conn.exec_driver_sql(
                "ALTER TABLE applications ADD COLUMN archived INTEGER NOT NULL DEFAULT 0"
            )


@contextmanager
def get_session() -> Iterator[Session]:
    session = Session(engine)
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# --- Settings store --------------------------------------------------------------

def get_setting(session: Session, key: str) -> Optional[str]:
    row = session.get(Setting, key)
    return row.value if row else None


def set_setting(session: Session, key: str, value: str) -> None:
    row = session.get(Setting, key)
    if row:
        row.value = value
    else:
        row = Setting(key=key, value=value)
    session.add(row)


# --- Send accounting (used by the anti-ban algorithm and quota display) ----------

def sent_today(session: Session, day: Optional[date] = None) -> int:
    """Count send_log rows with outcome 'sent' whose ts falls on `day` (default today)."""
    day = day or datetime.now().date()
    prefix = day.isoformat()  # ts stored as ISO; date prefix matches the calendar day
    stmt = (
        select(func.count())
        .select_from(SendLog)
        .where(SendLog.outcome == "sent")
        .where(SendLog.ts.like(f"{prefix}%"))
    )
    return int(session.exec(stmt).one())


def first_send_date(session: Session) -> Optional[date]:
    """Calendar date of the earliest successful send, or None if none yet."""
    stmt = select(func.min(SendLog.ts)).where(SendLog.outcome == "sent")
    earliest = session.exec(stmt).one()
    if not earliest:
        return None
    try:
        return datetime.fromisoformat(earliest).date()
    except ValueError:
        return date.fromisoformat(earliest[:10])


def send_outcomes(session: Session, since: Optional[str] = None) -> list[str]:
    """send_log outcome strings (for rolling bounce-rate). If `since` (an ISO
    timestamp) is given, only outcomes logged at/after it are returned — used so a
    Resume starts a fresh bounce window and old bounces stop blocking new sends."""
    stmt = select(SendLog.outcome)
    if since:
        stmt = stmt.where(SendLog.ts >= since)
    return [o for o in session.exec(stmt).all() if o]


def send_budget(session: Session) -> dict:
    """Today's ramp cap, sends used, and remaining budget."""
    cap = ramp_cap_for_today(
        first_send_date(session), datetime.now().date(), settings.daily_cap
    )
    used = sent_today(session)
    return {"cap_today": cap, "sent_today": used, "remaining": max(0, cap - used)}


# --- State for the dashboard -----------------------------------------------------

def get_state(session: Session) -> dict:
    """Applications table joined with company/contact, plus the send budget.

    Quota counters for CSE/Hunter are filled in by their integrations in later
    slices; here we return the send budget and an empty applications list when the
    DB is fresh.
    """
    rows = []
    stmt = select(Application, Company, Contact).join(
        Company, Application.company_id == Company.id, isouter=True
    ).join(Contact, Application.contact_id == Contact.id, isouter=True)
    for app, company, contact in session.exec(stmt).all():
        rows.append(
            {
                "id": app.id,
                "company": company.name if company else None,
                "role": company.role_title if company else None,
                "status": app.status,
                "last_checked_at": app.last_checked_at,
                "reply_excerpt": app.reply_excerpt,
                "email": contact.email if contact else None,
            }
        )
    return {"applications": rows, "send_budget": send_budget(session)}


def get_applications_full(session: Session) -> dict:
    """Full detail of every application + a status summary (for the tracker page)."""
    stmt = (
        select(Application, Company, Contact)
        .join(Company, Application.company_id == Company.id, isouter=True)
        .join(Contact, Application.contact_id == Contact.id, isouter=True)
    )
    rows = []
    for app, company, contact in session.exec(stmt).all():
        rows.append(
            {
                "id": app.id,
                "company": company.name if company else None,
                "role": company.role_title if company else None,
                "domain": company.domain if company else None,
                "location": company.location if company else None,
                "salary": company.salary if company else None,
                "email": contact.email if contact else None,
                "apply_url": contact.apply_url if contact else None,
                "verified": bool(contact.verified) if contact else False,
                "source": contact.source if contact else None,
                "status": app.status,
                "subject": app.email_subject,
                "sent_at": app.sent_at,
                "last_checked_at": app.last_checked_at,
                "reply_excerpt": app.reply_excerpt,
                "thread_id": app.gmail_thread_id,
                "apply_kind": app.apply_kind,
                "archived": bool(app.archived),
            }
        )
    # newest activity first
    rows.sort(key=lambda r: (r["sent_at"] or "", r["id"] or 0), reverse=True)

    def n(status):
        return sum(1 for r in rows if r["status"] == status)

    summary = {
        "total": len(rows),
        "applied": sum(1 for r in rows if r["sent_at"]),
        "awaiting": n("sent"),
        "interview": n("replied_interview"),
        "rejection": n("replied_rejection"),
        "needinfo": n("replied_needinfo"),
        "auto_ack": n("auto_ack"),
        "bounced": n("bounced"),
        "no_reply": n("no_reply"),
        "drafted": n("drafted"),
        "email_found": n("email_found"),
        "discovered": n("discovered"),
    }
    summary["replies"] = (
        summary["interview"] + summary["rejection"] + summary["needinfo"]
    )
    return {"applications": rows, "summary": summary}


# --- Discovery repository helpers (Slice 1) --------------------------------------

def find_company_by_source_url(session: Session, url: str) -> Optional[Company]:
    if not url:
        return None
    return session.exec(
        select(Company).where(Company.source_url == url)
    ).first()


def find_company_by_domain_role(
    session: Session, domain: Optional[str], role: Optional[str]
) -> Optional[Company]:
    if not domain or not role:
        return None
    return session.exec(
        select(Company)
        .where(func.lower(Company.domain) == domain.lower())
        .where(func.lower(Company.role_title) == role.lower())
    ).first()


def create_company(session: Session, **fields) -> Company:
    company = Company(**fields)
    session.add(company)
    session.flush()  # assign company.id within the transaction
    return company


def create_application(
    session: Session, company_id: int, status: str = "discovered"
) -> Application:
    app = Application(company_id=company_id, status=status)
    session.add(app)
    session.flush()
    return app


# --- Contact repository helpers (Slice 2) ----------------------------------------

def applications_needing_contact(session: Session) -> list:
    """(Application, Company) pairs that don't yet have a contact resolved."""
    stmt = (
        select(Application, Company)
        .join(Company, Application.company_id == Company.id)
        .where(Application.contact_id.is_(None))
    )
    return list(session.exec(stmt).all())


def applications_needing_email(session: Session):
    """(Application, Company, Contact|None) for COLD-DISCOVERY apps with no email yet —
    either no contact, or a contact whose email is NULL (an email-less portal-apply
    contact). Excludes referral form/email jobs (apply_kind set), which route their
    own way and must not consume Hunter quota. Used to back-fill HR emails via Hunter."""
    stmt = (
        select(Application, Company, Contact)
        .join(Company, Application.company_id == Company.id)
        .join(Contact, Application.contact_id == Contact.id, isouter=True)
        .where(Application.apply_kind.is_(None))
        .where((Application.contact_id.is_(None)) | (Contact.email.is_(None)))
    )
    return list(session.exec(stmt).all())


def create_contact(session: Session, **fields) -> Contact:
    contact = Contact(**fields)
    session.add(contact)
    session.flush()
    return contact


def find_contact_by_email(session: Session, email: Optional[str]) -> Optional[Contact]:
    if not email:
        return None
    return session.exec(
        select(Contact).where(func.lower(Contact.email) == email.lower())
    ).first()


def applications_unverified_pending(session: Session) -> list:
    """(Application, Company, Contact) with an email never run through verification.

    Targets imported contacts (verified=0, confidence IS NULL) so that ② Find
    Contacts verifies them via Hunter, without re-charging already-attempted ones.
    """
    stmt = (
        select(Application, Company, Contact)
        .join(Company, Application.company_id == Company.id)
        .join(Contact, Application.contact_id == Contact.id)
        .where(Contact.email.is_not(None))
        .where(Contact.verified == 0)
        .where(Contact.confidence.is_(None))
    )
    return list(session.exec(stmt).all())


def link_contact(
    session: Session, application: Application, contact_id: int, status: Optional[str]
) -> None:
    application.contact_id = contact_id
    if status:
        application.status = status
    session.add(application)


# --- Drafting repository helpers (Slice 3) ---------------------------------------

def applications_for_drafting(session: Session) -> list:
    """(Application, Company) pairs ready to draft (status 'email_found')."""
    stmt = (
        select(Application, Company)
        .join(Company, Application.company_id == Company.id)
        .where(Application.status == "email_found")
    )
    return list(session.exec(stmt).all())


def existing_bodies(session: Session) -> list[str]:
    """All non-empty stored email bodies (for the duplicate-body hash check)."""
    stmt = select(Application.email_body).where(Application.email_body.is_not(None))
    return [b for b in session.exec(stmt).all() if b]


def update_draft(
    session: Session, application: Application, subject: str, body: str
) -> None:
    application.email_subject = subject
    application.email_body = body
    application.status = "drafted"
    session.add(application)


def drafts_for_review(session: Session) -> list[dict]:
    """Drafts awaiting approval, with company/contact context (FR-15)."""
    stmt = (
        select(Application, Company, Contact)
        .join(Company, Application.company_id == Company.id)
        .join(Contact, Application.contact_id == Contact.id, isouter=True)
        .where(Application.status.in_(["drafted", "approved"]))
    )
    out = []
    for app, company, contact in session.exec(stmt).all():
        out.append(
            {
                "id": app.id,
                "company": company.name if company else None,
                "role": company.role_title if company else None,
                "to": contact.email if contact else None,
                "verified": bool(contact.verified) if contact else False,
                "subject": app.email_subject,
                "body": app.email_body,
                "status": app.status,
            }
        )
    return out


# --- Sender repository helpers (Slice 4) -----------------------------------------

def approved_applications(session: Session, ids: list[int]) -> list:
    """(Application, Company, Contact) for the given ids that are drafted/approved."""
    if not ids:
        return []
    stmt = (
        select(Application, Company, Contact)
        .join(Company, Application.company_id == Company.id)
        .join(Contact, Application.contact_id == Contact.id, isouter=True)
        .where(Application.id.in_(ids))
        .where(Application.status.in_(["drafted", "approved"]))
    )
    return list(session.exec(stmt).all())


def duplicate_within(
    session: Session, company_id: int, window_days: int, exclude_app_id: Optional[int] = None
) -> bool:
    """True if any other application to this company was sent within the window."""
    cutoff = (datetime.now() - timedelta(days=window_days)).isoformat()
    stmt = (
        select(func.count())
        .select_from(Application)
        .where(Application.company_id == company_id)
        .where(Application.status == "sent")
        .where(Application.sent_at.is_not(None))
        .where(Application.sent_at >= cutoff)
    )
    if exclude_app_id is not None:
        stmt = stmt.where(Application.id != exclude_app_id)
    return int(session.exec(stmt).one()) > 0


def record_send(
    session: Session,
    application: Application,
    thread_id: str,
    message_id: str,
    outcome: str = "sent",
    detail: str = "",
) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    application.status = "sent"
    application.gmail_thread_id = thread_id
    application.gmail_message_id = message_id
    application.sent_at = now
    session.add(application)
    session.add(SendLog(application_id=application.id, ts=now, outcome=outcome, detail=detail))


def record_skip(session: Session, application_id: int, reason: str) -> None:
    session.add(
        SendLog(
            application_id=application_id,
            ts=datetime.now().isoformat(timespec="seconds"),
            outcome="skipped",
            detail=reason,
        )
    )


def record_error(session: Session, application_id: int, detail: str) -> None:
    session.add(
        SendLog(
            application_id=application_id,
            ts=datetime.now().isoformat(timespec="seconds"),
            outcome="error",
            detail=detail,
        )
    )


def is_paused(session: Session) -> bool:
    return get_setting(session, "sending_paused") == "1"


def set_paused(session: Session, paused: bool) -> None:
    set_setting(session, "sending_paused", "1" if paused else "0")


def get_flag(session: Session, key: str) -> bool:
    return get_setting(session, key) == "1"


def set_flag(session: Session, key: str, value: bool) -> None:
    set_setting(session, key, "1" if value else "0")


# --- Reply-scan repository helpers (Slice 5) -------------------------------------

def applications_with_thread(session: Session) -> list:
    """(Application, Company) for every application that has a Gmail thread id."""
    stmt = (
        select(Application, Company)
        .join(Company, Application.company_id == Company.id)
        .where(Application.gmail_thread_id.is_not(None))
    )
    return list(session.exec(stmt).all())


def update_reply(
    session: Session,
    application: Application,
    status: Optional[str],
    excerpt: Optional[str],
) -> None:
    if status:
        application.status = status
    if excerpt is not None:
        application.reply_excerpt = excerpt[:200]
    application.last_checked_at = datetime.now().isoformat(timespec="seconds")
    session.add(application)


def record_bounce(session: Session, application: Application) -> bool:
    """Mark an application bounced and log it once for the rolling bounce-rate calc.

    Idempotent: a bounce/NDR email lingers in the inbox and is re-seen on every scan,
    so we log the 'bounced' outcome only ONCE per application — otherwise repeat scans
    inflate the rolling bounce-rate and trigger false auto-pauses. Returns True if this
    was a new bounce, False if already recorded.
    """
    now = datetime.now().isoformat(timespec="seconds")
    application.last_checked_at = now
    already = session.exec(
        select(SendLog)
        .where(SendLog.application_id == application.id)
        .where(SendLog.outcome == "bounced")
    ).first()
    if already:
        session.add(application)
        return False
    application.status = "bounced"
    session.add(application)
    session.add(SendLog(application_id=application.id, ts=now, outcome="bounced", detail="bounce"))
    return True


def touch_last_checked(session: Session) -> None:
    set_setting(session, "last_scan_at", datetime.now().isoformat(timespec="seconds"))


# --- Referral digest + Google-Forms auto-apply -----------------------------------

def _referral_source_url(job: dict) -> str:
    """A stable dedupe key per referral job.

    Forms key on URL **+ role**: one digest often points several distinct roles at
    the same generic application form, and we surface each (the operator reviews and
    can skip redundant ones) rather than silently dropping roles.
    """
    role = job.get("role", "").lower()
    if job.get("apply_kind") == "form" and job.get("apply_url"):
        return f"{job['apply_url'].split('?')[0].lower()}::{role}"  # ignore ?usp=... variants
    if job.get("apply_kind") == "email" and job.get("apply_email"):
        return f"mailto:{job['apply_email'].lower()}:{job.get('apply_subject','').lower()}"
    return f"referral://{job.get('company','').lower()}/{role}"


def ingest_referral_jobs(session: Session, jobs: list[dict]) -> dict:
    """Create company/contact/application rows for parsed referral jobs.

    form  -> status 'form_found' (browser form-filler)
    email -> status 'email_found' (existing draft/send pipeline)
    manual-> status 'discovered'  (operator handles)
    Dedupes by a per-job source key so re-pasting the same digest is safe.
    """
    summary = {"added": 0, "skipped": 0, "forms": 0, "emails": 0, "manual": 0}
    for job in jobs:
        source_url = _referral_source_url(job)
        if find_company_by_source_url(session, source_url):
            summary["skipped"] += 1
            continue
        kind = job.get("apply_kind", "manual")
        status = {"form": "form_found", "email": "email_found"}.get(kind, "discovered")
        company = create_company(
            session,
            name=job.get("company") or "Unknown",
            role_title=job.get("role") or "Role",
            location=job.get("location") or None,
            salary=job.get("stipend") or None,
            source_url=source_url,
            remote=1 if (job.get("location", "").strip().lower() == "remote") else 0,
            discovered_at=datetime.now().isoformat(timespec="seconds"),
        )
        contact = create_contact(
            session,
            company_id=company.id,
            email=(job.get("apply_email") or None) if kind == "email" else None,
            apply_url=(job.get("apply_url") or None) if kind == "form" else None,
            source="referral",
            # A referral digest is an operator-trusted source, so its emails are
            # treated as verified (they pass the verify-before-send guard).
            verified=1 if kind == "email" else 0,
            confidence=1.0 if kind == "email" else None,
            created_at=datetime.now().isoformat(timespec="seconds"),
        )
        app = Application(
            company_id=company.id,
            contact_id=contact.id,
            status=status,
            apply_kind=kind,
            form_url=job.get("apply_url") or None if kind == "form" else None,
            email_subject=job.get("apply_subject") or None if kind == "email" else None,
            email_cc=job.get("apply_cc") or None if kind == "email" else None,
        )
        session.add(app)
        summary["added"] += 1
        summary[{"form": "forms", "email": "emails"}.get(kind, "manual")] += 1
    return summary


def form_applications_pending(session: Session) -> list:
    """(Application, Company, Contact) for form jobs awaiting fill (found or errored)."""
    stmt = (
        select(Application, Company, Contact)
        .join(Company, Application.company_id == Company.id)
        .join(Contact, Application.contact_id == Contact.id, isouter=True)
        .where(Application.apply_kind == "form")
        .where(Application.status.in_(["form_found", "form_error"]))
    )
    return list(session.exec(stmt).all())


def form_applications_unsubmitted(session: Session) -> list:
    """(Application, Company, Contact) for every form job not yet submitted."""
    stmt = (
        select(Application, Company, Contact)
        .join(Company, Application.company_id == Company.id)
        .join(Contact, Application.contact_id == Contact.id, isouter=True)
        .where(Application.apply_kind == "form")
        .where(Application.status != "form_submitted")
    )
    return list(session.exec(stmt).all())


def form_application(session: Session, app_id: int):
    """(Application, Company) for one form job, or None."""
    stmt = (
        select(Application, Company)
        .join(Company, Application.company_id == Company.id)
        .where(Application.id == app_id)
        .where(Application.apply_kind == "form")
    )
    return session.exec(stmt).first()


def save_form_fill(
    session: Session, application: Application, answers_json: str,
    screenshot: Optional[str], status: str, note: str = "",
) -> None:
    application.form_answers = answers_json
    application.form_screenshot = screenshot
    application.status = status
    application.form_note = note
    application.last_checked_at = datetime.now().isoformat(timespec="seconds")
    session.add(application)


def mark_form_submitted(session: Session, application: Application, note: str = "") -> None:
    now = datetime.now().isoformat(timespec="seconds")
    application.status = "form_submitted"
    application.sent_at = now
    application.form_note = note
    session.add(application)
    session.add(SendLog(application_id=application.id, ts=now, outcome="form_submitted", detail=note))


def linkedin_jobs(session: Session, limit: int = 10,
                  statuses: list[str] | None = None) -> list[dict]:
    """Discovered LinkedIn jobs eligible for an apply flow: cold-discovery rows
    (apply_kind IS NULL) with a LinkedIn posting URL, not yet LinkedIn-applied or archived.
    `statuses` selects which application states to include (default the unapplied queue);
    the hard-apply flow passes li_external too. Returns [{id, url, company, role, ...}]."""
    statuses = statuses or ["discovered", "email_found"]
    stmt = (
        select(Application, Company)
        .join(Company, Application.company_id == Company.id)
        .where(Application.apply_kind.is_(None))
        .where((Application.archived == 0) | (Application.archived.is_(None)))
        .where(Company.source_url.like("%linkedin.com/jobs%"))
        .where(Application.status.in_(statuses))
    )
    out = []
    for app, company in session.exec(stmt).all():
        out.append({"id": app.id, "url": company.source_url,
                    "company": company.name, "role": company.role_title,
                    "location": company.location,
                    "urgent": int(company.urgent or 0)})
    # Urgent/immediate-hiring posts first, then plain "hiring" shouts, then newest.
    out.sort(key=lambda r: (r["urgent"], r["id"]), reverse=True)
    return out[:limit]


def set_li_status(session: Session, app_id: int, status: str, note: str) -> None:
    """Record the outcome of an assisted LinkedIn apply on an application."""
    app = session.get(Application, app_id)
    if not app:
        return
    app.status = status
    app.form_note = note
    app.last_checked_at = datetime.now().isoformat(timespec="seconds")
    session.add(app)


def archive_application(session: Session, app_id: int) -> Optional[str]:
    """Hide an application from active panels but KEEP it as history (non-destructive).
    Returns the company name (for the confirmation message) or None if not found."""
    app = session.get(Application, app_id)
    if not app:
        return None
    app.archived = 1
    session.add(app)
    company = session.get(Company, app.company_id) if app.company_id else None
    return company.name if company else "item"


def delete_application_cascade(session: Session, app_id: int) -> Optional[str]:
    """Remove an application + its contact, and the company if nothing else uses it.
    Returns the company name (for the confirmation message) or None if not found."""
    app = session.get(Application, app_id)
    if not app:
        return None
    cid, contact_id = app.company_id, app.contact_id
    company = session.get(Company, cid) if cid else None
    name = company.name if company else "item"
    for sl in session.exec(select(SendLog).where(SendLog.application_id == app_id)).all():
        session.delete(sl)
    session.delete(app)
    session.flush()
    if contact_id:
        contact = session.get(Contact, contact_id)
        if contact:
            session.delete(contact)
    if cid:
        other = session.exec(select(Application).where(Application.company_id == cid)).first()
        if not other and company:
            session.delete(company)
    return name


def list_form_jobs(session: Session) -> list[dict]:
    """All form-apply jobs with fill/review state, for the dashboard panel."""
    stmt = (
        select(Application, Company)
        .join(Company, Application.company_id == Company.id)
        .where(Application.apply_kind == "form")
        .where((Application.archived == 0) | (Application.archived.is_(None)))
    )
    out = []
    for app, company in session.exec(stmt).all():
        out.append({
            "id": app.id,
            "company": company.name,
            "role": company.role_title,
            "stipend": company.salary,
            "location": company.location,
            "form_url": app.form_url,
            "status": app.status,
            "screenshot": app.form_screenshot,
            "note": app.form_note,
            "answers": app.form_answers,
            "prefill_url": app.form_prefill_url,
        })
    out.sort(key=lambda r: r["id"], reverse=True)
    return out


def save_prefill(session: Session, app_id: int, url: str, answers_json: str, note: str) -> None:
    app = session.get(Application, app_id)
    if app:
        app.form_prefill_url = url
        app.form_answers = answers_json
        app.form_note = note
        if app.status == "form_found":
            app.status = "form_filled"
        session.add(app)
