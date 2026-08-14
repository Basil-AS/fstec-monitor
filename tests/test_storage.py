import asyncio
from pathlib import Path

import pytest

from fstec_monitor.crawler import gather_workers
from fstec_monitor.storage import ObjectStore, StorageQuotaExceeded


def test_store_reuses_existing_object_without_quota_cost(tmp_path: Path):
    store = ObjectStore(tmp_path / "objects", quota_root=tmp_path, quota_bytes=3)
    store.put(b"abc", ".bin")
    store.put(b"abc", ".bin")
    assert store.usage_bytes() == 3


def test_store_rejects_new_object_over_quota_and_keeps_old_data(tmp_path: Path):
    store = ObjectStore(tmp_path / "objects", quota_root=tmp_path, quota_bytes=3)
    store.put(b"old", ".bin")
    with pytest.raises(StorageQuotaExceeded):
        store.put(b"new", ".bin")
    assert store.usage_bytes() == 3


def test_worker_gather_waits_for_slow_workers_after_one_fails():
    finished = []

    async def failed():
        raise RuntimeError("one worker failed")

    async def slow():
        await asyncio.sleep(0.01)
        finished.append(True)

    results = asyncio.run(gather_workers((failed(), slow())))
    assert isinstance(results[0], RuntimeError)
    assert finished == [True]
