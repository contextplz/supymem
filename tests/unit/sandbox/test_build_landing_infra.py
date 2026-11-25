"""Fake sandbox allowlist, landing ingest, local infra adapter."""

from __future__ import annotations

import pytest
from tests.company_os_harness import company_os_test_client as TestClient

from tests.company_os_harness import load_company_os_app


def _boot(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/b12.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("IDEA_SYNC", "1")
    monkeypatch.setenv("BUILDER_SANDBOX", "fake")
    monkeypatch.setenv("LLM_OFFLINE", "1")
    monkeypatch.setenv("ONBOARDING_SYNC", "0")
    main = load_company_os_app()
    from app import state
    from app.worker.catalog import load_catalogue, load_trigger_policy

    load_catalogue.cache_clear()
    load_trigger_policy.cache_clear()
    state.reset_db_factory()
    return TestClient(main.create_app())


def test_terminal_allowlist() -> None:
    load_company_os_app()
    from app.sandbox.provider import FakeSandbox

    box = FakeSandbox()
    ok, reason = box.exec_allowed("npm test")
    assert ok is True
    denied, why = box.exec_allowed("curl https://example.com")
    assert denied is False
    assert why == "needs_approval"


@pytest.mark.parametrize("idea", ["Inventory forecasts for repair shops", "Swimming lesson journals for families"])
@pytest.mark.parametrize("repair", [False, True])
def test_local_legacy_build_uses_connected_tool_or_setup(tmp_path, monkeypatch, idea, repair) -> None:
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    client = _boot(tmp_path, monkeypatch)
    venture = client.post("/ventures", json={"raw_idea": idea, "start": False}).json()
    from app.builder import ctl
    from app.build import device_orders
    from app.core import local_capabilities
    from app.db.supymem_models import BuildRun, FounderRequest
    from app.state import db
    from app.workspaces.resolve import local_person
    from core.clock import utcnow
    from fastapi import HTTPException
    from sqlalchemy import select

    instruction = ("Improve " if repair else "Build ") + idea
    payload = {"workspace_id": venture["workspace_id"], "venture_id": venture["id"],
               "request_id": "request", "reason": "iteration" if repair else "product"}
    with db()() as session:
        session.add(FounderRequest(id="request", workspace_id=venture["workspace_id"], venture_id=venture["id"],
            source="iteration", intent="iterate", text=instruction, outcome="pending", created_at=utcnow()))
        session.commit()
    monkeypatch.setattr(local_capabilities, "enabled", lambda name: True)
    monkeypatch.setattr("app.coding_agents_consent.consents", lambda actor: {"codex": False})
    monkeypatch.setattr("desktop_runtime.agent_status.statuses", lambda: [])
    monkeypatch.setattr(ctl, "get_provider", lambda: pytest.fail("Your local build cannot use a fake sandbox"))
    calls = []

    def prepared(venture_id, caller, focus, **kwargs):
        assert caller.sub == local_person().id
        calls.append((venture_id, focus, kwargs))
        return {"id": "order", "venture_id": venture_id, "state": "pending", "review_digest": "digest"}

    entry = "prepare_coding_task_from_chat" if repair else "prepare_product_build_from_chat"
    other = "prepare_product_build_from_chat" if repair else "prepare_coding_task_from_chat"
    monkeypatch.setattr(device_orders, entry, prepared)
    monkeypatch.setattr(device_orders, other, lambda *args, **kwargs: pytest.fail("Your request used the wrong route"))
    monkeypatch.setattr(device_orders, "transition", lambda *args: {"id": "order", "venture_id": venture["id"],
        "state": "approved"})
    with pytest.raises(HTTPException) as blocked:
        ctl.run_build(payload)
    assert blocked.value.status_code == 409
    assert "Settings > Keys and connections > Coding tools" in blocked.value.detail["message"]
    assert not calls
    with db()() as session:
        assert session.get(FounderRequest, "request").outcome == "pending"
        assert not session.scalars(select(BuildRun)).all()
    monkeypatch.setattr("app.coding_agents_consent.consents", lambda actor: {"codex": True})
    monkeypatch.setattr("desktop_runtime.agent_status.statuses", lambda: [{"name": "codex", "installed": True,
        "signed_in": True, "subscription_login": True, "available": True}])
    monkeypatch.setattr("app.ventures.lifecycle.is_paused", lambda *args: False)
    order = ctl.run_build(payload)
    assert order["id"] == "order" and order["state"] == "approved" and order["review_path"]
    assert len(calls) == 1 and calls[0][0] == venture["id"] and instruction in calls[0][1]
    assert calls[0][2] == ({} if repair else {"provider": "codex"})
    with db()() as session:
        request = session.get(FounderRequest, "request")
        assert request.outcome == "accepted" and request.related_id == "order"
        assert not session.scalars(select(BuildRun)).all()
    with pytest.raises(HTTPException):
        ctl.run_build(payload)
    assert len(calls) == 1


@pytest.mark.parametrize("idea", ["Inventory forecasts for repair shops", "Swimming lesson journals for families"])
@pytest.mark.parametrize("provider_name", ["fake", "docker"])
def test_local_sandbox_start_never_generates_source(tmp_path, monkeypatch, idea, provider_name) -> None:
    load_company_os_app()
    from app.sandbox import provider
    from fastapi import HTTPException

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("BUILDER_SANDBOX", provider_name)
    root = tmp_path / "sandbox-data"
    monkeypatch.setattr(provider, "data_root", lambda: root)
    with pytest.raises(HTTPException) as blocked:
        provider.get_provider().start("requested-build", {"instruction": idea})
    assert "Settings > Keys and connections > Coding tools" in blocked.value.detail["message"]
    assert not root.exists()


@pytest.mark.parametrize("idea", ["Inventory forecasts for repair shops", "Swimming lesson journals for families"])
@pytest.mark.parametrize("provider_name", ["fake", "docker"])
def test_local_sandbox_status_requires_verified_results(tmp_path, monkeypatch, idea, provider_name) -> None:
    load_company_os_app()
    from app.sandbox import provider

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("BUILDER_SANDBOX", provider_name)
    (tmp_path / "index.html").write_text(idea, encoding="utf-8")
    status = provider.get_provider().status(str(tmp_path))
    assert status["state"] == "unverified"
    assert "Settings > Keys and connections > Coding tools" in status["message"]
    assert (tmp_path / "index.html").read_text(encoding="utf-8") == idea


@pytest.mark.parametrize("idea", ["Inventory forecasts for repair shops", "Swimming lesson journals for families"])
def test_local_qa_requires_checked_tool_results(monkeypatch, idea) -> None:
    load_company_os_app()
    from app.builder import ctl
    from fastapi import HTTPException

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setattr(ctl, "db", lambda: pytest.fail("Your local build needs actual coding tool checks"))
    with pytest.raises(HTTPException) as blocked:
        ctl.run_qa({"build_run_id": "legacy-build", "instruction": idea})
    assert blocked.value.status_code == 409
    assert blocked.value.detail["code"] == "verified_build_checks_required"
    assert "Build activity" in blocked.value.detail["message"]
    assert "Settings > Keys and connections > Coding tools" in blocked.value.detail["message"]


@pytest.mark.parametrize("idea", ["Inventory forecasts for repair shops", "Swimming lesson journals for families"])
def test_local_watch_never_reports_legacy_sandbox_success(tmp_path, monkeypatch, idea) -> None:
    from types import SimpleNamespace

    load_company_os_app()
    from app.builder import ctl

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    run = SimpleNamespace(state="succeeded", sandbox_ref=str(tmp_path), spec=idea)

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def get(self, model, run_id):
            return run

    monkeypatch.setattr(ctl, "db", lambda: Session)
    monkeypatch.setattr(ctl, "get_provider", lambda: pytest.fail("Your legacy build has no verified tool receipt"))
    result = ctl.watch_build({"build_run_id": "legacy-build"})
    assert result["state"] == "unverified" and result["sandbox"]["state"] == "unverified"
    assert "Settings > Keys and connections > Coding tools" in result["sandbox"]["message"]
    assert run.state == "succeeded"


@pytest.mark.hosted_only
@pytest.mark.parametrize("idea", ["Waitlist for warehouse receiving teams", "Waitlist for families learning pottery"])
def test_fake_legacy_build_storage_landing_and_infra(tmp_path, monkeypatch, idea) -> None:
    client = _boot(tmp_path, monkeypatch)
    ven = client.post("/ventures", json={"raw_idea": idea, "start": False}).json()
    vid = ven["id"]
    client.headers["X-Workspace-Id"] = ven["workspace_id"]
    # This is the explicit local fake-adapter test. Plan authoring has its own
    # signed, metered workflow; an unused template plan is no longer a fixture.
    from app.builder.ctl import run_build
    from app.infra.apps import provision, reconcile, sleep_app, wake_app
    from app.landing.service import capture_lead, generate_and_publish, ingest_visit

    built = run_build({"workspace_id": ven["workspace_id"], "venture_id": vid, "reason": "landing"})
    assert built["build_run_id"]
    events = client.get(f"/builds/{built['build_run_id']}/events")
    assert events.status_code == 200
    assert events.json()["items"] == []
    assert events.json()["provenance"] == "legacy_unverified"
    from app.db.supymem_models import BuildEvent
    from app.state import db
    from sqlalchemy import select

    with db()() as session:
        assert session.scalars(select(BuildEvent).where(BuildEvent.build_run_id == built["build_run_id"])).all()
    published = generate_and_publish(vid, ven["workspace_id"], idea)
    ingest_visit(published["analytics_key"], {"path": "/", "utm": {"utm_source": "test"}, "event": "page_view"})
    lead = capture_lead(published["id"], "baker@example.com")
    assert lead["email"] == "baker@example.com"
    infra = provision({"workspace_id": ven["workspace_id"], "venture_id": vid})
    slept = sleep_app(infra["app_id"])
    assert slept["state"] in {"sleeping", "building", "live", "deployed"}
    woke = wake_app(infra["app_id"])
    assert woke["id"] == infra["app_id"]
    report = reconcile({})
    assert report["adapter"] == "local"
    term = client.post(
        f"/apps/{infra['app_id']}/terminal/sessions",
        json={"command": "npm test"},
    )
    assert term.status_code == 200
    assert term.json()["allowed"] is True
    blocked = client.post(
        f"/apps/{infra['app_id']}/terminal/sessions",
        json={"command": "curl https://example.com"},
    )
    assert blocked.json()["allowed"] is False
