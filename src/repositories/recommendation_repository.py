"""Read/write boundary for Streamlit recommendation data.

The UI should not know table names or SQL details. This repository converts raw
DuckDB tables and dbt marts into dictionaries the Streamlit layer can render,
and it persists user-facing state such as applied/removed and AI eligibility.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit
from urllib.request import Request, build_opener

from src.ai.job_eligibility import (
    ELIGIBILITY_PROMPT_VERSION,
    JobEligibilityDecision,
)
from src.ai.resume_matcher import MATCHER_PROMPT_VERSION
from src.database import get_connection, initialize_database
from src.job_admission import evaluate_job_admission
from src.job_title_filter import (
    EXCLUDED_TITLE_SQL_REGEX,
    classify_job_title,
)
from src.ui.job_visibility import filter_user_reviewable_jobs


EXCLUDED_JOB_SOURCES: tuple[str, ...] = ()
EXCLUDED_JOB_SOURCES_SQL = ", ".join(
    f"'{source}'"
    for source in EXCLUDED_JOB_SOURCES
) or "'__no_excluded_sources__'"

TRACKING_QUERY_PARAMETERS = {
    "ao",
    "campaign",
    "cb",
    "clickid",
    "cs",
    "fbclid",
    "gclid",
    "guid",
    "igshid",
    "imp_id",
    "mc_cid",
    "mc_eid",
    "mkt_tok",
    "ref",
    "ref_src",
    "referrer",
    "s",
    "src",
    "t",
    "tr",
    "uid",
    "uido",
    "utm_campaign",
    "utm_content",
    "utm_id",
    "utm_medium",
    "utm_source",
    "utm_term",
    "vt",
}

EMBEDDED_DESTINATION_PARAMETERS = {
    "dest",
    "destination",
    "href",
    "link",
    "redirect",
    "redirect_uri",
    "redirect_url",
    "target",
    "u",
    "url",
}

REDIRECT_HOST_MARKERS = (
    "sendgrid.net",
    "sg3email.lensa.com",
)

REDIRECT_PATH_MARKERS = (
    "/ls/click",
    "/click",
    "/redirect",
)

JOB_ID_QUERY_PARAMETERS = {
    "gh_jid",
    "jid",
    "job_id",
    "jobid",
    "postingid",
    "requisitionid",
}

HIGH_PRIORITY_RECENT_DAYS = 7
MEDIUM_PRIORITY_RECENT_DAYS = 30
ENRICHMENT_WAITING_PRIORITY_CAP = 30
ENRICHMENT_WAITING_PRIORITY_STEP_DAYS = 3
MINIMUM_FULL_DESCRIPTION_WORDS = 80
DESCRIPTION_QUALITY_PATTERN = re.compile(
    r"\b("
    r"responsibilit(?:y|ies)|requirements?|qualifications?|"
    r"what\s+you(?:'|’)ll\s+do|you\s+will|duties|"
    r"skills?|experience|must\s+have|preferred"
    r")\b",
    re.IGNORECASE,
)

AI_PRIORITY_BY_RECOMMENDATION = {
    "apply": "High",
    "review": "Medium",
    "skip": "Low",
}

DETERMINISTIC_PRIORITY_LABELS = {
    "strong": "High",
    "possible": "Medium",
    "low": "Low",
    "mismatch": "Mismatch",
}


@dataclass(frozen=True)
class PostingIdentity:
    identity: str
    confidence: str
    identity_type: str
    resolved_url: str
    is_redirect: bool


@dataclass(frozen=True)
class FitPriority:
    label: str
    source: str
    reason: str


@dataclass(frozen=True)
class EnrichmentPriority:
    score: int
    tier: str
    reason: str


def normalize_source_name(source: Any) -> str:
    """Normalize source names before building cross-run posting identity."""
    return normalize_duplicate_text(source)


def normalize_apply_url_for_identity(apply_url: Any) -> str:
    """Normalize a URL while preserving non-tracking query parameters."""
    if not apply_url:
        return ""

    raw_url = str(apply_url).strip()

    if not raw_url:
        return ""

    parsed = urlsplit(raw_url)
    scheme = parsed.scheme.casefold() or "https"
    hostname = (parsed.hostname or "").casefold()

    if parsed.port:
        netloc = f"{hostname}:{parsed.port}"
    else:
        netloc = hostname

    path = re.sub(r"/+", "/", parsed.path or "/")
    if path != "/":
        path = path.rstrip("/")

    kept_query_parameters = [
        (key, value)
        for key, value in parse_qsl(
            parsed.query,
            keep_blank_values=True,
        )
        if key.casefold() not in TRACKING_QUERY_PARAMETERS
        and not key.casefold().startswith("utm_")
    ]
    kept_query_parameters.sort(
        key=lambda item: (
            item[0].casefold(),
            item[1],
        )
    )

    query = urlencode(
        kept_query_parameters,
        doseq=True,
    )

    return urlunsplit(
        (
            scheme,
            netloc,
            path,
            query,
            "",
        )
    )


def is_redirect_or_tracking_url(apply_url: Any) -> bool:
    """Return true for click-tracking URLs that need a resolved destination."""
    if not apply_url:
        return False

    parsed = urlsplit(str(apply_url).strip())
    hostname = (parsed.hostname or "").casefold()
    path = (parsed.path or "").casefold()

    if any(hostname.endswith(marker) for marker in REDIRECT_HOST_MARKERS):
        return True

    if any(marker in path for marker in REDIRECT_PATH_MARKERS):
        return True

    query_keys = {
        key.casefold()
        for key, _ in parse_qsl(parsed.query, keep_blank_values=True)
    }
    return bool(query_keys & EMBEDDED_DESTINATION_PARAMETERS)


def _decode_possible_url(value: str) -> str:
    decoded = value.strip()
    for _ in range(3):
        next_decoded = unquote(decoded).strip()
        if next_decoded == decoded:
            break
        decoded = next_decoded
    return decoded


def extract_embedded_destination_url(apply_url: Any) -> str:
    """Extract a real posting URL carried inside a redirect query parameter."""
    if not apply_url:
        return ""

    parsed = urlsplit(str(apply_url).strip())
    query_parameters = parse_qsl(parsed.query, keep_blank_values=True)

    for key, value in query_parameters:
        decoded_value = _decode_possible_url(value)
        if (
            key.casefold() in EMBEDDED_DESTINATION_PARAMETERS
            and decoded_value.startswith(("http://", "https://"))
        ):
            nested_destination = extract_embedded_destination_url(decoded_value)
            return nested_destination or decoded_value

    for _, value in query_parameters:
        decoded_value = _decode_possible_url(value)
        if decoded_value.startswith(("http://", "https://")):
            nested_destination = extract_embedded_destination_url(decoded_value)
            return nested_destination or decoded_value

    return ""


def resolve_redirect_final_url(
    apply_url: Any,
    opener: Any = None,
    timeout_seconds: float = 5.0,
) -> str:
    """Resolve a click-tracking URL to its final destination when possible."""
    raw_url = str(apply_url or "").strip()

    if not raw_url or not is_redirect_or_tracking_url(raw_url):
        return ""

    embedded_destination = extract_embedded_destination_url(raw_url)
    if embedded_destination:
        return embedded_destination

    request = Request(
        raw_url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 job-intelligence-pipeline link resolver"
            ),
        },
    )

    try:
        response = (opener or build_opener()).open(
            request,
            timeout=timeout_seconds,
        )
        with response:
            return str(response.geturl() or "").strip()
    except (HTTPError, URLError, TimeoutError, OSError, ValueError):
        return ""


def _extract_job_id_from_path(pattern: str, normalized_url: str) -> str:
    match = re.search(pattern, normalized_url, flags=re.IGNORECASE)
    return match.group(1) if match else ""


def extract_posting_platform_identity(normalized_url: str) -> str:
    """Return a stable platform job id from known ATS/job-board URL shapes."""
    if not normalized_url:
        return ""

    parsed = urlsplit(normalized_url)
    hostname = (parsed.hostname or "").casefold()
    query = {
        key.casefold(): value
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if value
    }

    for key in JOB_ID_QUERY_PARAMETERS:
        if query.get(key):
            return f"{hostname}:{key}:{query[key]}"

    linkedin_job_id = _extract_job_id_from_path(
        r"/jobs/view/([0-9]+)",
        normalized_url,
    )
    if linkedin_job_id:
        return f"linkedin:{linkedin_job_id}"

    greenhouse_job_id = _extract_job_id_from_path(
        r"/jobs/([0-9]+)",
        normalized_url,
    )
    if "greenhouse.io" in hostname and greenhouse_job_id:
        return f"greenhouse:{greenhouse_job_id}"

    lever_job_id = _extract_job_id_from_path(
        r"/([^/?#]+)$",
        normalized_url,
    )
    if "lever.co" in hostname and lever_job_id:
        return f"lever:{lever_job_id}"

    ashby_job_id = _extract_job_id_from_path(
        r"/job/([^/?#]+)",
        normalized_url,
    )
    if "ashbyhq.com" in hostname and ashby_job_id:
        return f"ashby:{ashby_job_id}"

    return ""


def build_posting_identity(
    source: Any,
    source_job_id: Any,
    apply_url: Any,
    record_key: Any,
    resolved_apply_url: Any = None,
) -> PostingIdentity:
    """Build the safest available identity for propagating user job status."""
    normalized_source = normalize_source_name(source)
    normalized_source_job_id = normalize_duplicate_text(source_job_id)

    if normalized_source_job_id:
        return PostingIdentity(
            identity=f"{normalized_source}|{normalized_source_job_id}",
            confidence="high",
            identity_type="source_job_id",
            resolved_url="",
            is_redirect=False,
        )

    raw_apply_url = str(apply_url or "").strip()
    record_identity = f"{normalized_source}|record:{record_key or ''}"
    redirect_url = is_redirect_or_tracking_url(raw_apply_url)
    destination_url = (
        str(resolved_apply_url or "").strip()
        or extract_embedded_destination_url(raw_apply_url)
    )

    if redirect_url and not destination_url:
        return PostingIdentity(
            identity=record_identity,
            confidence="low",
            identity_type="record_key",
            resolved_url="",
            is_redirect=True,
        )

    identity_url = destination_url or raw_apply_url
    normalized_url = normalize_apply_url_for_identity(identity_url)
    platform_identity = extract_posting_platform_identity(normalized_url)

    if platform_identity:
        return PostingIdentity(
            identity=f"{normalized_source}|{platform_identity}",
            confidence="high",
            identity_type="platform_job_id",
            resolved_url=normalized_url,
            is_redirect=redirect_url,
        )

    if normalized_url:
        return PostingIdentity(
            identity=f"{normalized_source}|{normalized_url}",
            confidence="medium" if not redirect_url else "high",
            identity_type="direct_url" if not redirect_url else "resolved_url",
            resolved_url=normalized_url,
            is_redirect=redirect_url,
        )

    return PostingIdentity(
        identity=record_identity,
        confidence="low",
        identity_type="record_key",
        resolved_url="",
        is_redirect=redirect_url,
    )


def exact_posting_identity(
    source: Any,
    source_job_id: Any,
    apply_url: Any,
    record_key: Any,
) -> str:
    """Return the status identity for one exact external posting."""
    return build_posting_identity(
        source=source,
        source_job_id=source_job_id,
        apply_url=apply_url,
        record_key=record_key,
    ).identity


def register_exact_posting_identity_function(connection) -> None:
    """Expose exact_posting_identity to DuckDB queries on this connection."""
    try:
        connection.create_function(
            "exact_posting_identity",
            exact_posting_identity,
            [str, str, str, str],
            str,
            null_handling="special",
        )
    except Exception:
        # DuckDB raises if the function is already registered on a connection.
        pass


def parse_json_list(value: str | None) -> list[str]:
    if not value:
        return []

    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return []

    if not isinstance(parsed, list):
        return []

    return [str(item) for item in parsed]


def parse_email_datetime(value: str | None) -> datetime | None:
    if not value:
        return None

    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)

    return parsed.astimezone(timezone.utc)


def normalize_datetime(value: Any) -> datetime | None:
    """Return a timezone-aware datetime from email/discovery date values."""
    if value is None:
        return None

    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)

        return value.astimezone(timezone.utc)

    parsed = parse_email_datetime(str(value))

    if parsed is not None:
        return parsed

    try:
        iso_value = str(value).strip().replace("Z", "+00:00")
        parsed_iso = datetime.fromisoformat(iso_value)
    except ValueError:
        return None

    if parsed_iso.tzinfo is None:
        return parsed_iso.replace(tzinfo=timezone.utc)

    return parsed_iso.astimezone(timezone.utc)


def best_job_date(job: dict[str, Any]) -> datetime | None:
    """Use email date first, then discovered_at, for recency decisions."""
    for field_name in ("sent_at", "email_date", "discovered_at"):
        parsed = normalize_datetime(job.get(field_name))

        if parsed is not None:
            return parsed

    return None


def job_age_days(
    job: dict[str, Any],
    now: datetime | None = None,
) -> int | None:
    job_date = best_job_date(job)

    if job_date is None:
        return None

    current_time = now or datetime.now(timezone.utc)

    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=timezone.utc)

    return max(
        0,
        (current_time.astimezone(timezone.utc) - job_date).days,
    )


def enrichment_waiting_days(
    job: dict[str, Any],
    now: datetime | None = None,
) -> int | None:
    """Estimate how long a still-incomplete job has waited for enrichment."""
    desc_state = description_state(job)

    if desc_state == "FULL_JD":
        return None

    reference_time = now or datetime.now(timezone.utc)

    if reference_time.tzinfo is None:
        reference_time = reference_time.replace(tzinfo=timezone.utc)

    last_attempted = normalize_datetime(
        job.get("previous_attempted_at")
        or job.get("enrichment_attempted_at")
        or job.get("official_url_resolved_at")
    )
    waiting_since = last_attempted or best_job_date(job)

    if waiting_since is None:
        return None

    return max(
        0,
        (
            reference_time.astimezone(timezone.utc)
            - waiting_since.astimezone(timezone.utc)
        ).days,
    )


def description_has_quality_signals(description: Any) -> bool:
    """Detect whether text looks like a real JD, not only long page chrome."""
    return bool(
        description
        and DESCRIPTION_QUALITY_PATTERN.search(str(description))
    )


def current_description_word_count(job: dict[str, Any]) -> int:
    word_count = job.get("raw_description_word_count")

    if word_count is None:
        word_count = job.get("description_word_count")

    try:
        return int(word_count or 0)
    except (TypeError, ValueError):
        return 0


def has_quality_description(job: dict[str, Any]) -> bool:
    if current_description_word_count(job) < MINIMUM_FULL_DESCRIPTION_WORDS:
        return False

    if job.get("description_quality_signals") is not None:
        return bool(job.get("description_quality_signals"))

    return description_has_quality_signals(job.get("description"))


def has_current_complete_ai_score(job: dict[str, Any]) -> bool:
    """Return true when a score can be treated as the current final answer."""
    if job.get("ai_score") is None:
        return False

    if job.get("ai_prompt_version") not in (None, MATCHER_PROMPT_VERSION):
        return False

    if bool(job.get("has_incomplete_description")):
        return False

    if job.get("description_complete") is False:
        return False

    if current_description_word_count(job) < MINIMUM_FULL_DESCRIPTION_WORDS:
        return False

    if job.get("description_state") not in (None, "FULL_JD"):
        return False

    scored_at = normalize_datetime(job.get("ai_scored_at"))
    description_updated_at = normalize_datetime(
        job.get("description_updated_at")
    )

    if scored_at and description_updated_at and scored_at < description_updated_at:
        return False

    return True


def derive_fit_priority(job: dict[str, Any]) -> FitPriority:
    """Derive the display priority without using it as a hidden filter."""
    if has_current_complete_ai_score(job):
        recommendation = str(job.get("recommendation") or "").casefold()
        label = AI_PRIORITY_BY_RECOMMENDATION.get(recommendation)

        if label:
            return FitPriority(
                label=label,
                source="AI assessment",
                reason=f"Current AI recommendation is {recommendation.upper()}.",
            )

    if job.get("description_state") in {
        "NEEDS_ENRICHMENT",
        "PARTIAL_JD",
        "ENRICHMENT_REJECTED",
    }:
        if job.get("title_classification") == "STRONG_MATCH":
            return FitPriority(
                label="Medium",
                source="Needs enrichment",
                reason="Strong title match, but the JD is not complete enough for final AI priority.",
            )

        if job.get("title_classification") == "POSSIBLE_MATCH":
            return FitPriority(
                label="Low",
                source="Needs enrichment",
                reason="Possible title match, but the JD needs enrichment before final ranking.",
            )

    if job.get("title_classification") == "FILTERED_OUT":
        return FitPriority(
            label="Mismatch",
            source="Pre-screening",
            reason="Title classification is outside the target lane.",
        )

    if job.get("critical_skill_gaps"):
        return FitPriority(
            label="Low",
            source="Pre-screening",
            reason="Deterministic screening found critical skill or specialization gaps.",
        )

    if job.get("admission_decision") == "include":
        if job.get("role_family_match") == "strong":
            return FitPriority(
                label="High",
                source="Pre-screening",
                reason="Deterministic screening found a strong target role fit.",
            )

        return FitPriority(
            label="Medium",
            source="Pre-screening",
            reason="Deterministic screening found a possible fit.",
        )

    if job.get("role_family_match") in {"strong", "possible"}:
        return FitPriority(
            label="Low",
            source="Pre-screening",
            reason="Base title is relevant, but evidence of fit is limited.",
        )

    return FitPriority(
        label="Mismatch",
        source="Pre-screening",
        reason="No strong role-family fit was found.",
    )


def calculate_enrichment_priority(
    job: dict[str, Any],
    now: datetime | None = None,
) -> EnrichmentPriority:
    """Score enrichment candidates so scarce fetches go to useful jobs first."""
    score = 0
    reasons: list[str] = []
    status = str(job.get("application_status") or "new").casefold()
    title_classification = str(
        job.get("title_classification") or "POSSIBLE_MATCH"
    )
    desc_state = description_state(job)
    age = job_age_days(job, now=now)
    attempt_count = int(job.get("attempt_count") or 0)
    waiting_days = enrichment_waiting_days(job, now=now)

    if status == "new":
        score += 40
        reasons.append("new")
    elif status == "applied":
        score -= 35
        reasons.append("applied penalty")
    elif status == "removed":
        score -= 100
        reasons.append("removed penalty")

    if title_classification == "STRONG_MATCH":
        score += 30
        reasons.append("strong title")
    elif title_classification == "POSSIBLE_MATCH":
        score += 15
        reasons.append("possible title")
    elif title_classification == "FILTERED_OUT":
        score -= 100
        reasons.append("title mismatch penalty")

    if job.get("critical_skill_gaps"):
        score -= 35
        reasons.append("critical gap penalty")

    if job.get("admission_decision") == "exclude":
        score -= 25
        reasons.append("pre-screen exclude penalty")

    if age is not None:
        if age <= HIGH_PRIORITY_RECENT_DAYS:
            score += 20
            reasons.append(f"recent <= {HIGH_PRIORITY_RECENT_DAYS}d")
        elif age <= MEDIUM_PRIORITY_RECENT_DAYS:
            score += 10
            reasons.append(f"recent <= {MEDIUM_PRIORITY_RECENT_DAYS}d")
        else:
            reasons.append(f"older than {MEDIUM_PRIORITY_RECENT_DAYS}d")

    if job.get("ai_score") is None:
        score += 10
        reasons.append("not AI scored")
    else:
        score -= 20
        reasons.append("already AI scored")

    if desc_state == "NEEDS_ENRICHMENT":
        score += 15
        reasons.append("no JD")
    elif desc_state == "PARTIAL_JD":
        score += 8
        reasons.append("partial JD")
    elif desc_state == "FULL_JD":
        score -= 20
        reasons.append("full JD")
    elif desc_state == "ENRICHMENT_REJECTED":
        score -= 40
        reasons.append("rejected enrichment")

    if (
        status == "new"
        and title_classification in {"STRONG_MATCH", "POSSIBLE_MATCH"}
        and desc_state in {
            "NEEDS_ENRICHMENT",
            "PARTIAL_JD",
            "ENRICHMENT_REJECTED",
        }
    ):
        if attempt_count <= 0:
            score += 20
            reasons.append("never attempted")
        else:
            attempt_penalty = min(20, attempt_count * 4)
            score -= attempt_penalty
            reasons.append(f"attempt penalty -{attempt_penalty}")

        if waiting_days is not None:
            waiting_bonus = min(
                ENRICHMENT_WAITING_PRIORITY_CAP,
                (
                    waiting_days
                    // ENRICHMENT_WAITING_PRIORITY_STEP_DAYS
                )
                * 5,
            )

            if waiting_bonus:
                score += waiting_bonus
                reasons.append(
                    f"waiting {waiting_days}d +{waiting_bonus}"
                )

    rule_score = job.get("rule_score") or job.get("match_score") or 0

    try:
        rule_points = min(10, max(0, int(rule_score) // 10))
    except (TypeError, ValueError):
        rule_points = 0

    if rule_points:
        score += rule_points
        reasons.append(f"rule +{rule_points}")

    if status == "new" and title_classification == "STRONG_MATCH":
        tier = "Tier 1" if age is None or age <= HIGH_PRIORITY_RECENT_DAYS else "Tier 3"
    elif status == "new" and title_classification == "POSSIBLE_MATCH":
        tier = "Tier 2" if age is None or age <= HIGH_PRIORITY_RECENT_DAYS else "Tier 4"
    else:
        tier = "Lowest priority"

    return EnrichmentPriority(
        score=score,
        tier=tier,
        reason=", ".join(reasons) or "No priority signals",
    )


def normalize_duplicate_text(value: Any) -> str:
    """Normalize display fields for conservative UI duplicate collapsing."""
    if value is None:
        return ""

    normalized = str(value).casefold()
    normalized = re.sub(
        r"[^a-z0-9]+",
        " ",
        normalized,
    )

    return re.sub(
        r"\s+",
        " ",
        normalized,
    ).strip()


def ui_duplicate_key(job: dict[str, Any]) -> tuple[str, str, str, str]:
    """Return a conservative display key used to collapse duplicate cards."""
    duplicate_fingerprint = str(
        job.get("duplicate_fingerprint") or ""
    ).strip()
    source = normalize_duplicate_text(job.get("source"))

    if duplicate_fingerprint and source:
        return (
            "fingerprint",
            source,
            duplicate_fingerprint,
            "",
        )

    return (
        normalize_duplicate_text(job.get("title")),
        normalize_duplicate_text(job.get("company_name")),
        normalize_duplicate_text(job.get("source")),
        normalize_duplicate_text(job.get("location")),
    )


def initialize_job_eligibility_table() -> None:
    initialize_database()

    with get_connection() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS resume_job_eligibility (
                resume_hash VARCHAR NOT NULL,
                canonical_job_key VARCHAR NOT NULL,
                record_key VARCHAR NOT NULL,
                decision VARCHAR NOT NULL,
                confidence VARCHAR NOT NULL,
                reason VARCHAR NOT NULL,
                matched_resume_signals VARCHAR,
                missing_or_mismatched_signals VARCHAR,
                model_name VARCHAR NOT NULL,
                prompt_version VARCHAR NOT NULL,
                screened_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (
                    resume_hash,
                    canonical_job_key,
                    prompt_version
                )
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS job_enrichment_attempts (
                record_key VARCHAR PRIMARY KEY,
                source VARCHAR NOT NULL,
                requested_url VARCHAR NOT NULL,
                final_url VARCHAR,
                status VARCHAR NOT NULL,
                http_status INTEGER,
                extraction_method VARCHAR,
                description_word_count INTEGER NOT NULL,
                error_message VARCHAR,
                attempt_count INTEGER NOT NULL DEFAULT 1,
                last_attempted_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        for statement in (
            """
            ALTER TABLE job_enrichment_attempts
            ADD COLUMN IF NOT EXISTS resolved_candidate_title VARCHAR
            """,
            """
            ALTER TABLE job_enrichment_attempts
            ADD COLUMN IF NOT EXISTS resolved_candidate_company VARCHAR
            """,
            """
            ALTER TABLE job_enrichment_attempts
            ADD COLUMN IF NOT EXISTS resolved_candidate_location VARCHAR
            """,
            """
            ALTER TABLE job_enrichment_attempts
            ADD COLUMN IF NOT EXISTS identity_confidence DOUBLE
            """,
            """
            ALTER TABLE job_enrichment_attempts
            ADD COLUMN IF NOT EXISTS title_similarity DOUBLE
            """,
            """
            ALTER TABLE job_enrichment_attempts
            ADD COLUMN IF NOT EXISTS company_similarity DOUBLE
            """,
            """
            ALTER TABLE job_enrichment_attempts
            ADD COLUMN IF NOT EXISTS identity_validation_reason VARCHAR
            """,
            """
            ALTER TABLE job_enrichment_attempts
            ADD COLUMN IF NOT EXISTS official_job_url VARCHAR
            """,
            """
            ALTER TABLE job_enrichment_attempts
            ADD COLUMN IF NOT EXISTS official_url_status VARCHAR
            """,
            """
            ALTER TABLE job_enrichment_attempts
            ADD COLUMN IF NOT EXISTS official_url_source VARCHAR
            """,
            """
            ALTER TABLE job_enrichment_attempts
            ADD COLUMN IF NOT EXISTS official_url_resolved_at TIMESTAMPTZ
            """,
            """
            ALTER TABLE job_enrichment_attempts
            ADD COLUMN IF NOT EXISTS official_url_confidence DOUBLE
            """,
            """
            ALTER TABLE job_enrichment_attempts
            ADD COLUMN IF NOT EXISTS official_url_validation_reason VARCHAR
            """,
            """
            ALTER TABLE job_enrichment_attempts
            ADD COLUMN IF NOT EXISTS official_resolved_title VARCHAR
            """,
            """
            ALTER TABLE job_enrichment_attempts
            ADD COLUMN IF NOT EXISTS official_resolved_company VARCHAR
            """,
            """
            ALTER TABLE job_enrichment_attempts
            ADD COLUMN IF NOT EXISTS official_resolved_location VARCHAR
            """,
        ):
            connection.execute(statement)


def load_latest_cached_resume_hash() -> str | None:
    """Return the newest cached resume profile for display-only fallbacks."""
    initialize_database()

    with get_connection() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS resume_profiles (
                resume_hash VARCHAR PRIMARY KEY,
                filename VARCHAR NOT NULL,
                model_name VARCHAR NOT NULL,
                profile_json VARCHAR NOT NULL,
                created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        result = connection.execute(
            """
            SELECT resume_hash
            FROM resume_profiles
            ORDER BY created_at DESC NULLS LAST
            LIMIT 1
            """
        ).fetchone()

    if not result:
        return None

    return str(result[0])


def resolve_display_resume_hash(
    resume_hash: str | None,
) -> str:
    """Use the active resume when present, otherwise the latest cached one."""
    if resume_hash:
        return resume_hash

    return load_latest_cached_resume_hash() or ""


def load_resume_profile_payload(
    resume_hash: str | None,
) -> dict[str, Any] | None:
    """Load the cached profile JSON used by the deterministic admission gate."""
    if not resume_hash:
        return None

    initialize_database()

    with get_connection() as connection:
        profile_table = connection.execute(
            """
            SELECT COUNT(*)
            FROM information_schema.tables
            WHERE table_name = 'resume_profiles'
            """
        ).fetchone()

        if not profile_table or not profile_table[0]:
            return None

        result = connection.execute(
            """
            SELECT profile_json
            FROM resume_profiles
            WHERE resume_hash = ?
            ORDER BY created_at DESC NULLS LAST
            LIMIT 1
            """,
            [resume_hash],
        ).fetchone()

    if not result:
        return None

    try:
        profile = json.loads(result[0])
    except json.JSONDecodeError:
        return None

    return profile if isinstance(profile, dict) else None


def description_state(job: dict[str, Any]) -> str:
    """Classify JD quality independently from resume-fit evaluation."""
    enrichment_status = str(job.get("enrichment_status") or "")

    if enrichment_status == "resolution_rejected":
        return "ENRICHMENT_REJECTED"

    words = current_description_word_count(job)

    if words >= MINIMUM_FULL_DESCRIPTION_WORDS and has_quality_description(job):
        return "FULL_JD"

    if words > 0:
        return "PARTIAL_JD"

    return "NEEDS_ENRICHMENT"


def apply_admission_gate(
    jobs: list[dict[str, Any]],
    resume_hash: str | None,
    preserve_statuses: tuple[str, ...] = (),
    include_low_priority: bool = True,
    keep_filtered_out: bool = False,
) -> list[dict[str, Any]]:
    """Attach resume-aware fit metadata and remove hard mismatches.

    ``include_low_priority`` keeps soft/uncertain target-lane jobs visible. It
    must not resurrect explicit hard exclusions such as critical title
    specializations missing from the production resume.
    """
    resume_profile = load_resume_profile_payload(resume_hash)

    if resume_profile is None:
        for job in jobs:
            job["description_state"] = description_state(job)
        return jobs

    admitted_jobs: list[dict[str, Any]] = []
    preserved_statuses = set(preserve_statuses) | {"applied", "removed"}

    for job in jobs:
        job["description_state"] = description_state(job)
        evaluation = evaluate_job_admission(
            job=job,
            resume_profile=resume_profile,
        )

        job["admission_decision"] = evaluation.admission_decision
        job["admission_reason"] = evaluation.admission_reason
        job["role_family_match"] = evaluation.role_family_match
        job["specialization_match"] = evaluation.specialization_match
        job["required_skill_match"] = evaluation.required_skill_match
        job["responsibility_match"] = evaluation.responsibility_match
        job["seniority_match"] = evaluation.seniority_match
        job["job_seniority_level"] = evaluation.job_seniority_level
        job["critical_skill_gaps"] = evaluation.critical_skill_gaps
        job["matched_resume_signals"] = evaluation.matched_resume_signals
        title_classification = (
            job.get("title_classification")
            or classify_job_title(job.get("title")).category
        )

        is_preserved_status = job.get("application_status") in preserved_statuses
        is_filtered_title = title_classification == "FILTERED_OUT"
        has_critical_gaps = bool(evaluation.critical_skill_gaps)
        is_hard_exclusion = has_critical_gaps or (
            is_filtered_title
            and not keep_filtered_out
        )
        is_soft_low_priority = (
            include_low_priority
            and not is_hard_exclusion
            and evaluation.admission_decision == "exclude"
        )

        if (
            is_preserved_status
            or evaluation.admission_decision == "include"
            or is_soft_low_priority
        ):
            admitted_jobs.append(job)

    return admitted_jobs


def save_job_eligibility_decision(
    decision: JobEligibilityDecision,
) -> None:
    initialize_job_eligibility_table()

    analysis = decision.analysis
    screened_at = datetime.now(timezone.utc)

    with get_connection() as connection:
        connection.execute(
            """
            INSERT INTO resume_job_eligibility (
                resume_hash,
                canonical_job_key,
                record_key,
                decision,
                confidence,
                reason,
                matched_resume_signals,
                missing_or_mismatched_signals,
                model_name,
                prompt_version,
                screened_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (
                resume_hash,
                canonical_job_key,
                prompt_version
            ) DO UPDATE SET
                record_key = excluded.record_key,
                decision = excluded.decision,
                confidence = excluded.confidence,
                reason = excluded.reason,
                matched_resume_signals = excluded.matched_resume_signals,
                missing_or_mismatched_signals = excluded.missing_or_mismatched_signals,
                model_name = excluded.model_name,
                screened_at = excluded.screened_at
            """,
            [
                decision.resume_hash,
                decision.canonical_job_key,
                decision.record_key,
                analysis.decision,
                analysis.confidence,
                analysis.reason,
                json.dumps(
                    analysis.matched_resume_signals,
                    ensure_ascii=False,
                ),
                json.dumps(
                    analysis.missing_or_mismatched_signals,
                    ensure_ascii=False,
                ),
                decision.model_name,
                decision.prompt_version,
                screened_at,
            ],
        )


def load_all_jobs(
    resume_hash: str | None = None,
) -> list[dict[str, Any]]:
    """Load reviewable UI jobs for one resume with fit/status metadata."""
    initialize_job_eligibility_table()
    selected_resume_hash = resolve_display_resume_hash(resume_hash)

    with get_connection() as connection:
        register_exact_posting_identity_function(connection)
        cursor = connection.execute(
            """
            WITH raw_jobs_with_identity AS (
                SELECT
                    *,
                    COALESCE(
                        NULLIF(job_fingerprint, ''),
                        record_key
                    ) AS duplicate_fingerprint,
                    exact_posting_identity(
                        source,
                        source_job_id,
                        apply_url,
                        record_key
                    ) AS exact_posting_key,
                    exact_posting_identity(
                        source,
                        source_job_id,
                        apply_url,
                        record_key
                    ) AS canonical_job_key,
                    exact_posting_identity(
                        source,
                        source_job_id,
                        apply_url,
                        record_key
                    ) AS posting_status_key
                FROM raw_jobs
            ),

            jobs AS (
                SELECT *
                FROM raw_jobs_with_identity
                QUALIFY ROW_NUMBER() OVER (
                    PARTITION BY canonical_job_key
                    ORDER BY
                        CASE
                            WHEN description IS NOT NULL
                             AND TRIM(description) <> ''
                            THEN 1
                            ELSE 0
                        END DESC,
                        description_updated_at DESC NULLS LAST,
                        discovered_at DESC NULLS LAST,
                        record_key
                ) = 1
            ),

            best_matches AS (
                SELECT
                    matched_jobs.canonical_job_key,
                    search_title,
                    match_score,
                    row_number() over (
                        partition by matched_jobs.canonical_job_key
                        order by
                            is_recommended desc,
                            needs_review desc,
                            match_score desc,
                            title_score desc,
                            search_id
                    ) as match_rank
                FROM job_matches
                INNER JOIN raw_jobs_with_identity AS matched_jobs
                    ON job_matches.record_key =
                       matched_jobs.record_key
            ),

            latest_application_status AS (
                SELECT
                    status_jobs.posting_status_key,
                    status.status,
                    row_number() over (
                        partition by status_jobs.posting_status_key
                        order by
                            status.updated_at desc nulls last,
                            status.record_key
                    ) as status_rank
                FROM application_status as status
                INNER JOIN raw_jobs_with_identity as status_jobs
                    ON status.record_key = status_jobs.record_key
            ),

            latest_eligibility AS (
                SELECT
                    canonical_job_key,
                    decision,
                    row_number() over (
                        partition by canonical_job_key
                        order by screened_at desc nulls last
                    ) as eligibility_rank
                FROM resume_job_eligibility
                WHERE resume_hash = ?
                  AND prompt_version = ?
            ),

            enrichment_attempts AS (
                SELECT
                    enriched_jobs.canonical_job_key,
                    status AS enrichment_status,
                    http_status AS enrichment_http_status,
                    error_message AS enrichment_error_message,
                    attempt_count AS enrichment_attempt_count,
                    final_url AS resolved_candidate_url,
                    resolved_candidate_title,
                    resolved_candidate_company,
                    resolved_candidate_location,
                    identity_confidence,
                    identity_validation_reason,
                    official_job_url,
                    official_url_status,
                    official_url_source,
                    official_url_resolved_at,
                    official_url_confidence,
                    official_url_validation_reason,
                    official_resolved_title,
                    official_resolved_company,
                    official_resolved_location,
                    last_attempted_at AS enrichment_attempted_at,
                    row_number() over (
                        partition by enriched_jobs.canonical_job_key
                        order by
                            case
                                when status = 'enriched' then 1
                                else 0
                            end desc,
                            last_attempted_at desc nulls last,
                            attempts.record_key
                    ) as enrichment_rank
                FROM job_enrichment_attempts
                    AS attempts
                INNER JOIN raw_jobs_with_identity
                    AS enriched_jobs
                    ON attempts.record_key =
                       enriched_jobs.record_key
            )

            SELECT
                jobs.record_key,
                jobs.exact_posting_key,
                jobs.canonical_job_key,
                jobs.duplicate_fingerprint,
                jobs.title,
                jobs.company_name,
                jobs.location,
                jobs.salary_text,
                jobs.source,
                jobs.apply_url,
                jobs.posted_age_text,
                jobs.email_date,
                jobs.discovered_at,
                jobs.description_updated_at,
                CASE
                    WHEN jobs.description IS NOT NULL
                     AND TRIM(jobs.description) <> ''
                    THEN array_length(
                        regexp_split_to_array(
                            TRIM(jobs.description),
                            '\\s+'
                        )
                    )
                    ELSE 0
                END AS raw_description_word_count,
                CASE
                    WHEN regexp_matches(
                        lower(coalesce(jobs.description, '')),
                        '\\b(responsibilit(y|ies)|requirements?|qualifications?|what\\s+you(''|’)ll\\s+do|you\\s+will|duties|skills?|experience|must\\s+have|preferred)\\b'
                    )
                    THEN true
                    ELSE false
                END AS description_quality_signals,
                jobs.normalized_title,
                jobs.title_classification,
                jobs.title_match_score,
                jobs.title_filter_reason,
                jobs.title_matched_pattern,
                enrichment_attempts.enrichment_status,
                enrichment_attempts.enrichment_http_status,
                enrichment_attempts.enrichment_error_message,
                enrichment_attempts.enrichment_attempt_count,
                enrichment_attempts.resolved_candidate_url,
                enrichment_attempts.resolved_candidate_title,
                enrichment_attempts.resolved_candidate_company,
                enrichment_attempts.resolved_candidate_location,
                enrichment_attempts.identity_confidence,
                enrichment_attempts.identity_validation_reason,
                enrichment_attempts.official_job_url,
                enrichment_attempts.official_url_status,
                enrichment_attempts.official_url_source,
                enrichment_attempts.official_url_resolved_at,
                enrichment_attempts.official_url_confidence,
                enrichment_attempts.official_url_validation_reason,
                enrichment_attempts.official_resolved_title,
                enrichment_attempts.official_resolved_company,
                enrichment_attempts.official_resolved_location,
                enrichment_attempts.enrichment_attempted_at,
                matches.search_title as best_search_title,
                matches.match_score as rule_score,
                coalesce(
                    canonical_status.status,
                    'new'
                ) as application_status,
                recommendations.ai_score,
                recommendations.recommendation,
                recommendations.confidence,
                recommendations.title_fit,
                recommendations.skills_fit,
                recommendations.experience_fit,
                recommendations.seniority_fit,
                recommendations.industry_fit,
                recommendations.location_fit,
                recommendations.matching_strengths,
                recommendations.hard_requirements_missing,
                recommendations.preferred_qualifications_missing,
                recommendations.risk_factors,
                recommendations.summary,
                recommendations.description_word_count,
                recommendations.description_complete,
                recommendations.has_incomplete_description,
                recommendations.ai_prompt_version,
                recommendations.ai_scored_at
            FROM jobs
            LEFT JOIN best_matches as matches
                ON jobs.canonical_job_key =
                   matches.canonical_job_key
               AND matches.match_rank = 1
            LEFT JOIN latest_application_status
                as canonical_status
                ON jobs.posting_status_key =
                    canonical_status.posting_status_key
               AND canonical_status.status_rank = 1
            LEFT JOIN analytics.mart_job_recommendations as recommendations
                ON jobs.canonical_job_key =
                   recommendations.canonical_job_key
               AND recommendations.resume_hash = ?
               AND recommendations.ai_prompt_version = ?
               AND recommendations.description_word_count >= 80
               AND coalesce(
                   recommendations.has_incomplete_description,
                   false
               ) = false
               AND recommendations.ai_scored_at >= coalesce(
                   jobs.description_updated_at,
                   TIMESTAMPTZ '1970-01-01 00:00:00+00'
               )
            LEFT JOIN latest_eligibility as eligibility
                ON jobs.canonical_job_key = eligibility.canonical_job_key
               AND eligibility.eligibility_rank = 1
            LEFT JOIN enrichment_attempts
                ON jobs.canonical_job_key =
                   enrichment_attempts.canonical_job_key
               AND enrichment_attempts.enrichment_rank = 1
            WHERE lower(coalesce(jobs.source, '')) NOT IN (
                  {excluded_job_sources}
              )
            ORDER BY
                jobs.discovered_at desc nulls last,
                jobs.title,
                jobs.company_name
            """.format(
                excluded_job_sources=EXCLUDED_JOB_SOURCES_SQL,
            ),
            [
                selected_resume_hash,
                ELIGIBILITY_PROMPT_VERSION,
                selected_resume_hash,
                MATCHER_PROMPT_VERSION,
            ],
        )

        columns = [description[0] for description in cursor.description]
        rows = cursor.fetchall()

    jobs = [dict(zip(columns, row)) for row in rows]

    for job in jobs:
        if not job.get("title_classification"):
            classification = classify_job_title(
                job.get("title")
            )
            job["normalized_title"] = classification.normalized_title
            job["title_classification"] = classification.category
            job["title_match_score"] = classification.score
            job["title_filter_reason"] = classification.reason
            job["title_matched_pattern"] = classification.matched_pattern

        email_datetime = parse_email_datetime(
            job.get("email_date")
        )

        if email_datetime is None:
            email_datetime = job.get("discovered_at")

            if (
                isinstance(email_datetime, datetime)
                and email_datetime.tzinfo is None
            ):
                email_datetime = email_datetime.replace(
                    tzinfo=timezone.utc
                )

        if isinstance(email_datetime, datetime):
            job["sent_at"] = email_datetime
        else:
            job["sent_at"] = None

        job["matching_strengths"] = parse_json_list(
            job.get("matching_strengths")
        )
        job["hard_requirements_missing"] = parse_json_list(
            job.get("hard_requirements_missing")
        )
        job["preferred_qualifications_missing"] = parse_json_list(
            job.get("preferred_qualifications_missing")
        )
        job["risk_factors"] = parse_json_list(
            job.get("risk_factors")
        )

    jobs = apply_admission_gate(
        jobs=filter_user_reviewable_jobs(jobs),
        resume_hash=selected_resume_hash,
        preserve_statuses=("applied", "removed"),
    )

    for job in jobs:
        priority = derive_fit_priority(job)
        job["fit_priority_label"] = priority.label
        job["fit_priority_source"] = priority.source
        job["fit_priority_reason"] = priority.reason

        enrichment_priority = calculate_enrichment_priority(job)
        job["enrichment_priority_score"] = enrichment_priority.score
        job["enrichment_priority_tier"] = enrichment_priority.tier
        job["enrichment_priority_reason"] = enrichment_priority.reason

    queue_candidates = sorted(
        [
            job
            for job in jobs
            if str(job.get("application_status") or "new").casefold()
            == "new"
            and job.get("description_state") in {
                "NEEDS_ENRICHMENT",
                "PARTIAL_JD",
                "ENRICHMENT_REJECTED",
            }
            and job.get("title_classification") != "FILTERED_OUT"
        ],
        key=lambda job: (
            job.get("enrichment_priority_score") or 0,
            job.get("sent_at")
            or job.get("discovered_at")
            or datetime.min.replace(tzinfo=timezone.utc),
        ),
        reverse=True,
    )

    for index, job in enumerate(queue_candidates, start=1):
        job["approximate_enrichment_queue_rank"] = index

    def review_priority(
        job: dict[str, Any],
    ) -> int:
        fit_priority = job.get("fit_priority_label")

        if fit_priority == "High":
            return 6

        if fit_priority == "Medium":
            return 5

        if fit_priority == "Low":
            return 2

        title_classification = job.get(
            "title_classification"
        )
        has_ai_score = has_current_complete_ai_score(job)
        has_verified_description = has_quality_description(job)

        if title_classification == "STRONG_MATCH" and has_ai_score:
            return 6

        if title_classification == "POSSIBLE_MATCH" and has_ai_score:
            return 5

        if title_classification == "STRONG_MATCH" and has_verified_description:
            return 4

        if title_classification == "POSSIBLE_MATCH" and has_verified_description:
            return 3

        if title_classification == "STRONG_MATCH":
            return 2

        if title_classification == "POSSIBLE_MATCH":
            return 1

        return 0

    sorted_jobs = sorted(
        jobs,
        key=lambda job: (
            {
                "new": 2,
                "applied": 1,
                "removed": 0,
            }.get(
                job.get("application_status", "new"),
                2,
            ),
            review_priority(job),
            job.get("ai_score") or -1,
            job.get("title_match_score") or 0,
            job.get("sent_at") or datetime.min.replace(
                tzinfo=timezone.utc
            ),
            job.get("title") or "",
            job.get("company_name") or "",
        ),
        reverse=True,
    )

    deduplicated_jobs: list[dict[str, Any]] = []
    seen_display_keys: set[tuple[str, str, str, str]] = set()

    for job in sorted_jobs:
        duplicate_key = ui_duplicate_key(job)

        if duplicate_key in seen_display_keys:
            continue

        seen_display_keys.add(duplicate_key)
        deduplicated_jobs.append(job)

    return deduplicated_jobs


def update_application_status(
    record_key: str,
    status: str,
) -> None:
    """Apply a status to duplicate alerts for the same exact posting."""
    if status not in {"new", "applied", "removed"}:
        raise ValueError(
            "Application status must be new, applied or removed."
        )

    initialize_database()

    updated_at = datetime.now(timezone.utc)

    with get_connection() as connection:
        register_exact_posting_identity_function(connection)
        canonical_row = connection.execute(
            """
            SELECT
                exact_posting_identity(
                    source,
                    source_job_id,
                    apply_url,
                    record_key
                ) AS posting_status_key
            FROM raw_jobs
            WHERE record_key = ?
            """,
            [record_key],
        ).fetchone()

        if not canonical_row:
            return

        related_rows = connection.execute(
            """
            SELECT record_key
            FROM raw_jobs
            WHERE exact_posting_identity(
                source,
                source_job_id,
                apply_url,
                record_key
            ) = ?
            """,
            [canonical_row[0]],
        ).fetchall()

        connection.executemany(
            """
            INSERT INTO application_status (
                record_key,
                status,
                updated_at
            )
            VALUES (?, ?, ?)
            ON CONFLICT (record_key) DO UPDATE SET
                status = excluded.status,
                updated_at = excluded.updated_at
            """,
            [
                [
                    related_row[0],
                    status,
                    updated_at,
                ]
                for related_row in related_rows
            ],
        )


def load_candidate_jobs(
    resume_hash: str,
    model_name: str | None,
    limit: int,
    minimum_rule_score: int,
    prompt_version: str = MATCHER_PROMPT_VERSION,
    reuse_any_model: bool = False,
) -> list[dict[str, Any]]:
    """Load jobs eligible for AI scoring for a resume."""
    initialize_job_eligibility_table()

    with get_connection() as connection:
        register_exact_posting_identity_function(connection)
        cursor = connection.execute(
            """
            WITH jobs AS (
                SELECT
                    *,
                    COALESCE(
                        NULLIF(job_fingerprint, ''),
                        record_key
                    ) AS duplicate_fingerprint,
                    exact_posting_identity(
                        source,
                        source_job_id,
                        apply_url,
                        record_key
                    ) AS exact_posting_key,
                    exact_posting_identity(
                        source,
                        source_job_id,
                        apply_url,
                        record_key
                    ) AS canonical_job_key,
                    exact_posting_identity(
                        source,
                        source_job_id,
                        apply_url,
                        record_key
                    ) AS posting_status_key
                FROM raw_jobs
            ),

            existing_scores AS (
                SELECT DISTINCT
                    current_jobs.canonical_job_key
                FROM resume_job_scores AS scores
                INNER JOIN jobs AS scored_jobs
                    ON scores.record_key =
                       scored_jobs.record_key
                INNER JOIN jobs AS current_jobs
                    ON scored_jobs.canonical_job_key =
                       current_jobs.canonical_job_key
                WHERE scores.resume_hash = ?
                  AND (
                      ? = true
                      OR scores.model_name = ?
                  )
                  AND coalesce(scores.prompt_version, 'v1') = ?
                  AND scores.description_complete = true
                  AND scores.scored_at >= coalesce(
                      current_jobs.description_updated_at,
                      TIMESTAMPTZ '1970-01-01 00:00:00+00'
                  )
            ),

            latest_application_status AS (
                SELECT
                    status_jobs.posting_status_key,
                    status.status,
                    ROW_NUMBER() OVER (
                        PARTITION BY status_jobs.posting_status_key
                        ORDER BY
                            status.updated_at DESC NULLS LAST,
                            status.record_key
                    ) AS status_rank
                FROM application_status AS status
                INNER JOIN jobs AS status_jobs
                    ON status.record_key =
                       status_jobs.record_key
            ),

            enrichment_attempts AS (
                SELECT
                    record_key,
                    status AS enrichment_status,
                    official_job_url,
                    official_url_status
                FROM job_enrichment_attempts
            ),

            ranked_candidates AS (
                SELECT
                    jobs.exact_posting_key,
                    jobs.canonical_job_key,
                    jobs.duplicate_fingerprint,
                    jobs.record_key,
                    jobs.title,
                    jobs.company_name,
                    jobs.location,
                    jobs.salary_text,
                    CASE
                        WHEN enrichment_attempts.enrichment_status =
                             'resolution_rejected'
                        THEN NULL
                        ELSE jobs.description
                    END AS description,
                    CASE
                        WHEN jobs.description IS NOT NULL
                         AND TRIM(jobs.description) <> ''
                        THEN array_length(
                            regexp_split_to_array(
                                TRIM(jobs.description),
                                '\\s+'
                            )
                        )
                        ELSE 0
                    END AS raw_description_word_count,
                    CASE
                        WHEN regexp_matches(
                            lower(coalesce(jobs.description, '')),
                            '\\b(responsibilit(y|ies)|requirements?|qualifications?|what\\s+you(''|’)ll\\s+do|you\\s+will|duties|skills?|experience|must\\s+have|preferred)\\b'
                        )
                        THEN true
                        ELSE false
                    END AS description_quality_signals,
                    jobs.source,
                    jobs.apply_url,
                    enrichment_attempts.official_job_url,
                    enrichment_attempts.official_url_status,
                    matches.match_score AS rule_score,
                    matches.search_id,
                    matches.search_title,
                    matches.is_recommended,
                    matches.needs_review,
                    ROW_NUMBER() OVER (
                        PARTITION BY jobs.canonical_job_key
                        ORDER BY
                            matches.is_recommended DESC,
                            matches.needs_review DESC,
                            matches.match_score DESC,
                            matches.title_score DESC,
                            CASE
                                WHEN jobs.description IS NOT NULL
                                 AND TRIM(jobs.description) <> ''
                                THEN 1
                                ELSE 0
                            END DESC,
                            jobs.description_updated_at DESC NULLS LAST,
                            jobs.discovered_at DESC NULLS LAST,
                            jobs.record_key
                    ) AS candidate_rank
                FROM jobs
                INNER JOIN job_matches AS matches
                    ON jobs.record_key = matches.record_key
                LEFT JOIN existing_scores
                    ON jobs.canonical_job_key =
                       existing_scores.canonical_job_key
                LEFT JOIN latest_application_status
                    ON jobs.posting_status_key =
                       latest_application_status.posting_status_key
                   AND latest_application_status.status_rank = 1
                LEFT JOIN resume_job_eligibility AS eligibility
                    ON jobs.canonical_job_key = eligibility.canonical_job_key
                   AND eligibility.resume_hash = ?
                   AND eligibility.prompt_version = ?
                LEFT JOIN enrichment_attempts
                    ON jobs.record_key = enrichment_attempts.record_key
                WHERE existing_scores.canonical_job_key IS NULL
                  AND matches.match_score >= ?
                  AND lower(coalesce(jobs.source, '')) NOT IN (
                      {excluded_job_sources}
                  )
                  AND COALESCE(
                      enrichment_attempts.enrichment_status,
                      ''
                  ) <> 'resolution_rejected'
                  AND jobs.description IS NOT NULL
                  AND TRIM(jobs.description) <> ''
                  AND array_length(
                      regexp_split_to_array(
                          TRIM(jobs.description),
                          '\\s+'
                      )
                  ) >= 80
                  AND regexp_matches(
                      lower(coalesce(jobs.description, '')),
                      '\\b(responsibilit(y|ies)|requirements?|qualifications?|what\\s+you(''|’)ll\\s+do|you\\s+will|duties|skills?|experience|must\\s+have|preferred)\\b'
                  )
                  AND coalesce(
                      jobs.title_classification,
                      CASE
                          WHEN regexp_matches(
                              lower(coalesce(jobs.title, '')),
                              ?
                          )
                          THEN 'FILTERED_OUT'
                          ELSE 'POSSIBLE_MATCH'
                      END
                  ) <> 'FILTERED_OUT'
                  AND COALESCE(
                      latest_application_status.status,
                      'new'
                  ) = 'new'
                  AND COALESCE(
                      eligibility.decision,
                      'needs_description'
                  ) IN ('eligible', 'needs_description')
            )

            SELECT
                exact_posting_key,
                canonical_job_key,
                duplicate_fingerprint,
                record_key,
                title,
                company_name,
                location,
                salary_text,
                description,
                raw_description_word_count,
                description_quality_signals,
                source,
                apply_url,
                official_job_url,
                official_url_status,
                rule_score,
                search_id,
                search_title
            FROM ranked_candidates
            WHERE candidate_rank = 1
            ORDER BY
                rule_score DESC,
                title,
                company_name
            """.format(
                excluded_job_sources=EXCLUDED_JOB_SOURCES_SQL,
            ),
            [
                resume_hash,
                reuse_any_model,
                model_name,
                prompt_version,
                resume_hash,
                ELIGIBILITY_PROMPT_VERSION,
                minimum_rule_score,
                EXCLUDED_TITLE_SQL_REGEX,
            ],
        )

        columns = [description[0] for description in cursor.description]
        rows = cursor.fetchall()

    jobs = filter_user_reviewable_jobs(
        [
            dict(zip(columns, row))
            for row in rows
        ]
    )

    return apply_admission_gate(
        jobs=jobs,
        resume_hash=resume_hash,
        include_low_priority=False,
    )[:limit]


def count_cached_canonical_scores(
    resume_hash: str,
    model_name: str | None,
    prompt_version: str = MATCHER_PROMPT_VERSION,
    reuse_any_model: bool = False,
) -> int:
    with get_connection() as connection:
        register_exact_posting_identity_function(connection)
        result = connection.execute(
            """
            WITH jobs AS (
                SELECT
                    record_key,
                    description_updated_at,
                    exact_posting_identity(
                        source,
                        source_job_id,
                        apply_url,
                        record_key
                    ) AS canonical_job_key
                FROM raw_jobs
            )

            SELECT COUNT(DISTINCT current_jobs.canonical_job_key)
            FROM resume_job_scores AS scores
            INNER JOIN jobs AS scored_jobs
                ON scores.record_key = scored_jobs.record_key
            INNER JOIN jobs AS current_jobs
                ON scored_jobs.canonical_job_key =
                   current_jobs.canonical_job_key
            WHERE scores.resume_hash = ?
              AND (
                  ? = true
                  OR scores.model_name = ?
              )
              AND coalesce(scores.prompt_version, 'v1') = ?
              AND scores.description_complete = true
              AND scores.scored_at >= coalesce(
                  current_jobs.description_updated_at,
                  TIMESTAMPTZ '1970-01-01 00:00:00+00'
              )
            """,
            [
                resume_hash,
                reuse_any_model,
                model_name,
                prompt_version,
            ],
        ).fetchone()

    return int(result[0]) if result else 0


def count_unscored_candidate_jobs(
    resume_hash: str,
    model_name: str | None,
    minimum_rule_score: int,
    prompt_version: str = MATCHER_PROMPT_VERSION,
    reuse_any_model: bool = False,
) -> int:
    """Count AI-score candidates after cache, status and eligibility filters."""
    initialize_job_eligibility_table()

    with get_connection() as connection:
        register_exact_posting_identity_function(connection)
        result = connection.execute(
            """
            WITH jobs AS (
                SELECT
                    *,
                    COALESCE(
                        NULLIF(job_fingerprint, ''),
                        record_key
                    ) AS duplicate_fingerprint,
                    exact_posting_identity(
                        source,
                        source_job_id,
                        apply_url,
                        record_key
                    ) AS exact_posting_key,
                    exact_posting_identity(
                        source,
                        source_job_id,
                        apply_url,
                        record_key
                    ) AS canonical_job_key,
                    exact_posting_identity(
                        source,
                        source_job_id,
                        apply_url,
                        record_key
                    ) AS posting_status_key
                FROM raw_jobs
            ),

            existing_scores AS (
                SELECT DISTINCT
                    current_jobs.canonical_job_key
                FROM resume_job_scores AS scores
                INNER JOIN jobs AS scored_jobs
                    ON scores.record_key =
                       scored_jobs.record_key
                INNER JOIN jobs AS current_jobs
                    ON scored_jobs.canonical_job_key =
                       current_jobs.canonical_job_key
                WHERE scores.resume_hash = ?
                  AND (
                      ? = true
                      OR scores.model_name = ?
                  )
                  AND coalesce(scores.prompt_version, 'v1') = ?
                  AND scores.description_complete = true
                  AND scores.scored_at >= coalesce(
                      current_jobs.description_updated_at,
                      TIMESTAMPTZ '1970-01-01 00:00:00+00'
                  )
            ),

            latest_application_status AS (
                SELECT
                    status_jobs.posting_status_key,
                    status.status,
                    ROW_NUMBER() OVER (
                        PARTITION BY status_jobs.posting_status_key
                        ORDER BY
                            status.updated_at DESC NULLS LAST,
                            status.record_key
                    ) AS status_rank
                FROM application_status AS status
                INNER JOIN jobs AS status_jobs
                    ON status.record_key =
                       status_jobs.record_key
            ),

            enrichment_attempts AS (
                SELECT
                    record_key,
                    status AS enrichment_status
                FROM job_enrichment_attempts
            ),

            ranked_candidates AS (
                SELECT
                    jobs.canonical_job_key,
                    ROW_NUMBER() OVER (
                        PARTITION BY jobs.canonical_job_key
                        ORDER BY
                            matches.is_recommended DESC,
                            matches.needs_review DESC,
                            matches.match_score DESC,
                            matches.title_score DESC,
                            CASE
                                WHEN COALESCE(
                                     enrichment_attempts.enrichment_status,
                                     ''
                                ) <> 'resolution_rejected'
                                 AND jobs.description IS NOT NULL
                                 AND TRIM(jobs.description) <> ''
                                THEN 1
                                ELSE 0
                            END DESC,
                            jobs.description_updated_at DESC NULLS LAST,
                            jobs.discovered_at DESC NULLS LAST,
                            jobs.record_key
                    ) AS candidate_rank
                FROM jobs
                INNER JOIN job_matches AS matches
                    ON jobs.record_key = matches.record_key
                LEFT JOIN existing_scores
                    ON jobs.canonical_job_key =
                       existing_scores.canonical_job_key
                LEFT JOIN latest_application_status
                    ON jobs.posting_status_key =
                       latest_application_status.posting_status_key
                   AND latest_application_status.status_rank = 1
                LEFT JOIN enrichment_attempts
                    ON jobs.record_key = enrichment_attempts.record_key
                LEFT JOIN resume_job_eligibility AS eligibility
                    ON jobs.canonical_job_key = eligibility.canonical_job_key
                   AND eligibility.resume_hash = ?
                   AND eligibility.prompt_version = ?
                WHERE existing_scores.canonical_job_key IS NULL
                  AND matches.match_score >= ?
                  AND lower(coalesce(jobs.source, '')) NOT IN (
                      {excluded_job_sources}
                  )
                  AND COALESCE(
                      enrichment_attempts.enrichment_status,
                      ''
                  ) <> 'resolution_rejected'
                  AND jobs.description IS NOT NULL
                  AND TRIM(jobs.description) <> ''
                  AND array_length(
                      regexp_split_to_array(
                          TRIM(jobs.description),
                          '\\s+'
                      )
                  ) >= 80
                  AND regexp_matches(
                      lower(coalesce(jobs.description, '')),
                      '\\b(responsibilit(y|ies)|requirements?|qualifications?|what\\s+you(''|’)ll\\s+do|you\\s+will|duties|skills?|experience|must\\s+have|preferred)\\b'
                  )
                  AND coalesce(
                      jobs.title_classification,
                      CASE
                          WHEN regexp_matches(
                              lower(coalesce(jobs.title, '')),
                              ?
                          )
                          THEN 'FILTERED_OUT'
                          ELSE 'POSSIBLE_MATCH'
                      END
                  ) <> 'FILTERED_OUT'
                  AND COALESCE(
                      latest_application_status.status,
                      'new'
                  ) = 'new'
                  AND COALESCE(
                      eligibility.decision,
                      'needs_description'
                  ) IN ('eligible', 'needs_description')
            )

            SELECT COUNT(*)
            FROM ranked_candidates
            WHERE candidate_rank = 1
            """.format(
                excluded_job_sources=EXCLUDED_JOB_SOURCES_SQL,
            ),
            [
                resume_hash,
                reuse_any_model,
                model_name,
                prompt_version,
                resume_hash,
                ELIGIBILITY_PROMPT_VERSION,
                minimum_rule_score,
                EXCLUDED_TITLE_SQL_REGEX,
            ],
        ).fetchone()

    return len(
        load_candidate_jobs(
            resume_hash=resume_hash,
            model_name=model_name,
            limit=100000,
            minimum_rule_score=minimum_rule_score,
            prompt_version=prompt_version,
            reuse_any_model=reuse_any_model,
        )
    )


def load_unscreened_job_eligibility_candidates(
    resume_hash: str,
    limit: int,
    prompt_version: str = ELIGIBILITY_PROMPT_VERSION,
) -> list[dict[str, Any]]:
    """Load one representative raw row per canonical job for AI screening."""
    initialize_job_eligibility_table()

    with get_connection() as connection:
        register_exact_posting_identity_function(connection)
        cursor = connection.execute(
            """
            WITH jobs AS (
                SELECT
                    *,
                    COALESCE(
                        NULLIF(job_fingerprint, ''),
                        record_key
                    ) AS duplicate_fingerprint,
                    exact_posting_identity(
                        source,
                        source_job_id,
                        apply_url,
                        record_key
                    ) AS exact_posting_key,
                    exact_posting_identity(
                        source,
                        source_job_id,
                        apply_url,
                        record_key
                    ) AS canonical_job_key,
                    exact_posting_identity(
                        source,
                        source_job_id,
                        apply_url,
                        record_key
                    ) AS posting_status_key
                FROM raw_jobs
            ),

            latest_application_status AS (
                SELECT
                    status_jobs.posting_status_key,
                    status.status,
                    ROW_NUMBER() OVER (
                        PARTITION BY status_jobs.posting_status_key
                        ORDER BY
                            status.updated_at DESC NULLS LAST,
                            status.record_key
                    ) AS status_rank
                FROM application_status AS status
                INNER JOIN jobs AS status_jobs
                    ON status.record_key = status_jobs.record_key
            ),

            enrichment_attempts AS (
                SELECT
                    record_key,
                    status AS enrichment_status,
                    official_job_url,
                    official_url_status
                FROM job_enrichment_attempts
            ),

            ranked_jobs AS (
                SELECT
                    jobs.exact_posting_key,
                    jobs.canonical_job_key,
                    jobs.duplicate_fingerprint,
                    jobs.record_key,
                    jobs.title,
                    jobs.company_name,
                    jobs.location,
                    jobs.salary_text,
                    CASE
                        WHEN enrichment_attempts.enrichment_status =
                             'resolution_rejected'
                        THEN NULL
                        ELSE jobs.description
                    END AS description,
                    jobs.source,
                    jobs.apply_url,
                    enrichment_attempts.official_job_url,
                    enrichment_attempts.official_url_status,
                    ROW_NUMBER() OVER (
                        PARTITION BY jobs.canonical_job_key
                        ORDER BY
                            CASE
                                WHEN COALESCE(
                                     enrichment_attempts.enrichment_status,
                                     ''
                                ) <> 'resolution_rejected'
                                 AND jobs.description IS NOT NULL
                                 AND TRIM(jobs.description) <> ''
                                THEN 1
                                ELSE 0
                            END DESC,
                            jobs.description_updated_at DESC NULLS LAST,
                            jobs.discovered_at DESC NULLS LAST,
                            jobs.record_key
                    ) AS job_rank
                FROM jobs
                LEFT JOIN resume_job_eligibility AS eligibility
                    ON jobs.canonical_job_key = eligibility.canonical_job_key
                   AND eligibility.resume_hash = ?
                   AND eligibility.prompt_version = ?
                LEFT JOIN latest_application_status
                    ON jobs.posting_status_key =
                       latest_application_status.posting_status_key
                   AND latest_application_status.status_rank = 1
                LEFT JOIN enrichment_attempts
                    ON jobs.record_key = enrichment_attempts.record_key
                WHERE eligibility.canonical_job_key IS NULL
                  AND lower(coalesce(jobs.source, '')) NOT IN (
                      {excluded_job_sources}
                  )
                  AND coalesce(
                      jobs.title_classification,
                      CASE
                          WHEN regexp_matches(
                              lower(coalesce(jobs.title, '')),
                              ?
                          )
                          THEN 'FILTERED_OUT'
                          ELSE 'POSSIBLE_MATCH'
                      END
                  ) <> 'FILTERED_OUT'
                  AND COALESCE(
                      latest_application_status.status,
                      'new'
                  ) = 'new'
            )

            SELECT
                exact_posting_key,
                canonical_job_key,
                duplicate_fingerprint,
                record_key,
                title,
                company_name,
                location,
                salary_text,
                description,
                source,
                apply_url,
                official_job_url,
                official_url_status
            FROM ranked_jobs
            WHERE job_rank = 1
            ORDER BY
                title,
                company_name
            """.format(
                excluded_job_sources=EXCLUDED_JOB_SOURCES_SQL,
            ),
            [
                resume_hash,
                prompt_version,
                EXCLUDED_TITLE_SQL_REGEX,
            ],
        )

        columns = [description[0] for description in cursor.description]
        rows = cursor.fetchall()

    jobs = filter_user_reviewable_jobs(
        [
            dict(zip(columns, row))
            for row in rows
        ]
    )

    return apply_admission_gate(
        jobs=jobs,
        resume_hash=resume_hash,
    )[:limit]


def load_pipeline_operation_metrics(
    resume_hash: str | None,
    minimum_rule_score: int,
) -> dict[str, Any]:
    """Return operations-tab counts from one consistent repository layer."""
    selected_resume_hash = resolve_display_resume_hash(resume_hash)
    reviewable_jobs = load_all_jobs(resume_hash=selected_resume_hash)

    metrics: dict[str, Any] = {
        "raw_jobs": 0,
        "exact_postings": 0,
        "reviewable_jobs": len(reviewable_jobs),
        "new_jobs": sum(
            1
            for job in reviewable_jobs
            if job.get("application_status", "new") == "new"
        ),
        "applied_jobs": sum(
            1
            for job in reviewable_jobs
            if job.get("application_status") == "applied"
        ),
        "removed_jobs": sum(
            1
            for job in reviewable_jobs
            if job.get("application_status") == "removed"
        ),
        "needs_enrichment": sum(
            1
            for job in reviewable_jobs
            if job.get("description_state") == "NEEDS_ENRICHMENT"
        ),
        "full_jd": sum(
            1
            for job in reviewable_jobs
            if job.get("description_state") == "FULL_JD"
        ),
        "partial_jd": sum(
            1
            for job in reviewable_jobs
            if job.get("description_state") == "PARTIAL_JD"
        ),
        "rejected_enrichment": sum(
            1
            for job in reviewable_jobs
            if job.get("description_state") == "ENRICHMENT_REJECTED"
        ),
        "ai_scored": sum(
            1
            for job in reviewable_jobs
            if job.get("ai_score") is not None
        ),
        "ai_score_eligible": 0,
        "enrichment_attempts": {},
    }

    if selected_resume_hash:
        metrics["ai_score_eligible"] = count_unscored_candidate_jobs(
            resume_hash=selected_resume_hash,
            model_name=None,
            minimum_rule_score=minimum_rule_score,
            reuse_any_model=True,
        )

    with get_connection() as connection:
        register_exact_posting_identity_function(connection)
        raw_result = connection.execute(
            """
            SELECT
                COUNT(*) AS raw_jobs,
                COUNT(
                    DISTINCT exact_posting_identity(
                        source,
                        source_job_id,
                        apply_url,
                        record_key
                    )
                ) AS exact_postings
            FROM raw_jobs
            """
        ).fetchone()

        if raw_result:
            metrics["raw_jobs"] = int(raw_result[0] or 0)
            metrics["exact_postings"] = int(raw_result[1] or 0)

        attempt_rows = connection.execute(
            """
            SELECT status, COUNT(*)
            FROM job_enrichment_attempts
            GROUP BY status
            """
        ).fetchall()

    metrics["enrichment_attempts"] = {
        str(status): int(count)
        for status, count in attempt_rows
    }

    return metrics


def load_recommendations(
    resume_hash: str | None,
) -> list[dict[str, Any]]:
    selected_resume_hash = resolve_display_resume_hash(resume_hash)

    with get_connection() as connection:
        cursor = connection.execute(
            """
            SELECT
                resume_hash,
                exact_posting_key,
                canonical_job_key,
                canonical_record_key,
                scored_record_key,
                job_title,
                company_name,
                location,
                salary_text,
                source,
                apply_url,
                posted_age_text,
                discovered_at,
                rule_score,
                best_search_title,
                ai_score,
                recommendation,
                confidence,
                title_fit,
                skills_fit,
                experience_fit,
                seniority_fit,
                industry_fit,
                location_fit,
                matching_strengths,
                hard_requirements_missing,
                preferred_qualifications_missing,
                risk_factors,
                summary,
                description_word_count,
                has_incomplete_description,
                ai_model_name,
                ai_prompt_version,
                ai_scored_at,
                ai_score_rank
            FROM analytics.mart_job_recommendations
            WHERE resume_hash = ?
              AND ai_prompt_version = ?
              AND lower(coalesce(source, '')) NOT IN (
                  {excluded_job_sources}
              )
            ORDER BY
                ai_score DESC,
                ai_score_rank
            """.format(
                excluded_job_sources=EXCLUDED_JOB_SOURCES_SQL,
            ),
            [
                selected_resume_hash,
                MATCHER_PROMPT_VERSION,
            ],
        )

        columns = [description[0] for description in cursor.description]
        rows = cursor.fetchall()

    recommendations = [dict(zip(columns, row)) for row in rows]

    for recommendation in recommendations:
        recommendation["matching_strengths"] = parse_json_list(
            recommendation.get("matching_strengths")
        )
        recommendation["hard_requirements_missing"] = parse_json_list(
            recommendation.get("hard_requirements_missing")
        )
        recommendation["preferred_qualifications_missing"] = parse_json_list(
            recommendation.get("preferred_qualifications_missing")
        )
        recommendation["risk_factors"] = parse_json_list(
            recommendation.get("risk_factors")
        )

    return recommendations
