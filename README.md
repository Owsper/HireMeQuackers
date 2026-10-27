# HireMeQuackers — Local-First AI Job-Hunting Agent

HireMeQuackers is a local web application that searches the web for job listings matching your preferences, evaluates how well you fit each position using Google Gemini (optional), tailors your CV, generates cover letters, and tracks your application pipeline — all in a simple SQLite database on your machine.

Everything runs in a Streamlit dashboard at [http://localhost:8501](http://localhost:8501).

---

## Table of Contents

1. [Prerequisites](#prerequisites)
2. [Installation](#installation)
3. [Configuration](#configuration)
4. [Running the Application](#running-the-application)
5. [Using the Dashboard](#using-the-dashboard)
6. [Uploading and Reviewing Your CV](#uploading-and-reviewing-your-cv)
7. [Configuring Search Settings](#configuring-search-settings)
8. [Triggering a Scan](#triggering-a-scan)
9. [Reviewing Match Scores](#reviewing-match-scores)
10. [Generating CVs and Cover Letters](#generating-cvs-and-cover-letters)
11. [Application Tracker](#application-tracker)
12. [Running Tests](#running-tests)
13. [Backup and Restore](#backup-and-restore)
14. [Known Limitations and Source-Specific Setup](#known-limitations)
15. [Adding a New Job-Source Adapter](#adding-a-new-source-adapter)
16. [Troubleshooting](#troubleshooting)
17. [Project Structure](#project-structure)

---

## Prerequisites

| Requirement | Minimum Version | Notes |
|---|---|---|
| Python | 3.10 | 3.11 or 3.12 recommended |
| pip | 23.0+ | Usually ships with Python |
| OS | Windows 10/11, macOS 12+, Linux | Windows PowerShell users: see below |

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

# 2. Create a virtual environment
py -m venv .venv

# 3. Activate it
.\.venv\Scripts\Activate.ps1

# 4. Install dependencies
pip install -r requirements.txt

# 5. Optional: install the Gemini SDK (for AI matching)
pip install google-genai

# 6. Optional: install PDF extraction
pip install PyMuPDF
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

After installing, create a `.env` file in the project root:

```bash
# Windows
copy .env.example .env

# macOS / Linux
cp .env.example .env
```

Then open `.env` in a text editor and fill in your values:

| Variable | Default | Description |
|---|---|---|
| `GEMINI_API_KEY` | `your_gemini_api_key_here` | Your Gemini API key (get one at [aistudio.google.com/apikey](https://aistudio.google.com/apikey)) |
| `GEMINI_MODEL` | `gemini-2.0-flash` | Model name to use for AI matching |
| `DATABASE_URL` | `sqlite:///./data/jobs.db` | SQLite database path |
| `APP_ENV` | `local` | Environment label |
| `LOG_LEVEL` | `INFO` | Logging verbosity |
| `REQUEST_TIMEOUT_SECONDS` | `15` | HTTP timeout for source requests |
| `MAX_RETRY_ATTEMPTS` | `3` | Retry count per source |
| `RATE_LIMIT_MIN_SECONDS` | `1.0` | Minimum pause between source requests |
| `MAX_RESPONSE_BYTES` | `3145728` | Response size cap (3 MB) |
| `SCAN_MAX_SOURCES` | `8` | How many sources to scan per run |
| `ENABLE_AI_MATCHING` | `true` | Toggle Gemini evaluation on/off |
| `ENABLE_PDF_EXPORT` | `true` | Enable PDF export of generated docs |

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
| **AI matching** | Requires a valid `GEMINI_API_KEY` and the `google-genai` package. Without them, jobs are discovered but not scored. |
| **PDF export** | Requires `PyMuPDF` (`pip install PyMuPDF`). DOCX always works. |
| **Playwright** | Disabled by default (`ENABLE_PLAYWRIGHT=false`). Enable only for JS-rendered pages where access is explicitly permitted. |
| **Source coverage** | Uses public ATS endpoints (Greenhouse, Lever), public RSS feeds, and public career-page HTML. It does **not** bypass logins, CAPTCHAs, paywalls, or anti-bot controls. |
| **Scan concurrency** | Sources are scanned sequentially to be conservative. To speed this up, adjust `SCAN_MAX_SOURCES` and `RATE_LIMIT_MIN_SECONDS`. |
| **Free tier** | Gemini free tier has daily/hourly message limits. Set `ENABLE_AI_MATCHING=false` to run scans without consuming quota. |

### Configuring additional sources

To add a new Greenhouse or Lever company, edit the source list in:
- `sources/greenhouse.py` — add to the slugs list
- `sources/lever.py` — add to `LEVER_SLUGS`

For RSS feeds, add a new entry in `sources/rss.py`.

For career-page HTML scraping, add a new entry in `sources/career_pages.py`.

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
├── .env.example            # Template for .env configuration
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
│   └── search_engine.py    # Search-provider API adapter (configurable)
│
├── services/
│   ├── discovery.py        # Orchestration: scan → normalize → dedupe → store
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
