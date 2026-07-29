from src.connectors.yahoo_imap import read_messages
from src.database import get_raw_jobs, insert_jobs
from src.parsers.linkedin import parse_linkedin_email


LINKEDIN_FOLDER = "jobs/Linkedin"


def main() -> None:
    messages = read_messages(
        folder_name=LINKEDIN_FOLDER,
        limit=20,
        unread_only=False,
    )

    if not messages:
        print("No LinkedIn emails found.")
        return

    total_inserted = 0
    total_skipped = 0

    for message in messages:
        jobs = parse_linkedin_email(
            text=message.get("text", ""),
            html=message.get("html"),
            links=message.get("links", []),
        )

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

    print("\nLinkedIn ingestion complete")
    print(f"Total inserted: {total_inserted}")
    print(f"Total skipped: {total_skipped}")
    print(f"Jobs currently in DuckDB: {len(get_raw_jobs())}")


if __name__ == "__main__":
    main()
