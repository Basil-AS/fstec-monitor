from __future__ import annotations

import asyncio
import time

import httpx
from sqlalchemy import func, select

from .config import settings
from .db import SessionLocal, init_db
from .models import Document, Event


def api_url(api_root: str, token: str, method: str) -> str:
    return f"{api_root.rstrip('/')}/bot{token}/{method}"


def is_admin(user_id: int | None, admin_id: int) -> bool:
    return user_id is not None and user_id == admin_id


class TelegramBot:
    def __init__(self) -> None:
        if not settings.telegram_bot_token:
            raise RuntimeError("FSTEC_TELEGRAM_BOT_TOKEN is required")
        self.token = settings.telegram_bot_token
        self.offset: int | None = None
        self.scan_lock = asyncio.Lock()
        self.scan_task: asyncio.Task[int] | None = None
        self.client = httpx.AsyncClient(timeout=40)

    async def close(self) -> None:
        await self.client.aclose()

    async def call(self, method: str, payload: dict) -> dict:
        response = await self.client.post(api_url(settings.telegram_api_root, self.token, method), json=payload)
        response.raise_for_status()
        body = response.json()
        if not body.get("ok"):
            raise RuntimeError(f"Telegram API error in {method}")
        return body.get("result", {})

    async def send(self, chat_id: int, text: str) -> None:
        for start in range(0, len(text), 3900):
            await self.call("sendMessage", {
                "chat_id": chat_id,
                "text": text[start:start + 3900],
                "disable_web_page_preview": True,
            })

    async def scan(self, baseline: bool = False) -> int:
        async with self.scan_lock:
            from .crawler import run_monitor
            count = await run_monitor(baseline=baseline)
            with SessionLocal() as session:
                await __import__("fstec_monitor.notify", fromlist=["notify_pending"]).notify_pending(session)
            return count

    def scan_is_running(self) -> bool:
        return self.scan_task is not None and not self.scan_task.done()

    def start_scan(self) -> bool:
        if self.scan_is_running():
            return False
        self.scan_task = asyncio.create_task(self.scan())
        return True

    def status_text(self) -> str:
        with SessionLocal() as session:
            documents = session.scalar(select(func.count(Document.id)).where(Document.active.is_(True))) or 0
            pending = session.scalar(select(func.count(Event.id)).where(Event.notified.is_(False))) or 0
            return f"ФСТЭК Monitor\nАктивных документов: {documents}\nНеотправленных событий: {pending}"

    async def handle(self, update: dict) -> None:
        message = update.get("message") or {}
        sender = message.get("from") or {}
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        text = (message.get("text") or "").strip()
        if not chat_id or not text:
            return
        if not is_admin(sender.get("id"), settings.telegram_admin_id):
            await self.send(chat_id, "Доступ разрешён только администратору.")
            return
        command = text.split()[0].split("@", 1)[0].lower()
        if command in {"/start", "/help"}:
            await self.send(chat_id, "Команды:\n/status — состояние\n/scan — запустить проверку\n/events — последние события")
        elif command == "/status":
            await self.send(chat_id, self.status_text())
        elif command == "/events":
            with SessionLocal() as session:
                events = session.scalars(select(Event).order_by(Event.id.desc()).limit(10)).all()
            await self.send(chat_id, "\n".join(f"{e.created_at:%Y-%m-%d %H:%M} {e.kind}: {e.summary}" for e in events) or "Событий нет.")
        elif command == "/scan":
            if self.start_scan():
                await self.send(chat_id, "Проверка запущена в фоне. По завершении изменения придут отдельными сообщениями.")
            else:
                await self.send(chat_id, "Проверка уже выполняется.")

    async def run(self) -> None:
        init_db()
        next_scan = time.monotonic() + settings.scan_interval_seconds
        self.start_scan()
        while True:
            if time.monotonic() >= next_scan:
                self.start_scan()
                next_scan = time.monotonic() + settings.scan_interval_seconds
            payload = {"timeout": 25, "allowed_updates": ["message"]}
            if self.offset is not None:
                payload["offset"] = self.offset
            try:
                updates = await self.call("getUpdates", payload)
                for update in updates:
                    self.offset = update["update_id"] + 1
                    await self.handle(update)
            except (httpx.HTTPError, RuntimeError):
                await asyncio.sleep(5)


async def main() -> None:
    bot = TelegramBot()
    try:
        await bot.run()
    finally:
        await bot.close()


if __name__ == "__main__":
    asyncio.run(main())
