"""Service layer for resume upload and recommendation generation.

Streamlit gives this module an uploaded file and display limits. The service
extracts the resume in memory, reuses or creates a cached profile, screens jobs,
scores eligible candidates, refreshes dbt, and returns safe UI summaries.
"""

from __future__ import annotations

import contextlib
import io
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.ai.resume_matcher import (
    MATCHER_PROMPT_VERSION,
    ResumeMatcherError,
    get_openai_model_name,
    is_gemini_daily_quota_error,
    score_resume_against_job,
    score_resume_against_job_openai,
)
from src.ai.resume_profiler import (
    ResumeProfile,
    get_model_name,
    profile_resume,
)
from src.resume.extractor import ExtractedResume, ResumeExtractionError, extract_resume
from src.job_screening import screen_unscreened_jobs, screening_failed_for_job
from src.repositories.recommendation_repository import (
    count_cached_canonical_scores,
    load_candidate_jobs,
    load_recommendations,
)
from src.scoring_diagnostics import log_scoring_failure, log_scoring_skip, exhausted_gemini_503, log_job_identity
from src.scoring_run_repository import (
    finish_scoring_run,
    start_scoring_run,
)
from src.score_jobs_ai import (
    initialize_ai_tables,
    load_cached_profile,
    save_cached_resume_text,
    save_job_score,
    save_resume_profile,
)


PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


@dataclass(frozen=True)
class PreparedResume:
    """Resume/profile pair ready for screening or job scoring."""

    resume: ExtractedResume
    profile: ResumeProfile
    model_name: str
    profile_was_cached: bool


@dataclass(frozen=True)
class ScoringSummary:
    resume_hash: str
    filename: str
    word_count: int
    page_count: int | None
    model_name: str
    profile_was_cached: bool
    candidate_count: int
    cached_score_count: int
    successful_score_count: int
    failed_score_count: int
    gemini_score_count: int
    openai_score_count: int
    new_score_count: int
    dbt_was_run: bool
    log_lines: tuple[str, ...] = ()
    jobs_skipped: int = 0
    screening_failures: int = 0


class JobRecommendationServiceError(Exception):
    """Raised when the Streamlit service cannot complete the workflow."""


def get_or_create_resume_profile(
    resume: ExtractedResume,
    model_name: str,
) -> tuple[ResumeProfile, bool]:
    """Reuse a cached profile for the resume hash, or create one once."""
    cached_profile = load_cached_profile(
        resume_hash=resume.resume_hash,
        model_name=model_name,
    )

    if cached_profile:
        save_cached_resume_text(
            resume_hash=resume.resume_hash,
            resume_text=resume.text,
        )
        return cached_profile, True

    profiled_resume = profile_resume(
        resume=resume,
        model_name=model_name,
    )

    save_resume_profile(
        resume_hash=resume.resume_hash,
        filename=resume.filename,
        model_name=model_name,
        profile=profiled_resume.profile,
        resume_text=resume.text,
    )

    return profiled_resume.profile, False


def prepare_resume_upload(
    uploaded_file: Any,
    candidate_id: str | None = None,
) -> PreparedResume:
    """Extract and activate an uploaded resume without scoring any jobs."""
    try:
        resume = extract_resume(uploaded_file)
        model_name = get_model_name()

        initialize_ai_tables()

        from src import database
        from src.candidate_assessments import tables_exist, candidate_for_resume, CandidateIdentityError
        with database.get_connection() as connection:
            if tables_exist(connection) and connection.execute('SELECT count(*) FROM candidates').fetchone()[0]:
                existing_owner = candidate_for_resume(connection, resume.resume_hash)
                candidate_id = candidate_id or existing_owner
                if existing_owner and existing_owner != candidate_id:
                    raise CandidateIdentityError('Resume belongs to a different registered candidate')
                if not candidate_id:
                    raise CandidateIdentityError('Confirm which registered candidate owns this resume before profiling')
                if not connection.execute('SELECT 1 FROM candidates WHERE candidate_id=?',[candidate_id]).fetchone():
                    raise CandidateIdentityError('Unknown candidate')

        resume_profile, profile_was_cached = get_or_create_resume_profile(
            resume=resume,
            model_name=model_name,
        )

        if candidate_id:
            from src import database
            from src.candidate_assessments import associate_resume
            with database.get_connection() as connection:
                connection.execute('BEGIN TRANSACTION')
                try:
                    associate_resume(connection, candidate_id, resume.resume_hash, confirmed=True)
                    connection.execute('COMMIT')
                except BaseException:
                    connection.execute('ROLLBACK')
                    raise

        return PreparedResume(
            resume=resume,
            profile=resume_profile,
            model_name=model_name,
            profile_was_cached=profile_was_cached,
        )

    except ResumeExtractionError:
        raise

    except Exception as error:
        print(
            "Unexpected resume preparation error: "
            f"{type(error).__name__}: {error}"
        )
        raise JobRecommendationServiceError(
            "The resume could not be prepared for scoring. "
            "Please try again later."
        ) from error


def run_dbt_build() -> None:
    """Refresh dbt analytics after new scores change mart output."""
    command = [
        str(PROJECT_ROOT / "scripts" / "dbt_jobs.sh"),
        "build",
    ]

    result = subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    if result.returncode != 0:
        print("dbt build failed.")
        print("STDOUT:")
        print(result.stdout)
        print("STDERR:")
        print(result.stderr)
        raise JobRecommendationServiceError(
            "The analytics refresh failed. Please try again later."
        )


def is_gemini_quota_exhausted(error: ResumeMatcherError) -> bool:
    """Return true only for explicit Gemini daily quota exhaustion."""
    return is_gemini_daily_quota_error(error)


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


def process_resume_upload(
    uploaded_file: Any,
    limit: int,
    minimum_rule_score: int,
    candidate_id: str | None = None,
) -> ScoringSummary:
    """Profile a resume, score a bounded set, and persist the scoring log."""
    prepared = prepare_resume_upload(
        uploaded_file, candidate_id=candidate_id
    )
    resume = prepared.resume
    model_name = prepared.model_name
    resume_profile = prepared.profile
    profile_was_cached = prepared.profile_was_cached

    run_id = start_scoring_run(
        resume_hash=resume.resume_hash,
        workflow="score_jobs",
    )
    output = io.StringIO()

    class _Tee:
        def __init__(self, *streams):
            self.streams = streams

        def write(self, value):
            for stream in self.streams:
                stream.write(value)
            return len(value)

        def flush(self):
            for stream in self.streams:
                stream.flush()

    successful_score_count = 0
    failed_score_count = 0
    gemini_score_count = 0
    openai_score_count = 0
    cached_score_count = 0
    candidate_jobs: list[dict[str, Any]] = []
    jobs_skipped = 0
    screening_failures = 0
    quota_exhausted = False
    dbt_was_run = False

    try:
        with contextlib.redirect_stdout(
            _Tee(sys.stdout, output)
        ):
            cached_score_count = count_cached_canonical_scores(
                resume_hash=resume.resume_hash,
                model_name=None,
                prompt_version=MATCHER_PROMPT_VERSION,
                reuse_any_model=True,
            )

            screening_summary = screen_unscreened_jobs(
                resume_hash=resume.resume_hash,
                resume_profile=resume_profile,
                limit=limit,
                model_name=model_name,
            )

            candidate_jobs = load_candidate_jobs(
                resume_hash=resume.resume_hash,
                model_name=model_name,
                limit=limit,
                minimum_rule_score=minimum_rule_score,
                prompt_version=MATCHER_PROMPT_VERSION,
                reuse_any_model=True,
            )

            failed_keys = getattr(screening_summary, "failed_record_keys", ())
            screening_failures = len(failed_keys) if isinstance(failed_keys,(tuple,list,set,frozenset)) else 0
            print(f"Eligibility screening failures: {screening_failures}")
            use_openai_fallback = False

            for index, job in enumerate(
                candidate_jobs,
                start=1,
            ):
                from src.candidate_assessments import scoring_claim
                if screening_failed_for_job(screening_summary, job):
                    jobs_skipped += 1
                    log_scoring_skip(job,index,len(candidate_jobs),"screening_failed_this_execution")
                    continue
                claim_details = {}
                with scoring_claim(resume.resume_hash, job, details=claim_details) as acquired:
                    if not acquired:
                        jobs_skipped += 1
                        log_scoring_skip(job,index,len(candidate_jobs),claim_details.get("reason", "candidate_already_completed_or_claimed"),claim_details)
                        continue
                    title = job.get("title") or "Untitled job"
                    company = (
                        job.get("company_name")
                        or "Unknown company"
                    )
                    print(
                        f"Scoring {index}/{len(candidate_jobs)}: "
                        f"{title} | {company}"
                    )

                    log_job_identity(job, claim_details)
                    try:
                        if use_openai_fallback:
                            match = score_resume_against_job_openai(
                                resume_profile=resume_profile,
                                resume_text=resume.text,
                                job=job,
                                model_name=get_openai_model_name(),
                            )
                        else:
                            match = score_resume_against_job(
                                resume_profile=resume_profile,
                                resume_text=resume.text,
                                job=job,
                                model_name=model_name,
                            )

                    except ResumeMatcherError as error:
                        provider = (
                            "OpenAI"
                            if use_openai_fallback
                            else "Gemini"
                        )
                        log_scoring_failure(
                            job,
                            index,
                            len(candidate_jobs),
                            provider,
                            error,
                        )

                        if (
                            not use_openai_fallback
                            and (is_gemini_quota_exhausted(error) or exhausted_gemini_503(error))
                        ):
                            quota_exhausted = is_gemini_quota_exhausted(error)
                            print(
                                "Gemini daily quota is exhausted. Switching to OpenAI fallback."
                                if quota_exhausted else "Gemini service is unavailable after retries. Switching to OpenAI fallback."
                            )
                            use_openai_fallback = True

                            try:
                                match = score_resume_against_job_openai(
                                    resume_profile=resume_profile,
                                    resume_text=resume.text,
                                    job=job,
                                    model_name=get_openai_model_name(),
                                )
                            except ResumeMatcherError as fallback_error:
                                log_scoring_failure(
                                    job,
                                    index,
                                    len(candidate_jobs),
                                    "OpenAI",
                                    fallback_error,
                                )
                                failed_score_count += 1

                                if is_openai_quota_exhausted(
                                    fallback_error
                                ):
                                    print(
                                        "OpenAI quota is exhausted. "
                                        "Stopping the remaining scoring requests."
                                    )
                                    break

                                continue

                            save_job_score(
                                resume_hash=resume.resume_hash,
                                match=match,
                            )
                            successful_score_count += 1
                            openai_score_count += 1
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
                            failed_score_count += 1
                            print(
                                "OpenAI quota is exhausted. "
                                "Stopping the remaining scoring requests."
                            )
                            break

                        failed_score_count += 1
                        continue

                    save_job_score(
                        resume_hash=resume.resume_hash,
                        match=match,
                    )
                    successful_score_count += 1

                    if use_openai_fallback:
                        openai_score_count += 1
                    else:
                        gemini_score_count += 1

                    print(
                        "Saved score: "
                        f"{match.overall_score}% "
                        f"{match.recommendation.upper()} "
                        f"({match.analysis.confidence} confidence)"
                    )

            print(f"Scoring outcomes: selected={len(candidate_jobs)}; saved={successful_score_count}; failed={failed_score_count}; skipped={jobs_skipped}; screening_failures={screening_failures}")
            if successful_score_count:
                print("Refreshing dbt analytics...")
                run_dbt_build()
                dbt_was_run = True

        log_lines = tuple(
            line.strip()
            for line in output.getvalue().splitlines()
            if line.strip()
        )

        summary = ScoringSummary(
            resume_hash=resume.resume_hash,
            filename=resume.filename,
            word_count=resume.word_count,
            page_count=resume.page_count,
            model_name=model_name,
            profile_was_cached=profile_was_cached,
            candidate_count=len(candidate_jobs),
            cached_score_count=cached_score_count,
            successful_score_count=successful_score_count,
            failed_score_count=failed_score_count,
            gemini_score_count=gemini_score_count,
            openai_score_count=openai_score_count,
            new_score_count=successful_score_count,
            jobs_skipped=jobs_skipped,
            screening_failures=screening_failures,
            dbt_was_run=dbt_was_run,
            log_lines=log_lines,
        )

        finish_scoring_run(
            run_id,
            status="completed",
            model_name=model_name,
            candidates_selected=len(candidate_jobs),
            already_scored=cached_score_count,
            scores_saved=successful_score_count,
            jobs_failed=failed_score_count,
            gemini_scores_saved=gemini_score_count,
            openai_scores_saved=openai_score_count,
            quota_exhausted=quota_exhausted,
            log_lines=log_lines,
        )

        return summary

    except (
        ResumeExtractionError,
        JobRecommendationServiceError,
    ):
        log_lines = tuple(
            line.strip()
            for line in output.getvalue().splitlines()
            if line.strip()
        )
        finish_scoring_run(
            run_id,
            status="failed",
            model_name=model_name,
            candidates_selected=len(candidate_jobs),
            already_scored=cached_score_count,
            scores_saved=successful_score_count,
            jobs_failed=failed_score_count,
            gemini_scores_saved=gemini_score_count,
            openai_scores_saved=openai_score_count,
            quota_exhausted=quota_exhausted,
            log_lines=log_lines,
        )
        raise

    except Exception as error:
        print(
            "Unexpected Streamlit workflow error: "
            f"{type(error).__name__}: {error}"
        )
        log_lines = tuple(
            line.strip()
            for line in output.getvalue().splitlines()
            if line.strip()
        )
        finish_scoring_run(
            run_id,
            status="failed",
            model_name=model_name,
            candidates_selected=len(candidate_jobs),
            already_scored=cached_score_count,
            scores_saved=successful_score_count,
            jobs_failed=failed_score_count,
            gemini_scores_saved=gemini_score_count,
            openai_scores_saved=openai_score_count,
            quota_exhausted=quota_exhausted,
            log_lines=log_lines,
        )
        raise JobRecommendationServiceError(
            "The scoring workflow could not be completed. "
            "Please try again later."
        ) from error



def rescore_saved_job(record_key: str, resume_hash: str) -> None:
    """Explicit single-job action; failure leaves the previous active assessment intact."""
    from src import database
    from src.candidate_assessments import candidate_for_resume, latest_candidate_resume, scoring_claim
    from src.score_jobs_ai import load_cached_resume_text
    with database.get_connection() as c:
        resume_hash = latest_candidate_resume(c, resume_hash)
        if not candidate_for_resume(c, resume_hash):
            raise JobRecommendationServiceError('Register the candidate before manual rescoring')
        cur = c.execute('SELECT * FROM raw_jobs WHERE record_key=?', [record_key])
        names = [x[0] for x in cur.description]
        row = cur.fetchone()
        if row is None:
            raise JobRecommendationServiceError('Job no longer exists')
        job = dict(zip(names, row))
    profile = load_cached_profile(resume_hash, get_model_name())
    text = load_cached_resume_text(resume_hash)
    if profile is None or text is None:
        raise JobRecommendationServiceError('Cached resume text and profile are required')
    with scoring_claim(resume_hash, job, manual=True) as acquired:
        if not acquired:
            raise JobRecommendationServiceError('Another scoring run already owns this job')
        try:
            match = score_resume_against_job(profile, job, resume_text=text, model_name=get_model_name())
        except ResumeMatcherError as exc:
            log_scoring_failure(job, 1, 1, 'Gemini', exc)
            if not (is_gemini_daily_quota_error(exc) or exhausted_gemini_503(exc) or 'PERMISSION_DENIED' in str(exc) or 'denied access' in str(exc).casefold()):
                raise
            match = score_resume_against_job_openai(profile, job, resume_text=text, model_name=get_openai_model_name())
        save_job_score(resume_hash=resume_hash, match=match)
