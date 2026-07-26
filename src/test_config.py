from src.config_loader import load_searches, load_sources


def main() -> None:
    sources = load_sources()
    defaults, searches = load_searches()

    print("Enabled sources:")
    for source in sources:
        print(
            f"- {source['source_name']} "
            f"({source['ingestion_method']})"
        )

    print("\nDefaults:")
    print(defaults)

    print("\nEnabled searches:")
    for search in searches:
        print(
            f"- {search['search_id']}: "
            f"{search['title']} — {search['location']}"
        )


if __name__ == "__main__":
    main()