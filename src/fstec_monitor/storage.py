from __future__ import annotations

import hashlib
from pathlib import Path

from .config import settings


class StorageQuotaExceeded(RuntimeError):
    """Raised before a new object would exceed the configured history quota."""


class ObjectStore:
    def __init__(self, root: Path | None = None, *, quota_root: Path | None = None, quota_bytes: int | None = None):
        self.root = root or settings.storage_dir
        self.quota_root = quota_root or self.root.parent
        self.quota_bytes = settings.storage_quota_bytes if quota_bytes is None else quota_bytes
        self.root.mkdir(parents=True, exist_ok=True)

    def usage_bytes(self) -> int:
        return sum(path.stat().st_size for path in self.quota_root.rglob("*") if path.is_file())

    def quota_status(self) -> tuple[int, int]:
        return self.usage_bytes(), self.quota_bytes

    def put(self, data: bytes, suffix: str = "") -> tuple[str, str]:
        digest = hashlib.sha256(data).hexdigest()
        rel = Path(digest[:2]) / digest[2:4] / f"{digest}{suffix}"
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            current = self.usage_bytes()
            if current + len(data) > self.quota_bytes:
                raise StorageQuotaExceeded(
                    f"storage quota exceeded: {current + len(data)} > {self.quota_bytes} bytes"
                )
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_bytes(data)
            tmp.replace(path)
        return digest, str(rel)

    def save_report(self, event_id: int, data: bytes) -> str:
        """Persist a generated Markdown report without ever deleting report files."""
        if isinstance(event_id, bool) or not isinstance(event_id, int) or event_id < 1:
            raise ValueError("event_id must be a positive integer")

        rel = Path("reports") / f"event-{event_id}.md"
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        previous_size = path.stat().st_size if path.exists() else 0
        current = self.usage_bytes()
        if current - previous_size + len(data) > self.quota_bytes:
            raise StorageQuotaExceeded(
                f"storage quota exceeded: {current - previous_size + len(data)} > {self.quota_bytes} bytes"
            )
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)
        return str(rel)

    def read(self, key: str) -> bytes:
        return (self.root / key).read_bytes()
