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


### Official job discovery: Tavily first, Serper backup

Configure `TAVILY_API_KEY` locally in `.env` to use Tavily as the primary
public-search provider. Keep `SERPER_API_KEY` as the secondary provider.
Never commit either secret to GitHub.

```dotenv
TAVILY_API_KEY=tvly-your-key-here
SERPER_API_KEY=your-existing-serper-key
```

The first available configured provider is Tavily, followed by Serper. A
Tavily **quota, authentication, rate-limit or server/network error** causes a
retry with Serper for that job. Separate per-provider circuits suppress
repeated failed requests within a run. A normal successful Tavily response
(even zero results) does **not** consume a Serper search. When neither key is
configured, public search is deferred. DuckDuckGo is no longer an automatic
fallback; it can be enabled explicitly for legacy diagnosis with
`PUBLIC_SEARCH_PROVIDER=duckduckgo` (not recommended for actual runs).

Tavily is called via its official HTTPS search endpoint with
`search_depth=basic`, `max_results=10`, `auto_parameters=false`, and no
answer/raw-content extraction. The Researcher plan provides 1,000 credits
per calendar month; each basic search consumes one credit.
See https://docs.tavily.com/documentation/api-credits and
https://www.tavily.com/pricing for current limits.

Source URLs, verified cache, and direct ATS discovery precede public search.
Search results supply candidate URLs **only**. Neither Tavily nor Serper
titles/snippets are trusted as job-posting identity: a fetched official page
must pass the same company/title/identity checks. No AI fit scores or
confirmed application URLs change solely from search metadata.

#### Measure actual API calls, not job counts

The normal automatic enrichment limit is 25 jobs, but the number of paid
searches is usually lower when direct pages or ATS adapters succeed.
Every targeted retry report contains `provider_metrics` for Tavily and Serper,
including `search_requests` and `successful_responses`. The enrichment
drain report also saves `public_search_requests`,
`public_search_requests_per_processed_job` and per-provider request counts.
The numbers include health-probe requests when a run performs probes.
Do not assume that the number of jobs equals credits consumed.

Run a small staged live validation **after** configuring the Tavily key:

```sh
python -m src.retry_targeted_enrichment --require-tavily --max-jobs 5 --batch-size 5 --delay 0.75 --report data/tavily-test-5.json
```

This can make real Tavily calls and persist the five attempted enrichment
records, so inspect the report before running a large backlog. Never paste
API keys in logs or chat. A Serper fallback will spend Serper credits.

#### Faster targeted retries and safe diagnostics

For `--max-jobs 1`, the targeted retry command **skips its two redundant
search-provider health probes**, so a job that reaches public search normally
uses one Tavily basic request (or an additional Serper request if Tavily fails).
Direct verified/source/ATS reuse may use no public-search requests. This mode
still saves the single attempted job; it is not a dry run.

Each targeted report now contains a `jobs[].timings` breakdown: source-page
fetch, ATS discovery, public search and total candidate fetch durations
(in milliseconds), the number of actual candidate fetches, and blocked-host
skips. The report also measures `verified_candidate_metadata_ms` (offline
trusted candidate preparation) and `save_attempt_ms` (DuckDB write).
`candidate_validation[].elapsed_ms` shows timing per candidate.
Targeted reports additionally include `queue_initial_load_ms`,
`queue_refresh_total_ms` and `queue_loader_calls`. With a bounded
`--max-jobs` run, the drain skips a full DuckDB queue refresh **when that
limit is reached**, recording `queue_refresh_skipped_at_limit` and
`remaining_unattempted_this_run`; a later invocation recomputes the actual
targeted queue. `remaining_targeted_queue` remains the most recent full
database snapshot and may still include the just-processed retryable posting.
These metrics are observational and **do not** change scoring or job identity.

Official resolution uses a cooperative 60-second time budget, with an 8-second
HTTP timeout for individual official candidate-page requests. If checking all
candidates would overrun the budget, resolution is deferred rather than
accepting a partial candidate set. This is **not a hard 60-second process
timeout**: a single in-flight request, dynamic browser render, or source fetch
can still overrun the budget. The normal source/verified URL behavior is
unchanged.

When an official host rejects a candidate request with 401/403/429, later
non-ATS URLs on that same hostname are skipped **for that one job only**.
If those skips leave potentially competing postings unchecked, resolution
is deferred instead of claiming a unique match. Shared ATS hosts are exempt
from this shortcut. The normal fetched-page identity/ambiguity checks are
unchanged. Alert-card trailing `|` separators are removed only from public
search queries; the stored posting title and identity are unchanged.

Example bounded live validation (stop Streamlit before using DuckDB write tools):

```sh
python -m src.retry_targeted_enrichment --require-tavily --max-jobs 1 --batch-size 1 --delay 0.75 --report data/tavily-single-diagnostic.json
```

Review timings and API request counts before choosing any broader retries.

#### Targeted retry SQL, low-credit searches and exact posting leads

Targeted transient-retry runs now prefilter `SEARCH_DEFERRED` and
`INSUFFICIENT_IDENTITY`, plus search-origin `BLOCKED`/`ERROR`,
in DuckDB **before loading descriptions and running resume admission checks**.
The status/score CTEs still retain the entire raw-job identity graph, and the
original Python eligibility and identity checks still run. The report adds
`queue_initial_load_phases` with initialization, SQL, description quality,
verified-candidate metadata, and admission gate timings. The nested
`verified_candidate_details` now shows candidate SQL loading, authoritative
key-based reuse, fuzzy annotation, total trusted candidate count and jobs
considered. Fuzzy annotation uses a per-load fingerprint index instead of
re-normalizing every historical candidate for every queued job; this is
only a candidate hint, not an identity assertion. Resume admission now
normalizes the unchanged resume profile and its scope signals once per
batch rather than for each job, while evaluating each job independently.
A local run is still needed to determine the actual saved time. Larger inventories
should benefit, but real elapsed time is measured on the local database.

Successful Tavily/Serper URL results are cached **within the shared HTTP
client for that run**, keyed by provider and normalized query. The cache stores
URLs and untrusted search titles as discovery/ranking hints; failed responses
are never cached, and each distinct posting record must still pass fetched
identity validation. Provider request metrics count actual paid API requests,
not cache hits; `jobs[].search.cache_hit` records individual reuse.

Search candidates must have a specific posting URL. ATS roots, corporate
career homepages and Dice job categories are excluded, though they remain in
the report's audit-only organic search results. When a search reveals an
actual employer Workday tenant homepage (for example, a `myworkdayjobs.com`
career site), the resolver can perform one bounded public Workday CXS listing
query for an exact title and discover specific candidate links **without a
second Tavily search**. These are still just leads: it will never accept the
CXS title, Tavily title, or snippet instead of fetched job metadata and the
existing strict identity validator. Closed/blocked or missing CXS listings
remain unverified, and no JD is fabricated.

LinkedIn individual-posting links can redirect to expired-job search/category
pages (for example, `/jobs/lead-data-analyst-jobs?trk=expired_jd_redirect`).
Even a 1,000-word page in that situation is **not** a job description for the
original posting. Source fetching rejects such redirects before extracting
HTML; the enrichment boundary independently rejects them even when alternate
extractors report `enriched`. LinkedIn source descriptions now require
fetched title/company evidence, and an unsuccessful employer/ATS lookup can
no longer label a source-page description as coming from `search`. Generic
employer `/careers/join-our-team` URLs also cannot enter official posting
candidate validation. These guards do not automatically modify existing
local descriptions saved by an older run: audit or back them up and correct
them separately if they came from an expired redirect.

When a valid individual LinkedIn URL returns HTTP 200 but omits
`JobPosting` JSON-LD title/employer metadata, the source may still contain
the actual visible job top card. The extractor now reads posting-owned
`h1.top-card-layout__title` and employer-link elements **only** for specific
`/jobs/view/<numeric-id>` URLs, rejecting conflicting canonical posting
IDs. It will only accept a description from the posting's own description
container when using this fallback, never from a generic `main` with
recommendations or navigation. The fetched company/title still go through
normal identity validation; source URL, expected title, snippets, and
`og:title` are not substituted for missing posting metadata. LinkedIn
pages without usable posting-owned metadata still reject safely.


### Offline audit: expired LinkedIn descriptions (read-only)

To scan **all stored nonempty LinkedIn descriptions** for expired-job
redirects, generic search-results text, and legacy source-identity bypasses,
stop Streamlit/other DuckDB writers and run:

```bash
python -m ops.audit_linkedin_expired_descriptions --report data/linkedin-expired-descriptions-audit.json
```

The scanner opens the existing DuckDB in `read_only=True` mode and makes
**zero Tavily, Serper, or other HTTP requests**. It does not migrate, alter,
delete, quarantine, or score any row. A JSON report contains classified
findings, exact record keys, URL provenance, counts, evidence markers, and
only a 200-character description preview. It does **not** export full JDs.

Classes are `confirmed_expired_search_page` (unsafe enriched LinkedIn redirect
**and** multiple generic listing markers), `likely_search_results_contamination`
(listing markers without matching redirect proof), `review_unsafe_posting_redirect`,
and `review_legacy_unverified_linkedin` (an old identity bypass). Only the
first is strong combined evidence; other classes require manual review before
any cleanup. `no_known_contamination_signal` is **not** proof that a posting
is correct. Export the report and inspect flagged records before considering
separate, backed-up repairs.

You can audit another database copy without touching the active DB by
providing `--database /path/to/jobs.duckdb`.



### Quarantine confirmed expired LinkedIn descriptions (guarded)

After reviewing `data/linkedin-expired-descriptions-audit.json`, you can quarantine
**only** the `confirmed_expired_search_page` group without deleting postings
or modifying any unconfirmed/likely rows. This is separate from the audit and
must be requested explicitly. **Stop Streamlit and other DuckDB users first.**

1. Review the current database against the original report (zero API credits,
   **read-only**):

```bash
python -m ops.quarantine_confirmed_linkedin_descriptions --audit-report data/linkedin-expired-descriptions-audit.json --expected-count 272 --plan-report data/linkedin-quarantine-dry-run.json
```

Inspect `ready_to_apply`, `eligible_for_quarantine` and
`safety_failures` in the plan. The expected count is deliberately explicit.
The command independently rechecks every current description, original and
redirected URL, enrichment status, job identity and search-listing markers,
against the audit record. Any changed or independently verified posting
prevents **all** changes.

2. Only after the dry run reports `ready_to_apply: true`, explicitly
   authorize the same reviewed count. This invocation first copies the
   **entire** closed DuckDB file under `data/quarantine/`, verifies its SHA-256
   checksum, and refuses to proceed if a `.wal` exists. It then rechecks
   all original descriptions and metadata and updates the confirmed records
   in **one transaction**:

```bash
python -m ops.quarantine_confirmed_linkedin_descriptions --audit-report data/linkedin-expired-descriptions-audit.json --expected-count 272 --apply --confirm-count 272 --plan-report data/linkedin-quarantine-applied.json
```

For every confirmed record, the script sets the inaccurate `raw_jobs.description`
to NULL and updates `description_updated_at` to invalidate older cached
scores. It changes the corresponding enrichment attempt to
`resolution_rejected`, clears misleading extraction metadata and description
hash, but **retains** original URLs, job record, attempted official URL/status,
attempt counts and other history. It does not delete AI score history, call
search APIs, reuse unsupported Workday results or automatically rescore jobs.
The original database remains recoverable from the checksum-verified full
backup. Resume normal jobs only after reviewing the outcome.

### Second-pass high-confidence LinkedIn listings cleanup (PR #26)

The original audit treated generic LinkedIn sign-in/alert chrome as potentially
contaminated, even when the stored page was an individual job posting.
The updated audit identifies a narrower `high_confidence_search_results_page`
category by the **generic search-results header, alert-creation text, and
numbered jobs-in-location listing**, independent of the most recent enrichment
attempt status. Legitimate individual job pages with sign-in prompts but
without that listing header remain in the review-only categories. It also
recognizes generic Spanish-region LinkedIn listings if the visible results-page
text has the same structural format.

Do **not** reuse the original 1,323-record audit for this second cleanup.
After stopping Streamlit and all DuckDB users, generate a fresh report:

```bash
python -m ops.audit_linkedin_expired_descriptions --report data/linkedin-expired-descriptions-pass2.json
```

For the local snapshot reviewed on October 16, 2026, the expected new category
contains **41** records (36 with expired redirects and 5 with a later
reset-to-job-page URL). Counts might differ if the database has changed. If
the new count is not exactly 41, stop and review the report before continuing.
The next command only reads the database and writes a dry-run JSON plan:

```bash
python -m ops.quarantine_confirmed_linkedin_descriptions --audit-report data/linkedin-expired-descriptions-pass2.json --target-class high_confidence_search_results_page --expected-count 41 --plan-report data/linkedin-pass2-dry-run.json
```

If and **only if** all 41 records pass independent content, URL, status,
source and verified-key checks (`ready_to_apply: true`, zero safety failures),
run the explicit opt-in command:

```bash
python -m ops.quarantine_confirmed_linkedin_descriptions --audit-report data/linkedin-expired-descriptions-pass2.json --target-class high_confidence_search_results_page --expected-count 41 --apply --confirm-count 41 --plan-report data/linkedin-pass2-applied.json
```

The code refuses `.wal` files, creates a new SHA-256-checked full database
backup, revalidates every candidate in a single transaction, and clears only
this explicitly selected class. It handles historical `enriched`,
`not_improved`, and `blocked` attempt statuses without trusting them as
proof of a correct JD. The original job rows, URLs, source attempts, and
historical score rows remain; clearing descriptions and bumping their
timestamps makes previously cached scores stale. Zero paid API requests.
