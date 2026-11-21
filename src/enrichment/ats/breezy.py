"""Breezy's posting-owned JSON, bound to its tenant and position identifier.

No guessed tenants, script execution, application submission or model calls.
"""
import json
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from src.verified_posting_identity import breezy_posting_scope


def posting_data(soup, url):
    scope = breezy_posting_scope(url)
    if not scope:
        return {}
    tenant = scope[0].split('.')[0]
    for script in soup.find_all('script'):
        try:
            payload = json.loads(script.get_text())
        except (ValueError, TypeError):
            continue
        data = payload.get('data', {}) if isinstance(payload, dict) else {}
        if not isinstance(data, dict):
            continue
        position, company = data.get('position'), data.get('companyForClient')
        if not isinstance(position, dict) or not isinstance(company, dict):
            continue
        other = position.get('company') or {}
        if (not isinstance(other, dict) or not isinstance(company.get('name'), str)
                or not isinstance(position.get('name'), str)
                or not isinstance(position.get('description'), str)
                or position.get('_id') != scope[1] or company.get('friendly_id') != tenant
                or other.get('friendly_id') != tenant or not company.get('name')
                or other.get('name') != company['name']):
            continue
        location = position.get('location') or {}
        if not isinstance(location, dict):
            location = {}
        remote = (location.get('remote_details') or {}).get('label') or ''
        country = (location.get('country') or {}).get('name') or ''
        return dict(title=position.get('name'), company=company['name'],
                    job_id=position['_id'], description=position.get('description') or '',
                    location=', '.join(filter(None, [location.get('name'), country, remote])),
                    employer_public_url=company.get('url'), state=position.get('state'))
    return {}


def employer_provenance(job, result, client):
    """Require the fetched corporate site to publish this exact ATS tenant.

    A search snippet, matching tenant spelling or ATS self-assertion is not
    sufficient. This explicit reciprocal link is an alternative to JSON-LD
    branding on a corporate site that does not publish Organization JSON-LD.
    """
    from src.enrichment.employer_careers import (
        CAREERS_CACHE, CACHE_TTL_SECONDS, PublicPages, public_https_url,
    )
    from src.enrichment.job_identity import normalize_company_name
    from src.enrichment.official_job_resolver import is_aggregator_domain
    import time
    scope = breezy_posting_scope(result.final_url or '')
    company = normalize_company_name(result.resolved_company)
    website = result.resolved_employer_website or ''
    if (not scope or company != normalize_company_name(job.get('company_name'))
            or result.resolved_job_id != scope[1] or not public_https_url(website)
            or is_aggregator_domain(website)
            or (urlsplit(website).hostname or '').endswith('.breezy.hr')):
        return None
    prior = CAREERS_CACHE.get(company)
    if (prior and time.monotonic() - prior[0] < CACHE_TTL_SECONDS
            and urlsplit(prior[1].get('careers_url', '')).hostname == scope[0]):
        return dict(prior[1])
    pages = PublicPages(client, job.setdefault('employer_discovery_trace', []), limit=3)
    response = pages.get(website)
    if response is None:
        return None
    soup = BeautifulSoup(response.text, 'html.parser')
    # The corporate document must independently name the employer and declare
    # its own URL. Reciprocal links alone cannot turn an ATS self-asserted
    # company name and arbitrary website into employer provenance.
    title = soup.title.get_text(' ', strip=True).split('|')[0] if soup.title else ''
    declared = soup.find('meta', property='og:url', content=True)
    if (normalize_company_name(title) != company or not declared
            or urlsplit(declared['content']).hostname != urlsplit(str(response.url)).hostname):
        return None
    for anchor in soup.find_all('a', href=True):
        target = urljoin(str(response.url), anchor['href'])
        if (public_https_url(target) and urlsplit(target).hostname == scope[0]
                and any(word in anchor.get_text(' ', strip=True).casefold()
                        for word in ('career', 'jobs', 'join our team'))):
            proof = dict(company=company, corporate_url=str(response.url),
                         careers_url=target, posting_url=result.final_url,
                         platforms=['breezy'], basis='fetched_reciprocal_employer_careers_link')
            CAREERS_CACHE[company] = (time.monotonic(), proof)
            return proof
    return None
