from src.audit_verified_posting_keys import (
    CLASS_CONFLICTING,
    CLASS_GENERIC_URL_KEY,
    CLASS_LEGACY_UNVERIFIED,
    CLASS_REPAIRABLE,
    classify_row,
)
from src.enrichment.official_job_resolver import OFFICIAL_FOUND_VERIFIED


def test_audit_classifies_repairable_legacy_key() -> None:
    finding = classify_row(
        {
            "record_key": "repairable",
            "source": "linkedin",
            "title": "Senior Data Engineer",
            "company_name": "Example",
            "verified_posting_key": "greenhouse:123",
            "official_url_status": OFFICIAL_FOUND_VERIFIED,
            "official_job_url": "https://boards.greenhouse.io/example/jobs/123",
        }
    )

    assert finding.classification == CLASS_REPAIRABLE
    assert finding.repair_source == "official_found_verified"


def test_audit_classifies_conflicting_legacy_key() -> None:
    finding = classify_row(
        {
            "record_key": "conflict",
            "source": "linkedin",
            "title": "Senior Data Engineer",
            "company_name": "Example",
            "verified_posting_key": "greenhouse:111",
            "official_url_status": OFFICIAL_FOUND_VERIFIED,
            "official_job_url": "https://boards.greenhouse.io/example/jobs/222",
        }
    )

    assert finding.classification == CLASS_CONFLICTING


def test_audit_classifies_generic_url_key() -> None:
    finding = classify_row(
        {
            "record_key": "generic",
            "source": "linkedin",
            "title": "Senior Data Engineer",
            "company_name": "Example",
            "verified_posting_key": (
                "official-url:https://boards.greenhouse.io/example"
            ),
        }
    )

    assert finding.classification == CLASS_GENERIC_URL_KEY


def test_audit_classifies_unproven_legacy_key() -> None:
    finding = classify_row(
        {
            "record_key": "legacy",
            "source": "linkedin",
            "title": "Senior Data Engineer",
            "company_name": "Example",
            "verified_posting_key": "greenhouse:123",
        }
    )

    assert finding.classification == CLASS_LEGACY_UNVERIFIED


def main() -> None:
    test_audit_classifies_repairable_legacy_key()
    test_audit_classifies_conflicting_legacy_key()
    test_audit_classifies_generic_url_key()
    test_audit_classifies_unproven_legacy_key()
    print("Verified posting key audit tests passed.")


if __name__ == "__main__":
    main()
