"""AI-primary Mac loop: action schema, safety blocks, focus guard, loop control, human-gate pause."""
import threading

import pytest
from coding_agents import cu_ai_loop as loop
from coding_agents.cu_ai_loop import (Action, ActionError, Caps, LoopState, caps_for, excluded_app, focus_block,
                                      human_gate, parse_reply, safety_block)
from desktop_runtime.cu_ai_engine import FileCheckpoint, Observation, run_loop, wait_for_human


def act(op, **kw):
    return {"op": op, "element_id": "", "x": 0, "y": 0, "text": "", "chord": "", "direction": "",
            "amount": 0, "app": "", "ms": 0, "summary": "", **kw}


def reply(*actions, subgoal_done=False, memory=""):
    return {"thought": "t", "memory": memory, "subgoal_done": subgoal_done, "actions": list(actions)}


def test_peekaboo_observation_runs_retry_verification_and_global_cursor(monkeypatch, tmp_path):
    from pathlib import Path
    from desktop_runtime import cu_ai_engine as engine
    from desktop_runtime.cu_ai_retries import semantic_state
    mac = object.__new__(engine.PeekabooMac)
    mac.dir = tmp_path
    dispatches, cursor, verified = [], [], []
    def driver(*args):
        if args[0] == "see":
            Path(args[args.index("--path") + 1]).write_bytes(b"synthetic capture")
            return {"data": {"application_name": "TextEdit", "snapshot_id": "owned-shot",
                "ui_elements": [{"id": "e1", "ax_role": "AXButton", "label": "Save",
                    "bounds": {"x": 120, "y": 60, "width": 40, "height": 20}}]}}
        dispatches.append(args)
        return {}
    monkeypatch.setattr(mac, "_run", driver)
    monkeypatch.setattr(engine, "frontmost_info", lambda: ("TextEdit", 123))
    monkeypatch.setattr(engine.agent_cursor, "emit", lambda *args, **kwargs: cursor.append(args))
    actions = iter([reply(act("click", element_id="e1")), reply(act("done", summary="saved"))])
    def verify(claim, obs, evidence):
        verified.append(claim)
        assert obs.notes == "" and obs.global_pixels is True
        assert len(semantic_state(obs)) == 64
        return True, "independent synthetic readback"
    result = run_loop("Save the note in TextEdit", lambda payload: (next(actions), {}), mac,
                      threading.Event(), verify=verify)
    assert result.outcome == "done" and verified == ["saved"]
    assert len(dispatches) == 1 and dispatches[0][:3] == ("click", "--at", "140,70")
    assert any(event[:3] == ("click", 140.0, 70.0) for event in cursor)


# --- schema -------------------------------------------------------------------------------------

def test_any_chord_is_accepted_and_batch_keeps_only_predictable_followups():
    r = parse_reply(reply(act("click", element_id="elem_2"), act("type", text="hi"), act("key", chord="cmd+shift+s"),
                          act("click", element_id="elem_9"), act("key", chord="return")))
    assert [a.op for a in r.actions] == ["click", "type", "key"]  # a later click is screen-dependent: dropped
    assert parse_reply(reply(act("key", chord="cmd+shift+g"))).actions[0].chord == "cmd+shift+g"


@pytest.mark.parametrize("bad", [
    act("click"), act("type"), act("key", chord="cmd+nonsense"), act("open_app"), act("explode"),
    act("scroll", direction="sideways")])
def test_invalid_actions_are_rejected(bad):
    with pytest.raises(ActionError):
        parse_reply(reply(bad))


def test_terminal_ops_go_alone_and_json_text_with_trailing_data_parses():
    r = parse_reply(reply(act("done", summary="ok"), act("type", text="x")))
    assert [a.op for a in r.actions] == ["done"]
    import json
    assert parse_reply(json.dumps(reply(act("wait", ms=500))) + "\n extra").actions[0].ms == 500


@pytest.mark.parametrize("summary", ["x" * 501, "界" * 501, {"claim": "complete"}])
def test_terminal_summary_is_not_silently_clipped_or_coerced(summary):
    with pytest.raises(ActionError, match="action_summary_invalid"):
        parse_reply(reply(act("done", summary=summary)))


def test_invalid_terminal_summary_is_repaired_without_effect_or_partial_verification():
    mac = FakeMac([("owned", [])])
    effects, verified, hints = [], [], []
    mac.act = lambda *args: effects.append(args)
    answers = iter([reply(act("done", summary="x" * 501)),
                    reply(act("done", summary="All required source facts."))])

    def decide(payload):
        hints.append(payload["history"])
        return next(answers), {}

    result = run_loop("Read the required facts", decide, mac, threading.Event(),
                      verify=lambda claim, *_: (verified.append(claim) or True, ""))
    assert result.outcome == "done" and not effects
    assert verified == ["All required source facts."]
    assert any("500 characters" in line for line in hints[-1])
    exact = "界" * 500
    assert parse_reply(reply(act("done", summary=exact))).actions[0].summary == exact


# --- safety -------------------------------------------------------------------------------------

def test_never_types_secrets_or_cards_or_into_secure_fields():
    assert safety_block(Action("type", text="4111 1111 1111 1111"), goal="g")
    assert safety_block(Action("type", text="sk-abcdefghijklmnopqrstuv"), goal="g")
    assert safety_block(Action("type", text="hello"), goal="g", focused_secure=True)
    assert safety_block(Action("type", text="Supymem test note"), goal="g") is None


def test_risky_controls_need_explicit_authorization_in_task():
    click = Action("click", element_id="e")
    assert safety_block(click, goal="organize files", target_label="Empty Trash")
    assert safety_block(click, goal="organize files", target_label="Delete")
    assert safety_block(click, goal="delete the old draft", target_label="Delete") is None
    assert safety_block(click, goal="organize files", target_label="Save") is None


def test_security_apps_and_destructive_chords_blocked():
    assert safety_block(Action("open_app", app="System Settings"), goal="tidy files")
    assert safety_block(Action("click", element_id="e"), goal="x", frontmost="System Settings")
    assert safety_block(Action("key", chord="cmd+shift+backspace"), goal="tidy files")
    assert safety_block(Action("key", chord="cmd+s"), goal="tidy files") is None


# --- focus guard (regression: keystrokes typed into the Claude chat composer) --------------------

def test_off_limits_apps_unless_named_by_the_task():
    assert excluded_app("Claude", "open TextEdit") and excluded_app("Terminal", "x") and excluded_app("iTerm2", "x")
    assert not excluded_app("Claude", "summarize my Claude conversation")
    assert not excluded_app("TextEdit", "x")


def test_focus_block_refuses_type_key_click_in_wrong_or_off_limits_app():
    kw = dict(goal="open TextEdit", focused_role="AXTextArea")
    typ = Action("type", text="hi")
    assert focus_block(typ, frontmost="Claude", pid=5, expected_pid=5, **kw)
    assert focus_block(Action("key", chord="return"), frontmost="Terminal", pid=5, expected_pid=5, **kw)
    assert focus_block(typ, frontmost="Safari", pid=6, expected_pid=5, **kw)            # focus moved
    assert focus_block(typ, frontmost="TextEdit", pid=5, expected_pid=5, goal="g", focused_role="AXButton")
    assert focus_block(typ, frontmost="TextEdit", pid=5, expected_pid=5, **kw) is None
    assert focus_block(Action("open_app", app="TextEdit"), frontmost="Claude", pid=1, expected_pid=2, **kw) is None


# --- loop control with a fake Mac ---------------------------------------------------------------

class FakeMac:
    def __init__(self, screens, front=("TextEdit", 10)):
        self.screens, self.i, self.front = screens, 0, front
        self.acted, self.activated = [], []
        self._role = "AXTextArea"

    def observe(self):
        name, elements = self.screens[min(self.i, len(self.screens) - 1)]
        self.i += 1
        return Observation(name.encode(), elements, self.front[0], "Doc", "s", False, self.front[1])

    def frontmost(self):
        return self.front

    def focused_role(self):
        return self._role

    def activate(self, name):
        self.activated.append(name)
        self.front = (name, 10)

    def act(self, action, obs):
        self.acted.append(action.op)
        self.fronts = getattr(self, "fronts", []) + [self.front[0]]
        return "ok"


def run(decider, mac, **kw):
    import desktop_runtime.cu_ai_engine as eng
    eng.time.sleep = lambda s: None
    kw.setdefault("verify", lambda *args: (True, ""))  # Explicit test verifier, never a production default.
    kw.setdefault("verify_subgoal", lambda *args: (True, "source-qualified test sub-goal"))
    return run_loop("save the note", decider, mac, kw.pop("stop", threading.Event()), **kw)


def scripted(*replies):
    it = iter(replies)
    return lambda payload: (next(it), {"in": 1, "out": 1})


def test_completion_rejection_stops_after_three_unchanged_claims():
    calls, plans = [], []
    mac = FakeMac([("same", [])])
    def decide(payload):
        return reply(act("done", summary="different claim"), subgoal_done=True, memory=str(len(calls))), {}
    result = run(decide, mac, verify=lambda *args: (calls.append(1) or False, "private reason"),
                 planner=lambda *args: plans.append(args[2]) or ["check"], caps=Caps(max_steps=30))
    assert len(calls) == 3
    assert len(plans) == 2  # initial plan and one safe alternative
    assert result.reason == "completion_retry_exhausted"
    assert "private reason" not in str(result.log)


def test_done_goes_through_the_verifier_and_only_a_pass_completes():
    mac = FakeMac([("a", []), ("b", [])])
    planner = lambda *a: ["save doc"]  # noqa: E731
    d = scripted(reply(act("done", summary="early")), reply(act("key", chord="cmd+s"), subgoal_done=True),
                 reply(act("done", summary="saved")))
    verdicts = iter([(False, "not saved yet"), (True, "ok")])
    seen = []
    result = run(d, mac, planner=planner, verify=lambda s, o, m: (seen.append(s), next(verdicts))[1])
    assert result.outcome == "done" and seen == ["early", "saved"]
    rows = [r for r in result.log if r.get("verified") is not None]
    assert [r["verified"] for r in rows] == [False, True]


def test_done_without_verifier_is_rejected_while_subgoals_remain():
    d = scripted(reply(act("done", summary="early")), reply(act("fail", summary="stop")))
    result = run(d, FakeMac([("a", [])]), planner=lambda *a: ["one", "two"], verify=None)
    assert result.outcome == "failed" and result.reason == "model_reported_failure"


def test_failed_verification_does_not_report_done():
    d = scripted(*[reply(act("done", summary="x"))] * 3, reply(act("fail", summary="gave up")))
    result = run(d, FakeMac([("a", [])]), verify=lambda *a: (False, "title not saved"))
    assert result.outcome == "failed"


@pytest.mark.parametrize("alternate_invalid", [False, True])
def test_rejected_done_uses_bounded_replans_even_with_interleaved_invalid_replies(alternate_invalid):
    calls, plans = [], []
    def decide(payload):
        calls.append(payload)
        if alternate_invalid and len(calls) % 2 == 0:
            return {"actions": [{"op": "private unsupported op"}]}, {}
        return reply(act("done", summary="unproved")), {}
    def planner(*args):
        plans.append(args[2])
        return ["perform the remaining task"]
    result = run(decide, FakeMac([("same", [])]), planner=planner,
                 verify=lambda *a: (False, "private verifier detail"),
                 caps=Caps(max_steps=40, no_progress_fail=3, repeat_fail=3, max_replans=1, explicit=True))
    assert result.outcome == "failed" and result.reason == "completion_retry_exhausted"
    assert len(calls) <= 7 and len(plans) == 2
    assert "private verifier detail" not in str(result.log)


def test_required_missing_output_replans_with_fixed_feedback_instead_of_repeating_done(tmp_path, monkeypatch):
    from coding_agents import cu_ai_tools as tools
    monkeypatch.setattr(tools.os.path, "expanduser", lambda path: path.replace("~", str(tmp_path), 1))
    plans, verdicts = [], []
    def planner(*args):
        plans.append(args[2])
        return ["produce the output"]
    result = run_loop("Save the report as ~/missing.txt", lambda _: (reply(act("done", summary="saved")), {}),
                      FakeMac([("same", [])]), threading.Event(), planner=planner,
                      verify=lambda *a: verdicts.append(True) or (True, ""),
                      caps=Caps(max_steps=40, no_progress_fail=2, repeat_fail=2, max_replans=1, explicit=True))
    assert result.outcome == "failed" and result.reason == "completion_retry_exhausted" and result.steps <= 4
    assert len(plans) == 2 and "completion_retry_exhausted" in plans[-1]
    assert verdicts == []


def test_validation_diagnostics_are_allowlisted_and_model_gets_action_reason():
    from coding_agents.cu_ai_loop import ActionError, validation_diagnostic
    assert validation_diagnostic(ActionError("private provider body")) == "reply_validation_failed"
    assert validation_diagnostic(ValueError("private JSON body")) == "reply_validation_failed"
    assert validation_diagnostic(ActionError("openai_response_incomplete")) == "openai_response_incomplete"
    payloads = []
    replies = iter([{"actions": [{"op": "click"}]}, reply(act("done", summary="verified"))])
    def decide(payload):
        payloads.append(payload)
        return next(replies), {}
    result = run(decide, FakeMac([("same", [])]))
    assert result.outcome == "done" and result.steps == 1
    assert result.log[0]["validation_code"] == "action_target_missing"
    assert "action_target_missing" in str(payloads[-1]["history"])
    assert "private" not in str(result.log)


def test_planner_keeps_conditional_validation_in_one_workflow():
    assert 'fewest sub-goals' in loop.PLAN_PROMPT
    assert 'Keep one form/workflow in one sub-goal' in loop.PLAN_PROMPT
    assert 'Conditional recovery is not a mandatory separate milestone' in loop.PLAN_PROMPT
    assert '2-8' not in loop.PLAN_PROMPT


def test_incomplete_provider_output_gets_compact_action_feedback_without_dispatch():
    payloads = []
    mac = FakeMac([('current source', [])])
    def decide(payload):
        payloads.append(payload)
        if len(payloads) == 1:
            raise ActionError('openai_response_incomplete')
        return reply(act('done', summary='Observed')), {}
    result = run(decide, mac)
    assert result.outcome == 'done' and not mac.acted
    assert 'openai_response_incomplete' in str(payloads[1]['history'])
    assert 'one compact JSON action' in str(payloads[1]['history'])
    assert 'without repeating delivered input' in str(payloads[1]['history'])


@pytest.mark.parametrize('summary', ['Should I correct the invalid field?', 'Which project should I choose?'])
def test_validation_handoff_reminder_is_bounded_and_survives_checkpoint(summary):
    mac = FakeMac([('current source', [])], front=('Chrome (browser)', 10))
    mac.form_validation_feedback = lambda obs: 'Private source validation text'
    payloads, saved = [], []
    checkpoint = type('Checkpoint', (), {'load': lambda self: None,
        'save': lambda self, value: saved.append(value), 'clear': lambda self: None})()
    def decide(payload):
        payloads.append(payload)
        return reply(act('needs_you', summary=summary)), {}
    result = run(decide, mac, checkpoint=checkpoint)
    assert result.reason == 'needs_founder' and len(payloads) == 2 and result.steps == 1
    assert 'Private source validation text' in str(payloads[1]['history'])
    assert 'If the original task authorizes' in str(payloads[1]['history'])
    assert saved[-1]['form_validation_reminded'] is True and not mac.acted
    assert 'Private source' not in str(result.log)
    checkpoint.load = lambda: saved[-1]
    resumed = run(decide, mac, checkpoint=checkpoint)
    assert resumed.reason == 'needs_founder' and len(payloads) == 3


@pytest.mark.parametrize('summary', ['Payment requires approval?', 'Which password?', 'Solve CAPTCHA?',
                                    'Need a verification code?', 'May I accept consent?'])
def test_validation_reminder_never_deflects_safety_handoffs(summary):
    mac = FakeMac([('current source', [])], front=('Chrome (browser)', 10))
    mac.form_validation_feedback = lambda obs: pytest.fail('safety handoff must not be deflected')
    result = run(scripted(reply(act('needs_you', summary=summary))), mac)
    assert result.reason == 'needs_founder' and not mac.acted and result.steps == 0


def test_validation_reminder_never_deflects_observed_human_gate():
    mac = FakeMac([('current source', [])], front=('Chrome (browser)', 10))
    observe = mac.observe
    def gated_observation():
        observed = observe()
        observed.human = 'captcha'
        return observed
    mac.observe = gated_observation
    mac.form_validation_feedback = lambda obs: pytest.fail('observed human gate must stop')
    result = run(scripted(reply(act('needs_you', summary='Should I correct the field?'))), mac)
    assert result.reason == 'needs_founder' and not mac.acted and result.steps == 0


def test_stop_during_invalid_reply_is_preserved():
    stop = threading.Event()
    def decide(payload):
        stop.set()
        raise ValueError("private provider body")
    result = run(decide, FakeMac([("same", [])]), stop=stop)
    assert result.outcome == "stopped" and "private" not in str(result.log)


def test_rejected_done_can_recover_then_produce_required_file(tmp_path, monkeypatch):
    from coding_agents import cu_ai_tools as tools
    monkeypatch.setattr(tools.os.path, "expanduser", lambda path: path.replace("~", str(tmp_path), 1))
    mac, plans = FakeMac([("same", [])]), []
    def save(action, obs):
        (tmp_path / "output.txt").write_text("owned synthetic output")
        return "ok"
    monkeypatch.setattr(mac, "act", save)
    replies = scripted(reply(act("done", summary="unproved")), reply(act("done", summary="unproved")),
                       reply(act("key", chord="cmd+s")), reply(act("done", summary="proved")))
    result = run_loop("Save report as ~/output.txt", replies, mac, threading.Event(),
                      planner=lambda *a: plans.append(a[2]) or ["produce output"], verify=lambda *a: (True, ""),
                      caps=Caps(max_steps=10, no_progress_fail=2, repeat_fail=2, max_replans=1, explicit=True))
    assert result.outcome == "done" and len(plans) == 2 and (tmp_path / "output.txt").is_file()


def test_step_cap_stop_and_no_progress():
    forever = lambda p: (reply(act("key", chord="tab")), {})  # noqa: E731
    capped = run(forever, FakeMac([(str(n), []) for n in range(50)]), caps=Caps(max_steps=3, explicit=True))
    assert capped.outcome == "capped" and capped.reason == "step_cap_reached" and capped.steps == 3
    stuck = run(forever, FakeMac([("same", [])]), caps=Caps(max_steps=50, no_progress_fail=4, repeat_fail=99, explicit=True))
    assert stuck.outcome == "failed" and stuck.reason == "route_retry_exhausted"
    stop = threading.Event()
    stop.set()
    assert run(forever, FakeMac([("a", [])]), stop=stop).outcome == "stopped"


def test_stuck_loop_replans_before_failing():
    plans = []
    planner = lambda goal, mem, reason, obs: plans.append(reason) or ["try another way"]  # noqa: E731
    calls = {"n": 0}

    def d(payload):
        calls["n"] += 1
        return (reply(act("done", summary="ok")) if calls["n"] > 3 else reply(act("key", chord="tab"))), {}
    result = run(d, FakeMac([("same", [])]), planner=planner,
                 caps=Caps(max_steps=50, no_progress_fail=3, repeat_fail=99, explicit=True),
                 verify=lambda *a: (True, ""))
    assert any(r.startswith("stuck") for r in plans) and result.outcome == "done"


def test_batch_acts_once_then_reobserves_and_records_latency():
    mac = FakeMac([("a", []), ("b", [])])
    d = scripted(reply(act("click", element_id="e1"), act("type", text="hi"), act("key", chord="return")),
                 reply(act("done", summary="ok")))
    elements = [{"id": "e1", "label": "Body", "bounds": {"x": 1, "y": 1, "width": 5, "height": 5}}]
    mac.screens = [("a", elements), ("b", elements)]
    result = run(d, mac)
    assert mac.acted == ["click", "type", "key"] and result.outcome == "done"
    assert set(result.log[0]["ms"]) == {"observe", "model", "act"}


def test_off_limits_frontmost_is_never_typed_into_and_drift_stops_without_activate():
    mac = FakeMac([("a", []), ("b", [])])
    d = scripted(reply(act("open_app", app="TextEdit")), reply(act("type", text="secret-free text")),
                 reply(act("done", summary="ok")))
    # TextEdit is the working app; then Claude steals focus before the type action.
    orig = mac.act

    def act_and_steal(action, obs):
        out = orig(action, obs)
        if action.op == "open_app":
            mac.front = ("Claude", 99)
        return out
    mac.act = act_and_steal
    result = run(d, mac)
    assert mac.activated == [] and "Claude" not in mac.fronts[1:]   # never re-activated; nothing ran in Claude
    assert result.outcome == "failed" and result.reason == "focus_drift_observed"


def test_checkpoint_resume_skips_finished_subgoals(tmp_path):
    ck = FileCheckpoint(tmp_path / "ck.json")
    state = LoopState()
    state.subgoals, state.subgoal, state.memory, state.steps = ["a", "b", "c"], 2, "file saved", 31
    ck.save({**state.snapshot("save the note"), "pending_dispatch": False})
    seen = {}

    def d(payload):
        seen.update(subgoal=payload["subgoal"], memory=payload["memory"], hist=payload["history"][0])
        return reply(act("done", summary="ok")), {}
    result = run(d, FakeMac([("a", [])]), checkpoint=ck, verify=lambda *a: (True, ""))
    assert seen["subgoal"] == 2 and seen["memory"] == "file saved" and "resumed" in seen["hist"]
    assert result.outcome == "done" and ck.load() is None
    assert not LoopState().restore(state.snapshot("another task"), "save the note")


def test_budgets_scale_with_plan_size_but_explicit_wins():
    assert caps_for(2).max_steps == 60 and caps_for(8).max_steps == 120 and caps_for(8).max_seconds >= 4800
    assert caps_for(3, max_steps=7).max_steps == 7


# --- CAPTCHA / verification -> Needs you -> resume ----------------------------------------------

def test_human_gate_detection():
    assert human_gate([{"label": "I'm not a robot"}]) == "captcha"
    assert human_gate([{"label": "Verify you are human"}]) == "captcha"
    assert human_gate([{"label": "Enter the code we sent to your phone"}]) == "code"
    assert human_gate([], "Just a moment...") == "captcha"
    assert human_gate([{"ax_role": "AXTextArea", "label": "my note about a verification code"}]) is None
    assert human_gate([{"label": "Save"}]) is None


def test_gate_is_offered_to_the_model_first_then_pauses_notifies_and_resumes():
    gate = [{"label": "I'm not a robot"}]
    mac = FakeMac([("captcha", gate), ("captcha2", gate), ("captcha3", gate), ("after", [{"label": "Welcome"}])])
    sent, order, hints = [], [], []
    resume = threading.Event()

    def pause(kind, prompt, obs):
        order.append(kind)
        verdict = wait_for_human(kind, prompt, mac, threading.Event(), resume, notify=sent.append, poll=0.01)
        mac.i = 3
        return verdict
    threading.Timer(0.05, resume.set).start()                      # founder clicks Continue
    replies = iter([reply(act("key", chord="tab")), reply(act("done", summary="ok"))])

    def d(payload):
        hints.append(payload["hint"])
        return next(replies), {}
    result = run(d, mac, pause=pause)
    assert "HUMAN STEP DETECTED" in hints[0] and "try" not in order       # alternatives offered before interrupting
    assert order == ["captcha"] and sent and "Continue" in sent[0] and result.outcome == "done"


def test_needs_you_from_the_model_is_sent_back_twice_before_interrupting_the_founder():
    asked, notified = [], []
    replies = iter([reply(act("needs_you", summary="stuck")), reply(act("needs_you", summary="stuck")),
                    reply(act("needs_you", summary="really stuck")), reply(act("done", summary="ok"))])
    plans = []
    result = run(lambda p: (next(replies), {}), FakeMac([("a", []), ("b", []), ("c", []), ("d", [])]),
                 planner=lambda g, m, reason, o: plans.append(reason) or ["another route"],
                 pause=lambda k, prompt, o: (asked.append(prompt), notified.append(k), "continue")[2],
                 verify=lambda *a: (True, ""))
    assert len(asked) == 1 and "Continue" in asked[0] and "really stuck" not in asked[0]               # only the third ask reaches the founder
    assert any("another route" in r for r in plans) and result.outcome == "done"


def test_wait_for_human_resolves_when_gate_disappears_times_out_and_stops():
    mac = FakeMac([("g", [{"label": "I'm not a robot"}]), ("clear", [])])
    seen = []
    assert wait_for_human("captcha", "p", mac, threading.Event(), threading.Event(),
                          notify=seen.append, poll=0.0, timeout=5) == "resolved" and seen == ["p"]
    gate = FakeMac([("g", [{"label": "I'm not a robot"}])])
    assert wait_for_human("captcha", "p", gate, threading.Event(), threading.Event(),
                          notify=lambda t: None, poll=0.01, timeout=0.1) == "timeout"
    stop = threading.Event()
    stop.set()
    assert wait_for_human("captcha", "p", gate, stop, threading.Event(), notify=lambda t: None) == "stopped"


def test_gate_timeout_fails_the_task_truthfully():
    mac = FakeMac([("g", [{"label": "Verify you are human"}])])
    result = run(scripted(reply(act("key", chord="tab"))), mac, pause=lambda k, p, o: "timeout")
    assert result.outcome == "failed" and result.reason == "needs_you_timeout"


def test_agent_cursor_events_bracket_the_task_with_screen_global_points(monkeypatch):
    from desktop_runtime import agent_cursor
    events = []
    monkeypatch.setattr(agent_cursor, "emit", lambda kind, x=None, y=None, label=None: events.append((kind, x, y, label)))
    el = [{"id": "e1", "label": "Save", "screen": {"x": 100, "y": 200, "w": 40, "h": 20}}]
    mac = FakeMac([("a", el), ("b", el)])
    d = scripted(reply(act("click", element_id="e1")), reply(act("done", summary="ok")))
    run(d, mac)
    kinds = [e[0] for e in events]
    assert kinds[0] == "begin" and kinds[-1] == "end" and "read" in kinds and kinds.count("done") == 1
    assert ("click", 120.0, 210.0, "Clicking") in events


def test_whole_goal_verifier_receives_unfinished_plan_before_acceptance():
    called = []
    result = run(scripted(reply(act("open_app", app="Notes")), reply(act("done", summary="all done")),
                          reply(act("done", summary="all done")), reply(act("done", summary="all done")),
                          reply(act("fail", summary="unable"))), FakeMac([("a", [])]),
                 planner=lambda *a: ["open editor", "write content", "save content"],
                 verify=lambda claim, obs, memory: (called.append(memory), (False, "only app is open"))[1])
    assert result.outcome == "failed" and result.reason == "model_reported_failure"
    assert "save content" in called[0]
    assert any(row.get("verified") is False for row in result.log)


def test_browser_failure_replans_from_fresh_state_once_without_repeating_effects():
    mac = FakeMac([("first", []), ("fresh", [])], front=("Chrome (browser)", 10))
    plans = []
    def planner(goal, memory, reason, obs):
        plans.append((reason, obs.png))
        return ["Read current result"]
    result = run(scripted(reply(act("navigate", text="https://example.test/")),
                          reply(act("fail", summary="result unclear")), reply(act("done", summary="observed"))),
                 mac, planner=planner)
    assert result.outcome == "done" and mac.acted == ["navigate"]
    assert any("model reported failure" in reason and png == b"fresh" for reason, png in plans)
    assert sum(row.get("outcome") == "browser_failure_replanned" for row in result.log) == 1


def test_browser_failure_retry_is_bounded_and_checkpointed():
    mac = FakeMac([("fresh", [])], front=("Chrome (browser)", 10))
    saved = []
    checkpoint = type("Checkpoint", (), {"load": lambda self: None, "save": lambda self, value: saved.append(value),
                                         "clear": lambda self: None})()
    result = run(scripted(reply(act("navigate", text="https://example.test/")),
                          reply(act("fail", summary="unclear")), reply(act("fail", summary="still unclear"))),
                 mac, planner=lambda *args: ["Read result"], checkpoint=checkpoint)
    assert result.reason == "model_reported_failure" and mac.acted == ["navigate"]
    assert saved[-1]["browser_failure_replanned"] is True


@pytest.mark.parametrize("summary", ["Please log in", "CAPTCHA requires you", "Payment needs approval", "Enter credentials"])
@pytest.mark.parametrize("operation", ["needs_you", "fail"])
def test_browser_founder_handoff_is_not_replanned(summary, operation):
    mac = FakeMac([("fresh", [])], front=("Chrome (browser)", 10))
    result = run(scripted(reply(act(operation, summary=summary))), mac, planner=lambda *args: ["Inspect"])
    assert result.reason == ("needs_founder" if operation == "needs_you" else "model_reported_failure") and mac.acted == []
    assert not any(row.get("outcome") == "browser_failure_replanned" for row in result.log)


def test_one_operation_can_complete_multiple_plan_hints_with_whole_task_proof():
    mac = FakeMac([("a", []), ("b", [{"id": "proof", "value": "sorted and formatted"}])])
    seen = []
    def verify(claim, obs, memory):
        seen.append(memory)
        return any(e.get("value") == "sorted and formatted" for e in obs.elements), "whole task proof"
    result = run(scripted(reply(act("key", chord="cmd+a")), reply(act("done", summary="formatted and sorted")),
                          reply(act("done", summary="formatted and sorted")), reply(act("done", summary="formatted and sorted"))),
                 mac, planner=lambda *a: ["open sheet", "format cells", "sort rows"], verify=verify)
    assert result.outcome == "done" and len(seen) == 1 and "sort rows" in seen[0]
    assert any(row.get("completed_subgoals") == 3 for row in result.log)


def test_browser_rejected_whole_claim_checks_progress_without_replanning_completed_work():
    mac = FakeMac([("result", [{"id": "proof", "value": "all requested items"}])], front=("Chrome (browser)", 10))
    plans, contexts, turns = [], [], []
    def planner(goal, memory, reason, obs):
        plans.append(reason)
        return ["Open page", "Add first", "Add second", "Add third"] if len(plans) == 1 else ["Read current result"]
    def decide(payload):
        turns.append(payload["subgoals"])
        return reply(act("done", summary="All requested items observed")), {}
    verdicts = iter([(False, "Need current result evidence"), (True, "Current source matches whole task")])
    result = run(decide, mac, planner=planner, verify=lambda claim, obs, context: (contexts.append(context), next(verdicts))[1])
    assert result.outcome == "done" and not mac.acted
    assert turns[-1] == ["Open page", "Add first", "Add second", "Add third"] and len(plans) == 1
    assert all("Add second" not in context and "Add third" not in context for context in contexts)
    assert any(row.get("outcome") == "subgoal_reported_complete" for row in result.log)


@pytest.mark.parametrize("operation", ["done", "observed_subgoal_done"])
def test_each_subgoal_completion_advances_without_dispatch_and_only_final_done_verifies(operation):
    mac = FakeMac([("current source", [])], front=("Chrome (browser)", 10))
    indices, verified, statuses = [], [], []
    def decide(payload):
        indices.append(payload["subgoal"])
        if "TERMINAL_ANSWER_ONLY" in payload["hint"]:
            return reply(act("done", summary="The observed requested fact is source-71")), {}
        if operation == "observed_subgoal_done":
            return reply(act("wait", ms=1), subgoal_done=True), {}
        return reply(act("done", summary="Current sub-goal complete")), {}
    result = run(decide, mac, planner=lambda *args: ["Open source", "Add first", "Add second"],
                 status=statuses.append, verify=lambda *args: (verified.append(args) or True, "whole task proof"))
    assert len(verified) == (2 if operation == "observed_subgoal_done" else 1) and not mac.acted and result.outcome == "done"
    if operation == "done":
        assert indices == [0] and result.steps == 0
    else:
        assert indices == [0, 1, 2, 2] and result.steps == 2
        assert result.summary == "The observed requested fact is source-71"
        assert "part 2 of 3" in statuses[0] and "part 3 of 3" in statuses[1]
        assert sum(row.get("outcome") == "subgoal_reported_complete" for row in result.log) == 2


@pytest.mark.parametrize("terminal", [act("done", summary="source-71"), act("done", summary="wrong fact"),
                                      act("click", element_id="b0")])
def test_final_observed_progress_requires_answer_only_turn_and_exact_answer_verification(terminal):
    mac = FakeMac([("source", [])], front=("Chrome (browser)", 10))
    calls, claims = [], []
    def decide(payload):
        calls.append(payload)
        if len(calls) == 1:
            return reply(act("wait", ms=1), subgoal_done=True), {}
        assert "TERMINAL_ANSWER_ONLY" in payload["hint"]
        return reply(terminal), {}
    def verify(claim, obs, context):
        claims.append(claim)
        return len(claims) == 1 or claim == "source-71", "independent source check"
    result = run(decide, mac, planner=lambda *args: ["Read source fact"], verify=verify)
    assert not mac.acted and len(calls) == 2
    if terminal["op"] == "click":
        assert result.reason == "terminal_answer_action_refused" and len(claims) == 1
    elif terminal["summary"] == "wrong fact":
        assert result.reason == "terminal_answer_unverified" and len(claims) == 2
    else:
        assert result.outcome == "done" and result.summary == "source-71" and len(claims) == 2


def test_subgoal_completion_spends_step_cap_and_is_checkpointed(tmp_path):
    checkpoint = FileCheckpoint(tmp_path / "progress.json")
    mac = FakeMac([("same source", [])], front=("Chrome (browser)", 10))
    result = run(scripted(reply(act("wait", ms=1), subgoal_done=True)), mac, checkpoint=checkpoint,
                 planner=lambda *args: ["One", "Two", "Three"],
                 caps=Caps(max_steps=1, explicit=True), verify=lambda *args: pytest.fail("early verifier"))
    assert result.reason == "step_cap_reached" and result.steps == 1 and not mac.acted
    saved = checkpoint.load()
    assert saved["subgoal"] == 1 and saved["steps"] == 1 and saved["pending_dispatch"] is False


def test_no_plan_or_final_subgoal_never_bypasses_negative_whole_task_verification():
    for plan in ([], ["Whole task"]):
        checked = []
        result = run(scripted(reply(act("done", summary="Not proven")), reply(act("fail", summary="stop"))),
                     FakeMac([("result", [])]), planner=lambda *args: plan,
                     verify=lambda *args: (checked.append(args) or False, "Required result missing"))
        assert result.outcome != "done" and len(checked) == 1


def test_replan_keeps_partial_source_and_actor_continues_with_missing_subgoal():
    mac = FakeMac([("one item already present", [])], front=("Chrome (browser)", 10))
    seen, plans = [], []
    def planner(goal, memory, reason, obs):
        plans.append((reason, obs.png))
        return ["Read result"] if len(plans) == 1 else ["Add remaining items", "Check result"]
    answers = iter([reply(act("fail", summary="Result incomplete")), reply(act("done", summary="Remaining items added")),
                    reply(act("done", summary="Whole task checked"))])
    def decide(payload):
        seen.append((payload["subgoals"], payload["subgoal"], payload["observation"].png))
        return next(answers), {}
    result = run(decide, mac, planner=planner)
    assert result.outcome == "done" and not mac.acted
    assert seen[1:] == [(["Add remaining items", "Check result"], 0, b"one item already present")]
    assert "Skip ALL already satisfied" in plans[1][0]


def test_actor_state_names_current_subgoal_and_existing_explicit_completion_contract():
    text = loop.build_user_text("Approved task", ["Open page", "Read result"], 1, "", [], "Chrome (browser)", "", [])
    assert "SUB-GOAL 2 OF 2: Read result" in text
    assert "subgoal_done=true" in loop.BROWSER_SYSTEM and "NEVER just one step" in loop.BROWSER_SYSTEM
    assert "CURRENT observed state" in loop.BROWSER_SYSTEM


def test_progress_checker_preserves_receipt_policy_and_authorized_corrections():
    assert loop.SUBGOAL_VERIFY_PROMPT.startswith(loop.VERIFY_PROMPT)
    assert 'qualified delivered fields plus an accepted result' in loop.SUBGOAL_VERIFY_PROMPT
    assert 'authorized validation corrections are not value mismatches' in loop.SUBGOAL_VERIFY_PROMPT
    assert 'Restore cleared task values' in loop.BROWSER_SYSTEM


def test_verifier_does_not_invent_optional_field_requirements_or_drop_task_requirements():
    assert 'Never invent requirements for optional fields' in loop.VERIFY_PROMPT
    assert 'Still require every task-specified value even when its field has required=false' in loop.VERIFY_PROMPT
    assert 'an absent required flag is unknown, not false' in loop.VERIFY_PROMPT
    assert 'whose responses include a POST/PUT/PATCH/DELETE with status below 400' in loop.VERIFY_PROMPT
    assert loop.SUBGOAL_VERIFY_PROMPT.startswith(loop.VERIFY_PROMPT)


def test_accepted_browser_result_is_verified_before_actor_reopens_or_escalates():
    mac = FakeMac([('Received', [])], front=('Chrome (browser)', 10))
    mac.submit_accepted = lambda: True
    checked = []
    result = run(scripted(reply(act('needs_you', summary='May I reopen the form to verify the accepted values?')),
                          reply(act('done', summary='Saved with the authorized correction'))),
                 mac, planner=lambda *args: ['Fill', 'Correct', 'Submit'],
                 verify=lambda *args: (checked.append(args) or True, 'Qualified fields and accepted result match task'))
    assert result.outcome == 'done' and not mac.acted and len(checked) == 2
    assert any(row.get('outcome') == 'goal_recheck_before_exit' for row in result.log)


@pytest.mark.parametrize("operation", ["done", "observed_subgoal_done"])
def test_unmatched_noop_claim_keeps_subgoal_and_returns_source_feedback(operation):
    seen, checks = [], []
    def decide(payload):
        seen.append((payload['subgoal'], list(payload['history'])))
        if len(seen) == 1:
            return (reply(act('wait', ms=1), subgoal_done=True) if operation == 'observed_subgoal_done'
                    else reply(act('done', summary='Step complete'))), {}
        return reply(act('needs_you', summary='Which project should I choose?')), {}
    result = run(decide, FakeMac([('unrelated page change', [])], front=('Chrome (browser)', 10)),
        planner=lambda *args: ['Submit accepted values', 'Read result'],
        verify_subgoal=lambda *args: (checks.append(args) or False, 'Code remains invalid; acceptance missing'),
        verify=lambda *args: (False, 'Whole task remains incomplete'))
    assert [index for index, _ in seen] == [0, 0] and len(checks) == 1
    assert 'Code remains invalid' in str(seen[1][1])
    assert not any(row.get('outcome') == 'subgoal_reported_complete' for row in result.log)


def test_applied_field_change_does_not_prove_accepted_subgoal():
    mac = FakeMac([('Code remains invalid', [{'id': 'b0', 'ax_role': 'input', 'label': 'Code'}])],
                  front=('Chrome (browser)', 10))
    mac.act = lambda *args: 'type ok (0.001s)'
    seen, checks = [], []
    def decide(payload):
        seen.append((payload['subgoal'], list(payload['history'])))
        return (reply(act('type', element_id='b0', text='abc123'), subgoal_done=True) if len(seen) == 1
                else reply(act('needs_you', summary='Which project should I choose?'))), {}
    result = run(decide, mac, planner=lambda *args: ['Submit accepted code', 'Read result'],
                 verify_subgoal=lambda *args: (checks.append(args) or False, 'Code still invalid; submit not accepted'),
                 verify=lambda *args: pytest.fail('whole task not yet claimed'))
    assert [index for index, _ in seen] == [0, 0] and len(checks) == 1
    assert 'Code still invalid' in str(seen[1][1])
    assert not any(row.get('outcome') == 'subgoal_reported_complete' for row in result.log)


def test_uncertain_subgoal_verifier_is_not_replayed_and_cannot_advance(tmp_path):
    import httpx
    checks = []
    checkpoint = FileCheckpoint(tmp_path / 'uncertain-progress.json')
    def verify(*args):
        checks.append(args)
        raise httpx.ReadTimeout('private provider outcome unknown')
    result = run(scripted(reply(act('wait', ms=1), subgoal_done=True)),
        FakeMac([('current page', [])], front=('Chrome (browser)', 10)), checkpoint=checkpoint,
        planner=lambda *args: ['Submit', 'Read result'], verify_subgoal=verify)
    assert result.reason == 'model_unavailable' and len(checks) == 1 and checkpoint.load()['subgoal'] == 0
    assert 'private provider' not in str(result.log)
    repeated = run(scripted(reply(act('done'))), FakeMac([('same page', [])]), checkpoint=checkpoint,
        planner=lambda *args: pytest.fail('unknown paid check cannot replan'),
        verify_subgoal=lambda *args: pytest.fail('unknown paid check cannot replay'))
    assert repeated.reason == 'checkpoint_subgoal_verification_requires_review'
    assert 'Review the current page' in repeated.summary


def test_ungated_browser_handoff_gets_one_validation_replan_without_accepting_done():
    mac = FakeMac([("validation", [{"id": "field", "value": "invalid"}])], front=("Chrome (browser)", 10))
    plans = []
    result = run(scripted(reply(act("needs_you", summary="Form rejected my values")),
                          reply(act("needs_you", summary="Still rejected"))), mac,
                 planner=lambda *args: plans.append(args[2]) or ["Correct offending field"],
                 verify=lambda *args: (False, "Not accepted"))
    assert result.reason == "needs_founder" and len(plans) == 2 and not mac.acted
    assert "validation messages" in plans[1]


def test_uncertain_browser_verification_never_replans_the_paid_call():
    import httpx
    mac = FakeMac([("result", [])], front=("Chrome (browser)", 10))
    plans = []
    def verify(*args):
        raise httpx.ReadTimeout("outcome uncertain")
    result = run(scripted(reply(act("done", summary="Observed")),
                          reply(act("needs_you", summary="Review the uncertain provider result?"))), mac,
                 planner=lambda *args: plans.append(args[2]) or ["Inspect"], verify=verify)
    assert result.reason == "needs_founder" and len(plans) == 1 and not mac.acted
    assert not any(row.get("outcome") == "browser_failure_replanned" for row in result.log)


@pytest.mark.parametrize("gate", ["captcha", "sign_in", "code"])
@pytest.mark.parametrize("operation", ["needs_you", "done", "observed_subgoal_done"])
def test_browser_observed_human_gate_never_replans_or_verifies(gate, operation):
    mac = FakeMac([("gate", [])], front=("Chrome (browser)", 10))
    original = mac.observe
    def observe():
        obs = original()
        obs.human = gate
        return obs
    mac.observe = observe
    plans = []
    answer = (reply(act("wait", ms=1), subgoal_done=True) if operation == "observed_subgoal_done"
              else reply(act(operation, summary="Cannot continue")))
    result = run(scripted(answer), mac,
                 planner=lambda *args: plans.append(args[2]) or ["Inspect", "Continue"],
                 verify=lambda *args: pytest.fail("human gate bypassed"))
    assert result.outcome != "done" and len(plans) == 1 and not mac.acted
    assert not any(row.get("outcome") == "subgoal_reported_complete" for row in result.log)


def test_explicit_browser_approval_handoff_pauses_without_alternative_replans():
    mac = FakeMac([("fresh", [])], front=("Chrome (browser)", 10))
    pauses, plans = [], []
    result = run(scripted(reply(act("needs_you", summary="Payment requires founder approval"))), mac,
                 planner=lambda *args: plans.append(args[2]) or ["Inspect"],
                 pause=lambda *args: pauses.append(args) or "stopped")
    assert result.outcome == "stopped" and len(pauses) == len(plans) == 1 and not mac.acted


def test_missing_explicit_saved_output_cannot_be_declared_done(tmp_path, monkeypatch):
    from coding_agents import cu_ai_tools as tools
    monkeypatch.setattr(tools.os.path, "expanduser", lambda path: path.replace("~", str(tmp_path), 1))
    result = run_loop("Save the report as ~/report.txt", scripted(reply(act("done", summary="saved")),
        reply(act("fail", summary="unable"))), FakeMac([("a", [])]), threading.Event(), verify=lambda *a: (True, ""))
    assert result.outcome == "failed" and not (tmp_path / "report.txt").exists()


def test_gui_saved_output_is_read_back_even_without_fs_tool(tmp_path, monkeypatch):
    from coding_agents import cu_ai_tools as tools
    monkeypatch.setattr(tools.os.path, "expanduser", lambda path: path.replace("~", str(tmp_path), 1))
    mac = FakeMac([("a", []), ("b", [])])
    def save(action, obs):
        (tmp_path / "report.txt").write_text("verified output")
        return "ok"
    monkeypatch.setattr(mac, "act", save)
    seen = []
    result = run_loop("Save the report as ~/report.txt", scripted(reply(act("key", chord="cmd+s")),
        reply(act("done", summary="saved"))), mac, threading.Event(),
        verify=lambda summary, obs, memory: (seen.append(memory), ("verified output" in memory, "missing disk evidence"))[1])
    assert result.outcome == "done" and "file," in seen[0]


def test_non_file_goal_and_explicit_no_save_goal_remain_supported(tmp_path, monkeypatch):
    from coding_agents import cu_ai_tools as tools
    monkeypatch.setattr(tools.os.path, "expanduser", lambda path: path.replace("~", str(tmp_path), 1))
    for goal in ("Open Notes and display the folder list", "Open ~/report.txt but do not save it", "Create a note titled ~/report.txt but do not save it", "Write ~/report.txt on a sticky note"):
        result = run_loop(goal, scripted(reply(act("done", summary="visible"))), FakeMac([("a", [])]),
                          threading.Event(), verify=lambda *a: (True, ""))
        assert result.outcome == "done"


def test_completed_checkpoint_stays_complete_and_advance_is_idempotent():
    state = LoopState(subgoals=["one", "two"], subgoal=1)
    assert state.advance() and state.advance() and state.subgoal == 2
    restored = LoopState()
    assert restored.restore(state.snapshot("goal"), "goal")
    assert restored.subgoal == 2


def test_input_path_is_not_an_output_requirement(tmp_path, monkeypatch):
    from coding_agents import cu_ai_tools as tools
    monkeypatch.setattr(tools.os.path, "expanduser", lambda path: path.replace("~", str(tmp_path), 1))
    expected = tmp_path / "report.txt"
    outputs = tools.required_outputs("Read ~/input.csv; export the resulting report to ~/report.txt")
    assert outputs == {expected: "file"}
    assert not tools.required_outputs("Do not save the report to ~/report.txt")
    assert not tools.required_outputs("Save time by reading ~/input.csv")
    assert not tools.required_outputs("Create a file later, first open ~/input.csv")
    assert tools.required_outputs("Create the file ~/report.txt") == {expected: "file_or_directory"}
    assert tools.required_outputs("Download ~/report.txt") == {expected: "file"}


def test_stop_during_completion_verification_never_becomes_done():
    stop = threading.Event()
    def verify(*args):
        stop.set()
        return True, "verified"
    result = run(scripted(reply(act("done", summary="finished"))), FakeMac([("a", [])]), stop=stop, verify=verify)
    assert result.outcome == "stopped" and result.reason == "stopped_by_founder"
    assert not any(row.get("outcome") == "whole_task_verified" for row in result.log)


def test_completion_never_reads_explicitly_excluded_private_input(tmp_path, monkeypatch):
    from coding_agents import cu_ai_tools as tools
    monkeypatch.setattr(tools.os.path, "expanduser", lambda path: path.replace("~", str(tmp_path), 1))
    (tmp_path / "out.txt").write_text("public report")
    (tmp_path / "private.txt").write_text("private input must not reach verifier")
    opened = []
    original_open = tools.os.open
    def track_open(path, *args, **kwargs):
        opened.append(str(path))
        return original_open(path, *args, **kwargs)
    monkeypatch.setattr(tools.os, "open", track_open)
    seen = []
    result = run_loop("Save the report as ~/out.txt; do not read ~/private.txt", scripted(reply(act("done", summary="saved"))),
        FakeMac([("a", [])]), threading.Event(), verify=lambda claim, obs, context: (seen.append(context), (True, ""))[1])
    assert result.outcome == "done" and "public report" in seen[0]
    assert not any(path == "private.txt" or path.endswith("/private.txt") for path in opened)
    assert "private input must not reach verifier" not in seen[0] and "private.txt" not in seen[0]

@pytest.mark.parametrize('fields,code', [
    ({'chord': 'write_text'}, 'action_fs_path_missing'),
    ({'app': '/tmp/reviewed', 'chord': 'invented-private-value'}, 'action_fs_verb_invalid'),
])
def test_fs_parse_diagnostics_distinguish_contract_failures_without_echo(fields, code):
    from coding_agents.cu_ai_loop import validation_diagnostic
    with pytest.raises(ActionError) as caught:
        parse_reply(reply(act('fs', **fields)))
    assert validation_diagnostic(caught.value) == code
    assert 'private-value' not in str(caught.value)

@pytest.mark.parametrize('fields,expected', [
    ({'chord': 'write_text'}, 'fs.path must contain the reviewed filesystem path'),
    ({'app': '/tmp/reviewed', 'chord': 'invented-private-value'}, 'fs.fs_op must be mkdir, move, copy'),
])
def test_fs_validation_feedback_explains_known_contract_without_raw_fields(fields, expected):
    payloads = []
    replies = iter([reply(act('fs', **fields)), reply(act('done'))])
    def decide(payload):
        payloads.append(payload)
        return next(replies), {}
    result = run(decide, FakeMac([('same', [])]))
    assert result.outcome == 'done'
    assert expected in str(payloads[-1]['history'])
    assert 'invented-private-value' not in str(result.log) + str(payloads[-1]['history'])


def test_canonical_fs_reaches_existing_scoped_executor_without_using_application_fields(monkeypatch):
    from coding_agents import cu_ai_tools
    calls = []
    monkeypatch.setattr(cu_ai_tools, 'run_fs', lambda verb, source, dest, goal, touched, **kw:
        calls.append((verb, source, dest, goal)) or 'listed')
    result = run(scripted(reply(act('fs', path='/tmp/reviewed', fs_op='list')), reply(act('done'))),
                 FakeMac([('same', [])]))
    assert result.outcome == 'done'
    assert calls == [('list', '/tmp/reviewed', '', 'save the note')]
    assert '/tmp/reviewed' not in str(result.log)


def test_private_shaped_provider_exception_never_becomes_public_failure_reason():
    def decide(payload):
        raise RuntimeError('private_api_token')
    result = run(decide, FakeMac([('same', [])]))
    assert result.reason == 'model_unavailable'
    assert 'private_api_token' not in str(result.log) + result.reason


@pytest.mark.parametrize('failure', [True, False])
def test_route_retry_budget_survives_ax_pixel_aliases_clock_and_model_claims(failure):
    class RouteMac:
        def __init__(self): self.calls, self.observations = [], 0
        def observe(self):
            self.observations += 1
            return Observation(str(self.observations).encode(), [{'id': f'volatile{self.observations}',
                'role': 'AXStaticText', 'label': f'clock {self.observations}'}], 'TextEdit', 'Doc', str(self.observations), pid=1)
        def frontmost(self): return ('TextEdit', 1)
        def focused_role(self): return None
        def activate(self, app): pass
        def act(self, action, obs):
            self.calls.append(action.op)
            return 'error: fixed_recipient_failure' if failure else 'clicked'
    mac, plans, turns = RouteMac(), [], []
    def decide(payload):
        turns.append(payload)
        if len(turns) > 12:
            return reply(act('fail')), {}
        action = act('click', element_id=f'volatile{len(turns)}') if len(turns) % 2 else act('double_click', x=50, y=60)
        return reply(action, subgoal_done=True, memory=f'completed {len(turns)}'), {}
    def planner(*args):
        plans.append(args[2])
        return ['Perform task']
    result = run_loop('Inspect TextEdit', decide, mac, threading.Event(), planner=planner,
        caps=Caps(max_steps=40, repeat_fail=99, no_progress_fail=99, max_replans=4, explicit=True))
    assert result.reason == 'route_retry_exhausted'
    assert len(mac.calls) == 3
    assert len(plans) == 2  # initial plan and one alternative, never another paid replay
    assert any(row.get('phase') == 'retry' and row.get('failure_code') == 'route_retry_exhausted' for row in result.log)


def test_route_guard_checkpoint_never_clears_retirement_or_trusts_claims():
    from desktop_runtime.cu_ai_retries import RetryGuard
    key = 'a' * 64
    guard = RetryGuard()
    assert guard.failed(key, 'click', 'native_action_failed') is None
    snapshot = guard.snapshot()
    guard.failed(key, 'click', 'native_action_failed')
    assert snapshot['counts'][key] == 1  # immutable checkpoint, not aliased mutable state
    assert guard.failed(key, 'click', 'action_no_observed_effect') == 'click'
    guard.recovery_used = True
    restored = RetryGuard()
    assert restored.restore(guard.snapshot())
    assert restored.rejected(key) and restored.recovery_used
    assert not RetryGuard().restore({'counts': {key: 0}, 'blocked': [], 'recovery_used': False})
    assert not RetryGuard().restore({'counts': {}, 'blocked': ['private path'], 'recovery_used': False})


def test_route_budget_cannot_clear_on_png_clock_or_ephemeral_ids():
    from desktop_runtime.cu_ai_retries import RetryGuard, semantic_state
    before = Observation(b'first', [{'id':'e1', 'role':'AXStaticText', 'label':'10:00'}], 'TextEdit', 'Doc', 'capture1', pid=1)
    after = Observation(b'changed', [{'id':'e11', 'role':'AXStaticText', 'label':'10:01'}], 'TextEdit', 'Renamed Doc', 'capture2', pid=1)
    assert semantic_state(before) == semantic_state(after)
    guard = RetryGuard()
    key = 'b' * 64
    guard.failed(key, 'click', 'native_action_failed')
    guard.failed(key, 'click', 'native_action_failed')
    guard.pending = [(key, 'click', semantic_state(before))]
    assert guard.observe(after) == 'click'
    assert guard.rejected(key)


def test_route_retirement_allows_one_different_authorized_route_then_goal_verifier():
    class Alternate(FakeMac):
        def __init__(self):
            super().__init__([('same', [])])
            self.calls = []
        def act(self, a, o):
            self.calls.append(a.op)
            return 'error: same_failure' if a.op == 'click' else 'pressed tab'
    mac = Alternate()
    choices = iter([reply(act('click', x=30, y=40))] * 3 + [reply(act('key', chord='tab')), reply(act('done'))])
    result = run_loop('Inspect TextEdit', lambda p: (next(choices), {}), mac, threading.Event(),
                      planner=lambda *a: ['Try an authorized route'], verify=lambda *a: (True, ''),
                      caps=Caps(max_steps=15, repeat_fail=99, no_progress_fail=99, max_replans=1, explicit=True))
    assert result.outcome == 'done' and mac.calls == ['click', 'click', 'click', 'key']



def test_route_budget_does_not_reset_on_repeated_focus_or_checkbox_cycles():
    from desktop_runtime.cu_ai_retries import RetryGuard, semantic_state
    key = 'c' * 64
    def observation(checked):
        return Observation(b'x', [{'role':'checkbox', 'checked': checked, 'focused': checked}], 'Chrome', '', '', pid=1)
    guard = RetryGuard()
    first, second = observation(False), observation(True)
    guard.pending = [(key, 'click', semantic_state(first))]
    assert guard.observe(second) is None  # first meaningful novel transition is progress
    for before, after in [(second, first), (first, second)]:
        guard.pending = [(key, 'click', semantic_state(before))]
        assert guard.observe(after) is None
    guard.pending = [(key, 'click', semantic_state(second))]
    assert guard.observe(first) == 'click'  # oscillation is not forward progress
    restored = RetryGuard()
    assert restored.restore(guard.snapshot()) and restored.rejected(key)


def test_engine_fixed_applied_metadata_only_for_closed_single_effects():
    from desktop_runtime.cu_ai_retries import acknowledged_effect
    assert acknowledged_effect(Action('click'), 'click e11')
    assert acknowledged_effect(Action('type'), 'typed 3 chars into scoped text element')
    assert acknowledged_effect(Action('menu'), 'menu Help > Report error')
    assert acknowledged_effect(Action('navigate'), 'navigate ok (0.1s)')
    assert not acknowledged_effect(Action('click'), 'ok')
    assert not acknowledged_effect(Action('click'), 'refused: target gone')
    assert not acknowledged_effect(Action('wait'), 'waited 300ms')
    assert not acknowledged_effect(Action('open_app'), 'launched exact application; launch_focus_uncertain; fresh observation required')
    assert not acknowledged_effect(Action('fs', path='~/file', fs_op='read_text'), 'read (3 bytes)')


def test_completion_rejection_new_scoped_state_can_be_independently_verified():
    observed = [{"ax_role": "AXTextField", "value": "before"}]
    changed = [{"ax_role": "AXTextField", "value": "after"}]
    mac = FakeMac([("same", observed)] * 3 + [("same", changed)])
    calls, plans = [], []
    def verifier(claim, obs, context):
        calls.append(obs.elements[0]['value'])
        return obs.elements[0]['value'] == 'after', 'not yet'
    result = run(lambda _: (reply(act('done')), {}), mac, verify=verifier,
                 planner=lambda *args: plans.append(1) or ['verify'])
    assert result.outcome == 'done' and len(calls) == 4 and len(plans) == 2


def test_completion_private_diagnostic_is_opt_in_and_sink_failure_cannot_pass():
    diagnostics = []
    result = run(scripted(reply(act('done')), reply(act('fail'))), FakeMac([('same', [])]),
                 verify=lambda *args: (False, 'PRIVATE verifier reason'), verifier_diagnostic=diagnostics.append)
    assert diagnostics[0]['reason'] == 'PRIVATE verifier reason'
    assert 'PRIVATE' not in str(result.log)
    def broken(value):
        raise RuntimeError('PRIVATE sink')
    result = run(scripted(reply(act('done'))), FakeMac([('same', [])]),
                 verify=lambda *args: (True, 'proof'), verifier_diagnostic=broken)
    assert result.outcome == 'done'


def test_completion_retry_checkpoint_denies_replay():
    from desktop_runtime.cu_ai_retries import RetryGuard, completion_key
    obs = FakeMac([('same', [])]).observe()
    key = completion_key(obs, '', '')
    guard = RetryGuard()
    for _ in range(3):
        guard.failed(key, 'done', 'independent_verifier_rejected')
    restored = RetryGuard()
    assert restored.restore(guard.snapshot()) and restored.rejected(key)
    # Changing untrusted summary/notes is not part of this identity.
    assert completion_key(obs, '', '') == key


def test_completion_evidence_closed_schema_and_sequence_not_progress():
    import json
    from desktop_runtime.cu_ai_retries import completion_context, completion_key
    mac = FakeMac([('same', [])])
    receipt = {'v': 1, 'source': 'owned_browser_backend', 'actions': [
        {'seq': 1, 'page_scope': 'a' * 32, 'action': 'type', 'status': 'ok', 'action_input_matched': True}], 'responses': []}
    mac.completion_evidence = lambda: receipt
    first = completion_context(mac)
    assert 'not business acceptance' in first and json.dumps(receipt, sort_keys=True) in first
    obs = mac.observe()
    key = completion_key(obs, '', first)
    receipt['actions'][0]['seq'] = 2
    receipt['actions'].append(dict(receipt['actions'][0]))
    assert completion_key(obs, '', completion_context(mac)) == key
    receipt['actions'][0]['body'] = 'PRIVATE'
    assert completion_context(mac) == ''
    assert completion_key(obs, 'actual fresh file digest', '') != completion_key(obs, '', '')


def test_stop_at_third_completion_rejection_wins_over_recovery():
    stop, calls = threading.Event(), []
    def verify(*args):
        calls.append(1)
        if len(calls) == 3:
            stop.set()
        return False, 'rejected'
    result = run(lambda _: (reply(act('done')), {}), FakeMac([('same', [])]), stop=stop, verify=verify)
    assert result.outcome == 'stopped' and len(calls) == 3


def test_completion_evidence_is_reset_before_observe_and_reset_failure_fails_closed():
    mac = FakeMac([('same', [])])
    events = []
    observe = mac.observe
    mac.reset_completion_evidence = lambda: events.append('reset')
    mac.observe = lambda: (events.append('observe'), observe())[1]
    assert run(scripted(reply(act('done'))), mac).outcome == 'done'
    assert events[:2] == ['reset', 'observe']
    def broken():
        raise ValueError('PRIVATE')
    mac.reset_completion_evidence = broken
    result = run(scripted(reply(act('done'))), mac)
    assert result.reason == 'completion_evidence_reset_failed' and 'PRIVATE' not in str(result.log)


def test_mixed_file_browser_verification_survives_provider_context_budget(monkeypatch):
    import json
    from coding_agents.cu_ai_loop import verify_text
    from coding_agents import cu_ai_tools
    receipt = {'v': 1, 'source': 'owned_browser_backend', 'actions': [
        {'seq': 1, 'page_scope': 'a' * 32, 'action': 'type', 'status': 'ok', 'action_input_matched': True}], 'responses': []}
    mac = FakeMac([('same', [])])
    mac.completion_evidence = lambda: receipt
    monkeypatch.setattr(cu_ai_tools, 'evidence', lambda _: 'file-content-' * 400)
    rendered = []
    def verifier(claim, obs, context):
        rendered.append(verify_text('task', claim, obs.frontmost, obs.window_title, obs.elements, context))
        return False, 'unproven'
    run(scripted(reply(act('done'))), mac, verify=verifier)
    assert rendered and 'owned_browser_backend' in rendered[0]
    assert json.dumps(receipt, separators=(',', ':'), sort_keys=True) in rendered[0]
    assert 'file evidence truncated' in rendered[0]
    assert len(rendered[0].encode()) <= 10240


def test_verification_budget_preserves_whole_receipts_and_marks_each_omission():
    import json
    from coding_agents.cu_ai_loop import verify_text
    from coding_agents.cu_verification_context import compose
    from desktop_runtime.cu_completion_evidence import validated_evidence
    row = {'seq': 1, 'page_scope': 'a' * 32, 'action': 'type', 'status': 'ok', 'element_index': 1,
           'before': {'field_type': 'text', 'checked': None}, 'after': {'field_type': 'text', 'checked': None}, 'action_input_matched': True}
    response = {'action_seq': 1, 'page_scope': 'a' * 32, 'method': 'POST', 'status': 202, 'resource': 'fetch'}
    envelope = {'v': 1, 'source': 'owned_browser_backend', 'actions': [dict(row, seq=i + 1) for i in range(16)], 'responses': [dict(response, action_seq=i + 1) for i in range(16)]}
    context = compose(['plan' * 1000], 'file' * 1000, '\n' + json.dumps(envelope), 'note' * 1000)
    whole = json.loads(context.splitlines()[1])
    assert validated_evidence(whole) == whole
    assert whole['actions'][-1]['seq'] == whole['responses'][-1]['action_seq'] == 16
    assert 'older whole browser receipts omitted' in context
    assert 'file evidence truncated' in context and 'planned checks truncated' in context and 'agent notes truncated' in context
    assert len(envelope['actions']) == len(envelope['responses']) == 16
    request = verify_text('任务' * 4000, 'claim' * 5000, 'app', 'title', [{'ax_role': 'AXTextArea', 'value': 'x' * 5000}], context, 'page' * 2000)
    assert len(request.encode()) <= 10240
    assert json.dumps(whole, separators=(',', ':'), sort_keys=True) in request

@pytest.mark.parametrize('changed', ['execution_id','review_digest','workspace_id','actor_id','device_id','reservation_id'])
def test_bound_checkpoint_cannot_cross_claim_identity(tmp_path,changed):
    binding={key:'owned-'+key for key in ('execution_id','review_digest','workspace_id','actor_id','device_id','reservation_id')}
    checkpoint=FileCheckpoint(tmp_path/'bound.json',binding=binding)
    checkpoint.save({**LoopState().snapshot('synthetic goal'),'pending_dispatch':False})
    other={**binding,changed:'foreign'}
    with pytest.raises(ValueError):
        FileCheckpoint(checkpoint.path,binding=other).load()


def test_crash_after_dispatch_persists_charge_and_holds_resume(tmp_path):
    stop=threading.Event()
    checkpoint=FileCheckpoint(tmp_path/'ck.json')
    mac=FakeMac([('a',[])])
    effects=[]
    def unknown_effect(a,obs):
        effects.append(a.op)
        raise KeyboardInterrupt('synthetic crash')
    mac.act=unknown_effect
    with pytest.raises(KeyboardInterrupt):
        run_loop('synthetic goal',lambda p:(reply(act('key',chord='cmd+s')),{}),mac,stop,
                 caps=Caps(max_steps=1,explicit=True),checkpoint=checkpoint)
    saved=checkpoint.load()
    assert effects==['key'] and saved['steps']==1 and saved['pending_dispatch'] is True
    result=run_loop('synthetic goal',lambda p:pytest.fail('pending effect cannot request another proposal'),
                    mac,stop,caps=Caps(max_steps=1,explicit=True),checkpoint=checkpoint)
    assert result.reason=='checkpoint_dispatch_requires_reconciliation' and result.steps==1
    assert effects==['key']


def test_checkpoint_write_failure_prevents_native_dispatch():
    class BrokenCheckpoint:
        def load(self): return None
        def save(self,value): raise OSError('synthetic unavailable storage')
    mac=FakeMac([('a',[])])
    with pytest.raises(OSError):
        run_loop('synthetic goal',lambda p:(reply(act('key',chord='cmd+s')),{}),mac,threading.Event(),checkpoint=BrokenCheckpoint())
    assert mac.acted==[]


def test_restored_cumulative_cap_prevents_observation_or_model(tmp_path):
    checkpoint=FileCheckpoint(tmp_path/'ck.json')
    state=LoopState()
    state.steps=1
    checkpoint.save({**state.snapshot('synthetic goal'),'pending_dispatch':False})
    mac=FakeMac([('a',[])])
    mac.observe=lambda:pytest.fail('exhausted cumulative cap cannot observe')
    result=run_loop('synthetic goal',lambda p:pytest.fail('exhausted cumulative cap cannot call model'),
                    mac,threading.Event(),caps=Caps(max_steps=1,explicit=True),checkpoint=checkpoint)
    assert result.reason=='step_cap_reached' and result.steps==1

@pytest.mark.parametrize('kind',['other','captcha','code'])
def test_continue_after_uncertain_dispatched_input_never_mutates_twice(kind):
    from desktop_runtime.cu_ai_engine import NeedsHuman
    mac=FakeMac([('a',[])])
    attempts=[]
    pauses=[]
    def act_and_unknown(action,obs):
        attempts.append(action.op)
        if len(attempts)==1:
            raise NeedsHuman(kind,'synthetic uncertain input')
        return 'pressed cmd+s'
    mac.act=act_and_unknown
    def pause(kind,prompt,obs):
        pauses.append(kind)
        return 'continue'
    replies=iter([reply(act('key',chord='cmd+s')),reply(act('key',chord='cmd+s')),reply(act('done',summary='done'))])
    result=run_loop('synthetic goal',lambda p:(next(replies),{}),mac,threading.Event(),pause=pause,
                    caps=Caps(max_steps=4,explicit=True),verify=lambda *a:(True,''))
    assert attempts==['key']
    assert pauses==[kind]
    assert result.reason=='effect_reconciliation_required' and result.outcome!='done'


def test_known_pre_dispatch_pause_can_continue_without_unknown_effect():
    from desktop_runtime.cu_ai_engine import NeedsHuman
    mac=FakeMac([('a',[])])
    calls=[]
    effects=[]
    def pre_admission(action,obs):
        calls.append(action.op)
        if len(calls)==1:
            raise NeedsHuman('other','known local pre-admission',dispatch_state='not_dispatched')
        effects.append(action.op)
        return 'pressed cmd+s'
    mac.act=pre_admission
    replies=iter([reply(act('key',chord='cmd+s')),reply(act('key',chord='cmd+s')),reply(act('done',summary='done'))])
    result=run_loop('synthetic goal',lambda p:(next(replies),{}),mac,threading.Event(),
                    pause=lambda *a:'continue',caps=Caps(max_steps=4,explicit=True),verify=lambda *a:(True,''))
    assert calls==['key','key'] and effects==['key'] and result.outcome=='done'


def test_unknown_effect_continue_observation_failure_keeps_checkpoint_pending(tmp_path):
    from desktop_runtime.cu_ai_engine import NeedsHuman
    mac=FakeMac([('a',[])])
    checkpoint=FileCheckpoint(tmp_path/'ck.json')
    observed=[]
    observe=mac.observe
    def readback():
        observed.append(True)
        if len(observed)>1:
            raise RuntimeError('synthetic read-only unavailable')
        return observe()
    mac.observe=readback
    calls=[]
    def uncertain(action,obs):
        calls.append(action.op)
        raise NeedsHuman('other','unknown')
    mac.act=uncertain
    result=run_loop('synthetic goal',lambda p:(reply(act('key',chord='cmd+s')),{}),mac,threading.Event(),
                    pause=lambda *a:'continue',checkpoint=checkpoint)
    assert calls==['key'] and len(observed)==2
    assert result.reason=='effect_reconciliation_required'
    assert checkpoint.load()['pending_dispatch'] is True and checkpoint.load()['steps']==1


def test_parse_reply_first_json_object_ignores_trailing_text():
    import pytest
    obj = '{"actions":[{"op":"done","summary":"ok"}],"memory":"","subgoal_done":true}'
    for raw in (obj, obj + "\n" + obj, obj + " trailing words", "  " + obj + "}}"):
        assert loop.parse_reply(raw).actions[0].op == "done"
    for bad in (obj[:-1], "x " + obj, "[" + obj + "]", ""):
        with pytest.raises(loop.ActionError):
            loop.parse_reply(bad)


def test_request_effort_is_valid_per_model():
    body = loop.build_request_body("gpt-6.1-sol", loop.BROWSER_MARKER + " t", "")
    assert body["reasoning"] == {"effort": "low"} and body["max_output_tokens"] == 4900
    assert loop.build_text_request_body("gpt-6.1-sol", "s", "u", "plan", 700)["reasoning"] == {"effort": "low"}
    assert loop.build_request_body("gpt-5.6-luna", "u", "")["reasoning"] == {"effort": "none"}
