from __future__ import annotations

import argparse
from dataclasses import dataclass

from src.ai.resume_matcher import (
    MATCHER_PROMPT_VERSION,
    ResumeMatcherError,
    get_openai_model_name,
    score_resume_against_job,
    score_resume_against_job_openai,
)
from src.ai.resume_profiler import get_model_name
from src.repositories.recommendation_repository import (
    count_cached_canonical_scores,
    load_candidate_jobs,
)
from src.database import get_connection
from src.score_jobs_ai import (
    initialize_ai_tables,
    load_cached_profile,
    save_job_score,
)
from src.ui.job_recommendation_service import run_dbt_build


@dataclass(frozen=True)
class BacklogSummary:
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


def is_quota_exhausted(error: ResumeMatcherError) -> bool:
    error_message = str(error)

    return (
        "429 RESOURCE_EXHAUSTED" in error_message
        and (
            "GenerateRequestsPerDay" in error_message
            or "quota exceeded" in error_message.casefold()
        )
    )


def is_openai_quota_exhausted(error: ResumeMatcherError) -> bool:
    error_message = str(error)

    return (
        "insufficient_quota" in error_message
        or (
            "429" in error_message
            and "quota" in error_message.casefold()
            and "openai" in error_message.casefold()
        )
    )


def load_latest_resume_hash(model_name: str) -> str | None:
    initialize_ai_tables()

    with get_connection() as connection:
        result = connection.execute(
            """
            SELECT resume_hash
            FROM resume_profiles
            WHERE model_name = ?
            ORDER BY created_at DESC NULLS LAST
            LIMIT 1
            """,
            [model_name],
        ).fetchone()

    if not result:
        return None

    return str(result[0])


def score_backlog(
    resume_hash: str | None,
    limit: int,
    minimum_rule_score: int,
) -> BacklogSummary:
    initialize_ai_tables()

    model_name = get_model_name()
    selected_resume_hash = resume_hash or load_latest_resume_hash(
        model_name=model_name,
    )

    if selected_resume_hash is None:
        raise ValueError(
            "No cached resume profile was found. Upload a resume in "
            "Streamlit once before running the backlog scorer."
        )

    resume_profile = load_cached_profile(
        resume_hash=selected_resume_hash,
        model_name=model_name,
    )

    if resume_profile is None:
        raise ValueError(
            "No cached resume profile was found for the selected resume. "
            "Upload the resume in Streamlit once before running the backlog scorer."
        )

    already_scored = count_cached_canonical_scores(
        resume_hash=selected_resume_hash,
        model_name=model_name,
        prompt_version=MATCHER_PROMPT_VERSION,
        reuse_any_model=True,
    )

    candidate_jobs = load_candidate_jobs(
        resume_hash=selected_resume_hash,
        model_name=model_name,
        limit=limit,
        minimum_rule_score=minimum_rule_score,
        prompt_version=MATCHER_PROMPT_VERSION,
        reuse_any_model=True,
    )

    scores_saved = 0
    jobs_failed = 0
    gemini_scores_saved = 0
    openai_scores_saved = 0
    quota_exhausted = False
    use_openai_fallback = False

    for index, job in enumerate(candidate_jobs, start=1):
        title = job.get("title") or "Untitled job"
        company = job.get("company_name") or "Unknown company"

        print(
            f"Scoring {index}/{len(candidate_jobs)}: "
            f"{title} | {company}"
        )

        try:
            if use_openai_fallback:
                match = score_resume_against_job_openai(
                    resume_profile=resume_profile,
                    job=job,
                    model_name=get_openai_model_name(),
                )
            else:
                match = score_resume_against_job(
                    resume_profile=resume_profile,
                    job=job,
                    model_name=model_name,
                )

        except ResumeMatcherError as error:
            print("Score failed for this job.")

            if is_quota_exhausted(error):
                quota_exhausted = True
                print(
                    "Gemini quota is exhausted. "
                    "Switching to OpenAI fallback."
                )

                use_openai_fallback = True

                try:
                    match = score_resume_against_job_openai(
                        resume_profile=resume_profile,
                        job=job,
                        model_name=get_openai_model_name(),
                    )

                except ResumeMatcherError as fallback_error:
                    jobs_failed += 1
                    print(
                        "OpenAI fallback also failed for this job."
                    )

                    if is_openai_quota_exhausted(
                        fallback_error
                    ):
                        print(
                            "OpenAI quota is exhausted. "
                            "Stopping the remaining backlog scoring requests."
                        )
                        break

                    continue

                save_job_score(
                    resume_hash=selected_resume_hash,
                    match=match,
                )
                scores_saved += 1
                openai_scores_saved += 1

                print(
                    "Saved fallback score: "
                    f"{match.overall_score}% "
                    f"{match.recommendation.upper()} "
                    f"({match.analysis.confidence} confidence)"
                )

                continue

            if (
                use_openai_fallback
                and is_openai_quota_exhausted(error)
            ):
                jobs_failed += 1
                print(
                    "OpenAI quota is exhausted. "
                    "Stopping the remaining backlog scoring requests."
                )
                break

            jobs_failed += 1
            continue

        save_job_score(
            resume_hash=selected_resume_hash,
            match=match,
        )
        scores_saved += 1

        if use_openai_fallback:
            openai_scores_saved += 1
        else:
            gemini_scores_saved += 1

        print(
            "Saved score: "
            f"{match.overall_score}% "
            f"{match.recommendation.upper()} "
            f"({match.analysis.confidence} confidence)"
        )

    dbt_was_run = False

    if scores_saved:
        print("Refreshing dbt analytics...")
        run_dbt_build()
        dbt_was_run = True

    return BacklogSummary(
        resume_hash=selected_resume_hash,
        model_name=model_name,
        candidates_selected=len(candidate_jobs),
        already_scored=already_scored,
        scores_saved=scores_saved,
        jobs_failed=jobs_failed,
        gemini_scores_saved=gemini_scores_saved,
        openai_scores_saved=openai_scores_saved,
        quota_exhausted=quota_exhausted,
        dbt_was_run=dbt_was_run,
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Resume AI scoring for unscored jobs using an existing "
            "cached resume profile."
        )
    )
    parser.add_argument(
        "--resume-hash",
        required=False,
        help=(
            "Resume hash for a profile already cached by Streamlit. "
            "Defaults to the latest cached profile."
        ),
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Maximum number of unscored jobs to attempt. Default: 20.",
    )
    parser.add_argument(
        "--min-rule-score",
        type=int,
        default=35,
        help="Minimum rule-match score to include. Default: 35.",
    )

    arguments = parser.parse_args()

    if arguments.limit < 1:
        print("--limit must be at least 1.")
        raise SystemExit(1)

    if not 0 <= arguments.min_rule_score <= 100:
        print("--min-rule-score must be between 0 and 100.")
        raise SystemExit(1)

    try:
        summary = score_backlog(
            resume_hash=arguments.resume_hash,
            limit=arguments.limit,
            minimum_rule_score=arguments.min_rule_score,
        )
    except Exception as error:
        print(f"Backlog scoring could not start: {error}")
        raise SystemExit(1) from error

    print("")
    print("Backlog scoring summary")
    print(f"Model: {summary.model_name}")
    print(f"Already scored before this run: {summary.already_scored}")
    print(f"Candidates selected: {summary.candidates_selected}")
    print(f"New scores saved: {summary.scores_saved}")
    print(f"Gemini scores saved: {summary.gemini_scores_saved}")
    print(f"OpenAI scores saved: {summary.openai_scores_saved}")
    print(f"Jobs failed: {summary.jobs_failed}")
    print(f"Quota exhausted: {summary.quota_exhausted}")
    print(f"dbt build run: {summary.dbt_was_run}")


if __name__ == "__main__":
    main()
