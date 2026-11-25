"""Owned fixture custody/commands only. No GUI, permissions or real typing."""
import json
from pathlib import Path
import plistlib
from types import SimpleNamespace

import pytest


@pytest.fixture
def fixture_module(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "scripts/desktop"))
    import cu_coexistence_fixture as fixture
    return fixture


def state(f, instance, expected_pid, **changes):
    value = {"instance_id": instance, "pid": expected_pid, "received_input_events": 0,
        "samples": 1, "updated_at": 100.0, "monotonic_at": 50.0, "is_active": True,
        "active_app_pid": expected_pid, "body_sha256": f.body_hash(0), "pointer_sha256": "b" * 64,
        "fixture_insertions": 0, "command_sequence": 0, "command_status": "ready"}
    return {**value, **changes}


@pytest.mark.parametrize("changes", [{"extra": "PRIVATE"}, {"pid": 999}, {"updated_at": 90},
    {"command_status": ["PRIVATE"]}, {"fixture_insertions": True}, {"command_sequence": -1},
    {"is_active": "PRIVATE"}, {"active_app_pid": False}, {"monotonic_at": float("nan")},
    {"pointer_sha256": "PRIVATE"}, {"received_input_events": -1}])
def test_state_requires_exact_owned_fresh_metadata(fixture_module, changes):
    f = fixture_module
    assert f.public_state(state(f, "owned", 123), "owned", 123, now=100.0) is not None
    assert f.public_state(state(f, "owned", 123, **changes), "owned", 123, now=100.0) is None


def test_construction_and_prepare_compile_only_unique_owned_bundles(fixture_module, tmp_path, monkeypatch):
    f = fixture_module
    calls = []
    def compile_only(args, **kwargs):
        calls.append(args)
        Path(args[-1]).write_bytes(b"synthetic fixture binary")
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(f.subprocess, "run", compile_only)
    monkeypatch.setattr(f.subprocess, "Popen", lambda *_a, **_k: pytest.fail("prepare launched GUI"))
    owned = f.OwnedUserWindows(tmp_path)
    result = owned.prepare()
    assert result["fixture_count"] == 2 and result["gui_started"] is False
    assert result["input_method"] == "source_owned_programmatic_fixture_insert"
    assert len(calls) == 1 and len(set(owned.instances)) == 2
    for slot, instance in enumerate(owned.instances):
        info = plistlib.loads((owned._path(slot, "Supymem owned user fixture.app") / "Contents/Info.plist").read_bytes())
        assert info["CFBundleIdentifier"] == "com.supymem.qa.userwindow." + instance
        assert info["NSPrincipalClass"] == "UserWindowApplication"
    owned.close()
    owned.close()
    assert owned.cleanup_verified


@pytest.mark.parametrize("action,slot,timeout", [("PRIVATE_ACTION", 0, 2), ("select", -1, 2),
    ("select", 0, 99), ("select", 0, float("nan")), (["PRIVATE"], 0, 2)])
def test_invalid_commands_have_no_file_or_process_effect(fixture_module, tmp_path, action, slot, timeout):
    f = fixture_module
    owned = f.OwnedUserWindows(tmp_path)
    owned.prepared = True
    owned.processes = [SimpleNamespace(pid=123), SimpleNamespace(pid=124)]
    before = list(owned.root.rglob("*"))
    with pytest.raises(ValueError):
        owned.issue(slot, action, timeout=timeout)
    assert list(owned.root.rglob("*")) == before and owned.receipts == []


def test_partial_start_closes_only_already_created_owned_process(fixture_module, tmp_path, monkeypatch):
    f = fixture_module
    owned = f.OwnedUserWindows(tmp_path)
    owned.prepared = True
    events = []
    process = SimpleNamespace(pid=123, poll=lambda: None,
        terminate=lambda: events.append("terminate_owned"), wait=lambda **_: events.append("wait_owned"))
    def spawn(*args, **kwargs):
        events.append("spawn")
        if len(events) > 1:
            raise OSError("PRIVATE_START_ERROR")
        return process
    monkeypatch.setattr(f.subprocess, "Popen", spawn)
    with pytest.raises(OSError):
        owned.start()
    assert events == ["spawn", "spawn", "terminate_owned", "wait_owned"]
    assert owned.closed and owned.cleanup_verified


def test_directory_replacement_refuses_before_command_write(fixture_module, tmp_path):
    f = fixture_module
    owned = f.OwnedUserWindows(tmp_path)
    old = owned.root.with_name(owned.root.name + "-preserved")
    owned.root.rename(old)
    owned.root.mkdir(mode=0o700)
    with pytest.raises(ValueError, match="custody_changed"):
        owned.issue(0, "select")
    assert list(owned.root.iterdir()) == []


def test_state_read_never_follows_symlink_or_admits_private_extra_fields(fixture_module, tmp_path, monkeypatch):
    import time
    f = fixture_module
    owned = f.OwnedUserWindows(tmp_path)
    owned.processes = [SimpleNamespace(pid=123)]
    folder = owned._path(0, "state.json").parent
    folder.mkdir(mode=0o700)
    other = tmp_path / "personal.json"
    other.write_text('{"PRIVATE":"not a fixture"}')
    owned._path(0, "state.json").symlink_to(other)
    assert owned.read(0) is None
    assert json.loads(other.read_text()) == {"PRIVATE": "not a fixture"}
    owned._path(0, "state.json").unlink()
    owned._path(0, "state.json").write_text(json.dumps(state(f, owned.instances[0], 123,
        updated_at=time.time(), extra="PRIVATE")))
    owned._path(0, "state.json").chmod(0o600)
    assert owned.read(0) is None


def test_unknown_delivery_is_consumed_once_without_replay(fixture_module, tmp_path, monkeypatch):
    f = fixture_module
    owned = f.OwnedUserWindows(tmp_path)
    owned.prepared = True
    owned.processes = [SimpleNamespace(pid=123, poll=lambda: None), SimpleNamespace(pid=124)]
    owned._path(0, "control.json").parent.mkdir(mode=0o700)
    monkeypatch.setattr(owned, "read", lambda _: None)
    with pytest.raises(RuntimeError, match="command_unconfirmed"):
        owned.issue(0, "select", timeout=.03)
    command = json.loads(owned._path(0, "control.json").read_text())
    assert command["sequence"] == 1 and owned.sequences == [1, 0]
    assert len(owned.receipts) == 1 and owned.receipts[0]["outcome"] == "unconfirmed"
    assert set(command) == {"instance_id", "sequence", "action", "issued_at"}


def test_failed_cleanup_is_sticky_and_never_kills_unowned_process(fixture_module, tmp_path):
    f = fixture_module
    owned = f.OwnedUserWindows(tmp_path)
    calls = []
    def fail():
        calls.append("owned_terminate")
        raise OSError("PRIVATE")
    owned.processes = [SimpleNamespace(pid=123, poll=lambda: None, terminate=fail)]
    for _ in range(2):
        with pytest.raises(RuntimeError, match="cleanup_unconfirmed"):
            owned.close()
    assert calls == ["owned_terminate"] and owned.cleanup_verified is False
