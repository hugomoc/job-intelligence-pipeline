from __future__ import annotations

import json
from urllib.parse import quote_plus

import httpx

from src.enrichment import official_job_resolver
from src.enrichment.ats import (
    ATS_DISCOVERY_CACHE,
    ashby,
    greenhouse,
    icims,
    lever,
    smartrecruiters,
    workday,
)
from src.enrichment.ats.base import (
    MAX_ATS_RESULTS,
    OfficialJobCandidate,
)
from src.enrichment.official_job_resolver import (
    OFFICIAL_AMBIGUOUS,
    OFFICIAL_FOUND_VERIFIED,
    OFFICIAL_NOT_FOUND,
    resolve_official_job,
)
from src.test_official_job_resolver import (
    DESCRIPTION,
    job_posting_html,
)
from src.ui.job_links import select_job_open_target


def mock_client(
    routes: dict[str, httpx.Response],
    fail_on_search: bool = False,
) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)

        if "duckduckgo.com/html" in url:
            if fail_on_search:
                raise AssertionError("generic search should not be called")
            url = "search"

        response = routes.get(url)

        if response is None:
            return httpx.Response(404, request=request)

        response.request = request
        return response

    return httpx.Client(
        transport=httpx.MockTransport(handler),
        follow_redirects=True,
    )


def analytics_job() -> dict:
    return {
        "record_key": "job-1",
        "source": "lensa",
        "title": "Senior Analytics Engineer",
        "company_name": "Seismic",
        "location": "Remote",
        "apply_url": "https://lensa.com/generic-alert",
    }


def acme_job(title: str = "Senior Data Engineer") -> dict:
    return {
        "record_key": "job-2",
        "source": "lensa",
        "title": title,
        "company_name": "Acme Analytics",
        "location": "Remote",
        "apply_url": "https://lensa.com/generic-alert",
    }


def test_greenhouse_finds_matching_job_and_ignores_unrelated() -> None:
    url = "https://boards-api.greenhouse.io/v1/boards/acmeanalytics/jobs?content=true"
    payload = {
        "jobs": [
            {
                "id": 1,
                "title": "Senior Data Engineer",
                "absolute_url": "https://boards.greenhouse.io/acmeanalytics/jobs/1",
                "location": {
                    "name": "Remote",
                },
            },
            {
                "id": 2,
                "title": "Java Developer",
                "absolute_url": "https://boards.greenhouse.io/acmeanalytics/jobs/2",
                "location": {
                    "name": "Remote",
                },
            },
        ]
    }

    with mock_client({url: httpx.Response(200, json=payload)}) as client:
        candidates = greenhouse.discover_candidates(acme_job(), client)

    assert len(candidates) == 1
    assert candidates[0].title == "Senior Data Engineer"
    assert candidates[0].location == "Remote"
    assert candidates[0].url == "https://boards.greenhouse.io/acmeanalytics/jobs/1"


def test_lever_finds_matching_job_and_rejects_wrong_title() -> None:
    url = "https://api.lever.co/v0/postings/acmeanalytics?mode=json"
    payload = [
        {
            "id": "lever-1",
            "text": "Senior Data Engineer",
            "hostedUrl": "https://jobs.lever.co/acmeanalytics/lever-1",
            "categories": {
                "location": "Remote",
            },
        },
        {
            "id": "lever-2",
            "text": "Frontend Engineer",
            "hostedUrl": "https://jobs.lever.co/acmeanalytics/lever-2",
            "categories": {
                "location": "Remote",
            },
        },
    ]

    with mock_client({url: httpx.Response(200, json=payload)}) as client:
        candidates = lever.discover_candidates(acme_job(), client)

    assert [candidate.title for candidate in candidates] == ["Senior Data Engineer"]


def test_ashby_parses_job_listing_data_and_finds_target() -> None:
    url = "https://api.ashbyhq.com/posting-api/job-board/acmeanalytics"
    payload = {
        "jobs": [
            {
                "id": "ashby-1",
                "title": "Senior Data Engineer",
                "locationName": "Remote",
            }
        ]
    }

    with mock_client({url: httpx.Response(200, json=payload)}) as client:
        candidates = ashby.discover_candidates(acme_job(), client)

    assert len(candidates) == 1
    assert candidates[0].source == "ashby"
    assert candidates[0].url == "https://jobs.ashbyhq.com/acmeanalytics/ashby-1"


def test_smartrecruiters_finds_target_job() -> None:
    url = "https://api.smartrecruiters.com/v1/companies/acmeanalytics/postings"
    payload = {
        "content": [
            {
                "id": "smart-1",
                "name": "Senior Data Engineer",
                "location": {
                    "city": "Remote",
                    "country": "US",
                },
            }
        ]
    }

    with mock_client({url: httpx.Response(200, json=payload)}) as client:
        candidates = smartrecruiters.discover_candidates(acme_job(), client)

    assert len(candidates) == 1
    assert candidates[0].source == "smartrecruiters"
    assert candidates[0].url == (
        "https://jobs.smartrecruiters.com/acmeanalytics/smart-1"
    )


def test_icims_parses_listing_and_supports_dynamic_description_fallback() -> None:
    title = "Senior Analytics Engineer"
    search_url = (
        "https://careers-seismic.icims.com/jobs/search"
        f"?ss=1&searchKeyword={quote_plus(title)}"
    )
    detail_url = "https://careers-seismic.icims.com/jobs/123/senior-analytics-engineer"
    listing_html = (
        "<html><body>"
        f"<a href='{detail_url}'>Senior Analytics Engineer</a>"
        "</body></html>"
    )
    original_dynamic_fetch = official_job_resolver.fetch_dynamic_job_description

    def fake_dynamic_fetch(url: str):
        assert url == detail_url

        from src.enrichment.job_description import JobDescriptionResult

        return JobDescriptionResult(
            requested_url=url,
            final_url=url,
            status="enriched",
            http_status=200,
            extraction_method="playwright_json_ld",
            description=DESCRIPTION,
            word_count=len(DESCRIPTION.split()),
            error_message=None,
            resolved_title="Senior Analytics Engineer",
            resolved_company="Seismic",
            resolved_location="Remote",
        )

    ATS_DISCOVERY_CACHE.clear()
    official_job_resolver.fetch_dynamic_job_description = fake_dynamic_fetch

    try:
        with mock_client(
            {
                search_url: httpx.Response(200, text=listing_html),
                detail_url: httpx.Response(
                    200,
                    text=job_posting_html(
                        "Senior Analytics Engineer",
                        "Seismic",
                        description="short",
                    ),
                    headers={"content-type": "text/html"},
                ),
            },
            fail_on_search=True,
        ) as client:
            resolution = resolve_official_job(analytics_job(), client)
    finally:
        official_job_resolver.fetch_dynamic_job_description = original_dynamic_fetch
        ATS_DISCOVERY_CACHE.clear()

    assert resolution.status == OFFICIAL_FOUND_VERIFIED
    assert resolution.official_job_url == detail_url
    assert resolution.official_url_source == "icims"


def test_workday_parses_mock_search_results_and_limits_results() -> None:
    url = "https://acmeanalytics.wd1.myworkdaysite.com/en-US/acmeanalytics/jobs"
    payload = {
        "jobPostings": [
            {
                "title": f"Senior Data Engineer {index}",
                "externalPath": f"/en-US/acmeanalytics/job/{index}",
                "locationsText": "Remote",
                "jobPostingId": f"WD-{index}",
            }
            for index in range(MAX_ATS_RESULTS + 5)
        ]
    }

    with mock_client({url: httpx.Response(200, json=payload)}) as client:
        candidates = workday.discover_candidates(acme_job(), client)

    assert len(candidates) == MAX_ATS_RESULTS
    assert candidates[0].source == "workday"


def test_resolver_uses_ats_before_generic_search() -> None:
    title = "Senior Analytics Engineer"
    search_url = (
        "https://careers-seismic.icims.com/jobs/search"
        f"?ss=1&searchKeyword={quote_plus(title)}"
    )
    detail_url = "https://careers-seismic.icims.com/jobs/123/senior-analytics-engineer"
    listing_html = (
        "<html><body>"
        f"<a href='{detail_url}'>Senior Analytics Engineer</a>"
        "</body></html>"
    )

    ATS_DISCOVERY_CACHE.clear()

    try:
        with mock_client(
            {
                search_url: httpx.Response(200, text=listing_html),
                detail_url: httpx.Response(
                    200,
                    text=job_posting_html(
                        "Senior Analytics Engineer",
                        "Seismic",
                    ),
                    headers={"content-type": "text/html"},
                ),
            },
            fail_on_search=True,
        ) as client:
            resolution = resolve_official_job(analytics_job(), client)
    finally:
        ATS_DISCOVERY_CACHE.clear()

    assert resolution.status == OFFICIAL_FOUND_VERIFIED
    assert resolution.official_url_source == "icims"


def test_resolver_uses_ats_listing_metadata_when_detail_lacks_identity() -> None:
    original_ats = official_job_resolver.discover_ats_candidates
    detail_url = "https://job-boards.greenhouse.io/monzo/jobs/8013699"
    description_html = (
        "<html><body><main>"
        f"{DESCRIPTION}"
        "</main></body></html>"
    )

    def greenhouse_candidate(job: dict, client: httpx.Client):
        return [
            OfficialJobCandidate(
                url=detail_url,
                label="Staff Analytics Engineer | Monzo | Cardiff, London or Remote (UK)",
                source="greenhouse",
                title="Staff Analytics Engineer",
                company="Monzo",
                location="Cardiff, London or Remote (UK)",
                job_id="8013699",
            )
        ]

    official_job_resolver.discover_ats_candidates = greenhouse_candidate

    try:
        with mock_client(
            {
                detail_url: httpx.Response(
                    200,
                    text=description_html,
                    headers={"content-type": "text/html"},
                ),
            },
            fail_on_search=True,
        ) as client:
            resolution = resolve_official_job(
                {
                    "source": "lensa",
                    "title": "Staff Analytics Engineer",
                    "company_name": "Monzo",
                    "location": "Remote",
                    "apply_url": "https://lensa.com/generic-alert",
                },
                client,
            )
    finally:
        official_job_resolver.discover_ats_candidates = original_ats

    assert resolution.status == OFFICIAL_FOUND_VERIFIED
    assert resolution.official_job_url == detail_url
    assert resolution.official_url_source == "greenhouse"
    assert resolution.resolved_title == "Staff Analytics Engineer"
    assert resolution.resolved_company == "Monzo"


def test_resolver_falls_back_to_generic_when_ats_has_no_match() -> None:
    original_ats = official_job_resolver.discover_ats_candidates
    generic_url = "https://boards.greenhouse.io/seismic/jobs/123"

    def no_ats_candidates(job: dict, client: httpx.Client):
        return []

    official_job_resolver.discover_ats_candidates = no_ats_candidates

    try:
        with mock_client(
            {
                "search": httpx.Response(
                    200,
                    text=(
                        "<html><body>"
                        "<a href='/l/?uddg="
                        f"{quote_plus(generic_url)}"
                        "'>Senior Analytics Engineer - Seismic Careers</a>"
                        "</body></html>"
                    ),
                ),
                generic_url: httpx.Response(
                    200,
                    text=job_posting_html(
                        "Senior Analytics Engineer",
                        "Seismic",
                    ),
                    headers={"content-type": "text/html"},
                ),
            }
        ) as client:
            resolution = resolve_official_job(analytics_job(), client)
    finally:
        official_job_resolver.discover_ats_candidates = original_ats

    assert resolution.status == OFFICIAL_FOUND_VERIFIED
    assert resolution.official_job_url == generic_url


def test_resolver_continues_when_ats_adapter_errors() -> None:
    original_ats = official_job_resolver.discover_ats_candidates
    generic_url = "https://boards.greenhouse.io/seismic/jobs/123"

    def broken_ats_candidates(job: dict, client: httpx.Client):
        raise RuntimeError("adapter failed")

    official_job_resolver.discover_ats_candidates = broken_ats_candidates

    try:
        with mock_client(
            {
                "search": httpx.Response(
                    200,
                    text=(
                        "<html><body>"
                        "<a href='/l/?uddg="
                        f"{quote_plus(generic_url)}"
                        "'>Senior Analytics Engineer - Seismic Careers</a>"
                        "</body></html>"
                    ),
                ),
                generic_url: httpx.Response(
                    200,
                    text=job_posting_html(
                        "Senior Analytics Engineer",
                        "Seismic",
                    ),
                    headers={"content-type": "text/html"},
                ),
            }
        ) as client:
            resolution = resolve_official_job(analytics_job(), client)
    finally:
        official_job_resolver.discover_ats_candidates = original_ats

    assert resolution.status == OFFICIAL_FOUND_VERIFIED
    assert resolution.official_job_url == generic_url


def test_multiple_ats_results_can_be_ambiguous() -> None:
    original_ats = official_job_resolver.discover_ats_candidates
    first_url = "https://jobs.lever.co/seismic/1"
    second_url = "https://jobs.lever.co/seismic/2"

    def ambiguous_ats_candidates(job: dict, client: httpx.Client):
        return [
            OfficialJobCandidate(
                url=first_url,
                label="Senior Analytics Engineer | Seismic",
                source="lever",
                title="Senior Analytics Engineer",
                company="Seismic",
            ),
            OfficialJobCandidate(
                url=second_url,
                label="Senior Analytics Engineer | Seismic",
                source="lever",
                title="Senior Analytics Engineer",
                company="Seismic",
            ),
        ]

    official_job_resolver.discover_ats_candidates = ambiguous_ats_candidates

    try:
        with mock_client(
            {
                first_url: httpx.Response(
                    200,
                    text=job_posting_html(
                        "Senior Analytics Engineer",
                        "Seismic",
                    ),
                    headers={"content-type": "text/html"},
                ),
                second_url: httpx.Response(
                    200,
                    text=job_posting_html(
                        "Senior Analytics Engineer",
                        "Seismic",
                    ),
                    headers={"content-type": "text/html"},
                ),
            },
            fail_on_search=True,
        ) as client:
            resolution = resolve_official_job(analytics_job(), client)
    finally:
        official_job_resolver.discover_ats_candidates = original_ats

    assert resolution.status == OFFICIAL_AMBIGUOUS
    assert resolution.official_job_url is None


def test_ats_candidate_failing_identity_is_never_stored() -> None:
    original_ats = official_job_resolver.discover_ats_candidates
    wrong_url = "https://jobs.lever.co/seismic/wrong"

    def wrong_ats_candidate(job: dict, client: httpx.Client):
        return [
            OfficialJobCandidate(
                url=wrong_url,
                label="Senior Analytics Engineer | Seismic",
                source="lever",
                title="Senior Analytics Engineer",
                company="Seismic",
            )
        ]

    official_job_resolver.discover_ats_candidates = wrong_ats_candidate

    try:
        with mock_client(
            {
                wrong_url: httpx.Response(
                    200,
                    text=job_posting_html(
                        "Software Engineer",
                        "Other Company",
                    ),
                    headers={"content-type": "text/html"},
                ),
                "search": httpx.Response(200, text="<html></html>"),
            }
        ) as client:
            resolution = resolve_official_job(analytics_job(), client)
    finally:
        official_job_resolver.discover_ats_candidates = original_ats

    assert resolution.status == OFFICIAL_NOT_FOUND
    assert resolution.official_job_url is None


def test_seismic_icims_fixture_prefers_official_url_in_ui() -> None:
    title = "Senior Analytics Engineer"
    search_url = (
        "https://careers-seismic.icims.com/jobs/search"
        f"?ss=1&searchKeyword={quote_plus(title)}"
    )
    detail_url = "https://careers-seismic.icims.com/jobs/456/senior-analytics-engineer"
    listing_html = (
        "<html><body>"
        f"<div data-job-title='{title}'>"
        f"<a href='{detail_url}'>{title}</a>"
        "<span>Remote</span>"
        "</div>"
        "</body></html>"
    )

    ATS_DISCOVERY_CACHE.clear()

    try:
        with mock_client(
            {
                search_url: httpx.Response(200, text=listing_html),
                detail_url: httpx.Response(
                    200,
                    text=job_posting_html(
                        title,
                        "Seismic",
                        "Remote",
                    ),
                    headers={"content-type": "text/html"},
                ),
            },
            fail_on_search=True,
        ) as client:
            resolution = resolve_official_job(analytics_job(), client)
    finally:
        ATS_DISCOVERY_CACHE.clear()

    target = select_job_open_target(
        {
            "apply_url": "https://lensa.com/generic-alert",
            "official_url_status": resolution.status,
            "official_job_url": resolution.official_job_url,
        }
    )

    assert resolution.status == OFFICIAL_FOUND_VERIFIED
    assert resolution.official_job_url == detail_url
    assert resolution.official_url_source == "icims"
    assert target.label == "Open official job"
    assert target.url == detail_url


def main() -> None:
    test_greenhouse_finds_matching_job_and_ignores_unrelated()
    test_lever_finds_matching_job_and_rejects_wrong_title()
    test_ashby_parses_job_listing_data_and_finds_target()
    test_smartrecruiters_finds_target_job()
    test_icims_parses_listing_and_supports_dynamic_description_fallback()
    test_workday_parses_mock_search_results_and_limits_results()
    test_resolver_uses_ats_before_generic_search()
    test_resolver_uses_ats_listing_metadata_when_detail_lacks_identity()
    test_resolver_falls_back_to_generic_when_ats_has_no_match()
    test_resolver_continues_when_ats_adapter_errors()
    test_multiple_ats_results_can_be_ambiguous()
    test_ats_candidate_failing_identity_is_never_stored()
    test_seismic_icims_fixture_prefers_official_url_in_ui()
    print("ATS adapter tests passed.")


if __name__ == "__main__":
    main()
