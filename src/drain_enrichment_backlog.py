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
)
from src.enrichment.official_job_resolver import OFFICIAL_FOUND_VERIFIED


class RateLimitGuard:
    """Stop after five consecutive 429s or ten in the last fifty responses."""
    def __init__(self):
        self.recent = deque(maxlen=50)
        self.total = 0
        self.consecutive = 0
        self.stopped = False

    def observe(self, response):
        limited = response.status_code == 429
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


def load_queue():
    # Cover the full inventory, rather than truncating diagnostics at 10,000.
    with get_connection() as connection:
        size = connection.execute('SELECT count(*) FROM raw_jobs').fetchone()[0]
    return load_jobs_to_enrich(limit=max(1, size), minimum_words=80,
                              source=None, retry_failed=False, force=False)


def process_one(job, client):
    try:
        processed = process_enrichment_job(job=job, client=client)
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
          guard=None, sleeper=time.sleep):
    started = time.monotonic()
    guard = guard or RateLimitGuard()
    queue = loader()
    report = {'database_path': str(DATABASE_PATH.resolve()),
              'started_at': datetime.now(timezone.utc).isoformat(),
              'starting': summarize_enrichment_queue(queue),
              'starting_never_attempted_needing_jd': len(pending_jobs(queue)),
              'processed': 0, 'descriptions_updated': 0,
              'official_resolved': 0, 'statuses': {}, 'batches': 0,
              'stop_reason': None, 'last_record_key': None}
    emit('Starting queue: ' + json.dumps(report['starting']))
    emit(f"Never-attempted jobs needing a JD: {report['starting_never_attempted_needing_jd']}")
    seen = set()
    limited_jobs = 0

    def persist():
        report['elapsed_seconds'] = round(time.monotonic() - started, 2)
        report['rate_limit_responses'] = guard.total
        if checkpoint:
            write_report(checkpoint, report)

    persist()
    try:
        while pending_jobs(queue):
            jobs = pending_jobs(queue)[:batch_size]
            if max_jobs is not None:
                jobs = jobs[:max_jobs - report['processed']]
            if not jobs:
                report['stop_reason'] = 'max_jobs reached'
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
                if attempted:
                    continue
                before_429 = guard.total
                emit(f"Processing {report['processed'] + 1}: {job['title']} | {job['company_name']} | {key}")
                processed = processor(job, client)
                seen.add(key)
                report['processed'] += 1
                batch_processed += 1
                batch_updated += int(processed.updated)
                report['descriptions_updated'] += int(processed.updated)
                official = processed.official_resolution
                report['official_resolved'] += int(bool(official and official.status == OFFICIAL_FOUND_VERIFIED))
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
            if max_jobs is not None and report['processed'] >= max_jobs:
                report['stop_reason'] = 'max_jobs reached' if pending_jobs(queue) else None
                break
        report['remaining'] = summarize_enrichment_queue(queue)
        report['remaining_never_attempted_needing_jd'] = len(pending_jobs(queue))
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
    args = parser.parse_args()
    if args.batch_size < 1 or args.delay < 0 or (args.max_jobs is not None and args.max_jobs < 1):
        parser.error('Batch size/max jobs must be positive; delay cannot be negative.')
    report_path = args.report or DATABASE_PATH.parent / (
        'enrichment-drain-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '.json')
    print(f'Active database: {DATABASE_PATH.resolve()}', flush=True)
    print(f'Report: {report_path}', flush=True)
    guard = RateLimitGuard()
    with create_http_client() as client:
        client.event_hooks.setdefault('response', []).append(guard.observe)
        drain(client, batch_size=args.batch_size, delay=args.delay,
              max_jobs=args.max_jobs, checkpoint=report_path, guard=guard,
              emit=lambda line: print(line, flush=True))


if __name__ == '__main__':
    main()
