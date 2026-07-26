import imaplib
import os
from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = PROJECT_ROOT / ".env"

YAHOO_IMAP_SERVER = "imap.mail.yahoo.com"
YAHOO_IMAP_PORT = 993


def load_credentials() -> tuple[str, str]:
    load_dotenv(ENV_PATH)

    email_address = os.getenv("YAHOO_EMAIL")
    app_password = os.getenv("YAHOO_APP_PASSWORD")

    if not email_address:
        raise ValueError("YAHOO_EMAIL is missing from the .env file.")

    if not app_password:
        raise ValueError("YAHOO_APP_PASSWORD is missing from the .env file.")

    return email_address, app_password


def test_connection() -> None:
    email_address, app_password = load_credentials()

    print(f"Connecting to Yahoo Mail as {email_address}...")

    try:
        with imaplib.IMAP4_SSL(
            YAHOO_IMAP_SERVER,
            YAHOO_IMAP_PORT,
        ) as mailbox:
            mailbox.login(email_address, app_password)

            print("Yahoo IMAP login successful.")

            status, folders = mailbox.list()

            if status != "OK":
                raise RuntimeError("Connected, but Yahoo folders could not be listed.")

            print("\nAvailable Yahoo folders:")

            if not folders:
                print("- No folders returned.")
                return

            for folder in folders:
                print(f"- {folder.decode('utf-8', errors='replace')}")

    except imaplib.IMAP4.error as error:
        print("Yahoo authentication failed.")
        print(f"Details: {error}")
        print(
            "\nVerify that YAHOO_APP_PASSWORD contains a Yahoo app "
            "password rather than your regular Yahoo password."
        )
        raise

def test_folder(folder_name: str) -> None:
    email_address, app_password = load_credentials()

    with imaplib.IMAP4_SSL(
        YAHOO_IMAP_SERVER,
        YAHOO_IMAP_PORT,
    ) as mailbox:
        mailbox.login(email_address, app_password)

        status, folder_data = mailbox.select(
            folder_name,
            readonly=True,
        )

        if status != "OK":
            raise RuntimeError(
                f"Yahoo folder could not be opened: {folder_name}"
            )

        message_count = int(folder_data[0])

        print(f"Folder opened successfully: {folder_name}")
        print(f"Messages currently in folder: {message_count}")




if __name__ == "__main__":
    test_connection()

    print("\nTesting LinkedIn folder...")
    test_folder("jobs/Linkedin")

    print("\nTesting Indeed folder...")
    test_folder("jobs/Indeed")

    print("\nTesting ZipRecruiter folder...")
    test_folder("jobs/ZipRecruiter")
    
    print("\nTesting Glassdoor folder...")
    test_folder("jobs/Glassdoor")