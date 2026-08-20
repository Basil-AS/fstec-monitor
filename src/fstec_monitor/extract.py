from __future__ import annotations

import io
import re
import zipfile

from lxml import etree

from .normalize import normalize_space


def extract_pdf(data: bytes) -> str:
    try:
        import pymupdf
        doc=pymupdf.open(stream=data, filetype="pdf")
        return "\n\n".join(page.get_text("text") for page in doc)
    except (OSError, RuntimeError, ValueError):
        return ""

def extract_odt(data: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            xml=zf.read("content.xml")
        root=etree.fromstring(xml)
        texts=[]
        for node in root.xpath("//*[local-name()='p' or local-name()='h' or local-name()='table-row']"):
            text=normalize_space(" ".join(node.itertext()))
            if text: texts.append(text)
        return "\n".join(texts)
    except (OSError, ValueError, zipfile.BadZipFile, etree.XMLSyntaxError):
        return ""

def semantic_text(data: bytes, content_type: str, url: str) -> str:
    low=url.lower(); ctype=content_type.lower()
    if low.endswith(".pdf") or "pdf" in ctype: text=extract_pdf(data)
    elif low.endswith(".odt") or "opendocument" in ctype: text=extract_odt(data)
    else: text=""
    text=re.sub(r"[ \t]+", " ", text)
    text=re.sub(r"\n{3,}", "\n\n", text).strip()
    return text
