from __future__ import annotations

import asyncio
import logging
import re
import time
from pathlib import Path

import httpx
from sqlalchemy import func, select

from .config import settings
from .db import SessionLocal, init_db
from .models import Attachment, AttachmentVersion, Document, Event, Snapshot
from .reports import event_report, safe_filename
from .storage import ObjectStore

log = logging.getLogger(__name__)
MEANINGFUL_KINDS = {"document_added", "html_content_changed", "attachment_added", "attachment_removed", "attachment_content_changed", "attachment_binary_changed"}


def api_url(api_root: str, token: str, method: str) -> str:
    return f"{api_root.rstrip('/')}/bot{token}/{method}"


def is_admin(user_id: int | None, admin_id: int) -> bool:
    return user_id is not None and user_id == admin_id


def _dt(value) -> str:
    return value.astimezone().strftime("%Y-%m-%d %H:%M") if value else "нет данных"


class TelegramBot:
    def __init__(self) -> None:
        if not settings.telegram_bot_token:
            raise RuntimeError("FSTEC_TELEGRAM_BOT_TOKEN is required")
        self.token = settings.telegram_bot_token
        self.offset: int | None = None
        self.scan_lock = asyncio.Lock()
        self.scan_task: asyncio.Task[int] | None = None
        self.client = httpx.AsyncClient(timeout=40)
        self.last_error_notice = 0.0

    async def close(self) -> None:
        await self.client.aclose()

    async def call(self, method: str, payload: dict) -> dict:
        response = await self.client.post(api_url(settings.telegram_api_root, self.token, method), json=payload)
        response.raise_for_status()
        body = response.json()
        if not body.get("ok"):
            raise RuntimeError(f"Telegram API error in {method}: {body.get('description', 'unknown')}")
        return body.get("result", {})

    async def send(self, chat_id: int, text: str) -> None:
        for start in range(0, len(text), 3900):
            await self.call("sendMessage", {"chat_id": chat_id, "text": text[start:start + 3900], "disable_web_page_preview": True})

    async def send_file(self, chat_id: int, name: str, data: bytes, caption: str = "") -> None:
        response = await self.client.post(
            api_url(settings.telegram_api_root, self.token, "sendDocument"),
            data={"chat_id": str(chat_id), "caption": caption[:900]},
            files={"document": (name, data, "application/octet-stream")},
        )
        response.raise_for_status()
        body = response.json()
        if not body.get("ok"):
            raise RuntimeError(f"Telegram API error in sendDocument: {body.get('description', 'unknown')}")

    async def scan(self, baseline: bool = False) -> int:
        async with self.scan_lock:
            from .crawler import run_monitor
            count = await run_monitor(baseline=baseline)
            with SessionLocal() as session:
                from .notify import notify_pending
                await notify_pending(session)
            return count

    async def _scan_task(self) -> int:
        try:
            count = await self.scan()
            await self.send(settings.telegram_admin_id, f"✅ Проверка завершена. Обработано документов: {count}. /status")
            return count
        except (OSError, RuntimeError, ValueError, httpx.HTTPError) as exc:
            log.exception("background scan failed")
            await self.report_error("ошибка фоновой проверки", exc)
            raise

    def scan_is_running(self) -> bool:
        return self.scan_task is not None and not self.scan_task.done()

    def start_scan(self) -> bool:
        if self.scan_is_running():
            return False
        self.scan_task = asyncio.create_task(self._scan_task())
        return True

    async def report_error(self, context: str, exc: Exception) -> None:
        now = time.monotonic()
        log.error("%s: %s", context, exc)
        if now - self.last_error_notice < 60:
            return
        self.last_error_notice = now
        try:
            await self.send(settings.telegram_admin_id, f"🔴 {context}: {type(exc).__name__}: {str(exc)[:600]}")
        except Exception:
            log.exception("could not notify admin about error")

    def status_text(self) -> str:
        with SessionLocal() as session:
            documents = session.scalar(select(func.count(Document.id)).where(Document.active.is_(True))) or 0
            attachments = session.scalar(select(func.count(Attachment.id)).where(Attachment.active.is_(True))) or 0
            pending = session.scalar(select(func.count(Event.id)).where(Event.notified.is_(False), Event.kind.in_(MEANINGFUL_KINDS))) or 0
            last_scan = session.scalar(select(func.max(Snapshot.fetched_at)))
            last_change = session.scalar(select(func.max(Event.created_at)).where(Event.kind.in_(MEANINGFUL_KINDS)))
            errors = session.scalar(select(func.count(Event.id)).where(Event.kind.in_({"fetch_error", "storage_error"}))) or 0
        used, quota = ObjectStore().quota_status()
        return ("ФСТЭК Monitor\n"
                f"Документы: {documents}\nВложения: {attachments}\n"
                f"Последняя проверка: {_dt(last_scan)}\nПоследнее изменение: {_dt(last_change)}\n"
                f"Статус изменений: {'есть новые' if pending else 'изменений нет'}\n"
                f"Хранилище: {used / 1024**3:.2f} / {quota / 1024**3:.2f} ГБ\nОшибок в журнале: {errors}")

    def changes_text(self, limit: int = 10) -> str:
        with SessionLocal() as session:
            events = session.scalars(select(Event).where(Event.kind.in_(MEANINGFUL_KINDS)).order_by(Event.id.desc()).limit(max(1, min(limit, 30)))).all()
            docs = {d.id: d for d in session.scalars(select(Document).where(Document.id.in_([e.document_id for e in events if e.document_id]))).all()}
        return "\n".join(f"#{e.id} {_dt(e.created_at)} {e.kind}: {docs.get(e.document_id).title if docs.get(e.document_id) else e.summary}" for e in events) or "Изменений нет."

    async def send_report(self, chat_id: int, event_id: int) -> None:
        store = ObjectStore()
        files: list[tuple[str, bytes, str]] = []
        with SessionLocal() as session:
            event = session.get(Event, event_id)
            if not event:
                await self.send(chat_id, f"Событие #{event_id} не найдено.")
                return
            doc = session.get(Document, event.document_id) if event.document_id else None
            report = event_report(event, doc.title if doc else "", doc.canonical_url if doc else "")
            hashes = re.findall(r"(?:old|new)=([0-9a-f]{64})", event.details)
            if hashes:
                versions = session.scalars(select(AttachmentVersion).where(AttachmentVersion.binary_sha256.in_(hashes))).all()
                for version in versions:
                    path = Path(store.root) / version.object_key
                    if path.exists() and path.stat().st_size <= settings.telegram_max_file_bytes:
                        prefix = "old" if version.binary_sha256 == hashes[0] else "new"
                        files.append((f"{prefix}-{safe_filename(doc.title if doc else 'document')}{path.suffix}", path.read_bytes(), "версия документа"))
            elif doc:
                snapshots = session.scalars(select(Snapshot).where(Snapshot.document_id == doc.id).order_by(Snapshot.id.desc()).limit(2)).all()
                for prefix, snapshot in zip(("new", "old"), snapshots):
                    path = Path(store.root) / snapshot.normalized_text_object
                    if path.exists() and path.stat().st_size <= settings.telegram_max_file_bytes:
                        files.append((f"{prefix}-{safe_filename(doc.title)}.txt", path.read_bytes(), "версия HTML-текста"))
        await self.send_file(chat_id, f"event-{event_id}-report.txt", report.encode(), "Подробный отчёт")
        for name, data, caption in files:
            await self.send_file(chat_id, name, data, caption)
        if not files:
            await self.send(chat_id, "Старую/новую версию не отправил: файл отсутствует или превышает лимит Telegram. История сохранена на сервере.")

    async def handle(self, update: dict) -> None:
        message = update.get("message") or {}
        sender = message.get("from") or {}
        chat_id = (message.get("chat") or {}).get("id")
        text = (message.get("text") or "").strip()
        if not chat_id or not text:
            return
        if not is_admin(sender.get("id"), settings.telegram_admin_id):
            await self.send(chat_id, "Доступ разрешён только администратору.")
            return
        parts = text.split()
        command = parts[0].split("@", 1)[0].lower()
        try:
            if command in {"/start", "/help"}:
                await self.send(chat_id, "Команды:\n/status — статистика и квота\n/changes [N] — последние изменения\n/report ID — подробный отчёт и версии\n/events — журнал\n/errors — ошибки\n/scan — запустить проверку")
            elif command == "/status":
                await self.send(chat_id, self.status_text())
            elif command in {"/changes", "/events"}:
                await self.send(chat_id, self.changes_text(int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 10))
            elif command == "/report":
                if len(parts) != 2 or not parts[1].isdigit():
                    await self.send(chat_id, "Использование: /report ID")
                else:
                    await self.send_report(chat_id, int(parts[1]))
            elif command == "/errors":
                with SessionLocal() as session:
                    errors = session.scalars(select(Event).where(Event.kind.in_({"fetch_error", "storage_error"})).order_by(Event.id.desc()).limit(10)).all()
                await self.send(chat_id, "\n".join(f"#{e.id} {_dt(e.created_at)} {e.summary}: {e.details[:300]}" for e in errors) or "Ошибок нет.")
            elif command == "/scan":
                await self.send(chat_id, "Проверка запущена в фоне." if self.start_scan() else "Проверка уже выполняется.")
        except (OSError, RuntimeError, ValueError, httpx.HTTPError) as exc:
            await self.report_error(f"ошибка команды {command}", exc)
            await self.send(chat_id, "Не удалось выполнить команду. Ошибка записана и отправлена администратору.")

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
            except (httpx.HTTPError, RuntimeError) as exc:
                await self.report_error("ошибка Telegram API", exc)
                await asyncio.sleep(5)


async def main() -> None:
    bot = TelegramBot()
    try:
        await bot.run()
    finally:
        await bot.close()


if __name__ == "__main__":
    asyncio.run(main())
