"""Reply-scanning stage (FR-22..FR-26, Technical-Spec §5.5 / §7.2).

Reads new inbound mail, matches each message to an application by gmail_thread_id,
classifies the reply via Groq, updates status + reply_excerpt + last_checked_at, and
applies the Gmail label. Bounce messages (from mailer-daemon/postmaster) set status
`bounced` and feed the rolling bounce-rate. Runs on demand (button ⑤) and hourly via
APScheduler. In DRY_RUN, a deterministic synthetic inbox is generated for sent
applications so classification + status updates are testable without Gmail/Groq.
"""
from __future__ import annotations

from ..config import settings
from ..db import (
    applications_with_thread,
    get_session,
    record_bounce,
    touch_last_checked,
    update_reply,
)
from ..integrations import gmail, groq_client
from ..logging_setup import log_event
from ..prompts import CLASSIFY_STATUS_MAP, CLASSIFY_SYSTEM

_BOUNCE_SENDERS = ("mailer-daemon", "postmaster")

# Deterministic synthetic replies (DRY_RUN), cycled across sent applications.
_SIM_REPLIES = [
    ("INTERVIEW", "Thanks for applying! We'd love to schedule an interview call next week. Please share your availability."),
    ("REJECTION", "Thank you for your interest. Unfortunately we have decided not to move forward with your application at this time."),
    ("NEEDINFO", "Could you please provide your latest resume and share details of your notice period?"),
    ("AUTO_ACK", "This is an automated message: we have received your application and will be in touch."),
]


def _classify(text: str) -> str:
    """Return one of INTERVIEW/REJECTION/NEEDINFO/AUTO_ACK/OTHER."""
    if settings.dry_run:
        return _classify_offline(text)
    token = groq_client.chat(CLASSIFY_SYSTEM, text[:1500], temperature=0.0, max_tokens=4)
    token = (token or "OTHER").strip().upper().split()[0]
    return token if token in CLASSIFY_STATUS_MAP else "OTHER"


def _classify_offline(text: str) -> str:
    low = text.lower()
    if any(k in low for k in ("interview", "schedule", "call next", "availability")):
        return "INTERVIEW"
    if any(k in low for k in ("unfortunately", "not move forward", "not moving", "regret")):
        return "REJECTION"
    if any(k in low for k in ("provide", "share your", "resume and", "notice period")):
        return "NEEDINFO"
    if any(k in low for k in ("received your application", "automated message")):
        return "AUTO_ACK"
    return "OTHER"


def _simulate_inbox(apps_by_thread: dict) -> list[dict]:
    """Build a deterministic synthetic inbox for sent (not-yet-replied) applications."""
    msgs = []
    idx = 0
    for thread_id, (app, company) in apps_by_thread.items():
        if app.status != "sent":
            continue
        _, text = _SIM_REPLIES[idx % len(_SIM_REPLIES)]
        msgs.append(
            {
                "id": f"sim-{app.id}",
                "thread_id": thread_id,
                "from": f"recruiting@{company.domain or 'example.com'}",
                "subject": f"Re: {app.email_subject or 'Application'}",
                "text": text,
            }
        )
        idx += 1
    return msgs


def _is_bounce(sender: str) -> bool:
    low = (sender or "").lower()
    return any(b in low for b in _BOUNCE_SENDERS)


def scan() -> dict:
    """Scan replies once. Returns a summary for the UI."""
    summary = {
        "checked": 0,
        "classified": 0,
        "bounced": 0,
        "interview": 0,
        "rejection": 0,
        "needinfo": 0,
        "auto_ack": 0,
        "dry_run": settings.dry_run,
        "message": "",
    }

    with get_session() as session:
        threaded = applications_with_thread(session)
        apps_by_thread = {app.gmail_thread_id: (app, company) for app, company in threaded}

        if settings.dry_run:
            messages = _simulate_inbox(apps_by_thread)
            label_id = None
        else:
            label_id = gmail.ensure_label(settings.gmail_label)
            window = max(1, settings.scan_interval_minutes // (60 * 24)) or 7
            messages = [
                gmail.get_message(m["id"])
                for m in gmail.list_messages(f"newer_than:{max(window, 1)}d -from:me")
            ]

        for msg in messages:
            match = apps_by_thread.get(msg.get("thread_id"))
            if not match:
                continue
            app, _company = match
            summary["checked"] += 1

            if _is_bounce(msg.get("from", "")):
                if record_bounce(session, app):  # only count NEW bounces (idempotent)
                    summary["bounced"] += 1
                    log_event("scan", msg.get("from", ""), "bounced", f"app={app.id}")
                continue

            token = _classify(msg.get("text", ""))
            status = CLASSIFY_STATUS_MAP.get(token)  # None -> keep 'sent'
            excerpt = (msg.get("text", "") or "").strip().replace("\n", " ")
            update_reply(session, app, status, excerpt)
            summary["classified"] += 1
            for key, tok in (
                ("interview", "INTERVIEW"),
                ("rejection", "REJECTION"),
                ("needinfo", "NEEDINFO"),
                ("auto_ack", "AUTO_ACK"),
            ):
                if token == tok:
                    summary[key] += 1

            if not settings.dry_run and label_id:
                try:
                    gmail.add_label(msg["id"], label_id)
                except Exception as e:  # noqa: BLE001
                    log_event("scan", msg.get("id", ""), "label_error", str(e))

            log_event("scan", msg.get("from", ""), "classified", f"{token} app={app.id}")

        touch_last_checked(session)

    note = " (DRY RUN - simulated inbox)" if settings.dry_run else ""
    summary["message"] = (
        f"Scanned {summary['checked']} reply(ies): {summary['interview']} interview, "
        f"{summary['rejection']} rejection, {summary['needinfo']} needinfo, "
        f"{summary['auto_ack']} auto-ack, {summary['bounced']} bounced.{note}"
    )
    log_event("scan", "batch", "ok", summary["message"])
    return summary


def scan_job() -> None:
    """APScheduler entry point (hourly). Never raises into the scheduler."""
    try:
        scan()
    except Exception as e:  # noqa: BLE001
        log_event("scan", "scheduler", "error", str(e))
