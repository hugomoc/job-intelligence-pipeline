"""Pure, conservative recovery of missing verified posting metadata.

The caller supplies this record's stored fetch and verification evidence. These
helpers never read a database, fetch a page, borrow another record's evidence,
or change the supplied row. A URL identifies a proposed posting; it does not
establish that this source record independently verified that posting.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
import math
from typing import Any, Mapping
from urllib.parse import urlsplit

from src.enrichment.job_identity import (
    ACCEPT_WITH_COMPANY_THRESHOLD,
    AUTHORITATIVE_LOCATION_SOURCES,
    COMPANY_MATCH_THRESHOLD,
    TITLE_REASONABLE_THRESHOLD,
    location_compatibility,
    validate_job_identity,
)
from src.enrichment.official_job_resolver import (
    KNOWN_ATS_DOMAINS,
    OFFICIAL_FOUND_VERIFIED,
    ats_type_for_url,
)
from src.job_domains import is_third_party_url
from src.verified_posting_identity import (
    VERIFIED_KEY_SOURCE_OFFICIAL_FOUND_VERIFIED,
    VERIFIED_KEY_TRUST_TRUSTED,
    is_specific_job_posting_url,
    verified_posting_key_from_url,
)


CLASS_RECOVERABLE = "A"
CLASS_UNSAFE = "B"
CLASS_INCONSISTENT = "C"

# These describe fetched structured data. A description's html_selector label
# alone cannot establish the source of historical title/company metadata.
STRUCTURED_EXTRACTION_METHODS = frozenset({
    "json_ld", "playwright_json_ld", "jobposting_json_ld",
    "greenhouse_remix_jobpost", "workday_posting_api",
})
FETCHED_IDENTITY_SOURCES = frozenset({
    "jobposting_json_ld", "greenhouse_remix_jobpost", "workday_posting_api",
    "workday_jobposting_employer_statement",
})
PROVENANCE_CAVEAT = (
    "Historical rows do not persist per-field title/company/location sources. "
    "Stored fetched candidate fields, matching official fields, successful own "
    "fetch, structured extraction and accepted validations corroborate recovery; "
    "the extraction method is not a persisted per-field provenance assertion. "
    "No authoritative location source is inferred."
)


@dataclass(frozen=True)
class MissingVerifiedIdentityFinding:
    classification: str
    reason: str
    record_key: str
    official_job_url: str
    derived_key: str = ""
    proposed_source: str | None = None
    proposed_trust: str | None = None
    proposed_timestamp: Any = None
    proposed_confidence: float | None = None
    evidence: dict[str, Any] = field(default_factory=dict)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _valid_http_url(value: Any) -> bool:
    try:
        parsed = urlsplit(_text(value))
        # Reading port also rejects a malformed or out-of-range port.
        port = parsed.port
        return bool(parsed.scheme.casefold() in {"http", "https"}
                    and parsed.hostname and not parsed.username
                    and not parsed.password and (port is None or port > 0))
    except (TypeError, ValueError):
        return False


def supported_specific_posting_key(
    url: Any, *, allow_employer: bool = False,
) -> str:
    """Return a safe concrete URL key; callers still need identity evidence.

    Employer URLs require an explicit opt-in by a caller holding employer
    provenance. No company-name/domain-token heuristic is used here. ATS host
    lookalikes remain rejected even when employer support is enabled.
    """
    if not _valid_http_url(url) or is_third_party_url(url):
        return ""
    try:
        host = (urlsplit(_text(url)).hostname or "").casefold().rstrip(".")
        provider = ats_type_for_url(_text(url))
        if not provider:
            if not allow_employer:
                return ""
            if any(domain in host for domain in KNOWN_ATS_DOMAINS):
                return ""
        if not is_specific_job_posting_url(_text(url)):
            return ""
        key = verified_posting_key_from_url(_text(url))
        if not provider and key.startswith((
            "ashby:", "greenhouse:", "icims:", "lever:", "smartrecruiters:", "workday:",
        )):
            # Older URL parsers use substring matches for some ATS families.
            # Employer opt-in must not turn those lookalikes into ATS proof.
            return ""
        return key
    except (TypeError, ValueError):
        return ""


def classify_missing_verified_identity(
    row: Mapping[str, Any],
) -> MissingVerifiedIdentityFinding:
    """Classify one missing-key row without changing or supplementing evidence.

    A: existing independent stored evidence supports this exact key.
    B: evidence is absent, ambiguous, rejected, or requires fresh verification.
    C: malformed data or a contradictory stored identity/official URL.
    """
    official_url = _text(row.get("official_job_url"))
    evidence: dict[str, Any] = {
        "provenance_caveat": PROVENANCE_CAVEAT,
        "stored_fields": {
            name: row.get(name) for name in (
                "title", "company_name", "location", "status",
                "enrichment_status", "previous_status", "http_status",
                "requested_url", "final_url", "resolved_candidate_url",
                "extraction_method", "resolved_candidate_title",
                "resolved_candidate_company", "resolved_candidate_location",
                "identity_validation_reason", "identity_confidence",
                "title_similarity", "company_similarity", "official_job_url",
                "official_url_status", "official_url_source",
                "official_url_resolved_at", "official_url_confidence",
                "official_url_validation_reason", "official_resolved_title",
                "official_resolved_company", "official_resolved_location",
                "verified_posting_key", "verified_posting_key_source",
                "verified_posting_key_trust", "reused_description",
                "matched_prior_record_key", "matched_prior_source",
                "resolved_candidate_title_source", "resolved_candidate_company_source",
                "resolved_candidate_location_source", "official_resolved_title_source",
                "official_resolved_company_source", "official_resolved_location_source",
            )
        },
    }
    evidence["missing_independent_identity_fields"] = [
        name for name in ("resolved_candidate_title", "resolved_candidate_company",
                          "official_resolved_title", "official_resolved_company")
        if not _text(row.get(name))
    ]
    derived_key = ""

    def finding(classification: str, reason: str) -> MissingVerifiedIdentityFinding:
        recoverable = classification == CLASS_RECOVERABLE
        return MissingVerifiedIdentityFinding(
            classification=classification, reason=reason,
            record_key=_text(row.get("record_key")),
            official_job_url=official_url, derived_key=derived_key,
            proposed_source=(VERIFIED_KEY_SOURCE_OFFICIAL_FOUND_VERIFIED
                             if recoverable else None),
            proposed_trust=VERIFIED_KEY_TRUST_TRUSTED if recoverable else None,
            proposed_timestamp=(row.get("official_url_resolved_at")
                                if recoverable else None),
            proposed_confidence=(float(row["official_url_confidence"])
                                 if recoverable else None),
            evidence=evidence,
        )

    if _text(row.get("official_url_status")) != OFFICIAL_FOUND_VERIFIED:
        return finding(CLASS_UNSAFE, "official status is not FOUND_VERIFIED")
    if not _text(row.get("record_key")):
        return finding(CLASS_INCONSISTENT, "record key is missing")
    if not _valid_http_url(official_url):
        return finding(CLASS_INCONSISTENT, "official URL is absent or malformed")
    if is_third_party_url(official_url):
        return finding(CLASS_UNSAFE, "third-party official URL cannot establish identity")

    allow_employer = _text(row.get("official_url_source")).casefold() == "employer"
    derived_key = supported_specific_posting_key(official_url, allow_employer=allow_employer)
    if not derived_key:
        return finding(CLASS_UNSAFE, "official URL is not a supported specific posting")

    stored_key = _text(row.get("verified_posting_key"))
    if stored_key:
        return finding(CLASS_INCONSISTENT,
                       "stored verified identity conflicts with official URL"
                       if stored_key != derived_key else "verified key is already present")
    trust = _text(row.get("verified_posting_key_trust")).casefold()
    if trust in {"untrusted", "conflict"}:
        return finding(CLASS_INCONSISTENT, "stored verified-key trust rejects recovery")
    if trust and trust != VERIFIED_KEY_TRUST_TRUSTED:
        return finding(CLASS_UNSAFE, "unrecognized stored verified-key trust")
    stored_source = _text(row.get("verified_posting_key_source")).casefold()
    if stored_source not in {"", "official_found_verified", "source_redirect_verified"}:
        return finding(CLASS_UNSAFE, "stored key provenance cannot be independently recovered")

    # A source's third-party apply/requested URL is expected; any concrete
    # official URL evidence it stores must agree with this exact posting.
    url_keys: dict[str, str] = {"official_job_url": derived_key}
    for name in ("final_url", "resolved_candidate_url", "requested_url", "apply_url"):
        url = _text(row.get(name))
        if not url:
            continue
        if not _valid_http_url(url):
            return finding(CLASS_INCONSISTENT, f"{name} is malformed")
        key = supported_specific_posting_key(url, allow_employer=allow_employer)
        if key:
            url_keys[name] = key
            if key != derived_key:
                evidence["url_keys"] = url_keys
                return finding(CLASS_INCONSISTENT, f"{name} conflicts with official posting identity")
    evidence["url_keys"] = url_keys
    if url_keys.get("final_url") != derived_key:
        return finding(CLASS_UNSAFE, "own fetched final URL does not independently bind the official key")

    status = next((_text(row.get(name)) for name in
                   ("status", "enrichment_status", "previous_status")
                   if _text(row.get(name))), "")
    if status != "enriched":
        return finding(CLASS_UNSAFE, "own fetch was not successfully enriched")
    try:
        http_status = int(row.get("http_status"))
    except (TypeError, ValueError, OverflowError):
        return finding(CLASS_UNSAFE, "successful own HTTP status is missing")
    if not 200 <= http_status < 300:
        return finding(CLASS_UNSAFE, "own fetch did not have a successful HTTP status")
    if (row.get("reused_description") is not False
            and row.get("reused_description") != 0):
        return finding(CLASS_UNSAFE, "own fetch provenance is missing or description was reused")
    if any(_text(row.get(name)) for name in ("matched_prior_record_key", "matched_prior_source")):
        return finding(CLASS_UNSAFE, "prior-record relation cannot establish independent verification")
    if "reuse" in _text(row.get("description_source")).casefold():
        return finding(CLASS_UNSAFE, "description provenance records reuse")
    if _text(row.get("extraction_method")).casefold() not in STRUCTURED_EXTRACTION_METHODS:
        return finding(CLASS_UNSAFE, "strong structured fetch corroboration is missing")

    for name in ("title", "company_name", "resolved_candidate_title",
                 "resolved_candidate_company", "official_resolved_title",
                 "official_resolved_company"):
        if not _text(row.get(name)):
            return finding(CLASS_UNSAFE, f"independent stored {name} evidence is missing")
    for name in ("resolved_candidate_title_source", "resolved_candidate_company_source",
                 "official_resolved_title_source", "official_resolved_company_source"):
        source = _text(row.get(name)).casefold()
        if source and source not in FETCHED_IDENTITY_SOURCES:
            return finding(CLASS_UNSAFE, f"{name} is not independently fetched provenance")
    if _text(row.get("identity_validation_reason")).casefold() != "identity accepted":
        return finding(CLASS_UNSAFE, "own stored identity validation is missing or rejected")
    if _text(row.get("official_url_validation_reason")).casefold() not in {
        "identity accepted", "new source record independently resolved to known official key",
    }:
        return finding(CLASS_UNSAFE, "stored official identity validation is missing or rejected")

    for name, minimum in (
        ("identity_confidence", ACCEPT_WITH_COMPANY_THRESHOLD),
        ("official_url_confidence", ACCEPT_WITH_COMPANY_THRESHOLD),
        ("title_similarity", TITLE_REASONABLE_THRESHOLD),
        ("company_similarity", COMPANY_MATCH_THRESHOLD),
    ):
        if row.get(name) is None:
            return finding(CLASS_UNSAFE, f"stored {name} evidence is missing")
        try:
            value = float(row[name])
        except (TypeError, ValueError, OverflowError):
            return finding(CLASS_INCONSISTENT, f"stored {name} is malformed")
        if not math.isfinite(value) or not 0 <= value <= 1:
            return finding(CLASS_INCONSISTENT, f"stored {name} is outside its valid range")
        if value < minimum:
            return finding(CLASS_UNSAFE, f"stored {name} does not meet current acceptance threshold")
    if row.get("official_url_resolved_at") is None:
        return finding(CLASS_UNSAFE, "stored official verification timestamp is missing")
    try:
        verified_at = row["official_url_resolved_at"]
        if not isinstance(verified_at, datetime):
            verified_at = datetime.fromisoformat(str(verified_at).replace("Z", "+00:00"))
        if verified_at.tzinfo is None or verified_at.utcoffset() is None:
            raise ValueError("verification timezone is unavailable")
    except (TypeError, ValueError, OverflowError):
        return finding(CLASS_INCONSISTENT, "stored official verification timestamp is malformed or lacks timezone")

    for prefix, label in (("resolved_candidate", "candidate"), ("official_resolved", "official")):
        resolved_location = row.get(prefix + "_location")
        location_source = row.get(prefix + "_location_source")
        validation = validate_job_identity(
            original_title=_text(row.get("title")),
            original_company=_text(row.get("company_name")),
            resolved_title=_text(row.get(prefix + "_title")),
            resolved_company=_text(row.get(prefix + "_company")),
            original_location=row.get("location"),
            resolved_location=resolved_location,
            resolved_location_source=location_source,
        )
        evidence["current_" + label + "_validation"] = asdict(validation)
        if not validation.accepted:
            return finding(CLASS_UNSAFE, f"current {label} identity validation rejected: {validation.reason}")
        if location_source not in AUTHORITATIVE_LOCATION_SOURCES:
            # Counterfactual veto only: this does not assert a persisted source
            # or establish acceptance. Missing provenance can never erase an
            # obvious geography conflict from a proposed metadata repair.
            possible_status, possible_reason = location_compatibility(
                row.get("location"), resolved_location, "jobposting_json_ld",
            )
            if possible_status == "mismatch":
                evidence["potential_location_conflict"] = {
                    "fields": label, "reason": possible_reason,
                    "source_is_stored": False,
                }
                return finding(CLASS_UNSAFE, "potential geography conflict requires independent location provenance")

    return finding(CLASS_RECOVERABLE,
                   "own stored successful fetch and accepted independent identity evidence support the exact official key")
