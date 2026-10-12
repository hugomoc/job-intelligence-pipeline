"""Explicit per-session ownership; no global or cross-user recommendation cache."""
from dataclasses import replace
from src.repositories.recommendation_repository import (
    classify_job_review_bucket, calculate_enrichment_priority, reviewable_job_metrics,
    prepare_job_display,
)


def invalidate_data(state, reason):
    """A real pipeline mutation, resume switch, or explicit user refresh."""
    state['jobs_snapshot_dirty'] = True
    state['jobs_snapshot_version'] = state.get('jobs_snapshot_version', 0) + 1
    state['dataset_version'] = state.get('dataset_version', 0) + 1
    state['jobs_snapshot_invalidation_reason'] = reason
    state.pop('scoring_backlog_snapshot', None)
    state.pop('operations_snapshot', None)


def jobs_snapshot(state, resume_hash, loader):
    if ('jobs_snapshot' not in state or state.get('jobs_snapshot_dirty')
            or state.get('jobs_snapshot_resume_hash') != resume_hash):
        jobs = loader(resume_hash=resume_hash)
        state['jobs_snapshot'] = jobs
        state['jobs_snapshot_resume_hash'] = resume_hash
        state['jobs_snapshot_dirty'] = False
        state['jobs_snapshot_version'] = state.get('jobs_snapshot_version', 0) + 1
    return state['jobs_snapshot']


def scoring_snapshot(state, resume_hash, minimum_rule_score, loader):
    key = (resume_hash, minimum_rule_score, state.get('dataset_version', 0))
    cached = state.get('scoring_backlog_snapshot')
    if cached is None or cached['key'] != key:
        cached = {'key': key, 'status': loader(resume_hash=resume_hash,
            minimum_rule_score=minimum_rule_score)}
        state['scoring_backlog_snapshot'] = cached
    return cached['status']


def patch_status(state, record_key, status, writer):
    """Callback work: one persistence operation, then exact fresh overlays.

    Never load jobs, score, enrich, fetch actionability, or request a second rerun.
    """
    jobs = state.get('jobs_snapshot', [])
    change = writer(record_key=record_key, status=status, reviewable_jobs=jobs)
    overlays = {row['record_key']: row for row in change.overlays}
    from src.actionability import settings, now_utc
    config, now = settings(), now_utc()
    for job in jobs:
        if job['record_key'] not in overlays: continue
        job.update(overlays[job['record_key']])
        job['job_review_bucket'] = classify_job_review_bucket(job, actionability_config=config, now=now)
        priority = calculate_enrichment_priority(job)
        job['enrichment_priority_score'] = priority.score
        job['enrichment_priority_tier'] = priority.tier
        job['enrichment_priority_reason'] = priority.reason
    if change.record_keys:
        state['jobs_snapshot_version'] = state.get('jobs_snapshot_version', 0) + 1
    cached = state.get('scoring_backlog_snapshot')
    if cached and change.record_keys:
        summary = cached['status']
        keys = summary.candidate_posting_keys
        if status in {'applied', 'removed'} and keys is not None:
            remaining = tuple(key for key in keys if key != change.exact_posting_key)
            cached['status'] = replace(summary, unscored_candidates=len(remaining),
                candidate_posting_keys=remaining)
        elif summary.resume_hash is not None:
            # Restoring NEW can add previously excluded candidates. Recount only
            # the sidebar on demand; never invalidate/reload the jobs snapshot.
            state.pop('scoring_backlog_snapshot', None)
    return change


def actionability_saved(state):
    """refresh_daily already patched rows from persisted results: bump revision.

    Dataset/fit/status caches do not change. Never reload recommendations merely
    to read back the same actionability upserts. Dependent view metrics see the
    new revision and rederive their inexpensive dictionary counts.
    """
    from src.actionability import settings, now_utc
    config, now = settings(), now_utc()
    for job in state.get('jobs_snapshot', []):
        job['job_review_bucket'] = classify_job_review_bucket(job, actionability_config=config, now=now)
    state['jobs_snapshot_version'] = state.get('jobs_snapshot_version', 0) + 1


def operation_snapshot(state, resume_hash, minimum_rule_score, jobs, backlog, loader):
    key = (resume_hash, minimum_rule_score, state.get('dataset_version', 0))
    cached = state.get('operations_snapshot')
    if cached is None or cached['key'] != key:
        metrics = loader(resume_hash=resume_hash, minimum_rule_score=minimum_rule_score,
            reviewable_jobs=jobs, unscored_candidates=backlog.unscored_candidates)
        cached = {'key': key, 'metrics': metrics, 'jobs_version': None}
        state['operations_snapshot'] = cached
    if cached['jobs_version'] != state.get('jobs_snapshot_version', 0):
        derived = reviewable_job_metrics(jobs)
        # Operational SQL counts are stable on status/actionability-only changes.
        for field in ('raw_jobs', 'exact_postings', 'enrichment_attempts', 'ai_score_eligible'):
            derived.pop(field, None)
        cached['metrics'].update(derived)
        cached['jobs_version'] = state.get('jobs_snapshot_version', 0)
    cached['metrics']['ai_score_eligible'] = backlog.unscored_candidates
    return cached['metrics']


def display_snapshot(state):
    version = state.get('jobs_snapshot_version', 0)
    if state.get('jobs_display_version') != version:
        state['jobs_display_snapshot'] = prepare_job_display(state.get('jobs_snapshot', []))
        state['jobs_display_version'] = version
    return state.get('jobs_display_snapshot', [])
