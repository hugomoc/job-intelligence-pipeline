from __future__ import annotations

from datetime import datetime, timezone

from src.ui.job_pipeline_state import derive_job_pipeline_state


def test_missing_description_linkedin_waits_for_enrichment_not_ai_failure() -> None:
    state = derive_job_pipeline_state(
        {
            "source": "linkedin",
            "title": "Senior Data Engineer",
            "company_name": "Robots & Pencils",
            "description_state": "NEEDS_ENRICHMENT",
            "raw_description_word_count": 0,
            "enrichment_status": "blocked",
            "enrichment_http_status": 429,
            "enrichment_attempt_count": 1,
            "official_url_status": None,
            "ai_score": None,
        }
    )

    assert state.label == (
        "Source fetch blocked - official lookup pending"
    )
    assert state.code == "ENRICHMENT_PENDING"


def test_full_jd_without_ai_score_waits_for_ai_scoring() -> None:
    state = derive_job_pipeline_state(
        {
            "description_state": "FULL_JD",
            "raw_description_word_count": 350,
            "ai_score": None,
        }
    )

    assert state.label == "Awaiting AI scoring"
    assert state.code == "SCORING_PENDING"


def test_scored_job_remains_ai_scored() -> None:
    state = derive_job_pipeline_state(
        {
            "description_state": "FULL_JD",
            "ai_score": 88,
        }
    )

    assert state.label == "AI-scored"
    assert state.code == "AI_SCORED"


def test_verified_official_url_is_exposed_in_details() -> None:
    state = derive_job_pipeline_state(
        {
            "description_state": "FULL_JD",
            "ai_score": None,
            "official_url_status": "FOUND_VERIFIED",
            "official_job_url": "https://company.example/jobs/123",
            "official_url_source": "greenhouse",
            "official_url_resolved_at": datetime(
                2026,
                10,
                1,
                tzinfo=timezone.utc,
            ),
        }
    )

    assert (
        "Official job URL",
        "https://company.example/jobs/123",
    ) in state.details
    assert (
        "Official-resolution source",
        "greenhouse",
    ) in state.details


def main() -> None:
    test_missing_description_linkedin_waits_for_enrichment_not_ai_failure()
    test_full_jd_without_ai_score_waits_for_ai_scoring()
    test_scored_job_remains_ai_scored()
    test_verified_official_url_is_exposed_in_details()
    print("Job pipeline state tests passed.")


if __name__ == "__main__":
    main()
