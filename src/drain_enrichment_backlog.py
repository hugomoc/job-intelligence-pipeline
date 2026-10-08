"""Drain never-attempted description enrichment using the existing safe resolver.

Run from the repository root with python -m src.drain_enrichment_backlog.
Attempts are committed individually; a later invocation resumes from DB state.
"""
from __future__ import annotations

import argparse
from collections import deque
from datetime import datetime, timezone
import json
from pathlib import Path
import time

from src.database import DATABASE_PATH, get_connection
from src.enrich_jobs import (
    create_http_client, failed_processing_result, load_jobs_to_enrich,
    process_enrichment_job, save_enrichment_attempt, summarize_enrichment_queue,
    prepare_verified_candidate_metadata_before_queue, existing_description_result,
    EnrichmentProcessingResult,
)
from src.enrichment.official_job_resolver import OFFICIAL_FOUND_VERIFIED, PublicSearchCircuit


class RateLimitGuard:
    """Stop after five consecutive 429s or ten in the last fifty responses."""
    def __init__(self):
        self.recent = deque(maxlen=50)
        self.total = 0
        self.consecutive = 0
        self.stopped = False

    def observe(self, response):
        limited = response.status_code == 429
        host = getattr(getattr(response, "request", None), "url", None)
        if host is not None and host.host.endswith("duckduckgo.com"):
            # Public-search throttling opens its own circuit, not the global one.
            return
        self.total += int(limited)
        self.consecutive = self.consecutive + 1 if limited else 0
        self.recent.append(limited)
        if self.consecutive >= 5 or sum(self.recent) >= 10:
            self.stopped = True


def pending_jobs(queue):
    return [job for job in queue if int(job.get('attempt_count') or 0) == 0
            and not job.get('previous_status')
            and job.get('needs_description_enrichment')
            and job.get('description_state') != 'FULL_JD']


def load_queue(retry_official_search=False):
    # Cover the full inventory, rather than truncating diagnostics at 10,000.
    with get_connection() as connection:
        size = connection.execute('SELECT count(*) FROM raw_jobs').fetchone()[0]
    return load_jobs_to_enrich(limit=max(1, size), minimum_words=80,
                              source=None, retry_failed=False, force=False,
                              never_attempted_only=not retry_official_search,
                              deduplicate=False, retry_official_search=retry_official_search)


def process_one(job, client, search_circuit=None):
    # Refresh verified candidates between records in a batch, so newly recovered
    # authoritative descriptions can be reused under the existing provenance rules.
    metadata = prepare_verified_candidate_metadata_before_queue([job], minimum_words=80)
    if metadata["offline_reused"]:
        job["drain_verified_reuse"] = True
        return EnrichmentProcessingResult(
            result=existing_description_result(job), stored_status="enriched",
            stored_error=None, identity_validation=None, official_resolution=None,
            updated=True,
        )
    try:
        options = {"search_circuit": search_circuit} if search_circuit is not None else {}
        processed = process_enrichment_job(job=job, client=client, **options)
    except Exception as error:
        processed = failed_processing_result(job, error)
    # Do not catch persistence errors: stopping is safer than losing attempts.
    save_enrichment_attempt(job=job, result=processed.result,
                            status=processed.stored_status,
                            error_message=processed.stored_error,
                            identity_validation=processed.identity_validation,
                            official_resolution=processed.official_resolution)
    return processed


def write_report(path, report):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(report, indent=2, default=str) + '\n')
    temporary.replace(path)


def drain(client, *, batch_size=250, delay=0.5, max_jobs=None,
          emit=print, checkpoint=None, loader=load_queue, processor=process_one,
          guard=None, sleeper=time.sleep, search_circuit=None, retry_official_search=False,
          stop_on_search_unavailable=False):
    started = time.monotonic()
    guard = guard or RateLimitGuard()
    search_circuit = search_circuit or PublicSearchCircuit()
    database_loader = loader is load_queue
    if retry_official_search and loader is load_queue:
        loader = lambda: load_queue(retry_official_search=True)
    def candidates(queue):
        return queue if retry_official_search else pending_jobs(queue)
    queue = loader()
    report = {'database_path': str(DATABASE_PATH.resolve()),
              'started_at': datetime.now(timezone.utc).isoformat(),
              'starting': summarize_enrichment_queue(queue),
              'starting_never_attempted_needing_jd': len(pending_jobs(queue)),
              'processed': 0, 'descriptions_updated': 0,
              'official_resolved': 0, 'verified_descriptions_reused': 0, 'statuses': {}, 'batches': 0,
              'stop_reason': None, 'last_record_key': None}
    emit('Starting queue: ' + json.dumps(report['starting']))
    emit(f"Never-attempted jobs needing a JD: {report['starting_never_attempted_needing_jd']}")
    seen = set()
    limited_jobs = 0

    def persist():
        report['elapsed_seconds'] = round(time.monotonic() - started, 2)
        report['rate_limit_responses'] = guard.total
        report['search_circuit'] = search_circuit.summary()
        if checkpoint:
            write_report(checkpoint, report)

    persist()
    try:
        while candidates(queue):
            jobs = [job for job in candidates(queue) if job["record_key"] not in seen][:batch_size]
            if max_jobs is not None:
                jobs = jobs[:max_jobs - report['processed']]
            if not jobs:
                report['stop_reason'] = 'max_jobs reached' if max_jobs is not None else None
                break
            report['batches'] += 1
            statuses = {}
            batch_processed = 0
            batch_updated = 0
            emit(f"Batch {report['batches']}: selected {len(jobs)}")
            for job in jobs:
                key = job['record_key']
                if key in seen:
                    raise RuntimeError(f'Queue made no progress: already processed {key}')
                # Recheck durable state before touching the network.
                with get_connection() as connection:
                    attempted = connection.execute(
                        'SELECT 1 FROM job_enrichment_attempts WHERE record_key = ?',
                        [key]).fetchone()
                if attempted and not retry_official_search:
                    continue
                before_429 = guard.total
                emit(f"Processing {report['processed'] + 1}: {job['title']} | {job['company_name']} | {key}")
                if processor is process_one:
                    processed = processor(job, client, search_circuit=search_circuit)
                else:
                    processed = processor(job, client)
                seen.add(key)
                report['processed'] += 1
                batch_processed += 1
                batch_updated += int(processed.updated)
                report['descriptions_updated'] += int(processed.updated)
                official = processed.official_resolution
                reused = bool(job.get('drain_verified_reuse'))
                report['verified_descriptions_reused'] += int(reused)
                report['official_resolved'] += int(reused or bool(official and official.status == OFFICIAL_FOUND_VERIFIED))
                status = processed.stored_status
                statuses[status] = statuses.get(status, 0) + 1
                report['statuses'][status] = report['statuses'].get(status, 0) + 1
                report['last_record_key'] = key
                is_limited = guard.total > before_429 or processed.result.http_status == 429
                if not is_limited:
                    is_limited = '429' in str(processed.stored_error or '')
                limited_jobs = limited_jobs + 1 if is_limited else 0
                emit(f"Saved: {status}; words={processed.result.word_count}; updated={processed.updated}")
                persist()
                if stop_on_search_unavailable and search_circuit.open:
                    report['stop_reason'] = 'public search provider circuit opened; targeted retries remain retryable'
                    break
                if guard.stopped or limited_jobs >= 5:
                    report['stop_reason'] = 'excessive rate limiting'
                    break
                if delay:
                    sleeper(delay)
            queue = loader()
            emit(f"Batch {report['batches']}: processed={batch_processed}; descriptions found={batch_updated}; "
                 f"blocked={statuses.get('blocked', 0)}; no description={statuses.get('no_description', 0)}; "
                 f"fetch errors={statuses.get('fetch_error', 0)}; remaining never attempted={len(pending_jobs(queue))}")
            report['remaining'] = summarize_enrichment_queue(queue)
            report['remaining_never_attempted_needing_jd'] = len(pending_jobs(queue))
            persist()
            if report['stop_reason']:
                break
            if retry_official_search:
                queue = [job for job in queue if job['record_key'] not in seen]
            if max_jobs is not None and report['processed'] >= max_jobs:
                report['stop_reason'] = 'max_jobs reached' if pending_jobs(queue) else None
                break
        report['remaining'] = summarize_enrichment_queue(queue)
        report['remaining_never_attempted_needing_jd'] = len(pending_jobs(queue))
        if database_loader:
            report['remaining_retryable_official_resolution_jobs'] = len(load_queue(retry_official_search=True))
    except (Exception, KeyboardInterrupt) as error:
        report['stop_reason'] = f'{type(error).__name__}: {error}'
        persist()
        raise
    finally:
        persist()
    emit('Final report: ' + json.dumps(report, default=str))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--batch-size', type=int, default=250)
    parser.add_argument('--delay', type=float, default=0.5)
    parser.add_argument('--max-jobs', type=int)
    parser.add_argument('--never-attempted-only', action='store_true', default=True,
                        help='Only new attempts are supported; enabled by default.')
    parser.add_argument('--report', type=Path)
    parser.add_argument('--retry-official-search', action='store_true',
                        help='Second pass: retry only deferred or transient public-search failures.')
    args = parser.parse_args()
    if args.batch_size < 1 or args.delay < 0 or (args.max_jobs is not None and args.max_jobs < 1):
        parser.error('Batch size/max jobs must be positive; delay cannot be negative.')
    report_path = args.report or DATABASE_PATH.parent / (
        'enrichment-drain-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '.json')
    print(f'Active database: {DATABASE_PATH.resolve()}', flush=True)
    print(f'Report: {report_path}', flush=True)
    guard = RateLimitGuard()
    search_circuit = PublicSearchCircuit()
    with create_http_client() as client:
        client.event_hooks.setdefault('response', []).append(guard.observe)
        drain(client, batch_size=args.batch_size, delay=args.delay,
              max_jobs=args.max_jobs, checkpoint=report_path, guard=guard,
              search_circuit=search_circuit, retry_official_search=args.retry_official_search,
              emit=lambda line: print(line, flush=True))


if __name__ == '__main__':
    main()
