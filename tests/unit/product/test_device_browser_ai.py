"""Actual durable phase/effect controller tests; provider is always a local stub."""

import json
import copy
from pathlib import Path
from dataclasses import replace

import pytest
import yaml
from pydantic import ValidationError
from tests.unit.product.test_device_orders import orders  # noqa: F401


def configure(monkeypatch, *, actor_low=False, **limits):
    from app.build import device_browser_ai as ai, device_orders as svc
    monkeypatch.delenv("DEVICE_BROWSER_CUAI_PROVIDER", raising=False)

    table = yaml.safe_load(
        (Path(__file__).parents[3] / "config/llm_pricing.yaml").read_text()
    )
    monkeypatch.setattr(ai, "pricing_table", lambda: table["models"])
    monkeypatch.setattr(ai, "load_pricing", lambda: table)
    for name, value in {
        "DEVICE_GENERAL_BROWSER_EXECUTION_ENABLED": "1",
        "DEVICE_BROWSER_EXECUTION_ENABLED": "1",
        "DEVICE_BROWSER_CUAI_EXECUTION_ENABLED": "1",
        "OPENAI_BROWSER_CUAI_MODELS": "gpt-6-sol,gpt-6.1-sol" if actor_low else "gpt-6-sol",
        "OPENAI_DEVICE_VISION_API_KEY": "synthetic-test-not-a-credential",
    }.items():
        monkeypatch.setenv(name, value)
    role = {
        "provider": "openai",
        "model": "gpt-6-sol",
        "service_tier": "default",
        "reasoning": "none",
        "max_output_tokens": 700,
    }
    roles = {name: dict(role) for name in ("plan", "actor", "verify")}
    if actor_low:
        roles["actor"].update(model="gpt-6.1-sol", reasoning="low")
    profile = svc.BrowserAIProfile(
        version="browser_cuai_text_v1",
        backend="cuai",
        policy_version=1,
        roles=roles,
        pricing_digest="0" * 64,
        max_provider_calls=8,
        max_request_bytes=65536,
        completion_policy="browser_semantic_review_v1",
    )
    profile.pricing_digest = ai.pricing_digest(profile)
    profile = profile.model_copy(update=limits)
    monkeypatch.setenv(
        "DEVICE_BROWSER_CUAI_PROFILE_JSON", json.dumps(profile.model_dump())
    )
    return profile


@pytest.fixture
def v2_order(orders, monkeypatch, request):  # noqa: F811
    from app.build import device_browser_ai as ai
    from app.db.models import CreditAccount
    svc, caller, manifest = orders
    read_only = getattr(request, "param", None) == "read_only"
    profile = configure(monkeypatch, version="browser_cuai_text_v2", policy_version=2)
    with svc.db()() as session:
        session.get(CreditAccount, "cra_device_test").granted_micro = 1_000_000
        session.commit()
    value = manifest.model_dump(exclude_none=True)
    value.update(applications=["com.google.Chrome"], actions=["browser.read"] if read_only else ["browser.read", "browser.interact"],
                 credit_ceiling_micro=500000, general_browser_verification={
                     "operation":"general_goal_v1", "start_url":"https://example.test/", "max_steps":8, "allow_type": not read_only,
                     "execution_profile":profile.model_dump()})
    saved = svc.prepare("v_one", caller, svc.Manifest.model_validate(value))
    for action in ("approve", "claim"):
        svc.transition("v_one", caller, saved["id"], saved["review_digest"], action)
    return svc, caller, saved, ai


def v2_request(order, **kwargs):
    request = request_for(order, **kwargs)
    notes = structured_notes_for(request)
    notes["schema_version"] = 2
    notes["prior_pages"], notes["input_history"] = [], []
    notes["current"]["source"].update(reads=[], reads_omitted=0)
    request.context.notes = json.dumps(notes)
    return request


@pytest.mark.parametrize("v2_order", ["interact", "read_only"], indirect=True)
@pytest.mark.parametrize("action", [{"op":"extract","text":"Seats"},
    {"op":"scroll","direction":"down","amount":2}, {"op":"navigate","text":"https://example.test/two"}])
def test_v2_actual_actor_child_is_one_use_and_unknown_effect_blocks_more_paid_calls(v2_order, monkeypatch, action):
    from app.build import device_effects as effects
    from coding_agents.owned_browser import cuai_document_target
    from coding_agents.cu_ai_loop import parse_action
    svc, caller, order, ai = v2_order
    args = ("v_one", caller, order["id"])
    sent = []
    monkeypatch.setattr(ai, "_provider_call", lambda body: (sent.append(body), response(json.dumps(
        {"actions":[action],"memory":"","subgoal_done":False})))[1])
    request = v2_request(order)
    if action["op"] == "navigate":
        request.context.links = [ai.NavigationTarget(id="c1", href=action["text"], tag="A", form=False)]
    result = ai.run(*args, request=request)
    proposal = result["effect_proposals"][0]
    assert proposal["candidate"]["operation"] == action["op"]
    if action["op"] != "navigate":
        assert proposal["candidate"]["target"] == cuai_document_target(request.current_load.model_dump(), parse_action(action))
    else:
        assert proposal["candidate"]["target"] == "c1"
    assert effects.admit(*args, invocation_id=proposal["invocation_id"],
        observation_digest=request.observation.digest(), effect_digest="e"*64) is True
    assert effects.admit(*args, invocation_id=proposal["invocation_id"],
        observation_digest=request.observation.digest(), effect_digest="e"*64) is False
    effects.receipt(*args, invocation_id=proposal["invocation_id"], effect_digest="e"*64, outcome="uncertain")
    with pytest.raises(ValueError, match="reconciliation"):
        ai.run(*args, request=v2_request(order, invocation="actor_two"))
    assert len(sent) == 1
    record = svc.retrieve(*args)["invocations"]["actor_one"]
    assert record["actual_micro"] > 0 and record["state"] == "completed"


@pytest.mark.parametrize("action", [{"op":"scroll","direction":"down","amount":True},
    {"op":"scroll","direction":"down","amount":6}, {"op":"extract","text":"x"*161},
    {"op":"extract","text":"sk-"+"a"*32}, {"op":"navigate","text":"https://example.test/invented"},
    {"op":"navigate","text":"javascript:alert(1)"}])
def test_v2_paid_invalid_reply_retains_usage_without_effect_child(v2_order, monkeypatch, action):
    svc, caller, order, ai = v2_order
    monkeypatch.setattr(ai, "_provider_call", lambda _: response(json.dumps({"actions":[action],"memory":"","subgoal_done":False})))
    with pytest.raises(ValueError):
        ai.run("v_one", caller, order["id"], request=v2_request(order))
    calls = svc.retrieve("v_one", caller, order["id"])["invocations"]
    assert list(calls) == ["actor_one"] and calls["actor_one"]["actual_micro"] > 0


def test_v2_source_and_cross_origin_offers_refuse_before_provider_admission(v2_order, monkeypatch):
    svc, caller, order, ai = v2_order
    monkeypatch.setattr(ai, "_provider_call", lambda _: pytest.fail("invalid source called provider"))
    for mutate in (lambda r: setattr(r.context,"notes","opaque notes"),
                   lambda r: setattr(r.context,"links",[ai.NavigationTarget(id="c1",href="https://foreign.test/",tag="A",form=False)]),
                   lambda r: setattr(r.context,"links",[ai.NavigationTarget(id="c9",href="https://example.test/two",tag="A",form=False)])):
        request = v2_request(order); mutate(request)
        with pytest.raises(ValueError):
            ai.run("v_one", caller, order["id"], request=request)
    assert svc.retrieve("v_one",caller,order["id"]).get("invocations",{}) == {}


def test_v2_schema_offers_bounded_verbs_without_changing_roles_or_caps(v2_order):
    _, _, order, ai = v2_order
    request = v2_request(order)
    profile, observation = ai.scoped(order, request)
    body = ai.request_body(order, request, profile, observation)
    offered = {op for variant in body["text"]["format"]["schema"]["properties"]["actions"]["items"]["anyOf"]
               for op in variant["properties"]["op"]["enum"]}
    assert offered == {"click","type","navigate","scroll","extract","wait","done","fail","needs_you"}
    assert body["model"] == "gpt-6-sol" and body["reasoning"] == {"effort":"none"}
    assert body["max_output_tokens"] == 700 and "tools" not in body


def test_v2_retained_read_requires_returned_exact_source_child_before_verification(v2_order,monkeypatch):
    from app.build import device_effects as effects
    svc,caller,order,ai=v2_order;args=("v_one",caller,order["id"])
    monkeypatch.setattr(ai,"_provider_call",lambda _:response(json.dumps({"actions":[{"op":"extract","text":"Seats"}],"memory":"","subgoal_done":False})))
    first=v2_request(order);result=ai.run(*args,request=first)
    child=result['effect_proposals'][0]['invocation_id']
    effects.admit(*args,invocation_id=child,observation_digest=first.observation.digest(),effect_digest='e'*64)
    req=v2_request(order,phase='verify',invocation='verify_two')
    notes=json.loads(req.context.notes)
    notes['current']['source']['reads']=[{'query':'Seats','passages':['23 seats'],'omitted':0,
        'invocation_id':child,'observation_digest':first.observation.digest(),'historical':True}]
    req.context.notes=json.dumps(notes)
    with pytest.raises(ValueError):ai.scoped(svc.retrieve(*args),req)
    effects.receipt(*args,invocation_id=child,effect_digest='e'*64,outcome='returned')
    ai.scoped(svc.retrieve(*args),req)
    for change in (lambda n:n['current']['source']['reads'][0].update(query='Plans'),
                   lambda n:n['current'].update(page_id='c'*32),
                   lambda n:n['current']['source']['reads'][0].update(historical=1)):
        changed=copy.deepcopy(notes);change(changed)
        altered=req.model_copy(deep=True)
        altered.context.notes=json.dumps(changed)
        # Keep the current envelope coherent; the historical child still belongs
        # to the original source page, query and typed historical marker.
        if changed['current']['page_id'] != notes['current']['page_id']:
            altered.current_load.page_id=changed['current']['page_id']
        with pytest.raises(ValueError):ai.scoped(svc.retrieve(*args),altered)
    sent=[]
    monkeypatch.setattr(ai,'_provider_call',lambda body:(sent.append(body),response('{"passed":true,"reason":"Historical source read"}'))[1])
    assert ai.run(*args,request=req)['effect_proposals']==[] and len(sent)==1
    assert req.context.notes in sent[0]['input'][1]['content'][0]['text']


@pytest.mark.parametrize("form", [0, 1, True, "false"])
def test_v2_navigation_offer_requires_source_boolean_non_form(form):
    from app.build import device_browser_ai as ai
    with pytest.raises(ValueError):
        ai.NavigationTarget(id="c1", href="https://example.test/two", tag="A", form=form)


def test_subgoal_progress_uses_existing_paid_verify_receipt_without_claiming_task_done(ai_order, monkeypatch):
    svc, caller, order, ai, sent = ai_order
    req = request_for(order, phase='verify', invocation='subgoal_check')
    req.context.reason = 'subgoal_progress'
    req.context.claim = 'Read current source title'
    monkeypatch.setattr(ai, '_provider_call', lambda body: (sent.append(body), response('{"passed":true,"reason":"Source title observed"}'))[1])
    result = ai.run('v_one', caller, order['id'], request=req)
    assert sent[0]['input'][0]['content'].startswith(ai.loop.SUBGOAL_VERIFY_PROMPT)
    assert 'compact schema-valid JSON' in sent[0]['input'][0]['content']
    assert 'SUB-GOAL TO CHECK: Read current source title' in sent[0]['input'][1]['content'][0]['text']
    saved = svc.retrieve('v_one', caller, order['id'])['invocations']['subgoal_check']
    assert saved['state'] == 'completed' and saved['actual_micro'] > 0
    assert result['task_verified'] is False and result['effect_proposals'] == []


def request_for(
    order, *, phase="actor", invocation="actor_one", terminal=False, reviewed=False
):
    from app.build import device_browser_ai as ai
    from coding_agents.jev_decision import Observation

    obs = Observation(
        revision="ps1_test",
        application="com.google.Chrome",
        location="https://example.test/",
        goal=order["manifest"]["instruction"],
        context="PAGE: Source title",
        controls=[
            {
                "id": "c1",
                "label": "Next",
                "operations": ["click", "type"],
                "visible": True,
                "enabled": True,
            }
        ],
        reviewed_text=["Founder"] if reviewed else [],
        permitted=True,
        confirmation_pending=False,
        prior_effect_uncertain=False,
    )
    return ai.BrowserAIBody(
        invocation_id=invocation,
        phase=phase,
        execution_profile_digest=ai.profile_digest(
            order["manifest"]["general_browser_verification"]["execution_profile"]
        ),
        checkpoint_digest="c" * 64,
        observation=obs,
        current_load={
            "qualified": True,
            "session_id": "a" * 32,
            "page_id": "b" * 32,
            "sequence": 1,
            "response": {
                "url": obs.location,
                "status": 200,
                "complete": True,
                "sequence": 1,
            },
            "location": obs.location,
            "document_id": "d" * 32,
            "ready_state": "complete",
            "visible_text": True,
            "visibility_scope": "bounded_dom_text_geometry",
            "observation_digest": obs.digest(),
        },
        context={"terminal_only": terminal, "claim": "Source title"},
    )


def structured_notes_for(request):
    """Exact source-owned notes wire, with a deterministic metadata collision."""
    request.current_load.session_id = "a" * 9 + "1234567890123" + "b" * 10
    source = {
        "facts": ["Source title"],
        "fields": [
            {
                "label": "Name",
                "type": "text",
                "populated": False,
                "reviewed_value_index": None,
            }
        ],
        "omitted": {"facts": 0, "fields": 0, "values": 0},
    }
    entry = {
        key: getattr(request.current_load, key)
        for key in (
            "session_id",
            "page_id",
            "document_id",
            "sequence",
            "location",
            "observation_digest",
        )
    }
    entry["source"] = source
    prior = copy.deepcopy(entry)
    prior["observation_digest"] = "a" * 20 + "1234567890123" + "b" * 31
    return {
        "schema_version": 1,
        "scope": "source_main_document_inputs_not_acceptance",
        "current": entry,
        "prior_pages": [prior],
        "input_history": [
            {
                **copy.deepcopy(prior),
                "invocation_id": "prior_actor_a0",
                "operation": "click",
                "delivery": "input_delivered",
                "acceptance": "unknown",
            }
        ],
        "omitted": {"pages": 0, "inputs": 0},
    }


def response(text, **override):
    return {
        "model": "gpt-6-sol",
        "service_tier": "default",
        "status": "completed",
        "usage": {"input_tokens": 100, "output_tokens": 10, "total_tokens": 110},
        "output": [{"content": [{"type": "output_text", "text": text}]}],
        **override,
    }


@pytest.mark.parametrize("task", [
    "Read inventory turnover for manufacturers.",
    "Read trail access details for weekend hikers.",
], ids=["b2b", "consumer"])
@pytest.mark.parametrize("failure,recovered", [("truncated", True), ("invalid", True), ("invalid", False), ("schema", True)])
@pytest.mark.parametrize("phase", ["plan", "actor", "verify"])
def test_known_unusable_reply_gets_one_compact_metered_retry_without_effects(
        ai_order, monkeypatch, task, failure, recovered, phase):
    import threading
    import time
    from types import SimpleNamespace
    from coding_agents.cu_page_context import bounded_page_observation
    from desktop_runtime.cu_owned_browser_adapter import CuOwnedBrowserAdapter
    from desktop_runtime.device_gateway import UpstreamRejected, WorkOrderGateway
    from app.chat.blockers import explain

    svc, caller, order, ai, sent = ai_order
    saved = copy.deepcopy(order)
    saved["manifest"]["instruction"] = task
    request = request_for(saved, phase=phase)
    request.observation.context = "PAGE TITLE: " + task + "\n" + "Source detail. " * 220
    request.current_load.observation_digest = request.observation.digest()
    observation = request.observation
    browser = SimpleNamespace(read_only=True, bounded_reads=False, session_id=request.current_load.session_id,
        last_load_evidence=request.current_load.model_dump(), _control_meta={})
    browser.verified_load = lambda obs: obs is observation
    browser.cuai_readback = lambda obs: "Current source title: " + task
    def bound(obs, budget):
        projected = bounded_page_observation(obs, budget)
        obs.context, obs.controls = projected.context, projected.controls
        browser.last_load_evidence["observation_digest"] = obs.digest()
    browser.bound_cuai_observation = bound
    valid = {"plan": {"subgoals": [task]}, "actor": {"actions": [{"op": "needs_you", "summary": task}],
        "memory": "", "subgoal_done": False}, "verify": {"passed": False, "reason": "Needs review"}}[phase]
    def provider(body):
        sent.append(body)
        if len(sent) == 2 and recovered:
            return response(json.dumps(valid))
        if failure == "schema":
            invalid = {"plan": {"subgoals": []}, "actor": {"actions": [{"op": "type", "element_id": "c1", "text": ""}],
                "memory": "", "subgoal_done": False}, "verify": {"passed": "yes", "reason": "Unchecked"}}[phase]
            return response(json.dumps(invalid))
        return response('{"actions":[', **({"status": "incomplete",
            "incomplete_details": {"reason": "max_output_tokens"},
            "usage": {"input_tokens": 100, "output_tokens": 700, "total_tokens": 800}}
            if failure == "truncated" else {}))
    monkeypatch.setattr(ai, "_provider_call", provider)
    original_scoped = ai.scoped
    monkeypatch.setattr(ai, "scoped", lambda stored, req: original_scoped(saved, req))
    profile = saved["manifest"]["general_browser_verification"]["execution_profile"]
    gateway = WorkOrderGateway(venture_id="v_one", order_id=order["id"], device_id="dev_one", request=lambda *_: None)
    gateway.execution_id = svc.retrieve("v_one", caller, order["id"])["execution_id"]
    gateway.browser_cuai_profile_digest = ai.profile_digest(profile)
    gateway.current = lambda: None
    invocations = []
    def paid(invocation, role, obs, **kwargs):
        invocations.append(invocation)
        wire = {**request.model_dump(), "invocation_id": invocation, "phase": role,
            "observation": obs.model_dump(), "current_load": browser.last_load_evidence,
            "context": kwargs["context"], "checkpoint_digest": kwargs["checkpoint_digest"]}
        try:
            return ai.run("v_one", caller, order["id"], request=ai.BrowserAIBody.model_validate(wire))
        except ValueError as error:
            raise UpstreamRejected(409, str(error)) from None
    gateway.browser_ai_call = paid
    adapter = CuOwnedBrowserAdapter(browser=browser, gateway=gateway, profile=profile, goal=task,
        stop=threading.Event(), deadline=time.monotonic() + 60)
    if recovered:
        result = adapter._call(phase, observation)
        assert json.loads(result["reply"]) == valid and result["effect_proposals"] == []
    else:
        with pytest.raises(PermissionError, match="browser_cuai_reply_unusable") as caught:
            adapter._call(phase, observation)
        gateway.browser_decision_failed(caught.value)
        message = explain("v_one", {"code": gateway.browser_decision_failure_code})["body_md"]
        assert "one compact retry" in message and "unconfirmed usage" in message and "Reconcile" in message
    durable = svc.retrieve("v_one", caller, order["id"])["invocations"]
    assert adapter.calls == len(sent) == len(set(invocations)) == 2
    assert all(durable[key]["actual_micro"] > 0 for key in invocations)
    assert not any(key.endswith("_a0") for key in durable)
    assert len(json.dumps(sent[1]).encode()) < len(json.dumps(sent[0]).encode()) <= profile["max_request_bytes"]
    assert "COMPACT RETRY:" in json.dumps(sent[1]) and "compact schema-valid JSON" in json.dumps(sent[0])


CLICK = json.dumps(
    {
        "actions": [{"op": "click", "element_id": "c1"}],
        "memory": "",
        "subgoal_done": False,
    }
)


@pytest.fixture
def ai_order(orders, monkeypatch, request):  # noqa: F811
    from app.build import device_browser_ai as ai
    from app.db.models import CreditAccount

    svc, caller, manifest = orders
    read_only = getattr(request, "param", None) == "read_only"
    profile = configure(monkeypatch, actor_low=getattr(request, "param", None) == "actor_low")
    with svc.db()() as session:
        session.get(CreditAccount, "cra_device_test").granted_micro = 1_000_000
        session.commit()
    value = manifest.model_dump(exclude_none=True)
    value.update(
        applications=["com.google.Chrome"],
        actions=["browser.read"] if read_only else ["browser.read", "browser.interact"],
        credit_ceiling_micro=500000,
        general_browser_verification={
            "operation": "general_goal_v1",
            "start_url": "https://example.test/",
            "max_steps": 2,
            "allow_type": not read_only,
            "fill_values": {} if read_only else {"name": "Founder"},
            "execution_profile": profile.model_dump(),
        },
    )
    saved = svc.prepare("v_one", caller, svc.Manifest.model_validate(value))
    for action in ("approve", "claim"):
        svc.transition("v_one", caller, saved["id"], saved["review_digest"], action)
    sent = []
    monkeypatch.setattr(
        ai, "_provider_call", lambda body: (sent.append(body), response(CLICK))[1]
    )
    return svc, caller, saved, ai, sent


@pytest.mark.parametrize("ai_order", ["read_only"], indirect=True)
@pytest.mark.parametrize("action", [{"op": "click", "element_id": "c1"}, {"op": "type", "element_id": "c1", "text": "Ada"}])
def test_read_only_actor_cannot_issue_input_or_replay_paid_attempt(ai_order, monkeypatch, action):
    svc, caller, order, ai, sent = ai_order
    req = request_for(order, reviewed=False)
    profile, observation = ai.scoped(order, req)
    body = ai.request_body(order, req, profile, observation)
    assert "READ-ONLY AUTHORITY" in body["input"][0]["content"][0]["text"]
    offered = {op for row in body["text"]["format"]["schema"]["properties"]["actions"]["items"]["anyOf"]
               for op in row["properties"]["op"]["enum"]}
    assert offered == {"wait", "done", "fail", "needs_you"}
    monkeypatch.setattr(ai, "_provider_call", lambda b: (sent.append(b), response(json.dumps(
        {"actions": [action], "memory": "", "subgoal_done": False})))[1])
    with pytest.raises(ValueError, match="reply_invalid"):
        ai.run("v_one", caller, order["id"], request=req)
    with pytest.raises(ValueError, match="already_attempted"):
        ai.run("v_one", caller, order["id"], request=req)
    calls = svc.retrieve("v_one", caller, order["id"])["invocations"]
    assert len(sent) == len(calls) == 1 and calls["actor_one"]["actual_micro"] > 0
    assert not any(row.get("proposal") or row.get("effect") for row in calls.values())


@pytest.mark.parametrize("ai_order", ["read_only"], indirect=True)
def test_read_only_completed_parent_cannot_be_used_to_create_a_forged_click_child(ai_order, monkeypatch):
    from app.build import device_invocations
    from coding_agents.jev_decision import Candidate
    svc, caller, order, ai, sent = ai_order
    req = request_for(order, reviewed=False)
    monkeypatch.setattr(ai, "_provider_call", lambda b: (sent.append(b), response(json.dumps(
        {"actions": [{"op": "wait", "ms": 1}], "memory": "", "subgoal_done": False})))[1])
    ai.run("v_one", caller, order["id"], request=req)
    with pytest.raises(ValueError, match="proposal_unavailable"):
        device_invocations.save_browser_actor_proposal("v_one", caller, order["id"], invocation_id=req.invocation_id,
            candidate=Candidate("click", "c1", req.observation.digest()))
    assert len(svc.retrieve("v_one", caller, order["id"])["invocations"]) == len(sent) == 1


@pytest.mark.parametrize("intent", ["Compare factory maintenance plans", "Compare family bike rentals"])
def test_exact_actor_parent_child_and_existing_effect_admission_once(ai_order, monkeypatch, intent):
    from app.build import device_effects as effects

    svc, caller, order, ai, sent = ai_order
    args = ("v_one", caller, order["id"])
    request = request_for(order)
    request.context.hint = "NOW: " + intent
    first_clock = "NOW: Tuesday 2026-10-06 23:59 (UTC+0000)"
    monkeypatch.setattr(ai.loop, "now_line", lambda: first_clock)
    result = ai.run(*args, request=request)
    assert result["authority"] == "proposal_only" and result["task_verified"] is False
    child = result["effect_proposals"][0]["invocation_id"]
    assert child == "actor_one_a0" and len(sent) == 1
    record = svc.retrieve(*args)["invocations"]["actor_one"]
    assert (
        record["source_kind"] == ai.SOURCE_KIND
        and record["quote_micro"] > record["actual_micro"] > 0
    )
    assert (
        sent[0]["model"] == "gpt-6-sol"
        and sent[0]["service_tier"] == "default"
        and "tools" not in sent[0]
    )
    provider_text = "\n".join(part["text"] for part in sent[0]["input"][1]["content"])
    assert first_clock in provider_text and request.context.hint in provider_text
    with pytest.raises(ValueError, match="proposal_unavailable"):
        effects.admit(
            *args,
            invocation_id="actor_one",
            observation_digest=request.observation.digest(),
            effect_digest="e" * 64,
        )
    assert effects.admit(
        *args,
        invocation_id=child,
        observation_digest=request.observation.digest(),
        effect_digest="e" * 64,
    )
    assert (
        effects.admit(
            *args,
            invocation_id=child,
            observation_digest=request.observation.digest(),
            effect_digest="e" * 64,
        )
        is False
    )
    with pytest.raises(ValueError, match="reconciliation"):
        ai.run(*args, request=request_for(order, invocation="actor_two"))
    effects.receipt(
        *args, invocation_id=child, effect_digest="e" * 64, outcome="returned"
    )
    monkeypatch.setattr(ai.loop, "now_line", lambda: "NOW: Wednesday 2026-10-07 00:00 (UTC+0000)")
    with pytest.raises(ValueError, match="^device_browser_ai_already_attempted$"):
        ai.run(*args, request=request)
    changed = request.model_copy(deep=True)
    changed.context.hint += " with weekend availability"
    with pytest.raises(ValueError, match="^device_invocation_conflict$"):
        ai.run(*args, request=changed)
    assert len(sent) == 1


@pytest.mark.parametrize("ai_order", ["interact", "read_only"], indirect=True)
@pytest.mark.parametrize(
    "phase,text",
    [
        ("plan", '{"subgoals":["Read title"]}'),
        ("verify", '{"passed":true,"reason":"source title"}'),
    ],
)
def test_planner_and_semantic_verifier_charge_but_never_effects(
    ai_order, monkeypatch, phase, text
):
    from app.build import device_effects as effects

    svc, caller, order, ai, sent = ai_order
    monkeypatch.setattr(
        ai, "_provider_call", lambda body: (sent.append(body), response(text))[1]
    )
    result = ai.run(
        "v_one", caller, order["id"], request=request_for(order, phase=phase)
    )
    assert len(sent) == 1 and sent[0]["reasoning"] == {"effort": "none"}
    assert sent[0]["max_output_tokens"] == 700
    if order["manifest"]["actions"] == ["browser.read"]:
        assert "READ-ONLY AUTHORITY" in sent[0]["input"][0]["content"]
    assert svc.retrieve("v_one", caller, order["id"])["invocations"]["actor_one"]["actual_micro"] > 0
    assert result["effect_proposals"] == [] and result["task_verified"] is False
    with pytest.raises(ValueError, match="proposal_unavailable"):
        effects.admit(
            "v_one",
            caller,
            order["id"],
            invocation_id="actor_one",
            observation_digest="a" * 64,
            effect_digest="b" * 64,
        )
    assert svc.retrieve("v_one", caller, order["id"])["state"] == "claimed"


@pytest.mark.parametrize(
    "change",
    [
        "profile",
        "goal",
        "location",
        "load_digest",
        "unqualified",
        "secret",
        "foreign_device",
    ],
)
def test_bad_scope_refuses_before_provider_or_invocation(ai_order, change):
    svc, caller, order, ai, sent = ai_order
    req = request_for(order)
    if change == "profile":
        req.execution_profile_digest = "f" * 64
    elif change == "goal":
        req.observation.goal = "Different goal"
    elif change == "location":
        req.observation.location = "https://foreign.test/"
    elif change == "load_digest":
        req.current_load.observation_digest = "f" * 64
    elif change == "unqualified":
        req.observation.permitted = False
    elif change == "secret":
        req.context.notes = "sk-" + "a" * 20
    else:
        caller = replace(caller, device_id="other_device")
    with pytest.raises((ValueError, KeyError)):
        ai.run("v_one", caller, order["id"], request=req)
    assert sent == [] and "invocations" not in svc.retrieve(
        "v_one", ai_order[1], order["id"]
    )


@pytest.mark.parametrize(
    "kind",
    [
        "transport",
        "missing_usage",
        "wrong_model",
        "wrong_tier",
        "incomplete",
        "bool_usage",
        "missing_total",
        "wrong_total",
        "bool_total",
        "float_total",
    ],
)
def test_unknown_usage_permanent_hold_no_retry_even_after_cancel(
    ai_order, monkeypatch, kind
):
    from app.build import device_invocations as calls

    svc, caller, order, ai, sent = ai_order

    def fail(body):
        sent.append(body)
        if kind == "transport":
            raise RuntimeError("synthetic transport")
        value = response(CLICK)
        if kind == "missing_usage":
            value.pop("usage")
        elif kind == "wrong_model":
            value["model"] = "other"
        elif kind == "wrong_tier":
            value["service_tier"] = "priority"
        elif kind == "incomplete":
            value["status"] = "incomplete"
        elif kind == "bool_usage":
            value["usage"]["input_tokens"] = True
        elif kind == "missing_total":
            value["usage"].pop("total_tokens")
        elif kind == "wrong_total":
            value["usage"]["total_tokens"] = 1
        elif kind == "bool_total":
            value["usage"]["total_tokens"] = True
        else:
            value["usage"]["total_tokens"] = 110.0
        return value

    monkeypatch.setattr(ai, "_provider_call", fail)
    args = ("v_one", caller, order["id"])
    with pytest.raises(ValueError, match="unconfirmed"):
        ai.run(*args, request=request_for(order))
    saved = svc.retrieve(*args)
    assert (
        saved["invocations"]["actor_one"]["state"] == "uncertain"
        and saved["inference_reconciliation_required"]
    )
    with pytest.raises(ValueError):
        ai.run(*args, request=request_for(order, invocation="new_call"))
    svc.transition(*args, order["review_digest"], "cancel")
    with pytest.raises(ValueError, match="usage_conflict"):
        calls.record_result(*args, invocation_id="actor_one", actual_micro=1)
    assert len(sent) == 1


def test_paid_invalid_actor_is_not_refunded_or_given_effect(ai_order, monkeypatch):
    svc, caller, order, ai, sent = ai_order
    monkeypatch.setattr(
        ai,
        "_provider_call",
        lambda body: (sent.append(body), response(CLICK.replace("c1", "c2")))[1],
    )
    with pytest.raises(ValueError, match="target_invalid"):
        ai.run("v_one", caller, order["id"], request=request_for(order))
    invocations = svc.retrieve("v_one", caller, order["id"])["invocations"]
    assert (
        len(invocations) == 1
        and invocations["actor_one"]["state"] == "completed"
        and invocations["actor_one"]["actual_micro"] > 0
    )


def test_terminal_only_durable_state_never_reopens_effects(ai_order, monkeypatch):
    svc, caller, order, ai, sent = ai_order
    done = json.dumps(
        {
            "actions": [{"op": "done", "summary": "Source title"}],
            "memory": "",
            "subgoal_done": True,
        }
    )
    monkeypatch.setattr(
        ai, "_provider_call", lambda body: (sent.append(body), response(done))[1]
    )
    args = ("v_one", caller, order["id"])
    result = ai.run(*args, request=request_for(order, terminal=True))
    assert result["effect_proposals"] == []
    with pytest.raises(ValueError, match="terminal_effect_refused"):
        ai.run(*args, request=request_for(order, invocation="resumed_actor"))
    assert len(sent) == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("service_tier", "priority"),
        ("reasoning", "high"),
        ("provider", "unknown"),
        ("max_output_tokens", True),
    ],
)
def test_saved_roles_are_closed(monkeypatch, field, value):
    profile = configure(monkeypatch).model_dump()
    profile["roles"]["actor"][field] = value
    from app.build.device_orders import BrowserAIProfile

    with pytest.raises(ValidationError):
        BrowserAIProfile.model_validate(profile)


@pytest.mark.parametrize("phase,model,effort", [
    ("actor", "gpt-6.1-sol", "none"), ("actor", "gpt-6.1-sol", "medium"),
    ("actor", "gpt-6-sol", "low"), ("actor", "gpt-6-luna", "low"),
    ("actor", "gpt-5.6-luna", "low"), ("plan", "gpt-6.1-sol", "low"),
    ("verify", "gpt-6.1-sol", "low"), ("plan", "gpt-6.1-sol", "none"),
    ("verify", "gpt-6.1-sol", "none"),
])
def test_actor_low_is_the_only_new_reviewed_role_pair(orders, monkeypatch, phase, model, effort):  # noqa: F811
    from app.build.device_orders import BrowserAIProfile
    profile = configure(monkeypatch, actor_low=True).model_dump()
    profile["roles"][phase].update(model=model, reasoning=effort)
    with pytest.raises(ValidationError):
        BrowserAIProfile.model_validate(profile)


@pytest.mark.parametrize("ai_order", ["actor_low"], indirect=True)
@pytest.mark.parametrize("phase", ["plan", "actor", "verify"])
def test_actor_low_wire_uses_saved_effort_and_total_token_cap(ai_order, phase):
    svc, caller, order, ai, _ = ai_order
    req = request_for(order, phase=phase)
    profile, observation = ai.scoped(order, req)
    body = ai.request_body(order, req, profile, observation)
    role = getattr(profile.roles, phase)
    assert body["model"] == role.model
    assert body["reasoning"] == {"effort": "low" if phase == "actor" else "none"}
    assert body["max_output_tokens"] == role.max_output_tokens == 700
    assert body["store"] is False and body["service_tier"] == "default"
    assert "tools" not in body and "temperature" not in body
    assert len(json.dumps(body).encode()) <= profile.max_request_bytes
    assert svc.retrieve("v_one", caller, order["id"]).get("invocations", {}) == {}


def test_inactive_model_cache_write_rates_preserve_active_profile_price(orders, monkeypatch):  # noqa: F811
    from app.build import device_browser_ai as ai, device_orders as svc
    configure(monkeypatch)
    root = Path(__file__).parents[3]
    active = svc.BrowserAIProfile.model_validate_json((root / "config/browser_cuai_profile.json").read_text())
    assert ai.pricing_digest(active) == active.pricing_digest
    assert ai.pricing_table()["gpt-5.6-luna"] == {"input_per_mtok": 0.2, "output_per_mtok": 1.2}
    assert ai.pricing_table()["gpt-6.1-sol"]["cache_write_per_mtok"] == 2.5
    assert ai.pricing_table()["gpt-6-luna"]["cache_write_per_mtok"] == 0.125
    assert {role.model for role in (active.roles.plan, active.roles.actor, active.roles.verify)} == {"gpt-6-luna"}
    assert {role.provider for role in (active.roles.plan, active.roles.actor, active.roles.verify)} == {"openai"}


def test_provider_env_qualifies_exact_azure_and_fallback_prices(monkeypatch):
    from app.build import device_browser_ai as ai, device_orders as svc
    configure(monkeypatch)
    value = json.loads((Path(__file__).parents[3] / "config/browser_cuai_profile.json").read_text())
    for role in value["roles"].values():
        role["model"] = "gpt-6-luna"
    value["pricing_digest"] = ai.pricing_digest(svc.BrowserAIProfile.model_validate(value))
    monkeypatch.setenv("DEVICE_BROWSER_CUAI_PROFILE_JSON", json.dumps(value))
    monkeypatch.setenv("OPENAI_BROWSER_CUAI_MODELS", "gpt-5.6-luna,gpt-6-luna")
    monkeypatch.setenv("DEVICE_BROWSER_CUAI_PROVIDER", "azure")
    monkeypatch.setenv("AZURE_OPENAI_CUAI_ENDPOINT", "https://example.openai.azure.com/openai/v1")
    monkeypatch.setenv("AZURE_OPENAI_CUAI_API_KEY", "synthetic")
    configured = ai.configured_profile()
    assert configured.roles.actor.provider == "azure" and configured.roles.actor.deployment == "gpt-6-luna"
    assert ai.assert_configuration(configured) == configured
    assert configured.pricing_digest != value["pricing_digest"]
    table = copy.deepcopy(ai.pricing_table())
    table["azure/gpt-6-luna"]["output_per_mtok"] = 0.6
    monkeypatch.setattr(ai, "pricing_table", lambda: table)
    assert ai.pricing_digest(configured) != configured.pricing_digest
    with pytest.raises(ValueError, match="configuration_unavailable"):
        ai.assert_configuration(configured)
    assert svc.BrowserAIProfile.model_validate(value).model_dump() == value


@pytest.mark.parametrize("fallback", [False, True, "content_filter", "timeout", "pre_output_timeout", "both_timeout", "missing_fallback_key", "wrong_model"])
def test_azure_phase_single_admission_provider_receipt_and_exact_price(ai_order, monkeypatch, fallback):
    svc, caller, order, ai, _ = ai_order
    order["manifest"]["task_id"] += "_azure"
    value = order["manifest"]["general_browser_verification"]["execution_profile"]
    for role in value["roles"].values():
        role.update(model="gpt-6-luna", provider="azure", endpoint="https://example.openai.azure.com/openai/v1",
                    deployment="gpt-6-luna")
    table = copy.deepcopy(ai.pricing_table())
    table["azure/gpt-6-luna"]["output_per_mtok"] = 0.6
    monkeypatch.setattr(ai, "pricing_table", lambda: table)
    profile = svc.BrowserAIProfile.model_validate(value)
    profile.pricing_digest = ai.pricing_digest(profile)
    value.update(profile.model_dump())
    monkeypatch.setenv("DEVICE_BROWSER_CUAI_PROFILE_JSON", json.dumps(value))
    monkeypatch.setenv("OPENAI_BROWSER_CUAI_MODELS", "gpt-6-luna")
    monkeypatch.setenv("AZURE_OPENAI_CUAI_API_KEY", "synthetic")
    prepared = svc.prepare("v_one", caller, svc.Manifest.model_validate(order["manifest"]))
    for action in ("approve", "claim"):
        svc.transition("v_one", caller, prepared["id"], prepared["review_digest"], action)
    sent = []
    def send(url, headers, body):
        sent.append((url, headers, body))
        import httpx
        if fallback in {'pre_output_timeout', 'both_timeout', 'missing_fallback_key'} and len(sent) == 1:
            if fallback == 'missing_fallback_key':
                monkeypatch.delenv('OPENAI_DEVICE_VISION_API_KEY', raising=False)
                monkeypatch.delenv('OPENAI_API_KEY', raising=False)
            raise ai.providers.NoOutputReadTimeout('no bytes received')
        if fallback == 'both_timeout':
            raise httpx.ReadTimeout('fallback uncertain')
        if fallback == "timeout":
            import httpx
            raise httpx.ReadTimeout("outcome unknown")
        if fallback is True and len(sent) == 1:
            return 429, {"error": {"code": "rate_limit"}}
        if fallback == "content_filter" and len(sent) == 1:
            return 400, {"error": {"code": "content_filter", "innererror": {"code": "ResponsibleAIPolicyViolation"}}}
        result = response('{"subgoals":["Read"]}', model="wrong" if fallback == "wrong_model" else "gpt-6-luna")
        if fallback not in {True, "content_filter", "pre_output_timeout"}:
            result.pop("service_tier")
        return 200, result
    monkeypatch.setattr(ai, "_send_provider", send)
    req = request_for(prepared, phase="plan")
    if fallback in {"timeout", "wrong_model", "both_timeout", "missing_fallback_key"}:
        with pytest.raises(ValueError, match="unconfirmed"):
            ai.run("v_one", caller, prepared["id"], request=req)
        saved = svc.retrieve("v_one", caller, prepared["id"])
        assert saved["invocations"][req.invocation_id]["state"] == "uncertain"
        assert saved["inference_reconciliation_required"] is True and len(sent) == (2 if fallback == 'both_timeout' else 1)
        if fallback in {'both_timeout', 'missing_fallback_key'}:
            receipt = saved['invocations'][req.invocation_id]
            assert receipt['provider_attempts'][0]['provider'] == 'azure'
            assert receipt['unknown_provider_micro'] > 0
            assert len(receipt['provider_attempts']) == len(sent)
        with pytest.raises(ValueError):
            ai.run("v_one", caller, prepared["id"], request=request_for(prepared, phase="plan", invocation="another"))
        assert len(sent) == (2 if fallback == 'both_timeout' else 1)
        return
    result = ai.run("v_one", caller, prepared["id"], request=req)
    served = "openai" if fallback else "azure"
    receipt = svc.retrieve("v_one", caller, prepared["id"])["invocations"][req.invocation_id]
    assert result["provider"] == receipt["provider"] == served
    assert receipt["actual_micro"] == ai.cost_micro("gpt-6-luna", 100, 10, rates=table[ai.pricing_key("gpt-6-luna", served)]) + receipt.get('unknown_provider_micro', 0)
    assert receipt["state"] == "completed" and len(sent) == (2 if fallback else 1)
    if fallback:
        expected = "azure_pre_output_timeout" if fallback == "pre_output_timeout" else "azure_content_filter" if fallback == "content_filter" else "azure_http_error"
        assert receipt["fallback_reason"] == result["fallback_reason"] == expected
        if fallback == 'pre_output_timeout':
            assert receipt['provider_attempts'] == [
                {'provider': 'azure', 'state': 'usage_unconfirmed', 'held_micro': receipt['unknown_provider_micro']},
                {'provider': 'openai', 'state': 'known'}]
            assert receipt['unknown_provider_micro'] > receipt['provider_actual_micro']
            assert result['azure_usage_unconfirmed'] is True
            assert ai.run('v_one', caller, prepared['id'], request=request_for(prepared, phase='plan', invocation='next'))['provider'] == 'azure'
            assert len(sent) == 3
        from app.build import device_invocations as calls
        with pytest.raises(ValueError, match="usage_conflict"):
            calls.record_result('v_one', caller, prepared['id'], invocation_id=req.invocation_id,
                actual_micro=receipt['provider_actual_micro'], provider='openai', fallback_reason='azure_connect_error')
        for reason in ('private-content', [], 'azure_content_filter'):
            with pytest.raises(ValueError, match="usage_invalid"):
                calls.record_result('v_one', caller, prepared['id'], invocation_id=req.invocation_id,
                    actual_micro=receipt['provider_actual_micro'], provider='azure', fallback_reason=reason)
    with pytest.raises(ValueError, match="already_attempted"):
        ai.run("v_one", caller, prepared["id"], request=req)
    assert len(sent) == (3 if fallback == 'pre_output_timeout' else 2 if fallback else 1)


def test_saved_pending_profile_change_keeps_cancel_and_fresh_review_working(ai_order, monkeypatch):
    svc, caller, order, ai, sent = ai_order
    order["manifest"]["task_id"] += "_pending"
    pending = svc.prepare("v_one", caller, svc.Manifest.model_validate(order["manifest"]))
    value = json.loads((Path(__file__).parents[3] / "config/browser_cuai_profile.json").read_text())
    monkeypatch.setenv("DEVICE_BROWSER_CUAI_PROFILE_JSON", json.dumps(value))
    monkeypatch.setenv("OPENAI_BROWSER_CUAI_MODELS", "gpt-6-sol,gpt-5.6-luna,gpt-6-luna")
    with pytest.raises(ValueError, match="configuration_unavailable"):
        svc.transition("v_one", caller, pending["id"], pending["review_digest"], "approve")
    from app.api.device_orders_api import _conflict_detail
    assert "Cancel" in _conflict_detail(ai.UNAVAILABLE)["message"] and "rerun" in _conflict_detail(ai.UNAVAILABLE)["message"]
    assert svc.transition("v_one", caller, pending["id"], pending["review_digest"], "cancel")["state"] == "cancelled"
    manifest = pending["manifest"]
    manifest["general_browser_verification"]["execution_profile"] = value
    fresh = svc.prepare("v_one", caller, svc.Manifest.model_validate(manifest))
    assert svc.transition("v_one", caller, fresh["id"], fresh["review_digest"], "approve")["state"] == "approved"
    assert not sent


def test_qualification_snapshot_binds_azure_and_fallback_rates(monkeypatch):
    from types import SimpleNamespace
    from app.build import browser_qualification as qualification, device_browser_ai as ai, device_orders as svc
    configure(monkeypatch)
    value = json.loads((Path(__file__).parents[3] / "config/browser_cuai_profile.json").read_text())
    for role in value["roles"].values():
        role.update(model="gpt-6-luna", provider="azure", endpoint="https://example.openai.azure.com/openai/v1", deployment="gpt-6-luna")
    profile = svc.BrowserAIProfile.model_validate(value)
    profile.pricing_digest = ai.pricing_digest(profile)
    monkeypatch.setenv("DEVICE_BROWSER_CUAI_PROFILE_JSON", json.dumps(profile.model_dump()))
    monkeypatch.setenv("OPENAI_BROWSER_CUAI_MODELS", "gpt-6-luna")
    monkeypatch.setenv("AZURE_OPENAI_CUAI_API_KEY", "synthetic")
    monkeypatch.setattr(qualification, "_authority", lambda *args, **kwargs: (object(), trial))
    monkeypatch.setattr(qualification, "_hold", lambda *args: None)
    monkeypatch.setattr(qualification, "_save", lambda *args: None)
    from app.llm import pricing
    monkeypatch.setattr(pricing, "pricing_table", ai.pricing_table)
    trial = {"profiles": {}, "attempts": {}, "allocations": {"dwo_one": {"remaining_micro_usd": 1000}}}
    body = {"id": "dwo_one", "state": "claimed", "execution_id": "dex_one",
        "manifest": {"general_browser_verification": {"execution_profile": profile.model_dump()}},
        "invocations": {"bai_one": {"source_kind": ai.SOURCE_KIND, "state": "calling", "phase": "plan",
            "profile_digest": ai.profile_digest(profile), "quote_micro": 100, "request_digest": "a" * 64}}}
    grant = SimpleNamespace(profile_digest=ai.profile_digest(profile))
    assert qualification.reserve_attempt(None, None, body, grant, invocation_id="bai_one") is True
    capture = trial["profiles"][grant.profile_digest]
    assert capture["provider"] == "azure" and set(capture["rates"]) == {"gpt-6-luna", "azure/gpt-6-luna"}
    assert capture["pricing_digest"] == profile.pricing_digest
    body['invocations']['bai_one'].update(state='completed', provider_actual_micro=1,
        actual_micro=51, unknown_provider_micro=50, usage={'input_tokens': 1, 'output_tokens': 0})
    assert qualification.settle_attempt(None, None, body, grant, invocation_id='bai_one') is False
    assert trial['allocations']['dwo_one']['remaining_micro_usd'] == 900


def test_restored_cache_write_price_requires_matching_candidate_review(orders, monkeypatch):  # noqa: F811
    from app.build import device_browser_ai as ai
    fresh = configure(monkeypatch, actor_low=True)
    old_table = copy.deepcopy(ai.pricing_table())
    old_table["gpt-6.1-sol"].pop("cache_write_per_mtok")
    with monkeypatch.context() as old:
        old.setattr(ai, "pricing_table", lambda: old_table)
        previous_digest = ai.pricing_digest(fresh)
    stale = fresh.model_copy(update={"pricing_digest": previous_digest})
    assert stale.pricing_digest != fresh.pricing_digest
    monkeypatch.setenv("DEVICE_BROWSER_CUAI_PROFILE_JSON", json.dumps(stale.model_dump()))
    with pytest.raises(ValueError, match="configuration_unavailable"):
        ai.assert_configuration(stale)


@pytest.mark.parametrize("missing", ["gate", "key", "models", "pricing"])
def test_configuration_exact_refusal_without_default_prices(monkeypatch, missing):
    from app.build import device_browser_ai as ai

    profile = configure(monkeypatch)
    if missing == "gate":
        monkeypatch.delenv("DEVICE_BROWSER_CUAI_EXECUTION_ENABLED")
    elif missing == "key":
        monkeypatch.delenv("OPENAI_DEVICE_VISION_API_KEY")
    elif missing == "models":
        monkeypatch.setenv("OPENAI_BROWSER_CUAI_MODELS", "other")
    else:
        monkeypatch.setattr(
            ai,
            "pricing_table",
            lambda: {"default": {"input_per_mtok": 99, "output_per_mtok": 99}},
        )
    with pytest.raises(ValueError, match="configuration_unavailable"):
        ai.assert_configuration(profile)


def test_cache_write_upper_rate_and_rounding_are_in_profile_price(monkeypatch):
    from app.build import device_browser_ai as ai

    profile = configure(monkeypatch)
    assert ai.cost_micro("gpt-6-sol", 272000, 0) == 680000
    assert ai.cost_micro("gpt-6-sol", 1000000, 0) == 5000000
    assert ai.cost_micro("gpt-6-sol", 272001, 100) == 1361505
    assert ai.cost_micro("gpt-6-sol", 1, 0) == 3
    assert profile.pricing_digest == ai.pricing_digest(profile)


def test_all_phases_share_persistent_call_cap_and_paid_usage(ai_order, monkeypatch):
    svc, caller, order, ai, sent = ai_order
    args = ("v_one", caller, order["id"])

    def provider(body):
        sent.append(body)
        phase = "verify" if body["text"]["format"]["name"] == "mac_verify" else "plan"
        return response(
            '{"passed":false,"reason":"incomplete"}'
            if phase == "verify"
            else '{"subgoals":["Read"]}'
        )

    monkeypatch.setattr(ai, "_provider_call", provider)
    for index in range(8):
        ai.run(
            *args,
            request=request_for(
                order,
                phase="plan" if index % 2 else "verify",
                invocation=f"phase_{index}",
            ),
        )
    with pytest.raises(ValueError, match="invocation_limit"):
        ai.run(*args, request=request_for(order, invocation="actor_after_cap"))
    assert len(sent) == 8 and all(
        c["actual_micro"] > 0 for c in svc.retrieve(*args)["invocations"].values()
    )


def test_actor_step_cap_survives_completed_effects(ai_order):
    from app.build import device_effects as effects

    svc, caller, order, ai, sent = ai_order
    args = ("v_one", caller, order["id"])
    for index in range(2):
        req = request_for(order, invocation=f"actor_{index}")
        child = ai.run(*args, request=req)["effect_proposals"][0]["invocation_id"]
        effects.admit(
            *args,
            invocation_id=child,
            observation_digest=req.observation.digest(),
            effect_digest="e" * 64,
        )
        effects.receipt(
            *args, invocation_id=child, effect_digest="e" * 64, outcome="returned"
        )
    with pytest.raises(ValueError, match="invocation_limit"):
        ai.run(*args, request=request_for(order, invocation="actor_three"))
    assert len(sent) == 2


def test_known_usage_overflow_retains_larger_cost_and_stops(ai_order, monkeypatch):
    svc, caller, order, ai, sent = ai_order
    monkeypatch.setattr(
        ai,
        "_provider_call",
        lambda body: (
            sent.append(body),
            response(
                CLICK,
                usage={
                    "input_tokens": 100000,
                    "output_tokens": 10,
                    "total_tokens": 100010,
                },
            ),
        )[1],
    )
    args = ("v_one", caller, order["id"])
    with pytest.raises(ValueError, match="usage_bounds"):
        ai.run(*args, request=request_for(order))
    saved = svc.retrieve(*args)
    call = saved["invocations"]["actor_one"]
    assert call["actual_micro"] >= call["provider_actual_micro"] > call["quote_micro"]
    assert (
        saved["inference_reconciliation_required"]
        and "actor_one_a0" not in saved["invocations"]
    )
    with pytest.raises(ValueError):
        ai.run(*args, request=request_for(order, invocation="retry"))
    assert len(sent) == 1


@pytest.mark.parametrize(
    "change", ["session", "reordered", "same_sequence_new_document"]
)
def test_current_source_history_cannot_adopt_stale_document(
    ai_order, monkeypatch, change
):
    svc, caller, order, ai, sent = ai_order
    monkeypatch.setattr(
        ai,
        "_provider_call",
        lambda body: (sent.append(body), response('{"subgoals":["Read"]}'))[1],
    )
    first = request_for(order, phase="plan", invocation="first")
    first.current_load.sequence = first.current_load.response.sequence = 2
    ai.run("v_one", caller, order["id"], request=first)
    next_req = request_for(order, phase="plan", invocation="next")
    if change == "session":
        next_req.current_load.session_id = "e" * 32
    elif change == "same_sequence_new_document":
        next_req.current_load.sequence = next_req.current_load.response.sequence = 2
        next_req.current_load.document_id = "e" * 32
    with pytest.raises(ValueError, match="request_invalid"):
        ai.run("v_one", caller, order["id"], request=next_req)
    assert len(sent) == 1


def test_type_proposal_requires_exact_saved_value_and_source_control(
    ai_order, monkeypatch
):
    from app.build import device_effects as effects

    svc, caller, order, ai, sent = ai_order
    args = ("v_one", caller, order["id"])
    text = json.dumps(
        {
            "actions": [{"op": "type", "element_id": "c1", "text": "Founder"}],
            "memory": "",
            "subgoal_done": False,
        }
    )
    monkeypatch.setattr(
        ai, "_provider_call", lambda body: (sent.append(body), response(text))[1]
    )
    req = request_for(order, reviewed=True)
    result = ai.run(*args, request=req)
    child = result["effect_proposals"][0]
    assert child["candidate"]["value_index"] == 0
    assert effects.admit(
        *args,
        invocation_id=child["invocation_id"],
        observation_digest=req.observation.digest(),
        effect_digest="e" * 64,
    )
    effects.receipt(
        *args,
        invocation_id=child["invocation_id"],
        effect_digest="e" * 64,
        outcome="not_dispatched",
        pre_dispatch_code="browser_pre_dispatch_target_changed",
    )
    assert (
        svc.retrieve(*args)["invocations"][child["invocation_id"]]["effect"]["state"]
        == "not_dispatched"
    )


def test_budget_ceiling_refuses_before_provider_even_with_open_call_cap(ai_order):
    from app.db.models import VentureDocument

    svc, caller, order, ai, sent = ai_order
    with svc.db()() as session:
        row = session.get(VentureDocument, order["id"])
        saved = json.loads(row.provenance_json)
        saved["invocations"] = {
            "prior": {
                "state": "completed",
                "request_digest": "a" * 64,
                "quote_micro": 500000,
                "actual_micro": 500000,
            }
        }
        row.provenance_json = json.dumps(saved)
        session.commit()
    with pytest.raises(ValueError, match="budget_exhausted"):
        ai.run("v_one", caller, order["id"], request=request_for(order))
    assert sent == []


def test_actor_receives_whole_source_readback_not_silent_head_cut(ai_order):
    _, _, order, ai, _ = ai_order
    req = request_for(order)
    req.observation.context = "Untrusted page prose. " * 180
    req.current_load.observation_digest = req.observation.digest()
    req.context.notes = json.dumps(
        {"history": ["source receipt " * 250], "tail": "EXACT WHOLE SOURCE VALUE"}
    )
    profile, observation = ai.scoped(order, req)
    body = ai.request_body(order, req, profile, observation)
    text = body["input"][1]["content"][0]["text"]
    assert req.context.notes in text and req.observation.context in text
    assert (
        "EXACT WHOLE SOURCE VALUE" in text
        and "supported operations: click,type" in text
    )


def test_saved_browser_profile_schema_excludes_long_harness_only_read_tools(ai_order):
    _, _, order, ai, sent = ai_order
    req = request_for(order)
    profile, observation = ai.scoped(order, req)
    body = ai.request_body(order, req, profile, observation)
    variants = body["text"]["format"]["schema"]["properties"]["actions"]["items"]["anyOf"]
    operations = {op for variant in variants for op in variant["properties"]["op"]["enum"]}
    assert operations == {"click", "type", "wait", "done", "fail", "needs_you"}
    assert not operations.intersection({"extract", "scroll", "navigate", "new_tab", "switch_tab"})
    assert sent == []  # Inspecting the actual reviewed request body has no transport authority.


def test_prior_page_source_clock_checked_after_valid_popup(ai_order, monkeypatch):
    svc, caller, order, ai, sent = ai_order
    monkeypatch.setattr(
        ai,
        "_provider_call",
        lambda body: (sent.append(body), response('{"subgoals":["Read"]}'))[1],
    )
    first = request_for(order, phase="plan", invocation="first_page")
    first.current_load.sequence = first.current_load.response.sequence = 2
    args = ("v_one", caller, order["id"])
    ai.run(*args, request=first)
    popup = request_for(order, phase="plan", invocation="popup")
    popup.current_load.page_id = "e" * 32
    popup.current_load.document_id = "f" * 32
    ai.run(*args, request=popup)
    with pytest.raises(ValueError, match="request_invalid"):
        ai.run(*args, request=request_for(order, phase="plan", invocation="old_page"))
    assert len(sent) == 2


def test_configuration_change_during_io_preserves_admitted_cost_no_effect(
    ai_order, monkeypatch
):
    svc, caller, order, ai, sent = ai_order

    def provider(body):
        sent.append(body)
        monkeypatch.setattr(ai, "pricing_table", lambda: {})
        return response(CLICK)

    monkeypatch.setattr(ai, "_provider_call", provider)
    args = ("v_one", caller, order["id"])
    with pytest.raises(ValueError, match="configuration_unavailable"):
        ai.run(*args, request=request_for(order))
    calls = svc.retrieve(*args)["invocations"]
    assert (
        len(calls) == 1
        and calls["actor_one"]["state"] == "completed"
        and calls["actor_one"]["actual_micro"] == 350
    )


@pytest.mark.parametrize(
    "change", ["thought", "summary", "wait_bool", "later_unsupported"]
)
def test_paid_malformed_actor_cannot_issue_effect_or_truncate_claim(
    ai_order, monkeypatch, change
):
    svc, caller, order, ai, sent = ai_order
    reply = json.loads(CLICK)
    if change == "thought":
        reply["thought"] = "extra"
    elif change == "summary":
        reply["actions"] = [{"op": "done", "summary": "x" * 501}]
    elif change == "wait_bool":
        reply["actions"] = [{"op": "wait", "ms": True}]
    else:
        reply["actions"].append({"op": "fs", "path": "/tmp/forbidden"})
    monkeypatch.setattr(
        ai,
        "_provider_call",
        lambda body: (sent.append(body), response(json.dumps(reply)))[1],
    )
    args = ("v_one", caller, order["id"])
    with pytest.raises(ValueError, match="reply_invalid"):
        ai.run(*args, request=request_for(order))
    saved = svc.retrieve(*args)
    assert len(sent) == 1 and len(saved["invocations"]) == 1
    assert saved["invocations"]["actor_one"]["actual_micro"] == 350


def test_actor_schema_advertises_only_adapter_delivery_shapes(ai_order):
    _, _, order, ai, _ = ai_order
    req = request_for(order)
    profile, observation = ai.scoped(order, req)
    body = ai.request_body(order, req, profile, observation)
    variants = body["text"]["format"]["schema"]["properties"]["actions"]["items"][
        "anyOf"
    ]
    assert {
        op for variant in variants for op in variant["properties"]["op"]["enum"]
    } == {"click", "type", "wait", "done", "fail", "needs_you"}


def test_unlisted_or_changed_public_profile_refuses_without_defaulting(
    ai_order, monkeypatch
):
    _, caller, order, ai, sent = ai_order
    configured = json.loads(
        __import__("os").environ["DEVICE_BROWSER_CUAI_PROFILE_JSON"]
    )
    configured["max_provider_calls"] += 1
    monkeypatch.setenv("DEVICE_BROWSER_CUAI_PROFILE_JSON", json.dumps(configured))
    with pytest.raises(ValueError, match="configuration_unavailable"):
        ai.run("v_one", caller, order["id"], request=request_for(order))
    assert not sent


def test_duplicate_saved_field_values_share_the_exact_source_reviewed_text(ai_order):
    _, _, order, ai, _ = ai_order
    order["manifest"]["general_browser_verification"]["fill_values"] = {
        "name": "Founder",
        "text": "Founder",
    }
    req = request_for(order, reviewed=True)
    _, observation = ai.scoped(order, req)
    candidate = ai._candidate(
        json.dumps(
            {
                "actions": [{"op": "type", "element_id": "c1", "text": "Founder"}],
                "memory": "",
                "subgoal_done": False,
            }
        ),
        observation,
        order["manifest"]["general_browser_verification"],
        False,
    )
    assert candidate.value_index == 0 and observation.reviewed_text == ["Founder"]


def test_verifier_receives_whole_source_json_separately_from_agent_memory(ai_order):
    _, _, order, ai, _ = ai_order
    req = request_for(order, phase="verify")
    req.context.notes = json.dumps({"history": "x" * 4880, "tail": "EXACT WHOLE VALUE"})
    req.context.memory = "m" * 700
    req.observation.context = "current source prose " * 160
    req.current_load.observation_digest = req.observation.digest()
    profile, observation = ai.scoped(order, req)
    body = ai.request_body(order, req, profile, observation)
    text = body["input"][1]["content"][0]["text"]
    assert (
        req.context.notes in text
        and req.observation.context in text
        and req.context.memory in text
    )


def test_over_byte_limit_source_envelope_refuses_instead_of_head_cut(ai_order):
    _, _, order, ai, sent = ai_order
    req = request_for(order, phase="verify")
    req.context.notes = "界" * 1800
    with pytest.raises(ValueError, match="request_invalid"):
        ai.run("v_one", ai_order[1], order["id"], request=req)
    assert sent == []


@pytest.mark.parametrize("phase", ["plan", "actor", "verify"])
def test_all_phases_receive_exact_approved_fill_index_mapping(ai_order, phase):
    _, _, order, ai, _ = ai_order
    req = request_for(order, phase=phase, reviewed=True)
    profile, observation = ai.scoped(order, req)
    body = ai.request_body(order, req, profile, observation)
    section = body["input"][1]["content"][-1]["text"]
    assert section.startswith("EXACT APPROVED NON-SECRET FILL VALUES")
    assert json.loads(section.split("\n", 1)[1]) == ["Founder"]


def test_bound_structured_custody_collisions_reach_actual_phase_unchanged(
    ai_order, monkeypatch
):
    svc, caller, order, ai, sent = ai_order
    req = request_for(order, phase="plan")
    req.context.notes = json.dumps(structured_notes_for(req))
    assert ai.loop.CARD.search(req.context.notes)  # Original raw scan refused this.
    monkeypatch.setattr(
        ai,
        "_provider_call",
        lambda body: (sent.append(body), response('{"subgoals":["Read source"]}'))[1],
    )
    result = ai.run("v_one", caller, order["id"], request=req)
    assert len(sent) == 1 and result["effect_proposals"] == []
    assert req.context.notes in sent[0]["input"][1]["content"][0]["text"]
    record = svc.retrieve("v_one", caller, order["id"])["invocations"]["actor_one"]
    assert record["state"] == "completed" and record["actual_micro"] > 0
    assert result["task_verified"] is False


def test_only_existing_actor_child_qualifies_namespaced_input_identifier(
    ai_order, monkeypatch
):
    from app.build import device_effects as effects

    svc, caller, order, ai, sent = ai_order
    args = ("v_one", caller, order["id"])
    invocation = "bai_" + "a" * 9 + "1234567890123" + "b" * 10
    first = request_for(order, invocation=invocation)
    notes = structured_notes_for(first)
    first.context.notes = json.dumps(notes)
    result = ai.run(*args, request=first)
    child = result["effect_proposals"][0]["invocation_id"]
    assert effects.admit(
        *args,
        invocation_id=child,
        observation_digest=first.observation.digest(),
        effect_digest="e" * 64,
    )
    effects.receipt(
        *args, invocation_id=child, effect_digest="e" * 64, outcome="returned"
    )
    second = request_for(order, phase="plan", invocation="next_plan")
    notes = structured_notes_for(second)
    notes["input_history"][0].update(
        invocation_id=child,
    )
    second.context.notes = json.dumps(notes)
    with pytest.raises(ValueError, match="request_invalid"):
        ai.run(*args, request=second)
    assert len(sent) == 1
    notes["input_history"][0]["observation_digest"] = first.observation.digest()
    second.context.notes = json.dumps(notes)
    monkeypatch.setattr(
        ai,
        "_provider_call",
        lambda body: (
            sent.append(body),
            response('{"subgoals":["Read current source"]}'),
        )[1],
    )
    assert ai.run(*args, request=second)["effect_proposals"] == []
    assert len(sent) == 2


@pytest.mark.parametrize(
    "surface",
    [
        "fact",
        "hash_fact",
        "label",
        "field_type",
        "prior_fact",
        "input_label",
        "location",
        "invocation",
        "unknown_namespaced_invocation",
        "opaque",
        "unknown_scope",
        "memory",
        "context",
        "control_label",
        "secret",
    ],
)
def test_metadata_qualification_never_exempts_content_or_opaque_notes(
    ai_order, surface
):
    svc, caller, order, ai, sent = ai_order
    req = request_for(order)
    notes = structured_notes_for(req)
    card = "4111111111111111"
    if surface == "fact":
        notes["current"]["source"]["facts"] = [card]
    elif surface == "hash_fact":
        notes["current"]["source"]["facts"] = ["a" * 20 + "1234567890123" + "b" * 31]
    elif surface in {"label", "field_type"}:
        notes["current"]["source"]["fields"][0][
            "label" if surface == "label" else "type"
        ] = card
    elif surface == "prior_fact":
        notes["prior_pages"][0]["source"]["facts"] = [card]
    elif surface == "input_label":
        notes["input_history"][0]["source"]["fields"][0]["label"] = card
    elif surface == "location":
        req.observation.location += card
        req.current_load.location = req.current_load.response.url = (
            req.observation.location
        )
        req.current_load.observation_digest = req.observation.digest()
        notes = structured_notes_for(req)
    elif surface == "invocation":
        notes["input_history"][0]["invocation_id"] = card
    elif surface == "unknown_namespaced_invocation":
        notes["input_history"][0]["invocation_id"] = (
            "bai_" + notes["current"]["session_id"] + "_a0"
        )
    elif surface == "opaque":
        notes = card
    elif surface == "unknown_scope":
        notes = {"scope": "unrecognized", "session_id": notes["current"]["session_id"]}
    elif surface == "memory":
        req.context.memory = card
    elif surface == "context":
        req.observation.context = card
        req.current_load.observation_digest = req.observation.digest()
        notes = structured_notes_for(req)
    elif surface == "control_label":
        req.observation.controls[0].label = card
        req.current_load.observation_digest = req.observation.digest()
        notes = structured_notes_for(req)
    else:
        notes["prior_pages"][0]["source"]["facts"] = ["sk-" + "a" * 20]
    req.context.notes = notes if isinstance(notes, str) else json.dumps(notes)
    with pytest.raises(ValueError, match="request_invalid"):
        ai.run("v_one", caller, order["id"], request=req)
    assert (
        sent == []
        and svc.retrieve("v_one", caller, order["id"]).get("invocations", {}) == {}
    )


@pytest.mark.parametrize(
    "change",
    [
        "version",
        "boolean_version",
        "extra",
        "raw_value",
        "current_session",
        "current_page",
        "current_document",
        "current_digest",
        "current_sequence",
        "history_session",
        "history_id_format",
        "history_origin",
        "input_session",
        "input_delivery",
        "input_acceptance",
        "boolean_sequence",
        "boolean_omission",
        "out_of_range_reviewed_index",
    ],
)
def test_structured_metadata_must_be_closed_typed_and_current_before_admission(
    ai_order, change
):
    svc, caller, order, ai, sent = ai_order
    req = request_for(order)
    notes = structured_notes_for(req)
    if change in {"version", "boolean_version"}:
        notes["schema_version"] = 2 if change == "version" else True
    elif change == "extra":
        notes["unreviewed"] = "a" * 64
    elif change == "raw_value":
        notes["current"]["source"]["fields"][0]["value"] = "Unreviewed content"
    elif change.startswith("current_"):
        key = change.removeprefix("current_")
        key = {
            "session": "session_id",
            "page": "page_id",
            "document": "document_id",
            "digest": "observation_digest",
        }.get(key, key)
        notes["current"][key] = (
            2
            if key == "sequence"
            else "e" * (64 if key == "observation_digest" else 32)
        )
    elif change == "history_session":
        notes["prior_pages"][0]["session_id"] = "e" * 32
    elif change == "history_id_format":
        notes["prior_pages"][0]["document_id"] = "-" * 32
    elif change == "history_origin":
        notes["prior_pages"][0]["location"] = "https://foreign.test/"
    elif change == "input_session":
        notes["input_history"][0]["session_id"] = "e" * 32
    elif change in {"input_delivery", "input_acceptance"}:
        notes["input_history"][0][change.removeprefix("input_")] = "accepted"
    elif change == "boolean_sequence":
        notes["current"]["sequence"] = True
    elif change == "boolean_omission":
        notes["omitted"]["pages"] = True
    else:
        notes["current"]["source"]["fields"][0]["reviewed_value_index"] = 1
    req.context.notes = json.dumps(notes)
    with pytest.raises(ValueError, match="request_invalid"):
        ai.run("v_one", caller, order["id"], request=req)
    assert (
        sent == []
        and svc.retrieve("v_one", caller, order["id"]).get("invocations", {}) == {}
    )
