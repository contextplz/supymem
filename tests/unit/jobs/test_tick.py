"""Tick HMAC + dedupe window idempotency."""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timezone

import pytest

from tests.company_os_harness import company_os_test_client as TestClient

from tests.company_os_harness import load_company_os_app


def _sign(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


@pytest.mark.parametrize("domain", ["Repair-shop schedules", "Family meal plans"])
@pytest.mark.parametrize("mode", ["local", pytest.param("hosted", marks=pytest.mark.hosted)])
def test_tick_is_idempotent_per_window(tmp_path, monkeypatch, domain, mode) -> None:
    monkeypatch.setenv("SUPYMEM_MODE", mode)
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/tick.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COS_TICK_HMAC_SECRET", "tick-secret-at-least-32-bytes-long")
    main = load_company_os_app()
    from app import state
    state.reset_db_factory()
    body = json.dumps({"tick": "5m"}).encode()
    sig = _sign("tick-secret-at-least-32-bytes-long", body)
    with TestClient(main.create_app()) as client:
        r1 = client.post(
            "/internal/tick",
            content=body,
            headers={"Content-Type": "application/json", "X-Tick-Signature": sig},
        )
        r2 = client.post(
            "/internal/tick",
            content=body,
            headers={"Content-Type": "application/json", "X-Tick-Signature": sig},
        )
    if mode == "local":
        assert r1.status_code == r2.status_code == 404, domain
        from app.core.local_secrets import update_settings
        from app.credits import reserve
        from app.tasks.scheduler import dispatch_tick
        from app.worker.main import _run_maintenance

        update_settings("capabilities", {"background_work": True}, {})
        moment = datetime(2026, 10, 8, 12, 4, tzinfo=timezone.utc)
        first = dispatch_tick("5m", now=moment)
        assert first["ids"] == dispatch_tick("5m", now=moment)["ids"], domain
        sweeps = []
        monkeypatch.setattr(reserve, "sweep_expired", lambda: sweeps.append("credits"))
        _run_maintenance()
        assert sweeps == ["credits"], domain
        assert set(first["enqueued"]) == {"inbox.briefing.tick", "inbox.cos_brief.tick"}, domain
    else:
        assert r1.status_code == r2.status_code == 200, (r1.text, r2.text)
        assert r1.json()["ids"] == r2.json()["ids"]
        assert "credits.sweep" in r1.json()["enqueued"]


@pytest.mark.hosted
def test_tick_rejects_bad_hmac(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SUPYMEM_MODE", "hosted")
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/tick2.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COS_TICK_HMAC_SECRET", "tick-secret-at-least-32-bytes-long")
    main = load_company_os_app()
    from app import state

    state.reset_db_factory()
    with TestClient(main.create_app()) as client:
        r = client.post(
            "/internal/tick",
            json={"tick": "5m"},
            headers={"X-Tick-Signature": "nope"},
        )
    assert r.status_code == 401


def test_api_lifespan_does_not_start_will_scheduler(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/api.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    started: list[str] = []

    def boom() -> None:
        started.append("yes")
        raise AssertionError("scheduler must not start in the API process")

    main = load_company_os_app()
    monkeypatch.setattr("app.will.scheduler.default_scheduler", boom)
    from app import state

    state.reset_db_factory()
    with TestClient(main.create_app()):
        pass
    assert started == []
