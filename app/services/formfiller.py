"""Map Google-Form questions to answers from the applicant profile.

Two layers:
  - match_field()  : keyword-match a question title to a profile field (pure).
  - plan_answers() : for a list of parsed questions, decide each answer -
                     a profile value, a chosen option, or "needs LLM" for
                     open-ended text (e.g. "why should we hire you").

The browser layer (integrations/browser.py) reads questions + types them; this
module is pure so it is unit-testable without a browser. Open questions are
answered by Groq in real mode and by a deterministic stub in DRY_RUN.
"""
from __future__ import annotations

import re

from ..config import settings
from ..integrations import groq_client
from ..profile import context_block, load_profile

# Question-title keyword -> profile field. Order matters: specific before generic
# (so "College Name" matches college, not full_name).
FIELD_MATCHERS: list[tuple[str, str]] = [
    ("email", r"e-?mail"),
    ("phone", r"phone|mobile|contact number|whatsapp|contact no\b"),
    ("linkedin", r"linkedin"),
    ("github", r"github"),
    ("geeksforgeeks", r"geeksforgeeks|geeks for geeks|\bgfg\b"),
    ("leetcode", r"leetcode|codeforces|codechef|hackerrank|coding profile|competitive programming"),
    ("cv_url", r"resume|cv\b|drive link|resume link|cv link"),
    ("portfolio", r"portfolio|personal website|website"),
    # college BEFORE graduation_year, and "graduation" not "graduat" — so
    # "Undergraduate College" maps to the college name, not the grad year.
    ("college", r"college|university|institute|institution"),
    ("graduation_year", r"graduation|passing year|year of passing|passout|pass-out|batch|year of grad"),
    ("degree", r"degree|branch|stream|qualification|\bcourse\b|specialization"),
    ("expected_stipend", r"expected stipend|expected ctc|expected salary|stipend expect|salary expect"),
    ("current_ctc", r"current ctc|current salary"),
    ("years_experience", r"experience|years of exp|work ex"),
    ("notice_period", r"notice period|availability|when can you (?:join|start)|date of joining"),
    ("willing_to_relocate", r"relocat"),
    ("city", r"current city|current location|\bcity\b|located in|residence|hometown"),
    ("gender", r"\bgender\b|\bsex\b"),
    ("full_name", r"full name|your name|candidate name|\bname\b"),
]


def match_field(question_title: str) -> str | None:
    ql = (question_title or "").lower()
    for key, pat in FIELD_MATCHERS:
        if re.search(pat, ql):
            return key
    return None


def best_option(value: str, options: list[str]) -> str | None:
    """Pick the option that best matches `value` (case/substring tolerant)."""
    if not value or not options:
        return None
    v = value.strip().lower()
    for opt in options:                       # exact
        if opt.strip().lower() == v:
            return opt
    for opt in options:                       # value inside option (e.g. 2026 in "2025/2026/2027")
        if v in opt.lower():
            return opt
    for opt in options:                       # option token inside value
        if opt.strip().lower() in v:
            return opt
    return None


_YESNO = re.compile(r"^\s*(yes|no)\b", re.I)


def plan_answers(questions: list[dict], profile: dict[str, str] | None = None) -> list[dict]:
    """Decide an answer for each question.

    questions: [{"title","type","options":[...],"required":bool}, ...]
      type in: SHORT_TEXT, PARAGRAPH, MULTIPLE_CHOICE, DROPDOWN, CHECKBOXES,
               LINEAR_SCALE, DATE, EMAIL, FILE_UPLOAD
    Returns the same dicts plus: answer (str), source, needs_llm (bool),
      blocked (bool, e.g. file upload that can't be auto-done).
    """
    p = profile or load_profile()
    out = []
    for q in questions:
        title = q.get("title", "")
        qtype = (q.get("type") or "SHORT_TEXT").upper()
        options = q.get("options") or []
        required = bool(q.get("required"))
        plan = dict(q, answer="", source="empty", needs_llm=False, blocked=False)

        if qtype == "FILE_UPLOAD":
            plan.update(blocked=True, source="file_upload")
            out.append(plan)
            continue

        key = match_field(title)
        value = p.get(key, "") if key else ""
        is_choice = qtype in ("MULTIPLE_CHOICE", "DROPDOWN", "CHECKBOXES")

        if key:
            # A known profile field. Use its value; if blank, flag MISSING
            # (the operator must fill profile.json) - never hallucinate it.
            if is_choice:
                chosen = best_option(value, options) if value else None
                if not chosen and key == "willing_to_relocate":
                    chosen = next((o for o in options if _YESNO.match(o)), None)
                if not chosen and key == "graduation_year" and p.get("graduation_year"):
                    chosen = best_option(p["graduation_year"], options)
                if chosen:
                    plan.update(answer=chosen, source="option")
                elif value:                       # have a value but no option fit -> LLM picks
                    plan.update(needs_llm=True, source="llm")
                else:
                    plan.update(source="missing", missing_field=key)
            elif value:
                plan.update(answer=value, source="profile")
            else:
                plan.update(source="missing", missing_field=key)
        else:
            # No known field -> genuinely open question. LLM answers (incl. option pick).
            if is_choice or qtype == "PARAGRAPH" or required:
                plan.update(needs_llm=True, source="llm")
            # else: optional unknown short text -> leave blank

        out.append(plan)
    return out


# --- LLM answers for open questions ----------------------------------------------

_OPEN_SYSTEM = (
    "You answer a single job-application form question as the candidate, in the "
    "first person. Be specific, honest, and concise. For a free-text question give "
    "2-4 sentences. If the question lists OPTIONS, reply with EXACTLY one of the "
    "options verbatim and nothing else. No preamble, no quotes."
)


def answer_open_question(question: dict, profile_ctx: str) -> str:
    title = question.get("title", "")
    options = question.get("options") or []
    role = question.get("_role", "")
    company = question.get("_company", "")
    opts = f"\nOPTIONS (choose one verbatim): {options}" if options else ""
    user = (
        f"Candidate profile:\n{profile_ctx}\n\n"
        f"Applying for: {role} at {company}\n"
        f"Question: {title}{opts}\n\nYour answer:"
    )
    return groq_client.chat(_OPEN_SYSTEM, user, temperature=0.5, max_tokens=220).strip()


def _dry_open_answer(question: dict, profile_ctx: str) -> str:
    """Deterministic offline answer so DRY_RUN can preview without Groq."""
    options = question.get("options") or []
    if options:
        return options[0]
    return (
        "I am a final-year B.Tech (ECE) AI/backend engineer who builds with Python, "
        "FastAPI, Next.js and LLM/agent/RAG systems. I ship production-quality code, "
        "learn fast, and would be excited to contribute to this team from day one."
    )


def fill_open_answers(planned: list[dict], company: str = "", role: str = "") -> list[dict]:
    """Resolve every needs_llm answer (Groq in real mode, stub in DRY_RUN)."""
    ctx = context_block()
    for plan in planned:
        if not plan.get("needs_llm"):
            continue
        plan["_company"], plan["_role"] = company, role
        try:
            plan["answer"] = (
                _dry_open_answer(plan, ctx) if settings.dry_run
                else answer_open_question(plan, ctx)
            )
            plan["needs_llm"] = False
        except Exception:
            plan["answer"] = _dry_open_answer(plan, ctx)
            plan["needs_llm"] = False
        plan.pop("_company", None)
        plan.pop("_role", None)
    return planned
