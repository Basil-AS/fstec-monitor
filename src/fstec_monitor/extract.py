from __future__ import annotations

import io
import zipfile

from lxml import etree

from .normalize import normalize_extracted_text, normalize_space


def extract_pdf(data: bytes) -> str | None:
    document = None
    try:
        import pymupdf

        document = pymupdf.open(stream=data, filetype="pdf")
        return "\n\n".join(page.get_text("text") for page in document)
    except Exception:  # noqa: BLE001 - malformed or unsupported PDFs are unknown, not empty text
        return None
    finally:
        if document is not None:
            document.close()


def extract_odt(data: bytes) -> str | None:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            xml = archive.read("content.xml")
        parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False)
        root = etree.fromstring(xml, parser=parser)
        texts = []
        for node in root.xpath(
            "//*[local-name()='p' or local-name()='h' or local-name()='table-row']"
            "[not(ancestor::*[local-name()='table-row'])]"
        ):
            text = normalize_space(" ".join(node.itertext()))
            if text:
                texts.append(text)
        return "\n\n".join(texts)
    except Exception:  # noqa: BLE001 - malformed or unsupported ODTs are unknown, not empty text
        return None


def semantic_text(data: bytes, content_type: str, url: str) -> str | None:
    low = url.lower()
    ctype = content_type.lower()
    if low.endswith(".pdf") or "pdf" in ctype:
        text = extract_pdf(data)
    elif low.endswith(".odt") or "opendocument" in ctype:
        text = extract_odt(data)
    else:
        return None
    return normalize_extracted_text(text) if text is not None else None
