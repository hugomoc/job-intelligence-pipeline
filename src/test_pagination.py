from src.ui.pagination import page_count, paginate_items


def test_541_items_at_25_per_page_is_22_pages() -> None:
    assert page_count(541, 25) == 22


def test_first_page_returns_first_25_items() -> None:
    window = paginate_items(list(range(1, 542)), page=1, page_size=25)

    assert window.total_pages == 22
    assert window.start_number == 1
    assert window.end_number == 25
    assert window.items == list(range(1, 26))


def test_final_page_returns_remaining_items() -> None:
    window = paginate_items(list(range(1, 542)), page=22, page_size=25)

    assert window.start_number == 526
    assert window.end_number == 541
    assert window.items == list(range(526, 542))


def test_invalid_page_is_clamped_safely() -> None:
    below_range = paginate_items(list(range(1, 51)), page=-10, page_size=25)
    above_range = paginate_items(list(range(1, 51)), page=99, page_size=25)

    assert below_range.current_page == 1
    assert below_range.items == list(range(1, 26))
    assert above_range.current_page == 2
    assert above_range.items == list(range(26, 51))


def test_page_size_changes_page_count() -> None:
    assert page_count(541, 50) == 11
    assert page_count(541, 100) == 6


def test_empty_result_has_safe_empty_page_state() -> None:
    window = paginate_items([], page=3, page_size=25)

    assert window.current_page == 1
    assert window.total_pages == 0
    assert window.start_number == 0
    assert window.end_number == 0
    assert window.items == []


def main() -> None:
    test_541_items_at_25_per_page_is_22_pages()
    test_first_page_returns_first_25_items()
    test_final_page_returns_remaining_items()
    test_invalid_page_is_clamped_safely()
    test_page_size_changes_page_count()
    test_empty_result_has_safe_empty_page_state()
    print("Pagination tests passed.")


if __name__ == "__main__":
    main()
