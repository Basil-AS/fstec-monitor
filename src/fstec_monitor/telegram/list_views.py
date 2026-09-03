"""Small, side-effect-free helpers for Telegram list screens."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TypeVar

T = TypeVar("T")


def paginate_lines(
    items: Sequence[T], page: int = 0, page_size: int = 5
) -> tuple[list[T], int, int]:
    """Return a bounded page, its zero-based index, and total page count."""
    size = max(1, min(int(page_size), 10))
    total = max(1, (len(items) + size - 1) // size)
    current = max(0, min(int(page), total - 1))
    start = current * size
    return list(items[start : start + size]), current, total


def pagination_row(page: int, pages: int, callback_for_page) -> list[dict[str, str]]:
    """Build a compact cyclic pager row without embedding domain logic."""
    previous = pages - 1 if page <= 0 else page - 1
    following = 0 if page + 1 >= pages else page + 1
    return [
        {"text": "◀️", "callback_data": callback_for_page(previous)},
        {"text": f"{page + 1}/{pages}", "callback_data": "v1:nav:noop"},
        {"text": "▶️", "callback_data": callback_for_page(following)},
    ]
