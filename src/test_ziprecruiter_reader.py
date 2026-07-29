from __future__ import annotations

import email
import imaplib
import os
from email.header import decode_header
from email.message import Message
from pathlib import Path
from urllib.parse import parse_qsl, urlparse

from bs4 import BeautifulSoup
from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = PROJECT_ROOT / ".env"

FIXTURE_DIR = PROJECT_ROOT / "tests" / "fixtures"
OUTPUT_PATH = FIXTURE_DIR / "ziprecruiter_job_alert.eml"

YAHOO_IMAP_SERVER = "imap.mail.yahoo.com"
YAHOO_IMAP_PORT = 993
ZIPRECRUITER_FOLDER = "jobs/ZipRecruiter"


def load_credentials() -> tuple[str, str]:
    load_dotenv(ENV_PATH)

    email_address = os.getenv("YAHOO_EMAIL")
    app_password = os.getenv("YAHOO_APP_PASSWORD")

    if not email_address:
        raise ValueError(
            "YAHOO_EMAIL is missing from the .env file."
        )

    if not app_password:
        raise ValueError(
            "YAHOO_APP_PASSWORD is missing from the .env file."
        )

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


def decode_part(part: Message) -> str:
    payload = part.get_payload(decode=True)

    if not payload:
        return ""

    charset = part.get_content_charset() or "utf-8"

    return payload.decode(
        charset,
        errors="replace",
    )


def extract_message_content(
    message: Message,
) -> tuple[str, str]:
    plain_text_parts: list[str] = []
    html_parts: list[str] = []

    if message.is_multipart():
        for part in message.walk():
            content_type = part.get_content_type()

            disposition = str(
                part.get("Content-Disposition", "")
            ).lower()

            if "attachment" in disposition:
                continue

            if content_type == "text/plain":
                content = decode_part(part)

                if content:
                    plain_text_parts.append(content)

            elif content_type == "text/html":
                content = decode_part(part)

                if content:
                    html_parts.append(content)

    else:
        content_type = message.get_content_type()
        content = decode_part(message)

        if content_type == "text/html":
            html_parts.append(content)

        elif content_type == "text/plain":
            plain_text_parts.append(content)

    return (
        "\n".join(plain_text_parts),
        "\n".join(html_parts),
    )


def print_text_preview(
    label: str,
    content: str,
    max_characters: int = 8_000,
) -> None:
    print(f"\n--- {label} preview ---")

    if not content:
        print("No content found.")
        return

    cleaned = content.strip()

    print(cleaned[:max_characters])

    if len(cleaned) > max_characters:
        print(
            "\n... preview truncated; "
            f"total characters: {len(cleaned)}"
        )


def summarize_url(url: str) -> str:
    """
    Print useful URL structure without dumping every tracking token.
    The complete original URL remains available in the saved .eml file.
    """
    parsed = urlparse(url)

    query_keys = sorted(
        {
            key
            for key, _ in parse_qsl(
                parsed.query,
                keep_blank_values=True,
            )
        }
    )

    summary = (
        f"scheme={parsed.scheme or '(none)'}\n"
        f"host={parsed.netloc or '(relative URL)'}\n"
        f"path={parsed.path or '/'}"
    )

    if query_keys:
        summary += (
            "\nquery keys="
            + ", ".join(query_keys)
        )

    return summary


def print_html_links(html_content: str) -> None:
    print("\n--- HTML links ---")

    if not html_content:
        print("No HTML content found.")
        return

    soup = BeautifulSoup(
        html_content,
        "html.parser",
    )

    links_seen: set[str] = set()
    link_number = 0

    for anchor in soup.find_all(
        "a",
        href=True,
    ):
        href = str(
            anchor.get("href", "")
        ).strip()

        if not href or href in links_seen:
            continue

        links_seen.add(href)
        link_number += 1

        text = anchor.get_text(
            " ",
            strip=True,
        )

        print(f"\nLink {link_number}")
        print(
            "Text:",
            text or "(no visible text)",
        )
        print(summarize_url(href))

        shortened_url = href[:500]

        if len(href) > 500:
            shortened_url += "... [truncated]"

        print(f"URL preview: {shortened_url}")

        if link_number >= 150:
            print(
                "\nStopped after 150 unique links."
            )
            break

    print(
        f"\nUnique links displayed: "
        f"{link_number}"
    )


def fetch_latest_ziprecruiter_email() -> bytes:
    email_address, app_password = load_credentials()

    print(
        f"Connecting to Yahoo Mail as "
        f"{email_address}..."
    )

    with imaplib.IMAP4_SSL(
        YAHOO_IMAP_SERVER,
        YAHOO_IMAP_PORT,
    ) as mailbox:
        mailbox.login(
            email_address,
            app_password,
        )

        status, folder_data = mailbox.select(
            ZIPRECRUITER_FOLDER,
            readonly=True,
        )

        if status != "OK":
            raise RuntimeError(
                "Yahoo folder could not be opened: "
                f"{ZIPRECRUITER_FOLDER}"
            )

        message_count = int(folder_data[0])

        print(
            "ZipRecruiter folder opened successfully."
        )
        print(
            f"Messages currently in folder: "
            f"{message_count}"
        )

        if message_count == 0:
            raise RuntimeError(
                "The ZipRecruiter folder is empty. "
                "Move an original ZipRecruiter job-alert "
                "email into "
                f"{ZIPRECRUITER_FOLDER} and run this again."
            )

        status, search_data = mailbox.uid(
            "search",
            None,
            "ALL",
        )

        if status != "OK" or not search_data:
            raise RuntimeError(
                "ZipRecruiter messages could not "
                "be searched."
            )

        message_uids = search_data[0].split()

        if not message_uids:
            raise RuntimeError(
                "No ZipRecruiter message UIDs "
                "were returned."
            )

        latest_uid = message_uids[-1]

        print(
            "Reading latest ZipRecruiter email UID: "
            f"{latest_uid.decode()}"
        )

        status, message_data = mailbox.uid(
            "fetch",
            latest_uid,
            "(RFC822)",
        )

        if status != "OK":
            raise RuntimeError(
                "The latest ZipRecruiter email "
                "could not be downloaded."
            )

        for response_part in message_data:
            if (
                isinstance(response_part, tuple)
                and isinstance(
                    response_part[1],
                    bytes,
                )
            ):
                return response_part[1]

    raise RuntimeError(
        "Yahoo returned no raw email content."
    )


def inspect_ziprecruiter_email(
    raw_email: bytes,
) -> None:
    message = email.message_from_bytes(
        raw_email
    )

    print("\n--- Email metadata ---")
    print(
        "From:",
        decode_header_value(
            message.get("From")
        ),
    )
    print(
        "To:",
        decode_header_value(
            message.get("To")
        ),
    )
    print(
        "Subject:",
        decode_header_value(
            message.get("Subject")
        ),
    )
    print(
        "Date:",
        decode_header_value(
            message.get("Date")
        ),
    )
    print(
        "Message-ID:",
        decode_header_value(
            message.get("Message-ID")
        ),
    )

    print("\n--- MIME parts ---")

    for index, part in enumerate(
        message.walk(),
        start=1,
    ):
        print(
            f"{index}. "
            f"type={part.get_content_type()}, "
            f"charset={part.get_content_charset()}, "
            f"disposition="
            f"{part.get_content_disposition()}"
        )

    plain_text, html_content = (
        extract_message_content(message)
    )

    print_text_preview(
        "Plain text",
        plain_text,
    )

    if html_content:
        soup = BeautifulSoup(
            html_content,
            "html.parser",
        )

        visible_html_text = soup.get_text(
            "\n",
            strip=True,
        )

    else:
        visible_html_text = ""

    print_text_preview(
        "HTML visible text",
        visible_html_text,
        max_characters=12_000,
    )

    print_html_links(html_content)


def save_fixture(raw_email: bytes) -> None:
    FIXTURE_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    OUTPUT_PATH.write_bytes(raw_email)

    print(
        "\nSaved raw ZipRecruiter sample to:"
    )
    print(OUTPUT_PATH)


def main() -> None:
    raw_email = (
        fetch_latest_ziprecruiter_email()
    )

    save_fixture(raw_email)

    inspect_ziprecruiter_email(
        raw_email
    )


if __name__ == "__main__":
    main()