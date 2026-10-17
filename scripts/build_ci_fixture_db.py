"""Build synthetic dbt inputs at JOB_INTELLIGENCE_DB_PATH, without app imports."""
from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_OWNERS = (
    ("src/database.py", "initialize_database"),
    ("src/score_jobs.py", "initialize_match_table"),
    ("src/score_jobs_ai.py", "initialize_ai_tables"),
    ("src/enrich_jobs.py", "initialize_enrichment_tables"),
)


def initialize_schemas(connection: duckdb.DuckDBPyConnection) -> None:
    """Execute the owners' literal initializer DDL without dotenv/API imports."""
    for filename, initializer in SCHEMA_OWNERS:
        tree = ast.parse((ROOT / filename).read_text())
        function = next(node for node in tree.body
                        if isinstance(node, ast.FunctionDef) and node.name == initializer)
        statements = sorted(
            (node for node in ast.walk(function)
             if isinstance(node, ast.Constant) and isinstance(node.value, str)
             and " ".join(node.value.upper().split()).startswith(("CREATE TABLE ", "ALTER TABLE "))),
            key=lambda node: node.lineno,
        )
        if not statements:
            raise RuntimeError(f"No initializer DDL found in {filename}:{initializer}")
        for statement in statements:
            connection.execute(statement.value)


def insert(connection: duckdb.DuckDBPyConnection, table: str, **values) -> None:
    columns = ", ".join(values)
    placeholders = ", ".join("?" for _ in values)
    connection.execute(f"INSERT INTO {table} ({columns}) VALUES ({placeholders})", list(values.values()))


def build_fixture(path: Path) -> None:
    path = path.expanduser().resolve()
    if path.is_relative_to(ROOT / "data"):
        raise ValueError("CI fixtures must be outside the repository data directory")
    if path.exists():
        raise FileExistsError(f"Refusing to overwrite an existing database: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    base = datetime(2024, 1, 1, tzinfo=timezone.utc)
    full_description = (
        "Responsibilities include building Python SQL data pipelines, data models and production analytics. "
        "Requirements include testing, warehouse design, documentation and collaboration with stakeholders. "
    ) * 8
    # Same source ID on the first two occurrences exercises exact deduplication.
    scenarios = (
        ("ci-apply-original", "101", 88, "apply", True),
        ("ci-apply-duplicate", "101", 84, "apply", True),
        ("ci-unscored", "102", None, None, True),
        ("ci-incomplete", "103", 80, "apply", False),
        ("ci-review", "104", 68, "review", True),
        ("ci-skip", "105", 40, "skip", True),
    )
    with duckdb.connect(str(path)) as connection:
        connection.execute("BEGIN TRANSACTION")
        initialize_schemas(connection)
        insert(connection, "resume_profiles", resume_hash="ci-profile", filename="synthetic-ci-profile",
               model_name="ci-fixture", profile_json=json.dumps({"skills": ["Python", "SQL"]}), created_at=base)
        for index, (key, source_id, score, recommendation, complete) in enumerate(scenarios):
            timestamp = base + timedelta(days=index)
            description = full_description if complete else "Short alert; full requirements unavailable."
            url = f"https://example.invalid/jobs/{source_id}"
            insert(connection, "raw_jobs", record_key=key, job_fingerprint=f"ci-opening-{source_id}",
                   source="linkedin", source_job_id=source_id, title="Data Engineer",
                   company_name=f"Synthetic Company {source_id}", location="Remote",
                   description=description, apply_url=url, email_message_id=f"ci-email-{index}",
                   discovered_at=timestamp, description_updated_at=timestamp)
            insert(connection, "processed_emails", email_key=f"ci-email-{index}", source="linkedin",
                   email_message_id=f"ci-email-{index}", processed_at=timestamp)
            insert(connection, "job_matches", record_key=key, search_id="ci-search",
                   search_title="Data Engineer", match_score=score if score is not None else 70,
                   title_score=90, location_score=80, keyword_score=75, freshness_score=10,
                   matched_keywords=json.dumps(["Python", "SQL"]), excluded_keywords="[]",
                   reasons=json.dumps(["Synthetic CI match"]), is_recommended=complete,
                   needs_review=not complete, scored_at=timestamp)
            insert(connection, "job_enrichment_attempts", record_key=key, source="linkedin",
                   requested_url=url, final_url=url, status="enriched" if complete else "no_description",
                   http_status=200, extraction_method="ci-fixture",
                   description_word_count=len(description.split()), last_attempted_at=timestamp)
            if score is not None:
                insert(connection, "resume_job_scores", resume_hash="ci-profile", record_key=key,
                       overall_score=score, recommendation=recommendation,
                       title_fit=score, skills_fit=score, experience_fit=score,
                       seniority_fit=score, industry_fit=score, location_fit=score, confidence="high",
                       matching_strengths=json.dumps(["Python", "SQL"]), hard_requirements_missing="[]",
                       preferred_qualifications_missing="[]", risk_factors="[]", summary="Synthetic CI score",
                       description_word_count=len(description.split()), description_complete=complete,
                       model_name="ci-fixture", prompt_version="v3", scored_at=timestamp + timedelta(hours=1))
        for key, status in (("ci-apply-original", "applied"), ("ci-unscored", "new"), ("ci-skip", "removed")):
            insert(connection, "application_status", record_key=key, status=status, updated_at=base)
        connection.execute("COMMIT")
    print(f"Built CI fixture with {len(scenarios)} synthetic job occurrences: {path}")


if __name__ == "__main__":
    destination = os.environ.get("JOB_INTELLIGENCE_DB_PATH", "").strip()
    if not destination:
        raise SystemExit("Set JOB_INTELLIGENCE_DB_PATH to a new disposable fixture path")
    build_fixture(Path(destination))
