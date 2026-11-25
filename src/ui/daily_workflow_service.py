"""Service layer behind the Streamlit daily-operations buttons.

The UI calls these functions for ingest, enrichment, rule matching, and backlog
scoring. Keeping this orchestration outside streamlit_app.py makes it easier to
test and keeps raw exception details out of the browser.
"""

from __future__ import annotations

import contextlib
import io
from dataclasses import dataclass

from src.targeted_enrichment import plan_daily_enrichment, plan_recent_hidden_recovery
from src.drain_enrichment_backlog import RateLimitGuard
from src.enrichment.official_job_resolver import PublicSearchCircuit

from src.config_loader import load_searches, load_sources
from src.database import (
    get_processed_email_count,
    get_raw_jobs,
)
from src.enrich_jobs import (
    DEFAULT_ENRICHMENT_LIMIT,
    create_http_client,
    load_jobs_to_enrich,
    process_enrichment_batch,
    annotate_known_official_candidates,
    load_verified_reuse_candidates,
)
from src.ingest_all import ingest_source
from src.matching.job_matcher import score_all_jobs
from src.score_jobs import (
    load_raw_jobs,
    refresh_job_matches,
)
from src.score_backlog import BacklogSummary, score_backlog
from src.score_backlog import load_latest_resume_hash
from src.scoring_run_repository import (
    finish_scoring_run,
    start_scoring_run,
)
from src.ai.resume_matcher import MATCHER_PROMPT_VERSION
from src.ai.resume_profiler import get_model_name
from src.repositories.recommendation_repository import (
    count_cached_canonical_scores,
    count_unscored_candidate_jobs,
    load_candidate_jobs,
)


@dataclass(frozen=True)
class IngestionSummary:
    inserted_jobs: int
    duplicate_jobs: int
    skipped_emails: int
    rule_matches: int
    processed_emails: int
    total_jobs: int
    log_lines: tuple[str, ...]


@dataclass(frozen=True)
class EnrichmentSummary:
    jobs_selected: int
    jobs_processed: int
    eligible_for_enrichment: int
    descriptions_updated: int
    enriched: int
    blocked: int
    no_description: int
    fetch_error: int
    invalid_url: int
    not_improved: int
    rule_matches_refreshed: int
    log_lines: tuple[str, ...]
    never_attempted: int = 0
    previously_attempted: int = 0
    needs_official_resolution: int = 0
    stopped_for_time_budget: bool = False
    elapsed_seconds: float = 0.0
    persistence_error: int = 0
    queue_statistics: dict | None = None
    search_requests: int = 0
    search_request_limit: int = 0
    search_circuit: dict | None = None
    recovery_record_keys: tuple[str, ...] | None = None


@dataclass(frozen=True)
class UiBacklogSummary:
    resume_hash: str
    model_name: str
    candidates_selected: int
    already_scored: int
    scores_saved: int
    jobs_failed: int
    gemini_scores_saved: int
    openai_scores_saved: int
    quota_exhausted: bool
    dbt_was_run: bool
    log_lines: tuple[str, ...]
    enrichment_jobs_processed: int = 0
    jobs_skipped: int = 0
    screening_failures: int = 0
    recovery_record_keys: tuple[str, ...] | None = None


@dataclass(frozen=True)
class ScoringBacklogStatus:
    resume_hash: str | None
    cached_scores: int
    unscored_candidates: int
    candidate_posting_keys: tuple[str, ...] | None = None


class DailyWorkflowError(Exception):
    """Raised when the UI daily workflow cannot complete safely."""


def capture_stdout_lines(function, *args, **kwargs):
    """Run a CLI-style function and return its printed lines for the UI log."""
    buffer = io.StringIO()

    with contextlib.redirect_stdout(buffer):
        result = function(*args, **kwargs)

    log_lines = tuple(
        line.strip()
        for line in buffer.getvalue().splitlines()
        if line.strip()
    )

    return result, log_lines


def run_email_ingestion() -> IngestionSummary:
    """Read configured folders, parse emails, store jobs, and refresh rules."""
    try:
        sources = load_sources()

        inserted_jobs = 0
        duplicate_jobs = 0
        skipped_emails = 0
        log_lines: list[str] = []

        for source in sources:
            (
                (
                    inserted,
                    duplicates,
                    emails_skipped,
                ),
                source_log_lines,
            ) = capture_stdout_lines(
                ingest_source,
                source,
            )

            log_lines.extend(source_log_lines)
            inserted_jobs += inserted
            duplicate_jobs += duplicates
            skipped_emails += emails_skipped

        defaults, searches = load_searches()
        jobs_for_matching = load_raw_jobs()
        match_results = []

        if inserted_jobs > 0 and searches and jobs_for_matching:
            match_results = score_all_jobs(
                jobs=jobs_for_matching,
                searches=searches,
                defaults=defaults,
            )
            refresh_job_matches(match_results)
        elif inserted_jobs == 0:
            log_lines.append(
                "No new jobs inserted; skipped rule-match refresh."
            )

        stored_jobs = get_raw_jobs()

        return IngestionSummary(
            inserted_jobs=inserted_jobs,
            duplicate_jobs=duplicate_jobs,
            skipped_emails=skipped_emails,
            rule_matches=len(match_results),
            processed_emails=get_processed_email_count(),
            total_jobs=len(stored_jobs),
            log_lines=tuple(log_lines),
        )

    except Exception as error:
        print(
            "Email ingestion failed: "
            f"{type(error).__name__}: {error}"
        )
        raise DailyWorkflowError(
            "Email ingestion could not be completed. Please try again later."
        ) from error


def refresh_rule_matches() -> int:
    defaults, searches = load_searches()
    jobs_for_matching = load_raw_jobs()

    if not searches or not jobs_for_matching:
        return 0

    match_results = score_all_jobs(
        jobs=jobs_for_matching,
        searches=searches,
        defaults=defaults,
    )
    refresh_job_matches(match_results)

    return len(match_results)


def load_daily_enrichment_plan(*, limit, minimum_words=80, source=None,
                               retry_failed=True, resume_hash=None,
                               actionability_only=False, recent_hidden_days=None):
    """Full stored inventory, then the SAME pure selector for UI and execution.

    No generic 10,000-row truncation, no authoritative-reuse writes. Production
    initializers remain normal; read-only replay suppresses migrations explicitly.
    """
    inventory = load_jobs_to_enrich(
        limit=limit, minimum_words=minimum_words, source=source,
        retry_failed=retry_failed, force=False, resume_hash=resume_hash,
        deduplicate=False, inventory_only=True,
    )
    if recent_hidden_days is not None:
        from src.repositories.recommendation_repository import load_all_jobs, prepare_job_display
        snapshot = load_all_jobs(resume_hash=resume_hash, for_session_snapshot=True)
        # Recovery targets jobs awaiting employer verification: "hidden" means
        # absent from the trusted-source set (every relevant job is now visible).
        from src.ui.job_visibility import passes_source_trust_gate
        visible = [job for job in prepare_job_display(snapshot) if passes_source_trust_gate(job)]
        selected, stats = plan_recent_hidden_recovery(inventory, visible, limit,
            days=recent_hidden_days, retry_failed=retry_failed, minimum_words=minimum_words)
    else:
        selected, stats = plan_daily_enrichment(inventory, limit, retry_failed=retry_failed,
                                               minimum_words=minimum_words,
                                               actionability_only=actionability_only)
    for configured in load_sources():
        stats['pending_employer_by_source'].setdefault(configured['source_id'], 0)
        stats['pending_employer_all_new_by_source'].setdefault(configured['source_id'], 0)
        if 'recovery_by_source' in stats:
            stats['recovery_by_source'].setdefault(configured['source_id'], dict(hidden=0, relevant=0,
                employer_verification=0, description_enrichment=0, activity_checks_due=0, cooldown=0, runnable=0, selected=0))
    return selected, stats


def run_description_enrichment(
    limit: int,
    minimum_words: int = 80,
    source: str | None = None,
    retry_failed: bool = True,
    resume_hash: str | None = None,
    max_seconds: float | None = None,
    search_request_limit: int = 25,
    actionability_only: bool = False,
    recent_hidden_days: int | None = None,
) -> EnrichmentSummary:
    """Fetch richer descriptions for stored jobs without changing source rows."""
    try:
        resume_hash = resume_hash or load_latest_resume_hash(get_model_name())
        if limit < 1 or search_request_limit < 0:
            raise ValueError("Positive batch size and nonnegative search budget required")
        jobs, queue_summary = load_daily_enrichment_plan(
            limit=limit, minimum_words=minimum_words, source=source,
            retry_failed=retry_failed, resume_hash=resume_hash,
            actionability_only=actionability_only, recent_hidden_days=recent_hidden_days,
        )
        log_lines: list[str] = [
            f"Jobs selected for enrichment/employer verification: {len(jobs)}",
            "Runnable queue: " + str(queue_summary),
        ]
        # Hints are discovery inputs only. Inventory inspection never copies
        # descriptions or writes attempts. The existing resolver verifies hints.
        if jobs:
            candidates = load_verified_reuse_candidates(minimum_words=minimum_words)
            from src.enrichment.employer_careers import remember_verified_careers
            for candidate in candidates:
                remember_verified_careers(candidate)
            annotate_known_official_candidates(
                jobs, candidates)
        from src.enrichment.public_search import SearchRequestBudget
        budget = SearchRequestBudget(search_request_limit)
        circuit = PublicSearchCircuit()
        guard = RateLimitGuard()
        with create_http_client() as client:
            client.search_request_budget = budget
            client.event_hooks.setdefault("response", []).append(guard.observe)
            batch_summary = process_enrichment_batch(
                jobs=jobs,
                client=client,
                delay_seconds=0.5,
                max_seconds=max_seconds,
                search_circuit=circuit,
                rate_limit_guard=guard,
            )

        from src.enrichment.public_search import provider_metrics
        counts = provider_metrics(client)
        tavily_calls = int(counts.get("tavily", {}).get("search_requests", 0))
        serper_calls = int(counts.get("serper", {}).get("search_requests", 0))
        circuit_statistics = circuit.summary()
        # Historical circuit.calls counts logical invocations (including cache
        # hits), while the dispatch budget counts actual provider requests.
        circuit_statistics['provider_invocations'] = circuit_statistics.pop('calls_attempted')
        circuit_statistics['budget_deferrals'] = circuit.budget_deferred
        circuit_statistics['provider_unavailable_deferrals'] = circuit.deferred - circuit.budget_deferred
        log_lines.extend(batch_summary.log_lines)
        log_lines.append(
            f"Public search API calls: Tavily={tavily_calls}; "
            f"Serper={serper_calls}; total={budget.attempted}/{budget.maximum}; "
            f"enrichment jobs processed={batch_summary.jobs_processed}"
        )

        rule_matches_refreshed = 0

        if batch_summary.descriptions_updated:
            rule_matches_refreshed = refresh_rule_matches()

        return EnrichmentSummary(
            jobs_selected=len(jobs),
            jobs_processed=batch_summary.jobs_processed,
            eligible_for_enrichment=(
                queue_summary["runnable"]
            ),
            descriptions_updated=batch_summary.descriptions_updated,
            enriched=batch_summary.totals["enriched"],
            blocked=batch_summary.totals["blocked"],
            no_description=batch_summary.totals["no_description"],
            fetch_error=batch_summary.totals["fetch_error"],
            persistence_error=batch_summary.totals.get("persistence_error", 0),
            invalid_url=batch_summary.totals["invalid_url"],
            not_improved=batch_summary.totals["not_improved"],
            rule_matches_refreshed=rule_matches_refreshed,
            log_lines=tuple(log_lines),
            never_attempted=queue_summary["fresh_description"],
            previously_attempted=queue_summary["eligible_deferred_retries"],
            needs_official_resolution=(
                queue_summary["employer_verification_pending"]
            ),
            stopped_for_time_budget=(
                batch_summary.stopped_for_time_budget
            ),
            elapsed_seconds=batch_summary.elapsed_seconds,
            queue_statistics=queue_summary,
            search_requests=budget.attempted,
            search_request_limit=budget.maximum,
            search_circuit=circuit_statistics,
            recovery_record_keys=queue_summary.get("recovery_record_keys"),
        )

    except Exception as error:
        print(
            "Description enrichment failed: "
            f"{type(error).__name__}: {error}"
        )
        raise DailyWorkflowError(
            "Job descriptions could not be enriched. Please try again later."
        ) from error


def automatic_enrichment_limit(
    scoring_limit: int,
) -> int:
    """Run enrichment broadly enough before the narrower AI scoring queue."""
    return max(
        max(scoring_limit, 1),
        DEFAULT_ENRICHMENT_LIMIT,
    )


def run_unscored_job_backlog(
    resume_hash: str | None,
    limit: int,
    minimum_rule_score: int,
    workflow: str = "score_unscored",
    enrichment_summary: EnrichmentSummary | None = None,
    enrichment_limit: int | None = None,
    retry_failed: bool = True,
    search_request_limit: int = 25,
    recent_hidden_days: int | None = None,
) -> UiBacklogSummary:
    selected_resume_hash = (
        resume_hash
        or load_latest_resume_hash(
            get_model_name()
        )
    )
    run_id = (
        start_scoring_run(
            resume_hash=selected_resume_hash,
            workflow=workflow,
        )
        if selected_resume_hash
        else None
    )

    try:
        if enrichment_summary is None:
            enrichment_summary = run_description_enrichment(
                limit=enrichment_limit or automatic_enrichment_limit(limit),
                minimum_words=80, resume_hash=resume_hash,
                retry_failed=retry_failed, search_request_limit=search_request_limit,
                recent_hidden_days=recent_hidden_days,
            )
        if recent_hidden_days is not None and enrichment_summary.recovery_record_keys is None:
            raise ValueError("Recent recovery requires an explicit record scope")
        summary, log_lines = capture_stdout_lines(
            score_backlog,
            resume_hash=resume_hash,
            limit=limit,
            minimum_rule_score=minimum_rule_score,
            **({"record_keys": enrichment_summary.recovery_record_keys}
               if enrichment_summary.recovery_record_keys is not None else {}),
        )
        combined_log_lines = (
            (
                "Automatic description enrichment",
                f"Jobs selected: {enrichment_summary.jobs_selected}",
                f"Jobs processed: {enrichment_summary.jobs_processed}",
                (
                    "Eligible for enrichment: "
                    f"{enrichment_summary.eligible_for_enrichment}"
                ),
                f"Never attempted: {enrichment_summary.never_attempted}",
                (
                    "Needs official lookup: "
                    f"{enrichment_summary.needs_official_resolution}"
                ),
                f"Descriptions updated: {enrichment_summary.descriptions_updated}",
                f"Pages blocked: {enrichment_summary.blocked}",
                f"No description found: {enrichment_summary.no_description}",
                f"Fetch errors: {enrichment_summary.fetch_error}",
                f"Persistence errors: {enrichment_summary.persistence_error}",
                f"Not improved: {enrichment_summary.not_improved}",
                "",
            )
            + enrichment_summary.log_lines
            + ("", "AI scoring")
            + log_lines
        )

        result = UiBacklogSummary(
            resume_hash=summary.resume_hash,
            model_name=summary.model_name,
            candidates_selected=summary.candidates_selected,
            already_scored=summary.already_scored,
            scores_saved=summary.scores_saved,
            jobs_failed=summary.jobs_failed,
            gemini_scores_saved=summary.gemini_scores_saved,
            openai_scores_saved=summary.openai_scores_saved,
            quota_exhausted=summary.quota_exhausted,
            dbt_was_run=summary.dbt_was_run,
            log_lines=combined_log_lines,
            enrichment_jobs_processed=enrichment_summary.jobs_processed,
            jobs_skipped=getattr(summary, "jobs_skipped", 0),
            screening_failures=getattr(summary, "screening_failures", 0),
            recovery_record_keys=enrichment_summary.recovery_record_keys,
        )

        if run_id:
            finish_scoring_run(
                run_id,
                status="completed",
                model_name=summary.model_name,
                candidates_selected=summary.candidates_selected,
                already_scored=summary.already_scored,
                scores_saved=summary.scores_saved,
                jobs_failed=summary.jobs_failed,
                gemini_scores_saved=summary.gemini_scores_saved,
                openai_scores_saved=summary.openai_scores_saved,
                quota_exhausted=summary.quota_exhausted,
                log_lines=combined_log_lines,
            )

        return result

    except Exception as error:
        if run_id:
            finish_scoring_run(
                run_id,
                status="failed",
                model_name=get_model_name(),
                candidates_selected=0,
                already_scored=0,
                scores_saved=0,
                jobs_failed=0,
                gemini_scores_saved=0,
                openai_scores_saved=0,
                quota_exhausted=False,
                log_lines=(
                    "Scoring workflow failed before completion.",
                ),
            )

        print(
            "Backlog scoring failed: "
            f"{type(error).__name__}: {error}"
        )
        raise DailyWorkflowError(
            "Unscored jobs could not be scored. Please try again later."
        ) from error


def load_scoring_backlog_status(
    resume_hash: str | None,
    minimum_rule_score: int,
) -> ScoringBacklogStatus:
    model_name = get_model_name()
    selected_resume_hash = resume_hash or load_latest_resume_hash(
        model_name=model_name,
    )

    from src.candidate_assessments import latest_candidate_resume
    from src import database
    if selected_resume_hash:
        with database.get_connection() as connection:
            selected_resume_hash = latest_candidate_resume(connection, selected_resume_hash)

    if selected_resume_hash is None:
        return ScoringBacklogStatus(
            resume_hash=None,
            cached_scores=0,
            unscored_candidates=0,
        )

    # The existing count helper ultimately counts this exact list. Keep its
    # scope membership so NEW→Applied/Removed can update the session count
    # without rebuilding candidates on every status interaction.
    candidates = load_candidate_jobs(
        resume_hash=selected_resume_hash, model_name=model_name, limit=100000,
        minimum_rule_score=minimum_rule_score, prompt_version=MATCHER_PROMPT_VERSION,
        reuse_any_model=True,
    )
    return ScoringBacklogStatus(
        resume_hash=selected_resume_hash,
        cached_scores=count_cached_canonical_scores(
            resume_hash=selected_resume_hash,
            model_name=model_name,
            prompt_version=MATCHER_PROMPT_VERSION,
            reuse_any_model=True,
        ),
        unscored_candidates=len(candidates),
        candidate_posting_keys=tuple(job['canonical_job_key'] for job in candidates),
    )
