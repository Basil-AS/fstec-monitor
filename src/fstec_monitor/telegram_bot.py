from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
from sqlalchemy import delete, func, select, update

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
    DAILY_NOON,
    SCHEDULE_MODES,
    next_scheduled_at,
    schedule_label,
)
from .storage import ObjectStore, StorageQuotaExceeded
from .telegram import keyboards as telegram_keyboards
from .telegram.callbacks import decode_callback
from .telegram.lifecycle import MessageLifecycleManager, ProgressCoalescer
from .telegram.navigation import NavigationStack, main_screen, screen_with_navigation
from .telegram.rendering import escape_html, has_html_markup, render_scan_progress
from .telegram.ux.callbacks import CallbackCodec
from .telegram.ux.messages import MessageLedger
from .telegram.ux.models import ViewModel

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
    ("report", "последний отчёт или ID события"),
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
_IDEMPOTENT_TELEGRAM_METHODS = frozenset({
    "answerCallbackQuery",
    "deleteMessage",
    "editMessageReplyMarkup",
    "editMessageText",
    "getUpdates",
    "sendChatAction",
    "setChatMenuButton",
    "setMyCommands",
})
_TELEGRAM_RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})


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
    return telegram_keyboards.settings_keyboard(notifications_enabled)


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
    return hashlib.sha256(category_key(value).encode()).hexdigest()[:16]


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
        self.lifecycle = MessageLifecycleManager(self)
        # UX toolkit façade: lifecycle remains the single source of truth.
        self.ux = MessageLedger(self.lifecycle)
        self.ux_callbacks = CallbackCodec(namespace="ux")
        self.progress_coalescer = ProgressCoalescer(self._refresh_progress_screen, interval=2.0)
        self.navigation: dict[int, NavigationStack] = {}
        self.last_error_notice = 0.0
        self._quota_cache: tuple[float, tuple[int, int]] | None = None

    async def close(self) -> None:
        for task in getattr(self, "_temporary_delete_tasks", set()):
            task.cancel()
        progress_coalescer = getattr(self, "progress_coalescer", None)
        if progress_coalescer is not None:
            await progress_coalescer.close()
        lifecycle = getattr(self, "lifecycle", None)
        if lifecycle is not None:
            await lifecycle.close()
        await self.client.aclose()

    async def call(self, method: str, payload: dict) -> dict:
        url = api_url(settings.telegram_api_root, self.token, method)
        attempts = max(1, min(settings.max_retries, 3)) if method in _IDEMPOTENT_TELEGRAM_METHODS else 1
        for attempt in range(attempts):
            try:
                response = await self.client.post(url, json=payload)
                if response.status_code in _TELEGRAM_RETRYABLE_STATUS_CODES and attempt + 1 < attempts:
                    retry_after = response.headers.get("retry-after")
                    try:
                        delay = min(30.0, max(0.0, float(retry_after))) if retry_after else 2.0 ** attempt
                    except ValueError:
                        delay = 2.0 ** attempt
                    await asyncio.sleep(delay)
                    continue
                response.raise_for_status()
                body = response.json()
                if not body.get("ok"):
                    raise RuntimeError(f"Telegram API error in {method}: {body.get('description', 'unknown')}")
                return body.get("result", {})
            except (httpx.TimeoutException, httpx.TransportError):
                if attempt + 1 >= attempts:
                    raise
                await asyncio.sleep(min(30.0, 2.0 ** attempt))
        raise RuntimeError(f"Telegram API call exhausted retries: {method}")

    def _log_tgux(
        self,
        method: str,
        payload: dict,
        *,
        chat_id: int | None = None,
        screen: str | None = None,
        reason: str = "api",
    ) -> None:
        if os.getenv("FSTEC_TGUX_LOGGING", "0").casefold() not in {"1", "true", "yes", "on"}:
            return
        chat_id = payload.get("chat_id", chat_id if chat_id is not None else "?")
        message_id = payload.get("message_id", "-")
        log.info("TGUX chat=%s method=%s message_id=%s screen=%s reason=%s", chat_id, method, message_id, screen or "-", reason)

    async def send(self, chat_id: int, text: str, reply_markup: dict | None = None, *, screen: str | None = None, reason: str = "navigation") -> int | None:
        message_id = None
        for start in range(0, len(text), 3900):
            payload = {
                "chat_id": chat_id,
                "text": text[start:start + 3900],
                "link_preview_options": {"is_disabled": True},
            }
            if has_html_markup(text):
                payload["parse_mode"] = "HTML"
            # A split response is one logical message: keep actionable buttons
            # only on its tail fragment so navigation never gets duplicated.
            if reply_markup is not None and start + 3900 >= len(text):
                payload["reply_markup"] = reply_markup
            result = await self.call("sendMessage", payload)
            if isinstance(result, dict):
                message_id = result.get("message_id", message_id)
            self._log_tgux(
                "sendMessage",
                {**payload, "message_id": message_id} if message_id is not None else payload,
                screen=screen,
                reason=reason,
            )
        return message_id

    async def edit_message(self, chat_id: int, message_id: int, text: str, reply_markup: dict | None = None, *, screen: str | None = None, reason: str = "navigation") -> None:
        payload = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "link_preview_options": {"is_disabled": True},
        }
        if has_html_markup(text):
            payload["parse_mode"] = "HTML"
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        self._log_tgux("editMessageText", payload, screen=screen, reason=reason)
        await self.call("editMessageText", payload)

    async def delete_message(self, chat_id: int, message_id: int) -> None:
        payload = {"chat_id": chat_id, "message_id": message_id}
        self._log_tgux("deleteMessage", payload, reason="cleanup")
        await self.call("deleteMessage", payload)

    async def edit_message_reply_markup(self, chat_id: int, message_id: int, reply_markup: dict | None = None) -> None:
        payload = {"chat_id": chat_id, "message_id": message_id, "reply_markup": reply_markup or {"inline_keyboard": []}}
        self._log_tgux("editMessageReplyMarkup", payload, reason="stale-media-keyboard")
        await self.call("editMessageReplyMarkup", payload)

    async def answer_callback(self, callback_query_id: str, text: str = "", *, chat_id: int | None = None) -> None:
        payload = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text[:200]
        self._log_tgux("answerCallbackQuery", payload, chat_id=chat_id, reason="callback-toast")
        await self.call("answerCallbackQuery", payload)

    async def send_chat_action(self, chat_id: int, action: str) -> None:
        payload = {"chat_id": chat_id, "action": action}
        self._log_tgux("sendChatAction", payload, reason="long-operation")
        await self.call("sendChatAction", payload)

    async def _delete_later(self, chat_id: int, message_id: int, ttl: float) -> None:
        await asyncio.sleep(ttl)
        try:
            await self.delete_message(chat_id, message_id)
        except (OSError, RuntimeError, httpx.HTTPError) as exc:
            log.debug("temporary Telegram message already gone chat=%s message=%s: %s", chat_id, message_id, exc)

    async def send_temporary(self, chat_id: int, text: str, ttl: float = 8.0) -> None:
        lifecycle = getattr(self, "lifecycle", None)
        if lifecycle is not None:
            await lifecycle.show_temporary(chat_id, text, ttl)
            return
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

    async def send_file(self, chat_id: int, name: str, data: bytes, caption: str = "") -> int | None:
        await self.send_chat_action(chat_id, "upload_document")
        response = await self.client.post(
            api_url(settings.telegram_api_root, self.token, "sendDocument"),
            data={"chat_id": str(chat_id), "caption": caption[:900]},
            files={"document": (name, data, "text/markdown" if name.endswith(".md") else "application/octet-stream")},
        )
        response.raise_for_status()
        body = response.json()
        if not body.get("ok"):
            raise RuntimeError(f"Telegram API error in sendDocument: {body.get('description', 'unknown')}")
        result = body.get("result") or {}
        message_id = result.get("message_id") if isinstance(result, dict) else None
        payload = {"chat_id": chat_id}
        if message_id is not None:
            payload["message_id"] = message_id
        self._log_tgux("sendDocument", payload, reason="persistent-report")
        if message_id and getattr(self, "lifecycle", None) is not None:
            self.lifecycle.remember_message(chat_id, message_id, persistent=True)
        return message_id

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
            return count
        except asyncio.CancelledError:
            self.scan_progress.state = "cancelled"
            self.scan_progress.stage = "Проверка остановлена"
            self.scan_progress.finished_at = datetime.now(UTC)
            await self.refresh_scan_status()
            raise
        except Exception as exc:
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

    async def _refresh_progress_screen(self, _progress: ScanProgress) -> None:
        await self.refresh_scan_status()

    def _update_scan_progress(self, stage: str, completed: int, total: int, errors: int = 0) -> None:
        self.scan_progress.stage = stage
        self.scan_progress.completed = completed
        self.scan_progress.total = total
        self.scan_progress.errors = errors
        now = time.monotonic()
        message = getattr(self, "scan_status_message", None)
        coalescer = getattr(self, "progress_coalescer", None)
        if message and coalescer is not None:
            coalescer.submit(self.scan_progress)
        elif message and now - getattr(self, "scan_status_last_update", 0.0) >= 2:
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
        lifecycle = getattr(self, "lifecycle", None)
        if lifecycle is not None:
            lifecycle.adopt_screen(chat_id, message_id, "scan")
            try:
                refreshed_message_id = await lifecycle.show_progress(chat_id, text, markup)
                if isinstance(refreshed_message_id, int):
                    self.scan_status_message = (chat_id, refreshed_message_id)
            except (OSError, RuntimeError, TimeoutError, httpx.HTTPError) as exc:
                log.debug("scan status message cannot be updated: %s", exc)
            return
        try:
            await self.edit_message(chat_id, message_id, text, markup)
        except (OSError, RuntimeError, TimeoutError, httpx.HTTPError) as exc:
            error_text = str(exc).lower()
            if any(marker in error_text for marker in ("message to edit not found", "message not found", "can't be edited", "cannot be edited")):
                log.debug("scan status message cannot be updated: %s", exc)
                self.scan_status_message = None
            else:
                # A transient transport failure must not discard the pointer:
                # the next coalesced update can recover the same screen.
                log.debug("scan status update deferred: %s", exc)

    def scan_progress_card(self) -> tuple[str, dict]:
        progress = getattr(self, "scan_progress", ScanProgress())
        controls = telegram_keyboards.scan_keyboard(progress.state)
        elapsed = ""
        if progress.started_at:
            end = progress.finished_at or datetime.now(UTC)
            elapsed = f"\nПрошло: {_fmt_duration((end - progress.started_at).total_seconds())}"
        rendered = render_scan_progress(progress)
        if progress.documents:
            rendered += f"\nНайдено изменений: {progress.documents}"
        return rendered + elapsed, controls

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
        log.warning("%s (%s)", context, type(exc).__name__)
        if not notify_admin:
            return
        if self.last_error_notice and now - self.last_error_notice < 60:
            return
        self.last_error_notice = now
        try:
            await self.send_temporary(
                settings.telegram_admin_id,
                f"⚠️ Ошибка в операции: {context}. Подробности записаны в журнал.",
                ttl=20,
            )
        except Exception as notify_exc:  # noqa: BLE001 — reporting must never amplify an outage
            # The original operation already has a concise warning above. A failed
            # notification is expected during Telegram/network outages; logging a
            # second traceback only floods the journal and obscures the root error.
            log.warning("could not notify admin about error (%s)", type(notify_exc).__name__)

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

    def changes_text(self, limit: int = 10, offset: int = 0) -> str:
        with SessionLocal() as session:
            events = session.scalars(
                select(Event)
                .where(Event.kind.in_(MEANINGFUL_KINDS))
                .order_by(Event.id.desc())
                .offset(max(0, offset))
                .limit(max(1, min(limit, 30)))
            ).all()
            docs = {d.id: d for d in session.scalars(select(Document).where(Document.id.in_([e.document_id for e in events if e.document_id]))).all()}
        if not events:
            return "Изменений нет."
        lines = ["📰 Последние изменения"]
        grouped: dict[str, dict[int | None, list[Event]]] = {}
        for event in events:
            document = docs.get(event.document_id) if event.document_id else None
            category = document.category if document and document.category else "Без категории"
            grouped.setdefault(category, {}).setdefault(event.document_id, []).append(event)
        for category, documents in sorted(grouped.items(), key=lambda item: item[0].casefold()):
            visible_documents: list[tuple[int | None, list[Event]]] = []
            for document_id, document_events in documents.items():
                has_document_added = any(event.kind == "document_added" for event in document_events)
                visible = [
                    event for event in document_events
                    if not (has_document_added and event.kind == "attachment_added")
                ]
                # A document may expose both ODT and PDF links. If there is no
                # document_added event, retain one user-facing attachment event
                # per filename family while still auditing both variants.
                attachment_events: dict[str, Event] = {}
                compact: list[Event] = []
                for event in visible:
                    if event.kind != "attachment_added":
                        compact.append(event)
                        continue
                    title = event.summary.partition(":")[2].strip().casefold()
                    for suffix in (".odt", ".pdf"):
                        if title.endswith(suffix):
                            title = title[: -len(suffix)].rstrip(" ._-–—")
                            break
                    previous = attachment_events.get(title)
                    if previous is None or event.summary.casefold().endswith(".odt"):
                        attachment_events[title] = event
                visible = [*compact, *attachment_events.values()]
                if visible:
                    visible_documents.append((document_id, sorted(visible, key=lambda event: (event.kind != "document_added", event.id))))
            if not visible_documents:
                continue
            lines.append(f"\n📁 {escape_html(category)}")
            for document_id, grouped_events in sorted(
                visible_documents,
                key=lambda item: max((event.id or 0) for event in item[1]),
                reverse=True,
            ):
                document = docs.get(document_id) if document_id else None
                title = document.title if document else "Общие события"
                lines.append(f"<b>{escape_html(title)}</b>")
                for event in grouped_events:
                    lines.append(f"• #{event.id} {_dt(event.created_at)} — {escape_html(event.summary)}")
        lines.append("\nДля полного Markdown-отчёта: /report (без ID — последнее событие).")
        return "\n".join(lines)

    @staticmethod
    def latest_change_id() -> int | None:
        with SessionLocal() as session:
            return session.scalar(
                select(Event.id)
                .where(Event.kind.in_(MEANINGFUL_KINDS))
                .order_by(Event.id.desc())
                .limit(1)
            )

    def changes_page(self, page: int = 0, page_size: int = 5) -> tuple[str, list[list[dict]]]:
        page_size = max(1, min(page_size, 10))
        with SessionLocal() as session:
            total = session.scalar(select(func.count(Event.id)).where(Event.kind.in_(MEANINGFUL_KINDS))) or 0
        pages = max(1, (total + page_size - 1) // page_size)
        page = max(0, min(page, pages - 1))
        text = self.changes_text(page_size, page * page_size)
        if pages == 1:
            return text, []
        previous = page - 1 if page else pages - 1
        following = page + 1 if page + 1 < pages else 0
        controls = [[
            {"text": "◀️", "callback_data": telegram_keyboards.encode_callback("screen", f"changes-page-{previous}")},
            {"text": f"{page + 1}/{pages}", "callback_data": telegram_keyboards.encode_callback("nav", "noop")},
            {"text": "▶️", "callback_data": telegram_keyboards.encode_callback("screen", f"changes-page-{following}")},
        ]]
        return text, controls

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
            buttons.append([
                {
                    "text": f"✅ {identity[:30]}",
                    "callback_data": telegram_keyboards.encode_callback("access", f"approve-{user.user_id}"),
                },
                {
                    "text": "❌",
                    "callback_data": telegram_keyboards.encode_callback("access", f"deny-{user.user_id}"),
                },
            ])
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
                "callback_data": telegram_keyboards.encode_callback("userignore", category_token(category)),
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
            buttons.append([{"text": f"{mark} {category[:40]}", "callback_data": telegram_keyboards.encode_callback("ignore", category_token(category))}])
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
                {"text": "🧹 Очистить", "callback_data": telegram_keyboards.encode_callback("errors", "clear-confirm")},
                {"text": "Отмена", "callback_data": telegram_keyboards.encode_callback("errors", "clear-cancel")},
            ]]
        }

    def clear_errors(self) -> int:
        with SessionLocal() as session:
            result = session.execute(delete(Event).where(Event.kind.in_(ERROR_KINDS)))
            session.commit()
            return result.rowcount or 0

    async def request_access(self, user_id: int, chat_id: int, username: str, display_name: str) -> bool:
        locks = getattr(self, "_access_request_locks", None)
        if locks is None:
            locks = self._access_request_locks = {}
        lock = locks.setdefault(user_id, asyncio.Lock())
        async with lock:
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
                        {"text": "✅ Разрешить", "callback_data": telegram_keyboards.encode_callback("access", f"approve-{user_id}")},
                        {"text": "❌ Отклонить", "callback_data": telegram_keyboards.encode_callback("access", f"deny-{user_id}")},
                    ]]
                })
                with SessionLocal() as session:
                    user = session.get(UserAccess, user_id)
                    if user:
                        user.notification_sent = True
                        session.commit()
                await self.send(chat_id, "Заявка на доступ отправлена администратору. Ожидайте решения.")
            return False

    def _navigation_stack(self, chat_id: int) -> NavigationStack:
        navigation = getattr(self, "navigation", None)
        if navigation is None:
            navigation = self.navigation = {}
        return navigation.setdefault(chat_id, NavigationStack())

    async def _show_screen(
        self,
        chat_id: int,
        screen: str,
        text: str,
        markup: dict | None = None,
        *,
        reset: bool = False,
        source_message: dict | None = None,
        reason: str = "navigation",
        payload: object | None = None,
    ) -> int | None:
        stack = self._navigation_stack(chat_id)
        if reset:
            stack.reset(screen)
        else:
            stack.push(screen, payload)
        lifecycle = getattr(self, "lifecycle", None)
        if lifecycle is None:
            return await self.send(chat_id, text, markup)
        return await self.ux.edit_or_send_menu(
            chat_id,
            ViewModel(screen, text, markup, payload=payload),
            source_message=source_message,
            reason=reason,
        )

    async def _render_screen(
        self,
        chat_id: int,
        screen: str,
        *,
        reset: bool = False,
        source_message: dict | None = None,
        reason: str = "navigation",
        payload: object | None = None,
    ) -> int | None:
        is_admin_user = chat_id == settings.telegram_admin_id
        if screen == "main":
            view = main_screen(is_admin=is_admin_user)
        elif screen == "status":
            text = await self.status_text()
            _, markup = self.scan_progress_card()
            view = screen_with_navigation(screen, text, markup.get("inline_keyboard", []))
        elif screen == "changes":
            page = payload.get("page", 0) if isinstance(payload, dict) else 0
            text, rows = await asyncio.to_thread(self.changes_page, page)
            view = screen_with_navigation(screen, text, rows)
        elif screen == "scan":
            text, markup = self.scan_progress_card()
            view = screen_with_navigation(screen, text, markup.get("inline_keyboard", []))
        elif screen == "settings":
            text = await asyncio.to_thread(self.settings_text)
            view = screen_with_navigation(screen, text, settings_keyboard(self.notifications_enabled()).get("inline_keyboard", []))
        elif screen == "filters":
            text, markup = await asyncio.to_thread(self.ignore_text)
            view = screen_with_navigation(screen, text, markup.get("inline_keyboard", []) if markup else [])
        elif screen == "my_ignore":
            text, markup = await asyncio.to_thread(self.user_ignore_text, chat_id)
            view = screen_with_navigation(screen, text, markup.get("inline_keyboard", []) if markup else [])
        elif screen == "users":
            text, markup = await asyncio.to_thread(self.users_text)
            view = screen_with_navigation(screen, text, markup.get("inline_keyboard", []) if markup else [])
        elif screen == "errors":
            with SessionLocal() as session:
                errors = session.scalars(select(Event).where(Event.kind.in_(ERROR_KINDS)).order_by(Event.id.desc()).limit(10)).all()
            text = "\\n".join(f"#{event.id} {_dt(event.created_at)} {event.summary}: {event.details[:300]}" for event in errors) or "Ошибок нет."
            view = screen_with_navigation(screen, text, [])
        elif screen == "help":
            if is_admin_user:
                text = (
                    "ℹ️ <b>Доступные команды</b>\\n\\n"
                    "/start — главное меню\\n"
                    "/status — состояние и последний запуск\\n"
                    "/changes — последние изменения\\n"
                    "/report — отчёт по последнему событию\\n"
                    "/scan — запустить проверку\\n"
                    "/settings — расписание и уведомления\\n"
                    "/ignore — глобальные категории\\n"
                    "/users — заявки на доступ\\n"
                    "/errors — журнал ошибок"
                )
            else:
                text = (
                    "ℹ️ <b>Доступные действия</b>\\n\\n"
                    "📰 Последние изменения — новые документы и обновления\\n"
                    "🚫 Мои категории — персональные фильтры\\n"
                    "Используйте /start для возврата в меню."
                )
            view = screen_with_navigation(screen, text, [])
        else:
            view = screen_with_navigation(screen, "ℹ️ Раздел готов. Используйте кнопки ниже.", [])
        return await self._show_screen(
            chat_id,
            view.name,
            view.text,
            view.markup,
            reset=reset,
            source_message=source_message,
            reason=reason,
            payload=payload,
        )

    @staticmethod
    def _callback_toast(data: str) -> str:
        if data.startswith("v1:settings:"):
            return "Сохраняю настройки…"
        if data.startswith("v1:scan:run"):
            return "Запускаю проверку…"
        if data.startswith("v1:scan:stop"):
            return "Останавливаю проверку…"
        if data.startswith("v1:scan:retry"):
            return "Повторяю проверку…"
        if data.startswith(("v1:ignore:", "v1:userignore:")):
            return "Обновляю фильтр…"
        if data.startswith("v1:nav:"):
            return "Открываю…"
        return "Обновлено"

    async def _answer_callback(
        self,
        callback_id: str | None,
        raw_data: str,
        stale: bool,
        chat_id: int | None = None,
    ) -> None:
        """Acknowledge a callback before dispatching any potentially slow work."""
        if not callback_id:
            return
        toast = "Экран устарел" if stale else self._callback_toast(raw_data)
        if raw_data in {"v1:scan:run", "v1:scan:retry"} and self.scan_is_running():
            toast = "Уже выполняется"
        elif raw_data == "v1:scan:stop" and not self.scan_is_running():
            toast = "Проверка уже завершена"
        answer = getattr(self, "answer_callback", None)
        try:
            if answer is not None:
                await answer(callback_id, toast, chat_id=chat_id)
            else:
                payload = {"callback_query_id": callback_id, "text": toast}
                self._log_tgux("answerCallbackQuery", payload, chat_id=chat_id, reason="callback-toast")
                await self.call("answerCallbackQuery", payload)
        except (OSError, RuntimeError, httpx.HTTPError) as exc:
            log.warning("answerCallbackQuery failed (expired query?): %s", exc)

    def _adopt_callback_screen(self, callback: dict) -> tuple[dict, int | None, int | None, bool]:
        """Validate callback freshness and bind its message to the lifecycle."""
        message = callback.get("message") or {}
        chat_id = (message.get("chat") or {}).get("id")
        message_id = message.get("message_id")
        lifecycle = getattr(self, "lifecycle", None)
        is_stale = bool(
            lifecycle is not None
            and chat_id
            and message_id
            and not lifecycle.is_media_message(message)
            and lifecycle.session(chat_id).message_id is not None
            and not lifecycle.is_current_screen_message(chat_id, message_id)
        )
        if lifecycle is not None and chat_id and message_id:
            if is_stale:
                log.info("ignoring stale callback chat=%s message=%s", chat_id, message_id)
            else:
                lifecycle.adopt_screen(chat_id, message_id, self._navigation_stack(chat_id).current, message=message)
        return message, chat_id, message_id, is_stale

    async def _dispatch_decoded_callback(
        self,
        action: str,
        value: str,
        chat_id: int | None,
        sender_id: int | None,
        message: dict,
        message_id: int | None,
        reply,
    ) -> bool:
        """Handle namespaced navigation/scan/settings callbacks."""
        if action == "menu" and value == "main":
            await self._render_screen(chat_id or sender_id, "main", reset=True, source_message=message)
            return True
        if action == "nav" and value == "back":
            stack = self._navigation_stack(chat_id or sender_id)
            screen, payload = stack.back_frame()
            await self._render_screen(
                chat_id or sender_id,
                screen,
                reset=True,
                source_message=message,
                reason="back",
                payload=payload,
            )
            return True
        if action == "screen":
            if value.startswith("changes-page-"):
                page = value.removeprefix("changes-page-")
                if page.isdigit():
                    stack = self._navigation_stack(chat_id or sender_id)
                    stack.replace("changes", {"page": int(page)})
                    await self._render_screen(chat_id or sender_id, "changes", source_message=message, payload={"page": int(page)})
                return True
            await self._render_screen(chat_id or sender_id, value, source_message=message)
            return True
        if action == "scan":
            target = chat_id or sender_id
            if value in {"run-cancel", "stop-cancel"}:
                await self._render_screen(target, "scan", source_message=message, reason="cancel")
                return True
            if value == "run":
                if self.start_scan():
                    lifecycle = getattr(self, "lifecycle", None)
                    current_message_id = message_id or (lifecycle.session(target).message_id if lifecycle else None)
                    if current_message_id:
                        self.remember_scan_message(target, current_message_id)
                await self._render_screen(target, "scan", source_message=message, reason="scan-start")
                return True
            if value == "retry":
                self.start_scan("retry")
                await self._render_screen(target, "scan", source_message=message, reason="scan-retry")
                return True
            if value == "status":
                await self._render_screen(target, "scan", source_message=message, reason="progress")
                return True
            if value == "stop":
                if not self.scan_is_running():
                    await self._render_screen(target, "scan", source_message=message, reason="scan-finished")
                    return True
                await self._show_screen(
                    target,
                    "scan",
                    "⏹ Остановить текущую проверку? Уже обработанные документы сохранятся.",
                    {"inline_keyboard": [[
                        {"text": "⏹ Да, остановить", "callback_data": telegram_keyboards.encode_callback("scan", "stop-confirm")},
                        {"text": "Отмена", "callback_data": telegram_keyboards.encode_callback("scan", "stop-cancel")},
                    ]]},
                    source_message=message,
                    reason="confirmation",
                )
                return True
            if value == "stop-confirm":
                self.stop_scan()
                await self._render_screen(target, "scan", source_message=message, reason="scan-stop")
                return True
        if action == "settings":
            if value.startswith("set-"):
                mode = value.removeprefix("set-")
                try:
                    self.set_schedule_mode(mode)
                except ValueError:
                    await reply("Неизвестный режим расписания.")
                    return True
                await self._render_screen(chat_id or sender_id, "settings", reset=True)
                return True
            if value == "notifications":
                self.set_notifications_enabled(not self.notifications_enabled())
                await self._render_screen(chat_id or sender_id, "settings", reset=True)
                return True
        if action == "errors":
            if value == "clear-cancel":
                await reply("Очистка журнала ошибок отменена.")
                return True
            if value == "clear-confirm":
                deleted = await asyncio.to_thread(self.clear_errors)
                await reply(f"🧹 Журнал ошибок очищен: удалено {deleted} событий.")
                return True
        return False

    async def _handle_access_decision(self, value: str, reply) -> None:
        """Settle an access request exactly once and close its action buttons."""
        try:
            decision, raw_user_id = value.split("-", 1)
            user_id = int(raw_user_id)
        except (ValueError, AttributeError):
            await reply("Заявка устарела.", {"inline_keyboard": []})
            return
        if decision not in {"approve", "deny"} or user_id <= 0:
            await reply("Заявка устарела.", {"inline_keyboard": []})
            return

        status = "approved" if decision == "approve" else "denied"
        target_chat = None
        with SessionLocal() as session:
            user = session.get(UserAccess, user_id)
            if not user:
                await reply("Заявка уже недоступна.", {"inline_keyboard": []})
                return
            result = session.execute(
                update(UserAccess)
                .where(UserAccess.user_id == user_id, UserAccess.status == "pending")
                .values(status=status, reviewed_at=datetime.now(UTC))
            )
            if result.rowcount != 1:
                verdict = "разрешён" if user.status == "approved" else "отклонён"
                await reply(f"Решение уже принято: доступ {verdict}.", {"inline_keyboard": []})
                return
            target_chat = user.chat_id
            session.commit()

        if target_chat:
            await self.send(
                target_chat,
                "✅ Доступ разрешён. Используйте /help." if status == "approved" else "❌ В доступе отказано.",
            )
        await reply(
            f"Пользователь {user_id}: {'доступ разрешён' if status == 'approved' else 'доступ отклонён'}.",
            {"inline_keyboard": []},
        )

    async def _dispatch_legacy_scan_callback(self, data: list[str], reply) -> bool:
        """Keep compatibility with scan buttons from pre-v1 bot messages."""
        if data == ["scan", "status"]:
            text, markup = self.scan_progress_card()
            await reply(text, markup)
            return True
        if data == ["scan", "stop"]:
            if not self.scan_is_running():
                text, markup = self.scan_progress_card()
                await reply(text, markup)
                return True
            await reply("Остановить текущую проверку? Уже обработанные документы сохранятся.", {
                "inline_keyboard": [[
                    {"text": "⏹ Да, остановить", "callback_data": telegram_keyboards.encode_callback("scan", "stop-confirm")},
                    {"text": "Отмена", "callback_data": telegram_keyboards.encode_callback("scan", "stop-cancel")},
                ]]
            })
            return True
        if data == ["scan", "stop", "cancel"]:
            text, markup = self.scan_progress_card()
            await reply("Остановка отменена.\n\n" + text, markup)
            return True
        if data == ["scan", "stop", "confirm"]:
            if self.stop_scan():
                text, markup = self.scan_progress_card()
                await reply("⏹ Остановка проверки запрошена.\n\n" + text, markup)
            else:
                text, markup = self.scan_progress_card()
                await reply("Проверка уже завершена.\n\n" + text, markup)
            return True
        if data == ["scan", "retry"]:
            if self.start_scan("retry"):
                text, markup = self.scan_progress_card()
                await reply(text, markup)
            else:
                text, markup = self.scan_progress_card()
                await reply("Повторный запуск не выполнен: проверка уже идёт.\n\n" + text, markup)
            return True
        if data == ["scan", "run", "cancel"]:
            await reply("Запуск проверки отменён.")
            return True
        if data == ["scan", "run", "confirm"]:
            if self.start_scan():
                text, markup = self.scan_progress_card()
                await reply("Проверка запущена в фоне.\n\n" + text, markup)
            else:
                text, markup = self.scan_progress_card()
                await reply("Проверка уже выполняется.\n\n" + text, markup)
            return True
        return False

    async def _dispatch_legacy_settings_callback(self, data: list[str], reply) -> bool:
        """Keep pre-v1 settings and error-cleanup buttons working."""
        if len(data) == 3 and data[0] == "settings" and data[1] == "set":
            try:
                self.set_schedule_mode(data[2])
            except ValueError:
                await reply("Неизвестный режим расписания.")
                return True
            await reply(f"✅ Расписание изменено: {schedule_label(data[2])}.", settings_keyboard(self.notifications_enabled()))
            return True
        if data == ["settings", "notifications", "toggle"]:
            enabled = not self.notifications_enabled()
            self.set_notifications_enabled(enabled)
            await reply(
                f"{'🔔 Уведомления включены' if enabled else '🔕 Уведомления выключены'}.",
                settings_keyboard(enabled),
            )
            return True
        if data == ["errors", "clear", "cancel"]:
            await reply("Очистка журнала ошибок отменена.")
            return True
        if data == ["errors", "clear", "confirm"]:
            deleted = await asyncio.to_thread(self.clear_errors)
            await reply(f"🧹 Журнал ошибок очищен: удалено {deleted} событий.")
            return True
        return False

    async def _dispatch_legacy_admin_callback(self, data: list[str], reply) -> bool:
        """Keep pre-v1 admin menu, filter, and access buttons working."""
        if data == ["menu", "main"]:
            await reply("Главное меню готово. Выберите действие:")
            return True
        if len(data) == 3 and data[0] == "ignore" and data[1] == "t":
            result = await asyncio.to_thread(self.toggle_ignored_category, data[2])
            await reply(result or "Категория не найдена — возможно, список изменился. Откройте /ignore заново.")
            return True
        if len(data) == 3 and data[0] == "access" and data[1] in {"approve", "deny"} and data[2].isdigit():
            await self._handle_access_decision(f"{data[1]}-{data[2]}", reply)
            return True
        return False

    async def _dispatch_legacy_user_callback(self, data: list[str], sender_id: int | None, reply) -> bool:
        """Keep pre-v1 personal category-filter buttons working for approved users."""
        if len(data) != 3 or data[:2] != ["userignore", "t"]:
            return False
        with SessionLocal() as session:
            user = session.get(UserAccess, sender_id) if sender_id else None
        if not is_allowed(user):
            return True
        result = await asyncio.to_thread(self.toggle_user_ignored_category, sender_id, data[2])
        await reply(result or "Категория не найдена — откройте раздел заново.", fallback_chat_id=sender_id)
        return True

    async def _reply_to_callback(
        self,
        callback: dict,
        text: str,
        markup: dict | None = None,
        fallback_chat_id: int | None = None,
    ) -> None:
        """Render callback feedback through the current lifecycle screen."""
        message = callback.get("message") or {}
        chat = message.get("chat") or {}
        message_id = message.get("message_id")
        chat_id = chat.get("id")
        lifecycle = getattr(self, "lifecycle", None)
        if lifecycle is not None and chat_id:
            if (callback.get("data") or "").startswith("scan:"):
                self.remember_scan_message(chat_id, message_id or lifecycle.session(chat_id).message_id)
            screen = self._navigation_stack(chat_id).current
            await lifecycle.show_screen(
                chat_id,
                screen,
                text,
                markup,
                source_message=message,
                reason="callback",
            )
            return
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

    async def handle_callback(self, callback: dict) -> None:
        callback_id = callback.get("id")
        sender = callback.get("from") or {}
        raw_data = callback.get("data") or ""
        callback_message, callback_chat_id, callback_message_id, stale_callback = self._adopt_callback_screen(callback)
        await self._answer_callback(callback_id, raw_data, stale_callback, callback_chat_id)

        async def reply(text: str, markup: dict | None = None, fallback_chat_id: int | None = None) -> None:
            await self._reply_to_callback(callback, text, markup, fallback_chat_id)

        sender_id = sender.get("id")
        data = raw_data.split(":")
        decoded = decode_callback(raw_data)
        message = callback_message
        chat_id = callback_chat_id
        message_id = callback_message_id
        if stale_callback:
            return
        if decoded:
            action, value = decoded
            admin_screens = {"status", "scan", "settings", "filters", "users", "errors"}
            admin_user = is_admin(sender_id, settings.telegram_admin_id)
            if ((action == "screen" and value in admin_screens) or action == "settings") and not admin_user:
                return
            if not admin_user:
                with SessionLocal() as session:
                    user = session.get(UserAccess, sender_id) if sender_id else None
                if not is_allowed(user):
                    return
            if await self._dispatch_decoded_callback(
                action, value, chat_id, sender_id, message, message_id, reply
            ):
                return
        if await self._dispatch_legacy_user_callback(data, sender_id, reply):
            return
        if decoded and decoded[0] in {"ignore", "userignore"}:
            action, token = decoded
            user_id = sender_id if action == "userignore" else None
            if action == "userignore":
                with SessionLocal() as session:
                    user = session.get(UserAccess, user_id) if user_id else None
                if not is_allowed(user):
                    return
                result = await asyncio.to_thread(self.toggle_user_ignored_category, user_id, token)
            else:
                result = await asyncio.to_thread(self.toggle_ignored_category, token)
            await reply(result or "Категория не найдена — откройте фильтры заново.")
            return
        if not is_admin(sender_id, settings.telegram_admin_id):
            return
        if await self._dispatch_legacy_settings_callback(data, reply):
            return
        if await self._dispatch_legacy_scan_callback(data, reply):
            return
        if await self._dispatch_legacy_admin_callback(data, reply):
            return
        if len(data) == 3 and data[0] == "v1" and data[1] == "access":
            await self._handle_access_decision(data[2], reply)
            return
        return

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
        try:
            store.save_report(event_id, report.encode("utf-8"))
        except (OSError, StorageQuotaExceeded) as exc:
            log.warning("report persistence failed event_id=%s error=%s", event_id, type(exc).__name__)
        return report, files

    async def send_report(self, chat_id: int, event_id: int) -> None:
        send_chat_action = getattr(self, "send_chat_action", None)
        if send_chat_action is not None:
            await send_chat_action(chat_id, "upload_document")
        result = await asyncio.to_thread(self._build_report, event_id)
        if result is None:
            await self.send_temporary(chat_id, f"Событие #{event_id} не найдено.", ttl=8)
            return
        report, files = result
        await self.send_file(chat_id, f"diff_{event_id}.md", report.encode(), "Подробный отчёт: old/new и diff")
        for name, data, caption in files:
            await self.send_file(chat_id, name, data, caption)
        if not files:
            await self.send_temporary(
                chat_id,
                "Старую/новую версию не отправил: файл отсутствует или превышает лимит Telegram. История сохранена на сервере.",
                ttl=12,
            )

    async def _dispatch_command(self, command: str, parts: list[str], chat_id: int, sender_id: int | None) -> None:
        """Route an authorized command to its screen or persistent operation."""
        if not is_admin(sender_id, settings.telegram_admin_id) and not is_user_command_allowed(command):
            await self.send_temporary(
                chat_id,
                "Доступно только получение обновлений и настройка личных категорий.",
                ttl=8,
            )
            return
        if command == "/start":
            await self._render_screen(chat_id, "main", reset=True)
        elif command == "/help":
            await self._render_screen(chat_id, "help", reset=True)
        elif command == "/status":
            message_id = await self._render_screen(chat_id, "status", reset=True)
            if message_id and is_admin(sender_id, settings.telegram_admin_id) and self.scan_is_running():
                self.remember_scan_message(chat_id, message_id)
        elif command in {"/changes", "/events"}:
            await self._render_screen(chat_id, "changes", reset=True)
        elif command in {"/report", "/diff"}:
            event_id = int(parts[1]) if len(parts) == 2 and parts[1].isdigit() else await asyncio.to_thread(self.latest_change_id)
            if event_id is None:
                await self.send_temporary(chat_id, "Изменений для отчёта пока нет.", ttl=8)
            else:
                await self.send_report(chat_id, event_id)
        elif command == "/errors":
            await self._render_screen(chat_id, "errors", reset=True)
        elif command == "/clear_errors":
            text_out, markup = await asyncio.to_thread(self.clear_errors_text)
            await self._show_screen(chat_id, "errors", text_out, markup, reset=True)
        elif command == "/users":
            await self._render_screen(chat_id, "users", reset=True)
        elif command == "/ignore":
            await self._render_screen(chat_id, "filters", reset=True)
        elif command == "/my_ignore":
            await self._render_screen(chat_id, "my_ignore", reset=True)
        elif command == "/scan":
            if self.scan_is_running():
                message_id = await self._render_screen(chat_id, "scan", reset=True)
                if message_id and is_admin(sender_id, settings.telegram_admin_id):
                    self.remember_scan_message(chat_id, message_id)
            else:
                await self._show_screen(
                    chat_id,
                    "scan",
                    "🔍 Запустить полную проверку каталога ФСТЭК сейчас?",
                    telegram_keyboards.scan_confirmation_keyboard(),
                    reset=True,
                )
        elif command == "/settings":
            await self._render_screen(chat_id, "settings", reset=True)

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
        lifecycle = getattr(self, "lifecycle", None)
        if lifecycle is not None:
            lifecycle.remember_context_message(chat_id, message)
            await lifecycle.cleanup_trigger_message(chat_id, message)
        sender_id = sender.get("id")
        if (not is_admin(sender_id, settings.telegram_admin_id)
                and (not sender_id or not await self.request_access(sender_id, chat_id, sender.get("username", ""), " ".join(filter(None, [sender.get("first_name"), sender.get("last_name")]))))):
            return
        parts = text.split()
        text = {**ADMIN_LABEL_COMMANDS, **USER_LABEL_COMMANDS}.get(text, text)
        parts = text.split()
        command = parts[0].split("@", 1)[0].lower()
        try:
            await self._dispatch_command(command, parts, chat_id, sender_id)
        except (OSError, RuntimeError, ValueError, httpx.HTTPError) as exc:
            await self.report_error(f"ошибка команды {command}", exc)
            await self._show_screen(
                chat_id,
                "error",
                "⚠️ Не удалось выполнить действие.\nОшибка записана в журнал.",
                {"inline_keyboard": [[
                    {"text": "🔁 Повторить", "callback_data": telegram_keyboards.encode_callback("screen", "help")},
                    {"text": "🏠 Главное меню", "callback_data": telegram_keyboards.encode_callback("menu", "main")},
                ]]},
                reset=True,
                reason="error",
            )
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
