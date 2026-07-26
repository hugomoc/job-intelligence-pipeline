import hashlib
import re
from pathlib import Path
from typing import Any

import duckdb


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
DATABASE_PATH = DATA_DIR / "jobs.duckdb"


def get_connection() -> duckdb.DuckDBPyConnection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(str(DATABASE_PATH))


def initialize_database() -> None:
    with get_connection() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS raw_jobs (
                record_key VARCHAR PRIMARY KEY,
                job_fingerprint VARCHAR NOT NULL,

                source VARCHAR NOT NULL,
                source_job_id VARCHAR,

                title VARCHAR NOT NULL,
                company_name VARCHAR NOT NULL,
                location VARCHAR,
                salary_text VARCHAR,
                description VARCHAR,
                apply_url VARCHAR NOT NULL,

                email_message_id VARCHAR,
                email_subject VARCHAR,
                email_date VARCHAR,
                source_folder VARCHAR,

                discovered_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS processed_emails (
                email_key VARCHAR PRIMARY KEY,
                source VARCHAR NOT NULL,
                email_message_id VARCHAR,
                email_subject VARCHAR,
                email_date VARCHAR,
                source_folder VARCHAR,
                processed_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
            )
            """
        )


def normalize_for_hash(value: str | None) -> str:
    if not value:
        return ""

    normalized = value.casefold().strip()
    normalized = re.sub(r"\s+", " ", normalized)

    return normalized


def create_hash(*values: str | None) -> str:
    combined = "|".join(
        normalize_for_hash(value)
        for value in values
    )

    return hashlib.sha256(
        combined.encode("utf-8")
    ).hexdigest()


def create_record_key(
    job: dict[str, Any],
    email_message_id: str,
) -> str:
    """
    Identifies one job occurrence from one source email.

    Reading the same email again will not insert the same
    source record twice.
    """
    return create_hash(
        job.get("source"),
        email_message_id,
        job.get("apply_url"),
    )


def create_job_fingerprint(
    job: dict[str, Any],
) -> str:
    """
    Identifies jobs that may represent the same opening.

    This will later support cross-source deduplication.
    """
    return create_hash(
        job.get("title"),
        job.get("company_name"),
        job.get("location"),
    )


def create_email_key(
    source: str,
    email_metadata: dict[str, Any],
) -> str:
    """
    Creates a stable key for an email.

    Yahoo's Message-ID header is preferred because IMAP
    sequence numbers can change.
    """
    permanent_message_id = email_metadata.get(
        "email_message_id"
    )

    if permanent_message_id:
        return create_hash(
            source,
            permanent_message_id,
        )

    return create_hash(
        source,
        email_metadata.get("folder"),
        email_metadata.get("sender"),
        email_metadata.get("subject"),
        email_metadata.get("date"),
    )


def is_email_processed(
    source: str,
    email_metadata: dict[str, Any],
) -> bool:
    initialize_database()

    email_key = create_email_key(
        source=source,
        email_metadata=email_metadata,
    )

    with get_connection() as connection:
        result = connection.execute(
            """
            SELECT 1
            FROM processed_emails
            WHERE email_key = ?
            """,
            [email_key],
        ).fetchone()

    return result is not None


def mark_email_processed(
    source: str,
    email_metadata: dict[str, Any],
) -> None:
    initialize_database()

    email_key = create_email_key(
        source=source,
        email_metadata=email_metadata,
    )

    connection_values = [
        email_key,
        source,
        email_metadata.get("email_message_id"),
        email_metadata.get("subject"),
        email_metadata.get("date"),
        email_metadata.get("folder"),
    ]

    with get_connection() as connection:
        connection.execute(
            """
            INSERT OR IGNORE INTO processed_emails (
                email_key,
                source,
                email_message_id,
                email_subject,
                email_date,
                source_folder
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            connection_values,
        )


def insert_jobs(
    jobs: list[dict[str, Any]],
    email_metadata: dict[str, Any],
) -> tuple[int, int]:
    initialize_database()

    inserted = 0
    skipped = 0

    email_message_id = (
        email_metadata.get("email_message_id")
        or create_email_key(
            source=jobs[0].get("source", "unknown")
            if jobs
            else "unknown",
            email_metadata=email_metadata,
        )
    )

    with get_connection() as connection:
        for job in jobs:
            record_key = create_record_key(
                job=job,
                email_message_id=email_message_id,
            )

            existing = connection.execute(
                """
                SELECT 1
                FROM raw_jobs
                WHERE record_key = ?
                """,
                [record_key],
            ).fetchone()

            if existing:
                skipped += 1
                continue

            job_fingerprint = create_job_fingerprint(job)

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
                    apply_url,
                    email_message_id,
                    email_subject,
                    email_date,
                    source_folder
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    record_key,
                    job_fingerprint,
                    job.get("source"),
                    job.get("source_job_id"),
                    job.get("title"),
                    job.get("company_name"),
                    job.get("location"),
                    job.get("salary_text"),
                    job.get("description"),
                    job.get("apply_url"),
                    email_message_id,
                    email_metadata.get("subject"),
                    email_metadata.get("date"),
                    email_metadata.get("folder"),
                ],
            )

            inserted += 1

    return inserted, skipped


def get_raw_jobs() -> list[tuple]:
    initialize_database()

    with get_connection() as connection:
        return connection.execute(
            """
            SELECT
                title,
                company_name,
                location,
                salary_text,
                source,
                discovered_at
            FROM raw_jobs
            ORDER BY discovered_at DESC
            """
        ).fetchall()


def get_processed_email_count() -> int:
    initialize_database()

    with get_connection() as connection:
        result = connection.execute(
            """
            SELECT COUNT(*)
            FROM processed_emails
            """
        ).fetchone()

    return int(result[0]) if result else 0