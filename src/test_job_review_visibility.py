from __future__ import annotations

from src.ai.resume_matcher import MATCHER_PROMPT_VERSION
from src.repositories.recommendation_repository import (
    has_current_complete_ai_score,
)
from src.ui.job_visibility import (
    REVIEW_BUCKET_LOW_FIT,
    REVIEW_BUCKET_NEEDS_REVIEW,
    REVIEW_BUCKET_RECOMMENDED,
    REVIEW_FILTER_ALL,
    REVIEW_FILTER_LOW_FIT,
    REVIEW_FILTER_RECOMMENDED,
    classify_job_review_bucket,
    filter_jobs_by_review_filter,
    is_visible_for_review_filter,
)


def complete_ai_job(**overrides) -> dict:
    job = {
        "title": "Senior Analytics Engineer",
        "application_status": "new",
        "ai_score": 72,
        "recommendation": "review",
        "confidence": "medium",
        "fit_priority_label": "Medium",
        "fit_priority_source": "AI assessment",
        "has_current_complete_ai_assessment": True,
        "ai_prompt_version": MATCHER_PROMPT_VERSION,
        "description_complete": True,
        "has_incomplete_description": False,
        "description_state": "FULL_JD",
        "description_word_count": 140,
        "raw_description_word_count": 140,
        "description_quality_signals": True,
        "ai_scored_at": "2026-10-07T10:00:00+00:00",
        "description_updated_at": "2026-10-06T10:00:00+00:00",
    }
    job.update(overrides)
    return job


def tag_current_assessment(job: dict) -> dict:
    job["has_current_complete_ai_assessment"] = (
        has_current_complete_ai_score(job)
    )
    return job


def test_high_confidence_skip_below_50_is_low_fit() -> None:
    job = complete_ai_job(
        ai_score=49,
        recommendation="skip",
        confidence="high",
        fit_priority_label="Low",
    )

    assert classify_job_review_bucket(job) == REVIEW_BUCKET_LOW_FIT


def test_consider_at_50_or_higher_is_recommended() -> None:
    job = complete_ai_job(
        ai_score=59,
        recommendation="consider",
        confidence="medium",
        fit_priority_label="Medium",
    )

    assert classify_job_review_bucket(job) == REVIEW_BUCKET_RECOMMENDED


def test_review_at_50_or_higher_is_recommended() -> None:
    job = complete_ai_job(
        ai_score=59,
        recommendation="review",
        confidence="medium",
        fit_priority_label="Medium",
    )

    assert classify_job_review_bucket(job) == REVIEW_BUCKET_RECOMMENDED


def test_skip_above_60_with_medium_confidence_stays_recommended() -> None:
    job = complete_ai_job(
        ai_score=61,
        recommendation="skip",
        confidence="medium",
        fit_priority_label="Low",
    )

    assert classify_job_review_bucket(job) == REVIEW_BUCKET_RECOMMENDED


def test_awaiting_enrichment_without_score_needs_review() -> None:
    job = complete_ai_job(
        ai_score=None,
        recommendation=None,
        confidence=None,
        description_state="NEEDS_ENRICHMENT",
        has_current_complete_ai_assessment=False,
    )

    assert classify_job_review_bucket(job) == REVIEW_BUCKET_NEEDS_REVIEW


def test_full_jd_without_score_needs_review() -> None:
    job = complete_ai_job(
        ai_score=None,
        recommendation=None,
        confidence=None,
        has_current_complete_ai_assessment=False,
    )

    assert classify_job_review_bucket(job) == REVIEW_BUCKET_NEEDS_REVIEW


def test_applied_low_fit_job_remains_visible_in_recommended_view() -> None:
    job = complete_ai_job(
        application_status="applied",
        ai_score=45,
        recommendation="skip",
        confidence="high",
        fit_priority_label="Low",
    )

    assert classify_job_review_bucket(job) == REVIEW_BUCKET_RECOMMENDED
    assert is_visible_for_review_filter(job, REVIEW_FILTER_RECOMMENDED)


def test_stale_score_is_needs_review_not_low_fit() -> None:
    job = tag_current_assessment(
        complete_ai_job(
            ai_score=45,
            recommendation="skip",
            confidence="high",
            ai_scored_at="2026-10-06T10:00:00+00:00",
            description_updated_at="2026-10-07T10:00:00+00:00",
        )
    )

    assert job["has_current_complete_ai_assessment"] is False
    assert classify_job_review_bucket(job) == REVIEW_BUCKET_NEEDS_REVIEW


def test_old_matcher_version_is_needs_review_not_low_fit() -> None:
    job = tag_current_assessment(
        complete_ai_job(
            ai_score=45,
            recommendation="skip",
            confidence="high",
            ai_prompt_version="v1",
        )
    )

    assert job["has_current_complete_ai_assessment"] is False
    assert classify_job_review_bucket(job) == REVIEW_BUCKET_NEEDS_REVIEW


def test_analytics_engineer_49_skip_regression_filters_correctly() -> None:
    job = complete_ai_job(
        title="Analytics Engineer, Unity",
        ai_score=49,
        recommendation="skip",
        confidence="high",
        fit_priority_label="Low",
        fit_priority_source="AI assessment",
        description_state="FULL_JD",
        application_status="new",
    )

    assert classify_job_review_bucket(job) == REVIEW_BUCKET_LOW_FIT
    assert not is_visible_for_review_filter(job, REVIEW_FILTER_RECOMMENDED)
    assert is_visible_for_review_filter(job, REVIEW_FILTER_ALL)
    assert is_visible_for_review_filter(job, REVIEW_FILTER_LOW_FIT)
    assert filter_jobs_by_review_filter(
        [job],
        REVIEW_FILTER_RECOMMENDED,
    ) == []
    assert filter_jobs_by_review_filter(
        [job],
        REVIEW_FILTER_LOW_FIT,
    ) == [job]


def main() -> None:
    test_high_confidence_skip_below_50_is_low_fit()
    test_consider_at_50_or_higher_is_recommended()
    test_review_at_50_or_higher_is_recommended()
    test_skip_above_60_with_medium_confidence_stays_recommended()
    test_awaiting_enrichment_without_score_needs_review()
    test_full_jd_without_score_needs_review()
    test_applied_low_fit_job_remains_visible_in_recommended_view()
    test_stale_score_is_needs_review_not_low_fit()
    test_old_matcher_version_is_needs_review_not_low_fit()
    test_analytics_engineer_49_skip_regression_filters_correctly()
    print("Job review visibility tests passed.")


if __name__ == "__main__":
    main()
