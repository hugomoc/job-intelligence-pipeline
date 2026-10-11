"""Shared classification of evidenced third-party job boards.

Domain label boundaries avoid blocking unrelated lookalike hosts. Built In's
primary domain is used by the existing parser; builtinsf.com is confirmed in
local historical verification. Other locale domains are not assumed.
"""
from urllib.parse import urlsplit

BUILT_IN_DOMAINS = frozenset({"builtin.com", "builtinsf.com"})
THIRD_PARTY_DOMAINS = frozenset({
    "bebee.com", "glassdoor.com", "jobleads.com", "joblookup.com",
    "jobright.ai", "lensa.com",
}) | BUILT_IN_DOMAINS

def is_domain_in(url, domains):
    try: host = (urlsplit(str(url or "")).hostname or "").casefold()
    except ValueError: return False
    return any(host == domain or host.endswith("." + domain) for domain in domains)

def is_third_party_url(url):
    return is_domain_in(url, THIRD_PARTY_DOMAINS)

def posting_key_can_be_trusted(key):
    key = str(key or "")
    return bool(key) and not (key.startswith("official-url:") and
        is_third_party_url(key[len("official-url:"):]))
