import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from fstec_monitor import crawler
from fstec_monitor.crawler import (
    Monitor,
    category_key,
    preferred_attachment_urls,
    snapshot_required,
)
from fstec_monitor.models import Base, Document, Event, ScanRun, Snapshot


def test_category_key_normalizes_nbsp_and_case():
    assert category_key("Информационные и аналитические материалы  185") == "информационные и аналитические материалы 185"
    assert category_key("  Информационные   и аналитические материалы ") == "информационные и аналитические материалы"


def test_markup_only_change_does_not_require_semantic_change():
    assert not snapshot_required("same", "same", True)
    assert snapshot_required("old", "new", True)
    assert snapshot_required("", "new", False)


def test_preferred_attachment_source_chooses_odt_over_pdf():
    links = [
        SimpleNamespace(url="https://example.test/report.pdf", title="Report"),
        SimpleNamespace(url="https://example.test/report.odt", title="Report"),
    ]
    assert preferred_attachment_urls(links) == {"https://example.test/report.odt"}


def test_preferred_attachment_source_falls_back_to_pdf():
    link = SimpleNamespace(url="https://example.test/report.pdf", title="Report")
    assert preferred_attachment_urls([link]) == {link.url}


_HTML = """<html><body><article><h1>Doc</h1><p>Content line</p>
<a href="/dokumenty/vse-dokumenty/cat/doc/file.pdf">file.pdf</a>
</article></body></html>"""


class _FakeFetcher:
    def __init__(self, html: str):
        self.html = html

    async def get(self, url, headers=None):
        return SimpleNamespace(
            status_code=200, text=self.html, content=self.html.encode(),
            url=url, headers={}, raise_for_status=lambda: None,
        )

    async def close(self):
        return None


def test_unchanged_document_does_not_create_new_snapshot(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/crawler.db")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(crawler, "SessionLocal", session_factory)
    monkeypatch.setattr(crawler.settings, "storage_dir", tmp_path / "objects")

    async def run():
        monitor = Monitor()
        monitor.fetcher = _FakeFetcher(_HTML)
        url = "https://fstec.ru/dokumenty/vse-dokumenty/cat/doc"
        await monitor.process_document(url, baseline=True)
        await monitor.process_document(url)

    asyncio.run(run())
    with session_factory() as session:
        assert session.scalar(select(func.count(Snapshot.id))) == 1


def test_recent_attachment_audit_is_not_repeated_for_unchanged_document(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/attachment-audit.db")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(crawler, "SessionLocal", session_factory)
    monkeypatch.setattr(crawler.settings, "storage_dir", tmp_path / "objects")

    async def run():
        monitor = Monitor()
        monitor.fetcher = _FakeFetcher(_HTML)
        calls = 0
        original_process_attachment = monitor.process_attachment

        async def tracked_process_attachment(_session, _document, _attachment, _baseline):
            nonlocal calls
            calls += 1
            return await original_process_attachment(_session, _document, _attachment, _baseline)

        monitor.process_attachment = tracked_process_attachment
        url = "https://fstec.ru/dokumenty/vse-dokumenty/cat/doc"
        await monitor.process_document(url, baseline=True)
        await monitor.process_document(url)
        return calls

    assert asyncio.run(run()) == 1


def test_new_document_emits_one_document_event_not_attachment_flood(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/new-events.db")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(crawler, "SessionLocal", session_factory)
    monkeypatch.setattr(crawler.settings, "storage_dir", tmp_path / "objects")

    async def run():
        monitor = Monitor()
        monitor.fetcher = _FakeFetcher(_HTML)
        await monitor.process_document("https://fstec.ru/dokumenty/vse-dokumenty/cat/doc", baseline=False)

    asyncio.run(run())
    with session_factory() as session:
        kinds = [event.kind for event in session.scalars(select(Event)).all()]
        assert kinds == ["document_added"]


def test_changed_document_creates_new_snapshot(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/crawler.db")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(crawler, "SessionLocal", session_factory)
    monkeypatch.setattr(crawler.settings, "storage_dir", tmp_path / "objects")

    async def run():
        monitor = Monitor()
        url = "https://fstec.ru/dokumenty/vse-dokumenty/cat/doc"
        monitor.fetcher = _FakeFetcher(_HTML)
        await monitor.process_document(url, baseline=True)
        monitor.fetcher = _FakeFetcher(_HTML.replace("Content line", "Content line changed"))
        await monitor.process_document(url)

    asyncio.run(run())
    with session_factory() as session:
        assert session.scalar(select(func.count(Snapshot.id))) == 2


def test_markup_only_change_archives_latest_html_and_reports_markup_event(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/crawler.db")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(crawler, "SessionLocal", session_factory)
    monkeypatch.setattr(crawler.settings, "storage_dir", tmp_path / "objects")

    async def run():
        monitor = Monitor()
        url = "https://fstec.ru/dokumenty/vse-dokumenty/cat/doc"
        monitor.fetcher = _FakeFetcher(_HTML)
        await monitor.process_document(url, baseline=True)
        markup_only = _HTML.replace("<p>Content line</p>", "<p><strong>Content</strong> line</p>")
        monitor.fetcher = _FakeFetcher(markup_only)
        await monitor.process_document(url)

    asyncio.run(run())
    with session_factory() as session:
        assert session.scalar(select(func.count(Snapshot.id))) == 2
        event = session.scalar(select(Event).where(Event.kind == "html_markup_changed"))
        assert event is not None
        assert session.scalar(select(Event).where(Event.kind == "html_content_changed")) is None
        assert "<strong>" in event.details


def test_reconcile_documents_confirms_removal_and_does_not_repeat_event(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/reconcile.db")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    with session_factory() as session:
        document = Document(canonical_url="https://fstec.ru/dokumenty/vse-dokumenty/cat/doc", title="Doc")
        session.add(document)
        session.commit()

        crawler.reconcile_document_presence(session, set(), baseline=False)
        assert document.active is True
        assert document.missing_runs == 1
        assert session.scalar(select(func.count(Event.id))) == 0

        crawler.reconcile_document_presence(session, set(), baseline=False)
        assert document.active is False
        assert document.missing_runs == 2
        assert session.scalar(select(func.count(Event.id)).where(Event.kind == "document_removed")) == 1

        crawler.reconcile_document_presence(session, set(), baseline=False)
        assert session.scalar(select(func.count(Event.id)).where(Event.kind == "document_removed")) == 1


def test_reconcile_documents_records_restoration(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/restore.db")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    url = "https://fstec.ru/dokumenty/vse-dokumenty/cat/doc"
    with session_factory() as session:
        document = Document(canonical_url=url, title="Doc", active=False, missing_runs=2)
        session.add(document)
        session.commit()

        crawler.reconcile_document_presence(session, {url}, baseline=False)

        assert document.active is True
        assert document.missing_runs == 0
        assert session.scalar(select(func.count(Event.id)).where(Event.kind == "document_restored")) == 1


class _FakeMonitor:
    def __init__(self, urls=(), fail: bool = False):
        self.urls = set(urls)
        self.fail = fail

    async def discover(self):
        if self.fail:
            raise RuntimeError("catalog down")
        return self.urls

    async def close(self):
        return None

    async def process_document(self, _url, baseline=False):
        return None


def _scan_db(monkeypatch, tmp_path, monitor):
    engine = create_engine(f"sqlite:///{tmp_path}/scan.db")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(crawler, "SessionLocal", session_factory)
    monkeypatch.setattr(crawler, "Monitor", monitor)
    return session_factory


def test_run_monitor_records_scan_run(monkeypatch, tmp_path):
    session_factory = _scan_db(monkeypatch, tmp_path, _FakeMonitor)

    count = asyncio.run(crawler.run_monitor(trigger="manual"))

    assert count == 0
    with session_factory() as session:
        run = session.scalar(select(ScanRun))
        assert run is not None
        assert run.trigger == "manual"
        assert run.documents == 0
        assert run.finished_at is not None
        assert run.error == ""


def test_run_monitor_reports_discovery_and_document_progress(monkeypatch, tmp_path):
    session_factory = _scan_db(monkeypatch, tmp_path, lambda: _FakeMonitor(urls={"a", "b"}))
    progress = []

    count = asyncio.run(crawler.run_monitor(trigger="manual", progress_callback=lambda *args: progress.append(args)))

    assert count == 2
    assert progress[0] == ("Обход каталога", 0, 0, 0)
    assert progress[-1] == ("Проверка документов", 2, 2, 0)
    assert len(progress) >= 3
    with session_factory() as session:
        assert session.scalar(select(ScanRun).order_by(ScanRun.id.desc())).finished_at is not None


def test_run_monitor_honors_cancellation_event(monkeypatch, tmp_path):
    session_factory = _scan_db(monkeypatch, tmp_path, lambda: _FakeMonitor(urls={"a", "b"}))
    cancel_event = asyncio.Event()
    cancel_event.set()

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(crawler.run_monitor(cancel_event=cancel_event))

    with session_factory() as session:
        run = session.scalar(select(ScanRun).order_by(ScanRun.id.desc()))
        assert run is not None
        assert run.error


def test_run_monitor_records_failed_scan_run(monkeypatch, tmp_path):
    session_factory = _scan_db(monkeypatch, tmp_path, lambda: _FakeMonitor(fail=True))

    with pytest.raises(RuntimeError, match="catalog down"):
        asyncio.run(crawler.run_monitor())

    with session_factory() as session:
        run = session.scalar(select(ScanRun))
        assert run is not None
        assert "catalog down" in run.error
        assert session.scalar(select(func.count(Event.id)).where(Event.kind == "fetch_error")) == 1
