"""No-network v9 harness tests, including opt-in and private output."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from evaluation.run_v9_gemini import (
    build_prompt, check_payload, evaluate, outside_repo, read_cases,
)


def fixture():
    return {"case_id": "synthetic-1", "jd": "Python required", "resume": "Python production experience"}


class V9GeminiHarnessTests(unittest.TestCase):
    def test_default_dry_run_never_calls_provider(self):
        with patch("evaluation.run_v9_gemini.call_gemini", side_effect=AssertionError("network")):
            result = evaluate([fixture()], live=False, max_calls=0, model="fake")
        self.assertEqual(result[0]["status"], "dry_run")
        self.assertNotIn("ledger", result[0])

    def test_live_requires_positive_budget_and_caps_cases(self):
        with self.assertRaises(ValueError):
            evaluate([fixture()], live=True, max_calls=0, model="fake")
        with self.assertRaises(ValueError):
            evaluate([fixture(), dict(fixture(), case_id="second")],
                     live=True, max_calls=1, model="fake")

    def test_prompt_never_includes_prior_score(self):
        case = fixture()
        case["original_score"] = 95
        prompt = build_prompt(case["jd"], case["resume"])
        self.assertIn("Python required", prompt)
        self.assertNotIn("95", prompt)

    def test_fake_live_payload_runs_validator(self):
        payload = {
            "fit_score": 95,
            "requirements": [{
                "group_id": "python",
                "jd_quote": "Python required",
                "kind": "mandatory",
                "logic": "single",
                "component": "Python",
                "min_years": None,
                "resume_quote": "Python production experience",
                "evidence_context": "employment",
                "status": "met",
                "materiality": "central",
                "reason": "Matching Python experience"
            }]
        }
        fake = lambda prompt, model: payload
        outcome = evaluate([fixture()], live=True, max_calls=1, model="fake", caller=fake)
        self.assertEqual(outcome[0]["status"], "validated")
        self.assertTrue(check_payload(payload, fixture()["jd"], fixture()["resume"])["valid"])

    def test_errors_dont_echo_sensitive_provider_contents(self):
        def bad(prompt, model):
            raise RuntimeError("SECRET RESUME IN ERROR")
        result = evaluate([fixture()], live=True, max_calls=1, model="fake", caller=bad)
        self.assertEqual(result[0]["status"], "error")
        self.assertNotIn("SECRET", json.dumps(result))

    def test_private_path_and_duplicate_validation(self):
        from evaluation.run_v9_gemini import ROOT
        with self.assertRaises(ValueError):
            outside_repo(ROOT / "secret.json")
        with tempfile.TemporaryDirectory() as temp:
            input_path = Path(temp) / "case.json"
            input_path.write_text(json.dumps([fixture(), fixture()]))
            with self.assertRaises(ValueError):
                read_cases(input_path)
            input_path.write_text(json.dumps([fixture()]))
            self.assertEqual(len(read_cases(input_path)), 1)


if __name__ == "__main__":
    unittest.main()
