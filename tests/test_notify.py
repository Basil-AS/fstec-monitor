import asyncio
from typing import ClassVar

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import fstec_monitor.notify as notify_module
from fstec_monitor.models import Base, Document, Event


class _TelegramResponse:
    def raise_for_status(self):
        return None

    def json(self):
        return {"ok": True}


class _TelegramClient:
    calls: ClassVar[list] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return _TelegramResponse()


def _session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/notify.db")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


def test_notify_pending_sends_one_change_digest_and_one_error_digest(monkeypatch, tmp_path):
    session = _session(tmp_path)
    document = Document(canonical_url="https://example.test/doc", title="Doc")
    session.add(document)
    session.flush()
    session.add_all([
        Event(document_id=document.id, kind="document_added", severity="warning", summary="Новый документ"),
        Event(document_id=document.id, kind="html_markup_changed", severity="info", summary="Шум"),
        Event(document_id=document.id, kind="fetch_error", severity="warning", summary="Сайт недоступен"),
    ])
    session.commit()
    _TelegramClient.calls = []
    monkeypatch.setattr(notify_module.httpx, "AsyncClient", lambda **_kwargs: _TelegramClient())
    monkeypatch.setattr(notify_module.settings, "telegram_bot_token", "token")
    monkeypatch.setattr(notify_module.settings, "telegram_chat_id", "123")

    sent = asyncio.run(notify_module.notify_pending(session))

    assert sent == 2
    assert len(_TelegramClient.calls) == 2
    assert all(event.notified for event in session.scalars(select(Event)).all())


def test_notify_pending_silences_an_already_delivered_error(monkeypatch, tmp_path):
    session = _session(tmp_path)
    session.add(Event(kind="fetch_error", severity="warning", summary="Сайт недоступен", notified=True))
    session.add(Event(kind="fetch_error", severity="warning", summary="Сайт недоступен", notified=False))
    session.commit()
    _TelegramClient.calls = []
    monkeypatch.setattr(notify_module.httpx, "AsyncClient", lambda **_kwargs: _TelegramClient())
    monkeypatch.setattr(notify_module.settings, "telegram_bot_token", "token")
    monkeypatch.setattr(notify_module.settings, "telegram_chat_id", "123")

    sent = asyncio.run(notify_module.notify_pending(session))

    assert sent == 0
    assert len(_TelegramClient.calls) == 0
    assert session.scalar(select(Event).where(Event.notified.is_(False))) is None
