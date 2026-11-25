"""Production worker/adapter control tests with synthetic source replies; no model I/O."""
from dataclasses import replace
import hashlib
import json
import threading
import time

import pytest

from coding_agents.cu_ai_loop import parse_action
from coding_agents.owned_browser import cuai_document_target
from desktop_runtime import device_worker
from desktop_runtime.cu_owned_browser_adapter import CuOwnedBrowserAdapter
from tests.unit.product.test_browser_qualification_contract import form_grant
from tests.unit.product.test_cu_owned_browser_adapter import Browser, Broker, Tracker, actor, profile


GRANT = replace(form_grant(), allowed_operations=("navigate", "type", "click", "hover", "new_tab", "switch_tab", "scroll", "extract"), max_tabs=2)
URL = GRANT.origin + GRANT.rules[0].path
TAB = "t" + "2" * 32


class QualifiedBrowser(Browser):
    def __init__(self, *, recovery=False, **kwargs):
        super().__init__(**kwargs)
        self.qualification_grant = GRANT
        self.recovery = recovery
        self.last_qualification_download = None
        self.founder_prompt = ""

    def observe(self):
        observation = super().observe()
        observation.location = URL
        if self.recovery:
            observation.controls, observation.reviewed_text = [], []
        self.last_load_evidence.update(location=URL, observation_digest=observation.digest())
        self.last_load_evidence["response"].update(url=URL)
        if self.recovery:
            self.last_load_evidence.update(kind="qualification_error", qualified=False)
            self.last_load_evidence["response"].update(status=503)
        return observation

    def verified_load(self, observation):
        return not self.recovery and super().verified_load(observation)

    def qualification_recoverable_load(self, observation):
        return self.recovery and observation is self.observation

    def cuai_readback(self, observation):
        assert self.verified_load(observation) or self.qualification_recoverable_load(observation)
        return json.dumps({"error": "Known 503 response", "qualified": False}) if self.recovery else "Observed fixture facts"

    def qualification_context(self, observation):
        return {"grant_digest": GRANT.grant_digest, "controls": [], "tabs": [], "frames": [], "links": []}

    def qualification_proposal(self, invocation, candidate, rules):
        assert rules == ()

    def cuai_navigation_targets(self, observation):
        return [] if self.recovery else [{"id": "c0", "href": URL, "tag": "A", "form": False}]

    def _source_dispatch(self, candidate, invocation, **extra):
        operation = candidate.operation
        effect = {"invocation": invocation, "observation": candidate.observation_digest, "operation": operation,
            "control": {"tag": "INPUT" if operation == "type" else "BUTTON" if operation == "hover" else "HTML",
                "form": False, "typed": operation == "type"}, "document_target": candidate.target, **extra}
        if operation == "new_tab" and not str(candidate.target).startswith("document_"):
            effect["control"].update(tag="A", href=URL)
        assert self.admit_effect(effect, "e" * 64) is True
        self.deliveries.append((operation, invocation))
        self.recovery = False

    def hover(self, candidate, invocation):
        self._source_dispatch(candidate, invocation)

    def tab_action(self, candidate, invocation, action):
        self._source_dispatch(candidate, invocation, tab_url=URL,
            **({"tab_target": candidate.target} if action.op == "switch_tab" else {}))

    def click(self, candidate, invocation, *, navigate_url=None, new_tab_url=None):
        assert new_tab_url == URL and navigate_url is None
        self._source_dispatch(candidate, invocation)

    def document_action(self, candidate, invocation, action):
        self._source_dispatch(candidate, invocation, **({"recovery_url": action.text} if action.op == "navigate" else {}))

    def type_text(self, candidate, invocation, text):
        assert text == "Ada"
        self.last_typed_label = "Reviewed fixture value"
        self._source_dispatch(candidate, invocation)


class QualifiedBroker(Broker):
    claim_attempted = False
    account_id = None

    def __init__(self, replies, *, direct=False):
        super().__init__(replies)
        self.direct, self.effects = direct, []

    def browser_ai_call(self, invocation, phase, observation, **kwargs):
        result = super().browser_ai_call(invocation, phase, observation, **kwargs)
        if phase != "actor":
            return result
        action = parse_action(json.loads(result["reply"])["actions"][0])
        if action.op in {"done", "fail", "needs_you", "wait"}:
            return result
        if action.op in {"extract", "scroll"} or action.op == "new_tab" and self.direct or action.op == "navigate":
            target = cuai_document_target(kwargs["current_load"], action)
        else:
            target = action.element_id or "c0"
        result["effect_proposals"] = [{"action_index": 0, "invocation_id": invocation + "_a0",
            "candidate": {"operation": action.op, "target": target, "observation_digest": observation.digest(),
                "requires_outcome_verification": False, "value_index": 0 if action.op == "type" else None},
            "qualification_rule_ids": []}]
        return result

    def claim(self):
        self.claim_attempted = True

    def qualification_current(self):
        return GRANT

    def admit_effect(self, invocation, observation, digest):
        self.effects.append(("admit", invocation))
        return True

    def effect_receipt(self, invocation, digest, outcome):
        self.effects.append((outcome, invocation))

    def stop_receipt(self, *args, **kwargs):
        return {"state": "stopped"}


def adapter(replies, *, recovery=False, direct=False, read_only=False):
    browser = QualifiedBrowser(recovery=recovery, read_only=read_only)
    broker = QualifiedBroker(replies, direct=direct)
    value = CuOwnedBrowserAdapter(browser=browser, gateway=broker, profile=profile(), goal=browser.goal,
        stop=threading.Event(), deadline=time.monotonic() + 60)
    return value, browser, broker


def worker_run(monkeypatch, replies, *, direct=False, recovery=False, pause=False, download=None, allow_type=False):
    broker, browsers = QualifiedBroker(replies, direct=direct), []
    def factory(**kwargs):
        browser = QualifiedBrowser(recovery=recovery, **kwargs)
        if pause:
            browser.pause_kind = "password"
            browser.founder_prompt = "Please take over to sign in yourself."
        browser.last_qualification_download = download
        browsers.append(browser)
        return browser
    monkeypatch.setattr(device_worker, "OwnedBrowser", factory)
    monkeypatch.setattr(device_worker, "OwnedProcesses", Tracker)
    worker = device_worker.DeviceWorker(broker, lambda _: None)
    worker._notify_terminal = lambda *args, **kwargs: None
    manifest = {"applications": ["com.google.Chrome"], "origins": [GRANT.origin], "instruction": Browser().goal,
        "actions": ["browser.read", "browser.interact"], "duration_seconds": 60,
        "general_browser_verification": {"operation": "general_goal_v1", "start_url": URL, "max_steps": 4,
            "allow_type": allow_type, "fill_values": {}, "execution_profile": profile()}}
    worker.run_general_browser({"state": "approved", "browser_qualification_grant": GRANT.to_dict()}, manifest)
    return worker, browsers[0], broker


@pytest.mark.parametrize("operation", ["needs_you", "fail"])
def test_terminal_distinction_keeps_bounded_redacted_question_without_effect(operation):
    summary = "Which option should I use? token sk-" + "a" * 32
    value, browser, broker = adapter([{"subgoals": ["Inspect"]}, actor(operation, summary=summary)])
    proposal = value.decide(browser.observe())
    assert proposal.terminal_operation == operation and proposal.summary == "Which option should I use? token [REDACTED]"
    assert proposal.action.summary == proposal.summary and browser.deliveries == [] and value.terminal_only
    assert [phase for phase, _, _ in broker.calls] == ["plan", "actor"]


@pytest.mark.parametrize("op,args", [("click", {"element_id": "c0"}), ("type", {"element_id": "c0", "text": "Ada"}),
    ("done", {"summary": "It worked"}), ("extract", {"text": "Price"}), ("new_tab", {"text": URL}),
    ("hover", {"element_id": "c0"}), ("switch_tab", {"element_id": TAB})])
def test_known_error_source_may_replan_but_never_mutate_or_claim_success(op, args):
    value, browser, broker = adapter([{"subgoals": ["Recover"]}, actor(op, **args)], recovery=True)
    with pytest.raises(PermissionError, match="action_unsupported"):
        value.decide(browser.observe())
    assert [phase for phase, _, _ in broker.calls] == ["plan", "actor"]
    assert all(kwargs["current_load"]["qualified"] is False for _, _, kwargs in broker.calls)
    assert browser.deliveries == [] and value.pending_dispatch is False


@pytest.mark.parametrize("action,direct,recovery", [
    (actor("hover", element_id="c0"), False, False),
    (actor("new_tab", text=URL), False, False),
    (actor("new_tab", text=URL), True, False),
    (actor("switch_tab", element_id=TAB), False, False),
    (actor("navigate", text=URL), False, True),
    (actor("type", element_id="c0", text="Ada"), False, False),
])
def test_worker_dispatches_exact_granted_source_once_and_preserves_settlement(monkeypatch, action, direct, recovery):
    worker, browser, broker = worker_run(monkeypatch, [{"subgoals": ["Read"]}, action,
        actor("done", summary="Observed fixture result"), {"passed": True, "reason": "Source"}], direct=direct, recovery=recovery)
    assert browser.closed and browser.deliveries == [(action["actions"][0]["op"], broker.effects[0][1])]
    assert browser.started == [URL]  # Exact grant query, without an analytics marker.
    assert broker.effects == [("admit", browser.deliveries[0][1]), ("returned", browser.deliveries[0][1])]
    assert worker.last["evidence"]["terminal_operation"] == "done"
    assert worker.last["task_verified"] is False and worker.last["evidence"]["pending_dispatch"] is False


@pytest.mark.parametrize("op", ["needs_you", "fail"])
def test_worker_terminal_prompt_and_hash_match_the_visible_redacted_answer(monkeypatch, op):
    summary = "Please choose an email address. sk-" + "a" * 32
    worker, browser, broker = worker_run(monkeypatch, [{"subgoals": ["Inspect"]}, actor(op, summary=summary)])
    visible = "Please choose an email address. [REDACTED]"
    assert worker.last["result_summary"] == visible
    assert worker.last["evidence"]["terminal_operation"] == op
    assert worker.last["evidence"]["answer_sha256"] == hashlib.sha256(visible.encode()).hexdigest()
    assert worker.last["evidence"]["outcome"] == "attention_required" and worker.last["task_verified"] is False
    if op == "needs_you":
        assert worker.last["founder_prompt"] == visible
    assert not broker.effects and browser.closed
    assert visible not in json.dumps(worker.last["evidence"]["steps"])


def test_dom_credential_pause_keeps_refusal_metadata_without_provider_or_input(monkeypatch):
    worker, browser, broker = worker_run(monkeypatch, [], pause=True)
    evidence = worker.last["evidence"]
    assert evidence["terminal_operation"] == "needs_you" and evidence["outcome"] == "attention_required"
    assert evidence["answer_sha256"] == hashlib.sha256(worker.last["founder_prompt"].encode()).hexdigest()
    assert broker.calls == broker.effects == [] and browser.closed


def test_owned_download_native_metadata_is_bounded_and_has_no_filesystem_paths(monkeypatch):
    metadata = {"filename": "report-owned.csv", "sha256": "f" * 64, "size_bytes": 42,
        "media_type": "text/csv", "request_id": "bqr_owned", "rule_id": "read_report",
        "path": "/private/owned/file.csv", "rows_excluding_header": 2}
    worker, _, _ = worker_run(monkeypatch, [{"subgoals": ["Read"]}, actor("done", summary="Saved"),
        {"passed": True, "reason": "Source"}], download=metadata)
    assert worker.last["evidence"]["downloads"] == [{key: value for key, value in metadata.items()
        if key in {"filename", "sha256", "size_bytes", "media_type", "request_id", "rule_id"}}]
    assert "/private/owned" not in json.dumps(worker.last["evidence"])


@pytest.mark.parametrize("action", [actor("new_tab", text=URL), actor("hover", element_id="c0"), actor("switch_tab", element_id=TAB)])
def test_grant_does_not_widen_read_only_authority(action):
    value, browser, _ = adapter([{"subgoals": ["Read"]}, action], read_only=True)
    with pytest.raises(PermissionError, match="action_unsupported"):
        value.decide(browser.observe())
    assert browser.deliveries == []
