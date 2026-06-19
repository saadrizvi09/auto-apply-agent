"""Email pattern generator (Technical-Spec §5.2 step 3 / FR-8)."""
from app.logic import generate_email_patterns


def test_full_name_order_and_specificity():
    out = generate_email_patterns("John", "Smith", "acme.com")
    assert out == [
        "john.smith@acme.com",
        "jsmith@acme.com",
        "john@acme.com",
        "careers@acme.com",
        "jobs@acme.com",
        "hr@acme.com",
        "talent@acme.com",
    ]


def test_domain_normalized():
    out = generate_email_patterns("Jane", "Doe", "@Acme.COM")
    assert out[0] == "jane.doe@acme.com"
    assert all(addr.endswith("@acme.com") for addr in out)


def test_no_name_only_role_inboxes():
    out = generate_email_patterns(None, None, "acme.com")
    assert out == [
        "careers@acme.com",
        "jobs@acme.com",
        "hr@acme.com",
        "talent@acme.com",
    ]


def test_names_with_punctuation_and_accents_cleaned():
    out = generate_email_patterns("Jo-Anne", "O'Brien", "acme.com")
    # non-letters stripped: joanne + obrien
    assert out[0] == "joanne.obrien@acme.com"
    assert out[1] == "jobrien@acme.com"


def test_first_name_only():
    out = generate_email_patterns("Sam", None, "acme.com")
    assert out[0] == "sam@acme.com"
    assert "careers@acme.com" in out
    # no first.last / flast without a last name
    assert not any(p.startswith("sam.") for p in out)


def test_empty_domain_returns_empty():
    assert generate_email_patterns("John", "Smith", "") == []
    assert generate_email_patterns("John", "Smith", None) == []


def test_no_duplicates():
    out = generate_email_patterns("Careers", None, "acme.com")
    # 'careers' as first name collides with the role inbox -> emitted once
    assert out.count("careers@acme.com") == 1
