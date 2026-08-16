from __future__ import annotations

from html import escape

import httpx
from sqlalchemy import select

from .config import settings
from .models import Document, Event
from .telegram_bot import api_url


def format_event(e: Event, document_url: str = "") -> str:
    icon={"critical":"🔴","warning":"🟠","info":"🔵"}.get(e.severity,"⚪")
    link = f'\n\n🔗 <a href="{escape(document_url, quote=True)}">Открыть страницу документа</a>' if document_url else ""
    return f"{icon} <b>ФСТЭК: {escape(e.summary)}</b>\n\n<code>{escape(e.kind)}</code>\n{escape(e.details[:3000])}{link}"


def should_notify_event(e: Event) -> bool:
    return True

async def notify_pending(session) -> int:
    if not settings.telegram_bot_token or not settings.telegram_chat_id: return 0
    events=session.scalars(select(Event).where(Event.notified.is_(False)).order_by(Event.id)).all()
    async with httpx.AsyncClient(timeout=30) as client:
        for e in events:
            if not should_notify_event(e):
                e.notified = True
                continue
            document = session.get(Document, e.document_id) if e.document_id else None
            r=await client.post(api_url(settings.telegram_api_root, settings.telegram_bot_token, "sendMessage"), json={"chat_id":settings.telegram_chat_id,"text":format_event(e, document.canonical_url if document else ""),"parse_mode":"HTML","disable_web_page_preview":True})
            r.raise_for_status(); e.notified=True
    session.commit(); return len(events)
