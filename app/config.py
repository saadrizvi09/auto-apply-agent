"""Configuration loader: reads .env, exposes typed settings.

Behavior values can be overridden at runtime via the `settings` DB table (set
through the UI); `get_setting()` resolves DB-override -> env -> default in that
order. Secrets (API keys, OAuth creds) are env/file only and never DB-stored.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# Project root = parent of the app/ package.
ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

DB_PATH = ROOT / "jobs.db"
LOG_PATH = ROOT / "autoapply.log"
CREDENTIALS_PATH = ROOT / "credentials.json"
TOKEN_PATH = ROOT / "token.json"
STATIC_DIR = ROOT / "static"

# Free-tier limits (for quota display).
CSE_DAILY_LIMIT = 100
HUNTER_MONTHLY_LIMIT = 50

# Gmail OAuth scopes (minimal).
GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
]


def _b(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _i(value: str | None, default: int) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _f(value: str | None, default: float) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class Settings:
    # Safety
    dry_run: bool
    first_send_test_inbox: str
    # Secrets
    groq_api_key: str
    groq_api_key_backup: str
    google_cse_api_key: str
    google_cse_id: str
    hunter_api_key: str
    hunter_api_key_backup: str
    # Behavior
    cv_path: str
    cv_summary: str
    account_created_date: str
    sender_name: str
    signature: str
    daily_cap: int
    warmup_ramp: bool
    li_daily_cap: int          # autonomous LinkedIn auto-apply cap/day
    li_warmup_ramp: bool       # ramp LinkedIn applies up over the first week
    send_delay_min: int
    send_delay_max: int
    bounce_pause_threshold: float
    min_account_age_days: int
    dup_window_days: int
    scan_interval_minutes: int
    gmail_label: str
    require_verified: bool


def load_settings() -> Settings:
    """Load settings from the environment (call once at startup or per request)."""
    return Settings(
        dry_run=_b(os.getenv("DRY_RUN"), True),
        first_send_test_inbox=os.getenv("FIRST_SEND_TEST_INBOX", "").strip(),
        groq_api_key=os.getenv("GROQ_API_KEY", "").strip(),
        # Optional fallback Groq key, used automatically on a 429 rate-limit.
        groq_api_key_backup=os.getenv("GROQ_API_KEY_BACKUP", "").strip(),
        google_cse_api_key=os.getenv("GOOGLE_CSE_API_KEY", "").strip(),
        google_cse_id=os.getenv("GOOGLE_CSE_ID", "").strip(),
        hunter_api_key=os.getenv("HUNTER_API_KEY", "").strip(),
        # Optional 2nd Hunter account key; used automatically when the first's
        # monthly free quota (50) is exhausted.
        hunter_api_key_backup=os.getenv("HUNTER_API_KEY_BACKUP", "").strip(),
        cv_path=os.getenv("CV_PATH", "./cv.pdf").strip(),
        cv_summary=os.getenv(
            "CV_SUMMARY",
            "Final-year CS student; DSA + full-stack (React/Node); 2 internships; "
            "projects in distributed systems and web apps.",
        ).strip(),
        account_created_date=os.getenv("ACCOUNT_CREATED_DATE", "").strip(),
        sender_name=os.getenv("SENDER_NAME", "Your Name").strip(),
        signature=os.getenv("SIGNATURE", "Your Name").strip(),
        daily_cap=_i(os.getenv("DAILY_CAP"), 20),
        # When False, skip the multi-day warm-up ramp and allow DAILY_CAP from day 1.
        warmup_ramp=_b(os.getenv("WARMUP_RAMP"), True),
        li_daily_cap=_i(os.getenv("LI_DAILY_CAP"), 50),
        li_warmup_ramp=_b(os.getenv("LI_WARMUP_RAMP"), True),
        send_delay_min=_i(os.getenv("SEND_DELAY_MIN"), 90),
        send_delay_max=_i(os.getenv("SEND_DELAY_MAX"), 180),
        bounce_pause_threshold=_f(os.getenv("BOUNCE_PAUSE_THRESHOLD"), 0.03),
        min_account_age_days=_i(os.getenv("MIN_ACCOUNT_AGE_DAYS"), 14),
        dup_window_days=_i(os.getenv("DUP_WINDOW_DAYS"), 5),
        scan_interval_minutes=_i(os.getenv("SCAN_INTERVAL_MINUTES"), 60),
        gmail_label=os.getenv("GMAIL_LABEL", "AutoApply").strip(),
        require_verified=_b(os.getenv("REQUIRE_VERIFIED"), True),
    )


# Module-level singleton for convenience; refreshed by reload_settings().
settings = load_settings()


def reload_settings() -> Settings:
    global settings
    load_dotenv(ROOT / ".env", override=True)
    settings = load_settings()
    return settings
