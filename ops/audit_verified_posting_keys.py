"""Audit stored verified posting keys before cross-source reuse backfills."""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from src.database import get_connection
from src.enrich_jobs import (
    OFFICIAL_FOUND_VERIFIED,
    direct_source_url_verified_key,
    initialize_enrichment_tables,
    stored_candidate_identity_accepted,
)
from src.verified_posting_identity import (
    VERIFIED_KEY_SOURCE_ACCEPTED_DIRECT_CANDIDATE,
    VERIFIED_KEY_SOURCE_DIRECT_ATS_SOURCE,
    VERIFIED_KEY_SOURCE_OFFICIAL_FOUND_VERIFIED,
    VERIFIED_KEY_TRUST_CONFLICT,
    VERIFIED_KEY_TRUST_TRUSTED,
    VERIFIED_KEY_TRUST_UNTRUSTED,
    is_specific_job_posting_url,
    verified_key_metadata_is_trusted,
    verified_posting_key_from_url,
)


CLASS_TRUSTED = "trusted"
CLASS_REPAIRABLE = "repairable"
CLASS_LEGACY_UNVERIFIED = "legacy_unverified"
CLASS_CONFLICTING = "conflicting"
CLASS_GENERIC_URL_KEY = "generic_url_key"
CLASS_INVALID = "invalid"


@dataclass(frozen=True)
class Evidence:
    key: str
    source: str
    confidence: float | None


@dataclass(frozen=True)
class AuditFinding:
    record_key: str
    source: str
    title: str
    company_name: str
    verified_posting_key: str
    classification: str
    reason: str
    repair_source: str | None = None
    repair_confidence: float | None = None


def load_rows() -> list[dict[str, Any]]:
    initialize_enrichment_tables()

    with get_connection() as connection:
        cursor = connection.execute(
            """
            SELECT
                attempts.record_key,
                attempts.source,
                jobs.title,
                jobs.company_name,
                jobs.apply_url,
                attempts.status AS previous_status,
                attempts.final_url AS resolved_candidate_url,
                attempts.identity_confidence,
                attempts.official_job_url,
                attempts.official_url_status,
                attempts.verified_posting_key,
                attempts.verified_posting_key_source,
                attempts.verified_posting_key_trust
            FROM job_enrichment_attempts AS attempts
            LEFT JOIN raw_jobs AS jobs
                ON attempts.record_key = jobs.record_key
            WHERE attempts.verified_posting_key IS NOT NULL
              AND TRIM(attempts.verified_posting_key) <> ''
            ORDER BY attempts.record_key
            """
        )
        columns = [description[0] for description in cursor.description]
        rows = cursor.fetchall()

    return [dict(zip(columns, row)) for row in rows]


def is_generic_official_url_key(key: str) -> bool:
    if not key.startswith("official-url:"):
        return False

    stored_url = key.removeprefix("official-url:")
    return not is_specific_job_posting_url(stored_url)


def row_evidence(row: dict[str, Any]) -> list[Evidence]:
    evidence: list[Evidence] = []
    official_url = str(row.get("official_job_url") or "").strip()

    if (
        str(row.get("official_url_status") or "").strip()
        == OFFICIAL_FOUND_VERIFIED
        and official_url
    ):
        key = verified_posting_key_from_url(official_url)

        if key:
            evidence.append(
                Evidence(
                    key=key,
                    source=VERIFIED_KEY_SOURCE_OFFICIAL_FOUND_VERIFIED,
                    confidence=None,
                )
            )

    direct_key = direct_source_url_verified_key(row)
    if direct_key:
        evidence.append(
            Evidence(
                key=direct_key,
                source=VERIFIED_KEY_SOURCE_DIRECT_ATS_SOURCE,
                confidence=1.0,
            )
        )

    resolved_url = str(row.get("resolved_candidate_url") or "").strip()
    if resolved_url and stored_candidate_identity_accepted(row):
        key = verified_posting_key_from_url(resolved_url)

        if key:
            evidence.append(
                Evidence(
                    key=key,
                    source=VERIFIED_KEY_SOURCE_ACCEPTED_DIRECT_CANDIDATE,
                    confidence=row.get("identity_confidence"),
                )
            )

    unique_evidence: dict[tuple[str, str], Evidence] = {}
    for item in evidence:
        unique_evidence[(item.key, item.source)] = item

    return list(unique_evidence.values())


def classify_row(row: dict[str, Any]) -> AuditFinding:
    record_key = str(row.get("record_key") or "")
    key = str(row.get("verified_posting_key") or "").strip()
    evidence = row_evidence(row)
    evidence_keys = {item.key for item in evidence}

    if not key:
        return AuditFinding(
            record_key=record_key,
            source=str(row.get("source") or ""),
            title=str(row.get("title") or ""),
            company_name=str(row.get("company_name") or ""),
            verified_posting_key=key,
            classification=CLASS_INVALID,
            reason="verified_posting_key is empty",
        )

    if is_generic_official_url_key(key):
        return AuditFinding(
            record_key=record_key,
            source=str(row.get("source") or ""),
            title=str(row.get("title") or ""),
            company_name=str(row.get("company_name") or ""),
            verified_posting_key=key,
            classification=CLASS_GENERIC_URL_KEY,
            reason="stored official-url key points to a generic board/careers page",
        )

    if evidence_keys and key not in evidence_keys:
        return AuditFinding(
            record_key=record_key,
            source=str(row.get("source") or ""),
            title=str(row.get("title") or ""),
            company_name=str(row.get("company_name") or ""),
            verified_posting_key=key,
            classification=CLASS_CONFLICTING,
            reason=(
                "stored key conflicts with authoritative evidence: "
                f"{', '.join(sorted(evidence_keys))}"
            ),
        )

    if verified_key_metadata_is_trusted(
        row.get("verified_posting_key_source"),
        row.get("verified_posting_key_trust"),
    ):
        return AuditFinding(
            record_key=record_key,
            source=str(row.get("source") or ""),
            title=str(row.get("title") or ""),
            company_name=str(row.get("company_name") or ""),
            verified_posting_key=key,
            classification=CLASS_TRUSTED,
            reason="stored key already has trusted provenance",
        )

    if key in evidence_keys:
        matched = next(item for item in evidence if item.key == key)
        return AuditFinding(
            record_key=record_key,
            source=str(row.get("source") or ""),
            title=str(row.get("title") or ""),
            company_name=str(row.get("company_name") or ""),
            verified_posting_key=key,
            classification=CLASS_REPAIRABLE,
            reason="stored key can be proven from authoritative row evidence",
            repair_source=matched.source,
            repair_confidence=matched.confidence,
        )

    return AuditFinding(
        record_key=record_key,
        source=str(row.get("source") or ""),
        title=str(row.get("title") or ""),
        company_name=str(row.get("company_name") or ""),
        verified_posting_key=key,
        classification=CLASS_LEGACY_UNVERIFIED,
        reason="stored key has no authoritative provenance or matching evidence",
    )


def repair_findings(findings: list[AuditFinding]) -> Counter[str]:
    counts: Counter[str] = Counter()
    now = datetime.now(timezone.utc)

    with get_connection() as connection:
        for finding in findings:
            if finding.classification == CLASS_REPAIRABLE:
                connection.execute(
                    """
                    UPDATE job_enrichment_attempts
                    SET
                        verified_posting_key_source = ?,
                        verified_posting_key_verified_at = ?,
                        verified_posting_key_confidence = ?,
                        verified_posting_key_trust = ?
                    WHERE record_key = ?
                    """,
                    [
                        finding.repair_source,
                        now,
                        finding.repair_confidence,
                        VERIFIED_KEY_TRUST_TRUSTED,
                        finding.record_key,
                    ],
                )
                counts["repaired"] += 1
                continue

            if finding.classification in {
                CLASS_LEGACY_UNVERIFIED,
                CLASS_GENERIC_URL_KEY,
                CLASS_INVALID,
            }:
                connection.execute(
                    """
                    UPDATE job_enrichment_attempts
                    SET
                        verified_posting_key_trust = ?,
                        verified_posting_key_source =
                            COALESCE(
                                NULLIF(verified_posting_key_source, ''),
                                'legacy_unknown'
                            ),
                        verified_posting_key_verified_at = NULL,
                        verified_posting_key_confidence = NULL
                    WHERE record_key = ?
                    """,
                    [
                        VERIFIED_KEY_TRUST_UNTRUSTED,
                        finding.record_key,
                    ],
                )
                counts["marked_untrusted"] += 1
                continue

            if finding.classification == CLASS_CONFLICTING:
                connection.execute(
                    """
                    UPDATE job_enrichment_attempts
                    SET verified_posting_key_trust = ?
                    WHERE record_key = ?
                    """,
                    [
                        VERIFIED_KEY_TRUST_CONFLICT,
                        finding.record_key,
                    ],
                )
                counts["marked_conflict"] += 1
                continue

            counts["left_unchanged"] += 1

    return counts


def print_findings(
    findings: list[AuditFinding],
    repair_counts: Counter[str] | None = None,
) -> None:
    counts = Counter(finding.classification for finding in findings)

    print("Verified posting key audit")
    print(f"Total keys inspected: {len(findings)}")
    for classification in (
        CLASS_TRUSTED,
        CLASS_REPAIRABLE,
        CLASS_LEGACY_UNVERIFIED,
        CLASS_CONFLICTING,
        CLASS_GENERIC_URL_KEY,
        CLASS_INVALID,
    ):
        print(f"{classification}: {counts[classification]}")

    if repair_counts is not None:
        print("Repair actions:")
        for key, value in sorted(repair_counts.items()):
            print(f"{key}: {value}")
    else:
        print("Dry run only. No database changes were made.")

    interesting = [
        finding
        for finding in findings
        if finding.classification != CLASS_TRUSTED
    ][:30]

    if interesting:
        print("Sample findings:")
        for finding in interesting:
            print(
                " - "
                f"{finding.classification}: {finding.record_key} | "
                f"{finding.source} | {finding.title} | "
                f"{finding.company_name} | {finding.verified_posting_key} | "
                f"{finding.reason}"
            )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Audit stored verified_posting_key rows and optionally repair "
            "only unambiguous authoritative provenance."
        )
    )
    parser.add_argument(
        "--repair",
        action="store_true",
        help="Persist unambiguous repairs and mark unsafe legacy keys untrusted.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    findings = [
        classify_row(row)
        for row in load_rows()
    ]
    repair_counts = repair_findings(findings) if args.repair else None
    print_findings(findings, repair_counts=repair_counts)


if __name__ == "__main__":
    main()
