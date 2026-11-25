import json
import threading
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from coding_agents.cu_ai_loop import Caps, LoopState
from desktop_runtime import cu_ai_engine as engine, cu_preflight
from desktop_runtime.cu_recovery import Recovery
from desktop_runtime.device_worker import DeviceWorker
from desktop_runtime.peekaboo_driver import AppChoice, PeekabooDriver


@pytest.fixture(params=[("Stock ledger", "com.example.Stock", "Review the stock report", "Stock reviewed"),
                       ("Recipe shelf", "com.example.Recipes", "Review the recipe collection", "Recipes reviewed")])
def domain(request, monkeypatch, tmp_path):
    app, bundle, goal, result = request.param
    monkeypatch.setenv("SUPYMEM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COMPANY_OS_WORKING_MEMORY_PATH", str(tmp_path / "working.sqlite"))
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/company.sqlite")
    monkeypatch.setattr(engine.agent_cursor, "emit", lambda *args, **kwargs: None)
    monkeypatch.setattr("desktop_runtime.local_notices.push_task_finished", lambda **kwargs: None)
    monkeypatch.setattr("desktop_runtime.local_notices.push_needs_you", lambda **kwargs: None)
    return SimpleNamespace(app=app, bundle=bundle, goal=goal, result=result)


def proposal(operation, *, subgoal_done=False, **arguments):
    return {"thought": "", "memory": "", "subgoal_done": subgoal_done,
            "actions": [{"op": operation, **arguments}]}, {}


class Mac:
    def __init__(self, domain, *, changing=False, effect="clicked current control"):
        self.app, self.pid = domain.app, 101
        self.domain, self.changing, self.effect = domain, changing, effect
        self.actions, self.calls, self.observations = [], [], 0
        self.permissions = {"accessibility": True, "screen_recording": True}
        self.windows = [{"pid": 101, "window_id": 300, "is_on_screen": True}]
        self.window_state = {"pid": 101, "window_id": 300, "snapshot_id": "current",
                             "elements": [{"role": "AXButton", "label": "Next"}]}

    def call(self, tool, arguments):
        self.calls.append((tool, arguments))
        return {"check_permissions": self.permissions, "list_windows": {"windows": self.windows},
                "get_window_state": self.window_state}[tool]

    def observe(self):
        self.observations += 1
        png = f"screen-{self.observations}".encode() if self.changing else b"same pixels"
        elements = [{"id": f"e{self.observations}", "ax_role": "AXButton",
                     "label": f"Next {self.observations}" if self.changing else "Next"}]
        if self.changing:
            elements.append({"id": "field", "ax_role": "AXTextField", "value": str(self.observations)})
        return engine.Observation(png, elements,
                                  self.app, self.domain.goal, f"snapshot-{self.observations}", pid=self.pid)

    def frontmost(self):
        return self.app, self.pid

    def focused_role(self):
        return None

    def act(self, action, observation):
        self.actions.append(action)
        return self.effect


def plan(domain, count=1):
    return [{"goal": domain.goal, "postcondition": {"role": "AXStaticText", "label": domain.result}}
            for _ in range(count)]


@pytest.mark.parametrize("permission", ["accessibility", "screen_recording", "automation", "unknown"])
def test_preflight_missing_grants_gives_exact_settings_before_window_access(domain, permission):
    mac = Mac(domain)
    if permission == "unknown":
        mac.permissions = {}
    else:
        mac.permissions[permission] = False
    problems = cu_preflight.check(domain.goal, mac, surfaces=((101, 300),), strict=True)
    expected = {"accessibility": "Accessibility", "screen_recording": "Screen & System Audio Recording",
                "automation": "Automation", "unknown": "Accessibility"}[permission]
    assert expected in " ".join(problems)
    assert [tool for tool, arguments in mac.calls] == ["check_permissions"]
    assert mac.actions == []


@pytest.mark.parametrize("failure", ["minimized", "off_space", "missing", "refused", "empty"])
def test_preflight_unreachable_window_requires_restore_without_foreground_input(domain, failure):
    mac = Mac(domain)
    if failure == "minimized":
        mac.windows[0]["is_minimized"] = True
    elif failure == "off_space":
        mac.windows[0]["is_on_screen"] = False
    elif failure == "missing":
        mac.windows = []
    elif failure == "refused":
        mac.window_state["effect"] = "refused"
    else:
        mac.window_state["elements"] = []
    problems = cu_preflight.check(domain.goal, mac, surfaces=((101, 300),), strict=True)
    assert "restore this window and retry" in " ".join(problems)
    assert all(tool in {"check_permissions", "list_windows", "get_window_state"} for tool, arguments in mac.calls)
    assert mac.actions == []


def test_preflight_checks_only_reviewed_app_automation_and_unknown_is_not_ready(domain):
    mac = Mac(domain)
    mac.app = "Calendar"
    probed = []
    problems = cu_preflight.check(domain.goal + " in Notes", mac, strict=True, surfaces=((101, 300),),
                                 probe=lambda app: probed.append(app) or "unknown")
    assert probed == ["calendar"]
    assert "Automation > Supymem > Calendar" in " ".join(problems)


@pytest.mark.parametrize("failure", ["permissions", "window_identity"])
def test_worker_preflight_never_claims_or_reconciles_unstarted_work(domain, monkeypatch, failure):
    app = AppChoice(domain.app, domain.bundle, 101, "123456")
    monkeypatch.setattr(PeekabooDriver, "running_apps", lambda: [app])
    monkeypatch.setattr(PeekabooDriver, "running_windows", lambda app: [] if failure == "window_identity" else [{"window_id": 300}])
    mac = Mac(domain)
    mac.permissions["accessibility"] = False
    gateway = SimpleNamespace(order_id="dwo_recovery", venture_id="ven_recovery", execution_id=None,
                              claim=lambda: pytest.fail("preflight must run before claim"),
                              stop_receipt=lambda *args, **kwargs: pytest.fail("unclaimed work has no stop receipt"))
    worker = DeviceWorker(gateway, lambda value: None)
    monkeypatch.setattr(worker, "_ai_backend", lambda: mac)
    manifest = {"applications": [domain.bundle], "origins": [], "actions": ["desktop.read", "desktop.interact"],
                "duration_seconds": 80, "instruction": domain.goal, "desktop_verification": {
                    "operation": "desktop_goal_v1", "application_bundle": domain.bundle, "process_id": 101,
                    "process_start_identity": "123456", "window_id": 300, "window_title": domain.goal,
                    "delivery_mode": "background", "max_steps": 8, "founder_goal": domain.goal}}
    worker.run_ai_loop({"state": "approved", "cu_recovery_v1": True}, manifest)
    assert worker.last["state"] == "not_started" and worker.last["task_verified"] is False
    expected = "Accessibility" if failure == "permissions" else "restore this window and retry"
    assert expected in worker.last["founder_prompt"]
    assert mac.actions == []


@pytest.mark.parametrize("enabled", [False, True, "true"])
def test_recovery_status_requires_exact_policy_flag_and_does_not_change_manifest(domain, monkeypatch, enabled):
    from tests.company_os_harness import load_company_os_app
    load_company_os_app()
    from app.build import device_orders
    from app.credits import policy
    body = {"state": "approved", "manifest": {"desktop_verification": {"founder_goal": domain.goal}}}
    monkeypatch.setattr(device_orders, "_scope", lambda *args: ("workspace", "actor", "device"))
    monkeypatch.setattr(device_orders, "authorize", lambda *args, **kwargs: None)
    monkeypatch.setattr(device_orders, "db", lambda: lambda: nullcontext(object()))
    monkeypatch.setattr(device_orders, "_saved_order", lambda *args: (None, dict(body)))
    monkeypatch.setattr(policy, "get_policy", lambda key: {"cu_recovery_v1": enabled})
    saved = device_orders.retrieve("venture", object(), "order")
    assert saved.get("cu_recovery_v1", False) is (enabled is True)
    assert saved["manifest"] == body["manifest"]


def test_subgoal_claim_cannot_advance_without_observed_postcondition(domain, tmp_path):
    mac = Mac(domain, changing=True)
    checkpoint = engine.FileCheckpoint(tmp_path / "checkpoint.json")
    def decide(payload):
        if not mac.actions:
            return proposal("click", element_id=payload["observation"].elements[0]["id"], subgoal_done=True)
        return proposal("fail", summary="You need to review the result.")
    result = engine.run_loop(domain.goal, decide, mac, threading.Event(), planner=lambda *args: plan(domain),
                             caps=Caps(max_steps=8, max_seconds=80, explicit=True), checkpoint=checkpoint, recovery=Recovery())
    saved = checkpoint.load()
    assert result.outcome == "failed" and saved["subgoal"] == 0
    assert saved["recovery"]["postconditions"] == [plan(domain)[0]["postcondition"]]
    assert any(row.get("outcome") == "subgoal_completion_rejected" for row in result.log)


@pytest.mark.parametrize("changing", [False, True])
def test_unchanged_pixels_or_repeated_action_get_one_alternate_then_handoff(domain, changing):
    mac = Mac(domain, changing=changing)
    pauses = []
    def decide(payload):
        if len(mac.actions) == 1 and "different authorized method" in payload["hint"]:
            return proposal("key", chord="tab")
        return proposal("click", element_id=payload["observation"].elements[0]["id"])
    if changing:
        original_observe = mac.observe
        def same_target():
            observation = original_observe()
            observation.elements[0]["label"] = "Next"
            return observation
        mac.observe = same_target
    mac.effect = "clicked control"
    original_act = mac.act
    def act(action, observation):
        original_act(action, observation)
        return "pressed tab" if action.op == "key" else "clicked control"
    mac.act = act
    result = engine.run_loop(domain.goal, decide, mac, threading.Event(), planner=lambda *args: plan(domain),
                             caps=Caps(max_steps=12, max_seconds=80, explicit=True), recovery=Recovery(),
                             pause=lambda *args: pauses.append(args) or "resolved")
    assert result.reason == "no_progress"
    assert [action.op for action in mac.actions] == ["click", "key"]
    assert len(pauses) == 1 and "before you retry" in pauses[0][1]
    assert not any(action.op in {"open_app", "switch_app"} for action in mac.actions)


@pytest.mark.parametrize("budget", ["steps", "time"])
def test_per_subgoal_budget_hands_off_before_total_task_budget(domain, monkeypatch, budget):
    mac = Mac(domain, changing=True)
    if budget == "time":
        clock = [0.0]
        monkeypatch.setattr(engine.time, "monotonic", lambda: clock[0])
        original_observe = mac.observe
        def observe():
            clock[0] += 15
            return original_observe()
        mac.observe = observe
    result = engine.run_loop(domain.goal, lambda payload: proposal("click", element_id=payload["observation"].elements[0]["id"]),
                             mac, threading.Event(), planner=lambda *args: plan(domain, 2),
                             caps=Caps(max_steps=8, max_seconds=80, explicit=True), recovery=Recovery(),
                             pause=lambda *args: "resolved")
    assert result.reason == f"subgoal_{budget[:-1] if budget == 'steps' else budget}_cap_reached"
    assert len(mac.actions) < 8 and result.seconds < 80


def test_checkpoint_retains_spent_budget_and_alternate_and_rejects_ambiguous_dispatch(domain, tmp_path):
    state = LoopState(caps=Caps(max_steps=12, max_seconds=80, explicit=True))
    recovery = Recovery()
    state.subgoals = recovery.plan(plan(domain, 2), state)
    state.steps = 3
    assert recovery.alternate()
    restored = Recovery()
    restored.restore(recovery.snapshot(), state)
    assert restored.started_steps == 0 and restored.alternate() is False
    checkpoint = engine.FileCheckpoint(tmp_path / "pending.json")
    checkpoint.save({**state.snapshot(domain.goal), "recovery": recovery.snapshot(), "pending_dispatch": True})
    mac = Mac(domain)
    result = engine.run_loop(domain.goal, lambda payload: pytest.fail("pending input must not be replayed"), mac,
                             threading.Event(), checkpoint=checkpoint, recovery=Recovery())
    assert result.reason == "checkpoint_dispatch_requires_reconciliation" and mac.actions == []
    assert checkpoint.load()["pending_dispatch"] is True


def test_unknown_action_effect_cannot_be_retried_as_an_alternate(domain, tmp_path):
    mac = Mac(domain, effect="ok")
    checkpoint = engine.FileCheckpoint(tmp_path / "uncertain.json")
    result = engine.run_loop(domain.goal, lambda payload: proposal("click", element_id=payload["observation"].elements[0]["id"]),
                             mac, threading.Event(), planner=lambda *args: plan(domain), checkpoint=checkpoint,
                             caps=Caps(max_steps=8, max_seconds=80, explicit=True), recovery=Recovery())
    assert result.reason == "effect_reconciliation_required" and len(mac.actions) == 1
    assert checkpoint.load()["pending_dispatch"] is True


def test_accessibility_targeting_precedes_coordinate_fallback(domain):
    state = LoopState(caps=Caps(max_steps=8, max_seconds=80, explicit=True))
    recovery = Recovery()
    state.subgoals = recovery.plan(plan(domain), state)
    observation = Mac(domain).observe()
    from coding_agents.cu_ai_loop import Action
    assert recovery.admit(Action("click", x=10, y=20), observation, state) == "accessibility_target_required"
    assert recovery.admit(Action("click", element_id=observation.elements[0]["id"]), observation, state) is None
    assert recovery.alternate()
    assert recovery.admit(Action("click", x=10, y=20), observation, state) is None


@pytest.mark.parametrize("field", ["label", "value"])
def test_native_postconditions_reject_truncated_readback(domain, field):
    from desktop_runtime.cu_ai_cua import CuaMac
    driver = Mac(domain)
    driver.windows[0].update({"layer": 0, "bounds": {"width": 600, "height": 400}, "z_index": 0})
    role = "AXStaticText" if field == "label" else "AXTextField"
    driver.window_state.update({"element_count": 1, "elements": [{"element_index": 0, "element_token": "current:0",
                                "role": role, field: domain.result, f"{field}_truncated": True}]})
    mac = CuaMac(driver.call)
    mac.set_target(101, domain.app)
    state = LoopState()
    recovery = Recovery()
    state.subgoals = recovery.plan([{"goal": domain.goal, "postcondition": {"role": role, field: domain.result}}], state)
    assert recovery.satisfied(mac.observe(), state) is False
    driver.window_state["elements"][0][f"{field}_truncated"] = False
    assert recovery.satisfied(mac.observe(), state) is True


def test_worker_uses_existing_string_plan_and_ax_first_then_independent_final_check(domain, monkeypatch):
    from datetime import datetime, timezone
    app = AppChoice(domain.app, domain.bundle, 101, "123456")
    monkeypatch.setattr(PeekabooDriver, "running_apps", lambda: [app])
    monkeypatch.setattr(PeekabooDriver, "running_windows", lambda app: [{"window_id": 300}])
    mac = Mac(domain, changing=True)
    original_observe = mac.observe
    def observe():
        observation = original_observe()
        if mac.actions:
            observation.elements.append({"id": "result", "ax_role": "AXStaticText", "label": domain.result})
        return observation
    mac.observe = observe
    mac.reviewed_goal_evidence = lambda observation, spec, pairs: (
        any(element.get("label") == spec["expected_visible_text"] for element in observation.elements), "reviewed_goal_predicates_observed")
    requests, receipts = [], []
    manifest = {"applications": [domain.bundle], "origins": [], "actions": ["desktop.read", "desktop.interact"],
                "duration_seconds": 80, "instruction": domain.goal, "desktop_verification": {
                    "operation": "desktop_goal_v1", "application_bundle": domain.bundle, "process_id": 101,
                    "process_start_identity": "123456", "window_id": 300, "window_title": domain.goal,
                    "delivery_mode": "background", "max_steps": 8, "founder_goal": domain.goal,
                    "expected_visible_text": domain.result}}
    class Gateway:
        order_id, venture_id, execution_id = "dwo_verified", "ven_verified", None
        claim_attempted = False
        def claim(self):
            self.claim_attempted = True
            self.execution_id = "dex_verified"
            return {"id": self.order_id, "venture_id": self.venture_id, "execution_id": self.execution_id,
                    "workspace_id": "workspace", "actor_id": "actor", "device_id": "device", "review_digest": "a" * 64,
                    "reservation_id": "reservation", "claimed_at": datetime.now(timezone.utc).isoformat(), "manifest": manifest}
        def ai_step(self, invocation_id, kind, context, png=None, **kwargs):
            requests.append((kind, context, png))
            if kind == "plan":
                assert len(context["reason"]) <= 200 and len(context["screen"]) <= 1500
                return {"authority": "proposal_only", "text": json.dumps({"subgoals": [json.dumps(plan(domain)[0])]})}
            if kind == "verify":
                return {"authority": "proposal_only", "text": '{"passed": true, "reason": "Current result observed"}'}
            assert len(context["hint"]) <= 500
            value = proposal("done", summary=domain.result) if mac.actions else proposal("click", element_id=context["elements"][0]["id"])
            return {"authority": "proposal_only", "reply": json.dumps(value[0])}
        def stop_receipt(self, digest, stopped, **values):
            receipts.append(values)
            return {"state": "stopped"}
    worker = DeviceWorker(Gateway(), lambda value: None)
    monkeypatch.setattr(worker, "_ai_backend", lambda: mac)
    worker.run_ai_loop({"state": "approved", "cu_recovery_v1": True}, manifest)
    assert worker.last["task_verified"] is True and receipts[0]["task_verified"] is True
    assert [kind for kind, context, png in requests] == ["plan", "step", "step", "verify"]
    assert next(png for kind, context, png in requests if kind == "step") is None
    assert len(mac.actions) == 1


def test_legacy_entry_reuses_flagged_native_recovery_without_broadening_review(domain, monkeypatch):
    worker = DeviceWorker(SimpleNamespace(order_id="order", venture_id="venture"), lambda value: None)
    received = []
    monkeypatch.setattr(worker, "run_ai_loop", lambda saved, manifest: received.append((saved, manifest)))
    saved = {"state": "approved", "cu_recovery_v1": True}
    manifest = {"instruction": domain.goal}
    worker.run_desktop(saved, manifest)
    assert received == [(saved, manifest)]
