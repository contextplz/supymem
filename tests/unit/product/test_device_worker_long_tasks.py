"""Long Mac tasks: budget awareness, subgoals, Save shortcut, retry-once, no-progress stop."""
from coding_agents.jev_decision import Candidate, desktop_subgoals
from desktop_runtime import cu_reliability as cu
from desktop_runtime import cua_driver
from desktop_runtime.device_worker import DeviceWorker
from desktop_runtime.peekaboo_driver import AppChoice, PeekabooDriver


def test_subgoals_split_in_order_and_keep_quoted_text_whole():
    goal = ('Open TextEdit, type "Hello. World" then save it as notes.txt. '
            "Close the window and reopen the file; verify the text")
    assert desktop_subgoals(goal) == [
        'Open TextEdit, type "Hello. World"', "save it as notes.txt",
        "Close the window and reopen the file", "verify the text"]
    assert desktop_subgoals("1. open x 2. do y") == ["open x", "do y"]


def test_goal_chords_only_when_the_founder_names_the_intent():
    assert cu.goal_chords("write a note and save it") == ["cmd+s"]
    assert cu.goal_chords("create a new document then save") == ["cmd+s", "cmd+n"]
    assert cu.goal_chords("open the page and read the title") == []


def test_compact_history_keeps_recent_and_counts_older():
    log = [f"click 'B{i}' ok" for i in range(15)]
    out = cu.compact_history(log, recent=3)
    assert out.startswith("12 earlier effects done; ")
    assert out.endswith("click 'B14' ok") and "B11" not in out
    assert cu.compact_history([]) == ""


def test_budget_line_warns_near_the_end():
    assert "Step 3 of 30" in cu.effect_budget_line(2, 30, 500)
    assert "nearly spent" not in cu.effect_budget_line(2, 30, 500)
    assert "nearly spent" in cu.effect_budget_line(28, 30, 500)


def _setup(monkeypatch, app_bundle="com.example.Editor"):
    app = AppChoice("Editor", app_bundle, 101, "123456")
    monkeypatch.setattr(PeekabooDriver, "running_apps", classmethod(lambda cls: [app]))
    monkeypatch.setattr(PeekabooDriver, "running_windows", classmethod(
        lambda cls, _app, **_kwargs: [{"window_id": 300, "on_screen": True}]))
    monkeypatch.setattr("desktop_runtime.local_notices.push_task_finished", lambda **_k: None)
    return app


def _run(monkeypatch, app, decide, *, goal, effect="confirmed", readout="Draft", max_steps=10,
         buttons=("Advance",), perception=None, protocol=None):
    effects, observations = [], []

    class Driver:
        def __init__(self, **_kwargs):
            assert _kwargs.get("observation_protocol") == protocol
        def __call__(self, arguments, *, session_id):
            if arguments["action"] == "capture":
                count = len(observations) + len(effects) + 1
                elements = [{"id": f"elem_{count}_{label}", "role": "AXButton", "label": label,
                             "enabled": True, "has_point": True, "actionable": True}
                            for label in buttons]
                elements.append({"id": f"r{count}", "role": "text", "label": readout, "readout": True,
                                 "enabled": True, "has_point": False, "actionable": False})
                return {"snapshot_id": f"ps1_{count}", "elements": elements, "perception": perception}
            effects.append(arguments)
            return {"effect": effect}
        def snapshot_ready_for_admission(self, _snapshot): return True
        def close(self): pass

    monkeypatch.setattr(cua_driver, "CuaWindowDriver", Driver)

    class Gateway:
        order_id, venture_id, execution_id = "dwo_long", "ven_ours", None
        claim_attempted = True
        def claim(self): self.execution_id = "dex_long"
        def current(self): return {"state": "claimed"}
        def decide(self, _invocation, observation):
            observations.append(observation)
            return decide(observation, len(observations))
        def admit_effect(self, *_args): return True
        def effect_receipt(self, *_args): return None
        def stop_receipt(self, _digest, _stopped, *, task_verified=False, **_reason):
            return {"state": "stopped"}

    manifest = {"applications": [app.bundle_id], "origins": [],
        "actions": ["desktop.read", "desktop.interact"], "duration_seconds": 300,
        "instruction": goal, "desktop_verification": {
            "operation": "desktop_goal_v1", "application_bundle": app.bundle_id,
            "process_id": 101, "process_start_identity": "123456", "window_id": 300,
            "window_title": "Editor", "delivery_mode": "background", "founder_goal": goal,
            "max_steps": max_steps, "reviewed_text": []}}
    worker = DeviceWorker(Gateway(), lambda _receipt: None,
                          cua_socket="/private/tmp/supymem-cua-test/driver.sock", cua_host_pid=1234, cua_protocol=protocol)
    worker.run_desktop({"state": "approved"}, manifest)
    return worker, effects, observations


def _click(label):
    def decide(observation, _n):
        control = next(c for c in observation.controls if c.label == label)
        return Candidate("click", control.id, observation.digest())
    return decide


def test_decision_context_carries_budget_and_ordered_subgoals(monkeypatch):
    app = _setup(monkeypatch)
    worker, _effects, observations = _run(
        monkeypatch, app, lambda o, n: Candidate("blocked", None, o.digest()),
        goal="Open the file then type the text. Save it", max_steps=12)
    context = observations[0].context
    assert "Step 1 of 12" in context
    assert "1) Open the file | 2) type the text | 3) Save it" in context
    assert len(context) <= 4000


def test_save_shortcut_is_offered_only_for_a_save_goal_and_dispatches_a_fixed_chord(monkeypatch):
    app = _setup(monkeypatch)

    def decide(observation, n):
        chord = next((c for c in observation.controls if "Command+S" in c.label), None)
        if chord is None:
            return Candidate("blocked", None, observation.digest())
        return Candidate("key", chord.id, observation.digest())

    worker, effects, _ = _run(monkeypatch, app, decide, goal="Write a note and save it", max_steps=2)
    assert effects and effects[0]["action"] == "key" and effects[0]["chord"] == "cmd+s"
    assert "element_id" not in effects[0]
    app2 = _setup(monkeypatch)
    worker, effects, observations = _run(monkeypatch, app2, decide, goal="Read the page title")
    assert effects == []
    assert not any("Command+S" in c.label for c in observations[0].controls)


def test_suspected_noop_click_is_retried_once_then_stopped(monkeypatch):
    app = _setup(monkeypatch)
    worker, effects, _ = _run(monkeypatch, app, _click("Advance"), goal="Press Advance",
                              effect="suspected_noop", max_steps=8)
    assert len(effects) == 2
    assert any(step["kind"] == "effect_retry" for step in worker.last["evidence"]["steps"])
    assert worker.last["outcome"] == "repeated_action_stopped"


def test_unchanged_screen_after_six_effects_stops_with_no_visible_progress(monkeypatch):
    app = _setup(monkeypatch)
    worker, effects, _ = _run(monkeypatch, app, _click("Advance"), goal="Press Advance until done",
                              max_steps=20)
    assert len(effects) == 6
    assert worker.last["outcome"] == "repeated_action_stopped"
    assert any(step.get("reason") == "no_visible_progress" for step in worker.last["evidence"]["steps"])
