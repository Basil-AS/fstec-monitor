from __future__ import annotations

import asyncio
import difflib
import logging
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from .config import settings
from .db import SessionLocal
from .extract import semantic_text
from .http import Fetcher, conditional_headers
from .models import Attachment, AttachmentVersion, BotSetting, Document, Event, ScanRun, Snapshot
from .normalize import normalize_document_html, sha
from .parser import canonicalize, is_document_url, parse_page
from .storage import ObjectStore, StorageQuotaExceeded

log = logging.getLogger(__name__)
REMOVAL_CONFIRMATION_RUNS = 2
ATTACHMENT_SUFFIXES = (".odt", ".pdf", ".docx", ".doc", ".xlsx", ".xls", ".zip", ".rar", ".7z")


def category_key(value: str) -> str:
    return " ".join(value.replace("\xa0", " ").split()).casefold()


def attachment_group_key(link) -> str:
    """Return a stable title key shared by ODT/PDF variants."""
    title = category_key(link.title or "")
    for suffix in ATTACHMENT_SUFFIXES:
        if title.endswith(suffix):
            title = title[: -len(suffix)].rstrip(" ._-\u2013\u2014")
            break
    if title:
        return title
    path_name = urlparse(link.url).path.rsplit("/", 1)[-1].casefold()
    for suffix in ATTACHMENT_SUFFIXES:
        if path_name.endswith(suffix):
            path_name = path_name[: -len(suffix)]
            break
    return path_name


def preferred_attachment_urls(attachments) -> set[str]:
    """Choose one comparison source per attachment title, preferring ODT."""
    groups: dict[str, list] = {}
    for link in attachments:
        groups.setdefault(attachment_group_key(link), []).append(link)
    selected: set[str] = set()
    for links in groups.values():
        selected_link = next(
            (link for link in links if urlparse(link.url).path.casefold().endswith(".odt")),
            next(
                (link for link in links if urlparse(link.url).path.casefold().endswith(".pdf")),
                links[0],
            ),
        )
        selected.add(selected_link.url)
    return selected


def ignored_category_keys() -> set[str]:
    """Env-configured categories plus the ones toggled from the Telegram bot."""
    keys = set(settings.ignored_category_set)
    try:
        with SessionLocal() as s:
            row = s.get(BotSetting, "ignored_categories")
    except SQLAlchemyError:
        row = None
    if row and row.value:
        keys |= {category_key(v) for v in row.value.splitlines() if v.strip()}
    return keys


def snapshot_required(previous_semantic: str, current_semantic: str, has_previous: bool) -> bool:
    return not has_previous or previous_semantic != current_semantic


def reconcile_document_presence(session, seen_urls: set[str], baseline: bool = False) -> None:
    """Reconcile a completed catalog discovery with stored document state.

    A document must be absent from two complete discoveries before it is
    considered removed. This protects against transient catalog failures and
    makes removal notifications one-shot; a later sighting records a restore.
    """
    now = datetime.now(UTC)
    documents = session.scalars(select(Document)).all()
    for document in documents:
        if document.canonical_url in seen_urls:
            was_inactive = not document.active
            document.active = True
            document.missing_runs = 0
            document.last_seen_at = now
            if was_inactive and not baseline:
                session.add(Event(
                    document_id=document.id,
                    kind="document_restored",
                    severity="warning",
                    summary=f"восстановлен документ: {document.title or document.canonical_url}",
                    details=document.canonical_url,
                    notified=False,
                ))
            continue
        if not document.active:
            continue
        document.missing_runs += 1
        if document.missing_runs < REMOVAL_CONFIRMATION_RUNS:
            continue
        document.active = False
        if not baseline:
            session.add(Event(
                document_id=document.id,
                kind="document_removed",
                severity="warning",
                summary=f"удалён документ: {document.title or document.canonical_url}",
                details=document.canonical_url,
                notified=False,
            ))


async def gather_workers(workers):
    """Wait for every worker before the shared HTTP client can be closed."""
    return await asyncio.gather(*workers, return_exceptions=True)


class Monitor:
    def __init__(self): self.store=ObjectStore(); self.fetcher=Fetcher()
    async def close(self): await self.fetcher.close()
    def event(self,s,doc,kind,severity,summary,details=""):
        s.add(Event(document_id=doc.id if doc else None,kind=kind,severity=severity,summary=summary,details=details,notified=False))
    async def discover(self) -> set[str]:
        queue=[canonicalize(settings.catalog_url)]; seen=set(); docs=set()
        prefix=canonicalize(settings.catalog_url)
        ignored=ignored_category_keys()
        while queue:
            url=queue.pop(0)
            if url in seen: continue
            seen.add(url)
            r=await self.fetcher.get(url); r.raise_for_status()
            parsed=parse_page(r.text,str(r.url),prefix)
            if category_key(parsed.category) in ignored:
                continue
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
        validators=conditional_headers((cached_doc.current_etag if cached_doc else "") or (cached_snapshot.etag if cached_snapshot else ""), (cached_doc.current_last_modified if cached_doc else "") or (cached_snapshot.last_modified if cached_snapshot else "")) if cached_doc and not audit_due else {}
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
            raw_hash=sha(raw); html_hash=sha(normalized_html); text_hash=sha(text)
            previous_semantic=doc.current_semantic_sha256 or (previous.semantic_sha256 if previous else "")
            previous_html=doc.current_html_sha256 or (previous.html_sha256 if previous else "")
            semantic_changed=snapshot_required(previous_semantic,text_hash,previous is not None)
            markup_changed=bool(previous and previous_html and previous_html!=html_hash)
            if semantic_changed:
                raw_hash,raw_key=self.store.put(raw,".html")
                html_hash,html_key=self.store.put(normalized_html.encode(),".normalized.html")
                text_hash,text_key=self.store.put(text.encode(),".txt")
                s.add(Snapshot(document_id=doc.id,status_code=r.status_code,final_url=str(r.url),raw_sha256=raw_hash,semantic_sha256=text_hash,html_sha256=html_hash,raw_object=raw_key,normalized_html_object=html_key,normalized_text_object=text_key,etag=r.headers.get("etag", ""),last_modified=r.headers.get("last-modified", "")))
                if previous and not baseline:
                    old=self.store.read(previous.normalized_text_object).decode(errors="replace")
                    diff="\n".join(difflib.unified_diff(old.splitlines(),text.splitlines(),fromfile="old",tofile="new",n=3))[:12000]
                    self.event(s,doc,"html_content_changed","critical",f"изменена страница: {doc.title or url}",diff)
            elif markup_changed:
                raw_hash, raw_key = self.store.put(raw, ".html")
                html_hash, html_key = self.store.put(normalized_html.encode(), ".normalized.html")
                text_hash, text_key = self.store.put(text.encode(), ".txt")
                s.add(Snapshot(
                    document_id=doc.id,
                    status_code=r.status_code,
                    final_url=str(r.url),
                    raw_sha256=raw_hash,
                    semantic_sha256=text_hash,
                    html_sha256=html_hash,
                    raw_object=raw_key,
                    normalized_html_object=html_key,
                    normalized_text_object=text_key,
                    etag=r.headers.get("etag", ""),
                    last_modified=r.headers.get("last-modified", ""),
                ))
                if not baseline:
                    old_html=self.store.read(previous.normalized_html_object).decode(errors="replace")
                    diff="\n".join(difflib.unified_diff(old_html.splitlines(),normalized_html.splitlines(),fromfile="old-html",tofile="new-html",n=2))[:12000]
                    self.event(s,doc,"html_markup_changed","info",f"изменена HTML-разметка: {doc.title or url}",diff)
            doc.current_html_sha256=html_hash; doc.current_semantic_sha256=text_hash; doc.current_etag=r.headers.get("etag", ""); doc.current_last_modified=r.headers.get("last-modified", "")
            doc.title=parsed.title or doc.title; doc.category=parsed.category or doc.category; doc.last_seen_at=datetime.now(UTC); doc.active=True; doc.missing_runs=0
            active_urls={a.url for a in parsed.attachments}
            known=s.scalars(select(Attachment).where(Attachment.document_id==doc.id)).all()
            for a in known:
                if a.url not in active_urls and a.active:
                    a.active=False
                    if not baseline:self.event(s,doc,"attachment_removed","warning",f"удалено вложение: {a.display_name}",a.url)
            # Attachment content is the actual change source. Recheck the
            # preferred ODT/PDF variant on every document fetch; binary hashes
            # make unchanged files cheap while avoiding stale attachment data.
            audit_attachments = previous is None or previous.semantic_sha256 != text_hash or audit_due
            preferred_urls = preferred_attachment_urls(parsed.attachments)
            for link in parsed.attachments:
                att=s.scalar(select(Attachment).where(Attachment.document_id==doc.id,Attachment.url==link.url))
                if not att:
                    att=Attachment(document_id=doc.id,url=link.url,display_name=link.title); s.add(att); s.flush()
                    # A new document already explains the attachment. Emitting
                    # one more event per format produced misleading floods.
                    if not baseline and not is_new:
                        self.event(s,doc,"attachment_added","warning",f"добавлено вложение: {link.title}",link.url)
                att.active=True; att.last_seen_at=datetime.now(UTC); att.display_name=link.title
                if link.url not in preferred_urls:
                    continue
                if audit_attachments or not s.scalar(select(AttachmentVersion.id).where(AttachmentVersion.attachment_id==att.id).limit(1)):
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
            self.event(s,doc,"attachment_binary_changed" if same else "attachment_content_changed","info" if same else "critical",f"обновлено вложение: {att.display_name}",f"{att.url}\nold={previous.binary_sha256}\nnew={digest}\nsemantic_same={same}")

def PathSuffix(url,ctype):
    p=urlparse(url).path.lower()
    for ext in (".pdf",".odt",".docx",".doc",".xlsx",".xls",".zip",".rar",".7z"):
        if p.endswith(ext): return ext
    if "pdf" in ctype:return ".pdf"
    if "opendocument" in ctype:return ".odt"
    return ".bin"

async def run_monitor(baseline=False,limit=0,trigger="cli",progress_callback=None,cancel_event=None):
    m=Monitor()
    urls=[]; error=""; started=datetime.now(UTC); completed=0; errors_count=0
    def report(stage, done, total, errors):
        if progress_callback:
            progress_callback(stage, done, total, errors)
    log.info("scan started trigger=%s baseline=%s limit=%s", trigger, baseline, limit or "none")
    try:
        report("Обход каталога", 0, 0, 0)
        with SessionLocal() as s:
            ignored=ignored_category_keys()
            for doc in s.scalars(select(Document).where(Document.active.is_(True))).all():
                if category_key(doc.category) in ignored:
                    doc.active=False
            s.commit()
        try:
            urls=sorted(await m.discover())
        except Exception as e:  # record crawl failure instead of dying silently
            with SessionLocal() as s:
                s.add(Event(kind="fetch_error",severity="critical",summary="ошибка обхода каталога",details=repr(e))); s.commit()
            raise
        if limit: urls=urls[:limit]
        log.info("catalog discovery completed documents=%d", len(urls))
        report("Проверка документов", 0, len(urls), 0)
        semaphore=asyncio.Semaphore(settings.max_concurrency)
        async def process(url):
            nonlocal completed, errors_count
            if cancel_event and cancel_event.is_set():
                raise asyncio.CancelledError
            async with semaphore:
                try: await m.process_document(url,baseline)
                except StorageQuotaExceeded as e:
                    errors_count += 1
                    with SessionLocal() as s:
                        s.add(Event(kind="storage_error", severity="critical", summary=f"квота хранилища достигнута при загрузке {url}", details=str(e))); s.commit()
                except Exception as e:  # noqa: BLE001 — isolate one bad document from the full crawl
                    errors_count += 1
                    with SessionLocal() as s:
                        s.add(Event(kind="fetch_error",severity="warning",summary=f"ошибка загрузки {url}",details=repr(e))); s.commit()
                finally:
                    completed += 1
                    report("Проверка документов", completed, len(urls), errors_count)
        await gather_workers(process(url) for url in urls)
        if cancel_event and cancel_event.is_set():
            raise asyncio.CancelledError
        with SessionLocal() as s:
            reconcile_document_presence(s, set(urls), baseline=baseline)
            s.commit()
        log.info("scan workers completed documents=%d", len(urls))
    except BaseException as e:
        error=repr(e)
        raise
    finally:
        await m.close()
        try:
            with SessionLocal() as s:
                s.add(ScanRun(started_at=started,finished_at=datetime.now(UTC),documents=len(urls),trigger=trigger,baseline=baseline,error=error))
                s.commit()
        except SQLAlchemyError:  # history must never break the scan itself
            pass
        log.info("scan finished trigger=%s documents=%d error=%s", trigger, len(urls), bool(error))
    return len(urls)
