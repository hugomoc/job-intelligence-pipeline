from src.connectors.yahoo_imap import read_messages
from src.parsers.indeed import parse_indeed_email


def main() -> None:
    messages = read_messages(
        folder_name="jobs/Indeed",
        limit=1,
        unread_only=False,
    )

    if not messages:
        print("No Indeed messages found.")
        return

    message = messages[0]
    jobs = parse_indeed_email(message["text"])

    print(f"Email subject: {message['subject']}")
    print(f"Jobs extracted: {len(jobs)}")

    for number, job in enumerate(jobs, start=1):
        print(f"\nJob {number}")
        print(f"Title: {job['title']}")
        print(f"Company: {job['company_name']}")
        print(f"Location: {job['location']}")
        print(f"Salary: {job['salary_text'] or 'Not provided'}")
        print(f"Description: {job['description'][:300]}")
        print(f"URL: {job['apply_url'][:150]}...")


if __name__ == "__main__":
    main()