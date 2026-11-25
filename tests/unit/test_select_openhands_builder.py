from __future__ import annotations

import pytest

from coding_agents import dispatcher
from coding_agents.detect import DetectedAgent
from coding_agents.inhouse import InhouseAdapter
from coding_agents.openhands_builder import OpenHandsBuilderAdapter
from coding_agents.protocol import CodingRequest, CodingResult
from coding_agents.select import hosted_inhouse_name, select


DOMAINS = ["Warehouse maintenance scheduling", "Family birdwatching journal"]
CONNECT_SCREEN = "Settings > Keys and connections > Coding tools"


@pytest.fixture(autouse=True)
def hosted_mode(monkeypatch):
    monkeypatch.delenv("SUPYMEM_MODE", raising=False)


def test_default_env_still_selects_inhouse_stub(monkeypatch):
    monkeypatch.delenv("CODING_AGENT", raising=False)
    assert hosted_inhouse_name() == "inhouse"
    choice = select(
        prefs=["inhouse"],
        detected=[],
        mode="local",
        bridge_connected=True,
    )
    assert choice is not None
    assert choice.adapter_name == "inhouse"


def test_openhands_env_selects_builder(monkeypatch):
    monkeypatch.setenv("CODING_AGENT", "openhands_builder")
    assert hosted_inhouse_name() == "openhands_builder"
    choice = select(
        prefs=["inhouse"],
        detected=[],
        mode="local",
        bridge_connected=True,
    )
    assert choice is not None
    assert choice.adapter_name == "openhands_builder"
    assert choice.mode == "inhouse_hosted"


def test_builder_infra_failure_falls_back_to_stub(monkeypatch):
    monkeypatch.setenv("CODING_AGENT", "openhands_builder")
    choice = select(
        prefs=["inhouse"],
        detected=[],
        mode="local",
        bridge_connected=True,
        failed=[("openhands_builder", "infra")],
    )
    assert choice is not None
    assert choice.adapter_name == "inhouse"


def test_hosted_fallback_uses_builder_when_opted_in(monkeypatch):
    monkeypatch.setenv("CODING_AGENT", "openhands_builder")
    choice = select(
        prefs=["cursor"],
        detected=[],
        mode="auto",
        bridge_connected=False,
        cloud_keys={},
        inhouse_hosted_available=True,
    )
    assert choice is not None
    assert choice.adapter_name == "openhands_builder"


@pytest.mark.parametrize("domain", DOMAINS)
def test_local_selection_keeps_subscription_tool_without_key_fallback(monkeypatch, domain):
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    connected = DetectedAgent("codex", "/synthetic/codex", "1", "subscription")
    choice = select(prefs=["codex"], detected=[connected], kind=domain)
    assert choice is not None and choice.detected is connected
    assert select(prefs=["inhouse", "openhands_builder"], detected=[], kind=domain) is None
    key_tool = DetectedAgent("codex", "/synthetic/codex", "1", "api_key")
    assert select(prefs=["codex", "claude"], detected=[key_tool,
        DetectedAgent("claude", "/synthetic/claude", "1", "subscription")], kind=domain) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("domain", DOMAINS)
@pytest.mark.parametrize("entry", ["dispatch", "dispatch_request"])
async def test_local_dispatch_passes_brief_or_connect_step_without_internal_work(monkeypatch, tmp_path, domain, entry):
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    connected = DetectedAgent("codex", "/synthetic/codex", "1", "subscription")
    monkeypatch.setattr(dispatcher, "detect", lambda: [connected])
    calls = []

    async def run(choice, request, **kwargs):
        calls.append((choice.adapter_name, request))
        return CodingResult(ok=True, session_id="subscription-result", changed_files=["tool-output.txt"])

    monkeypatch.setattr(dispatcher, "_run_selection", run)
    monkeypatch.setattr(dispatcher, "create_worktree", lambda *args: pytest.fail("Local fallback created files"))

    async def invoke():
        if entry == "dispatch":
            return await dispatcher.dispatch(prompt=domain, repo=tmp_path, branch="requested-build", prefs=["codex"])
        return await dispatcher.dispatch_request(CodingRequest(prompt=domain, workdir=tmp_path), prefs=["codex"])

    result = await invoke()
    assert result.ok and result.session_id == "subscription-result"
    assert len(calls) == 1 and calls[0][0] == "codex"
    assert domain in calls[0][1].prompt and calls[0][1].workdir == tmp_path
    monkeypatch.setattr(dispatcher, "detect", lambda: [])
    blocked = await invoke()
    assert not blocked.ok and CONNECT_SCREEN in blocked.error
    assert not blocked.changed_files and not blocked.argv
    assert len(calls) == 1 and not list(tmp_path.iterdir())


@pytest.mark.asyncio
@pytest.mark.parametrize("domain", DOMAINS)
@pytest.mark.parametrize("adapter_class", [InhouseAdapter, OpenHandsBuilderAdapter])
async def test_local_internal_adapter_refuses_before_files_or_billing(monkeypatch, tmp_path, domain, adapter_class):
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    adapter = adapter_class()

    async def forbidden(*args, **kwargs):
        pytest.fail("Local fallback reached the internal builder")

    monkeypatch.setattr(OpenHandsBuilderAdapter, "_post", forbidden)
    monkeypatch.setattr(adapter, "build_argv", lambda *args: pytest.fail("Local fallback prepared internal execution"))
    result = await adapter.run(CodingRequest(prompt=domain, workdir=tmp_path))
    assert not result.ok and CONNECT_SCREEN in result.error
    assert not result.argv and not result.changed_files and not list(tmp_path.iterdir())


@pytest.mark.asyncio
@pytest.mark.parametrize("domain", DOMAINS)
@pytest.mark.parametrize("name", ["inhouse", "openhands_builder", "codex_cloud"])
async def test_local_run_selection_cannot_bypass_tool_boundary(monkeypatch, tmp_path, domain, name):
    from coding_agents.select import Selection

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setattr(dispatcher, "load_adapter", lambda *args: pytest.fail("Local fallback loaded an adapter"))
    result = await dispatcher._run_selection(Selection(name, "inhouse_hosted", "fixture"),
        CodingRequest(prompt=domain, workdir=tmp_path), transcript_dir=None)
    assert not result.ok and CONNECT_SCREEN in result.error
    assert not list(tmp_path.iterdir())


@pytest.mark.asyncio
@pytest.mark.parametrize("domain", DOMAINS)
@pytest.mark.parametrize("failure", ["auth_failure", "binary_missing", "timed_out", "task_failed", "cancelled"])
async def test_local_tool_failure_keeps_result_and_gives_next_step(monkeypatch, tmp_path, domain, failure):
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setattr(dispatcher, "detect", lambda: [DetectedAgent("codex", "/synthetic/codex", "1", "subscription")])
    calls = []

    async def run(choice, request, **kwargs):
        calls.append(choice.adapter_name)
        return CodingResult(ok=False, error=failure, session_id="partial-result", changed_files=["partial.txt"])

    monkeypatch.setattr(dispatcher, "_run_selection", run)
    result = await dispatcher.dispatch_request(CodingRequest(prompt=domain, workdir=tmp_path), prefs=["codex"])
    assert not result.ok and calls == ["codex"]
    assert result.session_id == "partial-result" and result.changed_files == ["partial.txt"]
    if failure in {"auth_failure", "binary_missing"}:
        assert CONNECT_SCREEN in result.error
    else:
        assert "review" in result.error.lower() and "chat" in result.error.lower()


@pytest.mark.asyncio
@pytest.mark.parametrize("domain", DOMAINS)
async def test_local_api_queues_target_t_brief_and_stops_without_connected_tool(monkeypatch, domain):
    from tests.company_os_harness import load_company_os_app

    load_company_os_app()
    from app.admin import authn
    from app.api.coding_api import DispatchBody, post_dispatch
    from app.build import device_orders
    from app import coding_agents_consent
    from fastapi import HTTPException

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    caller = authn.CallerContext(sub="owner", email="", role="user", path="", method="POST", exp=0, nonce="")
    monkeypatch.setattr(authn, "verify_caller", lambda request: caller)
    monkeypatch.setattr("app.authz.workspace.assert_venture_access", lambda *args: ("owner", "workspace"))
    monkeypatch.setattr("app.core.local_capabilities.enabled", lambda name: True)
    monkeypatch.setattr("app.ventures.lifecycle.is_paused", lambda *args: False)
    monkeypatch.setattr(coding_agents_consent, "consents", lambda actor: {"codex": True})
    monkeypatch.setattr("desktop_runtime.agent_status.statuses", lambda: [{"name": "codex", "installed": True,
        "signed_in": True, "subscription_login": True, "available": True}])
    monkeypatch.setattr(dispatcher, "load_adapter", lambda *args: pytest.fail("API bypassed the device order"))
    orders = []

    def prepare(venture_id, owner, brief, **kwargs):
        orders.append((venture_id, owner, brief, kwargs))
        return {"id": "device-order", "venture_id": venture_id, "state": "pending", "review_digest": "digest"}

    monkeypatch.setattr(device_orders, "prepare_product_build_from_chat", prepare)
    approvals = []

    def transition(venture_id, owner, order_id, digest, action):
        approvals.append((order_id, digest, action))
        return {"id": order_id, "venture_id": venture_id, "state": "approved"}

    monkeypatch.setattr(device_orders, "transition", transition)
    body = DispatchBody(venture_id="venture", prompt="Build " + domain, prefs=["codex"], kind="product")
    result = await post_dispatch(body, object())
    assert result == {"id": "device-order", "venture_id": "venture", "state": "approved",
        "review_path": "/v/venture/device-work-orders/device-order"}
    assert orders == [("venture", caller, "Build the app with codex.\n\nBuild " + domain, {"provider": "codex"})]
    assert approvals == [("device-order", "digest", "approve")]
    monkeypatch.setattr(coding_agents_consent, "consents", lambda actor: {"codex": False})
    with pytest.raises(HTTPException) as blocked:
        await post_dispatch(body, object())
    assert blocked.value.status_code == 409 and "Coding tools" in blocked.value.detail["message"]
    assert len(orders) == len(approvals) == 1
