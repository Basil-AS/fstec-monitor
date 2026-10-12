from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError

import fstec_monitor.db as db_module
from fstec_monitor.models import Base


def test_postgresql_access_migration_is_safe_to_rerun(monkeypatch):
    statements = []

    class Connection:
        def exec_driver_sql(self, statement):
            statements.append(" ".join(statement.split()))

    class Engine:
        dialect = SimpleNamespace(name="postgresql")

        @contextmanager
        def begin(self):
            yield Connection()

    monkeypatch.setattr(db_module, "engine", Engine())
    monkeypatch.setattr(Base.metadata, "create_all", lambda _engine: None)

    db_module.init_db()
    db_module.init_db()

    normalized = [statement.casefold() for statement in statements]
    assert sum("alter table user_access add column if not exists" in sql for sql in normalized) == 8
    assert sum("update user_access set last_attempt_at = requested_at" in sql for sql in normalized) == 2
    assert sum("where request_nonce is null or request_nonce = ''" in sql for sql in normalized) == 2
    assert sum("create or replace function fstec_reject_access_event_mutation()" in sql for sql in normalized) == 2
    assert sum("drop trigger if exists user_access_events_append_only" in sql for sql in normalized) == 2
    assert sum("before update or delete on user_access_events" in sql for sql in normalized) == 2


def test_sqlite_connection_applies_busy_timeout_and_wal_pragmas():
    calls = []

    class Cursor:
        def execute(self, statement):
            calls.append(statement)

        def close(self):
            calls.append("closed")

    class Connection:
        def cursor(self):
            return Cursor()

    pragma_handler = getattr(db_module, "_sqlite_pragmas", None)
    if pragma_handler is None:
        pytest.skip("SQLite is not the configured default database dialect")

    pragma_handler(Connection(), None)

    assert calls == [
        "PRAGMA busy_timeout=30000",
        "PRAGMA journal_mode=WAL",
        "PRAGMA synchronous=NORMAL",
        "closed",
    ]


def test_sqlite_access_migration_backfills_and_installs_idempotent_audit_triggers(monkeypatch):
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE user_access ("
            "user_id BIGINT PRIMARY KEY, chat_id BIGINT NOT NULL, username TEXT NOT NULL DEFAULT '', "
            "display_name TEXT NOT NULL DEFAULT '', status VARCHAR(16) NOT NULL DEFAULT 'pending', "
            "requested_at DATETIME NOT NULL, reviewed_at DATETIME, notification_sent BOOLEAN NOT NULL DEFAULT 0)"
        )
        connection.exec_driver_sql(
            "CREATE TABLE user_access_events (id INTEGER PRIMARY KEY, user_id BIGINT NOT NULL, action TEXT NOT NULL)"
        )
        connection.exec_driver_sql(
            "INSERT INTO user_access (user_id, chat_id, requested_at) VALUES (40, 40, '2026-10-12 00:00:00')"
        )
    monkeypatch.setattr(db_module, "engine", engine)

    db_module.init_db()
    db_module.init_db()

    with engine.connect() as connection:
        columns = {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(user_access)")}
        row = connection.exec_driver_sql(
            "SELECT last_attempt_at, request_nonce FROM user_access WHERE user_id = 40"
        ).one()
        triggers = {row[0] for row in connection.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type = 'trigger' AND tbl_name = 'user_access_events'"
        )}
    assert {"resolved_by", "attempt_count", "last_attempt_at", "request_nonce"} <= columns
    assert row[0] == "2026-10-12 00:00:00"
    assert len(row[1]) == 12
    assert triggers == {"user_access_events_no_update", "user_access_events_no_delete"}

    with engine.begin() as connection:
        connection.exec_driver_sql(
            "INSERT INTO user_access_events (id, user_id, action) VALUES (1, 40, 'approved')"
        )
    for statement in (
        "UPDATE user_access_events SET action = 'tampered' WHERE id = 1",
        "DELETE FROM user_access_events WHERE id = 1",
    ):
        with pytest.raises(IntegrityError, match="append-only"), engine.begin() as connection:
            connection.exec_driver_sql(statement)
