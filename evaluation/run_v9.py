"""Opt-in, isolated v9 experiment for Gemini and OpenAI. Never touches scoring or DuckDB writes.

Both providers receive the *identical* prompt and schema, and every response
goes through the same validator and decision policy (``evaluation.v9_scoring``),
so differences in the results come from the model, not from the harness.

Input: private JSON list of {case_id, jd, resume, original_score?,
eligibility_blockers?, eligibility_confirmed?} or ``--from-db`` frozen cases.
Default is dry run. Live requests require --live and --max-calls.
Results go to a private path outside the repository.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path

from evaluation.v9_evidence import Ledger
from evaluation.v9_scoring import decide

ROOT = Path(__file__).resolve().parents[1]
PROMPT_VERSION = "v9.2"
FROZEN_NAMES = ("aurora", "netomi", "tesla", "centerwell", "constructor")
PROVIDERS = {
    "gemini": {"model": "gemini-3.5-flash", "key": "GEMINI_API_KEY"},
    "openai": {"model": "gpt-4.1", "key": "OPENAI_API_KEY"},
}


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
        blockers = item.get("eligibility_blockers", [])
        if not isinstance(blockers, list) or not all(isinstance(b, str) for b in blockers):
            raise ValueError("eligibility_blockers must be a list of strings")
        if not isinstance(item.get("eligibility_confirmed", False), bool):
            raise ValueError("eligibility_confirmed must be a boolean")
    if len({c["case_id"] for c in cases}) != len(cases):
        raise ValueError("Duplicate case IDs")
    return cases


PROMPT_RULES = """\
You are auditing how well ONE candidate's resume fits ONE job. Return a JSON
object matching the supplied schema: fit_score (0-100) and requirements.

REQUIREMENTS: read the COMPLETE job description.
- List EVERY mandatory qualification, every explicit personal eligibility
  requirement (work authorization, citizenship, clearance, on-site location,
  travel, licences), the preferred qualifications, and the central
  responsibilities.
- kind=mandatory only for qualifications the JD presents as required (listed
  under requirements/qualifications/"you have", or worded must/required/
  minimum). kind=preferred for "preferred", "nice to have", "bonus", "a plus",
  "ideally", "desired", or a separate preferred/bonus section. Never promote a
  preferred item to mandatory because a tool is named. kind=eligibility for
  personal eligibility. kind=responsibility for duties.
- materiality (mandatory rows only): central = the job cannot be done without
  it / it is the core of the role; peripheral = a secondary mandatory item.
- logic: all_of = every named component required ("X and Y"); any_of = any
  one named option suffices ("X or Y"); open_equivalent = the JD explicitly
  allows similar/equivalent tools ("such as", "or similar", "or equivalent",
  "e.g."); single = one qualification. For all_of/any_of use ONE row per
  named component, sharing the same group_id and the same verbatim jd_quote.
  Use distinct group_ids for distinct requirements.
- min_years: the integer only when the JD states a minimum duration.
- production_required: true when the JD asks for production, professional,
  hands-on commercial, or in-role experience (also true when min_years is set).

EVIDENCE: use only the resume text.
- jd_quote and resume_quote must be copied EXACTLY, character for character,
  from the JD and resume. Never paraphrase, merge separate lines, fix typos,
  or quote text that is not there. If you cannot find an exact supporting
  resume passage, set resume_quote to null, evidence_context to none, and the
  status to not_demonstrated or unclear. A fabricated quote is worse than a gap.
- evidence_context: employment = a bullet or line under a professional job;
  project = a personal, academic, portfolio, side, or course project;
  skills_list = a skills/tools/technologies list; none = no evidence.
- Portfolio projects, coursework and skills lists are NOT professional or
  production experience and do NOT establish years of tenure. For a
  production_required or min_years requirement they can be partial at most.
- Tenure needs employment evidence that shows the duration (dates or stated
  years) of using that specific skill. A job's overall dates only count when
  the quoted bullet shows the skill was used in that job.
- Equivalence: for open_equivalent rows, a different tool of the same kind
  (e.g. Looker for "Tableau, Power BI, or similar") can be met. For any_of,
  all_of and single rows, only the named technology counts: a related tool
  (e.g. Spark for "Kafka or RabbitMQ") is partial at most, and the component
  must appear in the resume quote to be met. For all_of, report each component.
- Status: met = fully demonstrated at the required level; partial = related
  or insufficient evidence (wrong context, shorter tenure, adjacent tool);
  unclear = the resume is ambiguous; not_demonstrated = no supporting
  evidence. Absence from the resume is not proof the candidate lacks a skill,
  but it is still not_demonstrated for scoring.
- Eligibility: never assume or invent the candidate's eligibility. Unless the
  resume states it explicitly, eligibility rows are unclear.

SCORE: fit_score must follow from your own requirement rows, not title
similarity.
- Any central mandatory requirement not_demonstrated means the candidate is
  not a strong fit: score well below the apply range.
- Partial or unclear central mandatory evidence rules out an exceptional score.
- If every mandatory requirement is met with employment evidence, do not
  under-score because of missing preferred items alone.
- Do not return a recommendation.
"""


def build_prompt(jd: str, resume: str) -> str:
    """No prior score supplied: protects against anchoring."""
    return PROMPT_RULES + "\nJOB DESCRIPTION:\n" + jd + "\n\nRESUME:\n" + resume


def check_payload(payload: object, jd: str, resume: str, case: dict | None = None) -> dict:
    case = case or {}
    return decide(
        Ledger.model_validate(payload), jd, resume,
        eligibility_blockers=case.get("eligibility_blockers") or [],
        eligibility_confirmed=bool(case.get("eligibility_confirmed", False)),
    )


GEMINI_SCHEMA_KEYWORDS = ("type", "enum", "nullable", "required", "property_ordering")


def gemini_response_schema() -> object:
    """Prune the SDK's Ledger conversion to the subset Gemini accepts.

    Passing the Pydantic model directly serializes ``additional_properties``
    (from ``extra="forbid"``) and ``min_length``/``min_items`` constraints,
    which the API rejects with HTTP 400 ``unsupported_schema``. Dropping them
    loses no rigor: ``check_payload`` re-validates every response against the
    full Pydantic contract locally.
    """
    from google.genai import types
    from google.genai import _transformers

    full = _transformers.t_schema(None, Ledger).model_dump(exclude_none=True, mode="json")

    def prune(node: dict) -> dict:
        kept = {key: node[key] for key in GEMINI_SCHEMA_KEYWORDS if key in node}
        if "properties" in node:
            kept["properties"] = {name: prune(sub) for name, sub in node["properties"].items()}
        if "items" in node:
            kept["items"] = prune(node["items"])
        return kept

    return types.Schema.model_validate(prune(full))


def gemini_generation_config() -> object:
    """The one config used by both the live request and the local probe."""
    from google.genai import types

    return types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=gemini_response_schema(),
        temperature=0.1,
    )


def collect_keys(node: object) -> list[str]:
    """All mapping keys anywhere in a JSON-like structure (never values)."""
    keys: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            keys.append(key)
            keys.extend(collect_keys(value))
    elif isinstance(node, list):
        for item in node:
            keys.extend(collect_keys(item))
    return keys


def openai_strict_schema() -> dict:
    """Convert the pydantic ledger schema to OpenAI strict structured outputs.

    Strict mode needs every property required, additionalProperties false,
    and no unsupported keywords. Optional fields stay nullable, so the
    contract is the same one Gemini receives.
    """
    schema = copy.deepcopy(Ledger.model_json_schema())

    def walk(node):
        if isinstance(node, dict):
            for key in ("default", "title", "minLength", "description"):
                node.pop(key, None)
            if node.get("type") == "object" and "properties" in node:
                node["additionalProperties"] = False
                node["required"] = list(node["properties"])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(schema)
    return schema


def call_gemini(prompt: str, model: str) -> object:
    # Imported only after the explicit live gate to keep dry runs offline.
    from google import genai

    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    response = client.models.generate_content(
        model=model,
        contents=prompt,
        config=gemini_generation_config(),
    )
    if not response.text:
        raise ValueError("Gemini returned an empty response")
    return json.loads(response.text)


def call_openai(prompt: str, model: str) -> object:
    from openai import OpenAI

    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.1,
        response_format={
            "type": "json_schema",
            "json_schema": {"name": "v9_ledger", "strict": True, "schema": openai_strict_schema()},
        },
    )
    content = response.choices[0].message.content
    if not content:
        raise ValueError("OpenAI returned an empty response")
    return json.loads(content)


CALLERS = {"gemini": call_gemini, "openai": call_openai}


def classify_bad_request(error: Exception) -> str:
    """Read provider details in memory, return an allowlisted category only."""
    import re

    raw = str(error).casefold()
    if re.search(r"(response.?schema|json.?schema|schema|additionalproperties|\\$defs|oneof|anyof|enum)", raw):
        return "unsupported_schema"
    if re.search(r"(model.*(not found|unsupported|invalid|does not exist)|invalid model|unknown model)", raw):
        return "invalid_model"
    if re.search(r"(temperature|generationconfig|response.?mime.?type|response.?format)", raw):
        return "unsupported_generation_config"
    if re.search(r"(token|context length|input too long|request too large)", raw):
        return "request_too_large"
    return "other_invalid_request"


def local_schema_probe(provider: str = "gemini") -> dict:
    """Serialize the exact live request schema, without a model or network call.

    Exercises the same schema/config objects the live call sends, then checks
    the serialized form for keywords the provider is known to reject, so a
    schema regression is caught before any paid request.
    """
    rejected = {
        "gemini": {"additional_properties", "min_length", "max_length",
                   "min_items", "minimum", "maximum", "title", "any_of"},
        "openai": {"minLength", "maxLength", "minItems",
                   "minimum", "maximum", "title", "default"},
    }[provider]
    try:
        if provider == "openai":
            serialized = openai_strict_schema()
            json.dumps(serialized)
        else:
            config = gemini_generation_config()
            serialized = config.model_dump(exclude_none=True, mode="json")
        offending = sorted(set(collect_keys(serialized)) & rejected)
        if offending:
            return {"status": "local_schema_error",
                    "local_schema_unsupported_keywords": offending}
        return {"status": "local_schema_serialized"}
    except Exception as error:
        return {"status": "local_schema_error", "error_type": type(error).__name__}


def safe_provider_error(error: Exception) -> dict:
    """Return allowlisted diagnostics, never provider text or request content."""
    info = {"error_type": type(error).__name__}
    raw_status = getattr(error, "status_code", None)
    if raw_status is None:
        raw_status = getattr(error, "code", None)
    if not isinstance(raw_status, int) or not 400 <= raw_status <= 599:
        return dict(info, error_category="unknown")

    info["http_status"] = raw_status
    categories = {
        401: "authentication",
        403: "permission_or_api_access",
        404: "model_not_found",
        429: "rate_limit_or_quota",
        408: "timeout",
        504: "timeout",
    }
    if raw_status == 400:
        category = classify_bad_request(error)
    elif raw_status >= 500:
        category = "provider_server_error"
    else:
        category = categories.get(raw_status, "other_http_error")
    return dict(info, error_category=category)


def evaluate(cases: list[dict], *, live: bool, max_calls: int, model: str,
             provider: str = "gemini", caller=None) -> list[dict]:
    if provider not in PROVIDERS:
        raise ValueError(f"Unknown provider {provider!r}")
    if max_calls < 0:
        raise ValueError("max_calls must not be negative")
    if live and max_calls < 1:
        raise ValueError("--live requires a positive --max-calls")
    if live and len(cases) > max_calls:
        raise ValueError("Too many cases for --max-calls; narrow the input")
    caller = caller or CALLERS[provider]
    results = []
    for case in cases:
        prompt = build_prompt(case["jd"], case["resume"])
        entry = {
            "case_id": case["case_id"],
            "provider": provider,
            "model": model,
            "prompt_version": PROMPT_VERSION,
            "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
            "original_score": case.get("original_score"),
            "status": "dry_run",
        }
        if live:
            try:
                payload = caller(prompt, model)
                decision = check_payload(payload, case["jd"], case["resume"], case)
                entry.update(
                    status="validated" if decision["validation"]["valid"] else "invalid",
                    model_score=decision["model_score"],
                    final_score=decision["final_score"],
                    recommendation=decision["recommendation"],
                    decision=decision,
                    ledger=payload,
                )
            except Exception as error:
                entry.update(status="error", **safe_provider_error(error))
                # Never persist provider message, prompt, resume, or API key.
        results.append(entry)
    return results


def main(default_provider: str | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sources = parser.add_mutually_exclusive_group(required=True)
    sources.add_argument("--input", type=Path)
    sources.add_argument("--from-db", nargs="+", choices=FROZEN_NAMES + ("all",),
                         help="Frozen case name(s), or 'all' for the five frozen cases")
    parser.add_argument("--provider", choices=tuple(PROVIDERS), default=default_provider,
                        required=default_provider is None)
    parser.add_argument("--database", type=Path, help="Override DuckDB path for --from-db")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", help="Defaults: gemini-3.5-flash / gpt-4.1")
    parser.add_argument("--check-schema", action="store_true", help="Run local schema serialization only, without API calls")
    parser.add_argument("--live", action="store_true", help="Explicitly authorize paid API calls")
    parser.add_argument("--max-calls", type=int, default=0)
    args = parser.parse_args()
    model = args.model or PROVIDERS[args.provider]["model"]
    destination = outside_repo(args.output)
    if args.input is not None:
        source = outside_repo(args.input)
        if source == destination:
            parser.error("Input and output paths must differ")
    if destination.exists():
        parser.error("Refusing to overwrite existing results")
    if args.from_db:
        from evaluation.v9_frozen_inputs import load_cases
        names = list(FROZEN_NAMES) if "all" in args.from_db else args.from_db
        cases = load_cases(names, **({"database": args.database} if args.database else {}))
    else:
        if args.database is not None:
            parser.error("--database requires --from-db")
        cases = read_cases(args.input)
    if args.live:
        # Match the application’s local .env configuration only on explicit live runs.
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
        key = PROVIDERS[args.provider]["key"]
        if not os.environ.get(key):
            parser.error(f"{key} is required for live calls")
    if args.check_schema:
        if args.live:
            parser.error("--check-schema cannot be combined with --live")
        results = [{"case_id": case["case_id"], **local_schema_probe(args.provider)} for case in cases]
    else:
        results = evaluate(cases, live=args.live, max_calls=args.max_calls,
                           model=model, provider=args.provider)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("x", encoding="utf-8") as file:
        json.dump(results, file, indent=2)
    print(f"{len(results)} case(s); provider={args.provider}; model={model}; "
          f"live={args.live}; output={destination}")
    for entry in results:
        if "recommendation" in entry:
            print(f"  {entry['case_id']}: model {entry['model_score']} -> final "
                  f"{entry['final_score']} {entry['recommendation']} ({entry['status']})")


if __name__ == "__main__":
    main()
