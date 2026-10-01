from src.resolvers.jobleads_resolver import JobLeadsJob
from src.resolvers.lensa_resolver import validate_resolved_jobleads_job


def test_lensa_resolver_rejects_wrong_jobleads_destination() -> None:
    validation = validate_resolved_jobleads_job(
        expected_title="Principal Data Engineer, Analytics",
        expected_company="DriveWealth",
        job=JobLeadsJob(
            title="Remote SAS Viya Development Engineer - Data Pipelines",
            company="NTT DATA",
            location="Remote",
            salary=None,
            employment_type=None,
            description="Build SAS Viya pipelines.",
            extraction_method="json_ld",
        ),
    )

    assert not validation.accepted
    assert "company mismatch" in validation.reason
    assert "title mismatch" in validation.reason


def test_lensa_resolver_accepts_matching_jobleads_destination() -> None:
    validation = validate_resolved_jobleads_job(
        expected_title="Principal Data Engineer, Analytics",
        expected_company="DriveWealth",
        job=JobLeadsJob(
            title="Principal Data Engineer - Analytics",
            company="DriveWealth",
            location="Remote",
            salary=None,
            employment_type=None,
            description="Build analytics data platforms.",
            extraction_method="json_ld",
        ),
    )

    assert validation.accepted


def main() -> None:
    test_lensa_resolver_rejects_wrong_jobleads_destination()
    test_lensa_resolver_accepts_matching_jobleads_destination()
    print("Lensa identity validation tests passed.")


if __name__ == "__main__":
    main()
