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
    # The production installation started before HTTP cache metadata existed.
    # Keep the lightweight SQLite deployment migratable without an external tool.
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
        ):
            existing = connection.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()
            if column.split()[0] not in {row[1] for row in existing}:
                connection.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {column}")
