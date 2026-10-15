"""Offline tests for deterministic audit of cached scores."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from src.audit_cached_fit import decode_list, replay, should_downgrade, needs_manual_review
from src.ai.resume_matcher import MATCHER_PROMPT_VERSION
from src.fit_gap_evidence import (
    approval_fingerprint, approved_candidates, approval_tokens, candidate_evidence,
)
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

    def test_replay_corrects_john_burns_cached_82_review(self):
        """Previously scored jobs can be corrected without a second AI call."""
        row = self.row(
            title="Data Engineer",
            company_name="John Burns Research and Consulting",
            description=(
                "<h2>What You Do</h2><p>"
                + "Build SQL Server data pipelines and implement governed dbt "
                  "models for reporting and metrics. " * 12
                + "</p><h2>What You Bring</h2>"
                "<p>3+ years in data engineering or analytics engineering, "
                "including at least 2 years building production dbt projects.</p>"
            ),
            overall_score=82,
            recommendation="review",
            profile_json=make_profile(
                production_skills=["SQL Server", "Python", "Snowflake", "LookML"],
                project_skills=["dbt Core data platform demo"],
                tools_platforms=["dbt"],
            ).model_dump_json(),
        )
        new_match = replay(row)
        self.assertEqual(new_match.overall_score, 49)
        self.assertEqual(new_match.recommendation, "skip")
        self.assertTrue(should_downgrade(row, new_match))
        self.assertTrue(any(
            "2+ years of professional dbt" in gap
            for gap in new_match.analysis.hard_requirements_missing
        ))
        proof = candidate_evidence(row, new_match)
        self.assertTrue(proof["all_new_gaps_have_explicit_mandatory_clause"])
        snippets = proof["gaps"][0]["matching_saved_jd_clauses"]
        self.assertEqual(len(snippets), 1)
        self.assertIn("2 years building production dbt", snippets[0]["text"])
        self.assertTrue(snippets[0]["mandatory"])
        self.assertEqual(len(approved_candidates(
            [(row, new_match)], {approval_fingerprint(row, new_match)}
        )), 1)

    def test_john_burns_replay_rejects_undated_dbt_tool_inventory(self):
        """Read-only replay must find the real hard gap even with dbt in tools."""
        row = self.row(
            title="Data Engineer",
            company_name="John Burns Research and Consulting",
            overall_score=82,
            recommendation="review",
            description=(
                "<h2>What You Bring</h2>"
                "<p>3+ years in data engineering or analytics engineering, "
                "including at least 2 years building production dbt projects.</p>"
            ),
            profile_json=make_profile(
                production_skills=["SQL Server", "Python", "Snowflake", "LookML"],
                project_skills=["AI-assisted FastAPI portfolio demo"],
                tools_platforms=["dbt", "Looker", "Spark"],
            ).model_dump_json(),
        )
        result = replay(row)
        self.assertEqual(result.overall_score, 49)
        self.assertEqual(result.recommendation, "skip")
        self.assertTrue(should_downgrade(row, result))
        proof = candidate_evidence(row, result)
        self.assertTrue(proof["all_new_gaps_have_explicit_mandatory_clause"])
        self.assertEqual(len(approved_candidates(
            [(row, result)], {approval_fingerprint(row, result)}
        )), 1)

    def test_tenure_evidence_rejects_generic_years_and_platform_alternatives(self):
        row = self.row(description=(
            "<h2>Required Qualifications</h2>"
            "<p>3+ years of data engineering, including some dbt exposure.</p>"
            "<p>2+ years using dbt or equivalent transformation frameworks.</p>"
        ))
        proposed = SimpleNamespace(
            overall_score=49, recommendation="skip",
            analysis=SimpleNamespace(hard_requirements_missing=[
                "Major gap: 2+ years of professional dbt experience"
            ]),
        )
        proof = candidate_evidence(row, proposed)
        self.assertFalse(proof["all_new_gaps_have_explicit_mandatory_clause"])
        self.assertEqual(proof["gaps"][0]["matching_saved_jd_clauses"], [])
        with self.assertRaisesRegex(ValueError, "no explicit"):
            approved_candidates(
                [(row, proposed)], {approval_fingerprint(row, proposed)}
            )

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

    def test_metlife_report_quotes_saved_mandatory_dbt_cloud_clause(self):
        row = self.row()
        result = replay(row)
        proof = candidate_evidence(row, result)
        self.assertTrue(proof["all_new_gaps_have_explicit_mandatory_clause"])
        snippets = proof["gaps"][0]["matching_saved_jd_clauses"]
        self.assertEqual(len(snippets), 1)
        self.assertIn("3+ years of hands-on experience using dbt Cloud", snippets[0]["text"])
        self.assertEqual(snippets[0]["section"], "required")
        self.assertTrue(snippets[0]["mandatory"])
        self.assertNotIn("profile_json", proof)
        self.assertNotIn("description", proof)
        selected = approved_candidates([(row, result)], {approval_fingerprint(row, result)})
        self.assertEqual(len(selected), 1)

    def test_do_not_approve_nonmandatory_or_unrelated_clause(self):
        row = self.row(description=(
            "<h2>Requirements</h2><p>SQL and Python production pipelines.</p>"
            "<h2>Preferred Qualifications</h2>"
            "<p>3+ years of hands-on experience using dbt Cloud.</p>"
        ))
        candidate = SimpleNamespace(
            overall_score=49, recommendation="skip", description_complete=True,
            analysis=SimpleNamespace(hard_requirements_missing=[
                "Major gap: 3+ years of professional dbt Cloud experience"
            ]),
        )
        proof = candidate_evidence(row, candidate)
        self.assertFalse(proof["all_new_gaps_have_explicit_mandatory_clause"])
        self.assertFalse(proof["gaps"][0]["matching_saved_jd_clauses"][0]["mandatory"])
        with self.assertRaisesRegex(ValueError, "no explicit"):
            approved_candidates(
                [(row, candidate)], {approval_fingerprint(row, candidate)}
            )

    def test_approvals_are_bound_to_exact_description_and_profile(self):
        row = self.row()
        match = replay(row)
        approved = {approval_fingerprint(row, match)}
        self.assertNotEqual(
            approval_fingerprint(row, match),
            approval_fingerprint(
                {**row, "description": row["description"] + " changed"}, match
            ),
        )
        with self.assertRaisesRegex(ValueError, "unknown or stale"):
            approved_candidates(
                [({**row, "description": row["description"] + " changed"}, match)],
                approved,
            )
        with self.assertRaisesRegex(ValueError, "unknown or stale"):
            approved_candidates([(row, match)], {"1" * 64})

    def test_approval_manifest_is_required_and_validated(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "approved.json"
            with self.assertRaisesRegex(ValueError, "Could not read"):
                approval_tokens(path)
            for value in ({}, {"version": 1, "approved_fingerprints": []},
                          {"version": 1, "approved_fingerprints": ["bogus"]}):
                path.write_text(json.dumps(value))
                with self.assertRaises(ValueError):
                    approval_tokens(path)
            path.write_text(json.dumps({
                "version": 1, "approved_fingerprints": ["a" * 64]
            }))
            self.assertEqual(approval_tokens(path), {"a" * 64})

    def test_apply_never_defaults_to_all_rows(self):
        process = subprocess.run(
            [sys.executable, "-m", "src.audit_cached_fit", "--apply"],
            capture_output=True, text=True,
        )
        self.assertNotEqual(process.returncode, 0)
        self.assertIn("--apply requires --approved-file", process.stderr)

    def test_json_list_decode_is_safe(self):
        self.assertEqual(decode_list(None), [])
        self.assertEqual(decode_list("{garbled"), [])
        self.assertEqual(decode_list('["dbt Cloud"]'), ["dbt Cloud"])


if __name__ == "__main__":
    unittest.main()
