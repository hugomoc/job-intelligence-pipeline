from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import httpx

from src import database, enrich_jobs
from src.enrich_jobs import DEFAULT_ENRICHMENT_LIMIT, EnrichmentProcessingResult
from src.enrichment.job_description import JobDescriptionResult
from src.enrichment.official_job_resolver import OfficialJobResolutionResult
from src.repositories import recommendation_repository
from src.score_backlog import BacklogSummary, load_latest_resume_hash
from src.scoring_run_repository import (
    finish_scoring_run,
    load_latest_scoring_run,
    start_scoring_run,
)
from src.ui import daily_workflow_service
from src.score_jobs_ai import load_cached_profile, save_resume_profile
from src.test_resume_matcher_validation import make_profile
from src.ui.daily_workflow_service import (
    EnrichmentSummary,
    DailyWorkflowError,
    ScoringBacklogStatus,
    automatic_enrichment_limit,
    run_unscored_job_backlog,
)


def test_resume_profile_cache_survives_scoring_model_change() -> None:
    with TemporaryDirectory() as directory:
        data_dir = Path(directory)

        with patch.object(database, "DATA_DIR", data_dir), patch.object(
            database, "DATABASE_PATH", data_dir / "jobs.duckdb"
        ):
            profile = make_profile()

            save_resume_profile(
                resume_hash="resume-123",
                filename="resume.pdf",
                model_name="gemini-2.5-flash-lite",
                profile=profile,
            )

            assert load_latest_resume_hash(
                "gemini-3.5-flash-lite"
            ) == "resume-123"

            loaded = load_cached_profile(
                resume_hash="resume-123",
                model_name="gemini-3.5-flash-lite",
            )

            assert loaded is not None
            assert loaded.professional_summary == profile.professional_summary


def test_automatic_enrichment_limit_uses_shared_default() -> None:
    assert DEFAULT_ENRICHMENT_LIMIT == 25
    assert automatic_enrichment_limit(20) == DEFAULT_ENRICHMENT_LIMIT


def test_automatic_enrichment_limit_expands_with_larger_ai_limit() -> None:
    assert automatic_enrichment_limit(50) == 50


def test_run_unscored_job_backlog_uses_broader_enrichment_limit() -> None:
    observed_limits: list[int] = []
    original_enrichment = daily_workflow_service.run_description_enrichment
    original_capture = daily_workflow_service.capture_stdout_lines
    original_start_run = daily_workflow_service.start_scoring_run
    original_finish_run = daily_workflow_service.finish_scoring_run

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
        daily_workflow_service.start_scoring_run = (
            lambda **kwargs: "fixture-run"
        )
        daily_workflow_service.finish_scoring_run = (
            lambda *args, **kwargs: None
        )

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
        daily_workflow_service.start_scoring_run = original_start_run
        daily_workflow_service.finish_scoring_run = original_finish_run

    assert observed_limits == [
        DEFAULT_ENRICHMENT_LIMIT,
        50,
    ]


def test_scoring_run_history_persists_latest_log() -> None:
    with TemporaryDirectory() as directory:
        data_dir = Path(directory)

        with patch.object(
            database,
            "DATA_DIR",
            data_dir,
        ), patch.object(
            database,
            "DATABASE_PATH",
            data_dir / "jobs.duckdb",
        ):
            first_run = start_scoring_run(
                resume_hash="resume-123",
                workflow="score_unscored",
            )
            finish_scoring_run(
                first_run,
                status="completed",
                model_name="gemini-test",
                candidates_selected=20,
                already_scored=10,
                scores_saved=19,
                jobs_failed=1,
                gemini_scores_saved=19,
                openai_scores_saved=0,
                quota_exhausted=False,
                log_lines=(
                    "Scoring 1/20: Example | Company",
                    "Saved score: 85% APPLY",
                ),
            )

            latest = load_latest_scoring_run(
                "resume-123"
            )

            assert latest is not None
            assert latest.workflow == "score_unscored"
            assert latest.scores_saved == 19
            assert latest.gemini_scores_saved == 19
            assert latest.openai_scores_saved == 0
            assert latest.log_lines == (
                "Scoring 1/20: Example | Company",
                "Saved score: 85% APPLY",
            )


@contextmanager
def disposable_enrichment_database():
    with TemporaryDirectory() as directory:
        data_dir = Path(directory)
        with patch.object(database, "DATA_DIR", data_dir), patch.object(
            database, "DATABASE_PATH", data_dir / "jobs.duckdb"
        ):
            enrich_jobs.initialize_enrichment_tables()
            yield


def run_partial_daily_batch() -> EnrichmentSummary:
    """Exercise the real service, batch and save guard on disposable state."""
    jobs = [
        dict(
            record_key=key,
            source="welcometothejungle",
            title="Senior Data Engineer",
            company_name=company,
            location="Remote",
            apply_url="https://example.com/alert/" + key,
            description="Thin alert",
            current_word_count=2,
            application_status="new",
            attempt_count=1,
            previous_attempted_at=datetime(2026, 10, 1 if key == "unsafe" else 2, tzinfo=timezone.utc),
            previous_status="blocked",
            official_url_status="SEARCH_DEFERRED",
            needs_description_enrichment=True,
            needs_official_resolution=True,
        )
        for key, company in (("unsafe", "Unsafe Co"), ("following", "Following Co"))
    ]
    processed_keys = []
    url = "https://jobs.ashbyhq.com/example/72669bd1-552c-40f2-b271-17b3d3b3cc8f"

    def fetched(job):
        return JobDescriptionResult(
            requested_url=url,
            final_url=url,
            status="enriched",
            http_status=200,
            extraction_method="json_ld",
            description="Responsibilities include SQL and dbt.",
            word_count=6,
            error_message=None,
            resolved_title=job["title"],
            resolved_company=job["company_name"],
        )

    def processor(job, client, **kwargs):
        processed_keys.append(job["record_key"])
        result = fetched(job)
        resolution = OfficialJobResolutionResult(
            status="FOUND_VERIFIED",
            official_job_url=url,
            official_url_source="ashby",
            official_url_resolved_at=datetime(2026, 10, 14, tzinfo=timezone.utc),
            official_url_confidence=1.0,
            official_url_validation_reason="identity accepted",
            resolved_title=result.resolved_title,
            resolved_company=(None if job["record_key"] == "unsafe" else result.resolved_company),
            resolved_location=None,
        )
        return EnrichmentProcessingResult(
            result=result,
            stored_status="enriched",
            stored_error=None,
            identity_validation=None,
            official_resolution=resolution,
            updated=False,
        )

    for job in jobs:
        enrich_jobs.save_enrichment_attempt(
            dict(job, attempt_count=0),
            replace(fetched(job), status="blocked", description="", word_count=0),
        )
    with database.get_connection() as connection:
        previous = connection.execute(
            "SELECT * FROM job_enrichment_attempts WHERE record_key='unsafe'"
        ).fetchall()

    def no_network(request):
        raise AssertionError("Unexpected live fetch: " + request.url.host)

    client = httpx.Client(transport=httpx.MockTransport(no_network))
    with patch.object(daily_workflow_service, "load_jobs_to_enrich", return_value=jobs), \
         patch.object(daily_workflow_service, "create_http_client", return_value=client), \
         patch.object(enrich_jobs, "process_enrichment_job", side_effect=processor), \
         patch.object(enrich_jobs.time, "sleep"), \
         patch.object(daily_workflow_service, "refresh_rule_matches") as refresh:
        summary = daily_workflow_service.run_description_enrichment(
            limit=25, retry_failed=True, resume_hash="test-resume"
        )

    assert processed_keys == ["unsafe", "following"]
    assert summary.jobs_selected == 2
    assert summary.jobs_processed == 2
    assert summary.descriptions_updated == 0
    assert summary.persistence_error == 1
    assert summary.fetch_error == 0
    assert summary.enriched == 1
    refresh.assert_not_called()
    with database.get_connection() as connection:
        assert connection.execute(
            "SELECT * FROM job_enrichment_attempts WHERE record_key='unsafe'"
        ).fetchall() == previous
        following = connection.execute(
            "SELECT status, attempt_count, official_url_status, verified_posting_key_trust "
            "FROM job_enrichment_attempts WHERE record_key='following'"
        ).fetchone()
    assert following == ("enriched", 2, "FOUND_VERIFIED", "trusted")
    log = "\n".join(summary.log_lines)
    assert "Persistence rejected" in log
    assert "Unsafe Co" in log and "unsafe" in log
    assert "ValueError:" in log
    assert "Following Co" in log and "Status: enriched" in log
    return summary


def test_daily_workflow_completes_after_real_guard_rejection() -> None:
    with disposable_enrichment_database():
        run_partial_daily_batch()


def test_orchestration_failure_still_has_generic_error() -> None:
    cause = RuntimeError("internal queue failure with private details")
    with patch.object(daily_workflow_service, "load_jobs_to_enrich", side_effect=cause), \
         patch("builtins.print"):
        try:
            daily_workflow_service.run_description_enrichment(limit=25, resume_hash="test-resume")
        except DailyWorkflowError as error:
            assert str(error) == "Job descriptions could not be enriched. Please try again later."
            assert error.__cause__ is cause
            assert "private details" not in str(error)
        else:
            raise AssertionError("Orchestration failures must remain DailyWorkflowError")


def test_streamlit_renders_partial_enrichment_diagnostics() -> None:
    from streamlit.testing.v1 import AppTest

    with disposable_enrichment_database():
        summary = run_partial_daily_batch()
    metrics = recommendation_repository.reviewable_job_metrics([])
    metrics.update(raw_jobs=0, exact_postings=0, enrichment_attempts={})
    with disposable_enrichment_database(), \
         patch.object(daily_workflow_service, "run_description_enrichment", return_value=summary), \
         patch.object(daily_workflow_service, "load_scoring_backlog_status",
                      return_value=ScoringBacklogStatus("test-resume", 0, 0, ())), \
         patch.object(recommendation_repository, "load_all_jobs", return_value=[]), \
         patch.object(recommendation_repository, "load_pipeline_operation_metrics", return_value=metrics), \
         patch("src.actionability.refresh_daily", side_effect=AssertionError("Unexpected actionability call")):
        app = AppTest.from_string(Path("streamlit_app.py").read_text(), default_timeout=30)
        app.session_state["resume_hash"] = "test-resume"
        app.run()
        assert not app.exception, str(app.exception)
        next(button for button in app.button if button.label == "Enrich descriptions").click().run()
        assert not app.exception, str(app.exception)
        assert not app.error
        assert any(item.label == "Description enrichment complete" for item in app.status)
        assert any(item.value == "Persistence errors: 1" for item in app.markdown)
        assert any(item.value == "Fetch errors: 0" for item in app.markdown)
        assert any("Persistence rejected" in item.value and "Following Co" in item.value for item in app.code)
        assert app.session_state["show_saved_jobs"] is True


def test_daily_careers_cache_seed_reuses_one_trusted_candidate_load() -> None:
    from copy import deepcopy
    from src.enrichment.employer_careers import CAREERS_CACHE

    job = dict(record_key='new', title='Data Engineer', company_name='Acme')
    original = deepcopy(job)
    url = 'https://careers.acme.com/jobs/123'
    trusted = dict(company_name='Acme', official_resolved_company='Acme Inc',
        official_url_status='FOUND_VERIFIED', official_job_url=url,
        verified_posting_key='official-url:' + url,
        verified_posting_key_source='official_found_verified',
        verified_posting_key_trust='trusted')
    untrusted = dict(trusted, company_name='Other', official_resolved_company='Other',
                     verified_posting_key_trust=None)
    candidates = [trusted, untrusted]
    stats = dict(runnable=1, fresh_description=0, eligible_deferred_retries=0,
                 employer_verification_pending=1)
    batch = enrich_jobs.EnrichmentBatchSummary(1, 0, 0,
        dict(enriched=0, blocked=0, no_description=0, fetch_error=0,
             invalid_url=0, not_improved=0), False, 0, ())

    def annotated(jobs, loaded):
        assert loaded is candidates
        assert list(CAREERS_CACHE) == ['acme']
        assert CAREERS_CACHE['acme'][1]['basis'] == 'previously_verified_employer'
        assert jobs == [original]  # Careers proof cannot become this job's identity.

    def processed(**kwargs):
        assert 'acme' in CAREERS_CACHE
        assert job == original
        return batch

    with patch.dict(CAREERS_CACHE, {}, clear=True), \
         patch.object(daily_workflow_service, 'load_daily_enrichment_plan', return_value=([job], stats)), \
         patch.object(daily_workflow_service, 'load_verified_reuse_candidates', return_value=candidates) as load, \
         patch.object(daily_workflow_service, 'annotate_known_official_candidates', side_effect=annotated) as annotate, \
         patch.object(daily_workflow_service, 'process_enrichment_batch', side_effect=processed), \
         patch.object(daily_workflow_service, 'create_http_client', return_value=httpx.Client(
             transport=httpx.MockTransport(lambda request: (_ for _ in ()).throw(AssertionError('No network'))))):
        result = daily_workflow_service.run_description_enrichment(1, resume_hash='fixture')
        load.assert_called_once_with(minimum_words=80)
        annotate.assert_called_once()
        assert result.search_requests == 0
        assert job == original


def test_empty_daily_queue_does_not_load_careers_history() -> None:
    stats = dict(runnable=0, fresh_description=0, eligible_deferred_retries=0,
                 employer_verification_pending=0)
    batch = enrich_jobs.EnrichmentBatchSummary(0, 0, 0,
        dict(enriched=0, blocked=0, no_description=0, fetch_error=0,
             invalid_url=0, not_improved=0), False, 0, ())
    with patch.object(daily_workflow_service, 'load_daily_enrichment_plan', return_value=([], stats)), \
         patch.object(daily_workflow_service, 'load_verified_reuse_candidates') as load, \
         patch.object(daily_workflow_service, 'process_enrichment_batch', return_value=batch), \
         patch.object(daily_workflow_service, 'create_http_client', return_value=httpx.Client(
             transport=httpx.MockTransport(lambda request: (_ for _ in ()).throw(AssertionError('No network'))))):
        daily_workflow_service.run_description_enrichment(1, resume_hash='fixture')
        load.assert_not_called()


def test_streamlit_all_unscored_choice_is_resolved_after_enrichment_and_capped() -> None:
    """The stale pre-run count (0) must not become limit 1; the service gets None."""
    from streamlit.testing.v1 import AppTest
    from src.ui.daily_workflow_service import UiBacklogSummary

    metrics = recommendation_repository.reviewable_job_metrics([])
    metrics.update(raw_jobs=0, exact_postings=0, enrichment_attempts={})
    calls = []

    def fake_backlog(**kwargs):
        calls.append(kwargs)
        return UiBacklogSummary(
            resume_hash="test-resume", model_name="fixture", candidates_selected=30, already_scored=0,
            scores_saved=30, jobs_failed=0, gemini_scores_saved=30, openai_scores_saved=0,
            quota_exhausted=False, dbt_was_run=False,
            log_lines=("Scoring limit: All unscored (max 30); eligible unscored after "
                       "ingestion/enrichment: 45; effective limit: 30",),
            limit_mode="all_unscored", effective_limit=30, eligible_before_scoring=45,
            scoring_attempts=30, remaining_eligible=15, selected_option="All unscored (max 30)",
            screening_limit=0, screening_attempts=0, screening_decisions_saved=0)

    with disposable_enrichment_database(), \
         patch.object(daily_workflow_service, "run_unscored_job_backlog", side_effect=fake_backlog), \
         patch.object(daily_workflow_service, "load_scoring_backlog_status",
                      return_value=ScoringBacklogStatus("test-resume", 0, 0, ())), \
         patch.object(recommendation_repository, "load_all_jobs", return_value=[]), \
         patch.object(recommendation_repository, "load_pipeline_operation_metrics", return_value=metrics), \
         patch("src.actionability.refresh_daily", return_value=None), \
         patch.dict("os.environ", {"SCREENING_MAX_PER_RUN": ""}):
        app = AppTest.from_string(Path("streamlit_app.py").read_text(), default_timeout=30)
        app.session_state["resume_hash"] = "test-resume"
        app.run()
        assert not app.exception, str(app.exception)
        selector = next(s for s in app.selectbox if s.label == "Unscored jobs to process")
        assert list(selector.options) == ["20", "50", "100", "All unscored (max 30)"]
        selector.set_value("All unscored (max 30)").run()
        next(b for b in app.button if b.label == "Score unscored jobs").click().run()
        assert not app.exception, str(app.exception)
        assert calls and calls[-1]["limit"] is None
        values = [item.value for item in app.markdown]
        assert "Effective scoring limit: 30" in values
        assert "Scoring attempts: 30" in values
        assert "Eligible unscored jobs remaining: 15" in values
        assert "Selected scoring batch option: All unscored (max 30)" in values
        assert "Configured screening limit: 0" in values
        assert "Eligibility screening: disabled by SCREENING_MAX_PER_RUN=0" in values
        assert "Screening attempts: 0" in values
        assert "New eligibility decisions saved: 0" in values
        assert any(c.value == "Eligibility screening: disabled by SCREENING_MAX_PER_RUN=0"
                   for c in app.caption)

        calls.clear()
        selector = next(s for s in app.selectbox if s.label == "Unscored jobs to process")
        selector.set_value("50").run()
        next(b for b in app.button if b.label == "Score unscored jobs").click().run()
        assert calls and calls[-1]["limit"] == 50


def test_streamlit_invalid_screening_setting_blocks_scoring_workflows() -> None:
    """An invalid SCREENING_MAX_PER_RUN is shown and blocks scoring before ingestion/enrichment."""
    from streamlit.testing.v1 import AppTest

    metrics = recommendation_repository.reviewable_job_metrics([])
    metrics.update(raw_jobs=0, exact_postings=0, enrichment_attempts={})
    with disposable_enrichment_database(), \
         patch.object(daily_workflow_service, "run_unscored_job_backlog",
                      side_effect=AssertionError("scoring must not start")), \
         patch.object(daily_workflow_service, "run_email_ingestion",
                      side_effect=AssertionError("ingestion must not start")), \
         patch.object(daily_workflow_service, "load_scoring_backlog_status",
                      return_value=ScoringBacklogStatus("test-resume", 0, 0, ())), \
         patch.object(recommendation_repository, "load_all_jobs", return_value=[]), \
         patch.object(recommendation_repository, "load_pipeline_operation_metrics", return_value=metrics), \
         patch("src.actionability.refresh_daily", return_value=None), \
         patch.dict("os.environ", {"SCREENING_MAX_PER_RUN": "-1"}):
        app = AppTest.from_string(Path("streamlit_app.py").read_text(), default_timeout=30)
        app.session_state["resume_hash"] = "test-resume"
        app.run()
        assert not app.exception, str(app.exception)
        assert any("SCREENING_MAX_PER_RUN must be a non-negative integer" in e.value for e in app.error)
        buttons = {b.label: b for b in app.button}
        assert buttons["Score unscored jobs"].disabled
        assert buttons["Ingest, enrich and score"].disabled
        assert not buttons["Enrich descriptions"].disabled


def main() -> None:
    test_resume_profile_cache_survives_scoring_model_change()
    test_automatic_enrichment_limit_uses_shared_default()
    test_automatic_enrichment_limit_expands_with_larger_ai_limit()
    test_run_unscored_job_backlog_uses_broader_enrichment_limit()
    test_scoring_run_history_persists_latest_log()
    test_daily_workflow_completes_after_real_guard_rejection()
    test_orchestration_failure_still_has_generic_error()
    test_streamlit_renders_partial_enrichment_diagnostics()
    test_daily_careers_cache_seed_reuses_one_trusted_candidate_load()
    test_empty_daily_queue_does_not_load_careers_history()
    test_streamlit_all_unscored_choice_is_resolved_after_enrichment_and_capped()
    test_streamlit_invalid_screening_setting_blocks_scoring_workflows()
    print("Daily workflow service tests passed.")


if __name__ == "__main__":
    main()
