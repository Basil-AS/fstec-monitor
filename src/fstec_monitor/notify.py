from __future__ import annotations

import logging
from html import escape

import httpx
from sqlalchemy import select

from .config import settings
from .models import Document, Event
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
    if not settings.telegram_bot_token or not settings.telegram_chat_id: return 0
    events=session.scalars(select(Event).where(Event.notified.is_(False)).order_by(Event.id)).all()
    sent = 0
    async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0)) as client:
        error_events = [event for event in events if event.kind in ERROR_KINDS]
        notified_errors = session.scalars(
            select(Event).where(Event.kind.in_(ERROR_KINDS), Event.notified.is_(True))
        ).all()
        error_events, duplicate_errors = split_new_errors(error_events, notified_errors)
        for event in duplicate_errors:
            event.notified = True
        if duplicate_errors:
            session.commit()
        if error_events:
            try:
                r = await client.post(
                    api_url(settings.telegram_api_root, settings.telegram_bot_token, "sendMessage"),
                    json={
                        "chat_id": settings.telegram_chat_id,
                        "text": format_error_digest(error_events),
                        "parse_mode": "HTML",
                        "disable_web_page_preview": True,
                    },
                )
                r.raise_for_status()
                body = r.json()
                if not body.get("ok"):
                    raise RuntimeError(body.get("description", "Telegram API rejected error digest"))
            except (httpx.HTTPError, RuntimeError) as exc:
                log.warning("error digest failed: %s", exc)
            else:
                for event in error_events:
                    event.notified = True
                session.commit()
                sent += 1

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
        if other_events:
            session.commit()
        if unique_events:
            documents = {
                document.id: document
                for document in session.scalars(
                    select(Document).where(Document.id.in_({event.document_id for event in unique_events if event.document_id}))
                ).all()
            }
            try:
                r = await client.post(
                    api_url(settings.telegram_api_root, settings.telegram_bot_token, "sendMessage"),
                    json={
                        "chat_id": settings.telegram_chat_id,
                        "text": format_change_digest(unique_events, documents),
                        "parse_mode": "HTML",
                        "disable_web_page_preview": True,
                    },
                )
                r.raise_for_status()
                body = r.json()
                if not body.get("ok"):
                    raise RuntimeError(body.get("description", "Telegram API rejected notification digest"))
            except (httpx.HTTPError, RuntimeError) as exc:
                log.warning("notification digest failed: %s", exc)
            else:
                for event in unique_events:
                    event.notified = True
                session.commit()
                sent += 1
    return sent
