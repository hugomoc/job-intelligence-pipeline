from pathlib import Path
from tempfile import TemporaryDirectory

from src import database
from src.repositories.recommendation_repository import (
    count_unscored_candidate_jobs,
    load_candidate_jobs,
)


def seed_job(
    record_key: str,
    description: str | None,
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
            VALUES (?, ?, 'test', ?, 'Example Co', 'Remote', NULL, ?, ?)
            """,
            [
                record_key,
                record_key,
                f"Data Engineer {record_key}",
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


def test_candidate_jobs_require_description() -> None:
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
                        prompt_version VARCHAR NOT NULL
                    )
                    """
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
                        "Builds reliable data pipelines and analytics models"
                    ]
                    * 12
                ),
            )

            candidates = load_candidate_jobs(
                resume_hash="resume-1",
                model_name="model-1",
                limit=10,
                minimum_rule_score=0,
                reuse_any_model=True,
            )

            assert [
                candidate["record_key"]
                for candidate in candidates
            ] == ["has-description"]
            assert count_unscored_candidate_jobs(
                resume_hash="resume-1",
                model_name="model-1",
                minimum_rule_score=0,
                reuse_any_model=True,
            ) == 1

        finally:
            database.DATA_DIR = old_data_dir
            database.DATABASE_PATH = old_database_path


def main() -> None:
    test_candidate_jobs_require_description()
    print("Recommendation repository tests passed.")


if __name__ == "__main__":
    main()
