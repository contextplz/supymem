"""Migrated PostgreSQL locks, signed admission, and zero external provider I/O."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event
from dataclasses import replace
import json
import secrets

import pytest
from tests.integration.test_postgres_auth_security import pg_database as pg_database  # noqa: F401
from tests.integration.test_postgres_journey_jobs import journey_db as journey_db  # noqa: F401
from tests.integration.test_postgres_device_orders import prepare
from tests.unit.product.test_device_browser_ai import (
    configure,
    request_for,
    response,
    CLICK,
    structured_notes_for,
    v2_request,
)


@pytest.fixture
def claimed(journey_db, monkeypatch, request):
    from app.build import device_orders as svc, device_browser_ai as ai
    from app.db.models import CreditAccount

    actor_low = getattr(request, "param", None) == "actor_low"
    read_only = getattr(request, "param", None) in {"read_only", "read_only_v2"}
    profile = configure(monkeypatch, actor_low=actor_low, **({"version":"browser_cuai_text_v2","policy_version":2}
                                       if getattr(request,"param",None) in {"v2", "read_only_v2"} else {}))
    original = svc.prepare

    def general(vid, caller, manifest, **kwargs):
        value = manifest.model_dump(exclude_none=True)
        value.update(
            applications=["com.google.Chrome"],
            actions=["browser.read"] if read_only else ["browser.read", "browser.interact"],
            credit_ceiling_micro=500000,
            general_browser_verification={
                "operation": "general_goal_v1",
                "start_url": "https://example.test/",
                "execution_profile": profile.model_dump(),
                "allow_type": not read_only,
                "fill_values": {} if read_only else {"name": "Founder"},
            },
        )
        return original(vid, caller, svc.Manifest.model_validate(value), **kwargs)

    with monkeypatch.context() as local:
        local.setattr(svc, "prepare", general)
        service, caller, order = prepare(
            journey_db, actions=["browser.read", "browser.interact"]
        )
    with journey_db["factory"]() as session:
        account = (
            session.query(CreditAccount)
            .filter_by(venture_id=journey_db["vid"], scope="venture")
            .one()
        )
        account.granted_micro = 1_000_000
        session.commit()
    args = (journey_db["vid"], caller, order["id"])
    for action in ("approve", "claim"):
        service.transition(*args, order["review_digest"], action)
    sent = []
    if not actor_low:
        monkeypatch.setattr(
            ai, "_provider_call", lambda body: (sent.append(body), response(CLICK))[1]
        )
    return journey_db, service, caller, order, args, ai, sent


@pytest.mark.parametrize("claimed", ["actor_low"], indirect=True)
@pytest.mark.parametrize("unknown", [False, True])
def test_signed_actor_low_real_http_wire_and_reasoning_charge_or_permanent_hold(claimed, monkeypatch, unknown):
    import httpx
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from core.assertion import sign_assertion
    from app.api.device_orders_api import router
    from app.build import device_stop
    from app.credits.accounts import get_account
    from app.db.models import CreditLedgerEntry, CreditReservation
    from sqlalchemy import select

    d, svc, caller, order, args, ai, sent = claimed
    secret = secrets.token_hex(32)
    monkeypatch.setenv("COS_INTERNAL_ASSERTION_SECRET", secret)
    app = FastAPI()
    app.include_router(router)
    path = f"/ventures/{d['vid']}/device-work-orders/{order['id']}/browser-ai-call"
    req = request_for(order)
    expected_profile = order["manifest"]["general_browser_verification"]["execution_profile"]
    assert expected_profile["roles"]["plan"]["reasoning"] == expected_profile["roles"]["verify"]["reasoning"] == "none"
    original_client = httpx.Client

    def transport(request):
        assert request.method == "POST" and str(request.url) == ai.ENDPOINT
        body = json.loads(request.content)
        sent.append(body)
        assert body["model"] == "gpt-6.1-sol" and body["reasoning"] == {"effort": "low"}
        assert body["max_output_tokens"] == 700 and body["store"] is False
        assert body["service_tier"] == "default" and "tools" not in body and "temperature" not in body
        if unknown:
            raise httpx.ReadTimeout("synthetic lost provider outcome", request=request)
        return httpx.Response(200, json=response(json.dumps({"actions": [{"op": "done", "summary": "Source title"}],
            "memory": "", "subgoal_done": True}), model="gpt-6.1-sol",
            usage={"input_tokens": 100, "output_tokens": 700, "total_tokens": 800,
                   "output_tokens_details": {"reasoning_tokens": 690}}))

    def provider_client(**options):
        assert options["follow_redirects"] is False and options["trust_env"] is False
        return original_client(transport=httpx.MockTransport(transport), **options)

    def signed_headers():
        return {"X-COS-Assertion": sign_assertion(secret, sub=d["actor"], email="", role="user",
            path=path, method="POST", device_id=caller.device_id)}

    with TestClient(app) as client:
        monkeypatch.setattr(ai.httpx, "Client", provider_client)
        result = client.post(path, json=req.model_dump(), headers=signed_headers())
        assert result.status_code == (409 if unknown else 200), result.text
        assert client.post(path, json=req.model_dump(), headers=signed_headers()).status_code == 409
        if unknown:
            req.invocation_id = "actor_second"
            assert client.post(path, json=req.model_dump(), headers=signed_headers()).status_code == 409
    saved = svc.retrieve(*args)
    call = saved["invocations"]["actor_one"]
    assert len(sent) == 1 and len(saved["invocations"]) == 1
    assert call["profile_digest"] == ai.profile_digest(expected_profile)
    svc.transition(*args, order["review_digest"], "cancel")
    stopped = device_stop.record(*args, execution_id=saved["execution_id"], evidence_digest="b" * 64,
                                 processes_stopped=True)
    account = get_account(d["vid"])
    with d["factory"]() as session:
        hold = session.get(CreditReservation, saved["reservation_id"])
        entries = session.scalars(select(CreditLedgerEntry).where(CreditLedgerEntry.request_id == order["id"])).all()
        if unknown:
            assert call["state"] == "uncertain" and saved["inference_reconciliation_required"]
            assert hold.status == "reconciling" and account.reserved_micro == hold.amount_micro
            assert account.spent_micro == 0 and entries == [] and stopped["stop_reconciliation_required"]
        else:
            # 100 input at the conservative $2.50/M rate + all 700 generated tokens at $10/M.
            assert call["usage"]["output_tokens"] == 700 and call["provider_actual_micro"] == 7250
            assert account.spent_micro == 7250 and account.reserved_micro == 0
            assert hold.status == "settled" and len(entries) == 1 and entries[0].amount_micro == -7250
            replay = device_stop.record(*args, execution_id=saved["execution_id"], evidence_digest="b" * 64,
                                        processes_stopped=True)
            assert replay["settlement_ledger_id"] == stopped["settlement_ledger_id"]


@pytest.mark.parametrize("claimed",["v2", "read_only_v2"],indirect=True)
@pytest.mark.parametrize("action",[{"op":"extract","text":"Seats"},
    {"op":"scroll","direction":"down","amount":2},{"op":"navigate","text":"https://example.test/two"}])
def test_signed_v2_source_child_concurrent_consumption_and_unknown_hold(claimed,monkeypatch,action):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from core.assertion import sign_assertion
    from app.api.device_orders_api import router
    from app.build import device_effects as effects
    d,svc,caller,order,args,ai,sent=claimed
    secret=secrets.token_hex(32);monkeypatch.setenv("COS_INTERNAL_ASSERTION_SECRET",secret)
    monkeypatch.setattr(ai,"_provider_call",lambda body:(sent.append(body),response(json.dumps(
        {"actions":[action],"memory":"","subgoal_done":False})))[1])
    app=FastAPI();app.include_router(router)
    path=f"/ventures/{d['vid']}/device-work-orders/{order['id']}/browser-ai-call"
    req=v2_request(order)
    if action['op']=='navigate':
        req.context.links=[ai.NavigationTarget(id='c1',href=action['text'],tag='A',form=False)]
    def headers(device=caller.device_id):
        return {'X-COS-Assertion':sign_assertion(secret,sub=d['actor'],email='',role='user',
            path=path,method='POST',device_id=device)}
    with TestClient(app) as client:
        assert client.post(path,json=req.model_dump(),headers=headers('dev_foreign')).status_code==404
        assert sent==[]
        result=client.post(path,json=req.model_dump(),headers=headers())
        assert result.status_code==200,result.text
        child=result.json()['effect_proposals'][0]['invocation_id']
        def consume(_):
            return effects.admit(*args,invocation_id=child,observation_digest=req.observation.digest(),effect_digest='e'*64)
        with ThreadPoolExecutor(max_workers=4) as pool:
            admitted=list(pool.map(consume,range(4)))
        assert admitted.count(True)==1 and admitted.count(False)==3
        effects.receipt(*args,invocation_id=child,effect_digest='e'*64,outcome='uncertain')
        next_request=v2_request(order,invocation='actor_next')
        assert client.post(path,json=next_request.model_dump(),headers=headers()).status_code==409
    calls=svc.retrieve(*args)['invocations']
    assert len(sent)==1 and calls['actor_one']['actual_micro']>0
    assert calls[child]['effect']['state']=='uncertain' and 'actor_next' not in calls


@pytest.mark.parametrize("claimed", ["read_only"], indirect=True)
@pytest.mark.parametrize("action", [{"op": "click", "element_id": "c1"}, {"op": "type", "element_id": "c1", "text": "Ada"}])
def test_signed_read_only_paid_bad_input_reply_has_no_child_and_no_redial(claimed, monkeypatch, action):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from core.assertion import sign_assertion
    from app.api.device_orders_api import router
    d, svc, caller, order, args, ai, sent = claimed
    secret = secrets.token_hex(32)
    monkeypatch.setenv("COS_INTERNAL_ASSERTION_SECRET", secret)
    monkeypatch.setattr(ai, "_provider_call", lambda b: (sent.append(b), response(json.dumps(
        {"actions": [action], "memory": "", "subgoal_done": False})))[1])
    app = FastAPI()
    app.include_router(router)
    path = f"/ventures/{d['vid']}/device-work-orders/{order['id']}/browser-ai-call"
    req = request_for(order, reviewed=False)
    with TestClient(app) as client:
        for _ in range(2):
            result = client.post(path, json=req.model_dump(), headers={"X-COS-Assertion": sign_assertion(secret,
                sub=d["actor"], email="", role="user", path=path, method="POST", device_id=caller.device_id)})
            assert result.status_code == 409
    calls = svc.retrieve(*args)["invocations"]
    assert len(sent) == len(calls) == 1 and calls[req.invocation_id]["actual_micro"] > 0
    assert not any(value.get("proposal") or value.get("effect") for value in calls.values())


def test_signed_mounted_actor_child_effect_chain_and_scope_refusals(
    claimed, monkeypatch
):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from core.assertion import sign_assertion
    from app.api.device_orders_api import router
    from app.build import device_effects as effects

    d, svc, caller, order, args, ai, sent = claimed
    secret = secrets.token_hex(32)
    monkeypatch.setenv("COS_INTERNAL_ASSERTION_SECRET", secret)
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    path = f"/ventures/{d['vid']}/device-work-orders/{order['id']}/browser-ai-call"
    req = request_for(order)

    def post(actor=d["actor"], device=caller.device_id):
        assertion = sign_assertion(
            secret,
            sub=actor,
            email="",
            role="user",
            path=path,
            method="POST",
            device_id=device,
        )
        return client.post(
            path, json=req.model_dump(), headers={"X-COS-Assertion": assertion}
        )

    assert client.post(path, json=req.model_dump()).status_code == 401
    assert post(actor="usr_foreign").status_code == 404
    assert post(device="dev_other").status_code == 404
    assert post(device=None).status_code == 403
    assert sent == []
    result = post()
    assert result.status_code == 200, result.text
    child = result.json()["effect_proposals"][0]["invocation_id"]
    assert effects.admit(
        *args,
        invocation_id=child,
        observation_digest=req.observation.digest(),
        effect_digest="e" * 64,
    )
    effects.receipt(
        *args, invocation_id=child, effect_digest="e" * 64, outcome="returned"
    )
    assert post().status_code == 409 and len(sent) == 1
    assert (
        svc.retrieve(*args)["state"] == "claimed"
        and result.json()["task_verified"] is False
    )


def test_signed_structured_metadata_collision_and_content_refusal_before_cost(
    claimed, monkeypatch
):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from core.assertion import sign_assertion
    from app.api.device_orders_api import router

    d, svc, caller, order, args, ai, sent = claimed
    secret = secrets.token_hex(32)
    monkeypatch.setenv("COS_INTERNAL_ASSERTION_SECRET", secret)
    app = FastAPI()
    app.include_router(router)
    path = f"/ventures/{d['vid']}/device-work-orders/{order['id']}/browser-ai-call"
    req = request_for(order, phase="plan")
    notes = structured_notes_for(req)

    def signed_headers():
        return {
            "X-COS-Assertion": sign_assertion(
                secret,
                sub=d["actor"],
                email="",
                role="user",
                path=path,
                method="POST",
                device_id=caller.device_id,
            )
        }

    with TestClient(app) as client:
        notes["current"]["source"]["facts"] = ["4111111111111111"]
        req.context.notes = json.dumps(notes)
        denied = client.post(path, json=req.model_dump(), headers=signed_headers())
        assert denied.status_code == 409 and sent == []
        assert svc.retrieve(*args).get("invocations", {}) == {}
        notes["current"]["source"]["facts"] = ["Source title"]
        req.context.notes = json.dumps(notes)
        assert ai.loop.CARD.search(req.context.notes)
        monkeypatch.setattr(
            ai,
            "_provider_call",
            lambda body: (sent.append(body), response('{"subgoals":["Read source"]}'))[
                1
            ],
        )
        result = client.post(path, json=req.model_dump(), headers=signed_headers())
    assert result.status_code == 200 and len(sent) == 1
    assert (
        result.json()["effect_proposals"] == []
        and result.json()["task_verified"] is False
    )
    saved = svc.retrieve(*args)
    assert saved["invocations"]["actor_one"]["state"] == "completed"
    assert (
        saved["invocations"]["actor_one"]["actual_micro"] > 0
        and saved["state"] == "claimed"
    )


def test_concurrent_phases_allow_only_one_provider_until_complete(claimed, monkeypatch):
    d, svc, caller, order, args, ai, sent = claimed
    entered, release = Event(), Event()

    def provider(body):
        sent.append(body)
        entered.set()
        assert release.wait(5)
        return response('{"subgoals":["Read source"]}')

    monkeypatch.setattr(ai, "_provider_call", provider)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(
            ai.run,
            *args,
            request=request_for(order, phase="plan", invocation="plan_one"),
        )
        assert entered.wait(5)
        try:
            with pytest.raises(ValueError, match="reconciliation"):
                ai.run(
                    *args,
                    request=request_for(order, phase="verify", invocation="verify_two"),
                )
        finally:
            release.set()
        assert first.result()["effect_proposals"] == []
    assert len(sent) == 1 and len(svc.retrieve(*args)["invocations"]) == 1


@pytest.mark.parametrize("newer_state", ["calling", "completed"])
def test_new_phase_in_paid_parent_child_interval_retires_old_actor_proposal(
    claimed, monkeypatch, newer_state
):
    from app.build import device_invocations as calls

    d, svc, caller, order, args, ai, sent = claimed
    original_save = calls.save_browser_actor_proposal
    entered, release = Event(), Event()

    def interleave(*save_args, **save_kwargs):
        def provider(body):
            sent.append(body)
            entered.set()
            assert release.wait(5)
            return response('{"subgoals":["Read current source"]}')

        monkeypatch.setattr(ai, "_provider_call", provider)
        with ThreadPoolExecutor(max_workers=1) as pool:
            newer = pool.submit(
                ai.run,
                *args,
                request=request_for(order, phase="plan", invocation="plan_newer"),
            )
            assert entered.wait(5)
            try:
                if newer_state == "completed":
                    release.set()
                    assert newer.result()["effect_proposals"] == []
                return original_save(*save_args, **save_kwargs)
            finally:
                release.set()
                newer.result()

    monkeypatch.setattr(calls, "save_browser_actor_proposal", interleave)
    with pytest.raises(ValueError, match="device_effect_proposal_unavailable"):
        ai.run(*args, request=request_for(order))
    saved = svc.retrieve(*args)
    assert saved["browser_ai_latest_invocation_id"] == "plan_newer"
    assert set(saved["invocations"]) == {"actor_one", "plan_newer"}
    assert all(
        c["state"] == "completed" and c["actual_micro"] > 0
        for c in saved["invocations"].values()
    )
    assert not any(c.get("effect") for c in saved["invocations"].values())
    assert len(sent) == 2


def test_new_completed_phase_retires_already_saved_unconsumed_actor_child(
    claimed, monkeypatch
):
    from app.build import device_effects as effects

    d, svc, caller, order, args, ai, sent = claimed
    first = ai.run(*args, request=request_for(order))
    child = first["effect_proposals"][0]["invocation_id"]
    monkeypatch.setattr(
        ai,
        "_provider_call",
        lambda body: (
            sent.append(body),
            response('{"subgoals":["Read current source"]}'),
        )[1],
    )
    assert (
        ai.run(
            *args, request=request_for(order, phase="plan", invocation="plan_newer")
        )["effect_proposals"]
        == []
    )
    with pytest.raises(ValueError, match="device_effect_proposal_unavailable"):
        effects.admit(
            *args,
            invocation_id=child,
            observation_digest=request_for(order).observation.digest(),
            effect_digest="e" * 64,
        )
    saved = svc.retrieve(*args)
    assert saved["invocations"]["actor_one"]["actual_micro"] > 0
    assert "effect" not in saved["invocations"][child] and len(sent) == 2


@pytest.mark.parametrize(
    "retirement",
    [
        "cancel",
        "task",
        "account",
        "review",
        "pricing",
        "general_gate",
        "provider_gate",
        "foreign_device",
    ],
)
def test_current_admission_refuses_retirement_before_provider(
    claimed, monkeypatch, retirement
):
    from app.db.models import VentureDocument, CreditAccount
    from app.db.supymem_models import PlanTask

    d, svc, caller, order, args, ai, sent = claimed
    if retirement == "cancel":
        svc.transition(*args, order["review_digest"], "cancel")
    elif retirement == "foreign_device":
        args = (d["vid"], replace(caller, device_id="other"), order["id"])
    elif retirement == "pricing":
        monkeypatch.setattr(ai, "pricing_table", lambda: {})
    elif retirement in {"general_gate", "provider_gate"}:
        monkeypatch.delenv(
            "DEVICE_GENERAL_BROWSER_EXECUTION_ENABLED"
            if retirement == "general_gate"
            else "DEVICE_BROWSER_CUAI_EXECUTION_ENABLED"
        )
    else:
        with d["factory"]() as session:
            if retirement == "account":
                session.query(CreditAccount).filter_by(
                    venture_id=d["vid"], scope="venture"
                ).one().status = "paused"
            elif retirement == "task":
                session.get(
                    PlanTask, order["manifest"]["task_id"]
                ).title = "Changed task"
            else:
                row = session.get(VentureDocument, order["id"])
                value = json.loads(row.provenance_json)
                value["review_digest"] = "f" * 64
                row.provenance_json = json.dumps(value)
            session.commit()
    with pytest.raises((ValueError, KeyError)):
        ai.run(*args, request=request_for(order))
    assert sent == []


def test_known_usage_settles_after_cancel_but_never_issues_effect(claimed, monkeypatch):
    d, svc, caller, order, args, ai, sent = claimed

    def provider(body):
        sent.append(body)
        svc.transition(*args, order["review_digest"], "cancel")
        return response(CLICK)

    monkeypatch.setattr(ai, "_provider_call", provider)
    with pytest.raises(ValueError):
        ai.run(*args, request=request_for(order))
    saved = svc.retrieve(*args)
    assert (
        saved["state"] == "cancelled"
        and saved["invocations"]["actor_one"]["state"] == "completed"
    )
    assert len(saved["invocations"]) == 1 and len(sent) == 1


def test_unknown_provider_and_effect_blocks_new_phase_not_just_mutations(
    claimed, monkeypatch
):
    from app.build import device_effects as effects

    d, svc, caller, order, args, ai, sent = claimed
    result = ai.run(*args, request=request_for(order))
    child = result["effect_proposals"][0]["invocation_id"]
    effects.admit(
        *args,
        invocation_id=child,
        observation_digest=request_for(order).observation.digest(),
        effect_digest="e" * 64,
    )
    effects.receipt(
        *args, invocation_id=child, effect_digest="e" * 64, outcome="uncertain"
    )
    with pytest.raises(ValueError, match="reconciliation"):
        ai.run(
            *args, request=request_for(order, phase="verify", invocation="verify_later")
        )
    assert len(sent) == 1


def test_real_product_catalog_explicit_chat_prepare_full_goal_no_substitution(
    journey_db, monkeypatch
):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from core.assertion import sign_assertion
    from app.api.device_orders_api import router

    from app.build import device_browser_ai as ai

    configure(monkeypatch)
    d = journey_db
    svc, caller, old = prepare(d)
    args = (d["vid"], caller, old["id"])
    sent = []
    monkeypatch.setattr(
        ai,
        "_provider_call",
        lambda body: pytest.fail("preparation cannot call a provider"),
    )
    svc.transition(*args, old["review_digest"], "cancel")
    from app.db.supymem_models import FounderAction
    from core.ids import new_id

    with d["factory"]() as session:
        session.add(
            FounderAction(
                id=new_id("fact"),
                workspace_id=d["wid"],
                venture_id=d["vid"],
                plan_task_id=old["manifest"]["task_id"],
                title="Review current browser task",
                steps='["Review browser scope"]',
                required_secret_slots="[]",
                state="open",
                blocks_task=True,
                created_at=svc.utcnow(),
            )
        )
        session.commit()
    secret = secrets.token_hex(32)
    monkeypatch.setenv("COS_INTERNAL_ASSERTION_SECRET", secret)
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    catalog_path = f"/ventures/{d['vid']}/device-browser-ai-profile"
    prepare_path = f"/ventures/{d['vid']}/device-work-orders/from-chat"

    def headers(path, method):
        return {
            "X-COS-Assertion": sign_assertion(
                secret,
                sub=d["actor"],
                email="",
                role="user",
                path=path,
                method=method,
                device_id=caller.device_id,
            )
        }

    offered = client.get(catalog_path, headers=headers(catalog_path, "GET"))
    assert offered.status_code == 200 and offered.json()["available"] is True
    goal = (
        "Open https://example.test:443/page?q=a%20b and compare the page headings. "
        + "Preserve this requirement. " * 24
    ).rstrip()
    body = {
        "instruction": goal,
        "browser_execution_profile_digest": offered.json()["execution_profile_digest"],
    }
    wrong = client.post(
        prepare_path,
        json={**body, "browser_execution_profile_digest": "f" * 64},
        headers=headers(prepare_path, "POST"),
    )
    assert wrong.status_code == 409 and wrong.json()["detail"]["code"] == ai.UNAVAILABLE
    result = client.post(prepare_path, json=body, headers=headers(prepare_path, "POST"))
    assert result.status_code == 200, result.text
    saved = result.json()
    assert saved["manifest"]["instruction"].startswith(goal)
    assert saved["manifest"]["origins"] == ["https://example.test:443"]
    assert (
        saved["manifest"]["general_browser_verification"]["start_url"]
        == "https://example.test:443/page?q=a%20b"
    )
    assert (
        saved["manifest"]["general_browser_verification"]["execution_profile"]
        == offered.json()["execution_profile"]
    )
    assert saved["state"] == "pending" and sent == []
    svc.transition(d["vid"], caller, saved["id"], saved["review_digest"], "cancel")
    renew_path = f"/ventures/{d['vid']}/device-work-orders/{saved['id']}/renew"
    renewed = client.post(renew_path, headers=headers(renew_path, "POST"))
    assert renewed.status_code == 200, renewed.text
    assert (
        renewed.json()["manifest"]["general_browser_verification"]["execution_profile"]
        == offered.json()["execution_profile"]
    )
    assert renewed.json()["state"] == "pending" and sent == []


def test_catalog_default_off_and_legacy_readonly_constraint_refuses(
    claimed, monkeypatch
):
    from app.api.device_orders_api import _prepare_from_chat, ChatDeviceRequest
    from fastapi import HTTPException

    d, svc, caller, order, args, ai, sent = claimed
    offered = ai.available_profile(d["vid"], caller)
    assert offered["available"] is True
    with pytest.raises(HTTPException) as refused:
        _prepare_from_chat(
            d["vid"],
            caller,
            ChatDeviceRequest(
                instruction="Use my Mac for a read-only browser task: open https://example.test/ and do not change anything.",
            ),
        )
    assert refused.value.detail["code"] == "device_read_only_browser_task_unavailable"
    monkeypatch.delenv("DEVICE_GENERAL_BROWSER_EXECUTION_ENABLED")
    assert ai.available_profile(d["vid"], caller) == {
        "available": False,
        "code": ai.UNAVAILABLE,
    }
    assert sent == []


@pytest.mark.parametrize("no_plan", [False, True])
@pytest.mark.parametrize("goal", [
    "Open https://trail-notebook.example/ and report the main headline. Read only.",
    "Use my Mac for a read-only browser task: open https://trail-notebook.example/ and report the main headline.",
])
def test_signed_exact_profile_read_only_preparation_and_old_client_zero_hold(journey_db, monkeypatch, no_plan, goal):
    from pathlib import Path
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from core.assertion import sign_assertion
    from app.api.device_orders_api import router
    from app.build import device_browser_ai as ai, device_orders as svc
    from app.db.models import CreditAccount, CreditReservation, VentureDocument
    from app.db.supymem_models import FounderAction
    from app.admin.authn import CallerContext
    from app.credits.accounts import ensure_account
    from core.ids import new_id
    from sqlalchemy import select

    d = journey_db
    if no_plan:
        caller = CallerContext(d["actor"], "", "user", "/test", "POST", 9999999999, "test", device_id="dev_one")
        ensure_account(d["vid"], workspace_id=d["wid"], owner_person_id=d["actor"])
    else:
        _, caller, old = prepare(d)
        svc.transition(d["vid"], caller, old["id"], old["review_digest"], "cancel")
    configure(monkeypatch)
    active = json.loads((Path(__file__).parents[2] / "config/browser_cuai_profile.json").read_text())
    monkeypatch.setenv("DEVICE_BROWSER_CUAI_PROFILE_JSON", json.dumps(active))
    monkeypatch.setenv("OPENAI_BROWSER_CUAI_MODELS", ",".join(sorted({role["model"] for role in active["roles"].values()})))
    monkeypatch.setattr(ai, "_provider_call", lambda _: pytest.fail("Preparation or approval dispatched a provider"))
    with d["factory"]() as session:
        account = session.scalars(select(CreditAccount).where(CreditAccount.venture_id == d["vid"], CreditAccount.scope == "venture")).one()
        account.granted_micro = 1_000_000
        if not no_plan:
            session.add(FounderAction(id=new_id("fact"), workspace_id=d["wid"], venture_id=d["vid"],
                plan_task_id=old["manifest"]["task_id"], title="Review browser task", steps='["Review browser scope"]',
                required_secret_slots="[]", state="open", blocks_task=True, created_at=svc.utcnow()))
        session.commit()
    secret = secrets.token_hex(32)
    monkeypatch.setenv("COS_INTERNAL_ASSERTION_SECRET", secret)
    app = FastAPI()
    app.include_router(router)
    base = f"/ventures/{d['vid']}/device-work-orders"
    profile = ai.available_profile(d["vid"], caller)
    def post(client, path, body):
        return client.post(path, json=body, headers={"X-COS-Assertion": sign_assertion(secret,
            sub=d["actor"], email="", role="user", path=path, method="POST", device_id=caller.device_id)})
    with TestClient(app) as client:
        body = {"instruction": goal, "browser_execution_profile_digest": profile["execution_profile_digest"]}
        mixed = post(client, base + "/from-chat", {**body, "target_application": "com.apple.Notes"})
        assert mixed.status_code == 409
        result = post(client, base + "/from-chat", body)
        assert result.status_code == 200, result.text
        saved = result.json()
        assert saved["state"] == "pending" and not saved.get("reservation_id") and not saved.get("claimed_at")
        assert saved["manifest"]["actions"] == ["browser.read"]
        spec = saved["manifest"]["general_browser_verification"]
        assert spec["allow_type"] is False and spec["fill_values"] == {} and spec["execution_profile"] == active
        assert bool(saved["task_snapshot"].get("plan_id")) is not no_plan
        assert post(client, base + "/from-chat", body).json()["id"] == saved["id"]
        changed_mode = post(client, base + "/from-chat", {**body, "instruction": "Open https://trail-notebook.example/ and report the main headline."})
        assert changed_mode.status_code == 409
        review = {"review_digest": saved["review_digest"]}
        path = base + "/" + saved["id"]
        assert post(client, path + "/approve", review).status_code == 409
        for bad in [False, 1, "true"]:
            assert post(client, path + "/approve", {**review, "read_only_parser_supported": bad}).status_code == 422
        with d["factory"]() as session:
            assert session.scalars(select(CreditReservation).where(CreditReservation.request_id == saved["id"])).all() == []
            assert session.scalars(select(CreditAccount).where(CreditAccount.venture_id == d["vid"], CreditAccount.scope == "venture")).one().reserved_micro == 0
            assert session.get(VentureDocument, saved["id"]).kind == svc.KIND
        approved = post(client, path + "/approve", {**review, "read_only_parser_supported": True})
        assert approved.status_code == 200 and approved.json()["state"] == "approved", approved.text
        claimed_order = post(client, path + "/claim", review)
        assert claimed_order.status_code == 200 and claimed_order.json()["state"] == "claimed", claimed_order.text
