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
