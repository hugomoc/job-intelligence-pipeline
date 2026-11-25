"""Persistent scoring-run history for UI diagnostics.

The scoring pipeline writes one compact run record after each scoring action so
the latest AI scoring log remains available after Streamlit reruns or restarts.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import uuid

from src.ai.resume_matcher import MATCHER_PROMPT_VERSION
from src.database import get_connection


@dataclass(frozen=True)
class ScoringRunRecord:
    run_id: str
    resume_hash: str
    workflow: str
    status: str
    started_at: datetime
    completed_at: datetime | None
    model_name: str | None
    prompt_version: str
    candidates_selected: int
    already_scored: int
    scores_saved: int
    jobs_failed: int
    gemini_scores_saved: int
    openai_scores_saved: int
    quota_exhausted: bool
    log_lines: tuple[str, ...]


def initialize_scoring_run_table() -> None:
    with get_connection() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS scoring_runs (
                run_id VARCHAR PRIMARY KEY,
                resume_hash VARCHAR NOT NULL,
                workflow VARCHAR NOT NULL,
                status VARCHAR NOT NULL,
                started_at TIMESTAMPTZ NOT NULL,
                completed_at TIMESTAMPTZ,
                model_name VARCHAR,
                prompt_version VARCHAR NOT NULL,
                candidates_selected INTEGER NOT NULL DEFAULT 0,
                already_scored INTEGER NOT NULL DEFAULT 0,
                scores_saved INTEGER NOT NULL DEFAULT 0,
                jobs_failed INTEGER NOT NULL DEFAULT 0,
                gemini_scores_saved INTEGER NOT NULL DEFAULT 0,
                openai_scores_saved INTEGER NOT NULL DEFAULT 0,
                quota_exhausted BOOLEAN NOT NULL DEFAULT FALSE,
                log_json VARCHAR NOT NULL DEFAULT '[]'
            )
            """
        )


def start_scoring_run(
    resume_hash: str,
    workflow: str,
) -> str:
    initialize_scoring_run_table()
    run_id = uuid.uuid4().hex
    started_at = datetime.now(timezone.utc)

    with get_connection() as connection:
        connection.execute(
            """
            INSERT INTO scoring_runs (
                run_id,
                resume_hash,
                workflow,
                status,
                started_at,
                prompt_version,
                log_json
            )
            VALUES (?, ?, ?, 'running', ?, ?, '[]')
            """,
            [
                run_id,
                resume_hash,
                workflow,
                started_at,
                MATCHER_PROMPT_VERSION,
            ],
        )

    return run_id


def finish_scoring_run(
    run_id: str,
    *,
    status: str,
    model_name: str | None,
    candidates_selected: int,
    already_scored: int,
    scores_saved: int,
    jobs_failed: int,
    gemini_scores_saved: int,
    openai_scores_saved: int,
    quota_exhausted: bool,
    log_lines: tuple[str, ...] | list[str],
) -> None:
    initialize_scoring_run_table()
    completed_at = datetime.now(timezone.utc)

    with get_connection() as connection:
        connection.execute(
            """
            UPDATE scoring_runs
            SET
                status = ?,
                completed_at = ?,
                model_name = ?,
                candidates_selected = ?,
                already_scored = ?,
                scores_saved = ?,
                jobs_failed = ?,
                gemini_scores_saved = ?,
                openai_scores_saved = ?,
                quota_exhausted = ?,
                log_json = ?
            WHERE run_id = ?
            """,
            [
                status,
                completed_at,
                model_name,
                candidates_selected,
                already_scored,
                scores_saved,
                jobs_failed,
                gemini_scores_saved,
                openai_scores_saved,
                quota_exhausted,
                json.dumps(list(log_lines), ensure_ascii=False),
                run_id,
            ],
        )


def load_latest_scoring_run(
    resume_hash: str | None = None,
) -> ScoringRunRecord | None:
    initialize_scoring_run_table()

    where_clause = ""
    parameters: list[object] = []

    if resume_hash:
        where_clause = "WHERE resume_hash = ?"
        parameters.append(resume_hash)

    with get_connection() as connection:
        cursor = connection.execute(
            f"""
            SELECT
                run_id,
                resume_hash,
                workflow,
                status,
                started_at,
                completed_at,
                model_name,
                prompt_version,
                candidates_selected,
                already_scored,
                scores_saved,
                jobs_failed,
                gemini_scores_saved,
                openai_scores_saved,
                quota_exhausted,
                log_json
            FROM scoring_runs
            {where_clause}
            ORDER BY started_at DESC
            LIMIT 1
            """,
            parameters,
        )
        row = cursor.fetchone()

    if row is None:
        return None

    try:
        parsed_log = json.loads(row[15] or "[]")
    except json.JSONDecodeError:
        parsed_log = []

    log_lines = tuple(
        str(item)
        for item in parsed_log
        if str(item).strip()
    )

    return ScoringRunRecord(
        run_id=str(row[0]),
        resume_hash=str(row[1]),
        workflow=str(row[2]),
        status=str(row[3]),
        started_at=row[4],
        completed_at=row[5],
        model_name=(str(row[6]) if row[6] else None),
        prompt_version=str(row[7]),
        candidates_selected=int(row[8] or 0),
        already_scored=int(row[9] or 0),
        scores_saved=int(row[10] or 0),
        jobs_failed=int(row[11] or 0),
        gemini_scores_saved=int(row[12] or 0),
        openai_scores_saved=int(row[13] or 0),
        quota_exhausted=bool(row[14]),
        log_lines=log_lines,
    )


# Written only when "Run daily" ingested and enriched successfully
# (streamlit_app: run_daily_clicked and ingestion_succeeded and enrichment_succeeded).
INGESTION_WORKFLOW = "ingest_enrich_score"


def load_latest_completed_ingestion_run() -> tuple[str, datetime] | None:
    """(run_id, completed_at) of the newest completed daily ingestion run.

    Failed or unfinished runs, score-only workflows and job discovery dates are
    never treated as evidence of a completed ingestion run.
    """
    initialize_scoring_run_table()
    with get_connection() as connection:
        row = connection.execute(
            """
            SELECT run_id, completed_at
            FROM scoring_runs
            WHERE workflow = ?
              AND status = 'completed'
              AND completed_at IS NOT NULL
            ORDER BY completed_at DESC, run_id DESC
            LIMIT 1
            """,
            [INGESTION_WORKFLOW],
        ).fetchone()
    return (str(row[0]), row[1]) if row else None
