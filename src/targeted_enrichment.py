"""Select first attempts and transient retries using the shared target-role rules."""
from src.job_title_filter import classify_job_title
from src.repositories.recommendation_repository import description_state, job_age_days


def relevant_missing_jd(job):
    return (
        str(job.get('application_status') or 'new').lower() == 'new'
        and classify_job_title(job.get('title')).category in {'STRONG_MATCH', 'POSSIBLE_MATCH'}
        and classify_job_title(job.get('title')).matched_pattern is not None
        and not job.get('critical_skill_gaps')
        and description_state(job) != 'FULL_JD'
        and not job.get('has_current_complete_ai_assessment')
        and job.get('ai_score') is None
    )


def first_attempt(job):
    return (relevant_missing_jd(job) and int(job.get('attempt_count') or 0) == 0
            and not job.get('previous_status'))


def targeted_retry(job):
    transient = job.get('official_url_status') == 'SEARCH_DEFERRED' or (
        job.get('official_url_status') in {'ERROR', 'BLOCKED'}
        and job.get('official_url_source') == 'search')
    return relevant_missing_jd(job) and transient


def select_targeted_retries(jobs):
    # Exact keys are the only duplicate grouping here; fingerprints are hints.
    selected = {}
    for job in sorted((j for j in jobs if targeted_retry(j)), key=lambda j: (
        classify_job_title(j.get('title')).category == 'STRONG_MATCH',
        -(job_age_days(j) if job_age_days(j) is not None else 100000),
        not bool(j.get('official_job_url')),
        int(j.get('rule_score') or 0), int(j.get('title_match_score') or 0),
        bool(j.get('needs_review')),
        j.get('official_url_status') == 'SEARCH_DEFERRED',
    ), reverse=True):
        key = job.get('exact_posting_key') or job['record_key']
        selected.setdefault(key, job)
    return list(selected.values())
