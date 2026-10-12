from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from .config import settings
from .models import Base

engine = create_engine(settings.database_url, future=True, connect_args={"timeout": 30, "check_same_thread": False} if settings.database_url.startswith("sqlite") else {})
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

if settings.database_url.startswith("sqlite"):
    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.close()

def init_db() -> None:
    Base.metadata.create_all(engine)
    if engine.dialect.name == "postgresql":
        with engine.begin() as connection:
            for column in (
                "resolved_by BIGINT",
                "attempt_count INTEGER NOT NULL DEFAULT 1",
                "last_attempt_at TIMESTAMPTZ",
                "request_nonce VARCHAR(16) DEFAULT ''",
            ):
                connection.exec_driver_sql(
                    f"ALTER TABLE user_access ADD COLUMN IF NOT EXISTS {column}"
                )
            connection.exec_driver_sql(
                "UPDATE user_access SET last_attempt_at = requested_at "
                "WHERE last_attempt_at IS NULL"
            )
            connection.exec_driver_sql(
                "UPDATE user_access SET request_nonce = "
                "substring(md5(random()::text || clock_timestamp()::text), 1, 12) "
                "WHERE request_nonce IS NULL OR request_nonce = ''"
            )
            connection.exec_driver_sql(
                "CREATE OR REPLACE FUNCTION fstec_reject_access_event_mutation() "
                "RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN "
                "RAISE EXCEPTION 'user access audit history is append-only'; END $$"
            )
            connection.exec_driver_sql(
                "DROP TRIGGER IF EXISTS user_access_events_append_only ON user_access_events"
            )
            connection.exec_driver_sql(
                "CREATE TRIGGER user_access_events_append_only "
                "BEFORE UPDATE OR DELETE ON user_access_events FOR EACH ROW "
                "EXECUTE FUNCTION fstec_reject_access_event_mutation()"
            )
        return
    if engine.dialect.name != "sqlite":
        return
    with engine.begin() as connection:
        for table, column in (
            ("documents", "last_attachment_audit_at DATETIME"),
            ("documents", "current_html_sha256 TEXT DEFAULT ''"),
            ("documents", "current_semantic_sha256 TEXT DEFAULT ''"),
            ("documents", "current_etag TEXT DEFAULT ''"),
            ("documents", "current_last_modified TEXT DEFAULT ''"),
            ("snapshots", "etag TEXT DEFAULT ''"),
            ("snapshots", "last_modified TEXT DEFAULT ''"),
            ("attachment_versions", "etag TEXT DEFAULT ''"),
            ("attachment_versions", "last_modified TEXT DEFAULT ''"),
            ("user_access", "resolved_by BIGINT"),
            ("user_access", "attempt_count INTEGER NOT NULL DEFAULT 1"),
            ("user_access", "last_attempt_at DATETIME"),
            ("user_access", "request_nonce TEXT DEFAULT ''"),
        ):
            existing = connection.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()
            if column.split()[0] not in {row[1] for row in existing}:
                connection.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {column}")
        connection.exec_driver_sql(
            "UPDATE user_access SET last_attempt_at = requested_at WHERE last_attempt_at IS NULL"
        )
        connection.exec_driver_sql(
            "UPDATE user_access SET request_nonce = lower(hex(randomblob(6))) "
            "WHERE request_nonce IS NULL OR request_nonce = ''"
        )
        for operation in ("UPDATE", "DELETE"):
            trigger = f"user_access_events_no_{operation.lower()}"
            connection.exec_driver_sql(
                f"CREATE TRIGGER IF NOT EXISTS {trigger} BEFORE {operation} ON user_access_events "
                "BEGIN SELECT RAISE(ABORT, 'user access audit history is append-only'); END"
            )
