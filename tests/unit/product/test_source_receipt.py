from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from desktop_runtime.source_receipt import capture, load_archive


def _workspace(monkeypatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path / "data"))
    workspace = tmp_path / "Documents" / "Supymem" / "ventures" / "safe-venture"
    workspace.mkdir(parents=True)
    (workspace / ".supymem-venture").write_text("ven_test\n")
    (workspace / "app.py").write_text("print('first version')\n")
    (workspace / "artifacts").mkdir()
    (workspace / "artifacts" / "old-bundle.tar.gz").write_bytes(b"unrelated transport bundle")
    return workspace


def _capture(provider="codex") -> dict:
    return capture(venture_id="ven_test", workspace_id="wsp_test", actor_id="person_test",
                   device_id="dev_test", plan_task_id="tsk_test", workspace_slug="safe-venture",
                   order_id="dwo_test", execution_id="dex_test", provider=provider)


@pytest.mark.parametrize("domain", ["Supplier invoice checks", "Family trail journals"])
@pytest.mark.parametrize("mode", ["local", "hosted"])
@pytest.mark.parametrize("legacy", [False, True])
def test_tool_workspace_is_shared_by_source_custody_and_read_only_inspection(monkeypatch, tmp_path, domain, mode, legacy):
    from desktop_runtime import owned_inspect
    from desktop_runtime.owned_terminal import OwnedTerminal

    monkeypatch.setenv("SUPYMEM_MODE", mode)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path / "data"))
    old = tmp_path / "Documents" / "Supymem" / "ventures" / "safe-venture"
    if legacy:
        old.mkdir(parents=True)
        (old / ".supymem-venture").write_text("ven_test\n")
        (old / "previous.txt").write_text(domain)
    terminal = OwnedTerminal(venture_id="ven_test", workspace_slug="safe-venture", tracker=None, authorize=lambda: True)
    workspace = terminal.workspace()
    assert workspace == (old if legacy or mode == "hosted" else tmp_path / "data" / "workspaces" / "safe-venture")
    (workspace / "index.html").write_text(domain)
    receipt = _capture()
    assert receipt["qa_verified"] is False and load_archive(receipt)
    monkeypatch.setattr(owned_inspect, "cursor_installed", lambda: True)
    inventory = owned_inspect.OwnedInspect(venture_id="ven_test", workspace_slug="safe-venture").inventory()
    assert inventory["writes_performed"] is False and "index.html" in inventory["sample_paths"]
    if legacy:
        assert (workspace / "previous.txt").read_text() == domain
    (workspace / ".supymem-venture").write_text("ven_other\n")
    with pytest.raises(ValueError, match="source_receipt_workspace_owner_mismatch"):
        _capture()
    with pytest.raises(ValueError, match="inspect_workspace_owner_mismatch"):
        owned_inspect.OwnedInspect(venture_id="ven_test", workspace_slug="safe-venture").inventory()


@pytest.mark.parametrize("provider", ["codex", "claude", "cursor", "opencode"])
@pytest.mark.parametrize("domain", ["supplier invoice tracking", "family hiking journal"])
def test_source_receipt_is_immutable_and_bound_to_stopped_order(monkeypatch, tmp_path, provider, domain):
    workspace = _workspace(monkeypatch, tmp_path)
    (workspace / "app.py").write_text(domain)
    first = _capture(provider)
    directory = tmp_path / "data" / "ventures" / "ven_test" / "coding_sources" / "dwo_test"
    archive = directory / first["archive_name"]
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == first["sha256"]
    assert load_archive(first) == archive.read_bytes()
    assert first["qa_verified"] is False
    assert first["file_count"] == 2  # app plus workspace owner marker; old bundle excluded.
    (workspace / "app.py").write_text("print('later version')\n")
    assert _capture(provider) == first
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == first["sha256"]
    with pytest.raises(ValueError, match="source_receipt_reconciliation_required"):
        capture(venture_id="ven_test", workspace_id="wsp_test", actor_id="person_test",
                device_id="dev_test", plan_task_id="tsk_test", workspace_slug="safe-venture",
                order_id="dwo_test", execution_id="dex_other", provider=provider)
    with pytest.raises(ValueError, match="source_receipt_reconciliation_required"):
        capture(venture_id="ven_test", workspace_id="wsp_test", actor_id="other_person",
                device_id="dev_test", plan_task_id="tsk_test", workspace_slug="safe-venture",
                order_id="dwo_test", execution_id="dex_test", provider=provider)
    with pytest.raises(ValueError, match="source_receipt_reconciliation_required"):
        _capture("claude" if provider == "codex" else "codex")


def test_source_receipt_rejects_credentials_and_wrong_workspace(monkeypatch, tmp_path):
    workspace = _workspace(monkeypatch, tmp_path)
    with pytest.raises(ValueError, match="source_receipt_workspace_owner_mismatch"):
        capture(venture_id="ven_other", workspace_id="wsp_test", actor_id="person_test",
                device_id="dev_test", plan_task_id="tsk_test", workspace_slug="safe-venture",
                order_id="dwo_test", execution_id="dex_test", provider="codex")
    (workspace / ".env.local").write_text("PRIVATE=secret\n")
    with pytest.raises(ValueError, match="artifact_credential_path"):
        _capture()


def test_source_receipt_detects_corrupt_archived_bytes(monkeypatch, tmp_path):
    _workspace(monkeypatch, tmp_path)
    first = _capture()
    archive = (tmp_path / "data" / "ventures" / "ven_test" / "coding_sources" / "dwo_test"
               / first["archive_name"])
    archive.write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="source_receipt_reconciliation_required"):
        _capture()
    with pytest.raises(ValueError, match="source_receipt_reconciliation_required"):
        load_archive(first)
