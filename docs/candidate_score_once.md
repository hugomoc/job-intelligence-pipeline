# Candidate-owned completed assessments

Resume file hashes identify input artifacts, not people. Candidate UUIDs are
registered once with normalized first/last name and an optional normalized
LinkedIn /in/ URL. Names never automatically merge people. Resume associations
require explicit confirmation, and a resume cannot belong to two candidates.
Uploading a linked candidate's new resume makes it the input for new jobs;
completed assessments stay authoritative without a version selector or badges.

Five additive tables hold candidates, confirmed resume associations, immutable
assessment snapshots, active assessment pointers, and scoring claims. Existing
resume_job_scores, profiles, statuses and scoring logs are never backfilled by
application startup. Unmigrated installations retain their legacy read path
until explicit registration/migration; no unconfirmed historical resumes are
silently imported. The migration does not update or delete existing table rows.

Each completed assessment is scoped to the existing trusted verified posting
identity, or to exact source posting identity when verification is unavailable.
Fingerprint/title similarity supplies no identity proof. Workday tenant scoping
and exact verified URL/key agreement use the existing implementation. Completion
requires a full-description result and a valid score/recommendation. Failures
and incomplete records do not become completed assessments.

Automatic screening/candidate queues exclude completed candidate jobs before
LIMIT. A database uniqueness constraint claims the candidate/job before any
scoring call, including provider fallback. Another process cannot open a
conflicting DuckDB writer; in-process duplicate claims fail closed. Claims have
no automatic expiry: a crashed run's retained claim must be inspected and
released explicitly before retry. A normal repeat cannot replace a completed
assessment, regardless of resume, model, prompt or description timestamp.

Manual "Rescore this job" is the only replacement path. It uses the candidate's
latest associated resume, appends a new snapshot, and changes the active pointer
in one transaction after success. The old source row and assessment remain.
Provider errors release the claim and leave the active result unchanged.

Streamlit overlays candidate assessments from source tables over resume-scoped
legacy SQL/marts, so dbt refresh is not needed for authoritative score display.
Candidate revision participates in session cache invalidation. Existing status
callbacks and verified display grouping remain unchanged. Cards sort by score
descending (null last), ingestion `raw_jobs.discovered_at` descending, and stable
posting identity ascending. Default date controls span the inventory; explicit
user filters remain. New resume admission cannot hide a completed assessment.
The optional mart_candidate_assessments derived table exposes active candidate
results for analytics, while existing marts remain backward compatible.

## Migration procedure

Use `python -m scripts.migrate_candidate_assessments --help`. A preview requires
explicit name, optional LinkedIn and each confirmed `--resume-hash`. Private
manifest and backup paths must be outside this repository. The preview creates
a readable timestamped backup, records its SHA-256 and fingerprints every
existing table, and records every original score column. Incomplete scores are
reported separately. Duplicate groups are shown in full. Active selection is
latest completed scoring timestamp; ties use record key, resume hash and source
checksum, never maximum score. All prior completed assessments are preserved.

Stop for explicit user approval. Only then run the separate `--apply MANIFEST`
mode with the same `--database`. Apply verifies preview drift and backup,
creates the additive schema in one transaction, validates all associations and
counts, and compares every pre-existing table fingerprint before commit. Any
failure rolls back. Keep the backup. Never run scoring as part of migration.

The initial implementation is a local application, not an authentication or
multi-tenant access-control system. Candidate IDs isolate score queries; this
must not be presented as remote-user authorization.

## Automatic queue safeguards

A compatible exact source-posting alias reuses a completed verified assessment.
Claims use the unique trusted verified scope even when the selected raw sibling
has no verification metadata. Competing positive requisitions never share an
assessment; an unverified sibling with conflicting proof is held. Existing
assessment rows and active pointers are not consolidated or promoted by lookup.

Automatic selectors use the display's exact-status-first, trusted-verified-status
fallback, including explicit New overrides. Applied and Removed openings are
excluded before queue limits. A claim rechecks status before provider calls.

Failed eligibility screens are reported with sanitized reasons and record keys,
and held out of the current execution. They are not cached as completed decisions;
a later controlled run can retry them. Genuinely unscreened jobs retain their
existing admission policy. Screening failure counts are separate from fit-score
failure counts. Selected fit-scoring positions reconcile to saved, failed and
skipped outcomes, with identity and reason recorded for skips and unattempted
positions after a provider stop.

After the existing bounded Gemini score-request retries exhaust HTTP 503,
the existing OpenAI fallback may take over. Validation/policy errors and generic
minute-rate-limit errors do not trigger that switch. No prompts, weights,
requirement rules, historical evidence or retry limits change.
