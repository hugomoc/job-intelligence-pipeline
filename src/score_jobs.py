"""Persist rule-based search/job match results.

The rule matcher is intentionally deterministic and cheap. Its scores decide
which raw jobs deserve enrichment or AI review, and dbt later reads this table
as part of the analytics lineage.
"""

import json
from typing import Any

from src.config_loader import load_searches
from src.database import (
    get_connection,
    initialize_database,
)
from src.matching.job_matcher import (
    MatchResult,
    score_all_jobs,
)


def initialize_match_table() -> None:
    """Create the rule-match table used before AI scoring."""
    initialize_database()

    with get_connection() as connection:
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

                is_recommended BOOLEAN NOT NULL,
                needs_review BOOLEAN NOT NULL
                    DEFAULT FALSE,

                scored_at TIMESTAMPTZ
                    DEFAULT CURRENT_TIMESTAMP,

                PRIMARY KEY (
                    record_key,
                    search_id
                )
            )
            """
        )

        # Adds the column when job_matches was created
        # before needs_review existed.
        connection.execute(
            """
            ALTER TABLE job_matches
            ADD COLUMN IF NOT EXISTS
                needs_review BOOLEAN
                DEFAULT FALSE
            """
        )


def load_raw_jobs() -> list[dict[str, Any]]:
    initialize_database()

    with get_connection() as connection:
        cursor = connection.execute(
            """
            SELECT
                record_key,
                source,
                source_job_id,
                title,
                company_name,
                location,
                salary_text,
                description,
                apply_url,
                discovered_at
            FROM raw_jobs
            """
        )

        columns = [
            description[0]
            for description
            in cursor.description
        ]

        rows = cursor.fetchall()

    return [
        dict(zip(columns, row))
        for row in rows
    ]


def refresh_job_matches(
    results: list[MatchResult],
) -> None:
    initialize_match_table()

    rows = [
        (
            result.record_key,
            result.search_id,
            result.search_title,
            result.match_score,
            result.title_score,
            result.location_score,
            result.keyword_score,
            result.freshness_score,
            json.dumps(
                result.matched_keywords,
                ensure_ascii=False,
            ),
            json.dumps(
                result.excluded_keywords,
                ensure_ascii=False,
            ),
            json.dumps(
                result.reasons,
                ensure_ascii=False,
            ),
            result.is_recommended,
            result.needs_review,
        )
        for result in results
    ]

    with get_connection() as connection:
        connection.execute(
            "BEGIN TRANSACTION"
        )

        try:
            # Recalculate all matches so changes in
            # searches.yml take effect immediately.
            connection.execute(
                "DELETE FROM job_matches"
            )

            if rows:
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
                    VALUES (
                        ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?
                    )
                    """,
                    rows,
                )

            connection.execute("COMMIT")

        except Exception:
            connection.execute("ROLLBACK")
            raise


def load_best_matches() -> list[dict[str, Any]]:
    initialize_match_table()

    with get_connection() as connection:
        cursor = connection.execute(
            """
            WITH ranked_matches AS (
                SELECT
                    jobs.record_key,
                    jobs.title,
                    jobs.company_name,
                    jobs.location,
                    jobs.salary_text,
                    jobs.source,
                    jobs.apply_url,

                    matches.search_id,
                    matches.search_title,
                    matches.match_score,
                    matches.title_score,
                    matches.location_score,
                    matches.keyword_score,
                    matches.freshness_score,
                    matches.matched_keywords,
                    matches.excluded_keywords,
                    matches.reasons,
                    matches.is_recommended,
                    matches.needs_review,

                    ROW_NUMBER() OVER (
                        PARTITION BY jobs.record_key
                        ORDER BY
                            matches.is_recommended DESC,
                            matches.needs_review DESC,
                            matches.match_score DESC,
                            matches.title_score DESC,
                            matches.search_id
                    ) AS match_rank

                FROM raw_jobs AS jobs

                INNER JOIN job_matches AS matches
                    ON jobs.record_key =
                       matches.record_key
            )

            SELECT
                title,
                company_name,
                location,
                salary_text,
                source,
                apply_url,
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
            FROM ranked_matches
            WHERE match_rank = 1
            ORDER BY
                is_recommended DESC,
                needs_review DESC,
                match_score DESC,
                title,
                company_name
            """
        )

        columns = [
            description[0]
            for description
            in cursor.description
        ]

        rows = cursor.fetchall()

    return [
        dict(zip(columns, row))
        for row in rows
    ]


def parse_json_list(
    value: str | None,
) -> list[str]:
    if not value:
        return []

    try:
        result = json.loads(value)
    except json.JSONDecodeError:
        return []

    if not isinstance(result, list):
        return []

    return [
        str(item)
        for item in result
    ]


def print_match_results(
    matches: list[dict[str, Any]],
) -> None:
    recommended_count = sum(
        1
        for match in matches
        if match["is_recommended"]
    )

    review_count = sum(
        1
        for match in matches
        if match["needs_review"]
    )

    low_match_count = (
        len(matches)
        - recommended_count
        - review_count
    )

    print("\nScoring complete")
    print(f"Jobs scored: {len(matches)}")
    print(
        f"Recommended jobs: "
        f"{recommended_count}"
    )
    print(
        f"Jobs needing review: "
        f"{review_count}"
    )
    print(
        f"Low matches: "
        f"{low_match_count}"
    )

    if not matches:
        return

    print("\nBest match for each job:")

    for match in matches:
        if match["is_recommended"]:
            status = "RECOMMENDED"

        elif match["needs_review"]:
            status = "REVIEW"

        else:
            status = "LOW MATCH"

        print(
            f"\n[{status}] "
            f"Score {match['match_score']}"
        )

        print(
            f"{match['title']} | "
            f"{match['company_name']}"
        )

        print(
            "Location: "
            f"{match['location'] or 'Not provided'}"
        )

        print(
            "Salary: "
            f"{match['salary_text'] or 'Not provided'}"
        )

        print(
            f"Source: {match['source']}"
        )

        print(
            "Best search: "
            f"{match['search_id']} "
            f"({match['search_title']})"
        )

        print(
            "Score breakdown: "
            f"title={match['title_score']}, "
            f"location={match['location_score']}, "
            f"keywords={match['keyword_score']}, "
            f"freshness={match['freshness_score']}"
        )

        matched_keywords = parse_json_list(
            match["matched_keywords"]
        )

        excluded_keywords = parse_json_list(
            match["excluded_keywords"]
        )

        if matched_keywords:
            print(
                "Matched keywords: "
                + ", ".join(
                    matched_keywords
                )
            )

        if excluded_keywords:
            print(
                "Excluded keywords: "
                + ", ".join(
                    excluded_keywords
                )
            )

        print(
            f"URL: {match['apply_url']}"
        )


def main() -> None:
    defaults, searches = load_searches()

    if not searches:
        print(
            "No enabled searches found in "
            "config/searches.yml."
        )
        return

    jobs = load_raw_jobs()

    if not jobs:
        print(
            "No jobs found in raw_jobs. "
            "Run python -m src.ingest_all first."
        )
        return

    print(f"Jobs loaded: {len(jobs)}")
    print(
        f"Enabled searches: "
        f"{len(searches)}"
    )

    results = score_all_jobs(
        jobs=jobs,
        searches=searches,
        defaults=defaults,
    )

    refresh_job_matches(results)

    best_matches = load_best_matches()

    print_match_results(best_matches)


if __name__ == "__main__":
    main()
