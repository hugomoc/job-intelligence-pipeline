"""Run private, human-reviewed requirement evidence through pure offline checks.

Input remains local. Output is aggregate JSON only (no JD, resume, IDs or excerpts).
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from evaluation.evidence_checks import evaluate_cases


def run_audit(path: Path) -> dict:
    with path.open(encoding="utf-8") as handle:
        cases = json.load(handle)
    if not isinstance(cases, list):
        raise ValueError("Expected a JSON list of reviewed cases")
    metrics = evaluate_cases(cases)
    return {key: value for key, value in metrics.items() if key != "cases"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path, help="Private local evidence JSON")
    args = parser.parse_args()
    print(json.dumps(run_audit(args.input), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
