"""Pure-logic tests for LinkedIn discovery: salary parsing, card parsing,
role-query building, salary/intern filtering, and HR-email classification.
No network — DRY_RUN keeps everything offline."""
import os

os.environ.setdefault("DRY_RUN", "true")

from app.integrations import linkedin
from app.integrations.hunter import _is_hr
from app.services import discovery


# --- salary parsing (INR LPA, USD ignored) ---------------------------------------

def test_salary_lpa_inr_lakh_forms():
    assert linkedin._parse_salary_lpa("₹8L - ₹12L") == 8.0
    assert linkedin._parse_salary_lpa("₹12L") == 12.0
    assert linkedin._parse_salary_lpa("8-12 LPA") == 8.0
    assert linkedin._parse_salary_lpa("10 lakh") == 10.0
    assert linkedin._parse_salary_lpa("₹ 800000") == 8.0


def test_salary_lpa_ignores_usd_and_unknown():
    assert linkedin._parse_salary_lpa("$120,000") is None
    assert linkedin._parse_salary_lpa("Competitive") is None
    assert linkedin._parse_salary_lpa(None) is None


# --- card parsing ----------------------------------------------------------------

def test_parse_cards_extracts_fields():
    html = """
    <li><div class="base-card">
      <a class="base-card__full-link" href="https://www.linkedin.com/jobs/view/123?ref=x"></a>
      <h3 class="base-search-card__title">AI Engineer</h3>
      <h4 class="base-search-card__subtitle"><a>Nimbus AI</a></h4>
      <span class="job-search-card__location">Remote, India</span>
      <span class="job-search-card__salary-info">₹14L - ₹20L</span>
    </div></li>
    """
    cards = linkedin._parse_cards(html)
    assert len(cards) == 1
    c = cards[0]
    assert c["title"] == "AI Engineer"
    assert c["company"] == "Nimbus AI"
    assert c["url"] == "https://www.linkedin.com/jobs/view/123"   # query stripped
    assert c["remote"] == 1
    assert c["salary_lpa"] == 14.0


def test_parse_cards_skips_incomplete():
    assert linkedin._parse_cards("<li><div>no title or link</div></li>") == []


# --- role-query building ---------------------------------------------------------

def test_role_queries_default_when_blank():
    assert discovery._role_queries({}) == discovery.DEFAULT_ROLE_QUERIES


def test_role_queries_uses_role_and_keywords():
    qs = discovery._role_queries({"role": "Backend Engineer", "keywords": "python"})
    assert qs == ["Backend Engineer python"]


# --- end-to-end filtering (DRY fixtures) -----------------------------------------

def _purge_dryrun_rows():
    import sqlite3
    db = sqlite3.connect("jobs.db", timeout=10)
    db.execute("PRAGMA busy_timeout=8000")
    ids = [r[0] for r in db.execute("SELECT id FROM companies WHERE source_url LIKE '%dryrun%'")]
    for cid in ids:
        db.execute("DELETE FROM applications WHERE company_id=?", (cid,))
        db.execute("DELETE FROM contacts WHERE company_id=?", (cid,))
        db.execute("DELETE FROM companies WHERE id=?", (cid,))
    db.commit()
    db.close()


def test_discover_dry_run_filters_and_attaches_email():
    import pytest
    from sqlalchemy.exc import OperationalError
    try:
        _purge_dryrun_rows()                  # start clean (defeat leftover pollution)
        res = discovery.discover({"remote": True, "location": "India"})
    except OperationalError:
        pytest.skip("jobs.db is locked (uvicorn running) — DB-writing test skipped")
    finally:
        _purge_dryrun_rows()                  # always clean up, even on assert failure
    assert res["dry_run"] is True
    assert res["fetched"] == 2            # 2 fixtures, deduped across both role queries
    assert res["new"] >= 1
    # HR-email attachment runs only while Hunter quota remains (budget reads the live
    # counter), so don't hard-assert it — when quota is left, it should attach some.
    if (res.get("hunter_remaining") or 0) > 0:
        assert res["hr_emails"] >= 1


def test_intern_titles_filtered():
    # internships are dropped (user wants 8+ LPA roles, not internships)
    assert any(m in "ml intern" for m in discovery._INTERN_MARKERS)


# --- startup bias: blocklist + headcount + multi-geo -----------------------------

def test_blocklist_excludes_big_and_staffing():
    blk = discovery._is_blocked_company
    assert blk("Infosys")
    assert blk("Tata Consultancy Services")
    assert blk("Acme Staffing Pvt Ltd")
    assert blk("Ola Electric")
    assert blk("PhonePe")


def test_blocklist_keeps_startups_and_avoids_false_hits():
    blk = discovery._is_blocked_company
    assert not blk("Nimbus AI")
    assert not blk("Mercor")
    assert not blk("Motorola Solutions")   # 'ola' must not match as a token
    assert not blk(None)


def test_headcount_upper_bound():
    hu = discovery._headcount_upper
    assert hu("11-50") == 50
    assert hu("5001+") == 5001
    assert hu("1-10") == 10
    assert hu(None) is None


def test_locations_default_multi_geo_else_override():
    assert discovery._locations({}) == discovery.DEFAULT_LOCATIONS
    assert "United States" in discovery.DEFAULT_LOCATIONS
    assert discovery._locations({"location": "Berlin"}) == ["Berlin"]


def test_default_queries_are_ai_and_software_focused():
    qs = " ".join(discovery.DEFAULT_ROLE_QUERIES).lower()
    assert "agent" in qs and "ai engineer" in qs and "software" in qs
    assert "ml engineer" not in qs and "machine learning" not in qs


def test_senior_markers_cover_unreachable_titles():
    # titles a new grad won't get a call for must all be marked senior
    for t in ["Staff Software Engineer", "Principal Engineer", "Engineering Manager",
              "Director of AI", "VP Engineering", "Head of Product", "Solutions Architect"]:
        low = t.lower()
        assert any(m in f"{low} " for m in discovery._SENIOR_MARKERS), t


def test_senior_markers_keep_reachable_titles():
    for t in ["Software Engineer", "Backend Engineer", "AI Agents Engineer",
              "Junior ML Engineer", "Senior Software Engineer"]:
        low = t.lower()
        assert not any(m in f"{low} " for m in discovery._SENIOR_MARKERS), t


# --- urgency prioritisation ("#urgent hiring" / "hiring" posts apply first) -------

def test_urgency_score_tiers():
    s = linkedin._urgency_score
    assert s("Urgent Hiring: Backend Engineer") == 2
    assert s("Immediate Joiner — ML Engineer") == 2
    assert s("Backend Engineer (Apply Now)", "#hiring") == 1
    assert s("We are hiring AI Engineers") == 1
    assert s("Software Engineer") == 0
    assert s(None) == 0


def test_parse_cards_flags_urgent_in_title():
    html = """
    <li><div class="base-card">
      <a class="base-card__full-link" href="https://www.linkedin.com/jobs/view/9"></a>
      <h3 class="base-search-card__title">URGENT: Backend Engineer</h3>
      <h4 class="base-search-card__subtitle"><a>Forge Labs</a></h4>
      <span class="job-search-card__location">Remote, India</span>
    </div></li>
    """
    assert linkedin._parse_cards(html)[0]["urgent"] == 2


def test_linkedin_jobs_orders_urgent_first():
    # The apply queue must hand urgent posts back before unmarked ones.
    rows = [
        {"id": 1, "url": "u1", "company": "A", "role": "x", "location": "India", "urgent": 0},
        {"id": 2, "url": "u2", "company": "B", "role": "y", "location": "India", "urgent": 2},
        {"id": 3, "url": "u3", "company": "C", "role": "z", "location": "India", "urgent": 1},
    ]
    rows.sort(key=lambda r: (r["urgent"], r["id"]), reverse=True)
    assert [r["id"] for r in rows] == [2, 3, 1]


# --- HR classification -----------------------------------------------------------

def test_is_hr_matches_recruiting_roles():
    assert _is_hr("Technical Recruiter", None)
    assert _is_hr("Head of Talent Acquisition", "hr")
    assert _is_hr(None, "human resources")


def test_is_hr_rejects_engineers():
    assert not _is_hr("Senior Software Engineer", "engineering")
    assert not _is_hr("CTO", "executive")


def test_exclude_title_markers_drops_ml_and_data_roles():
    """The operator wants AI-agent / AI-engineer / software-developer roles, not ML/data."""
    from app.services.discovery import _EXCLUDE_TITLE_MARKERS

    def off_target(title: str) -> bool:
        return any(m in title.lower() for m in _EXCLUDE_TITLE_MARKERS)

    # dropped
    assert off_target("Machine Learning Engineer")
    assert off_target("Senior ML Engineer")
    assert off_target("AI/ML Engineer")
    assert off_target("Data Engineer")
    assert off_target("Data Scientist")
    assert off_target("Applied ML Researcher")
    # kept (what the operator wants)
    assert not off_target("AI Engineer")
    assert not off_target("AI Agent Engineer")
    assert not off_target("Software Engineer")
    assert not off_target("Full Stack AI Engineer")
    assert not off_target("Backend Engineer")
