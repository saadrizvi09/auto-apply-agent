"""Tests for the LinkedIn Easy-Apply answer-bank mapping.

The browser/DOM driving can't be unit-tested (needs a live logged-in LinkedIn), but
the answer mapping CAN — and it's the safety-critical part: work-auth/sponsorship/
gender are answered from the India-scope answer bank, while disclosure questions
(disability/veteran/race/background) stay blank so the autonomous agent skips them."""
import os

os.environ.setdefault("DRY_RUN", "true")

from app.integrations.browser import _li_value_for, _li_radio_answer

PROFILE = {
    "full_name": "Saad Rizvi", "email": "saad@example.com", "phone": "8287608280",
    "city": "Delhi", "college": "Jamia Millia Islamia", "degree": "B.Tech (ECE)",
    "graduation_year": "2026", "years_experience": "1", "default_skill_years": "1",
    "expected_ctc_number": "700000", "current_ctc": "0",
    "notice_period": "Immediate", "willing_to_relocate": "Yes",
    "work_authorized_in": "India", "needs_sponsorship": "No", "gender": "Male",
    "address_line1": "Aman Apartment", "address_line2": "Okhla", "pincode": "110025",
    "country": "India",
    "linkedin": "https://linkedin.com/in/saad", "github": "https://github.com/saad",
    "portfolio": "",
}


# --- safe fields map ---------------------------------------------------------------

def test_maps_safe_text_fields():
    assert _li_value_for("Mobile phone number", PROFILE) == "8287608280"
    assert _li_value_for("How many years of experience do you have?", PROFILE) == "1"
    assert _li_value_for("Expected salary", PROFILE) == "700000"
    assert _li_value_for("Notice period", PROFILE) == "Immediate"
    assert _li_value_for("LinkedIn profile", PROFILE) == PROFILE["linkedin"]
    assert _li_value_for("Current city", PROFILE) == "Delhi"


def test_first_and_last_name():
    assert _li_value_for("First name", PROFILE) == "Saad"
    assert _li_value_for("Last name", PROFILE) == "Rizvi"


def test_bare_name_and_external_ats_labels():
    # external ATS (Ashby/Greenhouse) labels
    assert _li_value_for("Name", PROFILE) == "Saad Rizvi"          # bare single-name field
    assert _li_value_for("Email", PROFILE) == PROFILE["email"]
    assert _li_value_for("LinkedIn URL", PROFILE) == PROFILE["linkedin"]
    assert _li_value_for("GitHub URL", PROFILE) == PROFILE["github"]


def test_country_code_not_filled_with_number():
    # a country-code dropdown must NOT get the phone number
    assert _li_value_for("Phone country code", PROFILE) is None


# --- answer bank: work-auth / sponsorship / gender / salary / address --------------

def test_work_authorization_india_vs_foreign():
    # India (or unspecified) -> Yes; a foreign country -> No (truthfully filters out)
    assert _li_value_for("Are you legally authorized to work in India?", PROFILE) == "Yes"
    assert _li_value_for("Are you authorized to work?", PROFILE) == "Yes"
    assert _li_value_for("Are you legally authorized to work in the United States?", PROFILE) == "No"


def test_sponsorship_and_gender_from_bank():
    assert _li_value_for("Will you require visa sponsorship?", PROFILE) == "No"
    assert _li_value_for("Gender", PROFILE) == "Male"


def test_salary_and_default_skill_years():
    assert _li_value_for("Expected CTC", PROFILE) == "700000"
    assert _li_value_for("Current CTC", PROFILE) == "0"
    assert _li_value_for("How many years of experience do you have with React?", PROFILE) == "1"


def test_address_fields():
    assert _li_value_for("Address line 1", PROFILE) == "Aman Apartment"
    assert _li_value_for("Pin code", PROFILE) == "110025"


# --- still-blank disclosure questions (agent skips these) ---------------------------

def test_disclosure_questions_return_none():
    for q in [
        "Do you have a security clearance?",
        "Are you Hispanic or Latino?",
        "Do you have a disability?",
        "Have you ever been convicted of a felony?",
        "Are you willing to undergo a background check?",
        "Are you a protected veteran?",
    ]:
        assert _li_value_for(q, PROFILE) is None, q


def test_unknown_questions_return_none():
    assert _li_value_for("Describe a challenging project", PROFILE) is None
    assert _li_value_for("", PROFILE) is None


# --- radio mapping -----------------------------------------------------------------

def test_radio_safe_and_bank():
    assert _li_radio_answer("Are you comfortable working remotely?", PROFILE) == "Yes"
    assert _li_radio_answer("Are you willing to relocate?", PROFILE) == "Yes"
    assert _li_radio_answer("Are you authorized to work in India?", PROFILE) == "Yes"
    assert _li_radio_answer("Are you authorized to work in the US?", PROFILE) == "No"
    assert _li_radio_answer("Will you require sponsorship?", PROFILE) == "No"


def test_radio_disclosure_left_blank():
    assert _li_radio_answer("Do you have a disability?", PROFILE) is None
    assert _li_radio_answer("", PROFILE) is None
