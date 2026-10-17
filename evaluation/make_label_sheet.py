"""Create an unanchored private label sheet from stored results, read-only."""
from __future__ import annotations

import argparse
import csv
import os
import random
from collections import defaultdict
from pathlib import Path

import duckdb

from src.database import DATABASE_PATH  # Constants/pure helpers only; no connection opened on import.
from evaluation.metrics import UNRESOLVED, is_verified

REPO_ROOT = Path(__file__).resolve().parents[1]
LABEL_COLUMNS = (
    "record_key", "company", "title", "source", "source_apply_url", "stored_official_url",
    "description_excerpt", "resolution_label", "expected_official_url", "my_fit_rating", "my_decision",
)
LABEL_FIELDS = ("resolution_label", "expected_official_url", "my_fit_rating", "my_decision")


def private_path(path: Path, *, overwrite: bool = False, input_file: bool = False) -> Path:
    """Reject lexical paths and symlink targets in the repository, before I/O."""
    lexical = Path(os.path.abspath(path.expanduser()))
    resolved = lexical.resolve()
    if lexical.is_relative_to(REPO_ROOT) or resolved.is_relative_to(REPO_ROOT):
        raise ValueError("Private CSV paths must be outside the repository")
    if input_file:
        if not resolved.is_file():
            raise FileNotFoundError("Label input must be an existing CSV outside the repository")
    elif resolved.exists() and not overwrite:
        raise FileExistsError("Refusing to overwrite private output; select a new path or use --force for evaluation details")
    if resolved.is_dir():
        raise ValueError("CSV path must be a file")
    return resolved


def read_stored_rows(database: Path) -> list[dict]:
    """Use one read-only snapshot; no initializers or application API imports."""
    with duckdb.connect(str(database.expanduser().resolve()), read_only=True) as connection:
        result = connection.execute("""
            WITH latest_scores AS (
                SELECT record_key, resume_hash, overall_score, recommendation,
                       prompt_version, description_complete, scored_at,
                       ROW_NUMBER() OVER (
                           PARTITION BY record_key
                           ORDER BY scored_at DESC NULLS LAST, resume_hash ASC
                       ) AS row_number
                FROM resume_job_scores
            )
            SELECT jobs.record_key, jobs.company_name AS company, jobs.title, jobs.source,
                   jobs.apply_url AS source_apply_url, jobs.description,
                   attempts.official_job_url, attempts.official_url_status,
                   attempts.official_url_confidence, attempts.official_url_validation_reason,
                   attempts.official_url_source, attempts.official_url_resolved_at,
                   scores.overall_score, scores.recommendation, scores.prompt_version,
                   scores.description_complete, scores.scored_at, scores.resume_hash
            FROM raw_jobs AS jobs
            LEFT JOIN job_enrichment_attempts AS attempts USING (record_key)
            LEFT JOIN latest_scores AS scores ON scores.record_key = jobs.record_key
                                              AND scores.row_number = 1
            WHERE trim(coalesce(jobs.description, '')) <> ''
              AND lower(coalesce(attempts.error_message, '')) NOT LIKE '%quarantin%'
              AND lower(coalesce(attempts.identity_validation_reason, '')) NOT LIKE '%quarantin%'
            ORDER BY jobs.record_key
        """)
        columns = [column[0] for column in result.description]
        return [dict(zip(columns, row)) for row in result.fetchall()]


def _stratify(rows: list[dict], seed: int) -> list[dict]:
    groups = defaultdict(list)
    for row in rows:
        groups[(str(row.get("source") or ""), str(row.get("official_url_status") or ""))].append(row)
    rng = random.Random(seed)
    for group in groups.values():
        rng.shuffle(group)
    ordered = []
    while any(groups.values()):
        for key in sorted(groups):
            if groups[key]:
                ordered.append(groups[key].pop())
    return ordered


def sample_rows(rows: list[dict], n: int = 50, seed: int = 0) -> list[dict]:
    if n <= 0:
        raise ValueError("Sample size must be positive")
    verified = _stratify([row for row in rows if is_verified(row)], seed)
    unresolved = _stratify([row for row in rows if str(row.get("official_url_status") or "").upper() in UNRESOLVED], seed)
    verified_target = round(n * 0.8)
    selected = verified[:verified_target] + unresolved[:n - verified_target]
    remaining = verified[verified_target:] + unresolved[n - verified_target:]
    selected += remaining[:max(0, n - len(selected))]
    random.Random(seed).shuffle(selected)
    return selected


def make_label_sheet(database: Path, output: Path, n: int = 50, seed: int = 0) -> int:
    destination = private_path(output)
    selected = sample_rows(read_stored_rows(database), n, seed)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("x", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=LABEL_COLUMNS)
        writer.writeheader()
        for row in selected:
            writer.writerow({**{field: row.get(field, "") for field in LABEL_COLUMNS[:5]},
                             "stored_official_url": row.get("official_job_url") or "",
                             "description_excerpt": (row.get("description") or "")[:600],
                             **{field: "" for field in LABEL_FIELDS}})
    return len(selected)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=DATABASE_PATH)
    parser.add_argument("--output", type=Path, default=Path.home() / "job-eval/labels_todo.csv")
    parser.add_argument("--n", type=int, default=50)
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args()
    count = make_label_sheet(args.database, args.output, args.n, args.seed)
    print(f"Wrote {count} label rows (requested {args.n}); shortages are not fabricated.")


if __name__ == "__main__":
    main()
