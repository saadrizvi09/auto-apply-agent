# Build & Operations Runbook
### Project: AutoApply — Free Job-Application Emailer
**Version:** 1.0 · **Status:** Final · **Last updated:** 2026-06-09

---

## 1. One-time setup

### 1.1 Sender account
- Use your **aged** Gmail (better reputation than a new account).
- Confirm it's in good standing (no recent suspensions). Set `MIN_ACCOUNT_AGE_DAYS` satisfied.

### 1.2 Google Cloud (Gmail) — required
1. console.cloud.google.com → new project (e.g. `autoapply`).
2. Enable the **Gmail API**.
3. OAuth consent screen → **External** → **Testing** → add your Gmail as a **test user**.
4. Credentials → **OAuth client ID** → **Desktop app** → download as `credentials.json` into the project root.

### 1.3 Custom Search + Programmable Search Engine — OPTIONAL (legacy)
> Primary discovery is **LinkedIn guest-jobs scraping** (`app/integrations/linkedin.py`),
> which needs **no API key** — you can run Find Jobs with these blank. Custom Search is
> only a legacy ATS fallback (`discover_ats`) and is disabled in practice (the project key
> 403s). Set up only if you want the fallback:
> 1. In the Cloud project, also enable the **Custom Search API**.
> 2. Credentials → **API key** → copy → `GOOGLE_CSE_API_KEY`.
> 3. programmablesearchengine.google.com → create engine; add sites
>    `lever.co`, `greenhouse.io`, `ashbyhq.com`, `wellfound.com`, `instahyre.com`.
> 4. Copy the **Search engine ID** → `GOOGLE_CSE_ID`.

### 1.4 Groq
- console.groq.com → create API key (no card) → `GROQ_API_KEY` (required for drafting +
  form answers).
- Optional: create a 2nd key → `GROQ_API_KEY_BACKUP`; it's used automatically on a 429
  rate-limit from the primary.

### 1.5 Hunter
- hunter.io → sign up → API key → `HUNTER_API_KEY` (**50 lookups/month free**).
- Hunter supplies HR emails (domain-search by company name) and pre-send mailbox
  verification, so this 50/month cap is the **main throughput limit** — when it's
  exhausted, new email discovery + pre-send verification pause until the monthly reset.

### 1.6 Files
- Put your CV at `./cv.pdf` (keep it < 1 MB).
- Create `.env` (see 04-Technical-Spec §3) and fill keys.
- Ensure `.gitignore` excludes `.env`, `credentials.json`, `token.json`, `jobs.db`,
  `cv.pdf`, `*.pdf`, `jobs.csv`, `profile.json`, `*.log`, `.browser_profile/`,
  `form_shots/`.

## 2. Install & run

Requires **Python 3.11** (the default `python` may be 3.13 — use `py -3.11`).

```bash
py -3.11 -m venv .venv && source .venv/Scripts/activate   # POSIX: .venv/bin/activate
pip install -r requirements.txt
# first run triggers Gmail OAuth in browser, writes token.json
uvicorn app.main:app --host 127.0.0.1 --port 8000
# open http://127.0.0.1:8000
```

`requirements.txt` is pinned: `fastapi uvicorn[standard] apscheduler sqlmodel requests
beautifulsoup4 openai google-auth google-auth-oauthlib google-api-python-client
python-dotenv python-multipart openpyxl playwright pypdf` (+ `pytest`). Run the test
suite with `pytest` (63 unit tests). Everything is local-only (binds `127.0.0.1`).

## 3. First-run test (do this before any real volume)
1. ① Find Jobs with narrow filters → expect a handful of postings.
2. ② Find Contacts → expect verified emails or apply links.
3. ③ Draft Emails → read 2–3 drafts; confirm they reference the real role.
4. ④ Review & Send → approve **one**, addressed to a secondary inbox you own; confirm CV attached.
5. ⑤ Scan Replies → reply to it from the secondary inbox; confirm it's classified + labeled.

## 4. Operating guidance
- **Warm-up ramp** (on by default): Day 1–2 → **15**/day, Day 3–5 → **30**/day,
  Day 6+ → `DAILY_CAP`. `DAILY_CAP` defaults to 20 in code but the shipped `.env` may set
  50; the absolute ceiling is `RAMP_HARD_MAX=75` (clamped). Don't override the cap upward
  early. Setting `WARMUP_RAMP=false` skips the ramp (full `DAILY_CAP` from day 1) — riskier
  for a fresh sender; leave it on unless the inbox is already established.
- Sends are spaced a random **90–180s** apart (`SEND_DELAY_MIN`/`SEND_DELAY_MAX`).
- **Verify-before-send** is on (`REQUIRE_VERIFIED=true`) — recipients are mailbox-verified
  via Hunter just before sending. The operator can override by explicitly checking
  unverified recipients in **Review & Send**. Bounces are the #1 cause of blocks.
- **Hunter is the throughput limit** (50 lookups/month). When exhausted, email discovery
  and pre-send verification pause until the monthly reset — watch the "Hunter left this
  month" figure in the Find Jobs summary.
- **Bounce auto-pause**: if the bounce rate crosses `BOUNCE_PAUSE_THRESHOLD` (0.03) over a
  minimum 20-send sample, sending auto-pauses. Lower volume, re-verify your list, then
  click **Resume** to continue with a fresh bounce window.
- **First real send guard**: the first real send must go to `FIRST_SEND_TEST_INBOX` (a
  secondary inbox you own); real sending is refused until the recipient matches it.
- **Account-age warm-up gate**: sending is gated until the sender account is at least
  `MIN_ACCOUNT_AGE_DAYS` old (default 14) — set `ACCOUNT_CREATED_DATE` to confirm age.
- `DRY_RUN` defaults **on** — nothing is actually sent and no paid quota is used until you
  flip it to `false`. Run your normal personal email from the account too — mixed real
  traffic helps reputation.
- Leave the app running for hourly reply scans, or click ⑤ when you check in.

## 5. The brief to paste into Claude Code

> Build AutoApply exactly per these specs (01-PRD.md, 02-SRS.md, 03-Architecture.md,
> 04-Technical-Spec.md): a locally-run, single-user job-application emailer.
> Python FastAPI backend + SQLite + a static HTML/JS dashboard with five buttons
> (Find Jobs, Find Contacts, Draft Emails, Review & Send, Scan Replies). Discover jobs by
> scraping LinkedIn's public **guest** jobs endpoint (no login, no API key) with
> requests+BeautifulSoup — default roles AI Agents/LLM/Backend Engineer, geos
> India/US/Germany (remote-only), filtering out interns, over-senior titles,
> big-company/staffing firms, sub-min-LPA salaries, and over-headcount companies; keep
> Google Custom Search (ATS-domain restricted) only as an optional legacy fallback.
> Use Hunter.io for HR emails + recipient verification, Groq (OpenAI-compatible,
> llama-3.3-70b-versatile) for drafting + reply classification, and the Gmail API
> (OAuth: send, readonly, modify) for sending and reading replies. Attach a PDF CV.
> Implement the data model and endpoints in 04-Technical-Spec, and EVERY anti-ban rule
> in §6 (ramping daily cap with warm-up, warm-up gate, randomized 90–180s delays,
> verify-before-send, duplicate guard, bounce-rate auto-pause, operator approval before
> any send). Use
> APScheduler for hourly reply scans. Read secrets from .env and Gmail creds from
> credentials.json/token.json; bind to 127.0.0.1 only. Scaffold the project + SQLite
> schema first, then implement one button end-to-end at a time so I can test each
> before moving on. Provide a README with run commands.

## 6. Free-tier cheat sheet

| Service | Free limit | Role | If exceeded |
|---|---|---|---|
| LinkedIn guest jobs | none (no key/login) | **primary discovery** | IP rate-limited; back off, narrow role/geo, retry |
| Gmail (aged acct) | 500/day ceiling; **~40–50/day safe** | send + read | lower cap; never multi-account |
| Hunter | **50/month** | HR emails + verify | **main bottleneck** — pauses at limit; runs cap Hunter lookups/run |
| Groq | ~30 rpm / ~1,000 rpd | draft + classify | set `GROQ_API_KEY_BACKUP` (auto-used on 429); batch overnight |
| Custom Search (optional) | 100 queries/day | legacy fallback only | not needed — LinkedIn is primary |

## 7. Reality check (keep visible)
This is a **supporting channel**. For fresher SDE-1 at ₹8.4 LPA+, referrals and
LinkedIn out-perform cold email substantially. Run AutoApply in parallel with those,
not instead of them. No free setup safely sends hundreds/day from one inbox — volume
is bounded by deliverability, by design.

## 8. Google-Forms auto-apply setup (referral digests)
Optional module for applying via the Google Forms in "Referral Alert" emails.

1. **Browser engine** — `pip install -r requirements.txt` (pulls Playwright 1.49.1),
   then `py -3.11 -m playwright install chromium` (~150 MB Chromium download).
2. **One-time Google login** — `py -3.11 formtool.py login`; sign into the apply
   account and close the window. This uses your **real installed Chrome**
   (`channel="chrome"`), because Google blocks the bundled Chromium at sign-in. Session
   persists in `.browser_profile/` (git-ignored); `py -3.11 formtool.py check` confirms
   you're still logged in.
3. **Applicant profile** — `copy profile.example.json profile.json` and fill it
   (name, email, phone, college, grad year, **public CV link**, LinkedIn, GitHub,
   LeetCode, expected stipend…). These are the answers typed into forms. Git-ignored.
4. **Validate one form** — `py -3.11 formtool.py read "<form-url>"` prints the
   questions + planned answers (read-only); `... fill "<form-url>"` fills + screenshots
   to `form_shots/` without submitting. (`formtool.py` commands: `login`, `check`,
   `read`, `fill`, `apply`, `fillall`.)
5. **Use it** — dashboard → *"Referral inbox & auto-apply forms"*: paste the digest →
   **Prepare pre-filled links** → for each card, open its pre-filled link
   (`viewform?usp=pp_url&entry.X=...`) in your logged-in Chrome, **attach the CV
   manually** and **Submit** (Google blocks programmatic file upload + submit), then
   **Mark as submitted**. Under `DRY_RUN=true` the submit is simulated. Form cards can be
   **archived** (kept in Applications history) or **permanently deleted**.

Limits: file-upload (CV) questions and any missing `profile.json` field are flagged for
you to complete on the form, never auto-faked. Building pre-filled links uses no paid
quota; only Groq (open-question answers, real mode) has a real effect.
