"""Prompt templates (Technical-Spec §7)."""
from __future__ import annotations

# --- 7.1 Draft prompt ------------------------------------------------------------

DRAFT_SYSTEM = (
    "You write concise, specific job-application emails for an SDE-1/intern "
    "candidate in India. Output ONLY the email body, 90-130 words, plain text, "
    "professional but warm. No placeholders, no markdown, no links. Reference the "
    "exact role and one concrete detail about the company. End with the provided "
    "signature."
)

DRAFT_USER_TEMPLATE = (
    "Role: {role_title}\n"
    "Company: {company_name}\n"
    "Company detail: {scraped_detail}\n"
    "Candidate summary: {cv_summary}\n"
    "Signature: {signature}\n"
    "Write the email body now."
)

# --- International / remote cold-outreach variant --------------------------------
# For early-stage US/EU/foreign startups: lead with the remote-from-India
# cost/quality value proposition, since that is what converts a founder.

DRAFT_SYSTEM_INTL = (
    "You write short, direct cold job-application emails from an India-based "
    "AI/backend engineer to the FOUNDER or hiring lead of an early-stage "
    "international (US / EU / global) startup. The role is REMOTE. Output ONLY the "
    "email body, 90-130 words, plain text, warm and confident, no markdown, no "
    "links, no placeholders. Weave in, naturally: (1) genuine interest in the exact "
    "role and one concrete detail about the company; (2) the candidate is a strong "
    "AI/backend engineer who works remotely from India and delivers senior-quality "
    "work at a fraction of the cost of a comparable US/EU hire - a real advantage "
    "for a lean, fast-moving startup; (3) willingness to overlap with the team's "
    "timezone. Mention a CV is attached. End with the provided signature. Frame the "
    "cost point as smart value, never as 'cheap labour'."
)

DRAFT_USER_TEMPLATE_INTL = (
    "Role (remote): {role_title}\n"
    "Company: {company_name}\n"
    "Company detail: {scraped_detail}\n"
    "Company location/timezone: {location}\n"
    "Candidate summary: {cv_summary}\n"
    "Signature: {signature}\n"
    "Write the remote cold-outreach email body now, leading with genuine interest "
    "and weaving in the cost-effective remote-from-India advantage."
)


def draft_user_prompt(
    role_title: str,
    company_name: str,
    scraped_detail: str,
    cv_summary: str,
    signature: str,
) -> str:
    return DRAFT_USER_TEMPLATE.format(
        role_title=role_title,
        company_name=company_name,
        scraped_detail=scraped_detail,
        cv_summary=cv_summary,
        signature=signature,
    )


def draft_user_prompt_intl(
    role_title: str,
    company_name: str,
    scraped_detail: str,
    location: str,
    cv_summary: str,
    signature: str,
) -> str:
    return DRAFT_USER_TEMPLATE_INTL.format(
        role_title=role_title,
        company_name=company_name,
        scraped_detail=scraped_detail,
        location=location or "Remote",
        cv_summary=cv_summary,
        signature=signature,
    )


def subject_for(role_title: str, sender_name: str) -> str:
    # Templated per §7.1: "Application - {role_title} - {SENDER_NAME}"
    return f"Application - {role_title} - {sender_name}"


# --- 7.2 Reply-classification prompt ---------------------------------------------

CLASSIFY_SYSTEM = (
    "You classify a single email reply to a job application. Respond with ONLY one "
    "token from: INTERVIEW, REJECTION, NEEDINFO, AUTO_ACK, OTHER. INTERVIEW = "
    "invites to call/test/next step. REJECTION = not moving forward. NEEDINFO = "
    "asks for documents/details. AUTO_ACK = automated 'we received your "
    "application'. OTHER = anything else."
)

# Maps the model's token to an application status (OTHER keeps 'sent').
CLASSIFY_STATUS_MAP = {
    "INTERVIEW": "replied_interview",
    "REJECTION": "replied_rejection",
    "NEEDINFO": "replied_needinfo",
    "AUTO_ACK": "auto_ack",
    "OTHER": None,  # keep status 'sent', just store the excerpt
}
