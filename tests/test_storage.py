"""Regression tests for plugin-private state and directory counting."""

from __future__ import annotations

import asyncio

from plugin.plugins.git_memory import _count_files
from plugin.plugins.git_memory.storage import StateStore, StorageError


def test_count_files_skips_git_internals(tmp_path) -> None:
    root = tmp_path / "memory"
    (root / ".git" / "objects" / "pack").mkdir(parents=True)
    (root / ".git" / "HEAD").write_text("ref: refs/heads/main", encoding="utf-8")
    (root / ".git" / "config").write_text("[core]", encoding="utf-8")
    (root / ".git" / "objects" / "pack" / "a.pack").write_bytes(b"x")
    (root / "persona.txt").write_text("hello", encoding="utf-8")
    (root / "notes").mkdir()
    (root / "notes" / "n.md").write_text("n", encoding="utf-8")

    assert _count_files(root) == 2


def test_count_files_stops_at_cap(tmp_path) -> None:
    from plugin.plugins.git_memory import MAX_SCANNED_FILES

    root = tmp_path / "many"
    root.mkdir()
    for index in range(MAX_SCANNED_FILES + 10):
        (root / f"f{index}.txt").write_text("x", encoding="utf-8")

    assert _count_files(root) == MAX_SCANNED_FILES


def test_state_store_roundtrip_and_update(tmp_path) -> None:
    store = StateStore(tmp_path)
    assert store.load_sync() == {}

    asyncio.run(store.update({"a": 1}))

    assert store.load_sync() == {"a": 1}
    asyncio.run(store.update({"b": 2}))
    assert store.load_sync() == {"a": 1, "b": 2}


def test_state_store_update_merges_concurrent_patches(tmp_path) -> None:
    """并发 update 不得互相覆盖：每个 patch 都要出现在最终状态里。"""

    store = StateStore(tmp_path)
    asyncio.run(store.update({"base": 0}))

    async def scenario() -> None:
        await asyncio.gather(
            store.update({"last_sync": {"status": "ok"}}),
            store.update({"git": {"available": True}}),
            store.update({"pending_choice": {"code": "remote_newer"}}),
            store.update({"platform": {"platform": "windows"}}),
        )

    asyncio.run(scenario())

    state = store.load_sync()
    assert state["base"] == 0
    assert state["last_sync"] == {"status": "ok"}
    assert state["git"] == {"available": True}
    assert state["pending_choice"] == {"code": "remote_newer"}
    assert state["platform"] == {"platform": "windows"}


def test_state_store_rejects_corrupt_file(tmp_path) -> None:
    store = StateStore(tmp_path)
    store.path.write_text("not json at all", encoding="utf-8")

    assert store.load_sync() == {}
    asyncio.run(store.update({"a": 1}))
    assert store.load_sync() == {"a": 1}


def test_state_store_write_error_raises_storage_error(tmp_path, monkeypatch) -> None:
    from plugin.plugins.git_memory import storage as storage_module

    store = StateStore(tmp_path)
    monkeypatch.setattr(
        storage_module,
        "_atomic_write_bytes",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )

    try:
        asyncio.run(store.update({"a": 1}))
    except StorageError:
        pass
    else:
        raise AssertionError("写入失败必须抛出 StorageError")
