"""Unit tests for Tier-1 working memory + MemoryPort local adapter."""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path

import pytest

from tests.company_os_harness import load_company_os_app


@pytest.fixture()
def cos(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/cos.db")
    monkeypatch.setenv("COMPANY_OS_WORKING_MEMORY_PATH", str(tmp_path / "wm.sqlite3"))
    monkeypatch.setenv("COMPANY_OS_MEMORY_PATH", str(tmp_path / "mem.sqlite3"))
    main = load_company_os_app()
    from app import state as state_mod
    from app.memory.working import reset_working_memory_for_tests

    state_mod.MEM = reset_working_memory_for_tests(tmp_path / "wm.sqlite3")
    return main


def test_working_memory_survives_reopen(cos, tmp_path: Path) -> None:
    from app.memory.working import WorkingMemory

    path = tmp_path / "wm_persist.sqlite3"
    wm = WorkingMemory(path)
    wm["brief:ven_1"] = {"idea": "physio reminders"}
    wm.close()

    wm2 = WorkingMemory(path)
    assert wm2["brief:ven_1"]["idea"] == "physio reminders"
    wm2.close()


def test_memory_defaults_use_configured_writable_data_volume(cos, tmp_path, monkeypatch) -> None:
    from app.memory.working import WorkingMemory
    from app.memory.adapters.local import _default_path

    monkeypatch.delenv("COMPANY_OS_WORKING_MEMORY_PATH")
    monkeypatch.delenv("COMPANY_OS_MEMORY_PATH")
    writable = tmp_path / "volume"
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(writable))
    wm = WorkingMemory()
    try:
        wm["volume-check"] = True
        assert wm.path == writable / "working_memory.sqlite3"
        assert _default_path() == writable / "institutional_memory.sqlite3"
    finally:
        wm.close()


def test_working_memory_dict_api(cos, tmp_path: Path) -> None:
    from app.memory.working import WorkingMemory

    wm = WorkingMemory(tmp_path / "wm2.sqlite3")
    wm["a"] = 1
    assert wm.get("missing") is None
    assert wm.pop("a") == 1
    assert "a" not in wm
    wm["b"] = {"x": True}
    wm.clear()
    assert len(wm) == 0
    wm.close()


@pytest.mark.asyncio
@pytest.mark.skipif(not Path("/proc/self/fd").is_dir(), reason="Requires Linux file descriptor accounting")
async def test_memory_connections_stay_bounded_and_locked_writes_fail(cos, tmp_path: Path) -> None:
    from app.memory.adapters.local import LocalMemoryAdapter
    from app.memory.model import MemoryKind, MemoryWrite
    from app.memory.working import WorkingMemory

    working_path = tmp_path.resolve() / "working" / "memory.db"
    durable_path = tmp_path.resolve() / "durable" / "memory.db"
    stores = []
    try:
        for index in range(24):
            working = WorkingMemory(working_path)
            durable = LocalMemoryAdapter(durable_path)
            stores.extend((working, durable))
            venture, statement = (
                ("warehouse", "Dispatchers need a live freight queue") if index % 2
                else ("gardening", "Apartment gardeners want watering reminders")
            )
            working[venture] = statement
            assert working[venture] == statement
            assert await durable.remember(MemoryWrite(
                venture_id=venture, kind=MemoryKind.INSIGHT, subject="workflow", statement=statement,
                evidence=["interview"], confidence=0.8, confidence_source="founder", author="founder",
                dedupe_key=f"observation-{index}",
            ))

        descriptors = []
        for entry in Path("/proc/self/fd").iterdir():
            try:
                descriptors.append(os.readlink(entry))
            except FileNotFoundError:
                continue
        counts = {
            path.parent.name: tuple(descriptors.count(f"{path}{suffix}") for suffix in ("", "-wal", "-shm"))
            for path in (working_path, durable_path)
        }
        print(f"Memory FD counts (db, wal, shm): {counts}")
        assert all(max(per_file) <= 1 for per_file in counts.values()), counts

        locked = threading.Event()
        release = threading.Event()

        def hold_writer():
            with closing(sqlite3.connect(working_path)) as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                locked.set()
                release.wait(timeout=12)

        writer = threading.Thread(target=hold_writer, daemon=True)
        writer.start()
        try:
            assert locked.wait(timeout=5)
            started = time.monotonic()
            with pytest.raises(sqlite3.OperationalError, match="locked|busy"):
                working["warehouse"] = "not saved"
            elapsed = time.monotonic() - started
            print(f"Contended memory write failed after {elapsed:.2f}s")
            assert elapsed < 10
            assert working["warehouse"] != "not saved"
        finally:
            release.set()
            writer.join(timeout=2)
        working["warehouse"] = "saved after release"
        with closing(WorkingMemory(working_path)) as reopened:
            assert reopened["warehouse"] == "saved after release"
    finally:
        for store in stores:
            store.close()


def test_rrf_fuse_prefers_agreement(cos) -> None:
    from app.memory.fuse import rrf_order

    a = ["x", "y", "z"]
    b = ["y", "x", "w"]
    ordered = rrf_order([a, b], limit=3)
    assert ordered[0] in {"x", "y"}


def test_distiller_stingy_on_mundane(cos) -> None:
    from app.memory.distill import distill_task

    out = distill_task(venture_id="ven_1", task_summary="heartbeat ok")
    assert out == []


def test_distiller_captures_founder_correction(cos) -> None:
    from app.memory.distill import distill_task
    from app.memory.model import MemoryKind

    out = distill_task(
        venture_id="ven_1",
        task_summary="drafted post",
        founder_correction="avoid corporate abstractions; prefer plain verbs",
    )
    assert len(out) >= 1
    assert out[0].kind is MemoryKind.STYLE_CORRECTION
    assert out[0].confidence == 1.0


@pytest.mark.asyncio
async def test_local_adapter_remember_recall_supersede(cos, tmp_path: Path) -> None:
    from app.memory.adapters.local import LocalMemoryAdapter
    from app.memory.model import MemoryKind, MemoryWrite, RecallQuery, TaskRef
    from app.tenancy import bind

    adapter = LocalMemoryAdapter(tmp_path / "mem_a.sqlite3")
    with bind(workspace_id="ws_1", venture_id="ven_1", actor="test"):
        mid = await adapter.remember(
            MemoryWrite(
                venture_id="ven_1",
                kind=MemoryKind.INSIGHT,
                subject="channel.primary",
                statement="WhatsApp beats email for solo practices",
                evidence=["sig_1"],
                confidence=0.78,
                confidence_source="gate",
                author="system",
                dedupe_key="dk1",
            )
        )
        hits = await adapter.recall(RecallQuery(venture_id="ven_1", text="WhatsApp email", limit=5))
        assert any(h.mem_id == mid for h in hits)

        new_id = await adapter.supersede(
            mid,
            by=MemoryWrite(
                venture_id="ven_1",
                kind=MemoryKind.INSIGHT,
                subject="channel.primary",
                statement="WhatsApp is the primary channel",
                evidence=["sig_2"],
                confidence=0.85,
                confidence_source="gate",
                author="system",
                dedupe_key="dk2",
            ),
            reason="stronger sample",
        )
        chain = await adapter.timeline(venture_id="ven_1", subject="channel.primary")
        assert len(chain) >= 2
        assert chain[0].superseded_by == new_id

        pack = await adapter.context_pack(
            venture_id="ven_1",
            task=TaskRef(title="write outreach", capability_id="cap.reach.email"),
        )
        assert pack.venture_id == "ven_1"

        ans = await adapter.answer(venture_id="ven_1", question="What channel works?")
        assert ans.abstained is False
        assert ans.citations

    with bind(workspace_id="ws_1", venture_id="ven_empty", actor="test"):
        empty2 = await adapter.answer(venture_id="ven_empty", question="anything")
        assert empty2.abstained is True

    adapter.close()


def test_rejects_llm_self_confidence(cos) -> None:
    from app.memory.model import MemoryKind, MemoryWrite

    with pytest.raises(Exception):
        MemoryWrite(
            venture_id="ven_1",
            kind=MemoryKind.INSIGHT,
            subject="x",
            statement="y",
            evidence=["e1"],
            confidence=0.9,
            confidence_source="llm_self",
            author="bot",
            dedupe_key="bad",
        )


@pytest.mark.asyncio
async def test_forget_tombstones_not_deletes(cos, tmp_path: Path) -> None:
    from app.memory.adapters.local import LocalMemoryAdapter
    from app.memory.model import MemoryKind, MemoryWrite, RecallQuery
    from app.tenancy import bind

    adapter = LocalMemoryAdapter(tmp_path / "mem3.sqlite3")
    with bind(workspace_id="ws_1", venture_id="ven_1", actor="test"):
        mid = await adapter.remember(
            MemoryWrite(
                venture_id="ven_1",
                kind=MemoryKind.CONSTRAINT,
                subject="legal.geo",
                statement="Cannot operate in EU without GDPR review",
                evidence=["policy_1"],
                confidence=1.0,
                confidence_source="rule",
                author="system",
                dedupe_key="c1",
            )
        )
        await adapter.forget(venture_id="ven_1", mem_id=mid, reason="founder asked")
        hits = await adapter.recall(RecallQuery(venture_id="ven_1", text="GDPR", limit=5))
        assert all(h.mem_id != mid for h in hits)
        chain = await adapter.timeline(venture_id="ven_1", subject="legal.geo")
        assert chain == []
    adapter.close()


@pytest.mark.asyncio
async def test_tenancy_blocks_cross_venture_supersede(cos, tmp_path: Path) -> None:
    from app.memory.adapters.local import LocalMemoryAdapter
    from app.memory.model import MemoryKind, MemoryWrite
    from app.tenancy import bind

    adapter = LocalMemoryAdapter(tmp_path / "mem4.sqlite3")
    with bind(workspace_id="ws_1", venture_id="ven_a", actor="test"):
        mid = await adapter.remember(
            MemoryWrite(
                venture_id="ven_a",
                kind=MemoryKind.DECISION,
                subject="price",
                statement="Price at 499",
                evidence=["d1"],
                confidence=0.9,
                confidence_source="founder",
                author="founder",
                dedupe_key="p1",
            )
        )
    with bind(workspace_id="ws_1", venture_id="ven_b", actor="test"):
        with pytest.raises(PermissionError):
            await adapter.supersede(
                mid,
                by=MemoryWrite(
                    venture_id="ven_b",
                    kind=MemoryKind.DECISION,
                    subject="price",
                    statement="Price at 999",
                    evidence=["d2"],
                    confidence=0.9,
                    confidence_source="founder",
                    author="founder",
                    dedupe_key="p2",
                ),
                reason="steal",
            )
    adapter.close()


def test_mem_is_working_memory(cos) -> None:
    from app import state as state_mod
    from app.memory.working import WorkingMemory

    assert isinstance(state_mod.MEM, WorkingMemory)
