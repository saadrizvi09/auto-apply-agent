# AutoApply

A locally-run, single-user job-application agent. It **finds live engineering roles**,
applies to them across multiple channels — **autonomously where it safely can**, assisted
where it can't — and tracks every application. One dashboard drives:

- **Cold email outreach** — scrape LinkedIn for remote roles → resolve an HR/recruiter
  email (Hunter.io) → draft a tailored email (Groq) → send with your CV attached under
  strict anti-ban controls → scan replies to track status.
- **Autonomous applying** — an **Auto-Apply Center** that submits applications end-to-end
  on **LinkedIn Easy Apply**, **company ATS forms** (Greenhouse / Lever / Ashby),
  **Y Combinator's Work at a Startup**, **Cutshort**, and **ZipRecruiter** — each in your
  own logged-in Chrome, answering screening questions from a **learning answer bank**.
- **Google-Forms auto-apply** — ingest "Referral Alert" digests and pre-fill their forms.

**Local only.** Binds to `127.0.0.1`. No cloud, no public exposure. Every secret stays on
your machine. Built on free service tiers.

> **Honest disclaimer.** Automating applications on LinkedIn, YC, Cutshort and ZipRecruiter
> is against those platforms' Terms of Service and can get an account restricted. This tool
> runs on **your own machine, with your own logged-in session, at deliberately low volume**,
> and **stops the moment it hits a captcha / security check** — but the risk is real and
> yours to accept. Start small and watch the first runs.

---

## How applying works (the three layers)

Every autonomous channel shares the same engine:

1. **Static profile map** — name, email, phone, links, years of experience, work
   authorization, notice period, etc. come straight from `profile.json` (instant).
2. **Learning answer bank** (`answer_bank.json`) — every screening question the agent
   answers is saved, keyed by the question text. The next time that question appears
   (on any platform), it's answered instantly. Assisted runs also teach the bank from
   *your* answers.
3. **LLM fallback** (Groq) — anything the map and bank don't cover (open questions like
   "why this role?", custom numeric/skill questions) is answered truthfully **from your
   profile only**; if it can't answer honestly it replies `UNKNOWN` and the agent
   **skips** the job rather than submit a guess.

**Sensitive questions** — disability, veteran status, race, criminal history, salary
range, citizenship — are **never auto-answered**. A required sensitive question makes the
agent skip that job.

## The email pipeline

The dashboard's pipeline is five buttons, run left to right:

| # | Button | What it does |
|---|--------|------------------|
| ① | **Find Jobs** | Scrapes LinkedIn's public guest job search (no login) for remote roles across your geos, filters out interns / over-senior titles / big-cos / large teams / low salary, and resolves HR emails via Hunter. |
| ② | **Find Contacts** | Back-fills HR emails (Hunter, by company name) for jobs that didn't get one during Find Jobs. |
| ③ | **Draft Emails** | One tailored Groq email per role (90–130 words, CV attached, no links in body). US/EU rows get a remote-from-India cost/quality pitch. |
| ④ | **Review & Send** | You approve each draft; sending enforces every anti-ban rule (ramp, spacing, verify, dup guard, bounce auto-pause). |
| ⑤ | **Scan Replies** | Classifies replies (interview / rejection / needs-info / auto-ack), detects bounces, labels in Gmail. Also runs hourly. |

**Plus:** bulk **Import from file** (CSV/Excel), a **Paste & apply** box for any email or
form dump, **résumé autofill** of your profile from `cv.pdf`, and **Google-Forms
auto-apply** from referral digests.

## Auto-Apply Center

The dashboard's Auto-Apply Center applies for you across channels. Each channel runs in
your own logged-in Chrome, answers from the shared engine above, dedupes against an
already-applied set (`platform_applied.json`), uses randomised human-like pacing, and
**stops on any captcha / security check** rather than fighting it.

| Channel | What it does | Status |
|---|---|---|
| **LinkedIn + Company sites** | Easy Apply end-to-end; for non-Easy-Apply jobs it drives the company **ATS form** (Greenhouse / Lever / Ashby) — uploads the CV, answers, and **submits only after verifying the confirmation page**. Urgent/hiring posts first. | **Reliable** |
| **Y Combinator** (Work at a Startup) | Applies to startups with a **per-company, LLM-written message** (not templated — YC ignores generic blasts). Skips senior/exec roles. | ToS risk |
| **Cutshort** | Sweeps multiple skills across **remote + your city**, applies with a cover message + screening answers, flags AI-video-interview jobs as manual. | ToS risk |
| **ZipRecruiter** | 1-Click apply only; **stops instantly** on the PerimeterX press-&-hold wall (never auto-solves). Frequently blocked by design — treat as best-effort. | Fragile |

Per-channel **count box** caps a single run; blank = the daily cap. Caps are
env-overridable (`YC_DAILY_CAP=20`, `CUTSHORT_DAILY_CAP=12`, `ZIP_DAILY_CAP=6` by
default; LinkedIn ramps to `LI_DAILY_CAP`).

> **Why the channels differ.** LinkedIn Easy Apply and public ATS forms are the most
> reliable. YC/Cutshort/ZipRecruiter are login-gated and bot-defended; ZipRecruiter's
> PerimeterX is essentially unbeatable with a normal browser, so it's labelled *Fragile*
> on purpose. Cutshort caps ~15 applies/week on its free tier regardless of your settings.

## One-time logins

Each channel needs a single manual login (the platforms block scripted sign-in). The
session persists in `.browser_profile/` and is reused on every later run.

```powershell
py -3.11 formtool.py login                     # Google (for Google-Forms apply)
py -3.11 formtool.py lilogin                    # LinkedIn
py -3.11 formtool.py platlogin yc              # Y Combinator
py -3.11 formtool.py platlogin cutshort        # Cutshort
py -3.11 formtool.py platlogin ziprecruiter    # ZipRecruiter
py -3.11 formtool.py platcheck <platform>      # verify a session is actually logged in
```

## Find Jobs — how discovery works

Find Jobs scrapes LinkedIn's **public guest endpoint**
(`linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search`) — no account, lower ban
risk than the authenticated site. Each result is filtered, stored, and (quota permitting)
enriched with an HR email.

| Filter | Blank default | Effect |
|---|---|---|
| **Role** | `AI Agents Engineer`, `LLM Engineer`, `Backend Engineer` | LinkedIn keyword query (one search per role) |
| **Location** | `India`, `United States`, `Germany` | searched per geo |
| **Keywords** | — | extra terms appended to each role |
| **Min LPA** | `8` | drops jobs whose salary is shown *and* below it (LinkedIn rarely shows salary, so unknowns are kept) |
| **Max team size** | `500` | startup bias — drops companies whose Hunter headcount exceeds it |
| **Remote only** | on | LinkedIn `f_WT=2` remote filter |

**Always-on filters:** internships, over-senior titles (Staff / Principal / Director / VP
/ Head-of / Manager / Architect), and a blocklist of big enterprises, IT-services and
staffing agencies are removed — so results skew to small, reachable startups. A results
breakdown shows how many were dropped by each filter.

> HR emails are **not** on LinkedIn — they come from Hunter's domain search (by company
> name), filtered for recruiter/HR/talent roles. The urgent/"immediate hiring" posts are
> applied to first (title-based detection from the guest scraper).

## Safety defaults (read before running anything real)

- **`DRY_RUN=true` is the default.** Nothing is sent and no paid/quota API is called for
  real until you flip it off in `.env`.
- The **first real send must go to a secondary inbox you control** — set
  `FIRST_SEND_TEST_INBOX`; real sending refuses any other recipient until one real send
  succeeds.
- Every anti-ban rule is enforced before any email send:
  - **Ramping daily cap with warm-up** — Day 1–2 → 15, Day 3–5 → 30, Day 6+ → `DAILY_CAP`
    (default 20, ceiling 75). `WARMUP_RAMP=false` uses the full cap from day 1.
  - **Account-age gate** (`MIN_ACCOUNT_AGE_DAYS`), **randomised 90–180 s spacing**,
    **verify-before-send** (Hunter mailbox check), **duplicate guard** (`DUP_WINDOW_DAYS`),
    **bounce-rate auto-pause** (`BOUNCE_PAUSE_THRESHOLD`, min 20-send sample), and
    **per-email operator approval**.
- For autonomous applying: **low daily caps**, **human-like randomised pacing**,
  **char-by-char typing** (not instant paste), **per-job dedupe**, and a **hard stop on
  any captcha / checkpoint**. Submitting is irreversible — caps stay conservative on purpose.

## Requirements

- Python 3.11
- **Groq API key** (drafting + screening answers) — optional `GROQ_API_KEY_BACKUP`, used
  automatically on a 429.
- **Hunter.io API key** (HR-email discovery + verification; free tier = 50/month) —
  optional `HUNTER_API_KEY_BACKUP` for a second account when the first is exhausted.
- **Gmail API** OAuth (Desktop client) for sending + reply scanning.
- **Playwright + real Chrome** for all browser automation
  (`py -3.11 -m playwright install chromium`; the channels use your installed Chrome via
  `channel="chrome"`) and the one-time logins above.

## Setup

```powershell
# 1. Virtual environment
python -m venv .venv
.venv\Scripts\activate            # Windows  (source .venv/bin/activate on macOS/Linux)

# 2. Dependencies
pip install -r requirements.txt
py -3.11 -m playwright install chromium

# 3. Secrets + profile
copy .env.example .env            # then edit .env and fill in your keys
copy profile.example.json profile.json   # then fill in your details

# 4. (For real sending) Gmail OAuth client at credentials.json, and your CV at cv.pdf.
#    Both are git-ignored.
```

## Run

```powershell
run.bat      # normal use — no auto-reload, so apply runs finish uninterrupted
dev.bat      # development — auto-reloads on code changes (will kill an in-progress run)
```

Then open **http://127.0.0.1:8000**. (Equivalently:
`uvicorn app.main:app --host 127.0.0.1 --port 8000`.)

> Dashboard apply runs execute **inside the web server**. Use `run.bat` (no `--reload`) for
> real applying so a code change can't interrupt a batch. The CLI (`formtool.py platauto …`)
> runs as a separate process and is always interruption-proof.

## Apply from the CLI

Everything in the Auto-Apply Center is also available on the CLI (a separate process,
immune to dev-server reloads):

```powershell
py -3.11 formtool.py liapply 10                 # assisted LinkedIn: pre-fill, you submit
py -3.11 formtool.py liauto                      # autonomous LinkedIn + ATS, up to the cap
py -3.11 formtool.py platauto yc 3               # YC: apply to 3 (trailing int caps the run)
py -3.11 formtool.py platauto cutshort "backend, fastapi" 5   # Cutshort: these skills, 5 jobs
py -3.11 formtool.py platauto ziprecruiter "backend engineer" 1
```

A trailing integer caps that run; a comma-separated query targets specific skills (blank
sweeps the defaults). Set `PLATFORM_DEBUG=1` to print per-job diagnostics + screenshots.

## Run the tests

```powershell
pip install pytest
pytest
```

The suite (**100 tests**) locks the safety-critical logic: email patterns, posting dedupe,
ramp caps (send + LinkedIn), rolling bounce-rate, LinkedIn parsing + filters
(salary / blocklist / seniority / headcount), urgency scoring + urgent-first ordering, the
learning answer-bank (normalise / persist / LLM skip-if-unsure / option-matching), the
external-ATS submit/bot-wall detectors, the platform dedupe + caps, and form-field mapping.
The DOM-driving is validated live on first runs (it can't be unit-tested without a
logged-in account).

## Auto-apply to Google Forms (referral digests)

Many channels forward "Referral Alert" emails — a numbered list of roles, each ending in a
Google Form link or an email-to-apply. Paste one into the **Paste & apply** panel
(or **Scan Gmail**): form rows become form applications, email rows flow into ③ Draft → ④
Send. The engine reads each form's questions, maps them to your profile (open questions via
Groq), and builds a **pre-filled link** you open in your own logged-in Chrome to attach the
CV and Submit. **✓ Mark as submitted** records it; **×** archives the card.

```powershell
py -3.11 formtool.py check                       # confirm Google login + read a form
py -3.11 formtool.py read "<google-form-url>"    # print questions + planned answers
```

## Import contacts from a file (CSV / Excel)

Bulk-import a curated list via the **Import from file** panel or `POST /api/import`.
Columns: `company`, `role`, `email`, `domain`, `location`, `apply_url`, `verified`.
Download a starter from **Download template**. Re-importing the same file is safe
(duplicates by email are skipped).

## Résumé autofill

Drop your CV at `cv.pdf` and click **Read résumé**. It reads the PDF text + hyperlinks
(pypdf) and fills only the **blank** fields of `profile.json` — never overwriting values
you set, and skipping placeholder/dummy data.

## Going live (real APIs + real sends)

Do these in order; until you finish, leave `DRY_RUN=true`.

1. **Fill `.env`** — `GROQ_API_KEY` (+ optional backup), `HUNTER_API_KEY`, `SENDER_NAME` /
   `SIGNATURE` / `CV_SUMMARY`.
2. **Gmail OAuth** — Desktop-app `credentials.json` in the project root; the first real
   send/scan authorizes and writes `token.json` (auto-re-prompts if revoked).
3. **CV** — place your PDF at `cv.pdf` (or set `CV_PATH`). Real sends refuse without it.
4. **Warm-up gate** — `ACCOUNT_CREATED_DATE=YYYY-MM-DD` (your sender Gmail's creation date).
5. **First-send safety** — `FIRST_SEND_TEST_INBOX` = a secondary inbox **you own**.
6. **Flip the switch** — `DRY_RUN=false`, restart, approve emails one batch at a time.

> **Hunter free tier is 50 lookups/month.** Once exhausted, new email discovery and
> pre-send verification pause until reset; existing emails still send (trusting prior
> verification), protected by warm-up + bounce auto-pause.

## Files & secrets

**Git-ignored, never leave your machine:** `.env`, `credentials.json`, `token.json`,
`jobs.db`, `cv.pdf`, any résumé `*.pdf`, `jobs.csv`, `profile.json`, `answer_bank.json`,
`platform_applied.json`, `*.log`, the saved browser session `.browser_profile/`, and form
screenshots in `form_shots/`.

## Project layout

```
app/
  main.py            FastAPI app, routes, static mount, scheduler lifecycle (127.0.0.1)
  config.py          .env loader + typed settings (DRY_RUN default on; warm-up; backup keys)
  db.py              SQLite engine, schema init + self-healing migrations, repository helpers
  models.py          SQLModel models (companies[+salary,+headcount,+urgent], contacts,
                     applications[+apply_kind/form_*/email_cc/archived], send_log, settings)
  logic.py           pure safety logic (patterns, dedupe, ramp caps, bounce rate + min-sample)
  profile.py         applicant profile loader (profile.json) for form-filling
  services/          discovery (LinkedIn+Hunter), contacts, drafting (+intl pitch), sender (anti-ban),
                     replies, importer, resume (CV->profile), referrals, formfiller, forms,
                     answer_bank (learning bank + LLM screening fallback),
                     linkedin_apply (Easy Apply + ATS auto-submit, urgent-first),
                     platform_apply (YC / Cutshort / ZipRecruiter orchestration + caps)
  integrations/      gmail, groq_client (+backup-key failover), hunter (multi-key rotation),
                     linkedin (guest jobs scraper + urgency scoring), cse (legacy),
                     browser (Playwright/real Chrome: forms + LinkedIn Easy Apply + external ATS),
                     platforms (YC / Cutshort / ZipRecruiter drivers, login + dedupe)
  prompts.py         draft (India + international variants) + classify prompt templates
formtool.py          CLI: logins (Google/LinkedIn/platforms) + form tools + liapply/liauto/platauto
static/              index.html, app.js, referrals.js, styles.css  (dashboard + Auto-Apply Center)
tests/               unit tests (100): safety logic, LinkedIn discovery, answer-bank,
                     external-ATS detectors, platform caps/dedupe, urgency/queue order
run.bat / dev.bat    normal-use (no reload) / development (reload) launchers
```

See `docs/` for the full PRD, SRS, Architecture, Technical Spec, and Build Runbook.
