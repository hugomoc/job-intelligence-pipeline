from src.parsers.wellfound import parse_wellfound_email


SAMPLE_TEXT = """
Hi Hugo! I've found 8 new jobs that might interest you!

Principal Data Engineer
EnergyHub / 51-200 Employees
$180–220k | Remote only, United States | 10 years of exp | Full-time
Actively Hiring B2B Growth Stage
Learn More
<https://wellfound.com/jobs?job_listing_slug=4562812-principal-data-engineer>

Data Engineer
MedWatchers / 51-200 Employees
$100–150k | In office, San Diego | 3 years of exp | Full-time
Actively Hiring
Our Take
MedWatchers develops a SaaS platform.
Learn More <https://wellfound.com/jobs?job_listing_slug=4564717-data-engineer>
"""


def test_wellfound_digest_jobs_are_extracted() -> None:
    jobs = parse_wellfound_email(text=SAMPLE_TEXT)

    assert len(jobs) == 2
    assert jobs[0]["source"] == "wellfound"
    assert jobs[0]["source_job_id"] == "4562812"
    assert jobs[0]["title"] == "Principal Data Engineer"
    assert jobs[0]["company_name"] == "EnergyHub"
    assert jobs[0]["location"] == "Remote only, United States"
    assert jobs[0]["salary_text"] == "$180–220k"
    assert jobs[0]["description"] == "Employment type: Full-time"
    assert jobs[1]["company_name"] == "MedWatchers"
    assert jobs[1]["location"] == "In office, San Diego"


def main() -> None:
    test_wellfound_digest_jobs_are_extracted()
    print("Wellfound parser tests passed.")


if __name__ == "__main__":
    main()
