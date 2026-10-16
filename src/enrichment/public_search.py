"""Discovery-only public search. Never promote snippets to posting identity."""
from dataclasses import dataclass
import os
import httpx

SERPER_URL = "https://google.serper.dev/search"
TAVILY_URL = "https://api.tavily.com/search"

class PublicSearchError(RuntimeError):
    def __init__(self, provider, category):
        self.provider = provider
        self.category = category
        self.stop_provider = category in {"auth_configuration", "quota_exhausted"}
        # Never include response bodies, request headers or underlying exceptions.
        super().__init__(f"{provider} public search: {category}")

@dataclass(frozen=True)
class SearchResponse:
    urls: tuple[str, ...]
    result_count: int
    # Audit-only metadata, never passed to posting identity validation.
    untrusted_results: tuple[dict, ...] = ()


def available_providers():
    """Tavily first, Serper only as fallback; no automatic DDG scraping."""
    if os.environ.get("PUBLIC_SEARCH_PROVIDER", "").strip().lower() == "duckduckgo":
        # Explicit legacy diagnostic opt-in only; never automatic.
        return ("duckduckgo",)
    return tuple(
        provider for provider, name in (
            ("tavily", "TAVILY_API_KEY"), ("serper", "SERPER_API_KEY")
        ) if os.environ.get(name, "").strip()
    )


def selected_provider():
    return next(iter(available_providers()), "none")


def provider_metrics(client):
    if not hasattr(client, "public_search_metrics"):
        client.public_search_metrics = {}
    return client.public_search_metrics


def metric(client, provider, name):
    counters = provider_metrics(client).setdefault(provider, {name: 0 for name in (
        "search_requests", "successful_responses", "no_result_searches",
        "auth_configuration", "quota_exhausted", "rate_limit", "transient_error",
        "verified_official_resolutions", "descriptions_recovered")})
    counters[name] = counters.get(name, 0) + 1


def serper_search(query, client):
    key = os.environ.get("SERPER_API_KEY", "").strip()
    if not key:
        metric(client, "serper", "auth_configuration")
        raise PublicSearchError("serper", "auth_configuration")
    metric(client, "serper", "search_requests")
    try:
        # Request-scoped header; never set it on the shared ATS/source client.
        # Disable redirects so credentials cannot travel to another host.
        response = client.post(SERPER_URL, headers={"X-API-KEY": key},
                               json={"q": query, "num": 10}, follow_redirects=False)
    except httpx.RequestError:
        metric(client, "serper", "transient_error")
        raise PublicSearchError("serper", "transient_error") from None
    try:
        payload = response.json()
    except ValueError:
        payload = None
    # Use messages only for classification, never persist or echo provider bodies.
    message = str(payload.get("message", "") if isinstance(payload, dict) else "").lower()
    if response.status_code in {401, 403}:
        category = "quota_exhausted" if any(t in message for t in ("credit", "quota", "balance")) else "auth_configuration"
    elif response.status_code == 402 or (response.status_code >= 400 and any(t in message for t in ("credit", "quota", "balance"))):
        category = "quota_exhausted"
    elif response.status_code == 429:
        category = "rate_limit"
    elif response.status_code >= 500 or response.status_code >= 300:
        category = "transient_error" if response.status_code >= 500 or response.status_code < 400 else "auth_configuration"
    elif not isinstance(payload, dict) or not isinstance(payload.get("organic"), list):
        category = "transient_error"
    else:
        category = None
    if category:
        metric(client, "serper", category)
        raise PublicSearchError("serper", category)
    metric(client, "serper", "successful_responses")
    organic = payload["organic"]
    if not organic:
        metric(client, "serper", "no_result_searches")
    # Intentionally discard title, snippet, company and other identity hints.
    urls = tuple(dict.fromkeys(row["link"] for row in organic
                             if isinstance(row, dict) and isinstance(row.get("link"), str)))
    audit = tuple({"url": row.get("link", ""), "title": row.get("title", "")}
                  for row in organic if isinstance(row, dict))
    return SearchResponse(urls, len(organic), audit)

def tavily_search(query, client):
    """One basic Tavily search (=1 credit); discovery URLs only."""
    key = os.environ.get("TAVILY_API_KEY", "").strip()
    if not key:
        metric(client, "tavily", "auth_configuration")
        raise PublicSearchError("tavily", "auth_configuration")
    metric(client, "tavily", "search_requests")
    try:
        response = client.post(
            TAVILY_URL,
            headers={"Authorization": f"Bearer {key}"},
            json={
                "query": query, "search_depth": "basic",
                "max_results": 10, "topic": "general",
                "include_answer": False, "include_raw_content": False,
                "auto_parameters": False,
            },
            follow_redirects=False,
        )
    except httpx.RequestError:
        metric(client, "tavily", "transient_error")
        raise PublicSearchError("tavily", "transient_error") from None

    if response.status_code in {402, 432, 433}:
        category = "quota_exhausted"
    elif response.status_code in {401, 403, 400, 422}:
        category = "auth_configuration"
    elif response.status_code == 429:
        category = "rate_limit"
    elif response.status_code >= 500 or 300 <= response.status_code < 400:
        category = "transient_error"
    elif response.status_code >= 400:
        category = "auth_configuration"
    else:
        category = None
    if category:
        metric(client, "tavily", category)
        raise PublicSearchError("tavily", category)

    try:
        payload = response.json()
    except ValueError:
        payload = None
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        metric(client, "tavily", "transient_error")
        raise PublicSearchError("tavily", "transient_error")

    metric(client, "tavily", "successful_responses")
    hits = payload["results"]
    if not hits:
        metric(client, "tavily", "no_result_searches")
    urls = tuple(dict.fromkeys(
        item["url"] for item in hits
        if isinstance(item, dict) and isinstance(item.get("url"), str)
    ))
    audit = tuple(
        {"url": item.get("url", ""), "title": item.get("title", "")}
        for item in hits if isinstance(item, dict)
    )
    return SearchResponse(urls, len(hits), audit)
