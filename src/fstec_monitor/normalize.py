from __future__ import annotations

import hashlib
import re

from bs4 import BeautifulSoup, Tag

NOISE_SELECTORS = [
    "script", "style", "noscript", "nav", "footer", ".breadcrumbs", ".breadcrumb",
    ".pagination", ".social", ".share", ".dropfiles-content", ".com-content-article__info",
    ".article-info", ".item-info", ".tags", ".mod-tagspopular", ".eb-inst",
]

def sha(data: bytes | str) -> str:
    if isinstance(data, str): data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()

def normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", value.replace("\xa0", " ")).strip()

def find_content_root(soup: BeautifulSoup) -> Tag:
    selectors = [
        ".com-content-article__body", ".item-page .com-content-article__body",
        "article", "main article", ".item-page", ".com-content-article", "main", "#content",
    ]
    candidates = [soup.select_one(s) for s in selectors]
    candidates = [c for c in candidates if c]
    return max(candidates, key=lambda x: len(x.get_text(" ", strip=True)), default=soup.body or soup)

def normalize_document_html(html: str) -> tuple[str, str]:
    soup = BeautifulSoup(html, "lxml")
    root = find_content_root(soup)
    fragment = BeautifulSoup(str(root), "lxml")
    for sel in NOISE_SELECTORS:
        for node in fragment.select(sel): node.decompose()
    for node in fragment.find_all(True):
        attrs = {}
        if node.name == "a" and node.get("href"): attrs["href"] = node["href"]
        if node.name in {"td", "th"}:
            for a in ("rowspan", "colspan"):
                if node.get(a): attrs[a] = node[a]
        node.attrs = attrs
    allowed = {"h1","h2","h3","h4","h5","h6","p","ol","ul","li","table","thead","tbody","tr","td","th","strong","b","em","i","sup","sub","br","a","div"}
    for node in list(fragment.find_all(True)):
        if node.name not in allowed and node.name not in {"html", "body"}: node.unwrap()
    normalized_html = str(fragment.body or fragment)
    lines=[]
    for node in fragment.find_all(["h1","h2","h3","h4","h5","h6","p","li","tr"]):
        if node.name == "tr":
            text = " | ".join(normalize_space(c.get_text(" ", strip=True)) for c in node.find_all(["th","td"], recursive=False))
        else: text = normalize_space(node.get_text(" ", strip=True))
        if text: lines.append(text)
    return normalized_html, "\n".join(lines)
