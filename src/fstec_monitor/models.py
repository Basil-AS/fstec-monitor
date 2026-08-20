from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow(): return datetime.now(UTC)

class Base(DeclarativeBase): pass

class Document(Base):
    __tablename__ = "documents"
    id: Mapped[int] = mapped_column(primary_key=True)
    canonical_url: Mapped[str] = mapped_column(Text, unique=True)
    title: Mapped[str] = mapped_column(Text, default="")
    category: Mapped[str] = mapped_column(Text, default="")
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    missing_runs: Mapped[int] = mapped_column(Integer, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_attachment_audit_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    current_html_sha256: Mapped[str] = mapped_column(String(64), default="")
    current_semantic_sha256: Mapped[str] = mapped_column(String(64), default="")
    current_etag: Mapped[str] = mapped_column(Text, default="")
    current_last_modified: Mapped[str] = mapped_column(Text, default="")
    snapshots: Mapped[list[Snapshot]] = relationship(back_populates="document")

class Snapshot(Base):
    __tablename__ = "snapshots"
    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"), index=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    status_code: Mapped[int] = mapped_column(Integer)
    final_url: Mapped[str] = mapped_column(Text)
    raw_sha256: Mapped[str] = mapped_column(String(64))
    semantic_sha256: Mapped[str] = mapped_column(String(64))
    html_sha256: Mapped[str] = mapped_column(String(64))
    raw_object: Mapped[str] = mapped_column(Text)
    normalized_html_object: Mapped[str] = mapped_column(Text)
    normalized_text_object: Mapped[str] = mapped_column(Text)
    etag: Mapped[str] = mapped_column(Text, default="")
    last_modified: Mapped[str] = mapped_column(Text, default="")
    document: Mapped[Document] = relationship(back_populates="snapshots")

class Attachment(Base):
    __tablename__ = "attachments"
    __table_args__ = (UniqueConstraint("document_id", "url", name="uq_document_attachment_url"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"), index=True)
    url: Mapped[str] = mapped_column(Text)
    display_name: Mapped[str] = mapped_column(Text, default="")
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    active: Mapped[bool] = mapped_column(Boolean, default=True)

class AttachmentVersion(Base):
    __tablename__ = "attachment_versions"
    id: Mapped[int] = mapped_column(primary_key=True)
    attachment_id: Mapped[int] = mapped_column(ForeignKey("attachments.id"), index=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    status_code: Mapped[int] = mapped_column(Integer)
    content_type: Mapped[str] = mapped_column(Text, default="")
    content_length: Mapped[int] = mapped_column(BigInteger, default=0)
    binary_sha256: Mapped[str] = mapped_column(String(64))
    semantic_sha256: Mapped[str] = mapped_column(String(64), default="")
    object_key: Mapped[str] = mapped_column(Text)
    extracted_text_key: Mapped[str] = mapped_column(Text, default="")
    etag: Mapped[str] = mapped_column(Text, default="")
    last_modified: Mapped[str] = mapped_column(Text, default="")

class Event(Base):
    __tablename__ = "events"
    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    document_id: Mapped[int | None] = mapped_column(ForeignKey("documents.id"), nullable=True)
    kind: Mapped[str] = mapped_column(String(64), index=True)
    severity: Mapped[str] = mapped_column(String(16), default="info")
    summary: Mapped[str] = mapped_column(Text)
    details: Mapped[str] = mapped_column(Text, default="")
    notified: Mapped[bool] = mapped_column(Boolean, default=False)


class UserAccess(Base):
    __tablename__ = "user_access"
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger)
    username: Mapped[str] = mapped_column(Text, default="")
    display_name: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    notification_sent: Mapped[bool] = mapped_column(Boolean, default=False)


class UserIgnoredCategory(Base):
    __tablename__ = "user_ignored_categories"
    __table_args__ = (UniqueConstraint("user_id", "category_key", name="uq_user_ignored_category"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    category_key: Mapped[str] = mapped_column(String(255))
    category_name: Mapped[str] = mapped_column(Text)


class EventDelivery(Base):
    __tablename__ = "event_deliveries"
    __table_args__ = (UniqueConstraint("event_id", "chat_id", name="uq_event_delivery_chat"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"), index=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True)
    delivered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ScanRun(Base):
    __tablename__ = "scan_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    documents: Mapped[int] = mapped_column(Integer, default=0)
    trigger: Mapped[str] = mapped_column(String(16), default="cli")
    baseline: Mapped[bool] = mapped_column(Boolean, default=False)
    error: Mapped[str] = mapped_column(Text, default="")


class BotSetting(Base):
    __tablename__ = "bot_settings"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
