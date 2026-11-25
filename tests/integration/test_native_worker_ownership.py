"""Actual child-process identity and local durable recovery, not browser acceptance."""
import os
import sqlite3
import subprocess
import sys
import time

import psutil
import pytest
from desktop_runtime import device_journal
from desktop_runtime.owned_processes import OwnedProcesses


def test_owned_process_observation_does_not_adopt_preexisting_child():
    command = [sys.executable, "-c", "import time; time.sleep(30)"]
    baseline = subprocess.Popen(command)
    child = None
    try:
        tracker = OwnedProcesses()
        child = subprocess.Popen(command)
        tracker.capture()
        assert child.pid in {pid for pid, _ in tracker.observed}
        assert baseline.pid not in {pid for pid, _ in tracker.observed}
        assert tracker.stopped(timeout=.01) is False
        child.terminate()
        child.wait(timeout=5)
        assert tracker.stopped(timeout=.1) is True
        assert baseline.poll() is None
    finally:
        for process in (child, baseline):
            if process and process.poll() is None:
                process.terminate()
                process.wait(timeout=5)


def test_owned_process_cleanup_stops_descendant_that_created_new_session(tmp_path):
    child_pid = tmp_path / "child.pid"
    command = [sys.executable, "-c", (
        "import pathlib,subprocess,sys,time; "
        f"p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'],start_new_session=True); "
        f"pathlib.Path({str(child_pid)!r}).write_text(str(p.pid)); time.sleep(2)"
    )]
    tracker = OwnedProcesses()
    parent = psutil.Popen(command)
    tracker.register(parent)
    deadline = time.monotonic() + 3
    while not child_pid.exists() and time.monotonic() < deadline:
        time.sleep(.02)
    tracker.capture()
    escaped = int(child_pid.read_text())
    assert os.getpgid(escaped) != os.getpgid(os.getpid())
    assert tracker.stop_all(timeout=.5) is True
    assert OwnedProcesses.receipt_stopped(tracker.receipt()) is True


def test_native_journal_is_scoped_and_does_not_replay_interrupted_claim(monkeypatch, tmp_path):
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    scope = ("https://gateway.example.test", "usr_one", "dev_one")
    device_journal.create(scope, "ven_one", "dwo_one")
    device_journal.save(scope, "ven_one", "dwo_one", {"state": "claiming", "order_id": "dwo_one"})
    assert device_journal.read(scope, "ven_one", "dwo_one")["state"] == "claiming"
    with pytest.raises(ValueError, match="already_recorded"):
        device_journal.create(scope, "ven_one", "dwo_one")
    assert device_journal.read((scope[0], "usr_other", "dev_one"), "ven_one", "dwo_one") is None
    assert device_journal.read((scope[0], "usr_one", "dev_other"), "ven_one", "dwo_one") is None
    rotated = (scope[0], scope[1], "dev_other")
    assert device_journal.read_for_cancelled_recovery(rotated, "ven_one", "dwo_one", "dev_one")["state"] == "claiming"
    assert device_journal.read_for_cancelled_recovery((scope[0], "usr_other", "dev_other"),
                                                       "ven_one", "dwo_one", "dev_one") is None
    assert device_journal.read_for_cancelled_recovery(rotated, "ven_other", "dwo_one", "dev_one") is None
    # Only positively known not-started work may retry the same local launch.
    device_journal.save(scope, "ven_one", "dwo_one", {"state": "not_started"})
    device_journal.create(scope, "ven_one", "dwo_one")
    assert device_journal.read(scope, "ven_one", "dwo_one")["state"] == "starting"


def test_native_journal_upgrades_existing_outbox_database(monkeypatch, tmp_path):
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    database = tmp_path / "bridge_outbox.sqlite"
    with sqlite3.connect(database) as conn:
        conn.execute("""CREATE TABLE scoped_outbox (
            gateway TEXT NOT NULL, account_id TEXT NOT NULL, idempotency_key TEXT NOT NULL,
            method TEXT NOT NULL, path TEXT NOT NULL, body TEXT NOT NULL,
            created_at TEXT NOT NULL, sent_at TEXT, state TEXT NOT NULL DEFAULT 'queued',
            last_error TEXT, response_status INTEGER,
            PRIMARY KEY (gateway, account_id, idempotency_key))""")
    device_journal.probe()
    with sqlite3.connect(database) as conn:
        names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"scoped_outbox", "native_executions"} <= names


def test_quiesce_captures_new_session_before_parent_stop_and_blocks_late_write(tmp_path):
    marker, late = tmp_path / 'started', tmp_path / 'late'
    child_code = f"import pathlib,time; pathlib.Path({str(marker)!r}).write_text('started'); time.sleep(1.5); pathlib.Path({str(late)!r}).write_text('late')"
    parent_code = f"import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',{child_code!r}],start_new_session=True); time.sleep(20)"
    tracker = OwnedProcesses()
    parent = psutil.Popen([sys.executable, '-c', parent_code], start_new_session=True)
    tracker.register(parent)
    try:
        deadline = time.monotonic() + 3
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        assert marker.exists()
        # Cancel before the periodic one-second capture. Stopping the parent
        # group first used to orphan the child's separate process group.
        assert len(tracker.observed) == 1
        assert tracker.quiesce() is True
        os.killpg(parent.pid, __import__('signal').SIGKILL)
        parent.wait(timeout=3)
        assert tracker.stop_all(timeout=.1) is True
        assert len(tracker.observed) >= 2
        assert OwnedProcesses.receipt_stopped(tracker.receipt()) is True
        time.sleep(1.6)
        assert not late.exists()
    finally:
        tracker.stop_all(timeout=.1)
