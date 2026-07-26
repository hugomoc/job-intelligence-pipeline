from src.connectors.yahoo_imap import read_messages
from src.database import get_raw_jobs, insert_jobs
from src.parsers.indeed import parse_indeed_email


INDEED_FOLDER = "jobs/Indeed"


def main() -> None:
    messages = read_messages(
        folder_name=INDEED_FOLDER,
        limit=20,
        unread_only=False,
    )

    if not messages:
        print("No Indeed emails found.")
        return

    total_inserted = 0
    total_skipped = 0

    for message in messages:
        jobs = parse_indeed_email(message["text"])

        if not jobs:
            print(
                "No jobs extracted from email: "
                f"{message['subject']}"
            )
            continue

        inserted, skipped = insert_jobs(
            jobs=jobs,
            email_metadata=message,
        )

        total_inserted += inserted
        total_skipped += skipped

        print(f"\nEmail: {message['subject']}")
        print(f"Jobs parsed: {len(jobs)}")
        print(f"Inserted: {inserted}")
        print(f"Already stored: {skipped}")

    print("\nIngestion complete")
    print(f"Total inserted: {total_inserted}")
    print(f"Total skipped: {total_skipped}")

    stored_jobs = get_raw_jobs()

    print(f"\nJobs currently in DuckDB: {len(stored_jobs)}")

    for title, company, location, salary, source, discovered_at in stored_jobs:
        print(
            f"- {title} | {company} | "
            f"{location or 'Location not provided'} | "
            f"{salary or 'Salary not provided'} | "
            f"{source}"
        )


if __name__ == "__main__":
    main()