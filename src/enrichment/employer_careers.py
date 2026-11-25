"""Bounded employer-homepage -> careers inventory discovery, without search/AI.

Domain probes are leads, never provenance. Fetched corporate branding and an
explicit careers link establish the chain; posting metadata is checked later
by the existing resolver. Positive careers evidence is reusable, not a cached
negative assertion about an employer. No database access or writes.
"""
from __future__ import annotations

import ipaddress
import json
import re
import time
from dataclasses import replace
from urllib.parse import urlencode, urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup

from src.enrichment.ats.base import (
    anchors_from_html, company_slug_candidates, filter_and_rank_candidates, walk_json,
)
from src.enrichment.job_identity import normalize_company_name

CAREERS_CACHE: dict[str, tuple[float, dict]] = {}
CACHE_TTL_SECONDS = 6 * 60 * 60


def public_https_url(url):
    try:
        p = urlsplit(url)
        host = (p.hostname or '').rstrip('.').lower()
        if (p.scheme != 'https' or not host or '.' not in host or p.username
                or p.password or p.port not in (None, 443)
                or host.endswith(('.local', '.localhost', '.internal'))):
            return False
        # Use the transport's IDNA parsing, then the existing ATS DNS-label rule.
        host = httpx.URL(url).raw_host.decode('ascii').rstrip('.').lower()
        if any(not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?', label)
               for label in host.split('.')):
            return False
        try:
            return ipaddress.ip_address(host).is_global
        except ValueError:
            return True
    except (ValueError, httpx.InvalidURL):
        return False


class PublicPages:
    """Explicit GET/redirect budget; cached responses cost no new requests."""
    def __init__(self, client, trace, *, limit=12, deadline=None):
        self.client, self.trace, self.limit, self.deadline = client, trace, limit, deadline
        self.attempts = 0
        self.cache = getattr(client, '_employer_public_pages', {})
        client._employer_public_pages = self.cache
        self.failures = getattr(client, '_employer_failed_probes', {})
        client._employer_failed_probes = self.failures

    def get(self, url):
        for _ in range(3):
            if not public_https_url(url):
                self.trace.append(dict(url=url, reason='unsafe_public_url'))
                return None
            if url in self.cache:
                response = self.cache[url]
            else:
                for key in list(self.failures):
                    if self.failures[key] <= time.monotonic():
                        del self.failures[key]
                if url in self.failures:
                    self.trace.append(dict(url=url, reason='recent_exact_url_connection_failure_deferred'))
                    return None
                if self.attempts >= self.limit or (self.deadline and time.monotonic() >= self.deadline):
                    self.trace.append(dict(url=url, reason='public_request_budget_exhausted'))
                    return None
                self.attempts += 1
                event = dict(url=url, method='GET')
                self.trace.append(event)
                try:
                    response = self.client.get(url, timeout=8, follow_redirects=False)
                    event['http_status'] = response.status_code
                except (httpx.RequestError, httpx.InvalidURL) as error:
                    event['reason'] = type(error).__name__
                    if isinstance(error, httpx.ConnectError):
                        if len(self.failures) >= 64:
                            self.failures.pop(next(iter(self.failures)))
                        self.failures[url] = time.monotonic() + 15
                    return None
                if len(self.cache) >= 128:
                    self.cache.pop(next(iter(self.cache)))
                self.cache[url] = response
            if response.status_code in (301, 302, 303, 307, 308):
                url = urljoin(url, response.headers.get('location', ''))
                continue
            return response if response.status_code == 200 else None
        return None


def corporate_roots(company):
    tokens = normalize_company_name(company).split()
    slugs = company_slug_candidates(company)[:1]
    # A shortened two-word brand is a probe, subject to the same fetched proof.
    if len(tokens) > 2:
        slugs.append(''.join(tokens[:2]))
    roots = ['https://' + s + '.com/' for s in dict.fromkeys(slugs)]
    return [url for url in roots if public_https_url(url)]


# These are search-result intermediaries, not an employer's corporate home.
# Check hostname label boundaries; similarly named corporate sites remain eligible.
NON_EMPLOYER_LEAD_DOMAINS = frozenset({
    'indeed.com', 'talent.com', 'jobgether.com', 'tavily.com',
})


def excluded_corporate_lead(url):
    from src.enrichment.official_job_resolver import is_aggregator_domain
    try:
        host = (urlsplit(url).hostname or '').casefold()
    except (ValueError, TypeError):
        return True
    return (is_aggregator_domain(url) or any(
        host == domain or host.endswith('.' + domain)
        for domain in NON_EMPLOYER_LEAD_DOMAINS
    ))


# Generic leading words never identify an employer's domain on their own.
PARENT_PROBE_STOP_WORDS = frozenset({
    'a', 'an', 'the', 'and', 'of', 'at', 'for', 'in', 'on', 'by', 'to', 'with',
})


def parent_probe_token(company):
    """First significant company token (>= 3 chars), or None: no probe."""
    tokens = [t for t in normalize_company_name(company).split() if t not in PARENT_PROBE_STOP_WORDS]
    return tokens[0] if tokens and len(tokens[0]) >= 3 else None


def parent_roots_from_careers_leads(company, leads):
    """Generate bounded, untrusted apex probes from company-token subdomains.

    A search hit never establishes ownership. The derived site still needs
    independent JSON-LD corporate branding and a published careers link.
    The parent's first label must start with the first significant company
    token (``bask`` -> ``bask.health``, ``sevita`` -> ``sevitahealth.com``).
    """
    first = parent_probe_token(company)
    if not first:
        return []
    roots = []
    for lead in leads[:2]:
        if not public_https_url(lead) or excluded_corporate_lead(lead):
            continue
        labels = (urlsplit(lead).hostname or '').casefold().split('.')
        if len(labels) < 3:
            continue
        parent = '.'.join(labels[1:])
        if not labels[1].startswith(first):
            continue
        root = 'https://' + parent + '/'
        if public_https_url(root) and root not in roots:
            roots.append(root)
    return roots


def corporate_brand_matches(html, company, url):
    """Require full normalized company branding in fetched structured data.

    A domain token, arbitrary mention in text, or a JobPosting self-assertion
    cannot upgrade a guessed hostname into corporate provenance.
    """
    soup = BeautifulSoup(html, 'html.parser')
    expected = normalize_company_name(company)
    host = urlsplit(url).hostname
    for script in soup.find_all('script', type='application/ld+json'):
        try:
            nodes = walk_json(json.loads(script.get_text()))
            for node in nodes:
                if node.get('@type') not in ('Organization', 'Corporation', 'WebSite'):
                    continue
                if (normalize_company_name(node.get('legalName') or node.get('name')) == expected
                        and urlsplit(str(node.get('url') or '')).hostname == host):
                    return True
        except (ValueError, TypeError):
            continue
    return False


def corporate_brand_failure_reason(html, company, url):
    """Diagnostic category only; acceptance stays in corporate_brand_matches."""
    soup = BeautifulSoup(html, 'html.parser')
    expected = normalize_company_name(company)
    named = False
    organizations = False
    for script in soup.find_all('script', type='application/ld+json'):
        try:
            for node in walk_json(json.loads(script.get_text())):
                if node.get('@type') not in ('Organization', 'Corporation', 'WebSite'):
                    continue
                organizations = True
                if normalize_company_name(node.get('legalName') or node.get('name')) == expected:
                    named = True
        except (ValueError, TypeError):
            continue
    if not organizations:
        return 'organization_data_missing'
    return 'organization_host_mismatch' if named else 'organization_name_mismatch'


def platform_evidence(soup, url):
    from src.enrichment.official_job_resolver import ats_type_for_url
    names = set(filter(None, [ats_type_for_url(url)]))
    # Identify the published technology, not a guessed employer tenant.
    for el in soup.find_all(['a', 'script', 'iframe'], href=True) + soup.find_all(['script', 'iframe'], src=True):
        target = str(el.get('href') or el.get('src') or '')
        provider = ats_type_for_url(target)
        if provider:
            names.add(provider)
        host = urlsplit(urljoin(url, target)).hostname or ''
        for domain, name in [('avature.net', 'avature'), ('talentbrew.com', 'talentbrew')]:
            if host == domain or host.endswith('.' + domain):
                names.add(name)
    return sorted(names)


def inventory_search_urls(soup, base, title):
    """Use published GET keyword forms; never submit applicant/alert forms."""
    for form in soup.find_all('form'):
        if str(form.get('method', 'get')).lower() != 'get':
            continue
        action = urljoin(base, form.get('action') or base)
        if urlsplit(action).netloc != urlsplit(base).netloc:
            continue
        inputs = form.find_all('input', attrs={'name': True})
        keyword = next((i for i in inputs if i.get('type') == 'search'
                        or re.search(r'keyword|searchkeyword|searchtext', i.get('name', ''), re.I)), None)
        if keyword is None:
            continue
        values = {keyword['name']: title}
        values.update({i['name']: i.get('value', '') for i in inputs if i.get('type') == 'hidden'})
        yield action + ('&' if '?' in action else '?') + urlencode(values)


def remember_verified_careers(job):
    """Reuse already loaded, independently verified records; no DB lookup here."""
    from src.enrich_jobs import stored_verified_key_is_trusted
    from src.enrichment.official_job_resolver import ats_type_for_url
    from src.verified_posting_identity import verified_posting_key_from_url
    key = normalize_company_name(job.get('company_name'))
    url = str(job.get('official_job_url') or '')
    if (not key or job.get('official_url_status') != 'FOUND_VERIFIED'
            or not stored_verified_key_is_trusted(job)
            or normalize_company_name(job.get('official_resolved_company')) != key
            or not public_https_url(url)
            or job.get('verified_posting_key') != verified_posting_key_from_url(url)):
        return None
    parts = urlsplit(url)
    path = re.split(r'/(?:jobs?|positions?|job-detail)/', parts.path, maxsplit=1)[0] or '/'
    if ats_type_for_url(url) in ('lever', 'greenhouse', 'ashby', 'smartrecruiters', 'rippling'):
        path = '/' + parts.path.strip('/').split('/')[0]
    elif ats_type_for_url(url) in ('breezy', 'pinpoint'):
        path = '/'
    proof = dict(company=key, corporate_url=url,
                 careers_url=parts._replace(path=path, query='', fragment='').geturl(),
                 basis='previously_verified_employer',
                 platforms=list(filter(None, [ats_type_for_url(url)])))
    CAREERS_CACHE.setdefault(key, (time.monotonic(), proof))
    return proof


def discover_employer_candidates(job, client, *, deadline=None, corporate_leads=()):
    from src.enrichment.official_job_resolver import (
        ats_type_for_url, is_aggregator_domain, looks_like_individual_posting_url,
    )
    from src.enrichment.ats import workday, greenhouse, lever, ashby, smartrecruiters
    from src.verified_posting_identity import hosted_tenant_posting_scope, hosted_tenant_url_matches
    trace = job.setdefault('employer_discovery_trace', [])
    pages = PublicPages(client, trace, deadline=deadline)
    company = str(job.get('company_name') or '')
    key = normalize_company_name(company)
    if not key or not job.get('title'):
        return []
    cached = CAREERS_CACHE.get(key)
    proof = dict(cached[1]) if cached and time.monotonic() - cached[0] < CACHE_TTL_SECONDS else None
    if proof is None:
        # Existing trusted employer evidence may seed careers discovery. It is
        # not borrowed from another unverified record or a search result.
        proof = remember_verified_careers(job)
        if proof is None:
            # Up to four primary roots (search leads, then guessed .com roots)
            # plus up to two supplemental derived parents (one per lead). All
            # share this call's 12-GET PublicPages budget; redirects count.
            # The resolver makes at most two calls per job (24 counted GETs).
            # ATS discovery, public search and candidate fetches are bounded
            # by their own separate controls, not by this budget.
            # Discard known job aggregators *before* they occupy lead slots.
            leads = [lead for lead in corporate_leads
                     if public_https_url(lead) and not excluded_corporate_lead(lead)][:2]
            roots = list(dict.fromkeys([*leads, *corporate_roots(company)]))[:4]
            roots.extend(root for root in parent_roots_from_careers_leads(company, leads)
                         if root not in roots)
            for root in roots:
                # Every root source (lead, guessed .com, derived parent) must
                # pass the same guard before any request reaches the transport.
                if is_aggregator_domain(root) or not public_https_url(root):
                    continue
                response = pages.get(root)
                if response is None:
                    continue
                if not corporate_brand_matches(response.text, company, str(response.url)):
                    trace.append(dict(url=str(response.url), reason='corporate_brand_unconfirmed',
                                      detail=corporate_brand_failure_reason(
                                          response.text, company, str(response.url))))
                    continue
                trace.append(dict(url=str(response.url), reason='corporate_brand_confirmed'))
                soup = BeautifulSoup(response.text, 'html.parser')
                for a in soup.find_all('a', href=True):
                    if not re.search(r'\b(careers?|jobs?|join (?:our|the) team)\b', a.get_text(' ', strip=True), re.I):
                        continue
                    target = urljoin(str(response.url), a['href'])
                    if not public_https_url(target) or is_aggregator_domain(target):
                        continue
                    proof = dict(company=key, corporate_url=str(response.url), careers_url=target,
                                 basis='fetched_corporate_brand_and_published_careers_link')
                    trace.append(dict(url=target, reason='published_careers_link_found'))
                    break
                if proof:
                    break
                trace.append(dict(url=str(response.url), reason='published_careers_link_missing'))
    if proof is None:
        return []
    queue = [proof['careers_url']]
    visited, candidates = set(), []
    while queue and len(visited) < 5:
        url = queue.pop(0)
        if url in visited:
            continue
        visited.add(url)
        response = pages.get(url)
        if response is None:
            trace.append(dict(url=url, reason='careers_inventory_unavailable'))
            continue
        final = str(response.url)
        # Careers redirects may lead to an ATS, but never an aggregator.
        if is_aggregator_domain(final):
            continue
        soup = BeautifulSoup(response.text, 'html.parser')
        # A published external careers link is not permission to trust an
        # arbitrary redirected landing page. Require employer branding or a
        # link back to the corporate site in that fetched inventory.
        corporate_host = urlsplit(proof['corporate_url']).hostname
        branded = key in normalize_company_name(soup.title.get_text() if soup.title else '')
        backlink = any(urlsplit(urljoin(final, a['href'])).hostname == corporate_host
                       for a in soup.find_all('a', href=True))
        def site_path(value):
            path = urlsplit(value).path.strip('/').split('/')
            return path[1:] if path and re.fullmatch(r'[a-z]{2}-[a-z]{2}', path[0], re.I) else path
        known_tenant = (ats_type_for_url(final) and urlsplit(final).hostname == urlsplit(proof['careers_url']).hostname
                        and site_path(final)[:1] == site_path(proof['careers_url'])[:1])
        if not branded and not backlink and not known_tenant:
            trace.append(dict(url=final, reason='careers_employer_relationship_unconfirmed'))
            continue
        CAREERS_CACHE[key] = (time.monotonic(), dict(proof))
        proof['platforms'] = sorted(set(proof.get('platforms', [])) | set(platform_evidence(soup, final)))
        # Retain positive employer/platform evidence even when this title is
        # absent. It is not a cached negative assertion about job inventory.
        CAREERS_CACHE[key] = (time.monotonic(), dict(proof))
        found = anchors_from_html(response.text, final, 'employer', company)
        provider = ats_type_for_url(final)
        # Reuse existing tenant payload parsers rather than a second JD parser.
        if provider == 'workday':
            found += workday.parse_workday_jobs(workday.parse_embedded_json_payloads(response.text), final, company)
            if not found:
                found += workday.discover_from_search_homepage(job, client, final)
        elif provider in ('greenhouse', 'lever', 'ashby', 'smartrecruiters'):
            slug = urlsplit(final).path.strip('/').split('/')[0]
            endpoints = {
                'greenhouse': f'https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true',
                'lever': f'https://api.lever.co/v0/postings/{slug}?mode=json',
                'ashby': f'https://api.ashbyhq.com/posting-api/job-board/{slug}',
                'smartrecruiters': f'https://api.smartrecruiters.com/v1/companies/{slug}/postings',
            }
            if not found:
                api = pages.get(endpoints[provider])
                if api is not None:
                    try:
                        payload = api.json()
                        if provider == 'lever':
                            found += lever.parse_lever_jobs(payload, company)
                        else:
                            parser = {'greenhouse': greenhouse.parse_greenhouse_jobs,
                                      'ashby': ashby.parse_ashby_jobs,
                                      'smartrecruiters': smartrecruiters.parse_smartrecruiters_jobs}[provider]
                            found += parser(payload, slug, company)
                    except ValueError:
                        pass
        ranked = filter_and_rank_candidates(job, found, max_results=5)
        trace.append(dict(url=final, reason='careers_inventory_candidates',
                          candidates_found=len(ranked)))
        for candidate in ranked:
            # An arbitrary external link must not inherit employer provenance.
            same_host = urlsplit(candidate.url).hostname == urlsplit(final).hostname
            # Existing ATS APIs may return another provider-owned hostname,
            # but only the explicitly discovered tenant may inherit the chain.
            cp = urlsplit(candidate.url)
            tenant_path = cp.path.strip('/').split('/')[0]
            api_tenant_bound = bool(provider in ('greenhouse', 'lever', 'ashby', 'smartrecruiters')
                                    and ats_type_for_url(candidate.url) == provider and tenant_path == slug)
            if (not public_https_url(candidate.url) or is_aggregator_domain(candidate.url)
                    or not looks_like_individual_posting_url(candidate.url) or not (same_host or api_tenant_bound)):
                continue
            # Rippling tenants share one host: inherit provenance only within the
            # discovered tenant's own inventory (Pinpoint: same host already).
            hosted = hosted_tenant_posting_scope(candidate.url)
            if hosted and not hosted_tenant_url_matches(final, hosted[0], hosted[1]):
                continue
            evidence = dict(proof, inventory_url=final, posting_url=candidate.url)
            candidates.append(replace(candidate, source='employer', employer_provenance=evidence))
        if candidates:
            break
        queue.extend(inventory_search_urls(soup, final, str(job['title'])))
        for a in soup.find_all('a', href=True):
            target = urljoin(final, a['href'])
            if target in visited or not public_https_url(target) or is_aggregator_domain(target):
                continue
            label = a.get_text(' ', strip=True)
            if re.search(r'view all jobs|search jobs|open positions|job openings', label, re.I):
                if urlsplit(target).hostname == urlsplit(final).hostname or ats_type_for_url(target):
                    queue.append(target)
    if candidates:
        CAREERS_CACHE[key] = (time.monotonic(), dict(proof))
        job['employer_careers_provenance'] = proof
    return list({c.url: c for c in candidates}.values())
