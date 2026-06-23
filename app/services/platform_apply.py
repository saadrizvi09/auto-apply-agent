"""Orchestrates autonomous apply on external platforms: Y Combinator Work at a Startup,
Cutshort, and ZipRecruiter.

Thin layer over app/integrations/platforms.py: enforces a conservative per-day cap per
platform (these are ToS-restricted + bot-defended, so volume stays low), handles DRY_RUN,
maps the run outcomes to a clean summary, and records the daily counter. The drivers do the
browser work and fail safe (stop on any captcha/PerimeterX wall, never submit incomplete).
"""
from __future__ import annotations

import os
from datetime import date

from ..config import ROOT, settings
from ..db import get_session, get_setting, set_setting
from ..integrations import platforms
from ..logging_setup import log_event
from ..profile import load_profile


def _cap(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


# Per-day caps, env-overridable. NOTE: Cutshort's own free tier caps ~15 applies/WEEK and
# its captcha fires on high volume — raising CUTSHORT_DAILY_CAP past that just hits their wall.
_CAPS = {
    "yc": _cap("YC_DAILY_CAP", 20),
    "cutshort": _cap("CUTSHORT_DAILY_CAP", 12),
    "ziprecruiter": _cap("ZIP_DAILY_CAP", 6),
    "wellfound": _cap("WELLFOUND_DAILY_CAP", 15),
}
_LABEL = {"yc": "Y Combinator", "cutshort": "Cutshort", "ziprecruiter": "ZipRecruiter",
          "wellfound": "Wellfound"}


def _used_today(session, platform: str) -> int:
    raw = get_setting(session, f"plat_{platform}_{date.today().isoformat()}")
    try:
        return int(raw) if raw else 0
    except ValueError:
        return 0


def _add_used(session, platform: str, n: int) -> None:
    key = f"plat_{platform}_{date.today().isoformat()}"
    set_setting(session, key, str(_used_today(session, platform) + n))


def status() -> dict:
    """Light status for the dashboard: whether a login session likely exists + today's
    per-platform counts. (A deep login check opens a browser, so it's not done here.)"""
    with get_session() as session:
        used = {p: _used_today(session, p) for p in _CAPS}
    return {
        "session": (ROOT / ".browser_profile").exists(),
        "caps": _CAPS,
        "used_today": used,
    }


def autoapply(platform: str, query: str = "", location: str = "", remote: bool = True,
              limit: int | None = None) -> dict:
    """Run an autonomous apply batch on one platform up to its daily cap.

    `limit` further caps this single run (e.g. 1 for a watched first-run validation),
    without changing the daily cap."""
    if platform not in _CAPS:
        return {"ok": False, "message": f"Unknown platform '{platform}'."}
    profile = load_profile()
    label = _LABEL[platform]

    with get_session() as session:
        cap = _CAPS[platform]
        used = _used_today(session, platform)
        remaining = max(0, cap - used)
    if limit is not None:
        remaining = min(remaining, max(0, limit))

    if remaining <= 0:
        return {"ok": True, "submitted": 0,
                "message": f"{label}: daily cap reached ({used}/{cap}). Resumes tomorrow."}

    if settings.dry_run:
        log_event("platform_apply", platform, "dry_run", f"cap {cap}")
        return {"ok": True, "submitted": 0, "dry_run": True,
                "message": f"DRY RUN — would auto-apply to up to {remaining} {label} job(s)."}

    if platform == "yc":
        role_key = platforms._YC_ROLE.get((query or "").strip().lower(), "eng")
        results = platforms.yc_autoapply(profile, role_key, remote, remaining)
    elif platform == "cutshort":
        results = platforms.cutshort_autoapply(profile, query, remote, remaining)
    elif platform == "wellfound":
        results = platforms.wellfound_autoapply(profile, query, remote, remaining)
    else:  # ziprecruiter
        results = platforms.ziprecruiter_autoapply(profile, query or "software engineer",
                                                   location or "Remote", remaining)

    submitted = [r for r in results if r.get("outcome") == "submitted"]
    skipped = [r for r in results if str(r.get("outcome", "")).startswith("skipped")]
    captcha = any(r.get("outcome") == "captcha_stop" for r in results)
    needs_login = any(r.get("outcome") == "needs_login" for r in results)

    # Break down WHY jobs were skipped, so "0 applied" is explained (not a mystery).
    from collections import Counter
    reasons = Counter(str(r["outcome"]).split(":", 1)[1]
                      for r in skipped if ":" in str(r["outcome"]))
    skip_detail = ", ".join(f"{n} {reason.replace('-', ' ')}"
                            for reason, n in reasons.most_common())

    with get_session() as session:
        _add_used(session, platform, len(submitted))

    if needs_login:
        msg = (f"{label}: not logged in. Run  py -3.11 formtool.py platlogin {platform}  "
               "(log in, close the window), then retry.")
    elif captcha:
        msg = (f"{label}: stopped on a security check after {len(submitted)} apply(ies). "
               "Wait a while before retrying — pushing further risks the account.")
    elif not submitted and not skipped:
        msg = (f"{label}: 0 applied — every matching job was already applied to, or none were "
               "found. Try different skills (comma-separated) in the box, or it's done for now.")
    elif not submitted:
        msg = (f"{label}: auto-applied to 0 job(s); skipped {len(skipped)}"
               + (f" ({skip_detail})" if skip_detail else "")
               + ". Already-applied jobs are skipped automatically — try other skills for more.")
    else:
        msg = (f"{label}: auto-applied to {len(submitted)} job(s); skipped {len(skipped)}"
               + (f" ({skip_detail})" if skip_detail else "")
               + f". Daily cap {cap}.")
    log_event("platform_apply", platform, "ok", msg)
    return {"ok": True, "platform": platform, "submitted": len(submitted),
            "skipped": len(skipped), "captcha_stop": captcha, "message": msg}
