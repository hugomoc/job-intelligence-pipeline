"""Attach Description (V1): a user-pasted job description for one saved job.

A paste never verifies an employer or posting and never calls AI. The text is
stored in ``raw_jobs.description`` (the single text every stage reads); this
module records its provenance, one previous version for Undo, and supplies the
narrow scoring exception for a historical ``resolution_rejected`` attempt that
applies only while the active paste is exactly the current description.
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass
from typing import Any

from src.description_quality import MINIMUM_FULL_DESCRIPTION_WORDS, assess_description_quality

ATTACHMENT_TABLE = "job_description_attachments"
PROVENANCE_USER_PASTED = "user_pasted"
STATUS_ACTIVE = "active"
STATUS_SUPERSEDED = "superseded"
SUPERSEDED_BY_VERIFIED_OFFICIAL = "verified_official"
MAX_PASTE_CHARACTERS = 150_000
MAX_PASTE_WORDS = 20_000
FORM_DESCRIPTION_STATES = frozenset({"NEEDS_ENRICHMENT", "PARTIAL_JD", "ENRICHMENT_REJECTED"})
WRONG_JOB_WARNING = ("This description may belong to a different job. "
                     "Please confirm the company and position.")
USER_PASTED_RETAINED = "User-pasted description retained; unverified fetched text was not saved."

ATTACHMENT_DDL = f"""
CREATE TABLE IF NOT EXISTS {ATTACHMENT_TABLE} (
    record_key VARCHAR PRIMARY KEY,
    provenance VARCHAR NOT NULL,
    status VARCHAR NOT NULL,
    pasted_description VARCHAR NOT NULL,
    description_hash VARCHAR NOT NULL,
    word_count INTEGER NOT NULL,
    attached_at TIMESTAMPTZ NOT NULL,
    has_previous BOOLEAN NOT NULL,
    previous_description VARCHAR,
    previous_description_updated_at TIMESTAMPTZ,
    previous_was_user_pasted BOOLEAN NOT NULL,
    previous_attached_at TIMESTAMPTZ,
    superseded_at TIMESTAMPTZ,
    superseded_by VARCHAR
)
"""


class AttachmentError(ValueError):
    """The attachment request cannot be applied to this record."""


# --------------------------------------------------------------------------- text
_ZERO_WIDTH = re.compile("[​‌‍⁠﻿]")
_HTML_TAG = re.compile(r"<\s*/?\s*[A-Za-z][^<>]*>")
_DUPLICATE_LINE_MIN_WORDS = 6


def normalize_pasted_description(value: Any) -> tuple[str, int]:
    """Plain text with normalized whitespace; returns (text, repeated blocks removed)."""
    text = _ZERO_WIDTH.sub("", str(value or ""))
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\xa0", " ")
    if _HTML_TAG.search(text):
        from src.enrichment.job_description import html_fragment_to_text
        text = html_fragment_to_text(text)
    else:
        text = html.unescape(text)
    removed = 0
    flat = " ".join(text.split())
    words = flat.split()
    half = len(words) // 2
    if len(words) >= 20 and len(words) % 2 == 0 and words[:half] == words[half:]:
        # The whole text was pasted twice: keep the first copy's line structure.
        lines_all = text.split("\n")
        for cut in range(1, len(lines_all)):
            if " ".join(" ".join(lines_all[:cut]).split()) == " ".join(words[:half]):
                text = "\n".join(lines_all[:cut])
                break
        else:
            text = " ".join(words[:half])
        removed += 1
    lines, seen = [], set()
    for line in text.split("\n"):
        line = re.sub(r"[ \t]+", " ", line).strip()
        key = line.casefold()
        if len(line.split()) >= _DUPLICATE_LINE_MIN_WORDS:
            if key in seen:
                removed += 1
                continue
            seen.add(key)
        lines.append(line)
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return text, removed


_TITLE_GENERIC = frozenset({
    "senior", "sr", "junior", "jr", "lead", "staff", "principal", "i", "ii", "iii", "iv", "v",
    "remote", "hybrid", "onsite", "contract", "temporary", "the", "and", "of", "for", "in",
    "to", "a", "an", "with", "at", "level", "usa", "us",
})
_TITLE_ABBREVIATIONS = {"bi": "business intelligence", "eng": "engineer", "mgr": "manager",
                        "ml": "machine learning", "ai": "artificial intelligence",
                        "swe": "software engineer", "de": "data engineer"}
_COMPANY_STOP = frozenset({"the", "a", "an", "and", "of", "at", "for", "in", "on", "by", "to", "with"})


def _tokens(value: Any) -> list[str]:
    return re.findall(r"[a-z0-9]+", str(value or "").casefold())


def wrong_job_warning(text: str, title: Any, company: Any) -> str | None:
    """Warn (never block) when neither the company nor the title is recognizable."""
    from src.enrichment.job_identity import normalize_company_name
    words = _tokens(text)
    vocabulary = set(words)
    stems = {word[:5] for word in vocabulary if len(word) >= 5}
    joined = " " + " ".join(words) + " "
    company_name = normalize_company_name(str(company or ""))
    significant = [t for t in company_name.split() if t not in _COMPANY_STOP and len(t) >= 3]
    company_seen = bool(company_name) and (
        f" {company_name} " in joined or bool(significant and significant[0] in vocabulary))
    title_tokens = []
    for token in _tokens(title):
        for part in _TITLE_ABBREVIATIONS.get(token, token).split():
            if part not in _TITLE_GENERIC and len(part) >= 3 and part not in title_tokens:
                title_tokens.append(part)
    found = sum(1 for t in title_tokens if t in vocabulary or (len(t) >= 5 and t[:5] in stems))
    title_seen = bool(title_tokens) and found >= min(2, len(title_tokens))
    if company_seen or title_seen or not (company_name or title_tokens):
        return None
    return WRONG_JOB_WARNING


@dataclass(frozen=True)
class PasteValidation:
    accepted: bool
    text: str
    word_count: int
    reason: str
    duplicates_removed: int = 0
    warning: str | None = None


_QUALITY_MESSAGES = {
    "below minimum description length": (
        "This looks short or incomplete ({words} words). A complete job description needs at "
        f"least {MINIMUM_FULL_DESCRIPTION_WORDS} words with responsibilities or requirements."),
    "search/listing or navigation-dominated content": (
        "This looks like a search results, listing or navigation page, not one job description."),
    "insufficient JD structure/content evidence": (
        "This text lacks the responsibilities/requirements content of a complete job description."),
}


def validate_pasted_description(value: Any, *, title: Any = None, company: Any = None) -> PasteValidation:
    """Deterministic checks only. A FULL_JD is decided by the existing quality rules."""
    raw = str(value or "")
    if not raw.strip():
        return PasteValidation(False, "", 0, "Paste a job description first.")
    if len(raw) > MAX_PASTE_CHARACTERS:
        return PasteValidation(False, "", len(raw.split()),
                               "This looks like a whole page rather than one job description.")
    text, removed = normalize_pasted_description(raw)
    words = len(text.split())
    if words > MAX_PASTE_WORDS:
        return PasteValidation(False, text, words,
                               "This looks like a whole page rather than one job description.", removed)
    quality = assess_description_quality(text)
    if not quality.full_jd:
        message = _QUALITY_MESSAGES.get(quality.reason, quality.reason).format(words=words)
        return PasteValidation(False, text, words, message, removed)
    return PasteValidation(True, text, words, quality.reason, removed,
                           wrong_job_warning(text, title, company))


# ------------------------------------------------------------------- eligibility
def enrichment_rejection_applies(job: dict[str, Any]) -> bool:
    """A historical ``resolution_rejected`` attempt blocks scoring unless an
    active, hash-matching user paste is the current description (Option B)."""
    return (str(job.get("enrichment_status") or "") == "resolution_rejected"
            and not job.get("manual_description_active"))


def _content_hash(value: Any) -> str:
    from src.verified_posting_identity import description_hash
    return description_hash(None if value is None else str(value))


def attachment_matches(stored_hash: Any, description: Any) -> bool:
    """Active only while the paste is exactly (normalized) the current FULL_JD text."""
    return (bool(stored_hash) and stored_hash == _content_hash(description)
            and assess_description_quality(description).full_jd)


def ensure_attachment_table(connection) -> None:
    connection.execute(ATTACHMENT_DDL)


def attachment_table_exists(connection) -> bool:
    return bool(connection.execute(
        "SELECT count(*) FROM information_schema.tables WHERE table_name = ?",
        [ATTACHMENT_TABLE]).fetchone()[0])


def active_attachment_record_keys(connection) -> set[str]:
    if not attachment_table_exists(connection):
        return set()
    rows = connection.execute(
        f"""SELECT a.record_key, a.description_hash, r.description
            FROM {ATTACHMENT_TABLE} a JOIN raw_jobs r ON r.record_key = a.record_key
            WHERE a.status = ? AND a.provenance = ?""",
        [STATUS_ACTIVE, PROVENANCE_USER_PASTED]).fetchall()
    return {key for key, stored, description in rows if attachment_matches(stored, description)}


def register_attachment_function(connection) -> None:
    """SQL ``description_attachment_active(record_key)`` for the scoring loaders."""
    keys = active_attachment_record_keys(connection)
    connection.create_function("description_attachment_active", lambda key: key in keys,
                               [str], bool, null_handling="special")


def effective_enrichment_status_sql(column: str = "status", key: str = "record_key") -> str:
    """Status as seen by the scoring loaders; the stored attempt row is untouched."""
    return (f"CASE WHEN {column} = 'resolution_rejected' AND description_attachment_active({key}) "
            f"THEN 'user_pasted_description' ELSE {column} END")


def annotate_description_attachments(connection, jobs: list[dict[str, Any]]) -> None:
    """Add display/eligibility flags for each job's own record_key (never propagated)."""
    rows = {}
    if attachment_table_exists(connection):
        rows = {row[0]: row for row in connection.execute(
            f"""SELECT record_key, status, description_hash, has_previous, attached_at,
                       superseded_at, superseded_by, provenance
                FROM {ATTACHMENT_TABLE}""").fetchall()}
    for job in jobs:
        row = rows.get(job.get("record_key"))
        job.update(manual_description_status=None, manual_description_active=False,
                   manual_description_has_previous=False, manual_description_attached_at=None,
                   manual_description_superseded_at=None, manual_description_superseded_by=None)
        if row is None:
            continue
        _, status, stored_hash, has_previous, attached_at, superseded_at, superseded_by, provenance = row
        active = (status == STATUS_ACTIVE and provenance == PROVENANCE_USER_PASTED
                  and attachment_matches(stored_hash, job.get("description")))
        job.update(
            manual_description_status=(status if status != STATUS_ACTIVE or active else "stale"),
            manual_description_active=active,
            manual_description_has_previous=bool(active and has_previous),
            manual_description_attached_at=attached_at,
            manual_description_superseded_at=superseded_at if status == STATUS_SUPERSEDED else None,
            manual_description_superseded_by=superseded_by)


def attachment_form_mode(job: dict[str, Any]) -> str | None:
    """'attach' (first paste), 'replace' (own active paste) or None (hidden).

    V1: New, unscored jobs only. Hidden for scraped/verified FULL_JD, scored,
    Applied and Removed jobs.
    """
    if str(job.get("application_status") or "new").lower() != "new":
        return None
    if job.get("ai_score") is not None or job.get("candidate_assessment_id"):
        return None
    from src.repositories.recommendation_repository import has_current_complete_ai_score
    if has_current_complete_ai_score(job):
        return None
    if job.get("manual_description_active"):
        return "replace"
    from src.repositories.recommendation_repository import description_state
    return "attach" if description_state(job) in FORM_DESCRIPTION_STATES else None


def superseded_after_scoring(job: dict[str, Any]) -> bool:
    """A completed assessment predates a verified replacement of the user's paste."""
    from src.repositories.recommendation_repository import normalize_datetime
    superseded = normalize_datetime(job.get("manual_description_superseded_at"))
    scored = normalize_datetime(job.get("ai_scored_at"))
    return bool(job.get("ai_score") is not None and superseded and scored and scored < superseded)


# ------------------------------------------------------------------- persistence
@dataclass(frozen=True)
class AttachmentOutcome:
    status: str          # saved | unchanged | rejected | undone
    message: str
    word_count: int = 0
    warning: str | None = None
    duplicates_removed: int = 0


def _attachment_row(connection, record_key):
    row = connection.execute(
        f"""SELECT status, provenance, pasted_description, description_hash, word_count, attached_at,
                   has_previous, previous_description, previous_description_updated_at,
                   previous_was_user_pasted, previous_attached_at
            FROM {ATTACHMENT_TABLE} WHERE record_key = ?""", [record_key]).fetchone()
    if row is None:
        return None
    names = ("status", "provenance", "pasted_description", "description_hash", "word_count",
             "attached_at", "has_previous", "previous_description",
             "previous_description_updated_at", "previous_was_user_pasted", "previous_attached_at")
    return dict(zip(names, row))


def _table_exists(connection, name: str) -> bool:
    return bool(connection.execute(
        "SELECT count(*) FROM information_schema.tables WHERE table_name = ?", [name]).fetchone()[0])


def _blocked_reason(connection, record_key, description, active, resume_hash) -> str | None:
    """Defence in depth for the V1 visibility rule; the UI hides these cases."""
    status = connection.execute(
        """SELECT status FROM application_status WHERE record_key = ?
           ORDER BY updated_at DESC NULLS LAST LIMIT 1""",
        [record_key]).fetchone() if _table_exists(connection, "application_status") else None
    if status and str(status[0] or "new").lower() != "new":
        return "Applied or removed jobs cannot take an attached description."
    if resume_hash:
        from src.candidate_assessments import completed_record_keys, tables_exist
        if tables_exist(connection) and record_key in completed_record_keys(connection, resume_hash):
            return "This job is already scored. Use 'Rescore this job' for a paid re-score."
        scored = connection.execute(
            "SELECT count(*) FROM resume_job_scores WHERE resume_hash = ? AND record_key = ?",
            [resume_hash, record_key]).fetchone()[0] if _table_exists(connection, "resume_job_scores") else 0
        if scored:
            return "This job is already scored. Use 'Rescore this job' for a paid re-score."
    if not active:
        attempt = connection.execute(
            "SELECT status FROM job_enrichment_attempts WHERE record_key = ?",
            [record_key]).fetchone() if _table_exists(connection, "job_enrichment_attempts") else None
        from src.repositories.recommendation_repository import description_state
        state = description_state({"description": description, "enrichment_status": attempt[0] if attempt else None,
                                   "raw_description_word_count": len(str(description or "").split())})
        if state not in FORM_DESCRIPTION_STATES:
            return "This job already has a complete description."
    return None


def refresh_rule_matches_for_record(connection, record_key: str) -> int:
    """Recompute deterministic rule matches for this one record (no network, no AI)."""
    from src.config_loader import load_searches
    from src.matching.job_matcher import score_all_jobs
    from src.score_jobs import JOB_MATCH_INSERT_SQL, job_match_rows
    defaults, searches = load_searches()
    if not searches:
        return 0
    cursor = connection.execute(
        """SELECT record_key, source, source_job_id, title, company_name, location, salary_text,
                  description, apply_url, discovered_at
           FROM raw_jobs WHERE record_key = ?""", [record_key])
    row = cursor.fetchone()
    if row is None:
        return 0
    job = dict(zip([d[0] for d in cursor.description], row))
    rows = job_match_rows(score_all_jobs(jobs=[job], searches=searches, defaults=defaults))
    connection.execute("DELETE FROM job_matches WHERE record_key = ?", [record_key])
    if rows:
        connection.executemany(JOB_MATCH_INSERT_SQL, rows)
    return len(rows)


def _prepare(connection) -> None:
    ensure_attachment_table(connection)
    from src.score_jobs import initialize_match_table
    initialize_match_table()


def save_attached_description(record_key: str, pasted: Any, *,
                              resume_hash: str | None = None) -> AttachmentOutcome:
    """Validate, then one transaction: attachment row, raw description, rule matches.

    Identical content (normalized hash) is a strict no-op.
    """
    from src.database import get_connection
    with get_connection() as connection:
        _prepare(connection)
        job = connection.execute("SELECT title, company_name FROM raw_jobs WHERE record_key = ?",
                                 [record_key]).fetchone()
        if job is None:
            raise AttachmentError("Job not found.")
        validation = validate_pasted_description(pasted, title=job[0], company=job[1])
        if not validation.accepted:
            return AttachmentOutcome("rejected", validation.reason, validation.word_count,
                                     duplicates_removed=validation.duplicates_removed)
        connection.execute("BEGIN TRANSACTION")
        try:
            current, updated_at = connection.execute(
                "SELECT description, description_updated_at FROM raw_jobs WHERE record_key = ?",
                [record_key]).fetchone()
            new_hash = _content_hash(validation.text)
            if new_hash == _content_hash(current):
                connection.execute("ROLLBACK")
                return AttachmentOutcome("unchanged", "No change: this description is already saved.",
                                         validation.word_count)
            attachment = _attachment_row(connection, record_key)
            active = bool(attachment and attachment["status"] == STATUS_ACTIVE
                          and attachment["provenance"] == PROVENANCE_USER_PASTED
                          and attachment_matches(attachment["description_hash"], current))
            blocked = _blocked_reason(connection, record_key, current, active, resume_hash)
            if blocked:
                connection.execute("ROLLBACK")
                return AttachmentOutcome("rejected", blocked, validation.word_count)
            if active:
                previous = (attachment["pasted_description"], updated_at, True, attachment["attached_at"])
            else:
                previous = (current, updated_at, False, None)
            connection.execute(f"DELETE FROM {ATTACHMENT_TABLE} WHERE record_key = ?", [record_key])
            connection.execute(
                f"""INSERT INTO {ATTACHMENT_TABLE} (record_key, provenance, status, pasted_description,
                        description_hash, word_count, attached_at, has_previous, previous_description,
                        previous_description_updated_at, previous_was_user_pasted, previous_attached_at,
                        superseded_at, superseded_by)
                    VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, TRUE, ?, ?, ?, ?, NULL, NULL)""",
                [record_key, PROVENANCE_USER_PASTED, STATUS_ACTIVE, validation.text, new_hash,
                 validation.word_count, *previous])
            changed = connection.execute(
                """UPDATE raw_jobs SET description = ?, description_updated_at = CURRENT_TIMESTAMP
                   WHERE record_key = ? AND description IS NOT DISTINCT FROM ?
                   RETURNING record_key""", [validation.text, record_key, current]).fetchall()
            if len(changed) != 1:
                raise AttachmentError("The job description changed while saving; please retry.")
            refresh_rule_matches_for_record(connection, record_key)
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise
    note = " Repeated text was removed." if validation.duplicates_removed else ""
    return AttachmentOutcome(
        "saved", f"Description saved ({validation.word_count} words). The job is not verified; "
                 f"it will be considered by the next scoring run.{note}",
        validation.word_count, validation.warning, validation.duplicates_removed)


def undo_attached_description(record_key: str, *, resume_hash: str | None = None) -> AttachmentOutcome:
    """Restore the one previous description (which may be short, partial or empty)."""
    from src.database import get_connection
    with get_connection() as connection:
        _prepare(connection)
        connection.execute("BEGIN TRANSACTION")
        try:
            row = connection.execute(
                "SELECT description FROM raw_jobs WHERE record_key = ?", [record_key]).fetchone()
            if row is None:
                raise AttachmentError("Job not found.")
            current = row[0]
            attachment = _attachment_row(connection, record_key)
            if (not attachment or attachment["status"] != STATUS_ACTIVE
                    or attachment["provenance"] != PROVENANCE_USER_PASTED
                    or attachment["description_hash"] != _content_hash(current)):
                connection.execute("ROLLBACK")
                return AttachmentOutcome("rejected", "Undo is not available: your pasted description "
                                         "is no longer the current description.")
            if not attachment["has_previous"]:
                connection.execute("ROLLBACK")
                return AttachmentOutcome("rejected", "Nothing to undo.")
            blocked = _blocked_reason(connection, record_key, current, True, resume_hash)
            if blocked:
                connection.execute("ROLLBACK")
                return AttachmentOutcome("rejected", blocked)
            restored = attachment["previous_description"]
            changed = connection.execute(
                """UPDATE raw_jobs SET description = ?, description_updated_at = CURRENT_TIMESTAMP
                   WHERE record_key = ? AND description IS NOT DISTINCT FROM ?
                   RETURNING record_key""", [restored, record_key, current]).fetchall()
            if len(changed) != 1:
                raise AttachmentError("The job description changed while undoing; please retry.")
            if attachment["previous_was_user_pasted"]:
                connection.execute(
                    f"""UPDATE {ATTACHMENT_TABLE} SET pasted_description = ?, description_hash = ?,
                            word_count = ?, attached_at = ?, has_previous = FALSE,
                            previous_description = NULL, previous_description_updated_at = NULL,
                            previous_was_user_pasted = FALSE, previous_attached_at = NULL
                        WHERE record_key = ?""",
                    [restored, _content_hash(restored), len(str(restored or "").split()),
                     attachment["previous_attached_at"], record_key])
            else:
                connection.execute(f"DELETE FROM {ATTACHMENT_TABLE} WHERE record_key = ?", [record_key])
            refresh_rule_matches_for_record(connection, record_key)
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise
    return AttachmentOutcome("undone", "Previous description restored.",
                             len(str(restored or "").split()))


# --------------------------------------------------------------- enrichment guard
def unverified_write_guard_sql(table_alias: str = "raw_jobs") -> str:
    """SQL predicate for a description UPDATE: an untrusted write never replaces an
    active user paste. Bind one boolean parameter: the write is trusted official."""
    return (f"(? OR NOT EXISTS (SELECT 1 FROM {ATTACHMENT_TABLE} attachment "
            f"WHERE attachment.record_key = {table_alias}.record_key "
            f"AND attachment.status = '{STATUS_ACTIVE}'))")


def supersede_attachment(connection, record_key: str) -> None:
    """A verified official description replaced the paste: keep the pasted text,
    end the attachment (no Undo over the official text, no scoring exception)."""
    connection.execute(
        f"""UPDATE {ATTACHMENT_TABLE} SET status = ?, superseded_at = CURRENT_TIMESTAMP,
                superseded_by = ?, has_previous = FALSE
            WHERE record_key = ? AND status = ?""",
        [STATUS_SUPERSEDED, SUPERSEDED_BY_VERIFIED_OFFICIAL, record_key, STATUS_ACTIVE])
