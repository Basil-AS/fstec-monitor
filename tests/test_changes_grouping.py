from __future__ import annotations

from datetime import UTC, datetime

from fstec_monitor.models import Document, Event
from fstec_monitor.notify import format_change_digest


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
