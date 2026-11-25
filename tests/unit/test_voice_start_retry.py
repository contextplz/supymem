from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event

import httpx
import pytest

from tests.security.test_core_signed_boundary import client, headers, make_workspace


@pytest.mark.parametrize("idea", ["Fleet maintenance scheduling for depots", "Home gardening lessons for families"])
def test_voice_start_retry_mints_and_charges_once(client, monkeypatch, idea):
    from app.api import voice_api
    from app.credits.accounts import account_for_workspace
    from app.db.models import IdempotencyRecord
    from app.state import db

    make_workspace(client, "usr_alice")
    created = client.post("/ventures", json={"name": idea, "raw_idea": idea, "start": False},
                          headers=headers("/ventures", "POST"))
    assert created.status_code == 201, created.text
    venture = created.json()
    path = f"/ventures/{venture['id']}/voice/session"
    monkeypatch.setenv("SUPYMEM_VOICE_ENABLED", "1")
    monkeypatch.setenv("OPENAI_REALTIME_API_KEY", "sk-test-realtime")
    calls = []
    entered, finish = Event(), Event()
    now = voice_api.utcnow()
    pause_mint = False

    def mint(url, **kwargs):
        calls.append(kwargs)
        if pause_mint:
            entered.set()
            assert finish.wait(20)
        return httpx.Response(200, json={"value": f"ek_test_{len(calls)}",
            "expires_at": int((now + timedelta(minutes=1)).timestamp())}, request=httpx.Request("POST", url))

    monkeypatch.setattr(voice_api.httpx, "post", mint)

    def start(key="start-one", body=None):
        payload = body or {"tool_capability": "shared_tools_v1", "screen": {"title": idea, "view": "settings"}}
        return client.post(path, json=payload | {"idempotency_key": key}, headers=headers(path, "POST"))

    before = account_for_workspace(venture["workspace_id"])
    first = start()
    assert first.status_code == 200, first.text
    for _attempt in range(2):
        retry = start(body={"screen": {"view": "settings", "title": idea}, "tool_capability": "shared_tools_v1"})
        assert retry.status_code == 200, retry.text
        assert retry.json() == first.json()
    after = account_for_workspace(venture["workspace_id"])
    charged = voice_api.session_credits(first.json()["model"]) * voice_api.MICRO_PER_CREDIT
    assert after.spent_micro - before.spent_micro == charged
    assert after.reserved_micro == before.reserved_micro
    assert len(calls) == 1
    legacy_retry = client.post(path, json={"tool_capability": "shared_tools_v1", "screen": {"title": idea, "view": "settings"}},
        headers={**headers(path, "POST"), "Idempotency-Key": "start-one"})
    assert legacy_retry.status_code == 200 and legacy_retry.json() == first.json()
    conflict = client.post(path, json={"idempotency_key": "body-key"},
        headers={**headers(path, "POST"), "Idempotency-Key": "different-header-key"})
    assert conflict.status_code == 409 and len(calls) == 1
    assert start(body={"tool_capability": "future_v2"}).status_code == 409
    with db()() as session:
        saved = session.query(IdempotencyRecord).filter(IdempotencyRecord.scope.like("voice-start:%")).all()
        assert saved and all(first.json()["client_secret"] not in row.response_json for row in saved)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pause_mint = True
        pending = pool.submit(start, "start-two")
        assert entered.wait(20)
        try:
            duplicate = start("start-two")
            assert duplicate.status_code == 409, duplicate.text
        finally:
            finish.set()
        second = pending.result(timeout=20)
    assert second.status_code == 200, second.text
    assert second.json()["voice_telemetry_id"] != first.json()["voice_telemetry_id"]
    assert start("start-two").json() == second.json()
    monkeypatch.setattr(voice_api, "utcnow", lambda: now + timedelta(minutes=2))
    assert start().status_code == 409
    assert len(calls) == 2
    assert account_for_workspace(venture["workspace_id"]).spent_micro - before.spent_micro == 2 * charged
    rejected_calls = []

    def reject(url, **kwargs):
        rejected_calls.append(kwargs)
        if len(rejected_calls) == 1:
            return httpx.Response(503, request=httpx.Request("POST", url))
        return httpx.Response(200, json={"value": "ek_recovered",
            "expires_at": int((voice_api.utcnow() + timedelta(minutes=1)).timestamp())}, request=httpx.Request("POST", url))

    monkeypatch.setattr(voice_api.httpx, "post", reject)
    assert start("rejected-start").status_code == 502
    assert account_for_workspace(venture["workspace_id"]).spent_micro == after.spent_micro + charged
    assert account_for_workspace(venture["workspace_id"]).reserved_micro == before.reserved_micro
    assert len(rejected_calls) == 1
    recovered = start("rejected-start")
    assert recovered.status_code == 200, recovered.text
    assert start("rejected-start").json() == recovered.json()
    assert len(rejected_calls) == 2
    final = account_for_workspace(venture["workspace_id"])
    assert final.spent_micro == after.spent_micro + 2 * charged
    assert final.reserved_micro == before.reserved_micro
    assert start("").status_code == 422
    assert start("key" * 100).status_code == 422
    assert client.post(path, json={"tool_capability": "shared_tools_v1"},
        headers={**headers(path, "POST", "usr_bob"), "Idempotency-Key": "start-one"}).status_code == 404
    from app.surfaces.idempotency import purge_expired

    assert purge_expired(now=now + timedelta(days=2)) >= 3
    assert start().status_code == 409
    assert start("start-two").status_code == 409
    assert start("rejected-start").status_code == 409
    assert len(rejected_calls) == 2
    assert account_for_workspace(venture["workspace_id"]).spent_micro == final.spent_micro
