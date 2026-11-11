"""Compare v9 provider results against independently reviewed assessments.

Read-only and offline: reads private result files written by
``evaluation.run_v9`` (one per provider) and a private reference file, then
prints per-case and per-provider accuracy. No model calls, no database.

Reference file (private, outside Git), a JSON list such as::

    [{"case_id": "aurora",
      "recommendation": "LOW PRIORITY",
      "score": 55,
      "score_tolerance": 10,
      "central_mandatory": {"dbt": "not_demonstrated", "Looker": "met"}}]

``recommendation`` and ``score`` are your reviewed conclusions.
``central_mandatory`` (optional) maps a requirement component to your
reviewed status (met, partial, unclear, not_demonstrated) so requirement
interpretation is checked, not just the final number.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from evaluation.run_v9 import outside_repo
from evaluation.v9_scoring import RECOMMENDATIONS

TIER = {"SKIP": 0, "LOW PRIORITY": 1, "HUMAN REVIEW": 2, "APPLY": 3}
STATUSES = {"met", "partial", "unclear", "not_demonstrated"}
DEFAULT_TOLERANCE = 10


def load_reference(path: Path) -> dict[str, dict]:
    items = json.loads(outside_repo(path).read_text(encoding="utf-8"))
    if not isinstance(items, list) or not items:
        raise ValueError("Reference must be a nonempty JSON list")
    reference = {}
    for item in items:
        case_id = item.get("case_id")
        if not isinstance(case_id, str) or not case_id:
            raise ValueError("Reference case_id is required")
        if case_id in reference:
            raise ValueError(f"Duplicate reference case {case_id}")
        if item.get("recommendation") not in RECOMMENDATIONS:
            raise ValueError(f"{case_id}: recommendation must be one of {RECOMMENDATIONS}")
        score = item.get("score")
        if score is not None and (type(score) not in (int, float) or not 0 <= score <= 100):
            raise ValueError(f"{case_id}: score must be 0-100 or null")
        central = item.get("central_mandatory", {})
        if not isinstance(central, dict) or any(v not in STATUSES for v in central.values()):
            raise ValueError(f"{case_id}: central_mandatory statuses must be in {sorted(STATUSES)}")
        reference[case_id] = item
    return reference


def _component_status(entry: dict, component: str) -> str | None:
    """Best effective status the provider gave a reviewed component."""
    rows = entry.get("decision", {}).get("validation", {}).get("rows", [])
    wanted = component.casefold()
    order = ["met", "partial", "unclear", "not_demonstrated"]
    found = [r["effective_status"] for r in rows
             if r["kind"] == "mandatory"
             and (wanted == r["component"].casefold()
                  or wanted in r["component"].casefold()
                  or r["component"].casefold() in wanted)]
    if not found:
        return None
    return min(found, key=order.index)


def compare(reference: dict[str, dict], results: list[dict]) -> dict:
    """Score accuracy counts VALIDATED assessments only.

    An ``invalid`` entry failed the evidence contract: it is a validation
    failure, never a scoring result, and its diagnostic score stays labelled
    unvalidated. Provider/API errors and dry runs are reported separately.
    """
    providers: dict[str, dict] = {}
    for entry in results:
        label = f"{entry.get('provider', '?')}:{entry.get('model', '?')}"
        stats = providers.setdefault(label, {"cases": []})
        ref = reference.get(entry["case_id"])
        if ref is None:
            continue
        status = entry.get("status")
        case = {"case_id": entry["case_id"], "status": status,
                "expected_recommendation": ref["recommendation"], "expected_score": ref.get("score")}
        if status == "validated":
            tol = ref.get("score_tolerance", DEFAULT_TOLERANCE)
            final, raw = entry["final_score"], entry["model_score"]
            decision = entry.get("decision", {})
            case.update(
                recommendation=entry["recommendation"],
                technical_recommendation=entry.get("technical_recommendation",
                                                   entry["recommendation"]),
                model_score=raw,
                final_score=final,
                recommendation_correct=entry["recommendation"] == ref["recommendation"],
                tier_error=TIER[entry["recommendation"]] - TIER[ref["recommendation"]],
                unsupported_resume_citations=decision.get("unsupported_resume_citations", 0),
                unsupported_jd_citations=decision.get("unsupported_jd_citations", 0),
            )
            if ref.get("score") is not None:
                case.update(
                    final_error=final - ref["score"],
                    model_error=raw - ref["score"],
                    within_tolerance=abs(final - ref["score"]) <= tol,
                )
            components = {}
            for component, expected in ref.get("central_mandatory", {}).items():
                got = _component_status(entry, component)
                components[component] = {"expected": expected, "provider": got,
                                         "agree": got == expected}
            case["central_mandatory"] = components
        elif status == "invalid":
            # Older result files carried plain score keys on invalid entries;
            # either way the numbers are diagnostic only.
            case.update(
                validation_failures=len(entry.get("validation_failures")
                                        or entry.get("decision", {}).get("validation", {}).get("failures", [])),
                unvalidated_model_score=entry.get("unvalidated_model_score",
                                                  entry.get("model_score")),
                unvalidated_final_score=entry.get("unvalidated_final_score",
                                                  entry.get("final_score")),
                unvalidated_recommendation=entry.get("unvalidated_recommendation",
                                                     entry.get("recommendation")),
            )
        elif status == "error":
            case["error_category"] = entry.get("error_category", "unknown")
        stats["cases"].append(case)

    summary = {}
    for label, stats in providers.items():
        cases = stats["cases"]
        scored = [c for c in cases if c["status"] == "validated"]
        invalid = [c for c in cases if c["status"] == "invalid"]
        errors = [c for c in cases if c["status"] == "error"]
        with_score = [c for c in scored if "final_error" in c]
        components = [v for c in scored for v in c["central_mandatory"].values()]
        responded = len(scored) + len(invalid)
        summary[label] = {
            "cases": len(cases),
            "validated": len(scored),
            "invalid": len(invalid),
            "errors": len(errors),
            "dry_run_or_other": len(cases) - responded - len(errors),
            "validation_pass_rate": round(len(scored) / responded, 2) if responded else None,
            "recommendation_correct": sum(c["recommendation_correct"] for c in scored),
            "too_optimistic": sum(c["tier_error"] > 0 for c in scored),
            "too_pessimistic": sum(c["tier_error"] < 0 for c in scored),
            "final_score_mae": _mean([abs(c["final_error"]) for c in with_score]),
            "model_score_mae": _mean([abs(c["model_error"]) for c in with_score]),
            "within_tolerance": sum(c["within_tolerance"] for c in with_score),
            "scores_compared": len(with_score),
            "requirement_status_agreement": sum(v["agree"] for v in components),
            "requirement_status_total": len(components),
            "requirement_not_extracted": sum(v["provider"] is None for v in components),
            "unsupported_resume_citations": sum(c["unsupported_resume_citations"] for c in scored),
            "unsupported_jd_citations": sum(c["unsupported_jd_citations"] for c in scored),
            "cases_detail": cases,
        }
    missing = {label: sorted(set(reference) - {c["case_id"] for c in s["cases"]})
               for label, s in providers.items()}
    # Only validated assessments can improve a ranking: correct
    # recommendations first, then validated count, then score accuracy.
    ranking = sorted(
        summary,
        key=lambda k: (
            -summary[k]["recommendation_correct"],
            -summary[k]["validated"],
            -summary[k]["within_tolerance"],
            summary[k]["final_score_mae"] if summary[k]["final_score_mae"] is not None else 999,
            summary[k]["unsupported_resume_citations"] + summary[k]["unsupported_jd_citations"],
        ),
    )
    return {"providers": summary, "ranking": ranking, "missing_cases": missing}


def _mean(values):
    return round(sum(values) / len(values), 1) if values else None


def render(report: dict) -> str:
    lines = []
    for label in report["ranking"]:
        s = report["providers"][label]
        lines.append(f"== {label} ==")
        lines.append(
            f"validated {s['validated']}/{s['cases']} (invalid ledgers {s['invalid']}, "
            f"API errors {s['errors']}, other {s['dry_run_or_other']}); "
            f"validation pass rate {s['validation_pass_rate']}")
        lines.append(
            f"recommendation correct {s['recommendation_correct']}/{s['validated']} "
            f"(too optimistic {s['too_optimistic']}, too pessimistic {s['too_pessimistic']}); "
            f"score within tolerance {s['within_tolerance']}/{s['scores_compared']}; "
            f"MAE final {s['final_score_mae']} vs raw model {s['model_score_mae']} "
            f"(validated assessments only)")
        lines.append(
            f"requirement statuses agree {s['requirement_status_agreement']}/"
            f"{s['requirement_status_total']} (not extracted {s['requirement_not_extracted']}); "
            f"unsupported citations resume {s['unsupported_resume_citations']}, "
            f"JD {s['unsupported_jd_citations']}")
        for c in s["cases_detail"]:
            if c["status"] == "invalid":
                lines.append(
                    f"  !! {c['case_id']:<12} INVALID ledger "
                    f"({c['validation_failures']} contract failure(s)); unvalidated "
                    f"score {c['unvalidated_model_score']}->{c['unvalidated_final_score']} "
                    f"{c['unvalidated_recommendation']} — excluded from accuracy")
                continue
            if c["status"] != "validated":
                detail = c.get("error_category", c["status"])
                lines.append(f"  -- {c['case_id']:<12} {c['status']} ({detail})")
                continue
            flag = "ok " if c["recommendation_correct"] else "XX "
            overlay = ("" if c["recommendation"] == c["technical_recommendation"]
                       else f" [technical {c['technical_recommendation']}, eligibility override]")
            disagreements = [f"{k}: {v['provider']} (expected {v['expected']})"
                             for k, v in c["central_mandatory"].items() if not v["agree"]]
            lines.append(
                f"  {flag}{c['case_id']:<12} {c['recommendation']:<12}{overlay} "
                f"(expected {c['expected_recommendation']}) "
                f"score {c['model_score']}->{c['final_score']} (expected {c['expected_score']})"
                + (f"; requirement disagreements: {'; '.join(disagreements)}" if disagreements else ""))
        if report["missing_cases"].get(label):
            lines.append(f"  missing reference cases: {', '.join(report['missing_cases'][label])}")
    if report["ranking"]:
        top = report["ranking"][0]
        if report["providers"][top]["validated"] == 0:
            lines.append("No provider produced a validated assessment; "
                         "no accuracy winner can be declared.")
        else:
            lines.append(f"Most accurate on this sample: {top} "
                         "(5 cases is a smoke test, not a statistically reliable ranking)")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--results", type=Path, nargs="+", required=True)
    parser.add_argument("--output", type=Path, help="Optional private JSON report path")
    args = parser.parse_args()
    reference = load_reference(args.reference)
    results = []
    for path in args.results:
        results.extend(json.loads(outside_repo(path).read_text(encoding="utf-8")))
    report = compare(reference, results)
    print(render(report))
    if args.output:
        destination = outside_repo(args.output)
        with destination.open("x", encoding="utf-8") as file:
            json.dump(report, file, indent=2)


if __name__ == "__main__":
    main()
