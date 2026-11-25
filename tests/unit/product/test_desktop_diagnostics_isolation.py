"""Synthetic native failures retain evidence without touching installed history."""
import json


def test_default_fixture_error_log_and_queue_are_isolated(tmp_path, monkeypatch):
    from desktop_runtime import crash_report
    from pathlib import Path

    installed_home = tmp_path / "installed_home"
    installed_log = installed_home / "Library/Logs/Supymem/runtime.log"
    installed_log.parent.mkdir(parents=True)
    installed_log.write_text("Existing installed history\n")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: installed_home))
    monkeypatch.delenv("COMPANY_OS_DATA_DIR", raising=False)
    crash_report.report_error("device_worker", "run_mac_task", code="fixture_native_failure")
    assert installed_log.read_text() == "Existing installed history\n"
    log_path = crash_report._error_log_directory() / "runtime.log"
    assert log_path != installed_log and "fixture_native_failure" in log_path.read_text()
    queued = list(crash_report.queue_dir().glob("crash-*.json"))
    assert len(queued) == 1
    payload = json.loads(queued[0].read_text())
    assert payload["error_source"] == "device_worker"
    assert queued[0].is_relative_to(tmp_path / "desktop_data")
    assert not (installed_home / "crash_queue").exists()


def test_default_installed_log_location_is_preserved_without_override(tmp_path, monkeypatch):
    from desktop_runtime import crash_report
    from pathlib import Path

    monkeypatch.delenv("COMPANY_OS_RUNTIME_LOG_DIR")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert crash_report._error_log_directory() == tmp_path / "Library/Logs/Supymem"
