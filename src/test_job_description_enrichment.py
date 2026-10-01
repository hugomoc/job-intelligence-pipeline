from pathlib import Path
from tempfile import TemporaryDirectory

from src import database
from src.enrich_jobs import (
    initialize_enrichment_tables,
    save_enrichment_attempt,
    validate_enrichment_identity,
)
from src.enrichment.job_description import (
    JobDescriptionResult,
    appears_to_be_non_description_text,
)


def test_lensa_search_page_text_is_not_description() -> None:
    text = """
    Community
    Search Jobs
    ApplyAssist
    CareerPilot
    More
    Companies
    Insights
    Jobseekers
    Workstyle game
    Exact matches for your query ends here
    Time to start afresh? Try again, and narrow or broaden your search this time.
    """

    assert appears_to_be_non_description_text(text)


def test_real_job_description_signal_is_allowed() -> None:
    text = """
    About the role
    You will build data models, maintain analytics pipelines,
    and partner with business stakeholders.

    Requirements
    Experience with SQL, dbt, Python, and cloud data warehouses.
    """

    assert not appears_to_be_non_description_text(text)


def test_mismatched_resolved_job_is_rejected_and_not_saved() -> None:
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
                        description,
                        apply_url
                    )
                    VALUES (
                        'drivewealth-job',
                        'drivewealth-job',
                        'lensa',
                        'Principal Data Engineer, Analytics',
                        'DriveWealth',
                        'Remote',
                        NULL,
                        'https://lensa.example/job'
                    )
                    """
                )

            job = {
                "record_key": "drivewealth-job",
                "source": "lensa",
                "title": "Principal Data Engineer, Analytics",
                "company_name": "DriveWealth",
                "location": "Remote",
                "current_word_count": 0,
                "attempt_count": 0,
            }
            result = JobDescriptionResult(
                requested_url="https://lensa.example/job",
                final_url=(
                    "https://www.jobleads.com/us/jobs/q/"
                    "Remote%20SAS%20Viya%20Development%20Engineer"
                ),
                status="enriched",
                http_status=200,
                extraction_method="json_ld",
                description=(
                    "Remote SAS Viya Development Engineer - Data Pipelines. "
                    * 30
                ),
                word_count=180,
                error_message=None,
                resolved_title=(
                    "Remote SAS Viya Development Engineer - Data Pipelines"
                ),
                resolved_company="NTT DATA",
                resolved_location="Remote",
            )

            validation = validate_enrichment_identity(
                job=job,
                result=result,
            )

            assert not validation.accepted
            assert "company mismatch" in validation.reason

            save_enrichment_attempt(
                job=job,
                result=result,
                status="resolution_rejected",
                error_message=(
                    "Resolved candidate rejected: "
                    f"{validation.reason}"
                ),
                identity_validation=validation,
            )

            with database.get_connection() as connection:
                stored_job = connection.execute(
                    """
                    SELECT title, company_name, description
                    FROM raw_jobs
                    WHERE record_key = 'drivewealth-job'
                    """
                ).fetchone()
                attempt = connection.execute(
                    """
                    SELECT
                        status,
                        resolved_candidate_title,
                        resolved_candidate_company,
                        identity_validation_reason
                    FROM job_enrichment_attempts
                    WHERE record_key = 'drivewealth-job'
                    """
                ).fetchone()

            assert stored_job == (
                "Principal Data Engineer, Analytics",
                "DriveWealth",
                None,
            )
            assert attempt[0] == "resolution_rejected"
            assert attempt[1] == (
                "Remote SAS Viya Development Engineer - Data Pipelines"
            )
            assert attempt[2] == "NTT DATA"

        finally:
            database.DATA_DIR = old_data_dir
            database.DATABASE_PATH = old_database_path


def main() -> None:
    test_lensa_search_page_text_is_not_description()
    test_real_job_description_signal_is_allowed()
    test_mismatched_resolved_job_is_rejected_and_not_saved()
    print("Job description enrichment tests passed.")


if __name__ == "__main__":
    main()
