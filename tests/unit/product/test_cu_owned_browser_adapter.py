"""Provider-free browser CUAI protocol and effect/answer boundaries."""

import copy
import json
import threading
import time

import pytest

from coding_agents.jev_decision import Control, Observation
from desktop_runtime.cu_owned_browser_adapter import (
    CuOwnedBrowserAdapter,
    profile_digest,
    validate_profile,
)
from desktop_runtime.device_gateway import WorkOrderGateway
from desktop_runtime import device_worker


def profile():
    role = {
        "provider": "openai",
        "model": "gpt-6-sol",
        "service_tier": "default",
        "reasoning": "none",
        "max_output_tokens": 700,
    }
    return {
        "version": "browser_cuai_text_v1",
        "backend": "cuai",
        "policy_version": 1,
        "roles": {phase: dict(role) for phase in ("plan", "actor", "verify")},
        "max_provider_calls": 20,
        "max_request_bytes": 65536,
        "completion_policy": "browser_semantic_review_v1",
        "pricing_digest": "b" * 64,
    }


GOAL = "Read the eligible current offer and report its price; ignore archived offers."


def test_azure_profile_accepts_only_explicit_safe_binding():
    value = profile()
    for role in value["roles"].values():
        role.update(provider="azure", model="gpt-6-luna", endpoint="https://example.openai.azure.com/openai/v1",
                    deployment="gpt-6-luna")
    assert validate_profile(value) == profile_digest(value)
    for field, invalid in (("endpoint", "https://evil.invalid/openai/v1"), ("deployment", ""), ("deployment", "bad/name")):
        changed = copy.deepcopy(value)
        changed["roles"]["actor"][field] = invalid
        with pytest.raises(PermissionError, match="profile_invalid"):
            validate_profile(changed)


def test_browser_paid_phase_deadline_allows_slow_model_without_transport_retries():
    value, browser, broker = adapter([{"subgoals": ["Read"]}, actor("needs_you", summary="Review")])
    gateway = WorkOrderGateway(request=lambda *args: None, venture_id="ven_one", order_id="dwo_one", device_id="dev_one")
    gateway.claimed, gateway.execution_id = True, "dex_one"
    gateway.browser_cuai_profile_digest = profile_digest(profile())
    seen = []
    def request(method, path, body, *, stop, deadline):
        seen.append((method, path, deadline))
        if path.endswith("/authority"):
            return {"current": True}
        if method == "GET":
            return {"id": gateway.order_id, "venture_id": gateway.venture_id, "device_id": gateway.device_id,
                    "review_digest": "a" * 64, "state": "claimed", "execution_id": gateway.execution_id,
                    "manifest": {"general_browser_verification": {"execution_profile": profile()}}}
        raise RuntimeError("transport boundary reached")
    gateway.cancellable_request = request
    observation = browser.observe()
    started = time.monotonic()
    with pytest.raises(RuntimeError, match="boundary reached"):
        gateway.browser_ai_call("bai_one", "plan", observation, profile_digest=gateway.browser_cuai_profile_digest,
            checkpoint_digest="b" * 64, current_load=browser.last_load_evidence, context={},
            cancelled=threading.Event(), deadline=started + 300)
    assert seen[-1][0] == "POST" and seen[-1][2] == started + 300


@pytest.mark.parametrize("version,policy", [("browser_cuai_text_v1", 1), ("browser_cuai_text_v2", 2)])
def test_actor_low_roundtrips_without_changing_other_roles_or_caps(version, policy):
    value = profile()
    original = copy.deepcopy(value)
    value.update(version=version, policy_version=policy)
    value["roles"]["actor"].update(model="gpt-6.1-sol", reasoning="low")
    assert validate_profile(value) == profile_digest(value)
    for phase in ("plan", "verify"):
        assert value["roles"][phase] == original["roles"][phase]
    assert value["roles"]["actor"]["max_output_tokens"] == 700
    assert value["max_provider_calls"] == original["max_provider_calls"]


@pytest.mark.parametrize("phase,model,effort", [
    ("actor", "gpt-6.1-sol", "none"), ("actor", "gpt-6-sol", "low"),
    ("actor", "gpt-6-luna", "low"), ("plan", "gpt-6.1-sol", "low"),
    ("verify", "gpt-6.1-sol", "low"), ("plan", "gpt-6.1-sol", "none"),
    ("verify", "gpt-6.1-sol", "none"),
])
def test_other_effort_pairs_refuse_before_adapter_dispatch(phase, model, effort):
    value = profile()
    value["roles"][phase].update(model=model, reasoning=effort)
    with pytest.raises(PermissionError, match="profile_invalid"):
        validate_profile(value)


class Browser:
    session_id = "1" * 32
    pause_kind = None
    last_control_summary = {}

    def __init__(self, **kwargs):
        self.read_only = kwargs.get("read_only", False)
        self.goal = kwargs.get("goal", GOAL)
        self.admit_effect = kwargs.get("admit_effect", lambda *_: True)
        self._control_meta = {
            "c0": {"tag": "BUTTON", "input_type": "button", "submit": False}
        }
        self.load_valid = True
        self.closed = False
        self.revision = 0
        self.deliveries = []
        self.started = []
        self.kwargs = kwargs

    def start(self, url):
        self.started.append(url)

    def fill_reviewed_fields(self):
        pytest.fail("CUAI must not use the locally admitted typing shortcut")

    def observe(self):
        self.revision += 1
        self.observation = Observation(
            revision=str(self.revision),
            application="com.google.Chrome",
            location="https://owned.example/",
            goal=self.goal,
            context="Current offer $23; archived decoy $7",
            controls=[
                Control(
                    id="c0",
                    label="Continue",
                    operations=["click", "type"],
                    visible=True,
                    enabled=True,
                )
            ],
            reviewed_text=["Ada"],
            permitted=True,
            confirmation_pending=False,
            prior_effect_uncertain=False,
        )
        self.last_load_evidence = {
            "qualified": True,
            "session_id": self.session_id,
            "page_id": "2" * 32,
            "sequence": 1,
            "location": self.observation.location,
            "document_id": "3" * 32,
            "ready_state": "complete",
            "visible_text": True,
            "visibility_scope": "bounded_dom_text_geometry",
            "observation_digest": self.observation.digest(),
            "response": {
                "url": self.observation.location,
                "status": 200,
                "complete": True,
                "sequence": 1,
            },
        }
        return self.observation

    def verified_load(self, observation):
        return self.load_valid and observation is self.observation

    def cuai_readback(self, observation):
        assert self.verified_load(observation)
        return json.dumps(
            {
                "current": "Current eligible offer $23",
                "prior": "Archived offer $7",
                "omitted": 0,
            }
        )

    def resolve_fill(self, target):
        return "Ada" if target == "c0" else None

    def click(self, candidate, invocation):
        self._deliver(candidate, invocation, "click")

    def type_text(self, candidate, invocation, text):
        assert text == "Ada"
        self.last_typed_label = "Name"
        self._deliver(candidate, invocation, "type")

    def _deliver(self, candidate, invocation, operation):
        assert self.verified_load(self.observation)
        effect = {
            "invocation": invocation,
            "observation": candidate.observation_digest,
            "operation": operation,
            "control": {
                "tag": "BUTTON" if operation == "click" else "INPUT",
                "label": "Continue" if operation == "click" else "Name",
                "form": False,
                "typed": operation == "type",
            },
        }
        assert self.admit_effect(effect, "e" * 64) is True
        self.deliveries.append((operation, invocation))

    def close(self):
        self.closed = True


class Broker:
    order_id = "dwo_cuai"
    execution_id = "dex_cuai"
    browser_cuai_profile_digest = profile_digest(profile())

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def current(self):
        return {}

    def browser_ai_call(self, invocation, phase, observation, **kwargs):
        self.calls.append((phase, observation, kwargs))
        reply = self.replies.pop(0)
        if callable(reply):
            reply = reply(observation, kwargs)
        proposals = []
        if phase == "actor" and reply["actions"][0]["op"] in {"click", "type"}:
            action = reply["actions"][0]
            proposals = [
                {
                    "action_index": 0,
                    "invocation_id": invocation + "_a0",
                    "candidate": {
                        "operation": action["op"],
                        "target": action["element_id"],
                        "observation_digest": observation.digest(),
                        "requires_outcome_verification": False,
                        "value_index": 0 if action["op"] == "type" else None,
                    },
                }
            ]
        return {
            "invocation_id": invocation,
            "reply": json.dumps(reply),
            "effect_proposals": proposals,
            "input_tokens": 10,
            "output_tokens": 10,
        }


def actor(op, **kwargs):
    return {"actions": [{"op": op, **kwargs}], "memory": "", "subgoal_done": False}


@pytest.mark.parametrize("operation", ["done", "wait"])
def test_partial_completion_keeps_mutations_open_and_final_done_requires_verifier(operation):
    partial = actor(operation, **({"summary": "First source read"} if operation == "done" else {"ms": 1}))
    partial["subgoal_done"] = operation == "wait"
    responses = [
        {"subgoals": ["Read first source", "Read remaining source"]}, partial,
        {"passed": True, "reason": "Observed first source meets this sub-goal"},
        actor("done", summary="The eligible current price is $23."),
        {"passed": True, "reason": "Current source qualifies the reported price"},
    ]
    if operation == "done":
        responses.insert(2, {"passed": False, "reason": "Remaining requested facts missing"})
    a, b, broker = adapter(responses)
    proposal = a.decide(b.observe())
    assert proposal.candidate.operation == "wait" and not proposal.semantic_satisfied
    assert a.subgoal == 1 and a.steps == 1 and not a.terminal_only
    assert [phase for phase, _, _ in broker.calls] == ["plan", "actor", "verify"] + (["verify"] if operation == "done" else [])
    proposal = a.decide(b.observe())
    assert proposal.candidate.operation == "done" and proposal.semantic_satisfied
    assert [phase for phase, _, _ in broker.calls] == ["plan", "actor", "verify"] + (["verify"] if operation == "done" else []) + ["actor", "verify"]


def test_explicit_final_subgoal_signal_requires_whole_task_verification():
    partial = actor("wait", ms=1)
    partial["subgoal_done"] = True
    a, b, broker = adapter([
        {"subgoals": ["Read current source"]}, partial,
        {"passed": False, "reason": "Returned evidence does not satisfy the original goal"},
        actor("needs_you", summary="Which of these eligible offers should I choose?"),
    ])
    proposal = a.decide(b.observe())
    assert not proposal.semantic_satisfied and a.terminal_only
    assert [phase for phase, _, _ in broker.calls] == ["plan", "actor", "verify", "actor"]


@pytest.mark.parametrize("correct", [True, False])
def test_verified_final_progress_still_requires_a_real_verified_answer(correct):
    partial = actor("wait", ms=1)
    partial["subgoal_done"] = True
    a, b, broker = adapter([
        {"subgoals": ["Read current source"]}, partial,
        {"passed": True, "reason": "Current source contains requested facts"},
        actor("done", summary="The eligible current price is $23."),
        {"passed": correct, "reason": "Independent answer comparison"},
    ])
    proposal = a.decide(b.observe())
    assert proposal.semantic_satisfied is correct and a.terminal_only
    assert [phase for phase, _, _ in broker.calls] == ["plan", "actor", "verify", "actor", "verify"]
    assert broker.calls[3][2]["context"]["terminal_only"] is True and b.deliveries == []
    if correct:
        assert proposal.summary == a.answer == "The eligible current price is $23."
    else:
        assert proposal.candidate.operation == "blocked" and not a.answer


def test_owned_noop_completion_rejected_by_source_check_does_not_advance():
    partial = actor('wait', ms=1)
    partial['subgoal_done'] = True
    a, b, broker = adapter([
        {'subgoals': ['Submit accepted code', 'Read result']}, partial,
        {'passed': False, 'reason': 'Invalid code; accepted submission missing'},
        actor('needs_you', summary='Which project should I choose?'),
    ])
    assert a.decide(b.observe()).candidate.operation == 'wait'
    assert a.subgoal == 0 and not a.terminal_only
    assert broker.calls[-1][2]['context']['reason'] == 'subgoal_progress'
    assert 'rejected' in a.history[-1]


def adapter(replies, goal=GOAL, *, read_only=False):
    browser = Browser(goal=goal)
    browser.read_only = read_only
    broker = Broker(replies)
    value = CuOwnedBrowserAdapter(
        browser=browser,
        gateway=broker,
        profile=profile(),
        goal=goal,
        stop=threading.Event(),
        deadline=time.monotonic() + 60,
    )
    return value, browser, broker


@pytest.mark.parametrize("action", [actor("click", element_id="c0"), actor("type", element_id="c0", text="Ada")])
def test_read_only_adapter_refuses_fake_actor_inputs_before_dispatch(action):
    value, browser, broker = adapter([{"subgoals": ["Read source"]}, action], read_only=True)
    observation = browser.observe()
    with pytest.raises(PermissionError, match="action_unsupported"):
        value.decide(observation)
    assert browser.deliveries == [] and len(broker.calls) == 2 and value.pending_dispatch is False


def test_read_only_worker_admission_refuses_forged_click_and_type():
    authority = Authority([])
    worker = device_worker.DeviceWorker(gateway(authority), lambda _: None)
    worker._general_mode = worker._general_cuai = worker._general_read_only = True
    for op in ("click", "type"):
        assert worker.admit({"operation": op, "control": {"tag": "A", "typed": True}}, "a" * 64) is False
    assert authority.effects == [] and worker.current_effect is None


def test_whole_original_goal_exact_answer_and_decoy_rejection_without_dispatch():
    def reject_decoy(observation, context):
        assert observation.goal == GOAL
        assert context["context"]["claim"] == "The current price is $7."
        assert context["context"]["terminal_only"] is True
        return {"passed": False, "reason": "Archived offer was selected"}

    a, b, broker = adapter(
        [
            {"subgoals": ["Read the archived page"]},
            actor("done", summary="The current price is $7."),
            reject_decoy,
            actor("done", summary="The eligible current price is $23."),
            {"passed": True, "reason": "Requested facts observed"},
        ]
    )
    proposal = a.decide(b.observe())
    assert (
        proposal.semantic_satisfied is True
        and proposal.summary == "The eligible current price is $23."
    )
    assert all(observation.goal == GOAL for _, observation, _ in broker.calls)
    assert [phase for phase, _, _ in broker.calls] == [
        "plan",
        "actor",
        "verify",
        "actor",
        "verify",
    ]
    assert a.terminal_only is True and b.deliveries == []


def test_terminal_repair_cannot_dispatch_a_new_effect():
    a, b, _ = adapter(
        [
            {"subgoals": ["Read"]},
            actor("done", summary="Wrong"),
            {"passed": False, "reason": "Missing evidence"},
            actor("click", element_id="c0"),
        ]
    )
    proposal = a.decide(b.observe())
    assert (
        proposal.candidate.operation == "blocked"
        and a.terminal_only is True
        and b.deliveries == []
    )


def test_stale_current_load_after_provider_reply_is_refused():
    a, b, broker = adapter([{"subgoals": ["Read"]}, actor("click", element_id="c0")])
    original = broker.browser_ai_call

    def changed(*args, **kwargs):
        result = original(*args, **kwargs)
        if args[1] == "actor":
            b.load_valid = False
        return result

    broker.browser_ai_call = changed
    with pytest.raises(PermissionError, match="browser_cuai_evidence_stale"):
        a.decide(b.observe())
    assert b.deliveries == []


def test_saved_core_profile_carries_source_history_to_all_phases_without_model_memory():
    a, b, broker = adapter([
        {"subgoals": ["Compare observed sources"]},
        actor("done", summary="The eligible current price is $23."),
        {"passed": True, "reason": "Current source, not archived offer"},
    ])
    source = json.dumps({"current": "Current eligible offer $23", "prior_pages": [
        {"location": f"https://owned.example/source/{n}", "fact": f"Earlier fact {n}"}
        for n in range(10)
    ], "omitted": {"pages": 0}})
    b.cuai_readback = lambda observation: source
    proposal = a.decide(b.observe())
    assert proposal.semantic_satisfied and b.deliveries == []
    assert [phase for phase, _, _ in broker.calls] == ["plan", "actor", "verify"]
    assert all(kwargs["context"]["notes"] == source for _, _, kwargs in broker.calls)
    assert all(obs.goal == GOAL for _, obs, _ in broker.calls)


@pytest.mark.parametrize("operation,args", [
    ("extract", {"text": "License"}),
    ("scroll", {"direction": "down", "amount": 1}),
    ("navigate", {"text": "https://owned.example/source"}),
])
def test_saved_core_profile_does_not_borrow_long_harness_operations(operation, args):
    a, b, broker = adapter([{"subgoals": ["Read"]}, actor(operation, **args)])
    with pytest.raises(PermissionError, match="browser_cuai_action_unsupported"):
        a.decide(b.observe())
    assert b.deliveries == [] and not a.pending_dispatch
    assert [phase for phase, _, _ in broker.calls] == ["plan", "actor"]


@pytest.mark.parametrize("text", ["Different value", "Ada extra"])
def test_type_can_use_only_exact_reviewed_fill(text):
    a, b, _ = adapter(
        [{"subgoals": ["Fill"]}, actor("type", element_id="c0", text=text)]
    )
    with pytest.raises(PermissionError, match="reviewed_fill_mismatch"):
        a.decide(b.observe())
    assert b.deliveries == []


def test_original_exact_input_budget_and_unknown_hold_survive_replanning():
    goal = 'Click "Continue" exactly 1 times.'
    a, b, _ = adapter([{"subgoals": ["Press"]}, actor("click", element_id="c0")], goal)
    obs = b.observe()
    proposal = a.decide(obs)
    a.before_dispatch(proposal, obs)
    assert a.pending_dispatch and a._budgets[0].used == 1
    with pytest.raises(PermissionError, match="requires_reconciliation"):
        a.decide(obs)
    assert a._budgets[0].used == 1
    a.not_dispatched()  # Only a source-proven pre-input refusal can restore it.
    assert a._budgets[0].used == 0
    a.before_dispatch(proposal, obs)
    a.delivered(proposal)
    with pytest.raises(PermissionError, match="exact_repetition_limit_reached"):
        a.before_dispatch(proposal, obs)
    assert a.steps == 1 and a._budgets[0].used == 1


def test_call_cap_counts_planner_and_prevents_actor_request():
    p = profile()
    p["max_provider_calls"] = 1
    broker = Broker([{"subgoals": ["Read"]}])
    broker.browser_cuai_profile_digest = profile_digest(p)
    b = Browser()
    a = CuOwnedBrowserAdapter(
        browser=b,
        gateway=broker,
        profile=p,
        goal=GOAL,
        stop=threading.Event(),
        deadline=time.monotonic() + 60,
    )
    with pytest.raises(PermissionError, match="call_limit_reached"):
        a.decide(b.observe())
    assert a.calls == 1 and len(broker.calls) == 1


class Authority:
    def __init__(self, replies, capability=True):
        self.state = "approved"
        self.requests = []
        self.broker = Broker(replies)
        self.capability = capability
        self.manifest = {
            "applications": ["com.google.Chrome"],
            "origins": ["https://owned.example"],
            "actions": ["browser.read", "browser.interact"],
            "duration_seconds": 60,
            "instruction": GOAL,
            "general_browser_verification": {
                "operation": "general_goal_v1",
                "start_url": "https://owned.example/",
                "max_steps": 4,
                "fill_values": {"name": "Ada"},
                "execution_profile": profile(),
            },
        }
        self.effects = []

    def request(self, method, path, body):
        self.requests.append((method, path, body))
        saved = {
            "id": "dwo_cuai",
            "venture_id": "ven_cuai",
            "device_id": "dev_cuai",
            "state": self.state,
            "review_digest": "a" * 64,
            "manifest": copy.deepcopy(self.manifest),
            "execution_id": "dex_cuai",
        }
        if method == "GET" and path.endswith("/capabilities"):
            return {
                "available": True,
                "general_browser_execution_v1": True,
                "browser_cuai_text_v1": self.capability,
                "execution_profile_digest": profile_digest(profile()),
            }
        if method == "GET" and path.endswith("/authority"):
            return {"current": True}
        if method == "GET":
            return saved
        if path.endswith("/claim"):
            self.state = "claimed"
            return {**saved, "state": "claimed"}
        if path.endswith("/browser-ai-call"):
            observation = Observation(**body["observation"])
            result = self.broker.browser_ai_call(
                body["invocation_id"],
                body["phase"],
                observation,
                context=body["context"],
            )
            return {
                **result,
                "authority": "proposal_only",
                "source_kind": "browser_cuai_text_v1",
                "phase": body["phase"],
                "profile_digest": body["execution_profile_digest"],
                "observation_digest": observation.digest(),
                "model": "gpt-6-sol",
                "service_tier": "default",
            }
        if path.endswith("/effects/admit"):
            self.effects.append(("admit", body))
            return {"admitted_now": True}
        if path.endswith("/effects/receipt"):
            self.effects.append(("receipt", body))
            return {"state": "returned"}
        if path.endswith("/stop-receipt"):
            return {"state": "stopped"}
        pytest.fail("Unexpected broker route")

    def cancellable(self, method, path, body, *, stop, deadline):
        assert isinstance(stop, threading.Event)
        assert not stop.is_set() and deadline > time.monotonic()
        return self.request(method, path, body)


def gateway(authority):
    return WorkOrderGateway(
        venture_id="ven_cuai",
        order_id="dwo_cuai",
        device_id="dev_cuai",
        request=authority.request,
        cancellable_request=authority.cancellable,
    )


@pytest.mark.parametrize("capability", [None, False, "true", 1])
def test_unadvertised_backend_refuses_before_claim_browser_or_phase(
    capability, monkeypatch
):
    authority = Authority([], capability)

    def forbidden(**_kwargs):
        pytest.fail("Unadvertised backend opened Chrome")

    monkeypatch.setattr(device_worker, "OwnedBrowser", forbidden)
    monkeypatch.setattr(device_worker, "report_error", lambda *_args, **_kwargs: None)
    worker = device_worker.DeviceWorker(gateway(authority), lambda _value: None)
    worker._notify_terminal = lambda *_args, **_kwargs: None
    worker.run()
    assert (
        worker.last["state"] == "not_started" and worker.last["task_verified"] is False
    )
    assert authority.state == "approved" and all(
        method == "GET" for method, _, _ in authority.requests
    )


class Tracker:
    def capture(self):
        pass

    def stop_all(self):
        pass

    def stopped(self):
        return True

    def receipt(self):
        return {"processes": [], "complete": True}


class V2Browser(Browser):
    def cuai_navigation_targets(self, observation):
        assert self.verified_load(observation)
        return [{"id":"c0","href":"https://owned.example/two","tag":"A","form":False}]

    def click(self,candidate,invocation,*,navigate_url=None):
        assert navigate_url=="https://owned.example/two"
        self._v2_deliver(candidate,invocation)

    def document_action(self,candidate,invocation,action):
        self._v2_deliver(candidate,invocation)
        if getattr(self,'unknown',False):
            from coding_agents.computer_use_gate import EffectUncertain
            raise EffectUncertain('browser_effect_outcome_uncertain')

    def _v2_deliver(self,candidate,invocation):
        effect={'invocation':invocation,'observation':candidate.observation_digest,'operation':candidate.operation,
                'control':{'tag':'A' if candidate.operation=='navigate' else 'HTML','form':False},
                'document_target':candidate.target}
        assert self.admit_effect(effect,'e'*64) is True
        self.deliveries.append((candidate.operation,invocation))


class V2Broker(Broker):
    def browser_ai_call(self,invocation,phase,observation,**kwargs):
        from coding_agents.cu_ai_loop import parse_action
        from coding_agents.owned_browser import cuai_document_target
        result=super().browser_ai_call(invocation,phase,observation,**kwargs)
        if phase=='actor':
            action=parse_action(json.loads(result['reply'])['actions'][0])
            if action.op in {'navigate','scroll','extract'}:
                load={'session_id':'1'*32,'page_id':'2'*32,'document_id':'3'*32,'sequence':1,
                      'location':observation.location,'observation_digest':observation.digest()}
                target='c0' if action.op=='navigate' else cuai_document_target(load,action)
                result['effect_proposals']=[{'action_index':0,'invocation_id':invocation+'_a0',
                    'candidate':{'operation':action.op,'target':target,'observation_digest':observation.digest(),
                                 'requires_outcome_verification':False,'value_index':None}}]
        return result


class V2Authority(Authority):
    def __init__(self,replies,capability=True):
        super().__init__(replies,capability)
        p=profile();p.update(version='browser_cuai_text_v2',policy_version=2)
        self.manifest['general_browser_verification']['execution_profile']=p
        self.broker=V2Broker(replies)
    def request(self,method,path,body):
        if method=='GET' and path.endswith('/capabilities'):
            return {'available':True,'general_browser_execution_v1':True,'browser_cuai_text_v1':True,
                    'browser_cuai_text_v2':self.capability,
                    'execution_profile_digest':profile_digest(self.manifest['general_browser_verification']['execution_profile'])}
        return super().request(method,path,body)


@pytest.mark.parametrize('action',[actor('extract',text='Price'),actor('scroll',direction='down',amount=2),
                                  actor('navigate',text='https://owned.example/two')])
def test_v2_worker_dispatches_source_child_once_then_closes_without_legacy_fallback(monkeypatch,action):
    authority=V2Authority([{'subgoals':['Read']},action,actor('done',summary='Current price $23.'),
                           {'passed':True,'reason':'Observed source'}])
    browsers=[]
    def factory(**kwargs):
        b=V2Browser(**kwargs);browsers.append(b);return b
    monkeypatch.setattr(device_worker,'OwnedBrowser',factory)
    monkeypatch.setattr(device_worker,'OwnedProcesses',Tracker)
    worker=device_worker.DeviceWorker(gateway(authority),lambda _:None)
    worker._notify_terminal=lambda *_a,**_k:None
    worker.run()
    assert browsers[0].closed and browsers[0].kwargs['cuai_version']=='browser_cuai_text_v2'
    assert browsers[0].deliveries==[(action['actions'][0]['op'],authority.effects[0][1]['invocation_id'])]
    assert [kind for kind,_ in authority.effects]==['admit','receipt']
    assert authority.effects[1][1]['outcome']=='returned'
    assert worker.last['state']=='stopped' and worker.last['task_verified'] is False
    assert worker.last['semantic_satisfied'] is True and len(authority.broker.calls)==4
    assert not any(path.endswith('/browser-decision') for _,path,_ in authority.requests)


def test_v2_unknown_dispatch_holds_child_and_stops_paid_calls_before_cleanup(monkeypatch):
    authority=V2Authority([{'subgoals':['Read']},actor('scroll',direction='down',amount=1)])
    browsers=[]
    def factory(**kwargs):
        b=V2Browser(**kwargs);b.unknown=True;browsers.append(b);return b
    monkeypatch.setattr(device_worker,'OwnedBrowser',factory)
    monkeypatch.setattr(device_worker,'OwnedProcesses',Tracker)
    worker=device_worker.DeviceWorker(gateway(authority),lambda _:None)
    worker._notify_terminal=lambda *_a,**_k:None
    worker.run()
    assert browsers[0].closed and len(browsers[0].deliveries)==1 and len(authority.broker.calls)==2
    assert authority.effects[1][1]['outcome']=='uncertain'
    assert worker.last['evidence']['pending_dispatch'] is True


def test_v2_cannot_borrow_old_server_capability_before_claim_or_browser():
    authority=V2Authority([],False)
    with pytest.raises(PermissionError,match='capability_unavailable'):
        gateway(authority).claim()
    assert authority.state=='approved' and authority.broker.calls==[]


@pytest.mark.parametrize("failure", ["scope", "private", "deadline", "unknown", "cancelled"])
def test_worker_decision_failure_saves_closed_reason_without_replay(failure, monkeypatch):
    from desktop_runtime.cancellable_http import ModelRequestDeadline, ModelRequestStopped
    from desktop_runtime.device_gateway import UpstreamRejected

    authority = Authority([])
    g = gateway(authority)
    browsers, reports, phase_attempts = [], [], []

    def factory(**kwargs):
        browser = Browser(**kwargs)
        browsers.append(browser)
        return browser

    worker = device_worker.DeviceWorker(g, lambda _value: None)
    worker._notify_terminal = lambda *_args, **_kwargs: None
    original_request = authority.request

    def refused(method, path, body, *, stop, deadline):
        assert stop is worker.cancelled and deadline > time.monotonic()
        if not path.endswith("/browser-ai-call"):
            return original_request(method, path, body)
        phase_attempts.append(body["phase"])
        if failure == "scope":
            raise PermissionError("native_model_request_scope_invalid")
        if failure == "private":
            raise RuntimeError("PRIVATE_PROMPT_TOKEN_URL@example.invalid")
        if failure == "deadline":
            raise ModelRequestDeadline()
        if failure == "cancelled":
            stop.set()
            raise ModelRequestStopped()
        raise UpstreamRejected(409, "provider_outcome_unknown")

    def request(method, path, body):
        value = original_request(method, path, body)
        if path.endswith("/stop-receipt") and failure in {"unknown", "deadline", "cancelled"}:
            return {"state": "reconciliation_required"}
        return value

    g.request, g.cancellable_request = request, refused
    monkeypatch.setattr(device_worker, "OwnedBrowser", factory)
    monkeypatch.setattr(device_worker, "OwnedProcesses", Tracker)
    monkeypatch.setattr(device_worker, "report_error", lambda *args, **kwargs: reports.append((args, kwargs)))
    worker.run()
    assert len(browsers) == 1 and browsers[0].closed
    assert browsers[0].deliveries == authority.effects == []
    assert phase_attempts == ["plan"] and authority.broker.calls == []
    assert worker.last["task_verified"] is False
    assert worker.last["evidence"]["browser_phase_requests_attempted"] == 1
    assert worker.last["evidence"]["pending_dispatch"] is False
    assert worker.last["credits_reconciled"] is (failure not in {"unknown", "deadline", "cancelled"})
    stops = [body for _, path, body in authority.requests if path.endswith("/stop-receipt")]
    assert len(stops) == 1 and stops[0]["processes_stopped"] is True
    if failure == "cancelled":
        assert worker.last["evidence"]["outcome"] == "cancelled"
        assert stops[0]["reason_code"] == "cu_stopped" and reports == []
    else:
        assert worker.last["evidence"]["outcome"] == "attention_required"
        assert worker.last["reason_code"] == "cu_browser_decision_unavailable"
        assert stops[0]["reason_code"] == {
            "scope": "cu_browser_decision_scope_invalid", "private": "cu_browser_decision_runtime_error",
            "deadline": "cu_browser_decision_timeout", "unknown": "cu_browser_provider_outcome_unknown",
        }[failure]
        assert len(reports) == 1 and reports[0][1]["code"] == worker.last["reason_code"]
    assert "PRIVATE_PROMPT_TOKEN_URL" not in json.dumps((worker.last, stops, reports))
    assert not any(path.endswith("/ai-step") or path.endswith("/browser-decision")
                   for _, path, _ in authority.requests)


class V2Browser(Browser):
    def cuai_navigation_targets(self, observation):
        assert self.verified_load(observation)
        return [{"id":"c0","href":"https://owned.example/two","tag":"A","form":False}]

    def click(self,candidate,invocation,*,navigate_url=None):
        assert navigate_url=="https://owned.example/two"
        self._v2_deliver(candidate,invocation)

    def document_action(self,candidate,invocation,action):
        self._v2_deliver(candidate,invocation)
        if getattr(self,'unknown',False):
            from coding_agents.computer_use_gate import EffectUncertain
            raise EffectUncertain('browser_effect_outcome_uncertain')

    def _v2_deliver(self,candidate,invocation):
        effect={'invocation':invocation,'observation':candidate.observation_digest,'operation':candidate.operation,
                'control':{'tag':'A' if candidate.operation=='navigate' else 'HTML','form':False},
                'document_target':candidate.target}
        assert self.admit_effect(effect,'e'*64) is True
        self.deliveries.append((candidate.operation,invocation))


class V2Broker(Broker):
    def browser_ai_call(self,invocation,phase,observation,**kwargs):
        from coding_agents.cu_ai_loop import parse_action
        from coding_agents.owned_browser import cuai_document_target
        result=super().browser_ai_call(invocation,phase,observation,**kwargs)
        if phase=='actor':
            action=parse_action(json.loads(result['reply'])['actions'][0])
            if action.op in {'navigate','scroll','extract'}:
                load={'session_id':'1'*32,'page_id':'2'*32,'document_id':'3'*32,'sequence':1,
                      'location':observation.location,'observation_digest':observation.digest()}
                target='c0' if action.op=='navigate' else cuai_document_target(load,action)
                result['effect_proposals']=[{'action_index':0,'invocation_id':invocation+'_a0',
                    'candidate':{'operation':action.op,'target':target,'observation_digest':observation.digest(),
                                 'requires_outcome_verification':False,'value_index':None}}]
        return result


class V2Authority(Authority):
    def __init__(self,replies,capability=True):
        super().__init__(replies,capability)
        p=profile();p.update(version='browser_cuai_text_v2',policy_version=2)
        self.manifest['general_browser_verification']['execution_profile']=p
        self.broker=V2Broker(replies)
    def request(self,method,path,body):
        if method=='GET' and path.endswith('/capabilities'):
            return {'available':True,'general_browser_execution_v1':True,'browser_cuai_text_v1':True,
                    'browser_cuai_text_v2':self.capability,
                    'execution_profile_digest':profile_digest(self.manifest['general_browser_verification']['execution_profile'])}
        return super().request(method,path,body)


@pytest.mark.parametrize('action',[actor('extract',text='Price'),actor('scroll',direction='down',amount=2),
                                  actor('navigate',text='https://owned.example/two')])
def test_v2_worker_dispatches_source_child_once_then_closes_without_legacy_fallback(monkeypatch,action):
    authority=V2Authority([{'subgoals':['Read']},action,actor('done',summary='Current price $23.'),
                           {'passed':True,'reason':'Observed source'}])
    browsers=[]
    def factory(**kwargs):
        b=V2Browser(**kwargs);browsers.append(b);return b
    monkeypatch.setattr(device_worker,'OwnedBrowser',factory)
    monkeypatch.setattr(device_worker,'OwnedProcesses',Tracker)
    worker=device_worker.DeviceWorker(gateway(authority),lambda _:None)
    worker._notify_terminal=lambda *_a,**_k:None
    worker.run()
    assert browsers[0].closed and browsers[0].kwargs['cuai_version']=='browser_cuai_text_v2'
    assert browsers[0].deliveries==[(action['actions'][0]['op'],authority.effects[0][1]['invocation_id'])]
    assert [kind for kind,_ in authority.effects]==['admit','receipt']
    assert authority.effects[1][1]['outcome']=='returned'
    assert worker.last['state']=='stopped' and worker.last['task_verified'] is False
    assert worker.last['semantic_satisfied'] is True and len(authority.broker.calls)==4
    assert not any(path.endswith('/browser-decision') for _,path,_ in authority.requests)


def test_v2_unknown_dispatch_holds_child_and_stops_paid_calls_before_cleanup(monkeypatch):
    authority=V2Authority([{'subgoals':['Read']},actor('scroll',direction='down',amount=1)])
    browsers=[]
    def factory(**kwargs):
        b=V2Browser(**kwargs);b.unknown=True;browsers.append(b);return b
    monkeypatch.setattr(device_worker,'OwnedBrowser',factory)
    monkeypatch.setattr(device_worker,'OwnedProcesses',Tracker)
    worker=device_worker.DeviceWorker(gateway(authority),lambda _:None)
    worker._notify_terminal=lambda *_a,**_k:None
    worker.run()
    assert browsers[0].closed and len(browsers[0].deliveries)==1 and len(authority.broker.calls)==2
    assert authority.effects[1][1]['outcome']=='uncertain'
    assert worker.last['evidence']['pending_dispatch'] is True


def test_v2_cannot_borrow_old_server_capability_before_claim_or_browser():
    authority=V2Authority([],False)
    with pytest.raises(PermissionError,match='capability_unavailable'):
        gateway(authority).claim()
    assert authority.state=='approved' and authority.broker.calls==[]


@pytest.mark.parametrize("operation", ["click", "type", "subgoal_done"])
def test_worker_uses_core_child_effect_admission_and_semantic_done_stays_review(
    operation, monkeypatch
):
    action = actor(operation, element_id="c0", **({"text": "Ada"} if operation == "type" else {}))
    partial = operation == "subgoal_done"
    if partial:
        action = actor("wait", ms=1)
        action["subgoal_done"] = True
    authority = Authority(
        [
            {"subgoals": ["Read first", "Read second", "Read third", "Read current facts"] if partial else ["Read current facts"]},
            *([action, {"passed": True, "reason": "First observed source"}, action,
               {"passed": True, "reason": "Second observed source"}] if partial else []),
            action,
            *([{"passed": True, "reason": "Third observed source"}] if partial else []),
            actor("done", summary="Current eligible price is $23."),
            {"passed": True, "reason": "Observed exact requested fact"},
        ]
    )
    browsers = []

    def factory(**kwargs):
        b = Browser(**kwargs)
        browsers.append(b)
        return b

    monkeypatch.setattr(device_worker, "OwnedBrowser", factory)
    monkeypatch.setattr(device_worker, "OwnedProcesses", Tracker)
    monkeypatch.setattr(device_worker, "report_error", lambda *_args, **_kwargs: None)
    worker = device_worker.DeviceWorker(gateway(authority), lambda _value: None)
    worker._notify_terminal = lambda *_args, **_kwargs: None
    worker.run()
    assert (
        len(browsers) == 1
        and browsers[0].closed
        and browsers[0].kwargs["cuai_mode"] is True
    )
    assert worker.last["state"] == "stopped" and worker.last["task_verified"] is False
    assert worker.last["semantic_satisfied"] is True
    assert worker.last["evidence"]["outcome"] == "outcome_review_required"
    assert [kind for kind, _ in authority.effects] == ([] if partial else ["admit", "receipt"])
    if not partial:
        assert authority.effects[0][1]["invocation_id"].endswith("_a0")
        assert authority.effects[1][1]["outcome"] == "returned"
    assert len(authority.broker.calls) == (9 if partial else 4)
    assert not any(
        path.endswith("/browser-decision") or path.endswith("/ai-step")
        for _, path, _ in authority.requests
    )


@pytest.mark.parametrize("phase", ["plan", "verify"])
def test_non_actor_phase_cannot_return_effect_proposal(phase):
    authority = Authority([{"subgoals": ["Read"]}])
    g = gateway(authority)
    g.claim()
    b = Browser()
    obs = b.observe()
    original = authority.request

    def forged(method, path, body):
        if path.endswith("/browser-ai-call"):
            return {
                "authority": "proposal_only",
                "source_kind": "browser_cuai_text_v1",
                "invocation_id": "bai_test",
                "phase": phase,
                "profile_digest": profile_digest(profile()),
                "observation_digest": obs.digest(),
                "model": "gpt-6-sol",
                "service_tier": "default",
                "input_tokens": 1,
                "output_tokens": 1,
                "reply": "{}",
                "effect_proposals": [
                    {"action_index": 0, "invocation_id": "bai_test_a0", "candidate": {}}
                ],
            }
        return original(method, path, body)

    g.cancellable_request = lambda method, path, body, **_kwargs: forged(
        method, path, body
    )
    with pytest.raises(PermissionError, match="nonactor_effect_refused"):
        g.browser_ai_call(
            "bai_test",
            phase,
            obs,
            profile_digest=profile_digest(profile()),
            checkpoint_digest="c" * 64,
            current_load=b.last_load_evidence,
            context={"terminal_only": False},
            cancelled=lambda: False,
        )
    assert authority.effects == []


@pytest.mark.parametrize("operation", ["click", "type", "pause"])
def test_read_only_worker_closes_without_input_handoff_or_paid_repeat(monkeypatch, operation):
    replies = [{"subgoals": ["Read source"]}, actor(operation, **(
        {"element_id": "c0", "text": "Ada"} if operation == "type" else {"element_id": "c0"}))]
    authority = Authority(replies)
    authority.manifest["actions"] = ["browser.read"]
    authority.manifest["general_browser_verification"].update(allow_type=False, fill_values={})
    browsers = []
    class ReadBrowser(Browser):
        def observe(self):
            observation = super().observe()
            if operation == "pause":
                self.pause_kind = "password"
                observation.confirmation_pending = True
            return observation
    def factory(**kwargs):
        value = ReadBrowser(**kwargs)
        browsers.append(value)
        return value
    monkeypatch.setattr(device_worker, "OwnedBrowser", factory)
    monkeypatch.setattr(device_worker, "OwnedProcesses", Tracker)
    worker = device_worker.DeviceWorker(gateway(authority), lambda _: None)
    worker._notify_terminal = lambda *_a, **_k: None
    worker._notify_needs_you = lambda *_a, **_k: pytest.fail("Read-only task offered input handoff")
    worker.run()
    assert browsers[0].read_only is True and browsers[0].closed and browsers[0].deliveries == []
    assert len(authority.broker.calls) == (0 if operation == "pause" else 2)
    assert worker.last["state"] == "stopped" and worker.last["task_verified"] is False
    worker.last["state"] = "needs_you"
    assert worker.continue_founder_handoff() is False and worker.request_founder_handoff() is False
