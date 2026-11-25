"""Automatic effect verification must not leak an internal claim as the answer."""
import asyncio
import threading

import pytest

from coding_agents.cu_ai_loop import Caps
from desktop_runtime.cu_ai_engine import Observation, run_loop


GOAL = "Press the 'Advance' button exactly 2 times. Tell me the final count."


class Counter:
    def __init__(self):
        self.count = self.inputs = self.looks = 0

    def observe(self):
        self.looks += 1
        obs = Observation(str(self.count).encode(), [{"id": "b0", "ax_role": "button", "label": "Advance"}],
                          "Chrome (browser)", "Owned counter", "", False, None)
        obs.notes = f"PAGE TEXT: Current count: {self.count}"
        return obs

    def frontmost(self):
        return "Chrome (CDP)", 0

    def focused_role(self):
        return None

    def act(self, action, obs):
        self.inputs += 1
        self.count += 1
        return "click ok (0.01s)"


def reply(op, **kwargs):
    return {"actions": [{"op": op, **kwargs}]}, {"in": 7, "out": 3}


def run_case(*, answer=None, verify=None, stop=None, caps=None, mac=None, checkpoint=None):
    mac, proposals, claims = mac or Counter(), [], []
    stop = stop or threading.Event()
    def actor(payload):
        proposals.append(payload)
        if mac.count < 2:
            return reply("click", element_id="b0")
        assert "TERMINAL_ANSWER_ONLY" in payload["hint"]
        return answer(payload, mac, stop) if answer else reply("done", summary="The final count is 2.")
    def arbiter(claim, obs, evidence):
        claims.append((claim, obs, evidence))
        return verify(claim, obs, evidence, len(claims), stop) if verify else (True, "source count read back")
    result = run_loop(GOAL, actor, mac, stop, verify=arbiter,
                      caps=caps or Caps(max_steps=12, max_seconds=60, explicit=True), checkpoint=checkpoint)
    return result, mac, proposals, claims


def test_final_answer_is_grounded_reverified_and_model_usage_is_accounted():
    result, mac, proposals, claims = run_case()
    assert result.outcome == "done" and result.summary == "The final count is 2."
    assert mac.count == mac.inputs == 2 and len(proposals) == 3
    assert len(claims) == 2 and claims[1][0] == result.summary
    assert claims[1][1] is not claims[0][1]  # Fresh readback after answer-model latency.
    answer_rows = [r for r in result.log if r.get("phase") == "terminal_answer"]
    assert len(answer_rows) == 1 and answer_rows[0]["usage"] == {"in": 7, "out": 3}
    assert answer_rows[0]["ms"]["model"] >= 0


@pytest.mark.parametrize("op,kwargs", [("click", {"element_id": "b0"}),
                                       ("navigate", {"text": "https://owned.example/elsewhere"}),
                                       ("template", {"chord": "clipboard_write", "text": '{"text":"x"}'})])
def test_terminal_answer_cannot_dispatch_another_effect(op, kwargs):
    result, mac, proposals, claims = run_case(answer=lambda *a: reply(op, **kwargs))
    assert result.outcome == "failed" and result.reason == "terminal_answer_action_refused"
    assert mac.inputs == 2 and len(proposals) == 3 and len(claims) == 1


def test_exact_user_answer_rejection_never_reopens_input_or_retries_answer():
    result, mac, proposals, claims = run_case(answer=lambda *a: reply("done", summary="The final count is 3."),
                                            verify=lambda c,o,e,n,s: (n == 1, "answer contradicts source count"))
    assert result.outcome == "failed" and result.reason == "terminal_answer_unverified"
    assert result.summary == "" and mac.inputs == 2 and len(proposals) == 3 and len(claims) == 2


@pytest.mark.parametrize("reply_value", [{"actions": [{"op": "done", "summary": ""}]},
                                          {"actions": [{"op": "done", "summary": "2"}, {"op": "click", "element_id": "b0"}]},
                                          {"actions": [{"op": "bogus"}]}])
def test_invalid_terminal_reply_is_bounded_and_cannot_mutate(reply_value):
    result, mac, proposals, claims = run_case(answer=lambda *a: (reply_value, {}))
    assert result.outcome == "failed" and result.reason == "terminal_answer_invalid"
    assert mac.inputs == 2 and len(proposals) == 3 and len(claims) == 1


def test_answer_provider_exception_stays_private_and_never_replays_effect():
    def unavailable(*args):
        raise RuntimeError("private provider body must not enter the journal")
    result, mac, proposals, claims = run_case(answer=unavailable)
    assert result.outcome == "failed" and result.reason == "terminal_answer_unavailable"
    assert "private provider" not in str(result.log) and result.summary == ""
    assert mac.inputs == 2 and len(proposals) == 3 and len(claims) == 1


def test_stop_while_answer_is_generated_prevents_final_verification_and_effect():
    def stop_answer(payload, mac, stop):
        stop.set()
        return reply("done", summary="The final count is 2.")
    result, mac, proposals, claims = run_case(answer=stop_answer)
    assert result.outcome == "stopped" and result.reason == "stopped_by_founder"
    assert mac.inputs == 2 and len(proposals) == 3 and len(claims) == 1


def test_stop_during_automatic_verification_prevents_answer_model_call():
    def stop_verify(claim, obs, evidence, n, stop):
        stop.set()
        return True, "verified source count"
    result, mac, proposals, claims = run_case(verify=stop_verify)
    assert result.outcome == "stopped" and mac.inputs == 2 and len(proposals) == 2


def test_answer_latency_cannot_bypass_original_task_time_cap(monkeypatch):
    from desktop_runtime import cu_ai_engine as engine
    original, expired = engine.time.monotonic, [False]
    monkeypatch.setattr(engine.time, "monotonic", lambda: original() + (120 if expired[0] else 0))
    def delayed(*args):
        expired[0] = True
        return reply("done", summary="The final count is 2.")
    result, mac, proposals, claims = run_case(answer=delayed)
    assert result.outcome == "capped" and mac.inputs == 2 and len(claims) == 1


def test_fresh_answer_observation_failure_is_not_false_completion():
    class FailingRead(Counter):
        def observe(self):
            if self.looks >= 3:
                raise RuntimeError("private observation failure")
            return super().observe()
    result, mac, proposals, claims = run_case(mac=FailingRead())
    assert result.outcome == "failed" and result.reason == "terminal_answer_observation_unavailable"
    assert mac.inputs == 2 and len(claims) == 1


def test_answer_verifier_exception_is_not_false_completion():
    def unavailable(claim, obs, evidence, n, stop):
        if n == 2:
            raise RuntimeError("private verifier body")
        return True, "source count verified"
    result, mac, proposals, claims = run_case(verify=unavailable)
    assert result.outcome == "failed" and result.reason == "terminal_answer_unverified"
    assert mac.inputs == 2 and result.summary == "" and len(claims) == 2


def test_cancellation_preserves_exhausted_quota_and_resume_never_replays_input():
    saved, mac = [], Counter()
    class Checkpoint:
        def load(self):
            return saved[-1] if saved else None
        def save(self, row):
            saved.append(row)
        def clear(self):
            pass
    def cancelled(*args):
        raise asyncio.CancelledError()
    checkpoint = Checkpoint()
    with pytest.raises(asyncio.CancelledError):
        run_case(answer=cancelled, checkpoint=checkpoint, mac=mac)
    assert saved[-1]["pending_dispatch"] is False
    assert saved[-1]["exact_button_budgets"][0]["used"] == 2 and mac.inputs == 2
    result, mac, proposals, claims = run_case(checkpoint=checkpoint, mac=mac)
    assert result.outcome == "done" and mac.inputs == 2 and len(proposals) == 1


def test_stop_during_exact_answer_verification_never_returns_done():
    def stop_verify(claim, obs, evidence, n, stop):
        if n == 2:
            stop.set()
        return True, "source count verified"
    result, mac, proposals, claims = run_case(verify=stop_verify)
    assert result.outcome == "stopped" and mac.inputs == 2 and len(claims) == 2


def test_resumed_exhausted_input_budget_requires_answer_without_new_delivery():
    from coding_agents.cu_ai_loop import LoopState
    from desktop_runtime.cu_ai_retries import RetryGuard
    mac = Counter()
    mac.count = 2
    saved = {**LoopState().snapshot(GOAL), "pending_dispatch": False, "route_retries": RetryGuard().snapshot(),
             "exact_button_budgets": [{"label": "advance", "limit": 2, "used": 2}]}
    checkpoint = type("Checkpoint", (), {"load": lambda self: saved, "save": lambda self,row: None,
                                          "clear": lambda self: None})()
    result, mac, proposals, claims = run_case(mac=mac, checkpoint=checkpoint)
    assert result.outcome == "done" and result.summary == "The final count is 2."
    assert mac.inputs == 0 and len(proposals) == 1 and len(claims) == 2


def test_native_automatic_completion_allows_only_an_answer_and_never_reopens(monkeypatch):
    from coding_agents import cu_ai_tools as tools
    tool_calls, proposals, claims = [], [], []
    proof = "script ok (30 chars)" + tools.PRIVATE + "verified: note created, id x-coredata://owned"
    monkeypatch.setattr(tools, "run_template", lambda *a, **k: tool_calls.append(1) or proof)
    class Native(Counter):
        def observe(self):
            obs = super().observe()
            obs.frontmost, obs.window_title = "Notes", "Owned note"
            return obs
        def frontmost(self):
            return "Notes", 0
        def act(self, *args):
            pytest.fail("answer collection must never reopen or type in the app")
    def actor(payload):
        proposals.append(payload)
        if len(proposals) == 1:
            return reply("template", chord="notes_create", text='{"title":"Owned","body":"b"}')
        assert "TERMINAL_ANSWER_ONLY" in payload["hint"]
        return reply("done", summary="Created the note titled Owned with body b.")
    def verifier(claim, obs, evidence):
        claims.append((claim, evidence))
        assert "NATIVE TOOL READ-BACK" in evidence
        return True, "source tool receipt"
    result = run_loop("Create a note titled Owned with body b.", actor, Native(), threading.Event(), verify=verifier)
    assert result.outcome == "done" and result.summary == "Created the note titled Owned with body b."
    assert len(tool_calls) == 1 and len(proposals) == 2 and len(claims) == 2


def test_native_cancelled_answer_resume_cannot_replay_tool_when_fresh_proof_is_missing(monkeypatch):
    from coding_agents import cu_ai_tools as tools
    scripts, saved, proposals = [], [], []
    monkeypatch.setattr(tools, "run_template", lambda *a, **k: scripts.append(1) or
                        "script ok (30 chars)" + tools.PRIVATE + "verified: note created, id x-coredata://owned")
    checkpoint = type("Checkpoint", (), {"load": lambda self: saved[-1] if saved else None,
                                          "save": lambda self,row: saved.append(row), "clear": lambda self: None})()
    def actor(payload):
        proposals.append(payload)
        if "TERMINAL_ANSWER_ONLY" in payload["hint"]:
            raise asyncio.CancelledError()
        return reply("template", chord="notes_create", text='{"title":"Owned","body":"b"}')
    with pytest.raises(asyncio.CancelledError):
        run_loop("Create a note titled Owned with body b.", actor, Counter(), threading.Event(),
                 verify=lambda *a: (True, "source tool readback"), checkpoint=checkpoint)
    assert len(scripts) == 1 and saved[-1]["terminal_answer_pending"] is True
    result = run_loop("Create a note titled Owned with body b.", lambda p: pytest.fail("resume must not repeat any effect"),
                      Counter(), threading.Event(), verify=lambda *a: (False, "fresh note proof unavailable"), checkpoint=checkpoint)
    assert result.reason == "terminal_answer_unverified" and len(scripts) == 1


def test_answer_stage_resume_without_independent_verifier_fails_before_actor():
    from coding_agents.cu_ai_loop import LoopState
    from desktop_runtime.cu_ai_retries import RetryGuard
    saved = {**LoopState().snapshot(GOAL), "pending_dispatch": False, "terminal_answer_pending": True,
             "route_retries": RetryGuard().snapshot(), "exact_button_budgets": [{"label": "advance", "limit": 2, "used": 2}]}
    checkpoint = type("Checkpoint", (), {"load": lambda self: saved, "save": lambda self,row: None})()
    mac = Counter()
    result = run_loop(GOAL, lambda p: pytest.fail("missing verifier cannot restore mutation authority"), mac,
                      threading.Event(), checkpoint=checkpoint)
    assert result.reason == "independent_verifier_required" and mac.inputs == 0


@pytest.mark.parametrize("marker", ["true", 1, None])
def test_checkpoint_answer_phase_marker_is_strict_bool_and_cannot_erase_original_state(marker):
    from coding_agents.cu_ai_loop import LoopState
    from desktop_runtime.cu_ai_retries import RetryGuard
    saved = {**LoopState().snapshot(GOAL), "pending_dispatch": False, "terminal_answer_pending": marker,
             "route_retries": RetryGuard().snapshot(), "exact_button_budgets": [{"label": "advance", "limit": 2, "used": 2}]}
    checkpoint = type("Checkpoint", (), {"load": lambda self: saved,
                                          "save": lambda self,row: pytest.fail("invalid checkpoint must not be overwritten")})()
    mac = Counter()
    result = run_loop(GOAL, lambda p: pytest.fail("invalid marker must fail before actor"), mac,
                      threading.Event(), checkpoint=checkpoint, verify=lambda *a: (True, ""))
    assert result.reason == "checkpoint_invalid_or_task_changed" and mac.inputs == 0
    assert saved["terminal_answer_pending"] is marker
