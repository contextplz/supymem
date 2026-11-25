"""Offline, two-workspace attacks through the signed founder boundary."""

from __future__ import annotations

import asyncio
import ast
import inspect
import json
import os
import threading
from contextvars import ContextVar
from types import SimpleNamespace
from typing import get_args
from uuid import uuid4

import pytest
from fastapi import HTTPException
from tests.company_os_harness import company_os_test_client as TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.schema import CreateSchema, DropSchema
from starlette.requests import Request

from core.assertion import sign_assertion
from core.clock import utcnow
from tests.company_os_harness import load_company_os_app


SECRET = "tenant-isolation-test-secret-at-least-32-bytes"
DOMAINS = ("Warehouse inventory for distributors", "Meal planning for home cooks")


@pytest.fixture(params=DOMAINS)
def founders(request, tmp_path, monkeypatch):
    for name, value in {
        "APP_ENV": "local",
        "COMPANY_OS_REQUIRE_SIGNED_CALLER": "1",
        "COS_INTERNAL_ASSERTION_SECRET": SECRET,
        "COMPANY_OS_VAULT_SECRET": "tenant-test-vault-secret-at-least-32-bytes",
        "ADMIN_REQUIRE_TOTP": "1",
        "ADMIN_BOOTSTRAP_EMAILS": "security@example.test",
        "COMPANY_OS_DATA_DIR": str(tmp_path),
        "COMPANY_OS_WORKING_MEMORY_PATH": str(tmp_path / "memory.db"),
        "COMPANY_OS_DB_URL": f"sqlite:///{tmp_path}/founders.db",
        "MARKETING_LAB_LOG": str(tmp_path / "lab_log.jsonl"),
    }.items():
        monkeypatch.setenv(name, value)
    main = load_company_os_app()
    from app.core.local_mode import is_local
    from app.workspaces.resolve import LOCAL_PERSON_ID
    from app import state
    from app.db.models import (
        ActivityLine, Base, ContentBrief, MarketingJob, MarketingPlan,
        OnboardingRun, Person, Venture, VentureDocument, Workspace, WorkspaceMember,
    )

    url = os.environ.get("TEST_POSTGRES_URL")
    schema = "tenant_test_" + uuid4().hex
    engine = create_engine(url or f"sqlite:///{tmp_path}/founders.db")
    if url:
        with engine.begin() as connection:
            connection.execute(CreateSchema(schema))
        scoped_engine = engine.execution_options(schema_translate_map={None: schema})
    else:
        scoped_engine = engine
    tables = [model.__table__ for model in (
        Person, Workspace, WorkspaceMember, Venture, ActivityLine, OnboardingRun,
        ContentBrief, MarketingPlan, MarketingJob, VentureDocument,
    )]
    Base.metadata.create_all(scoped_engine, tables=tables)
    factory = sessionmaker(scoped_engine, expire_on_commit=False)
    state.set_factory(factory)
    now = utcnow()
    own = SimpleNamespace(person=LOCAL_PERSON_ID if is_local() else "per_a", workspace="wsp_a", venture="ven_a", domain=request.param)
    other = SimpleNamespace(person="per_b", workspace="wsp_b", venture="ven_b",
                            domain=next(domain for domain in DOMAINS if domain != request.param))
    with factory() as session:
        for founder in (own, other):
            session.add(Person(id=founder.person, display_name=founder.domain, created_at=now))
        session.flush()
        for founder in (own, other):
            session.add(Workspace(id=founder.workspace, owner_id=founder.person, kind="company",
                                  name=founder.domain, entry_mode="idea", created_at=now, updated_at=now))
        session.flush()
        for founder in (own, other):
            session.add(Venture(id=founder.venture, workspace_id=founder.workspace, name=founder.domain,
                                raw_idea="Private research: " + founder.domain, stage="draft",
                                created_at=now, updated_at=now))
            session.add(WorkspaceMember(id="wsm_" + founder.person, workspace_id=founder.workspace,
                                        person_id=founder.person, role="owner", created_at=now))
        session.commit()
    client = TestClient(main.create_app())
    if is_local():
        client.headers["Origin"] = str(client.base_url).rstrip("/")

    def headers(path, method="GET", founder=own, workspace=None):
        return {
            "X-COS-Assertion": sign_assertion(SECRET, sub=founder.person, email="", role="user",
                                              path=path, method=method),
            "X-Workspace-Id": workspace or founder.workspace,
        }

    try:
        yield SimpleNamespace(client=client, factory=factory, engine=engine, own=own, other=other,
                              headers=headers, root=tmp_path)
    finally:
        client.close()
        state.reset_db_factory()
        if url:
            with engine.begin() as connection:
                connection.execute(DropSchema(schema, cascade=True))
        engine.dispose()


NOT_FOUND = {"detail": {"error": {"code": "not_found", "message": "Not found."}}}


def test_research_handoff_rejects_foreign_and_missing_before_copy(founders, monkeypatch):
    from app.content import brief
    from app.marketing import handoff
    from app.state import MEM
    from app.tenancy import bind

    MEM[f"brief:{founders.other.venture}"] = {"research": {"summary": founders.other.domain}}
    sidecar = founders.root / "ventures" / founders.other.venture / "research"
    sidecar.mkdir(parents=True)
    (sidecar / "recon.json").write_text(json.dumps({"summary": founders.other.domain}))
    monkeypatch.setattr(handoff, "create_plan_from_brief", lambda **kwargs: {"ok": True})
    path = "/api/v1/marketing/plan/from-research"
    for identifier in (founders.other.venture, "ven_missing"):
        response = founders.client.post(path, json={"venture_id": identifier}, headers=founders.headers(path, "POST"))
        assert response.status_code == 404, response.text
        assert response.json() == NOT_FOUND
        assert brief.active(founders.own.workspace) is None
        with bind(workspace_id=founders.own.workspace, venture_id="", actor=founders.own.person):
            with pytest.raises(HTTPException) as denied:
                handoff.create_plan_from_research(workspace_id=founders.own.workspace, venture_id=identifier)
            assert denied.value.status_code == 404
        assert brief.active(founders.own.workspace) is None
    response = founders.client.post(path, json={"venture_id": founders.own.venture}, headers=founders.headers(path, "POST"))
    assert response.status_code == 200, response.text
    assert response.json()["brief"]["product"]["name"] == founders.own.domain


@pytest.mark.hosted_only
def test_gmail_connect_uses_authorized_workspace_not_client_user(founders, monkeypatch):
    from app.gtm.supymem import composio_gmail
    from app.marketing import lab

    calls = []

    def connect(method, path, *, json):
        assert (method, path) == ("POST", "/connected_accounts/link")
        calls.append(json["user_id"])
        return {"redirect_url": "https://oauth.example.test/connect", "connected_account_id": "ca_scoped"}

    monkeypatch.setattr(composio_gmail, "ensure_auth_config", lambda: "ac_scoped")
    monkeypatch.setattr(composio_gmail, "_request", connect)
    path = "/api/v1/marketing/lab/mail/connect"
    response = founders.client.post(path, json={"user_id": founders.other.workspace},
                                    headers=founders.headers(path, "POST"))
    assert response.status_code == 200, response.text
    assert calls == ["gmail:" + founders.own.workspace]
    assert response.json()["app_user_id"] == "gmail:" + founders.own.workspace
    entries = lab.read_lab_log(workspace_id=founders.own.workspace)
    assert entries[-1]["workspace_id"] == founders.own.workspace


@pytest.mark.hosted_only
def test_lab_log_read_clear_and_attribution_are_workspace_scoped(founders):
    path = "/api/v1/marketing/lab/log"
    for founder in (founders.own, founders.other):
        response = founders.client.post(path, json={"action": founder.domain, "request": {"subject": founder.domain}},
                                        headers=founders.headers(path, "POST", founder))
        assert response.status_code == 200, response.text
    reads = []
    for founder in (founders.own, founders.other):
        response = founders.client.get(path, headers=founders.headers(path, founder=founder))
        assert response.status_code == 200, response.text
        reads.append((founder.domain, [entry["action"] for entry in response.json()["entries"]]))
    forged = founders.client.post(path, json={"workspace_id": founders.other.workspace, "action": "client entry"},
                                  headers=founders.headers(path, "POST"))
    assert forged.status_code == 200, forged.text
    cleared = founders.client.post(path, json={"clear": True}, headers=founders.headers(path, "POST"))
    assert cleared.status_code == 200, cleared.text
    assert founders.client.get(path, headers=founders.headers(path)).json()["entries"] == []
    remaining = founders.client.get(path, headers=founders.headers(path, founder=founders.other)).json()["entries"]
    assert reads == [(founder.domain, [founder.domain]) for founder in (founders.own, founders.other)]
    assert forged.json()["entry"]["workspace_id"] == founders.own.workspace
    assert [entry["action"] for entry in remaining] == [founders.other.domain]


def test_every_ops_venture_selector_is_guarded_before_handler(founders, monkeypatch):
    from app.api import gtm_god_ops, ops_api, outreach_ops
    from app.authz import http
    from app.db.models import WorkspaceMember

    original_access = http.assert_venture_access
    venture_checks = []

    def check_venture(venture_id, subject):
        venture_checks.append(venture_id)
        return original_access(venture_id, subject)

    monkeypatch.setattr(http, "assert_venture_access", check_venture)
    with founders.factory() as session:
        session.add(WorkspaceMember(id="wsm_cross", workspace_id=founders.other.workspace,
                                    person_id=founders.own.person, role="contributor", created_at=utcnow()))
        session.commit()
    checked = []
    for module in (ops_api, outreach_ops, gtm_god_ops):
        for route in module.router.routes:
            endpoint = route.endpoint
            tree = ast.parse(inspect.getsource(endpoint))
            body_models = [model for parameter in route.dependant.body_params
                           for model in (parameter.field_info.annotation, *get_args(parameter.field_info.annotation))]
            query_selector = any(parameter.name == "venture_id" for parameter in route.dependant.query_params)
            path_selector = any(parameter.name == "venture_id" for parameter in route.dependant.path_params)
            body_selector = any("venture_id" in getattr(model, "model_fields", {}) for model in body_models)
            body_selector = body_selector or bool(body_models and any(
                isinstance(node, ast.Constant) and node.value == "venture_id" for node in ast.walk(tree)))
            if not (query_selector or path_selector or body_selector):
                continue
            for method in sorted(route.methods & {"GET", "POST", "PUT", "PATCH", "DELETE"}):
                for identifier in (founders.other.venture, "ven_missing"):
                    path = route.path
                    for parameter in route.dependant.path_params:
                        path = path.replace("{" + parameter.name + "}", identifier if parameter.name == "venture_id" else "missing")
                    kwargs = {"headers": founders.headers(path, method)}
                    if query_selector:
                        kwargs["params"] = {"venture_id": identifier}
                    if body_selector:
                        kwargs["json"] = {"venture_id": identifier}
                    previous_checks = len(venture_checks)
                    response = founders.client.request(method, path, **kwargs)
                    assert response.status_code == 404, (method, path, response.text)
                    assert response.json() == NOT_FOUND, (method, path, response.text)
                    assert identifier in venture_checks[previous_checks:], (method, path)
                checked.append((method, route.path))
    assert len(checked) >= 20, checked


def test_opengtm_foreign_state_is_not_reproduced_without_cli(founders, monkeypatch):
    from app.gtm import opengtm_bridge

    if opengtm_bridge.OPENGTM_BIN.is_file():
        pytest.skip("This offline probe only covers the missing CLI prerequisite")
    home = founders.root / "opengtm"
    home.mkdir()
    filename = founders.other.workspace + ".json"
    private_state = json.dumps({"leads": [{"subject": founders.other.domain}]})
    (home / filename).write_text(private_state)
    monkeypatch.setattr(opengtm_bridge, "OPENGTM_HOME", home)
    for suffix in ("status", "queue"):
        path = "/api/v1/ai-gtm/opengtm/outreach/" + suffix
        for founder in (founders.other, founders.own):
            response = founders.client.get(path, params={"state_file": filename},
                                           headers=founders.headers(path, founder=founder))
            assert response.status_code >= 400, response.text
            assert founders.other.domain not in response.text
    assert (home / filename).read_text() == private_state


@pytest.mark.hosted_only
def test_viewer_cannot_upload_multipart_venture_evidence(founders):
    from app.db.models import VentureDocument, WorkspaceMember

    with founders.factory() as session:
        session.add(WorkspaceMember(id="wsm_viewer", workspace_id=founders.other.workspace,
                                    person_id=founders.own.person, role="viewer", created_at=utcnow()))
        session.commit()
    path = "/entry/evidence/files"
    response = founders.client.post(path, data={"venture_id": founders.other.venture},
                                    files={"files": ("notes.txt", founders.own.domain.encode(), "text/plain")},
                                    headers=founders.headers(path, "POST"))
    assert response.status_code == 403, response.text
    assert response.json()["detail"]["code"] == "read_only_workspace"
    with founders.factory() as session:
        assert session.scalars(select(VentureDocument)).all() == []
    allowed = founders.client.post(path, data={"venture_id": founders.other.venture},
                                   files={"files": ("notes.txt", founders.other.domain.encode(), "text/plain")},
                                   headers=founders.headers(path, "POST", founders.other))
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["files"][0]["status"] == "read"


def _stream_request(founder):
    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    return Request({"type": "http", "method": "GET", "path": "/stream", "query_string": b"",
                    "headers": [(b"x-subject", founder.person.encode())]}, receive=receive)


def test_removed_collaborator_stops_receiving_activity(founders, monkeypatch):
    from app.api import activity
    from app.db.models import ActivityLine, WorkspaceMember

    with founders.factory() as session:
        session.add(WorkspaceMember(id="wsm_live", workspace_id=founders.other.workspace,
                                    person_id=founders.own.person, role="viewer", created_at=utcnow()))
        session.add(ActivityLine(venture_id=founders.other.venture, workspace_id=founders.other.workspace,
                                 phase="research", kind="note", status="done", text=founders.other.domain, ts=utcnow()))
        session.commit()
    path = f"/ventures/{founders.other.venture}/activity"
    assert founders.client.get(path, headers=founders.headers(path)).status_code == 200
    original_sleep = asyncio.sleep

    async def next_tick(delay):
        await original_sleep(0)

    monkeypatch.setattr(activity.asyncio, "sleep", next_tick)

    async def attack():
        response = await activity.stream_activity(founders.other.venture, _stream_request(founders.own))
        iterator = response.body_iterator
        try:
            assert founders.other.domain in await anext(iterator)
            assert await anext(iterator) == ": heartbeat\n\n"
            with founders.factory() as session:
                session.delete(session.get(WorkspaceMember, "wsm_live"))
                session.add(ActivityLine(venture_id=founders.other.venture, workspace_id=founders.other.workspace,
                                         phase="research", kind="note", status="done",
                                         text="Private update: " + founders.other.domain, ts=utcnow()))
                session.commit()
            remaining = []
            for _ in range(3):
                try:
                    remaining.append(await anext(iterator))
                except StopAsyncIteration:
                    break
            else:
                pytest.fail("Removed collaborator still has a live activity stream")
            assert "Private update" not in "".join(remaining)
        finally:
            await iterator.aclose()
        assert activity._STREAM_COUNTS[founders.own.person] == 0

    asyncio.run(attack())
    denied = founders.client.get(path, headers=founders.headers(path))
    assert denied.status_code == 404 and denied.json() == NOT_FOUND


def test_activity_poll_sql_is_off_loop_and_sessions_close_before_yield(founders):
    from app.api import activity
    from app.db.models import ActivityLine

    with founders.factory() as session:
        session.add(ActivityLine(venture_id=founders.own.venture, workspace_id=founders.own.workspace,
                                 phase="research", kind="note", status="done", text=founders.own.domain, ts=utcnow()))
        session.commit()
    marker = ContextVar("activity_scope_test", default=None)
    loop_thread = threading.get_ident()
    statements = []
    checkouts = []

    def sql(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement)
        assert threading.get_ident() != loop_thread, "Activity SQL ran on the event loop"
        assert marker.get() == founders.own.domain

    def checkout(connection, record, proxy):
        checkouts.append(connection)

    def checkin(connection, record):
        checkouts.remove(connection)

    event.listen(founders.engine, "before_cursor_execute", sql)
    event.listen(founders.engine, "checkout", checkout)
    event.listen(founders.engine, "checkin", checkin)

    async def poll():
        marker.set(founders.own.domain)
        response = await activity.stream_activity(founders.own.venture, _stream_request(founders.own))
        iterator = response.body_iterator
        try:
            assert founders.own.domain in await anext(iterator)
            assert statements
            assert checkouts == []
            assert await anext(iterator) == ": heartbeat\n\n"
            assert checkouts == []
        finally:
            await iterator.aclose()
        assert activity._STREAM_COUNTS[founders.own.person] == 0

    try:
        asyncio.run(poll())
    finally:
        event.remove(founders.engine, "before_cursor_execute", sql)
        event.remove(founders.engine, "checkout", checkout)
        event.remove(founders.engine, "checkin", checkin)
