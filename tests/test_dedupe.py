"""Posting dedupe (Technical-Spec §5.1 / FR-4)."""
from app.logic import dedupe_postings


def test_dedupe_by_source_url():
    postings = [
        {"source_url": "https://x.com/job/1", "domain": "x.com", "role_title": "SDE 1"},
        {"source_url": "https://x.com/job/1", "domain": "x.com", "role_title": "SDE 1"},
    ]
    assert len(dedupe_postings(postings)) == 1


def test_dedupe_by_domain_and_role_even_with_different_urls():
    postings = [
        {"source_url": "https://x.com/a", "domain": "x.com", "role_title": "SDE 1"},
        {"source_url": "https://x.com/b", "domain": "x.com", "role_title": "SDE 1"},
    ]
    out = dedupe_postings(postings)
    assert len(out) == 1
    assert out[0]["source_url"] == "https://x.com/a"  # first kept


def test_same_domain_different_role_kept():
    postings = [
        {"source_url": "https://x.com/a", "domain": "x.com", "role_title": "SDE 1"},
        {"source_url": "https://x.com/b", "domain": "x.com", "role_title": "Intern"},
    ]
    assert len(dedupe_postings(postings)) == 2


def test_url_match_case_insensitive():
    postings = [
        {"source_url": "https://X.com/Job/1", "domain": "x.com", "role_title": "SDE"},
        {"source_url": "https://x.com/job/1", "domain": "x.com", "role_title": "SDE"},
    ]
    assert len(dedupe_postings(postings)) == 1


def test_empty_source_urls_not_collapsed():
    # Blank URLs must not all collapse into a single record.
    postings = [
        {"source_url": "", "domain": "a.com", "role_title": "SDE 1"},
        {"source_url": "", "domain": "b.com", "role_title": "Intern"},
    ]
    assert len(dedupe_postings(postings)) == 2


def test_preserves_order():
    postings = [
        {"source_url": "u1", "domain": "a.com", "role_title": "r1"},
        {"source_url": "u2", "domain": "b.com", "role_title": "r2"},
        {"source_url": "u3", "domain": "c.com", "role_title": "r3"},
    ]
    out = dedupe_postings(postings)
    assert [p["source_url"] for p in out] == ["u1", "u2", "u3"]


def test_empty_input():
    assert dedupe_postings([]) == []
