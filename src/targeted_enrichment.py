"""Select first attempts and transient retries using the shared target-role rules."""
from src.job_title_filter import classify_job_title
from src.repositories.recommendation_repository import description_state, job_age_days

# Deferred employer-lookup retries for postings first discovered this recently
# are ordered ahead of older retries (newest first). Eligibility is unchanged.
RECENT_RETRY_DAYS = 14


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


def targeted_retry(job, *, include_employer=False, now=None):
    from src.enrich_jobs import needs_official_resolution, OFFICIAL_RETRY_STATUSES
    transient = job.get('official_url_status') in {'SEARCH_DEFERRED', 'INSUFFICIENT_IDENTITY'} or (
        job.get('official_url_status') in {'ERROR', 'BLOCKED'}
        and job.get('official_url_source') == 'search')
    # Daily employer-only work can have a complete JD or saved assessment.
    # Negative and ambiguous results are revisited only after the SAME cooldown.
    relevant = relevant_missing_jd(job) or include_employer
    permitted = transient or (include_employer and job.get('official_url_status') in OFFICIAL_RETRY_STATUSES)
    return relevant and permitted and needs_official_resolution(job, now=now)


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


def processing_groups(jobs):
    """Exact aliases first, then only unambiguous trusted identity merging."""
    from src.actionability import trusted_key
    exact_groups = {}
    for original in jobs:
        key = original.get('exact_posting_key') or original['record_key']
        exact_groups.setdefault(key, []).append(original)
    groups = {}
    for exact_key, members in exact_groups.items():
        keys = {trusted_key(j) for j in members} - {''}
        # Exact aliases stay together even when only ONE email occurrence has
        # verified metadata. Cross-source merging requires one trusted key.
        key = 'verified:' + next(iter(keys)) if len(keys) == 1 else exact_key
        groups.setdefault(key, []).extend(members)
    return groups


def plan_daily_enrichment(jobs, limit, *, retry_failed=True, minimum_words=80, now=None,
                          actionability_only=False):
    """One exact-identity queue for execution and diagnostics; no I/O.

    Employer verification is independent of description retrieval. Historical
    negative/ambiguous searches use the existing seven-day official retry policy.
    Oldest attempts sort first so a completed unsuccessful lookup rotates behind
    the untouched backlog. Fingerprints never merge postings.
    """
    from collections import Counter
    from datetime import datetime, timezone
    from src.actionability import trusted_key, is_fit_recommended
    from src.enrich_jobs import (needs_description_enrichment, needs_official_resolution,
                                FAILED_STATUSES, official_retry_cutoff, as_utc_datetime)

    if limit < 0:
        raise ValueError('Batch limit cannot be negative')
    now = now or datetime.now(timezone.utc)
    groups = processing_groups(jobs)
    stats = dict(inventory=len(groups), fresh_description=0, eligible_deferred_retries=0,
                 employer_verification_pending=0, full_jd_employer_pending=0,
                 fit_recommended_employer_pending=0, cooldown=0, excluded=0,
                 retry_disabled=0, runnable=0, selected=0)
    pending_sources = Counter({str(j.get('source') or 'unknown'): 0 for j in jobs})
    all_new_pending_sources = pending_sources.copy()
    candidates = []
    oldest = datetime.min.replace(tzinfo=timezone.utc)
    for key, members in groups.items():
        # Keep a usable JD and authoritative identity when an exact posting has
        # several email occurrences. Only scheduling state is shared otherwise.
        job = dict(max(members, key=lambda j: (
            bool(trusted_key(j)), description_state(j) == 'FULL_JD',
            as_utc_datetime(j.get('description_updated_at')) or oldest,
            as_utc_datetime(j.get('previous_attempted_at')) or oldest,
            str(j.get('record_key') or ''))))
        latest = max(members, key=lambda j: (
            as_utc_datetime(j.get('previous_attempted_at')) or oldest,
            as_utc_datetime(j.get('official_url_resolved_at')) or oldest))
        # First discovery is the earliest alias, so a new alert never makes an
        # old posting look recent. Any unknown alias date means unknown.
        discovered = [as_utc_datetime(j.get('discovered_at')) for j in members]
        first_discovered = None if any(d is None for d in discovered) else min(discovered)
        schedule = dict(job)
        if not trusted_key(job):
            for field in ('official_url_status', 'official_url_source',
                          'official_url_resolved_at', 'previous_attempted_at'):
                schedule[field] = latest.get(field)
        # Preserve personal history, including aliases, before considering work.
        classification = classify_job_title(job.get('title'))
        is_new = all(str(j.get('application_status') or 'new').lower() == 'new' for j in members)
        if is_new and not trusted_key(job):
            all_new_pending_sources[str(job.get('source') or 'unknown')] += 1
        conflicting_identity = len({trusted_key(j) for j in members} - {''}) > 1
        relevant = (is_new and not conflicting_identity
                    and classification.category in {'STRONG_MATCH', 'POSSIBLE_MATCH'}
                    and classification.matched_pattern is not None
                    and not job.get('critical_skill_gaps')
                    and (not job.get('has_current_complete_ai_assessment') or is_fit_recommended(job)))
        if not relevant:
            stats['excluded'] += 1
            continue
        pending = not trusted_key(job)
        full = description_state(job) == 'FULL_JD'
        recommended = is_fit_recommended(job)
        if pending:
            stats['employer_verification_pending'] += 1
            pending_sources[str(job.get('source') or 'unknown')] += 1
            stats['full_jd_employer_pending'] += full
            stats['fit_recommended_employer_pending'] += recommended
        schedule['requires_trusted_posting_key'] = pending
        official_due = pending and needs_official_resolution(schedule, now=now)
        cooling = pending and not official_due
        stats['cooldown'] += cooling
        attempted = any(int(j.get('attempt_count') or 0) > 0 or j.get('previous_status') for j in members)
        status = schedule.get('official_url_status')
        # No prior official status means an employer FIRST lookup, even if the
        # source JD has previously been fetched. Otherwise only permitted retry
        # statuses qualify; unknown historical statuses are not broad retries.
        employer_first = official_due and (not status or status == 'FOUND_VERIFIED')
        historical = official_due and targeted_retry(schedule, include_employer=pending, now=now)
        description_due = (relevant_missing_jd(job)
                           and needs_description_enrichment(job, minimum_words, True))
        last_attempt = as_utc_datetime(latest.get('previous_attempted_at'))
        description_retry = (description_due and attempted and bool(trusted_key(job))
                             and latest.get('previous_status') in FAILED_STATUSES
                             and (last_attempt is None or last_attempt <= official_retry_cutoff(now)))
        if (description_due and attempted and trusted_key(job)
                and latest.get('previous_status') in FAILED_STATUSES
                and last_attempt is not None and last_attempt > official_retry_cutoff(now)
                and not cooling):
            stats['cooldown'] += 1
        historical = historical or description_retry
        fresh_description = description_due and not attempted
        stats['fresh_description'] += fresh_description
        stats['eligible_deferred_retries'] += historical
        if historical and not retry_failed:
            stats['retry_disabled'] += 1
        allowed_official = employer_first or (pending and historical and retry_failed)
        # Fresh source retrieval can run in a search cooldown, but cannot start
        # another employer lookup. Source-only historical retries retain the
        # existing transient selector, rather than indiscriminately retrying.
        runnable = fresh_description or allowed_official or (description_retry and retry_failed)
        if actionability_only:
            runnable = allowed_official and recommended
        if not runnable:
            continue
        job.update(needs_description_enrichment=bool(description_due and not full and not actionability_only
                                                    and (not attempted or retry_failed)),
                   needs_official_resolution=bool(allowed_official),
                   requires_trusted_posting_key=pending,
                   daily_queue_kind=('fresh_description' if fresh_description else
                                     'deferred_retry' if historical else 'employer_first'),
                   daily_queue_last_attempt=latest.get('previous_attempted_at'),
                   daily_queue_first_discovered_at=first_discovered,
                   exact_posting_key=job.get('exact_posting_key') or job['record_key'])
        candidates.append(job)
    # Round-robin fresh and deferred lanes within ONE final bound. A busy fresh
    # stream cannot starve eligible history, nor can history starve new alerts.
    def priority(j):
        return (as_utc_datetime(j.get('daily_queue_last_attempt')) or oldest,
                not is_fit_recommended(j),
                classify_job_title(j.get('title')).category != 'STRONG_MATCH',
                -int(j.get('rule_score') or 0), str(j['record_key']))
    from datetime import timedelta
    reference = as_utc_datetime(now) or now
    recent_cutoff = reference - timedelta(days=RECENT_RETRY_DAYS)

    def retry_priority(j):
        # Recent tier first (newest discovery first); everything else, including
        # unknown discovery dates, keeps the existing oldest-attempt order.
        first = j.get('daily_queue_first_discovered_at')
        if first is not None and recent_cutoff <= first <= reference:
            return (0, -first.timestamp(), *priority(j))
        return (1, 0.0, *priority(j))
    fresh = sorted((j for j in candidates if j['daily_queue_kind'] != 'deferred_retry'), key=priority)
    retries = sorted((j for j in candidates if j['daily_queue_kind'] == 'deferred_retry'), key=retry_priority)
    ordered = []
    for index in range(max(len(fresh), len(retries))):
        if index < len(fresh): ordered.append(fresh[index])
        if index < len(retries): ordered.append(retries[index])
    selected = ordered[:limit]
    stats.update(runnable=len(candidates), selected=len(selected),
                 pending_employer_by_source=dict(sorted(pending_sources.items())),
                 pending_employer_all_new_by_source=dict(sorted(all_new_pending_sources.items())),
                 selected_by_source=dict(sorted(Counter(j['source'] for j in selected).items())))
    return selected, stats


def plan_recent_hidden_recovery(jobs, visible_jobs, limit, *, days=5, now=None,
                                retry_failed=True, minimum_words=80):
    """Pure scoped recovery plan. No historical visibility is inferred.

    Hidden means absent from the unrestricted All view now, not merely absent
    from Recommended or a user-filtered page. First discovery across aliases
    must fall within the rolling window; unknown dates are withheld.
    """
    from collections import Counter
    from datetime import datetime, timedelta, timezone
    from src.repositories.recommendation_repository import normalize_datetime
    from src.actionability import trusted_key, activity_check_due
    if days < 1 or limit < 0:
        raise ValueError('Positive recovery days and nonnegative limit required')
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)
    visible_records = {j['record_key'] for j in visible_jobs}
    groups = processing_groups(jobs)
    hidden = {}
    discovery = {}
    source_counts = {str(j.get('source') or 'unknown'): dict(hidden=0, relevant=0,
                     employer_verification=0, description_enrichment=0,
                     activity_checks_due=0, cooldown=0, runnable=0, selected=0) for j in jobs}
    excluded_older = excluded_unknown_date = preserved_history = 0
    for key, members in groups.items():
        dates = [normalize_datetime(j.get('discovered_at')) for j in members]
        if any(d is None for d in dates):
            excluded_unknown_date += 1
            continue
        first = min(dates)
        if first < cutoff or max(dates) > now:
            excluded_older += 1
            continue
        if any(str(j.get('application_status') or 'new').lower() != 'new' for j in members):
            preserved_history += 1
            continue
        if any(j['record_key'] in visible_records for j in members):
            continue
        hidden[key] = members
        discovery[key] = first
    inventory = [j for members in hidden.values() for j in members]
    runnable, stats = plan_daily_enrichment(inventory, len(inventory),
        retry_failed=retry_failed, minimum_words=minimum_words, now=now)
    # A group attribution is for accounting only, not a source quota or filter.
    record_group = {j['record_key']: key for key, members in hidden.items() for j in members}
    pending_records = []
    for key, members in hidden.items():
        _, item_stats = plan_daily_enrichment(members, len(members),
            retry_failed=retry_failed, minimum_words=minimum_words, now=now)
        representative = max(members, key=lambda j: (bool(trusted_key(j)),
            description_state(j) == 'FULL_JD', str(j['record_key'])))
        counts = source_counts[str(representative.get('source') or 'unknown')]
        relevant = item_stats['inventory'] - item_stats['excluded'] > 0
        if relevant and not trusted_key(representative):
            pending_records.append(representative['record_key'])
        counts['hidden'] += 1
        counts['relevant'] += relevant
        counts['employer_verification'] += relevant and not trusted_key(representative)
        counts['description_enrichment'] += relevant and description_state(representative) != 'FULL_JD'
        counts['activity_checks_due'] += relevant and activity_check_due(representative, now=now)
        counts['cooldown'] += item_stats['cooldown'] > 0
    runnable.sort(key=lambda j: (-discovery[record_group[j['record_key']]].timestamp(), str(j['record_key'])))
    selected = runnable[:limit]
    selected_groups = {record_group[j['record_key']] for j in selected}
    scope = tuple(sorted(j['record_key'] for key in selected_groups for j in hidden[key]))
    for row in runnable:
        source_counts[str(row.get('source') or 'unknown')]['runnable'] += 1
    for row in selected:
        source_counts[str(row.get('source') or 'unknown')]['selected'] += 1
    stats.update(recovery_mode='recent_hidden', recovery_days=days,
                 recovery_as_of=now.isoformat(), recovery_cutoff=cutoff.isoformat(),
                 recovery_hidden=len(hidden), recovery_relevant=sum(v['relevant'] for v in source_counts.values()),
                 recovery_by_source=source_counts, recovery_record_keys=scope,
                 recovery_pending_record_keys=tuple(sorted(pending_records)),
                 older_groups_excluded=excluded_older, unknown_discovery_excluded=excluded_unknown_date,
                 recent_personal_history_preserved=preserved_history, selected=len(selected),
                 selected_by_source=dict(Counter(j['source'] for j in selected)))
    return selected, stats
