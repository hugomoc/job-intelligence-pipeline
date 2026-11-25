"""Restartable scoring of admitted FULL_JDs using the existing score cache."""
import argparse
from datetime import datetime, timezone
import time
from pathlib import Path
from src.ai.resume_matcher import (MATCHER_PROMPT_VERSION, ResumeMatcherError,
    score_resume_against_job, score_resume_against_job_openai, get_openai_model_name)
from src.ai.resume_profiler import get_model_name
from src.score_backlog import load_latest_resume_hash, should_switch_to_openai
from src.score_jobs_ai import load_cached_profile, save_job_score
from src.repositories.recommendation_repository import load_candidate_jobs
from src.drain_enrichment_backlog import write_report

# Total provider scoring attempts per invocation, across all batches.
DEFAULT_MAX_ATTEMPTS = 30


def drain_scores(resume_hash, *, batch_size=25, delay=0.5, report_path=None,
                 loader=None, score=None, fallback=None, save=None, profile=None,
                 emit=print, sleeper=time.sleep, max_attempts=DEFAULT_MAX_ATTEMPTS):
    """Score admitted FULL_JDs in batches, never exceeding ``max_attempts``
    provider scoring attempts in total (a job counts once, success or failure)."""
    if max_attempts < 1:
        raise ValueError('max_attempts must be at least 1')
    from src import database
    from src.candidate_assessments import latest_candidate_resume
    from src.score_jobs_ai import load_cached_resume_text
    with database.get_connection() as connection:
        resume_hash = latest_candidate_resume(connection, resume_hash)
    model = get_model_name()
    profile = profile or load_cached_profile(resume_hash, model)
    if profile is None:
        raise ValueError('No current cached resume profile')
    loader = loader or (lambda: load_candidate_jobs(resume_hash, model, 100000, 0,
                          prompt_version=MATCHER_PROMPT_VERSION, reuse_any_model=True))
    def using_cached_text(provider):
        def call(**kwargs):
            text = load_cached_resume_text(resume_hash)
            if not text:
                raise ValueError('Cached exact resume text is required')
            return provider(**kwargs, resume_text=text)
        return call
    score = score or using_cached_text(score_resume_against_job)
    fallback = fallback or using_cached_text(score_resume_against_job_openai)
    save = save or save_job_score
    started = time.monotonic()
    report = dict(resume_hash=resume_hash, prompt_version=MATCHER_PROMPT_VERSION,
                  started_at=datetime.now(timezone.utc).isoformat(), selected=0,
                  scored=0, cached_reused=0, gemini=0, openai=0, failed=0,
                  batches=0, stop_reason=None, max_attempts=max_attempts, attempts=0)
    seen = set()
    use_openai = False
    queue = loader()
    report['starting_eligible_unscored'] = len(queue)
    def persist():
        report['elapsed_seconds'] = round(time.monotonic()-started, 2)
        report['remaining_eligible_unscored'] = len(queue)
        if report_path: write_report(Path(report_path), report)
    persist()
    while queue and not report['stop_reason']:
        if report['attempts'] >= max_attempts:
            report['stop_reason'] = f'total attempt cap reached ({max_attempts})'
            break
        jobs = [j for j in queue if j['canonical_job_key'] not in seen][:batch_size]
        if not jobs:
            report['stop_reason'] = 'remaining jobs failed in this run; rerun after investigation'
            break
        report['batches'] += 1
        before = {k:report[k] for k in ['selected','scored','cached_reused','gemini','openai','failed']}
        for job in jobs:
            if report['attempts'] >= max_attempts:
                report['stop_reason'] = f'total attempt cap reached ({max_attempts})'
                break
            from src.candidate_assessments import scoring_claim
            with scoring_claim(resume_hash, job) as acquired:
                if not acquired:
                    report['cached_reused'] += 1
                    continue
                seen.add(job['canonical_job_key']);report['selected'] += 1
                # An earlier score in this batch may now cover this exact/verified posting.
                if job['canonical_job_key'] not in {j['canonical_job_key'] for j in loader()}:
                    report['cached_reused'] += 1
                    persist();continue
                # Counted before the request: a failed job is never retried here.
                report['attempts'] += 1
                try:
                    fn = fallback if use_openai else score
                    match = fn(resume_profile=profile, job=job,
                               model_name=get_openai_model_name() if use_openai else model)
                except ResumeMatcherError as error:
                    message = str(error)
                    if any(t in message.lower() for t in ['quota', '429', 'resource_exhausted', 'rate limit']):
                        report['failed'] += 1
                        report['stop_reason'] = 'API quota/rate-limit safety: '+message
                        persist();break
                    if not use_openai and should_switch_to_openai(error):
                        use_openai = True
                        try:
                            match = fallback(resume_profile=profile,job=job,model_name=get_openai_model_name())
                        except ResumeMatcherError as other:
                            report['failed'] += 1
                            report['stop_reason'] = 'provider safety: '+str(other)
                            persist();break
                    else:
                        report['failed'] += 1;persist();continue
                save(resume_hash=resume_hash, match=match)
                report['scored'] += 1;report['openai' if use_openai else 'gemini'] += 1
                emit(f"Saved {report['scored']}: {job['title']} | {job['company_name']}")
                persist()
                if delay:sleeper(delay)
        previous = len(queue)
        queue = loader()
        report['cached_reused'] += max(0, previous-len(queue)-(report['scored']-before['scored'])-(report['cached_reused']-before['cached_reused']))
        emit('Batch '+str(report['batches'])+': '+ '; '.join(f'{k}={report[k]-before[k]}' for k in before)
             +f'; remaining eligible unscored={len(queue)}')
        persist()
    if not report['stop_reason']:
        report['stop_reason'] = ('no eligible unscored jobs remain' if not queue
                                 else 'stopped with eligible jobs remaining')
    if queue and report['attempts'] >= max_attempts:
        report['stop_reason'] = f'total attempt cap reached ({max_attempts})'
    report['fully_drained'] = not queue
    persist();return report


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--batch-size',type=int,default=25);p.add_argument('--delay',type=float,default=0.5)
    p.add_argument('--report',type=Path,required=True);p.add_argument('--resume-hash')
    p.add_argument('--max-attempts',type=int,default=DEFAULT_MAX_ATTEMPTS,
                   help='Total scoring attempts across all batches in this invocation '
                        f'(default {DEFAULT_MAX_ATTEMPTS}). Rerun to continue a capped drain.')
    a=p.parse_args()
    if a.batch_size<1 or a.delay<0:p.error('Invalid batch size/delay')
    if a.max_attempts<1:p.error('--max-attempts must be at least 1')
    rh=a.resume_hash or load_latest_resume_hash(get_model_name())
    if not rh:p.error('No cached resume')
    report = drain_scores(rh,batch_size=a.batch_size,delay=a.delay,report_path=a.report,
                          max_attempts=a.max_attempts)
    if report['scored']:
        from src.ui.job_recommendation_service import run_dbt_build
        run_dbt_build()
        report['dbt_was_run'] = True
        write_report(a.report, report)
    print(report,flush=True)

if __name__=='__main__':main()
