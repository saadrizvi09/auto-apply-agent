"""Rolling bounce-rate calc (Technical-Spec §6 auto-pause / FR-20)."""
from app.logic import rolling_bounce_rate

# Default BOUNCE_PAUSE_THRESHOLD from Technical-Spec §3 (kept inline so this
# safety-math test stays dependency-free).
PAUSE_THRESHOLD = 0.03


def test_four_bounces_in_one_hundred_sends():
    # The §11 safety scenario: 4 bounces in 100 sends -> 0.04, above the 3% pause.
    outcomes = ["sent"] * 100 + ["bounced"] * 4
    rate = rolling_bounce_rate(outcomes)
    assert abs(rate - 0.04) < 1e-9
    assert rate > PAUSE_THRESHOLD  # auto-pause would fire


def test_zero_sends_is_zero():
    assert rolling_bounce_rate([]) == 0.0
    assert rolling_bounce_rate(["skipped", "error"]) == 0.0


def test_under_threshold_does_not_trip():
    outcomes = ["sent"] * 100 + ["bounced"] * 2  # 2%
    rate = rolling_bounce_rate(outcomes)
    assert abs(rate - 0.02) < 1e-9
    assert rate <= 0.03


def test_skipped_and_error_excluded_from_calc():
    # Only 'sent' counts as denominator; skipped/error are neither sent nor bounced.
    outcomes = ["sent"] * 50 + ["bounced"] * 1 + ["skipped"] * 20 + ["error"] * 5
    rate = rolling_bounce_rate(outcomes)
    assert abs(rate - (1 / 50)) < 1e-9


def test_all_bounced():
    assert rolling_bounce_rate(["sent", "bounced"]) == 1.0  # 1 bounced / 1 sent


def test_min_sample_suppresses_tiny_samples():
    # 1 bounce in 12 sends = 8.3%, but below the 20-send minimum -> reported 0.0,
    # so a couple of early bad addresses don't permanently deadlock sending.
    outcomes = ["sent"] * 12 + ["bounced"] * 1
    assert rolling_bounce_rate(outcomes) > 0.03                  # raw rate is high
    assert rolling_bounce_rate(outcomes, min_sample=20) == 0.0   # but suppressed


def test_min_sample_enforced_once_sample_reached():
    # Once enough sends exist, the guard engages normally.
    outcomes = ["sent"] * 30 + ["bounced"] * 2  # 6.7%
    assert rolling_bounce_rate(outcomes, min_sample=20) > 0.03
