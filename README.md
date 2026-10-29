# HireMeQuackers — Local-First AI Job-Hunting Agent

HireMeQuackers is a local command-line application that discovers job and internship listings from multiple public sources, evaluates how well you fit each position using Google Gemini (optional), tailors your CV, generates cover letters, and tracks your application pipeline — all in a simple SQLite database on your machine.

Everything runs from the `quackers` command in your terminal — no web server, nothing listening on the network.

---

## New in v2 — Max-Coverage Discovery Engine

The v2 max-coverage discovery engine (October 2026) replaces the original single-search-provider pipeline with a multi-source, query-planner-driven scanner. Highlights:

- **13 job sources** (up from 5). In addition to Greenhouse, Lever, RSS feeds, Web Search, and Career Pages, v2 adds **RemoteOK** (keyless JSON API), **Hacker News** (Algolia job listings), and **EURES** (opt-in EU public-sector endpoint).
- **Hybrid search providers.** When `BRAVE_SEARCH_API_KEY` is set, the web-search source uses the **Brave Search API** with pagination. Without a key it automatically falls back to **DuckDuckGo** — the app runs with zero paid keys.
- **Query planner.** Instead of a single hand-typed query, a query planner builds up to `SCAN_MAX_TOTAL_QUERIES` complementary queries per scan: exact role titles + synonyms, skills x locations, German-language variants when European locations are set (`Praktikum`, `Werkstudent`, `Ausbildung`), ATS `site:` queries, informal hiring phrasing, and recent-posted variants.
- **Detail-page extraction.** Web and career-page results no longer come in as title + URL only. A detail extractor (JSON-LD `JobPosting` first, Open Graph tags second, HTML heuristics third) recovers description, salary, employment type, posted/closing dates, and application URL, and records an `extraction_confidence` so the CLI can tell you how reliable each field is.
- **Enrichment pass.** Newly stored jobs are automatically re-fetched (rate-limited, batched up to `ENRICHMENT_MAX_PER_SCAN`) to fill missing description text, so AI matching has real content to work on.
- **Dynamic source registry + health tracking.** Source connectivity (listings found, last seen, freshness, per-source failure counts) is persisted and shown by `quackers sources list`.
- **Scan diagnostics panel.** After each scan, `quackers scan` prints a per-source breakdown: `Success` / `Partial` / `Failed` / `Skipped` status, attempted, fetched, parsed, duplicate, and failed counts, plus a human-readable error message.
- **Per-source error isolation.** One failing source never discards results from the other sources.
- **SQLite migrations at startup** — `data/jobs.db` is upgraded additively and idempotently on every launch. No manual schema work required.

---

## Table of Contents

1. [New in v2 — Max-Coverage Discovery Engine](#new-in-v2-max-coverage-discovery-engine)
2. [Prerequisites](#prerequisites)
3. [Installation](#installation)
4. [Configuration](#configuration)
5. [Running the Application](#running-the-application)
6. [How a Scan Works](#how-a-scan-works)
7. [Command Reference](#command-reference)
8. [Uploading and Reviewing Your CV](#uploading-and-reviewing-your-cv)
9. [Configuring Search Settings](#configuring-search-settings)
10. [Triggering a Scan](#triggering-a-scan)
11. [Reviewing Match Scores](#reviewing-match-scores)
12. [Generating CVs and Cover Letters](#generating-cvs-and-cover-letters)
13. [Application Tracker](#application-tracker)
14. [Running Tests](#running-tests)
15. [Backup and Restore](#backup-and-restore)
16. [Known Limitations](#known-limitations)
17. [Safety](#safety)
18. [Adding a New Job-Source Adapter](#adding-a-new-source-adapter)
19. [Troubleshooting](#troubleshooting)
20. [Project Structure](#project-structure)

---

## Prerequisites

| Requirement | Minimum Version | Notes |
|---|---|---|
| Python | 3.14 | See install notes below if Python opens the Microsoft Store |
| pip | 23.0+ | Usually ships with Python |
| OS | Windows 10/11, macOS 12+, Linux | Windows PowerShell users: see below |

### API keys — all optional

| Key | Required? | What it enables |
|---|---|---|
| `GEMINI_API_KEY` | Optional | AI match scoring, tailored CV, and cover letter generation. Without it, deterministic scoring is used instead. |
| `BRAVE_SEARCH_API_KEY` | Optional | Improves web-search recall. Without it, the app falls back to DuckDuckGo automatically. |
| `EURES_API_URL` | Optional | Enables the EURES EU public-sector source. Without it, the source shows as `Skipped`. |

The app works fully with no keys at all.

**Windows PowerShell** — if `python` opens the Microsoft Store instead, run:
```powershell
py --version
```
and use `py` instead of `python` throughout the instructions.

**macOS / Linux** — confirm with:
```bash
python3 --version
```

---

## Installation

### Windows PowerShell

```powershell
# 1. Navigate to the project folder
cd C:\Users\<You>\Desktop\HireMeQuackers

# 2. Create a virtual environment (use python 3.14)
py -m venv .venv

# 3. Activate it
.\.venv\Scripts\Activate.ps1

# 4. Install dependencies
pip install -r requirements.txt
pip install -e .            # installs the `quackers` command

# 5. Optional: install the Gemini SDK (for AI matching)
pip install google-genai

# 6. Optional: install PDF extraction
pip install PyMuPDF

# 7. Create your .env (see Configuration below)
```

### macOS / Linux

```bash
# 1. Navigate to the project folder
cd HireMeQuackers

# 2. Create a virtual environment
python3 -m venv .venv

# 3. Activate it
source .venv/bin/activate

# 4. Install dependencies
pip install -r requirements.txt
pip install -e .            # installs the `quackers` command

# 5. Optional: install the Gemini SDK (for AI matching)
pip install google-genai

# 6. Optional: install PDF extraction
pip install PyMuPDF
```

> **Note:** The `google-genai` package is optional. Without it, HireMeQuackers runs in
> "deterministic-fallback" mode: scans work and jobs are stored, but AI match
> scores are not generated. All other features (job discovery, CV tailoring
> structure, cover letters, application tracking) still work fully.

---

## Configuration

After installing, create a `.env` file in the project root. (`.env` is git-ignored — there is no `.env.example` template in the repo, so create it by hand.) A minimal working file:

```bash
# .env — all keys optional; app runs without any of these
GEMINI_API_KEY=
GEMINI_MODEL=gemini-2.0-flash
DATABASE_URL=sqlite:///./data/jobs.db
BRAVE_SEARCH_API_KEY=
EURES_API_URL=
APP_ENV=local
LOG_LEVEL=INFO
```
Then open `.env` in a text editor and fill in your values:

| Variable | Default | Description |
|---|---|---|
| `GEMINI_API_KEY` | *(empty)* | Your Gemini API key (get one at [aistudio.google.com/apikey](https://aistudio.google.com/apikey)). Optional — app runs without it. |
| `GEMINI_MODEL` | `gemini-2.0-flash` | Model name to use for AI matching |
| `BRAVE_SEARCH_API_KEY` | *(empty)* | Brave Search API key for better web-search recall. Optional — DuckDuckGo fallback is automatic. |
| `EURES_API_URL` | *(empty)* | Endpoint to an EURES API. Optional — the EURES source is skipped (not failed) when unset. |
| `DATABASE_URL` | `sqlite:///./data/jobs.db` | SQLite database path |
| `SCAN_MAX_SOURCES` | `13` | How many sources to scan per run (currently 13 registered) |
| `SCAN_BUDGET_SECONDS` | `300` | Hard time budget per scan |
| `SCAN_MAX_TOTAL_QUERIES` | `40` | Cap on how many distinct queries the query planner generates per scan |
| `SCAN_REQUEST_CAP_PER_SOURCE` | `60` | Max HTTP requests a single source can make in one scan |
| `ENRICHMENT_MAX_PER_SCAN` | `25` | How many new jobs get detail-page enrichment per scan |
| `ENRICHMENT_DELAY_SECONDS` | `1.5` | Rate-limit between enrichment HTTP calls |
| `REQUEST_TIMEOUT_SECONDS` | `15` | HTTP timeout for source requests |
| `MAX_RETRY_ATTEMPTS` | `3` | Retry count per source |
| `RATE_LIMIT_MIN_SECONDS` | `1.0` | Minimum pause between source requests |
| `MAX_RESPONSE_BYTES` | `3145728` | Response size cap (3 MB) |
| `ENABLE_AI_MATCHING` | `true` | Toggle Gemini evaluation on/off |
| `ENABLE_PDF_EXPORT` | `true` | Enable PDF export of generated docs |
| `APP_ENV` | `local` | Environment label |
| `LOG_LEVEL` | `INFO` | Logging verbosity |

> **Gemini rate limit:** The Gemini free tier allows ~10 requests/hour and ~1,000/day.
> `GEMINI_API_KEY` is an optional convenience: when calls hit a quota limit or the key
> is missing, AI matching falls back silently to a deterministic (non-AI) score rather than raising.
> Discovery itself is unaffected either way.

The `data/` directory is created automatically on first launch and holds the
SQLite database. The `output/` directory stores generated CVs and letters.

---

## Running the Application

### Interactive session

Run `quackers` with no arguments to open an interactive session, similar to
Claude Code:

```text
╭──────────────────────────────────────────────╮
│ 🦆 Welcome to HireMeQuackers!                │
│   /help for commands · or just type          │
│   profile: Ada Lovelace (v1)                 │
│   jobs: 99 · last scan: 2026-10-29 09:26     │
│   AI: on (gemini-2.0-flash)                  │
╰──────────────────────────────────────────────╯

> find remote data internships and show the best 5
● scan_for_jobs(no_ai=False)
● list_jobs(sort_by_score=True, limit=5)
  ...
```

- Lines starting with `/` run commands: `/scan`, `/jobs --new`, `/job 12`,
  `/app set 12 shortlisted`, ... `Tab` completes command names, `↑` recalls history.
- Anything else is sent to Gemini, which calls the same commands as tools and
  answers in your language. This needs `GEMINI_API_KEY` in `.env`
  (set `GEMINI_MODEL` to change the model); without it, `/commands` still work.
- `/help` lists everything, `/clear` starts a new conversation, `/exit` or
  `Ctrl+C` twice quits.
- For safety the AI cannot reset data, change profiles, open URLs or export
  files, and marking a job as **applied** always asks you to confirm.

### Single commands

> Make sure your virtual environment is **activated** and you ran `pip install -e .`
> (this installs the `quackers` command).

```bash
quackers --help            # list every command
quackers status            # overview: counts, last scan, recent jobs, follow-ups
```

If the `quackers` command is not found, `python -m quackers ...` works the same way.

Load your CV, then scan:

```bash
quackers cv path/to/cv.pdf        # .pdf, .docx or .json
quackers cv                        # show the active CV
quackers scan                      # uses your CV + saved search settings
```

No CV? `quackers scan` answers "No CV" unless you say what to look for:

```bash
quackers scan --titles "Data Analyst, BI Intern" --type "remote, hybrid" --location "Berlin, Munich"
quackers scan --titles "Backend Developer" --type remote      # remote-only: no location needed
```

`--type` is any of `remote`, `hybrid`, `onsite` (comma-separated). `--titles`,
`--type` and `--location` apply to that scan only (also usable with a CV).
A JSON CV uses the same fields as `quackers profile edit`:
`{"name": "Ada", "email": "ada@example.com", "skills": ["Python", "SQL"],
"experience": ["Data intern at X (2025)"], "education": ["BSc Mathematics"]}`.

A typical first session:

```bash
quackers profile import ~/Documents/my_cv.pdf --edit      # 1. create your profile
quackers config set primary_title="Data Analyst" employment_types=internship,working_student
quackers config set locations="Berlin, Munich" include_remote=true   # 2. what to search for
quackers scan                                              # 3. discover jobs
quackers jobs --new --sort score --why                     # 4. review the results
quackers job 42                                            # 5. read one job in full
quackers docs cover-letter 42 --show                       # 6. draft a cover letter
quackers app set 42 shortlisted                            # 7. track it
```

Every command prints help with `-h`, e.g. `quackers jobs -h`.

---

## How a Scan Works

1. **Query plan.** The query planner builds up to `SCAN_MAX_TOTAL_QUERIES` queries per run, using the role titles, preferred skills, locations, and language hints in your search settings.
2. **Worker pool.** Sources run in a bounded thread pool (5 workers, configurable via `SCAN_MAX_SOURCES`).
3. **Normalisation + de-dup.** Listings are normalised and de-duplicated using strong keys (canonicalised URL, provider + job-ID). Loose title-similarity is used only to *flag* a potential duplicate — it does not discard any record.
4. **Enrichment.** Newly stored web and career-page jobs are re-fetched (rate-limited, up to `ENRICHMENT_MAX_PER_SCAN` per scan, `ENRICHMENT_DELAY_SECONDS` between calls) to recover description, salary, employment type, and apply URL when the initial listing had none.
5. **Matching.** Results are scored against your active CV profile using Gemini when available, or via the deterministic keyword/skill/education fallback when not.
6. **Storage.** Records are written to SQLite immediately so a network hang mid-scan cannot lose data.

> **Diagnosis after a scan:** `quackers scan` ends with a per-source table:
> `Success` / `Partial` / `Failed` / `Skipped`, with attempted, fetched, parsed,
> duplicate, and failed counts, plus the error message when a source reports an issue.
> Past scans are available with `quackers history list` / `quackers history show <id>`.

---

## Command Reference

| Command | What it does |
|---|---|
| `quackers status [--ai-check]` | Totals (jobs, new, shortlisted, prepared, applied, interviews), last scan, recent jobs, upcoming follow-ups. |
| `quackers ai-check` | Shows whether the Gemini SDK and API key are configured. |
| `quackers scan [--no-ai] [--source NAME]` | Runs a scan with the live search config. `--no-ai` skips Gemini scoring; `--source` limits the scan to one or more sources. |
| `quackers jobs [--new] [--min-score N] [--search TEXT] [--source S] [--sort score] [--why] [--limit N]` | Lists discovered jobs. `--why` explains why each job was included (title / location / source evidence). |
| `quackers job ID` | Full details: facts, match breakdown, requirements, application state, documents, description. |
| `quackers open ID [--apply]` | Opens the posting (or the application URL) in your browser. |
| `quackers profile list / show / import / edit / activate / deactivate` | Candidate profiles. `import` parses a PDF/DOCX CV; `edit` opens the profile as JSON in your editor. |
| `quackers config show / set / edit / save-as / load / profiles / reset` | Search settings. `set` takes `KEY=VALUE` pairs; `edit` opens the whole config as JSON. |
| `quackers app list / set / note / follow-up / interview / followups` | Application tracker. |
| `quackers docs cv / cover-letter / list / show / export / approve` | Generated CV suggestions and cover letters. |
| `quackers sources list / enable / disable` | Per-source health and on/off switches. |
| `quackers history list / show / rerun` | Past scans; `rerun` repeats a scan with that scan's config. |
| `quackers reset [--scope current or all]` | Deletes job-search data after showing exactly what will be removed. |

Commands that change or delete data ask for confirmation; pass `-y` / `--yes`
(or `--confirm` for `app set ... applied`) in scripts.

---

## Uploading and Reviewing Your CV

```bash
quackers profile import path/to/cv.pdf --edit
```

1. A copy of the CV is stored in `data/cv/` (the original is never modified).
2. HireMeQuackers extracts the text and splits it into sections (name, email,
   phone, summary, skills, education, experience, certifications,
   achievements, projects, languages) and prints its extraction confidence.
3. With `--edit`, the extracted fields open as JSON in your editor
   (`$EDITOR`, or Notepad on Windows / nano elsewhere). Fix anything that was
   mis-parsed, save, and close the editor.
4. Confirm to save. The new profile becomes the active one (use `--inactive`
   to keep the current one active).
5. Later changes: `quackers profile edit`. Each save with content changes
   increments the `profile_version` and invalidates cached AI evaluations tied
   to the old version.
6. Run `quackers ai-check` to confirm Gemini is configured.

> No CV content is uploaded to any external service except the minimum data
> required for a Gemini API call when AI matching is enabled.

---

## Configuring Search Settings

Show the live config:

```bash
quackers config show            # readable list
quackers config show --json     # exact JSON
```

Change individual values with `KEY=VALUE` pairs (lists are comma-separated,
`none` clears a value):

```bash
quackers config set employment_types=internship,working_student
quackers config set locations="Berlin, Hamburg" excluded_locations=London
quackers config set include_remote=true posted_within_days=14 salary_min=none
quackers config set enabled_sources=greenhouse,lever,remoteok
```

Or edit everything at once in your editor (validated before saving):

```bash
quackers config edit
```

Useful keys: `primary_title`, `title_synonyms`, `excluded_titles`,
`employment_types` (internship, working_student, part_time, full_time,
graduate, temporary, contract), `include_keywords`, `exclude_keywords`,
`required_skills`, `preferred_skills`, `locations`, `excluded_locations`,
`include_remote` / `include_hybrid` / `include_onsite` / `worldwide_remote`,
`require_verified_location`, `posted_within_days`, `salary_min`, `salary_max`,
`max_results`, `min_match_score`, `enabled_sources`.

Named profiles:

```bash
quackers config save-as "Berlin internship"
quackers config profiles
quackers config load "Berlin internship"
```

---

## Triggering a Scan

Scans are **explicit** — they only run when you run the command. There is no
built-in daily scheduler; to scan periodically, schedule `quackers scan` with
cron or Windows Task Scheduler.

```bash
quackers scan              # with Gemini scoring when configured
quackers scan --no-ai      # deterministic filters only (no API quota)
quackers scan --source lever --source greenhouse
```

Progress is printed per source. Press `Ctrl+C` to stop; anything already
stored stays in the database. The result shows `done` / `partial` / `error`,
discovered / new / duplicate counts, filtered-out jobs, enrichment counts and
the per-source table.

**Scan behavior:**
- Sources are scanned in a bounded pool (`SCAN_MAX_SOURCES`).
- Each source gets a conservative rate limit (`RATE_LIMIT_MIN_SECONDS`).
- Rate-limiting and bounded retries are built in (`MAX_RETRY_ATTEMPTS`).
- Failures are honest: a source only shows "success" if it actually returned data.
- Duplicate detection runs on content fingerprint + secondary similarity check
  across all sources.

---

## Reviewing Match Scores

Match scores (0–100) are only generated when:
- `ENABLE_AI_MATCHING=true` in `.env`, **and**
- `GEMINI_API_KEY` is a real key, **and**
- the scan ran without `--no-ai`.

Without them, the deterministic fallback score is shown instead.

```bash
quackers jobs --sort score --min-score 60    # best matches first
quackers job 42                              # full breakdown for one job
```

`quackers job` shows the per-area breakdown, evidence from your profile, and
missing or weak qualifications.

> Scores are **not** a guarantee of being hired. They reflect how closely your
> profile matches the stated job requirements.

---

## Generating CVs and Cover Letters

```bash
quackers docs cv 42 --show                         # CV tailoring suggestions
quackers docs cover-letter 42 --tone confident     # professional | casual | confident
quackers docs list                                 # everything generated so far
quackers docs show 7                               # print a document
quackers docs export 7 ~/Desktop/                  # copy it somewhere
quackers docs approve 7                            # mark it ready to use
```

- **CV suggestions** reuse your profile's verified fields only. No skills are
  invented.
- **Cover letters** use the employer name, job title, and relevant evidence
  from your profile.

Files are written to `data/outputs/` and versioned against your candidate
profile version.

---

## Application Tracker

**Statuses:**
`DISCOVERED → SHORTLISTED → PREPARED → APPROVED → APPLIED → INTERVIEW`
with optional terminal states: `REJECTED`, `WITHDRAWN`.

```bash
quackers app set 42 shortlisted
quackers app set 42 prepared
quackers app set 42 applied            # asks you to confirm you submitted it yourself
quackers app follow-up 42 2026-11-15
quackers app interview 42 "Technical round passed"
quackers app note 42 "Recruiter replied within 2 days"
quackers app list --status APPLIED
quackers app followups --days 7
```

**Transitions are explicit** — nothing is inferred. Marking a job as applied
requires a confirmation (or `--confirm`) to prevent accidental records.
Out-of-order moves (e.g. straight from DISCOVERED to INTERVIEW) are allowed but
print a warning.

Each application tracks shortlisted / prepared / approved / applied dates, a
follow-up date, interview status, and append-only notes.

---

## Running Tests

The test suite requires **no API key** and makes **no external network calls.**

```bash
# Windows PowerShell
.\.venv\Scripts\Activate.ps1
pytest

# macOS / Linux
source .venv/bin/activate
pytest
```

Test files (created in `tests/`):
- `test_database.py` — SQLite init, CRUD, deduplication
- `test_deduplication.py` — hard + soft duplicate detection
- `test_matching.py` — Gemini mock score validation
- `test_cv_tailoring.py` — CV tailoring output validation
- `test_settings.py` — search config validation
- `test_sources.py` — source adapters with mocked HTTP
- `test_detail_extractor.py` — JSON-LD, Open Graph, and HTML extraction
- `test_enrichment_service.py` — enrichment pass budget + delay logic
- `test_query_planner.py` — query generation (German variants, skill×location)
- `test_search_providers.py` — Brave/API + DuckDuckGo fallback
- `test_web_search_provider.py` — web-search source provider integration
- `test_dashboard_diagnostics.py` — per-source diagnostics status rows (`quackers scan`)
- `test_cli.py` — command routing, config coercion, terminal-output sanitising
- `test_shell.py` — interactive session, slash completion, Gemini tool loop (faked)
- `test_sources_new.py` — RemoteOK, HackerNews, EURES adapters
- `test_feed_registry.py` — bundled RSS feed list

Currently **171 tests passing**. All HTTP is mocked; no external calls are made.

Add `pytest-mock` if not already installed:
```bash
pip install pytest-mock
```

---

## Backup and Restore

### Backup

```bash
# Windows PowerShell
Copy-Item .\data\jobs.db .\data\jobs.db.backup-$(Get-Date -Format "yyyyMMdd")

# macOS / Linux
cp data/jobs.db data/jobs.db.backup-$(date +%Y%m%d)
```

### Restore

```bash
# Windows PowerShell
Copy-Item .\data\jobs.db.backup-20260101 .\data\jobs.db -Force

# macOS / Linux
cp data/jobs.db.backup-20260101 data/jobs.db
```

> Make sure no `quackers` command (e.g. a running scan) is active before restoring.

---

## Known Limitations

| Area | Limitation |
|---|---|
| **AI matching** | Requires a valid `GEMINI_API_KEY` and the `google-genai` package. Without them, jobs are discovered but scored using the deterministic keyword/skill/education fallback — no error is raised. |
| **Detail extraction** | Works on well-structured job pages. Title-only listings (e.g., some Hacker News job posts) are enriched only if the linked page has parseable content. `extraction_confidence` is reported per field so the CLI can tell you when data is uncertain. |
| **Web search without Brave key** | DuckDuckGo can return HTTP 202 soft-fails on some queries. The source handles this gracefully but coverage is reduced compared to Brave. |
| **EURES source** | Opt-in. EURES publishes machine-readable data behind the EURES Job Mobility Portal; no verified keyless public API endpoint is bundled. Set `EURES_API_URL` in `.env` if you have one. The source shows as `Skipped` (not `Failed`) when the URL is unset. |
| **LinkedIn / X / Instagram** | Not included. These platforms require paid credentials and/or block automated access by default. Only public, keyless, or explicitly-authorized source types are bundled. |
| **PDF export** | Requires `PyMuPDF` (`pip install PyMuPDF`). DOCX always works. |
| **Application submission** | The app can prepare a CV and cover letter and record the application status in your pipeline, but it cannot fill in and submit forms on an external ATS. You must do that step manually. |
| **Free tier** | Gemini free tier limits are ~10 req/hr and ~1 req/day per model (has changed). Set `ENABLE_AI_MATCHING=false` to avoid consuming quota. |

### Configuring additional sources

To add a new Greenhouse or Lever company, edit the source list in:
- `sources/greenhouse.py` — add to the slugs list
- `sources/lever.py` — add to `LEVER_SLUGS`

For RSS feeds, add a new entry in `sources/rss.py`.

For career-page HTML scraping, add a new entry in `sources/career_pages.py`.

---

## Safety

HireMeQuackers was designed with a conservative safety posture for a tool that operates on personal data and interacts with external services:

- **URL fetch safety** — `services/safe_requests.py` rejects `http://` loopback, private RFC 1918, link-local, and `file://` schemes. Only public `https://` URLs are fetched.
- **Content sanitisation** — User-visible and user-uploaded content is sanitised (HTML stripped, tags removed) before being stored in SQLite, and terminal control characters / escape sequences are stripped from anything printed by the CLI.
- **Robots.txt honoured for scoping** — `services/robots.py` is used to check whether a path is allowed for a specific crawl. It does not prevent pagination or repeated access to already-allowed paths.
- **API key handling** — Google Gemini API key is sent only to `aistudio.google.com` endpoints. Brave Search key is sent only to `api.search.brave.com`. No other service receives your API keys.
- **No CAPTCHA or login bypass** — The application does not implement any bot/anti-bot work-around, and will not bypass paywalls, logins, or CAPTCHAs.
- **SQLite single-process** — The DB uses a single connected process. Do not run two `quackers scan` commands at the same time, as this causes `database is locked` errors.

---

## Adding a New Source Adapter

1. Create `sources/my_source.py`.
2. Import `JobSource`, `RawListing`, `SourceResult` from `sources.base`.
3. Import `register` from `sources.registry`.
4. Subclass `JobSource` and implement:
   - `name` (class attribute, display name)
   - `slug` (class attribute, lowercase identifier)
   - `scan(cfg)` — returns a `SourceResult` with zero or more `RawListing`s.
5. Add the class with the `@register` decorator.
6. Import the module in `sources/__init__.py` so it gets registered.
7. Enable it: `quackers sources enable <name>` (check with `quackers sources list`).

---

## Troubleshooting

### "ModuleNotFoundError: google.genai"
```bash
pip install google-genai
```
The app still works without it — AI matching is just disabled. Add it to use
the full feature set.

### "quackers: command not found"
Run `pip install -e .` inside the activated virtual environment, or use
`python -m quackers ...` instead.

### A source shows "failed" in `quackers sources list`
- Check the error column of `quackers sources list`, or `quackers history show <scan-id>`.
- Common causes: the company's ATS slug changed, the endpoint is rate-limiting,
  or the site requires JavaScript to render.
- Built-in retries are enabled; try the scan again later.

### PDF generation fails (only DOCX works)
Install `PyMuPDF`:
```bash
pip install PyMuPDF
```
If it still fails, generate DOCX and convert to PDF using LibreOffice:
```bash
soffice --headless --convert-to.pdf output/my_cv.docx
```

### Playwright errors
Playwright is disabled by default. To enable it:
1. `pip install playwright`
2. `playwright install chromium`
3. Set `ENABLE_PLAYWRIGHT=true` in `.env`
4. Only use for pages where JS rendering is required and access is permitted.

### SQLite "database is locked"
Only one process can write to the SQLite DB at a time. Make sure you don't
run two `quackers scan` commands simultaneously.

### Match score is 0 or blank
- The job was scanned with `ENABLE_AI_MATCHING=false`, or
- `google-genai` is not installed, or
- Your `GEMINI_API_KEY` is the placeholder value.
Run a fresh scan with AI matching enabled after fixing any of the above.

---

## Project Structure

```
HireMeQuackers/
├── requirements.txt        # Python dependencies
├── pyproject.toml          # Project metadata, test config
├── conftest.py             # Shared pytest fixtures
│
├── config/
│   ├── settings.py         # Env variable loading (python-dotenv)
│   ├── search_config.py    # SearchConfig pydantic model, EmploymentType enum
│   ├── runtime_settings.py # Live settings persistence glue
│   └── rate_limit.py       # Per-source rate limiting
│
├── database/
│   ├── connection.py       # Engine + session factory (SQLite)
│   ├── migrations.py       # Additive DB migrations (new columns/tables)
│   ├── models.py           # SQLAlchemy 2.x models
│   └── repository.py       # All DB read/write operations
│
├── sources/
│   ├── base.py             # JobSource, RawListing, SourceResult types
│   ├── registry.py         # Source registration + lookup
│   ├── greenhouse.py       # Greenhouse public API adapter
│   ├── lever.py            # Lever public API adapter
│   ├── rss.py              # RSS/Atom feed adapter
│   ├── career_pages.py     # Public career-page HTML adapter
│   ├── search_engine.py    # Search-provider orchestration (query planner + worker pool)
│   ├── search_providers.py # Brave API + DuckDuckGo fallback layer
│   ├── remoteok.py         # RemoteOK keyless JSON API adapter
│   ├── hackernews.py       # Hacker News Algolia job-listings adapter
│   └── eures.py            # EURES EU public-sector adapter (opt-in)
│
├── services/
│   ├── discovery.py        # Orchestration: scan → normalize → dedupe → store → enrich
│   ├── normalization.py    # Raw listing → internal schema
│   ├── deduplication.py    # Fingerprint + similarity-based dedupe
│   ├── filters.py          # Deterministic keyword/location/employment filters
│   ├── matching.py         # Gemini match scoring + fallback
│   ├── gemini_client.py    # Gemini SDK wrapper + health check
│   ├── candidate_profile.py # Profile CRUD + versioning
│   ├── cv_parser.py        # PDF/DOCX → structured sections
│   ├── cv_tailoring.py     # Generate job-specific CV content
│   ├── cover_letter.py     # Generate cover letter text
│   ├── application_assistant.py # Application state machine
│   ├── scheduler.py        # OnDemandScheduler (single on-demand scan)
│   ├── query_planner.py    # Builds multi-query plan from user search settings
│   ├── feed_registry.py    # Bundled RSS feed list (default + supplementary)
│   ├── detail_extractor.py # JSON-LD → Open Graph → HTML heuristics
│   ├── enrichment_service.py # Post-store detail enrichment with rate limiting
│   ├── safe_requests.py    # SSRF-safe HTTP fetch (httpx)
│   ├── robots.py           # robots.txt checking
│   ├── sanitization.py     # HTML stripping, content sanitization
│   ├── cache.py            # Evaluation result cache
│   ├── usage_tracking.py   # Gemini API call counter
│   └── logger.py           # Logging setup
│
├── documents/
│   ├── exporter.py         # DOCX / PDF export
│   └── templates/          # Document templates
│
├── quackers/              # Command-line interface (`quackers`)
│   ├── cli.py              # argparse commands: scan, jobs, job, app, docs, profile, config, ...
│   ├── shell.py            # interactive session (`quackers` with no arguments)
│   ├── agent.py            # Gemini natural-language mode (tools = CLI commands)
│   ├── store.py            # DB session + row-to-dict helpers used by the commands
│   ├── term.py             # Terminal output: tables, colours, prompts, control-char stripping
│   └── __main__.py         # `python -m quackers`
│
├── data/                   # SQLite database (created on first run)
├── output/                 # Generated CVs and cover letters
├── test_data/              # Fixture files for tests
└── tests/                  # Pytest test suite
```
