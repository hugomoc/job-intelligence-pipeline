"""Backward-compatible entry point: the v9 harness with ``--provider gemini``.

All logic lives in ``evaluation.run_v9`` so Gemini and OpenAI share one prompt,
schema, validator and decision policy.
"""
from evaluation.run_v9 import (  # noqa: F401
    ROOT, build_prompt, call_gemini, check_payload, classify_bad_request,
    evaluate, local_schema_probe, main as _main, outside_repo, read_cases,
    safe_provider_error,
)


def main() -> None:
    _main(default_provider="gemini")


if __name__ == "__main__":
    main()
