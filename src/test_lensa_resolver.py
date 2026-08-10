from __future__ import annotations

import argparse
import asyncio

from src.resolvers.lensa_resolver import resolve_lensa_job


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Resolve one Lensa alert URL into the underlying "
            "JobLeads posting."
        )
    )
    parser.add_argument("--url", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--company", required=True)
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Run Chromium visibly for debugging.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print the extracted description.",
    )

    return parser.parse_args()


async def main_async() -> None:
    args = parse_args()

    print("Resolving:")
    print(f"Title: {args.title}")
    print(f"Company: {args.company}")
    print()

    result = await resolve_lensa_job(
        url=args.url,
        title=args.title,
        company=args.company,
        headless=not args.headed,
    )

    print()
    print("Result:")

    if result is None:
        print("No reliable matching Lensa job was found.")
        return

    print(f"Title: {result.title}")
    print(f"Company: {result.company}")
    print(f"Location: {result.location}")
    print(f"Salary: {result.salary}")
    print(f"Employment type: {result.employment_type}")
    print(f"Extraction method: {result.extraction_method}")
    print(f"Candidate cards: {result.candidate_count}")
    print(
        "Description length: "
        f"{len(result.description or '')}"
    )
    print(f"Resolved URL: {result.resolved_url}")

    if args.verbose and result.description:
        print()
        print(result.description)


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
