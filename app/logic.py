"""Pure, dependency-free logic helpers.

These four functions encode the safety-critical math and are locked by unit tests
(tests/test_patterns.py, test_dedupe.py, test_ramp_cap.py, test_bounce_rate.py).
They import nothing heavy so the tests stay fast and the services in
services/{discovery,contacts,sender}.py reuse them as the single source of truth.
"""
from __future__ import annotations

import re
from datetime import date

# Hard ceiling on the daily cap, regardless of configuration (Technical-Spec §3).
RAMP_HARD_MAX = 75


# --- Email pattern generator (Technical-Spec §5.2 step 3 / FR-8) -----------------

def generate_email_patterns(first: str | None, last: str | None, domain: str) -> list[str]:
    """Return candidate emails for a domain, ordered most- to least-specific.

    Person-specific patterns (first.last@, flast@, first@) are emitted only when a
    name is available; role inboxes (careers@, jobs@, hr@, talent@) always follow.
    Output is lowercased, de-duplicated, order-preserving.
    """
    domain = (domain or "").strip().lower().lstrip("@")
    if not domain:
        return []

    def clean(part: str | None) -> str:
        # Keep only ascii letters; drops spaces, dots, accents, punctuation.
        return re.sub(r"[^a-z]", "", (part or "").strip().lower())

    f = clean(first)
    l = clean(last)

    locals_: list[str] = []
    if f and l:
        locals_.append(f"{f}.{l}")   # first.last
        locals_.append(f"{f[0]}{l}")  # flast
    if f:
        locals_.append(f)            # first
    # Role-based inboxes always apply.
    locals_.extend(["careers", "jobs", "hr", "talent"])

    seen: set[str] = set()
    out: list[str] = []
    for lp in locals_:
        addr = f"{lp}@{domain}"
        if addr not in seen:
            seen.add(addr)
            out.append(addr)
    return out


# --- Posting dedupe (Technical-Spec §5.1 / FR-4) ---------------------------------

def _norm(value) -> str:
    return (str(value) if value is not None else "").strip().lower()


def dedupe_postings(postings: list[dict]) -> list[dict]:
    """Drop duplicate postings, preserving first occurrence.

    A posting is a duplicate if its source_url was already seen, OR if its
    (domain, role_title) pair was already seen. Empty source_url is not treated
    as a collision key (many blanks must not collapse to one).
    """
    seen_urls: set[str] = set()
    seen_pairs: set[tuple[str, str]] = set()
    out: list[dict] = []
    for p in postings:
        url = _norm(p.get("source_url"))
        pair = (_norm(p.get("domain")), _norm(p.get("role_title")))

        if url and url in seen_urls:
            continue
        if pair != ("", "") and pair in seen_pairs:
            continue

        if url:
            seen_urls.add(url)
        if pair != ("", ""):
            seen_pairs.add(pair)
        out.append(p)
    return out


# --- Ramp cap for today (Technical-Spec §3 ramp schedule + §6) --------------------

def ramp_cap_for_today(
    first_send_date: date | None,
    today: date,
    daily_cap: int,
) -> int:
    """Maximum sends allowed today under the warm-up ramp.

    Day 1–2 -> 15 · Day 3–5 -> 30 · Day 6+ -> DAILY_CAP (<=75).
    A short warm-up still protects a fresh sender's reputation; after it, the
    operator's DAILY_CAP binds. `first_send_date` None means no send yet -> day 1.
    The result never exceeds min(daily_cap, RAMP_HARD_MAX).
    """
    cap = max(0, min(int(daily_cap), RAMP_HARD_MAX))

    if first_send_date is None:
        day = 1
    else:
        day = (today - first_send_date).days + 1
        if day < 1:
            day = 1  # first send dated in the future -> clamp to day 1

    if day <= 2:
        tier = 15
    elif day <= 5:
        tier = 30
    else:
        tier = cap

    return min(tier, cap)


# --- Rolling bounce rate (Technical-Spec §6 auto-pause / FR-20) -------------------

# Don't enforce the bounce-rate pause until at least this many messages have been
# sent in the window — otherwise 1 bad address in a tiny sample (e.g. 1/12 = 8%)
# permanently blocks sending even after the cause is fixed.
MIN_BOUNCE_SAMPLE = 20


def li_ramp_cap(first_date: date | None, today: date, cap: int) -> int:
    """Warm-up ramp for autonomous LinkedIn auto-applies (lowers ban risk early).

    Day 1–2 -> 8 · Day 3–5 -> 15 · Day 6+ -> cap. `first_date` None = day 1.
    """
    cap = max(0, int(cap))
    if first_date is None:
        day = 1
    else:
        day = (today - first_date).days + 1
        if day < 1:
            day = 1
    if day <= 2:
        tier = 8
    elif day <= 5:
        tier = 15
    else:
        tier = cap
    return min(tier, cap)


def rolling_bounce_rate(outcomes: list[str], min_sample: int = 0) -> float:
    """bounced / sent over the supplied send_log outcomes.

    `outcomes` is a list of send_log outcome strings (e.g. "sent", "bounced",
    "skipped", "error"). Denominator counts messages that actually went out
    ("sent"); a later "bounced" outcome is the numerator. 4 bounces in 100 sends
    -> 0.04. Returns 0.0 when nothing has been sent, or when fewer than
    `min_sample` messages have been sent (too small a sample to act on).
    """
    sent = sum(1 for o in outcomes if o == "sent")
    bounced = sum(1 for o in outcomes if o == "bounced")
    if sent == 0 or sent < min_sample:
        return 0.0
    return bounced / sent
