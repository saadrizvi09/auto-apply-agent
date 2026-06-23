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
_VERBOSE = {"cutshort", "ziprecruiter", "wellfound"}   # cutshort + wellfound still validating live


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
    "wellfound": ("https://wellfound.com/login",
                  "Log into Wellfound (ex-AngelList), then CLOSE this window."),
}

# Where each platform lands when the session is VALID (used to detect logged-in state).
_HOME = {
    "yc": "https://www.workatastartup.com/companies",
    "cutshort": "https://cutshort.io/jobs",
    "ziprecruiter": "https://www.ziprecruiter.com/candidate/dashboard",
    "wellfound": "https://wellfound.com/jobs",
}

# POSITIVE logged-in markers (visible only when signed in) — checked BEFORE any login
# control, because these sites keep a "Sign In"/"Sign up" link in the page even when
# you're logged in (that caused a false negative on YC).
_LOGGED_IN_MARKERS = {
    "yc": ("My profile", "Inbox", "Education"),
    "cutshort": ("My profile", "Logout", "My applications", "Dashboard"),
    "ziprecruiter": ("My ZipRecruiter", "Sign Out", "Saved Jobs", "My Account"),
    "wellfound": ("Messages", "My profile", "Saved", "For you", "Log out"),
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


# Off-target titles to skip on EXTERNAL platforms too — the operator wants AI-agent /
# AI-engineer / software roles, NOT ML/data roles or internships. Mirrors the LinkedIn
# discovery filters (discovery._EXCLUDE_TITLE_MARKERS / _INTERN_MARKERS). Without this, an
# unfiltered pool (e.g. Cutshort's broad `python` slug) drifts onto Data Engineer / Robotics
# Intern / ETL roles. Multi-word markers match as substrings of a space-normalised title;
# the short "etl" marker is space-padded so it only matches a standalone token.
_OFF_TARGET_TITLE_MARKERS = (
    "machine learning", "ml engineer", "ml researcher", "ml scientist", "ml platform",
    "mlops", "ml ops", "data scientist", "data engineer", "data analyst",
    "research scientist", "deep learning", " etl ",
)
_INTERN_TITLE_MARKERS = ("intern", "internship", "trainee", "apprentice")
# Non-engineering roles that slip onto AI/eng listing pages (Wellfound's /role/r/ai-engineer
# mixes in marketing/sales/design/PM). The operator wants AI/software-engineering ONLY.
# NOTE: "designer" is intentionally listed but does NOT match "Design Engineer" (no trailing
# "er" after "design ") — that borderline-technical title is kept on purpose.
_NON_ENG_TITLE_MARKERS = (
    "marketing", "marketer", " sales ", "account executive", "account manager",
    "recruiter", "recruiting", "talent acquisition", "customer success", "customer support",
    "designer", "product manager", "program manager", "project manager",
    "business development", "content writer", "copywriter", "community manager",
)


# The operator is a 2026 FRESHER — "Senior"/"Sr"/"Lead"/etc. roles are low-probability, so
# skip them (this is on top of _YC_SKIP_TITLES' staff/principal/director/manager/architect).
# "Founding Engineer" is NOT senior and is kept (attainable for strong juniors at startups).
_SENIOR_MARKERS = (" senior ", " sr ", " snr ")


def _skip_reason(title: str, allow_intern: bool = False) -> str | None:
    """Return why this title is off-target for the operator, or None if it's a keeper.
    Catches ML/data roles, non-engineering roles (marketing/sales/design/PM), unreachable
    senior/lead/exec titles, and (by default) internships — so external auto-apply only submits
    realistic AI/software roles a 2026 fresher can actually land.

    allow_intern=True keeps internships (used for FOREIGN/worldwide-remote platforms — the
    operator will take an unpaid foreign role). India platforms keep the default (skip interns:
    Cutshort gives no salary on the card, so an India intern can't be confirmed >=8 LPA)."""
    import re
    low = " " + re.sub(r"[^a-z0-9]+", " ", (title or "").lower()).strip() + " "
    if not allow_intern and any(m in low for m in _INTERN_TITLE_MARKERS):
        return "intern"
    if any(m in low for m in _OFF_TARGET_TITLE_MARKERS) or any(m in low for m in _NON_ENG_TITLE_MARKERS):
        return "off-target"
    if any(m in low for m in _YC_SKIP_TITLES) or any(m in low for m in _SENIOR_MARKERS):
        return "senior-role"
    return None


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
                reason = _skip_reason(j.get("company"), allow_intern=True)  # YC startups, mostly foreign/remote
                if reason:
                    _dbg("yc", f"  [yc] skipping {reason} role '{j.get('company')}'")
                    results.append({"company": j["company"], "url": j["url"],
                                    "outcome": f"skipped:{reason}", "error": ""})
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


# Default Cutshort skill sweep when the query is blank — the operator targets AI-engineering
# roles, so lead with AI/LLM/agent skill slugs (each is a /jobs/{slug}-jobs page; all verified
# to return HTTP 200). Override by typing comma-separated skills in the dashboard box,
# e.g. "langchain, fastapi". `python` and `fastapi` are kept at the end because they return a
# large AI-backend pool (verified live) so a run still finds volume even if the narrow AI
# slugs are thin that day. ML/data-only slugs (machine-learning, data-science) are deliberately
# excluded — the operator does not want ML-engineer / data roles.
_CUTSHORT_DEFAULT_SKILLS = ["artificial-intelligence", "generative-ai", "llm", "langchain",
                            "ai-engineer", "nlp", "python", "fastapi"]


def _cutshort_applied(page) -> bool:
    """Best-effort: did the Cutshort application ACTUALLY go through? (success text, an
    'Applied' badge, or the apply button flipping to Applied/Withdraw). Used so we never
    mark 'submitted' just for clicking — clicking 'Apply now' only opens the verify modal."""
    try:
        body = (page.inner_text("body") or "").lower()
        if any(s in body for s in ("application sent", "successfully applied", "you have applied",
                                   "application submitted", "applied successfully",
                                   "thank you for applying", "your application has been",
                                   "we have shared your", "message sent", "your message has been sent",
                                   "message has been sent", "sent your application")):
            return True
        for sel in ('button:has-text("Applied")', 'button:has-text("Withdraw")',
                    ':text("Applied on")', ':text("You applied")'):
            if page.query_selector(sel):
                return True
    except Exception:
        pass
    return False


def _cutshort_tick_declaration(page) -> bool:
    """Tick the required 'I hereby declare…' consent checkbox (native or custom)."""
    markers = ("hereby", "declare", "true, complete", "true complete",
               "correct to the best", "i agree", "i consent")
    # 1. a native checkbox whose surrounding text is the declaration — walk up for the text
    for cb in page.query_selector_all('input[type="checkbox"]'):
        try:
            if cb.is_checked():
                continue
            t = (cb.evaluate(
                "e => { let n=e; for(let i=0;i<6&&n;i++){ n=n.parentElement; "
                "if(n && (n.innerText||'').length>20) return n.innerText; } return ''; }"
            ) or "").lower()
            if any(k in t for k in markers):
                try:
                    cb.check()
                except Exception:
                    cb.click()
                return True
        except Exception:
            continue
    # 2. custom checkbox: find the declaration text, click the checkbox-ish control in its row
    try:
        decl = (page.query_selector('text=/i hereby declare/i')
                or page.query_selector('text=/true, complete and correct/i'))
        if decl:
            cont = decl.evaluate_handle("e => e.closest('div,label,li,section')").as_element()
            if cont:
                box = (cont.query_selector('input[type="checkbox"]')
                       or cont.query_selector('[role="checkbox"]'))
                if box:
                    try:
                        box.check()
                    except Exception:
                        box.click()
                    return True
                cont.click()   # last resort: click the row (often toggles the checkbox)
                return True
    except Exception:
        pass
    return False


def _cutshort_verify(page, profile: dict) -> int:
    """Complete Cutshort's 'verify your data' modal: upload the (Required) résumé, select
    the Employment-status option by text (custom radios), and tick the (Required)
    declaration. Returns the number of actions taken."""
    clicked = 0
    # 1. résumé (Required) — upload ONLY if one isn't already attached (Cutshort shows
    #    "Upload another resume" + the filename once attached; re-uploading re-triggers the
    #    signedUrl error and is unnecessary).
    try:
        already = (page.query_selector('text=/upload another resume/i')
                   or page.query_selector('text=/resume_/i')
                   or page.query_selector('text=/\\.pdf/i'))
        cv = browser._cv_abspath()
        if cv and not already and browser._ext_upload_resume(page, cv):
            clicked += 1
    except Exception:
        pass
    # 2. employment status — click the desired option by text (default: Not working currently,
    #    which avoids the notice-period date field that "On notice period" demands)
    emp = profile.get("employment_status", "Not working currently")
    try:
        el = page.query_selector(f'text="{emp}"')
        if el and el.is_visible():
            el.click()
            clicked += 1
    except Exception:
        pass
    # 2b. if "Work remotely" is enabled, pick "In any timezone"
    try:
        tz = page.query_selector('text="In any timezone"')
        if tz and tz.is_visible():
            tz.click()
            clicked += 1
    except Exception:
        pass
    # 3. declaration / consent checkbox (Required)
    try:
        if _cutshort_tick_declaration(page):
            clicked += 1
    except Exception:
        pass
    return clicked


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
                reason = _skip_reason(j.get("company"))
                if reason:
                    _dbg("cutshort", f"  [cutshort] skipping {reason} role '{j.get('company')}'")
                    results.append({"company": j["company"], "url": j["url"],
                                    "outcome": f"skipped:{reason}", "error": ""})
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
                            # The job-page "Apply now" only OPENS Cutshort's "verify your
                            # data" modal; the real submit is completing it ("Save and
                            # continue" → … ). Click through the wizard and ONLY mark
                            # submitted when a real success signal appears (never on a click).
                            outcome = "skipped:no-submit"
                            for step in range(6):
                                if browser._ats_is_blocked(page):
                                    outcome = "captcha_stop"; break
                                if _cutshort_applied(page):
                                    outcome = "submitted"; break
                                ta = page.query_selector('textarea')   # optional cover note
                                if ta and not (ta.input_value() or "").strip():
                                    note = _yc_message(profile, j["company"] or "your team",
                                                       j.get("role") or "this role")
                                    if note:
                                        _react_fill(ta, note)
                                # fill profile/screening/radios/declaration; upload résumé too
                                browser._fill_external_form(page, profile, upload_cv=True,
                                                            company=j.get("company", ""),
                                                            role=j.get("role", ""))
                                cv_clicks = _cutshort_verify(page, profile)  # custom radios by text
                                page.wait_for_timeout(900)
                                miss = browser._ext_missing_required(page)
                                # forward button — modal steps FIRST, final submit last, so we
                                # advance the wizard instead of re-clicking the page's Apply now
                                fwd, fwd_txt = None, None
                                for sel in ('button:has-text("Save and continue")',
                                            'button:has-text("Save & continue")',
                                            'button:has-text("Continue")',
                                            'button:has-text("Next")',
                                            'button:has-text("Send")',           # final: message-to-founder screen
                                            'button:has-text("Submit application")',
                                            'button:has-text("Submit")',
                                            'button:has-text("Confirm")',
                                            'button:has-text("Apply now")'):
                                    el = page.query_selector(sel)
                                    if el and el.is_visible() and el.get_attribute("disabled") is None:
                                        fwd, fwd_txt = el, (el.inner_text() or "").strip()
                                        break
                                _dbg("cutshort", f"  [cutshort] {j['company']}: step {step+1} "
                                                 f"verify-clicks={cv_clicks} missing={miss} fwd={fwd_txt!r}")
                                if fwd and miss == 0:
                                    fwd.click()
                                    page.wait_for_timeout(2200)
                                    if browser._ats_is_blocked(page):
                                        outcome = "captcha_stop"; break
                                    if _cutshort_applied(page):
                                        outcome = "submitted"; break
                                    continue
                                outcome = ("skipped:required-unanswered" if miss > 0
                                           else "skipped:no-submit")
                                break
                            r["outcome"] = outcome
                            if outcome == "submitted":
                                applied += 1
                                _mark_applied("cutshort", key, j["company"])
                except Exception as e:  # noqa: BLE001
                    r["outcome"] = "error"; r["error"] = str(e)[:150]
                results.append(r)
                if r["outcome"] == "submitted":
                    tag = f"SUBMITTED ({applied}/{max_apply})"
                elif r["outcome"] == "error":
                    tag = f"error — {r.get('error', '')}"   # show WHY it errored
                else:
                    tag = r["outcome"]
                _say(f"  -> Cutshort {r['company']}: {tag}")
                if r["outcome"] == "captcha_stop":
                    break
                _human_pause(page, 6, 14) if r["outcome"] == "submitted" else page.wait_for_timeout(1000)
    except Exception as e:  # noqa: BLE001
        log_event("platforms", "cutshort_autoapply", "error", str(e))
    return results


# --- Wellfound (ex-AngelList): startup jobs, per-company message apply ------------
# Wellfound is the strongest board for the operator's target (remote-friendly startups that
# hire freshers globally). It applies like YC: open a job, click Apply, write a genuine
# per-company message, Send. Cloudflare-defended, so it STOPS on any challenge. Selectors are
# best-effort and get refined on the FIRST LIVE RUN (that's why it's in _VERBOSE).

# Map the operator's role text to a Wellfound role slug (/role/r/{slug}). Default to AI.
_WELLFOUND_ROLE = {
    "ai engineer": "ai-engineer", "ai": "ai-engineer", "ml": "machine-learning-engineer",
    "ai ml engineer": "ai-engineer", "machine learning": "machine-learning-engineer",
    "software engineer": "software-engineer", "backend": "backend-engineer",
    "backend engineer": "backend-engineer", "frontend": "frontend-engineer",
    "fullstack": "full-stack-engineer", "full stack": "full-stack-engineer",
    "data": "data-engineer",
}


def _wellfound_role_slug(query: str) -> str:
    q = (query or "").strip().lower()
    if not q:
        return "ai-engineer"
    if q in _WELLFOUND_ROLE:
        return _WELLFOUND_ROLE[q]
    return q.replace(" ", "-")


def _wellfound_message(profile: dict, company: str, role: str) -> str:
    """A genuine per-company message for a Wellfound application (no greeting/sign-off).
    Empty in DRY_RUN so the caller skips rather than sending a blank message."""
    from ..profile import context_block
    system = (
        "You write a short, specific job-application note to a startup on Wellfound "
        "(ex-AngelList). 3-5 sentences, plain text, no greeting or sign-off, first person. "
        "Reference the company and role concretely and name 1-2 things from the candidate's "
        "background that fit. Genuine and concise — never generic filler."
    )
    user = (f"Candidate:\n{context_block(profile)}\n\nCompany: {company}\nRole: {role}\n\n"
            "Write the message body only.")
    try:
        return (groq_client.chat(system, user, temperature=0.6, max_tokens=220) or "").strip()
    except Exception as e:  # noqa: BLE001
        log_event("platforms", "wellfound_message", "error", str(e)[:160])
        return ""


def _wellfound_discover(ctx, role_slug: str, remote: bool, limit: int) -> list[dict]:
    """Collect Wellfound job-detail links for one role. Job links look like /jobs/{id}-{slug}.
    Uses `/role/r/{slug}` — LIVE-VERIFIED to be the role-targeted listing AND already
    remote-biased (its heading is "Remote {Role} Jobs"). The old `/role/remote/{slug}` was an
    empty SEO hub page and `/role/{slug}-jobs` / `/role/l/remote/{slug}` 404. `remote` is kept
    for API symmetry; a city-scoped search would need the `/role/l/{slug}/{city}` form (not wired)."""
    import re
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    url = f"https://wellfound.com/role/r/{role_slug}"
    if not browser._safe_goto(page, url):
        _dbg("wellfound", f"  [wellfound] listing failed to load: {url}")
        return []
    page.wait_for_timeout(3000)
    if browser._ats_is_blocked(page):
        _dbg("wellfound", "  [wellfound] blocked on listing (Cloudflare)")
        return []
    h1 = page.query_selector("h1")
    if h1 and "not found" in (h1.inner_text() or "").lower():
        _dbg("wellfound", f"  [wellfound] role slug 404'd: {role_slug}")
        return []
    for _ in range(6):  # infinite scroll
        page.mouse.wheel(0, 4000)
        page.wait_for_timeout(1200)
    _shot("wellfound", page, "wellfound_listing.png")
    anchors = page.query_selector_all('a[href*="/jobs/"]')
    _dbg("wellfound", f"  [wellfound] found {len(anchors)} /jobs/ anchor(s) on {url}")
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
            full = href if href.startswith("http") else f"https://wellfound.com{href}"
            jobs.append({"url": full, "company": (a.inner_text() or "").strip()[:60], "role": role_slug})
        except Exception:
            continue
        if len(jobs) >= max(limit * 3, 3):
            break
    _dbg("wellfound", f"  [wellfound] discovered {len(jobs)} job link(s)")
    return jobs


def wellfound_autoapply(profile: dict, query: str, remote: bool, max_apply: int) -> list[dict]:
    """Apply to Wellfound startups with a per-company message. AI/software only (off-target,
    intern and senior titles skipped). STOPS on any Cloudflare/login wall. First live run
    validates the apply modal layout (verbose)."""
    results = []
    applied = 0
    role_slug = _wellfound_role_slug(query)
    try:
        with browser._context(headless=False) as ctx:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            targets = _wellfound_discover(ctx, role_slug, remote, max_apply)
            if not targets:
                return [{"company": "-", "outcome": "skipped:no-jobs-found",
                         "error": "no roles found / not logged in / blocked"}]
            _say(f"  -> Wellfound: {len(targets)} job(s) found for '{role_slug}' "
                 f"({'remote' if remote else 'any location'})")
            for idx, j in enumerate(targets, 1):
                if applied >= max_apply:
                    break
                key = f"wellfound:{j['url']}"
                if _already_applied(key):
                    continue
                reason = _skip_reason(j.get("company"), allow_intern=True)  # foreign/remote — unpaid OK
                if reason:
                    _dbg("wellfound", f"  [wellfound] skipping {reason} role '{j.get('company')}'")
                    results.append({"company": j["company"], "url": j["url"],
                                    "outcome": f"skipped:{reason}", "error": ""})
                    continue
                r = {"company": j["company"], "url": j["url"], "outcome": "", "error": ""}
                try:
                    if not browser._safe_goto(page, j["url"]):
                        r["outcome"] = "error"; r["error"] = "page load failed"
                        results.append(r); _say(f"  -> Wellfound {r['company']}: {r['outcome']}"); continue
                    page.wait_for_timeout(2000)
                    browser._human_dwell(page)
                    _shot("wellfound", page, f"wellfound_job_{idx}.png")
                    if browser._ats_is_blocked(page):
                        r["outcome"] = "captcha_stop"; results.append(r)
                        _say(f"  -> Wellfound {r['company']}: captcha_stop — stopping"); break
                    if any(s in page.url.lower() for s in ("login", "signin", "sign-in")):
                        r["outcome"] = "needs_login"; results.append(r)
                        _say(f"  -> Wellfound {r['company']}: needs_login — stopping"); break
                    btn = (page.query_selector('button:has-text("Apply")')
                           or page.query_selector('a:has-text("Apply")'))
                    _dbg("wellfound", f"  [wellfound] {j['company']}: apply button = {bool(btn)}")
                    if not btn:
                        r["outcome"] = "skipped:no-apply-button"
                    else:
                        company = j["company"] or "this company"
                        msg = _wellfound_message(profile, company, j.get("role") or "")
                        _dbg("wellfound", f"  [wellfound] {company}: message = {len(msg)} chars")
                        if not msg:
                            r["outcome"] = "skipped:no-message"
                        else:
                            btn.click()
                            try:
                                page.wait_for_selector('[role="dialog"] textarea, textarea', timeout=12_000)
                            except Exception:
                                pass
                            page.wait_for_timeout(1000)
                            _shot("wellfound", page, f"wellfound_job_{idx}_modal.png")
                            ta = (page.query_selector('[role="dialog"] textarea')
                                  or page.query_selector('textarea'))
                            if not ta:
                                r["outcome"] = "skipped:no-message-box"
                            else:
                                _react_fill(ta, msg)
                                page.wait_for_timeout(800)
                                send = (page.query_selector('[role="dialog"] button:has-text("Send")')
                                        or page.query_selector('button:has-text("Send application")')
                                        or page.query_selector('button:has-text("Submit application")')
                                        or page.query_selector('[role="dialog"] button[type="submit"]'))
                                if not send or send.get_attribute("disabled") is not None:
                                    r["outcome"] = "skipped:send-disabled"
                                else:
                                    send.click()
                                    page.wait_for_timeout(2500)
                                    r["outcome"] = "submitted"
                                    applied += 1
                                    _mark_applied("wellfound", key, company)
                except Exception as e:  # noqa: BLE001
                    r["outcome"] = "error"; r["error"] = str(e)[:150]
                results.append(r)
                tag = (f"SUBMITTED ({applied}/{max_apply})" if r["outcome"] == "submitted"
                       else (f"error — {r.get('error','')}" if r["outcome"] == "error" else r["outcome"]))
                _say(f"  -> Wellfound {r['company']}: {tag}")
                if r["outcome"] == "captcha_stop":
                    break
                _human_pause(page, 8, 18) if r["outcome"] == "submitted" else page.wait_for_timeout(1200)
    except Exception as e:  # noqa: BLE001
        log_event("platforms", "wellfound_autoapply", "error", str(e))
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
