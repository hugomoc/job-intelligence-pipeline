"""Portable offline invariants shared by fit scoring and job eligibility."""

import unittest
from unittest.mock import patch

from src.ai import resume_matcher
from src.ai.job_eligibility import JobEligibilityAnalysis, normalize_job_eligibility
from src.ai.requirement_policy import (
    central_specialization_gaps,
    description_requirement_clauses,
    personal_requirement_evidence,
    validate_hard_gap_clause,
)
from src.ai.resume_profiler import CandidateEligibilityFacts
from src.job_admission import evaluate_job_admission
from src.test_resume_matcher_validation import make_analysis, make_profile


# Give every synthetic posting a substantive complete description. Its shared
# delivery text provides ordinary fit signals without inventing specialization.
DELIVERY = (
    "Responsibilities include developing SQL analytics data pipelines with "
    "Snowflake and AWS, collaborating with stakeholders, documenting reliable "
    "data models, monitoring workflow quality, and delivering trustworthy "
    "reporting datasets for business teams. "
) * 5


def profile(**updates):
    values = {
        "production_skills": ["SQL", "Python", "Data pipelines"],
        "data_engineering_capabilities": ["Data pipelines"],
        "tools_platforms": ["Spark", "dbt"],
        "cloud_platforms": ["AWS", "GCP"],
        "databases_warehouses": ["Snowflake", "BigQuery", "SQL Server", "MySQL"],
    }
    values.update(updates)
    return make_profile().model_copy(update=values)


def job(clause, title="Data Engineer"):
    return {
        "record_key": "synthetic-posting",
        "title": title,
        "company_name": "Synthetic employer",
        "description": f"<h2>Responsibilities</h2><p>{DELIVERY}</p>{clause}",
    }


def proposed(gaps):
    return JobEligibilityAnalysis(
        decision="exclude", confidence="high", reason="Raw model claim",
        role_family_match="strong", critical_skill_gaps=gaps,
        admission_decision="exclude",
    )


def score(p, j, gaps):
    return resume_matcher.build_resume_job_match(
        p, j, "offline-test", make_analysis(gaps, score=85),
        len(j["description"].split()), True,
    )


class SharedRequirementPolicyTests(unittest.TestCase):
    def check_satisfied_alternative(self, clause, gap, p):
        j = job(f"<h2>Requirements</h2><p>{clause}</p>")
        self.assertTrue(resume_matcher.get_description_information(j)[2])
        direct = validate_hard_gap_clause(gap, j["description"], p)
        self.assertFalse(direct["keep_hard_gap"], direct)
        normalized = normalize_job_eligibility(proposed([gap]), p, j)
        self.assertEqual(normalized.decision, "eligible", normalized)
        self.assertEqual(normalized.critical_skill_gaps, [])
        match = score(p, j, [gap])
        self.assertEqual(match.analysis.hard_requirements_missing, [])
        self.assertGreater(match.overall_score, 49)
        return direct

    def test_matcher_exports_the_shared_validator_and_parser(self):
        self.assertIs(resume_matcher.validate_hard_gap_clause, validate_hard_gap_clause)
        self.assertIs(resume_matcher.description_requirement_clauses, description_requirement_clauses)

    def test_python_satisfies_java_or_python_for_both_gates(self):
        self.check_satisfied_alternative("Expert skills in Java or Python", "Java", profile())

    def test_neither_java_nor_python_retains_mandatory_gap_for_both_gates(self):
        p = profile(programming_languages=["SQL"], production_skills=["SQL", "Data pipelines"])
        j = job("<h2>Requirements</h2><p>Expert skills in Java or Python</p>")
        normalized = normalize_job_eligibility(proposed(["Java"]), p, j)
        self.assertEqual(normalized.decision, "exclude")
        self.assertIn("Java", normalized.critical_skill_gaps)
        self.assertEqual(score(p, j, ["Java"]).overall_score, 49)

    def test_sentence_ending_alias_punctuation_preserves_alternative(self):
        self.check_satisfied_alternative("Expert skills in Java or Python.", "Java", profile())

    def test_required_skills_heading_carries_mandatory_provenance(self):
        desc = "<h2>Required Skills and Experience:</h2><p>Expert knowledge of QLIK Advana.</p>"
        c = next(row for row in description_requirement_clauses(desc) if "Advana" in row["clause"])
        self.assertEqual(c["section"], "required")
        self.assertTrue(c["mandatory"])
        self.assertFalse(c["optional"])
        direct = validate_hard_gap_clause("QLIK Advana", desc, profile())
        self.assertTrue(direct["keep_hard_gap"], direct)

    def test_structured_null_personal_fields_are_unknown_not_negative(self):
        facts = CandidateEligibilityFacts.model_validate({
            "citizenship_country_codes": None,
            "authorized_work_countries": None,
            "requires_sponsorship": None,
            "security_clearances": None,
            "willing_to_relocate": None,
            "willing_to_travel": None,
            "max_travel_percent": None,
        })
        for clause, kind in (
            ("US citizenship is required", "citizenship"),
            ("US work authorization is required", "work_authorization"),
            ("Must hold current Secret security clearance", "clearance"),
        ):
            with self.subTest(kind=kind):
                self.check_personal(clause, kind, facts, "UNKNOWN")

    def test_spark_satisfies_spark_or_pyspark_for_both_gates(self):
        self.check_satisfied_alternative("Experience with Apache Spark or PySpark", "PySpark", profile())

    def test_snowflake_satisfies_warehouse_examples_for_both_gates(self):
        self.check_satisfied_alternative(
            "Familiarity with columnar data warehouses such as ClickHouse, Snowflake, BigQuery, or Redshift",
            "ClickHouse", profile(databases_warehouses=["Snowflake"]),
        )

    def test_bigquery_satisfies_warehouse_examples_for_both_gates(self):
        self.check_satisfied_alternative(
            "Familiarity with columnar data warehouses such as ClickHouse, Snowflake, BigQuery, or Redshift",
            "ClickHouse", profile(databases_warehouses=["BigQuery"]),
        )

    def test_comparable_oltp_uses_production_relational_evidence(self):
        direct = self.check_satisfied_alternative(
            "Postgres or comparable relational OLTP systems",
            "Postgres or comparable OLTP", profile(databases_warehouses=["SQL Server", "MySQL"]),
        )
        self.assertIn("sql_server", direct["clauses"][0]["resume_evidence"])
        self.assertIn("mysql", direct["clauses"][0]["resume_evidence"])

    def test_preferred_databricks_stays_optional_across_both_gates(self):
        j = job("<h2>Preferred qualifications</h2><p>Experience with Databricks</p>")
        clauses = description_requirement_clauses(j["description"])
        c = next(row for row in clauses if "Databricks" in row["clause"])
        self.assertEqual(c["section"], "optional")
        self.assertTrue(c["optional"])
        self.assertFalse(c["mandatory"])
        normalized = normalize_job_eligibility(proposed(["Databricks"]), profile(), j)
        self.assertEqual(normalized.decision, "eligible", normalized)
        self.assertNotIn("Databricks", normalized.critical_skill_gaps)
        self.assertGreater(score(profile(), j, ["Databricks"]).overall_score, 49)

    def test_desired_heading_does_not_inherit_required_section(self):
        desc = "<h2>Required qualifications</h2><p>SQL experience</p><h2>Desired Qualifications</h2><p>Microsoft Fabric experience</p>"
        c = next(row for row in description_requirement_clauses(desc) if "Fabric" in row["clause"])
        self.assertEqual(c["section"], "optional")
        self.assertFalse(c["mandatory"])
        j = job(desc)
        self.assertEqual(normalize_job_eligibility(proposed(["Microsoft Fabric"]), profile(), j).decision, "eligible")

    def test_domo_not_required_does_not_exclude_or_cap(self):
        j = job("<h2>Requirements</h2><p>Domo is helpful but not required.</p>")
        self.assertEqual(normalize_job_eligibility(proposed(["Domo"]), profile(), j).decision, "eligible")
        self.assertGreater(score(profile(), j, ["Domo"]).overall_score, 49)

    def test_separate_required_java_not_rescued_by_preferred_python(self):
        j = job("<h2>Requirements</h2><p>Java required.</p><h2>Preferred qualifications</h2><p>Python experience.</p>")
        self.assertEqual(normalize_job_eligibility(proposed(["Java"]), profile(), j).decision, "exclude")
        self.assertEqual(score(profile(), j, ["Java"]).overall_score, 49)

    def test_veeva_complete_fixture_normalizes_false_exclusion(self):
        j = job(
            "<h2>What You'll Do</h2><p>Work with JVM-based languages or Python on Spark-based data pipelines.</p>"
            "<p>Operate ML models in close cooperation with our data science team.</p>"
            "<h2>Requirements</h2><p>Expert skills in Java or Python</p>"
            "<p>Experience with Apache Spark or PySpark</p>"
            "<p>Experience writing software for the cloud (AWS or GCP)</p>"
            "<h2>Nice to have</h2><p>Experience with operating machine learning models (e.g., MLFlow)</p>",
            "Data Engineer (remote)",
        )
        raw = proposed(["Java", "Apache Spark", "PySpark", "Operating ML models"])
        self.assertEqual(evaluate_job_admission(j, profile()).admission_decision, "include")
        with patch("src.ai.job_eligibility.get_gemini_client") as client:
            normalized = normalize_job_eligibility(raw, profile(), j)
            client.assert_not_called()
        self.assertEqual(raw.decision, "exclude")
        self.assertEqual(raw.critical_skill_gaps, ["Java", "Apache Spark", "PySpark", "Operating ML models"])
        self.assertEqual(normalized.decision, "eligible", normalized)
        self.assertEqual(normalized.critical_skill_gaps, [])
        self.assertTrue(any("Python" in evidence for evidence in normalized.matched_resume_signals))
        self.assertTrue(any("Spark" in evidence for evidence in normalized.matched_resume_signals))
        self.assertEqual(central_specialization_gaps(j["description"], j["title"], profile()), [])
        self.assertEqual(score(profile(), j, raw.critical_skill_gaps).overall_score, 85)

    def test_essential_candidate_experience_is_mandatory_without_heading(self):
        clause = (
            "Deep experience with Databricks, Python, dbt, and Microsoft Fabric, "
            "along with strong fluency in healthcare data and compliance standards, is essential."
        )
        c = description_requirement_clauses(clause)[0]
        self.assertTrue(c["mandatory"], c)
        j = job(f"<p>{clause}</p>")
        self.assertEqual(normalize_job_eligibility(proposed(["Microsoft Fabric"]), profile(), j).decision, "exclude")
        self.assertEqual(score(profile(), j, ["Microsoft Fabric"]).overall_score, 49)

    def test_essential_business_slogan_is_not_mandatory(self):
        clause = "Our mission is essential to the communities we serve."
        c = description_requirement_clauses(clause)[0]
        self.assertFalse(c["mandatory"], c)
        self.assertFalse(validate_hard_gap_clause("community mission", clause, profile())["keep_hard_gap"])

    def check_personal(self, clause, kind, facts=None, expected="UNKNOWN"):
        p = profile(personal_eligibility=facts or CandidateEligibilityFacts())
        j = job(f"<h2>Requirements</h2><p>{clause}</p>")
        evidence = personal_requirement_evidence(clause, j["description"], p)
        self.assertEqual(evidence["kind"], kind)
        self.assertEqual(evidence["state"], expected, evidence)
        direct = validate_hard_gap_clause(clause, j["description"], p)
        self.assertEqual(direct["keep_hard_gap"], expected == "DOES_NOT_MEET", direct)
        normalized = normalize_job_eligibility(proposed([clause]), p, j)
        match = score(p, j, [clause])
        if expected == "DOES_NOT_MEET":
            self.assertEqual(normalized.decision, "exclude", normalized)
            self.assertEqual(match.overall_score, 49)
        elif expected == "UNKNOWN":
            self.assertEqual(normalized.decision, "eligible", normalized)
            self.assertEqual(normalized.critical_skill_gaps, [])
            self.assertTrue(any("UNKNOWN" in r for r in normalized.missing_or_mismatched_signals))
            self.assertGreater(match.overall_score, 49)
            self.assertEqual(match.recommendation, "review")
            self.assertTrue(any("UNKNOWN" in r for r in match.analysis.risk_factors))
        else:
            self.assertEqual(normalized.decision, "eligible", normalized)
            self.assertEqual(normalized.critical_skill_gaps, [])
            self.assertGreater(match.overall_score, 49)
        return evidence

    def test_unknown_work_authorization_is_not_failure(self):
        self.check_personal("US work authorization is required", "work_authorization")

    def test_explicit_missing_us_authorization_is_failure(self):
        self.check_personal("US work authorization is required", "work_authorization", CandidateEligibilityFacts(authorized_work_countries=[]), "DOES_NOT_MEET")

    def test_explicit_us_authorization_meets_condition(self):
        self.check_personal("US work authorization is required", "work_authorization", CandidateEligibilityFacts(authorized_work_countries=["US"]), "MEETS")

    def test_unknown_citizenship_is_not_failure(self):
        self.check_personal("US citizenship is mandatory", "citizenship")

    def test_explicit_non_us_citizenship_is_failure_when_only_us_allowed(self):
        self.check_personal("US citizenship is mandatory", "citizenship", CandidateEligibilityFacts(citizenship_country_codes=["BR"]), "DOES_NOT_MEET")

    def test_unknown_active_clearance_is_not_failure(self):
        self.check_personal("Must hold an active Secret security clearance", "clearance")

    def test_explicit_no_current_clearance_is_failure_if_active_required(self):
        self.check_personal("Must hold an active Secret security clearance", "clearance", CandidateEligibilityFacts(security_clearances=[]), "DOES_NOT_MEET")

    def test_no_current_clearance_does_not_prove_cannot_obtain(self):
        self.check_personal("Must be able to obtain Secret security clearance", "clearance", CandidateEligibilityFacts(security_clearances=[]), "UNKNOWN")

    def test_us_abbreviation_preserves_single_citizenship_clause(self):
        clauses = description_requirement_clauses("THIS POSITION WILL REQUIRE U.S. CITIZENSHIP.")
        self.assertEqual(len(clauses), 1, clauses)
        self.assertIn("CITIZENSHIP", clauses[0]["clause"])

    def test_central_kubernetes_specialization_excludes_without_model_gap(self):
        j = job("<p>Design production data services on Kubernetes.</p><p>Operate Kubernetes streaming systems and maintain their reliability.</p>")
        normalized = normalize_job_eligibility(proposed([]), profile(), j)
        self.assertEqual(normalized.decision, "exclude", normalized)
        self.assertTrue(any("Kubernetes" in gap for gap in normalized.critical_skill_gaps))

    def test_central_tm1_platform_excludes(self):
        j = job("<p>The primary focus is to design and develop IBM Planning Analytics TM1 models.</p><p>Build TM1 financial planning applications and oversee TM1 server performance.</p>")
        normalized = normalize_job_eligibility(proposed([]), profile(), j)
        self.assertEqual(normalized.decision, "exclude", normalized)
        self.assertTrue(any("TM1" in gap for gap in normalized.critical_skill_gaps))

    def test_central_geospatial_raster_specialization_excludes(self):
        j = job("<p>Design and build geospatial raster pipelines.</p><p>Develop raster satellite processing and coordinate reference transformations.</p>", "Geospatial Data Engineer")
        normalized = normalize_job_eligibility(proposed([]), profile(), j)
        self.assertEqual(normalized.decision, "exclude", normalized)
        self.assertTrue(any("geospatial" in gap for gap in normalized.critical_skill_gaps))

    def test_central_adf_integration_excludes(self):
        j = job("<p>Lead enterprise integration delivery using Azure Data Factory.</p><p>Design integration services with Azure Data Factory and Azure Integration Services.</p>")
        normalized = normalize_job_eligibility(proposed([]), profile(), j)
        self.assertEqual(normalized.decision, "exclude", normalized)
        self.assertTrue(any("Azure Data Factory" in gap for gap in normalized.critical_skill_gaps))

    def test_central_market_data_backend_excludes(self):
        j = job("<p>Build low-latency market-data feed handlers and venue connectivity.</p><p>Design low-latency market-data distribution systems for institutional trading.</p>", "Senior Market Data Engineer")
        normalized = normalize_job_eligibility(proposed([]), profile(), j)
        self.assertEqual(normalized.decision, "exclude", normalized)
        self.assertTrue(any("market-data" in gap for gap in normalized.critical_skill_gaps))

    def test_full_jd_coinbase_style_admission_has_specific_explanation(self):
        description = (
            "<h2>Responsibilities</h2>"
            "<p>Build low-latency market-data feed handlers and venue connectivity.</p>"
            "<p>Design low-latency market-data distribution systems for institutional trading.</p>"
            "<h2>Required Skills and Experience</h2>"
            "<p>Proficiency in low-latency Java or C++.</p>"
            "<p>Hands-on market-data protocol experience with FIX, ITCH or OUCH.</p>"
            "<p>Institutional trading systems demand dependable feed capture, normalization, "
            "distribution, operational response, and collaboration with exchange connectivity "
            "teams. Engineers own quality and execution across architecture, development, "
            "instrumentation, resilience, release planning, incident analysis, and continuous "
            "reliability improvement for high-throughput financial transactions. Partners rely "
            "on accurate consistent operational information and timely market communication "
            "through carefully maintained exchange feeds and robust trading services.</p>"
        )
        j = {"record_key": "synthetic-market", "title": "Senior Market Data Engineer", "description": description}
        self.assertTrue(resume_matcher.get_description_information(j)[2])
        admission = evaluate_job_admission(j, profile())
        self.assertEqual(admission.admission_decision, "exclude", admission)
        self.assertIn("complete JD", admission.admission_reason)
        self.assertIn("market-data", admission.admission_reason)
        self.assertNotIn("title family alone", admission.admission_reason)
        normalized = normalize_job_eligibility(proposed(["Java"]), profile(), j)
        self.assertEqual(normalized.decision, "exclude")
        self.assertNotIn("title family alone", normalized.reason)

    def test_explicit_mandatory_qlik_remains_cap_and_exclusion(self):
        j = job("<h2>Requirements</h2><p>3 years using QLIK Advana</p>")
        self.assertEqual(normalize_job_eligibility(proposed(["QLIK Advana"]), profile(), j).decision, "exclude")
        self.assertEqual(score(profile(), j, ["QLIK Advana"]).overall_score, 49)

    def test_explicit_mandatory_salesforce_remains_cap_and_exclusion(self):
        j = job("<p>Expert knowledge of Salesforce data model is required.</p>")
        self.assertEqual(normalize_job_eligibility(proposed(["Salesforce data model"]), profile(), j).decision, "exclude")
        self.assertEqual(score(profile(), j, ["Salesforce data model"]).overall_score, 49)


if __name__ == "__main__":
    unittest.main()
