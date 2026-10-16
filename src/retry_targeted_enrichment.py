"""Retry only relevant missing-JD postings after a small public-search health probe."""
import argparse
import json
import time
from pathlib import Path
from bs4 import BeautifulSoup
from src.database import get_connection
from src.enrich_jobs import load_jobs_to_enrich, create_http_client
from src.ai.resume_profiler import get_model_name
from src.score_backlog import load_latest_resume_hash
from src.targeted_enrichment import select_targeted_retries
from src.repositories.recommendation_repository import description_state
from src.enrichment.official_job_resolver import search_candidates, PublicSearchCircuit
from src.drain_enrichment_backlog import drain, RateLimitGuard, write_report, process_one
from src.enrichment.public_search import selected_provider, provider_metrics


def load_targeted_queue(resume_hash):
    with get_connection() as c:
        size=c.execute('select count(*) from raw_jobs').fetchone()[0]
    jobs=load_jobs_to_enrich(limit=max(1,size),minimum_words=80,source=None,
        retry_failed=False,force=False,resume_hash=resume_hash,deduplicate=False,
        retry_official_search=True)
    selected = select_targeted_retries(jobs)
    for job in selected:
        # Explicit selective retries may repeat a failed source attempt.
        # The selector has already excluded usable FULL_JDs.
        job["needs_description_enrichment"] = True
    return selected


def probe_search(jobs, client, search=search_candidates):
    probes=[]
    page = {}
    def observe(response):
        response.read()
        host = response.request.url.host
        if not host.endswith('duckduckgo.com') or response.status_code != 200:
            return
        soup = BeautifulSoup(response.text, 'html.parser')
        page['valid_results_page'] = bool(soup.select('.result__a, .no-results, .no-results__message'))
    hooks = client.event_hooks.setdefault('response', []) if client is not None else None
    if hooks is not None: hooks.append(observe)
    try:
        for job in jobs[:2]:
            page.clear()
            try:
                candidates=search(job,client)
                trace=job.get('public_search_trace', {})
                healthy=bool(candidates) or bool(trace.get('healthy')) or bool(page.get('valid_results_page'))
                probes.append(dict(record_key=job['record_key'],healthy=healthy,
                                   candidates=len(candidates),provider=trace.get('provider', selected_provider()),**page))
                if not healthy:return False,probes
            except Exception as error:
                probes.append(dict(record_key=job['record_key'],healthy=False,error=str(error)))
                return False,probes
        return bool(probes),probes
    finally:
        if hooks is not None: hooks.remove(observe)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--report',type=Path,required=True);p.add_argument('--batch-size',type=int,default=100)
    p.add_argument('--delay',type=float,default=0.75);p.add_argument('--resume-hash')
    p.add_argument('--max-jobs',type=int, help='Hard bound on completed attempts, for staged live validation.')
    p.add_argument('--exclude-report',type=Path,help='Skip records already attempted in an earlier staged report.')
    p.add_argument('--require-serper',action='store_true',help='Require Serper as primary (legacy).')
    p.add_argument('--require-tavily',action='store_true',help='Require Tavily as primary.')
    a=p.parse_args()
    if a.require_serper and selected_provider()!='serper':
        p.error('Serper is required as primary; remove TAVILY_API_KEY or use --require-tavily')
    if a.require_tavily and selected_provider()!='tavily':
        p.error('Tavily is required as primary; configure TAVILY_API_KEY; no attempts changed')
    if a.batch_size<1 or a.delay<0 or (a.max_jobs is not None and a.max_jobs<1):p.error('Invalid batch size/delay')
    rh=a.resume_hash or load_latest_resume_hash(get_model_name())
    if not rh:p.error('No cached resume')
    excluded=set()
    if a.exclude_report:
        excluded={j['record_key'] for j in json.loads(a.exclude_report.read_text()).get('jobs',[])}
    latest_targeted_count = [0]
    queue_load_ms = []
    def loader():
        started = time.monotonic()
        try:
            current = load_targeted_queue(rh)
            latest_targeted_count[0] = len(current)
            return [j for j in current if j['record_key'] not in excluded]
        finally:
            queue_load_ms.append(round(
                1000 * (time.monotonic() - started), 1
            ))
    jobs=loader()
    report=dict(resume_hash=rh,targeted_postings=len(jobs),processed=0,
                descriptions_updated=0,official_resolved=0)
    print(f'Targeted exact postings: {len(jobs)}',flush=True)
    guard=RateLimitGuard()
    circuit=PublicSearchCircuit()
    per_job=[]
    with create_http_client() as client:
        client.event_hooks.setdefault('response',[]).append(guard.observe)
        # A one-job test already exercises the provider; avoid two extra
        # credit-consuming health probes for the same single-job validation.
        if a.max_jobs == 1:
            healthy,probes = bool(jobs),[]
            report['health_probes_skipped'] = 'single-job live validation'
        else:
            healthy,probes = probe_search(jobs,client)
        report['health_probes']=probes
        report['provider_metrics']=provider_metrics(client)
        if not healthy:
            report['stop_reason']='public search unavailable or probe inconclusive; no retry attempts changed' if jobs else 'targeted queue empty'
            write_report(a.report,report);print(json.dumps(report),flush=True);return
        initial = [jobs]
        def queued_loader():
            return initial.pop() if initial else loader()
        def audited_processor(job, client):
            job.pop('public_search_trace', None)
            job.pop('candidate_validation_trace', None)
            processed=process_one(job,client,search_circuit=circuit)
            official=processed.official_resolution
            trace=job.get('public_search_trace', {})
            per_job.append(dict(record_key=job['record_key'],title=job['title'],
                timings=job.get('enrichment_timing', {}),
                company=job['company_name'],serper_called='serper' in trace.get('attempted_providers', []),
                tavily_called='tavily' in trace.get('attempted_providers', []),
                search_candidates=trace.get('candidates',0),search=trace,
                chosen_candidate_url=official.official_job_url if official else None,
                identity_validation=official.official_url_validation_reason if official else
                    (processed.identity_validation.reason if processed.identity_validation else None),
                candidate_validation=job.get('candidate_validation_trace',[]),
                official_status=official.status if official else None,
                jd_recovered=bool(processed.updated and description_state({
                    'description':processed.result.description,
                    'raw_description_word_count':processed.result.word_count})=='FULL_JD'),
                words=processed.result.word_count,stored_status=processed.stored_status))
            write_report(a.report,dict(report,processed=len(per_job),jobs=per_job,provider_metrics=provider_metrics(client)))
            return processed
        result=drain(client,batch_size=a.batch_size,delay=a.delay,checkpoint=None,
                     max_jobs=a.max_jobs,processor=audited_processor,
                     loader=queued_loader,guard=guard,search_circuit=circuit,
                     retry_official_search=True,stop_on_search_unavailable=True,emit=lambda line:print(line,flush=True))
        result.update(targeted_postings=report['targeted_postings'],health_probes=probes,
                      jobs=per_job,provider_metrics=provider_metrics(client),
                      remaining_targeted_queue=latest_targeted_count[0],provider_circuits=circuit.states,
                      queue_initial_load_ms=queue_load_ms[0] if queue_load_ms else None,
                      queue_refresh_total_ms=round(sum(queue_load_ms[1:]), 1),
                      queue_loader_calls=len(queue_load_ms),
                      remaining_unattempted_this_run=result.get('remaining', {}).get('eligible_for_enrichment'))
        write_report(a.report,result)

if __name__=='__main__':main()
