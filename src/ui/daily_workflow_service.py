"""Service layer behind the Streamlit daily-operations buttons.

The UI calls these functions for ingest, enrichment, rule matching, and backlog
scoring. Keeping this orchestration outside streamlit_app.py makes it easier to
test and keeps raw exception details out of the browser.
"""

from __future__ import annotations

import contextlib
import io
from dataclasses import dataclass

from src.config_loader import load_searches, load_sources
from src.database import (
    get_processed_email_count,
    get_raw_jobs,
)
from src.enrich_jobs import (
    create_http_client,
    fetch_job_description,
    load_jobs_to_enrich,
    save_enrichment_attempt,
    update_job_description,
    validate_enrichment_identity,
)
from src.ingest_all import ingest_source
from src.matching.job_matcher import score_all_jobs
from src.score_jobs import (
    load_raw_jobs,
    refresh_job_matches,
)
from src.score_backlog import BacklogSummary, score_backlog
from src.score_backlog import load_latest_resume_hash
from src.ai.resume_matcher import MATCHER_PROMPT_VERSION
from src.ai.resume_profiler import get_model_name
from src.repositories.recommendation_repository import (
    count_cached_canonical_scores,
    count_unscored_candidate_jobs,
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
    descriptions_updated: int
    enriched: int
    blocked: int
    no_description: int
    fetch_error: int
    invalid_url: int
    not_improved: int
    rule_matches_refreshed: int
    log_lines: tuple[str, ...]


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


@dataclass(frozen=True)
class ScoringBacklogStatus:
    resume_hash: str | None
    cached_scores: int
    unscored_candidates: int


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
) -> EnrichmentSummary:
    """Fetch richer descriptions for stored jobs without changing source rows."""
    try:
        jobs = load_jobs_to_enrich(
            limit=limit,
            minimum_words=minimum_words,
            source=source,
            retry_failed=retry_failed,
            force=False,
            resume_hash=resume_hash,
        )

        totals = {
            "enriched": 0,
            "blocked": 0,
            "no_description": 0,
            "fetch_error": 0,
            "invalid_url": 0,
            "not_improved": 0,
            "resolution_rejected": 0,
        }
        descriptions_updated = 0
        log_lines: list[str] = [
            f"Jobs selected for description enrichment: {len(jobs)}"
        ]

        with create_http_client() as client:
            for index, job in enumerate(jobs, start=1):
                log_lines.append(
                    "Enriching "
                    f"{index}/{len(jobs)}: "
                    f"{job.get('title') or 'Untitled job'} | "
                    f"{job.get('company_name') or 'Unknown company'}"
                )

                result = fetch_job_description(
                    url=job["apply_url"],
                    client=client,
                )

                updated = False
                stored_status = result.status
                stored_error = result.error_message
                identity_validation = None

                if result.status == "enriched":
                    identity_validation = validate_enrichment_identity(
                        job=job,
                        result=result,
                    )

                    if not identity_validation.accepted:
                        stored_status = "resolution_rejected"
                        stored_error = (
                            "Resolved candidate rejected: "
                            f"{identity_validation.reason}"
                        )
                        log_lines.append(
                            "Rejected enrichment candidate: "
                            f"{result.resolved_title or '<missing title>'} | "
                            f"{result.resolved_company or '<missing company>'}. "
                            f"Reason: {identity_validation.reason}"
                        )

                    else:
                        updated = update_job_description(
                            job=job,
                            result=result,
                        )

                        if updated:
                            descriptions_updated += 1

                        else:
                            stored_status = "not_improved"
                            stored_error = (
                                "The extracted description was not longer "
                                "than the existing description."
                            )

                save_enrichment_attempt(
                    job=job,
                    result=result,
                    status=stored_status,
                    error_message=stored_error,
                    identity_validation=identity_validation,
                )

                totals[stored_status] = (
                    totals.get(stored_status, 0) + 1
                )

                log_lines.append(
                    f"Status: {stored_status}; "
                    f"words: {result.word_count}; "
                    f"updated: {'yes' if updated else 'no'}"
                )

        rule_matches_refreshed = 0

        if descriptions_updated:
            rule_matches_refreshed = refresh_rule_matches()

        return EnrichmentSummary(
            jobs_selected=len(jobs),
            descriptions_updated=descriptions_updated,
            enriched=totals["enriched"],
            blocked=totals["blocked"],
            no_description=totals["no_description"],
            fetch_error=totals["fetch_error"],
            invalid_url=totals["invalid_url"],
            not_improved=totals["not_improved"],
            rule_matches_refreshed=rule_matches_refreshed,
            log_lines=tuple(log_lines),
        )

    except Exception as error:
        print(
            "Description enrichment failed: "
            f"{type(error).__name__}: {error}"
        )
        raise DailyWorkflowError(
            "Job descriptions could not be enriched. Please try again later."
        ) from error


def run_unscored_job_backlog(
    resume_hash: str | None,
    limit: int,
    minimum_rule_score: int,
) -> UiBacklogSummary:
    try:
        enrichment_summary = run_description_enrichment(
            limit=max(limit, 1),
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
                f"Descriptions updated: {enrichment_summary.descriptions_updated}",
                f"Pages blocked: {enrichment_summary.blocked}",
                f"No description found: {enrichment_summary.no_description}",
                f"Fetch errors: {enrichment_summary.fetch_error}",
                f"Not improved: {enrichment_summary.not_improved}",
                "",
            )
            + enrichment_summary.log_lines
            + ("", "AI scoring")
            + log_lines
        )

        return UiBacklogSummary(
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
        )

    except Exception as error:
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

    if selected_resume_hash is None:
        return ScoringBacklogStatus(
            resume_hash=None,
            cached_scores=0,
            unscored_candidates=0,
        )

    return ScoringBacklogStatus(
        resume_hash=selected_resume_hash,
        cached_scores=count_cached_canonical_scores(
            resume_hash=selected_resume_hash,
            model_name=model_name,
            prompt_version=MATCHER_PROMPT_VERSION,
            reuse_any_model=True,
        ),
        unscored_candidates=count_unscored_candidate_jobs(
            resume_hash=selected_resume_hash,
            model_name=model_name,
            minimum_rule_score=minimum_rule_score,
            prompt_version=MATCHER_PROMPT_VERSION,
            reuse_any_model=True,
        ),
    )
