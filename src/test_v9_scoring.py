"""Synthetic regressions for the shared v9 score/recommendation policy.

Each case mirrors a failure mode seen on the frozen Aurora/Netomi/Tesla/
CenterWell/Constructor reviews, using invented text only.
"""
import json
import tempfile
import unittest
from pathlib import Path

from evaluation.compare_v9 import compare, load_reference, render
from evaluation.run_v9 import build_prompt, evaluate, openai_strict_schema
from evaluation.v9_scoring import decide


def row(group, quote, component, logic="single", status="met", resume=None,
        context="employment", kind="mandatory", years=None, materiality="central",
        production=False):
    return dict(group_id=group, jd_quote=quote, component=component,
                logic=logic, kind=kind, status=status, resume_quote=resume,
                evidence_context=context if resume else "none",
                min_years=years, production_required=production,
                materiality=materiality if kind == "mandatory" else None,
                reason="Synthetic requirement")


def run(jd, resume, rows, score, **kwargs):
    return decide(dict(fit_score=score, requirements=rows), jd, resume, **kwargs)


SQL_JD = "Advanced SQL required."
SQL_RESUME = "Data Analyst, Acme (2017-2024): wrote SQL models daily."
SQL_ROW = row("sql", SQL_JD, "SQL", resume="wrote SQL models daily")


class V9DecisionTests(unittest.TestCase):
    def test_aurora_portfolio_dbt_cannot_reach_apply(self):
        jd = "2+ years with dbt and Looker in a production environment"
        resume = "Analyst (2018-2024): Looker 5 years professional. Portfolio: dbt project"
        rows = [
            row("combo", jd, "Looker", "all_of", resume="Looker 5 years professional",
                years=2, production=True),
            row("combo", jd, "dbt", "all_of", resume="dbt project", context="project",
                years=2, production=True),
        ]
        result = run(jd, resume, rows, 92)
        self.assertLessEqual(result["final_score"], 74)
        self.assertEqual(result["recommendation"], "HUMAN REVIEW")
        self.assertTrue(result["score_adjusted"])

    def test_aurora_honest_gap_is_low_priority(self):
        jd = "2+ years with dbt and Looker in a production environment"
        resume = "Analyst (2018-2024): Looker 5 years professional."
        rows = [
            row("combo", jd, "Looker", "all_of", resume="Looker 5 years professional", years=2),
            row("combo", jd, "dbt", "all_of", status="not_demonstrated", years=2),
        ]
        result = run(jd, resume, rows, 92)
        self.assertEqual(result["final_score"], 64)
        self.assertEqual(result["recommendation"], "LOW PRIORITY")

    def test_netomi_spark_is_not_kafka(self):
        jd = "Experience with Apache Kafka or RabbitMQ for asynchronous systems"
        resume = "Built Apache Spark production workflows"
        rows = [
            row("messaging", jd, "Kafka", "any_of", resume="Apache Spark production workflows"),
            row("messaging", jd, "RabbitMQ", "any_of", status="not_demonstrated"),
            SQL_ROW,
        ]
        result = run(jd + " " + SQL_JD, resume + " " + SQL_RESUME, rows, 90)
        self.assertNotEqual(result["recommendation"], "APPLY")
        self.assertLessEqual(result["final_score"], 74)

    def test_fabricated_resume_citation_never_counts(self):
        rows = [row("py", "Python required", "Python", resume="Python 7 years at Google")]
        result = run("Python required", "Excel reporting", rows, 95)
        self.assertEqual(result["unsupported_resume_citations"], 1)
        self.assertEqual(result["recommendation"], "HUMAN REVIEW")
        self.assertLessEqual(result["final_score"], 74)

    def test_score_contradicting_own_gap_is_capped(self):
        jd = "Python required. Kubernetes required."
        rows = [row("py", "Python required", "Python", resume="Python services"),
                row("k8s", "Kubernetes required", "Kubernetes", status="not_demonstrated")]
        result = run(jd, "Shipped Python services", rows, 92)
        self.assertEqual(result["final_score"], 64)
        self.assertEqual(result["recommendation"], "LOW PRIORITY")
        self.assertFalse(result["validation"]["valid"])

    def test_two_central_gaps_skip(self):
        jd = "Python required. Kubernetes required. Go required."
        rows = [row("py", "Python required", "Python", resume="Python services"),
                row("k8s", "Kubernetes required", "Kubernetes", status="not_demonstrated"),
                row("go", "Go required", "Go", status="not_demonstrated")]
        result = run(jd, "Shipped Python services", rows, 80)
        self.assertEqual(result["recommendation"], "SKIP")
        self.assertLessEqual(result["final_score"], 44)

    def test_tesla_equivalent_met_but_forecasting_unclear(self):
        jd = ("Visualization tools such as Power BI, Tableau, Excel, or similar. "
              "Demonstrated statistical methods particularly regarding budget forecasting.")
        resume = "Built Looker dashboards for business reporting"
        rows = [row("viz", "Visualization tools such as Power BI, Tableau, Excel, or similar",
                    "Looker", "open_equivalent", resume="Built Looker dashboards"),
                row("forecast", "Demonstrated statistical methods particularly regarding budget forecasting",
                    "budget forecasting", status="unclear")]
        result = run(jd, resume, rows, 93)
        self.assertEqual(result["final_score"], 84)
        self.assertEqual(result["recommendation"], "HUMAN REVIEW")

    def test_centerwell_missing_preferred_still_apply(self):
        jd = "Advanced SQL required. Databricks preferred."
        rows = [SQL_ROW, row("dbx", "Databricks preferred", "Databricks",
                             status="not_demonstrated", kind="preferred")]
        result = run(jd, SQL_RESUME, rows, 88)
        self.assertEqual(result["final_score"], 88)
        self.assertEqual(result["recommendation"], "APPLY")
        self.assertEqual(result["reasons"], [])

    def test_preferred_mislabelled_mandatory_is_not_a_gap(self):
        jd = "Advanced SQL required. Databricks preferred."
        rows = [SQL_ROW, row("dbx", "Databricks preferred", "Databricks", status="not_demonstrated")]
        result = run(jd, SQL_RESUME, rows, 88)
        self.assertEqual(result["central_mandatory"]["not_demonstrated"], 0)
        self.assertEqual(result["recommendation"], "HUMAN REVIEW")  # mislabel is an integrity issue

    def test_constructor_open_equivalent_apply(self):
        jd = "Modern analytics stacks such as dbt, Airflow, or similar"
        resume = "Engineer (2019-2024): Built production Airflow pipelines"
        rows = [row("modern", jd, "Airflow", "open_equivalent",
                    resume="Built production Airflow pipelines", production=True)]
        result = run(jd, resume, rows, 86)
        self.assertEqual(result["recommendation"], "APPLY")

    def test_tenure_with_employment_dates_is_met(self):
        jd = "3+ years of Python in production"
        resume = "Software Engineer, Acme, 2018-2024: Python 6 years in production services"
        rows = [row("py", jd, "Python", resume="Python 6 years in production services",
                    years=3, production=True)]
        result = run(jd, resume, rows, 85)
        self.assertEqual(result["validation"]["groups"][0]["status"], "met")
        self.assertEqual(result["recommendation"], "APPLY")

    def test_skills_list_cannot_prove_production(self):
        jd = "Production experience with Snowflake required"
        resume = "Skills: Snowflake, SQL"
        rows = [row("sf", jd, "Snowflake", resume="Skills: Snowflake", context="skills_list",
                    production=True)]
        result = run(jd, resume, rows, 85)
        self.assertEqual(result["validation"]["groups"][0]["status"], "partial")
        self.assertNotEqual(result["recommendation"], "APPLY")

    def test_under_scoring_is_flagged_not_skipped(self):
        result = run(SQL_JD, SQL_RESUME, [SQL_ROW], 35)
        self.assertTrue(result["under_scored"])
        self.assertEqual(result["recommendation"], "HUMAN REVIEW")
        self.assertEqual(result["final_score"], 35)  # never inflated, only surfaced

    def test_invented_requirement_does_not_lower_score(self):
        rows = [SQL_ROW, row("fake", "PhD in physics required", "PhD", status="not_demonstrated")]
        result = run(SQL_JD, SQL_RESUME, rows, 85)
        self.assertEqual(result["central_mandatory"]["not_demonstrated"], 0)
        self.assertEqual(result["recommendation"], "HUMAN REVIEW")

    def test_eligibility_blocker_and_unconfirmed_eligibility(self):
        jd = SQL_JD + " Active TS/SCI clearance required."
        rows = [SQL_ROW, row("clr", "Active TS/SCI clearance required.", "TS/SCI clearance",
                             status="unclear", kind="eligibility")]
        self.assertEqual(run(jd, SQL_RESUME, rows, 88)["recommendation"], "HUMAN REVIEW")
        self.assertEqual(run(jd, SQL_RESUME, rows, 88, eligibility_confirmed=True)["recommendation"], "APPLY")
        blocked = run(jd, SQL_RESUME, rows, 88, eligibility_blockers=["no clearance"])
        self.assertEqual(blocked["recommendation"], "SKIP")

    def test_no_mandatory_rows_needs_review(self):
        rows = [row("dbx", "Databricks preferred", "Databricks", status="not_demonstrated",
                    kind="preferred")]
        self.assertEqual(run("Databricks preferred", "", rows, 80)["recommendation"], "HUMAN REVIEW")


class V9ProviderParityTests(unittest.TestCase):
    def test_openai_strict_schema(self):
        schema = openai_strict_schema()
        text = json.dumps(schema)
        for banned in ('"default"', '"minLength"', '"title"'):
            self.assertNotIn(banned, text)

        def check(node):
            if isinstance(node, dict):
                if node.get("type") == "object" and "properties" in node:
                    self.assertFalse(node["additionalProperties"])
                    self.assertEqual(sorted(node["required"]), sorted(node["properties"]))
                for value in node.values():
                    check(value)
            elif isinstance(node, list):
                for value in node:
                    check(value)
        check(schema)
        self.assertIn("production_required", schema["$defs"]["Requirement"]["properties"])

    def test_both_providers_get_identical_prompt_and_policy(self):
        case = {"case_id": "c", "jd": SQL_JD, "resume": SQL_RESUME, "original_score": 97}
        payload = {"fit_score": 88, "requirements": [SQL_ROW]}
        out = {p: evaluate([case], live=True, max_calls=1, model="m", provider=p,
                           caller=lambda prompt, model: payload)[0]
               for p in ("gemini", "openai")}
        self.assertEqual(out["gemini"]["prompt_sha256"], out["openai"]["prompt_sha256"])
        self.assertEqual(out["gemini"]["recommendation"], out["openai"]["recommendation"])
        self.assertNotIn("97", build_prompt(case["jd"], case["resume"]))

    def test_unknown_provider_rejected(self):
        with self.assertRaises(ValueError):
            evaluate([], live=False, max_calls=0, model="m", provider="other")


class V9CompareTests(unittest.TestCase):
    def test_compare_ranks_accurate_provider_first(self):
        case = {"case_id": "aurora", "jd": SQL_JD, "resume": SQL_RESUME}
        good = {"fit_score": 86, "requirements": [SQL_ROW]}
        bad = {"fit_score": 30, "requirements": [SQL_ROW]}
        results = (
            evaluate([case], live=True, max_calls=1, model="g", provider="gemini",
                     caller=lambda p, m: good)
            + evaluate([case], live=True, max_calls=1, model="o", provider="openai",
                       caller=lambda p, m: bad)
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ref.json"
            path.write_text(json.dumps([{"case_id": "aurora", "recommendation": "APPLY",
                                         "score": 85, "central_mandatory": {"SQL": "met"}}]))
            reference = load_reference(path)
        report = compare(reference, results)
        self.assertEqual(report["ranking"][0], "gemini:g")
        gemini = report["providers"]["gemini:g"]
        self.assertEqual(gemini["recommendation_correct"], 1)
        self.assertEqual(gemini["requirement_status_agreement"], 1)
        self.assertEqual(report["providers"]["openai:o"]["too_pessimistic"], 1)
        self.assertIn("Most accurate", render(report))

    def test_reference_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ref.json"
            path.write_text(json.dumps([{"case_id": "x", "recommendation": "MAYBE"}]))
            with self.assertRaises(ValueError):
                load_reference(path)


if __name__ == "__main__":
    unittest.main()
