from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import quote

import httpx

from src import database
from src.enrich_jobs import (
    initialize_enrichment_tables,
    save_enrichment_attempt,
)
from src.enrichment import official_job_resolver as official_resolver
from src.enrichment.job_description import JobDescriptionResult
from src.enrichment.official_job_resolver import (
    OFFICIAL_AMBIGUOUS,
    OFFICIAL_BLOCKED,
    OFFICIAL_FOUND_VERIFIED,
    OFFICIAL_NOT_FOUND,
    OfficialJobResolutionResult,
    resolve_official_job,
    should_attempt_official_resolution,
)
from src.enrichment.job_identity import JobIdentityValidation
from src.ui.job_links import select_job_open_target


DESCRIPTION = " ".join(
    [
        "Responsibilities include building analytics models, SQL pipelines,"
        "dbt transformations, and stakeholder-ready data products."
    ]
    * 12
)


def job_posting_html(
    title: str,
    company: str,
    location: str = "Remote",
    description: str = DESCRIPTION,
) -> str:
    payload = {
        "@context": "https://schema.org",
        "@type": "JobPosting",
        "title": title,
        "hiringOrganization": {
            "@type": "Organization",
            "name": company,
        },
        "jobLocation": {
            "@type": "Place",
            "address": {
                "addressLocality": location,
                "addressCountry": "US",
            },
        },
        "description": description,
    }

    return (
        "<html><head>"
        "<script type='application/ld+json'>"
        f"{json.dumps(payload)}"
        "</script></head><body></body></html>"
    )


def search_html(*urls: str) -> str:
    anchors = []

    for url in urls:
        anchors.append(
            "<a class='result__a' "
            f"href='/l/?uddg={quote(url, safe='')}'>"
            "Senior Analytics Engineer - Seismic Careers"
            "</a>"
        )

    return "<html><body>" + "\n".join(anchors) + "</body></html>"


def mock_client(routes: dict[str, httpx.Response]) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        key = str(request.url)

        if "duckduckgo.com/html" in key:
            key = "search"

        response = routes.get(key)

        if response is None:
            return httpx.Response(404, request=request)

        response.request = request
        return response

    return httpx.Client(
        transport=httpx.MockTransport(handler),
        follow_redirects=True,
    )


def lensa_job() -> dict:
    return {
        "record_key": "lensa-seismic",
        "source": "lensa",
        "title": "Senior Analytics Engineer",
        "company_name": "Seismic",
        "location": "Remote",
        "apply_url": "https://lensa.com/generic-alert",
        "current_word_count": 0,
        "attempt_count": 0,
    }


def generic_lensa_result() -> JobDescriptionResult:
    return JobDescriptionResult(
        requested_url="https://lensa.com/generic-alert",
        final_url="https://lensa.com/search?q=data",
        status="no_description",
        http_status=200,
        extraction_method=None,
        description="",
        word_count=0,
        error_message="No sufficiently complete job description was found.",
    )


def accepted_validation() -> JobIdentityValidation:
    return JobIdentityValidation(
        accepted=True,
        confidence=1.0,
        title_similarity=1.0,
        company_similarity=1.0,
        occupation_match=True,
        reason="identity accepted",
    )


def test_lensa_generic_page_falls_back_to_official_job() -> None:
    official_url = "https://boards.greenhouse.io/seismic/jobs/123"

    with mock_client(
        {
            "search": httpx.Response(200, text=search_html(official_url)),
            official_url: httpx.Response(
                200,
                text=job_posting_html(
                    "Senior Analytics Engineer",
                    "Seismic",
                ),
                headers={"content-type": "text/html"},
            ),
        }
    ) as client:
        resolution = resolve_official_job(lensa_job(), client)

    assert resolution.status == OFFICIAL_FOUND_VERIFIED
    assert resolution.official_job_url == official_url
    assert resolution.description_result is not None
    assert resolution.description_result.word_count >= 80


def test_source_apply_url_is_preserved_when_official_url_is_stored() -> None:
    old_data_dir = database.DATA_DIR
    old_database_path = database.DATABASE_PATH

    with TemporaryDirectory() as temp_dir:
        database.DATA_DIR = Path(temp_dir)
        database.DATABASE_PATH = Path(temp_dir) / "jobs.duckdb"

        try:
            initialize_enrichment_tables()

            with database.get_connection() as connection:
                connection.execute(
                    """
                    INSERT INTO raw_jobs (
                        record_key,
                        job_fingerprint,
                        source,
                        title,
                        company_name,
                        location,
                        apply_url
                    )
                    VALUES (
                        'lensa-seismic',
                        'lensa-seismic',
                        'lensa',
                        'Senior Analytics Engineer',
                        'Seismic',
                        'Remote',
                        'https://lensa.com/generic-alert'
                    )
                    """
                )

            result = JobDescriptionResult(
                requested_url="https://boards.greenhouse.io/seismic/jobs/123",
                final_url="https://boards.greenhouse.io/seismic/jobs/123",
                status="enriched",
                http_status=200,
                extraction_method="json_ld",
                description=DESCRIPTION,
                word_count=len(DESCRIPTION.split()),
                error_message=None,
                resolved_title="Senior Analytics Engineer",
                resolved_company="Seismic",
                resolved_location="Remote",
            )
            official = OfficialJobResolutionResult(
                status=OFFICIAL_FOUND_VERIFIED,
                official_job_url="https://boards.greenhouse.io/seismic/jobs/123",
                official_url_source="greenhouse",
                official_url_resolved_at=datetime.now(timezone.utc),
                official_url_confidence=1.0,
                official_url_validation_reason="identity accepted",
                resolved_title="Senior Analytics Engineer",
                resolved_company="Seismic",
                resolved_location="Remote",
                description_result=result,
                identity_validation=accepted_validation(),
            )

            save_enrichment_attempt(
                job=lensa_job(),
                result=result,
                status="enriched",
                identity_validation=accepted_validation(),
                official_resolution=official,
            )

            with database.get_connection() as connection:
                apply_url, official_url = connection.execute(
                    """
                    SELECT raw_jobs.apply_url, attempts.official_job_url
                    FROM raw_jobs
                    INNER JOIN job_enrichment_attempts AS attempts
                        ON raw_jobs.record_key = attempts.record_key
                    WHERE raw_jobs.record_key = 'lensa-seismic'
                    """
                ).fetchone()

            assert apply_url == "https://lensa.com/generic-alert"
            assert official_url == "https://boards.greenhouse.io/seismic/jobs/123"

        finally:
            database.DATA_DIR = old_data_dir
            database.DATABASE_PATH = old_database_path


def test_good_direct_url_does_not_trigger_official_discovery() -> None:
    validation = accepted_validation()
    direct_result = JobDescriptionResult(
        requested_url="https://boards.greenhouse.io/seismic/jobs/123",
        final_url="https://boards.greenhouse.io/seismic/jobs/123",
        status="enriched",
        http_status=200,
        extraction_method="json_ld",
        description=DESCRIPTION,
        word_count=len(DESCRIPTION.split()),
        error_message=None,
        resolved_title="Senior Analytics Engineer",
        resolved_company="Seismic",
        resolved_location="Remote",
    )

    assert not should_attempt_official_resolution(
        job={"source": "greenhouse", "apply_url": direct_result.final_url},
        enrichment_result=direct_result,
        identity_validation=validation,
    )


def test_valid_aggregator_job_still_triggers_until_official_url_verified() -> None:
    validation = accepted_validation()
    lensa_result = JobDescriptionResult(
        requested_url="https://lensa.com/job/123",
        final_url="https://lensa.com/job/123",
        status="enriched",
        http_status=200,
        extraction_method="json_ld",
        description=DESCRIPTION,
        word_count=len(DESCRIPTION.split()),
        error_message=None,
        resolved_title="Senior Analytics Engineer",
        resolved_company="Seismic",
        resolved_location="Remote",
    )

    assert should_attempt_official_resolution(
        job={
            **lensa_job(),
            "official_url_status": None,
        },
        enrichment_result=lensa_result,
        identity_validation=validation,
    )
    assert not should_attempt_official_resolution(
        job={
            **lensa_job(),
            "official_url_status": OFFICIAL_FOUND_VERIFIED,
        },
        enrichment_result=lensa_result,
        identity_validation=validation,
    )


def test_ambiguous_official_matches_are_not_guessed() -> None:
    first_url = "https://boards.greenhouse.io/seismic/jobs/123"
    second_url = "https://jobs.lever.co/seismic/456"

    with mock_client(
        {
            "search": httpx.Response(200, text=search_html(first_url, second_url)),
            first_url: httpx.Response(
                200,
                text=job_posting_html("Senior Analytics Engineer", "Seismic"),
                headers={"content-type": "text/html"},
            ),
            second_url: httpx.Response(
                200,
                text=job_posting_html("Senior Analytics Engineer", "Seismic"),
                headers={"content-type": "text/html"},
            ),
        }
    ) as client:
        resolution = resolve_official_job(lensa_job(), client)

    assert resolution.status == OFFICIAL_AMBIGUOUS
    assert resolution.official_job_url is None


def test_no_official_match_marks_not_found() -> None:
    with mock_client({"search": httpx.Response(200, text="<html></html>")}) as client:
        resolution = resolve_official_job(lensa_job(), client)

    assert resolution.status == OFFICIAL_NOT_FOUND
    assert resolution.official_job_url is None


def test_blocked_employer_site_marks_blocked() -> None:
    official_url = "https://boards.greenhouse.io/seismic/jobs/123"

    with mock_client(
        {
            "search": httpx.Response(200, text=search_html(official_url)),
            official_url: httpx.Response(403, text="blocked"),
        }
    ) as client:
        resolution = resolve_official_job(lensa_job(), client)

    assert resolution.status == OFFICIAL_BLOCKED
    assert resolution.official_job_url is None


def test_blocked_same_host_is_fetched_once_per_job() -> None:
    """BairesDev-style applicant links: avoid retrying a host after 403."""
    urls = [
        "https://applicants.seismic.com/job/339/243255/apply",
        "https://applicants.seismic.com/job/960/282675/apply",
        "https://applicants.seismic.com/job/339/162086/apply",
    ]
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.extensions["timeout"]["read"] == 8.0
        return httpx.Response(403, text="blocked")

    job = lensa_job()
    candidates = [
        official_resolver.OfficialJobCandidate(url=url, label="", source="search")
        for url in urls
    ]
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        outcome = official_resolver.resolve_from_candidates(
            job, client, candidates, source="search"
        )

    assert outcome.status == OFFICIAL_BLOCKED
    assert outcome.official_job_url is None
    assert len(requests) == 1
    assert len(job["candidate_validation_trace"]) == 3
    assert sum(trace["fetch_status"] == "skipped_blocked_host"
               for trace in job["candidate_validation_trace"]) == 2
    assert job["enrichment_timing"]["blocked_host_skips"] == 2
    assert job["enrichment_timing"]["candidate_fetches"] == 1


def test_blocked_host_skip_cannot_create_false_verified_match() -> None:
    blocked_urls = [
        "https://applicants.seismic.com/job/1/apply",
        "https://applicants.seismic.com/job/2/apply",
    ]
    verified_url = "https://boards.greenhouse.io/seismic/jobs/123"
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if request.url.host == "applicants.seismic.com":
            return httpx.Response(403, text="blocked")
        return httpx.Response(
            200, text=job_posting_html("Senior Analytics Engineer", "Seismic"),
            headers={"content-type": "text/html"},
        )

    job = lensa_job()
    candidates = [
        official_resolver.OfficialJobCandidate(url=url, label="", source="search")
        for url in [*blocked_urls, verified_url]
    ]
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        outcome = official_resolver.resolve_from_candidates(
            job, client, candidates, source="search"
        )

    assert outcome.status == official_resolver.OFFICIAL_SEARCH_DEFERRED
    assert outcome.official_job_url is None
    assert len(requests) == 2


def test_budget_expiry_does_not_promote_partial_verified_candidates() -> None:
    """Never resolve to FOUND when a competing candidate was not inspected."""
    urls = [
        "https://boards.greenhouse.io/seismic/jobs/123",
        "https://boards.greenhouse.io/seismic/jobs/456",
    ]
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(
            200,
            text=job_posting_html("Senior Analytics Engineer", "Seismic"),
            headers={"content-type": "text/html"},
        )

    job = lensa_job()
    candidates = [
        official_resolver.OfficialJobCandidate(
            url=url, label="", source="search"
        ) for url in urls
    ]
    # Simulate time expiring immediately after evaluating the first candidate.
    moments = iter((10.0, 10.0, 10.2, 16.0))
    with patch.object(
        official_resolver, "time",
        SimpleNamespace(monotonic=lambda: next(moments))
    ):
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            outcome = official_resolver.resolve_from_candidates(
                job, client, candidates, source="search", deadline=15.0
            )

    assert outcome.status == official_resolver.OFFICIAL_SEARCH_DEFERRED
    assert outcome.official_job_url is None
    assert len(requests) == 1


def test_identity_mismatch_rejects_official_candidate() -> None:
    official_url = "https://boards.greenhouse.io/other/jobs/123"

    with mock_client(
        {
            "search": httpx.Response(200, text=search_html(official_url)),
            official_url: httpx.Response(
                200,
                text=job_posting_html("Software Engineer", "Other Company"),
                headers={"content-type": "text/html"},
            ),
        }
    ) as client:
        resolution = resolve_official_job(lensa_job(), client)

    assert resolution.status == OFFICIAL_NOT_FOUND
    assert resolution.official_job_url is None
    assert "company mismatch" in (resolution.official_url_validation_reason or "")


def test_dynamic_candidate_fetch_can_verify_official_job() -> None:
    official_url = "https://boards.greenhouse.io/seismic/jobs/123"
    original_dynamic_fetch = official_resolver.fetch_dynamic_job_description

    def fake_dynamic_fetch(url: str) -> JobDescriptionResult | None:
        assert url == official_url

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

    official_resolver.fetch_dynamic_job_description = fake_dynamic_fetch

    try:
        with mock_client(
            {
                "search": httpx.Response(200, text=search_html(official_url)),
                official_url: httpx.Response(
                    200,
                    text=job_posting_html(
                        "Senior Analytics Engineer",
                        "Seismic",
                        description="short",
                    ),
                    headers={"content-type": "text/html"},
                ),
            }
        ) as client:
            resolution = resolve_official_job(lensa_job(), client)
    finally:
        official_resolver.fetch_dynamic_job_description = original_dynamic_fetch

    assert resolution.status == OFFICIAL_FOUND_VERIFIED
    assert resolution.description_result is not None
    assert resolution.description_result.extraction_method == "playwright_json_ld"


def test_ui_open_link_prefers_verified_official_url() -> None:
    target = select_job_open_target(
        {
            "apply_url": "https://lensa.com/generic-alert",
            "official_url_status": OFFICIAL_FOUND_VERIFIED,
            "official_job_url": "https://boards.greenhouse.io/seismic/jobs/123",
        }
    )

    assert target.label == "Open official job"
    assert target.url == "https://boards.greenhouse.io/seismic/jobs/123"


def test_ui_open_link_blocks_unverified_aggregator_source_page() -> None:
    target = select_job_open_target(
        {
            "apply_url": "https://lensa.com/generic-alert",
            "official_url_status": OFFICIAL_NOT_FOUND,
        }
    )

    assert target.label == "Official job unavailable"
    assert target.url is None


def main() -> None:
    test_lensa_generic_page_falls_back_to_official_job()
    test_source_apply_url_is_preserved_when_official_url_is_stored()
    test_good_direct_url_does_not_trigger_official_discovery()
    test_valid_aggregator_job_still_triggers_until_official_url_verified()
    test_ambiguous_official_matches_are_not_guessed()
    test_no_official_match_marks_not_found()
    test_blocked_employer_site_marks_blocked()
    test_blocked_same_host_is_fetched_once_per_job()
    test_blocked_host_skip_cannot_create_false_verified_match()
    test_budget_expiry_does_not_promote_partial_verified_candidates()
    test_identity_mismatch_rejects_official_candidate()
    test_dynamic_candidate_fetch_can_verify_official_job()
    test_ui_open_link_prefers_verified_official_url()
    test_ui_open_link_blocks_unverified_aggregator_source_page()
    print("Official job resolver tests passed.")


if __name__ == "__main__":
    main()
