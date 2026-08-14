from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from .config import settings
from .models import Base

engine = create_engine(settings.database_url, future=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

def init_db() -> None:
    Base.metadata.create_all(engine)
    # The production installation started before HTTP cache metadata existed.
    # Keep the lightweight SQLite deployment migratable without an external tool.
    if engine.dialect.name != "sqlite":
        return
    with engine.begin() as connection:
        for table, column in (
            ("documents", "last_attachment_audit_at DATETIME"),
            ("snapshots", "etag TEXT DEFAULT ''"),
            ("snapshots", "last_modified TEXT DEFAULT ''"),
            ("attachment_versions", "etag TEXT DEFAULT ''"),
            ("attachment_versions", "last_modified TEXT DEFAULT ''"),
        ):
            existing = connection.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()
            if column.split()[0] not in {row[1] for row in existing}:
                connection.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {column}")
