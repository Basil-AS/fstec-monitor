from __future__ import annotations

from datetime import UTC, datetime

from fstec_monitor.models import Document, Event
from fstec_monitor.notify import _deduplicate_attachment_additions, format_change_digest
from fstec_monitor.telegram.change_views import paginate_change_groups, visible_change_groups


def test_change_digest_groups_document_and_hides_initial_attachment_noise():
    document = Document(id=7, canonical_url="https://example.test/doc", title="Доклад", category="Доклады")
    events = [
        Event(id=1, document_id=7, kind="attachment_added", severity="warning", summary="добавлено вложение: Доклад.pdf", created_at=datetime.now(UTC)),
        Event(id=2, document_id=7, kind="document_added", severity="warning", summary="добавлен документ: Доклад", created_at=datetime.now(UTC)),
        Event(id=3, document_id=7, kind="attachment_content_changed", severity="critical", summary="обновлено вложение: Доклад.odt", created_at=datetime.now(UTC)),
    ]

    digest = format_change_digest(events, {7: document})

    assert "📁 <b>Доклады</b>" in digest
    assert "добавлено вложение: Доклад.pdf" not in digest
    assert "добавлен документ: Доклад" in digest
    assert "обновлено вложение: Доклад.odt" in digest


def test_change_digest_shows_one_added_event_for_pdf_and_odt_variants():
    document = Document(id=8, canonical_url="https://example.test/doc", title="Доклад", category="Доклады")
    events = [
        Event(id=4, document_id=8, kind="attachment_added", severity="warning", summary="добавлено вложение: Доклад", details="https://example.test/report.pdf", created_at=datetime.now(UTC)),
        Event(id=5, document_id=8, kind="attachment_added", severity="warning", summary="добавлено вложение: Доклад", details="https://example.test/report.odt", created_at=datetime.now(UTC)),
    ]

    digest = format_change_digest(events, {8: document})

    assert digest.count("добавлено вложение: Доклад") == 1
    selected = _deduplicate_attachment_additions(events)
    assert selected[0].details.endswith("report.odt")


def test_changes_pagination_counts_visible_document_groups_not_raw_events():
    documents = {
        1: Document(id=1, title="A", category="А", canonical_url="https://example.test/a"),
        2: Document(id=2, title="B", category="Б", canonical_url="https://example.test/b"),
    }
    events = [
        Event(id=1, document_id=1, kind="document_added", summary="добавлен документ: A"),
        Event(id=2, document_id=1, kind="attachment_added", summary="добавлено вложение: A.pdf", details="a.pdf"),
        Event(id=3, document_id=1, kind="attachment_added", summary="добавлено вложение: A.odt", details="a.odt"),
        Event(id=4, document_id=2, kind="html_content_changed", summary="обновлён B"),
    ]

    groups = visible_change_groups(events, documents)
    selected, page, pages = paginate_change_groups(groups, page=1, page_size=1)

    assert len(groups) == 2
    assert page == 1
    assert pages == 2
    assert selected[0][0] == "Б"
    assert [event.kind for event in groups[0][2]] == ["document_added"]
