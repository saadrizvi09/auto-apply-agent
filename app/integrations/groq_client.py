"""Groq chat-completions wrapper (OpenAI-compatible) for drafting + classifying.

Uses the OpenAI SDK pointed at Groq's base URL, model llama-3.3-70b-versatile.
An in-process token-bucket rate limiter keeps batch calls under ~30 rpm so we don't
429 (Technical-Spec §9). In DRY_RUN no network call is made: drafting and reply
classification fall back to deterministic local logic so the pipeline is testable
without consuming Groq quota.
"""
from __future__ import annotations

import threading
import time

from ..config import settings
from ..logging_setup import log_event

GROQ_BASE_URL = "https://api.groq.com/openai/v1"
GROQ_MODEL = "llama-3.3-70b-versatile"
MAX_RETRIES = 3


class _RateLimiter:
    """Simple token bucket: `rate` tokens refilled per `per` seconds."""

    def __init__(self, rate: int = 25, per: float = 60.0):
        self.capacity = rate
        self.tokens = float(rate)
        self.per = per
        self.rate = rate
        self._lock = threading.Lock()
        self._last = time.monotonic()

    def acquire(self) -> None:
        with self._lock:
            now = time.monotonic()
            self.tokens = min(
                self.capacity, self.tokens + (now - self._last) * (self.rate / self.per)
            )
            self._last = now
            if self.tokens < 1:
                wait = (1 - self.tokens) * (self.per / self.rate)
                time.sleep(wait)
                self.tokens = 0
            else:
                self.tokens -= 1


_limiter = _RateLimiter(rate=25, per=60.0)
_clients: dict[str, object] = {}
# Index into the available keys; persists across calls so once the primary key is
# rate-limited we stay on the backup (which has fresh quota) instead of hammering it.
_active_key = 0


def _api_keys() -> list[tuple[str, str]]:
    """(label, key) pairs to try, primary first, backup (if set) second."""
    keys = [("primary", settings.groq_api_key)]
    if settings.groq_api_key_backup:
        keys.append(("backup", settings.groq_api_key_backup))
    return keys


def _client_for(api_key: str):
    """Build (and cache) an OpenAI client for a given Groq key."""
    if api_key not in _clients:
        from openai import OpenAI

        _clients[api_key] = OpenAI(api_key=api_key, base_url=GROQ_BASE_URL)
    return _clients[api_key]


def _is_rate_limit(e: Exception) -> bool:
    """True if the exception is an API rate-limit (HTTP 429)."""
    code = getattr(e, "status_code", None) or getattr(
        getattr(e, "response", None), "status_code", None
    )
    if code == 429:
        return True
    s = str(e).lower()
    return "rate limit" in s or "rate_limit" in s or "429" in s or "too many requests" in s


def chat(system: str, user: str, temperature: float = 0.7, max_tokens: int = 400) -> str:
    """One chat completion. Returns the assistant message text.

    On a 429 rate-limit, automatically switches to the backup Groq key (if
    GROQ_API_KEY_BACKUP is set) and retries. DRY_RUN callers should not reach here.
    """
    if settings.dry_run:
        log_event("groq", "chat", "dry_run", "skipped real call")
        return ""

    _limiter.acquire()
    keys = _api_keys()
    global _active_key
    last_err = ""
    # Allow an extra pass so a rate-limit switch doesn't eat a real retry.
    for attempt in range(1, MAX_RETRIES + len(keys)):
        label, api_key = keys[_active_key % len(keys)]
        try:
            resp = _client_for(api_key).chat.completions.create(
                model=GROQ_MODEL,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                temperature=temperature,
                max_tokens=max_tokens,
            )
            return (resp.choices[0].message.content or "").strip()
        except Exception as e:  # openai raises various
            last_err = str(e)
            if _is_rate_limit(e) and len(keys) > 1:
                _active_key += 1  # rotate to the next key (e.g. primary -> backup)
                next_label = keys[_active_key % len(keys)][0]
                log_event("groq", "chat", "rate_limit",
                          f"{label} key 429 -> switching to {next_label} key")
                continue  # retry immediately on the other key, no backoff
            if attempt < MAX_RETRIES:
                time.sleep(2 ** attempt)
    log_event("groq", "chat", "error", last_err)
    raise RuntimeError(f"Groq call failed: {last_err}")
