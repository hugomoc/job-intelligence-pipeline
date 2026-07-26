import argparse
from pathlib import Path

from src.resume.extractor import (
    ResumeExtractionError,
    extract_resume_file,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Extract text from a PDF or DOCX resume."
        )
    )

    parser.add_argument(
        "resume_path",
        type=Path,
        help="Path to a PDF or DOCX resume.",
    )

    parser.add_argument(
        "--show-text",
        action="store_true",
        help="Print the complete extracted resume text.",
    )

    arguments = parser.parse_args()

    try:
        resume = extract_resume_file(
            arguments.resume_path
        )
    except ResumeExtractionError as error:
        print(f"Resume extraction failed: {error}")
        raise SystemExit(1) from error

    print("Resume extraction successful")
    print(f"Filename: {resume.filename}")
    print(f"Format: {resume.file_extension}")
    print(
        f"Size: {resume.file_size_bytes:,} bytes"
    )
    print(
        "Pages: "
        f"{resume.page_count or 'Not available'}"
    )
    print(f"Words: {resume.word_count}")
    print(
        "Resume hash: "
        f"{resume.resume_hash[:16]}..."
    )

    if arguments.show_text:
        print("\nExtracted text:")
        print("-" * 60)
        print(resume.text)

    else:
        preview = resume.text[:1_000]

        print("\nText preview:")
        print("-" * 60)
        print(preview)

        if len(resume.text) > 1_000:
            print("\n[Preview truncated]")


if __name__ == "__main__":
    main()