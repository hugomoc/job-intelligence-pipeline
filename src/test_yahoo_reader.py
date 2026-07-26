from src.connectors.yahoo_imap import read_messages


def main() -> None:
    folder_name = "jobs/Indeed"

    messages = read_messages(
        folder_name=folder_name,
        limit=1,
        unread_only=False,
    )

    print(f"Messages retrieved: {len(messages)}")

    if not messages:
        print(f"No messages found in {folder_name}.")
        return

    message = messages[0]

    print("\nSubject:")
    print(message["subject"])

    print("\nFrom:")
    print(message["sender"])

    print("\nDate:")
    print(message["date"])

    print("\nText preview:")
    print(message["text"][:2_000])

    print("\nLinks:")
    for link in message["links"][:20]:
        print(f"- {link}")


if __name__ == "__main__":
    main()