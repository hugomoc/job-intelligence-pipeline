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

    def test_safe_provider_error_diagnostics(self):
        from evaluation.run_v9_gemini import safe_provider_error
        class FakeClientError(Exception):
            status_code = 400
        error = FakeClientError("SECRET API KEY AND RESUME")
        detail = safe_provider_error(error)
        self.assertEqual(detail["http_status"], 400)
        self.assertEqual(detail["error_category"], "other_invalid_request")
        self.assertNotIn("SECRET", json.dumps(detail))

    def test_safe_400_categories_without_raw_details(self):
        from evaluation.run_v9_gemini import safe_provider_error
        for message, expected in [
            ("response_schema includes unsupported oneOf PRIVATE", "unsupported_schema"),
            ("model does not exist PRIVATE", "invalid_model"),
            ("generationConfig temperature invalid PRIVATE", "unsupported_generation_config"),
            ("input too long PRIVATE", "request_too_large"),
        ]:
            class APIError(Exception):
                status_code = 400
            detail = safe_provider_error(APIError(message))
            self.assertEqual(detail["error_category"], expected)
            self.assertNotIn("PRIVATE", json.dumps(detail))

    def test_local_schema_probe_never_calls_network(self):
        from evaluation.run_v9_gemini import local_schema_probe
        from unittest.mock import patch
        with patch("google.genai.Client", side_effect=AssertionError("network")):
            result = local_schema_probe()
        self.assertIn(result["status"], ("local_schema_serialized", "local_schema_error"))

    def test_unknown_errors_do_not_expose_messages(self):
        from evaluation.run_v9_gemini import safe_provider_error
        detail = safe_provider_error(RuntimeError("PRIVATE PROMPT"))
        self.assertEqual(detail["error_category"], "unknown")
        self.assertNotIn("PRIVATE", json.dumps(detail))

    def test_quota_and_model_missing_categories(self):
        from evaluation.run_v9_gemini import safe_provider_error
        for code, category in [(404, "model_not_found"), (429, "rate_limit_or_quota"),
                               (403, "permission_or_api_access")]:
            class APIError(Exception):
                pass
            error = APIError("sensitive")
            error.code = code
            self.assertEqual(safe_provider_error(error)["error_category"], category)

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
