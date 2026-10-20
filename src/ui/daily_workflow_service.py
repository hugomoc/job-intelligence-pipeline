"""Service layer behind the Streamlit daily-operations buttons.

The UI calls these functions for ingest, enrichment, rule matching, and backlog
scoring. Keeping this orchestration outside streamlit_app.py makes it easier to
test and keeps raw exception details out of the browser.
"""

from __future__ import annotations

import contextlib
import io
from dataclasses import dataclass

from src.targeted_enrichment import first_attempt, select_targeted_retries
from src.drain_enrichment_backlog import RateLimitGuard
from src.enrichment.official_job_resolver import PublicSearchCircuit

from src.config_loader import load_searches, load_sources
from src.database import (
    get_processed_email_count,
    get_raw_jobs,
)
from src.enrich_jobs import (
    DEFAULT_ENRICHMENT_LIMIT,
    ENRICHMENT_DIAGNOSTIC_QUEUE_LIMIT,
    create_http_client,
    load_jobs_to_enrich,
    process_enrichment_batch,
    summarize_enrichment_queue,
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


def run_description_enrichment(
    limit: int,
    minimum_words: int = 80,
    source: str | None = None,
    retry_failed: bool = False,
    resume_hash: str | None = None,
    max_seconds: float | None = None,
) -> EnrichmentSummary:
    """Fetch richer descriptions for stored jobs without changing source rows."""
    try:
        resume_hash = resume_hash or load_latest_resume_hash(get_model_name())
        queue_jobs = load_jobs_to_enrich(
            limit=max(
                limit,
                ENRICHMENT_DIAGNOSTIC_QUEUE_LIMIT,
            ),
            minimum_words=minimum_words,
            source=source,
            retry_failed=retry_failed,
            force=False,
            resume_hash=resume_hash,
            deduplicate=False,
        )
        fresh_jobs = [job for job in queue_jobs if first_attempt(job)]
        retries = select_targeted_retries(queue_jobs) if retry_failed else []
        # First attempts are completed independently of the historical retry cap.
        jobs = fresh_jobs + retries[:max(0, limit - len(fresh_jobs))]

        queue_summary = summarize_enrichment_queue(queue_jobs)
        log_lines: list[str] = [
            f"Jobs selected for description enrichment: {len(jobs)}",
            (
                "Queue diagnostics: "
                f"eligible={queue_summary['eligible_for_enrichment']}; "
                f"never attempted={queue_summary['never_attempted']}; "
                f"previously attempted={queue_summary['previously_attempted']}; "
                "needs official lookup="
                f"{queue_summary['needs_official_resolution']}"
            ),
        ]

        guard = RateLimitGuard()
        with create_http_client() as client:
            client.event_hooks.setdefault("response", []).append(guard.observe)
            batch_summary = process_enrichment_batch(
                jobs=jobs,
                client=client,
                delay_seconds=0.5,
                max_seconds=max_seconds,
                search_circuit=PublicSearchCircuit(),
                rate_limit_guard=guard,
            )

        from src.enrichment.public_search import provider_metrics
        counts = provider_metrics(client)
        tavily_calls = int(counts.get("tavily", {}).get("search_requests", 0))
        serper_calls = int(counts.get("serper", {}).get("search_requests", 0))
        log_lines.extend(batch_summary.log_lines)
        log_lines.append(
            f"Public search API calls: Tavily={tavily_calls}; "
            f"Serper={serper_calls}; total={tavily_calls + serper_calls}; "
            f"enrichment jobs processed={batch_summary.jobs_processed}"
        )

        rule_matches_refreshed = 0

        if batch_summary.descriptions_updated:
            rule_matches_refreshed = refresh_rule_matches()

        return EnrichmentSummary(
            jobs_selected=len(jobs),
            jobs_processed=batch_summary.jobs_processed,
            eligible_for_enrichment=(
                queue_summary["eligible_for_enrichment"]
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
            never_attempted=queue_summary["never_attempted"],
            previously_attempted=queue_summary["previously_attempted"],
            needs_official_resolution=(
                queue_summary["needs_official_resolution"]
            ),
            stopped_for_time_budget=(
                batch_summary.stopped_for_time_budget
            ),
            elapsed_seconds=batch_summary.elapsed_seconds,
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
        enrichment_limit = automatic_enrichment_limit(limit)
        enrichment_summary = run_description_enrichment(
            limit=enrichment_limit,
            minimum_words=80,
            resume_hash=resume_hash,
        )
        summary, log_lines = capture_stdout_lines(
            score_backlog,
            resume_hash=resume_hash,
            limit=limit,
            minimum_rule_score=minimum_rule_score,
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
