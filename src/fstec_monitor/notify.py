from __future__ import annotations

from html import escape

import httpx
from sqlalchemy import select

from .config import settings
from .models import Event
from .telegram_bot import api_url


def format_event(e: Event) -> str:
    icon={"critical":"🔴","warning":"🟠","info":"🔵"}.get(e.severity,"⚪")
    return f"{icon} <b>ФСТЭК: {escape(e.summary)}</b>\n\n<code>{escape(e.kind)}</code>\n{escape(e.details[:3000])}"


def should_notify_event(e: Event) -> bool:
    return e.kind != "html_markup_changed"

async def notify_pending(session) -> int:
    if not settings.telegram_bot_token or not settings.telegram_chat_id: return 0
    events=session.scalars(select(Event).where(Event.notified.is_(False)).order_by(Event.id)).all()
    async with httpx.AsyncClient(timeout=30) as client:
        for e in events:
            if not should_notify_event(e):
                e.notified = True
                continue
            r=await client.post(api_url(settings.telegram_api_root, settings.telegram_bot_token, "sendMessage"), json={"chat_id":settings.telegram_chat_id,"text":format_event(e),"parse_mode":"HTML","disable_web_page_preview":True})
            r.raise_for_status(); e.notified=True
    session.commit(); return len(events)
