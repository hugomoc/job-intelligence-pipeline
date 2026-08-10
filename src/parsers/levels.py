from typing import Any

from src.parsers.builtin import parse_builtin_email


def parse_levels_email(
    text: str = "",
    html: str | None = None,
    links: list[str] | None = None,
) -> list[dict[str, Any]]:
    # The current Yahoo Levels folder contains Built In alerts.
    # Keep this wrapper separate so a native Levels.fyi parser can
    # replace it when a real sample arrives.
    return parse_builtin_email(
        text=text,
        html=html,
        links=links,
    )
