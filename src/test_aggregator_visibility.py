from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

from src import database
from src.enrich_jobs import load_jobs_to_enrich
from src.enrichment.official_job_resolver import (
    OFFICIAL_AMBIGUOUS,
    OFFICIAL_BLOCKED,
    OFFICIAL_ERROR,
    OFFICIAL_FOUND_VERIFIED,
    OFFICIAL_NOT_FOUND,
)
from src.repositories.recommendation_repository import (
    count_unscored_candidate_jobs,
    initialize_job_eligibility_table,
    load_all_jobs,
    load_candidate_jobs,
)
from src.ui.job_links import select_job_open_target
from src.ui.pagination import paginate_items


DESCRIPTION = " ".join(
    [
        "Responsibilities include building analytics models, SQL pipelines,"
        " dbt transformations, stakeholder reporting, and reliable data products."
    ]
    * 12
)


def with_temp_database(callback) -> None:
    old_data_dir = database.DATA_DIR
    old_database_path = database.DATABASE_PATH

    with TemporaryDirectory() as temp_dir:
        database.DATA_DIR = Path(temp_dir)
        database.DATABASE_PATH = Path(temp_dir) / "jobs.duckdb"

        try:
            database.initialize_database()
            initialize_job_eligibility_table()
            create_supporting_tables()
            callback()
        finally:
            database.DATA_DIR = old_data_dir
            database.DATABASE_PATH = old_database_path


def create_supporting_tables() -> None:
    with database.get_connection() as connection:
        connection.execute(
            """
            ALTER TABLE raw_jobs
            ADD COLUMN IF NOT EXISTS description_updated_at TIMESTAMPTZ
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS job_matches (
                record_key VARCHAR NOT NULL,
                search_id VARCHAR NOT NULL,
                search_title VARCHAR NOT NULL,
                match_score INTEGER NOT NULL,
                title_score INTEGER NOT NULL,
                location_score INTEGER NOT NULL,
                keyword_score INTEGER NOT NULL,
                freshness_score INTEGER NOT NULL,
                matched_keywords VARCHAR,
                excluded_keywords VARCHAR,
                reasons VARCHAR,
                is_recommended BOOLEAN,
                needs_review BOOLEAN
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS resume_job_scores (
                resume_hash VARCHAR NOT NULL,
                record_key VARCHAR NOT NULL,
                overall_score INTEGER NOT NULL,
                recommendation VARCHAR NOT NULL,
                title_fit INTEGER NOT NULL,
                skills_fit INTEGER NOT NULL,
                experience_fit INTEGER NOT NULL,
                seniority_fit INTEGER NOT NULL,
                industry_fit INTEGER NOT NULL,
                location_fit INTEGER NOT NULL,
                confidence VARCHAR NOT NULL,
                matching_strengths VARCHAR,
                hard_requirements_missing VARCHAR,
                preferred_qualifications_missing VARCHAR,
                risk_factors VARCHAR,
                summary VARCHAR,
                description_word_count INTEGER NOT NULL,
                model_name VARCHAR NOT NULL,
                prompt_version VARCHAR NOT NULL,
                description_complete BOOLEAN NOT NULL DEFAULT true,
                scored_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        connection.execute("CREATE SCHEMA IF NOT EXISTS analytics")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS analytics.mart_job_recommendations (
                canonical_job_key VARCHAR,
                resume_hash VARCHAR,
                ai_prompt_version VARCHAR,
                ai_score INTEGER,
                recommendation VARCHAR,
                confidence VARCHAR,
                title_fit INTEGER,
                skills_fit INTEGER,
                experience_fit INTEGER,
                seniority_fit INTEGER,
                industry_fit INTEGER,
                location_fit INTEGER,
                matching_strengths VARCHAR,
                hard_requirements_missing VARCHAR,
                preferred_qualifications_missing VARCHAR,
                risk_factors VARCHAR,
                summary VARCHAR,
                description_word_count INTEGER,
                description_complete BOOLEAN,
                has_incomplete_description BOOLEAN,
                ai_scored_at TIMESTAMPTZ
            )
            """
        )


def seed_job(
    record_key: str,
    source: str,
    title: str = "Senior Analytics Engineer",
    company_name: str = "Example Co",
    apply_url: str | None = None,
) -> None:
    apply_url = apply_url or f"https://example.com/jobs/{record_key}"

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
                salary_text,
                description,
                apply_url
            )
            VALUES (?, ?, ?, ?, ?, 'Remote', NULL, ?, ?)
            """,
            [
                record_key,
                record_key,
                source,
                title,
                company_name,
                DESCRIPTION,
                apply_url,
            ],
        )
        connection.execute(
            """
            INSERT INTO job_matches (
                record_key,
                search_id,
                search_title,
                match_score,
                title_score,
                location_score,
                keyword_score,
                freshness_score,
                matched_keywords,
                excluded_keywords,
                reasons,
                is_recommended,
                needs_review
            )
            VALUES (?, 'search-1', 'Analytics Engineer', 90, 90, 100, 90, 80,
                    '[]', '[]', '[]', true, false)
            """,
            [record_key],
        )


def seed_enrichment_attempt(
    record_key: str,
    source: str,
    official_status: str | None,
    official_url: str | None,
) -> None:
    with database.get_connection() as connection:
        connection.execute(
            """
            INSERT INTO job_enrichment_attempts (
                record_key,
                source,
                requested_url,
                final_url,
                status,
                description_word_count,
                attempt_count,
                official_job_url,
                official_url_status,
                official_url_source
            )
            VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
            """
            ,
            [
                record_key,
                source,
                f"https://lensa.com/jobs/{record_key}",
                f"https://lensa.com/jobs/{record_key}",
                "blocked" if official_status == OFFICIAL_BLOCKED else "enriched",
                len(DESCRIPTION.split()),
                official_url,
                official_status,
                "greenhouse" if official_url else None,
            ],
        )


def seed_lensa(
    record_key: str,
    official_status: str | None,
    official_url: str | None = None,
    apply_url: str | None = None,
) -> None:
    seed_job(
        record_key=record_key,
        source="lensa",
        title="Sr Analytics Engineer",
        company_name="Aceable",
        apply_url=apply_url or f"https://lensa.com/jobs/{record_key}",
    )

    if official_status is not None:
        seed_enrichment_attempt(
            record_key=record_key,
            source="lensa",
            official_status=official_status,
            official_url=official_url,
        )


def review_record_keys() -> list[str]:
    return [
        job["record_key"]
        for job in load_all_jobs(resume_hash="")
    ]


def scoring_record_keys() -> list[str]:
    return [
        job["record_key"]
        for job in load_candidate_jobs(
            resume_hash="resume-1",
            model_name="model-1",
            limit=100,
            minimum_rule_score=0,
        )
    ]


def test_unresolved_lensa_is_stored_and_enrichment_eligible_but_not_reviewable() -> None:
    def scenario() -> None:
        seed_lensa(
            record_key="aceable-null",
            official_status=None,
        )

        with database.get_connection() as connection:
            raw_count = connection.execute(
                "SELECT COUNT(*) FROM raw_jobs WHERE record_key = 'aceable-null'"
            ).fetchone()[0]

        enrichment_jobs = load_jobs_to_enrich(
            limit=10,
            minimum_words=40,
            source=None,
            retry_failed=False,
            force=False,
        )

        assert raw_count == 1
        assert [
            job["record_key"]
            for job in enrichment_jobs
        ] == ["aceable-null"]
        assert review_record_keys() == []

    with_temp_database(scenario)


def test_lensa_non_verified_statuses_are_hidden() -> None:
    def scenario() -> None:
        for status in (
            OFFICIAL_BLOCKED,
            OFFICIAL_NOT_FOUND,
            OFFICIAL_ERROR,
            OFFICIAL_AMBIGUOUS,
        ):
            seed_lensa(
                record_key=f"aceable-{status.lower()}",
                official_status=status,
            )

        assert review_record_keys() == []

    with_temp_database(scenario)


def test_verified_lensa_with_official_greenhouse_url_is_visible_and_opens_official() -> None:
    def scenario() -> None:
        official_url = "https://boards.greenhouse.io/aceable/jobs/123"
        seed_lensa(
            record_key="aceable-verified",
            official_status=OFFICIAL_FOUND_VERIFIED,
            official_url=official_url,
        )

        jobs = load_all_jobs(resume_hash="")
        target = select_job_open_target(jobs[0])

        assert [
            job["record_key"]
            for job in jobs
        ] == ["aceable-verified"]
        assert target.label == "Open official job"
        assert target.url == official_url

    with_temp_database(scenario)


def test_verified_lensa_with_lensa_official_url_is_hidden() -> None:
    def scenario() -> None:
        seed_lensa(
            record_key="aceable-bad-verified",
            official_status=OFFICIAL_FOUND_VERIFIED,
            official_url="https://lensa.com/jobs/aceable-bad-verified",
        )

        assert review_record_keys() == []

    with_temp_database(scenario)


def test_direct_jobs_remain_visible() -> None:
    def scenario() -> None:
        seed_job(
            record_key="greenhouse-direct",
            source="greenhouse",
            apply_url="https://boards.greenhouse.io/example/jobs/1",
        )
        seed_job(
            record_key="careers-direct",
            source="company",
            apply_url="https://example.com/careers/jobs/2",
        )

        assert set(review_record_keys()) == {
            "greenhouse-direct",
            "careers-direct",
        }

    with_temp_database(scenario)


def test_visibility_counts_pagination_and_scoring_ignore_unresolved_aggregators() -> None:
    def scenario() -> None:
        for index in range(10):
            seed_job(
                record_key=f"direct-{index}",
                source="greenhouse",
                apply_url=f"https://boards.greenhouse.io/example/jobs/{index}",
            )

        for index in range(20):
            seed_lensa(
                record_key=f"hidden-lensa-{index}",
                official_status=None,
            )

        for index in range(5):
            seed_lensa(
                record_key=f"verified-lensa-{index}",
                official_status=OFFICIAL_FOUND_VERIFIED,
                official_url=f"https://boards.greenhouse.io/aceable/jobs/{index}",
            )

        review_jobs = load_all_jobs(resume_hash="")
        page = paginate_items(
            review_jobs,
            page=1,
            page_size=25,
        )
        scoring_jobs = load_candidate_jobs(
            resume_hash="resume-1",
            model_name="model-1",
            limit=100,
            minimum_rule_score=0,
        )
        unscored_count = count_unscored_candidate_jobs(
            resume_hash="resume-1",
            model_name="model-1",
            minimum_rule_score=0,
        )

        assert len(review_jobs) == 15
        assert len(page.items) == 15
        assert len(scoring_jobs) == 15
        assert unscored_count == 15
        assert all(
            not job["record_key"].startswith("hidden-lensa")
            for job in review_jobs + scoring_jobs
        )

    with_temp_database(scenario)


def test_unresolved_lensa_open_target_has_no_url() -> None:
    target = select_job_open_target(
        {
            "source": "lensa",
            "apply_url": "https://lensa.com/jobs/generic",
            "official_url_status": OFFICIAL_BLOCKED,
            "official_job_url": None,
        }
    )

    assert target.url is None


def main() -> None:
    test_unresolved_lensa_is_stored_and_enrichment_eligible_but_not_reviewable()
    test_lensa_non_verified_statuses_are_hidden()
    test_verified_lensa_with_official_greenhouse_url_is_visible_and_opens_official()
    test_verified_lensa_with_lensa_official_url_is_hidden()
    test_direct_jobs_remain_visible()
    test_visibility_counts_pagination_and_scoring_ignore_unresolved_aggregators()
    test_unresolved_lensa_open_target_has_no_url()
    print("Aggregator visibility tests passed.")


if __name__ == "__main__":
    main()
