from __future__ import annotations

import logging
from html import escape

import httpx
from sqlalchemy import select

from .config import settings
from .crawler import category_key
from .models import BotSetting, Document, Event, EventDelivery, UserAccess, UserIgnoredCategory
from .telegram_bot import api_url

log = logging.getLogger(__name__)


def format_event(e: Event, document_url: str = "") -> str:
    icon={"critical":"🔴","warning":"🟠","info":"🔵"}.get(e.severity,"⚪")
    link = f'\n\n🔗 <a href="{escape(document_url, quote=True)}">Открыть страницу документа</a>' if document_url else ""
    return f"{icon} <b>ФСТЭК: {escape(e.summary)}</b>\n\n<code>{escape(e.kind)}</code>\n{escape(e.details[:3000])}{link}"


def format_change_digest(events: list[Event], documents: dict[int, Document]) -> str:
    lines = [f"🔔 Изменения ФСТЭК: {len(events)} событий"]
    for event in events[:20]:
        icon = {"critical": "🔴", "warning": "🟠", "info": "🔵"}.get(event.severity, "⚪")
        document = documents.get(event.document_id) if event.document_id else None
        link = ""
        if document:
            link = f' · <a href="{escape(document.canonical_url, quote=True)}">открыть</a>'
        lines.append(f"{icon} <b>{escape(event.summary[:240])}</b> <code>{escape(event.kind)}</code>{link}")
    if len(events) > 20:
        lines.append(f"… и ещё {len(events) - 20}. Подробности: /changes")
    return "\n".join(lines)


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
ERROR_KINDS = {"fetch_error", "storage_error"}


def should_notify_event(e: Event) -> bool:
    """Return whether an event belongs in the normal change stream.

    Operational errors are delivered separately as one digest so a retry storm
    cannot become one Telegram message per failed request.
    """
    return e.kind in MEANINGFUL_KINDS


def format_error_digest(events: list[Event]) -> str:
    lines = [f"🔴 Ошибки проверки: {len(events)} событий"]
    for event in events[:20]:
        detail = event.details.replace("\n", " ")[:240]
        suffix = f" — {detail}" if detail else ""
        lines.append(f"• {escape(event.kind)}: {escape(event.summary)}{escape(suffix)}")
    if len(events) > 20:
        lines.append(f"… и ещё {len(events) - 20}")
    return "\n".join(lines)


def error_key(event: Event) -> tuple[str, int | None, str]:
    return event.kind, event.document_id, event.summary


def split_new_errors(events: list[Event], previously_notified: list[Event]) -> tuple[list[Event], list[Event]]:
    """Return first-seen errors and duplicates that can be silenced safely."""
    known = {error_key(event) for event in previously_notified}
    unique: list[Event] = []
    duplicates: list[Event] = []
    for event in events:
        key = error_key(event)
        if key in known:
            duplicates.append(event)
        else:
            known.add(key)
            unique.append(event)
    return unique, duplicates

async def notify_pending(session) -> int:
    if not settings.telegram_bot_token:
        return 0
    setting = session.get(BotSetting, "notifications_enabled")
    if setting and setting.value == "0":
        return 0

    events = session.scalars(select(Event).where(Event.notified.is_(False)).order_by(Event.id)).all()
    recipients = {settings.telegram_admin_id}
    if settings.telegram_chat_id:
        try:
            recipients.add(int(settings.telegram_chat_id))
        except ValueError:
            log.warning("invalid FSTEC_TELEGRAM_CHAT_ID; skipping configured chat")
    recipients.update(
        user.chat_id
        for user in session.scalars(select(UserAccess).where(UserAccess.status == "approved")).all()
    )
    recipients.discard(None)
    if not recipients:
        return 0

    error_events = [event for event in events if event.kind in ERROR_KINDS]
    notified_errors = session.scalars(
        select(Event).where(Event.kind.in_(ERROR_KINDS), Event.notified.is_(True))
    ).all()
    error_events, duplicate_errors = split_new_errors(error_events, notified_errors)
    for event in duplicate_errors:
        event.notified = True

    other_events = [event for event in events if event.kind not in ERROR_KINDS]
    meaningful_events = [event for event in other_events if should_notify_event(event)]
    for event in other_events:
        if not should_notify_event(event):
            event.notified = True
    seen: set[tuple[str, int | None, str, str]] = set()
    unique_events: list[Event] = []
    for event in meaningful_events:
        fingerprint = (event.kind, event.document_id, event.summary, event.details)
        if fingerprint in seen:
            event.notified = True
            continue
        seen.add(fingerprint)
        unique_events.append(event)
    session.commit()

    candidate_events = error_events + unique_events
    if not candidate_events:
        return 0
    documents = {
        document.id: document
        for document in session.scalars(
            select(Document).where(Document.id.in_({event.document_id for event in candidate_events if event.document_id}))
        ).all()
    }
    approved_users = session.scalars(select(UserAccess).where(UserAccess.status == "approved")).all()
    user_by_chat = {user.chat_id: user.user_id for user in approved_users}
    ignored_by_user: dict[int, set[str]] = {}
    if user_by_chat:
        ignored_rows = session.scalars(
            select(UserIgnoredCategory).where(UserIgnoredCategory.user_id.in_(set(user_by_chat.values())))
        ).all()
        for row in ignored_rows:
            ignored_by_user.setdefault(row.user_id, set()).add(row.category_key)
    sent = 0
    async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0)) as client:
        for chat_id in sorted(recipients):
            delivered = set(session.scalars(
                select(EventDelivery.event_id).where(EventDelivery.chat_id == chat_id)
            ).all())
            ignored = ignored_by_user.get(user_by_chat.get(chat_id), set())
            def visible(event: Event, ignored: set[str] = ignored) -> bool:
                document = documents.get(event.document_id) if event.document_id else None
                return not ignored or not document or category_key(document.category) not in ignored
            hidden_events = [event for event in candidate_events if event.id not in delivered and not visible(event)]
            for event in hidden_events:
                session.add(EventDelivery(event_id=event.id, chat_id=chat_id))
            if hidden_events:
                session.commit()
            pending_errors = [event for event in error_events if event.id not in delivered and visible(event)]
            pending_changes = [event for event in unique_events if event.id not in delivered and visible(event)]
            messages = (
                (format_error_digest(pending_errors), pending_errors),
                (format_change_digest(pending_changes, documents), pending_changes),
            )
            for message, message_events in messages:
                if not message_events:
                    continue
                if not message:
                    continue
                try:
                    r = await client.post(
                        api_url(settings.telegram_api_root, settings.telegram_bot_token, "sendMessage"),
                        json={
                            "chat_id": chat_id,
                            "text": message,
                            "parse_mode": "HTML",
                            "disable_web_page_preview": True,
                        },
                    )
                    r.raise_for_status()
                    body = r.json()
                    if not body.get("ok"):
                        raise RuntimeError(body.get("description", "Telegram API rejected notification"))
                except (httpx.HTTPError, RuntimeError) as exc:
                    log.warning("notification digest failed chat=%s: %s", chat_id, exc)
                    continue
                for event in message_events:
                    session.add(EventDelivery(event_id=event.id, chat_id=chat_id))
                session.commit()
                sent += 1

    for event in candidate_events:
        if all(
            session.scalar(select(EventDelivery.id).where(EventDelivery.event_id == event.id, EventDelivery.chat_id == chat_id))
            for chat_id in recipients
        ):
            event.notified = True
    session.commit()
    return sent
