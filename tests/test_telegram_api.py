import asyncio
from types import SimpleNamespace

import pytest

from fstec_monitor.telegram.client import TelegramAPI, UnsupportedTelegramMethod, create_bot


class _FakeSession:
    def __init__(self):
        self.calls = []

    async def make_request(self, bot, method):
        self.calls.append((bot, method))
        if method.__api_method__ == "sendRichMessage":
            raise UnsupportedTelegramMethod("Telegram API method is not supported")
        return {"message_id": 31}


class _FakeBot:
    def __init__(self):
        self.session = _FakeSession()
        self.calls = []

    async def __call__(self, method):
        return await self.session.make_request(self, method)

    async def send_message(self, **kwargs):
        self.calls.append(("send_message", kwargs))
        return SimpleNamespace(message_id=31)

    async def edit_message_text(self, **kwargs):
        self.calls.append(("edit_message_text", kwargs))
        return True

    async def send_document(self, **kwargs):
        self.calls.append(("send_document", kwargs))
        return True

    async def delete_message(self, **kwargs):
        self.calls.append(("delete_message", kwargs))
        return True

    async def answer_callback_query(self, **kwargs):
        self.calls.append(("answer_callback_query", kwargs))
        return True

    async def set_my_commands(self, **kwargs):
        self.calls.append(("set_my_commands", kwargs))
        return True

    async def set_chat_menu_button(self, **kwargs):
        self.calls.append(("set_chat_menu_button", kwargs))
        return True



def test_create_bot_uses_loopback_api_server(monkeypatch):
    calls = {}

    class FakeSession:
        def __init__(self, *, api):
            calls["api"] = api

    class FakeServer:
        @classmethod
        def from_base(cls, base, *, is_local):
            calls["base"] = base
            calls["is_local"] = is_local
            return "local-server"

    class FakeBot:
        def __init__(self, token, *, session):
            calls["token"] = token
            calls["session"] = session

    monkeypatch.setattr("fstec_monitor.telegram.client.AiohttpSession", FakeSession)
    monkeypatch.setattr("fstec_monitor.telegram.client.TelegramAPIServer", FakeServer)
    monkeypatch.setattr("fstec_monitor.telegram.client.Bot", FakeBot)

    bot = create_bot("test-token", "http://127.0.0.1:18081")

    assert isinstance(bot, FakeBot)
    assert calls["base"] == "http://127.0.0.1:18081"
    assert calls["is_local"] is True
    assert calls["api"] == "local-server"
    assert calls["token"] == "test-token"
    assert isinstance(calls["session"], FakeSession)


def test_create_bot_rejects_remote_api_roots():
    with pytest.raises(ValueError):
        create_bot("test-token", "https://api.telegram.org")


def test_create_bot_accepts_exact_teya_internal_api_endpoint(monkeypatch):
    calls = {}

    class FakeSession:
        def __init__(self, *, api):
            calls["api"] = api

    class FakeServer:
        @classmethod
        def from_base(cls, base, *, is_local):
            calls["base"] = base
            calls["is_local"] = is_local
            return "internal-server"

    class FakeBot:
        def __init__(self, token, *, session):
            calls["token"] = token
            calls["session"] = session

    monkeypatch.setattr("fstec_monitor.telegram.client.AiohttpSession", FakeSession)
    monkeypatch.setattr("fstec_monitor.telegram.client.TelegramAPIServer", FakeServer)
    monkeypatch.setattr("fstec_monitor.telegram.client.Bot", FakeBot)

    bot = create_bot("test-token", "http://telegram-bot-api:8081")

    assert isinstance(bot, FakeBot)
    assert calls["base"] == "http://telegram-bot-api:8081"
    assert calls["is_local"] is True
    assert calls["api"] == "internal-server"
    assert isinstance(calls["session"], FakeSession)


@pytest.mark.parametrize(
    "api_root",
    [
        "http://telegram-bot-api:8082",
        "http://other-bot-api:8081",
        "http://example.test:8081",
        "http://telegram-bot-api:8081/bot",
        "http://telegram-bot-api:8081/?query=1",
    ],
)
def test_create_bot_rejects_noncanonical_internal_api_roots(api_root):
    with pytest.raises(ValueError):
        create_bot("test-token", api_root)



def test_typed_send_edit_markup_and_document_upload():
    async def scenario():
        bot = _FakeBot()
        api = TelegramAPI("test-token", "http://127.0.0.1:18081", bot=bot)
        markup = {"inline_keyboard": [[{"text": "Open", "callback_data": "menu:panel"}]]}

        await api.call("sendMessage", {"chat_id": 7, "text": "hello", "reply_markup": markup})
        await api.call("editMessageText", {
            "chat_id": 7,
            "message_id": 31,
            "text": "updated",
            "reply_markup": markup,
        })
        await api.send_document(7, "report.md", b"# report", "Report")
        await api.call("sendMessage", {"chat_id": 7, "text": "access", "reply_markup": {"remove_keyboard": True}})
        await api.call("answerCallbackQuery", {"callback_query_id": "cb-1"})

        assert [name for name, _ in bot.calls] == [
            "send_message", "edit_message_text", "send_document", "send_message", "answer_callback_query"
        ]
        sent_markup = bot.calls[0][1]["reply_markup"]
        assert sent_markup.inline_keyboard[0][0].callback_data == "menu:panel"
        assert bot.calls[1][1]["message_id"] == 31
        assert bot.calls[1][1]["reply_markup"].inline_keyboard[0][0].text == "Open"
        assert bot.calls[3][1]["reply_markup"].remove_keyboard is True
        upload = bot.calls[2][1]["document"]
        assert upload.filename == "report.md"
        assert upload.data == b"# report"

    asyncio.run(scenario())



def test_plain_send_result_keeps_legacy_message_id_contract():
    async def scenario():
        bot = _FakeBot()
        api = TelegramAPI("test-token", "http://127.0.0.1:18081", bot=bot)
        result = await api.call("sendMessage", {"chat_id": 7, "text": "plain"})
        assert isinstance(result, dict)
        assert result["message_id"] == 31

    asyncio.run(scenario())


def test_rich_fallback_requires_explicit_unsupported_error_and_happens_once():
    async def scenario():
        bot = _FakeBot()
        api = TelegramAPI("test-token", "http://127.0.0.1:18081", bot=bot)
        await api.send_rich(7, "# status", {"inline_keyboard": []})
        assert len(bot.session.calls) == 1
        assert len(bot.calls) == 1
        assert bot.calls[0][0] == "send_message"
        assert bot.calls[0][1]["reply_markup"].inline_keyboard == []

        class TimeoutSession(_FakeSession):
            async def make_request(self, bot, method):
                self.calls.append((bot, method))
                raise TimeoutError("request timed out")

        timeout_bot = _FakeBot()
        timeout_bot.session = TimeoutSession()
        timeout_api = TelegramAPI("test-token", "http://127.0.0.1:18081", bot=timeout_bot)
        with pytest.raises(TimeoutError):
            await timeout_api.send_rich(7, "# status")
        assert len(timeout_bot.session.calls) == 1
        assert timeout_bot.calls == []

        from aiogram.exceptions import TelegramNotFound

        class MissingMessageSession(_FakeSession):
            async def make_request(self, bot, method):
                self.calls.append((bot, method))
                raise TelegramNotFound(method, "Bad Request: message to edit not found")

        missing_bot = _FakeBot()
        missing_bot.session = MissingMessageSession()
        missing_api = TelegramAPI("test-token", "http://127.0.0.1:18081", bot=missing_bot)
        with pytest.raises(RuntimeError, match="message to edit not found"):
            await missing_api.call_rich(
                "editMessageText",
                {"chat_id": 7, "message_id": 9, "rich_message": {"markdown": "# status"}},
                {"chat_id": 7, "message_id": 9, "text": "status"},
            )
        assert len(missing_bot.session.calls) == 1
        assert missing_bot.calls == []

    asyncio.run(scenario())


def test_rich_edit_rejects_unsupported_payload_but_not_missing_message():
    async def scenario():
        from aiogram.exceptions import TelegramBadRequest

        class OldServerSession(_FakeSession):
            async def make_request(self, bot, method):
                self.calls.append((bot, method))
                if method.__api_method__ == "editMessageText" and hasattr(method, "rich_message"):
                    raise TelegramBadRequest(method, "Bad Request: can't parse method parameter: rich_message")
                return True

        bot = _FakeBot()
        bot.session = OldServerSession()
        api = TelegramAPI("test-token", "http://127.0.0.1:18081", bot=bot)
        result = await api.call_rich(
            "editMessageText",
            {
                "chat_id": 7,
                "message_id": 31,
                "rich_message": {"markdown": "# Heading"},
                "reply_markup": {"inline_keyboard": []},
            },
            {
                "chat_id": 7,
                "message_id": 31,
                "text": "<b>Heading</b>",
                "parse_mode": "HTML",
                "reply_markup": {"inline_keyboard": []},
            },
        )
        assert result is True
        assert len(bot.session.calls) == 1
        assert bot.calls == [("edit_message_text", {
            "chat_id": 7,
            "message_id": 31,
            "text": "<b>Heading</b>",
            "parse_mode": "HTML",
            "reply_markup": bot.calls[0][1]["reply_markup"],
        })]
        assert api.rich_supported["editMessageText"] is False
        assert await api.send_rich(7, "# send still supported") is True
        assert len(bot.session.calls) == 2
        assert bot.session.calls[1][1].__api_method__ == "sendRichMessage"
        assert api.rich_supported["sendRichMessage"] is True

        timeout_bot = _FakeBot()

        class TimeoutSession(_FakeSession):
            async def make_request(self, bot, method):
                self.calls.append((bot, method))
                raise TimeoutError("unknown outcome")

        timeout_bot.session = TimeoutSession()
        timeout_api = TelegramAPI("test-token", "http://127.0.0.1:18081", bot=timeout_bot)
        with pytest.raises(TimeoutError, match="unknown outcome"):
            await timeout_api.call_rich(
                "editMessageText",
                {"chat_id": 7, "message_id": 31, "rich_message": {"markdown": "# Heading"}},
                {"chat_id": 7, "message_id": 31, "text": "<b>Heading</b>"},
            )
        assert timeout_bot.calls == []

    asyncio.run(scenario())


def test_nested_incoming_rich_text_update_validation_is_bounded():
    from aiogram.types import Update

    node = "leaf"
    for kind in ("spoiler", "bold", "subscript", "italic", "underline", "strikethrough", "spoiler"):
        node = {"type": kind, "text": node}
    update = Update.model_validate(
        {
            "update_id": 1,
            "message": {
                "message_id": 1,
                "date": 1,
                "chat": {"id": 1, "type": "private"},
                "rich_message": {
                    "blocks": [
                        {
                            "type": "details",
                            "summary": "",
                            "blocks": [{"type": "paragraph", "text": node}],
                        }
                    ]
                },
            },
        }
    )

    assert update.message is not None
    assert update.message.rich_message is not None
