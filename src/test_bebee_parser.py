from src.parsers.bebee import parse_bebee_email


SAMPLE_TEXT = """
Hi Hugo,
Your alert: Looker Developer (LookML and Reports)
There are 20 new openings matching your profile today. Here's the strongest fit.
1. Microsoft Power Applications Developer | Active Secret Clearance | San Diego, CA — ARES Enterprise · San Diego
https://bebee.com/us/jobs/microsoft-power-applications-developer-active-secret-clearance-san-diego-ca-ares-enterprise-san-dieg--ss-us-14gyc23
2. Software Developer — KMS Software LLC · San Diego · $48,000-$60,000 /year
https://bebee.com/us/jobs/software-developer-kms-software-llc-san-diego--recooty-01JXWXNCQ4WWBDT5Y4JD1T8XFV
See all jobs: https://bebee.com/us/jobs?q=looker
"""


def test_bebee_digest_jobs_are_extracted() -> None:
    jobs = parse_bebee_email(text=SAMPLE_TEXT)

    assert len(jobs) == 2
    assert jobs[0]["source"] == "bebee"
    assert jobs[0]["title"] == (
        "Microsoft Power Applications Developer | Active Secret Clearance | San Diego, CA"
    )
    assert jobs[0]["company_name"] == "ARES Enterprise"
    assert jobs[0]["location"] == "San Diego"
    assert jobs[1]["company_name"] == "KMS Software LLC"
    assert jobs[1]["salary_text"] == "$48,000-$60,000 /year"


def main() -> None:
    test_bebee_digest_jobs_are_extracted()
    print("Bebee parser tests passed.")


if __name__ == "__main__":
    main()
