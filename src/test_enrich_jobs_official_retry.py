from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx

from src import database
from src import enrich_jobs
from src.enrich_jobs import (
    initialize_enrichment_tables,
    load_jobs_to_enrich,
    needs_official_resolution,
    process_enrichment_job,
    save_enrichment_attempt,
)
from src.enrichment.job_description import JobDescriptionResult
from src.enrichment.official_job_resolver import (
    OFFICIAL_FOUND_VERIFIED,
    OFFICIAL_NOT_FOUND,
    OFFICIAL_BLOCKED,
    OfficialJobResolutionResult,
    should_attempt_official_resolution,
)
from src.enrichment.job_identity import JobIdentityValidation
from src.ui.job_links import select_job_open_target


LENSA_MONZO_URL = (
    "https://lensa.com/senior-analytics-engineer-jobs-hiring-remote/"
    "tp-jobstop/8141a1ddf83b0c22d554b3c43f06d6d27967f69dbb8b80ec75b0a706f22fd7c6"
    "?tr=501992f9672d45a5933f256a8a4e7f92intc1"
    "&utm_source=jobalert&utm_medium=default_jobs&utm_campaign=8"
)
GREENHOUSE_MONZO_URL = "https://boards.greenhouse.io/monzo/jobs/123"
DESCRIPTION = " ".join(
    [
        "Responsibilities include analytics engineering, SQL modeling, dbt,"
        " stakeholder partnership, experimentation analysis, and reliable data products."
    ]
    * 12
)


GLASSDOOR_URL = (
    "https://www.glassdoor.com/job-listing/data-engineer-example-"
    "JV_IC1147341_KO0,13.htm?jobListingId=12345"
)


def job_posting_html(
    title: str = "Data Engineer",
    company: str = "Example Co",
    location: str = "Remote",
    description: str = DESCRIPTION,
) -> str:
    payload = {
        "@context": "https://schema.org",
        "@type": "JobPosting",
        "title": title,
        "description": description,
        "hiringOrganization": {
            "@type": "Organization",
            "name": company,
        },
        "jobLocation": {
            "@type": "Place",
            "address": {
                "addressLocality": location,
            },
        },
    }

    return (
        "<html><head><script type='application/ld+json'>"
        f"{json.dumps(payload)}"
        "</script></head><body></body></html>"
    )


def mock_client(
    routes: dict[str, httpx.Response],
) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        response = routes.get(str(request.url))

        if response is None:
            return httpx.Response(
                404,
                request=request,
            )

        response.request = request
        return response

    return httpx.Client(
        transport=httpx.MockTransport(handler),
        follow_redirects=True,
    )


def with_temp_database(callback) -> None:
    old_data_dir = database.DATA_DIR
    old_database_path = database.DATABASE_PATH

    with TemporaryDirectory() as temp_dir:
        database.DATA_DIR = Path(temp_dir)
        database.DATABASE_PATH = Path(temp_dir) / "jobs.duckdb"

        try:
            initialize_enrichment_tables()
            create_supporting_tables()
            callback()
        finally:
            database.DATA_DIR = old_data_dir
            database.DATABASE_PATH = old_database_path


def create_supporting_tables() -> None:
    with database.get_connection() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS job_matches (
                record_key VARCHAR,
                match_score INTEGER,
                is_recommended BOOLEAN,
                needs_review BOOLEAN
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS resume_job_scores (
                record_key VARCHAR,
                overall_score INTEGER,
                description_complete BOOLEAN,
                scored_at TIMESTAMPTZ
            )
            """
        )


def insert_raw_job(
    record_key: str,
    source: str = "lensa",
    title: str = "Staff Analytics Engineer",
    company_name: str = "Monzo",
    description: str | None = None,
    apply_url: str = LENSA_MONZO_URL,
) -> None:
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
                description,
                apply_url
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                record_key,
                record_key,
                source,
                title,
                company_name,
                "Remote",
                description,
                apply_url,
            ],
        )


def insert_attempt(
    record_key: str,
    source: str = "lensa",
    status: str = "no_description",
    official_status: str | None = None,
    official_url: str | None = None,
    official_resolved_at: datetime | None = None,
    attempted_at: datetime | None = None,
) -> None:
    timestamp = attempted_at or datetime.now(timezone.utc)

    with database.get_connection() as connection:
        connection.execute(
            """
            INSERT INTO job_enrichment_attempts (
                record_key,
                source,
                requested_url,
                final_url,
                status,
                http_status,
                extraction_method,
                description_word_count,
                error_message,
                attempt_count,
                last_attempted_at,
                official_job_url,
                official_url_status,
                official_url_source,
                official_url_resolved_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                record_key,
                source,
                LENSA_MONZO_URL,
                None,
                status,
                None,
                None,
                0,
                "No description found.",
                1,
                timestamp,
                official_url,
                official_status,
                "greenhouse" if official_url else None,
                official_resolved_at,
            ],
        )


def selected_record_keys() -> list[str]:
    jobs = load_jobs_to_enrich(
        limit=20,
        minimum_words=40,
        source=None,
        retry_failed=False,
        force=False,
    )

    return [
        job["record_key"]
        for job in jobs
    ]


def selected_jobs() -> list[dict]:
    return load_jobs_to_enrich(
        limit=20,
        minimum_words=40,
        source=None,
        retry_failed=False,
        force=False,
    )


def test_previous_failed_lensa_attempt_with_null_official_status_is_selected() -> None:
    def scenario() -> None:
        insert_raw_job("monzo-old")
        insert_attempt("monzo-old", official_status=None)

        assert selected_record_keys() == ["monzo-old"]

    with_temp_database(scenario)


def test_found_verified_is_not_selected_again_for_official_resolution() -> None:
    def scenario() -> None:
        insert_raw_job("monzo-verified")
        insert_attempt(
            "monzo-verified",
            official_status=OFFICIAL_FOUND_VERIFIED,
            official_url=GREENHOUSE_MONZO_URL,
        )

        assert selected_record_keys() == []

    with_temp_database(scenario)


def test_official_not_found_recently_is_in_cooldown() -> None:
    def scenario() -> None:
        insert_raw_job("monzo-recent")
        insert_attempt(
            "monzo-recent",
            official_status=OFFICIAL_NOT_FOUND,
            official_resolved_at=datetime.now(timezone.utc) - timedelta(days=1),
        )

        assert selected_record_keys() == []

    with_temp_database(scenario)


def test_official_not_found_after_cooldown_is_selected() -> None:
    def scenario() -> None:
        insert_raw_job("monzo-old-not-found")
        insert_attempt(
            "monzo-old-not-found",
            official_status=OFFICIAL_NOT_FOUND,
            official_resolved_at=datetime.now(timezone.utc) - timedelta(days=8),
        )

        assert selected_record_keys() == ["monzo-old-not-found"]

    with_temp_database(scenario)


def test_full_description_lensa_without_official_url_is_selected() -> None:
    def scenario() -> None:
        insert_raw_job(
            "monzo-full",
            description=DESCRIPTION,
        )

        jobs = selected_jobs()

        assert [job["record_key"] for job in jobs] == ["monzo-full"]
        assert jobs[0]["needs_description_enrichment"] is False
        assert jobs[0]["needs_official_resolution"] is True

    with_temp_database(scenario)


def test_direct_greenhouse_full_description_is_not_selected() -> None:
    def scenario() -> None:
        insert_raw_job(
            record_key="greenhouse-full",
            source="greenhouse",
            title="Staff Analytics Engineer",
            company_name="Monzo",
            description=DESCRIPTION,
            apply_url=GREENHOUSE_MONZO_URL,
        )

        assert selected_record_keys() == []

    with_temp_database(scenario)


def test_enriched_aggregator_page_still_triggers_official_resolution_when_missing() -> None:
    validation = JobIdentityValidation(
        accepted=True,
        confidence=1.0,
        title_similarity=1.0,
        company_similarity=1.0,
        occupation_match=True,
        reason="identity accepted",
    )
    result = JobDescriptionResult(
        requested_url=LENSA_MONZO_URL,
        final_url=LENSA_MONZO_URL,
        status="enriched",
        http_status=200,
        extraction_method="json_ld",
        description=DESCRIPTION,
        word_count=len(DESCRIPTION.split()),
        error_message=None,
        resolved_title="Staff Analytics Engineer",
        resolved_company="Monzo",
        resolved_location="Remote",
    )

    assert should_attempt_official_resolution(
        job={
            "source": "lensa",
            "apply_url": LENSA_MONZO_URL,
            "official_url_status": None,
        },
        enrichment_result=result,
        identity_validation=validation,
    )
    assert not should_attempt_official_resolution(
        job={
            "source": "lensa",
            "apply_url": LENSA_MONZO_URL,
            "official_url_status": OFFICIAL_FOUND_VERIFIED,
        },
        enrichment_result=result,
        identity_validation=validation,
    )


def test_monzo_historical_lensa_record_resolves_official_url_without_refetching_lensa() -> None:
    original_fetch = enrich_jobs.fetch_job_description
    original_resolve = enrich_jobs.resolve_official_job

    def scenario() -> None:
        insert_raw_job("monzo-old")
        insert_attempt("monzo-old", official_status=None)
        job = selected_jobs()[0]

        def forbidden_fetch(*args, **kwargs):
            raise AssertionError("Lensa page should not be refetched")

        def fake_resolve(job: dict, client: httpx.Client):
            description_result = JobDescriptionResult(
                requested_url=GREENHOUSE_MONZO_URL,
                final_url=GREENHOUSE_MONZO_URL,
                status="enriched",
                http_status=200,
                extraction_method="json_ld",
                description=DESCRIPTION,
                word_count=len(DESCRIPTION.split()),
                error_message=None,
                resolved_title="Staff Analytics Engineer",
                resolved_company="Monzo",
                resolved_location="Remote",
            )
            identity_validation = JobIdentityValidation(
                accepted=True,
                confidence=1.0,
                title_similarity=1.0,
                company_similarity=1.0,
                occupation_match=True,
                reason="identity accepted",
            )

            return OfficialJobResolutionResult(
                status=OFFICIAL_FOUND_VERIFIED,
                official_job_url=GREENHOUSE_MONZO_URL,
                official_url_source="greenhouse",
                official_url_resolved_at=datetime.now(timezone.utc),
                official_url_confidence=1.0,
                official_url_validation_reason="identity accepted",
                resolved_title="Staff Analytics Engineer",
                resolved_company="Monzo",
                resolved_location="Remote",
                description_result=description_result,
                identity_validation=identity_validation,
            )

        enrich_jobs.fetch_job_description = forbidden_fetch
        enrich_jobs.resolve_official_job = fake_resolve

        try:
            with httpx.Client() as client:
                processed = process_enrichment_job(job, client)

            save_enrichment_attempt(
                job=job,
                result=processed.result,
                status=processed.stored_status,
                error_message=processed.stored_error,
                identity_validation=processed.identity_validation,
                official_resolution=processed.official_resolution,
            )
        finally:
            enrich_jobs.fetch_job_description = original_fetch
            enrich_jobs.resolve_official_job = original_resolve

        with database.get_connection() as connection:
            row = connection.execute(
                """
                SELECT
                    raw_jobs.apply_url,
                    attempts.official_job_url,
                    attempts.official_url_status,
                    attempts.official_url_source,
                    attempts.official_url_confidence
                FROM raw_jobs
                INNER JOIN job_enrichment_attempts AS attempts
                    ON raw_jobs.record_key = attempts.record_key
                WHERE raw_jobs.record_key = 'monzo-old'
                """
            ).fetchone()

        assert row[0] == LENSA_MONZO_URL
        assert row[1] == GREENHOUSE_MONZO_URL
        assert row[2] == OFFICIAL_FOUND_VERIFIED
        assert row[3] == "greenhouse"
        assert row[4] == 1.0

        target = select_job_open_target(
            {
                "apply_url": row[0],
                "official_job_url": row[1],
                "official_url_status": row[2],
            }
        )

        assert target.label == "Open official job"
        assert target.url == GREENHOUSE_MONZO_URL

    with_temp_database(scenario)


def test_needs_official_resolution_direct_function() -> None:
    now = datetime(2026, 10, 2, tzinfo=timezone.utc)

    assert needs_official_resolution(
        {
            "source": "lensa",
            "apply_url": LENSA_MONZO_URL,
            "official_url_status": None,
        },
        now=now,
    )
    assert not needs_official_resolution(
        {
            "source": "lensa",
            "apply_url": LENSA_MONZO_URL,
            "official_url_status": OFFICIAL_NOT_FOUND,
            "official_url_resolved_at": now - timedelta(days=1),
        },
        now=now,
    )
    assert needs_official_resolution(
        {
            "source": "lensa",
            "apply_url": LENSA_MONZO_URL,
            "official_url_status": OFFICIAL_NOT_FOUND,
            "official_url_resolved_at": now - timedelta(days=8),
        },
        now=now,
    )


def test_glassdoor_digest_without_description_is_selected_for_resolution() -> None:
    def scenario() -> None:
        insert_raw_job(
            "glassdoor-new",
            source="glassdoor",
            title="Data Engineer",
            company_name="Example Co",
            apply_url=GLASSDOOR_URL,
        )

        jobs = selected_jobs()

        assert [
            job["record_key"]
            for job in jobs
        ] == ["glassdoor-new"]
        assert jobs[0]["needs_official_resolution"] is True

    with_temp_database(scenario)


def test_glassdoor_full_description_is_saved() -> None:
    def scenario() -> None:
        insert_raw_job(
            "glassdoor-description",
            source="glassdoor",
            title="Data Engineer",
            company_name="Example Co",
            apply_url=GLASSDOOR_URL,
        )
        job = selected_jobs()[0]

        with mock_client(
            {
                GLASSDOOR_URL: httpx.Response(
                    200,
                    text=job_posting_html(),
                    headers={"content-type": "text/html"},
                )
            }
        ) as client:
            result = process_enrichment_job(
                job,
                client,
            )

        with database.get_connection() as connection:
            stored_description = connection.execute(
                """
                SELECT description
                FROM raw_jobs
                WHERE record_key = 'glassdoor-description'
                """
            ).fetchone()[0]

        assert result.updated is True
        assert result.stored_status == "enriched"
        assert stored_description == DESCRIPTION

    with_temp_database(scenario)


def test_glassdoor_blocked_page_uses_verified_official_description() -> None:
    def scenario() -> None:
        insert_raw_job(
            "glassdoor-official",
            source="glassdoor",
            title="Data Engineer",
            company_name="Example Co",
            apply_url=GLASSDOOR_URL,
        )
        job = selected_jobs()[0]
        original_resolve = enrich_jobs.resolve_official_job

        def fake_resolve_official_job(job, client):
            return OfficialJobResolutionResult(
                status=OFFICIAL_FOUND_VERIFIED,
                official_job_url="https://boards.greenhouse.io/example/jobs/123",
                official_url_source="greenhouse",
                official_url_resolved_at=datetime.now(timezone.utc),
                official_url_confidence=1.0,
                official_url_validation_reason="identity accepted",
                resolved_title="Data Engineer",
                resolved_company="Example Co",
                resolved_location="Remote",
                description_result=JobDescriptionResult(
                    requested_url="https://boards.greenhouse.io/example/jobs/123",
                    final_url="https://boards.greenhouse.io/example/jobs/123",
                    status="enriched",
                    http_status=200,
                    extraction_method="json_ld",
                    description=DESCRIPTION,
                    word_count=len(DESCRIPTION.split()),
                    error_message=None,
                    resolved_title="Data Engineer",
                    resolved_company="Example Co",
                    resolved_location="Remote",
                ),
                identity_validation=JobIdentityValidation(
                    accepted=True,
                    confidence=1.0,
                    title_similarity=1.0,
                    company_similarity=1.0,
                    occupation_match=True,
                    reason="accepted",
                ),
            )

        enrich_jobs.resolve_official_job = fake_resolve_official_job

        try:
            with mock_client(
                {
                    GLASSDOOR_URL: httpx.Response(
                        403,
                        text="<html>captcha access denied</html>",
                    )
                }
            ) as client:
                result = process_enrichment_job(
                    job,
                    client,
                )
        finally:
            enrich_jobs.resolve_official_job = original_resolve

        assert result.updated is True
        assert result.official_resolution is not None
        assert result.official_resolution.status == OFFICIAL_FOUND_VERIFIED

    with_temp_database(scenario)


def test_glassdoor_blocked_without_official_match_stays_unscored() -> None:
    def scenario() -> None:
        insert_raw_job(
            "glassdoor-unresolved",
            source="glassdoor",
            title="Data Engineer",
            company_name="Example Co",
            apply_url=GLASSDOOR_URL,
        )
        job = selected_jobs()[0]
        original_resolve = enrich_jobs.resolve_official_job

        def fake_resolve_official_job(job, client):
            return OfficialJobResolutionResult(
                status=OFFICIAL_NOT_FOUND,
                official_job_url=None,
                official_url_source="search",
                official_url_resolved_at=datetime.now(timezone.utc),
                official_url_confidence=None,
                official_url_validation_reason="no verified match",
                resolved_title=None,
                resolved_company=None,
                resolved_location=None,
            )

        enrich_jobs.resolve_official_job = fake_resolve_official_job

        try:
            with mock_client(
                {
                    GLASSDOOR_URL: httpx.Response(
                        403,
                        text="<html>captcha access denied</html>",
                    )
                }
            ) as client:
                result = process_enrichment_job(
                    job,
                    client,
                )
        finally:
            enrich_jobs.resolve_official_job = original_resolve

        assert result.updated is False
        assert result.official_resolution is not None
        assert result.official_resolution.status == OFFICIAL_NOT_FOUND

    with_temp_database(scenario)


def test_glassdoor_failed_resolution_retries_after_cooldown() -> None:
    def scenario() -> None:
        insert_raw_job(
            "glassdoor-old",
            source="glassdoor",
            title="Data Engineer",
            company_name="Example Co",
            apply_url=GLASSDOOR_URL,
        )
        insert_attempt(
            "glassdoor-old",
            source="glassdoor",
            official_status=OFFICIAL_NOT_FOUND,
            official_resolved_at=datetime.now(timezone.utc) - timedelta(days=8),
        )

        assert selected_record_keys() == ["glassdoor-old"]

    with_temp_database(scenario)


def test_glassdoor_wrong_company_description_is_rejected() -> None:
    def scenario() -> None:
        insert_raw_job(
            "glassdoor-wrong-company",
            source="glassdoor",
            title="Data Engineer",
            company_name="Example Co",
            apply_url=GLASSDOOR_URL,
        )
        job = selected_jobs()[0]

        with mock_client(
            {
                GLASSDOOR_URL: httpx.Response(
                    200,
                    text=job_posting_html(company="Wrong Co"),
                    headers={"content-type": "text/html"},
                )
            }
        ) as client:
            result = process_enrichment_job(
                job,
                client,
            )

        assert result.updated is False
        assert result.stored_status == "resolution_rejected"

    with_temp_database(scenario)


def main() -> None:
    test_previous_failed_lensa_attempt_with_null_official_status_is_selected()
    test_found_verified_is_not_selected_again_for_official_resolution()
    test_official_not_found_recently_is_in_cooldown()
    test_official_not_found_after_cooldown_is_selected()
    test_full_description_lensa_without_official_url_is_selected()
    test_direct_greenhouse_full_description_is_not_selected()
    test_enriched_aggregator_page_still_triggers_official_resolution_when_missing()
    test_monzo_historical_lensa_record_resolves_official_url_without_refetching_lensa()
    test_needs_official_resolution_direct_function()
    test_glassdoor_digest_without_description_is_selected_for_resolution()
    test_glassdoor_full_description_is_saved()
    test_glassdoor_blocked_page_uses_verified_official_description()
    test_glassdoor_blocked_without_official_match_stays_unscored()
    test_glassdoor_failed_resolution_retries_after_cooldown()
    test_glassdoor_wrong_company_description_is_rejected()
    print("Official enrichment retry tests passed.")


if __name__ == "__main__":
    main()
