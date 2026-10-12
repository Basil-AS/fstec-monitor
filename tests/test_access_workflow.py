import asyncio
import re

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import fstec_monitor.telegram_bot as telegram_bot_module
from fstec_monitor.models import Base, UserAccess, UserAccessEvent
from fstec_monitor.telegram_bot import TelegramBot


@pytest.fixture
def access_db(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/access-workflow.db")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(telegram_bot_module, "SessionLocal", factory)
    monkeypatch.setattr(telegram_bot_module, "init_db", lambda: None)
    monkeypatch.setattr(telegram_bot_module.settings, "telegram_admin_id", 99)
    return factory


def test_new_access_request_uses_nonce_bound_buttons_and_audits(access_db):
    bot = TelegramBot.__new__(TelegramBot)
    sent = []

    async def send(*args, **kwargs):
        sent.append((args, kwargs))

    bot.send = send
    asyncio.run(bot.request_access(42, 42, "requester", "Requester"))

    with access_db() as session:
        user = session.get(UserAccess, 42)
        events = session.scalars(
            select(UserAccessEvent).where(UserAccessEvent.user_id == 42).order_by(UserAccessEvent.id)
        ).all()
        nonce = user.request_nonce
        assert user.status == "pending"
        assert re.fullmatch(r"[0-9a-f]{12}", nonce)
        assert [(event.action, event.old_status, event.new_status) for event in events] == [
            ("request_created", None, "pending"),
            ("admin_notified", "pending", "pending"),
        ]

    assert [call[0][0] for call in sent] == [99, 42]
    assert "Заявка на доступ отправлена" in sent[1][0][1]
    markup = sent[0][0][2]
    callbacks = [button["callback_data"] for row in markup["inline_keyboard"] for button in row]
    assert callbacks == [
        f"v1:access:approve-42-pending-{nonce}",
        f"v1:access:deny-42-pending-{nonce}",
    ]


def test_access_decision_requires_admin_current_nonce_and_is_audited_once(access_db):
    with access_db() as session:
        session.add(UserAccess(
            user_id=42, chat_id=42, status="pending", request_nonce="abcdef123456",
            username="requester", display_name="Requester",
        ))
        session.commit()

    bot = TelegramBot.__new__(TelegramBot)
    bot.call = _async_noop
    bot.edit_message = _async_noop
    bot.send = _async_noop
    reply = _reply

    async def decide(actor_id, payload):
        await bot._handle_access_decision(payload, reply, actor_id=actor_id)

    asyncio.run(decide(100, "approve-42-pending-abcdef123456"))
    with access_db() as session:
        user = session.get(UserAccess, 42)
        assert user.status == "pending"
        assert session.scalars(select(UserAccessEvent)).all() == []

    stale = "approve-42-pending-000000000000"
    asyncio.run(decide(99, stale))
    with access_db() as session:
        assert session.get(UserAccess, 42).status == "pending"
        assert session.scalars(select(UserAccessEvent)).all() == []

    current = "approve-42-pending-abcdef123456"
    asyncio.run(decide(99, current))
    asyncio.run(decide(99, current))
    with access_db() as session:
        user = session.get(UserAccess, 42)
        events = session.scalars(select(UserAccessEvent)).all()
        assert user.status == "approved"
        assert user.resolved_by == 99
        assert user.request_nonce != "abcdef123456"
        assert [(event.action, event.old_status, event.new_status, event.actor_id) for event in events] == [
            ("approve", "pending", "approved", 99),
        ]


def test_legacy_access_button_without_nonce_is_rejected(access_db):
    with access_db() as session:
        session.add(UserAccess(user_id=42, chat_id=42, status="pending", request_nonce="abcdef123456"))
        session.commit()

    bot = TelegramBot.__new__(TelegramBot)
    bot.call = _async_noop
    bot.edit_message = _async_noop
    bot.send = _async_noop

    asyncio.run(bot.handle_callback({
        "id": "legacy-access",
        "from": {"id": 99},
        "message": {"message_id": 7, "chat": {"id": 99, "type": "private"}},
        "data": "access:approve:42",
    }))

    with access_db() as session:
        assert session.get(UserAccess, 42).status == "pending"
        assert session.scalars(select(UserAccessEvent)).all() == []


def test_explicit_request_reopens_denied_state_with_new_nonce_and_audit(access_db):
    with access_db() as session:
        session.add(UserAccess(
            user_id=42, chat_id=42, status="denied", request_nonce="abcdef123456",
            resolved_by=99, notification_sent=True,
        ))
        session.commit()

    bot = TelegramBot.__new__(TelegramBot)
    sent = []

    async def send(*args, **kwargs):
        sent.append((args, kwargs))

    bot.send = send
    asyncio.run(bot.request_access(42, 42, "requester", "Requester", explicit=True))

    with access_db() as session:
        user = session.get(UserAccess, 42)
        events = session.scalars(select(UserAccessEvent).order_by(UserAccessEvent.id)).all()
        assert user.status == "pending"
        assert user.request_nonce != "abcdef123456"
        assert [(event.action, event.old_status, event.new_status, event.actor_id) for event in events] == [
            ("request_renewed", "denied", "pending", 42),
            ("admin_notified", "pending", "pending", 99),
        ]
    assert [call[0][0] for call in sent] == [99, 42]
    assert "Заявка на доступ отправлена" in sent[1][0][1]


@pytest.mark.parametrize(
    ("status", "notification_sent", "expected_recipients"),
    [
        ("pending", True, []),
        ("denied", True, []),
        ("blocked", True, []),
        ("pending", False, [99]),
    ],
)
def test_unauthenticated_retries_do_not_repeat_user_ack_but_retry_admin_delivery(
    access_db, status, notification_sent, expected_recipients
):
    with access_db() as session:
        session.add(UserAccess(
            user_id=42,
            chat_id=42,
            status=status,
            request_nonce="abcdef123456",
            notification_sent=notification_sent,
            username="requester",
            display_name="Requester",
        ))
        session.commit()

    bot = TelegramBot.__new__(TelegramBot)
    sent = []

    async def send(*args, **kwargs):
        sent.append((args, kwargs))

    bot.send = send
    asyncio.run(bot.request_access(42, 42, "requester", "Requester"))

    with access_db() as session:
        user = session.get(UserAccess, 42)
        assert user.attempt_count == 2
        assert user.status == status
        assert user.notification_sent is True
    assert [call[0][0] for call in sent] == expected_recipients


async def _async_noop(*_args, **_kwargs):
    return None


async def _reply(*_args, **_kwargs):
    return None


def test_request_access_command_is_user_allowed_and_reaches_explicit_reopen():
    bot = TelegramBot.__new__(TelegramBot)
    called = []

    async def request_access(user_id, chat_id, username, display_name, *, explicit=False):
        called.append((user_id, chat_id, username, display_name, explicit))
        return False

    bot.request_access = request_access
    asyncio.run(bot.handle({
        "message": {
            "from": {"id": 42, "username": "requester", "first_name": "Requester"},
            "chat": {"id": 42, "type": "private"},
            "text": "/request_access",
        }
    }))

    assert telegram_bot_module.is_user_command_allowed("/request_access")
    assert called == [(42, 42, "requester", "Requester", True)]


def test_user_command_menu_exposes_request_access():
    assert {command for command, _description in telegram_bot_module.USER_COMMANDS} == {
        "start", "help", "request_access"
    }


@pytest.mark.parametrize(
    ("old_status", "action", "new_status"),
    [
        ("pending", "approve", "approved"),
        ("pending", "deny", "denied"),
        ("pending", "block", "blocked"),
        ("approved", "block", "blocked"),
        ("approved", "revoke", "denied"),
        ("denied", "approve", "approved"),
        ("denied", "block", "blocked"),
        ("blocked", "unblock", "denied"),
    ],
)
def test_users_screen_exposes_nonce_bound_access_transitions(access_db, old_status, action, new_status):
    nonce = "abcdef123456"
    with access_db() as session:
        session.add(UserAccess(
            user_id=42, chat_id=42, status=old_status, request_nonce=nonce,
            username="requester", display_name="Requester",
        ))
        session.commit()

    bot = TelegramBot.__new__(TelegramBot)
    bot.call = _async_noop
    bot.edit_message = _async_noop
    bot.send = _async_noop
    text, markup = bot.users_text()
    callbacks = [button["callback_data"] for row in markup["inline_keyboard"] for button in row]
    callback_data = f"v1:access:{action}-42-{old_status}-{nonce}"

    assert "статус:" in text
    assert callback_data in callbacks
    action_button = next(
        button for row in markup["inline_keyboard"] for button in row
        if button["callback_data"] == callback_data
    )
    assert "#42" in action_button["text"]
    assert telegram_bot_module.ACCESS_STATUS_LABELS[old_status] in action_button["text"]
    asyncio.run(bot.handle_callback({
        "id": f"transition-{action}",
        "from": {"id": 99},
        "message": {"message_id": 7, "chat": {"id": 99, "type": "private"}},
        "data": callback_data,
    }))

    with access_db() as session:
        user = session.get(UserAccess, 42)
        event = session.scalar(select(UserAccessEvent).where(UserAccessEvent.action == action))
        assert user.status == new_status
        assert user.request_nonce != nonce
        assert event is not None
        assert (event.old_status, event.new_status, event.actor_id) == (old_status, new_status, 99)


@pytest.mark.parametrize("failure", ["non_admin", "stale_nonce"])
def test_users_transition_callback_rejects_unauthorized_or_stale_actions(access_db, failure):
    with access_db() as session:
        session.add(UserAccess(
            user_id=42, chat_id=42, status="approved", request_nonce="abcdef123456",
            username="requester", display_name="Requester",
        ))
        session.commit()

    bot = TelegramBot.__new__(TelegramBot)
    bot.call = _async_noop
    bot.edit_message = _async_noop
    bot.send = _async_noop
    _text, markup = bot.users_text()
    revoke = next(
        button["callback_data"]
        for row in markup["inline_keyboard"]
        for button in row
        if button["callback_data"].startswith("v1:access:revoke-")
    )
    if failure == "stale_nonce":
        revoke = revoke[:-1] + ("0" if revoke[-1] != "0" else "1")
    actor_id = 100 if failure == "non_admin" else 99
    asyncio.run(bot.handle_callback({
        "id": f"revoke-{failure}",
        "from": {"id": actor_id},
        "message": {"message_id": 7, "chat": {"id": actor_id, "type": "private"}},
        "data": revoke,
    }))

    with access_db() as session:
        assert session.get(UserAccess, 42).status == "approved"
        assert session.scalars(select(UserAccessEvent)).all() == []


def test_users_screen_escapes_user_controlled_identity_markup(access_db):
    with access_db() as session:
        session.add(UserAccess(
            user_id=42,
            chat_id=42,
            status="pending",
            request_nonce="abcdef123456",
            username="<svg/onload=alert(1)>",
            display_name="<b>Injected</b>",
        ))
        session.commit()

    text, _markup = TelegramBot.__new__(TelegramBot).users_text()

    assert "&lt;b&gt;Injected&lt;/b&gt;" in text
    assert "<b>Injected</b>" not in text
    assert "<svg" not in text
