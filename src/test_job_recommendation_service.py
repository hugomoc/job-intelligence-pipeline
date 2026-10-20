from __future__ import annotations

from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from docx import Document

from src import database
from src.resume.extractor import extract_resume
from src.scoring_run_repository import load_latest_scoring_run
from src.score_jobs_ai import (
    load_cached_resume_text,
    save_resume_profile,
)
from src.test_resume_matcher_validation import make_profile
from src.ui import job_recommendation_service


class FakeUploadedResume:
    name = "resume.docx"
    type = (
        "application/vnd.openxmlformats-officedocument."
        "wordprocessingml.document"
    )

    def __init__(self, payload: bytes):
        self._payload = payload

    def getvalue(self) -> bytes:
        return self._payload


def build_resume_upload() -> FakeUploadedResume:
    document = Document()
    document.add_paragraph(
        "Hugo Batista hugo@example.com 555-555-1212 "
        "Senior Data Engineer with more than twelve years of experience "
        "building production SQL pipelines, Python automation, dimensional "
        "models, Snowflake warehouses, Airflow workflows, and analytics "
        "platforms for business stakeholders and engineering teams."
    )
    buffer = BytesIO()
    document.save(buffer)
    return FakeUploadedResume(buffer.getvalue())


def test_prepare_uploaded_resume_backfills_v7_text_without_scoring() -> None:
    uploaded = build_resume_upload()
    extracted = extract_resume(uploaded)

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
            save_resume_profile(
                resume_hash=extracted.resume_hash,
                filename=extracted.filename,
                model_name="fixture-model",
                profile=make_profile(),
            )

            assert (
                load_cached_resume_text(
                    extracted.resume_hash
                )
                is None
            )

            with patch.object(
                job_recommendation_service,
                "get_model_name",
                return_value="fixture-model",
            ), patch.object(
                job_recommendation_service,
                "profile_resume",
                side_effect=AssertionError(
                    "Cached resume preparation must not re-profile."
                ),
            ), patch.object(
                job_recommendation_service,
                "score_resume_against_job",
                side_effect=AssertionError(
                    "Preparing a resume must not score jobs."
                ),
            ), patch.object(
                job_recommendation_service,
                "score_resume_against_job_openai",
                side_effect=AssertionError(
                    "Preparing a resume must not score jobs."
                ),
            ):
                prepared = (
                    job_recommendation_service.prepare_resume_upload(
                        uploaded
                    )
                )

            cached_text = load_cached_resume_text(
                extracted.resume_hash
            )

            assert prepared.resume.resume_hash == extracted.resume_hash
            assert prepared.profile_was_cached is True
            assert cached_text is not None
            assert "production SQL pipelines" in cached_text
            assert "hugo@example.com" not in cached_text
            assert "555-555-1212" not in cached_text


def test_prepare_new_resume_creates_profile_but_does_not_score_jobs() -> None:
    uploaded = build_resume_upload()
    profile = make_profile()

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
        ), patch.object(
            job_recommendation_service,
            "get_model_name",
            return_value="fixture-model",
        ), patch.object(
            job_recommendation_service,
            "profile_resume",
            return_value=SimpleNamespace(
                profile=profile
            ),
        ) as profiler, patch.object(
            job_recommendation_service,
            "score_resume_against_job",
            side_effect=AssertionError(
                "Preparing a resume must not score jobs."
            ),
        ), patch.object(
            job_recommendation_service,
            "score_resume_against_job_openai",
            side_effect=AssertionError(
                "Preparing a resume must not score jobs."
            ),
        ):
            prepared = (
                job_recommendation_service.prepare_resume_upload(
                    uploaded
                )
            )

            profiler.assert_called_once()
            assert prepared.profile_was_cached is False
            assert load_cached_resume_text(
                prepared.resume.resume_hash
            )


def test_process_resume_upload_persists_scoring_log() -> None:
    uploaded = build_resume_upload()
    extracted = extract_resume(uploaded)

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
            save_resume_profile(
                resume_hash=extracted.resume_hash,
                filename=extracted.filename,
                model_name="fixture-model",
                profile=make_profile(),
            )

            fixture_match = SimpleNamespace(
                overall_score=85,
                recommendation="apply",
                analysis=SimpleNamespace(
                    confidence="high"
                ),
            )

            with patch.object(
                job_recommendation_service,
                "get_model_name",
                return_value="fixture-model",
            ), patch.object(
                job_recommendation_service,
                "screen_unscreened_jobs",
            ), patch.object(
                job_recommendation_service,
                "count_cached_canonical_scores",
                return_value=0,
            ), patch.object(
                job_recommendation_service,
                "load_candidate_jobs",
                return_value=[
                    {
                        "record_key": "record-1",
                        "title": "Senior Data Engineer",
                        "company_name": "Example Co",
                    }
                ],
            ), patch.object(
                job_recommendation_service,
                "score_resume_against_job",
                return_value=fixture_match,
            ), patch.object(
                job_recommendation_service,
                "score_resume_against_job_openai",
                side_effect=AssertionError(
                    "OpenAI fallback should not be used."
                ),
            ), patch.object(
                job_recommendation_service,
                "save_job_score",
            ) as save_score, patch.object(
                job_recommendation_service,
                "run_dbt_build",
            ):
                summary = (
                    job_recommendation_service.process_resume_upload(
                        uploaded_file=uploaded,
                        limit=1,
                        minimum_rule_score=0,
                    )
                )

            assert summary.successful_score_count == 1
            assert summary.gemini_score_count == 1
            assert any(
                "Saved score: 85% APPLY" in line
                for line in summary.log_lines
            )
            save_score.assert_called_once()

            latest = load_latest_scoring_run(
                extracted.resume_hash
            )
            assert latest is not None
            assert latest.workflow == "score_jobs"
            assert latest.scores_saved == 1
            assert latest.gemini_scores_saved == 1
            assert any(
                "Senior Data Engineer | Example Co" in line
                for line in latest.log_lines
            )


if __name__ == "__main__":
    test_prepare_uploaded_resume_backfills_v7_text_without_scoring()
    test_prepare_new_resume_creates_profile_but_does_not_score_jobs()
    test_process_resume_upload_persists_scoring_log()
    print("Job recommendation service tests passed.")
