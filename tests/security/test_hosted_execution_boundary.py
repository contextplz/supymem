"""Synthetic, offline checks for hosted credentials and child-process admission."""
from __future__ import annotations

from contextlib import nullcontext
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from coding_agents import load_adapter
from coding_agents.env import CHILD_ENV_VARS, child_env, hosted_execution
from coding_agents.protocol import CodingRequest, CodingResult
from coding_agents.select import Selection
from tests.company_os_harness import load_company_os_app
from tests.security.test_core_signed_boundary import client as signed_client, headers, make_workspace


@pytest.fixture
def local_env(monkeypatch):
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("COMPANY_OS_ENV", "local")
    monkeypatch.setenv("COMPANY_OS_REQUIRE_SIGNED_CALLER", "0")
    for name in ("ECS_CONTAINER_METADATA_URI", "ECS_CONTAINER_METADATA_URI_V4", "PUBLIC_GATEWAY_URL"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def core_http(tmp_path, monkeypatch, local_env):
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/synthetic.db")
    monkeypatch.setenv("LLM_OFFLINE", "1")
    load_company_os_app()
    from app.authz import http
    return http


def _request(path: str) -> Request:
    return Request({"type": "http", "method": "POST", "path": path,
                    "headers": [], "query_string": b""},
                   receive=AsyncMock(return_value={"type": "http.request", "body": b"{}", "more_body": False}))


def test_signed_editor_cannot_mint_hermes_or_test_byok(signed_client, monkeypatch):
    from app.api import hermes_api, llm_settings_api
    from app.db.models import Venture, WorkspaceMember
    from app.state import db
    from core.clock import utcnow
    from llm_gateway import sole
    wid = make_workspace(signed_client, "usr_alice")
    make_workspace(signed_client, "usr_bob")
    with db()() as session:
        session.add(Venture(id="ven_synthetic_credentials", workspace_id=wid,
                            name="Synthetic security check", raw_idea="Synthetic security check",
                            stage="draft", autonomy_level="L1", created_at=utcnow(), updated_at=utcnow()))
        session.add(WorkspaceMember(id="wsm_synthetic_editor", workspace_id=wid,
                                    person_id="usr_bob", role="editor", created_at=utcnow()))
        session.commit()
    calls = []
    monkeypatch.setattr(hermes_api, "bootstrap_hermes", lambda *args, **kwargs: calls.append("mint") or {"synthetic": True})
    monkeypatch.setattr(llm_settings_api.founder_provider, "to_provider_config", lambda wid: None)
    completion = AsyncMock(return_value=SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))]))
    monkeypatch.setattr(sole, "env_chat_completion", completion)
    for path in ("/ventures/ven_synthetic_credentials/hermes/bootstrap", f"/workspaces/{wid}/llm-settings/test"):
        denied = signed_client.post(path, headers=headers(path, "POST", "usr_bob"))
        assert denied.status_code == 403, denied.text
        assert denied.json()["detail"]["code"] == "workspace_admin_required"
    assert calls == []
    completion.assert_not_awaited()
    for path in ("/ventures/ven_synthetic_credentials/hermes/bootstrap", f"/workspaces/{wid}/llm-settings/test"):
        allowed = signed_client.post(path, headers=headers(path, "POST", "usr_alice"))
        assert allowed.status_code == 200, allowed.text
    assert calls == ["mint"]
    completion.assert_awaited_once()


def test_signed_dispatch_denied_before_body_and_files(signed_client, monkeypatch):
    from app.api import coding_api
    def forbidden(*args, **kwargs):
        pytest.fail("hosted HTTP request reached coding execution")
    monkeypatch.setattr(coding_api, "dispatch", forbidden)
    path = "/coding/dispatch"
    denied = signed_client.post(path, json={}, headers=headers(path, "POST"))
    assert denied.status_code == 403, denied.text
    assert denied.json()["detail"]["code"] == "tenant_contract_required"


@pytest.mark.asyncio
@pytest.mark.parametrize("path,params", [
    ("/ventures/ven_synthetic/hermes/bootstrap", {"venture_id": "ven_synthetic"}),
    ("/workspaces/ws_synthetic/llm-settings/test", {"workspace_id": "ws_synthetic"}),
    ("/workspaces/ws_synthetic/llm-settings/test/", {"workspace_id": "ws_synthetic"}),
])
@pytest.mark.parametrize("role", ["editor", "owner", "admin"])
async def test_credentials_require_admin_before_handler(core_http, monkeypatch, path, params, role):
    from app.authz import workspace
    from app.workspaces import service
    monkeypatch.setattr(core_http, "assert_venture_access", lambda *args: ("ven_synthetic", "ws_synthetic"))
    monkeypatch.setattr(core_http, "assert_workspace_access", lambda *args: "ws_synthetic")
    monkeypatch.setattr(workspace, "subject_person_id", lambda subject: "per_synthetic")
    monkeypatch.setattr(service, "role_of", lambda *args: role)
    if role == "editor":
        with pytest.raises(HTTPException) as denied:
            await core_http.resource_scope(_request(path), "app.api.hermes_api", params, "synthetic", signed=True)
        assert denied.value.status_code == 403
        assert denied.value.detail["code"] == "workspace_admin_required"
    else:
        scope = await core_http.resource_scope(_request(path), "app.api.hermes_api", params, "synthetic", signed=True)
        assert scope.workspace_id == "ws_synthetic"


@pytest.mark.parametrize("path", ["/coding/dispatch", "/coding/dispatch/"])
def test_hosted_dispatch_route_is_closed(core_http, path):
    with pytest.raises(HTTPException) as denied:
        core_http.enforce_hosted_contract(_request(path), "app.api.coding_api")
    assert denied.value.status_code == 403
    assert denied.value.detail["code"] == "tenant_contract_required"


@pytest.mark.asyncio
@pytest.mark.parametrize("binding,status", [("same_owner", 200), ("other_venture", 404), ("other_owner", 403), ("missing", 404)])
async def test_handoff_resource_requires_venture_and_owner(core_http, monkeypatch, binding, status):
    from app import state
    from app.authz import workspace
    from app.workspaces import service
    model = ModuleType("app.db.task_models")
    model.Task = type("SyntheticTask", (), {})
    monkeypatch.setitem(sys.modules, "app.db.task_models", model)
    row = SimpleNamespace(workspace_id="ws_synthetic",
                          venture_id="ven_other" if binding == "other_venture" else "ven_synthetic",
                          owner_person_id="per_other" if binding == "other_owner" else "per_synthetic")
    monkeypatch.setattr(state, "db", lambda: lambda: nullcontext(SimpleNamespace(
        get=lambda *args: None if binding == "missing" else row)))
    monkeypatch.setattr(core_http, "assert_venture_access", lambda *args: ("ven_synthetic", "ws_synthetic"))
    monkeypatch.setattr(core_http, "assert_workspace_access", lambda *args: "ws_synthetic")
    monkeypatch.setattr(workspace, "subject_person_id", lambda subject: "per_synthetic")
    monkeypatch.setattr(service, "role_of", lambda *args: "owner")
    request = _request("/ventures/ven_synthetic/handoffs/handoff_synthetic/cancel")
    params = {"venture_id": "ven_synthetic", "handoff_id": "handoff_synthetic"}
    if status == 200:
        scope = await core_http.resource_scope(request, "app.api.handoffs_api", params, "synthetic", signed=True)
        assert scope.venture_id == "ven_synthetic"
    else:
        with pytest.raises(HTTPException) as denied:
            await core_http.resource_scope(request, "app.api.handoffs_api", params, "synthetic", signed=True)
        assert denied.value.status_code == status


@pytest.mark.asyncio
async def test_unavailable_handoff_model_fails_closed(core_http, monkeypatch):
    monkeypatch.setitem(sys.modules, "app.db.task_models", None)
    with pytest.raises(HTTPException) as denied:
        await core_http.resource_scope(_request("/handoffs/synthetic"), "app.api.handoffs_api",
                                       {"handoff_id": "synthetic"}, "synthetic", signed=True)
    assert denied.value.status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("identifier", ["handoff_id", "task_id"])
@pytest.mark.parametrize("binding,status", [
    ("same_owner", 200), ("other_owner", 403), ("missing", 404),
    ("workspace_mismatch", 404), ("foreign_workspace", 404), ("viewer", 403),
])
async def test_durable_task_alias_binds_saved_venture_workspace_and_owner(core_http, monkeypatch, identifier, binding, status):
    from app import state
    from app.authz import workspace
    from app.workspaces import service
    model = ModuleType("app.db.task_models")
    model.Task = type("SyntheticTask", (), {})
    monkeypatch.setitem(sys.modules, "app.db.task_models", model)
    # Deliberately use an ID with no durable-task prefix. The handler module,
    # persisted row and verified subject establish authority.
    resource_id = "planned-looking-synthetic"
    row = SimpleNamespace(workspace_id="ws_other" if binding == "workspace_mismatch" else "ws_synthetic",
                          venture_id="ven_synthetic",
                          owner_person_id="per_other" if binding == "other_owner" else "per_synthetic")
    lookups = []
    def get(saved_model, saved_id):
        lookups.append((saved_model, saved_id))
        assert saved_model is model.Task
        return None if binding == "missing" else row
    monkeypatch.setattr(state, "db", lambda: lambda: nullcontext(SimpleNamespace(get=get)))
    def venture_access(venture_id, subject):
        assert venture_id == "ven_synthetic" and subject == "synthetic"
        if binding == "foreign_workspace":
            raise core_http._not_found()
        return venture_id, "ws_synthetic"
    monkeypatch.setattr(core_http, "assert_venture_access", venture_access)
    # The founder can access both workspaces. A contradictory saved Task /
    # Venture binding must still fail independently of membership checks.
    monkeypatch.setattr(core_http, "assert_workspace_access", lambda wid, subject: wid)
    monkeypatch.setattr(workspace, "subject_person_id", lambda subject: "per_synthetic")
    monkeypatch.setattr(service, "role_of", lambda *args: "viewer" if binding == "viewer" else "owner")
    params = {identifier: resource_id}
    path = f"/tasks/{resource_id}/cancel" if identifier == "task_id" else f"/handoffs/{resource_id}/cancel"
    if status == 200:
        scope = await core_http.resource_scope(_request(path), "app.api.handoffs_api", params, "synthetic", signed=True)
        assert (scope.workspace_id, scope.venture_id, scope.actor) == ("ws_synthetic", "ven_synthetic", "synthetic")
    else:
        with pytest.raises(HTTPException) as denied:
            await core_http.resource_scope(_request(path), "app.api.handoffs_api", params, "synthetic", signed=True)
        assert denied.value.status_code == status
    assert lookups == [(model.Task, resource_id)]


@pytest.mark.asyncio
async def test_durable_task_alias_without_model_fails_closed(core_http, monkeypatch):
    monkeypatch.setitem(sys.modules, "app.db.task_models", None)
    with pytest.raises(HTTPException) as denied:
        await core_http.resource_scope(_request("/tasks/planned-looking-synthetic/cancel"), "app.api.handoffs_api",
                                       {"task_id": "planned-looking-synthetic"}, "synthetic", signed=True)
    assert denied.value.status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("module", ["app.api.tasks_api", "app.api.plans_api", ""])
async def test_unrelated_task_id_never_uses_durable_task_resolver(core_http, monkeypatch, module):
    from app.authz import workspace
    from app.workspaces import service
    monkeypatch.setitem(sys.modules, "app.db.task_models", None)
    monkeypatch.setattr(core_http, "assert_venture_access", lambda vid, subject: (vid, "ws_synthetic"))
    monkeypatch.setattr(workspace, "subject_person_id", lambda subject: "per_synthetic")
    monkeypatch.setattr(service, "role_of", lambda *args: "owner")
    # The plans API still uses its existing resource resolver. A task_id alone
    # is not one of its declared journey resource IDs.
    if module == "app.api.plans_api":
        monkeypatch.setattr(core_http, "journey_resources", lambda params: [])
    scope = await core_http.resource_scope(_request("/ventures/ven_synthetic/tasks/tsk_synthetic/run"), module,
        {"venture_id": "ven_synthetic", "task_id": "tsk_synthetic"}, "synthetic", signed=True)
    assert (scope.workspace_id, scope.venture_id) == ("ws_synthetic", "ven_synthetic")


@pytest.mark.parametrize("app_env,expected", [
    ("production", "https://gateway.supymem.com"),
    ("staging", "https://gateway.staging.supymem.com"),
    ("local", "http://127.0.0.1:3200"),
])
def test_hermes_uses_trusted_gateway(core_http, monkeypatch, app_env, expected):
    from app.hermes.bootstrap import _public_origin
    monkeypatch.setenv("APP_ENV", app_env)
    assert _public_origin(None) == expected
    assert _public_origin(expected + "/") == expected


def test_signed_local_qa_keeps_its_configured_gateway(core_http, monkeypatch):
    from app.hermes.bootstrap import _public_origin
    monkeypatch.setenv("COMPANY_OS_REQUIRE_SIGNED_CALLER", "1")
    monkeypatch.setenv("PUBLIC_GATEWAY_URL", "http://127.0.0.1:4320")
    assert _public_origin("http://127.0.0.1:4320") == "http://127.0.0.1:4320"


@pytest.mark.parametrize("origin", [
    "https://attacker.example", "https://gateway.supymem.com.attacker.example",
    "https://gateway.supymem.com@attacker.example", "https://gateway.supymem.com/path",
    "https://gateway.supymem.com?redirect=attacker", "http://127.0.0.1:3200", "https://[",
])
def test_bad_hermes_origin_cannot_revoke_or_mint(core_http, monkeypatch, origin):
    from app.hermes import bootstrap
    monkeypatch.setenv("APP_ENV", "production")
    def forbidden(*args, **kwargs):
        pytest.fail("invalid origin crossed credential-mutation boundary")
    for name in ("ensure_hermes_app", "_revoke_hermes_keys", "create_key"):
        monkeypatch.setattr(bootstrap, name, forbidden)
    with pytest.raises(HTTPException) as denied:
        bootstrap.bootstrap_hermes("ws_synthetic", "ven_synthetic", public_origin=origin)
    assert denied.value.status_code == 400
    assert denied.value.detail["code"] == "hermes_origin_not_allowed"


def test_child_env_never_inherits_platform_or_unknown_credentials(local_env):
    safe = {key: "/synthetic" for key in CHILD_ENV_VARS}
    unsafe = {key: "synthetic" for key in (
        "OPENAI_API_KEY", "PAGEBUILDER_OPENAI_API_KEY", "OPENAI_DEVICE_VISION_API_KEY",
        "COMPOSIO_API_KEY", "RESEND_API_KEY", "COS_INTERNAL_ASSERTION_SECRET",
        "SESSION_SECRET", "COMPANY_OS_VAULT_SECRET", "DATABASE_URL", "COMPANY_OS_DB_URL",
        "STRIPE_SECRET_KEY", "GITHUB_TOKEN", "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
        "AWS_CONTAINER_CREDENTIALS_FULL_URI", "AWS_SESSION_TOKEN", "NEW_VENDOR_SECRET",
        "NODE_OPTIONS", "PYTHONPATH", "LD_PRELOAD", "HTTPS_PROXY", "_COS_OWNED_AUTH",
    )}
    assert child_env(safe | unsafe) == safe
    assert {key: child_env(safe)[key] for key in ("HOME", "USER", "LOGNAME")} == {
        key: safe[key] for key in ("HOME", "USER", "LOGNAME")}


@pytest.mark.parametrize("signal", [
    {"APP_ENV": "production"}, {"APP_ENV": "staging"}, {"APP_ENV": "invalid"},
    {"COMPANY_OS_ENV": "cloud"}, {"COMPANY_OS_REQUIRE_SIGNED_CALLER": "1"},
    {"ECS_CONTAINER_METADATA_URI_V4": "http://169.254.170.2/synthetic"},
])
def test_hosted_signals_close_direct_local_adapter_import(local_env, monkeypatch, signal):
    for name, value in signal.items():
        monkeypatch.setenv(name, value)
    assert hosted_execution()
    with pytest.raises(RuntimeError, match="excluded from cloud"):
        load_adapter("opencode")


def test_hosted_detection_does_not_launch_version_commands(local_env, monkeypatch):
    import coding_agents.detect as detection
    monkeypatch.setenv("APP_ENV", "production")
    def forbidden(*args, **kwargs):
        pytest.fail("hosted discovery reached a CLI")
    monkeypatch.setattr(detection.shutil, "which", forbidden)
    monkeypatch.setattr(detection.subprocess, "run", forbidden)
    assert detection.detect() == []


@pytest.mark.asyncio
async def test_hosted_dispatch_cannot_override_cli_guard(local_env, tmp_path, monkeypatch):
    from coding_agents import dispatcher
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("LLM_OFFLINE", "0")
    def forbidden(*args, **kwargs):
        pytest.fail("hosted dispatch discovered or launched a subscription CLI")
    monkeypatch.setattr(dispatcher, "detect", forbidden)
    monkeypatch.setattr(dispatcher, "require_consent", forbidden)
    monkeypatch.setattr(dispatcher, "_run_selection", forbidden)
    monkeypatch.setattr(dispatcher, "_cloud_keys_present", lambda: {})
    choices = iter([Selection("opencode", "local", "synthetic"), None])
    monkeypatch.setattr(dispatcher, "select", lambda **kwargs: next(choices))
    result = await dispatcher.dispatch_request(CodingRequest(prompt="synthetic", workdir=tmp_path),
                                               allow_server_side_byo=True)
    assert not result.ok and result.error == "no_adapter_available"


@pytest.mark.asyncio
async def test_local_dispatch_still_uses_subscription_path(local_env, tmp_path, monkeypatch):
    from coding_agents import dispatcher
    monkeypatch.setenv("LLM_OFFLINE", "0")
    monkeypatch.setattr(dispatcher, "detect", lambda: [])
    monkeypatch.setattr(dispatcher, "select", lambda **kwargs: Selection("codex", "local", "synthetic"))
    monkeypatch.setattr(dispatcher, "require_consent", lambda name: None)
    runner = AsyncMock(return_value=CodingResult(ok=True, argv=["synthetic-subscription"]))
    monkeypatch.setattr(dispatcher, "_run_selection", runner)
    result = await dispatcher.dispatch_request(CodingRequest(prompt="synthetic", workdir=tmp_path))
    assert result.ok
    runner.assert_awaited_once()


@pytest.mark.asyncio
async def test_direct_subprocess_runner_refuses_hosted_before_files(local_env, tmp_path, monkeypatch):
    from coding_agents.subprocess_runner import run_live
    monkeypatch.setenv("APP_ENV", "production")
    class ForbiddenAdapter:
        def build_argv(self, req):
            pytest.fail("hosted runner crossed launch boundary")
    result = await run_live(adapter=ForbiddenAdapter(), req=CodingRequest(prompt="synthetic", workdir=tmp_path))
    assert not result.ok and result.error == "cloud_forbids_byo"
    assert not list(tmp_path.iterdir())
