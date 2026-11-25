"""Compatibility checks for the founder isolation patch, without providers."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.company_os_harness import company_os_client

from core.clock import utcnow
from tests.unit.tenancy.test_founder_isolation import founders, _stream_request


ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(autouse=True)
def credit_policy_table(founders):
    from app.credits.policy import invalidate
    from app.db.models import CreditPolicy

    CreditPolicy.__table__.create(founders.factory.kw["bind"])
    invalidate()
    yield
    invalidate()


@pytest.fixture
def workspace_only_app(founders):
    from app.admin.authn import verify_caller
    from app.api import ops_api
    from app.authz.http import bind_subject_header
    from app.authz.workspace import assert_workspace_access
    from app.tenancy import bind

    app = FastAPI()

    @app.middleware("http")
    async def workspace_only(request, call_next):
        caller = verify_caller(request)
        bind_subject_header(request, caller.sub)
        workspace = assert_workspace_access(request.headers["x-workspace-id"], caller.sub)
        with bind(workspace_id=workspace, venture_id="", actor=caller.sub):
            return await call_next(request)

    app.include_router(ops_api.router)
    with company_os_client(app) as client:
        yield client


def test_multipart_entry_upload_saves_exact_bytes(founders):
    from app.intake.evidence import venture_evidence

    data = ((founders.own.domain + " café\n") * 40_000 + "End").encode()
    assert len(data) > 1024 * 1024
    path = "/entry/evidence/files"
    response = founders.client.post(path, data={"venture_id": founders.own.venture},
                                    files={"files": ("notes.txt", data, "text/plain")},
                                    headers=founders.headers(path, "POST"))
    assert response.status_code == 200, response.text
    assert response.json()["files"][0]["status"] == "read"
    saved = venture_evidence(founders.own.venture)
    assert "".join(row["text"] for row in saved).encode() == data
    assert venture_evidence(founders.other.venture) == []


def test_ops_guard_keeps_existing_body_validation(founders, workspace_only_app, monkeypatch):
    from app.api import gtm_god_ops, ops_api, outreach_ops
    from app.content import generation
    from app.marketing import handoff

    inventory = sorted(
        f"{method} {route.path}"
        for module in (ops_api, outreach_ops, gtm_god_ops) for route in module.router.routes
        for method in route.methods & {"POST", "PUT", "PATCH", "DELETE"}
        if module is not gtm_god_ops or method != "POST"
    )
    Path("/tmp/fixtenant-review-routes.txt").write_text("\n".join(inventory) + "\n")
    monkeypatch.setattr(generation, "quote", lambda workspace, venture, data: {
        "workspace": workspace, "venture": venture, "data": data})
    monkeypatch.setattr(handoff, "create_plan_from_research", lambda **kwargs: kwargs)
    for path in ("/api/v1/content/generation/quote", "/api/v1/marketing/plan/from-research"):
        bodies = [
            {"content": b"not json", "headers": {"Content-Type": "text/plain"}},
            {"content": b""},
            {"content": b"opaque bytes", "headers": {"Content-Type": "application/octet-stream"}},
            *({"json": {"venture_id": value, "kind": "topics"}}
              for value in (None, 17, True, [founders.own.venture], {"id": founders.own.venture}, founders.own.venture)),
        ]
        for body in bodies:
            responses = []
            for client in (workspace_only_app, founders.client):
                kwargs = {**body, "headers": {**founders.headers(path, "POST"), **body.get("headers", {})}}
                response = client.post(path, **kwargs)
                responses.append((response.status_code, response.json()))
            assert responses[0] == responses[1], (path, body, responses)


def test_ops_large_multipart_upload_keeps_exact_bytes(founders, workspace_only_app, monkeypatch):
    from app.db.models import MarketingAsset
    from app.marketing.media import store

    MarketingAsset.__table__.create(founders.factory.kw["bind"])
    monkeypatch.setattr(store, "MEDIA_ROOT", founders.root / "media")
    data = b"%PDF-1.4\n" + (founders.own.domain.encode() + b"\n") * 80_000
    assert len(data) > 1024 * 1024
    path = "/api/v1/marketing/assets"
    for client in (workspace_only_app, founders.client):
        response = client.post(path, data={"kind": "other"},
                               files={"file": ("notes.pdf", data, "application/pdf")},
                               headers=founders.headers(path, "POST"))
        assert response.status_code == 200, response.text
        asset = response.json()["asset"]
        assert asset["workspace_id"] == founders.own.workspace
        assert Path(asset["path"]).read_bytes() == data


@pytest.mark.parametrize("identity", ["local", "scoped_api_key", "scoped_internal"])
def test_headerless_stream_uses_only_authenticated_or_local_identity(founders, identity):
    from app.api import activity
    from app.authz.workspace import assert_venture_access, subject_person_id
    from app.db.models import ActivityLine, Person, WorkspaceMember
    from app.tenancy import bind
    from app.workspaces.resolve import LOCAL_PERSON_ID

    assert subject_person_id(None) == LOCAL_PERSON_ID
    with founders.factory() as session:
        if identity == "local":
            if session.get(Person, LOCAL_PERSON_ID) is None:
                session.add(Person(id=LOCAL_PERSON_ID, display_name=founders.own.domain, created_at=utcnow()))
                session.flush()
            membership = session.scalar(select(WorkspaceMember).where(
                WorkspaceMember.workspace_id == founders.own.workspace,
                WorkspaceMember.person_id == LOCAL_PERSON_ID,
            ))
            if membership is None:
                session.add(WorkspaceMember(id="wsm_local", workspace_id=founders.own.workspace,
                                            person_id=LOCAL_PERSON_ID, role="viewer", created_at=utcnow()))
            else:
                membership.role = "viewer"
        session.add(ActivityLine(venture_id=founders.own.venture, workspace_id=founders.own.workspace,
                                 phase="research", kind="note", status="done", text=founders.own.domain, ts=utcnow()))
        session.commit()
    request = _stream_request(founders.own)
    request.scope["headers"] = []

    async def read():
        response = await activity.stream_activity(founders.own.venture, request)
        iterator = response.body_iterator
        try:
            assert founders.own.domain in await anext(iterator)
        finally:
            await iterator.aclose()
        assert activity._STREAM_COUNTS["anon"] == 0
        response = await activity.stream_activity(founders.other.venture, request)
        events = [event async for event in response.body_iterator]
        assert len(events) == 1 and "Not found." in events[0]
        assert founders.other.domain not in events[0]
        assert activity._STREAM_COUNTS["anon"] == 0

    if identity == "local":
        assert_venture_access(founders.own.venture, None)
        asyncio.run(read())
    else:
        with bind(workspace_id=founders.own.workspace, venture_id=founders.own.venture, actor=founders.own.person):
            asyncio.run(read())


@pytest.mark.parametrize("failure", ["missing", "blocked", "forbidden", "server", "database", "serialization", "disconnect", "sleep", "cancelled"])
def test_stream_failure_ends_cleanly_without_private_details(founders, monkeypatch, failure):
    from app.api import activity
    from fastapi import HTTPException

    private = "Private provider failure: " + founders.other.domain
    request = _stream_request(founders.own)

    def snapshot(*args):
        if failure in {"missing", "blocked"}:
            raise HTTPException(404 if failure == "missing" else 423, private)
        if failure == "forbidden":
            raise HTTPException(403, private)
        if failure == "server":
            raise HTTPException(500, private)
        if failure == "database":
            raise RuntimeError(private)
        if failure == "cancelled":
            raise asyncio.CancelledError()
        return None, [{"seq": 1, "text": founders.own.domain,
                       **({"invalid": object()} if failure == "serialization" else {})}]

    async def fail(*args):
        raise OSError(private)

    monkeypatch.setattr(activity, "_stream_snapshot", snapshot)
    if failure == "disconnect":
        monkeypatch.setattr(request, "is_disconnected", fail)
    if failure == "sleep":
        monkeypatch.setattr(activity.asyncio, "sleep", fail)

    async def read():
        response = await activity.stream_activity(founders.own.venture, request)
        if failure == "cancelled":
            with pytest.raises(asyncio.CancelledError):
                await anext(response.body_iterator)
            assert activity._STREAM_COUNTS[founders.own.person] == 0
            return
        events = [event async for event in response.body_iterator]
        assert len(events) <= 3
        assert "event: unavailable" in events[-1]
        assert private not in "".join(events)
        assert activity._STREAM_COUNTS[founders.own.person] == 0

    asyncio.run(read())


@pytest.mark.hosted_only
def test_mac_01108_bearer_stream_reaches_verified_core(founders, monkeypatch):
    from app.api import activity
    from app.db.models import ActivityLine

    source = (ROOT / "src-tauri/src/events.rs").read_text()
    assert ".bearer_auth(&identity.access_token)" in source
    assert '.header("Last-Event-ID", last)' in source
    with founders.factory() as session:
        session.add(ActivityLine(venture_id=founders.own.venture, workspace_id=founders.own.workspace,
                                 phase="research", kind="note", status="done", text=founders.own.domain, ts=utcnow()))
        session.commit()
    for name, attributes in {
        "app.audit_tap": {"audit_tap": None}, "app.session": {"Session": SimpleNamespace},
        "app.vault": {"vault": SimpleNamespace(secret_values=lambda *args, **kwargs: [])},
    }.items():
        module = ModuleType(name)
        module.__dict__.update(attributes)
        monkeypatch.setitem(sys.modules, name, module)
    spec = importlib.util.spec_from_file_location("review_gateway_proxy", ROOT / "services/gateway/app/proxy.py")
    proxy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(proxy)
    proxy.settings = SimpleNamespace(company_os_url="http://core.test")
    ticks = []
    original_snapshot = activity._stream_snapshot

    def snapshot(*args):
        result = original_snapshot(*args)
        ticks.append(1)
        return result

    async def disconnected(request):
        return request.app is founders.client.app and bool(ticks)

    monkeypatch.setattr(activity, "_stream_snapshot", snapshot)
    monkeypatch.setattr(Request, "is_disconnected", disconnected)
    gateway = FastAPI()
    forwarded = []

    async def backend(request):
        forwarded.append(request)
        return await httpx.ASGITransport(founders.client.app).handle_async_request(request)

    @gateway.get("/api/{path:path}")
    async def forward(path: str, request: Request):
        assert "x-subject" not in request.headers
        session = SimpleNamespace(subject=founders.own.person, email="", device_id="dev_legacy")
        async with httpx.AsyncClient(transport=httpx.MockTransport(backend)) as client:
            monkeypatch.setattr(proxy, "stream_client", lambda: client)
            return await proxy.forward(path, request, session=session)

    with TestClient(gateway) as client:
        response = client.get(f"/api/ventures/{founders.own.venture}/activity/stream", headers={
            "Authorization": "Bearer legacy-desktop-session", "User-Agent": "SupymemDesktop/0.1.108",
            "Accept": "text/event-stream", "Last-Event-ID": "0",
        })
    assert response.status_code == 200, response.text
    assert founders.own.domain in response.text and "event: activity" in response.text
    assert forwarded[0].headers["x-subject"] == founders.own.person
    assert forwarded[0].headers["x-cos-assertion"]


@pytest.mark.hosted_only
def test_gmail_callers_keep_header_query_and_local_workspace_selection(founders, monkeypatch):
    from app.content.oauth import callbacks
    from app.db.models import Workspace
    from app.gtm.supymem import composio_gmail
    from app.workspaces.resolve import LOCAL_PERSON_ID

    caller_files = [path for directory in ("apps/console/src", "src-tauri", "packages")
                    for path in (ROOT / directory).rglob("*")
                    if path.is_file() and path.suffix in {".ts", ".tsx", ".rs", ".py"}
                    and any(endpoint in path.read_text() for endpoint in ("/ai-gtm/gmail/connect", "/marketing/lab/mail/connect"))]
    assert caller_files == [ROOT / "apps/console/src/lib/opsApi.ts"]
    caller = caller_files[0].read_text()
    assert 'url.searchParams.set("workspace_id", workspaceId)' in caller
    assert 'headers.set("X-Workspace-ID", workspaceId)' in caller
    calls = []

    def connect(method, path, *, json):
        calls.append(json["user_id"])
        return {"redirect_url": "https://oauth.example.test/connect", "connected_account_id": "ca_review"}

    monkeypatch.setattr(composio_gmail, "ensure_auth_config", lambda: "ac_review")
    monkeypatch.setattr(composio_gmail, "_request", connect)
    monkeypatch.setattr(callbacks, "start_managed", lambda *, workspace_id, **kwargs: {
        "app_user_id": composio_gmail.user_id_for(workspace_id)})
    for path in ("/api/v1/marketing/lab/mail/connect", "/api/v1/ai-gtm/gmail/connect"):
        for selector in ("header", "query"):
            headers = founders.headers(path, "POST")
            params = {}
            if selector == "query":
                headers.pop("X-Workspace-Id")
                params = {"workspace_id": founders.own.workspace}
            response = founders.client.post(path, json={}, params=params, headers=headers)
            assert response.status_code == 200, response.text
            assert response.json()["app_user_id"] == "gmail:" + founders.own.workspace
        monkeypatch.setenv("COMPANY_OS_REQUIRE_SIGNED_CALLER", "0")
        response = founders.client.post(path, json={})
        assert response.status_code == 200, response.text
        workspace_id = response.json()["app_user_id"].removeprefix("gmail:")
        with founders.factory() as session:
            assert session.get(Workspace, workspace_id).owner_id == LOCAL_PERSON_ID
        monkeypatch.setenv("COMPANY_OS_REQUIRE_SIGNED_CALLER", "1")
    assert "gmail:" + founders.other.workspace not in calls


@pytest.mark.parametrize("effect", ["connect", "send"])
@pytest.mark.parametrize("unwritable", ["directory", "file"])
def test_lab_log_write_failure_preserves_business_result(founders, monkeypatch, effect, unwritable):
    from app.gtm.supymem import pipeline
    from app.marketing import lab

    monkeypatch.delenv("MARKETING_LAB_LOG")
    path = lab.lab_log_path(founders.own.workspace)
    assert path.is_relative_to(founders.root)
    path.parent.mkdir(parents=True)
    target = path.parent if unwritable == "directory" else path
    if unwritable == "file":
        path.touch()
    target.chmod(0o555 if unwritable == "directory" else 0o444)
    outcome = {"ok": True, "subject": founders.own.domain, "provider_message_id": "msg_review"}
    calls = []

    def provider(workspace_id, **kwargs):
        calls.append((workspace_id, kwargs))
        return outcome

    monkeypatch.setattr(pipeline, "start_gmail_connect", provider)
    monkeypatch.setattr(pipeline, "send_test_email", provider)
    try:
        with pytest.raises(PermissionError):
            with path.open("a"):
                pass
        if effect == "connect":
            result = lab.start_mail_connect(founders.own.workspace)
        else:
            result = lab.send_lab_test_email(workspace_id=founders.own.workspace, to="owner@example.test",
                                             subject=founders.own.domain, body=founders.own.domain)
        assert result is outcome
        assert len(calls) == 1 and calls[0][0] == founders.own.workspace
    finally:
        target.chmod(0o755 if unwritable == "directory" else 0o644)


def test_entrypoint_preserves_writable_data_root(founders, monkeypatch):
    from app.marketing import lab

    entrypoint = ROOT / "docker/entrypoint.sh"
    source = entrypoint.read_text()
    assert 'DATA_DIR="${COMPANY_OS_DATA_DIR:-/workspace/data}"' in source
    assert 'chown -R appuser:appuser "$DATA_DIR"' in source
    assert 'exec gosu appuser "$0" "$@"' in source
    assert "COMPANY_OS_DATA_DIR=/workspace/data" in (ROOT / "services/company_os/Dockerfile").read_text()
    assert os.getuid() != 0
    environment = os.environ.copy()
    environment["PATH"] = str(Path(sys.executable).parent) + os.pathsep + environment.get("PATH", "")
    subprocess.run(["sh", str(entrypoint), "sh", "-c", 'test -w "$COMPANY_OS_DATA_DIR" && touch "$COMPANY_OS_DATA_DIR/entrypoint-probe"'],
                   env=environment, check=True)
    monkeypatch.delenv("MARKETING_LAB_LOG")
    lab.append_lab_log({"action": founders.own.domain}, workspace_id=founders.own.workspace)
    assert lab.read_lab_log(workspace_id=founders.own.workspace)[0]["action"] == founders.own.domain
