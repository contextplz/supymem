"""Worker readiness follows database progress, not mere process startup."""
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

from tests.company_os_harness import load_company_os_app


def test_worker_health_requires_live_pid_fresh_poll_and_running_state(tmp_path, monkeypatch):
    load_company_os_app()
    from app.worker.health import healthy, path, write
    monkeypatch.setenv("COS_WORKER_HEALTH_PATH", str(tmp_path / "worker-health.json"))
    assert not healthy()
    write("running")
    assert healthy()
    path().write_text(json.dumps({"state": "running", "pid": os.getpid(), "at": time.time() - 120}))
    assert not healthy()
    write("stopped")
    assert not healthy()
    path().write_text('{"state":"running","pid":0}')
    assert not healthy()


def test_poll_and_heartbeat_health_writes_do_not_share_temporary_file(tmp_path, monkeypatch):
    load_company_os_app()
    from app.worker.health import healthy, write

    monkeypatch.setenv("COS_WORKER_HEALTH_PATH", str(tmp_path / "worker-health.json"))
    original_write = Path.write_text
    written = Barrier(2)

    def write_together(path, *args, **kwargs):
        result = original_write(path, *args, **kwargs)
        written.wait(timeout=5)
        return result

    monkeypatch.setattr(Path, "write_text", write_together)
    with ThreadPoolExecutor(max_workers=2) as pool:
        writes = [pool.submit(write, "running") for _ in range(2)]
        for pending in writes:
            pending.result(timeout=5)
    assert healthy()
    assert list(tmp_path.glob("*.tmp")) == []
