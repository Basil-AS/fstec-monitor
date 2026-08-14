from __future__ import annotations
import hashlib
from pathlib import Path
from .config import settings

class ObjectStore:
    def __init__(self, root: Path | None = None):
        self.root = root or settings.storage_dir
        self.root.mkdir(parents=True, exist_ok=True)

    def put(self, data: bytes, suffix: str = "") -> tuple[str, str]:
        digest = hashlib.sha256(data).hexdigest()
        rel = Path(digest[:2]) / digest[2:4] / f"{digest}{suffix}"
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_bytes(data)
            tmp.replace(path)
        return digest, str(rel)

    def read(self, key: str) -> bytes:
        return (self.root / key).read_bytes()
