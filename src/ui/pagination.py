"""Small pagination helpers shared by the Streamlit UI and tests."""

from __future__ import annotations

from dataclasses import dataclass
from math import ceil
from typing import Sequence, TypeVar


T = TypeVar("T")

PAGE_SIZE_OPTIONS = (25, 50, 100)


@dataclass(frozen=True)
class PaginationWindow[T]:
    """A safely clamped page slice and display metadata."""

    items: list[T]
    current_page: int
    page_size: int
    total_items: int
    total_pages: int
    start_number: int
    end_number: int


def normalize_page_size(page_size: int) -> int:
    """Use the closest supported page size without allowing an unbounded page."""
    if page_size in PAGE_SIZE_OPTIONS:
        return page_size

    return PAGE_SIZE_OPTIONS[0]


def page_count(total_items: int, page_size: int) -> int:
    """Return the number of pages for a result set."""
    page_size = normalize_page_size(page_size)

    if total_items <= 0:
        return 0

    return ceil(total_items / page_size)


def clamp_page(page: int, total_items: int, page_size: int) -> int:
    """Clamp a requested page into the valid range, keeping empty results safe."""
    total_pages = page_count(total_items, page_size)

    if total_pages == 0:
        return 1

    return max(1, min(page, total_pages))


def paginate_items(
    items: Sequence[T],
    page: int,
    page_size: int,
) -> PaginationWindow[T]:
    """Return only the items for the current page plus UI display metadata."""
    total_items = len(items)
    page_size = normalize_page_size(page_size)
    total_pages = page_count(total_items, page_size)
    current_page = clamp_page(page, total_items, page_size)

    if total_items == 0:
        return PaginationWindow(
            items=[],
            current_page=current_page,
            page_size=page_size,
            total_items=0,
            total_pages=0,
            start_number=0,
            end_number=0,
        )

    start_index = (current_page - 1) * page_size
    end_index = min(start_index + page_size, total_items)

    return PaginationWindow(
        items=list(items[start_index:end_index]),
        current_page=current_page,
        page_size=page_size,
        total_items=total_items,
        total_pages=total_pages,
        start_number=start_index + 1,
        end_number=end_index,
    )
