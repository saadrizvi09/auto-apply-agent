"""Playwright browser engine for Google Forms (read questions, fill, submit).

Uses a PERSISTENT context (user-data-dir = .browser_profile) so a one-time Google
login is reused on every run - required because the referral forms demand sign-in.
Playwright is imported lazily so DRY_RUN and the unit tests never need the browser.

Nothing here submits unless `submit=True` is passed explicitly, and the orchestrator
only does that after operator approval. Sync API on purpose: call it from a sync
(threadpool) route, never inside the asyncio loop.
"""
from __future__ import annotations

import os
import random as _random
from contextlib import contextmanager

from ..config import ROOT, settings
from ..logging_setup import log_event


def _cv_abspath() -> str:
    p = settings.cv_path
    path = ROOT / p if not os.path.isabs(p) else p
    return str(path) if os.path.exists(path) else ""


def _try_attach_resume(page, cv_path: str) -> bool:
    """Best-effort auto-upload of the CV into a Google Forms file-upload question.

    Clicks 'Add file', then sets the local file on whatever <input type=file> the
    Google Drive picker exposes (across frames). Fragile by nature - returns False
    so the operator can finish by hand if Google's picker doesn't cooperate.
    """
    if not cv_path:
        return False
    add = page.query_selector('div[role="button"]:has-text("Add file")') \
        or page.query_selector('div[role="button"]:has-text("Add a file")')
    if not add:
        return False
    try:
        add.click()
        page.wait_for_timeout(2500)
        # The Drive picker loads in (cross-origin) child frames. Try clicking an
        # "Upload" tab if present, then set files on any file input we can find.
        for _ in range(2):
            for frame in page.frames:
                try:
                    tab = frame.query_selector('text=/^\\s*Upload\\s*$/')
                    if tab:
                        tab.click()
                        page.wait_for_timeout(1200)
                except Exception:
                    pass
                try:
                    inp = frame.query_selector('input[type="file"]')
                    if inp:
                        inp.set_input_files(cv_path)
                        page.wait_for_timeout(5000)  # allow the upload to finish
                        return True
                except Exception:
                    continue
            page.wait_for_timeout(1200)
    except Exception as e:  # noqa: BLE001
        log_event("browser", "attach_resume", "error", str(e))
    return False

PROFILE_DIR = ROOT / ".browser_profile"
SHOTS_DIR = ROOT / "form_shots"
_HEADLESS = os.getenv("BROWSER_HEADLESS", "false").strip().lower() in {"1", "true", "yes", "on"}


def _ensure_proactor_loop_policy() -> None:
    """On Windows, Playwright launches its browser driver via a subprocess, which the
    asyncio SelectorEventLoop can't do (raises NotImplementedError). Uvicorn (esp. with
    --reload) sets the global policy to Selector, so when the browser runs from inside
    the server it dies. Force the Proactor policy before Playwright starts its own loop;
    this only affects loops created afterwards (Playwright's), not uvicorn's running one."""
    import sys

    if sys.platform != "win32":
        return
    import asyncio

    try:
        if not isinstance(asyncio.get_event_loop_policy(), asyncio.WindowsProactorEventLoopPolicy):
            asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    except Exception:  # noqa: BLE001
        pass


@contextmanager
def _context(headless: bool | None = None):
    """Yield a persistent browser context (logged-in Google session)."""
    from playwright.sync_api import sync_playwright

    _ensure_proactor_loop_policy()
    PROFILE_DIR.mkdir(exist_ok=True)
    pw = sync_playwright().start()
    launch_kwargs = dict(
        user_data_dir=str(PROFILE_DIR),
        headless=_HEADLESS if headless is None else headless,
        # Maximize the real window (a fixed viewport doesn't resize the window in
        # headed mode) so the operator can reach Submit/Next without cramped tabs.
        no_viewport=True,
        # Hide the automation fingerprint so Google accepts the sign-in.
        args=["--disable-blink-features=AutomationControlled", "--start-maximized"],
        ignore_default_args=["--enable-automation"],
    )
    # Prefer the user's REAL installed Chrome (Google trusts it; bundled Chromium
    # is often blocked at login). Fall back to bundled Chromium if Chrome is absent.
    try:
        ctx = pw.chromium.launch_persistent_context(channel="chrome", **launch_kwargs)
    except Exception:
        ctx = pw.chromium.launch_persistent_context(**launch_kwargs)
    try:
        yield ctx
    finally:
        # The operator often closes the window by hand (login flows wait for that), which
        # already tears down the context — so ctx.close() would raise TargetClosedError.
        # Swallow it; the persistent session is saved as you browse, before the close.
        try:
            ctx.close()
        except Exception:
            pass
        try:
            pw.stop()
        except Exception:
            pass


def launch_login() -> None:
    """Open a visible browser at Google sign-in for the one-time login. Blocks
    until you close the window."""
    with _context(headless=False) as ctx:
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto("https://accounts.google.com/", wait_until="domcontentloaded")
        print("Log into the Google account you apply with, then CLOSE the window.")
        try:
            page.wait_for_event("close", timeout=300_000)  # up to 5 min
        except Exception:
            pass


# --- DOM reading -----------------------------------------------------------------

def _question_type(item) -> tuple[str, list[str]]:
    """Infer (TYPE, options) for one Google Form question container."""
    if item.query_selector('[role="button"][aria-label*="file" i], [aria-label*="Add file" i]'):
        return "FILE_UPLOAD", []
    radios = item.query_selector_all('[role="radio"]')
    if radios:
        opts = [r.get_attribute("aria-label") or r.get_attribute("data-value") or "" for r in radios]
        return "MULTIPLE_CHOICE", [o for o in opts if o]
    checks = item.query_selector_all('[role="checkbox"]')
    if checks:
        opts = [c.get_attribute("aria-label") or c.get_attribute("data-value") or "" for c in checks]
        return "CHECKBOXES", [o for o in opts if o]
    if item.query_selector('[role="listbox"]'):
        opts = [
            o.get_attribute("data-value") or o.inner_text()
            for o in item.query_selector_all('[role="option"]')
        ]
        opts = [o.strip() for o in opts if o and o.strip().lower() not in ("choose", "")]
        return "DROPDOWN", opts
    if item.query_selector("textarea"):
        return "PARAGRAPH", []
    if item.query_selector('input[type="date"]'):
        return "DATE", []
    if item.query_selector('input[type="email"]'):
        return "EMAIL", []
    return "SHORT_TEXT", []


def _read_questions(page) -> list[dict]:
    questions = []
    for item in page.query_selector_all('div[role="listitem"]'):
        heading = item.query_selector('[role="heading"]')
        title = (heading.inner_text() if heading else "").strip()
        if not title:
            continue
        required = bool(item.query_selector('[aria-label="Required question"]')) or title.endswith("*")
        title = title.rstrip(" *").strip()
        qtype, options = _question_type(item)
        questions.append({"title": title, "type": qtype, "options": options,
                          "required": required})
    return questions


_FBTYPE = {0: "SHORT_TEXT", 1: "PARAGRAPH", 2: "MULTIPLE_CHOICE", 3: "DROPDOWN",
           4: "CHECKBOXES", 5: "LINEAR_SCALE", 9: "DATE", 10: "TIME", 13: "FILE_UPLOAD"}


def read_form_full(url: str) -> dict:
    """Read a form's questions WITH their entry IDs (needed to build prefill links).

    Parses the FB_PUBLIC_LOAD_DATA_ blob from the logged-in form page. Returns
    {title, fields:[{title,type,entry_id,options,required}], signin_required, error}.
    """
    import json
    import re
    import time

    result = {"title": "", "fields": [], "signin_required": False, "error": ""}
    last_err = ""
    # Two attempts: a fresh Chrome launch on the SAME persistent profile can fail if
    # the previous form's Chrome hasn't released the profile lock yet (build_prefill
    # reads forms back-to-back). Retrying after the prior context fully closes fixes it.
    for attempt in range(2):
        try:
            with _context() as ctx:
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=45_000)
                # Poll for the data blob instead of a fixed wait — these forms render
                # FB_PUBLIC_LOAD_DATA_ a beat after domcontentloaded.
                html = ""
                for _ in range(24):  # up to ~12s
                    if "accounts.google.com" in page.url:
                        result["signin_required"] = True
                        return result
                    html = page.content()
                    if "FB_PUBLIC_LOAD_DATA_" in html:
                        break
                    page.wait_for_timeout(500)
            # Original (strict) pattern first; fall back to a tolerant one.
            m = (re.search(r"FB_PUBLIC_LOAD_DATA_\s*=\s*(\[.*?\]);</script>", html, re.S)
                 or re.search(r"FB_PUBLIC_LOAD_DATA_\s*=\s*(\[.*?\])\s*;\s*</script", html, re.S)
                 or re.search(r"FB_PUBLIC_LOAD_DATA_\s*=\s*(\[.*?\])\s*;", html, re.S))
            if not m:
                last_err = "couldn't read the form structure (form not fully loaded)"
                continue  # retry once
            data = json.loads(m.group(1))
            if len(data) > 1 and len(data[1]) > 8:
                result["title"] = data[1][8] or ""
            for it in (data[1][1] or []):
                if not it[4]:
                    continue  # section header / image, no answerable entry
                sub = it[4][0]
                result["fields"].append({
                    "title": (it[1] or "").strip(),
                    "type": _FBTYPE.get(it[3], "OTHER"),
                    "entry_id": sub[0],
                    "options": [o[0] for o in (sub[1] or []) if o and o[0]],
                    "required": bool(sub[2]) if len(sub) > 2 else False,
                })
            return result
        except Exception as e:  # noqa: BLE001
            last_err = str(e) or type(e).__name__
        time.sleep(1.5)  # let the profile lock release before retrying

    log_event("browser", "read_form_full", "error", last_err)
    result["error"] = last_err or "couldn't read the form structure"
    return result


def read_form(url: str) -> dict:
    """Open a Google Form and return {title, questions, signin_required, error}."""
    try:
        with _context() as ctx:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=45_000)
            page.wait_for_timeout(1500)
            low = page.content().lower()
            if "accounts.google.com" in page.url or "sign in to continue" in low:
                return {"title": "", "questions": [], "signin_required": True, "error": ""}
            title_el = page.query_selector('[role="heading"]')
            return {
                "title": (title_el.inner_text().strip() if title_el else ""),
                "questions": _read_questions(page),
                "signin_required": False,
                "error": "",
            }
    except Exception as e:
        log_event("browser", "read_form", "error", str(e))
        return {"title": "", "questions": [], "signin_required": False, "error": str(e)}


# --- Filling + submit ------------------------------------------------------------

def _fill_item(item, plan: dict, only_if_empty: bool = False) -> bool:
    """Fill one question container from a planned answer. Returns True if applied.

    only_if_empty=True skips fields that already have a value (so re-running over a
    page won't clobber the operator's manual edits or re-click chosen options).
    """
    answer = plan.get("answer", "")
    qtype = (plan.get("type") or "SHORT_TEXT").upper()
    if not answer or plan.get("blocked"):
        return False
    try:
        if qtype in ("MULTIPLE_CHOICE", "CHECKBOXES"):
            role = "radio" if qtype == "MULTIPLE_CHOICE" else "checkbox"
            if only_if_empty and item.query_selector(f'[role="{role}"][aria-checked="true"]'):
                return False
            el = item.query_selector(f'[role="{role}"][aria-label="{answer}"]') \
                or item.query_selector(f'[role="{role}"][data-value="{answer}"]')
            if el:
                el.click()
                return True
        elif qtype == "DROPDOWN":
            if only_if_empty:
                cur = item.query_selector('[role="listbox"] [aria-selected="true"]')
                if cur and (cur.get_attribute("data-value") or "").strip():
                    return False
            box = item.query_selector('[role="listbox"]')
            if box:
                box.click()
                item.page.wait_for_timeout(300)
                opt = item.query_selector(f'[role="option"][data-value="{answer}"]')
                if opt:
                    opt.click()
                    return True
        else:  # text / paragraph / email / date
            el = item.query_selector("textarea") or item.query_selector('input[type="text"]') \
                or item.query_selector('input[type="email"]') or item.query_selector("input")
            if el:
                if only_if_empty and (el.input_value() or "").strip():
                    return False
                el.fill(answer)
                return True
    except Exception as e:
        log_event("browser", "fill_item", "error", f"{plan.get('title','')}: {e}")
    return False


def fill_and_finish(url: str, planned: list[dict]) -> dict:
    """Open a form in a VISIBLE browser, auto-fill every text/choice field, then
    LEAVE IT OPEN so the operator attaches the resume + clicks Submit by hand.

    This is the realistic flow for forms with a required file-upload (the one thing
    automation can't do). Blocks until the operator closes the window.
    """
    by_title = {p["title"].strip().lower(): p for p in planned}
    result = {"filled": 0, "total": 0, "signin_required": False, "error": ""}
    try:
        with _context(headless=False) as ctx:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=45_000)
            page.wait_for_timeout(1500)
            if "accounts.google.com" in page.url:
                result["signin_required"] = True
                return result
            for item in page.query_selector_all('div[role="listitem"]'):
                heading = item.query_selector('[role="heading"]')
                if not heading:
                    continue
                result["total"] += 1
                plan = by_title.get(heading.inner_text().rstrip(" *").strip().lower())
                if plan and _fill_item(item, plan):
                    result["filled"] += 1
            # Best-effort: try to auto-attach the resume (Google's picker may block it).
            has_upload = any(
                (p.get("type") or "").upper() == "FILE_UPLOAD" for p in planned
            )
            attached = _try_attach_resume(page, _cv_abspath()) if has_upload else False
            result["resume_attached"] = attached
            if attached:
                print("  -> filled + RESUME ATTACHED automatically. "
                      "Just check it, click Submit, then CLOSE this window.")
            else:
                print(f"  -> filled {result['filled']}/{result['total']} fields. "
                      "Now ATTACH YOUR RESUME, click Submit, then CLOSE this window.")
            try:
                page.wait_for_event("close", timeout=900_000)  # up to 15 min
            except Exception:
                pass
            return result
    except Exception as e:
        log_event("browser", "fill_and_finish", "error", str(e))
        result["error"] = str(e)
        return result


def apply_form(url: str, plan_fn) -> dict:
    """Open a form ONCE: read its questions, plan answers, fill, try the resume
    upload, then leave it open for the operator to submit.

    `plan_fn(questions) -> planned` is supplied by the caller (it maps questions to
    profile answers). Doing read+fill in a single browser context avoids the
    profile-lock race that left some forms unfilled.
    """
    result = {"filled": 0, "total": 0, "signin_required": False,
              "resume_attached": False, "error": ""}
    try:
        with _context(headless=False) as ctx:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=45_000)
            page.wait_for_timeout(1500)
            if "accounts.google.com" in page.url:
                result["signin_required"] = True
                return result
            questions = _read_questions(page)
            planned = plan_fn(questions)
            by_title = {p["title"].strip().lower(): p for p in planned}
            for item in page.query_selector_all('div[role="listitem"]'):
                heading = item.query_selector('[role="heading"]')
                if not heading:
                    continue
                result["total"] += 1
                plan = by_title.get(heading.inner_text().rstrip(" *").strip().lower())
                if plan and _fill_item(item, plan):
                    result["filled"] += 1
            has_upload = any((p.get("type") or "").upper() == "FILE_UPLOAD" for p in planned)
            if has_upload:
                result["resume_attached"] = _try_attach_resume(page, _cv_abspath())
            tail = ("filled + RESUME ATTACHED. Check it, click Submit, then CLOSE this window."
                    if result["resume_attached"] else
                    "Attach your resume, click Submit, then CLOSE this window.")
            print(f"  -> filled {result['filled']}/{result['total']} fields. {tail}")
            try:
                page.wait_for_event("close", timeout=900_000)
            except Exception:
                pass
            return result
    except Exception as e:  # noqa: BLE001
        log_event("browser", "apply_form", "error", str(e))
        result["error"] = str(e)
        return result


def _fill_visible(page, plan_fn, job, attached: bool) -> tuple[int, int, bool]:
    """Fill the currently visible page of a form (only empty fields). Returns
    (filled_now, total_on_page, attached). Safe to call repeatedly — it skips fields
    that already have a value, so it never clobbers the operator's edits and it fills
    new sections as the operator clicks 'Next'."""
    planned = plan_fn(_read_questions(page), job)
    by_title = {p["title"].strip().lower(): p for p in planned}
    filled = total = 0
    for item in page.query_selector_all('div[role="listitem"]'):
        heading = item.query_selector('[role="heading"]')
        if not heading:
            continue
        total += 1
        plan = by_title.get(heading.inner_text().rstrip(" *").strip().lower())
        if plan and _fill_item(item, plan, only_if_empty=True):
            filled += 1
    if not attached and any((p.get("type") or "").upper() == "FILE_UPLOAD" for p in planned):
        attached = _try_attach_resume(page, _cv_abspath())
    return filled, total, attached


def apply_session(jobs: list[dict], plan_fn) -> list[dict]:
    """Apply to several forms in ONE browser session (a tab per form).

    jobs: [{"url","company","role"}]. Opens each form in a tab and fills its visible
    page. Then keeps the window open and CONTINUOUSLY re-fills whatever page is
    visible in each tab — so multi-page forms get their later sections filled as the
    operator clicks 'Next' (after attaching the CV). One Playwright engine for the
    whole run avoids the 'Sync API inside asyncio loop' corruption.
    """
    results = []
    tabs = []  # [page, job, attached]
    try:
        with _context(headless=False) as ctx:
            for idx, j in enumerate(jobs):
                r = {"company": j["company"], "filled": 0, "total": 0,
                     "resume_attached": False, "signin_required": False, "error": ""}
                page = ctx.pages[0] if (idx == 0 and ctx.pages) else ctx.new_page()
                try:
                    page.goto(j["url"], wait_until="domcontentloaded", timeout=45_000)
                    try:
                        page.wait_for_selector('div[role="listitem"]', timeout=20_000)
                    except Exception:
                        pass
                    page.wait_for_timeout(800)
                    if "accounts.google.com" in page.url:
                        r["signin_required"] = True
                        results.append(r)
                        continue
                    r["filled"], r["total"], r["resume_attached"] = _fill_visible(page, plan_fn, j, False)
                    tabs.append([page, j, r["resume_attached"]])
                    print(f"  -> tab {idx+1}: {j['company']} - filled {r['filled']}/{r['total']} "
                          f"on page 1; resume {'ATTACHED' if r['resume_attached'] else 'attach it yourself'}")
                except Exception as e:  # noqa: BLE001
                    r["error"] = str(e)
                    log_event("browser", "apply_session", "error", f"{j['company']}: {e}")
                results.append(r)

            print("\nAll first pages filled. As you click 'Next' on a multi-page form, "
                  "the next page auto-fills too. Attach the CV, Submit each form, then "
                  "CLOSE THE WINDOW when you're done with all of them.")
            # Keep alive + continuously fill the visible page of each tab.
            while True:
                try:
                    if not ctx.pages:
                        break
                    for entry in tabs:
                        page, job, attached = entry
                        try:
                            if page.is_closed():
                                continue
                            _, _, entry[2] = _fill_visible(page, plan_fn, job, attached)
                        except Exception:
                            continue
                    ctx.pages[0].wait_for_timeout(2500)
                except Exception:
                    break
    except Exception as e:  # noqa: BLE001
        log_event("browser", "apply_session", "error", str(e))
    return results


def fill_form(url: str, planned: list[dict], screenshot_path: str, submit: bool = False) -> dict:
    """Fill a form from planned answers, screenshot it, optionally submit.

    Returns {filled, total, submitted, signin_required, screenshot, error}.
    Matches planned answers to on-page questions by title.
    """
    SHOTS_DIR.mkdir(exist_ok=True)
    by_title = {p["title"].strip().lower(): p for p in planned}
    result = {"filled": 0, "total": 0, "submitted": False, "signin_required": False,
              "screenshot": "", "error": ""}
    try:
        with _context() as ctx:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=45_000)
            page.wait_for_timeout(1500)
            if "accounts.google.com" in page.url:
                result["signin_required"] = True
                return result
            items = page.query_selector_all('div[role="listitem"]')
            for item in items:
                heading = item.query_selector('[role="heading"]')
                if not heading:
                    continue
                title = heading.inner_text().rstrip(" *").strip().lower()
                plan = by_title.get(title)
                result["total"] += 1
                if plan and _fill_item(item, plan):
                    result["filled"] += 1
            page.wait_for_timeout(400)
            page.screenshot(path=screenshot_path, full_page=True)
            result["screenshot"] = screenshot_path

            if submit:
                btn = page.query_selector('div[role="button"]:has-text("Submit")') \
                    or page.query_selector('div[role="button"]:has-text("Submit form")')
                if btn:
                    btn.click()
                    page.wait_for_timeout(2000)
                    result["submitted"] = "formResponse" in page.url or \
                        bool(page.query_selector('text=Your response has been recorded'))
                    log_event("browser", url, "submitted", f"ok={result['submitted']}")
                else:
                    result["error"] = "Submit button not found"
            return result
    except Exception as e:
        log_event("browser", "fill_form", "error", str(e))
        result["error"] = str(e)
        return result


# === LinkedIn Easy Apply (assisted: pre-fill, the operator submits) =============
#
# Drives the operator's OWN logged-in LinkedIn (same persistent Chrome profile).
# It opens each job, clicks "Easy Apply", and pre-fills the safe fields it can map
# from the profile. It NEVER clicks "Submit application" — the human reviews any
# screening questions and submits. Sensitive questions (work authorization, visa
# sponsorship, EEO/diversity, background) are deliberately left blank for the human.

def launch_linkedin_login() -> None:
    """Open a visible browser at LinkedIn sign-in for the one-time login. Blocks
    until you close the window. Reuses the same .browser_profile as the Google login —
    so once you log in, the session persists across every run (no repeat logins)."""
    with _context(headless=False) as ctx:
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded")
        print("Log into your LinkedIn (email/password, or Continue with Google ->"
              " your LinkedIn's Google account), then CLOSE the window.")
        try:
            page.wait_for_event("close", timeout=300_000)
        except Exception:
            pass


def linkedin_logged_in() -> bool:
    """True if the saved session is still logged into LinkedIn (opens the feed and
    checks we don't bounce to login/authwall/checkpoint)."""
    try:
        with _context(headless=False) as ctx:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded", timeout=45_000)
            page.wait_for_timeout(2500)
            url = page.url.lower()
            return "/feed" in url and not any(
                s in url for s in ("login", "authwall", "checkpoint", "signup")
            )
    except Exception as e:  # noqa: BLE001
        log_event("browser", "li_check", "error", str(e))
        return False


# Identity / disclosure questions we still leave blank — answering them automatically
# is sensitive self-ID; required ones make the autonomous agent SKIP the job instead.
_LI_SENSITIVE = (
    "disab", "veteran", "race", "ethnic", "hispanic", "felony",
    "background check", "criminal", "clearance", "salary range",
)
# Foreign-country markers — a work-authorization question naming one of these is
# answered "No" (operator works in India only), which truthfully filters that job out.
_FOREIGN_PHRASES = (
    "united states", "u.s.", "u.s ", "america", "united kingdom", "canada",
    "europe", "german", "ireland", "australia", "singapore", "netherlands", "u.k.",
)
_FOREIGN_TOKENS = {"us", "usa", "uk", "eu", "uae"}


def _mentions_foreign(low: str) -> bool:
    import re
    if any(ph in low for ph in _FOREIGN_PHRASES):
        return True
    return bool(set(re.split(r"[^a-z]+", low)) & _FOREIGN_TOKENS)


def _li_value_for(label: str, p: dict) -> str | None:
    """Map a screening/contact label to a profile/answer-bank value. None = unknown or
    sensitive (the autonomous agent then skips the job rather than guess)."""
    low = (label or "").lower()
    if not low or any(k in low for k in _LI_SENSITIVE):
        return None

    def has(*ks):
        return any(k in low for k in ks)

    # Work authorization / sponsorship (operator: authorized in India, no sponsorship there)
    if has("authoriz", "legally", "right to work", "eligible to work", "work permit", "lawfully"):
        return "No" if _mentions_foreign(low) else "Yes"
    if has("sponsor", "visa"):
        return p.get("needs_sponsorship", "No")
    if has("citizen"):
        return None  # do not assert citizenship
    if "gender" in low or low.strip(" *") == "sex":
        return p.get("gender")
    if has("country code"):
        return None  # country-code dropdown (LinkedIn pre-sets it) — never the number

    # Any "years of experience" question -> the answer-bank value
    if (has("year") and has("experience")) or has("how many year"):
        return p.get("years_experience") or p.get("default_skill_years") or "1"

    # Contact / name
    if has("mobile", "phone", "contact number"):
        return p.get("phone")
    if has("email"):
        return p.get("email")
    if has("first name", "given name"):
        return (p.get("full_name") or "").split(" ")[0]
    if has("last name", "surname", "family name"):
        return (p.get("full_name") or "").split(" ")[-1]
    if has("full name", "your name", "candidate name", "applicant name"):
        return p.get("full_name")
    if low.strip().rstrip("*").strip() in ("name",):
        return p.get("full_name")

    # Compensation
    if has("current") and has("ctc", "salary", "compensation"):
        return p.get("current_ctc")
    if has("expected", "salary", "compensation", "ctc", "stipend", "pay"):
        return p.get("expected_ctc_number") or p.get("expected_ctc") or p.get("expected_stipend")
    if has("notice"):
        return p.get("notice_period")

    # Address / location
    if has("address line 1", "address 1", "street address", "address line1"):
        return p.get("address_line1")
    if has("address line 2", "address 2", "address line2"):
        return p.get("address_line2")
    if has("pin code", "pincode", "postal", "zip"):
        return p.get("pincode")
    if has("country") and not has("code"):
        return p.get("country")
    if has("current location", "city", "based in", "where are you", "location"):
        return p.get("city")

    # Links / education
    if has("linkedin"):
        return p.get("linkedin")
    if has("github"):
        return p.get("github")
    if has("portfolio", "website", "personal site"):
        return p.get("portfolio")
    if has("college", "university", "school"):
        return p.get("college")
    if has("degree", "qualification"):
        return p.get("degree")
    if has("graduat"):
        return p.get("graduation_year")
    return None


def _li_radio_answer(question: str, p: dict) -> str | None:
    """Best-guess answer for a LinkedIn radio/dropdown question. None = skip the job."""
    low = (question or "").lower()
    if not low or any(k in low for k in _LI_SENSITIVE):
        return None

    def has(*ks):
        return any(k in low for k in ks)

    if has("authoriz", "legally", "right to work", "eligible to work", "work permit", "lawfully"):
        return "No" if _mentions_foreign(low) else "Yes"
    if has("sponsor", "visa"):
        return p.get("needs_sponsorship", "No")
    if "gender" in low:
        return p.get("gender")
    if "relocat" in low:
        return p.get("willing_to_relocate") or "Yes"
    if has("currently working", "currently employed", "presently working",
           "are you working", "currently in a job", "working professional"):
        return p.get("currently_working", "Yes")
    if has("notice"):
        return p.get("notice_period") or "Immediate"
    if has("remote", "work from home", "comfortable", "willing", "able to",
           "available", "immediately", "start", "agree", "acknowledge"):
        return "Yes"
    return None


# --- answer resolution: static profile map -> learned bank -> LLM (skip if unsure) ---
#
# These wrap the static maps above with the learning answer-bank and an LLM fallback
# (app/services/answer_bank.py). Sensitive self-ID questions are NEVER auto-answered
# here (they return None, so a required one makes the agent skip the job). Any answer
# the LLM produces is banked, so the same question is answered instantly next time.

def _li_resolve_text(label: str, p: dict, options: list[str] | None = None) -> str | None:
    """Resolve a text/select field's answer. None => leave blank / skip the job."""
    low = (label or "").lower()
    if not low or any(k in low for k in _LI_SENSITIVE):
        return None
    val = _li_value_for(label, p)              # 1. static profile map (fast, exact)
    if val:
        return str(val)
    from ..services import answer_bank
    banked = answer_bank.get(label)            # 2. previously learned
    if banked:
        return banked
    ans = answer_bank.llm_answer(label, p, options=options)   # 3. LLM (truthful or UNKNOWN)
    if ans:
        answer_bank.remember(label, ans)
    return ans


def _li_resolve_choice(question: str, p: dict, options: list[str]) -> str | None:
    """Resolve a radio / multiple-choice answer, constrained to `options`."""
    low = (question or "").lower()
    if not low or any(k in low for k in _LI_SENSITIVE):
        return None
    val = _li_radio_answer(question, p)        # 1. static map (Yes/No heuristics)
    if val:
        return str(val)
    from ..services import answer_bank
    banked = answer_bank.get(question)         # 2. learned
    if banked:
        return banked
    ans = answer_bank.llm_answer(question, p, options=options)   # 3. LLM
    if ans:
        answer_bank.remember(question, ans)
    return ans


def _human_fill(el, text: str, page) -> None:
    """Type like a human: focus, clear any prefill, then type per-character with a
    small randomised delay. Falls back to fill() if typing isn't supported. This
    avoids the instant-paste fingerprint that LinkedIn's automation detection flags."""
    try:
        el.click()
        page.wait_for_timeout(int(_random.uniform(120, 360)))
        try:
            el.fill("")
        except Exception:
            pass
        el.type(str(text), delay=_random.uniform(45, 130))
    except Exception:
        try:
            el.fill(str(text))
        except Exception:
            pass


def _human_dwell(page) -> None:
    """A little human-like activity before acting: small mouse move + short scroll,
    randomised timing. Cheap signal against 'humanly impossible' linear automation."""
    try:
        page.mouse.move(_random.uniform(120, 700), _random.uniform(120, 500),
                        steps=_random.randint(3, 9))
        page.wait_for_timeout(int(_random.uniform(400, 1300)))
        page.mouse.wheel(0, _random.uniform(200, 600))
        page.wait_for_timeout(int(_random.uniform(500, 1600)))
    except Exception:
        pass


def _li_learnable(label: str, p: dict, is_choice: bool = False) -> bool:
    """True if an operator's own answer to this question SHOULD be banked from an
    assisted run: it's a real question, NOT a sensitive self-ID one (never learn those),
    and the static profile map doesn't already cover it (so we only learn the custom
    screening questions worth remembering)."""
    low = (label or "").lower()
    if not low or any(k in low for k in _LI_SENSITIVE):
        return False
    static = _li_radio_answer(label, p) if is_choice else _li_value_for(label, p)
    return static is None


def _li_label_for(modal, el) -> str:
    eid = el.get_attribute("id")
    if eid:
        try:
            lab = modal.query_selector(f'label[for="{eid}"]')
            if lab:
                return (lab.inner_text() or "").strip()
        except Exception:
            pass
    return (el.get_attribute("aria-label") or "").strip()


def _fill_linkedin_modal(page, p: dict) -> tuple[int, int, bool]:
    """Fill the visible Easy Apply modal step (only empty, safe fields). Returns
    (filled_now, total_fields, resume_attached). Idempotent — re-callable as the
    operator clicks Next through multi-step modals. Never clicks Submit."""
    modal = page.query_selector("div.jobs-easy-apply-modal") or page.query_selector('div[role="dialog"]')
    if not modal:
        return 0, 0, False
    filled = total = 0

    # text / tel / number inputs
    for inp in modal.query_selector_all(
        'input[type="text"], input[type="tel"], input[type="number"], input:not([type])'
    ):
        label = _li_label_for(modal, inp)
        if not label:
            continue
        total += 1
        try:
            if (inp.input_value() or "").strip():
                continue
        except Exception:
            continue
        val = _li_resolve_text(label, p)
        if val:
            try:
                _human_fill(inp, str(val), page)
                filled += 1
                # Location is a typeahead — pick the dropdown suggestion, else
                # LinkedIn shows "Please enter a valid answer" for raw text.
                if any(k in label.lower() for k in ("location", "city")):
                    page.wait_for_timeout(900)
                    opt = (page.query_selector(".basic-typeahead__selectable")
                           or page.query_selector('[role="option"]'))
                    if opt:
                        opt.click()
                    else:
                        inp.press("ArrowDown")
                        inp.press("Enter")
            except Exception:
                pass

    # native <select> dropdowns
    for sel in modal.query_selector_all("select"):
        label = _li_label_for(modal, sel)
        if not label:
            continue
        total += 1
        try:
            sel_opts = [(o.inner_text() or "").strip() for o in sel.query_selector_all("option")]
            sel_opts = [o for o in sel_opts if o and o.lower() not in ("select an option", "")]
        except Exception:
            sel_opts = []
        val = _li_resolve_text(label, p, options=sel_opts)
        if not val:
            continue
        try:
            sel.select_option(label=str(val))
            filled += 1
        except Exception:
            try:
                sel.select_option(value=str(val))
                filled += 1
            except Exception:
                pass

    # radio / multiple-choice fieldsets (Yes/No and custom option sets)
    for fs in modal.query_selector_all("fieldset"):
        legend = fs.query_selector("legend")
        q = (legend.inner_text() if legend else "").strip()
        if not q:
            continue
        total += 1
        fs_labels = [t for t in
                     ((lab.inner_text() or "").strip() for lab in fs.query_selector_all("label"))
                     if t]
        ans = _li_resolve_choice(q, p, fs_labels)
        if not ans:
            continue
        for lab in fs.query_selector_all("label"):
            if ans.lower() in ((lab.inner_text() or "").strip().lower()):
                try:
                    lab.click()
                    filled += 1
                except Exception:
                    pass
                break

    # resume upload (best-effort)
    attached = False
    cv = _cv_abspath()
    if cv:
        fileinp = modal.query_selector('input[type="file"]')
        if fileinp:
            try:
                fileinp.set_input_files(cv)
                attached = True
            except Exception:
                pass
    return filled, total, attached


def _capture_linkedin_answers(page, p: dict) -> int:
    """Teach the bank from an ASSISTED run: read the operator's OWN answers out of the
    visible Easy Apply modal and remember each non-sensitive screening question the static
    profile map doesn't already cover. So next time the autonomous agent meets the same
    question, it answers it instantly from the bank instead of skipping or guessing.

    Idempotent and safe to call repeatedly from the keep-alive loop — `remember` only
    writes on change, and the LATEST value wins, so the operator's final answer is what
    persists (an intermediate half-typed value gets overwritten on the next pass). Returns
    how many answers were captured this pass."""
    modal = page.query_selector("div.jobs-easy-apply-modal") or page.query_selector('div[role="dialog"]')
    if not modal:
        return 0
    from ..services import answer_bank
    saved = 0

    # text / tel / number inputs
    for inp in modal.query_selector_all(
        'input[type="text"], input[type="tel"], input[type="number"], input:not([type])'
    ):
        label = _li_label_for(modal, inp)
        if not _li_learnable(label, p):
            continue
        try:
            val = (inp.input_value() or "").strip()
        except Exception:
            continue
        if val:
            answer_bank.remember(label, val)
            saved += 1

    # native <select> dropdowns — read the chosen option's text
    for sel in modal.query_selector_all("select"):
        label = _li_label_for(modal, sel)
        if not _li_learnable(label, p):
            continue
        try:
            val = (sel.evaluate(
                "e => e.options[e.selectedIndex] ? e.options[e.selectedIndex].text : ''"
            ) or "").strip()
        except Exception:
            val = ""
        if val and val.lower() not in ("select an option", "select"):
            answer_bank.remember(label, val)
            saved += 1

    # radio / multiple-choice fieldsets — read the checked option's label
    for fs in modal.query_selector_all("fieldset"):
        legend = fs.query_selector("legend")
        q = (legend.inner_text() if legend else "").strip()
        if not _li_learnable(q, p, is_choice=True):
            continue
        chosen = ""
        for radio in fs.query_selector_all('input[type="radio"]'):
            try:
                if radio.is_checked():
                    rid = radio.get_attribute("id")
                    lab = fs.query_selector(f'label[for="{rid}"]') if rid else None
                    chosen = (lab.inner_text() if lab else "").strip()
                    break
            except Exception:
                continue
        if chosen:
            answer_bank.remember(q, chosen)
            saved += 1
    return saved


# --- Generic external-ATS application forms (Ashby / Greenhouse / Lever / …) -----
# Many LinkedIn "Apply" buttons redirect to a company ATS instead of Easy Apply.
# Those forms use ordinary labelled inputs, so we can pre-fill them the same way.

_ATS_HINTS = (
    "ashbyhq.com", "greenhouse.io", "lever.co", "myworkdayjobs", "workday",
    "smartrecruiters", "icims", "bamboohr", "recruitee", "workable", "teamtailor",
    "join.com", "/application", "/apply", "jobs.", "careers.",
)


def _find_easy_apply(page):
    """The Easy Apply control on a LinkedIn job page — which LinkedIn renders as either
    a <button> OR an <a> link (verified live: a[aria-label*="Easy Apply"]). Returns the
    element if it's Easy Apply (not 'Apply on company website'), else None."""
    for sel in ("button.jobs-apply-button",
                'button[aria-label*="Easy Apply" i]',
                'a[aria-label*="Easy Apply" i]',
                'button:has-text("Easy Apply")'):
        try:
            el = page.query_selector(sel)
            if el and el.is_visible():
                blob = ((el.inner_text() or "") + " " + (el.get_attribute("aria-label") or "")).lower()
                if "easy apply" in blob:
                    return el
        except Exception:
            continue
    return None


def _is_application_page(page) -> bool:
    """True if a non-LinkedIn tab looks like a job-application form we should fill."""
    url = (page.url or "").lower()
    if "linkedin.com" in url or url.startswith(("chrome:", "about:")):
        return False
    if any(h in url for h in _ATS_HINTS):
        return True
    try:  # heuristic: has an email field AND a resume/file upload
        return bool(page.query_selector('input[type="email"], input[name*="email" i]')
                    and page.query_selector('input[type="file"]'))
    except Exception:
        return False


def _ext_label_for(page, el) -> str:
    """Best-effort label for an arbitrary form input: <label for>, aria-label,
    placeholder, name, or the nearest preceding text node."""
    eid = el.get_attribute("id")
    if eid:
        try:
            lab = page.query_selector(f'label[for="{eid}"]')
            if lab and (lab.inner_text() or "").strip():
                return lab.inner_text().strip()
        except Exception:
            pass
    for attr in ("aria-label", "placeholder", "name"):
        v = el.get_attribute(attr)
        if v and v.strip() and "example.com" not in v.lower():
            return v.strip()
    try:
        txt = el.evaluate(
            """e => { let n=e;
                for (let i=0;i<4 && n;i++){ n = n.previousElementSibling || n.parentElement;
                  if(!n) break; const t=(n.innerText||n.textContent||'').trim();
                  if(t && t.length<80) return t; } return ''; }"""
        )
        return (txt or "").strip()
    except Exception:
        return ""


def _fill_external_form(page, p: dict, upload_cv: bool = True) -> tuple[int, int, bool]:
    """Pre-fill a generic ATS application form (only empty, safe fields). Idempotent,
    never submits. Returns (filled_now, total, resume_attached).

    upload_cv=False skips the file upload — used for sites that attach the résumé from a
    saved profile (e.g. Cutshort's Talent Card), where re-uploading cv.pdf just errors."""
    filled = total = 0
    for el in page.query_selector_all(
        'input[type="text"], input[type="email"], input[type="tel"], input[type="url"], '
        'input:not([type]), textarea'
    ):
        try:
            if not el.is_visible() or (el.input_value() or "").strip():
                continue
        except Exception:
            continue
        label = _ext_label_for(page, el)
        if not label:
            continue
        total += 1
        val = _li_resolve_text(label, p)
        if val:
            try:
                _human_fill(el, str(val), page)
                filled += 1
            except Exception:
                pass

    for sel in page.query_selector_all("select"):
        try:
            if not sel.is_visible():
                continue
        except Exception:
            continue
        s_label = _ext_label_for(page, sel)
        try:
            s_opts = [(o.inner_text() or "").strip() for o in sel.query_selector_all("option")]
            s_opts = [o for o in s_opts if o and o.lower() not in ("select an option", "select", "")]
        except Exception:
            s_opts = []
        val = _li_resolve_text(s_label, p, options=s_opts)
        if not val:
            continue
        try:
            sel.select_option(label=str(val))
            filled += 1
        except Exception:
            try:
                sel.select_option(value=str(val))
                filled += 1
            except Exception:
                pass

    # radio / multiple-choice fieldsets (e.g. "Are you currently working?" Yes/No)
    for fs in page.query_selector_all("fieldset"):
        try:
            legend = fs.query_selector("legend")
            q = (legend.inner_text() if legend else "").strip()
            if not q:
                continue
            total += 1
            labels = [t for t in
                      ((lab.inner_text() or "").strip() for lab in fs.query_selector_all("label"))
                      if t]
            ans = _li_resolve_choice(q, p, labels)
            if not ans:
                continue
            for lab in fs.query_selector_all("label"):
                if ans.lower() in ((lab.inner_text() or "").strip().lower()):
                    lab.click()
                    filled += 1
                    break
        except Exception:
            continue

    attached = False
    cv = _cv_abspath()
    if upload_cv and cv:
        for fi in page.query_selector_all('input[type="file"]'):
            try:
                fi.set_input_files(cv)
                attached = True
                break
            except Exception:
                continue
    return filled, total, attached


# === Autonomous external-ATS auto-submit (Greenhouse / Lever / Ashby / …) ==========
#
# Drives a generic company-ATS application form to SUBMISSION. Reuses the same answer
# resolver + bank + CV upload as Easy Apply. Research-driven safety rules:
#  - Upload the resume FIRST, then fill — every ATS parses the resume on upload and can
#    async-overwrite typed name/email (the "parse race").
#  - SUBMIT only when no required field is left unanswered (else leave it for assisted).
#  - VERIFY a confirmation page/text — Greenhouse/Lever/Ashby run an INVISIBLE captcha at
#    submit, which can make the click silently fail. Never trust the click alone.
#  - STOP the whole run on a visible bot-wall (Cloudflare / PerimeterX press-and-hold).

_ATS_BLOCK_MARKERS = (
    "press & hold", "press and hold", "verify you are human", "verify you're human",
    "are you a robot", "complete the security check", "checking your browser",
    "unusual traffic", "access denied",
)
_ATS_SUCCESS_MARKERS = (
    "thank you for applying", "application submitted", "thanks for applying",
    "your application has been submitted", "we received your application",
    "application received", "successfully submitted", "submission received",
)


def _ats_is_blocked(page) -> bool:
    """A VISIBLE bot-wall (Cloudflare challenge / PerimeterX press-and-hold). The invisible
    reCAPTCHA/hCaptcha badge present on most ATS forms is NOT a block — don't flag it."""
    try:
        u = (page.url or "").lower()
        if any(s in u for s in ("/cdn-cgi/challenge", "px-captcha", "/_human", "perimeterx")):
            return True
        body = (page.inner_text("body") or "").lower()
        return any(s in body for s in _ATS_BLOCK_MARKERS)
    except Exception:
        return False


def _ext_submit_button(page):
    """The submit control on an ATS form, most-specific first. Skips disabled buttons."""
    for sel in ('button:has-text("Submit application")',
                'button:has-text("Submit Application")',
                '#btn-submit', '[data-qa="btn-submit"]',
                'button[aria-label*="Submit" i]',
                'button[type="submit"]', 'input[type="submit"]',
                'button:has-text("Submit")'):
        try:
            el = page.query_selector(sel)
            if el and el.is_visible() and el.get_attribute("disabled") is None \
                    and (el.get_attribute("aria-disabled") or "false") != "true":
                return el
        except Exception:
            continue
    return None


def _ext_missing_required(page) -> int:
    """Count visible required text/select fields still empty, plus any field flagged
    aria-invalid after a submit attempt. Resume file inputs are excluded (handled by the
    upload step). Used to refuse submitting an incomplete application."""
    n = 0
    try:
        for el in page.query_selector_all('[required], [aria-required="true"]'):
            try:
                if not el.is_visible():
                    continue
                if (el.get_attribute("type") or "").lower() == "file":
                    continue
                if not (el.input_value() or "").strip():
                    n += 1
            except Exception:
                continue
        n += len(page.query_selector_all('[aria-invalid="true"]'))
    except Exception:
        pass
    return n


def _ext_submitted(page, before_url: str) -> bool:
    """True once the ATS shows a confirmation (URL or page text). SPA forms (Ashby)
    confirm via text with no navigation, so check both."""
    try:
        u = (page.url or "").lower()
        if u != (before_url or "").lower() and any(
            s in u for s in ("confirmation", "thank", "submitted", "success", "complete", "applied")
        ):
            return True
        body = (page.inner_text("body") or "").lower()
        return any(s in body for s in _ATS_SUCCESS_MARKERS)
    except Exception:
        return False


def _external_autosubmit(page, p: dict) -> str:
    """Drive a generic ATS apply form to submission. Returns:
    'submitted' | 'skipped:<reason>' | 'captcha_stop' | 'error:<msg>'."""
    try:
        page.wait_for_timeout(1800)
        if _ats_is_blocked(page):
            return "captcha_stop"
        # 1. upload the CV FIRST (beat the resume parse-race), then let it settle
        cv = _cv_abspath()
        if cv:
            for fi in page.query_selector_all('input[type="file"]'):
                try:
                    fi.set_input_files(cv)
                    page.wait_for_timeout(3500)
                    break
                except Exception:
                    continue
        # 2. fill labelled fields via the resolver; two passes re-assert parser-overwrites
        for _ in range(2):
            _fill_external_form(page, p)
            page.wait_for_timeout(700)
        _human_dwell(page)
        # 3. never submit an incomplete application — leave it for the assisted flow
        if _ext_missing_required(page) > 0:
            return "skipped:required-unanswered"
        btn = _ext_submit_button(page)
        if not btn:
            return "skipped:no-submit-button"
        before = page.url
        try:
            btn.click()
        except Exception:
            return "skipped:submit-click-failed"
        # 4. confirm — the invisible captcha can silently eat the submit
        for _ in range(14):
            page.wait_for_timeout(1000)
            if _ats_is_blocked(page):
                return "captcha_stop"
            if _ext_submitted(page, before):
                return "submitted"
            if _ext_missing_required(page) > 0:
                return "skipped:validation-failed"
        return "skipped:no-confirmation"
    except Exception as e:  # noqa: BLE001
        return f"error:{str(e)[:120]}"


def _find_external_apply(page):
    """The 'Apply' (on company website) control on a LinkedIn job page — it opens the
    company ATS in a new tab. Returns the element, or None. Skips the Easy Apply button."""
    for sel in ('button[aria-label*="Apply to" i]', 'a[aria-label*="Apply to" i]',
                'button.jobs-apply-button', 'a.jobs-apply-button',
                'a:has-text("Apply")', 'button:has-text("Apply")'):
        try:
            el = page.query_selector(sel)
            if el and el.is_visible():
                blob = ((el.inner_text() or "") + " " + (el.get_attribute("aria-label") or "")).lower()
                if "easy apply" in blob:
                    continue
                if "apply" in blob:
                    return el
        except Exception:
            continue
    return None


def _external_apply_flow(ctx, page, p: dict) -> str:
    """From a LinkedIn job page that is NOT Easy Apply: click 'Apply' (opens the company
    ATS in a new tab), then auto-submit that ATS form. Returns the _external_autosubmit
    outcome, or 'external' if we couldn't reach a fillable form (left for manual)."""
    ext = _find_external_apply(page)
    if not ext:
        return "external"
    atspage = None
    try:
        with ctx.expect_page(timeout=20_000) as pi:
            ext.click()
        atspage = pi.value
        try:
            atspage.wait_for_load_state("domcontentloaded", timeout=30_000)
        except Exception:
            pass
        atspage.wait_for_timeout(1500)
    except Exception:
        atspage = page if _is_application_page(page) else None
    if atspage is None or not _is_application_page(atspage):
        if atspage is not None and atspage is not page:
            try:
                atspage.close()
            except Exception:
                pass
        return "external"
    outcome = _external_autosubmit(atspage, p)
    if atspage is not page:
        try:
            atspage.close()
        except Exception:
            pass
    return outcome


def linkedin_apply_session(jobs: list[dict], profile: dict) -> list[dict]:
    """Open each LinkedIn job, click Easy Apply, pre-fill the modal, and HOLD the
    window open for the operator to review + Submit. Never submits. Jobs that
    redirect to an external ATS get their form pre-filled too (any open tab is filled).

    jobs: [{"url","company","role"}]. One Playwright engine for the whole run."""
    results = []
    tabs = []  # [page, job, attached]
    try:
        with _context(headless=False) as ctx:
            for idx, j in enumerate(jobs):
                r = {"company": j.get("company"), "role": j.get("role"),
                     "easy_apply": False, "external": False, "needs_login": False,
                     "filled": 0, "total": 0, "resume_attached": False, "error": ""}
                page = ctx.pages[0] if (idx == 0 and ctx.pages) else ctx.new_page()
                try:
                    page.goto(j["url"], wait_until="domcontentloaded", timeout=45_000)
                    page.wait_for_timeout(1500)
                    if any(s in page.url for s in ("linkedin.com/login", "/checkpoint", "/authwall")):
                        r["needs_login"] = True
                        results.append(r)
                        continue
                    btn = _find_easy_apply(page)
                    if not btn:
                        r["external"] = True
                        results.append(r)
                        continue
                    r["easy_apply"] = True
                    btn.click()
                    page.wait_for_selector('div.jobs-easy-apply-modal, div[role="dialog"]', timeout=15_000)
                    page.wait_for_timeout(1200)
                    r["filled"], r["total"], r["resume_attached"] = _fill_linkedin_modal(page, profile)
                    tabs.append([page, j, r["resume_attached"]])
                    print(f"  -> {j.get('company')}: Easy Apply opened, pre-filled "
                          f"{r['filled']}/{r['total']} field(s).")
                except Exception as e:  # noqa: BLE001
                    r["error"] = str(e)
                    log_event("browser", "li_apply", "error", f"{j.get('company')}: {e}")
                results.append(r)

            print("\nForms are pre-filled. In EACH tab: answer any highlighted "
                  "questions, click Next through the steps (later steps auto-fill "
                  "too), then click SUBMIT yourself. External-apply jobs (Ashby / "
                  "Greenhouse / etc.) opened in a tab get pre-filled too. "
                  "CLOSE THE WINDOW when done with all of them.")
            # Keep alive + continuously fill EVERY open tab: LinkedIn Easy Apply
            # modals and any external ATS application form the operator clicks into.
            while True:
                try:
                    pages = list(ctx.pages)
                    if not pages:
                        break
                    for page in pages:
                        try:
                            if page.is_closed():
                                continue
                            if "linkedin.com" in (page.url or "").lower():
                                _fill_linkedin_modal(page, profile)
                                _capture_linkedin_answers(page, profile)  # learn the operator's answers
                            elif _is_application_page(page):
                                _fill_external_form(page, profile)
                        except Exception:
                            continue
                    pages[0].wait_for_timeout(2500)
                except Exception:
                    break
    except Exception as e:  # noqa: BLE001
        log_event("browser", "li_apply_session", "error", str(e))
    return results


# === Autonomous LinkedIn Easy Apply (auto-submit, skip-and-log on unknowns) =======
#
# Drives each Easy Apply modal to completion: fills every step from the answer bank,
# and SUBMITS only when all required fields are satisfied. If a required question can't
# be answered confidently, it DISCARDS that application (never submits a wrong/guessed
# answer) and moves on. Stops the whole run if LinkedIn shows a captcha/checkpoint.


def _li_button(scope, *labels):
    """First visible button matching any of the aria-labels / texts."""
    for t in labels:
        try:
            b = (scope.query_selector(f'button[aria-label*="{t}" i]')
                 or scope.query_selector(f'button:has-text("{t}")'))
            if b and b.is_visible():
                return b
        except Exception:
            continue
    return None


def _li_has_errors(page) -> bool:
    """True if the modal shows a validation error (a required field we couldn't fill)."""
    try:
        for sel in (".artdeco-inline-feedback--error",
                    ".fb-dash-form-element__error-text", '[role="alert"]'):
            for el in page.query_selector_all(sel):
                if el.is_visible() and (el.inner_text() or "").strip():
                    return True
    except Exception:
        pass
    try:
        modal = page.query_selector("div.jobs-easy-apply-modal") or page.query_selector('div[role="dialog"]')
        body = (modal.inner_text() if modal else "").lower()
        return "please enter a valid" in body or "this field is required" in body
    except Exception:
        return False


def _li_progress(page) -> str:
    try:
        pb = page.query_selector("progress") or page.query_selector('[role="progressbar"]')
        if pb:
            return (pb.get_attribute("value") or pb.get_attribute("aria-valuenow") or "")
    except Exception:
        pass
    try:
        h = page.query_selector('div.jobs-easy-apply-modal h3, div[role="dialog"] h3')
        return (h.inner_text() if h else "")[:60]
    except Exception:
        return ""


def _li_discard(page) -> None:
    """Close the Easy Apply modal WITHOUT submitting (dismiss -> Discard)."""
    try:
        x = page.query_selector('button[aria-label="Dismiss"]') or page.query_selector('button[aria-label*="Dismiss" i]')
        if x:
            x.click()
            page.wait_for_timeout(700)
        disc = _li_button(page, "Discard")
        if disc:
            disc.click()
            page.wait_for_timeout(500)
    except Exception:
        pass


def _li_close_after_submit(page) -> None:
    try:
        done = _li_button(page, "Done")
        if done:
            done.click()
            return
        x = page.query_selector('button[aria-label="Dismiss"]')
        if x:
            x.click()
    except Exception:
        pass


def _li_is_captcha(page) -> bool:
    u = (page.url or "").lower()
    if "checkpoint" in u or "captcha" in u:
        return True
    try:
        if page.query_selector('iframe[src*="recaptcha"], iframe[title*="captcha" i]'):
            return True
        body = (page.inner_text("body") or "").lower()
        return any(s in body for s in ("security check", "unusual activity",
                                       "verify you're a human", "let's do a quick"))
    except Exception:
        return False


def _easy_apply_autosubmit(page, p: dict) -> str:
    """Drive an ALREADY-OPEN Easy Apply modal to completion. Returns:
    'submitted' | 'skipped:<reason>' | 'error:<msg>'. Never submits when a required
    field is unanswered — it discards instead."""
    try:
        for _ in range(12):  # generous step budget
            _fill_linkedin_modal(page, p)          # fill the visible step
            page.wait_for_timeout(700)

            submit = _li_button(page, "Submit application")
            if submit:
                if _li_has_errors(page):
                    _li_discard(page)
                    return "skipped:required-unanswered"
                # don't auto-follow the company
                try:
                    fol = page.query_selector('label:has-text("follow") input[type="checkbox"]')
                    if fol and fol.is_checked():
                        fol.uncheck()
                except Exception:
                    pass
                submit.click()
                page.wait_for_timeout(2500)
                _li_close_after_submit(page)
                return "submitted"

            btn = _li_button(page, "Review your application", "Review",
                             "Continue to next step", "Next")
            if not btn:
                _li_discard(page)
                return "skipped:no-forward-button"
            before = _li_progress(page)
            btn.click()
            page.wait_for_timeout(1200)
            if _li_has_errors(page) or _li_progress(page) == before:
                _li_discard(page)               # a required field blocked us
                return "skipped:blocked"
        _li_discard(page)
        return "skipped:too-many-steps"
    except Exception as e:  # noqa: BLE001
        try:
            _li_discard(page)
        except Exception:
            pass
        return f"error:{str(e)[:120]}"


# (random imported at top as _random)

# Cooperative stop flag — set by /api/linkedin/stop, checked between jobs.
_AUTOAPPLY_STOP = False


def request_autoapply_stop() -> None:
    global _AUTOAPPLY_STOP
    _AUTOAPPLY_STOP = True


def _safe_goto(page, url: str) -> bool:
    """Navigate with retry/backoff. LinkedIn sometimes stalls or throttles; one slow
    load shouldn't error the whole job. Returns True if the page loaded."""
    for attempt in range(2):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=40_000)
            return True
        except Exception:
            try:  # fallback: a more lenient wait, then accept whatever rendered
                page.goto(url, wait_until="commit", timeout=40_000)
                page.wait_for_timeout(3000)
                return True
            except Exception:
                page.wait_for_timeout(4000 * (attempt + 1))  # back off before retry
    return False


def linkedin_autoapply_session(jobs: list[dict], profile: dict, max_apply: int = 30,
                               external_submit: bool = True) -> list[dict]:
    """AUTONOMOUS: apply to up to `max_apply` Easy Apply jobs end-to-end (submits).
    Skips (discards) any job with an unanswerable required field. Stops on a LinkedIn
    captcha/checkpoint. Human-like delay between applications.

    jobs: [{"id","url","company","role"}]. Returns one result dict per job attempted."""
    global _AUTOAPPLY_STOP
    _AUTOAPPLY_STOP = False
    results = []
    applied = 0
    consecutive_errors = 0
    try:
        with _context(headless=False) as ctx:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            for j in jobs:
                if applied >= max_apply or _AUTOAPPLY_STOP:
                    if _AUTOAPPLY_STOP:
                        log_event("browser", "li_autoapply", "stopped", "operator requested stop")
                    break
                r = {"id": j.get("id"), "company": j.get("company"),
                     "role": j.get("role"), "outcome": "", "error": ""}
                try:
                    if not _safe_goto(page, j["url"]):
                        r["outcome"] = "error"
                        r["error"] = "page failed to load (LinkedIn slow/throttling)"
                        consecutive_errors += 1
                        results.append(r)
                        if consecutive_errors >= 4:   # LinkedIn likely throttling — back off
                            log_event("browser", "li_autoapply", "abort", "4 load failures — stopping")
                            break
                        page.wait_for_timeout(int(_random.uniform(40_000, 95_000)))
                        continue
                    consecutive_errors = 0
                    page.wait_for_timeout(1800)
                    _human_dwell(page)   # read-the-posting mouse move + scroll (anti-ban)
                    if any(s in page.url.lower() for s in ("login", "authwall", "checkpoint")):
                        r["outcome"] = "needs_login"
                        results.append(r)
                        break  # stop the whole run
                    if _li_is_captcha(page):
                        r["outcome"] = "captcha_stop"
                        results.append(r)
                        log_event("browser", "li_autoapply", "captcha_stop", "stopping run")
                        break  # STOP — do not push LinkedIn further
                    btn = _find_easy_apply(page)
                    if not btn:
                        # Not Easy Apply → try the company ATS form (Greenhouse/Lever/Ashby).
                        if external_submit:
                            r["outcome"] = _external_apply_flow(ctx, page, profile)
                            r["external"] = True
                            if r["outcome"] == "submitted":
                                applied += 1
                            print(f"  -> {j.get('company')}: external {r['outcome']}  "
                                  f"({applied}/{max_apply} submitted)")
                            if r["outcome"] == "captcha_stop":
                                results.append(r)
                                log_event("browser", "li_autoapply", "captcha_stop",
                                          "ATS bot-wall — stopping run")
                                break  # STOP on a bot-wall, don't push further
                        else:
                            r["outcome"] = "external"
                            print(f"  -> {j.get('company')}: external (auto-submit off)")
                    else:
                        btn.click()
                        page.wait_for_selector('div.jobs-easy-apply-modal, div[role="dialog"]', timeout=15_000)
                        page.wait_for_timeout(1000)
                        r["outcome"] = _easy_apply_autosubmit(page, profile)
                        if r["outcome"] == "submitted":
                            applied += 1
                        print(f"  -> {j.get('company')}: {r['outcome']}  ({applied}/{max_apply} submitted)")
                except Exception as e:  # noqa: BLE001
                    r["outcome"] = "error"
                    r["error"] = str(e)[:150]
                    log_event("browser", "li_autoapply", "error", f"{j.get('company')}: {e}")
                results.append(r)
                # human-like pause between applications (anti-ban): non-linear, with an
                # occasional longer "break" so the cadence never looks machine-regular.
                pause = _random.uniform(40_000, 95_000)
                if _random.random() < 0.15:
                    pause += _random.uniform(60_000, 150_000)
                page.wait_for_timeout(int(pause))
    except Exception as e:  # noqa: BLE001
        log_event("browser", "li_autoapply_session", "error", str(e))
    return results
