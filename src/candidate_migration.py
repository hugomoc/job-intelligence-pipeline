"""Explicit, preview-bound legacy association. Never invoked by application startup."""
import hashlib
import json
import uuid
from collections import defaultdict
from datetime import datetime

from src import candidate_assessments as store


def canonical(value):
    return json.dumps(value,sort_keys=True,default=str,ensure_ascii=False,separators=(',',':'))


def sha(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def snapshot(connection):
    result = {}
    for schema,table in connection.execute("SELECT table_schema,table_name FROM information_schema.tables WHERE table_type='BASE TABLE' ORDER BY 1,2").fetchall():
        if table.startswith('candidate_') or table=='candidates':
            continue
        cur = connection.execute('SELECT * FROM "'+schema.replace('"','""')+'"."'+table.replace('"','""')+'"')
        names = [x[0] for x in cur.description]
        result[schema+'.'+table] = sha(sorted(sha(dict(zip(names,row))) for row in cur.fetchall()))
    return result


def make_preview(connection,first_name,last_name,linkedin_url,resume_hashes,*,candidate_id=None):
    if not resume_hashes or len(set(resume_hashes))!=len(resume_hashes):
        raise ValueError('Specify distinct confirmed resume hashes')
    first,last = store.normalize_name(first_name),store.normalize_name(last_name)
    if not first or not last:
        raise ValueError('Explicit candidate name is required')
    if store.tables_exist(connection) and connection.execute('SELECT count(*) FROM candidates').fetchone()[0]:
        raise ValueError('Existing candidate registry requires a separately reviewed association preview')
    cid = candidate_id or str(uuid.uuid4())
    identities = store.job_identities(connection)
    exact_identities = store.job_identities(connection, prefer_verified=False)
    profiles = []
    scores = []
    skipped = []
    for h in resume_hashes:
        rows = connection.execute('SELECT * FROM resume_profiles WHERE resume_hash=?',[h]).fetchall()
        if len(rows)!=1:
            raise ValueError('Missing/ambiguous cached resume: '+h)
        profiles.append({'resume_hash':h,'profile_sha256':sha(rows[0])})
        cur = connection.execute('SELECT * FROM resume_job_scores WHERE resume_hash=? ORDER BY record_key',[h])
        names = [d[0] for d in cur.description]
        for row in cur.fetchall():
            score = json.loads(canonical(dict(zip(names,row))))
            if not store.assessment_complete(score):
                skipped.append({'resume_hash':h,'record_key':score['record_key'],'reason':'incomplete assessment'})
                continue
            if score['record_key'] not in identities:
                raise ValueError('Missing source job identity')
            scores.append({'job_identity':identities[score['record_key']], 'exact_posting_identity':exact_identities[score['record_key']], 'source_sha256':sha(score), 'score':score})
    groups = defaultdict(list)
    for score in scores:
        groups[score['job_identity']].append(score)
    def rank(row):
        s=row['score']
        return (datetime.fromisoformat(s['scored_at']).timestamp(),s['record_key'],s['resume_hash'],row['source_sha256'])
    active = {identity:max(group,key=rank)['source_sha256'] for identity,group in groups.items()}
    unmapped = connection.execute('SELECT resume_hash FROM resume_profiles ORDER BY resume_hash').fetchall()
    return {'mode':'preview_only','candidate':{'candidate_id':cid,'first_name':first,'last_name':last,'linkedin_url':store.normalize_linkedin_url(linkedin_url)},
        'resume_hashes':resume_hashes,'active_resume_hash':resume_hashes[-1], 'profiles':profiles,
        'source_tables_sha256':snapshot(connection),'assessments':scores,'active':active,'skipped':skipped,
        'unassociated_resume_hashes':[h for h, in unmapped if h not in resume_hashes],
        'duplicate_groups':[{'job_identity':identity,'assessments':[{'record_key':r['score']['record_key'],'resume_hash':r['score']['resume_hash'],'score':r['score']['overall_score'],'scored_at':r['score']['scored_at']} for r in group], 'active_source_sha256':active[identity]} for identity,group in groups.items() if len(group)>1],
        'selection_rule':'Latest completed scored_at; ties by record_key, resume_hash, then immutable source checksum. Never highest score.',
        'new_tables':['candidates','candidate_resumes','candidate_assessments','candidate_active_assessments','candidate_scoring_claims']}


def apply_preview(connection,preview):
    """Run inside one caller-owned transaction; a failure must roll it back."""
    fresh = make_preview(connection,preview['candidate']['first_name'],preview['candidate']['last_name'],preview['candidate']['linkedin_url'],preview['resume_hashes'],candidate_id=preview['candidate']['candidate_id'])
    if sha(fresh)!=sha(preview):
        raise ValueError('Migration preview drift; generate and approve a new preview')
    store.initialize(connection)
    c = preview['candidate']
    connection.execute('INSERT INTO candidates(candidate_id,first_name,last_name,linkedin_url) VALUES(?,?,?,?)',[c['candidate_id'],c['first_name'],c['last_name'],c['linkedin_url']])
    for h in preview['resume_hashes']:
        store.associate_resume(connection,c['candidate_id'],h,confirmed=True,activate=False)
    connection.execute('UPDATE candidates SET active_resume_hash=? WHERE candidate_id=?',[preview['active_resume_hash'],c['candidate_id']])
    for row in preview['assessments']:
        score=row['score']
        aid=str(uuid.uuid5(uuid.UUID(c['candidate_id']),row['source_sha256']))
        connection.execute('INSERT INTO candidate_assessments VALUES(?,?,?,?,?,?,?,?,?)',[aid,c['candidate_id'],row['job_identity'],score['record_key'],score['resume_hash'],canonical({**score, '_exact_posting_identity':row['exact_posting_identity']}),score['scored_at'],'migration',row['source_sha256']])
        if preview['active'][row['job_identity']]==row['source_sha256']:
            connection.execute('INSERT INTO candidate_active_assessments VALUES(?,?,?)',[c['candidate_id'],row['job_identity'],aid])
    assert connection.execute('SELECT count(*) FROM candidate_assessments WHERE candidate_id=?',[c['candidate_id']]).fetchone()[0]==len(preview['assessments'])
    assert len(store.active_assessments(connection,c['candidate_id']))==len(preview['active'])
    verify_applied(connection, preview)


def verify_applied(connection,preview):
    """Verify persisted associations and every migrated snapshot without touching sources."""
    cid=preview['candidate']['candidate_id']
    assert snapshot(connection)==preview['source_tables_sha256'], 'Existing source data changed'
    associations=connection.execute('SELECT resume_hash FROM candidate_resumes WHERE candidate_id=? ORDER BY resume_hash',[cid]).fetchall()
    assert [r[0] for r in associations]==sorted(preview['resume_hashes'])
    candidate=connection.execute('SELECT first_name,last_name,linkedin_url,active_resume_hash FROM candidates WHERE candidate_id=?',[cid]).fetchone()
    c=preview['candidate']
    assert candidate==(c['first_name'],c['last_name'],c['linkedin_url'],preview['active_resume_hash'])
    rows=connection.execute('SELECT imported_key,payload_json FROM candidate_assessments WHERE candidate_id=?',[cid]).fetchall()
    assert len(rows)==len(preview['assessments'])
    imported={key:json.loads(payload) for key,payload in rows}
    for original in preview['assessments']:
        expected={**original['score'],'_exact_posting_identity':original['exact_posting_identity']}
        assert imported[original['source_sha256']]==expected, 'Original score metadata differs'
    active=connection.execute('''SELECT p.job_identity,a.imported_key FROM candidate_active_assessments p
        JOIN candidate_assessments a ON p.assessment_id=a.assessment_id AND p.candidate_id=a.candidate_id
        WHERE p.candidate_id=?''',[cid]).fetchall()
    assert dict(active)==preview['active'], 'Active-record selection differs'
