import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from src import database
from src.repositories.recommendation_repository import (
    apply_admission_gate,
    build_posting_identity,
    calculate_enrichment_priority,
    count_unscored_candidate_jobs,
    derive_fit_priority,
    description_state,
    exact_posting_identity,
    extract_embedded_destination_url,
    load_candidate_jobs,
    load_latest_cached_resume_hash,
    normalize_apply_url_for_identity,
    resolve_redirect_final_url,
    resolve_display_resume_hash,
    save_job_eligibility_decision,
    ui_duplicate_key,
    update_application_status,
)
from src.ai.job_eligibility import (
    ELIGIBILITY_PROMPT_VERSION,
    JobEligibilityAnalysis,
    JobEligibilityDecision,
)
from src.ai.resume_matcher import MATCHER_PROMPT_VERSION
from src.job_title_filter import (
    excluded_job_title_reason,
    is_excluded_job_title,
)


def seed_job(
    record_key: str,
    description: str | None,
    title: str | None = None,
    source: str = "test",
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
                salary_text,
                description,
                apply_url
            )
            VALUES (?, ?, ?, ?, 'Example Co', 'Remote', NULL, ?, ?)
            """,
            [
                record_key,
                record_key,
                source,
                title or f"Data Engineer {record_key}",
                description,
                f"https://example.com/{record_key}",
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
            VALUES (?, 'search-1', 'Data Engineer', 90, 90, 100, 90, 80,
                    '[]', '[]', '[]', true, false)
            """,
            [record_key],
        )


def seed_status_job(
    record_key: str,
    job_fingerprint: str,
    title: str,
    company_name: str,
    apply_url: str,
    source: str = "jobright",
    source_job_id: str | None = None,
) -> None:
    with database.get_connection() as connection:
        connection.execute(
            """
            INSERT INTO raw_jobs (
                record_key,
                job_fingerprint,
                source,
                source_job_id,
                title,
                company_name,
                location,
                salary_text,
                description,
                apply_url
            )
            VALUES (?, ?, ?, ?, ?, ?, 'Remote', NULL, ?, ?)
            """,
            [
                record_key,
                job_fingerprint,
                source,
                source_job_id,
                title,
                company_name,
                " ".join(
                    [
                        "Responsibilities include Python SQL Snowflake "
                        "data pipelines and requirements include "
                        "analytics engineering experience."
                    ] * 20
                ),
                apply_url,
            ],
        )


def status_for(record_key: str) -> str | None:
    with database.get_connection() as connection:
        result = connection.execute(
            """
            SELECT status
            FROM application_status
            WHERE record_key = ?
            """,
            [record_key],
        ).fetchone()

    return str(result[0]) if result else None


def seed_resume_profile(
    resume_hash: str = "resume-1",
) -> None:
    with database.get_connection() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS resume_profiles (
                resume_hash VARCHAR PRIMARY KEY,
                filename VARCHAR NOT NULL,
                model_name VARCHAR NOT NULL,
                profile_json VARCHAR NOT NULL,
                created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        connection.execute(
            """
            INSERT OR REPLACE INTO resume_profiles (
                resume_hash,
                filename,
                model_name,
                profile_json
            )
            VALUES (?, ?, ?, ?)
            """,
            [
                resume_hash,
                "resume.pdf",
                "model-1",
                json.dumps(
                    {
                        "target_roles": [
                            "Senior Data Engineer",
                            "Analytics Engineer",
                            "BI Developer",
                        ],
                        "seniority": "senior",
                        "years_of_relevant_experience": 12,
                        "production_skills": [
                            "Python",
                            "SQL",
                            "Snowflake",
                            "AWS",
                        ],
                        "project_skills": [
                            "dbt",
                        ],
                        "data_engineering_capabilities": [
                            "data pipelines",
                            "analytics models",
                            "data warehouse",
                        ],
                    }
                ),
            ],
        )


def test_candidate_jobs_require_complete_descriptions() -> None:
    old_data_dir = database.DATA_DIR
    old_database_path = database.DATABASE_PATH

    with TemporaryDirectory() as temp_dir:
        database.DATA_DIR = Path(temp_dir)
        database.DATABASE_PATH = Path(temp_dir) / "jobs.duckdb"

        try:
            database.initialize_database()

            with database.get_connection() as connection:
                connection.execute(
                    """
                    ALTER TABLE raw_jobs
                    ADD COLUMN IF NOT EXISTS description_updated_at TIMESTAMPTZ
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE job_matches (
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
                    CREATE TABLE resume_job_scores (
                        resume_hash VARCHAR NOT NULL,
                        record_key VARCHAR NOT NULL,
                        model_name VARCHAR NOT NULL,
                        prompt_version VARCHAR NOT NULL,
                        description_complete BOOLEAN NOT NULL DEFAULT true,
                        scored_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE resume_profiles (
                        resume_hash VARCHAR PRIMARY KEY,
                        filename VARCHAR NOT NULL,
                        model_name VARCHAR NOT NULL,
                        profile_json VARCHAR NOT NULL,
                        created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
                connection.execute(
                    """
                    INSERT INTO resume_profiles (
                        resume_hash,
                        filename,
                        model_name,
                        profile_json
                    )
                    VALUES (?, ?, ?, ?)
                    """,
                    [
                        "resume-1",
                        "resume.pdf",
                        "model-1",
                        json.dumps(
                            {
                                "target_roles": [
                                    "Senior Data Engineer",
                                    "Analytics Engineer",
                                ],
                                "seniority": "senior",
                                "years_of_relevant_experience": 12,
                                "production_skills": [
                                    "Python",
                                    "SQL",
                                    "Snowflake",
                                    "AWS",
                                    "dbt",
                                ],
                                "data_engineering_capabilities": [
                                    "data pipelines",
                                    "analytics models",
                                    "data warehouse",
                                ],
                            }
                        ),
                    ],
                )

            seed_job("missing-description", None)
            seed_job("blank-description", "   ")
            seed_job(
                "short-description",
                "Builds reliable data pipelines and analytics models.",
            )
            seed_job(
                "has-description",
                " ".join(
                    [
                        "Responsibilities include building reliable data "
                        "pipelines and analytics models using Python SQL "
                        "Snowflake AWS. Requirements include senior data "
                        "engineering experience."
                    ]
                    * 12
                ),
            )
            seed_job(
                "java-developer",
                " ".join(
                    [
                        "Builds reliable software systems and data integrations"
                    ]
                    * 12
                ),
                title="Java Developer",
            )
            seed_job(
                "power-bi-data-engineer",
                " ".join(
                    [
                        "Builds data pipelines and Power BI semantic models"
                    ]
                    * 12
                ),
                title="Remote Data Engineer III Power BI Expert",
            )
            seed_job(
                "ai-excluded-job",
                " ".join(
                    [
                        "Builds reliable data pipelines and analytics models"
                    ]
                    * 12
                ),
                title="Forward Deployed Engineer",
            )
            seed_job(
                "lensa-scoreable-job",
                " ".join(
                    [
                        "Responsibilities include building reliable data "
                        "pipelines and analytics models. Requirements include "
                        "SQL Snowflake and Python experience."
                    ]
                    * 12
                ),
                source="lensa",
            )

            save_job_eligibility_decision(
                JobEligibilityDecision(
                    canonical_job_key="ai-excluded-job",
                    record_key="ai-excluded-job",
                    resume_hash="resume-1",
                    model_name="model-1",
                    prompt_version=ELIGIBILITY_PROMPT_VERSION,
                    analysis=JobEligibilityAnalysis(
                        decision="exclude",
                        confidence="high",
                        reason="Outside target role lane.",
                        matched_resume_signals=[],
                        missing_or_mismatched_signals=[
                            "Forward deployed role"
                        ],
                    ),
                )
            )

            candidates = load_candidate_jobs(
                resume_hash="resume-1",
                model_name="model-1",
                limit=10,
                minimum_rule_score=0,
                reuse_any_model=True,
            )

            assert {
                candidate["record_key"]
                for candidate in candidates
            } == {
                "has-description",
                "lensa-scoreable-job",
            }
            assert count_unscored_candidate_jobs(
                resume_hash="resume-1",
                model_name="model-1",
                minimum_rule_score=0,
                reuse_any_model=True,
            ) == 2

        finally:
            database.DATA_DIR = old_data_dir
            database.DATABASE_PATH = old_database_path


def test_admission_gate_keeps_target_titles_needing_enrichment() -> None:
    old_data_dir = database.DATA_DIR
    old_database_path = database.DATABASE_PATH

    with TemporaryDirectory() as temp_dir:
        database.DATA_DIR = Path(temp_dir)
        database.DATABASE_PATH = Path(temp_dir) / "jobs.duckdb"

        try:
            database.initialize_database()
            seed_resume_profile()

            jobs = apply_admission_gate(
                jobs=[
                    {
                        "record_key": "missing-jd",
                        "title": "Senior Data Engineer",
                        "company_name": "Example Co",
                        "location": "Remote",
                        "description": None,
                        "raw_description_word_count": 0,
                        "title_classification": "STRONG_MATCH",
                        "application_status": "new",
                    },
                    {
                        "record_key": "valid-lensa",
                        "title": "Senior Analytics Engineer",
                        "company_name": "Example Co",
                        "location": "Remote",
                        "description": None,
                        "raw_description_word_count": 0,
                        "title_classification": "STRONG_MATCH",
                        "application_status": "new",
                        "source": "lensa",
                    },
                ],
                resume_hash="resume-1",
            )

            assert {
                job["record_key"]
                for job in jobs
            } == {
                "missing-jd",
                "valid-lensa",
            }
            assert {
                job["description_state"]
                for job in jobs
            } == {"NEEDS_ENRICHMENT"}

        finally:
            database.DATA_DIR = old_data_dir
            database.DATABASE_PATH = old_database_path


def test_admission_gate_preserves_manual_statuses_for_filtered_titles() -> None:
    old_data_dir = database.DATA_DIR
    old_database_path = database.DATABASE_PATH

    with TemporaryDirectory() as temp_dir:
        database.DATA_DIR = Path(temp_dir)
        database.DATABASE_PATH = Path(temp_dir) / "jobs.duckdb"

        try:
            database.initialize_database()
            seed_resume_profile()

            jobs = apply_admission_gate(
                jobs=[
                    {
                        "record_key": "filtered-new",
                        "title": "Software Engineer",
                        "company_name": "Example Co",
                        "location": "Remote",
                        "description": None,
                        "title_classification": "FILTERED_OUT",
                        "application_status": "new",
                    },
                    {
                        "record_key": "filtered-applied",
                        "title": "Software Engineer",
                        "company_name": "Example Co",
                        "location": "Remote",
                        "description": None,
                        "title_classification": "FILTERED_OUT",
                        "application_status": "applied",
                    },
                    {
                        "record_key": "filtered-removed",
                        "title": "Software Engineer",
                        "company_name": "Example Co",
                        "location": "Remote",
                        "description": None,
                        "title_classification": "FILTERED_OUT",
                        "application_status": "removed",
                    },
                ],
                resume_hash="resume-1",
            )

            assert {
                job["record_key"]
                for job in jobs
            } == {
                "filtered-applied",
                "filtered-removed",
            }

        finally:
            database.DATA_DIR = old_data_dir
            database.DATABASE_PATH = old_database_path


def test_description_state_is_separate_from_fit() -> None:
    assert description_state(
        {
            "raw_description_word_count": 120,
            "enrichment_status": "enriched",
            "description_quality_signals": True,
        }
    ) == "FULL_JD"
    assert description_state(
        {
            "raw_description_word_count": 120,
            "description": " ".join(["navigation chrome"] * 120),
        }
    ) == "PARTIAL_JD"
    assert description_state({"raw_description_word_count": 25}) == (
        "PARTIAL_JD"
    )
    assert description_state({"raw_description_word_count": 0}) == (
        "NEEDS_ENRICHMENT"
    )
    assert description_state(
        {
            "raw_description_word_count": 120,
            "enrichment_status": "resolution_rejected",
        }
    ) == "ENRICHMENT_REJECTED"


def test_fit_priority_uses_current_complete_ai_score() -> None:
    base_job = {
        "description_state": "FULL_JD",
        "description_word_count": 160,
        "description_complete": True,
        "has_incomplete_description": False,
        "ai_prompt_version": MATCHER_PROMPT_VERSION,
        "ai_scored_at": datetime(2026, 10, 1, tzinfo=timezone.utc),
    }

    apply_priority = derive_fit_priority(
        {
            **base_job,
            "ai_score": 95,
            "recommendation": "apply",
        }
    )
    review_priority = derive_fit_priority(
        {
            **base_job,
            "ai_score": 82,
            "recommendation": "review",
        }
    )
    skip_priority = derive_fit_priority(
        {
            **base_job,
            "ai_score": 35,
            "recommendation": "skip",
        }
    )

    assert apply_priority.label == "High"
    assert apply_priority.source == "AI assessment"
    assert review_priority.label == "Medium"
    assert skip_priority.label == "Low"


def test_fit_priority_falls_back_when_ai_score_is_incomplete_or_stale() -> None:
    deterministic_job = {
        "ai_score": 95,
        "recommendation": "apply",
        "description_state": "PARTIAL_JD",
        "description_word_count": 40,
        "description_complete": False,
        "has_incomplete_description": True,
        "title_classification": "STRONG_MATCH",
        "admission_decision": "include",
        "role_family_match": "strong",
    }
    stale_job = {
        **deterministic_job,
        "description_state": "FULL_JD",
        "description_word_count": 160,
        "description_complete": True,
        "has_incomplete_description": False,
        "ai_scored_at": datetime(2026, 9, 1, tzinfo=timezone.utc),
        "description_updated_at": datetime(2026, 10, 1, tzinfo=timezone.utc),
    }

    incomplete_priority = derive_fit_priority(deterministic_job)
    stale_priority = derive_fit_priority(stale_job)
    no_ai_priority = derive_fit_priority(
        {
            "title_classification": "STRONG_MATCH",
            "admission_decision": "include",
            "role_family_match": "strong",
            "description_state": "FULL_JD",
        }
    )

    assert incomplete_priority.source == "Needs enrichment"
    assert stale_priority.source == "Pre-screening"
    assert no_ai_priority.label == "High"
    assert no_ai_priority.source == "Pre-screening"


def test_enrichment_priority_orders_useful_new_jobs_first() -> None:
    now = datetime(2026, 10, 1, tzinfo=timezone.utc)

    def priority(job: dict) -> int:
        return calculate_enrichment_priority(job, now=now).score

    new_strong_recent = {
        "application_status": "new",
        "title_classification": "STRONG_MATCH",
        "raw_description_word_count": 0,
        "discovered_at": now - timedelta(days=1),
        "rule_score": 90,
    }
    old_strong = {
        **new_strong_recent,
        "discovered_at": now - timedelta(days=45),
    }
    new_possible_recent = {
        **new_strong_recent,
        "title_classification": "POSSIBLE_MATCH",
    }
    applied_recent = {
        **new_strong_recent,
        "application_status": "applied",
    }
    removed_recent = {
        **new_strong_recent,
        "application_status": "removed",
    }

    assert priority(new_strong_recent) > priority(old_strong)
    assert priority(new_strong_recent) > priority(new_possible_recent)
    assert priority(new_possible_recent) > priority(applied_recent)
    assert priority(applied_recent) > priority(removed_recent)


def test_ai_score_cache_uses_exact_posting_not_duplicate_fingerprint() -> None:
    old_data_dir = database.DATA_DIR
    old_database_path = database.DATABASE_PATH

    with TemporaryDirectory() as temp_dir:
        database.DATA_DIR = Path(temp_dir)
        database.DATABASE_PATH = Path(temp_dir) / "jobs.duckdb"

        try:
            database.initialize_database()
            seed_resume_profile()

            with database.get_connection() as connection:
                connection.execute(
                    """
                    ALTER TABLE raw_jobs
                    ADD COLUMN IF NOT EXISTS description_updated_at TIMESTAMPTZ
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE job_matches (
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
                    CREATE TABLE resume_job_scores (
                        resume_hash VARCHAR NOT NULL,
                        record_key VARCHAR NOT NULL,
                        model_name VARCHAR NOT NULL,
                        prompt_version VARCHAR NOT NULL,
                        description_complete BOOLEAN NOT NULL DEFAULT true,
                        scored_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )

            shared_fingerprint = "senior-data-engineer-example-remote"
            description = " ".join(
                [
                    "Responsibilities include Python SQL Snowflake AWS "
                    "data pipelines and requirements include analytics "
                    "engineering experience."
                ] * 20
            )

            seed_status_job(
                record_key="july-posting",
                job_fingerprint=shared_fingerprint,
                title="Senior Data Engineer",
                company_name="Example Co",
                apply_url="https://example.com/jobs?jobId=123",
                source_job_id="123",
            )
            seed_status_job(
                record_key="october-posting",
                job_fingerprint=shared_fingerprint,
                title="Senior Data Engineer",
                company_name="Example Co",
                apply_url="https://example.com/jobs?jobId=456",
                source_job_id="456",
            )

            with database.get_connection() as connection:
                connection.execute(
                    """
                    UPDATE raw_jobs
                    SET description = ?
                    WHERE record_key IN ('july-posting', 'october-posting')
                    """,
                    [description],
                )
                connection.executemany(
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
                    VALUES (?, 'search-1', 'Data Engineer', 90, 90, 100, 90, 80,
                            '[]', '[]', '[]', true, false)
                    """,
                    [
                        ["july-posting"],
                        ["october-posting"],
                    ],
                )
                connection.execute(
                    """
                    INSERT INTO resume_job_scores (
                        resume_hash,
                        record_key,
                        model_name,
                        prompt_version,
                        description_complete,
                        scored_at
                    )
                    VALUES (
                        'resume-1',
                        'july-posting',
                        'model-1',
                        ?,
                        true,
                        CURRENT_TIMESTAMP
                    )
                    """,
                    [MATCHER_PROMPT_VERSION],
                )

            candidates = load_candidate_jobs(
                resume_hash="resume-1",
                model_name="model-1",
                limit=10,
                minimum_rule_score=0,
                reuse_any_model=False,
            )

            assert [
                candidate["record_key"]
                for candidate in candidates
            ] == ["october-posting"]

        finally:
            database.DATA_DIR = old_data_dir
            database.DATABASE_PATH = old_database_path


def test_application_status_uses_exact_posting_identity() -> None:
    old_data_dir = database.DATA_DIR
    old_database_path = database.DATABASE_PATH

    with TemporaryDirectory() as temp_dir:
        database.DATA_DIR = Path(temp_dir)
        database.DATABASE_PATH = Path(temp_dir) / "jobs.duckdb"

        try:
            database.initialize_database()

            shared_fingerprint = "same-title-company-location"

            seed_status_job(
                record_key="original-posting",
                job_fingerprint=shared_fingerprint,
                title="Senior Data Engineer",
                company_name="Example Co",
                apply_url=(
                    "https://jobright.ai/jobs/info/abc123?"
                    "utm_source=email"
                ),
            )
            seed_status_job(
                record_key="exact-duplicate-posting",
                job_fingerprint=shared_fingerprint,
                title="Senior Data Engineer",
                company_name="Example Co",
                apply_url=(
                    "https://jobright.ai/jobs/info/abc123?"
                    "utm_campaign=daily"
                ),
            )
            seed_status_job(
                record_key="reposted-same-title-company",
                job_fingerprint=shared_fingerprint,
                title="Senior Data Engineer",
                company_name="Example Co",
                apply_url=(
                    "https://jobright.ai/jobs/info/new456?"
                    "utm_source=email"
                ),
            )
            seed_status_job(
                record_key="same-title-different-company",
                job_fingerprint="same-title-different-company",
                title="Senior Data Engineer",
                company_name="Other Co",
                apply_url="https://jobright.ai/jobs/info/other789",
            )
            seed_status_job(
                record_key="same-company-different-job",
                job_fingerprint="same-company-different-job",
                title="Analytics Engineer",
                company_name="Example Co",
                apply_url="https://jobright.ai/jobs/info/analytics999",
            )

            assert status_for("original-posting") is None
            assert status_for("exact-duplicate-posting") is None
            assert status_for("reposted-same-title-company") is None

            update_application_status(
                record_key="original-posting",
                status="applied",
            )

            assert status_for("original-posting") == "applied"
            assert status_for("exact-duplicate-posting") == "applied"
            assert status_for("reposted-same-title-company") is None
            assert status_for("same-title-different-company") is None
            assert status_for("same-company-different-job") is None

            update_application_status(
                record_key="exact-duplicate-posting",
                status="removed",
            )

            assert status_for("original-posting") == "removed"
            assert status_for("exact-duplicate-posting") == "removed"
            assert status_for("reposted-same-title-company") is None

        finally:
            database.DATA_DIR = old_data_dir
            database.DATABASE_PATH = old_database_path


def test_apply_url_identity_removes_tracking_parameters_only() -> None:
    first_url = (
        "HTTPS://Example.com/jobs/view?"
        "jobId=123&utm_source=email&utm_medium=alert&ref=newsletter"
    )
    second_url = (
        "https://example.com/jobs/view?"
        "utm_campaign=daily&jobId=123"
    )

    assert normalize_apply_url_for_identity(first_url) == (
        normalize_apply_url_for_identity(second_url)
    )


def test_apply_url_identity_preserves_job_id_parameters() -> None:
    first_url = "https://example.com/jobs/view?jobId=123&utm_source=email"
    second_url = "https://example.com/jobs/view?jobId=456&utm_source=email"

    assert normalize_apply_url_for_identity(first_url) != (
        normalize_apply_url_for_identity(second_url)
    )


def test_apply_url_identity_preserves_greenhouse_job_id() -> None:
    first_url = "https://boards.greenhouse.io/acme/jobs?gh_jid=111&utm_source=email"
    second_url = "https://boards.greenhouse.io/acme/jobs?gh_jid=222&utm_source=email"

    assert normalize_apply_url_for_identity(first_url) != (
        normalize_apply_url_for_identity(second_url)
    )


def test_apply_url_identity_preserves_unknown_parameters() -> None:
    first_url = "https://redirect.example.com/ls/click?upn=abc&utm_source=email"
    second_url = "https://redirect.example.com/ls/click?upn=def&utm_source=email"

    assert normalize_apply_url_for_identity(first_url) != (
        normalize_apply_url_for_identity(second_url)
    )


def test_source_job_id_overrides_url_identity() -> None:
    first_identity = exact_posting_identity(
        source="LinkedIn",
        source_job_id="123",
        apply_url="https://example.com/jobs?jobId=999",
        record_key="record-1",
    )
    second_identity = exact_posting_identity(
        source="linkedin",
        source_job_id="456",
        apply_url="https://example.com/jobs?jobId=999",
        record_key="record-2",
    )

    assert first_identity != second_identity


def test_exact_duplicate_urls_share_status_with_tracking_removed() -> None:
    old_data_dir = database.DATA_DIR
    old_database_path = database.DATABASE_PATH

    with TemporaryDirectory() as temp_dir:
        database.DATA_DIR = Path(temp_dir)
        database.DATABASE_PATH = Path(temp_dir) / "jobs.duckdb"

        try:
            database.initialize_database()

            seed_status_job(
                record_key="first-alert",
                job_fingerprint="same-job",
                title="Senior Data Engineer",
                company_name="Example Co",
                apply_url=(
                    "https://example.com/jobs/view?"
                    "jobId=123&utm_source=email"
                ),
            )
            seed_status_job(
                record_key="second-alert",
                job_fingerprint="same-job",
                title="Senior Data Engineer",
                company_name="Example Co",
                apply_url=(
                    "https://example.com/jobs/view?"
                    "utm_campaign=daily&jobId=123"
                ),
            )

            update_application_status(
                record_key="first-alert",
                status="applied",
            )

            assert status_for("first-alert") == "applied"
            assert status_for("second-alert") == "applied"

        finally:
            database.DATA_DIR = old_data_dir
            database.DATABASE_PATH = old_database_path


def test_linkedin_job_ids_share_only_when_exact() -> None:
    first_identity = exact_posting_identity(
        source="LinkedIn",
        source_job_id=None,
        apply_url="https://www.linkedin.com/jobs/view/4446128599/?trk=email",
        record_key="record-1",
    )
    duplicate_identity = exact_posting_identity(
        source="linkedin",
        source_job_id=None,
        apply_url="https://www.linkedin.com/jobs/view/4446128599/",
        record_key="record-2",
    )
    different_identity = exact_posting_identity(
        source="linkedin",
        source_job_id=None,
        apply_url="https://www.linkedin.com/jobs/view/5555555555/",
        record_key="record-3",
    )

    assert first_identity == duplicate_identity
    assert first_identity != different_identity


def test_embedded_destination_url_unwraps_for_identity() -> None:
    destination = "https://boards.greenhouse.io/acme/jobs/12345?gh_jid=12345"
    redirect_url = (
        "https://email.example.com/ls/click?"
        f"url={destination}&utm_source=email"
    )

    assert extract_embedded_destination_url(redirect_url) == destination
    assert exact_posting_identity(
        source="greenhouse",
        source_job_id=None,
        apply_url=redirect_url,
        record_key="record-1",
    ) == exact_posting_identity(
        source="greenhouse",
        source_job_id=None,
        apply_url=destination,
        record_key="record-2",
    )


def test_unresolved_lensa_redirects_do_not_share_status() -> None:
    first_identity = exact_posting_identity(
        source="lensa",
        source_job_id=None,
        apply_url=(
            "https://sg3email.lensa.com/ls/click?"
            "upn=opaque-a&utm_source=jobalert"
        ),
        record_key="record-1",
    )
    second_identity = exact_posting_identity(
        source="lensa",
        source_job_id=None,
        apply_url=(
            "https://sg3email.lensa.com/ls/click?"
            "upn=opaque-b&utm_source=jobalert"
        ),
        record_key="record-2",
    )

    assert first_identity == "lensa|record:record-1"
    assert second_identity == "lensa|record:record-2"
    assert first_identity != second_identity


def test_unresolved_sendgrid_redirects_do_not_share_status() -> None:
    first_identity = exact_posting_identity(
        source="welcometothejungle",
        source_job_id=None,
        apply_url="https://u9255466.ct.sendgrid.net/ls/click?upn=opaque-a",
        record_key="record-1",
    )
    second_identity = exact_posting_identity(
        source="welcometothejungle",
        source_job_id=None,
        apply_url="https://u9255466.ct.sendgrid.net/ls/click?upn=opaque-b",
        record_key="record-2",
    )

    assert first_identity == "welcometothejungle|record:record-1"
    assert second_identity == "welcometothejungle|record:record-2"
    assert first_identity != second_identity


def test_resolved_redirect_identity_uses_final_destination() -> None:
    first_identity = build_posting_identity(
        source="lensa",
        source_job_id=None,
        apply_url="https://sg3email.lensa.com/ls/click?upn=opaque-a",
        record_key="record-1",
        resolved_apply_url="https://example.com/jobs/view?jobId=123",
    )
    duplicate_identity = build_posting_identity(
        source="lensa",
        source_job_id=None,
        apply_url="https://sg3email.lensa.com/ls/click?upn=opaque-b",
        record_key="record-2",
        resolved_apply_url=(
            "https://example.com/jobs/view?jobId=123&utm_source=email"
        ),
    )
    different_identity = build_posting_identity(
        source="lensa",
        source_job_id=None,
        apply_url="https://sg3email.lensa.com/ls/click?upn=opaque-c",
        record_key="record-3",
        resolved_apply_url="https://example.com/jobs/view?jobId=456",
    )

    assert first_identity.identity == duplicate_identity.identity
    assert first_identity.identity != different_identity.identity
    assert first_identity.identity_type == "platform_job_id"
    assert first_identity.confidence == "high"


def test_http_redirect_resolver_returns_final_url_safely() -> None:
    class FakeResponse:
        def __init__(self, final_url: str) -> None:
            self.final_url = final_url

        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def geturl(self) -> str:
            return self.final_url

    class FakeOpener:
        def open(self, request: object, timeout: float) -> FakeResponse:
            assert timeout == 3.0
            return FakeResponse("https://example.com/jobs/view?jobId=123")

    final_url = resolve_redirect_final_url(
        "https://sg3email.lensa.com/ls/click?upn=opaque-a",
        opener=FakeOpener(),
        timeout_seconds=3.0,
    )

    assert final_url == "https://example.com/jobs/view?jobId=123"


def test_http_redirect_resolver_uses_embedded_destination_before_network() -> None:
    destination = "https://example.com/jobs/view?jobId=123"
    redirect_url = f"https://email.example.com/ls/click?url={destination}"

    assert resolve_redirect_final_url(redirect_url) == destination


def test_different_source_job_ids_never_share_identity() -> None:
    first_identity = exact_posting_identity(
        source="jobright",
        source_job_id="abc123",
        apply_url="https://example.com/jobs/view?jobId=999",
        record_key="record-1",
    )
    second_identity = exact_posting_identity(
        source="jobright",
        source_job_id="def456",
        apply_url="https://example.com/jobs/view?jobId=999",
        record_key="record-2",
    )

    assert first_identity != second_identity


def test_ui_duplicate_key_collapses_same_source_fingerprint() -> None:
    first_job = {
        "source": "linkedin",
        "source_job_id": "4472060729",
        "exact_posting_key": "linkedin|4472060729",
        "duplicate_fingerprint": "same-title-company-location",
        "title": "Data Engineer",
        "company_name": "Example Co",
        "location": "United States (Remote)",
    }
    second_job = {
        "source": "LinkedIn",
        "source_job_id": "4472293631",
        "exact_posting_key": "linkedin|4472293631",
        "duplicate_fingerprint": "same-title-company-location",
        "title": "Data Engineer",
        "company_name": "Example Co",
        "location": "United States (Remote)",
    }

    assert exact_posting_identity(
        source=first_job["source"],
        source_job_id=first_job["source_job_id"],
        apply_url=None,
        record_key="first-record",
    ) != exact_posting_identity(
        source=second_job["source"],
        source_job_id=second_job["source_job_id"],
        apply_url=None,
        record_key="second-record",
    )
    assert ui_duplicate_key(first_job) == ui_duplicate_key(second_job)


def test_ui_duplicate_key_keeps_different_sources_separate() -> None:
    linkedin_job = {
        "source": "linkedin",
        "duplicate_fingerprint": "same-title-company-location",
    }
    glassdoor_job = {
        "source": "glassdoor",
        "duplicate_fingerprint": "same-title-company-location",
    }

    assert ui_duplicate_key(linkedin_job) != ui_duplicate_key(glassdoor_job)


def test_reposted_same_company_title_different_posting_id_stays_new() -> None:
    old_data_dir = database.DATA_DIR
    old_database_path = database.DATABASE_PATH

    with TemporaryDirectory() as temp_dir:
        database.DATA_DIR = Path(temp_dir)
        database.DATABASE_PATH = Path(temp_dir) / "jobs.duckdb"

        try:
            database.initialize_database()

            seed_status_job(
                record_key="july-posting",
                job_fingerprint="same-title-company",
                title="Senior Data Engineer",
                company_name="Example Co",
                apply_url="https://example.com/jobs/view?jobId=123",
            )
            seed_status_job(
                record_key="october-repost",
                job_fingerprint="same-title-company",
                title="Senior Data Engineer",
                company_name="Example Co",
                apply_url="https://example.com/jobs/view?jobId=456",
            )

            update_application_status(
                record_key="july-posting",
                status="applied",
            )

            assert status_for("july-posting") == "applied"
            assert status_for("october-repost") is None

        finally:
            database.DATA_DIR = old_data_dir
            database.DATABASE_PATH = old_database_path


def test_title_exclusion_reasons_are_detected() -> None:
    assert is_excluded_job_title("Oracle Developer 2884 OJO")
    assert is_excluded_job_title("Java Developer")
    assert is_excluded_job_title(
        "Remote Software Engineer III - ML-Driven Asset Analytics"
    )
    assert not is_excluded_job_title(
        "Remote Data Engineer III Power BI & Data Pipelines Expert"
    )
    assert is_excluded_job_title("AEP RTCDP Developer")
    assert is_excluded_job_title("Hi there,")
    assert is_excluded_job_title("[Senior BI Analyst - Remote Dashboards &")
    assert is_excluded_job_title("|")
    assert not is_excluded_job_title("Senior Data Engineer")
    assert excluded_job_title_reason("Java Developer") == (
        "primary job family is a non-target specialist developer role"
    )


def test_display_resume_hash_falls_back_to_latest_cached_profile() -> None:
    old_data_dir = database.DATA_DIR
    old_database_path = database.DATABASE_PATH

    with TemporaryDirectory() as temp_dir:
        database.DATA_DIR = Path(temp_dir)
        database.DATABASE_PATH = Path(temp_dir) / "jobs.duckdb"

        try:
            database.initialize_database()

            with database.get_connection() as connection:
                connection.execute(
                    """
                    CREATE TABLE resume_profiles (
                        resume_hash VARCHAR PRIMARY KEY,
                        filename VARCHAR NOT NULL,
                        model_name VARCHAR NOT NULL,
                        profile_json VARCHAR NOT NULL,
                        created_at TIMESTAMPTZ
                    )
                    """
                )
                connection.execute(
                    """
                    INSERT INTO resume_profiles (
                        resume_hash,
                        filename,
                        model_name,
                        profile_json,
                        created_at
                    )
                    VALUES
                        ('older-resume', 'old.pdf', 'model', '{}',
                         '2026-01-01 00:00:00+00'),
                        ('newer-resume', 'new.pdf', 'model', '{}',
                         '2026-01-02 00:00:00+00')
                    """
                )

            assert load_latest_cached_resume_hash() == "newer-resume"
            assert resolve_display_resume_hash(None) == "newer-resume"
            assert resolve_display_resume_hash("active-resume") == (
                "active-resume"
            )

        finally:
            database.DATA_DIR = old_data_dir
            database.DATABASE_PATH = old_database_path


def main() -> None:
    test_candidate_jobs_require_complete_descriptions()
    test_admission_gate_keeps_target_titles_needing_enrichment()
    test_admission_gate_preserves_manual_statuses_for_filtered_titles()
    test_description_state_is_separate_from_fit()
    test_fit_priority_uses_current_complete_ai_score()
    test_fit_priority_falls_back_when_ai_score_is_incomplete_or_stale()
    test_enrichment_priority_orders_useful_new_jobs_first()
    test_ai_score_cache_uses_exact_posting_not_duplicate_fingerprint()
    test_application_status_uses_exact_posting_identity()
    test_apply_url_identity_removes_tracking_parameters_only()
    test_apply_url_identity_preserves_job_id_parameters()
    test_apply_url_identity_preserves_greenhouse_job_id()
    test_apply_url_identity_preserves_unknown_parameters()
    test_source_job_id_overrides_url_identity()
    test_exact_duplicate_urls_share_status_with_tracking_removed()
    test_linkedin_job_ids_share_only_when_exact()
    test_embedded_destination_url_unwraps_for_identity()
    test_unresolved_lensa_redirects_do_not_share_status()
    test_unresolved_sendgrid_redirects_do_not_share_status()
    test_resolved_redirect_identity_uses_final_destination()
    test_http_redirect_resolver_returns_final_url_safely()
    test_http_redirect_resolver_uses_embedded_destination_before_network()
    test_different_source_job_ids_never_share_identity()
    test_ui_duplicate_key_collapses_same_source_fingerprint()
    test_ui_duplicate_key_keeps_different_sources_separate()
    test_reposted_same_company_title_different_posting_id_stays_new()
    test_title_exclusion_reasons_are_detected()
    test_display_resume_hash_falls_back_to_latest_cached_profile()
    print("Recommendation repository tests passed.")


if __name__ == "__main__":
    main()
