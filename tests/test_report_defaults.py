from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import fstec_monitor.telegram_bot as telegram_module
from fstec_monitor.models import Base, Event
from fstec_monitor.telegram_bot import TelegramBot


def test_latest_change_id_ignores_operational_errors(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/changes.db")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    with session_factory() as session:
        session.add_all([
            Event(id=1, kind="document_added", severity="warning", summary="new"),
            Event(id=2, kind="fetch_error", severity="warning", summary="temporary"),
            Event(id=3, kind="attachment_content_changed", severity="critical", summary="updated"),
        ])
        session.commit()
    monkeypatch.setattr(telegram_module, "SessionLocal", session_factory)

    assert TelegramBot.latest_change_id() == 3


def test_build_report_persists_markdown_artifact(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/reports.db")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    with session_factory() as session:
        session.add(Event(id=41, kind="document_added", severity="warning", summary="Новый документ"))
        session.commit()
    monkeypatch.setattr(telegram_module, "SessionLocal", session_factory)
    monkeypatch.setattr(telegram_module.settings, "storage_dir", tmp_path / "objects")

    result = TelegramBot.__new__(TelegramBot)._build_report(41)

    assert result is not None
    report_path = tmp_path / "objects" / "reports" / "event-41.md"
    assert report_path.exists()
    assert "событие #41" in report_path.read_text(encoding="utf-8")
