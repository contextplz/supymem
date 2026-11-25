from __future__ import annotations

from fastapi.testclient import TestClient
import pytest

from tests.company_os_harness import load_company_os_app, local_owner_headers


def _client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/cuj.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LLM_OFFLINE", "1")
    main = load_company_os_app()
    from app import state

    state.reset_db_factory()
    return TestClient(main.create_app(), base_url="http://127.0.0.1",
        client=("127.0.0.1", 50000), headers=local_owner_headers())


def test_computer_use_job_is_single_claim_and_reports_running(tmp_path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)
    ven = client.post(
        "/ventures",
        json={"raw_idea": "A waitlist for neighbourhood bakers taking custom cake orders", "start": False},
    )
    assert ven.status_code == 201, ven.text
    vid = ven.json()["id"]
    from app.build.computer_use import enqueue_computer_use

    job = enqueue_computer_use(vid, prompt="Build the bakery waitlist.", title="Build")
    listed = client.get(f"/ventures/{vid}/computer-use/jobs")
    assert listed.status_code == 200, listed.text
    assert [item["id"] for item in listed.json()["items"]] == [job["id"]]
    claimed = client.post(f"/ventures/{vid}/computer-use/jobs/{job['id']}/claim")
    assert claimed.status_code == 200, claimed.text
    assert claimed.json()["status"] == "claimed"
    assert client.get(f"/ventures/{vid}/computer-use/jobs").json()["items"] == []
    duplicate = client.post(f"/ventures/{vid}/computer-use/jobs/{job['id']}/claim")
    assert duplicate.status_code == 409
    running = client.post(
        f"/ventures/{vid}/computer-use/jobs/{job['id']}/status",
        json={"status": "running", "detail": "Hermes session started."},
    )
    assert running.status_code == 200, running.text
    assert running.json()["status"] == "running"


def test_computer_use_job_rejects_invalid_or_out_of_order_status(tmp_path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)
    ven = client.post("/ventures", json={"raw_idea": "A bakery waitlist", "start": False})
    vid = ven.json()["id"]
    from app.build.computer_use import enqueue_computer_use

    job = enqueue_computer_use(vid, prompt="Build it.")
    invalid = client.post(
        f"/ventures/{vid}/computer-use/jobs/{job['id']}/status",
        json={"status": "completed"},
    )
    assert invalid.status_code == 409


@pytest.mark.parametrize("mode", ["hosted", "local"])
@pytest.mark.parametrize("idea", ["Supplier stock planning", "Family hiking routes"], ids=["b2b", "consumer"])
def test_onboarding_does_not_queue_computer_use_without_owner_background_work(tmp_path, monkeypatch, mode, idea) -> None:
    import types
    from unittest import mock

    monkeypatch.setenv("SUPYMEM_MODE", mode)
    _client(tmp_path, monkeypatch)
    if mode == "local":
        from app.core.local_secrets import update_settings

        update_settings("capabilities", {"background_work": False}, {})
    from app.onboarding.steps import build

    ctx = types.SimpleNamespace(venture=types.SimpleNamespace(id="v1", raw_idea=idea))
    for value in (None, "", "true", "0"):
        if value is None:
            monkeypatch.delenv("DEVICE_DESKTOP_EXECUTION_ENABLED", raising=False)
        else:
            monkeypatch.setenv("DEVICE_DESKTOP_EXECUTION_ENABLED", value)
        with mock.patch("app.build.computer_use.enqueue_computer_use") as enqueue:
            build._queue_computer_use(ctx, "Co", "one liner")
        enqueue.assert_not_called()
    monkeypatch.setenv("DEVICE_DESKTOP_EXECUTION_ENABLED", "1")
    with mock.patch("app.build.computer_use.enqueue_computer_use") as enqueue:
        build._queue_computer_use(ctx, "Co", "one liner")
    if mode == "local":
        enqueue.assert_not_called()
    else:
        enqueue.assert_called_once()
    if mode == "local":
        from app.core.local_secrets import update_settings

        update_settings("capabilities", {"background_work": False}, {})
        with mock.patch("app.build.computer_use.enqueue_computer_use") as enqueue:
            build._queue_computer_use(ctx, "Co", "one liner")
        enqueue.assert_not_called()
