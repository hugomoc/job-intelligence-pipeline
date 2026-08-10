from src.parsers.jobot import parse_jobot_email


def test_jobot_single_job_is_extracted() -> None:
    jobs = parse_jobot_email(
        text="""
        Data Engineer at Example Company
        Remote
        $120K-$150K
        View job
        https://www.jobot.com/details/data-engineer/example
        """,
        html=None,
        links=["https://www.jobot.com/details/data-engineer/example"],
    )

    assert len(jobs) == 1
    assert jobs[0]["source"] == "jobot"
    assert jobs[0]["title"] == "Data Engineer"
    assert jobs[0]["company_name"] == "Example Company"
    assert jobs[0]["location"] == "Remote"
    assert jobs[0]["salary_text"] == "$120K-$150K"


def main() -> None:
    test_jobot_single_job_is_extracted()
    print("Jobot parser tests passed.")


if __name__ == "__main__":
    main()
