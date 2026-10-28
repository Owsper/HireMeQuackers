# HireMeQuackers — Local-First AI Job-Hunting Agent

HireMeQuackers is a local web application that discovers job and internship listings from multiple public sources, evaluates how well you fit each position using Google Gemini (optional), tailors your CV, generates cover letters, and tracks your application pipeline — all in a simple SQLite database on your machine.

Everything runs in a Streamlit dashboard at [http://localhost:8501](http://localhost:8501).

---

## New in v2 — Max-Coverage Discovery Engine

The v2 max-coverage discovery engine (October 2026) replaces the original single-search-provider pipeline with a multi-source, query-planner-driven scanner. Highlights:

- **13 job sources** (up from 5). In addition to Greenhouse, Lever, RSS feeds, Web Search, and Career Pages, v2 adds **RemoteOK** (keyless JSON API), **Hacker News** (Algolia job listings), and **EURES** (opt-in EU public-sector endpoint).
- **Hybrid search providers.** When `BRAVE_SEARCH_API_KEY` is set, the web-search source uses the **Brave Search API** with pagination. Without a key it automatically falls back to **DuckDuckGo** — the app runs with zero paid keys.
- **Query planner.** Instead of a single hand-typed query, a query planner builds up to `SCAN_MAX_TOTAL_QUERIES` complementary queries per scan: exact role titles + synonyms, skills x locations, German-language variants when European locations are set (`Praktikum`, `Werkstudent`, `Ausbildung`), ATS `site:` queries, informal hiring phrasing, and recent-posted variants.
- **Detail-page extraction.** Web and career-page results no longer come in as title + URL only. A detail extractor (JSON-LD `JobPosting` first, Open Graph tags second, HTML heuristics third) recovers description, salary, employment type, posted/closing dates, and application URL, and records an `extraction_confidence` so the UI can tell you how reliable each field is.
- **Enrichment pass.** Newly stored jobs are automatically re-fetched (rate-limited, batched up to `ENRICHMENT_MAX_PER_SCAN`) to fill missing description text, so AI matching has real content to work on.
- **Dynamic source registry + health tracking.** Source connectivity (listings found, last seen, freshness, per-source failure counts) is persisted and surfaced on the Sources page.
- **Scan diagnostics panel.** After each scan, the Overview page shows a per-source breakdown: `Success` / `Partial` / `Failed` / `Skipped` status, attempted, fetched, parsed, duplicate, and failed counts, plus a human-readable error message.
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
7. [Using the Dashboard](#using-the-dashboard)
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

> Make sure your virtual environment is **activated** first.

```bash
streamlit run app.py
```

Your browser should open automatically at **http://localhost:8501**.
If it doesn't, open that URL manually.

To stop the app, press `Ctrl+C` in the terminal.

---

## How a Scan Works

1. **Query plane.** The query planner builds up to `SCAN_MAX_TOTAL_QUERIES` queries per run, using the role titles, preferred skills, locations, and language hints in your search settings.
2. **Worker pool.** Sources run in a bounded thread pool (5 workers, configurable via `SCAN_MAX_SOURCES`).
3. **Normalisation + de-dup.** Listings are normalised and de-duplicated using strong keys (canonicalised URL, provider + job-ID). Loose title-similarity is used only to *flag* a potential duplicate — it does not discard any record.
4. **Enrichment.** Newly stored web and career-page jobs are re-fetched (rate-limited, up to `ENRICHMENT_MAX_PER_SCAN` per scan, `ENRICHMENT_DELAY_SECONDS` between calls) to recover description, salary, employment type, and apply URL when the initial listing had none.
5. **Matching.** Results are scored against your uploaded CV profile using Gemini when available, or via the deterministic keyword/skill/education fallback when not.
6. **Storage.** Records are written to SQLite immediately so a network hang mid-scan cannot lose data.

> **Diagnosis after a scan:** After a scan finishes, expand **"Source diagnostics"** on the
> Overview page to see per-source rows: `Success` / `Partial` / `Failed` / `Skipped`,
> with attempted, fetched, parsed, duplicate, and failed counts, plus a human-readable
> error message when a source reports an issue.

---

## Using the Dashboard

The sidebar on the left gives you access to **10 pages**:

| Page | What it does |
|---|---|
| **Overview** (⓿) | At-a-glance stats: total jobs, new jobs, shortlisted, prepared, applied, interviews, follow-ups. Also has the **"Scan for new jobs now"** button and the AI health check. |
| **Search Settings** (1⚙️) | Edit keywords, employment types, locations, filters, workplace preferences, and save/load named profiles. |
| **Job Discovery** (2🔍) | Browse, filter, sort, and shortlist all discovered jobs. "New since last scan" toggle. |
| **Job Detail** (3📋) | Full job description, extracted requirements, AI match breakdown, missing qualifications, shortlist / apply actions, and document generation buttons. |
| **Candidate Profile** (4👤) | Upload your CV (PDF/DOCX), review and edit the extracted profile, save changes. Profile version bumps on each edit. |
| **Documents** (5📄) | List all generated CVs and cover letters per job, preview, download, approve, or regenerate. |
| **Application Tracker** (6📚) | Full pipeline per job: Discovered → Shortlisted → Prepared → Approved → Applied → Interview → Rejected/Withdrawn. Notes, follow-up dates, interview status. |
| **Sources** (7🌐) | Per-source health: last scan date, listings found, error messages, enable/disable toggle. |
| **Scan History** (8📅) | Every past scan with its config, timing (start/duration), new/duplicate counts, failures, and a "re-run this config" button. |

---

## Uploading and Reviewing Your CV

1. Go to **Candidate Profile** (☄ icon).
2. Click **"Upload CV"** and select your `.pdf` or `.docx` file.
3. HireMeQuackers extracts text and splits it into sections (name, email, phone,
   summary, skills, education, experience, certifications, achievements,
   projects, languages).
4. Review and correct any extracted fields before saving.
5. Click **"Save profile"**. Each save with content changes increments the
   `profile_version` and invalidates cached AI evaluations tied to the old version.
6. Go to **AI health check** on the Overview page to confirm Gemini is configured.

> The CV file is stored locally in `data/`. No CV content is uploaded to any
> external service except the minimum data required for a Gemini API call when
> you explicitly trigger AI matching.

---

## Configuring Search Settings

1. Go to **Search Settings** (1⚙️).
2. Edit your search criteria:
   - **Employment types** — select one or more: Internship, Working Student,
     Part-time, Full-time, Graduate, Temporary, Contract, Other.
   - **Keywords** — include / exclude keywords (comma-separated).
   - **Skills** — required and preferred skills (comma-separated).
   - **Locations** — included / excluded locations, language requirements.
   - **Workplace arrangement** — remote, hybrid, onsite, worldwide-remote toggles.
   - **Filters** — posted-within-days (1/3/7/14/30), salary min/max, max results.
   - **Boolean groups** — optional AND / OR / NOT keyword groups.
   - **Sources** — which registered sources to include in scans.
3. Click **"Apply + persist to live settings"** to save.
4. Optionally enter a name in the "Save current as profile" field and click
   **"Save profile"** to store a reusable named profile.
5. To load a saved profile later, select it from the dropdown and click
   **"Load profile"**.

---

## Triggering a Scan

Scans are **explicit** — they only run when you click the button. There is no
automatic daily scheduler.

1. Go to **Overview** (⓿).
2. Optionally toggle **"Evaluate with Gemini"** if you want AI match scores
   (costs API quota) or leave it off for deterministic filters only.
3. Click **"Scan for new jobs now"**.
4. Watch the progress bar. You can click **"Stop scan"** to cancel.
5. When complete, a result card appears showing:
   - `done` / `partial` / `error` status
   - Total discovered, new jobs, duplicates
   - Per-source failures (click to expand)
6. New and updated jobs are saved to the database immediately — even if the
   app closes or the API fails mid-scan.

**Scan behavior:**
- Sources are scanned in sequence (bounded by `SCAN_MAX_SOURCES`).
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
- You clicked "Scan" with "Evaluate with Gemini" checked.

To view scores:
- **Job Discovery** page: the "Match" column shows the score for each job.
   Set the "Min match score" filter to sort by fit.
- **Job Detail** page: the "Match" tab shows the full breakdown:
  - Per-skill match (required vs. preferred)
  - Experience, education, language, location fit
  - Evidence from your profile
  - Missing or weak qualifications
  - Confidence level

> Scores are **not** a guarantee of being hired. They reflect how closely your
> profile matches the stated job requirements.

---

## Generating CVs and Cover Letters

On the **Job Detail** page, the "Documents" tab offers:

- **Generate tailored CV** — produces a job-specific CV that reuses your
  profile (verified fields only). No skills are invented. The output is
  saved to `output/` and logged in the Documents page.
- **Generate cover letter** — produces a professional cover letter using the
  employer name, job title, and relevant evidence from your profile.

Both documents are:
- Versioned against your candidate profile version
- Previewable on the **Documents** page
- Downloadable as DOCX (PDF export requires `PyMuPDF` + a system LaTeX install)
- Approvable (gitkeep) before you use them for a submission

---

## Application Tracker

The **Application Tracker** (6📚) is the single view of your pipeline.

**Statuses:**
`DISCOVERED → SHORTLISTED → PREPARED → APPROVED → APPLIED → INTERVIEW`
with optional terminal states: `REJECTED`, `WITHDRAWN`.

**Transitions are explicit** — nothing is inferred. To mark a job as "Applied",
you must click the "Mark as Applied" button in the Job Detail page. This is
a deliberate confirmation to prevent accidental submissions.

Each application row tracks:
- Shortlisted date, documents-prepared date, approved date, applied date
- Follow-up date (upcoming follow-ups appear on Overview and the tracker)
- Interview status (free text, e.g., "Technical round passed")
- Notes (append-only history)

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
- `test_dashboard_diagnostics.py` — per-source diagnostics status rows
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

> Stop the Streamlit app before restoring. If you have a live app open,
> close it first (`Ctrl+C`).

---

## Known Limitations

| Area | Limitation |
|---|---|
| **AI matching** | Requires a valid `GEMINI_API_KEY` and the `google-genai` package. Without them, jobs are discovered but scored using the deterministic keyword/skill/education fallback — no error is raised. |
| **Detail extraction** | Works on well-structured job pages. Title-only listings (e.g., some Hacker News job posts) are enriched only if the linked page has parseable content. `extraction_confidence` is reported per field so the UI can tell you when data is uncertain. |
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
- **Content sanitisation** — User-visible and user-uploaded content is sanitised (HTML stripped, tags removed) before being stored in SQLite or rendered in the Streamlit UI.
- **Robots.txt honoured for scoping** — `services/robots.py` is used to check whether a path is allowed for a specific crawl. It does not prevent pagination or repeated access to already-allowed paths.
- **API key handling** — Google Gemini API key is sent only to `aistudio.google.com` endpoints. Brave Search key is sent only to `api.search.brave.com`. No other service receives your API keys.
- **No CAPTCHA or login bypass** — The application does not implement any bot/anti-bot work-around, and will not bypass paywalls, logins, or CAPTCHAs.
- **SQLite single-process** — The DB uses a single connected process. Do not run more than one `streamlit run app.py` instance simultaneously, as this causes `database is locked` errors.

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
7. Enable it in the UI: **Sources** page → toggle it on.

---

## Troubleshooting

### "ModuleNotFoundError: google.genai"
```bash
pip install google-genai
```
The app still works without it — AI matching is just disabled. Add it to use
the full feature set.

### Port 8501 already in use
```bash
# Windows
streamlit run app.py --server.port 8502

# macOS / Linux
streamlit run app.py --server.port 8502
```

### A source shows "failed" in the Sources page
- Check the error message on the Sources page.
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
run two instances of `streamlit run app.py` simultaneously.

### Match score is 0 or blank
- The job was scanned with `ENABLE_AI_MATCHING=false`, or
- `google-genai` is not installed, or
- Your `GEMINI_API_KEY` is the placeholder value.
Run a fresh scan with AI matching enabled after fixing any of the above.

---

## Project Structure

```
HireMeQuackers/
├── app.py                  # Streamlit entry point, sidebar nav
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
│   ├── scheduler.py        # OnDemandScheduler (background scan thread)
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
├── ui/
│   ├── dashboard.py        # Overview page (stats + scan button)
│   ├── settings_page.py    # Search settings form
│   ├── jobs_page.py        # Job discovery / list with filters
│   ├── job_detail_page.py  # Single job detail
│   ├── cv_page.py          # Candidate profile + CV upload
│   ├── documents_page.py   # Generated docs list + download
│   ├── applications_page.py # Application pipeline tracker
│   ├── sources_page.py     # Source health + enable/disable
│   ├── scan_history.py     # Past scans + retry
│   └── util.py             # Shared session + formatters
│
├── data/                   # SQLite database (created on first run)
├── output/                 # Generated CVs and cover letters
├── test_data/              # Fixture files for tests
└── tests/                  # Pytest test suite
```
