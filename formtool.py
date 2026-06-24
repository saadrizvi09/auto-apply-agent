"""Standalone helper to set up + test the Google-Forms browser engine.

Run these from the project root (the app server does NOT need to be running):

  py -3.11 formtool.py login
      Opens a visible browser. Log into the Google account you apply with,
      then close the window. The session is saved in .browser_profile/ and
      reused for every later run (forms require sign-in).

  py -3.11 formtool.py read  <form_url>
      Opens the form and prints the questions it found + how each would be
      answered from your profile.json. Reads only - never submits.

  py -3.11 formtool.py check
      Quick test: are we signed into Google? Tells you if login is valid.

  py -3.11 formtool.py fill  <form_url>
      Fills the form and saves a screenshot to form_shots/ - does NOT submit.
      Use this to eyeball that the answers land in the right fields.

  py -3.11 formtool.py fillall
      Fills EVERY pending form job from the dashboard list (visible browser),
      so you don't have to fill them one at a time. Does NOT submit.

  py -3.11 formtool.py lilogin
      One-time LinkedIn login (same .browser_profile). Log in, close the window.

  py -3.11 formtool.py liapply [N]
      Assisted LinkedIn Easy Apply: opens up to N matching jobs (default 10),
      pre-fills the safe fields, and HOLDS the window for you to review the
      screening questions and Submit. Never submits for you.

  py -3.11 formtool.py lihard [N]
      HARD APPLY (assisted): opens up to N NON-Easy-Apply jobs (default 12),
      walks into each company ATS, AI-fills everything it knows, and HOLDS the
      windows for you to review + Submit. Never submits for you.

  py -3.11 formtool.py platlogin <yc|cutshort|ziprecruiter|wellfound|instahyre>
      One-time login for an external platform (same .browser_profile). Log in,
      close the window. Required before that platform's auto-apply will work.

  py -3.11 formtool.py platauto <yc|cutshort|ziprecruiter|wellfound|instahyre> [query]
      AUTONOMOUS apply on that platform up to its daily cap. ToS-restricted +
      bot-defended; it STOPS on any security check. Watch the window.
"""
from __future__ import annotations

import sys

from app.integrations import browser
from app.profile import load_profile, missing_required
from app.services.formfiller import fill_open_answers, plan_answers


def _print_plan(planned: list[dict]) -> None:
    for p in planned:
        src = p["source"].upper()
        ans = p.get("answer", "")
        ans = (ans[:64] + "...") if len(ans) > 64 else ans
        flag = ""
        if p.get("blocked"):
            flag = "  << UPLOAD: do manually"
        elif p["source"] == "missing":
            flag = f"  << FILL profile.json: {p.get('missing_field')}"
        print(f"  [{src:8}] {p['title'][:46]:46} -> {ans!r}{flag}")


def cmd_login() -> None:
    browser.launch_login()
    print("Login session saved to .browser_profile/")


def cmd_read(url: str) -> None:
    info = browser.read_form(url)
    if info["signin_required"]:
        print("This form requires sign-in. Run:  py -3.11 formtool.py login")
        return
    if info["error"]:
        print("Error:", info["error"])
        return
    print(f"Form: {info['title']}\nFound {len(info['questions'])} questions:\n")
    planned = fill_open_answers(plan_answers(info["questions"]))
    _print_plan(planned)
    miss = missing_required()
    if miss:
        print("\nProfile gaps (fill profile.json):", ", ".join(miss))


def cmd_fill(url: str) -> None:
    info = browser.read_form(url)
    if info["signin_required"]:
        print("This form requires sign-in. Run:  py -3.11 formtool.py login")
        return
    planned = fill_open_answers(plan_answers(info["questions"]),
                               company="", role=info.get("title", ""))
    shot = str(browser.SHOTS_DIR / "preview.png")
    res = browser.fill_form(url, planned, shot, submit=False)
    print(f"Filled {res['filled']}/{res['total']} fields. Screenshot: {res['screenshot']}")
    if res["error"]:
        print("Note:", res["error"])


def cmd_fillall() -> None:
    """Fill every pending form job (from the dashboard list) in a visible browser."""
    from app.services import forms

    res = forms.fill_pending()
    print("\n" + res["message"] + "\n")
    for f in forms.list_jobs()["forms"]:
        print(f"  [{f['status']:13}] {f['company']} - {f['role']}")
        if f["note"]:
            print(f"        {f['note']}")
    print("\nReview each form, then submit from the dashboard "
          "(set DRY_RUN=false in .env first for a real submit).")


def cmd_apply() -> None:
    """Open each pending form pre-filled and hold it open so you attach the CV + submit."""
    from app.db import get_session, list_form_jobs
    from app.profile import load_profile

    with get_session() as s:
        jobs = [j for j in list_form_jobs(s)
                if j["status"] in ("form_found", "form_filled", "form_error")]
    if not jobs:
        print("No forms to apply to. Paste a referral email in the dashboard first.")
        return
    # Dedupe by form URL so shared forms (e.g. 3 Times Internet roles) appear once.
    seen, uniq = set(), []
    for j in jobs:
        key = (j["form_url"] or "").split("?")[0]
        if key and key not in seen:
            seen.add(key)
            uniq.append({"url": j["form_url"], "company": j["company"], "role": j["role"]})
    print(f"\n{len(uniq)} form(s) to apply to (shared forms merged). For EACH: it "
          "auto-fills, you attach your resume, click Submit, then CLOSE the tab.\n")

    def plan_fn(questions, job):
        return fill_open_answers(plan_answers(questions),
                                 company=job["company"], role=job["role"])

    results = browser.apply_session(uniq, plan_fn)
    print("\n--- summary ---")
    for r in results:
        if r.get("signin_required"):
            print(f"  {r['company']}: NOT logged in - run: py -3.11 formtool.py login")
        elif r.get("error"):
            print(f"  {r['company']}: error - {r['error']}")
        else:
            print(f"  {r['company']}: filled {r['filled']}/{r['total']}, "
                  f"resume {'attached' if r['resume_attached'] else 'manual'}")
    print("All forms handled.")


def cmd_check() -> None:
    """Quick login check: are we signed into Google for the forms?"""
    url = "https://docs.google.com/forms/d/e/1FAIpQLSef9PKc_zNrJcKxK0TlDsk4oSSo9Ybb-Vh6O4lWkYRjrRIQaQ/viewform"
    info = browser.read_form(url)
    if info["signin_required"]:
        print("NOT logged in - the form bounced to Google sign-in.\n"
              "Run:  py -3.11 formtool.py login   (sign in fully, then close the window)")
    elif info["error"]:
        print("Error:", info["error"])
    else:
        print(f"Logged in OK - read {len(info['questions'])} questions from the test form.")


def cmd_lilogin() -> None:
    """One-time LinkedIn login in the same .browser_profile (separate from Google)."""
    browser.launch_linkedin_login()
    print("LinkedIn login saved to .browser_profile/")


def cmd_liapply(limit: int) -> None:
    """Assisted LinkedIn Easy Apply: pre-fill matching jobs, hold for review+submit."""
    from app.services import linkedin_apply
    res = linkedin_apply.apply_assisted(limit=limit)
    print(res.get("message", ""))


def cmd_licheck() -> None:
    """Is the saved LinkedIn session still logged in?"""
    if browser.linkedin_logged_in():
        print("LinkedIn: logged in OK - the session is saved and will persist.")
    else:
        print("LinkedIn: NOT logged in. Run:  py -3.11 formtool.py lilogin")


def cmd_liauto() -> None:
    """AUTONOMOUS LinkedIn Easy Apply: auto-submit up to the daily ramp cap."""
    from app.services import linkedin_apply
    res = linkedin_apply.autoapply()
    print(res.get("message", ""))


def cmd_lihard(limit: int) -> None:
    """HARD APPLY (assisted): open non-Easy-Apply jobs, AI-fill the company ATS, you Submit."""
    from app.services import linkedin_apply
    res = linkedin_apply.hard_apply_assisted(limit=limit)
    print(res.get("message", ""))


_PLATFORMS = ("yc", "cutshort", "ziprecruiter", "wellfound", "instahyre")


def cmd_platlogin(platform: str) -> None:
    """One-time login for an external platform (YC / Cutshort / ZipRecruiter)."""
    from app.integrations import platforms
    if platform not in _PLATFORMS:
        print(f"Unknown platform '{platform}'. Use one of: {', '.join(_PLATFORMS)}")
        return
    platforms.launch_login(platform)
    print(f"{platform} login saved to .browser_profile/")


def cmd_platcheck(platform: str) -> None:
    """Diagnostic login check: reports logged-in state, the landing URL, the reason, and
    a screenshot path of what the bot saw. Never applies to anything."""
    from app.integrations import platforms
    if platform not in _PLATFORMS:
        print(f"Unknown platform '{platform}'. Use one of: {', '.join(_PLATFORMS)}")
        return
    r = platforms.login_probe(platform)
    state = "LOGGED IN" if r["logged_in"] else "NOT logged in"
    print(f"{platform}: {state}")
    print(f"  landed on : {r['url']}")
    print(f"  reason    : {r['reason']}")
    if r.get("shot"):
        print(f"  screenshot: {r['shot']}  (open it to see what the bot sees)")
    if not r["logged_in"]:
        print(f"  -> if you ARE logged in there, this is a false negative — tell me the "
              f"URL+reason above. Otherwise run:  py -3.11 formtool.py platlogin {platform}")


def cmd_platauto(platform: str, args: list[str]) -> None:
    """AUTONOMOUS apply on an external platform up to its daily cap.

    A trailing integer caps this run (e.g. `platauto yc "ai ml engineer" 1` = try 1 job),
    useful for a watched first-run validation."""
    from app.services import platform_apply
    if platform not in _PLATFORMS:
        print(f"Unknown platform '{platform}'. Use one of: {', '.join(_PLATFORMS)}")
        return
    limit = None
    toks = list(args)
    if toks and toks[-1].isdigit():
        limit = int(toks[-1])
        toks = toks[:-1]
    res = platform_apply.autoapply(platform, query=" ".join(toks), limit=limit)
    print(res.get("message", ""))


def main() -> None:
    try:  # job titles can carry non-Latin chars the Windows cp1252 console can't encode
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    if len(sys.argv) < 2:
        print(__doc__)
        return
    cmd = sys.argv[1].lower()
    if cmd == "login":
        cmd_login()
    elif cmd == "check":
        cmd_check()
    elif cmd == "apply":
        cmd_apply()
    elif cmd == "fillall":
        cmd_fillall()
    elif cmd == "lilogin":
        cmd_lilogin()
    elif cmd == "licheck":
        cmd_licheck()
    elif cmd == "liauto":
        cmd_liauto()
    elif cmd == "liapply":
        cmd_liapply(int(sys.argv[2]) if len(sys.argv) >= 3 else 10)
    elif cmd == "lihard":
        cmd_lihard(int(sys.argv[2]) if len(sys.argv) >= 3 else 12)
    elif cmd == "platlogin" and len(sys.argv) >= 3:
        cmd_platlogin(sys.argv[2].lower())
    elif cmd == "platcheck" and len(sys.argv) >= 3:
        cmd_platcheck(sys.argv[2].lower())
    elif cmd == "platauto" and len(sys.argv) >= 3:
        cmd_platauto(sys.argv[2].lower(), sys.argv[3:])
    elif cmd == "read" and len(sys.argv) >= 3:
        cmd_read(sys.argv[2])
    elif cmd == "fill" and len(sys.argv) >= 3:
        cmd_fill(sys.argv[2])
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
