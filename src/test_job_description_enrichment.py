from src.enrichment.job_description import (
    appears_to_be_non_description_text,
)


def test_lensa_search_page_text_is_not_description() -> None:
    text = """
    Community
    Search Jobs
    ApplyAssist
    CareerPilot
    More
    Companies
    Insights
    Jobseekers
    Workstyle game
    Exact matches for your query ends here
    Time to start afresh? Try again, and narrow or broaden your search this time.
    """

    assert appears_to_be_non_description_text(text)


def test_real_job_description_signal_is_allowed() -> None:
    text = """
    About the role
    You will build data models, maintain analytics pipelines,
    and partner with business stakeholders.

    Requirements
    Experience with SQL, dbt, Python, and cloud data warehouses.
    """

    assert not appears_to_be_non_description_text(text)


def main() -> None:
    test_lensa_search_page_text_is_not_description()
    test_real_job_description_signal_is_allowed()
    print("Job description enrichment tests passed.")


if __name__ == "__main__":
    main()
