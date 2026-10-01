from src.enrichment.job_identity import (
    normalize_company_name,
    validate_job_identity,
)


def test_company_normalization() -> None:
    assert normalize_company_name("NTT DATA, Inc.") == "ntt data"
    assert normalize_company_name("Example Corp.") == "example"


def test_production_lensa_jobleads_mismatch_is_rejected() -> None:
    validation = validate_job_identity(
        original_title="Principal Data Engineer, Analytics",
        original_company="DriveWealth",
        resolved_title=(
            "Remote SAS Viya Development Engineer - Data Pipelines"
        ),
        resolved_company="NTT DATA",
    )

    assert not validation.accepted
    assert "company mismatch" in validation.reason
    assert "title mismatch" in validation.reason


def test_same_drivewealth_job_is_accepted() -> None:
    validation = validate_job_identity(
        original_title="Principal Data Engineer, Analytics",
        original_company="DriveWealth",
        resolved_title="Principal Data Engineer - Analytics",
        resolved_company="DriveWealth",
    )

    assert validation.accepted


def test_abbreviated_seniority_is_accepted() -> None:
    validation = validate_job_identity(
        original_title="Sr. Data Engineer",
        original_company="Example Corp",
        resolved_title="Senior Data Engineer - Data Platform",
        resolved_company="Example Corp.",
    )

    assert validation.accepted


def test_occupation_mismatch_rejects_even_when_company_matches() -> None:
    validation = validate_job_identity(
        original_title="Data Engineer",
        original_company="Company A",
        resolved_title="Software Engineer - Data",
        resolved_company="Company A",
    )

    assert not validation.accepted
    assert "primary occupation mismatch" in validation.reason


def test_company_mismatch_rejects_matching_title() -> None:
    validation = validate_job_identity(
        original_title="Analytics Engineer",
        original_company="Company A",
        resolved_title="Analytics Engineer",
        resolved_company="Company B",
    )

    assert not validation.accepted
    assert "company mismatch" in validation.reason


def test_missing_company_requires_extremely_strong_title() -> None:
    accepted = validate_job_identity(
        original_title="Principal Data Engineer, Analytics",
        original_company="DriveWealth",
        resolved_title="Principal Data Engineer, Analytics",
        resolved_company=None,
    )
    rejected = validate_job_identity(
        original_title="Principal Data Engineer, Analytics",
        original_company="DriveWealth",
        resolved_title="Data Engineer",
        resolved_company=None,
    )

    assert accepted.accepted
    assert not rejected.accepted


def main() -> None:
    test_company_normalization()
    test_production_lensa_jobleads_mismatch_is_rejected()
    test_same_drivewealth_job_is_accepted()
    test_abbreviated_seniority_is_accepted()
    test_occupation_mismatch_rejects_even_when_company_matches()
    test_company_mismatch_rejects_matching_title()
    test_missing_company_requires_extremely_strong_title()
    print("Job identity tests passed.")


if __name__ == "__main__":
    main()
