"""Offline tests for the autonomous external-ATS engine's decision logic — the parts
that decide whether a submit succeeded or a bot-wall was hit. The DOM-driving parts
(fill/submit/upload) need a live browser and are validated on first real run, like the
LinkedIn Easy-Apply selectors were. Here we exercise the pure URL/text detectors with a
minimal fake page, because a false 'submitted' would wrongly mark a job applied and a
false 'blocked' would needlessly halt the run."""
import os

os.environ.setdefault("DRY_RUN", "true")

from app.integrations import browser


class _FakePage:
    """Minimal stand-in exposing the two members the detectors read."""
    def __init__(self, url="", body=""):
        self._url = url
        self._body = body

    @property
    def url(self):
        return self._url

    def inner_text(self, _selector):
        return self._body


# --- bot-wall detection (Cloudflare / PerimeterX press-and-hold) ------------------

def test_ats_is_blocked_detects_visible_walls():
    b = browser._ats_is_blocked
    assert b(_FakePage(body="Please Press & Hold to confirm you are human"))
    assert b(_FakePage(body="Checking your browser before accessing the site"))
    assert b(_FakePage(url="https://x.com/cdn-cgi/challenge-platform/h/g"))
    assert b(_FakePage(url="https://ziprecruiter.com/_human/verify"))


def test_ats_is_blocked_ignores_normal_form():
    # an ordinary ATS form (which carries an INVISIBLE recaptcha badge) is NOT a wall
    b = browser._ats_is_blocked
    assert not b(_FakePage(url="https://boards.greenhouse.io/acme/jobs/1",
                           body="Apply for this job. First name. Submit application."))
    assert not b(_FakePage(url="https://jobs.lever.co/acme/123/apply", body="Resume/CV"))


# --- submission confirmation detection --------------------------------------------

def test_ext_submitted_via_url_change():
    s = browser._ext_submitted
    assert s(_FakePage(url="https://job-boards.greenhouse.io/acme/jobs/1/confirmation",
                       body="x"), "https://job-boards.greenhouse.io/acme/jobs/1")
    assert s(_FakePage(url="https://jobs.lever.co/acme/123/apply/thanks", body="x"),
             "https://jobs.lever.co/acme/123/apply")


def test_ext_submitted_via_confirmation_text():
    # Ashby (SPA) confirms with text and no URL change
    s = browser._ext_submitted
    assert s(_FakePage(url="u", body="Thank you for applying! We received your application."), "u")
    assert s(_FakePage(url="u", body="Your application has been submitted."), "u")


def test_ext_submitted_false_on_unsubmitted_form():
    s = browser._ext_submitted
    assert not s(_FakePage(url="u", body="Submit your application below"), "u")
    assert not s(_FakePage(url="https://jobs.lever.co/acme/123/apply", body="Resume"),
                 "https://jobs.lever.co/acme/123/apply")


# --- precise option matching (the dropdown/combobox/radio selector) ---------------
# A naive substring match made "India" select "British Indian Ocean Territory (+246)",
# which then flagged the phone number "too long". This is the regression guard.

def test_best_option_match_rejects_substring_country_trap():
    m = browser._best_option_match
    # "india" is a substring of "indian" — must NOT be chosen; the real India row wins
    assert m("India", ["British Indian Ocean Territory +246", "India +91",
                        "Indonesia +62"]) == "India +91"
    # even when the trap sorts first and India has no dial code shown
    assert m("India", ["British Indian Ocean Territory", "India", "Indonesia"]) == "India"


def test_best_option_match_exact_and_yesno():
    m = browser._best_option_match
    assert m("Yes", ["Yes", "No"]) == "Yes"
    assert m("No", ["Yes", "No"]) == "No"
    # whole-word phrase inside a longer label
    assert m("Not working currently",
             ["I am on notice period", "I am not working currently"]) == \
        "I am not working currently"


def test_best_option_match_prefix_and_abbrev():
    m = browser._best_option_match
    # option is an abbreviation of the (longer) answer -> pick the option
    assert m("Bachelor of Technology", ["Bachelor", "Master", "PhD"]) == "Bachelor"
    # answer is a prefix of exactly one option
    assert m("Senior", ["Senior Engineer", "Junior Engineer"]) == "Senior Engineer"


def test_best_option_match_returns_none_when_nothing_fits():
    m = browser._best_option_match
    assert m("India", ["Canada", "Australia"]) is None
    assert m("", ["Yes", "No"]) is None


# --- self-identification handling (the "are you transgender?" ask) -----------------
# On EXTERNAL forms the LLM answers self-ID from the profile, falling back to a
# "decline to self-identify" option so a required EEO field doesn't block submit. On
# LinkedIn (allow_sensitive=False) these stay blank. Hard-block legal questions never auto.

def test_decline_option_finds_prefer_not():
    d = browser._decline_option
    assert d(["Male", "Female", "I prefer not to say"]) == "I prefer not to say"
    assert d(["Yes", "No", "Decline to self-identify"]) == "Decline to self-identify"
    assert d(["Yes", "No"]) is None


def test_is_self_id_classification():
    s = browser._is_self_id
    assert s("are you transgender?")
    assert s("disability status")
    assert s("are you hispanic/latino?")
    assert not s("what is your gender?")        # plain gender -> answered from profile (Male)
    assert not s("are you willing to relocate?")


def _neutralize_bank(monkeypatch):
    # keep the offline test from reading/writing the real answer_bank.json
    import app.services.answer_bank as ab
    monkeypatch.setattr(ab, "get", lambda q: None)
    monkeypatch.setattr(ab, "remember", lambda q, a: None)


def test_self_id_falls_back_to_decline_on_external(monkeypatch):
    _neutralize_bank(monkeypatch)
    p = {"gender": "Male", "willing_to_relocate": "Yes"}
    # DRY_RUN -> LLM returns None -> decline option is chosen (valid, unblocks submit)
    assert browser._li_resolve_choice(
        "Are you transgender?", p, ["Yes", "No", "Decline to self-identify"],
        allow_sensitive=True) == "Decline to self-identify"


def test_self_id_blank_without_decline_and_on_linkedin(monkeypatch):
    _neutralize_bank(monkeypatch)
    p = {"gender": "Male"}
    # no decline option offered + LLM unavailable -> None (leave blank)
    assert browser._li_resolve_choice(
        "Are you transgender?", p, ["Yes", "No"], allow_sensitive=True) is None
    # LinkedIn path never auto-answers self-ID
    assert browser._li_resolve_choice(
        "Are you transgender?", p, ["Yes", "No", "Decline to self-identify"],
        allow_sensitive=False) is None


def test_hard_block_legal_questions_never_auto(monkeypatch):
    _neutralize_bank(monkeypatch)
    p = {"gender": "Male"}
    assert browser._li_resolve_choice(
        "Have you ever been convicted of a felony?", p, ["Yes", "No"],
        allow_sensitive=True) is None


def test_non_sensitive_choice_still_answered(monkeypatch):
    _neutralize_bank(monkeypatch)
    p = {"willing_to_relocate": "Yes"}
    assert browser._li_resolve_choice(
        "Are you willing to relocate?", p, ["Yes", "No"]) == "Yes"


# --- external-platform title filter (_skip_reason) -------------------------------
# Mirrors the LinkedIn discovery filters so Cutshort/Wellfound auto-apply stays on-target
# (the Cutshort `python` pool previously surfaced Data Engineer / Robotics Intern / ETL).

def test_skip_reason_drops_off_target_and_interns():
    from app.integrations.platforms import _skip_reason
    # off-target ML/data roles
    assert _skip_reason("Data Engineer") == "off-target"
    assert _skip_reason("Senior Data Scientist") == "off-target"
    assert _skip_reason("Machine Learning Engineer") == "off-target"
    assert _skip_reason("SQL Developer & ETL, Python, Cloud Engineer") == "off-target"
    # internships / trainee
    assert _skip_reason("Robotics Intern") == "intern"
    assert _skip_reason("Software Trainee") == "intern"
    # unreachable senior/exec
    assert _skip_reason("Staff Software Engineer") == "senior-role"
    assert _skip_reason("Engineering Manager") == "senior-role"


def test_skip_reason_keeps_target_ai_and_software_roles():
    from app.integrations.platforms import _skip_reason
    for keep in ["AI Engineer", "Generative AI Engineer", "Senior AI Engineer",
                 "Backend Engineer (Python/Golang)", "Full Stack Developer",
                 "Senior Full Stack Engineer (AI Platform)", "Software Engineer"]:
        assert _skip_reason(keep) is None, keep


def test_wellfound_role_slug_defaults_to_ai():
    from app.integrations.platforms import _wellfound_role_slug
    assert _wellfound_role_slug("") == "ai-engineer"
    assert _wellfound_role_slug("AI Engineer") == "ai-engineer"
    assert _wellfound_role_slug("backend engineer") == "backend-engineer"
    assert _wellfound_role_slug("Golang Developer") == "golang-developer"  # slugified fallback


def test_wellfound_registered_across_layers():
    from app.integrations import platforms
    from app.services import platform_apply
    assert "wellfound" in platforms._LOGIN and "wellfound" in platforms._HOME
    assert "wellfound" in platform_apply._CAPS and platform_apply._CAPS["wellfound"] > 0
    assert hasattr(platforms, "wellfound_autoapply")
