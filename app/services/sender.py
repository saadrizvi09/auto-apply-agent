"""Throttled, capped, safe sender (Technical-Spec §6 — the critical path).

Every anti-ban rule is enforced here, in order:
  - operator approval        : only the application_ids the operator approved are sent
  - sending pause            : a prior auto-pause blocks the whole run until resumed
  - ramping daily cap        : ramp_cap_for_today() minus sent_today()
  - warm-up gate             : account age must reach MIN_ACCOUNT_AGE_DAYS
  - bounce auto-pause        : rolling bounce rate over threshold -> pause + stop
  - verify-before-send       : REQUIRE_VERIFIED blocks unverified recipients
  - portal-apply skip        : contacts with only an apply URL are skipped
  - duplicate guard          : one email per company within DUP_WINDOW_DAYS
  - first-real-send guard    : the first real send must go to FIRST_SEND_TEST_INBOX
  - randomized human spacing : sleep 90-180s (config) between sends
  - PDF attached, never a link (enforced in gmail.send)

Nothing sends without explicit per-email operator approval, and in DRY_RUN nothing
sends at all.
"""
from __future__ import annotations

import random
import time
from datetime import date, datetime

from ..config import settings
from ..db import (
    approved_applications,
    duplicate_within,
    first_send_date,
    get_flag,
    get_session,
    is_paused,
    record_error,
    record_send,
    record_skip,
    send_outcomes,
    sent_today,
    set_flag,
    set_paused,
)
from ..db import get_setting
from ..integrations import gmail, hunter
from ..logic import (
    MIN_BOUNCE_SAMPLE,
    RAMP_HARD_MAX,
    ramp_cap_for_today,
    rolling_bounce_rate,
)
from ..logging_setup import log_event

FIRST_SEND_FLAG = "first_real_send_done"


def account_age_days() -> int | None:
    """Days since the sender account was created, or None if not configured."""
    raw = settings.account_created_date
    if not raw:
        return None
    try:
        created = date.fromisoformat(raw)
    except ValueError:
        return None
    return (date.today() - created).days


def _send_delay_seconds() -> float:
    lo, hi = settings.send_delay_min, settings.send_delay_max
    if hi < lo:
        lo, hi = hi, lo
    return random.uniform(lo, hi)


def _first_send_guard(session, recipient: str) -> str | None:
    """Return a skip reason if this real send is blocked by the first-send guard.

    The first real send must target FIRST_SEND_TEST_INBOX (a secondary inbox the
    operator controls). Once one real send has succeeded, the guard is lifted.
    """
    if get_flag(session, FIRST_SEND_FLAG):
        return None
    inbox = settings.first_send_test_inbox
    if not inbox:
        return "first-send inbox not set (set FIRST_SEND_TEST_INBOX before any real send)"
    if recipient.lower() != inbox.lower():
        return f"first real send must go to FIRST_SEND_TEST_INBOX ({inbox})"
    return None


def send_approved(ids: list[int], allow_unverified: bool = False) -> dict:
    """Send the operator-approved applications, enforcing every anti-ban rule.

    `allow_unverified=True` means the operator explicitly checked unverified
    recipients in Review & Send, so the verify-before-send flag gate is bypassed for
    them. The real mailbox check (Hunter, when quota remains), duplicate guard,
    bounce auto-pause and ramp cap still apply.
    """
    summary = {
        "requested": len(ids),
        "sent": 0,
        "skipped": 0,
        "errors": 0,
        "dry_run": settings.dry_run,
        "stopped": None,
        "skips": [],
        "message": "",
    }

    def _skip(app_id, reason):
        summary["skipped"] += 1
        summary["skips"].append({"id": app_id, "reason": reason})

    with get_session() as session:
        # --- pre-flight gates -----------------------------------------------------
        if is_paused(session):
            summary["stopped"] = "Sending is paused (auto-paused on high bounce rate). Resume to continue."
            summary["message"] = summary["stopped"]
            log_event("send", "batch", "blocked", "paused")
            return summary

        today = datetime.now().date()
        if settings.warmup_ramp:
            cap = ramp_cap_for_today(first_send_date(session), today, settings.daily_cap)
        else:
            # Warm-up disabled by operator: full DAILY_CAP from day 1 (hard-ceiling clamped).
            cap = max(0, min(settings.daily_cap, RAMP_HARD_MAX))
        if sent_today(session) >= cap:
            summary["stopped"] = f"Daily cap reached ({sent_today(session)}/{cap})."
            summary["message"] = summary["stopped"]
            log_event("send", "batch", "blocked", "daily cap")
            return summary

        age = account_age_days()
        if age is None or age < settings.min_account_age_days:
            have = "unknown" if age is None else f"{age}d"
            summary["stopped"] = (
                f"Warm-up gate: account age {have} < required "
                f"{settings.min_account_age_days}d. Set ACCOUNT_CREATED_DATE once the "
                f"account is confirmed aged."
            )
            summary["message"] = summary["stopped"]
            log_event("send", "batch", "blocked", "warm-up gate")
            return summary

        # --- per-email loop -------------------------------------------------------
        for application, company, contact in approved_applications(session, ids):
            if sent_today(session) >= cap:
                summary["stopped"] = f"Daily cap reached ({cap})."
                break

            window_start = get_setting(session, "bounce_window_start")
            rate = rolling_bounce_rate(
                send_outcomes(session, since=window_start), min_sample=MIN_BOUNCE_SAMPLE
            )
            if rate > settings.bounce_pause_threshold:
                set_paused(session, True)
                summary["stopped"] = (
                    f"Auto-paused: bounce rate {rate:.1%} exceeds "
                    f"{settings.bounce_pause_threshold:.0%} threshold. Fix/remove bad "
                    f"addresses, then Resume to continue."
                )
                log_event("send", "batch", "auto_paused", f"rate={rate:.3f}")
                break

            if (
                settings.require_verified
                and not allow_unverified
                and not (contact and contact.verified)
            ):
                _skip(application.id, "unverified recipient")
                record_skip(session, application.id, "unverified")
                continue

            # Real mailbox check before sending. Hunter-discovered emails are flagged
            # 'verified' from SEARCH confidence, not deliverability — that's what
            # bounced. Verify once via Hunter's email-verifier; skip undeliverable
            # addresses (no bounce) and stamp the rest so re-runs don't re-spend quota.
            if (
                settings.require_verified
                and not settings.dry_run
                and contact
                and contact.email
                and (contact.source or "").startswith("hunter")
                and not (contact.source or "").endswith("+v")
                and hunter.remaining_this_month(session) > 0   # only when we CAN verify
            ):
                verdict = hunter.verify(session, contact.email)
                status = verdict["status"]
                if verdict["verified"]:
                    # Confirmed deliverable — stamp so re-runs don't re-spend quota.
                    contact.source = (contact.source or "hunter") + "+v"
                    contact.confidence = round(verdict["confidence"], 3)
                    session.add(contact)
                elif status == "invalid":
                    # Definitively bad mailbox — skip (no bounce) and downgrade.
                    contact.verified = 0
                    session.add(contact)
                    _skip(application.id, "undeliverable (invalid mailbox)")
                    record_skip(session, application.id, "undeliverable")
                    continue
                # else (accept_all / unknown / quota_exhausted / error): can't confirm
                # but not proven bad — trust the search verification and send. Crucially
                # we do NOT downgrade verified, so a transient/quota outcome never blocks
                # future sends.
            if contact and contact.apply_url and not contact.email:
                _skip(application.id, "portal apply (no email)")
                record_skip(session, application.id, "portal apply")
                continue
            if not (contact and contact.email):
                _skip(application.id, "no email address")
                record_skip(session, application.id, "no email")
                continue
            if duplicate_within(
                session, company.id, settings.dup_window_days, exclude_app_id=application.id
            ):
                _skip(application.id, f"duplicate within {settings.dup_window_days} days")
                record_skip(session, application.id, "dup")
                continue

            if not settings.dry_run:
                guard = _first_send_guard(session, contact.email)
                if guard:
                    _skip(application.id, guard)
                    record_skip(session, application.id, guard)
                    continue

            # --- send -------------------------------------------------------------
            try:
                res = gmail.send(
                    to=contact.email,
                    subject=application.email_subject,
                    body=application.email_body,
                    attach_path=settings.cv_path,
                    cc=application.email_cc,
                )
            except Exception as e:  # noqa: BLE001 — record and continue
                summary["errors"] += 1
                record_error(session, application.id, str(e))
                log_event("send", contact.email, "error", str(e))
                continue

            record_send(session, application, res["thread_id"], res["message_id"])
            summary["sent"] += 1
            if not settings.dry_run:
                set_flag(session, FIRST_SEND_FLAG, True)

            delay = _send_delay_seconds()
            log_event("send", contact.email, "sent", f"next in {delay:.0f}s")
            # Human-like spacing. Skipped in DRY_RUN so the build is testable fast.
            if not settings.dry_run:
                time.sleep(delay)

    note = " (DRY RUN - nothing actually sent)" if settings.dry_run else ""
    base = (
        f"Sent {summary['sent']}, skipped {summary['skipped']}, errors {summary['errors']}."
    )
    summary["message"] = (summary["stopped"] + " " if summary["stopped"] else "") + base + note
    log_event("send", "batch", "ok", summary["message"])
    return summary
