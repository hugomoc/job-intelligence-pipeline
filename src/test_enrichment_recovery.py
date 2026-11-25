"""Offline regressions for bounded daily discovery and actionability recovery."""
import json
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import duckdb
import httpx

from src import enrich_jobs
from src.actionability import Settings, activity_check_due, refresh, trusted_key
from src.enrichment import official_job_resolver as resolver
from src.enrichment.public_search import SearchRequestBudget, provider_metrics
from src.targeted_enrichment import plan_daily_enrichment
from src.ui import daily_workflow_service as service
from src.test_actionability import job as actionable_job, client as activity_client, page
from src.test_daily_workflow_service import disposable_enrichment_database
from src.test_enrich_jobs_official_retry import DESCRIPTION, job_posting_html

NOW = datetime(2026, 11, 19, tzinfo=timezone.utc)
OLD = NOW - timedelta(days=8)


def job(key='a', *, full=False, **kwargs):
    description = DESCRIPTION if full else 'Short alert'
    result = dict(record_key=key, exact_posting_key='exact:' + key, source='lensa',
                  title='Data Engineer', company_name='Example Co', location='Remote',
                  apply_url='https://lensa.com/job/' + key, description=description,
                  current_word_count=len(description.split()), raw_description_word_count=len(description.split()),
                  description_quality_signals=full, description_state='FULL_JD' if full else 'PARTIAL_JD',
                  application_status='new', ai_score=None, attempt_count=0, previous_status=None)
    result.update(kwargs)
    return result


def retry(key='retry', *, full=False, status='SEARCH_DEFERRED', when=OLD, **kwargs):
    return job(key, full=full, attempt_count=1, previous_status='blocked',
               official_url_status=status, official_url_source='search',
               previous_attempted_at=when, official_url_resolved_at=when, **kwargs)


def plan(jobs, limit=25, **kwargs):
    return plan_daily_enrichment(jobs, limit, now=NOW, **kwargs)


def test_default_daily_retries_share_final_bound_and_exact_identity():
    jobs = [job('fresh-' + str(n)) for n in range(8)] + [retry('retry-' + str(n)) for n in range(8)]
    jobs.append(dict(jobs[-1], record_key='duplicate-email', duplicate_fingerprint='same-fingerprint'))
    selected, stats = plan(jobs, 5)
    assert len(selected) == 5 and stats['runnable'] == 16
    assert sum(j['daily_queue_kind'] == 'deferred_retry' for j in selected) == 2
    assert len({j['exact_posting_key'] for j in selected}) == len(selected)
    assert stats['fresh_description'] == 8 and stats['eligible_deferred_retries'] == 8
    assert sum(stats['selected_by_source'].values()) == 5


def test_full_jd_employer_first_and_permitted_negative_retries():
    for status in (None, 'OFFICIAL_NOT_FOUND', 'AMBIGUOUS', 'SEARCH_DEFERRED', 'INSUFFICIENT_IDENTITY'):
        source_job = job(full=True) if status is None else retry(full=True, status=status)
        selected, stats = plan([source_job])
        assert len(selected) == 1
        assert selected[0]['needs_official_resolution']
        assert not selected[0]['needs_description_enrichment']
        assert stats['full_jd_employer_pending'] == 1


def test_cooldowns_apply_even_to_transient_retries_and_duplicate_email():
    for status in enrich_jobs.OFFICIAL_RETRY_STATUSES:
        recent = retry(status=status, when=NOW - timedelta(days=1), full=True)
        alias = dict(recent, record_key='alias', attempt_count=0, previous_status=None,
                     previous_attempted_at=None, official_url_resolved_at=None, official_url_status=None)
        selected, stats = plan([alias, recent])
        assert selected == [] and stats['cooldown'] == 1
        assert stats['eligible_deferred_retries'] == 0
        selected, _ = plan([retry(status=status, full=True, when=NOW - timedelta(days=7))])
        assert len(selected) == 1


def test_description_retrieval_cannot_bypass_official_cooldown():
    fresh = job(official_url_status='OFFICIAL_NOT_FOUND', official_url_resolved_at=NOW)
    selected, stats = plan([fresh])
    assert len(selected) == 1 and stats['cooldown'] == 1
    assert selected[0]['needs_description_enrichment']
    assert not selected[0]['needs_official_resolution']
    assert not resolver.should_attempt_official_resolution(selected[0], None, None)


def test_retries_can_be_disabled_without_blocking_fresh_identity():
    selected, stats = plan([retry(), job('new-full', full=True)], retry_failed=False)
    assert [j['record_key'] for j in selected] == ['new-full']
    assert stats['eligible_deferred_retries'] == 1 and stats['retry_disabled'] == 1


def test_pending_diagnostics_include_invisible_sources_and_exclusions():
    inventory = [retry('lensa'), job('full', full=True, source='builtin'),
                 retry('cool', when=NOW, source='glassdoor'),
                 job('removed', application_status='removed'),
                 job('applied', application_status='applied'),
                 job('unrelated', title='Registered Nurse'),
                 job('hard-gap', critical_skill_gaps=['specialized role'])]
    selected, stats = plan(inventory, 2)
    assert stats['inventory'] == 7 and stats['excluded'] == 4
    assert stats['runnable'] == 2 and stats['selected'] == 2
    assert stats['employer_verification_pending'] == 3
    assert stats['pending_employer_by_source'] == {'builtin': 1, 'glassdoor': 1, 'lensa': 1}
    assert not any(j['record_key'] in {'removed', 'applied', 'unrelated', 'hard-gap'} for j in selected)


def test_unsuccessful_attempt_rotates_and_repeated_processing_is_idempotent():
    inventory = [retry('first', when=OLD - timedelta(days=2)), retry('second', when=OLD)]
    selected, _ = plan(inventory, 1)
    assert selected[0]['record_key'] == 'first'
    inventory[0].update(previous_attempted_at=NOW, official_url_resolved_at=NOW,
                        official_url_status='OFFICIAL_NOT_FOUND', attempt_count=2)
    selected, _ = plan(inventory, 1)
    assert selected[0]['record_key'] == 'second'
    inventory[1].update(previous_attempted_at=NOW, official_url_resolved_at=NOW,
                        official_url_status='AMBIGUOUS', attempt_count=2)
    assert plan(inventory)[0] == []


def test_fit_recommended_full_jd_is_routed_even_for_linkedin():
    fit = job(full=True, source='linkedin', ai_score=85, recommendation='apply',
              has_current_complete_ai_assessment=True)
    selected, stats = plan([fit, job('unscored', full=True)], actionability_only=True)
    assert [j['record_key'] for j in selected] == ['a']
    assert stats['fit_recommended_employer_pending'] == 1
    assert not trusted_key(fit)


def test_full_jd_source_not_refetched_and_verified_identity_then_activity():
    source_job = job(full=True, source='linkedin', ai_score=85, recommendation='apply',
                     has_current_complete_ai_assessment=True)
    selected, _ = plan([source_job])
    url = 'https://job-boards.greenhouse.io/example/jobs/123'
    calls = []
    def handler(request):
        calls.append(str(request.url))
        if request.url.host == 'api.tavily.com':
            return httpx.Response(200, json={'results': [{'url': url}]})
        assert str(request.url) == url, 'Must not fetch the source page for FULL_JD'
        return httpx.Response(200, text=job_posting_html(), headers={'content-type': 'text/html'})
    with disposable_enrichment_database(), \
         patch.dict(os.environ, {'TAVILY_API_KEY': 'offline', 'SERPER_API_KEY': '', 'PUBLIC_SEARCH_PROVIDER': ''}), \
         patch.object(resolver, 'discover_ats_candidates', return_value=[]), \
         patch('src.enrichment.employer_careers.discover_employer_candidates', return_value=[]), \
         patch.object(enrich_jobs, 'fetch_job_description', side_effect=AssertionError('Unexpected source refetch')), \
         httpx.Client(transport=httpx.MockTransport(handler)) as client:
        client.search_request_budget = SearchRequestBudget(1)
        result = enrich_jobs.process_enrichment_job(selected[0], client, resolver.PublicSearchCircuit(),
                                                   defer_description_write=True)
        assert result.official_resolution.status == 'FOUND_VERIFIED'
        enrich_jobs.save_enrichment_attempt(selected[0], result.result,
            status=result.stored_status, official_resolution=result.official_resolution,
            identity_validation=result.identity_validation, description_update=result.pending_description)
        from src import database
        with database.get_connection() as connection:
            cursor = connection.execute('SELECT * FROM job_enrichment_attempts WHERE record_key=?', ['a'])
            stored = dict(zip([d[0] for d in cursor.description], cursor.fetchone()))
        selected[0].update(stored)
        assert trusted_key(selected[0]) == 'greenhouse:123'
        # A separate fetched activity response is required, never inferred from
        # lookup success. This mocked page has an actual application control.
        with activity_client(page(company='Example Co')) as activity, duckdb.connect(':memory:') as db:
            metrics = refresh(selected, db, activity, Settings(), NOW)
        assert metrics['checks'] == 1 and metrics['ACTIVE'] == 1
    assert all('lensa' not in url and 'linkedin' not in url for url in calls)


def test_missing_key_does_not_consume_activity_budget_or_create_cache():
    jobs = [actionable_job(record_key='missing', official_url_status=None, ai_score=100), actionable_job()]
    with duckdb.connect(':memory:') as db, activity_client(page()) as client:
        metrics = refresh(jobs, db, client, Settings(batch_size=1), NOW)
        assert metrics['checks'] == 1 and metrics['missing_key'] == 1 and metrics['ACTIVE'] == 1
        assert db.execute('SELECT record_key FROM job_actionability').fetchall() == [('a',)]
        again = refresh(jobs, db, client, Settings(batch_size=1), NOW)
        assert again['checks'] == 0 and again['cache_hits'] == 1
        assert not activity_check_due(jobs[0], Settings(), NOW)


def test_actual_search_budget_counts_failed_fallback_and_cached_responses():
    calls = []
    def handle(request):
        calls.append(request.url.host)
        if request.url.host == 'api.tavily.com':
            return httpx.Response(432)
        return httpx.Response(200, json={'organic': []})
    with patch.dict(os.environ, {'TAVILY_API_KEY': 'offline', 'SERPER_API_KEY': 'offline', 'PUBLIC_SEARCH_PROVIDER': ''}), \
         httpx.Client(transport=httpx.MockTransport(handle)) as client:
        client.search_request_budget = SearchRequestBudget(2)
        circuit = resolver.PublicSearchCircuit()
        assert circuit.search(job(), client) == []
        assert len(calls) == client.search_request_budget.attempted == 2
        assert circuit.state('tavily')['open']
        assert circuit.search(job(), client) == []  # Successful Serper cache is free.
        assert len(calls) == 2
        try:
            circuit.search(job('different', company_name='Another Co'), client)
        except resolver.SearchDeferredError:
            pass
        else:
            raise AssertionError('Budget must stop new requests')
        assert len(calls) == 2 and circuit.deferred == 1
        assert sum(m['search_requests'] for m in provider_metrics(client).values()) == 2


def test_budget_blocks_fallback_after_failed_last_request():
    requests = []
    with patch.dict(os.environ, {'TAVILY_API_KEY': 'offline', 'SERPER_API_KEY': 'offline', 'PUBLIC_SEARCH_PROVIDER': ''}), \
         httpx.Client(transport=httpx.MockTransport(lambda r: (requests.append(r), httpx.Response(503))[1])) as client:
        client.search_request_budget = SearchRequestBudget(1)
        circuit = resolver.PublicSearchCircuit()
        try:
            circuit.search(job(), client)
        except resolver.SearchDeferredError:
            pass
        else:
            raise AssertionError('Fallback must not exceed budget')
        assert len(requests) == 1 and circuit.deferred == 1
        assert not circuit.state('serper')['open']  # Budget exhaustion is not provider illness.


def test_zero_search_budget_and_circuit_breaker_are_independent():
    calls = []
    with patch.dict(os.environ, {'TAVILY_API_KEY': 'offline', 'SERPER_API_KEY': '', 'PUBLIC_SEARCH_PROVIDER': ''}), \
         httpx.Client(transport=httpx.MockTransport(lambda r: (calls.append(r), httpx.Response(503))[1])) as client:
        client.search_request_budget = SearchRequestBudget(5)
        circuit = resolver.PublicSearchCircuit(threshold=2)
        for index in range(3):
            try:
                circuit.search(job(str(index)), client)
            except (resolver.PublicSearchError, resolver.SearchDeferredError):
                pass
        assert len(calls) == 2 and circuit.open and circuit.deferred == 1
        client.search_request_budget = SearchRequestBudget(0)
        try:
            resolver.PublicSearchCircuit().search(job(), client)
        except resolver.SearchDeferredError:
            pass
        assert len(calls) == 2


def test_daily_service_default_selects_history_and_bounds_actual_requests():
    inventory = [retry('old'), job('fresh', full=True)]
    with patch.object(service, 'load_jobs_to_enrich', return_value=inventory) as loader, \
         patch.object(service, 'load_verified_reuse_candidates', return_value=[]), \
         patch.object(service, 'process_enrichment_batch') as process, \
         patch.object(service, 'create_http_client') as factory:
        client = httpx.Client(transport=httpx.MockTransport(lambda r: (_ for _ in ()).throw(AssertionError('No network'))))
        factory.return_value = client
        process.return_value = enrich_jobs.EnrichmentBatchSummary(2, 2, 0,
            enrich_jobs.default_enrichment_totals(), False, 0, ())
        summary = service.run_description_enrichment(limit=2, resume_hash='resume', search_request_limit=3)
        assert summary.jobs_selected == summary.jobs_processed == 2
        assert summary.queue_statistics['eligible_deferred_retries'] == 1
        assert process.call_args.kwargs['client'].search_request_budget.maximum == 3
        assert loader.call_args.kwargs['inventory_only'] is True
        assert set(summary.queue_statistics['pending_employer_by_source']) >= {'lensa', 'builtin', 'bebee', 'glassdoor'}


def test_combined_daily_workflow_reuses_completed_enrichment():
    summary = service.EnrichmentSummary(0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, ())
    from src.score_backlog import BacklogSummary
    scored = BacklogSummary('resume', 'model', 0, 0, 0, 0, 0, 0, False, False)
    with patch.object(service, 'start_scoring_run', return_value=None), \
         patch.object(service, 'run_description_enrichment', side_effect=AssertionError('Redundant pass')), \
         patch.object(service, 'capture_stdout_lines', return_value=(scored, ())):
        result = service.run_unscored_job_backlog('resume', 20, 0, enrichment_summary=summary)
    assert result.enrichment_jobs_processed == 0


def test_verified_identity_allows_only_due_description_retry_without_search():
    verified = actionable_job(record_key='verified')
    source_job = retry('verified', **{k: verified[k] for k in (
        'official_job_url', 'verified_posting_key', 'verified_posting_key_source', 'verified_posting_key_trust')})
    source_job['official_url_status'] = 'FOUND_VERIFIED'
    selected, stats = plan([source_job])
    assert len(selected) == 1 and stats['eligible_deferred_retries'] == 1
    assert selected[0]['needs_description_enrichment']
    assert not selected[0]['needs_official_resolution']
    assert plan([source_job], retry_failed=False)[0] == []
    source_job['previous_attempted_at'] = NOW
    assert plan([source_job])[0] == []
    assert plan([source_job])[1]['cooldown'] == 1


def test_low_fit_assessments_do_not_consume_employer_recovery_budget():
    low = job(full=True, ai_score=20, recommendation='skip', has_current_complete_ai_assessment=True)
    selected, stats = plan([low])
    assert selected == [] and stats['excluded'] == 1


def test_exact_aliases_with_partial_trust_do_not_split_and_conflicts_are_excluded():
    official = actionable_job(record_key='verified')
    fields = {k: official[k] for k in (
        'official_url_status', 'official_job_url', 'verified_posting_key',
        'verified_posting_key_source', 'verified_posting_key_trust')}
    trusted = job('verified', **fields)
    untrusted = job('untrusted', exact_posting_key=trusted['exact_posting_key'])
    selected, stats = plan([trusted, untrusted])
    assert len(selected) == 1 and stats['inventory'] == 1
    assert not selected[0]['needs_official_resolution']
    conflicting = dict(trusted, record_key='conflicting',
                       official_job_url=official['official_job_url'] + '4',
                       verified_posting_key='greenhouse:1234')
    selected, stats = plan([trusted, conflicting])
    assert selected == [] and stats['excluded'] == 1


def test_search_redirect_cannot_bypass_request_budget():
    requests = []
    def handle(request):
        requests.append(str(request.url))
        return httpx.Response(302, headers={'location': 'https://duckduckgo.com/html/?next=1'})
    with patch.dict(os.environ, {'PUBLIC_SEARCH_PROVIDER': 'duckduckgo'}), \
         httpx.Client(transport=httpx.MockTransport(handle), follow_redirects=True) as client:
        client.search_request_budget = SearchRequestBudget(1)
        try:
            resolver.PublicSearchCircuit().search(job(), client)
        except RuntimeError:
            pass
        assert len(requests) == client.search_request_budget.attempted == 1


def test_recent_hidden_uses_discovery_not_attempt_or_email_time():
    from src.targeted_enrichment import plan_recent_hidden_recovery
    older = job('older', discovered_at=NOW-timedelta(days=6), previous_attempted_at=NOW)
    recent = job('recent', discovered_at=NOW-timedelta(days=1), previous_attempted_at=OLD)
    recent['email_date'] = NOW-timedelta(days=100)
    selected, stats = plan_recent_hidden_recovery([older, recent], [], 10, now=NOW)
    assert [j['record_key'] for j in selected] == ['recent']
    assert stats['older_groups_excluded'] == 1


def test_recent_hidden_newest_first_all_sources_and_no_old_alias_revival():
    from src.targeted_enrichment import plan_recent_hidden_recovery
    inventory = [job(source, source=source, discovered_at=NOW-timedelta(hours=hour), full=True)
                 for hour, source in enumerate(('builtin','glassdoor','lensa','bebee','linkedin'),1)]
    old = job('old', discovered_at=NOW-timedelta(days=20))
    alias = dict(old, record_key='new-email', discovered_at=NOW)
    inventory += [old, alias]
    selected, stats = plan_recent_hidden_recovery(inventory, [], 3, now=NOW)
    assert [j['source'] for j in selected] == ['builtin','glassdoor','lensa']
    assert stats['recovery_hidden'] == 5
    assert not any(j['needs_description_enrichment'] for j in selected)
    assert set(stats['recovery_record_keys']) == {'builtin','glassdoor','lensa'}


def test_recent_hidden_visibility_history_cooldown_and_unknown_dates():
    from src.targeted_enrichment import plan_recent_hidden_recovery
    rows = [job('visible', discovered_at=NOW), job('applied', discovered_at=NOW, application_status='applied'),
            job('removed', discovered_at=NOW, application_status='removed'), job('unknown'),
            job('future', discovered_at=NOW+timedelta(minutes=1)),
            retry('deferred', when=NOW-timedelta(days=1), discovered_at=NOW-timedelta(days=1))]
    selected, stats = plan_recent_hidden_recovery(rows, [rows[0]], 10, now=NOW)
    assert selected == [] and stats['recovery_record_keys'] == ()
    assert stats['recovery_hidden'] == stats['recovery_relevant'] == stats['cooldown'] == 1
    assert stats['recent_personal_history_preserved'] == 2 and stats['unknown_discovery_excluded'] == 1


def test_scoped_repository_queries_keep_status_graph_and_empty_means_no_work():
    from src.test_recommendation_repository import with_temp_review_database
    from src.test_scoring_queue_guards import seed_linked
    from src.repositories import recommendation_repository as repo
    def scenario():
        seed_linked('recent','900001')
        seed_linked('older','900002')
        seed_linked('removed-alias','900001',state='removed')
        args = dict(resume_hash='unregistered', model_name=None, limit=10, minimum_rule_score=0, reuse_any_model=True)
        assert repo.load_candidate_jobs(**args, record_keys=()) == []
        assert repo.load_candidate_jobs(**args, record_keys=('recent',)) == []  # alias history outside scope still wins
        screened = repo.load_unscreened_job_eligibility_candidates('unregistered',10,record_keys=('recent',))
        assert screened == []
        assert repo.load_unscreened_job_eligibility_candidates('unregistered',10,record_keys=()) == []
        assert [j['record_key'] for j in repo.load_candidate_jobs(**args,record_keys=('older',))] == ['older']
    with_temp_review_database(scenario)


def test_recent_scoring_scope_preserves_completed_assessment_guard():
    from src.test_recommendation_repository import with_temp_review_database
    from src.test_scoring_queue_guards import setup_alias, NEW
    from src.repositories import recommendation_repository as repo
    def scenario():
        setup_alias(completed=True)
        assert repo.load_candidate_jobs(NEW,None,10,0,reuse_any_model=True,record_keys=('sibling',)) == []
        assert repo.load_unscreened_job_eligibility_candidates(NEW,10,record_keys=('sibling',)) == []
    with_temp_review_database(scenario)


def test_daily_scoring_propagates_even_empty_recovery_scope():
    from dataclasses import replace
    from src.score_backlog import BacklogSummary
    enrichment = service.EnrichmentSummary(0,0,0,0,0,0,0,0,0,0,0,(),recovery_record_keys=())
    scored = BacklogSummary('resume','model',0,0,0,0,0,0,False,False)
    with patch.object(service,'start_scoring_run',return_value=None), \
         patch.object(service,'capture_stdout_lines',return_value=(scored,())) as capture:
        result = service.run_unscored_job_backlog('resume',10,0,enrichment_summary=enrichment,recent_hidden_days=5)
        assert capture.call_args.kwargs['record_keys'] == ()
        assert result.recovery_record_keys == ()


# --- Recent employer-verification retries come first (RECENT_RETRY_DAYS) -----------------
from src.targeted_enrichment import RECENT_RETRY_DAYS  # noqa: E402


def lookup_retry(key, *, discovered=None, attempted=OLD, **kwargs):
    """An unverified posting whose employer lookup found nothing, as in the frozen cohort."""
    return retry(key, status='OFFICIAL_NOT_FOUND', when=attempted, discovered_at=discovered, **kwargs)


def stale_retries(count, *, attempted=NOW - timedelta(days=30)):
    # Older than the recent window, attempted earlier than any recent posting.
    return [lookup_retry(f'stale-{n:04d}', discovered=NOW - timedelta(days=60),
                         attempted=attempted + timedelta(seconds=n)) for n in range(count)]


def retry_order(jobs, **kwargs):
    selected, stats = plan(jobs, len(jobs), **kwargs)
    return [j['record_key'] for j in selected if j['daily_queue_kind'] == 'deferred_retry'], selected, stats


def test_recent_retry_after_cooldown_is_selected_ahead_of_2000_older_retries():
    recent = lookup_retry('recent', discovered=NOW - timedelta(days=9), attempted=NOW - timedelta(days=8))
    fresh = [job(f'fresh-{n:02d}') for n in range(30)]
    selected, stats = plan(fresh + stale_retries(2000) + [recent])
    assert 'recent' in [j['record_key'] for j in selected]
    assert len(selected) == 25 and stats['eligible_deferred_retries'] == 2001
    # Without a discovery date it would sit behind every older attempt, as before.
    undated = dict(recent, discovered_at=None)
    selected, _ = plan(fresh + stale_retries(2000) + [undated])
    assert 'recent' not in [j['record_key'] for j in selected]


def test_recent_retry_inside_its_cooldown_is_not_selected():
    cooling = lookup_retry('cooling', discovered=NOW - timedelta(days=4), attempted=NOW - timedelta(days=3))
    selected, stats = plan(stale_retries(3) + [cooling])
    assert 'cooling' not in [j['record_key'] for j in selected]
    assert stats['cooldown'] == 1 and stats['runnable'] == 3


def test_older_retries_keep_oldest_attempt_order():
    attempts = [NOW - timedelta(days=d) for d in (9, 30, 12, 20, 15)]
    dated = [lookup_retry(f'old-{n}', discovered=NOW - timedelta(days=40 + n), attempted=a)
             for n, a in enumerate(attempts)]
    expected = [j['record_key'] for j in sorted(dated, key=lambda j: j['previous_attempted_at'])]
    assert retry_order(dated)[0] == expected
    # Identical to the order the same postings get with no discovery dates (pre-change order).
    assert retry_order([dict(j, discovered_at=None) for j in dated])[0] == expected


def test_fresh_lane_and_round_robin_are_unchanged():
    fresh = [job(f'fresh-{n}') for n in range(4)]
    recent = [lookup_retry(f'recent-{n}', discovered=NOW - timedelta(days=8 + n)) for n in range(3)]
    old = stale_retries(3)
    _, selected, _ = retry_order(fresh + old + recent)
    kinds = [j['daily_queue_kind'] for j in selected]
    assert kinds[:6] == ['fresh_description', 'deferred_retry'] * 3
    baseline = plan(fresh + old + [dict(j, discovered_at=None) for j in recent], 25)[0]
    lane = lambda rows: [j['record_key'] for j in rows if j['daily_queue_kind'] != 'deferred_retry']
    assert lane(selected) == lane(baseline) == [f'fresh-{n}' for n in range(4)]
    # Newest recent discovery first, then the older tier in its existing order.
    assert retry_order(fresh + old + recent)[0] == ['recent-0', 'recent-1', 'recent-2'] + [j['record_key'] for j in old]


def test_retry_failed_false_still_disables_recent_retries_with_same_counters():
    recent = [lookup_retry(f'recent-{n}', discovered=NOW - timedelta(days=9)) for n in range(2)]
    fresh = [job('fresh')]
    selected, stats = plan(fresh + recent, retry_failed=False)
    _, baseline = plan(fresh + [dict(j, discovered_at=None) for j in recent], retry_failed=False)
    assert [j['record_key'] for j in selected] == ['fresh']
    assert stats['retry_disabled'] == 2 and stats == baseline


def test_new_alias_alert_does_not_make_an_old_posting_recent():
    old_alias = lookup_retry('old-alias', discovered=NOW - timedelta(days=40), attempted=NOW - timedelta(days=20))
    new_alert = dict(old_alias, record_key='new-alert', discovered_at=NOW - timedelta(days=1),
                     attempt_count=0, previous_status=None, previous_attempted_at=None)
    other_recent = lookup_retry('other-recent', discovered=NOW - timedelta(days=10), attempted=NOW - timedelta(days=9))
    order, selected, stats = retry_order([old_alias, new_alert, other_recent])
    assert stats['inventory'] == 2 and len(order) == 2
    assert order[0] == 'other-recent'
    group = next(j for j in selected if j['exact_posting_key'] == 'exact:old-alias')
    assert group['daily_queue_first_discovered_at'] == NOW - timedelta(days=40)


def test_existing_exclusions_still_apply_to_recent_postings():
    recent = NOW - timedelta(days=9)
    applied = lookup_retry('applied', discovered=recent)
    applied_alias = dict(applied, record_key='applied-alias', application_status='applied')
    filtered = lookup_retry('filtered', discovered=recent, title='Senior Software Engineer, Quality')
    completed = lookup_retry('completed', discovered=recent, full=True, ai_score=20, recommendation='skip',
                             has_current_complete_ai_assessment=True)
    official = actionable_job(record_key='verified')
    trusted = dict(lookup_retry('verified', discovered=recent), **{k: official[k] for k in (
        'official_url_status', 'official_job_url', 'verified_posting_key',
        'verified_posting_key_source', 'verified_posting_key_trust')})
    selected, stats = plan([applied, applied_alias, filtered, completed, trusted])
    assert stats['excluded'] == 3
    assert all(not j['needs_official_resolution'] for j in selected)
    assert {j['record_key'] for j in selected} <= {'verified'}


def test_fourteen_day_boundary_is_inclusive_and_deterministic():
    edge = NOW - timedelta(days=RECENT_RETRY_DAYS)
    on_edge = lookup_retry('on-edge', discovered=edge, attempted=NOW - timedelta(days=8))
    past_edge = lookup_retry('past-edge', discovered=edge - timedelta(microseconds=1),
                             attempted=NOW - timedelta(days=30))
    twin_a = lookup_retry('twin-a', discovered=NOW - timedelta(days=10))
    twin_b = lookup_retry('twin-b', discovered=NOW - timedelta(days=10))
    jobs = [past_edge, twin_b, on_edge, twin_a]
    order = retry_order(jobs)[0]
    assert order == ['twin-a', 'twin-b', 'on-edge', 'past-edge']
    assert retry_order(list(reversed(jobs)))[0] == order


def test_missing_discovery_timestamp_is_never_recent():
    undated = lookup_retry('undated', discovered=None, attempted=NOW - timedelta(days=10))
    partly = lookup_retry('partly', discovered=NOW - timedelta(days=2), attempted=NOW - timedelta(days=20))
    partly_alias = dict(partly, record_key='partly-alias', discovered_at=None)
    text_date = lookup_retry('text-date', discovered='2026-11-17T00:00:00+00:00', attempted=NOW - timedelta(days=15))
    recent = lookup_retry('recent', discovered=NOW - timedelta(days=9), attempted=NOW - timedelta(days=8))
    _, selected, _ = retry_order([undated, partly, partly_alias, text_date, recent])
    postings = [j['exact_posting_key'] for j in selected if j['daily_queue_kind'] == 'deferred_retry']
    # Only the dated recent posting is promoted; the rest keep oldest-attempt order.
    assert postings == ['exact:recent', 'exact:partly', 'exact:text-date', 'exact:undated']
    assert all(j['daily_queue_first_discovered_at'] is None for j in selected if j['record_key'] != 'recent')


if __name__ == '__main__':
    for name, function in list(globals().items()):
        if name.startswith('test_') and callable(function):
            function()
    print('Enrichment recovery regressions passed.')
