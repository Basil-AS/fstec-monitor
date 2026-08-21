from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class MessageKind(StrEnum):
    MENU = "menu"
    CONTEXT = "context"
    TEMPORARY = "temporary"
    PERSISTENT = "persistent"


@dataclass(frozen=True)
class MessageRef:
    chat_id: int
    message_id: int
    kind: MessageKind = MessageKind.CONTEXT


@dataclass(frozen=True)
class Pagination:
    page: int
    pages: int

    def __post_init__(self) -> None:
        if self.pages < 1 or not 1 <= self.page <= self.pages:
            raise ValueError("page must be within 1..pages")


@dataclass(frozen=True)
class ViewModel:
    screen: str
    text: str
    reply_markup: dict[str, Any] | None = None
    payload: Any = None
    generation: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
