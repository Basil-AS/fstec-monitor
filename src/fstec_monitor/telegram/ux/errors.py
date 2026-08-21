from __future__ import annotations

from enum import StrEnum


class TelegramErrorKind(StrEnum):
    NOT_MODIFIED = "not_modified"
    MISSING = "missing"
    NOT_EDITABLE = "not_editable"
    TIMEOUT = "timeout"
    OTHER = "other"


def classify_telegram_error(error: BaseException) -> TelegramErrorKind:
    message = str(error).casefold()
    if "message is not modified" in message:
        return TelegramErrorKind.NOT_MODIFIED
    if "message to edit not found" in message or "message not found" in message:
        return TelegramErrorKind.MISSING
    if "can't be edited" in message or "cannot be edited" in message:
        return TelegramErrorKind.NOT_EDITABLE
    if isinstance(error, TimeoutError) or "timeout" in message:
        return TelegramErrorKind.TIMEOUT
    return TelegramErrorKind.OTHER


def is_recoverable_edit_error(error: BaseException) -> bool:
    return classify_telegram_error(error) in {
        TelegramErrorKind.NOT_MODIFIED,
        TelegramErrorKind.MISSING,
        TelegramErrorKind.NOT_EDITABLE,
    }
