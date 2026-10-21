"""Synthetic v9 evidence-ledger acceptance and rejection regressions."""
import unittest

from pydantic import ValidationError
from evaluation.v9_evidence import validate_ledger, Ledger


def row(group, quote, component, logic="single", status="met", resume=None,
        context="employment", kind="mandatory", years=None, materiality="central"):
    return dict(group_id=group, jd_quote=quote, component=component,
                logic=logic, kind=kind, status=status, resume_quote=resume,
                evidence_context=context if resume else "none",
                min_years=years, materiality=materiality if kind == "mandatory" else None,
                reason="Synthetic manually labeled requirement")


def run(jd, resume, rows, score=95):
    return validate_ledger(dict(fit_score=score, requirements=rows), jd, resume)


class V9Tests(unittest.TestCase):
    def test_aurora_and_requires_both_production_tenures(self):
        jd = "2+ years with dbt and Looker in a production environment"
        resume = "Looker 5 years professional; dbt portfolio project"
        rows = [
            row("combo", jd, "Looker", "all_of", resume="Looker 5 years professional", years=2),
            row("combo", jd, "dbt", "all_of", status="not_demonstrated",
                resume="dbt portfolio project", context="project", years=2),
        ]
        result = run(jd, resume, rows)
        self.assertEqual(result["groups"][0]["status"], "not_demonstrated")
        self.assertFalse(result["valid"])

    def test_aurora_portfolio_claim_met_is_downgraded(self):
        jd = "2+ years with dbt and Looker in a production environment"
        resume = "Looker 5 years professional; dbt portfolio project"
        rows = [
            row("combo", jd, "Looker", "all_of", resume="Looker 5 years professional", years=2),
            row("combo", jd, "dbt", "all_of", resume="dbt portfolio project", context="project", years=2),
        ]
        result = run(jd, resume, rows)
        self.assertEqual(result["groups"][0]["status"], "unclear")
        self.assertTrue(result["warnings"])

    def test_netomi_or_does_not_accept_spark(self):
        jd = "Experience with Apache Kafka or RabbitMQ for asynchronous systems"
        resume = "Apache Spark production workflows"
        rows = [
            row("messaging", jd, "Kafka", "any_of", "not_demonstrated"),
            row("messaging", jd, "RabbitMQ", "any_of", "not_demonstrated"),
        ]
        self.assertFalse(run(jd, resume, rows)["valid"])
        rows[0] = row("messaging", jd, "Kafka", "any_of", resume=resume, status="met")
        self.assertFalse(run(jd, resume, rows)["valid"])

    def test_netomi_either_named_option_suffices(self):
        jd = "Experience with Apache Kafka or RabbitMQ for asynchronous systems"
        resume = "Apache Kafka in production"
        rows = [row("messaging", jd, "Kafka", "any_of", resume=resume),
                row("messaging", jd, "RabbitMQ", "any_of", status="not_demonstrated")]
        result = run(jd, resume, rows)
        self.assertTrue(result["valid"])
        self.assertEqual(result["groups"][0]["status"], "met")

    def test_tesla_looker_equivalent_and_forecasting_unclear(self):
        jd = ("Visualization tools such as Power BI, Tableau, Excel, or similar. "
              "Demonstrated statistical methods particularly regarding budget forecasting.")
        resume = "Built Looker dashboards for business reporting"
        rows = [row("viz", "Visualization tools such as Power BI, Tableau, Excel, or similar",
                    "Looker", "open_equivalent", resume="Built Looker dashboards"),
                row("forecast", "Demonstrated statistical methods particularly regarding budget forecasting",
                    "budget forecasting", status="unclear")]
        result = run(jd, resume, rows)
        self.assertTrue(result["valid"])
        self.assertEqual(len(result["warnings"]), 1)

    def test_centerwell_optional_not_promoted(self):
        jd = "Advanced SQL required. Databricks preferred."
        resume = "SQL 8 years in production"
        rows = [row("sql", "Advanced SQL required", "SQL", resume=resume),
                row("dbx", "Databricks preferred", "Databricks", "single",
                    "not_demonstrated", kind="preferred", materiality=None)]
        self.assertTrue(run(jd, resume, rows)["valid"])
        rows[1]["kind"] = "mandatory"
        rows[1]["materiality"] = "central"
        self.assertFalse(run(jd, resume, rows)["valid"])

    def test_constructor_airflow_valid_alternative(self):
        jd = "Modern analytics stacks such as dbt, Airflow, or similar"
        resume = "Built production Airflow pipelines"
        rows = [row("modern", jd, "Airflow", "open_equivalent",
                    resume="Built production Airflow pipelines")]
        self.assertTrue(run(jd, resume, rows)["valid"])

    def test_false_quotes_and_groups_rejected(self):
        jd = "Python and SQL required"
        resume = "Python 5 years; SQL 5 years"
        rows = [row("both", jd, "Python", "all_of", resume="Python 5 years"),
                row("both", jd, "SQL", "any_of", resume="SQL 5 years")]
        self.assertFalse(run(jd, resume, rows)["valid"])
        rows[1]["logic"] = "all_of"
        rows[1]["jd_quote"] = "Unrelated invented requirement"
        self.assertFalse(run(jd, resume, rows)["valid"])
        rows[1]["jd_quote"] = jd
        rows[1]["resume_quote"] = "Invented SQL evidence"
        self.assertFalse(run(jd, resume, rows)["valid"])

    def test_strict_schema_and_no_recommendation(self):
        with self.assertRaises(ValidationError):
            Ledger.model_validate({"fit_score": 95, "recommendation": "apply", "requirements": []})

    def test_peripheral_gap_not_auto_failure(self):
        jd = "Familiarity with rare internal tool"
        result = run(jd, "", [row("p", jd, "rare internal tool", status="not_demonstrated",
                                  materiality="peripheral")])
        self.assertTrue(result["valid"])


if __name__ == "__main__":
    unittest.main()
