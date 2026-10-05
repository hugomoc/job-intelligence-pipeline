from __future__ import annotations

from src.enrich_jobs import DEFAULT_ENRICHMENT_LIMIT
from src.score_backlog import BacklogSummary
from src.ui import daily_workflow_service
from src.ui.daily_workflow_service import (
    EnrichmentSummary,
    automatic_enrichment_limit,
    run_unscored_job_backlog,
)


def test_automatic_enrichment_limit_uses_shared_default() -> None:
    assert DEFAULT_ENRICHMENT_LIMIT == 25
    assert automatic_enrichment_limit(20) == DEFAULT_ENRICHMENT_LIMIT


def test_automatic_enrichment_limit_expands_with_larger_ai_limit() -> None:
    assert automatic_enrichment_limit(50) == 50


def test_run_unscored_job_backlog_uses_broader_enrichment_limit() -> None:
    observed_limits: list[int] = []
    original_enrichment = daily_workflow_service.run_description_enrichment
    original_capture = daily_workflow_service.capture_stdout_lines

    def fake_enrichment(**kwargs) -> EnrichmentSummary:
        observed_limits.append(kwargs["limit"])

        return EnrichmentSummary(
            jobs_selected=0,
            jobs_processed=0,
            eligible_for_enrichment=0,
            descriptions_updated=0,
            enriched=0,
            blocked=0,
            no_description=0,
            fetch_error=0,
            invalid_url=0,
            not_improved=0,
            rule_matches_refreshed=0,
            log_lines=(),
        )

    def fake_capture(function, **kwargs):
        return (
            BacklogSummary(
                resume_hash="resume",
                model_name="test-model",
                candidates_selected=0,
                already_scored=0,
                scores_saved=0,
                jobs_failed=0,
                gemini_scores_saved=0,
                openai_scores_saved=0,
                quota_exhausted=False,
                dbt_was_run=False,
            ),
            (),
        )

    try:
        daily_workflow_service.run_description_enrichment = fake_enrichment
        daily_workflow_service.capture_stdout_lines = fake_capture

        run_unscored_job_backlog(
            resume_hash="resume",
            limit=20,
            minimum_rule_score=0,
        )
        run_unscored_job_backlog(
            resume_hash="resume",
            limit=50,
            minimum_rule_score=0,
        )
    finally:
        daily_workflow_service.run_description_enrichment = (
            original_enrichment
        )
        daily_workflow_service.capture_stdout_lines = original_capture

    assert observed_limits == [
        DEFAULT_ENRICHMENT_LIMIT,
        50,
    ]


def main() -> None:
    test_automatic_enrichment_limit_uses_shared_default()
    test_automatic_enrichment_limit_expands_with_larger_ai_limit()
    test_run_unscored_job_backlog_uses_broader_enrichment_limit()
    print("Daily workflow service tests passed.")


if __name__ == "__main__":
    main()
