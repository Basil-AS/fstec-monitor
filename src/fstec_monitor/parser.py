from __future__ import annotations
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse, urldefrag
from bs4 import BeautifulSoup
from .normalize import find_content_root, normalize_space

FILE_EXTENSIONS = {".pdf", ".odt", ".doc", ".docx", ".xls", ".xlsx", ".zip", ".rar", ".7z"}

@dataclass(frozen=True)
class Link: url: str; title: str
@dataclass
class ParsedDocument:
    title: str
    category: str
    document_links: list[Link]
    attachments: list[Link]

def canonicalize(url: str) -> str:
    url = urldefrag(url)[0]
    parsed = urlparse(url)
    path = parsed.path.rstrip("/") or "/"
    return parsed._replace(path=path, query="", fragment="").geturl()

def is_attachment(url: str) -> bool:
    path = urlparse(url).path.lower()
    return any(path.endswith(ext) for ext in FILE_EXTENSIONS) or "/download/" in path or "?download" in url.lower()

def parse_page(html: str, page_url: str, catalog_prefix: str) -> ParsedDocument:
    soup = BeautifulSoup(html, "lxml")
    title_node = soup.select_one("h1") or soup.select_one("title")
    title = normalize_space(title_node.get_text(" ", strip=True)) if title_node else ""
    crumbs = [normalize_space(x.get_text(" ", strip=True)) for x in soup.select(".breadcrumb a, .breadcrumbs a")]
    category = crumbs[-1] if crumbs else ""
    root = find_content_root(soup)
    docs, files, seen_docs, seen_files = [], [], set(), set()
    for a in root.find_all("a", href=True):
        absolute = canonicalize(urljoin(page_url, a["href"]))
        text = normalize_space(a.get_text(" ", strip=True)) or absolute.rsplit("/",1)[-1]
        if is_attachment(absolute):
            if absolute not in seen_files: files.append(Link(absolute,text)); seen_files.add(absolute)
        elif absolute.startswith(catalog_prefix.rstrip("/") + "/") and absolute != canonicalize(page_url):
            if absolute not in seen_docs: docs.append(Link(absolute,text)); seen_docs.add(absolute)
    return ParsedDocument(title, category, docs, files)
