"""Pinpoint and Rippling tenant-hosted postings: employer provenance only.

Posting fields (title, employer, location, identifier, description, canonical
URL) come from the existing JobPosting JSON-LD extraction in job_description.
The ATS tenant name and the posting's self-declared hiringOrganization are not
employer proof. The employer's own corporate site must independently name the
employer and publish a careers link to this exact tenant: the host for Pinpoint
({tenant}.pinpointhq.com), the host plus first path segment for Rippling
(ats.rippling.com/{tenant}).

No guessed tenants, private APIs, script execution, logins or browser.
"""
import json
import time
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from src.verified_posting_identity import (
    hosted_tenant_posting_scope, hosted_tenant_url_matches, is_hosted_tenant_board_host,
)


def employer_website(soup) -> str | None:
    """The website a JobPosting's hiringOrganization declares (a lead, not proof)."""
    from src.enrichment.ats.base import walk_json
    for script in soup.find_all('script', attrs={'type': 'application/ld+json'}):
        try:
            payload = json.loads(script.string or script.get_text(strip=True) or '')
        except (ValueError, TypeError):
            continue
        for node in walk_json(payload):
            kind = node.get('@type')
            kinds = kind if isinstance(kind, list) else [kind]
            if 'JobPosting' not in kinds:
                continue
            org = node.get('hiringOrganization')
            if not isinstance(org, dict):
                continue
            for value in (org.get('sameAs'), org.get('url')):
                for item in (value if isinstance(value, list) else [value]):
                    if isinstance(item, str) and item.strip().startswith('https://'):
                        return item.strip()
    return None


def employer_provenance(job, result, client):
    """Require the fetched corporate site to publish this exact ATS tenant."""
    from src.enrichment.employer_careers import (
        CAREERS_CACHE, CACHE_TTL_SECONDS, PublicPages, public_https_url,
    )
    from src.enrichment.job_identity import normalize_company_name
    from src.enrichment.official_job_resolver import is_aggregator_domain
    scope = hosted_tenant_posting_scope(result.final_url or '')
    company = normalize_company_name(result.resolved_company)
    website = result.resolved_employer_website or ''
    if (not scope or not company or company != normalize_company_name(job.get('company_name'))
            or result.resolved_company_source != 'jobposting_json_ld'
            or not public_https_url(website) or is_aggregator_domain(website)
            or is_hosted_tenant_board_host(urlsplit(website).hostname or '')):
        return None
    platform, tenant, _ = scope
    prior = CAREERS_CACHE.get(company)
    if (prior and time.monotonic() - prior[0] < CACHE_TTL_SECONDS
            and hosted_tenant_url_matches(prior[1].get('careers_url'), platform, tenant)):
        return dict(prior[1])
    pages = PublicPages(client, job.setdefault('employer_discovery_trace', []), limit=3)
    response = pages.get(website)
    if response is None:
        return None
    soup = BeautifulSoup(response.text, 'html.parser')
    # The corporate document must independently name the employer and declare
    # its own URL; a reciprocal link alone cannot create employer provenance.
    title = soup.title.get_text(' ', strip=True).split('|')[0] if soup.title else ''
    declared = soup.find('meta', property='og:url', content=True)
    if (normalize_company_name(title) != company or not declared
            or urlsplit(declared['content']).hostname != urlsplit(str(response.url)).hostname):
        return None
    for anchor in soup.find_all('a', href=True):
        target = urljoin(str(response.url), anchor['href'])
        if (public_https_url(target) and hosted_tenant_url_matches(target, platform, tenant)
                and any(word in anchor.get_text(' ', strip=True).casefold()
                        for word in ('career', 'jobs', 'join our team'))):
            proof = dict(company=company, corporate_url=str(response.url),
                         careers_url=target, posting_url=result.final_url,
                         platforms=[platform], basis='fetched_reciprocal_employer_careers_link')
            CAREERS_CACHE[company] = (time.monotonic(), proof)
            return proof
    return None
