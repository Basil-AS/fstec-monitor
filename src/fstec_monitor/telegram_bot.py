from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
from sqlalchemy import delete, func, select

from .access import access_request_text, is_allowed
from .config import settings
from .db import SessionLocal, init_db
from .models import (
    Attachment,
    AttachmentVersion,
    BotSetting,
    Document,
    Event,
    ScanRun,
    Snapshot,
    UserAccess,
    UserIgnoredCategory,
)
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
from .telegram import keyboards as telegram_keyboards

log = logging.getLogger(__name__)
MEANINGFUL_KINDS = {
    "document_added",
    "document_removed",
    "document_restored",
    "html_content_changed",
    "attachment_added",
    "attachment_removed",
    "attachment_content_changed",
    "attachment_binary_changed",
}
ERROR_KINDS = ("fetch_error", "storage_error")
IGNORED_SETTING_KEY = "ignored_categories"
QUOTA_CACHE_SECONDS = 300
ADMIN_COMMANDS = (
    ("start", "открыть меню"),
    ("status", "статистика и квота"),
    ("changes", "последние изменения"),
    ("report", "отчёт по ID события"),
    ("errors", "последние ошибки"),
    ("clear_errors", "очистить журнал ошибок"),
    ("scan", "запустить проверку"),
    ("users", "заявки на доступ"),
    ("ignore", "игнорируемые категории"),
    ("settings", "настройки расписания"),
    ("help", "справка по командам"),
)
ADMIN_LABEL_COMMANDS = {
    "📊 Статус": "/status",
    "📰 Изменения": "/changes",
    "🔍 Проверить сейчас": "/scan",
    "🧯 Ошибки": "/errors",
    "👥 Пользователи": "/users",
    "🚫 Игнор категорий": "/ignore",
    "⚙️ Настройки": "/settings",
    "ℹ️ Помощь": "/help",
}
USER_LABEL_COMMANDS = {
    "📰 Последние изменения": "/changes",
    "🚫 Мои категории": "/my_ignore",
    "ℹ️ Помощь": "/help",
}
USER_ALLOWED_COMMANDS = {"/start", "/help", "/changes", "/my_ignore"}


@dataclass
class ScanProgress:
    state: str = "idle"
    stage: str = "Проверка не запущена"
    completed: int = 0
    total: int = 0
    errors: int = 0
    started_at: datetime | None = None
    finished_at: datetime | None = None
    last_error: str = ""
    documents: int = 0

    @property
    def percent(self) -> int:
        return round(self.completed * 100 / self.total) if self.total else 0


def api_url(api_root: str, token: str, method: str) -> str:
    return f"{api_root.rstrip('/')}/bot{token}/{method}"


def is_admin(user_id: int | None, admin_id: int) -> bool:
    return user_id is not None and user_id == admin_id


def telegram_commands() -> list[dict[str, str]]:
    return [{"command": command, "description": description} for command, description in ADMIN_COMMANDS]


def admin_keyboard() -> dict:
    return telegram_keyboards.admin_keyboard()


def user_keyboard() -> dict:
    return telegram_keyboards.user_keyboard()


def is_user_command_allowed(command: str) -> bool:
    return command.split("@", 1)[0].lower() in USER_ALLOWED_COMMANDS


def settings_keyboard(notifications_enabled: bool = True) -> dict:
    return {
        "inline_keyboard": [
            [{"text": "✅ Раз в сутки · 12:00", "callback_data": f"settings:set:{DAILY_NOON}"}],
            [{"text": "Раз в сутки · 00:00", "callback_data": f"settings:set:{DAILY_MIDNIGHT}"}],
            [{"text": "Каждые 2 часа", "callback_data": f"settings:set:{EVERY_TWO_HOURS}"}],
            [{"text": "⏸ Выключить автозапуск", "callback_data": f"settings:set:{DISABLED}"}],
            [{"text": f"{'🔔' if notifications_enabled else '🔕'} Уведомления: {'включены' if notifications_enabled else 'выключены'}", "callback_data": "settings:notifications:toggle"}],
            [{"text": "↩️ Главное меню", "callback_data": "menu:main"}],
        ]
    }


def _dt(value) -> str:
    return value.astimezone().strftime("%Y-%m-%d %H:%M") if value else "нет данных"


def _fmt_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds} с"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} мин {sec:02d} с"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} ч {minutes:02d} мин"


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
        self.scan_cancel_event = asyncio.Event()
        self.scan_progress = ScanProgress()
        self.scan_status_message: tuple[int, int] | None = None
        self.scan_status_update_task: asyncio.Task | None = None
        self.scan_status_last_update = 0.0
        self._temporary_delete_tasks: set[asyncio.Task] = set()
        self.next_scan_at: datetime | None = None
        self.schedule_mode: str | None = None
        self.client = httpx.AsyncClient(timeout=40)
        self.last_error_notice = 0.0
        self._quota_cache: tuple[float, tuple[int, int]] | None = None

    async def close(self) -> None:
        for task in getattr(self, "_temporary_delete_tasks", set()):
            task.cancel()
        await self.client.aclose()

    async def call(self, method: str, payload: dict) -> dict:
        response = await self.client.post(api_url(settings.telegram_api_root, self.token, method), json=payload)
        response.raise_for_status()
        body = response.json()
        if not body.get("ok"):
            raise RuntimeError(f"Telegram API error in {method}: {body.get('description', 'unknown')}")
        return body.get("result", {})

    async def send(self, chat_id: int, text: str, reply_markup: dict | None = None) -> int | None:
        message_id = None
        for start in range(0, len(text), 3900):
            payload = {
                "chat_id": chat_id,
                "text": text[start:start + 3900],
                "link_preview_options": {"is_disabled": True},
            }
            if reply_markup is not None:
                payload["reply_markup"] = reply_markup
            elif chat_id == settings.telegram_admin_id:
                payload["reply_markup"] = admin_keyboard()
            else:
                payload["reply_markup"] = user_keyboard()
            result = await self.call("sendMessage", payload)
            if isinstance(result, dict):
                message_id = result.get("message_id", message_id)
        return message_id

    async def edit_message(self, chat_id: int, message_id: int, text: str, reply_markup: dict | None = None) -> None:
        payload = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "link_preview_options": {"is_disabled": True},
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        await self.call("editMessageText", payload)

    async def delete_message(self, chat_id: int, message_id: int) -> None:
        await self.call("deleteMessage", {"chat_id": chat_id, "message_id": message_id})

    async def _delete_later(self, chat_id: int, message_id: int, ttl: float) -> None:
        await asyncio.sleep(ttl)
        try:
            await self.delete_message(chat_id, message_id)
        except (OSError, RuntimeError, httpx.HTTPError) as exc:
            log.debug("temporary Telegram message already gone chat=%s message=%s: %s", chat_id, message_id, exc)

    async def send_temporary(self, chat_id: int, text: str, ttl: float = 8.0) -> None:
        for start in range(0, len(text), 3900):
            result = await self.call("sendMessage", {
                "chat_id": chat_id,
                "text": text[start:start + 3900],
                "link_preview_options": {"is_disabled": True},
            })
            if isinstance(result, dict) and result.get("message_id"):
                task = asyncio.create_task(self._delete_later(chat_id, result["message_id"], ttl))
                tasks = getattr(self, "_temporary_delete_tasks", set())
                tasks.add(task)
                self._temporary_delete_tasks = tasks
                task.add_done_callback(tasks.discard)

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

    async def scan(self, baseline: bool = False, trigger: str = "manual", progress_callback=None, cancel_event=None) -> int:
        async with self.scan_lock:
            from .crawler import run_monitor
            count = await run_monitor(
                baseline=baseline,
                trigger=trigger,
                progress_callback=progress_callback,
                cancel_event=cancel_event,
            )
            with SessionLocal() as session:
                from .notify import notify_pending
                await notify_pending(session)
            return count

    async def _scan_task(self, trigger: str = "manual") -> int:
        started = time.monotonic()
        try:
            count = await self.scan(
                trigger=trigger,
                progress_callback=self._update_scan_progress,
                cancel_event=self.scan_cancel_event,
            )
            self.scan_progress.state = "completed"
            self.scan_progress.stage = "Проверка завершена"
            self.scan_progress.documents = count
            self.scan_progress.finished_at = datetime.now(UTC)
            await self.refresh_scan_status()
            if getattr(self, "scan_status_message", None) is None:
                await self.send_temporary(settings.telegram_admin_id, f"✅ Проверка завершена. Обработано документов: {count}. Длительность: {_fmt_duration(time.monotonic() - started)}.")
            return count
        except asyncio.CancelledError:
            self.scan_progress.state = "cancelled"
            self.scan_progress.stage = "Проверка остановлена"
            self.scan_progress.finished_at = datetime.now(UTC)
            await self.refresh_scan_status()
            raise
        except (OSError, RuntimeError, ValueError, httpx.HTTPError) as exc:
            self.scan_progress.state = "failed"
            self.scan_progress.stage = "Проверка завершилась с ошибкой"
            self.scan_progress.last_error = str(exc)[:500]
            self.scan_progress.finished_at = datetime.now(UTC)
            await self.refresh_scan_status()
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

    def notifications_enabled(self) -> bool:
        with SessionLocal() as session:
            setting = session.get(BotSetting, "notifications_enabled")
        return setting is None or setting.value != "0"

    def set_notifications_enabled(self, enabled: bool) -> None:
        init_db()
        with SessionLocal() as session:
            setting = session.get(BotSetting, "notifications_enabled")
            if setting is None:
                session.add(BotSetting(key="notifications_enabled", value="1" if enabled else "0"))
            else:
                setting.value = "1" if enabled else "0"
            session.commit()

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

    def start_scan(self, trigger: str = "manual") -> bool:
        if self.scan_is_running():
            return False
        self.scan_cancel_event = asyncio.Event()
        self.scan_progress = ScanProgress(state="running", stage="Подготовка проверки", started_at=datetime.now(UTC))
        self.scan_task = asyncio.create_task(self._scan_task(trigger))
        self.scan_task.add_done_callback(self._scan_task_done)
        return True

    def stop_scan(self) -> bool:
        if not self.scan_is_running():
            return False
        self.scan_cancel_event.set()
        self.scan_progress.state = "cancelled"
        self.scan_progress.stage = "Остановка проверки…"
        self.scan_progress.finished_at = datetime.now(UTC)
        self.scan_task.cancel()
        return True

    def _update_scan_progress(self, stage: str, completed: int, total: int, errors: int = 0) -> None:
        self.scan_progress.stage = stage
        self.scan_progress.completed = completed
        self.scan_progress.total = total
        self.scan_progress.errors = errors
        now = time.monotonic()
        message = getattr(self, "scan_status_message", None)
        if message and now - getattr(self, "scan_status_last_update", 0.0) >= 2:
            self.scan_status_last_update = now
            update_task = getattr(self, "scan_status_update_task", None)
            if update_task is None or update_task.done():
                self.scan_status_update_task = asyncio.create_task(self.refresh_scan_status())

    def remember_scan_message(self, chat_id: int, message_id: int) -> None:
        self.scan_status_message = (chat_id, message_id)

    async def refresh_scan_status(self) -> None:
        message = getattr(self, "scan_status_message", None)
        if not message:
            return
        chat_id, message_id = message
        text, markup = self.scan_progress_card()
        try:
            await self.edit_message(chat_id, message_id, text, markup)
        except RuntimeError as exc:
            if "message is not modified" not in str(exc).lower():
                log.debug("scan status message cannot be updated: %s", exc)
                self.scan_status_message = None

    def scan_progress_card(self) -> tuple[str, dict]:
        progress = getattr(self, "scan_progress", ScanProgress())
        if progress.state == "running":
            controls = [[
                {"text": "🔄 Обновить", "callback_data": "scan:status"},
                {"text": "⏹ Остановить", "callback_data": "scan:stop"},
            ]]
        elif progress.state in {"failed", "cancelled", "completed"}:
            controls = [[{"text": "🔁 Повторить проверку", "callback_data": "scan:retry"}]]
        else:
            controls = [[{"text": "▶️ Запустить проверку", "callback_data": "scan:run:confirm"}]]
        lines = [f"🔍 {progress.stage}"]
        if progress.total:
            lines.append(f"Прогресс: {progress.completed}/{progress.total} ({progress.percent}%)")
        if progress.errors:
            lines.append(f"Ошибок отдельных документов: {progress.errors}")
        if progress.last_error:
            lines.append(f"Последняя ошибка: {progress.last_error}")
        return "\n".join(lines), {"inline_keyboard": controls}

    def _scan_task_done(self, task: asyncio.Task[int]) -> None:
        if self.scan_task is task:
            self.scan_task = None
        if task.cancelled():
            return
        try:
            task.result()
        except Exception:
            # _scan_task already reports the failure. Retrieving the result
            # here prevents an unhandled-task warning and allows later scans.
            log.debug("background scan task finished with an error", exc_info=True)

    async def report_error(self, context: str, exc: Exception, *, notify_admin: bool = True) -> None:
        now = time.monotonic()
        log.warning("%s (%s): %s", context, type(exc).__name__, str(exc)[:300])
        if not notify_admin:
            return
        if now - self.last_error_notice < 60:
            return
        self.last_error_notice = now
        try:
            await self.send_temporary(settings.telegram_admin_id, f"🔴 {context}: {type(exc).__name__}: {str(exc)[:600]}", ttl=20)
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
        progress_text, _ = self.scan_progress_card()
        text = (f"{progress_text}\n\n"
                "ФСТЭК Monitor\n"
                f"Документы: {stats['documents']}\nВложения: {stats['attachments']}\n"
                f"Последняя проверка: {_dt(stats['last_scan'])}\nПоследнее изменение: {_dt(stats['last_change'])}\n"
                f"Статус изменений: {'есть новые' if stats['pending'] else 'изменений нет'}\n"
                f"Хранилище: {used / 1024**3:.2f} / {quota / 1024**3:.2f} ГБ\nОшибок в журнале: {stats['errors']}\n"
                f"Расписание: {schedule_label(mode)}\n"
                f"Следующая проверка: {_dt(next_run) if next_run else 'выключен'}\n"
                f"Состояние: {'проверка выполняется' if self.scan_is_running() else 'ожидание'}")
        if stats["last_duration"] is not None:
            text += f"\nДлительность последней проверки: {_fmt_duration(stats['last_duration'])}"
        if stats["avg_duration"] is not None:
            text += f"\nСредняя длительность ({stats['runs_count']} зап.): {_fmt_duration(stats['avg_duration'])}"
        return text

    def _status_stats(self) -> dict:
        with SessionLocal() as session:
            runs = session.scalars(
                select(ScanRun).where(ScanRun.finished_at.is_not(None)).order_by(ScanRun.id.desc()).limit(7)
            ).all()
            durations = []
            for run in runs:
                started, finished = run.started_at, run.finished_at
                if started and started.tzinfo is None:
                    started = started.replace(tzinfo=UTC)
                if finished and finished.tzinfo is None:
                    finished = finished.replace(tzinfo=UTC)
                if started and finished:
                    durations.append((finished - started).total_seconds())
            return {
                "documents": session.scalar(select(func.count(Document.id)).where(Document.active.is_(True))) or 0,
                "attachments": session.scalar(select(func.count(Attachment.id)).where(Attachment.active.is_(True))) or 0,
                "pending": session.scalar(select(func.count(Event.id)).where(Event.notified.is_(False), Event.kind.in_(MEANINGFUL_KINDS))) or 0,
                "last_scan": session.scalar(select(func.max(Snapshot.fetched_at))),
                "last_change": session.scalar(select(func.max(Event.created_at)).where(Event.kind.in_(MEANINGFUL_KINDS))),
                "errors": session.scalar(select(func.count(Event.id)).where(Event.kind.in_(ERROR_KINDS))) or 0,
                "last_duration": durations[0] if durations else None,
                "avg_duration": sum(durations) / len(durations) if durations else None,
                "runs_count": len(durations),
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

    def user_ignored_categories(self, user_id: int) -> list[str]:
        with SessionLocal() as session:
            return [
                item.category_name
                for item in session.scalars(
                    select(UserIgnoredCategory).where(UserIgnoredCategory.user_id == user_id).order_by(UserIgnoredCategory.category_name)
                ).all()
            ]

    def toggle_user_ignored_category(self, user_id: int, token: str) -> str | None:
        with SessionLocal() as session:
            known = [c for c in session.scalars(select(Document.category).distinct()).all() if c]
            target = next((c for c in sorted(known) if category_token(c) == token), None)
            if target is None:
                return None
            key = category_key(target)
            existing = session.scalar(
                select(UserIgnoredCategory).where(
                    UserIgnoredCategory.user_id == user_id,
                    UserIgnoredCategory.category_key == key,
                )
            )
            if existing:
                session.delete(existing)
                session.commit()
                return f"✅ Категория снова включена: {target}"
            session.add(UserIgnoredCategory(user_id=user_id, category_key=key, category_name=target))
            session.commit()
            return f"🚫 Категория скрыта из ваших уведомлений: {target}"

    def user_ignore_text(self, user_id: int) -> tuple[str, dict | None]:
        ignored = self.user_ignored_categories(user_id)
        ignored_keys = {category_key(c) for c in ignored}
        with SessionLocal() as session:
            known = sorted(c for c in session.scalars(select(Document.category).distinct()).all() if c)
        lines = ["🚫 Мои категории", "Выберите категории, по которым не получать уведомления:"]
        lines.append("Скрыты: " + ", ".join(ignored) if ignored else "Скрытых категорий нет")
        buttons = []
        for category in known[:30]:
            hidden = category_key(category) in ignored_keys
            buttons.append([{
                "text": f"{'✅' if hidden else '👁️'} {category[:40]}",
                "callback_data": f"userignore:t:{category_token(category)}",
            }])
        return "\n".join(lines), {"inline_keyboard": buttons} if buttons else None

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

        async def reply(text: str, markup: dict | None = None, fallback_chat_id: int | None = None) -> None:
            message = callback.get("message") or {}
            chat = message.get("chat") or {}
            message_id = message.get("message_id")
            chat_id = chat.get("id")
            if chat_id and message_id:
                if (callback.get("data") or "").startswith("scan:"):
                    self.remember_scan_message(chat_id, message_id)
                try:
                    await self.edit_message(chat_id, message_id, text, markup)
                    return
                except RuntimeError as exc:
                    if "message is not modified" in str(exc).lower():
                        return
                    log.debug("callback message cannot be edited: %s", exc)
            await self.send(fallback_chat_id or chat_id or settings.telegram_admin_id, text, markup)

        sender_id = sender.get("id")
        data = (callback.get("data") or "").split(":")
        if len(data) == 3 and data[0] == "userignore" and data[1] == "t":
            with SessionLocal() as session:
                user = session.get(UserAccess, sender_id) if sender_id else None
            if not is_allowed(user):
                return
            result = await asyncio.to_thread(self.toggle_user_ignored_category, sender_id, data[2])
            await reply(result or "Категория не найдена — откройте раздел заново.", fallback_chat_id=sender.get("id"))
            return
        if not is_admin(sender_id, settings.telegram_admin_id):
            return
        if data == ["menu", "main"]:
            await reply("Главное меню готово. Выберите действие:")
            return
        if len(data) == 3 and data[0] == "settings" and data[1] == "set":
            try:
                self.set_schedule_mode(data[2])
            except ValueError:
                await reply("Неизвестный режим расписания.")
                return
            await reply(f"✅ Расписание изменено: {schedule_label(data[2])}.", settings_keyboard(self.notifications_enabled()))
            return
        if data == ["settings", "notifications", "toggle"]:
            enabled = not self.notifications_enabled()
            self.set_notifications_enabled(enabled)
            await reply(
                f"{'🔔 Уведомления включены' if enabled else '🔕 Уведомления выключены'}.",
                settings_keyboard(enabled),
            )
            return
        if data == ["errors", "clear", "cancel"]:
            await reply("Очистка журнала ошибок отменена.")
            return
        if data == ["errors", "clear", "confirm"]:
            deleted = await asyncio.to_thread(self.clear_errors)
            await reply(f"🧹 Журнал ошибок очищен: удалено {deleted} событий.")
            return
        if data == ["scan", "status"]:
            text, markup = self.scan_progress_card()
            await reply(text, markup)
            return
        if data == ["scan", "stop"]:
            if not self.scan_is_running():
                text, markup = self.scan_progress_card()
                await reply(text, markup)
                return
            await reply("Остановить текущую проверку? Уже обработанные документы сохранятся.", {
                "inline_keyboard": [[
                    {"text": "⏹ Да, остановить", "callback_data": "scan:stop:confirm"},
                    {"text": "Отмена", "callback_data": "scan:stop:cancel"},
                ]]
            })
            return
        if data == ["scan", "stop", "cancel"]:
            text, markup = self.scan_progress_card()
            await reply("Остановка отменена.\n\n" + text, markup)
            return
        if data == ["scan", "stop", "confirm"]:
            if self.stop_scan():
                text, markup = self.scan_progress_card()
                await reply("⏹ Остановка проверки запрошена.\n\n" + text, markup)
            else:
                text, markup = self.scan_progress_card()
                await reply("Проверка уже завершена.\n\n" + text, markup)
            return
        if data == ["scan", "retry"]:
            if self.start_scan("retry"):
                text, markup = self.scan_progress_card()
                await reply(text, markup)
            else:
                text, markup = self.scan_progress_card()
                await reply("Повторный запуск не выполнен: проверка уже идёт.\n\n" + text, markup)
            return
        if data == ["scan", "run", "cancel"]:
            await reply("Запуск проверки отменён.")
            return
        if data == ["scan", "run", "confirm"]:
            if self.start_scan():
                text, markup = self.scan_progress_card()
                await reply("Проверка запущена в фоне.\n\n" + text, markup)
            else:
                text, markup = self.scan_progress_card()
                await reply("Проверка уже выполняется.\n\n" + text, markup)
            return
        if len(data) == 3 and data[0] == "ignore" and data[1] == "t":
            result = await asyncio.to_thread(self.toggle_ignored_category, data[2])
            await reply(result or "Категория не найдена — возможно, список изменился. Откройте /ignore заново.")
            return
        if len(data) != 3 or data[0] != "access" or data[1] not in {"approve", "deny"} or not data[2].isdigit():
            return
        user_id = int(data[2])
        status = "approved" if data[1] == "approve" else "denied"
        with SessionLocal() as session:
            user = session.get(UserAccess, user_id)
            if not user:
                await reply(f"Пользователь {user_id} не найден.")
                return
            user.status = status
            user.reviewed_at = datetime.now(UTC)
            session.commit()
            target_chat = user.chat_id
        await self.send(target_chat, "✅ Доступ разрешён. Используйте /help." if status == "approved" else "❌ В доступе отказано.")
        await reply(f"Пользователь {user_id}: {'доступ разрешён' if status == 'approved' else 'доступ отклонён'}.")

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
        text = {**ADMIN_LABEL_COMMANDS, **USER_LABEL_COMMANDS}.get(text, text)
        parts = text.split()
        command = parts[0].split("@", 1)[0].lower()
        try:
            if not is_admin(sender_id, settings.telegram_admin_id) and not is_user_command_allowed(command):
                await self.send(chat_id, "Доступно только получение обновлений и настройка личных категорий.")
                return
            if command in {"/start", "/help"}:
                if is_admin(sender_id, settings.telegram_admin_id):
                    await self.send(chat_id, "Меню администратора готово.\n\n📊 Статус — статистика, квота и расписание\n📰 Изменения — последние изменения\n🔍 Проверить сейчас — ручной запуск\n🧯 Ошибки — журнал ошибок\n👥 Пользователи — заявки на доступ\n🚫 Игнор категорий — глобальный ignore\n⚙️ Настройки — расписание и уведомления\n/report ID — подробный отчёт")
                else:
                    await self.send(chat_id, "Меню готово.\n\n📰 Последние изменения — сводка событий\n🚫 Мои категории — персональный список скрытых категорий\nℹ️ Вы получаете только уведомления об интересующих изменениях.")
            elif command == "/status":
                status_text = await self.status_text()
                _, status_markup = self.scan_progress_card()
                message_id = await self.send(chat_id, status_text, status_markup)
                if message_id and is_admin(sender_id, settings.telegram_admin_id) and self.scan_is_running():
                    self.remember_scan_message(chat_id, message_id)
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
            elif command == "/my_ignore":
                text_out, markup = await asyncio.to_thread(self.user_ignore_text, sender_id)
                await self.send(chat_id, text_out, markup)
            elif command == "/scan":
                if self.scan_is_running():
                    progress_text, progress_markup = self.scan_progress_card()
                    message_id = await self.send(chat_id, progress_text, progress_markup)
                    if message_id and is_admin(sender_id, settings.telegram_admin_id):
                        self.remember_scan_message(chat_id, message_id)
                else:
                    await self.send(chat_id, "Запустить полную проверку каталога ФСТЭК сейчас?", {
                        "inline_keyboard": [[
                            {"text": "▶️ Запустить", "callback_data": "scan:run:confirm"},
                            {"text": "Отмена", "callback_data": "scan:run:cancel"},
                        ]]
                    })
            elif command == "/settings":
                await self.send(chat_id, await asyncio.to_thread(self.settings_text), settings_keyboard(self.notifications_enabled()))
        except (OSError, RuntimeError, ValueError, httpx.HTTPError) as exc:
            await self.report_error(f"ошибка команды {command}", exc)
            await self.send_temporary(chat_id, "Не удалось выполнить команду. Ошибка записана и отправлена администратору.", ttl=15)
        finally:
            log.info("command %s took %.2fs", command, time.monotonic() - started_at)

    async def handle_update_safely(self, update: dict) -> None:
        """Keep one malformed update from terminating the long-poll loop."""
        try:
            await self.handle(update)
        except Exception as exc:
            log.exception("failed to process Telegram update")
            await self.report_error("ошибка обработки update", exc)

    async def run(self) -> None:
        init_db()
        try:
            await self.configure_menu()
        except (OSError, RuntimeError, ValueError, httpx.HTTPError) as exc:
            await self.report_error("не удалось настроить меню Telegram", exc, notify_admin=False)
        self.schedule_mode = self.get_schedule_mode()
        self.next_scan_at = next_scheduled_at(self.schedule_mode, datetime.now().astimezone())
        while True:
            current_mode = self.get_schedule_mode()
            if current_mode != self.schedule_mode:
                self.schedule_mode = current_mode
                self.next_scan_at = next_scheduled_at(current_mode, datetime.now().astimezone())
            now = datetime.now().astimezone()
            if self.next_scan_at is not None and now >= self.next_scan_at:
                self.start_scan("auto")
                self.next_scan_at = next_scheduled_at(current_mode, now + timedelta(seconds=1))
            payload = {"timeout": 25, "allowed_updates": ["message", "callback_query"]}
            if self.offset is not None:
                payload["offset"] = self.offset
            try:
                updates = await self.call("getUpdates", payload)
                for update in updates:
                    self.offset = update["update_id"] + 1
                    await self.handle_update_safely(update)
            except httpx.TimeoutException as exc:
                # Long polling sits on a slow request by design; a read timeout is
                # a transient blip, not an incident worth paging the admin about.
                log.info("getUpdates %s, retrying long poll", type(exc).__name__)
                await asyncio.sleep(1)
            except (httpx.HTTPError, RuntimeError) as exc:
                await self.report_error("ошибка Telegram API", exc, notify_admin=False)
                await asyncio.sleep(5)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    bot = TelegramBot()
    try:
        await bot.run()
    finally:
        await bot.close()


if __name__ == "__main__":
    # httpx INFO logs full request URLs, which would leak the bot token into journald.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(main())
