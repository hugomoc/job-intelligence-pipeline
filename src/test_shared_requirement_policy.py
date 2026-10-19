"""Portable offline invariants shared by fit scoring and job eligibility."""

import unittest
from unittest.mock import patch

from src.ai import resume_matcher
from src.ai.job_eligibility import JobEligibilityAnalysis, normalize_job_eligibility
from src.ai.requirement_policy import (
    central_specialization_gaps,
    description_requirement_clauses,
    explicit_production_ai_requirements,
    personal_requirement_evidence,
    validate_hard_gap_clause,
    required_platform_tenure_gaps,
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

    def test_john_burns_explicit_production_dbt_minimum(self):
        """Unrelated data-engineering OR must not waive a separate dbt minimum."""
        candidate = profile(
            production_skills=["SQL Server", "Snowflake", "Python", "LookML"],
            project_skills=["dbt Core portfolio transformation models"],
            tools_platforms=["dbt", "Airflow", "Snowflake"],
        )
        posting = job(
            "<h2>What You'll Do</h2>"
            "<p>Build and maintain dbt models across staging, intermediate, "
            "and mart layers. Migrate T-SQL views into tested dbt models.</p>"
            "<h2>What You Bring</h2>"
            "<p>3+ years in data engineering or analytics engineering, "
            "including at least 2 years building production dbt projects.</p>"
            "<p>Strong SQL and dimensional modeling experience.</p>"
            "<h2>Nice to have</h2>"
            "<p>Azure data services and SQL Server.</p>",
            "Data Engineer",
        )
        clauses = description_requirement_clauses(posting["description"])
        self.assertTrue(any(
            x["mandatory"] and "2 years building production dbt" in x["clause"]
            for x in clauses
        ))
        missing, uncertainties = required_platform_tenure_gaps(
            posting["description"], candidate,
        )
        self.assertEqual(len(missing), 1, (missing, uncertainties))
        self.assertIn("2+ years of professional dbt experience", missing[0])
        self.assertEqual(uncertainties, [])
        scored = score(candidate, posting, [])
        self.assertEqual(scored.overall_score, 49)
        self.assertEqual(scored.recommendation, "skip")
        self.assertTrue(any("2+ years of professional dbt" in x
                            for x in scored.analysis.hard_requirements_missing))
        screened = normalize_job_eligibility(proposed([]), candidate, posting)
        self.assertEqual(screened.decision, "exclude")
        self.assertTrue(any("2+ years of professional dbt" in x
                            for x in screened.critical_skill_gaps))

    def test_nearby_years_do_not_become_platform_tenure(self):
        candidate = profile(
            production_skills=["SQL", "Python"],
            project_skills=["dbt portfolio project"],
        )
        for clause in (
            "3+ years in data engineering or analytics engineering, "
            "including some exposure to dbt projects.",
            "3+ years of data engineering, including experience with dbt.",
        ):
            posting = job(
                "<h2>What You Bring</h2><p>" + clause + "</p>"
            )
            self.assertEqual(
                required_platform_tenure_gaps(posting["description"], candidate),
                ([], []),
            )

    def test_local_tool_alternatives_are_not_individually_mandatory(self):
        candidate = profile(production_skills=["Python", "SQL"])
        for clause in (
            "2+ years of production dbt or equivalent transformation technology.",
            "3+ years working with dbt or Airflow.",
            "3+ years of experience in Snowflake or BigQuery.",
        ):
            posting = job("<h2>Required Qualifications</h2><p>" + clause + "</p>")
            self.assertEqual(
                required_platform_tenure_gaps(posting["description"], candidate),
                ([], []),
            )

    def test_dated_production_dbt_meets_local_requirement(self):
        candidate = profile(
            production_skills=["2+ years building production dbt Core models"],
            project_skills=[],
        )
        posting = job(
            "<h2>What You Bring</h2>"
            "<p>3+ years in data engineering or analytics engineering, "
            "including at least 2 years building production dbt projects.</p>"
        )
        self.assertEqual(
            required_platform_tenure_gaps(posting["description"], candidate),
            ([], []),
        )
        self.assertEqual(score(candidate, posting, []).recommendation, "apply")
        self.assertEqual(
            normalize_job_eligibility(proposed([]), candidate, posting).decision,
            "eligible",
        )

    def test_undated_professional_dbt_is_review_not_assumed_two_years(self):
        candidate = profile(production_skills=["Production dbt Core models"])
        posting = job(
            "<h2>What You Bring</h2>"
            "<p>3+ years in data engineering or analytics engineering, "
            "including at least 2 years building production dbt projects.</p>"
        )
        missing, unknown = required_platform_tenure_gaps(
            posting["description"], candidate,
        )
        self.assertEqual(missing, [])
        self.assertEqual(len(unknown), 1)
        self.assertEqual(score(candidate, posting, []).recommendation, "review")

    def test_metlife_dbt_cloud_is_mandatory_professional_tenure(self):
        p = profile(
            project_skills=["dbt Core", "dbt transformations"],
            tools_platforms=["dbt"],
            production_skills=["SQL", "Python", "AWS Redshift"],
        )
        j = job(
            "<h2>Required Qualifications</h2>"
            "<p>3+ years of experience in data engineering.</p>"
            "<p>3+ years of hands-on experience using DBT Cloud.</p>"
            "<p>1+ years of experience with Airflow, Prefect, or Dagster.</p>",
            "Data Analytics Engineer",
        )
        missing, uncertain = required_platform_tenure_gaps(j["description"], p)
        self.assertEqual(len(missing), 1, (missing, uncertain))
        self.assertIn("dbt Cloud", missing[0])
        self.assertEqual(uncertain, [])
        match = score(p, j, [])
        self.assertEqual(match.overall_score, 49)
        self.assertEqual(match.recommendation, "skip")
        self.assertTrue(any("dbt Cloud" in x for x in match.analysis.hard_requirements_missing))
        self.assertEqual(normalize_job_eligibility(proposed([]), p, j).decision, "exclude")

    def test_dbt_cloud_prod_without_tenure_is_review_not_false_proof(self):
        p = profile(production_skills=["SQL", "Python", "dbt Cloud"])
        j = job(
            "<h2>Required Qualifications</h2>"
            "<p>3+ years of hands-on experience using dbt Cloud.</p>"
        )
        missing, uncertain = required_platform_tenure_gaps(j["description"], p)
        self.assertEqual(missing, [])
        self.assertEqual(len(uncertain), 1)
        match = score(p, j, [])
        self.assertEqual(match.overall_score, 74)
        self.assertEqual(match.recommendation, "review")
        self.assertTrue(any("dbt Cloud" in x for x in match.analysis.risk_factors))

    def test_explicit_professional_tenure_satisfies_dbt_cloud(self):
        p = profile(production_skills=["5+ years of production dbt Cloud development"])
        j = job(
            "<h2>Required Qualifications</h2>"
            "<p>3+ years of hands-on experience using dbt Cloud.</p>"
        )
        self.assertEqual(required_platform_tenure_gaps(j["description"], p), ([], []))
        match = score(p, j, [])
        self.assertEqual(match.overall_score, 85)
        self.assertEqual(match.recommendation, "apply")

    def test_preferred_platform_and_open_alternatives_are_not_hard_tenure(self):
        p = profile(production_skills=["SQL", "Python"])
        for clause in (
            "<h2>Preferred Qualifications</h2><p>3+ years of dbt Cloud experience.</p>",
            "<h2>Required Qualifications</h2><p>3+ years experience with Snowflake or BigQuery.</p>",
            "<h2>Required Qualifications</h2><p>3+ years data engineering experience.</p>",
        ):
            j = job(clause)
            self.assertEqual(required_platform_tenure_gaps(j["description"], p), ([], []))
            self.assertEqual(score(p, j, []).overall_score, 85)

    def test_project_only_dbt_cloud_does_not_prove_production_years(self):
        p = profile(
            project_skills=["dbt Cloud portfolio project"],
            tools_platforms=["dbt Cloud"],
            production_skills=["SQL"],
        )
        j = job(
            "<h2>Required Qualifications</h2>"
            "<p>3+ years of hands-on experience using dbt Cloud.</p>"
        )
        missing, uncertain = required_platform_tenure_gaps(j["description"], p)
        self.assertEqual(len(missing), 1)
        self.assertIn("dbt Cloud", missing[0])
        self.assertEqual(uncertain, [])
        self.assertEqual(score(p, j, []).recommendation, "skip")

    def test_modmed_cloud_preference_does_not_make_databricks_optional(self):
        j = job(
            "<h2>What You'll Do</h2>"
            "<p>Manage and optimize our Databricks environment.</p>"
            "<h2>What You Bring</h2>"
            "<p>Fluency in SQL, dbt, and ideally Python.</p>"
            "<p>Experience working with Databricks (on AWS preferred).</p>",
            "Senior Data Analytics Engineer",
        )
        clause = next(
            row for row in description_requirement_clauses(j["description"])
            if "Experience working with Databricks" in row["clause"]
        )
        self.assertEqual(clause["section"], "required")
        self.assertTrue(clause["mandatory"])
        self.assertFalse(clause["optional"])
        self.assertTrue(
            validate_hard_gap_clause("Experience with Databricks", j["description"], profile())["keep_hard_gap"]
        )
        match = score(profile(), j, [])
        self.assertEqual(match.overall_score, 49)
        self.assertEqual(match.recommendation, "skip")
        self.assertTrue(
            any("Databricks" in gap for gap in match.analysis.hard_requirements_missing)
        )
        self.assertEqual(normalize_job_eligibility(proposed([]), profile(), j).decision, "exclude")

    def test_databricks_preferred_itself_stays_optional(self):
        j = job("<h2>What You Bring</h2><p>Experience with Databricks (preferred).</p>")
        clause = next(
            row for row in description_requirement_clauses(j["description"])
            if "Experience with Databricks" in row["clause"]
        )
        self.assertEqual(clause["section"], "required")
        self.assertTrue(clause["optional"])
        self.assertFalse(clause["mandatory"])
        self.assertFalse(
            validate_hard_gap_clause("Experience with Databricks", j["description"], profile())["keep_hard_gap"]
        )
        self.assertEqual(score(profile(), j, []).overall_score, 85)

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

    def test_piedmont_preferred_domain_and_degree_are_not_experience_gaps(self):
        p = profile(industries=["e-commerce"])
        j = job(
            "<h2>Requirements</h2>"
            "<p>1 year of analysis work including Data Visualization and or Data Modeling, "
            "preferably in a multi-entity healthcare provider environment.</p>"
            "<p>Bachelor's Degree in Business, Finance, Accounting, "
            "Information Technology, or closely-related field Required.</p>",
            "BI Developer",
        )
        gaps = ["Experience in a multi-entity healthcare provider environment",
                "Major gap: ledger or accounting data-domain expertise"]
        for gap in gaps:
            with self.subTest(gap=gap):
                self.assertFalse(validate_hard_gap_clause(
                    gap, j["description"], p)["keep_hard_gap"])
        self.assertEqual(score(p, j, gaps).analysis.hard_requirements_missing, [])

    def test_construction_accounting_and_healthcare_required_still_hard(self):
        p = profile(industries=["e-commerce"])
        for gap, clause in (
            ("Experience in a healthcare setting", "Experience in a healthcare setting."),
            ("Major gap: ledger or accounting data-domain expertise",
             "Working knowledge of construction financial reporting: "
             "percentage-of-completion accounting, work-in-progress schedules, "
             "over and under billings, backlog and margin fade."),
        ):
            with self.subTest(gap=gap):
                j = job("<h2>Requirements</h2><p>" + clause + "</p>")
                self.assertTrue(validate_hard_gap_clause(
                    gap, j["description"], p)["keep_hard_gap"])
                self.assertEqual(score(p, j, [gap]).recommendation, "skip")

    def test_generic_bi_years_cannot_prove_tableau_tenure(self):
        p = profile(tools_platforms=["Looker", "LookML"])
        j = job("<h2>Requirements</h2>"
                "<p>8+ years of business intelligence development experience.</p>"
                "<p>Build Tableau dashboards and provide reporting solutions.</p>",
                "Senior Business Intelligence Developer")
        gap = ("Major gap: 8+ years of professional Tableau experience "
               "(not demonstrated in professional experience)")
        self.assertEqual(required_platform_tenure_gaps(j["description"], p), ([], []))
        self.assertFalse(validate_hard_gap_clause(
            gap, j["description"], p)["keep_hard_gap"])
        self.assertNotIn(gap, score(p, j, [gap]).analysis.hard_requirements_missing)

    def test_explicit_dbt_cloud_tenure_still_requires_production(self):
        p = profile(production_skills=["SQL", "Python"],
                    project_skills=["Portfolio dbt Core models"])
        j = job("<h2>Requirements</h2>"
                "<p>3+ years of hands-on experience using DBT Cloud.</p>")
        gap = ("Major gap: 3+ years of professional dbt Cloud experience "
               "(not demonstrated in professional experience)")
        self.assertTrue(validate_hard_gap_clause(
            gap, j["description"], p)["keep_hard_gap"])
        self.assertEqual(score(p, j, [gap]).recommendation, "skip")

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

    def test_job_responsibilities_heading_preserves_delivery_provenance(self):
        desc = (
            "<h2>Job Responsibilities</h2>"
            "<p>Develop extractions, transformation routines, and user exits "
            "using ABAP development programming language.</p>"
        )
        clause = next(
            row for row in description_requirement_clauses(desc)
            if "ABAP" in row["clause"]
        )
        self.assertEqual(clause["section"], "responsibilities")
        self.assertFalse(clause["mandatory"])
        self.assertFalse(clause["optional"])

    def test_abap_central_responsibility_prevents_false_apply(self):
        p = profile()
        j = job(
            "<h2>Job Responsibilities</h2>"
            "<p>Develop extractions, transformation routines, and user exits "
            "using ABAP development programming language.</p>",
            "Business Intelligence Developer II",
        )
        direct = validate_hard_gap_clause(
            "ABAP development experience",
            j["description"],
            p,
        )
        self.assertFalse(direct["keep_hard_gap"], direct)
        self.assertEqual(
            direct["reason"],
            "central responsibility unsatisfied",
            direct,
        )

        match = score(p, j, ["ABAP development experience"])
        self.assertEqual(match.recommendation, "review")
        self.assertLessEqual(match.overall_score, 69)
        self.assertEqual(match.analysis.hard_requirements_missing, [])
        self.assertTrue(any(
            risk.startswith("Major central responsibility gap:")
            and "ABAP" in risk
            for risk in match.analysis.risk_factors
        ))

    def test_central_responsibility_is_satisfied_when_tool_is_demonstrated(self):
        p = profile(
            production_skills=[
                "SQL",
                "Python",
                "Data pipelines",
                "ABAP development",
            ]
        )
        j = job(
            "<h2>Job Responsibilities</h2>"
            "<p>Develop extractions, transformation routines, and user exits "
            "using ABAP development programming language.</p>",
            "Business Intelligence Developer II",
        )
        match = score(p, j, ["ABAP development experience"])
        self.assertEqual(match.recommendation, "apply")
        self.assertEqual(match.overall_score, 85)
        self.assertFalse(any(
            risk.startswith("Major central responsibility gap:")
            for risk in match.analysis.risk_factors
        ))

    def test_optional_tool_mention_does_not_become_central_responsibility(self):
        p = profile()
        j = job(
            "<h2>Nice to have</h2>"
            "<p>ABAP development experience preferred.</p>",
            "Business Intelligence Developer II",
        )
        direct = validate_hard_gap_clause(
            "ABAP development experience",
            j["description"],
            p,
        )
        self.assertNotEqual(
            direct["reason"],
            "central responsibility unsatisfied",
            direct,
        )
        match = score(p, j, ["ABAP development experience"])
        self.assertEqual(match.recommendation, "apply")
        self.assertGreater(match.overall_score, 69)
        self.assertFalse(any(
            risk.startswith("Major central responsibility gap:")
            for risk in match.analysis.risk_factors
        ))

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

    def test_generic_support_agents_do_not_require_production_rag(self):
        p = profile()
        j = job(
            "<h2>Required Qualifications</h2>"
            "<p>Experience supporting customer service agents and building "
            "data integrations for CRM tools.</p>"
            "<p>Strong SQL, Python, and operational reporting skills required.</p>"
        )
        gap = "production RAG, agentic workflows, and AI tool integrations"
        self.assertFalse(validate_hard_gap_clause(gap, j["description"], p)["keep_hard_gap"])
        self.assertEqual(score(p, j, []).recommendation, "apply")
        self.assertEqual(normalize_job_eligibility(proposed([gap]), p, j).decision, "eligible")

    def test_optional_ai_automation_is_not_mandatory_production_rag(self):
        p = profile()
        j = job(
            "<h2>Required Qualifications</h2><p>Strong Python and SQL skills.</p>"
            "<h2>Preferred Qualifications</h2>"
            "<p>Experience building production RAG systems or agentic AI is a plus.</p>"
        )
        gap = "production RAG, agentic workflows, and AI tool integrations"
        self.assertFalse(validate_hard_gap_clause(gap, j["description"], p)["keep_hard_gap"])
        self.assertEqual(score(p, j, []).recommendation, "apply")
        self.assertEqual(normalize_job_eligibility(proposed([gap]), p, j).decision, "eligible")

    def test_audit_ai_alternatives_and_tool_usage_do_not_require_production_rag(self):
        """The Oct 15 audit incorrectly rejected four employers (five rows)."""
        p = profile(
            production_skills=["SQL", "Python", "LookML semantic layer models"],
            bi_analytics_skills=["Looker semantic models and governed metrics"],
            project_skills=["AI-assisted personal analytics demo"],
        )
        clauses = {
            "ElevenLabs / UMAI": (
                "Experience serving data to LLM-based agents or building "
                "semantic / metrics layers consumed by AI tools."
            ),
            "Owner.com": (
                "Experience building a semantic layer, or data infrastructure "
                "for AI/ML and LLM retrieval (RAG, feature stores, dataset compliance)"
            ),
            "WorkOS": (
                "Comfortable using LLMs and coding agents as part of your "
                "day-to-day development workflow, with the judgment to "
                "validate their output and scale it into shared processes, "
                "and embracing AI and automation to scale platform work "
                "and accelerate your teammates"
            ),
        }
        gap = "production RAG, agentic workflows, and AI tool integrations"
        for company, clause in clauses.items():
            with self.subTest(company=company):
                j = job("<h2>Required Qualifications</h2><p>" + clause + "</p>")
                self.assertEqual(explicit_production_ai_requirements(j["description"]), [])
                self.assertFalse(
                    validate_hard_gap_clause(gap, j["description"], p)["keep_hard_gap"]
                )
                self.assertGreater(score(p, j, []).overall_score, 49)
                self.assertEqual(
                    normalize_job_eligibility(proposed([gap]), p, j).decision,
                    "eligible",
                )

    def test_audit_databricks_lists_optional_and_knowledge_are_not_production_requirements(self):
        """Incidental platform references must not become hard production gaps."""
        p = profile(
            production_skills=["SQL", "Python", "Snowflake", "Spark"],
            tools_platforms=["Spark", "dbt"],
        )
        cases = {
            "ScolerTec": (
                "<h2>Requirements</h2>"
                "<p>Senior BI Architect – Databricks, AWS & Self-Service Analytics</p>"
                "<p>Exposure to Databricks and/or AWS analytics platforms preferred.</p>"
            ),
            "BECU": (
                "<h2>Requirements</h2>"
                "<p>Demonstrated experience using Modern data platforms "
                "( Microsoft Fabric, Azure Databricks, Big Query, Snowflake).</p>"
            ),
            "Centene": (
                "<p>Enhanced understanding of cloud analytics components "
                "including AWS, Databricks, Data Mesh, Delta Lake, and Data "
                "Lakehouse environments required.</p>"
            ),
            "Protective Life": (
                "<h2>Requirements</h2>"
                "<p>Hands-on experience with Databricks or a comparable "
                "Spark-based lakehouse, including Delta Lake tables, MERGE, "
                "and incremental load patterns</p>"
            ),
        }
        for company, clause in cases.items():
            with self.subTest(company=company):
                j = job(clause, "Data Engineer")
                self.assertFalse(validate_hard_gap_clause(
                    "Databricks production experience", j["description"], p
                )["keep_hard_gap"])
                self.assertGreater(score(p, j, []).overall_score, 49)
                self.assertEqual(
                    normalize_job_eligibility(
                        proposed(["Databricks production experience"]), p, j
                    ).decision,
                    "eligible",
                )

    def test_audit_supporting_ml_is_not_owning_production_model_deployment(self):
        p = profile(ai_ml_experience=["Machine Learning Models"])
        j = job(
            "<h2>Required Qualifications</h2>"
            "<p>Familiarity with basic machine learning concepts, data "
            "preparation techniques, and model evaluation processes.</p>"
            "<h2>Responsibilities</h2>"
            "<p>Work alongside data scientists and engineers to facilitate "
            "data access and support ML model deployment and maintenance.</p>"
        )
        gap = "production machine-learning model deployment"
        self.assertFalse(
            validate_hard_gap_clause(gap, j["description"], p)["keep_hard_gap"]
        )
        self.assertGreater(score(p, j, []).overall_score, 49)
        self.assertEqual(
            normalize_job_eligibility(proposed([gap]), p, j).decision, "eligible"
        )

    def test_explicit_production_ml_deployment_and_databricks_still_exclude(self):
        p = profile(production_skills=["SQL", "Python"],
                    tools_platforms=["dbt", "Spark"])
        cases = [
            ("production machine-learning model deployment",
             "Hands-on experience deploying machine learning models to production."),
            ("Databricks production experience",
             "Deep experience building production pipelines in Databricks required."),
        ]
        for gap, clause in cases:
            with self.subTest(gap=gap):
                j = job("<h2>Required Qualifications</h2><p>" + clause + "</p>")
                self.assertTrue(
                    validate_hard_gap_clause(gap, j["description"], p)["keep_hard_gap"]
                )
                self.assertEqual(score(p, j, []).recommendation, "skip")
                self.assertEqual(
                    normalize_job_eligibility(proposed([gap]), p, j).decision,
                    "exclude",
                )

    def test_production_project_tenure_not_waived_by_undated_tool_inventory(self):
        """Regression: a generic dbt inventory must not hide John Burns' JD."""
        p = profile(
            production_skills=["SQL Server", "Python", "Snowflake", "LookML"],
            tools_platforms=["dbt", "Looker", "Apache Spark"],
            project_skills=["Personal Python API demo"],
        )
        j = job(
            "<h2>What You Bring</h2><p>"
            "3+ years in data engineering or analytics engineering, "
            "including at least 2 years building production dbt projects.</p>"
        )
        missing, uncertain = required_platform_tenure_gaps(j["description"], p)
        self.assertEqual(len(missing), 1, (missing, uncertain))
        self.assertIn("2+ years of professional dbt", missing[0])
        self.assertEqual(uncertain, [])
        match = score(p, j, [])
        self.assertEqual(match.overall_score, 49)
        self.assertEqual(match.recommendation, "skip")
        self.assertEqual(
            normalize_job_eligibility(proposed([]), p, j).decision,
            "exclude",
        )
        # A credible, though undated, actual professional dbt delivery
        # assertion must still be REVIEW rather than a false hard rejection.
        p = p.model_copy(update={
            "production_skills": p.production_skills + [
                "Built production dbt project models for business reporting"
            ],
        })
        missing, uncertain = required_platform_tenure_gaps(j["description"], p)
        self.assertEqual(missing, [])
        self.assertEqual(len(uncertain), 1)
        self.assertEqual(score(p, j, []).recommendation, "review")

    def test_undated_nonproject_platform_inventory_is_review_not_false_skip(self):
        for tool, clause in (
            ("LookML", "3+ years of professional Looker development experience."),
            ("Apache Spark", "2+ years building production Apache Spark pipelines."),
        ):
            with self.subTest(tool=tool):
                p = profile(
                    production_skills=["SQL", "Python"],
                    tools_platforms=[tool],
                    project_skills=["dbt Core portfolio models"],
                )
                j = job("<h2>Required Qualifications</h2><p>" + clause + "</p>")
                missing, uncertain = required_platform_tenure_gaps(
                    j["description"], p
                )
                self.assertEqual(missing, [])
                self.assertEqual(len(uncertain), 1)
                self.assertEqual(score(p, j, []).recommendation, "review")
        # Explicit dated LookML production experience DOES prove Looker tenure.
        p = profile(production_skills=["4+ years of production LookML modeling"],
                    tools_platforms=["Looker"], project_skills=[])
        j = job("<h2>Requirements</h2>"
                "<p>3+ years of professional Looker development experience.</p>")
        self.assertEqual(required_platform_tenure_gaps(j["description"], p),
                         ([], []))
        # An explicitly project-only platform still cannot satisfy tenure.
        p = profile(production_skills=["SQL"], tools_platforms=["dbt"],
                    project_skills=["Personal dbt portfolio"])
        j = job("<h2>Requirements</h2>"
                "<p>2+ years building production dbt projects.</p>")
        missing, uncertain = required_platform_tenure_gaps(j["description"], p)
        self.assertEqual(len(missing), 1)
        self.assertEqual(uncertain, [])
        self.assertEqual(score(p, j, []).recommendation, "skip")

    def test_required_ai_production_ownership_remains_a_hard_gap(self):
        p = profile(project_skills=["Personal AI chatbot using RAG and agentic workflows"])
        j = job(
            "<h2>Required Qualifications</h2>"
            "<p>Hands-on experience deploying RAG and agentic AI systems to production.</p>"
            "<p>Strong SQL data engineering and infrastructure skills.</p>"
        )
        gap = "production RAG, agentic workflows, and AI tool integrations"
        self.assertTrue(validate_hard_gap_clause(gap, j["description"], p)["keep_hard_gap"])
        match = score(p, j, [])
        self.assertEqual(match.recommendation, "skip")
        self.assertEqual(match.overall_score, 49)
        self.assertEqual(normalize_job_eligibility(proposed([]), p, j).decision, "exclude")

    def test_real_production_ai_experience_satisfies_both_gates(self):
        p = profile(
            ai_ml_experience=[
                "Owned production RAG pipelines and deployed agentic LLM "
                "systems serving real users."
            ],
        )
        j = job(
            "<h2>Required Qualifications</h2>"
            "<p>Hands-on experience deploying production RAG and agentic AI systems.</p>"
        )
        self.assertEqual(
            normalize_job_eligibility(proposed([]), p, j).decision, "eligible"
        )
        self.assertEqual(score(p, j, []).recommendation, "apply")

    def test_ai_requirement_must_be_in_same_mandatory_clause(self):
        p = profile()
        j = job(
            "<h2>Responsibilities</h2>"
            "<p>Team members may explore LLM and AI agents.</p>"
            "<h2>Required Qualifications</h2>"
            "<p>Experience deploying reliable production SQL pipelines "
            "and building conventional Python integrations.</p>"
        )
        gap = "production RAG, agentic workflows, and AI tool integrations"
        self.assertFalse(validate_hard_gap_clause(gap, j["description"], p)["keep_hard_gap"])
        self.assertEqual(score(p, j, []).recommendation, "apply")

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
