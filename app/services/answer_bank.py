"""Learning answer-bank for LinkedIn Easy-Apply screening questions.

Two layers, in order of trust:

  1. A persistent JSON bank (answer_bank.json) keyed by the NORMALISED question text.
     Every answer the agent commits to (from the profile map, the LLM, or a human in
     an assisted run) is written here, so the *next* time the same question appears it
     is answered instantly with no guessing. After a few runs the bank covers almost
     everything the operator's roles ask.

  2. An LLM fallback (Groq) for open-ended questions the static profile map and the
     bank don't cover ("Why do you want this role?", "Rate your Python 1-10", custom
     numeric/skill questions). It answers truthfully FROM THE PROFILE ONLY and is told
     to reply 'UNKNOWN' when it can't — in which case the caller skips the job rather
     than submit a guess ("skip if unsure"). Whatever it does answer is banked.

The bank file is git-ignored (personal data). In DRY_RUN the LLM is never called
(groq_client short-circuits), so this module stays import-safe and offline for tests.
"""
from __future__ import annotations

import json
import re
import threading

from ..config import ROOT
from ..integrations import groq_client
from ..logging_setup import log_event
from ..profile import context_block

BANK_PATH = ROOT / "answer_bank.json"

_LOCK = threading.Lock()
_CACHE: dict[str, str] | None = None


def _normalize(question: str) -> str:
    """Stable key for a question: lowercased, whitespace-collapsed, trailing
    punctuation / required-asterisks stripped. Two phrasings that differ only in
    spacing or a trailing '*'/'?' map to the same answer."""
    q = (question or "").strip().lower()
    q = re.sub(r"\s+", " ", q)
    return q.rstrip(" *?:.").strip()


def _load() -> dict[str, str]:
    global _CACHE
    if _CACHE is None:
        data: dict[str, str] = {}
        if BANK_PATH.exists():
            try:
                raw = json.loads(BANK_PATH.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    data = {str(k): str(v) for k, v in raw.items() if v}
            except (ValueError, OSError):
                data = {}
        _CACHE = data
    return _CACHE


def get(question: str) -> str | None:
    """A previously-learned answer for this question, or None."""
    key = _normalize(question)
    if not key:
        return None
    val = _load().get(key)
    return val or None


def remember(question: str, answer: str) -> None:
    """Persist a question -> answer so future runs answer it instantly."""
    key = _normalize(question)
    ans = (answer or "").strip()
    if not key or not ans:
        return
    with _LOCK:
        bank = _load()
        if bank.get(key) == ans:
            return
        bank[key] = ans
        try:
            BANK_PATH.write_text(json.dumps(bank, indent=2, ensure_ascii=False), encoding="utf-8")
        except OSError as e:  # noqa: BLE001
            log_event("answer_bank", "remember", "error", str(e))


def count() -> int:
    """How many answers are currently banked (for run summaries)."""
    return len(_load())


_LLM_SYSTEM = (
    "You fill in job-application screening questions on behalf of a candidate. "
    "Answer TRUTHFULLY using ONLY the candidate profile provided. "
    "Reply with the answer text ONLY — no preamble, no explanation, no quotes. "
    "For a numeric question reply with a number only. "
    "For a yes/no question reply exactly 'Yes' or 'No'. "
    "If a list of options is given, reply with EXACTLY one of them, verbatim. "
    "If you cannot answer truthfully from the profile, reply with exactly 'UNKNOWN'."
)


def llm_answer(question: str, profile: dict, options: list[str] | None = None,
               self_id: bool = False) -> str | None:
    """Ask the LLM to answer one screening question from the profile. Returns the
    answer string, or None to SKIP (the LLM said UNKNOWN, the call failed, or — for a
    multiple-choice question — the answer didn't match any offered option).

    self_id=True marks a voluntary self-identification question (gender/disability/
    veteran/transgender/…): the LLM answers from the profile when it can, otherwise picks
    a 'prefer not to say' option (if offered) instead of leaving a required field blank.

    Never called in DRY_RUN (groq_client returns '' there) — returns None instead."""
    q = (question or "").strip()
    if not q:
        return None
    opts = [o for o in (options or []) if o and o.strip()]
    user = f"Candidate profile:\n{context_block(profile)}\n\nQuestion: {q}"
    if opts:
        user += "\nChoose exactly one of these options: " + " | ".join(opts)
    if self_id:
        user += ("\nThis is a voluntary self-identification question. If the profile "
                 "clearly implies the answer, answer truthfully (Yes/No or the matching "
                 "option); if it cannot be determined, choose a 'prefer not to say' / "
                 "'decline to self-identify' option if offered, otherwise reply 'UNKNOWN'.")
    user += "\nAnswer:"
    try:
        raw = groq_client.chat(_LLM_SYSTEM, user, temperature=0.2, max_tokens=120)
    except Exception as e:  # noqa: BLE001 — never let an LLM hiccup crash a run
        log_event("answer_bank", "llm", "error", str(e)[:160])
        return None
    ans = (raw or "").strip().strip('"').strip()
    if not ans or ans.upper() == "UNKNOWN":
        return None
    if opts:  # must map to a real option, else we'd submit an invalid choice -> skip
        low = ans.lower()
        for o in opts:
            if o.lower() == low or o.lower() in low or low in o.lower():
                return o
        return None
    return ans
