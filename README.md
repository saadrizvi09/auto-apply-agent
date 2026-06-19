# AutoApply — Free Job-Application Emailer

A locally-run, single-user tool that **finds live remote engineering roles on LinkedIn**,
resolves an HR/recruiter email for each (via Hunter.io), drafts a tailored application
email, and sends it with your PDF CV attached — under strict anti-ban controls — then
scans replies to track status. It also ingests "Referral Alert" digest emails and
**auto-fills their Google Forms** using pre-filled links you open in your own logged-in
browser, and **assists LinkedIn Easy Apply** by pre-filling the application for you to
review and submit.

**Local only.** Binds to `127.0.0.1`. No cloud, no public exposure. All secrets stay
on your machine. Built on free service tiers.

> Supporting channel. Run alongside referrals and LinkedIn applications. Volume is
> capped for deliverability safety — by design.

---

## The pipeline

The dashboard is five buttons, run left to right:

| # | Button | What it does now |
|---|--------|------------------|
| ① | **Find Jobs** | Scrapes LinkedIn's public guest job search (no login) for remote roles across your geos, filters out interns / over-senior titles / big-cos / large teams / low salary, and resolves HR emails via Hunter. |
| ② | **Find Contacts** | Back-fills HR emails (Hunter, by company name) for any jobs that didn't get one during Find Jobs. |
| ③ | **Draft Emails** | One tailored Groq email per role (90–130 words, CV attached, no links in body). US/EU rows get a remote-from-India cost/quality pitch. |
| ④ | **Review & Send** | You approve each draft; sending enforces every anti-ban rule (ramp, spacing, verify, dup guard, bounce auto-pause). |
| ⑤ | **Scan Replies** | Classifies replies (interview / rejection / needs-info / auto-ack), detects bounces, labels in Gmail. Also runs hourly. |

**Plus:** bulk **Import from file** (CSV/Excel), a **Paste & apply** box for any email
or form dump, **résumé autofill** of your profile from `cv.pdf`, **Google-Forms
auto-apply** from referral digests (parse → pre-fill link → review → submit), and
**assisted LinkedIn Easy Apply** (pre-fill → you submit).

## Find Jobs — how discovery works

Find Jobs scrapes LinkedIn's **public guest endpoint**
(`linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search`) — no account, lower ban
risk than the authenticated site. Each result is filtered, stored, and (quota
permitting) enriched with an HR email.

**Filter inputs (dashboard "Find jobs" panel):**

| Filter | Blank default | Effect |
|---|---|---|
| **Role** | `AI Agents Engineer`, `LLM Engineer`, `Backend Engineer` | LinkedIn keyword query (one search per role) |
| **Location** | `India`, `United States`, `Germany` | searched per geo, remote-only |
| **Keywords** | — | extra terms appended to each role |
| **Min LPA** | `8` | drops jobs whose salary is shown *and* below it (LinkedIn rarely shows salary, so unknowns are kept) |
| **Max team size** | `500` | startup bias — drops companies whose Hunter headcount exceeds it |
| **Remote only** | on | LinkedIn `f_WT=2` remote filter |

**Always-on filters:** internships, over-senior titles (Staff / Principal / Director /
VP / Head-of / Manager / Architect), and a blocklist of big enterprises, IT-services and
staffing agencies are removed — so results skew to small, reachable startups. After each
run a **results breakdown** shows how many were dropped by each filter.

> HR emails are **not** on LinkedIn — they come from Hunter's domain search (by company
> name), filtered for recruiter/HR/talent roles. The legacy Google Custom Search path
> (`discover_ats`) is retained as a fallback but is not used by default.

## Safety defaults (read before you run anything real)

- **`DRY_RUN=true` is the default.** Nothing is sent and no paid/quota API is called for
  real until you flip it off in `.env`.
- The **first real send must go to a secondary inbox you control** — set
  `FIRST_SEND_TEST_INBOX` in `.env`; real sending refuses any other recipient until one
  real send has succeeded.
- Every anti-ban rule is enforced before any send:
  - **Ramping daily cap with warm-up** — Day 1–2 → 15, Day 3–5 → 30, Day 6+ → `DAILY_CAP`
    (the ramp target you set in `.env`; shipped default 20, absolute ceiling 75). Set
    `WARMUP_RAMP=false` to use the full cap from day 1.
  - **Warm-up gate** — sending is blocked until the sender account is `MIN_ACCOUNT_AGE_DAYS` old.
  - **Randomized 90–180 s delays** between sends (human-like spacing).
  - **Verify-before-send** — unverified recipients are skipped, unless you explicitly
    check them in Review & Send (a confirm dialog appears). Hunter-found emails are also
    **mailbox-verified** right before sending (when Hunter quota remains); a definitively
    invalid address is skipped, never bounced.
  - **Duplicate guard** — one email per company within `DUP_WINDOW_DAYS`.
  - **Bounce-rate auto-pause** — if the rolling bounce rate (min 20-send sample, counted
    once per address) crosses `BOUNCE_PAUSE_THRESHOLD`, sending pauses; **Resume** starts
    a fresh window.
  - **Per-email operator approval** — nothing sends without your explicit checkbox.

## Requirements

- Python 3.11
- A **Groq API key** (drafting + form answers) — and optionally a second one as
  `GROQ_API_KEY_BACKUP`, used automatically on a 429 rate-limit.
- A **Hunter.io API key** (HR-email discovery + verification; free tier = 50/month) —
  and optionally a second account's key as `HUNTER_API_KEY_BACKUP`, used automatically
  when the first's monthly quota is exhausted.
- **Gmail API** OAuth (Desktop client) for sending + reply scanning.
- For Google-Forms auto-apply **and** assisted LinkedIn Easy Apply: Playwright + Chromium
  (`py -3.11 -m playwright install chromium`) and a one-time Google / LinkedIn login.
- *(Optional / legacy)* a Google Custom Search key + engine if you want the old ATS-board
  discovery fallback.

See `docs/05-Build-Runbook.md` for step-by-step key setup.

## Setup

```powershell
# 1. Create and activate a virtual environment
python -m venv .venv
.venv\Scripts\activate            # Windows
# source .venv/bin/activate       # macOS / Linux

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure secrets
copy .env.example .env            # then edit .env and fill in your keys

# 4. (For real sending) put your Gmail OAuth client at credentials.json
#    and your CV at cv.pdf. Both are git-ignored.
```

## Run

```powershell
# Local-only bind. Open http://127.0.0.1:8000
uvicorn app.main:app --host 127.0.0.1 --port 8000
# or:  python -m app.main
```

## Run the tests

```powershell
pip install pytest
pytest
```

The suite (63 tests) locks the safety-critical logic before anything depends on it:
email patterns, posting dedupe, ramp-cap, rolling bounce-rate (incl. min-sample),
LinkedIn parsing + filters (salary / blocklist / seniority / headcount), referral
parsing, and form-field mapping.

## Import contacts from a file (CSV / Excel)

Instead of (or alongside) ① Find Jobs, bulk-import a curated list via the **Import from
file** panel (drag-and-drop) or `POST /api/import`.

| Column | Meaning |
|---|---|
| `company` | Company / organization name |
| `role` | Role title |
| `email` | Contact email (rows with one become `email_found`) |
| `domain` | Company domain (auto-derived from the email if omitted) |
| `location` | City / "Remote" |
| `apply_url` | Portal-apply link (rows with no email stay `discovered`) |
| `verified` | `yes`/`true` marks the email trusted (skips Hunter) |

Download a starter from **Download template** (`GET /api/import/template`). Re-importing
the same file is safe — duplicates (by email) are skipped.

## Résumé autofill

Drop your CV at `cv.pdf` and click **Read résumé** (or `POST /api/profile/read-resume`).
It reads the PDF text + hyperlinks (pypdf) and fills only the **blank** fields of your
`profile.json` (email, phone, LinkedIn/GitHub/LeetCode links, etc.) — it never overwrites
values you set, and skips placeholder/dummy data.

## Auto-apply to Google Forms (referral digests)

Many channels forward "Referral Alert" emails — a numbered list of roles, each ending in
**either a Google Form link or an email-to-apply**. AutoApply ingests these and applies
under the same review-then-act discipline.

**One-time setup**

1. **Install the browser engine:**
   ```powershell
   pip install -r requirements.txt
   py -3.11 -m playwright install chromium
   ```
2. **Log into Google once** — these forms require sign-in, so the filler drives **real
   Chrome** (`channel="chrome"`; the bundled Chromium is blocked by Google at sign-in),
   saving the session in `.browser_profile/`:
   ```powershell
   py -3.11 formtool.py login
   ```
3. **Fill your profile** — copy `profile.example.json` to `profile.json` and fill it in
   (name, email, phone, college, graduation year, **public CV link**, LinkedIn, GitHub,
   LeetCode, expected stipend …). Set the CV link to **Anyone with the link → Viewer**.

**Daily use (dashboard → referral / "Paste & apply" panel)**

1. **Paste** the whole referral email and click **Parse & add jobs**. Google-Form rows
   become form applications; email-to-apply rows flow into ③ Draft → ④ Send (with the
   digest's subject, any Cc, and a links footer). Or **Scan Gmail** to pull digests.
2. **Prepare pre-filled links** — the engine reads each form's questions, maps them to
   your profile (open questions like "why should we hire you" answered by Groq), and
   builds a **pre-filled link** (`viewform?usp=pp_url&entry.X=…`).
3. **Open pre-filled form ↗** — opens the form **already filled, all pages**, in your own
   logged-in Chrome. Attach your CV (file-upload questions must be done by hand — Google
   blocks programmatic upload + submit), then **Submit**. Click **✓ Mark as submitted**
   to record it. The **×** button **archives** a card (kept in history; Shift-click to
   delete permanently).

Validate the engine on one form from the CLI:
```powershell
py -3.11 formtool.py check                       # confirm login + read a form
py -3.11 formtool.py read "<google-form-url>"    # print questions + planned answers
```

## Apply on LinkedIn (assisted)

For LinkedIn jobs you found in ① Find Jobs, the **Apply on LinkedIn (assisted)** panel
opens the **Easy Apply** flow in your own logged-in Chrome and **pre-fills** it — then
**you review and click Submit**. It never auto-submits.

> **Why assisted, not fully automatic:** automating actions on your real LinkedIn account
> violates LinkedIn's Terms and risks an account ban, and a hands-off bot would answer
> screening questions wrong and submit irreversibly. So the safe, consistent design (same
> as the Google-Forms flow) is pre-fill + human submit. Sensitive questions — work
> authorization, visa sponsorship, citizenship, EEO/diversity, background — are
> **deliberately left blank** for you to answer.

**One-time setup** — log into LinkedIn once in the same browser profile:
```powershell
py -3.11 formtool.py lilogin     # log into LinkedIn, then close the window
```

**Use it** — dashboard **Apply on LinkedIn** (opens up to 10 matching jobs at once), or:
```powershell
py -3.11 formtool.py liapply 10  # pre-fill up to 10 Easy-Apply jobs, hold for review
```
In each tab: answer any highlighted screening questions, click Next (later steps auto-fill
too), then **Submit yourself**. Non-Easy-Apply jobs are flagged to apply on the company site.

> Best-effort: LinkedIn changes its page markup and detects automation, so start with a
> small batch and watch for any "unusual activity" prompts. The safe-field mapping that
> decides which questions to auto-answer is unit-tested; the page-driving is not.

## Going live (real APIs + real sends)

Do these in order. Until you finish, leave `DRY_RUN=true`.

1. **Fill `.env`** — `GROQ_API_KEY` (+ optional `GROQ_API_KEY_BACKUP`), `HUNTER_API_KEY`,
   and your `SENDER_NAME` / `SIGNATURE` / `CV_SUMMARY`. (CSE keys optional/legacy.)
2. **Gmail OAuth** — put the Desktop-app `credentials.json` in the project root. The
   first real send/scan opens a browser to authorize and writes `token.json`. (If the
   token is ever revoked, the app re-prompts a fresh login automatically.)
3. **CV** — place your PDF at `cv.pdf` (or set `CV_PATH`). Real sends refuse without it.
4. **Warm-up gate** — set `ACCOUNT_CREATED_DATE=YYYY-MM-DD` to your sender Gmail's
   creation date.
5. **First-send safety** — set `FIRST_SEND_TEST_INBOX` to a secondary inbox **you own**.
   The first real send is refused unless addressed there.
6. **Flip the switch** — set `DRY_RUN=false` and restart. Approve emails one batch at a
   time; nothing sends without your per-email approval.

If the bounce rate crosses `BOUNCE_PAUSE_THRESHOLD`, sending auto-pauses — fix/remove the
bad addresses, then **Resume** (which starts a fresh bounce window).

> **Hunter free tier is 50 lookups/month.** Find Jobs caps HR lookups per run; once the
> month is exhausted, new email discovery and pre-send verification pause until reset —
> existing emails still send (trusting prior verification), protected by the warm-up +
> bounce auto-pause.

## Files & secrets

**Git-ignored, never leave your machine:** `.env`, `credentials.json`, `token.json`,
`jobs.db`, `cv.pdf`, any résumé `*.pdf`, `jobs.csv`, `profile.json`, `*.log`, the saved
browser session `.browser_profile/`, and form screenshots in `form_shots/`.

## Project layout

```
app/
  main.py            FastAPI app, routes, static mount, scheduler lifecycle (127.0.0.1)
  config.py          .env loader + typed settings (DRY_RUN default on; WARMUP_RAMP; backup Groq key)
  db.py              SQLite engine, schema init + self-healing migrations, repository helpers
  models.py          SQLModel models (companies[+salary,+headcount], contacts,
                     applications[+apply_kind/form_*/email_cc/archived], send_log, settings)
  logic.py           pure safety logic (patterns, dedupe, ramp cap, bounce rate + min-sample)
  logging_setup.py   structured autoapply.log
  profile.py         applicant profile loader (profile.json) for form-filling
  services/          discovery (LinkedIn+Hunter), contacts (Hunter back-fill), drafting (+intl pitch),
                     sender (anti-ban), replies, importer, resume (CV->profile),
                     referrals (digest parse), formfiller (Q->A map), forms (pre-fill orchestration),
                     linkedin_apply (assisted Easy Apply: pre-fill, operator submits)
  integrations/      gmail, groq_client (+backup-key failover), hunter (verify + HR domain-search,
                     multi-key rotation), linkedin (guest jobs scraper), cse (legacy),
                     browser (Playwright/real Chrome: Google Forms + LinkedIn Easy Apply)
  prompts.py         draft (India + international variants) + classify prompt templates
formtool.py          CLI: Google/LinkedIn login + check / read / fill a form + liapply
static/              index.html, app.js, referrals.js, styles.css  (dashboard)
tests/               unit tests (63): pure logic + LinkedIn discovery + referral parse + form mapping
```

See `docs/` for the full PRD, SRS, Architecture, Technical Spec, and Build Runbook.
