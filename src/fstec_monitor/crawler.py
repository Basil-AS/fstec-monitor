from __future__ import annotations

import asyncio
import difflib
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse

from sqlalchemy import select

from .config import settings
from .db import SessionLocal
from .extract import semantic_text
from .http import Fetcher, conditional_headers
from .models import Attachment, AttachmentVersion, Document, Event, Snapshot
from .normalize import normalize_document_html, sha
from .parser import canonicalize, is_document_url, parse_page
from .storage import ObjectStore, StorageQuotaExceeded


class Monitor:
    def __init__(self): self.store=ObjectStore(); self.fetcher=Fetcher()
    async def close(self): await self.fetcher.close()
    def event(self,s,doc,kind,severity,summary,details=""):
        s.add(Event(document_id=doc.id if doc else None,kind=kind,severity=severity,summary=summary,details=details,notified=kind=="html_markup_changed"))
    async def discover(self) -> set[str]:
        queue=[canonicalize(settings.catalog_url)]; seen=set(); docs=set()
        prefix=canonicalize(settings.catalog_url)
        while queue:
            url=queue.pop(0)
            if url in seen: continue
            seen.add(url)
            r=await self.fetcher.get(url); r.raise_for_status()
            parsed=parse_page(r.text,str(r.url),prefix)
            for link in parsed.document_links:
                if is_document_url(link.url, prefix):
                    docs.add(link.url)
                elif link.url not in seen:
                    queue.append(link.url)
            if len(seen)>10000: raise RuntimeError("crawl safety limit exceeded")
        return docs
    async def process_document(self,url:str,baseline:bool=False):
        with SessionLocal() as cached_session:
            cached_doc=cached_session.scalar(select(Document).where(Document.canonical_url==url))
            cached_snapshot=(cached_session.scalar(select(Snapshot).where(Snapshot.document_id==cached_doc.id).order_by(Snapshot.id.desc())) if cached_doc else None)
        now=datetime.now(UTC)
        last_audit=cached_doc.last_attachment_audit_at if cached_doc else None
        if last_audit and last_audit.tzinfo is None:
            last_audit=last_audit.replace(tzinfo=UTC)
        audit_due=(cached_doc is None or last_audit is None or now-last_audit >= timedelta(seconds=settings.attachment_audit_interval_seconds))
        validators=conditional_headers(cached_snapshot.etag, cached_snapshot.last_modified) if cached_snapshot and not audit_due else {}
        r=await self.fetcher.get(url, headers=validators)
        if r.status_code==304 and cached_doc:
            with SessionLocal() as s:
                doc=s.get(Document,cached_doc.id); doc.last_seen_at=now; doc.active=True; doc.missing_runs=0; s.commit()
            return
        r.raise_for_status(); raw=r.content
        parsed=parse_page(r.text,str(r.url),canonicalize(settings.catalog_url))
        normalized_html,text=normalize_document_html(r.text)
        with SessionLocal() as s:
            doc=s.scalar(select(Document).where(Document.canonical_url==url))
            is_new=doc is None
            if is_new: doc=Document(canonical_url=url,title=parsed.title,category=parsed.category); s.add(doc); s.flush()
            previous=s.scalar(select(Snapshot).where(Snapshot.document_id==doc.id).order_by(Snapshot.id.desc()))
            raw_hash,raw_key=self.store.put(raw,".html")
            html_hash,html_key=self.store.put(normalized_html.encode(),".normalized.html")
            text_hash,text_key=self.store.put(text.encode(),".txt")
            changed=previous is None or previous.raw_sha256!=raw_hash or previous.semantic_sha256!=text_hash
            if changed:
                s.add(Snapshot(document_id=doc.id,status_code=r.status_code,final_url=str(r.url),raw_sha256=raw_hash,semantic_sha256=text_hash,html_sha256=html_hash,raw_object=raw_key,normalized_html_object=html_key,normalized_text_object=text_key,etag=r.headers.get("etag", ""),last_modified=r.headers.get("last-modified", "")))
                if previous and not baseline:
                    old=self.store.read(previous.normalized_text_object).decode(errors="replace")
                    diff="\n".join(difflib.unified_diff(old.splitlines(),text.splitlines(),fromfile="old",tofile="new",n=3))[:12000]
                    kind="html_content_changed" if previous.semantic_sha256!=text_hash else "html_markup_changed"
                    self.event(s,doc,kind,"critical" if kind=="html_content_changed" else "info",f"изменена страница: {doc.title or url}",diff)
            doc.title=parsed.title or doc.title; doc.category=parsed.category or doc.category; doc.last_seen_at=datetime.now(UTC); doc.active=True; doc.missing_runs=0
            active_urls={a.url for a in parsed.attachments}
            known=s.scalars(select(Attachment).where(Attachment.document_id==doc.id)).all()
            for a in known:
                if a.url not in active_urls and a.active:
                    a.active=False
                    if not baseline:self.event(s,doc,"attachment_removed","warning",f"удалено вложение: {a.display_name}",a.url)
            audit_attachments = previous is None or previous.semantic_sha256 != text_hash or audit_due
            for link in parsed.attachments:
                att=s.scalar(select(Attachment).where(Attachment.document_id==doc.id,Attachment.url==link.url))
                if not att:
                    att=Attachment(document_id=doc.id,url=link.url,display_name=link.title); s.add(att); s.flush()
                    if not baseline:self.event(s,doc,"attachment_added","warning",f"добавлено вложение: {link.title}",link.url)
                att.active=True; att.last_seen_at=datetime.now(UTC); att.display_name=link.title
                if audit_attachments:
                    try:
                        await self.process_attachment(s,doc,att,baseline)
                    except StorageQuotaExceeded as exc:
                        self.event(s, doc, "storage_error", "critical", f"квота хранилища достигнута: {att.display_name}", str(exc))
                    except Exception as exc:  # noqa: BLE001 — one broken attachment must not hide the document
                        self.event(s, doc, "fetch_error", "warning", f"ошибка вложения: {att.display_name}", repr(exc))
            if audit_attachments:
                doc.last_attachment_audit_at=now
            if is_new and not baseline:self.event(s,doc,"document_added","warning",f"добавлен документ: {doc.title or url}",url)
            s.commit()
    async def process_attachment(self,s,doc,att,baseline):
        previous=s.scalar(select(AttachmentVersion).where(AttachmentVersion.attachment_id==att.id).order_by(AttachmentVersion.id.desc()))
        r=await self.fetcher.get(att.url, headers=conditional_headers(previous.etag, previous.last_modified) if previous else {})
        if r.status_code==304:
            return
        r.raise_for_status(); data=r.content
        digest,key=self.store.put(data,PathSuffix(att.url,r.headers.get("content-type","")))
        if previous and previous.binary_sha256==digest:return
        text=semantic_text(data,r.headers.get("content-type",""),att.url); sem=sha(text) if text else ""
        _,text_key=self.store.put(text.encode(),".txt") if text else ("","")
        s.add(AttachmentVersion(attachment_id=att.id,status_code=r.status_code,content_type=r.headers.get("content-type",""),content_length=len(data),binary_sha256=digest,semantic_sha256=sem,object_key=key,extracted_text_key=text_key,etag=r.headers.get("etag", ""),last_modified=r.headers.get("last-modified", "")))
        if previous and not baseline:
            same=bool(sem and sem==previous.semantic_sha256)
            self.event(s,doc,"attachment_binary_changed" if same else "attachment_content_changed","info" if same else "critical",f"заменено вложение: {att.display_name}",f"{att.url}\nold={previous.binary_sha256}\nnew={digest}\nsemantic_same={same}")

def PathSuffix(url,ctype):
    p=urlparse(url).path.lower()
    for ext in (".pdf",".odt",".docx",".doc",".xlsx",".xls",".zip",".rar",".7z"):
        if p.endswith(ext): return ext
    if "pdf" in ctype:return ".pdf"
    if "opendocument" in ctype:return ".odt"
    return ".bin"

async def run_monitor(baseline=False,limit=0):
    m=Monitor()
    try:
        urls=sorted(await m.discover())
        if limit: urls=urls[:limit]
        semaphore=asyncio.Semaphore(settings.max_concurrency)
        async def process(url):
            async with semaphore:
                try: await m.process_document(url,baseline)
                except StorageQuotaExceeded as e:
                    with SessionLocal() as s:
                        s.add(Event(kind="storage_error", severity="critical", summary=f"квота хранилища достигнута при загрузке {url}", details=str(e))); s.commit()
                except Exception as e:  # noqa: BLE001 — isolate one bad document from the full crawl
                    with SessionLocal() as s:
                        s.add(Event(kind="fetch_error",severity="warning",summary=f"ошибка загрузки {url}",details=repr(e))); s.commit()
        await asyncio.gather(*(process(url) for url in urls))
    finally: await m.close()
    return len(urls)
