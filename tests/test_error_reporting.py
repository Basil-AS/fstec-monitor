import asyncio

from fstec_monitor.telegram_bot import TelegramBot


def test_report_error_does_not_send_exception_details_to_admin():
    bot = TelegramBot.__new__(TelegramBot)
    bot.last_error_notice = 0.0
    sent = []

    async def send_temporary(*args, **kwargs):
        sent.append((args, kwargs))

    bot.send_temporary = send_temporary
    asyncio.run(bot.report_error("polling", RuntimeError("token=super-secret-value")))

    assert sent
    assert "super-secret-value" not in sent[0][0][1]
    assert "RuntimeError" not in sent[0][0][1]
