from collections.abc import Callable
from typing import Any

from src.config_loader import load_sources
from src.connectors.yahoo_imap import read_messages
from src.database import (
    get_processed_email_count,
    get_raw_jobs,
    insert_jobs,
    is_email_processed,
    mark_email_processed,
)
from src.parsers.builtin import parse_builtin_email
from src.parsers.bebee import parse_bebee_email
from src.parsers.glassdoor import parse_glassdoor_email
from src.parsers.indeed import parse_indeed_email
from src.parsers.jobleads import parse_jobleads_email
from src.parsers.joblookup import parse_joblookup_email
from src.parsers.jobright import parse_jobright_email
from src.parsers.jobot import parse_jobot_email
from src.parsers.ladders import parse_ladders_email
from src.parsers.lensa import parse_lensa_email
from src.parsers.levels import parse_levels_email
from src.parsers.linkedin import parse_linkedin_email
from src.parsers.remotehunter import parse_remotehunter_email
from src.parsers.welcometothejungle import parse_welcometothejungle_email
from src.parsers.wellfound import parse_wellfound_email
from src.parsers.ziprecruiter import parse_ziprecruiter_email


ParserFunction = Callable[
    [dict[str, Any]],
    list[dict[str, Any]],
]


def parse_indeed_message(
    message: dict[str, Any],
) -> list[dict[str, Any]]:
    return parse_indeed_email(
        message["text"]
    )


def parse_builtin_message(
    message: dict[str, Any],
) -> list[dict[str, Any]]:
    return parse_builtin_email(
        text=message.get("text", ""),
        html=message.get("html"),
        links=message.get("links", []),
    )


def parse_jobot_message(
    message: dict[str, Any],
) -> list[dict[str, Any]]:
    return parse_jobot_email(
        text=message.get("text", ""),
        html=message.get("html"),
        links=message.get("links", []),
    )


def parse_joblookup_message(
    message: dict[str, Any],
) -> list[dict[str, Any]]:
    return parse_joblookup_email(
        text=message.get("text", ""),
        html=message.get("html"),
        links=message.get("links", []),
    )


def parse_jobleads_message(
    message: dict[str, Any],
) -> list[dict[str, Any]]:
    return parse_jobleads_email(
        text=message.get("text", ""),
        html=message.get("html"),
        links=message.get("links", []),
    )


def parse_bebee_message(
    message: dict[str, Any],
) -> list[dict[str, Any]]:
    return parse_bebee_email(
        text=message.get("text", ""),
        html=message.get("html"),
        links=message.get("links", []),
    )


def parse_wellfound_message(
    message: dict[str, Any],
) -> list[dict[str, Any]]:
    return parse_wellfound_email(
        text=message.get("text", ""),
        html=message.get("html"),
        links=message.get("links", []),
    )


def parse_levels_message(
    message: dict[str, Any],
) -> list[dict[str, Any]]:
    return parse_levels_email(
        text=message.get("text", ""),
        html=message.get("html"),
        links=message.get("links", []),
    )


def parse_welcometothejungle_message(
    message: dict[str, Any],
) -> list[dict[str, Any]]:
    return parse_welcometothejungle_email(
        text=message.get("text", ""),
        html=message.get("html"),
        links=message.get("links", []),
    )


def parse_glassdoor_message(
    message: dict[str, Any],
) -> list[dict[str, Any]]:
    return parse_glassdoor_email(
        text=message["text"],
        links=message["links"],
    )


def parse_linkedin_message(
    message: dict[str, Any],
) -> list[dict[str, Any]]:
    return parse_linkedin_email(
        text=message.get("text", ""),
        html=message.get("html"),
        links=message.get("links", []),
    )


def parse_ladders_message(
    message: dict[str, Any],
) -> list[dict[str, Any]]:
    return parse_ladders_email(
        text=message.get("text", ""),
        html=message.get("html"),
        links=message.get("links", []),
    )


def parse_lensa_message(
    message: dict[str, Any],
) -> list[dict[str, Any]]:
    return parse_lensa_email(
        text=message.get("text", ""),
        html=message.get("html"),
        links=message.get("links", []),
    )


def parse_jobright_message(
    message: dict[str, Any],
) -> list[dict[str, Any]]:
    return parse_jobright_email(
        text=message.get("text", ""),
        html=message.get("html"),
        links=message.get("links", []),
    )


def parse_remotehunter_message(
    message: dict[str, Any],
) -> list[dict[str, Any]]:
    return parse_remotehunter_email(
        text=message.get("text", ""),
        html=message.get("html"),
        links=message.get("links", []),
    )


def parse_ziprecruiter_message(
    message: dict[str, Any],
) -> list[dict[str, Any]]:
    return parse_ziprecruiter_email(
        text=message.get("text", ""),
        html=message.get("html"),
        links=message.get("links", []),
    )


PARSERS: dict[str, ParserFunction] = {
    "builtin": parse_builtin_message,
    "indeed": parse_indeed_message,
    "glassdoor": parse_glassdoor_message,
    "bebee": parse_bebee_message,
    "jobleads": parse_jobleads_message,
    "joblookup": parse_joblookup_message,
    "jobright": parse_jobright_message,
    "jobot": parse_jobot_message,
    "ladders": parse_ladders_message,
    "levels": parse_levels_message,
    "lensa": parse_lensa_message,
    "linkedin": parse_linkedin_message,
    "remotehunter": parse_remotehunter_message,
    "welcometothejungle": parse_welcometothejungle_message,
    "wellfound": parse_wellfound_message,
    "ziprecruiter": parse_ziprecruiter_message,
}


def ingest_source(
    source: dict[str, Any],
) -> tuple[int, int, int]:
    source_id = source["source_id"]
    source_name = source["source_name"]
    folder_name = source["mailbox_folder"]

    parser = PARSERS.get(source_id)

    if parser is None:
        print(
            f"\nSkipping {source_name}: "
            "parser not implemented."
        )
        return 0, 0, 0

    print(f"\nProcessing {source_name}")
    print(f"Folder: {folder_name}")

    try:
        messages = read_messages(
            folder_name=folder_name,
            limit=100,
            unread_only=False,
            username_env=source.get(
                "username_env",
                "YAHOO_EMAIL",
            ),
            password_env=source.get(
                "password_env",
                "YAHOO_APP_PASSWORD",
            ),
        )
    except ValueError as error:
        print(f"Skipping {source_name}: {error}")
        return 0, 0, 0

    if not messages:
        print("No emails found.")
        return 0, 0, 0

    source_inserted = 0
    source_duplicates = 0
    source_emails_skipped = 0

    for message in messages:
        if is_email_processed(
            source=source_id,
            email_metadata=message,
        ):
            source_emails_skipped += 1
            continue

        try:
            jobs = parser(message)
        except Exception as error:
            print(
                "\nUnable to parse email: "
                f"{message['subject']}"
            )
            print(f"Reason: {error}")

            # Do not mark failed emails as processed.
            # They can be retried after fixing the parser.
            continue

        if not jobs:
            print(
                "\nNo jobs extracted from email: "
                f"{message['subject']}"
            )

            # Do not mark it processed because an empty result
            # could indicate a parser problem.
            continue

        inserted, duplicates = insert_jobs(
            jobs=jobs,
            email_metadata=message,
        )

        mark_email_processed(
            source=source_id,
            email_metadata=message,
        )

        source_inserted += inserted
        source_duplicates += duplicates

        print(f"\nEmail: {message['subject']}")
        print(f"Jobs parsed: {len(jobs)}")
        print(f"Inserted: {inserted}")
        print(f"Already stored: {duplicates}")

    print(
        "Previously processed emails skipped: "
        f"{source_emails_skipped}"
    )

    return (
        source_inserted,
        source_duplicates,
        source_emails_skipped,
    )


def main() -> None:
    sources = load_sources()

    total_inserted = 0
    total_duplicates = 0
    total_emails_skipped = 0

    print("Starting job ingestion")

    for source in sources:
        (
            inserted,
            duplicates,
            emails_skipped,
        ) = ingest_source(source)

        total_inserted += inserted
        total_duplicates += duplicates
        total_emails_skipped += emails_skipped

    stored_jobs = get_raw_jobs()
    processed_email_count = get_processed_email_count()

    print("\nIngestion complete")
    print(f"Total inserted: {total_inserted}")
    print(
        f"Duplicate jobs skipped: "
        f"{total_duplicates}"
    )
    print(
        f"Previously processed emails skipped: "
        f"{total_emails_skipped}"
    )
    print(
        f"Emails recorded as processed: "
        f"{processed_email_count}"
    )
    print(f"Total jobs in DuckDB: {len(stored_jobs)}")

    source_counts: dict[str, int] = {}

    for job in stored_jobs:
        source = job[4]

        source_counts[source] = (
            source_counts.get(source, 0) + 1
        )

    print("\nJobs by source:")

    for source, count in sorted(source_counts.items()):
        print(f"- {source}: {count}")


if __name__ == "__main__":
    main()
