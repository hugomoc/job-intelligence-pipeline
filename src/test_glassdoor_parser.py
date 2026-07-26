from src.connectors.yahoo_imap import read_messages
from src.parsers.glassdoor import parse_glassdoor_email


GLASSDOOR_FOLDER = "jobs/Glassdoor"


def main() -> None:
    messages = read_messages(
        folder_name=GLASSDOOR_FOLDER,
        limit=1,
        unread_only=False,
    )

    if not messages:
        print("No Glassdoor emails found.")
        return

    message = messages[0]

    jobs = parse_glassdoor_email(
        text=message["text"],
        links=message["links"],
    )

    print(f"Email subject: {message['subject']}")
    print(f"Jobs extracted: {len(jobs)}")

    for number, job in enumerate(jobs, start=1):
        print(f"\nJob {number}")
        print(f"Title: {job['title']}")
        print(f"Company: {job['company_name']}")
        print(f"Location: {job['location']}")
        print(
            f"Salary: "
            f"{job['salary_text'] or 'Not provided'}"
        )
        print(
            f"Source job ID: "
            f"{job['source_job_id'] or 'Not provided'}"
        )
        print(f"URL: {job['apply_url'][:150]}...")


if __name__ == "__main__":
    main()