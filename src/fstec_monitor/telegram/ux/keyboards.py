from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from .callbacks import CallbackCodec
from .models import Pagination


def pagination_row(pagination: Pagination, codec: CallbackCodec, action: str = "page") -> list[dict[str, Any]]:
    return [
        {"text": "◀️", "callback_data": codec.encode(action, pagination.page - 1)} if pagination.page > 1 else {"text": "·", "callback_data": codec.encode("noop")},
        {"text": f"{pagination.page}/{pagination.pages}", "callback_data": codec.encode("noop")},
        {"text": "▶️", "callback_data": codec.encode(action, pagination.page + 1)} if pagination.page < pagination.pages else {"text": "·", "callback_data": codec.encode("noop")},
    ]


def navigation_row(codec: CallbackCodec, *, back: bool = True, home: bool = True, refresh: bool = False) -> list[dict[str, Any]]:
    row: list[dict[str, Any]] = []
    if back:
        row.append({"text": "← Назад", "callback_data": codec.encode("back")})
    if home:
        row.append({"text": "🏠 Меню", "callback_data": codec.encode("home")})
    if refresh:
        row.append({"text": "🔄 Обновить", "callback_data": codec.encode("refresh")})
    return row


def result_actions(codec: CallbackCodec, actions: Iterable[tuple[str, str]]) -> list[list[dict[str, Any]]]:
    return [[{"text": label, "callback_data": codec.encode(action)} for label, action in actions]]


def with_navigation(
    rows: Sequence[Sequence[dict[str, Any]]],
    codec: CallbackCodec,
    *,
    back: bool = True,
    home: bool = True,
    refresh: bool = False,
) -> dict[str, Any]:
    return {"inline_keyboard": [list(row) for row in rows] + [navigation_row(codec, back=back, home=home, refresh=refresh)]}
