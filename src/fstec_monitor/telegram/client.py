from __future__ import annotations

import ipaddress
from typing import Any, ClassVar, Self
from urllib.parse import urlsplit

from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.exceptions import TelegramAPIError, TelegramNetworkError, TelegramRetryAfter
from aiogram.methods.base import TelegramMethod
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeChat,
    InlineKeyboardMarkup,
    MenuButtonCommands,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from aiogram.types.input_file import BufferedInputFile


class UnsupportedTelegramMethod(RuntimeError):
    """The configured Bot API server explicitly does not implement a method."""


class _RichMethod(TelegramMethod[dict[str, Any]]):
    chat_id: int
    rich_message: dict[str, Any]
    message_id: int | None = None
    reply_markup: InlineKeyboardMarkup | None = None
    link_preview_options: dict[str, Any] | None = None

    @property
    def __returning__(self) -> type[dict[str, Any]]:
        return dict


class SendRichMessage(_RichMethod):
    __api_method__: ClassVar[str] = "sendRichMessage"


class EditRichMessage(_RichMethod):
    __api_method__: ClassVar[str] = "editMessageText"


class TelegramAPI:
    """Typed aiogram transport shared by bot handlers and notification delivery."""

    def __init__(self, token: str, api_root: str, *, bot: Bot | None = None) -> None:
        self.token = token
        self.api_root = api_root
        self.bot = bot or create_bot(token, api_root)
        self.rich_supported: dict[str, bool] = {}

    async def call(self, method: str, payload: dict[str, Any]) -> Any:
        values = dict(payload)
        if "reply_markup" in values:
            values["reply_markup"] = self._markup(values["reply_markup"])
        try:
            if method in {"sendRichMessage", "editMessageText"} and "rich_message" in values:
                request = (SendRichMessage if method == "sendRichMessage" else EditRichMessage)(**values)
                return self._legacy_result(await self.bot(request))
            if method == "sendMessage":
                return self._legacy_result(await self.bot.send_message(**values))
            if method == "sendChatAction":
                return await self.bot.send_chat_action(**values)
            if method == "editMessageReplyMarkup":
                return await self.bot.edit_message_reply_markup(**values)
            if method == "editMessageText":
                return await self.bot.edit_message_text(**values)
            if method == "deleteMessage":
                return await self.bot.delete_message(**values)
            if method == "answerCallbackQuery":
                return await self.bot.answer_callback_query(**values)
            if method == "sendDocument":
                document = values.pop("document")
                if isinstance(document, bytes):
                    values["document"] = BufferedInputFile(document, filename=values.pop("filename", "report.bin"))
                return await self.bot.send_document(**values)
            if method == "setMyCommands":
                commands = [BotCommand.model_validate(command) for command in values.pop("commands")]
                scope = self._command_scope(values.pop("scope", None))
                return await self.bot.set_my_commands(commands, scope=scope, **values)
            if method == "setChatMenuButton":
                menu_button = values.get("menu_button")
                if menu_button and menu_button.get("type") == "commands":
                    values["menu_button"] = MenuButtonCommands()
                return await self.bot.set_chat_menu_button(**values)
            raise ValueError(f"Unsupported Telegram API operation: {method}")
        except TelegramRetryAfter:
            raise
        except TelegramAPIError as exc:
            if self._is_unsupported(exc):
                raise UnsupportedTelegramMethod(f"Telegram API method is unsupported: {method}") from None
            raise self._safe_api_error(method, exc) from None
        except TelegramNetworkError as exc:
            raise RuntimeError(f"Telegram API transport failed ({type(exc).__name__})") from None

    async def call_rich(
        self,
        method: str,
        payload: dict[str, Any],
        fallback_payload: dict[str, Any],
        *,
        fallback_method: str | None = None,
    ) -> Any:
        fallback_method = fallback_method or method
        if self.rich_supported.get(method) is False:
            return await self.call(fallback_method, fallback_payload)
        try:
            result = await self.call(method, payload)
        except UnsupportedTelegramMethod:
            self.rich_supported[method] = False
            return await self.call(fallback_method, fallback_payload)
        self.rich_supported[method] = True
        return result

    async def send_rich(
        self,
        chat_id: int,
        markdown: str,
        reply_markup: dict[str, Any] | None = None,
        *,
        fallback_html: str | None = None,
    ) -> Any:
        from .rendering import markdown_to_telegram_html

        rich: dict[str, Any] = {
            "chat_id": chat_id,
            "rich_message": {"markdown": markdown},
            "link_preview_options": {"is_disabled": True},
        }
        fallback: dict[str, Any] = {
            "chat_id": chat_id,
            "text": fallback_html if fallback_html is not None else markdown_to_telegram_html(markdown),
            "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True},
        }
        if reply_markup is not None:
            rich["reply_markup"] = fallback["reply_markup"] = reply_markup
        return await self.call_rich("sendRichMessage", rich, fallback, fallback_method="sendMessage")

    async def send_document(self, chat_id: int, name: str, data: bytes, caption: str = "") -> Any:
        content_type = "text/markdown" if name.endswith(".md") else "application/octet-stream"
        return await self.bot.send_document(
            chat_id=chat_id,
            document=BufferedInputFile(data, filename=name),
            caption=caption[:900],
            disable_content_type_detection=content_type != "text/markdown",
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_args: object) -> bool:
        # The client is process-shared; individual notification batches do not close it.
        return False

    async def close(self) -> None:
        await self.bot.session.close()

    @staticmethod
    def _legacy_result(value: Any) -> Any:
        """Preserve the dict-shaped result contract used by existing handlers."""
        model_dump = getattr(value, "model_dump", None)
        if callable(model_dump):
            return model_dump(mode="python", exclude_none=True)
        message_id = getattr(value, "message_id", None)
        if message_id is not None:
            return {"message_id": message_id}
        return value

    @staticmethod
    def _markup(value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        if "inline_keyboard" in value:
            return InlineKeyboardMarkup.model_validate(value)
        if "keyboard" in value:
            return ReplyKeyboardMarkup.model_validate(value)
        if "remove_keyboard" in value:
            return ReplyKeyboardRemove.model_validate(value)
        return value

    @staticmethod
    def _command_scope(value: dict[str, Any] | None) -> Any:
        if value is None or value.get("type") == "default":
            return None
        if value.get("type") == "all_private_chats":
            return BotCommandScopeAllPrivateChats()
        if value.get("type") == "chat":
            return BotCommandScopeChat(chat_id=value["chat_id"])
        return value

    @staticmethod
    def _is_unsupported(exc: BaseException) -> bool:
        message = str(exc).casefold()
        method_errors = ("method not found", "method is not supported", "unknown method")
        rich_payload_errors = (
            "can't parse method parameter: rich_message",
            "can't parse rich_message",
            "unsupported field: rich_message",
            "unknown field: rich_message",
            "field rich_message is not supported",
        )
        return any(phrase in message for phrase in (*method_errors, *rich_payload_errors))

    def _safe_api_error(self, method: str, exc: BaseException) -> RuntimeError:
        message = str(exc).replace(self.token, "[REDACTED]")[:300]
        return RuntimeError(f"Telegram API error in {method}: {message}")


def create_bot(token: str, api_root: str) -> Bot:
    parsed = urlsplit(api_root)
    host = (parsed.hostname or "").casefold()
    try:
        port = parsed.port
    except ValueError:
        port = None
    try:
        loopback = host == "localhost" or ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = False

    local_root = loopback and parsed.path in {"", "/"}
    teya_network_root = (
        host == "telegram-bot-api"
        and parsed.scheme == "http"
        and port == 8081
        and parsed.path in {"", "/"}
    )
    if (
        parsed.scheme not in {"http", "https"}
        or not (local_root or teya_network_root)
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "Telegram API root must point to loopback or the Teya internal Bot API endpoint"
        )
    api = TelegramAPIServer.from_base(api_root, is_local=True)
    session = AiohttpSession(api=api)
    return Bot(token=token, session=session)


_shared_api: TelegramAPI | None = None


def get_telegram_api(token: str | None = None, api_root: str | None = None) -> TelegramAPI:
    global _shared_api
    if _shared_api is None:
        if not token or not api_root:
            from ..config import settings

            token = token or settings.telegram_bot_token
            api_root = api_root or settings.telegram_api_root
        if not token:
            raise RuntimeError("FSTEC_TELEGRAM_BOT_TOKEN is required")
        _shared_api = TelegramAPI(token, api_root)
    return _shared_api


async def close_telegram_api() -> None:
    global _shared_api
    api, _shared_api = _shared_api, None
    if api is not None:
        await api.close()
