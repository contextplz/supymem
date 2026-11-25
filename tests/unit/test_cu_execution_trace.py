"""Source/transport interval logic only. No GUI, provider, or app is launched."""
import json
from pathlib import Path
from types import SimpleNamespace
import threading

import pytest


@pytest.fixture
def sources(monkeypatch):
    root = Path(__file__).resolve().parents[2]
    monkeypatch.syspath_prepend(str(root / "packages"))
    monkeypatch.syspath_prepend(str(root / "scripts/desktop"))
    from cu_execution_trace import ExecutionTrace, HarnessBindings
    from coding_agents import cu_ai_tools as tools, cu_ai_templates as catalog
    from desktop_runtime import agent_cursor
    import subprocess

    class Client:
        def __init__(self, *args, **kwargs):
            self.closed = False
            self.response = object()
        def post(self, *args, **kwargs):
            return self.response
        def close(self):
            self.closed = True

    class Mac:
        def observe(self):
            return SimpleNamespace(elements=[{"value": "PRIVATE_OBSERVATION"}])

    def call(*_):
        return {"effect": "verified", "body": "PRIVATE_NATIVE_RESPONSE"}
    call.close = lambda: None
    call.is_reviewed_child = lambda pid, window: (pid, window) == (123, 7)
    mac = Mac()
    mac.call = call
    trial = SimpleNamespace(httpx=SimpleNamespace(Client=Client))
    return SimpleNamespace(ExecutionTrace=ExecutionTrace, HarnessBindings=HarnessBindings,
                           tools=tools, catalog=catalog, cursor=agent_cursor,
                           subprocess=subprocess, mac=mac, trial=trial, Client=Client)


def test_pure_invalid_mail_is_invocation_only_without_transport(sources, monkeypatch):
    s = sources
    monkeypatch.setattr(s.subprocess, "Popen", lambda *_a, **_k: pytest.fail("invalid params dispatched"))
    trace = s.ExecutionTrace()
    with s.HarnessBindings(trace, mac=s.mac, trial=s.trial) as binding:
        result = s.tools.run_template("mail_draft", '{"recipients":["private@example.test"]}',
                                      "Create a Mail draft.")
    assert result.startswith("refused: mail_draft_keys_invalid:")
    assert binding.installation_verified and trace.complete()
    assert len(trace.events) == 2
    assert trace.events[-1]["outcome"] == "refused"
    assert trace.events[-1]["proof"] == "invocation_only"
    assert trace.events[-1]["confirmed_subprocess_count"] == 0
    assert "private@example.test" not in json.dumps(trace.events)


class FakeProcess:
    """Fake constructor receipt; these tests never prove actual Mac dispatch."""
    pid = 456
    returncode = 0
    args = []
    def __init__(self, *args, **kwargs):
        pass
    def communicate(self, *args, **kwargs):
        return "verified: PRIVATE_SCRIPT_OUTPUT", "PRIVATE_STDERR"
    def poll(self):
        return 0
    def kill(self):
        pass
    def wait(self, *args, **kwargs):
        return 0


def test_corrected_mail_records_nested_process_receipt_without_content(sources, monkeypatch):
    s = sources
    dispatched = []
    def popen(*args, **kwargs):
        dispatched.append(args)
        return FakeProcess()
    monkeypatch.setattr(s.subprocess, "Popen", popen)
    trace = s.ExecutionTrace()
    params = {"to": "qa-one@example.test,qa-two@example.test", "subject": "PRIVATE_SUBJECT",
              "body": "PRIVATE_BODY"}
    goal = "Create a Mail draft to qa-one@example.test and qa-two@example.test with subject PRIVATE_SUBJECT and body PRIVATE_BODY."
    with s.HarnessBindings(trace, mac=s.mac, trial=s.trial):
        result = s.tools.run_template("mail_draft", json.dumps(params), goal, stop=threading.Event())
    assert result.startswith("script ok") and len(dispatched) == 1
    receipt = next(row for row in trace.events if row["event_stage"] == "process_started")
    assert receipt["process_pid"] == 456 and receipt["operation"] == "script_executor"
    assert receipt["parent_span_id"] is not None
    assert trace.events[-1]["proof"] == "process_started"
    assert trace.events[-1]["confirmed_subprocess_count"] == 1
    serialized = json.dumps(trace.events)
    for private in (*params.values(), goal, "PRIVATE_SCRIPT_OUTPUT", "PRIVATE_STDERR"):
        assert private not in serialized
    assert trace.complete()


def test_failed_constructor_is_not_promoted_to_started_process(sources, monkeypatch):
    s = sources
    def fail(*_a, **_k):
        raise OSError("PRIVATE_PROCESS_ERROR")
    monkeypatch.setattr(s.subprocess, "Popen", fail)
    trace = s.ExecutionTrace()
    with s.HarnessBindings(trace, mac=s.mac, trial=s.trial):
        result = s.tools.run_script("Mail", 'tell application "Mail"\nPRIVATE_SCRIPT\nend tell',
                                   "Mail draft", stop=threading.Event(), trusted=True)
    assert result.startswith("refused:")
    assert not any(row["event_stage"] == "process_started" for row in trace.events)
    assert all(row["proof"] == "invocation_only" for row in trace.events)
    assert "PRIVATE" not in json.dumps(trace.events)
    assert trace.complete()


def test_inline_clipboard_refusal_does_not_invent_a_no_effect_receipt(sources):
    s = sources
    trace = s.ExecutionTrace()
    with s.HarnessBindings(trace, mac=s.mac, trial=s.trial):
        # Source-owned access predicate refuses before importing AppKit.
        result = s.catalog._clipboard("clipboard_write", {"text": "PRIVATE_CLIPBOARD"},
                                      "A task without clipboard authority.", threading.Event())
    assert result.startswith("refused:")
    assert all(row["kind"] == "inline_native_invocation" for row in trace.events)
    assert trace.events[-1]["proof"] == "invocation_only"
    assert "not_dispatched" not in json.dumps(trace.events) and "PRIVATE" not in json.dumps(trace.events)


def test_exception_and_interrupt_restore_exact_function_and_instance_custody(sources, monkeypatch):
    s = sources
    def interrupt(*_a, **_k):
        raise KeyboardInterrupt("PRIVATE_INTERRUPT")
    monkeypatch.setattr(s.tools, "run_fs", interrupt)
    originals = (s.tools.run_template, s.tools.run_fs, s.subprocess.Popen, s.trial.httpx, s.mac.call)
    trace = s.ExecutionTrace()
    with pytest.raises(KeyboardInterrupt):
        with s.HarnessBindings(trace, mac=s.mac, trial=s.trial):
            assert "observe" in s.mac.__dict__
            s.tools.run_fs("write_text", "PRIVATE_PATH", "PRIVATE_BYTES", "PRIVATE_GOAL")
    assert originals == (s.tools.run_template, s.tools.run_fs, s.subprocess.Popen, s.trial.httpx, s.mac.call)
    assert "observe" not in s.mac.__dict__
    assert trace.events[-1]["outcome"] == "exception" and trace.complete()
    assert "PRIVATE" not in json.dumps(trace.events)


def test_native_target_ids_and_callable_authority_survive_wrapping(sources):
    s = sources
    trace = s.ExecutionTrace()
    original = s.mac.call
    with s.HarnessBindings(trace, mac=s.mac, trial=s.trial):
        assert s.mac.call.close is original.close
        assert s.mac.call.is_reviewed_child(123, 7)
        result = s.mac.call("hotkey", {"target": {"pid": 123, "window_id": 7}, "chord": "PRIVATE_KEYS"})
        assert result["body"] == "PRIVATE_NATIVE_RESPONSE"
        s.mac.call("PRIVATE_TOOL", {"target": {"pid": True, "window_id": -7}})
    assert trace.events[0]["target_pid"] == 123 and trace.events[0]["window_id"] == 7
    assert trace.events[2]["operation"] == "other" and "target_pid" not in trace.events[2]
    assert all(row["proof"] == "invocation_only" for row in trace.events)
    assert "PRIVATE" not in json.dumps(trace.events)


def test_provider_instance_wraps_only_requests_preserving_response_and_cleanup(sources):
    s = sources
    trace = s.ExecutionTrace()
    module = s.trial.httpx
    with s.HarnessBindings(trace, mac=s.mac, trial=s.trial):
        client = s.trial.httpx.Client(headers={"Authorization": "PRIVATE_KEY"})
        response = client.post("https://api.openai.com/v1/responses", json={"body": "PRIVATE_PROMPT"})
        assert response is client.response
        client.close()
        assert client.closed
    assert s.trial.httpx is module
    assert len(trace.events) == 2 and trace.events[0]["kind"] == "provider_request"
    assert trace.events[0]["operation"] == "responses"
    assert "PRIVATE" not in json.dumps(trace.events)


def test_other_thread_and_unrelated_process_creation_are_not_traced(sources, monkeypatch):
    s = sources
    monkeypatch.setattr(s.subprocess, "Popen", lambda *_a, **_k: FakeProcess())
    trace = s.ExecutionTrace()
    values = []
    with s.HarnessBindings(trace, mac=s.mac, trial=s.trial):
        s.subprocess.Popen(["PRIVATE_UNRELATED_COMMAND"])
        def other_thread():
            s.mac.observe()
            s.mac.call("hotkey", {"pid": 123})
            values.append(s.trial.httpx.Client())
            s.tools.run_script("Mail", 'tell application "Mail"\nend tell', "Mail", stop=threading.Event(), trusted=True)
        worker = threading.Thread(target=other_thread)
        worker.start()
        worker.join(timeout=2)
        assert not worker.is_alive()
    assert trace.events == [] and isinstance(values[0], s.Client) and trace.complete()


def test_later_foreign_binding_is_never_overwritten_and_marks_evidence_incomplete(sources):
    s = sources
    trace = s.ExecutionTrace()
    def foreign(*_a, **_k):
        return "foreign owner"
    original = s.mac.call
    with s.HarnessBindings(trace, mac=s.mac, trial=s.trial):
        s.mac.call = foreign
    assert s.mac.call is foreign and not trace.complete()
    s.mac.call = original


def test_partial_installation_restores_already_owned_bindings(sources, monkeypatch):
    s = sources
    original = s.tools.run_template
    monkeypatch.delattr(s.tools, "evidence")
    trace = s.ExecutionTrace()
    binding = s.HarnessBindings(trace, mac=s.mac, trial=s.trial)
    with pytest.raises(AttributeError):
        binding.__enter__()
    assert s.tools.run_template is original and not binding.enabled
    assert not binding.installation_verified and binding.bindings == []


def test_unknown_labels_are_withheld_overflow_and_active_intervals_stay_honest(sources):
    trace = sources.ExecutionTrace(max_events=2)
    with trace.span("PRIVATE_KIND", "PRIVATE_OPERATION") as parent:
        assert trace.active_ids() == [parent["span_id"]]
        with trace.span("observation_invocation", "observe") as child:
            assert trace.active_ids() == [parent["span_id"], child["span_id"]]
    assert trace.active_ids() == [] and not trace.complete() and trace.overflow
    assert "PRIVATE" not in json.dumps(trace.events)


def test_continuous_focus_sample_records_active_ids_without_waiving_stationary_failure(sources):
    import time
    from cu_focus_trace import FocusTrace, traced_call
    s = sources
    decoy = SimpleNamespace(instance="run", process=SimpleNamespace(pid=123),
        read=lambda: {"instance_id": "run", "pid": 123, "received_input_events": 0,
            "samples": 3, "updated_at": time.time(), "body_sha256": "a" * 64,
            "is_active": True, "active_app_pid": 123})
    focus = FocusTrace(decoy, lambda: ("PRIVATE_NAME", 999))
    execution = s.ExecutionTrace()
    focus.execution = execution
    with execution.span("provider_request", "responses"):
        focus.sample()
    focus.sample()
    assert focus.samples[0]["execution_span_ids"] == [1]
    assert focus.samples[1]["execution_span_ids"] == []
    assert focus.verified(True) is False  # Even a provider-only switch remains failed.
    original = s.mac.call
    wrapped = traced_call(original, focus)
    assert wrapped.is_reviewed_child(123, 7) and wrapped.close is original.close
