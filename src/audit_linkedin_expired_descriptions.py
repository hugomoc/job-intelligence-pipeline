"""Read-only offline audit of LinkedIn descriptions contaminated by expired redirects.

No HTTP client, enrichment processing, migrations, or database writes.
Produces a JSON report containing only metadata and short evidence snippets.
"""
from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import duckdb

from src.database import DATABASE_PATH
from src.enrichment.job_description import is_unsafe_linkedin_posting_redirect

CLASS_CONFIRMED = "confirmed_expired_search_page"
CLASS_HIGH_CONFIDENCE = "high_confidence_search_results_page"
CLASS_LIKELY = "likely_search_results_contamination"
CLASS_REDIRECT_REVIEW = "review_unsafe_posting_redirect"
CLASS_IDENTITY_REVIEW = "review_legacy_unverified_linkedin"
CLASS_NO_SIGNAL = "no_known_contamination_signal"

# Multiple independent pieces of job-search/navigation text are stronger than
# the mere presence of "jobs" or "apply" on a legitimate job description.
SEARCH_PAGE_MARKERS = (
    "get notified when a new job is posted",
    "sign in to set job alerts",
    "jobs in united states",
    "be an early applicant",
    "actively hiring",
    "set alert",
    "new to linkedin? join now",
)


def search_page_evidence(description: str) -> list[str]:
    text = re.sub(r"\s+", " ", str(description or "")).casefold()
    matched = [phrase for phrase in SEARCH_PAGE_MARKERS if phrase in text]
    if re.search(r"\b[\d,]+\s*\+?\s+[a-z\s]+ jobs in united states\b", text):
        matched.append("numbered jobs-in-united-states listing")
    return sorted(set(matched))


def has_generic_search_results_header(description: str) -> bool:
    """Identify generic listings without mistaking genuine job-page chrome.

    Job pages also have "Set alert" and "New to LinkedIn" boilerplate.
    Those phrases alone must never trigger this high-confidence category.
    """
    prefix = re.sub(r"\\s+", " ", str(description or "")[:900]).casefold().strip()
    if not prefix.startswith("get notified about new "):
        return False
    if "sign in to create job alert" not in prefix[:270]:
        return False
    return bool(
        re.search(
            r"\\b[\\d,]+\\s*\\+?\\s+.{1,100}?\\bjobs in [a-z]",
            prefix[:500],
        )
    )


def classify_row(row: dict[str, Any]) -> dict[str, Any]:
    description = str(row.get("description") or "")
    requested_url = str(row.get("apply_url") or "")
    final_url = str(row.get("final_url") or "")
    status = str(row.get("attempt_status") or "")
    rejected_redirect = is_unsafe_linkedin_posting_redirect(
        requested_url, final_url
    )
    markers = search_page_evidence(description)
    text_is_listing = (
        len(markers) >= 3
        or ("numbered jobs-in-united-states listing" in markers and len(markers) >= 2)
    )

    if rejected_redirect and status == "enriched" and text_is_listing:
        classification = CLASS_CONFIRMED
        reason = "Enrichment saved text from an expired/generic LinkedIn redirect; text contains multiple search-listing markers."
    elif has_generic_search_results_header(description):
        classification = CLASS_HIGH_CONFIDENCE
        reason = (
            "Stored text begins with a generic LinkedIn job-results header and "
            "numbered listing, rather than the requested individual posting."
        )
    elif text_is_listing:
        classification = CLASS_LIKELY
        reason = "Stored description contains multiple LinkedIn job-search listing markers; inspect original provenance."
    elif rejected_redirect:
        classification = CLASS_REDIRECT_REVIEW
        reason = "Attempt ended at an expired/search/different LinkedIn posting URL; stored text is not proven to come from that page."
    elif (
        status == "enriched"
        and "identity validation not required for this source" in
        str(row.get("identity_validation_reason") or "").casefold()
        and len(description.split()) >= 80
    ):
        classification = CLASS_IDENTITY_REVIEW
        reason = "Long LinkedIn description was accepted under legacy identity-validation bypass; no contamination confirmed."
    else:
        classification = CLASS_NO_SIGNAL
        reason = "No recognizable expired-redirect contamination signals (not a verification of job authenticity)."

    return {
        "classification": classification,
        "reason": reason,
        "record_key": row.get("record_key"),
        "title": row.get("title"),
        "company": row.get("company_name"),
        "source": row.get("source"),
        "original_url": requested_url,
        "final_url": final_url or None,
        "attempt_status": status or None,
        "official_status": row.get("official_url_status"),
        "verified_posting_key": row.get("verified_posting_key"),
        "description_source": row.get("description_source"),
        "description_words": len(description.split()),
        "description_characters": len(description),
        "evidence_markers": (
            sorted(set(markers + ["generic results-list header and job-count"]))
            if has_generic_search_results_header(description) else markers
        ),
        "description_preview": re.sub(r"\s+", " ", description).strip()[:200],
    }


def load_rows(database_path: Path) -> list[dict[str, Any]]:
    if not database_path.is_file():
        raise FileNotFoundError(f"DuckDB file not found: {database_path}")
    # An actual READ_ONLY database connection, not merely a promise to avoid
    # INSERT/UPDATE/DELETE. Never initialize or migrate the user's DB.
    with duckdb.connect(str(database_path), read_only=True) as db:
        cursor = db.execute(
            """
            SELECT
                r.record_key, r.source, r.title, r.company_name,
                r.apply_url, r.description,
                a.final_url, a.status AS attempt_status,
                a.identity_validation_reason, a.description_source,
                a.official_url_status, a.verified_posting_key
            FROM raw_jobs r
            LEFT JOIN job_enrichment_attempts a
                ON r.record_key = a.record_key
            WHERE (
                LOWER(COALESCE(r.source, '')) LIKE '%linkedin%'
                OR LOWER(COALESCE(r.apply_url, '')) LIKE '%linkedin.com/%'
                OR LOWER(COALESCE(a.final_url, '')) LIKE '%linkedin.com/%'
            )
              AND NULLIF(TRIM(COALESCE(r.description, '')), '') IS NOT NULL
            ORDER BY r.record_key
            """
        )
        fields = [col[0] for col in cursor.description]
        return [dict(zip(fields, row)) for row in cursor.fetchall()]


def build_report(rows: list[dict[str, Any]]) -> dict[str, Any]:
    findings = [classify_row(row) for row in rows]
    counts = Counter(item["classification"] for item in findings)
    flagged = [
        item for item in findings
        if item["classification"] != CLASS_NO_SIGNAL
    ]
    priority = {
        CLASS_CONFIRMED: 0,
        CLASS_HIGH_CONFIDENCE: 1,
        CLASS_LIKELY: 2,
        CLASS_REDIRECT_REVIEW: 3,
        CLASS_IDENTITY_REVIEW: 4,
    }
    flagged.sort(key=lambda item: (
        priority[item["classification"]], str(item["company"] or ""),
        str(item["record_key"] or ""),
    ))
    return {
        "audit": "stored_linkedin_description_redirects",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "read_only": True,
        "network_requests": 0,
        "scanned_stored_descriptions": len(rows),
        "flagged_records": len(flagged),
        "counts": {
            classification: counts[classification]
            for classification in (
                CLASS_CONFIRMED, CLASS_HIGH_CONFIDENCE, CLASS_LIKELY,
                CLASS_REDIRECT_REVIEW, CLASS_IDENTITY_REVIEW,
                CLASS_NO_SIGNAL,
            )
        },
        "findings": flagged,
        "note": (
            "No database rows were changed. Findings are evidence for manual "
            "review, not automatic proof of identity or an instruction to delete."
        ),
    }


def audit(database_path: Path) -> dict[str, Any]:
    return build_report(load_rows(database_path))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=DATABASE_PATH)
    parser.add_argument(
        "--report", type=Path,
        default=DATABASE_PATH.parent / "linkedin-expired-descriptions-audit.json",
    )
    args = parser.parse_args()
    report = audit(args.database)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.report.with_suffix(args.report.suffix + ".tmp")
    temporary.write_text(json.dumps(report, indent=2, default=str) + "\n")
    temporary.replace(args.report)

    print("LinkedIn stored description audit (read-only; zero API credits)")
    print("Descriptions scanned:", report["scanned_stored_descriptions"])
    print("Flagged for review:", report["flagged_records"])
    for classification, count in report["counts"].items():
        print(f"{classification}: {count}")
    for item in report["findings"][:30]:
        print(
            f" - {item['classification']} | {item['company']} | "
            f"{item['title']} | {item['record_key']} | {item['reason']}"
        )
    print("JSON report:", args.report)
    print("No database changes made.")


if __name__ == "__main__":
    main()
