from __future__ import annotations

import asyncio
import logging

from fstec_monitor.telegram_bot import TelegramBot


def test_cli_suppresses_httpx_request_urls(monkeypatch) -> None:
    import logging

    monkeypatch.setattr(logging.getLogger("httpx"), "level", logging.NOTSET)
    import fstec_monitor.cli  # noqa: F401

    assert logging.getLogger("httpx").level >= logging.WARNING


def test_send_uses_html_only_for_explicit_markup_and_logs_metadata(monkeypatch, caplog) -> None:
    bot = TelegramBot.__new__(TelegramBot)
    calls = []

    async def call(method, payload):
        calls.append((method, payload))
        return {"message_id": 41}

    bot.call = call
    monkeypatch.setenv("FSTEC_TGUX_LOGGING", "1")
    with caplog.at_level(logging.INFO):
        message_id = asyncio.run(bot.send(7, "<b>Меню</b>", {"inline_keyboard": []}, screen="main"))

    assert message_id == 41
    assert calls[0][0] == "sendMessage"
    assert calls[0][1]["parse_mode"] == "HTML"
    assert "TGUX chat=7 method=sendMessage message_id=- screen=main reason=navigation" in caplog.text
    assert "token" not in caplog.text.casefold()


def test_chat_action_and_callback_toast_are_recorded_as_metadata(monkeypatch, caplog) -> None:
    bot = TelegramBot.__new__(TelegramBot)
    calls = []

    async def call(method, payload):
        calls.append((method, payload))
        return {}

    bot.call = call
    monkeypatch.setenv("FSTEC_TGUX_LOGGING", "1")

    async def scenario():
        await bot.send_chat_action(7, "upload_document")
        await bot.answer_callback("callback-1", "Сохранено", chat_id=7)

    with caplog.at_level("INFO"):
        asyncio.run(scenario())

    assert calls == [
        ("sendChatAction", {"chat_id": 7, "action": "upload_document"}),
        ("answerCallbackQuery", {"callback_query_id": "callback-1", "text": "Сохранено"}),
    ]
    assert "TGUX chat=7 method=answerCallbackQuery message_id=- screen=- reason=callback-toast" in caplog.text


def test_markup_cleanup_has_a_dedicated_bot_api_method() -> None:
    bot = TelegramBot.__new__(TelegramBot)
    calls = []

    async def call(method, payload):
        calls.append((method, payload))
        return {}

    bot.call = call
    asyncio.run(bot.edit_message_reply_markup(7, 41))

    assert calls == [
        ("editMessageReplyMarkup", {
            "chat_id": 7,
            "message_id": 41,
            "reply_markup": {"inline_keyboard": []},
        })
    ]
