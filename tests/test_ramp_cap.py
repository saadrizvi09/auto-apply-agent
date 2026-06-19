"""Ramp cap for today (Technical-Spec §3 ramp schedule + §6 warm-up)."""
from datetime import date

from app.logic import ramp_cap_for_today

FIRST = date(2026, 6, 1)
CAP = 50  # high cap so the ramp tiers (not the cap) are what binds


def test_none_first_send_treated_as_day_one():
    assert ramp_cap_for_today(None, date(2026, 6, 10), CAP) == 15


def test_day_1_to_2_is_fifteen():
    assert ramp_cap_for_today(FIRST, date(2026, 6, 1), CAP) == 15  # day 1
    assert ramp_cap_for_today(FIRST, date(2026, 6, 2), CAP) == 15  # day 2


def test_day_3_to_5_is_thirty():
    assert ramp_cap_for_today(FIRST, date(2026, 6, 3), CAP) == 30  # day 3
    assert ramp_cap_for_today(FIRST, date(2026, 6, 5), CAP) == 30  # day 5


def test_day_6_plus_is_daily_cap():
    assert ramp_cap_for_today(FIRST, date(2026, 6, 6), CAP) == 50   # day 6
    assert ramp_cap_for_today(FIRST, date(2026, 7, 1), CAP) == 50


def test_daily_cap_hard_ceiling_of_seventyfive():
    # Even if configured above 75, the result is clamped to 75.
    assert ramp_cap_for_today(FIRST, date(2026, 7, 1), 500) == 75


def test_small_daily_cap_clamps_early_tiers():
    # A cap below a tier value must lower that tier too (never exceed the cap).
    assert ramp_cap_for_today(FIRST, date(2026, 6, 1), 10) == 10  # day 1, tier 15 -> 10
    assert ramp_cap_for_today(FIRST, date(2026, 6, 3), 12) == 12  # day 3, tier 30 -> 12


def test_future_first_send_clamps_to_day_one():
    assert ramp_cap_for_today(date(2026, 6, 20), date(2026, 6, 10), CAP) == 15


# --- LinkedIn auto-apply ramp (Day1-2=8, Day3-5=15, Day6+=cap) ----------------------

def test_li_ramp_cap_schedule():
    from app.logic import li_ramp_cap
    f = date(2026, 6, 1)
    assert li_ramp_cap(None, date(2026, 6, 1), 30) == 8     # no first date -> day 1
    assert li_ramp_cap(f, date(2026, 6, 2), 30) == 8        # day 2
    assert li_ramp_cap(f, date(2026, 6, 3), 30) == 15       # day 3
    assert li_ramp_cap(f, date(2026, 6, 5), 30) == 15       # day 5
    assert li_ramp_cap(f, date(2026, 6, 6), 30) == 30       # day 6 -> full cap
    assert li_ramp_cap(f, date(2026, 6, 6), 10) == 10       # cap below tier clamps
