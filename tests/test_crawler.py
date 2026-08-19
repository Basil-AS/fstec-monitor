import asyncio
from types import SimpleNamespace

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from fstec_monitor import crawler
from fstec_monitor.crawler import Monitor, category_key, snapshot_required
from fstec_monitor.models import Base, Snapshot


def test_category_key_normalizes_nbsp_and_case():
    assert category_key("Информационные и аналитические материалы  185") == "информационные и аналитические материалы 185"
    assert category_key("  Информационные   и аналитические материалы ") == "информационные и аналитические материалы"


def test_markup_only_change_does_not_require_archived_snapshot():
    assert not snapshot_required("same", "same", True)
    assert snapshot_required("old", "new", True)
    assert snapshot_required("", "new", False)


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
