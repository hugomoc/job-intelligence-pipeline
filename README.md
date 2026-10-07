# Job Intelligence Pipeline

[![CI](https://github.com/hugomoc/job-intelligence-pipeline/actions/workflows/ci.yml/badge.svg)](https://github.com/hugomoc/job-intelligence-pipeline/actions/workflows/ci.yml)

AI-powered job intelligence pipeline that ingests job-alert emails, enriches job postings, scores resume fit, and serves recommendations through Streamlit and dbt analytics.

This project is designed as a daily job-search cockpit: open the UI, ingest new job-alert emails, enrich descriptions when possible, AI-score unscored jobs, review ranked opportunities, and track whether each job is new, applied, or removed.

## What It Does

- Reads job-alert emails from Yahoo IMAP folders.
- Parses job cards from LinkedIn, Indeed, Glassdoor, Ladders, RemoteHunter, and ZipRecruiter alerts.
- Stores operational job data in DuckDB.
- Deduplicates repeated job records with a canonical job identity.
- Enriches postings by fetching public job-description pages when available.
- Extracts resume text from uploaded PDF or DOCX files in memory.
- Reuses cached resume profiles by `resume_hash`.
- Scores only resume/job combinations that have not already been scored.
- Uses Gemini for AI scoring with OpenAI fallback.
- Runs dbt to build analytics views and marts.
- Serves a Streamlit UI for reviewing jobs, opening apply links, and marking status.

## Architecture

```text
Email folders
  -> Python ingestion and parsers
  -> DuckDB operational tables
  -> Description enrichment
  -> Resume profiling and AI scoring
  -> dbt staging, intermediate, and mart models
  -> Streamlit recommendations UI
```

## Tech Stack

- Python
- DuckDB
- dbt-duckdb
- Streamlit
- Gemini API
- OpenAI API
- BeautifulSoup / httpx
- Playwright
- PDF and DOCX resume extraction

## Repository Layout

```text
config/
  searches.yml              # Rule-based search definitions
  sources.yml               # Email source/folder configuration

job_intelligence_dbt/
  models/
    staging/                # Source-cleaning views
    intermediate/           # Deduplication and latest-score views
    marts/                  # Analytics tables for the UI
  tests/                    # dbt singular tests

scripts/
  dbt_jobs.sh               # dbt wrapper with absolute DuckDB path

src/
  ai/                       # Resume profiling and job scoring
  connectors/               # Yahoo IMAP email reader
  enrichment/               # Public job-description extraction
  matching/                 # Rule-based job matching
  parsers/                  # Source-specific email parsers
  repositories/             # Database access for UI workflows
  resume/                   # In-memory resume extraction
  ui/                       # Streamlit orchestration services

streamlit_app.py            # Streamlit presentation layer
```

## Data Model

Python owns the operational source tables in `data/jobs.duckdb`:

- `raw_jobs`
- `job_matches`
- `resume_profiles`
- `resume_job_scores`
- `processed_emails`
- `application_status`
- `job_enrichment_attempts`

dbt reads those tables and creates analytics objects in the `analytics` schema. dbt does not modify or recreate the Python-owned operational tables.

The final recommendation mart is:

```text
analytics.mart_job_recommendations
```

It supports ranking jobs by resume hash, canonical job identity, AI score, recommendation status, confidence, fit dimensions, missing qualifications, risks, and summary.

## Canonical Job Identity

Repeated emails or multiple source records can refer to the same job. The project uses a canonical key:

```sql
coalesce(nullif(job_fingerprint, ''), record_key)
```

That key is used for deduplication, cached scoring, application status, and the recommendation mart so the same job is not repeatedly scored or shown as a separate opportunity.

## Setup

Create and activate a virtual environment:

```bash
python -m venv venv312job
source venv312job/bin/activate
pip install -r requirements.txt
```

Install the Playwright browser used by the standalone Lensa resolver:

```bash
playwright install chromium
```

Create a local `.env` file:

```bash
YAHOO_EMAIL=your_email@example.com
YAHOO_APP_PASSWORD=your_yahoo_app_password
GEMINI_API_KEY=your_gemini_api_key
OPENAI_API_KEY=your_openai_api_key
```

Optional:

```bash
OPENAI_MODEL=gpt-4.1-mini
```

The `.env` file is ignored by git.

Optional browser resolver settings:

```bash
PLAYWRIGHT_HEADLESS=true
JOB_RESOLVER_DELAY_SECONDS=1
```

## Running the App

Start Streamlit:

```bash
python -m streamlit run streamlit_app.py
```

The UI supports the daily workflow:

1. Upload a PDF or DOCX resume.
2. Ingest new email alerts.
3. Enrich job descriptions from public pages.
4. Score unscored jobs.
5. Review jobs ordered by AI score.
6. Open apply links.
7. Mark jobs as applied or removed.

Uploaded resume files and original resume text are not retained by the app. The app stores a structured resume profile and scores keyed by `resume_hash` so repeated runs can avoid duplicate AI calls.

## dbt

Use the wrapper script so dbt always points at the correct DuckDB database:

```bash
./scripts/dbt_jobs.sh debug
./scripts/dbt_jobs.sh parse
./scripts/dbt_jobs.sh build
```

The wrapper sets:

```bash
JOB_INTELLIGENCE_DB_PATH=<repo-root>/data/jobs.duckdb
```

and uses the active virtual environment's dbt executable when available.

## Backlog Scoring

To score unscored jobs outside the UI:

```bash
python -m src.score_backlog --limit 20 --min-rule-score 0
```

The scorer:

- loads the latest cached resume profile unless a `resume_hash` is provided
- selects unscored canonical jobs
- avoids rescoring jobs already scored for the same resume and prompt version
- uses Gemini first
- switches to OpenAI fallback when Gemini quota is exhausted
- runs dbt build after new scores are saved

## Standalone Lensa Resolver

Lensa alert links can open a search-results page instead of the individual
posting. The standalone resolver uses Playwright to open the Lensa page, locate
the matching job card by title and company, click that card's `Read more` link,
and extract the underlying JobLeads posting.

Manual test:

```bash
python -m src.test_lensa_resolver \
  --url "<LENSA_URL>" \
  --title "Remote Data Modeling & BI Analyst (EST)" \
  --company "Hayward Holdings, Inc." \
  --headed
```

The command prints the resolved URL and description length. It does not print the
full description unless `--verbose` is passed.

## Tests and Checks

GitHub Actions runs the deterministic offline test suite on pushes and pull
requests to `main`.

Compile Python files:

```bash
python -m compileall -q src scripts streamlit_app.py
```

Run the same offline CI test list locally:

```bash
python scripts/run_ci_tests.py
```

Run dbt validation:

```bash
./scripts/dbt_jobs.sh build
```

The dbt layer includes tests for:

- required identifiers
- unique source keys where appropriate
- valid recommendation and confidence values
- score ranges between 0 and 100
- AI scores referencing existing jobs
- apply recommendations meeting score thresholds
- recommendation records having application URLs
- JSON validity for stored JSON text fields
- uniqueness of `resume_hash + canonical_job_key` in the mart

## Privacy and Local Files

The following are intentionally ignored by git:

- `.env`
- `data/`
- `*.duckdb`
- `*.pdf`
- `*.docx`
- virtual environments
- dbt logs and target artifacts
- Python cache files

This keeps API keys, local email-derived data, resumes, and generated artifacts out of the repository.

## Current Limitations

- Many job-alert emails do not include full job descriptions.
- Some job sites block automated description fetching or return unstable redirect links.
- Glassdoor recommendation links may redirect to a different job later.
- LinkedIn should not be automated through a logged-in browser session because that may violate platform terms and put the account at risk.
- AI recommendations are less reliable when the description is incomplete, so the UI labels those jobs clearly.

## Future Improvements

- Search for official employer or ATS postings when an alert only contains a platform redirect.
- Add more robust public-page enrichment for Workday, Greenhouse, Lever, Ashby, and SmartRecruiters.
- Add description-quality scoring before AI resume matching.
- Add a dedicated review queue for jobs that need manual description capture.
- Add dashboard metrics for daily new jobs, scored jobs, applied jobs, and removed jobs.
