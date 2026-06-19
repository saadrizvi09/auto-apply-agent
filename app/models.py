"""SQLModel data models — mirror the SQLite schema in Technical-Spec §2.

status enum (applications.status):
  discovered -> email_found -> drafted -> approved -> sent ->
  {replied_interview | replied_rejection | replied_needinfo | auto_ack
   | bounced | no_reply}
"""
from __future__ import annotations

from typing import Optional

from sqlmodel import Field, SQLModel


class Company(SQLModel, table=True):
    __tablename__ = "companies"

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    domain: Optional[str] = None
    source_url: Optional[str] = Field(default=None, unique=True)  # posting URL (dedupe key)
    role_title: Optional[str] = None
    location: Optional[str] = None
    salary: Optional[str] = None
    remote: int = 0
    headcount: Optional[str] = None        # Hunter employee-range, e.g. "11-50" (startup signal)
    urgent: int = 0                        # 2 = urgent/immediate hiring post, 1 = "hiring" shout, 0 = none
    discovered_at: Optional[str] = None


class Contact(SQLModel, table=True):
    __tablename__ = "contacts"

    id: Optional[int] = Field(default=None, primary_key=True)
    company_id: Optional[int] = Field(default=None, foreign_key="companies.id")
    email: Optional[str] = None
    apply_url: Optional[str] = None        # set when apply is via portal, not email
    source: Optional[str] = None           # posting | scraped | pattern
    verified: int = 0
    confidence: Optional[float] = None
    created_at: Optional[str] = None


class Application(SQLModel, table=True):
    __tablename__ = "applications"

    id: Optional[int] = Field(default=None, primary_key=True)
    company_id: Optional[int] = Field(default=None, foreign_key="companies.id")
    contact_id: Optional[int] = Field(default=None, foreign_key="contacts.id")
    status: Optional[str] = None
    email_subject: Optional[str] = None
    email_cc: Optional[str] = None          # extra Cc recipient (from referral digests)
    email_body: Optional[str] = None
    gmail_thread_id: Optional[str] = None
    gmail_message_id: Optional[str] = None
    sent_at: Optional[str] = None
    last_checked_at: Optional[str] = None
    reply_excerpt: Optional[str] = None
    # Google-Forms auto-apply (referral digests)
    apply_kind: Optional[str] = None        # email | form | manual
    form_url: Optional[str] = None
    form_answers: Optional[str] = None       # JSON: planned answers for review
    form_screenshot: Optional[str] = None    # filename under form_shots/
    form_note: Optional[str] = None          # last fill/submit note (e.g. needs login)
    form_prefill_url: Optional[str] = None   # pre-filled form link (opens filled in your browser)
    archived: int = 0                        # hidden from active panels but kept as history


class SendLog(SQLModel, table=True):
    __tablename__ = "send_log"

    id: Optional[int] = Field(default=None, primary_key=True)
    application_id: Optional[int] = None
    ts: Optional[str] = None
    outcome: Optional[str] = None          # sent | bounced | skipped | error
    detail: Optional[str] = None


class Setting(SQLModel, table=True):
    __tablename__ = "settings"

    key: str = Field(primary_key=True)
    value: Optional[str] = None
