"""Offline tests for read-only, existing-score 49% audits."""
from __future__ import annotations

from datetime import date, datetime, timezone
import json
import unittest

from src.audit_49_scores import audit_score, build_report, local_day
from src.ai.resume_matcher import MATCHER_PROMPT_VERSION
from src.test_resume_matcher_validation import make_profile


def row(*, gap=None, description="", score=49, company="Example"):
    p = make_profile(
        production_skills=["SQL Server", "Snowflake", "Python", "Spark"],
        tools_platforms=["dbt", "Spark", "LookML"],
        project_skills=["Personal FastAPI portfolio project"],
    )
    return {
        "record_key": company.lower(),
        "resume_hash": "test-hash",
        "title": "Data Engineer",
        "company_name": company,
        "source": "lensa",
        "email_date": "2026-10-11",
        "description": description,
        "location": "Remote",
        "profile_json": p.model_dump_json(),
        "scored_at": datetime(2026, 10, 15, 16, 15, tzinfo=timezone.utc),
        "overall_score": score,
        "recommendation": "skip" if score == 49 else "apply",
        "title_fit": 95,
        "skills_fit": 88,
        "experience_fit": 90,
        "seniority_fit": 91,
        "industry_fit": 80,
        "location_fit": 100,
        "confidence": "high",
        "matching_strengths": json.dumps(["Strong SQL"]),
        "hard_requirements_missing": json.dumps([gap] if gap else []),
        "preferred_qualifications_missing": "[]",
        "risk_factors": "[]",
        "summary": "Synthetic stored assessment",
        "description_word_count": len(description.split()),
        "description_complete": True,
        "model_name": "offline",
        "prompt_version": MATCHER_PROMPT_VERSION,
        "description_is_current": True,
    }


class Existing49AuditTests(unittest.TestCase):
    def test_local_day_is_calendar_day_in_pacific(self):
        self.assertEqual(
            local_day(datetime(2026, 10, 15, 4, tzinfo=timezone.utc),
                      "America/Los_Angeles"),
            date(2026, 10, 14),
        )

    def test_audit_hard_gap_shows_clause_and_component_score(self):
        jd = (
            "<h2>Responsibilities</h2><p>"
            + "Build SQL Server data pipelines and governed reports. " * 15
            + "</p><h2>What You Bring</h2>"
            "<p>3+ years in data engineering or analytics engineering, "
            "including at least 2 years building production dbt projects.</p>"
        )
        gap = (
            "Major gap: 2+ years of professional dbt experience "
            "(not demonstrated in professional experience)"
        )
        item = audit_score(row(gap=gap, description=jd, company="John Burns"))
        self.assertGreater(item["component_score_without_final_49_cap"], 49)
        self.assertEqual(
            item["gaps"][0]["policy_status"],
            "still_missing_under_tenure_policy",
        )
        self.assertTrue(any(
            "2 years building production dbt projects" in c["text"]
            and c["mandatory"] and not c["optional"]
            for c in item["gaps"][0]["matching_saved_jd_clauses"]
        ))
        self.assertEqual(item["current_replay"]["score"], 49)
        serialized = json.dumps(item)
        self.assertNotIn("profile_json", serialized)
        self.assertNotIn("Personal FastAPI portfolio project", serialized)

    def test_false_platform_gap_is_flagged_without_writing_scores(self):
        jd = (
            "<h2>Requirements</h2>"
            "<p>Hands-on experience with Databricks or a comparable "
            "Spark-based lakehouse.</p>"
        )
        item = audit_score(row(
            gap="Major gap: Databricks production experience",
            description=jd,
        ))
        self.assertEqual(
            item["gaps"][0]["policy_status"],
            "not_confirmed_by_current_policy",
        )
        self.assertTrue(item["needs_attention"])

    def test_49_without_hard_gaps_is_detected(self):
        item = audit_score(row())
        self.assertEqual(item["stored_hard_gap_count"], 0)
        self.assertTrue(item["needs_attention"])

    def test_report_filters_by_scoring_date_not_email_date(self):
        first = row(company="First")
        second = row(company="Second", score=88)
        third = row(company="Third")
        third["scored_at"] = datetime(
            2026, 10, 14, 16, tzinfo=timezone.utc
        )
        result = build_report(
            [first, second, third],
            date(2026, 10, 15), "America/Los_Angeles",
        )
        self.assertEqual(result["total_scored_on_date"], 2)
        self.assertEqual(result["score_49_count"], 1)
        self.assertEqual(result["score_49_without_stored_hard_gap"], 1)
        self.assertEqual(result["jobs"][0]["company"], "First")
        self.assertTrue(result["read_only"])


if __name__ == "__main__":
    unittest.main()
