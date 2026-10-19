"""Deterministic stdout diagnostics without provider payloads or credentials."""

from __future__ import annotations

import os
import re

from pydantic import ValidationError


def sanitize_diagnostic_text(value: object) -> str:
    """Retain error summaries, omitting payload sections and credential values."""
    message = str(value)
    # Matcher wrappers include model JSON previews; these can echo resume data.
    message = re.split(
        r'(?i)(?:response preview|request payload|request body|resume(?: text)?|'
        r'prompt|contents|messages)[\"\']?\s*[:=]', message, maxsplit=1,
    )[0]
    # Pydantic reprs contain rejected input values. Structured validation below
    # normally handles these, but this also covers detached string exceptions.
    message = re.sub(r'(?s)\[type=[^\]]*input_value=.*', '[input omitted]', message)
    for name, secret in os.environ.items():
        if len(secret) >= 6 and re.search(
                r'(TOKEN|SECRET|PASSWORD|CREDENTIAL|API_KEY|ACCESS_KEY)', name, re.I):
            message = message.replace(secret, '[redacted]')
    message = re.sub(r'(?i)\b(?:https?|ftp)://[^\s<>]+', '[URL omitted]', message)
    message = re.sub(r'(?i)\b(?:bearer|basic)\s+[^\s,;"\']+', '[authorization redacted]', message)
    message = re.sub(
        r'(?i)(\b(?:authorization|proxy-authorization|api[-_ ]?key|access[-_ ]?token|'
        r'refresh[-_ ]?token|token|password|passwd|secret|credentials?)\b["\']?\s*[:=]\s*)'
        r'(?:"[^"]*"|\'[^\']*\'|[^\s,;]+)', r'\1[redacted]', message,
    )
    message = re.sub(r'\b(?:sk-[A-Za-z0-9_-]{10,}|AIza[A-Za-z0-9_-]+|AKIA[A-Z0-9]{16})\b',
                     '[redacted]', message)
    return ' '.join(message.split())[:1200]


def scoring_error_message(error: Exception) -> str:
    """Keep validation field/type/reason without input values or model previews."""
    cause = error
    seen = set()
    while cause is not None and id(cause) not in seen:
        seen.add(id(cause))
        if isinstance(cause, ValidationError):
            details = '; '.join(
                f"{'.'.join(map(str, item['loc']))}: {item['type']}: {item['msg']}"
                for item in cause.errors(include_input=False, include_context=False, include_url=False)
            )
            return sanitize_diagnostic_text('Response validation failed: ' + details)
        cause = cause.__cause__
    return sanitize_diagnostic_text(error)


def log_scoring_failure(job: dict, index: int, total: int, provider: str, error: Exception) -> None:
    """Print one safe record per provider attempt; never affect scoring decisions."""
    print('Score failed:')
    print(f'  Position: {index}/{total}')
    print('  Record key: ' + sanitize_diagnostic_text(job.get('record_key') or '<missing>'))
    print('  Job: ' + sanitize_diagnostic_text(job.get('title') or 'Untitled job')
          + ' | ' + sanitize_diagnostic_text(job.get('company_name') or 'Unknown company'))
    print('  Provider: ' + provider)
    print('  Error: ' + type(error).__name__ + ': ' + scoring_error_message(error))
