from __future__ import annotations

import asyncio
import fcntl
import logging
from html import escape
from pathlib import Path
from urllib.parse import urlparse

import httpx
from sqlalchemy import select

from .config import settings
from .crawler import category_key
from .models import BotSetting, Document, Event, EventDelivery, UserAccess, UserIgnoredCategory
from .telegram_bot import api_url

log = logging.getLogger(__name__)
TELEGRAM_TEXT_LIMIT = 4096
_TELEGRAM_RETRYABLE_STATUS_CODES = frozenset({408, 425, 429, 500, 502, 503, 504})


def _notification_lock_path() -> Path:
    return Path(settings.storage_dir).resolve().parent / ".notify.lock"


def _acquire_notification_lock():
    path = _notification_lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    return handle


def _release_notification_lock(handle) -> None:
    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    handle.close()


def _single_flight(coro):
    async def wrapped(*args, **kwargs):
        handle = await asyncio.to_thread(_acquire_notification_lock)
        if handle is None:
            log.info("notification delivery already running; deferring this pass")
            return 0
        try:
            return await coro(*args, **kwargs)
        finally:
            await asyncio.to_thread(_release_notification_lock, handle)

    return wrapped


def format_event(e: Event, document_url: str = "") -> str:
    icon={"critical":"🔴","warning":"🟠","info":"🔵"}.get(e.severity,"⚪")
    link = f'\n\n🔗 <a href="{escape(document_url, quote=True)}">Открыть страницу документа</a>' if document_url else ""
    return f"{icon} <b>ФСТЭК: {escape(e.summary)}</b>\n\n<code>{escape(e.kind)}</code>\n{escape(e.details[:3000])}{link}"


def _attachment_variant_key(event: Event) -> str:
    """Normalize an attachment-added event so PDF/ODT variants share a key."""
    title = event.summary.partition(":")[2].strip().casefold()
    for suffix in (".odt", ".pdf"):
        if title.endswith(suffix):
            title = title[: -len(suffix)].rstrip(" ._-–—")
            break
    if title:
        return title
    return urlparse(event.details.splitlines()[0]).path.rsplit("/", 1)[-1].casefold()


def _deduplicate_attachment_additions(events: list[Event]) -> list[Event]:
    """Keep one user-facing add event per document attachment family.

    Both variants remain persisted and are still audited; the digest prefers
    ODT because it is the semantic comparison source.
    """
    result: list[Event] = []
    selected: dict[tuple[int | None, str], Event] = {}
    for event in events:
        if event.kind != "attachment_added":
            result.append(event)
            continue
        key = (event.document_id, _attachment_variant_key(event))
        previous = selected.get(key)
        if previous is None or event.details.lower().endswith(".odt"):
            selected[key] = event
    result.extend(sorted(selected.values(), key=lambda event: event.id or 0))
    return result


def format_change_digest(events: list[Event], documents: dict[int, Document]) -> str:
    grouped: dict[tuple[str, int | None], list[Event]] = {}
    for event in events:
        document = documents.get(event.document_id) if event.document_id else None
        category = getattr(document, "category", "") if document else ""
        category = category or "Без категории"
        grouped.setdefault((category, event.document_id), []).append(event)

    # A new document and its initial ODT/PDF links are one user-visible fact.
    # Keep the document event and suppress redundant attachment_added children.
    visible_groups: list[tuple[tuple[str, int | None], list[Event]]] = []
    for key, group in grouped.items():
        has_document_added = any(event.kind == "document_added" for event in group)
        if has_document_added:
            group = [event for event in group if event.kind != "attachment_added"]
        group = _deduplicate_attachment_additions(group)
        if group:
            visible_groups.append((key, sorted(group, key=lambda event: (event.kind != "document_added", event.id))))
    visible_groups.sort(key=lambda item: (item[0][0].casefold(), item[0][1] or 0))

    lines = [f"🔔 Изменения ФСТЭК: {sum(len(group) for _, group in visible_groups)} событий"]
    for (category, document_id), group in visible_groups[:20]:
        document = documents.get(document_id) if document_id else None
        title = getattr(document, "title", "") if document else "Общие события"
        link = f' · <a href="{escape(document.canonical_url, quote=True)}">открыть</a>' if document else ""
        lines.append(f"\n📁 <b>{escape(category)}</b>\n<b>{escape(title)}</b>")
        for event in group:
            icon = {"critical": "🔴", "warning": "🟠", "info": "🔵"}.get(event.severity, "⚪")
            lines.append(f"{icon} {escape(event.summary[:240])} <code>{escape(event.kind)}</code>{link}")
    if len(visible_groups) > 20:
        lines.append(f"… и ещё {len(visible_groups) - 20} документов. Подробности: /changes")
    return "\n".join(lines)


def _split_digest(header: str, rows: list[tuple[str, Event | None]]) -> list[tuple[str, list[Event]]]:
    parts: list[tuple[str, list[Event]]] = []
    lines = [header]
    events: list[Event] = []
    for line, event in rows:
        if len("\n".join(lines + [line])) > TELEGRAM_TEXT_LIMIT and len(lines) > 1:
            parts.append(("\n".join(lines), events))
            lines = [header]
            events = []
        lines.append(line)
        if event is not None:
            events.append(event)
    if len(lines) > 1:
        parts.append(("\n".join(lines), events))
    return parts


def _format_change_digest_parts(
    events: list[Event], documents: dict[int, Document]
) -> list[tuple[str, list[Event]]]:
    grouped: dict[tuple[str, int | None], list[Event]] = {}
    for event in events:
        document = documents.get(event.document_id) if event.document_id else None
        category = getattr(document, "category", "") if document else ""
        grouped.setdefault((category or "Без категории", event.document_id), []).append(event)

    visible_groups: list[tuple[tuple[str, int | None], list[Event]]] = []
    for key, group in grouped.items():
        if any(event.kind == "document_added" for event in group):
            group = [event for event in group if event.kind != "attachment_added"]
        group = _deduplicate_attachment_additions(group)
        if group:
            visible_groups.append((key, sorted(group, key=lambda event: (event.kind != "document_added", event.id))))
    visible_groups.sort(key=lambda item: (item[0][0].casefold(), item[0][1] or 0))

    rows: list[tuple[str, Event | None]] = []
    for (category, document_id), group in visible_groups[:20]:
        document = documents.get(document_id) if document_id else None
        title = getattr(document, "title", "") if document else "Общие события"
        link = f' · <a href="{escape(document.canonical_url, quote=True)}">открыть</a>' if document else ""
        rows.append((f"\n📁 <b>{escape(category)}</b>\n<b>{escape(title)}</b>", None))
        for event in group:
            icon = {"critical": "🔴", "warning": "🟠", "info": "🔵"}.get(event.severity, "⚪")
            rows.append((f"{icon} {escape(event.summary[:240])} <code>{escape(event.kind)}</code>{link}", event))
    if len(visible_groups) > 20:
        rows.append((f"… и ещё {len(visible_groups) - 20} документов. Подробности: /changes", None))
    return _split_digest(
        f"🔔 Изменения ФСТЭК: {sum(len(group) for _, group in visible_groups)} событий", rows
    )


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
    return _format_error_digest_parts(events)[0][0] if events else ""


def _format_error_digest_parts(events: list[Event]) -> list[tuple[str, list[Event]]]:
    rows: list[tuple[str, Event | None]] = []
    for event in events[:20]:
        detail = event.details.replace("\n", " ")[:240]
        suffix = f" — {detail}" if detail else ""
        rows.append((f"• {escape(event.kind)}: {escape(event.summary)}{escape(suffix)}", event))
    if len(events) > 20:
        rows.append((f"… и ещё {len(events) - 20}", None))
    return _split_digest(f"🔴 Ошибки проверки: {len(events)} событий", rows)


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


def _notification_recipients(session) -> set[int]:
    """Resolve configured and approved recipients once per delivery pass."""
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
    return recipients


def _pending_event_batches(session, events: list[Event]) -> tuple[list[Event], list[Event]]:
    """Filter, deduplicate, and persist event bookkeeping before delivery."""
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
    return error_events, unique_events


def _notification_context(session, candidate_events: list[Event]):
    documents = {
        document.id: document
        for document in session.scalars(
            select(Document).where(
                Document.id.in_({event.document_id for event in candidate_events if event.document_id})
            )
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
    global_ignore_setting = session.get(BotSetting, "ignored_categories")
    globally_ignored = {
        category_key(value)
        for value in (global_ignore_setting.value if global_ignore_setting else "").splitlines()
        if value.strip()
    }
    return documents, user_by_chat, ignored_by_user, globally_ignored


async def _deliver_to_recipients(
    session,
    recipients: set[int],
    candidate_events: list[Event],
    error_events: list[Event],
    unique_events: list[Event],
    documents: dict[int, Document],
    user_by_chat: dict[int, int],
    ignored_by_user: dict[int, set[str]],
    globally_ignored: set[str],
) -> int:
    sent = 0
    async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0)) as client:
        for chat_id in sorted(recipients):
            delivered = set(session.scalars(
                select(EventDelivery.event_id).where(EventDelivery.chat_id == chat_id)
            ).all())
            ignored = globally_ignored | ignored_by_user.get(user_by_chat.get(chat_id), set())

            def visible(
                event: Event,
                ignored: set[str] = ignored,
            ) -> bool:
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
                (_format_error_digest_parts(pending_errors), pending_errors),
                (_format_change_digest_parts(pending_changes, documents), pending_changes),
            )
            for parts, message_events in messages:
                if not message_events or not parts:
                    continue
                for message, part_events in parts:
                    try:
                        await _post_notification(
                            client,
                            api_url(settings.telegram_api_root, settings.telegram_bot_token, "sendMessage"),
                            {
                                "chat_id": chat_id,
                                "text": message,
                                "parse_mode": "HTML",
                                "link_preview_options": {"is_disabled": True},
                            },
                        )
                    except (httpx.HTTPError, RuntimeError) as exc:
                        log.warning("notification digest failed chat=%s: %s", chat_id, exc)
                        break
                    for event in part_events:
                        session.add(EventDelivery(event_id=event.id, chat_id=chat_id))
                    session.commit()
                    sent += 1
    return sent


async def _post_notification(client: httpx.AsyncClient, url: str, payload: dict) -> None:
    """Deliver one notification without duplicating an unknown send outcome.

    A transport timeout can happen after Telegram accepted ``sendMessage``.
    Retrying that request would create duplicate notifications, so only an
    explicit retryable HTTP response is retried.
    """
    attempts = max(1, settings.max_retries)
    last_error: Exception | None = None
    for attempt in range(attempts):
        response = None
        retry = False
        try:
            response = await client.post(url, json=payload)
            response.raise_for_status()
            body = response.json()
            if not body.get("ok"):
                raise RuntimeError(body.get("description", "Telegram API rejected notification"))
            return
        except httpx.HTTPStatusError as exc:
            last_error = exc
            retry = exc.response is not None and exc.response.status_code in _TELEGRAM_RETRYABLE_STATUS_CODES
        except (httpx.TimeoutException, httpx.TransportError, RuntimeError) as exc:
            last_error = exc
        finally:
            close = getattr(response, "aclose", None)
            if close is not None:
                try:
                    await close()
                except (OSError, RuntimeError, httpx.HTTPError) as exc:
                    log.debug("could not close notification response: %s", exc)
        if retry and attempt + 1 < attempts:
            await asyncio.sleep(min(60, 2**attempt))
            continue
        break
    assert last_error is not None
    raise last_error

@_single_flight
async def notify_pending(session) -> int:
    if not settings.telegram_bot_token:
        return 0
    setting = session.get(BotSetting, "notifications_enabled")
    if setting and setting.value == "0":
        return 0

    events = session.scalars(select(Event).where(Event.notified.is_(False)).order_by(Event.id)).all()
    recipients = _notification_recipients(session)
    if not recipients:
        return 0

    error_events, unique_events = _pending_event_batches(session, events)

    candidate_events = error_events + unique_events
    if not candidate_events:
        return 0
    documents, user_by_chat, ignored_by_user, globally_ignored = _notification_context(session, candidate_events)
    sent = await _deliver_to_recipients(
        session, recipients, candidate_events, error_events, unique_events,
        documents, user_by_chat, ignored_by_user, globally_ignored,
    )

    for event in candidate_events:
        if all(
            session.scalar(select(EventDelivery.id).where(EventDelivery.event_id == event.id, EventDelivery.chat_id == chat_id))
            for chat_id in recipients
        ):
            event.notified = True
    session.commit()
    return sent
