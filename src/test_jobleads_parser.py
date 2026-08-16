from src.parsers.jobleads import parse_jobleads_email


def test_jobleads_marketing_email_is_ignored() -> None:
    jobs = parse_jobleads_email(
        text="""
        Hugo: Your request for a resume review
        This headhunter might be missing from your network.
        Message from the Founder and Managing Director of JobLeads.
        """
    )

    assert jobs == []


def main() -> None:
    test_jobleads_marketing_email_is_ignored()
    print("JobLeads parser tests passed.")


if __name__ == "__main__":
    main()
