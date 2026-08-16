from typing import Any


def parse_jobleads_email(
    text: str = "",
    html: str | None = None,
    links: list[str] | None = None,
) -> list[dict[str, Any]]:
    """
    JobLeads samples currently in the mailbox are marketing emails, not
    job-alert digests. Keep this parser intentionally conservative so those
    emails do not create bogus jobs.
    """
    return []
