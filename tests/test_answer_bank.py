"""Tests for the learning answer-bank, the LLM screening-question fallback, and the
widened auto-apply scope (India + remote/global). All offline: DRY_RUN keeps the real
Groq call from firing, and the bank is redirected to a temp file so the operator's real
answer_bank.json is never touched."""
import json
import os

os.environ.setdefault("DRY_RUN", "true")

from app.integrations import browser
from app.services import answer_bank, linkedin_apply


# --- normalisation ---------------------------------------------------------------

def test_normalize_collapses_whitespace_and_strips_markers():
    n = answer_bank._normalize
    assert n("  How many   years? ") == "how many years"
    assert n("Years of experience *") == "years of experience"
    assert n("Email Address:") == "email address"
    assert n("") == ""


# --- persistent bank (learning) --------------------------------------------------

def test_remember_and_get_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(answer_bank, "BANK_PATH", tmp_path / "ab.json")
    monkeypatch.setattr(answer_bank, "_CACHE", None)

    assert answer_bank.get("How many years of Python?") is None     # empty bank
    answer_bank.remember("How many years of Python? ", "5")
    # different phrasing/casing/spacing resolves to the same learned answer
    assert answer_bank.get("how many   years of python") == "5"
    assert answer_bank.count() == 1
    # and it persisted to disk under the normalised key
    saved = json.loads((tmp_path / "ab.json").read_text(encoding="utf-8"))
    assert saved == {"how many years of python": "5"}


def test_remember_ignores_blanks(tmp_path, monkeypatch):
    monkeypatch.setattr(answer_bank, "BANK_PATH", tmp_path / "ab.json")
    monkeypatch.setattr(answer_bank, "_CACHE", None)
    answer_bank.remember("", "x")
    answer_bank.remember("Q", "")
    assert answer_bank.count() == 0


# --- LLM fallback (skip-if-unsure, option-constrained) ---------------------------

def test_llm_answer_offline_in_dry_run_returns_none():
    # DRY_RUN => groq_client.chat returns '' => skip (no network, safe for tests/CI)
    assert answer_bank.llm_answer("Why do you want this role?", {}) is None


def test_llm_answer_maps_to_offered_option(monkeypatch):
    monkeypatch.setattr(answer_bank.groq_client, "chat", lambda *a, **k: "Yes")
    assert answer_bank.llm_answer("Are you comfortable with on-call?", {},
                                  options=["Yes", "No"]) == "Yes"


def test_llm_answer_rejects_answer_not_in_options():
    # an answer that matches no offered option must be skipped, never submitted
    import app.services.answer_bank as ab
    orig = ab.groq_client.chat
    ab.groq_client.chat = lambda *a, **k: "Maybe"
    try:
        assert ab.llm_answer("Pick one", {}, options=["Yes", "No"]) is None
    finally:
        ab.groq_client.chat = orig


def test_llm_answer_unknown_means_skip(monkeypatch):
    monkeypatch.setattr(answer_bank.groq_client, "chat", lambda *a, **k: "UNKNOWN")
    assert answer_bank.llm_answer("Obscure unanswerable question", {}) is None


def test_llm_answer_free_text_numeric(monkeypatch):
    monkeypatch.setattr(answer_bank.groq_client, "chat", lambda *a, **k: "5")
    assert answer_bank.llm_answer("Years of experience with Docker?", {}) == "5"


# --- widened auto-apply scope: India + remote/global -----------------------------

def test_scope_attempts_all_then_workauth_filters():
    # LinkedIn's guest scraper labels REMOTE jobs by country (not "Remote"), so we can't
    # distinguish remote-US from on-site-US on the location string. The agent therefore
    # attempts everything in scope; the work-authorisation screening answer ("No" for a
    # country the operator can't work in) discards the genuinely on-site-only foreign ones
    # inside the Easy-Apply flow.
    f = linkedin_apply._in_apply_scope
    assert f({"location": "Bengaluru, India"})
    assert f({"location": "Remote"})
    assert f({"location": "Tampa, FL"})                 # foreign — attempted, work-auth filters
    assert f({"location": "London, United Kingdom"})
    assert f({"location": ""})


# --- assisted runs teach the bank: what's worth learning -------------------------

def test_li_learnable_learns_custom_questions_only():
    p = {"phone": "8287608280", "city": "Delhi", "willing_to_relocate": "Yes"}
    learn = browser._li_learnable
    # custom screening questions the static map can't answer => learn them
    assert learn("Why do you want to work here?", p) is True
    assert learn("Rate your Python from 1 to 10", p) is True
    assert learn("Which shift do you prefer?", p, is_choice=True) is True


def test_li_learnable_skips_sensitive_and_already_known():
    p = {"phone": "8287608280", "city": "Delhi", "willing_to_relocate": "Yes"}
    learn = browser._li_learnable
    # never bank sensitive self-ID answers
    assert learn("Do you have a disability?", p) is False
    assert learn("Are you a protected veteran?", p) is False
    # already covered by the static profile map => no need to learn
    assert learn("Phone", p) is False
    assert learn("Are you willing to relocate?", p, is_choice=True) is False
    assert learn("", p) is False
