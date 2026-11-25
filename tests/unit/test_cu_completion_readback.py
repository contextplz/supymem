"""Deterministic-first completion: tool read-backs, give-up re-check, strategy rule, empty observations."""
import threading

import pytest

from coding_agents import cu_ai_tools as tools
from desktop_runtime.cu_ai_engine import Observation, run_loop

GOAL = "In the Notes app, create a note titled 'Zed' with body 'b'"
PROOF = "script ok (30 chars)" + tools.PRIVATE + "verified: note created, id x-coredata://1"


def act(op, **kw):
    return {"op": op, "element_id": "", "x": 0, "y": 0, "text": "", "chord": "", "direction": "", "amount": 0,
            "app": "", "ms": 0, "summary": "", **kw}


def reply(*a):
    return {"thought": "", "memory": "", "subgoal_done": False, "actions": list(a)}


class Mac:
    def __init__(self, elements=None, png=b"x"):
        self.elements, self.png, self.looks = elements if elements is not None else [], png, 0

    def observe(self):
        self.looks += 1
        return Observation(self.png, self.elements, "Notes", "Notes", "s", False, 1)

    def frontmost(self):
        return ("Notes", 1)

    def focused_role(self):
        return None

    def activate(self, name):
        pass

    def act(self, action, obs):
        return "ok"


def scripted(*items):
    it = iter(items)
    return lambda payload: (next(it), {})


def test_template_readback_is_deterministic_proof_and_bare_ok_is_not():
    assert tools.readback(PROOF).startswith("verified: note created")
    assert tools.readback("script ok (2 chars)" + tools.PRIVATE + "ok") is None
    assert tools.readback("refused: nope") is None


def test_give_up_after_a_verified_effect_is_rechecked_not_trusted(monkeypatch):
    monkeypatch.setattr(tools, "run_template", lambda *a, **k: PROOF)
    seen = []
    verdicts = iter([False, True, True])
    result = run_loop(GOAL, scripted(reply(act("template", chord="notes_create", text="{}")), reply(act("fail", summary="x")),
                                     reply(act("done", summary="Created Zed with body b."))),
                      Mac(), threading.Event(), verify=lambda s, o, ctx: (seen.append(ctx) or next(verdicts), ""))
    assert result.outcome == "done" and result.summary == "Created Zed with body b."
    assert len(seen) == 3 and all("NATIVE TOOL READ-BACK" in context for context in seen)


def test_visual_rejection_never_overrides_readback_it_is_done_unconfirmed(monkeypatch):
    monkeypatch.setattr(tools, "run_template", lambda *a, **k: PROOF)
    calls = []
    result = run_loop(GOAL, scripted(reply(act("template", chord="notes_create", text="{}")), reply(act("done", summary="saved"))),
                      Mac(), threading.Event(), verify=lambda *a: (calls.append(1) or False, "cannot see it"))
    assert result.outcome == "done_unconfirmed" and len(calls) == 2


def test_positive_tool_is_verified_before_redundant_reopen(monkeypatch):
    monkeypatch.setattr(tools, "run_template", lambda *a, **k: PROOF)
    proposals = []
    def decide(payload):
        proposals.append(payload)
        if len(proposals) == 2:
            assert 'TERMINAL_ANSWER_ONLY' in payload['hint']
            return reply(act('done', summary='Created Zed with body b.')), {}
        assert len(proposals) == 1
        return reply(act("template", chord="notes_create", text="{}")), {}
    mac = Mac()
    mac.act = lambda *a: pytest.fail('answer collection must not reopen the app')
    result = run_loop(GOAL, decide, mac, threading.Event(), verify=lambda *a: (True, ""))
    assert result.outcome == "done" and len(proposals) == 2


def test_partial_tool_whole_task_rejection_continues_remaining_actions(monkeypatch):
    monkeypatch.setattr(tools, "run_template", lambda *a, **k: PROOF)
    proposals = []
    verdicts = iter([(False, "another approved step remains"), (True, "")])
    def decide(payload):
        proposals.append(payload)
        return (reply(act("template", chord="notes_create", text="{}")) if len(proposals) == 1 else reply(act("done"))), {}
    result = run_loop(GOAL, decide, Mac(), threading.Event(), verify=lambda *a: next(verdicts))
    assert result.outcome == "done" and len(proposals) == 2


def test_fail_with_one_strategy_is_bounced_back_twice_then_accepted(strict_strategies=None):
    calls = []

    def decide(payload):
        calls.append(1)
        return reply(act("fail", summary="no")), {}
    import desktop_runtime.cu_ai_engine as eng
    eng.MIN_STRATEGIES = 2
    try:
        result = run_loop("do it", decide, Mac(), threading.Event())
    finally:
        eng.MIN_STRATEGIES = 0
    assert result.reason == "model_reported_failure" and len(calls) == 3


def test_empty_observation_without_screenshot_errors_after_retries():
    result = run_loop("do it", lambda p: (reply(act("done")), {}), Mac(png=b""), threading.Event(), verify=lambda *a: (True, ""))
    assert result.outcome == "failed" and result.reason == "observation_empty"
