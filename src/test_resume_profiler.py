import argparse
import json
from pathlib import Path

from src.ai.resume_profiler import (
    ResumeProfilerError,
    profile_resume,
    redact_personal_information,
)
from src.resume.extractor import (
    ResumeExtractionError,
    extract_resume_file,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Create an AI profile from a resume."
        )
    )

    parser.add_argument(
        "resume_path",
        type=Path,
        help="Path to a PDF or DOCX resume.",
    )

    parser.add_argument(
        "--show-redacted",
        action="store_true",
        help=(
            "Display the resume text after "
            "contact information is removed."
        ),
    )

    arguments = parser.parse_args()

    try:
        resume = extract_resume_file(
            arguments.resume_path
        )

        if arguments.show_redacted:
            print("\nRedacted resume:")
            print("-" * 60)
            print(
                redact_personal_information(
                    resume.text
                )
            )
            print("-" * 60)

        print("\nCreating AI candidate profile...")

        result = profile_resume(resume)

    except (
        ResumeExtractionError,
        ResumeProfilerError,
    ) as error:
        print(f"Profile creation failed: {error}")
        raise SystemExit(1) from error

    print("\nResume profile created")
    print(f"Filename: {result.filename}")
    print(
        "Resume hash: "
        f"{result.resume_hash[:16]}..."
    )
    print(f"Model: {result.model_name}")

    print("\nCandidate profile:")
    print("-" * 60)

    profile_json = (
        result.profile.model_dump_json(
            indent=2
        )
    )

    # Parse and print again so Unicode is displayed
    # normally instead of escaped.
    parsed_profile = json.loads(
        profile_json
    )

    print(
        json.dumps(
            parsed_profile,
            indent=2,
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()