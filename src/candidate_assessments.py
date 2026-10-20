"""Stable candidate registration and append-only, score-once assessments.

No inferred name matches, implicit legacy migration, provider calls or expiration.
Callers own transactions; production backfill is a separately approved operation.
"""
from __future__ import annotations

import json
import re
import unicodedata
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from urllib.parse import unquote, urlsplit

from src import database


class CandidateIdentityError(ValueError):
    pass


class AssessmentConflict(ValueError):
    pass


def normalize_name(value):
    return ' '.join(unicodedata.normalize('NFKC', value).casefold().split())


def normalize_linkedin_url(value):
    if not value or not value.strip():
        return None
    value = value.strip()
    parsed = urlsplit(value if '://' in value else 'https://' + value)
    host = (parsed.hostname or '').casefold().rstrip('.')
    if (parsed.scheme not in {'http', 'https'} or parsed.username or parsed.password
            or parsed.port is not None or not (host == 'linkedin.com' or host.endswith('.linkedin.com'))):
        raise CandidateIdentityError('A LinkedIn profile URL is required')
    path = unquote(parsed.path).strip('/')
    if not re.fullmatch(r'in/[A-Za-z0-9_-]+', path, re.I):
        raise CandidateIdentityError('Use an exact /in/ LinkedIn profile URL')
    return 'https://www.linkedin.com/' + path.casefold()


def tables_exist(connection):
    return bool(connection.execute("SELECT count(*) FROM information_schema.tables WHERE table_schema='main' AND table_name='candidates'").fetchone()[0])


def initialize(connection):
    connection.execute('''CREATE TABLE IF NOT EXISTS candidates (
        candidate_id VARCHAR PRIMARY KEY, first_name VARCHAR NOT NULL,
        last_name VARCHAR NOT NULL, linkedin_url VARCHAR UNIQUE,
        active_resume_hash VARCHAR, revision BIGINT DEFAULT 0,
        created_at TIMESTAMPTZ DEFAULT current_timestamp)''')
    connection.execute('''CREATE TABLE IF NOT EXISTS candidate_resumes (
        resume_hash VARCHAR PRIMARY KEY, candidate_id VARCHAR NOT NULL REFERENCES candidates(candidate_id),
        associated_at TIMESTAMPTZ DEFAULT current_timestamp)''')
    connection.execute('''CREATE TABLE IF NOT EXISTS candidate_assessments (
        assessment_id VARCHAR PRIMARY KEY, candidate_id VARCHAR NOT NULL REFERENCES candidates(candidate_id),
        job_identity VARCHAR NOT NULL, record_key VARCHAR NOT NULL, resume_hash VARCHAR NOT NULL,
        payload_json VARCHAR NOT NULL, completed_at TIMESTAMPTZ NOT NULL,
        origin VARCHAR NOT NULL CHECK(origin IN ('migration','automatic','manual')),
        imported_key VARCHAR UNIQUE)''')
    connection.execute('''CREATE TABLE IF NOT EXISTS candidate_active_assessments (
        candidate_id VARCHAR NOT NULL REFERENCES candidates(candidate_id), job_identity VARCHAR NOT NULL,
        assessment_id VARCHAR NOT NULL UNIQUE REFERENCES candidate_assessments(assessment_id),
        PRIMARY KEY(candidate_id, job_identity))''')
    connection.execute('''CREATE TABLE IF NOT EXISTS candidate_scoring_claims (
        candidate_id VARCHAR NOT NULL REFERENCES candidates(candidate_id), job_identity VARCHAR NOT NULL, token VARCHAR NOT NULL UNIQUE,
        manual BOOLEAN NOT NULL, started_at TIMESTAMPTZ DEFAULT current_timestamp,
        PRIMARY KEY(candidate_id, job_identity))''')


def register_candidate(connection, first_name, last_name, linkedin_url=None, *, candidate_id=None, confirm_existing=False):
    first, last = normalize_name(first_name), normalize_name(last_name)
    if not first or not last:
        raise CandidateIdentityError('Supply both first and last name')
    linkedin = normalize_linkedin_url(linkedin_url)
    if candidate_id is not None:
        row = connection.execute('SELECT candidate_id FROM candidates WHERE candidate_id=?', [candidate_id]).fetchone()
        if not row or not confirm_existing:
            raise CandidateIdentityError('Explicit confirmation of an existing candidate is required')
        return row[0]
    if linkedin and connection.execute('SELECT 1 FROM candidates WHERE linkedin_url=?', [linkedin]).fetchone():
        raise CandidateIdentityError('This LinkedIn profile is registered; confirm the existing candidate')
    # Identical names are allowed, but never used as an automatic identity match.
    candidate_id = str(uuid.uuid4())
    connection.execute('INSERT INTO candidates(candidate_id,first_name,last_name,linkedin_url) VALUES(?,?,?,?)', [candidate_id,first,last,linkedin])
    return candidate_id


def associate_resume(connection, candidate_id, resume_hash, *, confirmed=False, activate=True):
    if not confirmed:
        raise CandidateIdentityError('Confirm the candidate before linking a resume')
    if not connection.execute('SELECT 1 FROM resume_profiles WHERE resume_hash=?', [resume_hash]).fetchone():
        raise CandidateIdentityError('Cached resume does not exist')
    previous = connection.execute('SELECT candidate_id FROM candidate_resumes WHERE resume_hash=?', [resume_hash]).fetchone()
    if previous and previous[0] != candidate_id:
        raise CandidateIdentityError('Resume is already associated with another candidate')
    if not previous:
        connection.execute('INSERT INTO candidate_resumes(resume_hash,candidate_id) VALUES(?,?)', [resume_hash,candidate_id])
    if activate:
        connection.execute('UPDATE candidates SET active_resume_hash=?, revision=revision+1 WHERE candidate_id=?', [resume_hash,candidate_id])


def candidate_for_resume(connection, resume_hash):
    if not tables_exist(connection) or not resume_hash:
        return None
    row = connection.execute('SELECT candidate_id FROM candidate_resumes WHERE resume_hash=?', [resume_hash]).fetchone()
    return row[0] if row else None


def candidate_context(connection, resume_hash):
    candidate = candidate_for_resume(connection, resume_hash)
    if not candidate:
        return None
    return connection.execute('SELECT candidate_id,active_resume_hash,revision FROM candidates WHERE candidate_id=?', [candidate]).fetchone()


def job_identities(connection, record_keys=None, *, prefer_verified=True):
    """Use the existing trust boundary; exact source identity is the safe fallback."""
    from src.repositories.recommendation_repository import exact_posting_identity, trusted_display_posting_key
    fields = ['record_key','source','source_job_id','apply_url']
    existing = {r[0] for r in connection.execute('DESCRIBE raw_jobs').fetchall()}
    select = ','.join(name if name in existing else 'NULL AS '+name for name in fields)
    where = ' WHERE record_key IN (SELECT unnest(?))' if record_keys is not None else ''
    args = [list(record_keys)] if record_keys is not None else []
    cursor = connection.execute('SELECT '+select+' FROM raw_jobs'+where, args)
    raw = [dict(zip([d[0] for d in cursor.description], r)) for r in cursor.fetchall()]
    attempts = {}
    if connection.execute("SELECT count(*) FROM information_schema.tables WHERE table_name='job_enrichment_attempts'").fetchone()[0]:
        fields = ['record_key','status','official_url_status','official_job_url','verified_posting_key','verified_posting_key_trust','verified_posting_key_source']
        present = {r[0] for r in connection.execute('DESCRIBE job_enrichment_attempts').fetchall()}
        select = ','.join(name if name in present else 'NULL AS '+name for name in fields)
        cursor = connection.execute('SELECT '+select+' FROM job_enrichment_attempts'+where,args)
        attempts = {r['record_key']:r for r in [dict(zip([d[0] for d in cursor.description], row)) for row in cursor.fetchall()]}
    result = {}
    for row in raw:
        # Enrichment status has a different UI name; proof fields remain verbatim.
        attempt = attempts.get(row['record_key'], {})
        merged = {**row, **attempt, 'enrichment_status': attempt.get('status')}
        verified = trusted_display_posting_key(merged)
        exact = exact_posting_identity(row.get('source'),row.get('source_job_id'),row.get('apply_url'),row['record_key'])
        result[row['record_key']] = ('verified:' + verified) if verified and prefer_verified else ('exact:' + exact)
    return result


def posting_aliases(connection):
    """Only exact source IDs can bridge a missing proof to one trusted posting."""
    identities = job_identities(connection)
    exact = job_identities(connection, prefer_verified=False)
    proofs = {}
    for key, identity in identities.items():
        if identity.startswith('verified:'):
            proofs.setdefault(exact[key], set()).add(identity)
    return identities, exact, proofs


def scoring_identity(connection, record_key):
    identities, exact, proofs = posting_aliases(connection)
    identity = identities.get(record_key)
    if identity and identity.startswith('exact:'):
        compatible = proofs.get(exact[record_key], set())
        if len(compatible) > 1:
            return None  # Hold an unverified sibling with competing positive proof.
        if len(compatible) == 1:
            return next(iter(compatible))
    return identity


def active_assessments(connection, candidate_id):
    if not candidate_id:
        return {}
    cursor = connection.execute('''SELECT a.assessment_id,a.job_identity,a.record_key,a.payload_json
        FROM candidate_active_assessments p JOIN candidate_assessments a ON p.assessment_id=a.assessment_id
        AND p.candidate_id=a.candidate_id WHERE p.candidate_id=?''', [candidate_id])
    rows = cursor.fetchall()
    current_identities = job_identities(connection, [row[2] for row in rows])
    exact_identities = job_identities(connection, [row[2] for row in rows], prefer_verified=False)
    result = {}
    for aid,identity,key,payload in rows:
        item = {'assessment_id':aid,'job_identity':identity,'record_key':key,'score':json.loads(payload)}
        # Current authoritative proof may unite formerly separate exact postings.
        scope = (current_identities.get(key,identity)
                 if identity.startswith('exact:') and exact_identities.get(key)==identity
                 else identity)
        old = result.get(scope)
        if old is None or (datetime.fromisoformat(item['score']['scored_at']),aid) > (datetime.fromisoformat(old['score']['scored_at']),old['assessment_id']):
            result[scope] = item
    return result



def assessment_lookup(connection,candidate_id):
    active = active_assessments(connection,candidate_id)
    originals = list(active.values())
    current, exact, proofs = posting_aliases(connection)
    for row in originals:
        frozen_exact = row['score'].get('_exact_posting_identity')
        key = row['record_key']
        if not frozen_exact or exact.get(key) != frozen_exact:
            continue
        known = proofs.get(frozen_exact, set())
        identity = row['job_identity']
        # Never bridge competing positive requisitions, or an assessment whose
        # source now positively identifies a different verified posting.
        if len(known) > 1 or (identity.startswith('verified:') and known - {identity}):
            continue
        if identity.startswith('verified:'):
            # Prefer the established verified assessment over an accidental
            # exact-scope duplicate; no persisted pointer or history is changed.
            active[frozen_exact] = row
        elif current.get(key, '').startswith('exact:'):
            active.setdefault(frozen_exact, row)
        if len(known) == 1:
            active.setdefault(next(iter(known)), row)
    return active

def completed_record_keys(connection, resume_hash):
    candidate = candidate_for_resume(connection,resume_hash)
    if not candidate:
        return set()
    active = assessment_lookup(connection,candidate)
    identities = job_identities(connection)
    return {key for key,identity in identities.items() if identity in active}


def register_completed_filter(connection,resume_hash):
    keys = completed_record_keys(connection,resume_hash)
    connection.create_function('candidate_has_completed',lambda key:key in keys,[str],bool,null_handling='special')


def overlay_saved_scores(connection,resume_hash,jobs):
    candidate = candidate_for_resume(connection,resume_hash)
    if not candidate:
        if tables_exist(connection) and connection.execute('SELECT count(*) FROM candidates').fetchone()[0]:
            raise CandidateIdentityError('Select a registered candidate; this resume has not been confirmed')
        return
    active = assessment_lookup(connection,candidate)
    identities = job_identities(connection)
    for job in jobs:
        saved = active.get(identities.get(job['record_key']))
        # Never leak resume-scoped/mart rows from a different candidate or unimported history.
        job.update(ai_score=None, recommendation=None, ai_scored_at=None, ai_prompt_version=None)
        if not saved:
            continue
        score = saved['score']
        for field in ['recommendation','confidence','title_fit','skills_fit','experience_fit','seniority_fit','industry_fit','location_fit','requirement_matches','matching_strengths','hard_requirements_missing','preferred_qualifications_missing','risk_factors','summary','description_word_count','description_complete']:
            job[field] = score.get(field)
        job.update(ai_score=score['overall_score'], ai_prompt_version=score['prompt_version'],
            ai_scored_at=score['scored_at'], ai_model_name=score['model_name'],
            ai_score_scope='exact' if saved['record_key']==job['record_key'] else 'verified_posting',
            has_incomplete_description=False,candidate_assessment_id=saved['assessment_id'],candidate_id=candidate)


def assessment_complete(score):
    return (score.get('description_complete') is True and score.get('description_word_count',0)>=80
        and isinstance(score.get('overall_score'),int) and 0<=score['overall_score']<=100
        and score.get('recommendation') in {'apply','review','skip'})


def append_assessment(connection,candidate_id,identity,score,*,manual=False,origin=None,imported_key=None):
    if not assessment_complete(score):
        raise AssessmentConflict('Incomplete assessment cannot become authoritative')
    if not manual:
        completed = assessment_lookup(connection, candidate_id).get(identity)
        if completed:
            return completed['assessment_id'], False
    current = connection.execute('SELECT assessment_id FROM candidate_active_assessments WHERE candidate_id=? AND job_identity=?',[candidate_id,identity]).fetchone()
    if current and not manual:
        return current[0],False
    score = dict(score)
    score['_exact_posting_identity'] = job_identities(connection,[score['record_key']],prefer_verified=False)[score['record_key']]
    assessment_id = str(uuid.uuid4())
    connection.execute('INSERT INTO candidate_assessments VALUES(?,?,?,?,?,?,?,?,?)',[assessment_id,candidate_id,identity,score['record_key'],score['resume_hash'],json.dumps(score,default=str,ensure_ascii=False),score['scored_at'],origin or ('manual' if manual else 'automatic'),imported_key])
    if current:
        connection.execute('UPDATE candidate_active_assessments SET assessment_id=? WHERE candidate_id=? AND job_identity=?',[assessment_id,candidate_id,identity])
    else:
        connection.execute('INSERT INTO candidate_active_assessments VALUES(?,?,?)',[candidate_id,identity,assessment_id])
    connection.execute('UPDATE candidates SET revision=revision+1 WHERE candidate_id=?',[candidate_id])
    return assessment_id,True


@contextmanager
def scoring_claim(resume_hash,job,*,manual=False,details=None):
    """Atomic claim before providers; no time-based automatic retry of a live claim."""
    token = None
    candidate = None
    identity = None
    with database.get_connection() as c:
        candidate = candidate_for_resume(c,resume_hash)
        if candidate:
            identity = scoring_identity(c, job['record_key'])
            if details is not None:
                details.update(identity=identity, exact_identity=job_identities(c, [job['record_key']], prefer_verified=False).get(job['record_key']))
            if identity is None and not job_identities(c,[job['record_key']],prefer_verified=False).get(job['record_key']):
                raise AssessmentConflict('Job has no exact identity')
            from src.repositories.recommendation_repository import automatic_statuses
            if identity is None:
                yield_value = False
                if details is not None:
                    details['reason'] = 'conflicting_verified_posting_evidence'
            elif not manual and automatic_statuses(c, [job['record_key']]).get(job['record_key'], 'new') != 'new':
                yield_value = False
                if details is not None:
                    details['reason'] = 'application_status_already_handled'
            elif not manual and identity in assessment_lookup(c,candidate):
                yield_value = False
                if details is not None:
                    details['reason'] = 'candidate_already_completed'
            else:
                token = str(uuid.uuid4())
                try:
                    c.execute('INSERT INTO candidate_scoring_claims(candidate_id,job_identity,token,manual) VALUES(?,?,?,?)',[candidate,identity,token,manual])
                except Exception as exc:
                    import duckdb
                    if not isinstance(exc,(duckdb.ConstraintException,duckdb.TransactionException)):
                        raise
                    token = None
                yield_value = token is not None
                if not yield_value and details is not None:
                    details['reason'] = 'claimed_by_another_run'
                if token and not manual:
                    # A competing worker can finish and release its claim
                    # between our initial lookup and INSERT. Recheck while
                    # owning the unique claim, before any provider is called.
                    reason = None
                    if identity in assessment_lookup(c, candidate):
                        reason = 'candidate_already_completed'
                    elif automatic_statuses(c, [job['record_key']]).get(job['record_key'], 'new') != 'new':
                        reason = 'application_status_already_handled'
                    elif scoring_identity(c, job['record_key']) != identity:
                        reason = 'posting_identity_changed_before_scoring'
                    if reason:
                        c.execute('DELETE FROM candidate_scoring_claims WHERE token=?', [token])
                        token = None
                        yield_value = False
                        if details is not None:
                            details['reason'] = reason
        else:
            yield_value = True # Unmigrated legacy callers retain existing behavior.
    try:
        yield yield_value
    finally:
        if token:
            with database.get_connection() as c:
                c.execute('DELETE FROM candidate_scoring_claims WHERE token=?',[token])


def persist_score(connection,resume_hash,score):
    """Return None for an unmigrated caller; otherwise append without legacy deletion."""
    candidate = candidate_for_resume(connection,resume_hash)
    if not candidate:
        return None
    identity = scoring_identity(connection, score['record_key'])
    claim = connection.execute('SELECT manual FROM candidate_scoring_claims WHERE candidate_id=? AND job_identity=?',[candidate,identity]).fetchone()
    if not claim:
        raise AssessmentConflict('Candidate scoring requires an explicit scoring claim')
    if claim[0]:
        active = assessment_lookup(connection,candidate).get(identity)
        if active and active['job_identity'] != identity:
            connection.execute('DELETE FROM candidate_active_assessments WHERE candidate_id=? AND job_identity=?',[candidate,active['job_identity']])
    return append_assessment(connection,candidate,identity,score,manual=claim[0])


def latest_candidate_resume(connection,resume_hash):
    ctx = candidate_context(connection,resume_hash)
    if not ctx and tables_exist(connection) and connection.execute('SELECT count(*) FROM candidates').fetchone()[0]:
        raise CandidateIdentityError('Resume is not associated with a confirmed candidate')
    return ctx[1] if ctx and ctx[1] else resume_hash
