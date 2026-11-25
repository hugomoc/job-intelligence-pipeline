"""Read-only discovery cards, separate from verified/recommended job views."""
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from src.actionability import trusted_key
from src.enrich_jobs import OFFICIAL_RETRY_COOLDOWN_DAYS, OFFICIAL_RETRY_STATUSES
from src.repositories.recommendation_repository import normalize_datetime
from src.targeted_enrichment import plan_recent_hidden_recovery
from src.ui.job_visibility import passes_source_trust_gate


def pending_verification_rows(snapshot, visible_jobs, *, days=5, now=None):
    """Use the shared recent-hidden relevance/identity rules, even in cooldown.

    This consumes the existing session snapshot; no new DB/API operation occurs.
    The projection intentionally contains no score, recommendation or ACTIVE flag.
    """
    now = now or datetime.now(timezone.utc)
    # "Hidden" keeps its meaning: absent from the employer-verified/direct-source
    # set. Every job is now visible in Jobs, so pass only trusted-source cards.
    visible_jobs = [job for job in visible_jobs if passes_source_trust_gate(job)]
    adapted = []
    for item in snapshot:
        job = dict(item)
        job.update(discovered_at=item.get('first_discovered_at', item.get('discovered_at')),
                   current_word_count=item.get('raw_description_word_count', 0),
                   previous_status=item.get('enrichment_status'),
                   attempt_count=item.get('enrichment_attempt_count', 0),
                   previous_attempted_at=item.get('enrichment_attempted_at'))
        adapted.append(job)
    _, stats = plan_recent_hidden_recovery(adapted, visible_jobs, 0, days=days, now=now)
    pending = set(stats['recovery_pending_record_keys'])
    result = []
    for job in adapted:
        if job['record_key'] not in pending or trusted_key(job) or passes_source_trust_gate(job):
            continue
        status = job.get('official_url_status') or 'NOT_ATTEMPTED'
        last = normalize_datetime(job.get('official_url_resolved_at')) or normalize_datetime(job.get('previous_attempted_at'))
        retry = last + timedelta(days=OFFICIAL_RETRY_COOLDOWN_DAYS) if last and status in OFFICIAL_RETRY_STATUSES else None
        reason = job.get('official_url_validation_reason') or 'Verified employer posting identity has not been established.'
        if retry and retry > now:
            reason += ' Official lookup is in the existing retry cooldown.'
        result.append(dict(record_key=job['record_key'], title=job.get('title') or '',
            employer=job.get('company_name') or '', source=job.get('source') or '',
            location=job.get('location') or 'Not stored',
            discovered_at=normalize_datetime(job.get('discovered_at')),
            original_source_url=job.get('apply_url') or '',
            description_state=job.get('description_state') or 'NEEDS_ENRICHMENT',
            description_word_count=int(job.get('current_word_count') or 0),
            title_classification=job.get('title_classification') or '',
            verification_status=status, last_attempt=normalize_datetime(job.get('previous_attempted_at')) or last,
            next_retry=retry, blocked_reason=reason))
    return sorted(result, key=lambda row: (-(row['discovered_at'].timestamp()), row['record_key']))


def description_evidence_label(row):
    """Stored-description evidence; never implies an assessment is possible."""
    state, words = row.get('description_state'), int(row.get('description_word_count') or 0)
    if state == 'FULL_JD':
        return f'Complete description stored ({words} words)'
    if state == 'PARTIAL_JD':
        return f'Partial description only ({words} words)'
    if state == 'ENRICHMENT_REJECTED':
        return 'Missing (fetched description rejected by identity checks)'
    return 'Missing (no job description stored)'


def usable_source_url(url):
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    return parsed.scheme in {'http', 'https'} and bool(parsed.hostname) and not parsed.username and not parsed.password


def render_pending_verification(st, rows):
    """No apply, score, restore or network controls in this discovery-only view."""
    st.info('Unverified source listings. Employer identity and posting activity are unconfirmed. These are not application recommendations.')
    st.caption(f'{len(rows)} relevant New postings awaiting employer verification.')
    if not rows:
        st.write('No pending postings match the discovery window and relevance filters.')
    def date_text(value):
        return value.isoformat() if value else 'Not recorded'
    for row in rows:
        with st.container(border=True):
            st.subheader(row['title'])
            st.write(row['employer'])
            st.caption(f"Source: {row['source']} · Location: {row['location']}")
            st.write('First discovery: ' + date_text(row['discovered_at']))
            st.write('Description completeness: ' + row['description_state'])
            st.write('Employer verification: ' + row['verification_status'])
            st.write('Last attempt: ' + date_text(row['last_attempt']))
            st.write('Next retry: ' + (date_text(row['next_retry']) if row['next_retry'] else 'No scheduled cooldown; normal processing rules apply'))
            st.write('Blocked reason: ' + row['blocked_reason'])
            try:
                parsed = urlsplit(row['original_source_url'])
                usable = parsed.scheme in {'http', 'https'} and bool(parsed.hostname) and not parsed.username and not parsed.password
            except ValueError:
                usable = False
            if usable:
                st.link_button('Open original source listing — unverified', row['original_source_url'])
            else:
                st.caption('Unverified original source link unavailable.')
