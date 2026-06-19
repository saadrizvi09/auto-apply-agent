"""Pure-logic tests for referral parsing + form answer mapping (no browser/network)."""
import os
from pathlib import Path

os.environ.setdefault("DRY_RUN", "true")

from app.services import referrals
from app.services.formfiller import best_option, match_field, plan_answers

FIXTURE = Path(__file__).parent / "fixtures" / "referral_sample.txt"


# --- referral digest parsing -----------------------------------------------------

def test_parses_all_seven_jobs():
    jobs = referrals.parse_heuristic(FIXTURE.read_text(encoding="utf-8"))
    assert len(jobs) == 7


def test_routes_forms_and_emails():
    jobs = referrals.parse_heuristic(FIXTURE.read_text(encoding="utf-8"))
    kinds = [j["apply_kind"] for j in jobs]
    assert kinds.count("form") == 5
    assert kinds.count("email") == 2


def test_form_url_extracted():
    jobs = referrals.parse_heuristic(FIXTURE.read_text(encoding="utf-8"))
    scaler = next(j for j in jobs if j["company"] == "Scaler")
    assert scaler["apply_kind"] == "form"
    assert scaler["apply_url"].startswith("https://docs.google.com/forms/")
    assert " " not in scaler["apply_url"]


def test_email_target_cc_and_subject():
    jobs = referrals.parse_heuristic(FIXTURE.read_text(encoding="utf-8"))
    planys = next(j for j in jobs if j["company"] == "Planys")
    assert planys["apply_email"] == "hrrecruiter@planystech.com"
    assert planys["apply_cc"] == "ashokb@planystech.com"
    assert planys["apply_subject"] == "SE Intern application"


def test_promotional_footer_ignored():
    jobs = referrals.parse_heuristic(FIXTURE.read_text(encoding="utf-8"))
    assert all("whatsapp" not in j["company"].lower() for j in jobs)
    assert all(j["company"] != "Unknown" for j in jobs)


def test_empty_digest_is_empty():
    assert referrals.parse_heuristic("") == []


# --- field matching --------------------------------------------------------------

def test_match_specific_before_generic():
    assert match_field("College Name") == "college"     # not full_name
    assert match_field("Full Name") == "full_name"
    assert match_field("Email Address") == "email"
    assert match_field("Phone / WhatsApp Number") == "phone"
    assert match_field("Resume / CV link") == "cv_url"
    assert match_field("LeetCode / Codeforces profile") == "leetcode"
    assert match_field("Graduation Batch") == "graduation_year"


def test_best_option_year_inside_range():
    assert best_option("2026", ["2024", "2025/2026/2027", "2028"]) == "2025/2026/2027"
    assert best_option("Yes", ["Yes", "No"]) == "Yes"
    assert best_option("xyz", ["Yes", "No"]) is None


# --- answer planning -------------------------------------------------------------

PROFILE = {
    "full_name": "Saad Rizvi", "email": "saad@example.com", "github": "gh/saad",
    "graduation_year": "2026", "degree": "B.Tech", "willing_to_relocate": "Yes",
    # deliberately missing: phone, college, cv_url
}


def _q(title, type="SHORT_TEXT", options=None, required=False):
    return {"title": title, "type": type, "options": options or [], "required": required}


def test_known_field_filled_from_profile():
    plan = plan_answers([_q("Full Name", required=True)], PROFILE)[0]
    assert plan["answer"] == "Saad Rizvi"
    assert plan["source"] == "profile"


def test_missing_profile_field_flagged_not_hallucinated():
    plan = plan_answers([_q("Phone Number", required=True)], PROFILE)[0]
    assert plan["source"] == "missing"
    assert plan["missing_field"] == "phone"
    assert plan["answer"] == ""
    assert plan["needs_llm"] is False          # must NOT route to the LLM


def test_choice_picks_option():
    plan = plan_answers(
        [_q("Batch", "MULTIPLE_CHOICE", ["2024", "2025", "2026"], required=True)], PROFILE
    )[0]
    assert plan["answer"] == "2026"
    assert plan["source"] == "option"


def test_relocate_defaults_yes():
    plan = plan_answers(
        [_q("Willing to relocate?", "MULTIPLE_CHOICE", ["Yes", "No"], required=True)], PROFILE
    )[0]
    assert plan["answer"] == "Yes"


def test_open_question_routed_to_llm():
    plan = plan_answers([_q("Why should we hire you?", "PARAGRAPH", required=True)], PROFILE)[0]
    assert plan["needs_llm"] is True
    assert plan["source"] == "llm"


def test_file_upload_blocked():
    plan = plan_answers([_q("Upload resume", "FILE_UPLOAD", required=True)], PROFILE)[0]
    assert plan["blocked"] is True
    assert plan["needs_llm"] is False


# --- referral ingest (routing + dedupe), against a throwaway DB -------------------

def _temp_session():
    from sqlmodel import Session, SQLModel, create_engine
    import app.models  # noqa: F401  (register tables)
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(eng)
    return Session(eng)


def test_ingest_routes_and_dedupes():
    from app.db import ingest_referral_jobs
    jobs = referrals.parse_heuristic(FIXTURE.read_text(encoding="utf-8"))
    with _temp_session() as s:
        first = ingest_referral_jobs(s, jobs)
        assert first["added"] == 7
        assert first["forms"] == 5 and first["emails"] == 2
        # Re-ingesting the same digest adds nothing (dedupe by source key).
        again = ingest_referral_jobs(s, jobs)
        assert again["added"] == 0 and again["skipped"] == 7


def test_bare_form_links_are_caught():
    from app.services.referrals import parse_digest
    dump = ("notes... apply https://docs.google.com/forms/d/e/1FAIpQLSaaa/viewform "
            "and https://docs.google.com/forms/d/e/1FAIpQLSbbb/viewform?usp=send_form done")
    jobs = parse_digest(dump)
    forms = [j for j in jobs if j["apply_kind"] == "form"]
    assert len(forms) == 2
    assert all(f["apply_url"].startswith("https://docs.google.com/forms/") for f in forms)


# --- resume extraction -----------------------------------------------------------

def test_resume_extract_skips_dummy_and_placeholders():
    from app.services.resume import extract_fields
    text = (
        "Saad Rizvi  saad@real.com  Phone: 9876543210  alt +91 8287608280\n"
        "https://github.com/saadrizvi09  https://linkedin.com/in/CHANGE-ME\n"
        "https://leetcode.com/u/saadrizvi1234"
    )
    out = extract_fields(text)
    assert out["phone"] == "8287608280"            # dummy 9876543210 skipped
    assert out["email"] == "saad@real.com"
    assert out["github"].endswith("/saadrizvi09")
    assert "linkedin" not in out                   # CHANGE-ME placeholder skipped
    assert out["leetcode"].endswith("/saadrizvi1234")


def test_ingest_sets_form_status():
    from app.db import ingest_referral_jobs, list_form_jobs
    jobs = referrals.parse_heuristic(FIXTURE.read_text(encoding="utf-8"))
    with _temp_session() as s:
        ingest_referral_jobs(s, jobs)
        form_jobs = list_form_jobs(s)
        assert len(form_jobs) == 5
        assert all(fj["status"] == "form_found" for fj in form_jobs)
        assert all(fj["form_url"].startswith("https://docs.google.com/forms/") for fj in form_jobs)
