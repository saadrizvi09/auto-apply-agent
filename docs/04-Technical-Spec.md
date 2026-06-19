# Technical Specification (Design Spec)
### Project: AutoApply — Free Job-Application Emailer
**Version:** 1.1 · **Status:** Final · **Last updated:** 2026-06-18

---

## 1. Project structure

```
autoapply/
├── app/
│   ├── main.py            # FastAPI app, routes, static mount, scheduler start
│   ├── config.py          # env + settings loader
│   ├── db.py              # SQLite engine, schema init + self-healing migrations, repo helpers
│   ├── models.py          # SQLModel models
│   ├── logic.py           # pure safety math (patterns, dedupe, ramp cap, bounce rate)
│   ├── profile.py         # applicant profile (profile.json) used to auto-fill forms
│   ├── logging_setup.py   # structured log setup + log_event helper
│   ├── services/
│   │   ├── discovery.py   # LinkedIn guest jobs (discover) + legacy CSE (discover_ats)
│   │   ├── contacts.py    # Hunter-by-name + page/site/pattern waterfall + verify
│   │   ├── drafting.py    # Groq draft generation
│   │   ├── sender.py      # throttled, capped, safe sending via Gmail
│   │   ├── replies.py     # Gmail read + Groq classify + status update
│   │   ├── importer.py    # bulk CSV/XLSX import of companies + contacts
│   │   ├── resume.py      # pypdf: read cv.pdf -> fill profile.json
│   │   ├── referrals.py   # parse referral digests -> form/email/manual jobs
│   │   ├── forms.py       # Google-Forms auto-apply orchestrator (ingest->fill->submit)
│   │   └── formfiller.py  # pure question->profile-field mapping + answer planning
│   ├── integrations/
│   │   ├── gmail.py       # OAuth + send + list/read + label
│   │   ├── groq_client.py # OpenAI-compatible client (primary + backup key)
│   │   ├── cse.py         # Google Custom Search wrapper (legacy)
│   │   ├── hunter.py      # find-HR-emails + verify wrapper
│   │   ├── linkedin.py    # LinkedIn guest jobs scraper (BeautifulSoup)
│   │   └── browser.py     # Playwright (real Chrome) engine for Google Forms
│   └── prompts.py         # draft + classify prompt templates
├── static/                # index.html, applications.html, app.js, styles.css (dashboard)
├── formtool.py            # CLI: set up / test the Google-Forms browser engine (login/read/...)
├── .env                   # secrets (git-ignored)
├── credentials.json       # Gmail OAuth client (git-ignored)
├── token.json             # Gmail OAuth token (git-ignored, created on first auth)
├── profile.json           # applicant form-answer profile (git-ignored)
├── .browser_profile/      # persistent Playwright context (one-time Google login)
├── form_shots/            # form-fill review screenshots
├── cv.pdf                 # CV attachment (path configurable)
├── jobs.db                # SQLite (git-ignored)
├── requirements.txt
└── README.md
```

## 2. Data model (SQLite)

```sql
CREATE TABLE companies (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  domain TEXT,
  source_url TEXT UNIQUE,          -- posting URL (dedupe key)
  role_title TEXT,
  location TEXT,
  salary TEXT,                     -- raw salary string from the posting (if any)
  remote INTEGER DEFAULT 0,
  headcount TEXT,                  -- Hunter employee range, e.g. "11-50" (startup signal)
  discovered_at TEXT
);

CREATE TABLE contacts (
  id INTEGER PRIMARY KEY,
  company_id INTEGER REFERENCES companies(id),
  email TEXT,
  apply_url TEXT,                  -- if apply is via portal not email
  source TEXT,                     -- posting|scraped|pattern
  verified INTEGER DEFAULT 0,
  confidence REAL,
  created_at TEXT
);

CREATE TABLE applications (
  id INTEGER PRIMARY KEY,
  company_id INTEGER REFERENCES companies(id),
  contact_id INTEGER REFERENCES contacts(id),
  status TEXT,                     -- see status enum below
  email_subject TEXT,
  email_cc TEXT,                   -- extra Cc recipient (from referral digests)
  email_body TEXT,
  gmail_thread_id TEXT,
  gmail_message_id TEXT,
  sent_at TEXT,
  last_checked_at TEXT,
  reply_excerpt TEXT,
  -- Google-Forms auto-apply (referral digests):
  apply_kind TEXT,                 -- email | form | manual
  form_url TEXT,
  form_answers TEXT,               -- JSON: planned answers for review
  form_screenshot TEXT,            -- filename under form_shots/
  form_note TEXT,                  -- last fill/submit note (e.g. needs login)
  form_prefill_url TEXT,           -- pre-filled form link (opens filled in your browser)
  archived INTEGER NOT NULL DEFAULT 0  -- hidden from active panels but kept as history
);

CREATE TABLE send_log (
  id INTEGER PRIMARY KEY,
  application_id INTEGER,
  ts TEXT,
  outcome TEXT,                    -- sent|bounced|skipped|error
  detail TEXT
);

CREATE TABLE settings (
  key TEXT PRIMARY KEY,
  value TEXT
);
```

**status enum:**
`discovered → email_found → drafted → approved → sent →
{replied_interview | replied_rejection | replied_needinfo | auto_ack | bounced | no_reply}`

Google-Forms apply track (apply_kind = form):
`form_found → form_filled → form_submitted` (`form_error` on a failed fill).

**Schema migrations** are self-healing in `db.init_db()`: it runs `PRAGMA
table_info(...)` and `ALTER TABLE ... ADD COLUMN` for any of the columns above
(`salary`, `headcount`, `apply_kind`, `form_url`, `form_answers`, `form_screenshot`,
`form_note`, `form_prefill_url`, `email_cc`, `archived`) missing from an older DB, so
existing `jobs.db` files upgrade in place on startup.

## 3. Configuration (`.env` + settings)

```
# Safety
DRY_RUN=true                 # default true: no network/quota, nothing actually sent
FIRST_SEND_TEST_INBOX=       # first real send must go here (a secondary inbox you own)

# API keys
GROQ_API_KEY=
GROQ_API_KEY_BACKUP=         # optional; used automatically on a 429 rate-limit
GOOGLE_CSE_API_KEY=          # legacy (CSE/ATS discovery path only)
GOOGLE_CSE_ID=               # legacy
HUNTER_API_KEY=

# Behavior (overridable in settings table via UI)
CV_PATH=./cv.pdf
CV_SUMMARY=                  # short candidate summary used in draft prompts
ACCOUNT_CREATED_DATE=        # ISO date; warm-up gate compares against it
SENDER_NAME=Your Name
SIGNATURE=Your Name
DAILY_CAP=50                 # ramp target; see schedule below (hard-clamped to 75)
WARMUP_RAMP=true             # false = allow DAILY_CAP from day 1 (still clamped to 75)
SEND_DELAY_MIN=90            # seconds
SEND_DELAY_MAX=180
BOUNCE_PAUSE_THRESHOLD=0.03  # 3%
MIN_ACCOUNT_AGE_DAYS=14      # warm-up gate; ACCOUNT_CREATED_DATE must be this old
DUP_WINDOW_DAYS=5
SCAN_INTERVAL_MINUTES=60
GMAIL_LABEL=AutoApply
REQUIRE_VERIFIED=true        # block sends to unverified unless overridden
```

Defaults shown are the code defaults in `config.Settings`. Other constants live in
code, not `.env`: `config.HUNTER_MONTHLY_LIMIT=50` and `CSE_DAILY_LIMIT=100` (quota
display); `logic.RAMP_HARD_MAX=75` (hard ceiling on the daily cap) and
`logic.MIN_BOUNCE_SAMPLE=20` (min sends before the bounce-rate pause can fire).

**Ramp schedule (`ramp_cap_for_today`, reads day-since-first-send):**
Day 1–2 → 15/day · Day 3–5 → 30/day · Day 6+ → up to DAILY_CAP, capped at
`min(DAILY_CAP, 75)`. When `WARMUP_RAMP=false` the ramp tiers are skipped and the cap
is `min(DAILY_CAP, 75)` from day 1.

## 4. API endpoints (FastAPI)

| Method | Path | Purpose |
|---|---|---|
| GET | `/` | Serve dashboard (`index.html`) |
| GET | `/applications` | Serve application-tracker page (`applications.html`) |
| GET | `/api/applications` | Full application detail + status summary |
| GET | `/api/state` | State + quotas + send budget + paused + bounce_rate + send_status |
| GET | `/api/quota` | Remaining CSE/Hunter/daily-send budget |
| POST | `/api/discover` | Body: `DiscoverFilters` → run LinkedIn discovery → summary |
| POST | `/api/contacts` | Resolve+verify contacts for postings without one |
| POST | `/api/draft` | Generate drafts for postings in `email_found` |
| GET | `/api/drafts` | List drafts for review |
| POST | `/api/send` | Body: `SendRequest` → background throttled send of approved |
| POST | `/api/scan` | Run reply scan now (also runs hourly via scheduler) |
| POST | `/api/settings` | Update behavior settings and/or pause/resume sending |
| GET | `/api/import/template` | Download CSV import template |
| POST | `/api/import` | Bulk-import companies + contacts from uploaded `.csv`/`.xlsx` |
| POST | `/api/referrals/ingest` | Parse a pasted referral digest → store its jobs |
| POST | `/api/referrals/scan` | Scan Gmail for referral digests + ingest (real mode) |
| GET | `/api/forms` | All form-apply jobs + fill/review state |
| POST | `/api/forms/fill` | Background: fill pending forms + screenshot (never submits) |
| POST | `/api/forms/prefill` | Background: build pre-filled form links to open yourself |
| POST | `/api/forms/submit` | Operator-approved submit of one reviewed form |
| POST | `/api/forms/mark-submitted` | Record a form submitted by hand |
| POST | `/api/forms/delete` | Archive a form job (kept as history) |
| POST | `/api/forms/delete-permanent` | Permanently delete a form job (destructive) |
| GET | `/form_shots/{filename}` | Serve a form-fill review screenshot |
| GET | `/api/profile` | Current applicant profile + still-blank required fields |
| POST | `/api/profile/read-resume` | Read `cv.pdf` and auto-fill the profile |

**`DiscoverFilters`:** `role`, `location`, `remote` (default `true`), `keywords`,
`min_lpa`, `max_headcount`.
**`SendRequest`:** `application_ids: [int]`, `allow_unverified` (default `false`).
**`SettingsUpdate`:** `sending_paused`, `values: {key: str}`. Resuming
(`sending_paused=false`) starts a fresh `bounce_window_start` so prior bounces stop
blocking new sends.

`/api/send` and the `/api/forms/{fill,prefill}` routes run in background tasks; the UI
polls `/api/state` / `/api/forms` for progress (no websockets).

## 5. Stage logic

### 5.1 Discovery (`services/discovery.py`)
`discover()` is now LinkedIn-based (the CSE/ATS path is preserved as
`discover_ats()`, a disabled fallback — the project's CSE key 403s).
- Build queries: when the role box is blank, fan out `DEFAULT_ROLE_QUERIES =
  ["AI Agents Engineer", "LLM Engineer", "Backend Engineer"]`; when location is
  blank, fan out `DEFAULT_LOCATIONS = ["India", "United States", "Germany"]`.
- For each role × geo call `linkedin.search_jobs(role, location, remote, limit=PER_QUERY_LIMIT=15)`;
  merge and dedupe by URL.
- Filter out: internships (`_INTERN_MARKERS`), over-senior titles (`_SENIOR_MARKERS`),
  big-co/IT-services/staffing firms (`_BIG_OR_STAFFING` blocklist), salary below
  `min_lpa` (default `DEFAULT_MIN_LPA = 8`), and (after Hunter) companies whose
  headcount upper bound exceeds `max_headcount` (default `DEFAULT_MAX_HEADCOUNT = 500`).
- Per-run caps: `MAX_JOBS_PER_RUN=40`, `MAX_HUNTER_LOOKUPS_PER_RUN=12`,
  `PER_QUERY_LIMIT=15`.
- For each kept job, store the company/posting, then attach an HR email via
  `hunter.find_hr_emails` (marked verified when confidence ≥ `HR_VERIFY_THRESHOLD = 0.70`);
  also records Hunter `headcount`/`domain`.

`integrations/linkedin.py`: scrapes the unauthenticated `GUEST_ENDPOINT`
(see-more-jobs API) with BeautifulSoup; `f_WT=2` requests remote; `_parse_salary_lpa`
reads INR LPA (lower bound, ignores USD/EUR/GBP); `_parse_cards` extracts
title/company/location/url/salary. Backs off on 429/403. DRY_RUN returns a fixture.

### 5.2 Contact resolution (`services/contacts.py`)
Three passes:
- **Pass A — Hunter by name** (for LinkedIn rows with no domain): for each
  email-less application, `_hunter_resolve()` calls `hunter.find_hr_emails` by company
  name, updating the existing email-less contact in place (or creating one) and
  recording `headcount`/`domain`. Quota-bounded;
  `applications_needing_email` excludes rows whose `apply_kind` is set (forms etc.).
- **Pass B — page/site/pattern waterfall** (domain known), stop at first success:
  1. Parse posting page for `mailto:` / apply email / apply URL.
  2. Fetch `/careers`, `/contact`, `/about`; regex emails on company domain.
  3. Generate patterns: `first.last@`, `flast@`, `first@`, `careers@`, `jobs@`, `hr@`, `talent@`.
  Then verify the chosen email via Hunter; store `verified`, `confidence`. If only an
  apply URL exists it is stored and the posting is flagged "portal apply" (no email send).
- **Pass 2 — verify imported emails**: contacts that have an email but were never
  verified (`verified=0`, `confidence` NULL) are run through Hunter; idempotent.

`HR_VERIFY_THRESHOLD = 0.70`.

### 5.3 Drafting (`services/drafting.py`)
- Call Groq with the draft prompt (§7.1), passing role, company, one scraped detail, and a short CV summary (from a config string, not the full PDF).
- Enforce 90–130 words; reject+regenerate if identical to a prior body (hash check).

### 5.4 Sender (`services/sender.py`)
`send_approved(ids, allow_unverified=False)` enforces every anti-ban rule (full
algorithm in §6). `allow_unverified=True` (operator-checked in Review & Send) bypasses
only the verified-flag gate; the live Hunter mailbox check, duplicate guard, bounce
auto-pause and ramp cap still apply.

### 5.5 Replies (`services/replies.py`)
- `users.messages.list` with `q=newer_than:Nd -from:me` since `last_checked_at`.
- Match each message's `threadId` to an application.
- Send body to Groq classifier (§7.2); map to status.
- Update `status`, `reply_excerpt`, `last_checked_at`; apply Gmail label.
- Bounce detection: messages from `mailer-daemon` / `postmaster` matched to a thread → status `bounced`, logged for bounce-rate calc.

## 6. Anti-ban algorithm (the critical path)

Pseudocode for `send_approved(ids, allow_unverified=False)`:

```
# --- pre-flight gates ---
if is_paused(): return "sending paused (resume to continue)"
cap = ramp_cap_for_today(first_send_date(), today, DAILY_CAP)   # if WARMUP_RAMP
      else min(DAILY_CAP, RAMP_HARD_MAX)                          # else
if sent_today() >= cap: return "daily cap reached"
if account_age_days() is None or < MIN_ACCOUNT_AGE_DAYS: return "warm-up gate"

# --- per-email loop ---
for app, company, c in approved(ids):
    if sent_today() >= cap: break
    rate = rolling_bounce_rate(send_outcomes(since=bounce_window_start),
                               min_sample=MIN_BOUNCE_SAMPLE)       # 20
    if rate > BOUNCE_PAUSE_THRESHOLD:
        set_paused(True); notify("auto-paused: bounce rate high"); break

    if REQUIRE_VERIFIED and not allow_unverified and not c.verified:
        skip("unverified"); continue

    # PRE-SEND live verify for hunter-sourced emails (search confidence != deliverability)
    if REQUIRE_VERIFIED and not DRY_RUN and c.email
       and c.source.startswith("hunter") and not c.source.endswith("+v")
       and hunter.remaining_this_month() > 0:                      # only when we CAN verify
        v = hunter.verify(c.email)
        if v.verified:        c.source += "+v"; c.confidence = v.confidence   # stamp, send
        elif v.status == "invalid":  c.verified = 0; skip("undeliverable"); continue
        # else accept_all/unknown/quota/error: trust search verification, send (no downgrade)

    if c.apply_url and not c.email: skip("portal apply"); continue
    if not c.email: skip("no email"); continue
    if duplicate_within(company.id, DUP_WINDOW_DAYS): skip("dup"); continue
    if not DRY_RUN and first_send_guard(c.email): skip(guard); continue  # first send -> test inbox

    res = gmail.send(to=c.email, subject=app.subject, body=app.body,
                     attach=CV_PATH, cc=app.email_cc)
    record_send(app, res.thread_id, res.message_id); set_flag(FIRST_SEND_DONE)
    if not DRY_RUN: sleep(uniform(SEND_DELAY_MIN, SEND_DELAY_MAX))   # 90–180s
```

Hard rules enforced: pause gate; ramping daily cap (or `min(DAILY_CAP, RAMP_HARD_MAX)`
when `WARMUP_RAMP=false`); warm-up age gate; verified-only (unless `allow_unverified`);
pre-send live Hunter verify for hunter-sourced addresses; no duplicates; first real send
must hit `FIRST_SEND_TEST_INBOX`; randomized human-like spacing; auto-pause on bounce
(over a rolling window starting at `bounce_window_start`, only once `MIN_BOUNCE_SAMPLE=20`
messages have been sent); one email per company; CV PDF attached (never a link);
operator-approved only. `record_bounce` is idempotent (returns whether it recorded a new
bounce). Resuming from a pause resets the bounce window so fixed addresses can send again.

## 7. Prompt templates (`prompts.py`)

### 7.1 Draft prompt (system + user)
**System:**
> You write concise, specific job-application emails for an SDE-1/intern candidate in India. Output ONLY the email body, 90–130 words, plain text, professional but warm. No placeholders, no markdown, no links. Reference the exact role and one concrete detail about the company. End with the provided signature.

**User (filled at runtime):**
```
Role: {role_title}
Company: {company_name}
Company detail: {scraped_detail}
Candidate summary: {cv_summary}     # e.g. "Final-year CS, DSA + full-stack (React/Node), 2 internships, projects X/Y"
Signature: {signature}
Write the email body now.
```
Subject generated separately or templated: `Application — {role_title} — {SENDER_NAME}`.

### 7.2 Reply-classification prompt
**System:**
> You classify a single email reply to a job application. Respond with ONLY one token from: INTERVIEW, REJECTION, NEEDINFO, AUTO_ACK, OTHER. INTERVIEW = invites to call/test/next step. REJECTION = not moving forward. NEEDINFO = asks for documents/details. AUTO_ACK = automated "we received your application". OTHER = anything else.

**User:** the raw reply text (truncated to ~1,500 chars).
Map: INTERVIEW→replied_interview, REJECTION→replied_rejection, NEEDINFO→replied_needinfo, AUTO_ACK→auto_ack, OTHER→(keep `sent`, set reply_excerpt).

## 8. Gmail integration notes (`integrations/gmail.py`)
- Auth: `InstalledAppFlow` from `credentials.json` (Desktop client), token cached in `token.json`.
- Send: build RFC-822 MIME (`email.mime`), base64url-encode, `users.messages.send`.
- Read: `users.messages.list` + `get(format='full')`; extract plain text part.
- Label: ensure label exists (`labels.create` once), then `messages.modify` addLabelIds.

## 9. Groq integration notes (`integrations/groq_client.py`)
- Use OpenAI SDK pointed at `https://api.groq.com/openai/v1`, model `llama-3.3-70b-versatile`.
- `_api_keys()` returns the primary `GROQ_API_KEY` first and `GROQ_API_KEY_BACKUP`
  second (when set). `chat()` rotates to the backup key on a 429 rate-limit
  (`_is_rate_limit`) and retries immediately; `_active_key` persists across calls so we
  stay on the fresh key rather than re-hammering the throttled one.
- Respect ~25 rpm: small in-process token-bucket limiter so batch drafting doesn't 429.
- In DRY_RUN no network call is made (drafting/classification fall back to local logic).

## 10. Error handling & logging
- All integration calls: try/except + exponential backoff (3 tries).
- Structured local log file `autoapply.log`: stage, target, outcome, ts.
- UI surfaces last error per stage and remaining quotas.

## 11. Testing checklist
- Unit (63 tests, `pytest`): pattern generator, dedupe, ramp-cap-for-today and
  bounce-rate (both updated for the `MIN_BOUNCE_SAMPLE=20` rule); LinkedIn discovery
  (`test_linkedin_discovery.py` — salary/card parse, default role queries + locations,
  big-co blocklist, headcount cap, seniority filter, HR classification); referral
  parsing + form-filler field mapping (`test_referrals_formfiller.py`).
- Integration (manual): one real discovery query; one verify; one draft; one send to
  your `FIRST_SEND_TEST_INBOX`; one reply classified; one Google Form fill + submit.
- Safety: simulate ≥20 sends with bounces over threshold → confirm auto-pause fires.
