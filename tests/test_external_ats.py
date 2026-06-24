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
    # internships / trainee (skipped by default — India platforms can't confirm >=8 LPA)
    assert _skip_reason("Robotics Intern") == "intern"
    assert _skip_reason("Software Trainee") == "intern"
    # unreachable senior/lead/exec (operator is a fresher)
    assert _skip_reason("Staff Software Engineer") == "senior-role"
    assert _skip_reason("Engineering Manager") == "senior-role"


def test_skip_reason_drops_senior_for_fresher():
    """Operator is a 2026 fresher — Senior/Sr/Lead titles are low-probability, skip them."""
    from app.integrations.platforms import _skip_reason
    assert _skip_reason("Senior AI Engineer") == "senior-role"
    assert _skip_reason("Senior - AI Engineer") == "senior-role"
    assert _skip_reason("Sr. Software Engineer") == "senior-role"
    assert _skip_reason("Senior Full Stack Engineer (AI Platform)") == "senior-role"
    assert _skip_reason("Lead Software Engineer") == "senior-role"


def test_skip_reason_allow_intern_for_foreign():
    """Foreign/worldwide-remote platforms keep internships (operator will take an unpaid
    foreign role); the senior + off-target filters still apply."""
    from app.integrations.platforms import _skip_reason
    assert _skip_reason("AI Engineering Intern", allow_intern=True) is None
    assert _skip_reason("Software Engineer Intern", allow_intern=True) is None
    # but allow_intern does NOT rescue senior or off-target
    assert _skip_reason("Senior AI Engineer", allow_intern=True) == "senior-role"
    assert _skip_reason("Marketing Intern", allow_intern=True) == "off-target"


def test_skip_reason_keeps_target_ai_and_software_roles():
    from app.integrations.platforms import _skip_reason
    for keep in ["AI Engineer", "Generative AI Engineer", "Founding Engineer",
                 "Backend Engineer (Python/Golang)", "Full Stack Developer",
                 "Founding AI Engineer", "Software Engineer",
                 "Design Engineer"]:   # 'designer' must NOT match 'Design Engineer'
        assert _skip_reason(keep) is None, keep


def test_skip_reason_drops_non_engineering_roles():
    """Wellfound's /role/r/ai-engineer mixes in marketing/sales/design/PM — drop those."""
    from app.integrations.platforms import _skip_reason
    for drop in ["Product Marketing Lead", "Growth Marketer", "Sales Engineer",
                 "Account Executive", "Technical Recruiter", "Customer Success Manager",
                 "Product Designer", "Senior Product Manager", "Project Manager"]:
        assert _skip_reason(drop) == "off-target", drop


def test_wellfound_role_slug_defaults_to_ai():
    from app.integrations.platforms import _wellfound_role_slug
    assert _wellfound_role_slug("") == "ai-engineer"
    assert _wellfound_role_slug("AI Engineer") == "ai-engineer"
    assert _wellfound_role_slug("backend engineer") == "backend-engineer"
    assert _wellfound_role_slug("Golang Developer") == "golang-developer"  # slugified fallback


def test_wellfound_blocked_detects_visa_banner():
    """Wellfound disables Send for US-in-country/no-sponsorship roles; detect the banner."""
    from app.integrations.platforms import _wellfound_blocked

    class _P:
        def __init__(self, body):
            self._b = body
        def query_selector(self, sel):
            return None              # force the inner_text("body") fallback path
        def inner_text(self, _sel):
            return self._b

    assert _wellfound_blocked(_P(
        "Ravenna Software does not offer visa sponsorship and requires all remote "
        "workers to be in-country. Your profile indicates you require sponsorship."))
    assert not _wellfound_blocked(_P("Cover Letter. Tell us why you're a great fit for this role."))


def test_wellfound_location_restricted_filters_geo_locked_roles():
    """Skip roles geo-locked outside India BEFORE opening the modal, but never drop a
    role that still allows India/worldwide remote (the ok-markers veto the skip)."""
    from app.integrations.platforms import _wellfound_location_restricted

    class _P:
        def __init__(self, body):
            self._b = body
        def inner_text(self, _sel):
            return self._b

    # Clearly US-only with no global allowance -> restricted.
    assert _wellfound_location_restricted(_P(
        "AI Engineer. Remote (US). Must be authorized to work in the United States."))
    assert _wellfound_location_restricted(_P(
        "Software Engineer. United States only. US citizen or green card required."))
    # US company but explicitly hires worldwide / India -> NOT restricted (ok-marker vetoes).
    assert not _wellfound_location_restricted(_P(
        "Remote (Worldwide). We're a US company hiring globally. US time-zone overlap nice."))
    assert not _wellfound_location_restricted(_P(
        "AI Engineer. Remote — India. Must be authorized to work in the US is NOT required."))
    # Ordinary unrestricted listing -> NOT restricted.
    assert not _wellfound_location_restricted(_P(
        "AI Engineer. Remote. Build LLM agents with FastAPI. Competitive pay."))


def test_wellfound_registered_across_layers():
    from app.integrations import platforms
    from app.services import platform_apply
    assert "wellfound" in platforms._LOGIN and "wellfound" in platforms._HOME
    assert "wellfound" in platform_apply._CAPS and platform_apply._CAPS["wellfound"] > 0
    assert hasattr(platforms, "wellfound_autoapply")


def test_instahyre_allows_interns_any_salary():
    """Instahyre applies to every on-target intern regardless of stipend (salary ignored);
    only off-target / senior titles still drop."""
    from app.integrations.platforms import _skip_reason

    # on-target interns are kept (no salary gate)
    assert _skip_reason("Backend Engineer (Internship)", allow_intern=True) is None
    assert _skip_reason("Full Stack Developer Intern", allow_intern=True) is None
    assert _skip_reason("Software Development Engineer (Internship)", allow_intern=True) is None
    # off-target / senior interns still drop
    assert _skip_reason("C++ Content Writer - Intern (Internship)", allow_intern=True) == "off-target"
    assert _skip_reason("Senior Data Scientist Intern", allow_intern=True) == "off-target"


def test_instahyre_registered_across_layers():
    from app.integrations import platforms
    from app.services import platform_apply
    assert "instahyre" in platforms._LOGIN and "instahyre" in platforms._HOME
    assert "instahyre" in platforms._LOGGED_IN_MARKERS
    assert "instahyre" in platform_apply._CAPS and platform_apply._CAPS["instahyre"] > 0
    assert platform_apply._LABEL.get("instahyre") == "Instahyre"
    assert hasattr(platforms, "instahyre_autoapply")


def test_instahyre_modal_title_and_apply_button():
    """Modal <h1> is read for _skip_reason; the apply-button matcher picks the 'Apply' CTA
    and never the 'Not interested' decline control (live flow = View -> modal -> Apply)."""
    from app.integrations.platforms import _instahyre_modal_title, _instahyre_apply_btn

    class _El:
        def __init__(self, text, visible=True):
            self._t = text
            self._v = visible
        def inner_text(self):
            return self._t
        def is_visible(self):
            return self._v

    class _Dlg:
        def __init__(self, mapping):
            self._m = mapping        # selector -> element
        def query_selector(self, sel):
            return self._m.get(sel)

    # Title comes from the modal <h1>.
    dlg = _Dlg({"h1": _El("Senior AI Engineer")})
    assert _instahyre_modal_title(dlg) == "Senior AI Engineer"

    # Only "Not interested" present -> matcher returns None (never auto-declines).
    decline_only = _Dlg({'button:has-text("Apply")': _El("Not interested")})
    assert _instahyre_apply_btn(decline_only) is None

    # The real 'Apply' CTA is selected.
    good = _Dlg({'button:has-text("Apply")': _El("Apply")})
    assert _instahyre_apply_btn(good) is not None
