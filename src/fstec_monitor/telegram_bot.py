from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
from sqlalchemy import delete, func, select

from .access import access_request_text, is_allowed
from .config import settings
from .db import SessionLocal, init_db
from .models import Attachment, AttachmentVersion, BotSetting, Document, Event, Snapshot, UserAccess
from .normalize import normalize_space
from .reports import event_report_md, safe_filename
from .schedule import (
    DAILY_MIDNIGHT,
    DAILY_NOON,
    DISABLED,
    EVERY_TWO_HOURS,
    SCHEDULE_MODES,
    next_scheduled_at,
    schedule_label,
)
from .storage import ObjectStore

log = logging.getLogger(__name__)
MEANINGFUL_KINDS = {"document_added", "html_content_changed", "attachment_added", "attachment_removed", "attachment_content_changed", "attachment_binary_changed"}
ERROR_KINDS = ("fetch_error", "storage_error")
IGNORED_SETTING_KEY = "ignored_categories"
QUOTA_CACHE_SECONDS = 300
ADMIN_COMMANDS = (
    ("start", "открыть меню"),
    ("status", "статистика и квота"),
    ("changes", "последние изменения"),
    ("report", "отчёт по ID события"),
    ("diff", "diff изменения как Markdown-файл"),
    ("errors", "последние ошибки"),
    ("clear_errors", "очистить журнал ошибок"),
    ("scan", "запустить проверку"),
    ("users", "заявки на доступ"),
    ("ignore", "игнорируемые категории"),
    ("settings", "настройки расписания"),
    ("help", "справка по командам"),
)


def api_url(api_root: str, token: str, method: str) -> str:
    return f"{api_root.rstrip('/')}/bot{token}/{method}"


def is_admin(user_id: int | None, admin_id: int) -> bool:
    return user_id is not None and user_id == admin_id


def telegram_commands() -> list[dict[str, str]]:
    return [{"command": command, "description": description} for command, description in ADMIN_COMMANDS]


def admin_keyboard() -> dict:
    return {
        "keyboard": [
            [{"text": "/status"}, {"text": "/changes"}],
            [{"text": "/scan"}, {"text": "/errors"}],
            [{"text": "/users"}, {"text": "/ignore"}],
            [{"text": "🔍 Проверить сейчас"}, {"text": "⚙️ Настройки"}],
            [{"text": "/help"}],
        ],
        "resize_keyboard": True,
        "is_persistent": True,
        "input_field_placeholder": "Выберите действие или введите /report ID",
    }


def settings_keyboard() -> dict:
    return {
        "inline_keyboard": [
            [{"text": "✅ Раз в сутки · 12:00", "callback_data": f"settings:set:{DAILY_NOON}"}],
            [{"text": "Раз в сутки · 00:00", "callback_data": f"settings:set:{DAILY_MIDNIGHT}"}],
            [{"text": "Каждые 2 часа", "callback_data": f"settings:set:{EVERY_TWO_HOURS}"}],
            [{"text": "⏸ Выключить автозапуск", "callback_data": f"settings:set:{DISABLED}"}],
            [{"text": "↩️ Главное меню", "callback_data": "menu:main"}],
        ]
    }


def _dt(value) -> str:
    return value.astimezone().strftime("%Y-%m-%d %H:%M") if value else "нет данных"


def category_key(value: str) -> str:
    return normalize_space(value).casefold()


def category_token(value: str) -> str:
    return hashlib.sha1(category_key(value).encode()).hexdigest()[:16]


class TelegramBot:
    def __init__(self) -> None:
        if not settings.telegram_bot_token:
            raise RuntimeError("FSTEC_TELEGRAM_BOT_TOKEN is required")
        self.token = settings.telegram_bot_token
        self.offset: int | None = None
        self.scan_lock = asyncio.Lock()
        self.scan_task: asyncio.Task[int] | None = None
        self.next_scan_at: datetime | None = None
        self.schedule_mode: str | None = None
        self.client = httpx.AsyncClient(timeout=40)
        self.last_error_notice = 0.0
        self._quota_cache: tuple[float, tuple[int, int]] | None = None

    async def close(self) -> None:
        await self.client.aclose()

    async def call(self, method: str, payload: dict) -> dict:
        response = await self.client.post(api_url(settings.telegram_api_root, self.token, method), json=payload)
        response.raise_for_status()
        body = response.json()
        if not body.get("ok"):
            raise RuntimeError(f"Telegram API error in {method}: {body.get('description', 'unknown')}")
        return body.get("result", {})

    async def send(self, chat_id: int, text: str, reply_markup: dict | None = None) -> None:
        for start in range(0, len(text), 3900):
            payload = {"chat_id": chat_id, "text": text[start:start + 3900], "disable_web_page_preview": True}
            if chat_id == settings.telegram_admin_id:
                payload["reply_markup"] = admin_keyboard()
            if reply_markup is not None:
                payload["reply_markup"] = reply_markup
            await self.call("sendMessage", payload)

    async def configure_menu(self) -> None:
        await self.call("setMyCommands", {
            "commands": telegram_commands(),
            "scope": {"type": "chat", "chat_id": settings.telegram_admin_id},
            "language_code": "ru",
        })
        try:
            await self.call("setChatMenuButton", {
                "chat_id": settings.telegram_admin_id,
                "menu_button": {"type": "commands"},
            })
        except (OSError, RuntimeError, httpx.HTTPError) as exc:
            log.warning("could not set Telegram chat menu button: %s", exc)

    async def send_file(self, chat_id: int, name: str, data: bytes, caption: str = "") -> None:
        response = await self.client.post(
            api_url(settings.telegram_api_root, self.token, "sendDocument"),
            data={"chat_id": str(chat_id), "caption": caption[:900]},
            files={"document": (name, data, "text/markdown" if name.endswith(".md") else "application/octet-stream")},
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

    def get_schedule_mode(self) -> str:
        with SessionLocal() as session:
            setting = session.get(BotSetting, "scan_schedule")
            mode = setting.value if setting else DAILY_NOON
        return mode if mode in SCHEDULE_MODES else DAILY_NOON

    def set_schedule_mode(self, mode: str) -> None:
        if mode not in SCHEDULE_MODES:
            raise ValueError(f"Unknown schedule mode: {mode}")
        init_db()
        with SessionLocal() as session:
            setting = session.get(BotSetting, "scan_schedule")
            if setting is None:
                setting = BotSetting(key="scan_schedule", value=mode)
                session.add(setting)
            else:
                setting.value = mode
            session.commit()
        self.next_scan_at = next_scheduled_at(mode, datetime.now().astimezone())

    def settings_text(self) -> str:
        mode = self.get_schedule_mode()
        next_run = next_scheduled_at(mode, datetime.now().astimezone())
        next_text = _dt(next_run) if next_run else "выключен"
        state = "идёт сейчас" if self.scan_is_running() else "не выполняется"
        return ("⚙️ Настройки проверки\n"
                f"Расписание: {schedule_label(mode)}\n"
                f"Следующая проверка: {next_text}\n"
                f"Текущее состояние: {state}\n\n"
                "Выберите новый режим:")

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

    async def quota_status(self) -> tuple[int, int]:
        now = time.monotonic()
        if self._quota_cache and now - self._quota_cache[0] < QUOTA_CACHE_SECONDS:
            return self._quota_cache[1]
        status = await asyncio.to_thread(ObjectStore().quota_status)
        self._quota_cache = (now, status)
        return status

    async def status_text(self) -> str:
        stats = await asyncio.to_thread(self._status_stats)
        used, quota = await self.quota_status()
        mode = self.get_schedule_mode()
        next_run = next_scheduled_at(mode, datetime.now().astimezone())
        return ("ФСТЭК Monitor\n"
                f"Документы: {stats['documents']}\nВложения: {stats['attachments']}\n"
                f"Последняя проверка: {_dt(stats['last_scan'])}\nПоследнее изменение: {_dt(stats['last_change'])}\n"
                f"Статус изменений: {'есть новые' if stats['pending'] else 'изменений нет'}\n"
                f"Хранилище: {used / 1024**3:.2f} / {quota / 1024**3:.2f} ГБ\nОшибок в журнале: {stats['errors']}\n"
                f"Расписание: {schedule_label(mode)}\n"
                f"Следующая проверка: {_dt(next_run) if next_run else 'выключен'}\n"
                f"Состояние: {'проверка выполняется' if self.scan_is_running() else 'ожидание'}")

    def _status_stats(self) -> dict:
        with SessionLocal() as session:
            return {
                "documents": session.scalar(select(func.count(Document.id)).where(Document.active.is_(True))) or 0,
                "attachments": session.scalar(select(func.count(Attachment.id)).where(Attachment.active.is_(True))) or 0,
                "pending": session.scalar(select(func.count(Event.id)).where(Event.notified.is_(False), Event.kind.in_(MEANINGFUL_KINDS))) or 0,
                "last_scan": session.scalar(select(func.max(Snapshot.fetched_at))),
                "last_change": session.scalar(select(func.max(Event.created_at)).where(Event.kind.in_(MEANINGFUL_KINDS))),
                "errors": session.scalar(select(func.count(Event.id)).where(Event.kind.in_(ERROR_KINDS))) or 0,
            }

    def changes_text(self, limit: int = 10) -> str:
        with SessionLocal() as session:
            events = session.scalars(select(Event).where(Event.kind.in_(MEANINGFUL_KINDS)).order_by(Event.id.desc()).limit(max(1, min(limit, 30)))).all()
            docs = {d.id: d for d in session.scalars(select(Document).where(Document.id.in_([e.document_id for e in events if e.document_id]))).all()}
        return "\n".join(f"#{e.id} {_dt(e.created_at)} {e.kind}: {docs.get(e.document_id).title if docs.get(e.document_id) else e.summary}" for e in events) or "Изменений нет."

    def users_text(self) -> tuple[str, dict | None]:
        with SessionLocal() as session:
            users = session.scalars(select(UserAccess).where(UserAccess.status == "pending").order_by(UserAccess.requested_at)).all()
        if not users:
            return "Новых заявок на доступ нет.", None
        lines = [f"Заявки на доступ: {len(users)}"]
        buttons = []
        for user in users:
            identity = f"@{user.username}" if user.username else user.display_name or "без имени"
            lines.append(f"{identity} — ID {user.user_id}")
            buttons.append([{"text": f"✅ {identity[:30]}", "callback_data": f"access:approve:{user.user_id}"}, {"text": "❌", "callback_data": f"access:deny:{user.user_id}"}])
        return "\n".join(lines), {"inline_keyboard": buttons}

    def ignored_categories_db(self) -> list[str]:
        init_db()
        with SessionLocal() as session:
            setting = session.get(BotSetting, IGNORED_SETTING_KEY)
            value = setting.value if setting else ""
        return [line.strip() for line in value.splitlines() if line.strip()]

    def set_ignored_categories_db(self, categories: list[str]) -> None:
        init_db()
        with SessionLocal() as session:
            setting = session.get(BotSetting, IGNORED_SETTING_KEY)
            if setting is None:
                setting = BotSetting(key=IGNORED_SETTING_KEY, value="\n".join(categories))
                session.add(setting)
            else:
                setting.value = "\n".join(categories)
            session.commit()

    def toggle_ignored_category(self, token: str) -> str | None:
        with SessionLocal() as session:
            known = [c for c in session.scalars(select(Document.category).distinct()).all() if c]
        target = next((c for c in sorted(known) if category_token(c) == token), None)
        if target is None:
            return None
        ignored = self.ignored_categories_db()
        keys = {category_key(c) for c in ignored}
        if category_key(target) in keys:
            self.set_ignored_categories_db([c for c in ignored if category_key(c) != category_key(target)])
            return f"✅ Категория снова отслеживается: {target}"
        self.set_ignored_categories_db(ignored + [target])
        return f"🚫 Категория добавлена в игнор: {target}"

    def ignore_text(self) -> tuple[str, dict | None]:
        env_ignored = sorted(settings.ignored_category_set)
        db_ignored = self.ignored_categories_db()
        db_keys = {category_key(c) for c in db_ignored}
        lines = ["Игнорируемые категории:"]
        if env_ignored:
            lines.append("Из конфигурации (.env):")
            lines.extend(f"• {name}" for name in env_ignored)
        if db_ignored:
            lines.append("Добавлены через бота:")
            lines.extend(f"• {name}" for name in db_ignored)
        if not env_ignored and not db_ignored:
            lines.append("пока нет")
        with SessionLocal() as session:
            known = sorted(c for c in session.scalars(select(Document.category).distinct()).all() if c)
        buttons = []
        for category in known[:20]:
            ignored = category_key(category) in db_keys or category_key(category) in set(env_ignored)
            mark = "🚫" if ignored else "👁"
            buttons.append([{"text": f"{mark} {category[:40]}", "callback_data": f"ignore:t:{category_token(category)}"}])
        if not buttons:
            return "\n".join(lines), None
        lines.append("")
        lines.append("Нажмите на категорию, чтобы переключить игнор (🚫 — игнорируется):")
        return "\n".join(lines), {"inline_keyboard": buttons}

    def clear_errors_text(self) -> tuple[str, dict | None]:
        with SessionLocal() as session:
            count = session.scalar(select(func.count(Event.id)).where(Event.kind.in_(ERROR_KINDS))) or 0
        if not count:
            return "Журнал ошибок пуст.", None
        return (f"В журнале ошибок {count} событий (fetch_error, storage_error).\n"
                "Удалить их? История документов и diff'ы не затрагивается."), {
            "inline_keyboard": [[
                {"text": "🧹 Очистить", "callback_data": "errors:clear:confirm"},
                {"text": "Отмена", "callback_data": "errors:clear:cancel"},
            ]]
        }

    def clear_errors(self) -> int:
        with SessionLocal() as session:
            result = session.execute(delete(Event).where(Event.kind.in_(ERROR_KINDS)))
            session.commit()
            return result.rowcount or 0

    async def request_access(self, user_id: int, chat_id: int, username: str, display_name: str) -> bool:
        should_notify = False
        with SessionLocal() as session:
            user = session.get(UserAccess, user_id)
            if user and is_allowed(user):
                return True
            if not user:
                user = UserAccess(user_id=user_id, chat_id=chat_id, username=username, display_name=display_name, status="pending")
                session.add(user)
                should_notify = True
            elif user.status != "pending":
                user.status = "pending"
                user.chat_id = chat_id
                user.username = username
                user.display_name = display_name
                user.notification_sent = False
                should_notify = True
            else:
                user.chat_id = chat_id
                user.username = username
                user.display_name = display_name
                should_notify = not user.notification_sent
            session.commit()
        if should_notify:
            await self.send(settings.telegram_admin_id, access_request_text(user_id, username, display_name), {
                "inline_keyboard": [[
                    {"text": "✅ Разрешить", "callback_data": f"access:approve:{user_id}"},
                    {"text": "❌ Отклонить", "callback_data": f"access:deny:{user_id}"},
                ]]
            })
            with SessionLocal() as session:
                user = session.get(UserAccess, user_id)
                if user:
                    user.notification_sent = True
                    session.commit()
        await self.send(chat_id, "Заявка на доступ отправлена администратору. Ожидайте решения." if should_notify else "Ваша заявка уже ожидает решения администратора.")
        return False

    async def handle_callback(self, callback: dict) -> None:
        callback_id = callback.get("id")
        sender = callback.get("from") or {}
        if callback_id:
            try:
                await self.call("answerCallbackQuery", {"callback_query_id": callback_id})
            except (OSError, RuntimeError, httpx.HTTPError) as exc:
                log.warning("answerCallbackQuery failed (expired query?): %s", exc)
        if not is_admin(sender.get("id"), settings.telegram_admin_id):
            return
        data = (callback.get("data") or "").split(":")
        if data == ["menu", "main"]:
            await self.send(settings.telegram_admin_id, "Главное меню готово. Выберите действие:")
            return
        if len(data) == 3 and data[0] == "settings" and data[1] == "set":
            try:
                self.set_schedule_mode(data[2])
            except ValueError:
                await self.send(settings.telegram_admin_id, "Неизвестный режим расписания.")
                return
            await self.send(settings.telegram_admin_id, f"✅ Расписание изменено: {schedule_label(data[2])}.", settings_keyboard())
            return
        if data == ["errors", "clear", "cancel"]:
            await self.send(settings.telegram_admin_id, "Очистка журнала ошибок отменена.")
            return
        if data == ["errors", "clear", "confirm"]:
            deleted = await asyncio.to_thread(self.clear_errors)
            await self.send(settings.telegram_admin_id, f"🧹 Журнал ошибок очищен: удалено {deleted} событий.")
            return
        if len(data) == 3 and data[0] == "ignore" and data[1] == "t":
            result = await asyncio.to_thread(self.toggle_ignored_category, data[2])
            await self.send(settings.telegram_admin_id, result or "Категория не найдена — возможно, список изменился. Откройте /ignore заново.")
            return
        if len(data) != 3 or data[0] != "access" or data[1] not in {"approve", "deny"} or not data[2].isdigit():
            return
        user_id = int(data[2])
        status = "approved" if data[1] == "approve" else "denied"
        with SessionLocal() as session:
            user = session.get(UserAccess, user_id)
            if not user:
                await self.send(settings.telegram_admin_id, f"Пользователь {user_id} не найден.")
                return
            user.status = status
            user.reviewed_at = datetime.now(UTC)
            session.commit()
            target_chat = user.chat_id
        await self.send(target_chat, "✅ Доступ разрешён. Используйте /help." if status == "approved" else "❌ В доступе отказано.")
        await self.send(settings.telegram_admin_id, f"Пользователь {user_id}: {'доступ разрешён' if status == 'approved' else 'доступ отклонён'}.")

    def _build_report(self, event_id: int) -> tuple[str, list[tuple[str, bytes, str]]] | None:
        store = ObjectStore()
        files: list[tuple[str, bytes, str]] = []
        with SessionLocal() as session:
            event = session.get(Event, event_id)
            if not event:
                return None
            doc = session.get(Document, event.document_id) if event.document_id else None
            report = event_report_md(event, doc.title if doc else "", doc.canonical_url if doc else "")
            hashes = re.findall(r"(?:old|new)=([0-9a-f]{64})", event.details)
            if hashes:
                versions = session.scalars(select(AttachmentVersion).where(AttachmentVersion.binary_sha256.in_(hashes))).all()
                for version in versions:
                    path = Path(store.root) / version.object_key
                    if path.exists() and path.stat().st_size <= settings.telegram_max_file_bytes:
                        prefix = "old" if version.binary_sha256 == hashes[0] else "new"
                        files.append((f"{prefix}-{safe_filename(doc.title if doc else 'document', suffix=path.suffix)}", path.read_bytes(), "версия документа"))
            elif doc:
                snapshots = session.scalars(select(Snapshot).where(Snapshot.document_id == doc.id).order_by(Snapshot.id.desc()).limit(2)).all()
                for prefix, snapshot in zip(("new", "old"), snapshots):
                    path = Path(store.root) / snapshot.normalized_text_object
                    if path.exists() and path.stat().st_size <= settings.telegram_max_file_bytes:
                        files.append((f"{prefix}-{safe_filename(doc.title)}", path.read_bytes(), "версия HTML-текста"))
        return report, files

    async def send_report(self, chat_id: int, event_id: int) -> None:
        result = await asyncio.to_thread(self._build_report, event_id)
        if result is None:
            await self.send(chat_id, f"Событие #{event_id} не найдено.")
            return
        report, files = result
        await self.send_file(chat_id, f"diff_{event_id}.md", report.encode(), "Подробный отчёт: old/new и diff")
        for name, data, caption in files:
            await self.send_file(chat_id, name, data, caption)
        if not files:
            await self.send(chat_id, "Старую/новую версию не отправил: файл отсутствует или превышает лимит Telegram. История сохранена на сервере.")

    async def handle(self, update: dict) -> None:
        started_at = time.monotonic()
        if update.get("callback_query"):
            try:
                await self.handle_callback(update["callback_query"])
            except (OSError, RuntimeError, ValueError, httpx.HTTPError) as exc:
                await self.report_error("ошибка обработки callback", exc)
            return
        message = update.get("message") or {}
        sender = message.get("from") or {}
        chat_id = (message.get("chat") or {}).get("id")
        text = (message.get("text") or "").strip()
        if not chat_id or not text:
            return
        sender_id = sender.get("id")
        if (not is_admin(sender_id, settings.telegram_admin_id)
                and (not sender_id or not await self.request_access(sender_id, chat_id, sender.get("username", ""), " ".join(filter(None, [sender.get("first_name"), sender.get("last_name")]))))):
            return
        parts = text.split()
        text_commands = {"🔍 Проверить сейчас": "/scan", "⚙️ Настройки": "/settings"}
        text = text_commands.get(text, text)
        parts = text.split()
        command = parts[0].split("@", 1)[0].lower()
        try:
            if command in {"/scan", "/users", "/ignore", "/settings", "/clear_errors"} and not is_admin(sender_id, settings.telegram_admin_id):
                await self.send(chat_id, "Эта команда доступна только администратору.")
                return
            if command in {"/start", "/help"}:
                await self.send(chat_id, "Меню готово.\n\n/status — статистика, квота и расписание\n/changes [N] — последние изменения\n/report ID — отчёт и diff_<ID>.md с версиями\n/diff ID — то же самое\n/errors — ошибки\n/clear_errors — очистить журнал ошибок\n/ignore — управление игнором категорий\n/scan — проверить сейчас\n/settings — настроить расписание")
            elif command == "/status":
                await self.send(chat_id, await self.status_text())
            elif command in {"/changes", "/events"}:
                await self.send(chat_id, await asyncio.to_thread(self.changes_text, int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 10))
            elif command in {"/report", "/diff"}:
                if len(parts) != 2 or not parts[1].isdigit():
                    await self.send(chat_id, f"Использование: {command} ID")
                else:
                    await self.send_report(chat_id, int(parts[1]))
            elif command == "/errors":
                with SessionLocal() as session:
                    errors = session.scalars(select(Event).where(Event.kind.in_(ERROR_KINDS)).order_by(Event.id.desc()).limit(10)).all()
                await self.send(chat_id, "\n".join(f"#{e.id} {_dt(e.created_at)} {e.summary}: {e.details[:300]}" for e in errors) or "Ошибок нет.")
            elif command == "/clear_errors":
                text_out, markup = await asyncio.to_thread(self.clear_errors_text)
                await self.send(chat_id, text_out, markup)
            elif command == "/users":
                text_out, markup = await asyncio.to_thread(self.users_text)
                await self.send(chat_id, text_out, markup)
            elif command == "/ignore":
                text_out, markup = await asyncio.to_thread(self.ignore_text)
                await self.send(chat_id, text_out, markup)
            elif command == "/scan":
                await self.send(chat_id, "Проверка запущена в фоне." if self.start_scan() else "Проверка уже выполняется.")
            elif command == "/settings":
                await self.send(chat_id, await asyncio.to_thread(self.settings_text), settings_keyboard())
        except (OSError, RuntimeError, ValueError, httpx.HTTPError) as exc:
            await self.report_error(f"ошибка команды {command}", exc)
            await self.send(chat_id, "Не удалось выполнить команду. Ошибка записана и отправлена администратору.")
        finally:
            log.info("command %s took %.2fs", command, time.monotonic() - started_at)

    async def run(self) -> None:
        init_db()
        try:
            await self.configure_menu()
        except (OSError, RuntimeError, ValueError, httpx.HTTPError) as exc:
            await self.report_error("не удалось настроить меню Telegram", exc)
        self.schedule_mode = self.get_schedule_mode()
        self.next_scan_at = next_scheduled_at(self.schedule_mode, datetime.now().astimezone())
        while True:
            current_mode = self.get_schedule_mode()
            if current_mode != self.schedule_mode:
                self.schedule_mode = current_mode
                self.next_scan_at = next_scheduled_at(current_mode, datetime.now().astimezone())
            now = datetime.now().astimezone()
            if self.next_scan_at is not None and now >= self.next_scan_at:
                self.start_scan()
                self.next_scan_at = next_scheduled_at(current_mode, now + timedelta(seconds=1))
            payload = {"timeout": 25, "allowed_updates": ["message", "callback_query"]}
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
    # httpx INFO logs full request URLs, which would leak the bot token into journald.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(main())
