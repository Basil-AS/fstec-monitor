from __future__ import annotations

import asyncio

from fstec_monitor.telegram_bot import TelegramBot


def test_user_cannot_open_admin_screen_through_callback() -> None:
    async def scenario() -> None:
        bot = TelegramBot.__new__(TelegramBot)
        bot.call = lambda *_args, **_kwargs: _completed()
        opened: list[str] = []

        async def render(_chat_id: int, screen: str, **_kwargs):
            opened.append(screen)

        bot._render_screen = render
        await bot.handle_callback({
            "id": "callback",
            "from": {"id": 42},
            "message": {"message_id": 7, "chat": {"id": 42}},
            "data": "v1:screen:settings",
        })

        assert opened == []

    async def _completed():
        return {}

    asyncio.run(scenario())
