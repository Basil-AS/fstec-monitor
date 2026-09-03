import asyncio
from typing import ClassVar

import httpx
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import fstec_monitor.notify as notify_module
from fstec_monitor.models import (
    Base,
    Document,
    Event,
    EventDelivery,
    UserAccess,
    UserIgnoredCategory,
)


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
    monkeypatch.setattr(notify_module.settings, "telegram_admin_id", 123)
    monkeypatch.setattr(notify_module.settings, "telegram_admin_id", 123)

    sent = asyncio.run(notify_module.notify_pending(session))

    assert sent == 2
    assert len(_TelegramClient.calls) == 2
    assert all(event.notified for event in session.scalars(select(Event)).all())


def test_notify_pending_retries_transient_telegram_transport_failure(monkeypatch, tmp_path):
    session = _session(tmp_path)
    session.add(Event(kind="document_added", severity="info", summary="Новый документ"))
    session.commit()

    class RetryingClient(_TelegramClient):
        attempts = 0

        async def post(self, url, **kwargs):
            self.attempts += 1
            if self.attempts == 1:
                raise httpx.ReadTimeout("temporary Telegram timeout")
            return await super().post(url, **kwargs)

    client = RetryingClient()
    monkeypatch.setattr(notify_module.httpx, "AsyncClient", lambda **_kwargs: client)
    monkeypatch.setattr(notify_module.settings, "telegram_bot_token", "token")
    monkeypatch.setattr(notify_module.settings, "telegram_chat_id", "123")
    monkeypatch.setattr(notify_module.settings, "telegram_admin_id", 123)
    monkeypatch.setattr(notify_module.settings, "max_retries", 2)
    monkeypatch.setattr(notify_module.settings, "request_delay_seconds", 0)

    assert asyncio.run(notify_module.notify_pending(session)) == 1
    assert client.attempts == 2


def test_attachment_variants_are_deduplicated_per_document():
    events = [
        Event(document_id=1, kind="attachment_added", summary="добавлено вложение: Report.pdf", details="https://example.test/report.pdf"),
        Event(document_id=1, kind="attachment_added", summary="добавлено вложение: Report.odt", details="https://example.test/report.odt"),
        Event(document_id=2, kind="attachment_added", summary="добавлено вложение: Report.pdf", details="https://example.test/other.pdf"),
    ]

    result = notify_module._deduplicate_attachment_additions(events)

    assert len(result) == 2
    assert {event.document_id for event in result} == {1, 2}


def test_change_digest_orders_categories_before_their_updates():
    documents = {
        1: Document(canonical_url="https://example.test/b", title="B doc", category="Бета"),
        2: Document(canonical_url="https://example.test/a", title="A doc", category="Альфа"),
    }
    events = [
        Event(document_id=1, kind="document_added", severity="info", summary="Документ B"),
        Event(document_id=1, kind="attachment_content_changed", severity="info", summary="Файл B"),
        Event(document_id=2, kind="document_added", severity="info", summary="Документ A"),
    ]

    digest = notify_module.format_change_digest(events, documents)

    assert digest.index("📁 <b>Альфа") < digest.index("📁 <b>Бета")
    assert digest.index("Документ A") < digest.index("Файл B")


def test_notify_pending_splits_oversized_digest(monkeypatch, tmp_path):
    session = _session(tmp_path)
    document = Document(canonical_url="https://example.test/doc", title="Doc")
    session.add(document)
    session.flush()
    session.add_all([
        Event(
            document_id=document.id,
            kind="document_added",
            severity="info",
            summary=f"Документ {index} " + "x" * 240,
        )
        for index in range(30)
    ])
    session.commit()
    _TelegramClient.calls = []
    monkeypatch.setattr(notify_module.httpx, "AsyncClient", lambda **_kwargs: _TelegramClient())
    monkeypatch.setattr(notify_module.settings, "telegram_bot_token", "token")
    monkeypatch.setattr(notify_module.settings, "telegram_chat_id", "123")
    monkeypatch.setattr(notify_module.settings, "telegram_admin_id", 123)

    sent = asyncio.run(notify_module.notify_pending(session))

    assert sent == len(_TelegramClient.calls) > 1
    assert all(len(call[1]["json"]["text"]) <= 4096 for call in _TelegramClient.calls)
    assert len(session.scalars(select(EventDelivery)).all()) == 30
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


def test_notify_pending_delivers_event_to_each_approved_chat(monkeypatch, tmp_path):
    session = _session(tmp_path)
    document = Document(canonical_url="https://example.test/doc", title="Doc")
    session.add(document)
    session.flush()
    session.add_all([
        UserAccess(user_id=10, chat_id=200, status="approved"),
        UserAccess(user_id=11, chat_id=201, status="approved"),
        Event(document_id=document.id, kind="document_added", severity="warning", summary="Новый документ"),
    ])
    session.commit()
    _TelegramClient.calls = []
    monkeypatch.setattr(notify_module.httpx, "AsyncClient", lambda **_kwargs: _TelegramClient())
    monkeypatch.setattr(notify_module.settings, "telegram_bot_token", "token")
    monkeypatch.setattr(notify_module.settings, "telegram_chat_id", "123")
    monkeypatch.setattr(notify_module.settings, "telegram_admin_id", 123)

    sent = asyncio.run(notify_module.notify_pending(session))

    assert sent == 3
    assert len(_TelegramClient.calls) == 3
    assert session.scalar(select(Event).where(Event.notified.is_(False))) is None
    assert session.scalar(select(EventDelivery.event_id)) is not None
    assert len(session.scalars(select(EventDelivery)).all()) == 3


def test_notify_pending_serializes_concurrent_delivery_passes(monkeypatch, tmp_path):
    session = _session(tmp_path)
    session.add(Event(kind="document_added", severity="info", summary="Новый документ"))
    session.commit()
    second = _session(tmp_path)
    _TelegramClient.calls = []

    class SlowTelegramClient(_TelegramClient):
        async def post(self, url, **kwargs):
            await asyncio.sleep(0.05)
            return await super().post(url, **kwargs)

    monkeypatch.setattr(notify_module.httpx, "AsyncClient", lambda **_kwargs: SlowTelegramClient())
    monkeypatch.setattr(notify_module.settings, "telegram_bot_token", "token")
    monkeypatch.setattr(notify_module.settings, "telegram_chat_id", "123")
    monkeypatch.setattr(notify_module.settings, "telegram_admin_id", 123)
    monkeypatch.setattr(notify_module.settings, "storage_dir", str(tmp_path / "objects"))

    async def run_both():
        return await asyncio.gather(
            notify_module.notify_pending(session),
            notify_module.notify_pending(second),
        )

    sent = asyncio.run(run_both())

    assert sorted(sent) == [0, 1]
    assert len(_TelegramClient.calls) == 1


def test_notify_pending_applies_personal_category_ignore(monkeypatch, tmp_path):
    session = _session(tmp_path)
    kept = Document(canonical_url="https://example.test/kept", title="Kept", category="Приказы")
    hidden = Document(canonical_url="https://example.test/hidden", title="Hidden", category="Методические документы")
    session.add_all([kept, hidden])
    session.flush()
    session.add_all([
        UserAccess(user_id=10, chat_id=200, status="approved"),
        UserIgnoredCategory(
            user_id=10,
            category_key="методические документы",
            category_name="Методические документы",
        ),
        Event(document_id=kept.id, kind="document_added", severity="info", summary="Оставить"),
        Event(document_id=hidden.id, kind="document_added", severity="info", summary="Скрыть"),
    ])
    session.commit()
    _TelegramClient.calls = []
    monkeypatch.setattr(notify_module.httpx, "AsyncClient", lambda **_kwargs: _TelegramClient())
    monkeypatch.setattr(notify_module.settings, "telegram_bot_token", "token")
    monkeypatch.setattr(notify_module.settings, "telegram_chat_id", "123")
    monkeypatch.setattr(notify_module.settings, "telegram_admin_id", 123)

    sent = asyncio.run(notify_module.notify_pending(session))

    assert sent == 2
    messages_by_chat = {call[1]["json"]["chat_id"]: call[1]["json"]["text"] for call in _TelegramClient.calls}
    assert "Оставить" in messages_by_chat[123]
    assert "Скрыть" in messages_by_chat[123]
    assert "Оставить" in messages_by_chat[200]
    assert "Скрыть" not in messages_by_chat[200]
    assert len(session.scalars(select(EventDelivery)).all()) == 4
    assert all(event.notified for event in session.scalars(select(Event)).all())


def test_admin_notification_ignores_personal_category_filter(monkeypatch, tmp_path):
    session = _session(tmp_path)
    document = Document(
        canonical_url="https://example.test/hidden-from-user",
        title="Административное обновление",
        category="Скрытая категория",
    )
    session.add(document)
    session.flush()
    session.add_all([
        UserAccess(user_id=151599744, chat_id=151599744, status="approved"),
        UserIgnoredCategory(
            user_id=151599744,
            category_key="скрытая категория",
            category_name="Скрытая категория",
        ),
        Event(document_id=document.id, kind="document_added", severity="warning", summary="Важно админу"),
    ])
    session.commit()
    _TelegramClient.calls = []
    monkeypatch.setattr(notify_module.httpx, "AsyncClient", lambda **_kwargs: _TelegramClient())
    monkeypatch.setattr(notify_module.settings, "telegram_bot_token", "token")
    monkeypatch.setattr(notify_module.settings, "telegram_chat_id", "")
    monkeypatch.setattr(notify_module.settings, "telegram_admin_id", 151599744)

    assert asyncio.run(notify_module.notify_pending(session)) == 1
    message = _TelegramClient.calls[0][1]["json"]["text"]
    assert "Важно админу" in message
