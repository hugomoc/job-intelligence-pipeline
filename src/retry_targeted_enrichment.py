"""Retry only relevant missing-JD postings after a small public-search health probe."""
import argparse
import json
from pathlib import Path
from bs4 import BeautifulSoup
from src.database import get_connection
from src.enrich_jobs import load_jobs_to_enrich, create_http_client
from src.ai.resume_profiler import get_model_name
from src.score_backlog import load_latest_resume_hash
from src.targeted_enrichment import select_targeted_retries
from src.enrichment.official_job_resolver import search_candidates, PublicSearchCircuit
from src.drain_enrichment_backlog import drain, RateLimitGuard, write_report


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
                healthy=bool(candidates) or bool(page.get('valid_results_page'))
                probes.append(dict(record_key=job['record_key'],healthy=healthy,
                                   candidates=len(candidates),**page))
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
    a=p.parse_args()
    if a.batch_size<1 or a.delay<0:p.error('Invalid batch size/delay')
    rh=a.resume_hash or load_latest_resume_hash(get_model_name())
    if not rh:p.error('No cached resume')
    loader=lambda:load_targeted_queue(rh)
    jobs=loader()
    report=dict(resume_hash=rh,targeted_postings=len(jobs),processed=0,
                descriptions_updated=0,official_resolved=0)
    print(f'Targeted exact postings: {len(jobs)}',flush=True)
    guard=RateLimitGuard()
    with create_http_client() as client:
        client.event_hooks.setdefault('response',[]).append(guard.observe)
        healthy,probes=probe_search(jobs,client)
        report['health_probes']=probes
        if not healthy:
            report['stop_reason']='public search unavailable or probe inconclusive; no retry attempts changed' if jobs else 'targeted queue empty'
            write_report(a.report,report);print(json.dumps(report),flush=True);return
        initial = [jobs]
        def queued_loader():
            return initial.pop() if initial else loader()
        result=drain(client,batch_size=a.batch_size,delay=a.delay,checkpoint=a.report,
                     loader=queued_loader,guard=guard,search_circuit=PublicSearchCircuit(),
                     retry_official_search=True,stop_on_search_unavailable=True,emit=lambda line:print(line,flush=True))
        result.update(targeted_postings=report['targeted_postings'],health_probes=probes)
        write_report(a.report,result)

if __name__=='__main__':main()
