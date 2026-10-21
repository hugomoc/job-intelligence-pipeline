"""Opt-in, isolated Gemini v9 experiment. Never touches scoring or DuckDB.

Input: private JSON list of {case_id, jd, resume, original_score?}.
Default is dry run. Live requests require --live and --max-calls.
Results go to a private path outside the repository.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path

from evaluation.v9_evidence import Ledger, validate_ledger

ROOT = Path(__file__).resolve().parents[1]


def outside_repo(path: Path) -> Path:
    absolute = path.expanduser().resolve()
    if absolute.is_relative_to(ROOT):
        raise ValueError("Private input/output must be outside the repository")
    return absolute


def read_cases(path: Path) -> list[dict]:
    source = outside_repo(path)
    cases = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(cases, list) or not cases:
        raise ValueError("Expected nonempty JSON list of private cases")
    for item in cases:
        if not isinstance(item, dict) or not all(
            isinstance(item.get(k), str) and item[k].strip()
            for k in ("case_id", "jd", "resume")
        ):
            raise ValueError("Each case needs case_id, jd, and resume text")
    if len({c["case_id"] for c in cases}) != len(cases):
        raise ValueError("Duplicate case IDs")
    return cases


def build_prompt(jd: str, resume: str) -> str:
    """No prior score supplied: protects against anchoring."""
    return (
        "Assess this job against only the provided resume. Return a JSON object "
        "matching the supplied schema, with fit_score (0-100) and requirements. "
        "List EVERY material mandatory qualification, distinguishing preferred "
        "qualifications and general responsibilities. Preserve exact AND versus OR "
        "logic: all_of means every grouped component required; any_of means any "
        "named option suffices; open_equivalent only if the JD permits similar or "
        "equivalent tools; single for a single qualification. For all_of/any_of, "
        "use the same verbatim jd_quote and group_id for each component; use "
        "distinct group_ids for distinct requirements. JD and resume quotes "
        "must be exact substrings (no paraphrases). min_years is a nonnegative "
        "integer only when explicitly required. Resume evidence_context must "
        "distinguish employment, project, skills_list, and none. Do not infer "
        "production tenure from projects, skills lists, or unrelated tools. "
        "Use met, partial, not_demonstrated, or unclear; missing documentation "
        "does not prove a candidate lacks a skill. Centrality refers to how "
        "important an explicit mandatory qualification is for this job. "
        "Never invent eligibility or experience, and do not return a "
        "recommendation. Score based on the evidence rather than job-title "
        "similarity alone. If a central mandatory requirement is not "
        "demonstrated, reconsider an exceptional score.\n\n"
        "JOB DESCRIPTION:\n" + jd + "\n\nRESUME:\n" + resume
    )


def check_payload(payload: object, jd: str, resume: str) -> dict:
    ledger = Ledger.model_validate(payload)
    return validate_ledger(ledger, jd, resume)


def call_gemini(prompt: str, model: str) -> object:
    # Imported only after the explicit live gate to keep dry runs offline.
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    response = client.models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=Ledger,
            temperature=0.1,
        ),
    )
    if not response.text:
        raise ValueError("Gemini returned an empty response")
    return json.loads(response.text)


def evaluate(cases: list[dict], *, live: bool, max_calls: int, model: str,
             caller=call_gemini) -> list[dict]:
    if max_calls < 0:
        raise ValueError("max_calls must not be negative")
    if live and max_calls < 1:
        raise ValueError("--live requires a positive --max-calls")
    if live and len(cases) > max_calls:
        raise ValueError("Too many cases for --max-calls; narrow the input")
    results = []
    for case in cases:
        prompt = build_prompt(case["jd"], case["resume"])
        entry = {
            "case_id": case["case_id"],
            "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
            "model": model,
            "status": "dry_run",
        }
        if live:
            try:
                payload = caller(prompt, model)
                result = check_payload(payload, case["jd"], case["resume"])
                entry.update(status="validated" if result["valid"] else "invalid",
                             validation=result, ledger=payload)
            except Exception as error:
                entry.update(status="error", error_type=type(error).__name__)
                # Do not leak resume/JD or provider error contents.
        results.append(entry)
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", default="gemini-3.5-flash-lite")
    parser.add_argument("--live", action="store_true", help="Explicitly authorize paid API calls")
    parser.add_argument("--max-calls", type=int, default=0)
    args = parser.parse_args()
    source = outside_repo(args.input)
    destination = outside_repo(args.output)
    if source == destination:
        parser.error("Input and output paths must differ")
    if destination.exists():
        parser.error("Refusing to overwrite existing results")
    cases = read_cases(source)
    if args.live:
        # Match the application’s local .env configuration only on explicit live runs.
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
        if not os.environ.get("GEMINI_API_KEY"):
            parser.error("GEMINI_API_KEY is required for live calls")
    results = evaluate(cases, live=args.live, max_calls=args.max_calls, model=args.model)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("x", encoding="utf-8") as file:
        json.dump(results, file, indent=2)
    print(f"{len(results)} case(s); live={args.live}; output={destination}")


if __name__ == "__main__":
    main()
