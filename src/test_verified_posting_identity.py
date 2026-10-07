from src.verified_posting_identity import (
    candidate_duplicate_fingerprint,
    description_hash,
    normalize_candidate_company,
    normalize_candidate_location,
    normalize_candidate_title,
    verified_posting_key_from_url,
)


def test_verified_key_uses_ats_identity() -> None:
    assert (
        verified_posting_key_from_url(
            "https://boards.greenhouse.io/example/jobs/123"
        )
        == "greenhouse:123"
    )
    assert (
        verified_posting_key_from_url(
            "https://jobs.lever.co/example/abc-123"
        )
        == "lever:abc-123"
    )


def test_similar_jobs_with_different_official_ids_do_not_merge() -> None:
    assert verified_posting_key_from_url(
        "https://boards.greenhouse.io/example/jobs/123"
    ) != verified_posting_key_from_url(
        "https://boards.greenhouse.io/example/jobs/456"
    )


def test_title_company_location_candidate_normalization() -> None:
    assert normalize_candidate_title("Sr. Data Engineer") == (
        normalize_candidate_title("Senior Data Engineer")
    )
    assert normalize_candidate_company("Robots & Pencils") == (
        normalize_candidate_company("Robots and Pencils")
    )
    assert normalize_candidate_location("Remote only, United States") == (
        normalize_candidate_location("United States (Remote)")
    )


def test_candidate_fingerprint_is_fuzzy_not_verified_identity() -> None:
    assert candidate_duplicate_fingerprint(
        "Sr. Data Engineer",
        "Robots & Pencils",
        "Remote only, United States",
    ) == candidate_duplicate_fingerprint(
        "Senior Data Engineer",
        "Robots and Pencils",
        "United States (Remote)",
    )
    assert (
        verified_posting_key_from_url(None)
        != candidate_duplicate_fingerprint(
            "Senior Data Engineer",
            "Robots and Pencils",
            "United States (Remote)",
        )
    )


def test_description_hash_is_content_normalized() -> None:
    assert description_hash("Python, SQL, and Snowflake") == description_hash(
        "python sql and snowflake"
    )


def main() -> None:
    test_verified_key_uses_ats_identity()
    test_similar_jobs_with_different_official_ids_do_not_merge()
    test_title_company_location_candidate_normalization()
    test_candidate_fingerprint_is_fuzzy_not_verified_identity()
    test_description_hash_is_content_normalized()
    print("Verified posting identity tests passed.")


if __name__ == "__main__":
    main()
