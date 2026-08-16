"""Yahoo IMAP reader for job-alert folders.

The connector returns normalized message dictionaries with text, HTML, links,
and stable metadata. It only reads mail; ingestion decides whether an email has
already been processed and whether parsed jobs should be stored.
"""

import imaplib
import os
import re
from email import message_from_bytes
from email.header import decode_header
from email.message import Message
from html import unescape
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup
from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
ENV_PATH = PROJECT_ROOT / ".env"

YAHOO_IMAP_SERVER = "imap.mail.yahoo.com"
YAHOO_IMAP_PORT = 993


def load_credentials(
    username_env: str = "YAHOO_EMAIL",
    password_env: str = "YAHOO_APP_PASSWORD",
) -> tuple[str, str]:
    """Load Yahoo credentials from environment variables named by config."""
    load_dotenv(ENV_PATH)

    email_address = os.getenv(username_env)
    app_password = os.getenv(password_env)

    if not email_address:
        raise ValueError(f"{username_env} is missing from .env.")

    if not app_password:
        raise ValueError(f"{password_env} is missing from .env.")

    return email_address, app_password


def decode_header_value(value: str | None) -> str:
    if not value:
        return ""

    decoded_parts: list[str] = []

    for part, encoding in decode_header(value):
        if isinstance(part, bytes):
            decoded_parts.append(
                part.decode(
                    encoding or "utf-8",
                    errors="replace",
                )
            )
        else:
            decoded_parts.append(part)

    return "".join(decoded_parts)


def decode_payload(message_part: Message) -> str:
    payload = message_part.get_payload(decode=True)

    if payload is None:
        return ""

    charset = message_part.get_content_charset() or "utf-8"

    return payload.decode(charset, errors="replace")


def extract_email_content(message: Message) -> tuple[str, str]:
    plain_text_parts: list[str] = []
    html_parts: list[str] = []

    if message.is_multipart():
        for part in message.walk():
            content_type = part.get_content_type()
            disposition = str(part.get("Content-Disposition", ""))

            if "attachment" in disposition.lower():
                continue

            if content_type == "text/plain":
                plain_text_parts.append(decode_payload(part))

            elif content_type == "text/html":
                html_parts.append(decode_payload(part))
    else:
        content_type = message.get_content_type()

        if content_type == "text/plain":
            plain_text_parts.append(decode_payload(message))

        elif content_type == "text/html":
            html_parts.append(decode_payload(message))

    plain_text = unescape(
        "\n".join(plain_text_parts).strip()
    )
    html = "\n".join(html_parts).strip()

    if plain_text:
        return plain_text, html

    if html:
        soup = BeautifulSoup(html, "html.parser")
        text = unescape(
            soup.get_text(separator="\n", strip=True)
        )
        return text, html

    return "", ""


def extract_links(html: str, text: str) -> list[str]:
    links: list[str] = []
    seen: set[str] = set()

    def add_link(url: str) -> None:
        cleaned_url = url.strip().rstrip(".,);]")

        if not cleaned_url.startswith(("http://", "https://")):
            return

        if cleaned_url in seen:
            return

        seen.add(cleaned_url)
        links.append(cleaned_url)

    # Preserve the order in which links appear in the HTML email.
    if html:
        soup = BeautifulSoup(html, "html.parser")

        for anchor in soup.find_all("a", href=True):
            add_link(anchor["href"])

    # Add plain-text URLs that were not already found in HTML.
    for url in re.findall(r'https?://[^\s<>"]+', text):
        add_link(url)

    return links


def read_messages(
    folder_name: str,
    limit: int = 10,
    unread_only: bool = False,
    username_env: str = "YAHOO_EMAIL",
    password_env: str = "YAHOO_APP_PASSWORD",
) -> list[dict[str, Any]]:
    email_address, app_password = load_credentials(
        username_env=username_env,
        password_env=password_env,
    )

    messages: list[dict[str, Any]] = []

    with imaplib.IMAP4_SSL(
        YAHOO_IMAP_SERVER,
        YAHOO_IMAP_PORT,
    ) as mailbox:
        mailbox.login(email_address, app_password)

        status, _ = mailbox.select(
            folder_name,
            readonly=True,
        )

        if status != "OK":
            raise RuntimeError(
                f"Unable to open Yahoo folder: {folder_name}"
            )

        search_filter = "UNSEEN" if unread_only else "ALL"

        status, search_data = mailbox.search(
            None,
            search_filter,
        )

        if status != "OK":
            raise RuntimeError(
                f"Unable to search Yahoo folder: {folder_name}"
            )

        message_ids = search_data[0].split()

        # Read the newest messages first.
        selected_ids = list(reversed(message_ids[-limit:]))

        for message_id in selected_ids:
            status, message_data = mailbox.fetch(
                message_id,
                "(RFC822)",
            )

            if status != "OK" or not message_data:
                continue

            raw_email = None

            for response_part in message_data:
                if isinstance(response_part, tuple):
                    raw_email = response_part[1]
                    break

            if not raw_email:
                continue

            message = message_from_bytes(raw_email)

            text, html = extract_email_content(message)
            links = extract_links(html, text)

            messages.append(
                {
                    "message_id": message_id.decode(),
                    "email_message_id": decode_header_value(
                        message.get("Message-ID")
                    ),
                    "folder": folder_name,
                    "subject": decode_header_value(
                        message.get("Subject")
                    ),
                    "sender": decode_header_value(
                        message.get("From")
                    ),
                    "recipient": decode_header_value(
                        message.get("To")
                    ),
                    "mailbox_account": email_address,
                    "date": message.get("Date", ""),
                    "text": text,
                    "html": html,
                    "links": links,
                }
            )

    return messages
