"""File import stage: bulk-add companies + contacts from a CSV or Excel file.

An alternative to Custom Search discovery — the operator supplies a curated list
(company, role, email, ...) and the rows become `companies` + `contacts` +
`applications` records, ready for ② Find Contacts (Hunter verify) → ③ Draft → send.

Header matching is flexible/case-insensitive. Recognized columns:
  company (required) · role · email · domain · location · apply_url · verified
Rows with an email become status `email_found`; rows with only an apply_url stay
`discovered`. Imported emails are stored unverified (confidence NULL) unless a
truthy `verified` column is given, so ② Find Contacts can verify them via Hunter.
"""
from __future__ import annotations

import csv
import io
from datetime import datetime

from ..db import (
    create_application,
    create_company,
    create_contact,
    find_company_by_source_url,
    find_contact_by_email,
    get_session,
    link_contact,
)
from ..logging_setup import log_event

# Canonical column -> accepted header variants (all lowercased on compare).
HEADER_SYNONYMS = {
    "company": {"company", "company name", "organization", "organisation", "org", "employer", "company_name"},
    "role": {"role", "title", "position", "job title", "job_title", "job", "role title"},
    "email": {"email", "email address", "e-mail", "contact email", "mail", "email_address"},
    "domain": {"domain", "website", "site", "web", "url domain"},
    "location": {"location", "city", "place", "loc"},
    "salary": {"salary", "ctc", "compensation", "pay", "package", "stipend", "lpa"},
    "apply_url": {"apply_url", "apply url", "apply link", "application url", "link", "url", "apply"},
    "verified": {"verified", "is verified", "trusted", "confirmed"},
}

TEMPLATE_HEADERS = ["company", "role", "email", "domain", "location", "salary", "apply_url", "verified"]


def template_csv() -> str:
    rows = [
        TEMPLATE_HEADERS,
        ["Acme Corp", "SDE-1 Backend", "careers@acme.com", "acme.com", "Gurgaon", "8-12 LPA", "", "no"],
        ["Bolt Labs", "Software Engineering Intern", "jobs@boltlabs.io", "boltlabs.io", "Remote", "40k/mo", "", "no"],
        ["Cobalt", "Frontend Engineer", "", "cobalt.dev", "Noida", "", "https://cobalt.dev/careers", "no"],
    ]
    buf = io.StringIO()
    csv.writer(buf).writerows(rows)
    return buf.getvalue()


def _norm(h) -> str:
    return str(h).strip().lower() if h is not None else ""


def _map_headers(headers: list) -> dict:
    """Map canonical column name -> column index, from a header row."""
    mapping: dict[str, int] = {}
    for i, h in enumerate(headers):
        key = _norm(h)
        for canon, variants in HEADER_SYNONYMS.items():
            if key in variants and canon not in mapping:
                mapping[canon] = i
    return mapping


def _cell(cells: list, mapping: dict, key: str) -> str:
    i = mapping.get(key)
    if i is None or i >= len(cells) or cells[i] is None:
        return ""
    return str(cells[i]).strip()


def _row_to_dict(cells: list, mapping: dict) -> dict:
    return {k: _cell(cells, mapping, k) for k in HEADER_SYNONYMS}


def _rows_from_matrix(matrix: list[list]) -> list[dict]:
    # Find the first non-empty row to use as the header.
    header_idx = next((i for i, r in enumerate(matrix) if any(_norm(c) for c in r)), None)
    if header_idx is None:
        return []
    mapping = _map_headers(matrix[header_idx])
    if not mapping:
        raise ValueError(
            "No recognized columns. Expected at least one of: "
            + ", ".join(TEMPLATE_HEADERS)
        )
    return [_row_to_dict(r, mapping) for r in matrix[header_idx + 1:]]


def parse_csv(data: bytes) -> list[dict]:
    text = data.decode("utf-8-sig", errors="replace")
    matrix = list(csv.reader(io.StringIO(text)))
    return _rows_from_matrix(matrix)


def parse_xlsx(data: bytes) -> list[dict]:
    import openpyxl  # lazy: CSV import works without it

    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    ws = wb.active
    matrix = [[c.value for c in row] for row in ws.iter_rows()]
    wb.close()
    return _rows_from_matrix(matrix)


def _truthy(v: str) -> bool:
    return _norm(v) in {"1", "true", "yes", "y", "verified", "trusted"}


def _titleize(slug: str) -> str:
    return " ".join(w.capitalize() for w in slug.replace("-", " ").replace("_", " ").split())


def import_rows(rows: list[dict]) -> dict:
    summary = {"imported": 0, "skipped": 0, "blank": 0, "with_email": 0, "portal_only": 0, "message": ""}
    seen_emails: set[str] = set()
    seen_urls: set[str] = set()

    with get_session() as session:
        for row in rows:
            email = _norm(row.get("email"))
            company = row.get("company", "").strip()
            role = row.get("role", "").strip() or "Role"
            domain = (row.get("domain", "").strip().lower().lstrip("@")) or (
                email.split("@", 1)[1] if "@" in email else ""
            )
            apply_url = row.get("apply_url", "").strip()
            location = row.get("location", "").strip() or None
            salary = row.get("salary", "").strip() or None

            # Derive a company name if missing.
            if not company:
                if domain:
                    company = _titleize(domain.split(".")[0])
                elif email:
                    company = _titleize(email.split("@", 1)[0])

            # Blank/unusable row.
            if not company and not email and not apply_url:
                summary["blank"] += 1
                continue

            source_url = ("import://" + (email or f"{company}/{role}")).lower()

            # Dedupe (within file + against DB).
            if email and (email in seen_emails or find_contact_by_email(session, email)):
                summary["skipped"] += 1
                continue
            if source_url in seen_urls or find_company_by_source_url(session, source_url):
                summary["skipped"] += 1
                continue

            verified = _truthy(row.get("verified"))
            company_rec = create_company(
                session,
                name=company or "Unknown",
                domain=domain or None,
                source_url=source_url,
                role_title=role,
                location=location,
                salary=salary,
                remote=1 if (location and location.lower() == "remote") else 0,
                discovered_at=datetime.now().isoformat(timespec="seconds"),
            )
            contact = create_contact(
                session,
                company_id=company_rec.id,
                email=email or None,
                apply_url=apply_url or None,
                source="import",
                verified=1 if verified else 0,
                confidence=1.0 if verified else None,
                created_at=datetime.now().isoformat(timespec="seconds"),
            )
            status = "email_found" if email else "discovered"
            app = create_application(session, company_id=company_rec.id, status=status)
            link_contact(session, app, contact.id, status)

            if email:
                seen_emails.add(email)
                summary["with_email"] += 1
            else:
                summary["portal_only"] += 1
            seen_urls.add(source_url)
            summary["imported"] += 1

    summary["message"] = (
        f"Imported {summary['imported']} row(s) "
        f"({summary['with_email']} with email, {summary['portal_only']} portal-only); "
        f"skipped {summary['skipped']} duplicate(s)"
        + (f", {summary['blank']} blank" if summary["blank"] else "")
        + ". Run ② Find Contacts to verify the emails."
    )
    log_event("import", "batch", "ok", summary["message"])
    return summary
