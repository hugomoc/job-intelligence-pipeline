from src.connectors.yahoo_imap import read_messages


GLASSDOOR_FOLDER = "jobs/Glassdoor"


def main() -> None:
    messages = read_messages(
        folder_name=GLASSDOOR_FOLDER,
        limit=1,
        unread_only=False,
    )

    print(f"Messages retrieved: {len(messages)}")

    if not messages:
        print(f"No messages found in {GLASSDOOR_FOLDER}.")
        return

    message = messages[0]

    print("\nSubject:")
    print(message["subject"])

    print("\nFrom:")
    print(message["sender"])

    print("\nDate:")
    print(message["date"])

    print("\nText preview:")
    print(message["text"][:5_000])

    print("\nLinks:")
    for link in message["links"][:30]:
        print(f"- {link}")


if __name__ == "__main__":
    main()