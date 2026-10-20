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


def log_scoring_failure(job: dict, index: int, total: int, provider: str, error: Exception, *, stage: str = "Score") -> None:
    """Print one safe record per provider attempt; never affect scoring decisions."""
    print(stage + ' failed:')
    print(f'  Position: {index}/{total}')
    print('  Record key: ' + sanitize_diagnostic_text(job.get('record_key') or '<missing>'))
    print('  Job: ' + sanitize_diagnostic_text(job.get('title') or 'Untitled job')
          + ' | ' + sanitize_diagnostic_text(job.get('company_name') or 'Unknown company'))
    print('  Provider: ' + provider)
    print('  Error: ' + type(error).__name__ + ': ' + scoring_error_message(error))


def log_scoring_skip(job, index, total, reason, details=None):
    """Keep skip outcomes attributable without leaking URL credentials/payloads."""
    print('Skipped: candidate already scored or another scoring run owns this job.'
          if reason in {'candidate_already_completed','claimed_by_another_run'}
          else 'Skipped: ' + sanitize_diagnostic_text(reason))
    print(f'  Position: {index}/{total}')
    log_job_identity(job, details)
    print('  Reason: ' + sanitize_diagnostic_text(reason))


def exhausted_gemini_503(error):
    """Recognize only request-wrapper failures emitted after bounded retries."""
    message = str(error)
    if not message.startswith('Gemini was unable to score the job. '):
        return False
    cause = error.__cause__
    if cause is not None and getattr(cause, 'code', None) == 503:
        return True
    return bool(re.match(r'Gemini was unable to score the job\. (?:ServerError|APIError|HTTPStatusError): (?:HTTP )?503\b', message))


def log_job_identity(job, details=None):
    details = details or {}
    print('  Record key: ' + sanitize_diagnostic_text(job.get('record_key') or '<missing>'))
    for label, value in [
        ('Canonical identity', details.get('identity') or job.get('canonical_job_key')),
        ('Exact identity', details.get('exact_identity') or job.get('exact_posting_key') or job.get('canonical_job_key') or job.get('record_key')),
    ]:
        value = str(value or '<unavailable>')
        safe = sanitize_diagnostic_text(value)
        if safe != value:
            import hashlib
            safe += ' [sha256:' + hashlib.sha256(value.encode()).hexdigest() + ']'
        print('  ' + label + ': ' + safe)
