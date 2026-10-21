"""Pure synthetic fixture tests; no private JDs or API/database access."""
import unittest
import tempfile
import json
from pathlib import Path

from evaluation.run_evidence_audit import run_audit

from evaluation.evidence_checks import assess_case, evaluate_cases, validate_case


def case(case_id="synthetic", score=95, label="questionable",
         status="not_demonstrated", mandatory=True, evidence=""):
    return {
        "case_id": case_id,
        "stored_score": score,
        "review_label": label,
        "requirements": [{
            "requirement": "Fictional requirement",
            "mandatory": mandatory,
            "status": status,
            "resume_evidence": evidence,
        }],
    }


class EvidenceChecksTests(unittest.TestCase):
    def test_unsupported_mandatory_high_score_is_flagged(self):
        outcome = assess_case(case())
        self.assertEqual({f["code"] for f in outcome["flags"]},
                         {"mandatory_evidence_gap", "high_score_unsupported_mandatory"})
        self.assertEqual(outcome["mandatory_not_demonstrated"], 1)

    def test_optional_gap_does_not_block_high_score(self):
        self.assertFalse(assess_case(case(mandatory=False))["flagged"])

    def test_uncertain_is_not_claimed_missing(self):
        outcome = assess_case(case(status="unclear"))
        self.assertEqual(outcome["mandatory_not_demonstrated"], 0)
        self.assertIn("high_score_uncertain_mandatory", [f["code"] for f in outcome["flags"]])

    def test_low_score_gap_reports_evidence_without_high_score_flag(self):
        outcome = assess_case(case(score=50))
        self.assertNotIn("high_score_unsupported_mandatory", [f["code"] for f in outcome["flags"]])

    def test_met_requires_citation_and_is_not_flagged(self):
        with self.assertRaises(ValueError):
            validate_case(case(status="met"))
        outcome = assess_case(case(status="met", evidence="Fictional resume project"))
        self.assertFalse(outcome["flagged"])

    def test_explicit_mandatory_and_review_label_required(self):
        bad = case()
        bad["requirements"][0]["mandatory"] = "yes"
        with self.assertRaises(ValueError):
            assess_case(bad)
        bad = case()
        bad["review_label"] = "apply"
        with self.assertRaises(ValueError):
            assess_case(bad)

    def test_descriptive_metrics_and_duplicate_rejection(self):
        rows = [
            case("inflated", label="inflated"),
            case("supported", label="strongly_supported", status="met", evidence="Synthetic resume quote"),
            case("uncertain", label="questionable", status="partial"),
        ]
        results = evaluate_cases(rows)
        self.assertEqual(results["known_inflated_detected"], 1)
        self.assertEqual(results["supported_false_flags"], 0)
        self.assertEqual(results["n"], 3)
        with self.assertRaises(ValueError):
            evaluate_cases([rows[0], rows[0]])

    def test_local_aggregate_runner_does_not_emit_private_case_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'private.json'
            private = case('private-id', label='inflated')
            private['requirements'][0]['requirement'] = 'PRIVATE REQUIREMENT TEXT'
            path.write_text(json.dumps([private]))
            result = run_audit(path)
            self.assertEqual(result['known_inflated_detected'], 1)
            self.assertNotIn('cases', result)
            self.assertNotIn('private-id', json.dumps(result))
            self.assertNotIn('PRIVATE REQUIREMENT TEXT', json.dumps(result))

    def test_no_input_mutation(self):
        original = case()
        assess_case(original)
        self.assertEqual(original, case())


if __name__ == "__main__":
    unittest.main()
