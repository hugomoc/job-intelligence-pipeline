"""Offline tests for deterministic audit of cached scores."""
import unittest
from types import SimpleNamespace

from src.audit_cached_fit import decode_list, replay, should_downgrade, needs_manual_review
from src.ai.resume_matcher import MATCHER_PROMPT_VERSION
from src.test_resume_matcher_validation import make_profile


class CachedFitAuditTests(unittest.TestCase):
    def row(self, **updates):
        values = {
            "record_key": "fixture",
            "title": "Data Analytics Engineer",
            "company_name": "Example Company",
            "description": (
                "<h2>Responsibilities</h2><p>"
                + (
                    "Build and maintain reliable production SQL and Python pipelines "
                    "on AWS and partner with business analysts to deliver governed data models. "
                ) * 9
                + "</p><h2>Required Qualifications</h2>"
                "<p>3+ years of hands-on experience using dbt Cloud.</p>"
            ),
            "location": "Remote",
            "resume_hash": "hash",
            "model_name": "test",
            "prompt_version": MATCHER_PROMPT_VERSION,
            "description_is_current": True,
            "description_complete": True,
            "overall_score": 91,
            "recommendation": "apply",
            "title_fit": 91,
            "skills_fit": 91,
            "experience_fit": 91,
            "seniority_fit": 91,
            "industry_fit": 91,
            "location_fit": 91,
            "confidence": "high",
            "matching_strengths": "[]",
            "hard_requirements_missing": "[]",
            "preferred_qualifications_missing": "[]",
            "risk_factors": "[]",
            "summary": "Synthetic cached result",
            "profile_json": make_profile(
                production_skills=["SQL", "Python"],
                project_skills=["dbt", "Prefect"],
            ).model_dump_json(),
        }
        values.update(updates)
        return values

    def test_replay_downgrades_mandatory_cloud_platform_tenure(self):
        row = self.row()
        result = replay(row)
        self.assertEqual(result.overall_score, 49)
        self.assertEqual(result.recommendation, "skip")
        self.assertTrue(should_downgrade(row, result))

    def test_replay_handles_ambiguous_professional_years_as_review(self):
        row = self.row(profile_json=make_profile(
            production_skills=["SQL", "Python", "dbt Cloud"],
        ).model_dump_json())
        result = replay(row)
        self.assertEqual(result.overall_score, 74)
        self.assertEqual(result.recommendation, "review")
        self.assertFalse(should_downgrade(row, result))
        self.assertTrue(needs_manual_review(row, result))

    def test_no_upgrade_and_no_stale_overwrite(self):
        row = self.row(overall_score=49, recommendation="skip")
        upgraded = SimpleNamespace(overall_score=85, recommendation="apply", description_complete=True)
        self.assertFalse(should_downgrade(row, upgraded))
        lowered = SimpleNamespace(overall_score=40, recommendation="skip", description_complete=True)
        self.assertFalse(should_downgrade({**row, "description_is_current": False}, lowered))
        self.assertFalse(should_downgrade({**row, "prompt_version": "v1"}, lowered))
        self.assertFalse(should_downgrade({**row, "description_complete": False}, lowered))
        self.assertFalse(should_downgrade(
            self.row(overall_score=93, recommendation="apply"),
            SimpleNamespace(overall_score=92, recommendation="apply", description_complete=True),
        ))

    def test_score_drift_alone_never_changes_database(self):
        row = self.row(overall_score=94, recommendation="apply")
        small = SimpleNamespace(
            overall_score=87, recommendation="apply", description_complete=True,
            analysis=SimpleNamespace(hard_requirements_missing=[]),
        )
        self.assertFalse(should_downgrade(row, small))
        self.assertFalse(needs_manual_review(row, small))

    def test_review_recommendation_change_is_manual_only(self):
        row = self.row(overall_score=93, recommendation="apply")
        review = SimpleNamespace(
            overall_score=93, recommendation="review", description_complete=True,
            analysis=SimpleNamespace(hard_requirements_missing=[]),
        )
        self.assertFalse(should_downgrade(row, review))
        self.assertTrue(needs_manual_review(row, review))

    def test_previous_hard_gap_is_not_a_new_automated_downgrade(self):
        gap = "Major gap: 3+ years of professional dbt Cloud experience (not demonstrated in professional experience)"
        row = self.row(
            hard_requirements_missing='["' + gap + '"]'
        )
        match = replay(row)
        self.assertEqual(match.recommendation, "skip")
        self.assertFalse(should_downgrade(row, match))

    def test_json_list_decode_is_safe(self):
        self.assertEqual(decode_list(None), [])
        self.assertEqual(decode_list("{garbled"), [])
        self.assertEqual(decode_list('["dbt Cloud"]'), ["dbt Cloud"])


if __name__ == "__main__":
    unittest.main()
