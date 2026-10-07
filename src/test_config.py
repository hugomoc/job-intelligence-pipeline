from src.config_loader import load_searches, load_sources


def main() -> None:
    sources = load_sources()
    defaults, searches = load_searches()

    print("Enabled sources:")
    for source in sources:
        assert source.get("source_id")
        assert source.get("source_name")
        assert source.get("mailbox_folder")

        print(
            f"- {source['source_name']} "
            f"({source['mailbox_folder']})"
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
