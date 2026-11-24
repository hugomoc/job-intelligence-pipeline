#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Honor an explicit override (tests/CI) so dbt and src.database use one file.
export JOB_INTELLIGENCE_DB_PATH="${JOB_INTELLIGENCE_DB_PATH:-$REPO_ROOT/data/jobs.duckdb}"

if [[ -n "${VIRTUAL_ENV:-}" && -x "$VIRTUAL_ENV/bin/dbt" ]]; then
    DBT_BIN="$VIRTUAL_ENV/bin/dbt"
else
    DBT_BIN="$REPO_ROOT/venv312job/bin/dbt"
fi

PROJECT_DIR="$REPO_ROOT/job_intelligence_dbt"

exec "$DBT_BIN" "$@" \
    --project-dir "$PROJECT_DIR" \
    --profiles-dir "$PROJECT_DIR"
