"""Assembled private Core HTTP + persisted PostgreSQL; only task/provider I/O mocked."""
import json
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.integration.test_postgres_auth_security import pg_database as pg_database
from tests.integration.test_postgres_journey_jobs import journey_db as journey_db
from tests.integration.test_postgres_build_pools import build_pool as build_pool, snapshot
from tests.integration.test_postgres_build_model_receipts import model_run as model_run, FENCE, REQUEST
from tests.security.test_core_signed_boundary import SECRET


@pytest.fixture()
def bridge(model_run, monkeypatch):
    from app.builder import intent_bridge, credentials
    from app.db.supymem_models import BuildRun
    monkeypatch.setenv("COS_INTERNAL_ASSERTION_SECRET", SECRET)
    monkeypatch.setenv("COMPANY_OS_VAULT_SECRET", "isolated-build-vault-more-than-32-bytes")
    monkeypatch.setenv("ADMIN_REQUIRE_TOTP", "1")
    monkeypatch.setenv("ADMIN_BOOTSTRAP_EMAILS", "operator@example.test")
    from app.main import create_app
    d = model_run
    arn = "arn:aws:ecs:us-east-1:000000000000:task/local-fixture/" + uuid4().hex
    with d["factory"]() as session:
        session.get(BuildRun, d["run_id"]).sandbox_ref = arn
        session.commit()
    control_token = "isolated-task-control-token-more-than-32-bytes"
    intent_bridge.install_task_binding(d["run_id"], base_url="http://10.91.0.11:8000", task_arn=arn,
                                      task_control_token=control_token, **FENCE)
    events = [json.dumps({"id": "system-event-001", "kind": "SystemPromptEvent", "parent_id": None, "source": "agent"})]
    reads = []
    def transport(request):
        reads.append(request)
        assert str(request.url) == "http://10.91.0.11:8000/v1/events"
        assert request.headers["authorization"] == "Bearer " + control_token
        assert request.headers["x-supymem-build-id"] == d["run_id"]
        assert request.headers["x-supymem-generation"] == "1"
        assert "x-build-controller" not in request.headers
        return httpx.Response(200, json={"build_run_id": d["run_id"], "generation": 1, "events": events})
    monkeypatch.setattr(intent_bridge, "_client", lambda: httpx.Client(transport=httpx.MockTransport(transport), trust_env=False, follow_redirects=False))
    token = credentials.issue_intent_credential(d["run_id"], **FENCE)
    model_token = credentials.issue_model_credential(d["run_id"], **FENCE)
    headers = {"Authorization": "Bearer " + token, "X-Supymem-Build-ID": d["run_id"], "X-Supymem-Generation": "1"}
    client = TestClient(create_app())  # no startup scheduler/lifespan
    yield {**d, "client": client, "headers": headers, "intent_token": token, "model_token": model_token,
           "control_token": control_token, "reads": reads, "events": events,
           "base": f"/internal/builds/{d['run_id']}/intent"}
    client.close()


def test_outside_controller_reads_task_and_persists_receipt_acknowledgment(bridge):
    from app.db.supymem_models import BuildRun, BuildModelCall
    d, c = bridge, bridge["client"]
    result = c.post(d["base"] + "/synchronize", headers=d["headers"], json={})
    assert result.status_code == 200, result.text
    assert result.json() == {"cursor": "system-event-001", "consumed_invocations": []}
    body = {"invocation_key": "intent-over-http", "input_event_id": "system-event-001", "request": REQUEST}
    prepared = c.post(d["base"] + "/prepare", headers=d["headers"], json=body)
    assert prepared.status_code == 200, prepared.text
    cid = prepared.json()["invocation_id"]
    assert c.post(d["base"] + "/prepare", headers=d["headers"], json=body).json()["invocation_id"] == cid
    response = c.post(f"/internal/builds/{d['run_id']}/v1/chat/completions", json=REQUEST,
        headers={"Authorization": "Bearer " + d["model_token"], "X-Supymem-Invocation-ID": cid})
    assert response.status_code == 200, response.text
    d["events"].append(json.dumps({"id": "agent-event-001", "kind": "MessageEvent", "parent_id": "system-event-001",
        "source": "agent", "llm_response_id": cid, "llm_message": {"role": "assistant", "content": []}}))
    for _ in range(2):
        synced = c.post(d["base"] + "/synchronize", headers=d["headers"], json={})
        assert synced.status_code == 200, synced.text
        assert synced.json() == {"cursor": "agent-event-001", "consumed_invocations": [cid]}
    with d["factory"]() as session:
        receipt = session.get(BuildModelCall, cid)
        assert receipt.consumed_event_id == "agent-event-001"
        assert json.loads(session.get(BuildRun, d["run_id"]).spec)["agent_cursor"] == "agent-event-001"
        assert len(session.scalars(select(BuildModelCall).where(BuildModelCall.build_run_id == d["run_id"])).all()) == 1
    assert len(d["calls"]) == 1 and snapshot(d)[1] > 0


def test_intent_model_task_credentials_and_bound_headers_cannot_cross_audiences(bridge):
    d, c = bridge, bridge["client"]
    path = d["base"] + "/synchronize"
    for wrong in (d["model_token"], d["control_token"], "founder-token"):
        assert c.post(path, headers={**d["headers"], "Authorization": "Bearer " + wrong}, json={}).status_code == 401
    for key, value in (("X-Supymem-Build-ID", "other-run"), ("X-Supymem-Generation", "2")):
        assert c.post(path, headers={**d["headers"], key: value}, json={}).status_code == 403
    assert c.post(path, headers=d["headers"], json={"events": []}).status_code == 422
    assert c.post(d["base"] + "/prepare", headers=d["headers"], json={"invocation_key": "x",
        "input_event_id": None, "request": REQUEST, "base_url": "http://attacker.test"}).status_code == 422
    assert c.post(f"/internal/builds/{d['run_id']}/v1/chat/completions", headers=d["headers"], json=REQUEST).status_code == 401
    assert not d["reads"] and not d["calls"]


@pytest.mark.parametrize("fault", ["redirect", "foreign", "invalid_event", "unsupported_event", "oversize"])
def test_untrusted_task_snapshots_fail_before_model_allocation(bridge, monkeypatch, fault):
    from app.builder import intent_bridge
    from app.db.supymem_models import BuildModelCall
    d = bridge
    def transport(request):
        if fault == "redirect":
            return httpx.Response(303, headers={"Location": "http://169.254.169.254/latest/meta-data"})
        if fault == "oversize":
            return httpx.Response(200, content=b"x" * 129, headers={"Content-Type": "application/json"})
        value = {"build_run_id": d["run_id"], "generation": 1, "events": d["events"]}
        if fault == "foreign":
            value["build_run_id"] = "other-run"
        if fault == "invalid_event":
            value["events"] = ["[]"]
        if fault == "unsupported_event":
            value["events"] = [json.dumps({"id": "unknown-event-001", "kind": "DelegateEvent", "parent_id": None})]
        return httpx.Response(200, json=value)
    if fault == "oversize":
        monkeypatch.setattr(intent_bridge, "MAX_SNAPSHOT_BYTES", 128)
    monkeypatch.setattr(intent_bridge, "_client", lambda: httpx.Client(transport=httpx.MockTransport(transport), trust_env=False, follow_redirects=False))
    result = d["client"].post(d["base"] + "/prepare", headers=d["headers"], json={
        "invocation_key": "bad-snapshot", "input_event_id": None, "request": REQUEST})
    assert result.status_code == 409, result.text
    with d["factory"]() as session:
        assert not session.scalar(select(BuildModelCall.id).where(BuildModelCall.build_run_id == d["run_id"]))
    assert not d["calls"] and snapshot(d)[1] == 0


@pytest.mark.parametrize("url", ["https://example.com", "http://127.0.0.1:8000", "http://169.254.169.254:8000",
                                "http://10.91.0.11:8000/admin", "http://user:pass@10.91.0.11:8000", "http://10.91.0.11:3300"])
def test_task_reader_rejects_unapproved_endpoint_shapes(url):
    from app.builder.intent_bridge import _endpoint
    from app.builder.model_proxy import BuildModelError
    with pytest.raises(BuildModelError, match="endpoint_invalid"):
        _endpoint(url)
