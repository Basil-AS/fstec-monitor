from fstec_monitor.extract import extract_odt, extract_pdf, semantic_text


def test_corrupt_attachment_extraction_stays_unknown():
    assert extract_pdf(b"not a PDF") is None
    assert extract_odt(b"not a ZIP container") is None


def test_unsupported_attachment_type_has_no_semantic_text():
    assert semantic_text(b"body", "text/plain", "https://example.test/document.txt") is None


def test_extract_pdf_closes_document_after_success_and_failure(monkeypatch):
    import sys
    from types import SimpleNamespace

    class Page:
        def __init__(self, fail=False):
            self.fail = fail

        def get_text(self, _kind):
            if self.fail:
                raise ValueError("bad page")
            return "document text"

    class Document:
        def __init__(self, fail=False):
            self.closed = False
            self.fail = fail

        def __iter__(self):
            return iter([Page(fail=self.fail)])

        def close(self):
            self.closed = True

    for fail, expected in ((False, "document text"), (True, None)):
        document = Document(fail=fail)
        monkeypatch.setitem(
            sys.modules,
            "pymupdf",
            SimpleNamespace(open=lambda _document=document, **_kwargs: _document),
        )

        assert extract_pdf(b"pdf bytes") == expected
        assert document.closed


def test_extract_odt_does_not_repeat_table_cell_paragraphs():
    import io
    import zipfile

    xml = b"""<document xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"
        xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0">
      <text:p>Before table</text:p>
      <table:table-row>
        <table:table-cell><text:p>Alpha</text:p></table:table-cell>
        <table:table-cell><text:p>Beta</text:p></table:table-cell>
      </table:table-row>
      <text:p>After table</text:p>
    </document>"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("content.xml", xml)

    assert extract_odt(buffer.getvalue()) == "Before table\n\nAlpha Beta\n\nAfter table"
