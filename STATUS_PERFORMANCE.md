# Streamlit status performance

Jobs are owned by Streamlit session state, not a process-wide cache. Ordinary
filters, pagination, page-size changes and status changes reuse the loaded data.
Mark applied/removed use on_click callbacks: persistence precedes one normal
rerun, and every affected cached status scope is patched before filtering.

The repository still identifies duplicates with exact_posting_identity. It writes
one INSERT SELECT ON CONFLICT operation and returns thin status overlays. Exact
status overrides trusted verified status, including the original timestamp and
record-key tie-break rules. Third-party/untrusted keys never gain propagation.
No description, score, identity, application-status policy or actionability rule
is changed. The callback performs small scope/status reads, not recommendations,
AI admission reconstruction, provider requests or an explicit rerun.

The session retains initially loaded hidden display/admission candidates, so a
status change can reveal a legitimate historical counterpart or change the
representative card. Existing admission/source rules are evaluated once for NEW
at load time; historical preservation and the same sorting/dedup apply in memory.
No fuzzy status matching is introduced. Queue ranks also update in memory.

Snapshot invalidation/revision rules:

- Successful ingestion with inserted jobs: invalidate jobs, sidebar and Operations.
- Enrichment with processed attempts: invalidate descriptions/provenance views.
- Scoring with saved scores or automatic enrichment: invalidate; a changed resume
  hash also invalidates. No-op scoring reuses data.
- Changed requested resume hash: reload once; cache counts per resume/threshold.
- Refresh saved jobs: explicitly reload all data. Use this after out-of-band CLI
  or another session changes the database; no blind global caching is used.
- Successful actionability writes: synchronize the already-patched rows, advance
  the view revision, and reuse data. Network trigger policy is unchanged.
- Mark applied/removed: patch every affected exact/trusted scope, advance the view
  revision; never invalidate/reload recommendations.
- Partial pipeline/persistence errors: conservatively invalidate for recovery.

Sidebar counts retain the exact eligible candidate scope list returned by the
existing candidate loader. Applied/Removed removes the written exact scope from
that list. Verified-inherited statuses are not blindly subtracted because the
existing candidate count uses exact persisted scopes. Cached score counts stay
unchanged. The legacy NEW restoration path recounts sidebar eligibility only.

Operations accepts the current visible jobs and cached unscored count. Expensive
raw/exact/enrichment SQL is session-cached per data/resume/threshold version;
status-only revisions rederive dictionary counts. Review classification shares
one fresh policy read per batch; no global jobs/config cache weakens freshness.

Deterministic performance instrumentation uses Streamlit AppTest with mocked DB
and network boundaries, plus real temporary-DuckDB correctness tests. Expected
saved-jobs call counts:

| Operation | Initial before → after | Mark Applied/Removed before → after |
| --- | --- | --- |
| load_all_jobs | 2 → 1 | 3 → 0 |
| Operations SQL loader | 1 → 1 | 1 → 0 |
| Sidebar count loader | 1 → 1 | 2 → 0 |
| Status write operation | — | 1 → 1 |
| Script runs per status interaction | — | 2 → 1 |
| Actionability network calls | — | 0 → 0 |

The first click run previously stopped before Operations at explicit st.rerun;
its second run then loaded Jobs and Operations, totaling three dataset loads.
Cold loads, actual pipeline mutation refreshes, and changed scoring thresholds
still require repository work. Status persistence still scans exact scopes and
reads thin status/provenance rows. Filtering/sorting/rendering remain Python/UI
work. Mocked call-count results are not a production latency benchmark. Automated
tests never write the historical production database.
