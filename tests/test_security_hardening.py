import hashlib
import io
import zipfile

import fstec_monitor.extract as extract_module
from fstec_monitor.extract import extract_odt
from fstec_monitor.telegram_bot import category_token


def test_category_token_uses_sha256():
    value = "Методические документы"

    assert category_token(value) == hashlib.sha256(value.casefold().encode()).hexdigest()[:16]


def test_extract_odt_uses_explicit_safe_xml_parser(monkeypatch):
    content_xml = b"<office:document-content xmlns:office='urn:o'><office:body/></office:document-content>"
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        archive.writestr("content.xml", content_xml)
    observed = {}
    original = extract_module.etree.fromstring

    def capture_parser(xml, *args, **kwargs):
        observed["parser"] = kwargs.get("parser")
        return original(xml, *args, **kwargs)

    monkeypatch.setattr(extract_module.etree, "fromstring", capture_parser)

    extract_odt(data.getvalue())

    assert observed["parser"] is not None
