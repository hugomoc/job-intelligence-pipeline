"""Regression matrix for select_job_open_target: every link state, exact label,
destination and note. Only the matched-but-not-FOUND_VERIFIED label changed;
expectations below are the pre-change values captured from the unmodified code.
"""
import unittest
from unittest.mock import patch

from src.ui.job_links import select_job_open_target

OFFICIAL = "https://boards.greenhouse.io/acme/jobs/123"
RESOLVED = "https://careers.acme.example/jobs/456"
LINKEDIN = "https://www.linkedin.com/jobs/view/4400000001/"
GLASSDOOR = "https://www.glassdoor.com/job-listing/data-engineer-acme-JV_IC1.htm"
THIRD_PARTY = "https://www.lensa.com/job/data-engineer/789"

VERIFIED_ACTIVE_NOTE = "Verified official employer/ATS posting."
UNCONFIRMED_NOTE = "Current actionability is unconfirmed."
NOT_ACTIONABLE_NOTE = "Posting is not currently actionable for a new application."
AGGREGATOR_NOTE = "Aggregator listing. Employer posting and current activity are not verified."
THIRD_PARTY_NOTE = "Third-party listing; not verified employer/ATS evidence. Current actionability is unknown."
MATCHED_NOTE = "Resolved page passed identity validation."
DEFAULT_NOTE = "Verified official application link unavailable; this opens the source listing."


def job(**fields):
    return {"source": "linkedin", "apply_url": LINKEDIN, **fields}


def verified(**fields):
    return job(**{"official_url_status": "FOUND_VERIFIED", "official_job_url": OFFICIAL, **fields})


def matched(**fields):
    return job(**{"resolved_candidate_url": RESOLVED, "enrichment_status": "enriched",
                  "identity_confidence": 0.9, **fields})


# (state, effective_status patch or None, input variants, expected label, url, note)
CASES = [
    ("third_party_official_url", None,
     [job(official_job_url=THIRD_PARTY, official_url_status="FOUND_VERIFIED"),
      job(official_job_url=THIRD_PARTY, official_url_status="OFFICIAL_NOT_FOUND")],
     "Inspect third-party listing", THIRD_PARTY, THIRD_PARTY_NOTE),
    ("verified_official_active", "ACTIVE", [verified()],
     "Open official job", OFFICIAL, VERIFIED_ACTIVE_NOTE),
    ("verified_official_actionability_unknown", "UNKNOWN", [verified()],
     "Open official job", OFFICIAL, UNCONFIRMED_NOTE),
    ("verified_official_inactive", "INACTIVE", [verified()],
     "Inspect posting (inactive)", OFFICIAL, NOT_ACTIONABLE_NOTE),
    ("verified_official_location_mismatch", "LOCATION_MISMATCH", [verified()],
     "Inspect posting (location mismatch)", OFFICIAL, NOT_ACTIONABLE_NOTE),
    ("aggregator_usable_source_url", None,
     [job(source="glassdoor", apply_url=GLASSDOOR),
      job(source="glassdoor", apply_url=GLASSDOOR, official_url_status="OFFICIAL_NOT_FOUND",
          resolved_candidate_url=RESOLVED, enrichment_status="enriched", identity_confidence=0.95)],
     "Open original source listing — unverified", GLASSDOOR, AGGREGATOR_NOTE),
    ("aggregator_unusable_source_url", None,
     [job(source="glassdoor", apply_url=""), job(source="glassdoor", apply_url="javascript:void(0)")],
     "Open original source listing — unverified", None, AGGREGATOR_NOTE),
    # The corrected state: identity-matched candidate page, official status not FOUND_VERIFIED.
    ("matched_candidate_not_found_verified", None,
     [matched(official_url_status="OFFICIAL_NOT_FOUND"),
      matched(official_url_status="SEARCH_DEFERRED", identity_confidence=0.78),
      matched(official_url_status="AMBIGUOUS"),
      matched(identity_confidence=None)],                       # no official status at all
     "Open matched posting (not verified)", RESOLVED, MATCHED_NOTE),
    # FOUND_VERIFIED reached this branch only because no usable official URL exists:
    # the existing label is retained (no change to verification semantics).
    ("matched_candidate_found_verified_without_official_url", None,
     [matched(official_url_status="FOUND_VERIFIED")],
     "Open verified job", RESOLVED, MATCHED_NOTE),
    ("identity_not_passed_with_official_status", None,
     [matched(official_url_status="OFFICIAL_NOT_FOUND", identity_confidence=0.5),
      matched(official_url_status="OFFICIAL_NOT_FOUND", enrichment_status="resolution_rejected"),
      job(official_url_status="OFFICIAL_NOT_FOUND")],
     "Open source page", LINKEDIN, "Official job link status: OFFICIAL_NOT_FOUND."),
    ("official_status_without_source_url", None,
     [job(apply_url="", official_url_status="SEARCH_DEFERRED")],
     "Open source page", None, "Official job link status: SEARCH_DEFERRED."),
    ("default_source_url", None,
     [job(), matched(identity_confidence=0.1), job(official_url_status="FOUND_VERIFIED")],
     "Open job", LINKEDIN, DEFAULT_NOTE),
    ("default_without_source_url", None,
     [job(apply_url=""), job(apply_url="", official_url_status="FOUND_VERIFIED")],
     "Open job", None, DEFAULT_NOTE),
]


def target_for(job_fields, actionability):
    if actionability is None:
        return select_job_open_target(job_fields)
    with patch("src.actionability.effective_status", return_value=actionability):
        return select_job_open_target(job_fields)


class JobOpenTargetMatrix(unittest.TestCase):
    def test_every_link_state_label_destination_and_note(self):
        for state, actionability, variants, label, url, note in CASES:
            with self.subTest(state=state):
                for variant in variants:
                    target = target_for(variant, actionability)
                    self.assertEqual((target.label, target.url, target.note), (label, url, note), variant)

    def test_matrix_covers_each_distinct_state_once(self):
        self.assertEqual(len({case[0] for case in CASES}), len(CASES))


if __name__ == "__main__":
    unittest.main()
