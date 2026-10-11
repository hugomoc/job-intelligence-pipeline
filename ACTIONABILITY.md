# Current actionability

Fit and current application eligibility are separate. A current complete AI
assessment first passes the existing fit rules. Only fresh ACTIVE results allow
new jobs into Recommended. INACTIVE, LOCATION_MISMATCH and UNKNOWN are visible in
Needs review with their fit scores preserved. Applied/Removed history is unchanged.

Successful Streamlit Score jobs and Score unscored jobs workflows (including the
combined Ingest → Enrich → Score daily workflow) automatically check up to 25 serious
new candidates. The explicit Check current postings button checks the next pending
batch. Ordinary rendering, filters and pagination only read persisted results and
never trigger network requests. Low-fit, SKIP and historical jobs are not fetched.
Fresh UNKNOWN results also consume no new checks. Successive workflow/manual checks
converge through pending jobs, prioritizing APPLY then descending fit score.
Each triggered batch can take up to the cap times the 12-second fetch timeout.

`defaults.actionability` in `config/searches.yml` configures candidate_country,
ttl_hours (24) and batch_size (25). Candidate geography is configuration, not personal
information in code. Missing geography cannot establish ACTIVE. Fresh UNKNOWN
results are cached too. URL/key/trust/context changes or TTL expiry invalidate
cached results. Rows are scoped to record key, trusted exact posting key, official
URL and country; no cross-record actionability reuse occurs.

The lazily created `job_actionability` table stores status, check time, reason,
bound identity/context and fetched evidence JSON. Repository reads neither create
this table nor fetch pages. UI refresh writes each result individually. Existing
jobs, enrichment statuses, posting_status, application_status, scores and
descriptions are not updated. No historical migration is performed automatically
by the implementation task; the normal UI has a bounded daily refresh.

ACTIVE requires exact trusted key/URL continuity, independent fetched identity
validation and an enabled application control. Authoritative geography conflicts
veto actionability. Missing/ambiguous location is not a mismatch. HTTP 200 is not
proof. Canonical/requisition conflicts and redirects to other postings or search
pages are UNKNOWN. Explicit ATS closure at the trusted exact URL or an expired
JobPosting validThrough produces INACTIVE. HTTP 404 alone, blocks, rate limits and
network failures remain UNKNOWN. Identity thresholds and trust rules are unchanged.

Metrics report targets, checks, cache hits, deferred jobs, states, blocked/network
failures, and otherwise recommended jobs redirected to review or excluded for
inactivity/geography. Excluded from Recommended never means application_status=removed.

Limitations: JavaScript-only pages without authoritative identity or application
controls remain UNKNOWN. Legacy official URLs without trusted key provenance remain
UNKNOWN. Country compatibility does not prove work authorization or timezone fit.
No expected/search metadata is promoted to fetched authority to improve coverage.

Historical targeted enrichment was fully drained before this branch: eligible,
needs_description and needs_official_resolution were all zero. This feature makes
no decision based on the earlier 909-job count. Shared third-party classification
rejects evidenced Built In domains (builtin.com and builtinsf.com), prevents trusted
key generation/reuse, and preserves legacy diagnostic review/history without
rewriting their database rows. No other Built In locale domains are assumed.
