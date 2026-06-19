"""Gmail API integration: OAuth, send (with PDF attach), list/read, label.

Auth uses InstalledAppFlow from credentials.json (Desktop client); the token is
cached in token.json. Google libraries are imported lazily inside _get_service so
DRY_RUN runs need no credentials and no google packages loaded. In DRY_RUN, send()
returns synthetic ids and logs what it WOULD send — nothing leaves the machine.
"""
from __future__ import annotations

import base64
import os
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from .. import config
from ..config import settings
from ..logging_setup import log_event

_service = None


def _get_service():
    """Build (and cache) an authorized Gmail service. Real mode only."""
    global _service
    if _service is not None:
        return _service

    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    creds = None
    if config.TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(
            str(config.TOKEN_PATH), config.GMAIL_SCOPES
        )
    if not creds or not creds.valid:
        refreshed = False
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                refreshed = True
            except RefreshError as e:
                # Refresh token revoked/expired (e.g. OAuth app in "Testing" mode
                # expires tokens after 7 days). Fall back to a fresh interactive
                # login instead of 500-crashing the request.
                log_event("gmail", "auth", "refresh_failed", str(e)[:200])
                creds = None
        if not refreshed:
            flow = InstalledAppFlow.from_client_secrets_file(
                str(config.CREDENTIALS_PATH), config.GMAIL_SCOPES
            )
            creds = flow.run_local_server(port=0)
        config.TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")

    _service = build("gmail", "v1", credentials=creds)
    return _service


def auth_status() -> str:
    """'ok' | 'needs_login' | 'unconfigured' — a fast check that NEVER launches the
    interactive login (so the dashboard can show connection health proactively)."""
    if not config.CREDENTIALS_PATH.exists():
        return "unconfigured"
    if not config.TOKEN_PATH.exists():
        return "needs_login"
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials

        creds = Credentials.from_authorized_user_file(str(config.TOKEN_PATH), config.GMAIL_SCOPES)
        if creds and creds.valid:
            return "ok"
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())  # silent refresh
            config.TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
            return "ok"
        return "needs_login"
    except Exception:  # noqa: BLE001
        return "needs_login"


def _build_mime(to: str, subject: str, body: str, attach_path: str | None,
                cc: str | None = None) -> MIMEMultipart:
    msg = MIMEMultipart()
    msg["To"] = to
    if cc:
        msg["Cc"] = cc
    msg["From"] = settings.sender_name
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain", "utf-8"))
    if attach_path and os.path.exists(attach_path):
        with open(attach_path, "rb") as fh:
            part = MIMEApplication(fh.read(), _subtype="pdf")
        part.add_header(
            "Content-Disposition", "attachment", filename=os.path.basename(attach_path)
        )
        msg.attach(part)
    return msg


def send(to: str, subject: str, body: str, attach_path: str | None = None,
         cc: str | None = None) -> dict:
    """Send one email with the CV attached. Returns {thread_id, message_id}.

    Honors DRY_RUN (no network, synthetic ids). The CV attachment is required for
    real sends (FR-13): a missing CV raises so the sender records an error instead
    of sending a link-less, attachment-less email. `cc` adds a Cc recipient.
    """
    if settings.dry_run:
        synthetic = f"dryrun-{abs(hash((to, subject))) % 10_000_000}"
        cv_note = "cv-ok" if (attach_path and os.path.exists(attach_path)) else "cv-MISSING"
        cc_note = f" cc={cc}" if cc else ""
        log_event("send", to, "dry_run", f"would send '{subject}'{cc_note} [{cv_note}]")
        return {"thread_id": f"thread-{synthetic}", "message_id": f"msg-{synthetic}"}

    if not attach_path or not os.path.exists(attach_path):
        raise FileNotFoundError(f"CV not found at {attach_path!r}; refusing to send without it")

    msg = _build_mime(to, subject, body, attach_path, cc=cc)
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    sent = _get_service().users().messages().send(userId="me", body={"raw": raw}).execute()
    log_event("send", to, "sent", f"thread={sent.get('threadId')}")
    return {"thread_id": sent.get("threadId"), "message_id": sent.get("id")}


# --- Reply scanning support (Slice 5) --------------------------------------------

def list_messages(query: str) -> list[dict]:
    """Return [{id, threadId}, ...] for a Gmail search query. Real mode only."""
    resp = (
        _get_service()
        .users()
        .messages()
        .list(userId="me", q=query)
        .execute()
    )
    return resp.get("messages", []) or []


def get_message(msg_id: str) -> dict:
    """Fetch a full message and return {thread_id, from, subject, text}."""
    m = (
        _get_service()
        .users()
        .messages()
        .get(userId="me", id=msg_id, format="full")
        .execute()
    )
    headers = {h["name"].lower(): h["value"] for h in m.get("payload", {}).get("headers", [])}
    return {
        "id": m.get("id"),
        "thread_id": m.get("threadId"),
        "from": headers.get("from", ""),
        "subject": headers.get("subject", ""),
        "text": _extract_text(m.get("payload", {})),
    }


def _extract_text(payload: dict) -> str:
    """Pull the plain-text body out of a Gmail message payload."""
    if payload.get("mimeType") == "text/plain":
        data = payload.get("body", {}).get("data")
        if data:
            return base64.urlsafe_b64decode(data).decode("utf-8", "ignore")
    for part in payload.get("parts", []) or []:
        text = _extract_text(part)
        if text:
            return text
    return ""


def ensure_label(name: str) -> str:
    """Return the label id for `name`, creating it if needed. Real mode only."""
    svc = _get_service()
    existing = svc.users().labels().list(userId="me").execute().get("labels", [])
    for lab in existing:
        if lab["name"].lower() == name.lower():
            return lab["id"]
    created = (
        svc.users()
        .labels()
        .create(userId="me", body={"name": name})
        .execute()
    )
    return created["id"]


def add_label(msg_id: str, label_id: str) -> None:
    _get_service().users().messages().modify(
        userId="me", id=msg_id, body={"addLabelIds": [label_id]}
    ).execute()
