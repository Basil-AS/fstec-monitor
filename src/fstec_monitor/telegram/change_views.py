"""Visible change grouping and pagination for the Telegram changes screen."""

from __future__ import annotations

from collections import defaultdict
from typing import Any


def _attachment_family(event: Any) -> str:
    title = str(event.summary or "").partition(":")[2].strip().casefold()
    for suffix in (".odt", ".pdf"):
        if title.endswith(suffix):
            title = title[: -len(suffix)].rstrip(" ._-–—")
            break
    return title or str(event.details or "").splitlines()[0].casefold()


def visible_change_groups(events: list[Any], documents: dict[int, Any]) -> list[tuple[str, int | None, list[Any]]]:
    """Return the user-visible category/document groups in stable screen order.

    Initial attachment events and duplicate PDF/ODT variants are removed before
    pagination, so every page contains complete visible document groups.
    """
    grouped: dict[tuple[str, int | None], list[Any]] = defaultdict(list)
    for event in events:
        document = documents.get(event.document_id) if event.document_id else None
        category = getattr(document, "category", "") or "Без категории"
        grouped[(category, event.document_id)].append(event)

    result: list[tuple[str, int | None, list[Any]]] = []
    for (category, document_id), group in grouped.items():
        has_document_added = any(event.kind == "document_added" for event in group)
        visible = [event for event in group if not (has_document_added and event.kind == "attachment_added")]
        attachment_events: dict[str, Any] = {}
        compact: list[Any] = []
        for event in visible:
            if event.kind != "attachment_added":
                compact.append(event)
                continue
            family = _attachment_family(event)
            previous = attachment_events.get(family)
            if previous is None or ".odt" in str(event.details).casefold():
                attachment_events[family] = event
        visible = [*compact, *attachment_events.values()]
        if visible:
            visible.sort(key=lambda event: (event.kind != "document_added", event.id or 0))
            result.append((category, document_id, visible))

    return sorted(
        result,
        key=lambda item: (item[0].casefold(), -max((event.id or 0) for event in item[2])),
    )


def paginate_change_groups(
    groups: list[tuple[str, int | None, list[Any]]], page: int, page_size: int
) -> tuple[list[tuple[str, int | None, list[Any]]], int, int]:
    page_size = max(1, min(page_size, 10))
    pages = max(1, (len(groups) + page_size - 1) // page_size)
    page = max(0, min(page, pages - 1))
    start = page * page_size
    return groups[start : start + page_size], page, pages
