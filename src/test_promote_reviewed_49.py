"""Offline tests for explicit, audited promotions of wrongly capped 49% records."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch
import unittest

from src.promote_reviewed_49 import (
    approvals, candidates_for_day, promotion_candidate,
)
from src.test_audit_49_scores import row


def piedmont() -> dict:
    description = (
        "<h2>Responsibilities</h2><p>"
        + "Build SQL analytical pipelines and governed dashboards for data teams. " * 12
        + "</p><h2>Required qualifications</h2>"
        "<p>1 year of analysis work including Data Visualization and or Data Modeling, "
        "preferably in a multi-entity healthcare provider environment.</p>"
        "<p>Bachelor's degree in Business, Finance, Accounting, "
        "Information Technology, or closely-related field Required.</p>"
    )
    saved = row(
        company="Piedmont", description=description,
        gap="Experience in a multi-entity healthcare provider environment",
    )
    saved["hard_requirements_missing"] = json.dumps([
        "Experience in a multi-entity healthcare provider environment",
        "Major gap: ledger or accounting data-domain expertise",
    ])
    return saved


def result(score=74, recommendation="review", gaps=()):
    return SimpleNamespace(
        overall_score=score,
        recommendation=recommendation,
        description_complete=True,
        analysis=SimpleNamespace(hard_requirements_missing=list(gaps)),
    )


class Reviewed49PromotionTests(unittest.TestCase):
    def test_piedmont_stored_hard_gaps_both_invalid(self):
        saved = piedmont()
        with patch("src.promote_reviewed_49.replay", return_value=result()):
            candidate = promotion_candidate(saved)
        self.assertIsNotNone(candidate)
        preview, _ = candidate
        self.assertEqual(preview["company"], "Piedmont")
        self.assertEqual(preview["old_score"], 49)
        self.assertEqual(preview["new_score"], 74)
        self.assertEqual(preview["new_recommendation"], "review")
        self.assertEqual(len(preview["previous_gaps"]), 2)
        self.assertEqual(len(preview["approval_fingerprint"]), 64)
        self.assertNotIn(saved["description"], json.dumps(preview))
        self.assertNotIn(saved["profile_json"], json.dumps(preview))

    def test_fingerprint_changes_if_description_profile_or_score_changes(self):
        saved = piedmont()
        with patch("src.promote_reviewed_49.replay", return_value=result()):
            original = promotion_candidate(saved)[0]["approval_fingerprint"]
            for key, new in (
                ("description", saved["description"] + " Changed"),
                ("profile_json", saved["profile_json"] + " "),
                ("scored_at", saved["scored_at"] + timedelta(seconds=1)),
                ("resume_hash", "new-resume"),
                ("record_key", "new-job"),
            ):
                changed = dict(saved, **{key: new})
                self.assertNotEqual(
                    promotion_candidate(changed)[0]["approval_fingerprint"],
                    original, key,
                )

    def test_genuine_hard_gap_never_promoted_even_if_replay_is_review(self):
        saved = row(
            gap="Experience in a healthcare setting",
            description=(
                "<h2>Required Qualifications</h2>"
                "<p>Experience in a healthcare setting.</p>"
            ),
        )
        with patch("src.promote_reviewed_49.replay", return_value=result()):
            self.assertIsNone(promotion_candidate(saved))

    def test_new_replay_hard_gap_or_apply_are_not_automatically_promoted(self):
        saved = piedmont()
        for replay_result in (
            result(score=74, gaps=["Missing capability"]),
            result(score=90, recommendation="apply"),
            result(score=49),
        ):
            with patch("src.promote_reviewed_49.replay", return_value=replay_result):
                self.assertIsNone(promotion_candidate(saved))

    def test_old_stale_or_partial_scores_not_eligible(self):
        saved = piedmont()
        for modifications in (
            {"description_is_current": False},
            {"description_complete": False},
            {"recommendation": "review"},
            {"overall_score": 83},
            {"profile_json": ""},
            {"prompt_version": "v1"},
        ):
            with patch("src.promote_reviewed_49.replay", return_value=result()):
                self.assertIsNone(promotion_candidate(dict(saved, **modifications)))

    def test_only_matching_scored_date_is_included(self):
        old = piedmont()
        other = dict(piedmont(), record_key="older")
        other["scored_at"] = datetime(2026, 10, 14, 16, tzinfo=timezone.utc)
        with patch("src.promote_reviewed_49.replay", return_value=result()):
            proposals, errors = candidates_for_day(
                [old, other], date(2026, 10, 15), "America/Los_Angeles"
            )
        self.assertEqual(len(proposals), 1)
        self.assertEqual(errors, [])

    def test_explicit_approval_list_must_be_valid_and_unique(self):
        with TemporaryDirectory() as folder:
            manifest = Path(folder) / "approval.json"
            digest = "a" * 64
            manifest.write_text(json.dumps({
                "version": 1, "approved_fingerprints": [digest]
            }))
            self.assertEqual(approvals(manifest), {digest})
            for value in (
                {"version": 1, "approved_fingerprints": []},
                {"version": 1, "approved_fingerprints": [digest, digest]},
                {"version": 1, "approved_fingerprints": ["bad"]},
                {"version": 1, "approved_fingerprints": [digest], "extra": True},
            ):
                manifest.write_text(json.dumps(value))
                with self.assertRaises(ValueError):
                    approvals(manifest)


if __name__ == "__main__":
    unittest.main()
