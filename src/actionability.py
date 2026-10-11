"""Current actionability, separate from fit, enrichment and personal status.

Only exact, trusted posting evidence is checked. No discovery or AI calls.
"""
from __future__ import annotations
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit, urljoin
import httpx
from bs4 import BeautifulSoup
from src.config_loader import load_searches
from src.enrichment.official_job_resolver import is_aggregator_domain
from src.enrichment.job_description import (extract_job_description_from_html, walk_json,
    is_job_posting_type, BLOCK_PAGE_PHRASES, create_http_client)
from src.enrichment.job_identity import validate_job_identity, location_compatibility
from src.verified_posting_identity import verified_posting_key_from_url, verified_key_metadata_is_trusted

STATUSES = ('ACTIVE', 'INACTIVE', 'LOCATION_MISMATCH', 'UNKNOWN')
@dataclass(frozen=True)
class Settings:
    country: str = 'United States'
    ttl_hours: float = 24
    batch_size: int = 25
    def __post_init__(self):
        if self.ttl_hours <= 0 or self.batch_size < 1:
            raise ValueError('Actionability TTL and batch size must be positive')
    @property
    def context(self):
        return json.dumps({'country':self.country},sort_keys=True)

def settings():
    defaults,_=load_searches()
    config=defaults.get('actionability',{})
    return Settings(country=config.get('candidate_country',''),ttl_hours=float(config.get('ttl_hours',24)),batch_size=int(config.get('batch_size',25)))

def now_utc(): return datetime.now(timezone.utc)
def trusted_key(job):
    if job.get('official_url_status') != 'FOUND_VERIFIED': return ''
    if not verified_key_metadata_is_trusted(job.get('verified_posting_key_source'),job.get('verified_posting_key_trust')): return ''
    if is_aggregator_domain(job.get('official_job_url')): return ''
    key=job.get('verified_posting_key') or ''
    return key if key and verified_posting_key_from_url(job.get('official_job_url') or '')==key else ''

@dataclass(frozen=True)
class Result:
    status: str
    reason: str
    checked_at: str
    posting_key: str
    official_url: str
    candidate_context: str
    evidence: dict

def effective_status(job, config=None, now=None):
    config=config or settings();now=now or now_utc()
    try:
        checked=datetime.fromisoformat(str(job.get('actionability_checked_at')).replace('Z','+00:00'))
        if checked.tzinfo is None: checked=checked.replace(tzinfo=timezone.utc)
        fresh=timedelta(0)<=now-checked<=timedelta(hours=config.ttl_hours)
    except (ValueError,TypeError): fresh=False
    key=trusted_key(job)
    valid=fresh and key and key==job.get('actionability_posting_key') and job.get('official_job_url')==job.get('actionability_official_url') and config.context==job.get('actionability_candidate_context')
    return job.get('actionability_status','UNKNOWN') if valid and job.get('actionability_status') in STATUSES else 'UNKNOWN'

def check_job(job, client, config=None, now=None):
    config=config or settings();now=now or now_utc();url=job.get('official_job_url') or '';key=trusted_key(job)
    def result(status,reason,**evidence):
        return Result(status,reason,now.isoformat(),key,url,config.context,evidence)
    if not key: return result('UNKNOWN','Trusted exact official posting key unavailable')
    try: response=client.get(url)
    except httpx.RequestError: return result('UNKNOWN','Temporary network/timeout failure',error_category='network')
    final=str(response.url);code=response.status_code
    if code in (401,403,429) or code>=500:
        return result('UNKNOWN','Blocked or temporary HTTP failure',http_status=code,error_category='blocked' if code in (401,403,429) else 'network')
    if verified_posting_key_from_url(final)!=key:
        return result('UNKNOWN','Redirect did not retain exact trusted posting identity',http_status=code,final_url=final)
    soup=BeautifulSoup(response.text,'html.parser')
    visible=BeautifulSoup(response.text,'html.parser')
    for tag in visible(['script','style','nav','footer']): tag.decompose()
    text=visible.get_text(' ',strip=True).casefold()
    if any(p in text for p in BLOCK_PAGE_PHRASES): return result('UNKNOWN','Bot challenge',http_status=code,error_category='blocked')
    parsed=extract_job_description_from_html(requested_url=url,final_url=final,http_status=code,html_text=response.text)
    nodes=[]
    for script in soup.find_all('script',type='application/ld+json'):
        try: nodes.extend(n for n in walk_json(json.loads(script.get_text())) if is_job_posting_type(n.get('@type')))
        except (ValueError,TypeError): pass
    evidence={'http_status':code,'final_url':final,'resolved_title':parsed.resolved_title,'resolved_title_source':parsed.resolved_title_source,'resolved_company':parsed.resolved_company,'resolved_company_source':parsed.resolved_company_source,'resolved_location':parsed.resolved_location,'resolved_location_source':parsed.resolved_location_source,'resolved_job_id':parsed.resolved_job_id,'resolved_job_id_source':parsed.resolved_job_id_source}
    # Title/company are independently extracted, never supplied as resolved fields.
    from src.enrich_jobs import trusted_original_identity_key
    original_proof=trusted_original_identity_key(job)
    identity=validate_job_identity(original_title=job.get('title'),original_company=job.get('company_name'),resolved_title=parsed.resolved_title,resolved_company=parsed.resolved_company,trusted_original_posting_key=original_proof,resolved_posting_key=verified_posting_key_from_url(final))
    evidence['identity_reason']=identity.reason
    # JobPosting canonical URL is fetched evidence, not a search hint.
    for node in nodes:
        canonical=node.get('url')
        if canonical and verified_posting_key_from_url(str(canonical))!=key:
            return result('UNKNOWN','Structured posting URL differs from trusted exact posting',**evidence)
    # A structured different requisition cannot inherit the URL's historical trust.
    old_id=(job.get('official_resolved_job_id') or '').strip()
    if old_id and parsed.resolved_job_id and old_id!=parsed.resolved_job_id:
        return result('UNKNOWN','Fetched requisition differs from trusted posting',**evidence)
    if 'myworkdayjobs.com' in (urlsplit(url).hostname or '') and parsed.resolved_job_id and parsed.resolved_job_id not in urlsplit(final).path:
        return result('UNKNOWN','Fetched requisition differs from trusted posting',**evidence)
    if key.startswith('greenhouse:') and parsed.resolved_job_id and key.split(':',1)[1]!=parsed.resolved_job_id:
        return result('UNKNOWN','Fetched requisition differs from trusted posting',**evidence)
    # ATS-specific closure text at the exact URL; never interpret HTTP 404 alone.
    ats= urlsplit(url).hostname or ''
    closure_text=' '.join(el.get_text(' ',strip=True).casefold() for el in visible.find_all(['h1','h2','h3','[role=alert]']))
    if len(text)<500: closure_text+=' '+text
    closure=any(p in closure_text for p in ('this job is no longer available','this job is no longer accepting applications','this position has been filled','this job has been closed','this job has expired','job posting has expired','job not found','this job has been removed'))
    known_ats=any(ats==d or ats.endswith('.'+d) for d in ('greenhouse.io','ashbyhq.com','lever.co','myworkdayjobs.com','smartrecruiters.com','icims.com'))
    if closure and (identity.accepted or known_ats): return result('INACTIVE','Exact posting explicitly reports closed/removed',evidence_source='fetched_posting_closed_state',**evidence)
    if code>=400: return result('UNKNOWN','HTTP error without authoritative closure evidence',**evidence)
    if not identity.accepted: return result('UNKNOWN','Current posting identity not established: '+identity.reason,**evidence)
    for node in nodes:
        expiry=node.get('validThrough')
        if expiry:
            try:
                date=datetime.fromisoformat(str(expiry).replace('Z','+00:00'))
                if date.tzinfo is None: date=date.replace(tzinfo=timezone.utc)
                if date<now: return result('INACTIVE','Authoritative JobPosting validThrough has expired',evidence_source='jobposting_json_ld',validThrough=expiry,**evidence)
            except ValueError: pass
    if not config.country: return result('UNKNOWN','Candidate geography is not configured',**evidence)
    location,reason=location_compatibility(config.country,parsed.resolved_location,parsed.resolved_location_source)
    # Extend country coverage for candidate eligibility only, never posting identity.
    loc=(parsed.resolved_location or '').casefold()
    if location!='mismatch' and parsed.resolved_location_source and config.country=='United States':
        foreign=('europe','european union','brazil','germany','france','spain','portugal','ireland','poland','netherlands','india')
        us=any(x in loc for x in ('united states','usa','remote - us'))
        if not us and any(x in loc for x in foreign): location,reason='mismatch','Authoritative location excludes candidate US geography'
    # Only explicit residency requirements in the fetched posting count. General
    # office mentions, timezone preferences and original alert text never do.
    requirement=re.search(
        r'(?:must (?:be based|reside|live)|required to (?:reside|live)|only (?:hire|hiring) candidates (?:based|located)) in ([^.!\n]{2,100})',
        parsed.description, re.I)
    if requirement and config.country=='United States':
        scope=requirement.group(1).casefold()
        if any(x in scope for x in ('europe','european union','canada','brazil','united kingdom','germany','france','india')) and not re.search(r'\b(united states|usa|us|worldwide|globally)\b',scope):
            location,reason='mismatch','Explicit fetched posting residency requirement excludes candidate US geography'
            evidence['location_requirement']=requirement.group(0)
            evidence['location_requirement_source']='fetched_posting_explicit_residency_requirement'
    evidence.update(location_compatibility=location,location_reason=reason)
    if location=='mismatch': return result('LOCATION_MISMATCH',reason,**evidence)
    # Require an enabled application control, not merely an HTTP-200 JD shell.
    controls=[el for el in soup.find_all(['a','button','input','form']) if not el.has_attr('disabled') and el.get('aria-disabled')!='true']
    def usable_control(el):
        if el.name == 'a':
            href=str(el.get('href') or '')
            if href.startswith('#'):
                return 'application' in href or 'apply' in href
            return bool(href) and verified_posting_key_from_url(urljoin(final,href))==key
        return True
    controls=[el for el in controls if usable_control(el)]
    apply_signal=any(('apply' in el.get_text(' ',strip=True).casefold() or 'apply' in str(el.get('value','')).casefold() or 'application' in str(el.get('id','')).casefold() or 'application' in str(el.get('action','')).casefold()) for el in controls)
    if not apply_signal: return result('UNKNOWN','Exact posting has no confirmed application control',**evidence)
    return result('ACTIVE','Exact live posting with application control; no authoritative geography conflict',evidence_source='fetched_posting_application_control',**evidence)

SCHEMA='''CREATE TABLE IF NOT EXISTS job_actionability (
record_key VARCHAR PRIMARY KEY, actionability_status VARCHAR NOT NULL,
actionability_checked_at VARCHAR NOT NULL, actionability_reason VARCHAR NOT NULL,
actionability_posting_key VARCHAR NOT NULL, actionability_official_url VARCHAR NOT NULL,
actionability_candidate_context VARCHAR NOT NULL, actionability_evidence VARCHAR NOT NULL)'''

def attach_cached(jobs,connection):
    exists=connection.execute("SELECT count(*) FROM information_schema.tables WHERE table_name='job_actionability'").fetchone()[0]
    if not exists: return jobs
    r=connection.execute('SELECT * FROM job_actionability');names=[d[0] for d in r.description]
    cached={row[0]:dict(zip(names,row)) for row in r.fetchall()}
    for job in jobs: job.update(cached.get(job['record_key'],{}))
    return jobs

def refresh(jobs,connection,client,config=None,now=None):
    from src.ui.job_visibility import classify_job_fit_bucket
    config=config or settings();now=now or now_utc()
    metrics={s:0 for s in STATUSES};metrics.update(candidates=0,checks=0,cache_hits=0,blocked=0,network=0,moved_to_needs_review=0,removed_from_recommended=0,deferred=0)
    targets=[j for j in jobs if j.get('application_status','new')=='new' and j.get('has_current_complete_ai_assessment') and classify_job_fit_bucket(j)=='recommended' and str(j.get('recommendation','')).lower()!='skip']
    def priority(job):
        try: score=float(job.get('ai_score') or 0)
        except (TypeError,ValueError): score=0
        return (str(job.get('recommendation') or '').lower()!='apply', -score, str(job.get('record_key') or ''))
    targets.sort(key=priority)
    metrics['candidates']=len(targets)
    attach_cached(jobs,connection)
    for job in targets:
        if effective_status(job,config,now)!='UNKNOWN': metrics['cache_hits']+=1;continue
        # Fresh UNKNOWN results are cached too: avoid repeated blocked requests.
        if job.get('actionability_status')=='UNKNOWN' and not trusted_key(job):
            try:
                checked=datetime.fromisoformat(str(job.get('actionability_checked_at')).replace('Z','+00:00'))
                if checked.tzinfo is None: checked=checked.replace(tzinfo=timezone.utc)
                fresh=timedelta(0)<=now-checked<=timedelta(hours=config.ttl_hours)
            except (ValueError,TypeError): fresh=False
            if fresh and job.get('actionability_official_url')==job.get('official_job_url') and job.get('actionability_candidate_context')==config.context and not job.get('actionability_posting_key'):
                metrics['cache_hits']+=1;continue
        probe=dict(job,actionability_status='ACTIVE')
        if effective_status(probe,config,now)=='ACTIVE': metrics['cache_hits']+=1;continue
        if metrics['checks']>=config.batch_size: metrics['deferred']+=1;continue
        value=check_job(job,client,config,now);metrics['checks']+=1;metrics[value.status]+=1
        metrics['blocked']+=value.evidence.get('error_category')=='blocked';metrics['network']+=value.evidence.get('error_category')=='network'
        metrics['moved_to_needs_review']+=value.status=='UNKNOWN';metrics['removed_from_recommended']+=value.status in ('INACTIVE','LOCATION_MISMATCH')
        fields=(job['record_key'],value.status,value.checked_at,value.reason,value.posting_key,value.official_url,value.candidate_context,json.dumps(value.evidence))
        connection.execute(SCHEMA)
        connection.execute('INSERT OR REPLACE INTO job_actionability VALUES (?, ?, ?, ?, ?, ?, ?, ?)',fields)
        names=('record_key','actionability_status','actionability_checked_at','actionability_reason','actionability_posting_key','actionability_official_url','actionability_candidate_context','actionability_evidence')
        job.update(dict(zip(names,fields)))
    return metrics

def refresh_daily(jobs):
    from src.database import get_connection
    with get_connection() as connection, create_http_client(timeout_seconds=12) as client:
        return refresh(jobs,connection,client)
