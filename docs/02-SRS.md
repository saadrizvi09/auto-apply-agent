# Software Requirements Specification (SRS / SRD)
### Project: AutoApply — Free Job-Application Emailer
**Version:** 1.1 · **Status:** Final · **Last updated:** 2026-06-18

---

## 1. Introduction

### 1.1 Purpose
Define the functional and non-functional requirements for AutoApply, a single-user,
locally-run job-application emailer.

### 1.2 Scope
The system discovers live job postings, resolves contacts, drafts and sends
personalized application emails with a CV attachment, and tracks reply status — using
only free service tiers. See 01-PRD.md for product context.

### 1.3 Definitions
- **Posting:** a single live job opening discovered from LinkedIn (guest jobs) or another source.
- **Contact:** an email address (or apply link) associated with a posting/company.
- **Application:** a record of one application (email, Google Form, or manual) for one posting.
- **ATS:** Applicant Tracking System (Lever, Greenhouse, Ashby, etc.) — used only by the legacy fallback discovery path.

## 2. Overall description

### 2.1 Product perspective
Standalone local web app: FastAPI backend + SQLite + browser frontend. Integrates with
the LinkedIn guest jobs endpoint and external HTTP APIs (Hunter, Groq, Gmail). Google
Forms referral applications open as pre-filled links in the operator's own Chrome.

### 2.2 User class
Single operator (the job seeker). No roles/permissions, no auth beyond Gmail OAuth.

### 2.3 Operating environment
Local machine, Python 3.11+, modern browser. Outbound HTTPS required.

### 2.4 Constraints
Free tiers only; no paid hosting; Gmail behavioral sending limits; LinkedIn guest
endpoint is IP-rate-limited; Hunter free tier is 50 lookups/month.

---

## 3. Functional requirements

### 3.1 Discovery
- **FR-1** The system shall accept search filters: role, location, free-text keywords, remote-only (bool), minimum salary (LPA), and maximum team size (headcount). When role or location is blank it shall default to roles {AI Agents Engineer, LLM Engineer, Backend Engineer} × geos {India, United States, Germany}.
- **FR-2** The system shall query the LinkedIn public **guest** jobs endpoint (`linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search`, no login) for each role × location, with the remote work-type filter (`f_WT=2`) when remote-only is set, and parse the returned HTML cards into postings (title, company, location, URL, remote flag, and salary when shown). The legacy Google Custom Search / ATS path is retained only as a disabled fallback (`discover_ats`).
- **FR-3** The system shall parse results into `companies` records: name, domain (resolved later via Hunter), posting URL, role title, location, salary, remote flag.
- **FR-4** The system shall deduplicate postings by posting URL within the batch and against the DB before storing.
- **FR-5** The system shall filter out: internships/trainee roles; over-senior titles (staff/principal/director/vp/head-of/manager/architect/lead/distinguished); a blocklist of big enterprises, IT-services/consultancies and staffing agencies; postings whose parsed salary (when shown) is below the minimum LPA (default 8); and, after a Hunter lookup, companies whose headcount upper bound exceeds the maximum team size (default 500).
- **FR-5a** The system shall enforce per-run safety caps so one Find Jobs click cannot exhaust quotas: ≤40 jobs stored/run, ≤12 Hunter lookups/run, ≤15 cards per role × location query. On a LinkedIn 429/403 it shall back off and return whatever was collected, reporting possible rate-limiting rather than crashing.

### 3.2 Contact resolution
- **FR-6 (Pass A — LinkedIn rows, no domain)** For postings discovered without a company domain (the LinkedIn case), the system shall resolve an HR/recruiter email via a Hunter **domain-search by company name** (`find_hr_emails`), preferring HR/recruiting positions then highest confidence, and update the existing email-less contact in place. Referral form/email jobs are excluded from this pass.
- **FR-7 (Pass B — domain-known rows)** For postings with a known company domain, the system shall run a waterfall, stopping at first success: (1) extract a `mailto:`/email or apply link from the posting page; (2) scrape the company site (`/careers`, `/contact`, `/about`, root) for published emails on the domain; (3) generate common email patterns (`careers@`, `jobs@`, `hr@`, `talent@`, plus `first.last@`/`flast@`/`first@` when a name is known) against the domain.
- **FR-8** If only an apply URL exists, the system shall store it and flag the posting "portal apply" (no email send).
- **FR-9** The system shall verify candidate emails via Hunter.io and store verification status and confidence. Pass A treats a domain-search confidence ≥ 0.70 as verified; Pass B and a separate Pass 2 (verify previously-imported emails) call Hunter's email-verifier. Hunter usage is bounded by the free tier (50/month, `HUNTER_MONTHLY_LIMIT`); on quota exhaustion remaining rows are reported, not crashed.
- **FR-10** The system shall mark a contact as `unverified` if verification fails or quota is exhausted, and **shall not auto-send** to unverified contacts unless the operator explicitly overrides (§3.4).

### 3.3 Drafting
- **FR-11** The system shall generate one tailored email per posting using Groq, referencing the specific role title and ≥1 company-specific detail.
- **FR-11a** The system shall route postings whose location is US/EU/international (`_is_international`) to a remote-from-India cost/quality value-proposition pitch (`DRAFT_SYSTEM_INTL`); India-located postings use the standard pitch. Referral email applications shall keep the referral digest's subject and append a coding-profile links footer (GitHub/LeetCode/etc.) to the body.
- **FR-12** Drafts shall be 90–130 words, plain text, with a configurable signature and no tracking pixels or shortened links.
- **FR-13** The system shall attach a configurable PDF CV (path in config) to each email.
- **FR-14** The system shall store drafts with status `drafted` and never send at draft time.

### 3.4 Review & Send
- **FR-15** The system shall present all drafts in a review table; the operator selects which to send.
- **FR-16** The system shall send only operator-approved emails.
- **FR-17 (Warm-up ramp)** The system shall enforce a ramping daily cap and refuse to exceed it: Day 1–2 → 15, Day 3–5 → 30, Day 6+ → `DAILY_CAP` (configurable), with a hard ceiling of `RAMP_HARD_MAX = 75`. The `WARMUP_RAMP` setting (default true) gates this; when false the operator gets the full `DAILY_CAP` (still clamped to the hard ceiling) from day 1.
- **FR-17a (Warm-up gate)** The system shall block all sending until the sender account age reaches `MIN_ACCOUNT_AGE_DAYS` (default 14), and the first real send must target `FIRST_SEND_TEST_INBOX` (an inbox the operator controls) before any external send is allowed.
- **FR-18** The system shall insert a randomized delay (configurable, default 90–180 s) between sends.
- **FR-19** The system shall record `gmail_thread_id`, subject, body, and `sent_at` for each sent email, attach the CV (and a `Cc` when present), and set status `sent`.
- **FR-19a (Pre-send verification)** Before sending, the system shall block unverified recipients **unless** the operator explicitly checked them in Review & Send (`allow_unverified`, confirmed). For Hunter-sourced emails it shall additionally run a real Hunter mailbox verification pre-send: skip only on status `invalid` (no bounce); on quota/error/unknown it shall trust the prior verification and send, never downgrading a verified contact.
- **FR-20** The system shall detect bounces and set status `bounced`; bounce counting is idempotent (one row per application). The rolling bounce rate is computed over `send_outcomes` since `bounce_window_start` and is only enforced once at least `MIN_BOUNCE_SAMPLE = 20` messages have been sent in the window; if it then exceeds the threshold (default 3%), the system shall auto-pause sending and notify the operator. Resuming opens a fresh bounce window.
- **FR-21** The system shall prevent duplicate sends to the same company within a configurable window (`DUP_WINDOW_DAYS`, default 5 days).

### 3.5 Reply scanning & status
- **FR-22** The system shall read new inbound mail since the last check and match messages to applications via `gmail_thread_id`.
- **FR-23** The system shall classify each human reply via Groq into: `replied_interview`, `replied_rejection`, `replied_needinfo`, `auto_ack`, and update status accordingly.
- **FR-24** The system shall apply a Gmail label (configurable, e.g. `AutoApply`) to processed threads.
- **FR-25** The system shall run reply scanning automatically on a schedule (default hourly) and on demand via the UI.
- **FR-26** The system shall update `last_checked_at` and store a short `reply_excerpt` per application.

### 3.6 Dashboard
- **FR-27** The system shall display a table: Company | Role | Status | Last checked | Reply snippet.
- **FR-28** The system shall expose action buttons mapping to §3.1–3.5.
- **FR-28a (Find-jobs filters)** The discovery panel shall offer inputs for Role, Location, Keywords, Min LPA, Max team size, and Remote-only, and after a run display a per-filter results breakdown (new, with HR email, plus counts skipped as big-co / too-large / too-senior / dup / intern / below-salary, and Hunter quota left).
- **FR-28b (Paste & apply)** The dashboard shall provide a paste-&-apply box for adding referral/manual application rows directly.
- **FR-28c (Résumé autofill)** The dashboard shall offer a read-résumé action that autofills the operator profile from the configured CV.
- **FR-28d (Form cards)** Google-Forms referral applications shall appear as cards with actions: open the pre-filled form in the operator's browser, attempt auto-submit, mark submitted, and archive (or permanently delete).
- **FR-29** The system shall display remaining free-tier quotas (Hunter, daily send budget).
- **FR-30** The system shall display the supporting-channel banner (PRD §11).

---

## 4. Non-functional requirements

- **NFR-1 (Cost):** All runtime services shall be free-tier; the system shall never require payment.
- **NFR-2 (Deliverability/Safety):** Sending behavior shall comply with all anti-ban rules in 04-Technical-Spec §Anti-ban. Account protection takes precedence over throughput.
- **NFR-3 (Security):** Secrets (`.env`, `credentials.json`, `token.json`) shall be local-only and git-ignored; the system shall not be deployed to a public host.
- **NFR-4 (Reliability):** External API failures shall be retried with exponential backoff; a failed stage shall not corrupt stored state (transactional writes).
- **NFR-5 (Usability):** The operator shall complete the full pipeline using only the five buttons; no command line needed after setup.
- **NFR-6 (Observability):** Every send and classification shall be logged locally with timestamp, target, and outcome.
- **NFR-7 (Performance):** Discovery/scrape of a batch of ≤50 postings shall complete within a few minutes; long jobs shall report progress.
- **NFR-8 (Portability):** Runs on Windows, macOS, Linux with Python 3.11+.
- **NFR-9 (Data):** All data stored locally in SQLite; no third-party data store.
- **NFR-10 (Compliance):** Emails shall be individually relevant job applications (not bulk marketing); the operator is the data controller for any contacts stored.

## 5. External interface requirements

| Interface | Provider | Use | Free limit |
|---|---|---|---|
| Guest jobs endpoint (`seeMoreJobPostings/search`) | LinkedIn | Discovery (no login) | IP rate-limited (429/403 → back off) |
| Domain-search + Email-verifier APIs | Hunter.io | Resolve HR emails (by company name) + verify recipients | 50 lookups/month |
| Chat Completions (OpenAI-compatible) | Groq | Draft + classify | ~30 rpm / ~1,000 rpd; `GROQ_API_KEY` + optional `GROQ_API_KEY_BACKUP` (auto-failover on 429) |
| Gmail API | Google | Send (with CV + Cc) + read mail | 500 sends/day ceiling (reputation-gated) |
| Custom Search JSON API | Google | Legacy/fallback discovery only (disabled) | 100 queries/day |

## 6. Data requirements
See 04-Technical-Spec §Data model. Entities: `companies`, `contacts`, `applications`,
plus a `settings` store and a `send_log`. Self-healing migrations in `db.init_db` add
missing columns on startup. Current additions beyond the original schema:
- **companies:** `salary`, `headcount` (Hunter employee-range startup signal).
- **applications:** `apply_kind` (`email` | `form` | `manual`), `form_url`, `form_answers`,
  `form_screenshot`, `form_note`, `form_prefill_url`, `email_cc`, `archived`.
- **statuses:** add `form_found`, `form_filled`, `form_submitted` to the application lifecycle.

## 7. Acceptance criteria (v1 done when)
- All functional requirements (FR-1..FR-30 and their sub-requirements) implemented and demonstrable through the UI.
- A test run of 5 real applications sends, attaches the CV, and tracks at least one reply classification correctly.
- Bounce auto-pause triggers correctly in a simulated >3% bounce scenario (once the minimum sample of 20 sends is met).
- Zero paid services used.
