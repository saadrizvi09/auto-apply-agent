"""Autonomous apply on external job platforms — Y Combinator Work at a Startup,
Cutshort, and ZipRecruiter.

Each reuses the shared real-Chrome persistent profile, the answer resolver + learning
bank, human-like typing/pacing, and the bot-wall STOP from browser.py. Important, honest
constraints (see the research notes in memory):

  * ALL THREE prohibit automation in their ToS — this is the operator's accepted risk.
  * Each needs a ONE-TIME manual login (Google blocks scripted sign-in); the session then
    persists in .browser_profile.
  * Their apply DOM lives behind auth, so selectors here are best-effort and get refined
    on the FIRST LIVE RUN (exactly how LinkedIn Easy-Apply's `<a>`-vs-`<button>` was found).
  * Every driver fails SAFE: it never crashes the run, never submits an incomplete app,
    STOPS the whole run on a visible captcha / PerimeterX press-and-hold, dedupes against
    a persisted applied-set, and respects a low per-run cap.

ZipRecruiter additionally: vanilla Playwright leaks via CDP `Runtime.Enable`, so PerimeterX
can flag it regardless of profile/IP. This driver therefore only attempts 1-Click on loaded
results and HANDS OFF the instant a challenge appears — it is a fragile assistant, not a
reliable autopilot.
"""
from __future__ import annotations

import json
import os
import random as _random

from ..config import ROOT, settings
from ..logging_setup import log_event
from . import browser, groq_client

APPLIED_PATH = ROOT / "platform_applied.json"

# Verbose step-by-step logging + per-job screenshots. A platform stays verbose only while
# it's being validated on first runs; once proven it moves out of _VERBOSE and goes quiet.
# Force-on for any platform with env PLATFORM_DEBUG=1. The concise per-job outcome line
# ("-> YC <role>: submitted") always prints regardless.
_DEBUG_ALL = os.getenv("PLATFORM_DEBUG", "").strip().lower() in {"1", "true", "yes", "on"}
_VERBOSE = {"ziprecruiter"}   # not yet live-validated (yc + cutshort are proven)


def _verbose(platform: str) -> bool:
    return _DEBUG_ALL or platform in _VERBOSE


def _say(msg: str) -> None:
    """print() that can't crash the run on the Windows cp1252 console — some job titles
    carry non-Latin characters that the console can't encode."""
    try:
        print(msg)
    except Exception:
        try:
            print(msg.encode("ascii", "replace").decode("ascii"))
        except Exception:
            pass


def _dbg(platform: str, msg: str) -> None:
    if _verbose(platform):
        _say(msg)


def _shot(platform: str, page, name: str) -> str:
    """Screenshot to form_shots/ only when the platform is verbose. Returns path or ''."""
    if not _verbose(platform):
        return ""
    try:
        browser.SHOTS_DIR.mkdir(exist_ok=True)
        p = str(browser.SHOTS_DIR / name)
        page.screenshot(path=p)
        return p
    except Exception:
        return ""

# One-time login entry points (opened headed; operator logs in then closes the window).
_LOGIN = {
    "yc": ("https://www.workatastartup.com/",
           "Log into Work at a Startup (YC) — 'Log In' top-right — then CLOSE this window."),
    "cutshort": ("https://cutshort.io/",
                 "Log into Cutshort (Candidate login), then CLOSE this window."),
    "ziprecruiter": ("https://www.ziprecruiter.com/authn/login",
                     "Log into ZipRecruiter, then CLOSE this window."),
}

# Where each platform lands when the session is VALID (used to detect logged-in state).
_HOME = {
    "yc": "https://www.workatastartup.com/companies",
    "cutshort": "https://cutshort.io/jobs",
    "ziprecruiter": "https://www.ziprecruiter.com/candidate/dashboard",
}

# POSITIVE logged-in markers (visible only when signed in) — checked BEFORE any login
# control, because these sites keep a "Sign In"/"Sign up" link in the page even when
# you're logged in (that caused a false negative on YC).
_LOGGED_IN_MARKERS = {
    "yc": ("My profile", "Inbox", "Education"),
    "cutshort": ("My profile", "Logout", "My applications", "Dashboard"),
    "ziprecruiter": ("My ZipRecruiter", "Sign Out", "Saved Jobs", "My Account"),
}


# --- persisted dedupe set --------------------------------------------------------

def _load_applied() -> dict:
    if APPLIED_PATH.exists():
        try:
            d = json.loads(APPLIED_PATH.read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else {}
        except (ValueError, OSError):
            return {}
    return {}


def _already_applied(key: str) -> bool:
    return key in _load_applied()


def _mark_applied(platform: str, key: str, company: str) -> None:
    d = _load_applied()
    d[key] = {"platform": platform, "company": company}
    try:
        APPLIED_PATH.write_text(json.dumps(d, indent=2, ensure_ascii=False), encoding="utf-8")
    except OSError as e:  # noqa: BLE001
        log_event("platforms", "mark_applied", "error", str(e))


# --- login / session -------------------------------------------------------------

def launch_login(platform: str) -> None:
    """Open the platform's login page in the shared Chrome profile; block until closed."""
    url, msg = _LOGIN[platform]
    with browser._context(headless=False) as ctx:
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(url, wait_until="domcontentloaded")
        print(msg)
        try:
            page.wait_for_event("close", timeout=300_000)
        except Exception:
            pass


def logged_in(platform: str) -> bool:
    """True if the saved session for this platform is signed in. Delegates to login_probe
    (positive-marker-first, so a leftover 'Sign In' link doesn't cause a false negative)."""
    return login_probe(platform)["logged_in"]


def login_probe(platform: str) -> dict:
    """Diagnostic login check: opens the platform home, screenshots what the bot sees,
    and returns {logged_in, url, reason, shot}. Used to debug first-run login state
    instead of guessing — never applies to anything."""
    out = {"logged_in": False, "url": "", "reason": "error", "shot": ""}
    try:
        browser.SHOTS_DIR.mkdir(exist_ok=True)
        shot = str(browser.SHOTS_DIR / f"{platform}_check.png")
        with browser._context(headless=False) as ctx:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(_HOME[platform], wait_until="domcontentloaded", timeout=45_000)
            page.wait_for_timeout(3500)
            out["url"] = page.url
            try:
                page.screenshot(path=shot)
                out["shot"] = shot
            except Exception:
                pass
            if any(s in page.url.lower()
                   for s in ("login", "signin", "sign-in", "authn", "authwall", "/auth")):
                out["reason"] = "bounced-to-login"
                return out
            # POSITIVE markers first — these only show when signed in, and beat a leftover
            # "Sign In" link that the site keeps in the page even when logged in.
            for m in _LOGGED_IN_MARKERS.get(platform, ()):
                try:
                    el = page.query_selector(f'text="{m}"')
                    if el and el.is_visible():
                        out["logged_in"] = True
                        out["reason"] = f"marker:{m}"
                        return out
                except Exception:
                    continue
            for sel in ('a:has-text("Log In")', 'button:has-text("Log In")',
                        'a:has-text("Sign in to apply")', 'button:has-text("Sign in to apply")'):
                try:
                    el = page.query_selector(sel)
                    if el and el.is_visible():
                        out["reason"] = f"login-control-visible ({sel})"
                        return out
                except Exception:
                    continue
            out["logged_in"] = True  # no login wall + no login control => assume signed in
            out["reason"] = "no-login-control"
            return out
    except Exception as e:  # noqa: BLE001
        out["reason"] = f"error:{str(e)[:120]}"
        log_event("platforms", f"{platform}_probe", "error", str(e))
        return out


def _human_pause(page, min_s: float = 10, max_s: float = 22) -> None:
    """Randomised, non-linear pause between applications. Defaults are moderate; callers
    pass a tighter range for low-risk platforms (YC) and a wider one for aggressive bot
    defenses (ZipRecruiter/PerimeterX)."""
    pause = _random.uniform(min_s * 1000, max_s * 1000)
    if _random.random() < 0.12:
        pause += _random.uniform(15_000, 35_000)  # occasional longer break
    page.wait_for_timeout(int(pause))


def _react_fill(el, text: str) -> None:
    """Set a React-controlled input/textarea value so the framework's onChange fires
    (plain .fill() can leave the Send button disabled). Native setter + input/change."""
    el.evaluate(
        """(node, value) => {
            const proto = node.tagName === 'TEXTAREA'
                ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype;
            const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
            setter.call(node, value);
            node.dispatchEvent(new Event('input', {bubbles: true}));
            node.dispatchEvent(new Event('change', {bubbles: true}));
        }""",
        text,
    )


# --- Y Combinator: Work at a Startup ---------------------------------------------

_YC_ROLE = {"ai ml engineer": "eng", "software engineer": "eng", "backend engineer": "eng",
            "frontend": "eng", "fullstack": "eng", "data": "eng", "ml": "eng"}

# Role titles a 2026 new-grad won't get a call for — skip them (keeps "Senior" and
# "Founding Engineer", which startups offer to strong juniors). Tokens are space-padded
# and matched against a punctuation-normalised title, so "Lead, Engineer" is caught.
_YC_SKIP_TITLES = (" staff ", " principal ", " lead ", " head of ", " director ", " vp ",
                   " vice president ", " chief ", " cto ", " ceo ", " coo ", " cfo ",
                   " engineering manager ", " eng manager ", " architect ", " distinguished ")


def _is_senior_title(title: str) -> bool:
    import re
    low = " " + re.sub(r"[^a-z0-9]+", " ", (title or "").lower()).strip() + " "
    return any(m in low for m in _YC_SKIP_TITLES)


def _yc_message(profile: dict, company: str, role: str) -> str:
    """A genuinely per-company outreach message (YC ghosts templated blasts). Empty in
    DRY_RUN — the caller then skips, so we never send a blank message."""
    from ..profile import context_block
    system = (
        "You write a short, specific job-application message to a startup founder on YC's "
        "Work at a Startup. 3-5 sentences, plain text, no greeting or sign-off, first person. "
        "Reference the company and role concretely and name 1-2 things from the candidate's "
        "background that fit. Genuine and concise — never generic filler."
    )
    user = (f"Candidate:\n{context_block(profile)}\n\nCompany: {company}\nRole: {role}\n\n"
            "Write the message body only.")
    try:
        return (groq_client.chat(system, user, temperature=0.6, max_tokens=220) or "").strip()
    except Exception as e:  # noqa: BLE001
        log_event("platforms", "yc_message", "error", str(e)[:160])
        return ""


def _yc_discover(ctx, role_key: str, remote: bool, limit: int) -> list[dict]:
    import re
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    url = f"https://www.workatastartup.com/companies?role={role_key}&layout=list"
    if remote:
        url += "&remote=yes"
    if not browser._safe_goto(page, url):
        _dbg("yc", "  [yc] discovery: listing page failed to load")
        return []
    page.wait_for_timeout(3000)
    for _ in range(6):  # infinite scroll
        page.mouse.wheel(0, 4000)
        page.wait_for_timeout(1200)
    _shot("yc", page, "yc_listing.png")
    anchors = page.query_selector_all('a[href*="/jobs/"]')
    _dbg("yc", f"  [yc] found {len(anchors)} /jobs/ anchor(s) on the listing")
    jobs, seen = [], set()
    for a in anchors:
        try:
            href = a.get_attribute("href") or ""
            if not re.search(r"/jobs/\d", href):   # real job detail page only
                continue
            path = href.split("?")[0]
            if path in seen:
                continue
            seen.add(path)
            full = href if href.startswith("http") else f"https://www.workatastartup.com{href}"
            jobs.append({"url": full, "company": (a.inner_text() or "").strip()[:60], "role": ""})
        except Exception:
            continue
        if len(jobs) >= max(limit * 3, 3):
            break
    _dbg("yc", f"  [yc] discovered {len(jobs)} job link(s)")
    return jobs


def yc_autoapply(profile: dict, role_key: str, remote: bool, max_apply: int) -> list[dict]:
    """Apply to YC startups with a per-company message."""
    results = []
    applied = 0
    try:
        with browser._context(headless=False) as ctx:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            targets = _yc_discover(ctx, role_key, remote, max_apply)
            if not targets:
                return [{"company": "-", "outcome": "skipped:no-jobs-found",
                         "error": "no roles found / not logged in"}]
            for idx, j in enumerate(targets, 1):
                if applied >= max_apply:
                    break
                key = f"yc:{j['url']}"
                if _already_applied(key):
                    continue
                if _is_senior_title(j.get("company")):
                    _dbg("yc", f"  [yc] skipping senior/exec role '{j.get('company')}'")
                    results.append({"company": j["company"], "url": j["url"],
                                    "outcome": "skipped:senior-role", "error": ""})
                    continue
                r = {"company": j["company"], "url": j["url"], "outcome": "", "error": ""}
                try:
                    if not browser._safe_goto(page, j["url"]):
                        r["outcome"] = "error"; r["error"] = "page load failed"
                        results.append(r)
                        _say(f"  -> YC {r['company']}: {r['outcome']}")
                        continue
                    page.wait_for_timeout(2000)
                    browser._human_dwell(page)
                    _shot("yc", page, f"yc_job_{idx}.png")
                    if browser._ats_is_blocked(page) or "login" in page.url.lower():
                        r["outcome"] = "captcha_stop" if browser._ats_is_blocked(page) else "needs_login"
                        results.append(r)
                        _say(f"  -> YC {r['company']}: {r['outcome']} — stopping")
                        break
                    btn = (page.query_selector('a:has-text("Apply")')
                           or page.query_selector('button:has-text("Apply")'))
                    _dbg("yc", f"  [yc] {j['company']}: apply button = {bool(btn)}")
                    if not btn:
                        r["outcome"] = "skipped:no-apply-button"
                    else:
                        company = j["company"] or "this company"
                        msg = _yc_message(profile, company, j.get("role") or "")
                        _dbg("yc", f"  [yc] {company}: message = {len(msg)} chars")
                        if not msg:
                            r["outcome"] = "skipped:no-message"
                        else:
                            btn.click()
                            try:
                                page.wait_for_selector('[role="dialog"] textarea, textarea', timeout=12_000)
                            except Exception:
                                pass
                            page.wait_for_timeout(1000)
                            _shot("yc", page, f"yc_job_{idx}_modal.png")
                            ta = (page.query_selector('[role="dialog"] textarea')
                                  or page.query_selector('textarea'))
                            if not ta:
                                r["outcome"] = "skipped:no-message-box"
                            else:
                                _react_fill(ta, msg)
                                page.wait_for_timeout(800)
                                send = (page.query_selector('button:has-text("Send")')
                                        or page.query_selector('[role="dialog"] button[type="submit"]'))
                                if not send or send.get_attribute("disabled") is not None:
                                    r["outcome"] = "skipped:send-disabled"
                                else:
                                    send.click()
                                    page.wait_for_timeout(2500)
                                    r["outcome"] = "submitted"
                                    applied += 1
                                    _mark_applied("yc", key, company)
                except Exception as e:  # noqa: BLE001
                    r["outcome"] = "error"; r["error"] = str(e)[:150]
                results.append(r)
                tag = "SUBMITTED" if r["outcome"] == "submitted" else r["outcome"]
                _say(f"  -> YC {r['company']}: {tag}"
                     + (f" ({applied}/{max_apply})" if r["outcome"] == "submitted" else ""))
                if r["outcome"] == "submitted":
                    _human_pause(page, 6, 14)   # short pause only after a real submit
                else:
                    page.wait_for_timeout(1000)
    except Exception as e:  # noqa: BLE001
        log_event("platforms", "yc_autoapply", "error", str(e))
    return results


# --- Cutshort --------------------------------------------------------------------

def _cutshort_discover(ctx, skill: str, location: str, limit: int) -> list[dict]:
    """Discover jobs for one skill in one location bucket. location: 'remote' →
    remote-{slug}-jobs; a city slug (e.g. 'delhi-ncr') → {slug}-jobs-in-{city}; '' → all."""
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    slug = skill.strip().lower().replace(" ", "-") or "software-developer"
    if location == "remote":
        path = f"remote-{slug}-jobs"
    elif location:
        path = f"{slug}-jobs-in-{location}"
    else:
        path = f"{slug}-jobs"
    url = f"https://cutshort.io/jobs/{path}"
    if not browser._safe_goto(page, url):
        _dbg("cutshort", f"  [cutshort] listing failed to load: {url}")
        return []
    page.wait_for_timeout(3000)
    for _ in range(5):
        page.mouse.wheel(0, 4000)
        page.wait_for_timeout(1100)
    _shot("cutshort", page, "cutshort_listing.png")
    anchors = page.query_selector_all('a[href*="/job/"]')
    _dbg("cutshort", f"  [cutshort] found {len(anchors)} /job/ anchor(s) on {url}")
    jobs, seen = [], set()
    for a in anchors:
        try:
            href = a.get_attribute("href") or ""
            if not href:
                continue
            path_key = href.split("?")[0]
            if path_key in seen:
                continue
            seen.add(path_key)
            full = href if href.startswith("http") else f"https://cutshort.io{href}"
            jobs.append({"url": full, "company": (a.inner_text() or "").strip()[:60], "role": skill})
        except Exception:
            continue
        if len(jobs) >= max(limit * 3, 3):
            break
    _dbg("cutshort", f"  [cutshort] discovered {len(jobs)} job link(s)")
    return jobs


# Default Cutshort skill sweep when the query is blank — covers the operator's whole
# target range in one run (each is a /jobs/{slug}-jobs page). Override by typing one or
# more comma-separated skills in the dashboard box, e.g. "backend, fastapi".
_CUTSHORT_DEFAULT_SKILLS = ["ai-engineer", "ai-agent", "machine-learning", "llm",
                            "software-developer", "backend-developer",
                            "full-stack-developer", "python", "react"]


def cutshort_autoapply(profile: dict, query: str, remote: bool, max_apply: int) -> list[dict]:
    """Apply to Cutshort jobs across one or MORE skill pools (message + screening via the
    resolver). Blank query → sweep _CUTSHORT_DEFAULT_SKILLS; else comma-separated skills.
    Flags AI-video-interview jobs manual; stops on captcha/Turnstile."""
    skills = [s.strip().lower().replace(" ", "-") for s in (query or "").split(",") if s.strip()] \
        or _CUTSHORT_DEFAULT_SKILLS
    results = []
    applied = 0
    try:
        with browser._context(headless=False) as ctx:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            # sweep every skill across REMOTE + onsite Delhi-NCR; aggregate + dedupe
            locations = ["remote", "delhi-ncr"] if remote else ["delhi-ncr", ""]
            targets, seen = [], set()
            for sk in skills:
                for loc in locations:
                    for j in _cutshort_discover(ctx, sk, loc, max_apply):
                        k = j["url"].split("?")[0]
                        if k not in seen:
                            seen.add(k)
                            targets.append(j)
                if len(targets) >= max(max_apply * 5, 20):
                    break
            _say(f"  -> Cutshort: {len(targets)} job(s) found across {len(skills)} skill(s) "
                 f"(remote + Delhi-NCR)")
            if not targets:
                return [{"company": "-", "outcome": "skipped:no-jobs-found",
                         "error": "no jobs found / not logged in"}]
            for idx, j in enumerate(targets, 1):
                if applied >= max_apply:
                    break
                key = f"cutshort:{j['url']}"
                if _already_applied(key):
                    continue
                if _is_senior_title(j.get("company")):
                    results.append({"company": j["company"], "url": j["url"],
                                    "outcome": "skipped:senior-role", "error": ""})
                    continue
                r = {"company": j["company"], "url": j["url"], "outcome": "", "error": ""}
                try:
                    if not browser._safe_goto(page, j["url"]):
                        r["outcome"] = "error"; r["error"] = "page load failed"
                        results.append(r); _say(f"  -> Cutshort {r['company']}: {r['outcome']}"); continue
                    page.wait_for_timeout(2000)
                    browser._human_dwell(page)
                    _shot("cutshort", page, f"cutshort_job_{idx}.png")
                    if browser._ats_is_blocked(page):
                        r["outcome"] = "captcha_stop"; results.append(r)
                        _say(f"  -> Cutshort {r['company']}: captcha_stop — stopping"); break
                    if "login" in page.url.lower() or "auth" in page.url.lower():
                        r["outcome"] = "needs_login"; results.append(r)
                        _say(f"  -> Cutshort {r['company']}: needs_login — stopping"); break
                    # NOTE: don't pre-skip on "video"+"interview" in the page text — Cutshort
                    # advertises its AI-video screener (Voila) on EVERY job page, so that
                    # matched everywhere. Click Apply first, then detect a REAL video step.
                    btn = (page.query_selector('button:has-text("Apply")')
                           or page.query_selector('a:has-text("Apply")'))
                    _dbg("cutshort", f"  [cutshort] {j['company']}: apply button = {bool(btn)}")
                    if not btn:
                        r["outcome"] = "skipped:no-apply-button"
                    else:
                        btn.click()
                        page.wait_for_timeout(2000)
                        _shot("cutshort", page, f"cutshort_job_{idx}_apply.png")
                        try:   # reveal the real button labels so we know the submit control
                            labels = [(b.inner_text() or "").strip()[:30]
                                      for b in page.query_selector_all("button")
                                      if b.is_visible() and (b.inner_text() or "").strip()]
                            _dbg("cutshort", f"  [cutshort] {j['company']}: buttons = {labels[:14]}")
                        except Exception:
                            pass
                        flow = (page.inner_text("body") or "").lower()
                        # a real video-interview step asks to start/record on camera
                        video = any(m in flow for m in (
                            "start interview", "start the interview", "begin interview",
                            "enable camera", "allow camera", "record your answer",
                            "record a video", "video interview will", "proctored"))
                        _dbg("cutshort", f"  [cutshort] {j['company']}: video-step = {video}")
                        if video:
                            r["outcome"] = "skipped:video-interview-manual"
                        else:
                            ta = page.query_selector('textarea')   # optional cover note
                            if ta and not (ta.input_value() or "").strip():
                                note = _yc_message(profile, j["company"] or "your team",
                                                   j.get("role") or "this role")
                                if note:
                                    _react_fill(ta, note)
                            # screening fields (fills + learns); skip cv upload — Cutshort
                            # attaches the Talent Card résumé, re-uploading cv.pdf errors.
                            browser._fill_external_form(page, profile, upload_cv=False)
                            page.wait_for_timeout(700)
                            missing = browser._ext_missing_required(page)
                            _dbg("cutshort", f"  [cutshort] {j['company']}: required-unanswered = {missing}")
                            if missing > 0:
                                r["outcome"] = "skipped:required-unanswered"
                            else:
                                sub = None
                                for sel in ('button:has-text("Apply now")',
                                            'button:has-text("Submit application")',
                                            'button:has-text("Send application")',
                                            'button:has-text("Apply to this job")',
                                            'button:has-text("Submit")',
                                            'button:has-text("Confirm")'):
                                    el = page.query_selector(sel)
                                    if el and el.is_visible():
                                        sub = el
                                        break
                                sub = sub or browser._ext_submit_button(page)
                                _dbg("cutshort", f"  [cutshort] {j['company']}: submit button = {bool(sub)}")
                                if not sub:
                                    r["outcome"] = "skipped:no-submit"
                                else:
                                    sub.click()
                                    page.wait_for_timeout(2500)
                                    if browser._ats_is_blocked(page):
                                        r["outcome"] = "captcha_stop"
                                    else:
                                        r["outcome"] = "submitted"
                                        applied += 1
                                        _mark_applied("cutshort", key, j["company"])
                except Exception as e:  # noqa: BLE001
                    r["outcome"] = "error"; r["error"] = str(e)[:150]
                results.append(r)
                tag = "SUBMITTED" if r["outcome"] == "submitted" else r["outcome"]
                _say(f"  -> Cutshort {r['company']}: {tag}"
                     + (f" ({applied}/{max_apply})" if r["outcome"] == "submitted" else ""))
                if r["outcome"] == "captcha_stop":
                    break
                _human_pause(page, 6, 14) if r["outcome"] == "submitted" else page.wait_for_timeout(1000)
    except Exception as e:  # noqa: BLE001
        log_event("platforms", "cutshort_autoapply", "error", str(e))
    return results


# --- ZipRecruiter (fragile: 1-Click only, stop on PerimeterX) --------------------

def ziprecruiter_autoapply(profile: dict, search: str, location: str, max_apply: int) -> list[dict]:
    """1-Click apply on already-loaded ZipRecruiter results. STOPS the instant a Press &
    Hold / PerimeterX challenge appears (never auto-solves). Deliberately low volume."""
    results = []
    applied = 0
    try:
        with browser._context(headless=False) as ctx:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            url = ("https://www.ziprecruiter.com/jobs-search?"
                   f"search={search.replace(' ', '+')}&location={location.replace(' ', '+')}&zipapply=1")
            if not browser._safe_goto(page, url):
                return [{"company": "-", "outcome": "error", "error": "search load failed"}]
            page.wait_for_timeout(3000)
            browser._human_dwell(page)
            if browser._ats_is_blocked(page):
                return [{"company": "-", "outcome": "captcha_stop",
                         "error": "PerimeterX challenge on search — stopped"}]
            if "login" in page.url.lower() or "authn" in page.url.lower():
                return [{"company": "-", "outcome": "needs_login", "error": "not logged in"}]
            cards = page.query_selector_all('button:has-text("1-Click Apply"), button:has-text("Apply")')
            for card in cards:
                if applied >= max_apply:
                    break
                r = {"company": "ZipRecruiter job", "outcome": "", "error": ""}
                try:
                    if not card.is_visible():
                        continue
                    card.click()
                    page.wait_for_timeout(2500)
                    if browser._ats_is_blocked(page):
                        r["outcome"] = "captcha_stop"; results.append(r)
                        log_event("platforms", "ziprecruiter", "captcha_stop", "press-and-hold — stop")
                        break  # HARD STOP — never push PerimeterX
                    # some 1-Click jobs add screening questions
                    browser._fill_external_form(page, profile)
                    if browser._ext_missing_required(page) > 0:
                        r["outcome"] = "skipped:required-unanswered"; results.append(r)
                        _human_pause(page); continue
                    sub = browser._ext_submit_button(page) or page.query_selector('button:has-text("Submit")')
                    if sub:
                        sub.click()
                        page.wait_for_timeout(2000)
                        if browser._ats_is_blocked(page):
                            r["outcome"] = "captcha_stop"; results.append(r); break
                    r["outcome"] = "submitted"
                    applied += 1
                    _say(f"  -> ZipRecruiter: submitted ({applied}/{max_apply})")
                except Exception as e:  # noqa: BLE001
                    r["outcome"] = "error"; r["error"] = str(e)[:150]
                results.append(r)
                _human_pause(page)
    except Exception as e:  # noqa: BLE001
        log_event("platforms", "ziprecruiter_autoapply", "error", str(e))
    return results
