from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx

from src import database
from src import enrich_jobs
from src.enrich_jobs import (
    DEFAULT_ENRICHMENT_LIMIT,
    EnrichmentProcessingResult,
    authoritative_verified_key_for_job,
    initialize_enrichment_tables,
    load_jobs_to_enrich,
    needs_official_resolution,
    process_enrichment_batch,
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
from src.ui.daily_workflow_service import automatic_enrichment_limit
from src.verified_posting_identity import description_hash


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
    location: str = "Remote",
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
                location,
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
    attempt_count: int = 1,
    http_status: int | None = None,
    verified_posting_key: str | None = None,
    description_hash_value: str | None = None,
    official_resolved_title: str | None = None,
    official_resolved_company: str | None = None,
    official_resolved_location: str | None = None,
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
                official_url_resolved_at,
                official_resolved_title,
                official_resolved_company,
                official_resolved_location,
                verified_posting_key,
                description_hash
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                record_key,
                source,
                LENSA_MONZO_URL,
                None,
                status,
                http_status,
                None,
                0,
                "No description found.",
                attempt_count,
                timestamp,
                official_url,
                official_status,
                "greenhouse" if official_url else None,
                official_resolved_at,
                official_resolved_title,
                official_resolved_company,
                official_resolved_location,
                verified_posting_key,
                description_hash_value,
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


def test_never_attempted_relevant_job_beats_repeated_failed_job() -> None:
    def scenario() -> None:
        now = datetime.now(timezone.utc)
        insert_raw_job(
            "robots-never",
            source="linkedin",
            title="Senior Data Engineer",
            company_name="Robots & Pencils",
            apply_url="https://www.linkedin.com/jobs/view/111",
        )
        insert_raw_job(
            "robots-repeated",
            source="linkedin",
            title="Senior Data Engineer",
            company_name="Robots & Pencils",
            apply_url="https://www.linkedin.com/jobs/view/222",
        )
        insert_attempt(
            "robots-repeated",
            source="linkedin",
            status="blocked",
            official_status=OFFICIAL_NOT_FOUND,
            official_resolved_at=now - timedelta(days=8),
            attempted_at=now - timedelta(days=8),
            attempt_count=6,
        )

        jobs = load_jobs_to_enrich(
            limit=2,
            minimum_words=40,
            source=None,
            retry_failed=False,
            force=False,
        )

        assert jobs[0]["record_key"] == "robots-never"
        assert jobs[0]["description_state"] == "NEEDS_ENRICHMENT"
        assert jobs[0]["needs_official_resolution"] is True

    with_temp_database(scenario)


def test_default_batch_can_select_more_than_five_jobs() -> None:
    def scenario() -> None:
        for index in range(6):
            insert_raw_job(
                f"batch-{index}",
                source="linkedin",
                title="Senior Data Engineer",
                company_name=f"Company {index}",
                apply_url=f"https://www.linkedin.com/jobs/view/{index}",
            )

        jobs = load_jobs_to_enrich(
            limit=DEFAULT_ENRICHMENT_LIMIT,
            minimum_words=40,
            source=None,
            retry_failed=False,
            force=False,
        )

        assert DEFAULT_ENRICHMENT_LIMIT == 25
        assert len(jobs) == 6

    with_temp_database(scenario)


def test_rank_21_linkedin_job_is_in_default_ui_enrichment_batch() -> None:
    def scenario() -> None:
        now = datetime.now(timezone.utc)

        for index in range(20):
            insert_raw_job(
                f"higher-priority-{index}",
                source="linkedin",
                title="Senior Data Engineer",
                company_name=f"Higher Priority {index}",
                apply_url=f"https://www.linkedin.com/jobs/view/higher-{index}",
            )

        insert_raw_job(
            "robots-pencils-rank-21",
            source="linkedin",
            title="Senior Data Engineer",
            company_name="Robots & Pencils",
            apply_url="https://www.linkedin.com/jobs/view/robots-rank-21",
        )
        insert_attempt(
            "robots-pencils-rank-21",
            source="linkedin",
            status="blocked",
            official_status=OFFICIAL_BLOCKED,
            official_resolved_at=now - timedelta(days=8),
            attempted_at=now - timedelta(days=8),
            attempt_count=5,
            http_status=429,
        )

        too_narrow_jobs = load_jobs_to_enrich(
            limit=20,
            minimum_words=40,
            source=None,
            retry_failed=False,
            force=False,
        )
        default_jobs = load_jobs_to_enrich(
            limit=automatic_enrichment_limit(20),
            minimum_words=40,
            source=None,
            retry_failed=False,
            force=False,
        )
        robots_job = next(
            job
            for job in default_jobs
            if job["record_key"] == "robots-pencils-rank-21"
        )

        assert "robots-pencils-rank-21" not in [
            job["record_key"]
            for job in too_narrow_jobs
        ]
        assert automatic_enrichment_limit(20) == DEFAULT_ENRICHMENT_LIMIT
        assert robots_job["enrichment_queue_rank"] == 21
        assert robots_job["description_state"] == "NEEDS_ENRICHMENT"
        assert robots_job["previous_status"] == "blocked"
        assert robots_job["needs_official_resolution"] is True

    with_temp_database(scenario)


def test_batch_continues_after_individual_enrichment_failure() -> None:
    def scenario() -> None:
        insert_raw_job(
            "batch-fail",
            source="linkedin",
            title="Senior Data Engineer",
            company_name="Failure Co",
            apply_url="https://www.linkedin.com/jobs/view/333",
        )
        insert_raw_job(
            "batch-continue",
            source="linkedin",
            title="Senior Data Engineer",
            company_name="Success Co",
            apply_url="https://www.linkedin.com/jobs/view/444",
        )
        jobs = load_jobs_to_enrich(
            limit=DEFAULT_ENRICHMENT_LIMIT,
            minimum_words=40,
            source=None,
            retry_failed=False,
            force=False,
        )
        original_processor = enrich_jobs.process_enrichment_job

        def fake_processor(job: dict, client) -> EnrichmentProcessingResult:
            if job["record_key"] == "batch-fail":
                raise RuntimeError("synthetic fetch failure")

            return EnrichmentProcessingResult(
                result=JobDescriptionResult(
                    requested_url=job["apply_url"],
                    final_url=job["apply_url"],
                    status="no_description",
                    http_status=200,
                    extraction_method="test",
                    description="",
                    word_count=0,
                    error_message="No description found.",
                ),
                stored_status="no_description",
                stored_error="No description found.",
                identity_validation=None,
                official_resolution=None,
                updated=False,
            )

        try:
            enrich_jobs.process_enrichment_job = fake_processor
            summary = process_enrichment_batch(
                jobs=jobs,
                client=object(),
                delay_seconds=0,
            )
        finally:
            enrich_jobs.process_enrichment_job = original_processor

        assert summary.jobs_processed == 2
        assert summary.totals["fetch_error"] == 1
        assert summary.totals["no_description"] == 1

        with database.get_connection() as connection:
            attempt_count = connection.execute(
                """
                SELECT COUNT(*)
                FROM job_enrichment_attempts
                """
            ).fetchone()[0]

        assert attempt_count == 2

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


def test_linkedin_missing_description_is_selected_for_official_resolution() -> None:
    def scenario() -> None:
        insert_raw_job(
            "linkedin-machinify",
            source="linkedin",
            title="Senior Data Engineer - Analytics",
            company_name="Machinify",
            apply_url="https://www.linkedin.com/jobs/view/4469362702/",
        )

        jobs = selected_jobs()

        assert [job["record_key"] for job in jobs] == ["linkedin-machinify"]
        assert jobs[0]["needs_description_enrichment"] is True
        assert jobs[0]["needs_official_resolution"] is True

    with_temp_database(scenario)


def test_indeed_incomplete_description_is_selected_for_official_resolution() -> None:
    def scenario() -> None:
        insert_raw_job(
            "indeed-incomplete",
            source="indeed",
            title="Senior Analytics Engineer",
            company_name="Example Co",
            description="Short alert summary with no requirements.",
            apply_url="https://www.indeed.com/viewjob?jk=123",
        )

        jobs = selected_jobs()

        assert [job["record_key"] for job in jobs] == ["indeed-incomplete"]
        assert jobs[0]["needs_description_enrichment"] is True
        assert jobs[0]["needs_official_resolution"] is True

    with_temp_database(scenario)


def test_unknown_source_missing_description_can_use_official_resolution() -> None:
    def scenario() -> None:
        insert_raw_job(
            "unknown-source",
            source="newboard",
            title="Senior Data Engineer",
            company_name="Example Co",
            apply_url="https://jobs.example-board.test/job/123",
        )

        jobs = selected_jobs()

        assert [job["record_key"] for job in jobs] == ["unknown-source"]
        assert jobs[0]["needs_official_resolution"] is True

    with_temp_database(scenario)


def test_linkedin_incomplete_direct_fetch_uses_official_fallback() -> None:
    original_fetch = enrich_jobs.fetch_job_description
    original_resolve = enrich_jobs.resolve_official_job

    def scenario() -> None:
        insert_raw_job(
            "linkedin-fallback",
            source="linkedin",
            title="Senior Data Engineer - Analytics",
            company_name="Machinify",
            apply_url="https://www.linkedin.com/jobs/view/4469362702/",
        )
        job = selected_jobs()[0]

        def fake_fetch(*args, **kwargs):
            return JobDescriptionResult(
                requested_url=job["apply_url"],
                final_url=job["apply_url"],
                status="no_description",
                http_status=200,
                extraction_method="html_selector",
                description="",
                word_count=0,
                error_message="No sufficiently complete job description was found.",
            )

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
                resolved_title="Senior Data Engineer - Analytics",
                resolved_company="Machinify",
                resolved_location="Remote",
            )
            identity_validation = JobIdentityValidation(
                accepted=True,
                confidence=1.0,
                title_similarity=1.0,
                company_similarity=1.0,
                occupation_match=True,
                reason="accepted",
            )

            return OfficialJobResolutionResult(
                status=OFFICIAL_FOUND_VERIFIED,
                official_job_url=GREENHOUSE_MONZO_URL,
                official_url_source="greenhouse",
                official_url_resolved_at=datetime.now(timezone.utc),
                official_url_confidence=1.0,
                official_url_validation_reason="identity accepted",
                resolved_title="Senior Data Engineer - Analytics",
                resolved_company="Machinify",
                resolved_location="Remote",
                description_result=description_result,
                identity_validation=identity_validation,
            )

        enrich_jobs.fetch_job_description = fake_fetch
        enrich_jobs.resolve_official_job = fake_resolve

        try:
            with httpx.Client() as client:
                result = process_enrichment_job(job, client)
        finally:
            enrich_jobs.fetch_job_description = original_fetch
            enrich_jobs.resolve_official_job = original_resolve

        assert result.updated is True
        assert result.official_resolution is not None
        assert result.official_resolution.status == OFFICIAL_FOUND_VERIFIED

    with_temp_database(scenario)


def test_direct_greenhouse_valid_description_skips_official_search() -> None:
    original_resolve = enrich_jobs.resolve_official_job

    def scenario() -> None:
        insert_raw_job(
            record_key="greenhouse-valid",
            source="greenhouse",
            title="Staff Analytics Engineer",
            company_name="Monzo",
            apply_url=GREENHOUSE_MONZO_URL,
        )
        job = selected_jobs()[0]

        def forbidden_resolve(*args, **kwargs):
            raise AssertionError("Official search should not run after a valid ATS fetch")

        enrich_jobs.resolve_official_job = forbidden_resolve

        try:
            with mock_client(
                {
                    GREENHOUSE_MONZO_URL: httpx.Response(
                        200,
                        text=job_posting_html(
                            title="Staff Analytics Engineer",
                            company="Monzo",
                        ),
                        headers={"content-type": "text/html"},
                    )
                }
            ) as client:
                result = process_enrichment_job(job, client)
        finally:
            enrich_jobs.resolve_official_job = original_resolve

        assert result.updated is True
        assert result.official_resolution is None

    with_temp_database(scenario)


def test_failed_official_resolution_cooldown_applies_to_linkedin() -> None:
    def scenario() -> None:
        insert_raw_job(
            "linkedin-recent-not-found",
            source="linkedin",
            title="Senior Data Engineer - Analytics",
            company_name="Machinify",
            apply_url="https://www.linkedin.com/jobs/view/4469362702/",
        )
        insert_attempt(
            "linkedin-recent-not-found",
            source="linkedin",
            official_status=OFFICIAL_NOT_FOUND,
            official_resolved_at=datetime.now(timezone.utc) - timedelta(days=1),
        )

        assert selected_record_keys() == []

    with_temp_database(scenario)


def test_same_duplicate_from_multiple_sources_is_selected_once() -> None:
    def scenario() -> None:
        insert_raw_job(
            "linkedin-copy",
            source="linkedin",
            title="Senior Data Engineer - Analytics",
            company_name="Machinify",
            apply_url="https://www.linkedin.com/jobs/view/4469362702/",
        )
        insert_raw_job(
            "lensa-copy",
            source="lensa",
            title="Senior Data Engineer - Analytics",
            company_name="Machinify",
            apply_url=LENSA_MONZO_URL,
        )

        with database.get_connection() as connection:
            connection.execute(
                """
                UPDATE raw_jobs
                SET job_fingerprint = 'machinify-senior-data-engineer'
                WHERE record_key IN ('linkedin-copy', 'lensa-copy')
                """
            )

        assert len(selected_jobs()) == 1

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


def test_fuzzy_historical_duplicate_does_not_reuse_before_verification() -> None:
    def scenario() -> None:
        verified_key = "greenhouse:123"
        official_url = "https://boards.greenhouse.io/robotsandpencils/jobs/123"
        full_description = " ".join(
            [
                "Responsibilities include Python SQL Snowflake AWS data "
                "pipelines and analytics engineering requirements."
            ] * 20
        )

        insert_raw_job(
            "linkedin-robots",
            source="linkedin",
            title="Senior Data Engineer",
            company_name="Robots & Pencils",
            location="United States (Remote)",
            description=full_description,
            apply_url="https://www.linkedin.com/jobs/view/111/",
        )
        insert_attempt(
            "linkedin-robots",
            source="linkedin",
            status="enriched",
            official_status=OFFICIAL_FOUND_VERIFIED,
            official_url=official_url,
            verified_posting_key=verified_key,
            description_hash_value=description_hash(full_description),
            official_resolved_title="Senior Data Engineer",
            official_resolved_company="Robots and Pencils",
            official_resolved_location="Remote only, United States",
        )
        insert_raw_job(
            "wellfound-robots",
            source="wellfound",
            title="Sr. Data Engineer",
            company_name="Robots and Pencils",
            location="Remote only, United States",
            description="short alert summary",
            apply_url=(
                "https://wellfound.com/jobs?"
                "job_listing_slug=222-sr-data-engineer"
            ),
        )

        jobs = load_jobs_to_enrich(
            limit=100,
            minimum_words=80,
            source=None,
            retry_failed=False,
            force=False,
        )

        selected_jobs_by_key = {
            job["record_key"]
            for job in jobs
        }

        assert "wellfound-robots" in selected_jobs_by_key

        selected = next(
            job
            for job in jobs
            if job["record_key"] == "wellfound-robots"
        )
        assert selected["known_official_candidate_status"] == (
            "single_candidate_needs_verification"
        )
        assert selected["known_official_candidate_key"] == verified_key

        with database.get_connection() as connection:
            raw = connection.execute(
                """
                SELECT description
                FROM raw_jobs
                WHERE record_key = 'wellfound-robots'
                """
            ).fetchone()
            attempt = connection.execute(
                """
                SELECT verified_posting_key
                FROM job_enrichment_attempts
                WHERE record_key = 'wellfound-robots'
                """
            ).fetchone()

        assert raw is not None
        assert raw[0] == "short alert summary"
        assert attempt is None

    with_temp_database(scenario)


def test_ambiguous_fuzzy_candidates_do_not_reuse_description() -> None:
    def scenario() -> None:
        full_description = " ".join(
            [
                "Responsibilities include Python SQL Snowflake AWS data "
                "pipelines and analytics engineering requirements."
            ] * 20
        )

        for record_key, verified_key, official_url in (
            (
                "linkedin-robots-111",
                "greenhouse:111",
                "https://boards.greenhouse.io/example/jobs/111",
            ),
            (
                "linkedin-robots-222",
                "greenhouse:222",
                "https://boards.greenhouse.io/example/jobs/222",
            ),
        ):
            insert_raw_job(
                record_key,
                source="linkedin",
                title="Senior Data Engineer",
                company_name="Example Co",
                location="Remote US",
                description=full_description,
                apply_url=f"https://www.linkedin.com/jobs/view/{record_key}/",
            )
            insert_attempt(
                record_key,
                source="linkedin",
                status="enriched",
                official_status=OFFICIAL_FOUND_VERIFIED,
                official_url=official_url,
                verified_posting_key=verified_key,
                description_hash_value=description_hash(full_description),
                official_resolved_title="Senior Data Engineer",
                official_resolved_company="Example Co",
                official_resolved_location="Remote US",
            )

        insert_raw_job(
            "wellfound-example",
            source="wellfound",
            title="Sr. Data Engineer",
            company_name="Example Co",
            location="United States (Remote)",
            description="short alert summary",
            apply_url=(
                "https://wellfound.com/jobs?"
                "job_listing_slug=999-sr-data-engineer"
            ),
        )

        jobs = load_jobs_to_enrich(
            limit=100,
            minimum_words=80,
            source=None,
            retry_failed=False,
            force=False,
        )
        selected = next(
            job
            for job in jobs
            if job["record_key"] == "wellfound-example"
        )

        assert selected["known_official_candidate_status"] == "ambiguous"
        assert selected["known_official_candidate_count"] == 2

        with database.get_connection() as connection:
            raw = connection.execute(
                """
                SELECT description
                FROM raw_jobs
                WHERE record_key = 'wellfound-example'
                """
            ).fetchone()
            attempt = connection.execute(
                """
                SELECT verified_posting_key
                FROM job_enrichment_attempts
                WHERE record_key = 'wellfound-example'
                """
            ).fetchone()

        assert raw is not None
        assert raw[0] == "short alert summary"
        assert attempt is None

    with_temp_database(scenario)


def test_authoritative_official_url_reuses_description_offline() -> None:
    def scenario() -> None:
        verified_key = "greenhouse:123"
        official_url = "https://boards.greenhouse.io/robotsandpencils/jobs/123"
        full_description = " ".join(
            [
                "Responsibilities include Python SQL Snowflake AWS data "
                "pipelines and analytics engineering requirements."
            ] * 20
        )

        insert_raw_job(
            "linkedin-robots",
            source="linkedin",
            title="Senior Data Engineer",
            company_name="Robots & Pencils",
            location="United States (Remote)",
            description=full_description,
            apply_url="https://www.linkedin.com/jobs/view/111/",
        )
        insert_attempt(
            "linkedin-robots",
            source="linkedin",
            status="enriched",
            official_status=OFFICIAL_FOUND_VERIFIED,
            official_url=official_url,
            verified_posting_key=verified_key,
            description_hash_value=description_hash(full_description),
            official_resolved_title="Senior Data Engineer",
            official_resolved_company="Robots and Pencils",
            official_resolved_location="Remote only, United States",
        )
        insert_raw_job(
            "greenhouse-robots",
            source="greenhouse",
            title="Sr. Data Engineer",
            company_name="Robots and Pencils",
            location="Remote only, United States",
            description="short alert summary",
            apply_url=official_url,
        )

        jobs = load_jobs_to_enrich(
            limit=100,
            minimum_words=80,
            source=None,
            retry_failed=False,
            force=False,
        )

        assert "greenhouse-robots" not in [
            job["record_key"]
            for job in jobs
        ]

        with database.get_connection() as connection:
            raw = connection.execute(
                """
                SELECT description
                FROM raw_jobs
                WHERE record_key = 'greenhouse-robots'
                """
            ).fetchone()
            attempt = connection.execute(
                """
                SELECT
                    verified_posting_key,
                    description_hash,
                    matched_prior_record_key,
                    matched_prior_source,
                    reused_description
                FROM job_enrichment_attempts
                WHERE record_key = 'greenhouse-robots'
                """
            ).fetchone()

        assert raw is not None
        assert len(raw[0].split()) >= 80
        assert attempt == (
            verified_key,
            description_hash(full_description),
            "linkedin-robots",
            "linkedin",
            True,
        )

    with_temp_database(scenario)


def test_historical_candidate_page_alone_does_not_verify_source() -> None:
    def scenario() -> None:
        original_resolve = enrich_jobs.resolve_official_job
        verified_key = "greenhouse:123"
        official_url = "https://boards.greenhouse.io/robotsandpencils/jobs/123"
        full_description = " ".join(
            [
                "Responsibilities include Python SQL Snowflake AWS data "
                "pipelines and analytics engineering requirements."
            ] * 20
        )

        insert_raw_job(
            "linkedin-robots",
            source="linkedin",
            title="Senior Data Engineer",
            company_name="Robots & Pencils",
            location="United States (Remote)",
            description=full_description,
            apply_url="https://www.linkedin.com/jobs/view/111/",
        )
        insert_attempt(
            "linkedin-robots",
            source="linkedin",
            status="enriched",
            official_status=OFFICIAL_FOUND_VERIFIED,
            official_url=official_url,
            verified_posting_key=verified_key,
            description_hash_value=description_hash(full_description),
            official_resolved_title="Senior Data Engineer",
            official_resolved_company="Robots and Pencils",
            official_resolved_location="Remote only, United States",
        )
        insert_raw_job(
            "wellfound-robots",
            source="wellfound",
            title="Sr. Data Engineer",
            company_name="Robots and Pencils",
            location="Remote only, United States",
            description="short alert summary",
            apply_url=(
                "https://wellfound.com/jobs?"
                "job_listing_slug=222-sr-data-engineer"
            ),
        )

        jobs = load_jobs_to_enrich(
            limit=100,
            minimum_words=80,
            source=None,
            retry_failed=False,
            force=False,
        )
        job = next(
            job
            for job in jobs
            if job["record_key"] == "wellfound-robots"
        )

        assert job["known_official_candidate_status"] == (
            "single_candidate_needs_verification"
        )

        def fake_resolve(job: dict, client: httpx.Client):
            return OfficialJobResolutionResult(
                status=OFFICIAL_NOT_FOUND,
                official_job_url=None,
                official_url_source=None,
                official_url_resolved_at=datetime.now(timezone.utc),
                official_url_confidence=None,
                official_url_validation_reason="not found",
                resolved_title=None,
                resolved_company=None,
                resolved_location=None,
            )

        enrich_jobs.resolve_official_job = fake_resolve

        try:
            with mock_client(
                {
                    job["apply_url"]: httpx.Response(404),
                    official_url: httpx.Response(
                        200,
                        text=job_posting_html(
                            title="Senior Data Engineer",
                            company="Robots and Pencils",
                            location="Remote only, United States",
                            description=full_description,
                        ),
                        headers={"content-type": "text/html"},
                    ),
                }
            ) as client:
                summary = process_enrichment_batch(
                    [job],
                    client,
                )
        finally:
            enrich_jobs.resolve_official_job = original_resolve

        assert summary.descriptions_updated == 0

        with database.get_connection() as connection:
            attempt = connection.execute(
                """
                SELECT
                    verified_posting_key,
                    official_job_url,
                    reused_description,
                    matched_prior_record_key
                FROM job_enrichment_attempts
                WHERE record_key = 'wellfound-robots'
                """
            ).fetchone()

        assert attempt == (
            None,
            None,
            False,
            None,
        )

    with_temp_database(scenario)


def test_source_redirect_to_same_key_verifies_known_candidate() -> None:
    def scenario() -> None:
        verified_key = "greenhouse:123"
        official_url = "https://boards.greenhouse.io/robotsandpencils/jobs/123"
        full_description = " ".join(
            [
                "Responsibilities include Python SQL Snowflake AWS data "
                "pipelines and analytics engineering requirements."
            ] * 20
        )

        insert_raw_job(
            "linkedin-robots",
            source="linkedin",
            title="Senior Data Engineer",
            company_name="Robots & Pencils",
            location="United States (Remote)",
            description=full_description,
            apply_url="https://www.linkedin.com/jobs/view/111/",
        )
        insert_attempt(
            "linkedin-robots",
            source="linkedin",
            status="enriched",
            official_status=OFFICIAL_FOUND_VERIFIED,
            official_url=official_url,
            verified_posting_key=verified_key,
            description_hash_value=description_hash(full_description),
            official_resolved_title="Senior Data Engineer",
            official_resolved_company="Robots and Pencils",
            official_resolved_location="Remote only, United States",
        )
        insert_raw_job(
            "wellfound-robots",
            source="wellfound",
            title="Sr. Data Engineer",
            company_name="Robots and Pencils",
            location="Remote only, United States",
            description="short alert summary",
            apply_url=(
                "https://wellfound.com/jobs?"
                "job_listing_slug=222-sr-data-engineer"
            ),
        )

        job = next(
            job
            for job in load_jobs_to_enrich(
                limit=100,
                minimum_words=80,
                source=None,
                retry_failed=False,
                force=False,
            )
            if job["record_key"] == "wellfound-robots"
        )

        with mock_client(
            {
                job["apply_url"]: httpx.Response(
                    302,
                    headers={"location": official_url},
                ),
                official_url: httpx.Response(
                    200,
                    text=job_posting_html(
                        title="Senior Data Engineer",
                        company="Robots and Pencils",
                        location="Remote only, United States",
                        description=full_description,
                    ),
                    headers={"content-type": "text/html"},
                ),
            }
        ) as client:
            summary = process_enrichment_batch(
                [job],
                client,
            )

        assert summary.descriptions_updated == 1

        with database.get_connection() as connection:
            attempt = connection.execute(
                """
                SELECT
                    verified_posting_key,
                    official_job_url,
                    reused_description,
                    matched_prior_record_key
                FROM job_enrichment_attempts
                WHERE record_key = 'wellfound-robots'
                """
            ).fetchone()

        assert attempt == (
            verified_key,
            official_url,
            False,
            "linkedin-robots",
        )

    with_temp_database(scenario)


def test_source_redirect_to_different_key_overrides_fuzzy_candidate() -> None:
    def scenario() -> None:
        historical_url = "https://boards.greenhouse.io/example/jobs/111"
        new_url = "https://boards.greenhouse.io/example/jobs/222"
        full_description = " ".join(
            [
                "Responsibilities include Python SQL Snowflake AWS data "
                "pipelines and analytics engineering requirements."
            ] * 20
        )

        insert_raw_job(
            "linkedin-example",
            source="linkedin",
            title="Senior Data Engineer",
            company_name="Example Co",
            location="Remote US",
            description=full_description,
            apply_url="https://www.linkedin.com/jobs/view/111/",
        )
        insert_attempt(
            "linkedin-example",
            source="linkedin",
            status="enriched",
            official_status=OFFICIAL_FOUND_VERIFIED,
            official_url=historical_url,
            verified_posting_key="greenhouse:111",
            description_hash_value=description_hash(full_description),
            official_resolved_title="Senior Data Engineer",
            official_resolved_company="Example Co",
            official_resolved_location="Remote US",
        )
        insert_raw_job(
            "wellfound-example",
            source="wellfound",
            title="Sr. Data Engineer",
            company_name="Example Co",
            location="United States (Remote)",
            description="short alert summary",
            apply_url=(
                "https://wellfound.com/jobs?"
                "job_listing_slug=222-sr-data-engineer"
            ),
        )

        job = next(
            job
            for job in load_jobs_to_enrich(
                limit=100,
                minimum_words=80,
                source=None,
                retry_failed=False,
                force=False,
            )
            if job["record_key"] == "wellfound-example"
        )

        with mock_client(
            {
                job["apply_url"]: httpx.Response(
                    302,
                    headers={"location": new_url},
                ),
                new_url: httpx.Response(
                    200,
                    text=job_posting_html(
                        title="Senior Data Engineer",
                        company="Example Co",
                        location="Remote US",
                        description=full_description,
                    ),
                    headers={"content-type": "text/html"},
                ),
            }
        ) as client:
            summary = process_enrichment_batch(
                [job],
                client,
            )

        assert summary.descriptions_updated == 1

        with database.get_connection() as connection:
            attempt = connection.execute(
                """
                SELECT
                    verified_posting_key,
                    official_job_url,
                    reused_description,
                    matched_prior_record_key
                FROM job_enrichment_attempts
                WHERE record_key = 'wellfound-example'
                """
            ).fetchone()

        assert attempt == (
            "greenhouse:222",
            new_url,
            False,
            None,
        )

    with_temp_database(scenario)


def test_authoritative_key_requires_verified_official_status() -> None:
    official_url = "https://boards.greenhouse.io/example/jobs/111"

    assert authoritative_verified_key_for_job(
        {
            "official_job_url": official_url,
            "official_url_status": OFFICIAL_FOUND_VERIFIED,
        }
    ) == "greenhouse:111"

    for status in (OFFICIAL_NOT_FOUND, OFFICIAL_BLOCKED, "AMBIGUOUS", None):
        assert authoritative_verified_key_for_job(
            {
                "official_job_url": official_url,
                "official_url_status": status,
            }
        ) == ""


def test_authoritative_key_requires_accepted_resolved_candidate() -> None:
    official_url = "https://boards.greenhouse.io/example/jobs/111"

    assert authoritative_verified_key_for_job(
        {
            "resolved_candidate_url": official_url,
            "previous_status": "enriched",
            "identity_confidence": 0.91,
        }
    ) == "greenhouse:111"
    assert authoritative_verified_key_for_job(
        {
            "resolved_candidate_url": official_url,
            "previous_status": "resolution_rejected",
            "identity_confidence": 0.91,
        }
    ) == ""


def test_direct_ats_source_apply_url_is_authoritative() -> None:
    assert authoritative_verified_key_for_job(
        {
            "source": "greenhouse",
            "apply_url": "https://boards.greenhouse.io/example/jobs/111",
        }
    ) == "greenhouse:111"


def main() -> None:
    test_previous_failed_lensa_attempt_with_null_official_status_is_selected()
    test_found_verified_is_not_selected_again_for_official_resolution()
    test_official_not_found_recently_is_in_cooldown()
    test_official_not_found_after_cooldown_is_selected()
    test_never_attempted_relevant_job_beats_repeated_failed_job()
    test_default_batch_can_select_more_than_five_jobs()
    test_rank_21_linkedin_job_is_in_default_ui_enrichment_batch()
    test_batch_continues_after_individual_enrichment_failure()
    test_full_description_lensa_without_official_url_is_selected()
    test_direct_greenhouse_full_description_is_not_selected()
    test_enriched_aggregator_page_still_triggers_official_resolution_when_missing()
    test_monzo_historical_lensa_record_resolves_official_url_without_refetching_lensa()
    test_needs_official_resolution_direct_function()
    test_glassdoor_digest_without_description_is_selected_for_resolution()
    test_linkedin_missing_description_is_selected_for_official_resolution()
    test_indeed_incomplete_description_is_selected_for_official_resolution()
    test_unknown_source_missing_description_can_use_official_resolution()
    test_linkedin_incomplete_direct_fetch_uses_official_fallback()
    test_direct_greenhouse_valid_description_skips_official_search()
    test_failed_official_resolution_cooldown_applies_to_linkedin()
    test_same_duplicate_from_multiple_sources_is_selected_once()
    test_glassdoor_full_description_is_saved()
    test_glassdoor_blocked_page_uses_verified_official_description()
    test_glassdoor_blocked_without_official_match_stays_unscored()
    test_glassdoor_failed_resolution_retries_after_cooldown()
    test_glassdoor_wrong_company_description_is_rejected()
    test_fuzzy_historical_duplicate_does_not_reuse_before_verification()
    test_ambiguous_fuzzy_candidates_do_not_reuse_description()
    test_authoritative_official_url_reuses_description_offline()
    test_historical_candidate_page_alone_does_not_verify_source()
    test_source_redirect_to_same_key_verifies_known_candidate()
    test_source_redirect_to_different_key_overrides_fuzzy_candidate()
    test_authoritative_key_requires_verified_official_status()
    test_authoritative_key_requires_accepted_resolved_candidate()
    test_direct_ats_source_apply_url_is_authoritative()
    print("Official enrichment retry tests passed.")


if __name__ == "__main__":
    main()
