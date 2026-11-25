"""Private owned-fixture custody, exact invocation links, fail-closed privacy."""
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
from types import SimpleNamespace

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts/desktop"))
import cu_ai_trial as trial
import cu_browser_heldout_suite as suite
from cu_acceptance import reviewed_browser_profile
from cu_trial_private_evidence import PrivateEvidenceError, PrivateTrialEvidence
from desktop_runtime.cu_ai_browser import BrowserMac
from desktop_runtime.cu_ai_engine import Observation


@pytest.fixture
def fx():
    fixture = suite.HeldoutFixture("custody12345").start()
    try:
        yield fixture
    finally:
        fixture.close()


def recorder(fx, tmp_path, **kwargs):
    return PrivateTrialEvidence(fx, suite.TASKS[2].goal(fx), run_id="a" * 32,
                                task="workspace_fields", repetition=1, source_digest="b" * 64,
                                harness_digest="c" * 64, directory=tmp_path, **kwargs)


def call(evidence, fx, **kwargs):
    return evidence.request("PRIVATE CLAIM AND PAGE", "PRIVATE READBACK", model="gpt-5.6-luna",
                            phase="whole_task_verification", current_url=fx.url("workspace"),
                            snapshot_id="snapshot-1", tab_state=(1, "owned"), **kwargs)


def test_exact_private_payloads_and_links_with_content_free_export(fx, tmp_path):
    evidence = recorder(fx, tmp_path)
    first = call(evidence, fx)
    evidence.verdict(first, (False, "PRIVATE REASON"))
    evidence.engine_verdict({"step": 4, "passed": False, "reason": "PRIVATE REASON", "code": "independent_verifier_rejected"})
    evidence.finalize({"summary": "PRIVATE ANSWER"}, {"predicate": False, "private_source": "PRIVATE SOURCE"})
    manifest = json.loads((evidence.path / "manifest.json").read_text())
    assert manifest["binding"]["trial_id"] == evidence.trial_id
    assert manifest["calls"][0]["call_id"] == first
    request = json.loads((evidence.path / "verify-0001-request.json").read_text())
    verdict = json.loads((evidence.path / "verify-0001-verdict.json").read_text())
    engine = json.loads((evidence.path / "engine-verdict-0001.json").read_text())
    assert request["user_text"] == "PRIVATE CLAIM AND PAGE" and request["readback_context"] == "PRIVATE READBACK"
    assert verdict["request_sha256"] == manifest["calls"][0]["request_sha256"]
    assert engine["verifier_call_id"] == first
    for row in manifest["files"]:
        path = evidence.path / row["name"]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == row["sha256"]
        assert stat.S_IMODE(path.stat().st_mode) == 0o400
    assert stat.S_IMODE(evidence.path.stat().st_mode) == 0o500
    public = json.dumps(evidence.public_metadata())
    for private in ("PRIVATE", str(evidence.path), fx.base, fx.uid, suite.TASKS[2].goal(fx)):
        assert private not in public
    assert evidence.public_metadata()["diagnostic_custody_verified"] is True


def test_fresh_trial_identity_and_non_model_judgment_not_linked_to_previous_call(fx, tmp_path):
    a, b = recorder(fx, tmp_path), recorder(fx, tmp_path)
    assert a.path != b.path and a.trial_id != b.trial_id
    first = call(a, fx)
    a.verdict(first, (True, "yes"))
    a.engine_verdict({"step": 1, "passed": False, "reason": "no output"})
    value = json.loads((a.path / "engine-verdict-0001.json").read_text())
    assert value["verifier_call_id"] is None
    with pytest.raises(PrivateEvidenceError, match="call_mismatch"):
        b.verdict(first, (True, "yes"))
    a.close()
    b.close()


@pytest.mark.parametrize("mode", ["url", "bound", "pending", "schema", "unfinished"])
def test_input_bounds_and_unknown_custody_fail_closed(fx, tmp_path, mode):
    evidence = recorder(fx, tmp_path)
    with pytest.raises(PrivateEvidenceError):
        if mode == "url":
            evidence.request("x", "x", model="m", phase="whole_task_verification", current_url="https://example.com")
        elif mode == "bound":
            evidence.request("x" * 10241, "x", model="m", phase="whole_task_verification", current_url=fx.url("workspace"))
        elif mode == "schema":
            evidence.verdict(call(evidence, fx), (1, "yes"))
        else:
            call(evidence, fx)
            call(evidence, fx) if mode == "pending" else evidence.finalize({}, {})
    assert evidence.public_metadata()["diagnostic_custody_verified"] is False
    evidence.close()


def test_no_follow_exclusive_files_and_no_partial_final_manifest(fx, tmp_path, monkeypatch):
    linked = tmp_path / "linked"
    linked.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(PrivateEvidenceError, match="directory_invalid"):
        recorder(fx, linked)
    evidence = recorder(fx, tmp_path)
    victim = tmp_path / "untouched"
    victim.write_text("untouched")
    (evidence.path / "verify-0001-request.json").symlink_to(victim)
    with pytest.raises(PrivateEvidenceError, match="write_failed"):
        call(evidence, fx)
    assert victim.read_text() == "untouched"
    evidence.close()
    evidence = recorder(fx, tmp_path)
    monkeypatch.setattr(os, "link", lambda *a, **kw: (_ for _ in ()).throw(OSError("PRIVATE OS TEXT")))
    with pytest.raises(PrivateEvidenceError, match="write_failed") as error:
        evidence.finalize({}, {})
    assert "PRIVATE" not in str(error.value) and not (evidence.path / "manifest.json").exists()
    assert not evidence.public_metadata()["diagnostic_custody_verified"]
    evidence.close()


def test_total_records_and_repeated_finalization_fail_closed(fx, tmp_path):
    evidence = recorder(fx, tmp_path)
    evidence.MAX_CALLS = 1
    evidence.verdict(call(evidence, fx), (True, "yes"))
    with pytest.raises(PrivateEvidenceError, match="bound_exceeded"):
        call(evidence, fx)
    evidence.close()
    evidence = recorder(fx, tmp_path)
    evidence.finalize({}, {})
    with pytest.raises(PrivateEvidenceError, match="closed"):
        evidence.finalize({}, {})


def install_fake_trial(monkeypatch, fx, outcome="ok"):
    """Real run_task verify wrapper, fake provider and engine: no model or GUI."""
    bodies, observed_stops = [], []
    monkeypatch.setenv("OPENAI_DEVICE_VISION_API_KEY", "AUTH_SECRET_MUST_NOT_BE_RECORDED")
    class Client:
        def __init__(self, **kwargs):
            assert kwargs["trust_env"] is False
        def post(self, url, *, json, headers):
            assert headers["Authorization"].endswith("AUTH_SECRET_MUST_NOT_BE_RECORDED")
            bodies.append(json)
            if outcome == "timeout":
                raise httpx.ReadTimeout("PRIVATE PROVIDER DETAIL")
            if outcome == "cancel":
                raise KeyboardInterrupt()
            return SimpleNamespace(status_code=200, content=b"stub", json=lambda: {"output": [{"content": [{"type": "output_text", "text": '{"passed":true,"reason":"PRIVATE ACCEPTANCE"}'}]}], "usage": {"input_tokens": 5, "output_tokens": 3}})
        def close(self):
            pass
    monkeypatch.setattr(trial.httpx, "Client", Client)
    def read_response(client, url, headers, body):
        response = client.post(url, headers=headers, json=body)
        return response.status_code, json.dumps(response.json()).encode()
    monkeypatch.setattr(trial, "read_response", read_response)
    def engine(task, decide, mac, stop, *, verify, log, verifier_diagnostic, **kwargs):
        obs = Observation(frontmost="Chrome (browser)", window_title="PRIVATE WINDOW", elements=[],
                          png=b"", snapshot_id="snapshot", notes="PRIVATE PAGE")
        obs.browser_snapshot_id = "snapshot-real"
        obs.browser_tab_state = (1, "tab-real")
        log({"phase": "terminal_answer", "outcome": "terminal_answer_received"})
        try:
            verdict = verify("PRIVATE ANSWER", obs, "PRIVATE READBACK")
        except Exception:
            observed_stops.append(stop.is_set())
        else:
            verifier_diagnostic({"step": 2, "passed": verdict[0], "reason": verdict[1], "code": None})
        return SimpleNamespace(outcome="done", reason="", summary="PRIVATE ANSWER", steps=2, log=[])
    monkeypatch.setattr(trial, "run_loop", engine)
    mac = BrowserMac(factory=lambda: None)
    mac.current_url = fx.url("workspace")
    return mac, bodies, observed_stops


def test_actual_verify_wrapper_retains_exact_input_without_changing_provider_call(fx, tmp_path, monkeypatch, capsys):
    mac, bodies, _ = install_fake_trial(monkeypatch, fx)
    goal = suite.TASKS[2].goal(fx)
    plain = trial.run_task(goal, mac=mac)
    evidence = recorder(fx, tmp_path)
    captured = trial.run_task(goal, mac=mac, private_evidence=evidence)
    assert bodies[0] == bodies[1] and plain["model_calls"] == captured["model_calls"] == 1
    request = json.loads((evidence.path / "verify-0001-request.json").read_text())
    assert request["phase"] == "terminal_answer_verification"
    assert request["user_text"] == bodies[1]["input"][1]["content"][0]["text"]
    assert request["observed_snapshot_id"] == "snapshot-real"
    evidence.finalize(captured, {"oracle": True})
    for path in evidence.path.iterdir():
        assert b"AUTH_SECRET_MUST_NOT_BE_RECORDED" not in path.read_bytes()
    stdout = capsys.readouterr().out
    assert "PRIVATE" not in stdout and "AUTH_SECRET" not in stdout


@pytest.mark.parametrize("outcome,code", [("timeout", "verifier_transport_timeout"), ("cancel", "verifier_interrupted")])
def test_transport_and_cancel_have_exact_invocation_receipt_without_replay(fx, tmp_path, monkeypatch, outcome, code):
    mac, bodies, _ = install_fake_trial(monkeypatch, fx, outcome)
    evidence = recorder(fx, tmp_path)
    if outcome == "cancel":
        with pytest.raises(KeyboardInterrupt):
            trial.run_task(suite.TASKS[2].goal(fx), mac=mac, private_evidence=evidence)
    else:
        trial.run_task(suite.TASKS[2].goal(fx), mac=mac, private_evidence=evidence)
    assert len(bodies) == 1
    receipt = json.loads((evidence.path / "verify-0001-verdict.json").read_text())
    assert receipt["result"] == {"error_code": code}
    evidence.finalize({"outcome": "failed"}, {"oracle": False})


def test_capture_failure_sets_stop_before_provider_call_and_native_scope_refuses(fx, tmp_path, monkeypatch):
    mac, bodies, stops = install_fake_trial(monkeypatch, fx)
    evidence = recorder(fx, tmp_path)
    evidence.MAX_FILE_BYTES = 1
    trial.run_task(suite.TASKS[2].goal(fx), mac=mac, private_evidence=evidence)
    assert not bodies and stops == [True] and evidence.error_code == "qa_private_evidence_bound_exceeded"
    evidence.close()
    evidence = recorder(fx, tmp_path)
    with pytest.raises(PrivateEvidenceError, match="scope_invalid"):
        trial.run_task(suite.TASKS[2].goal(fx), mac=SimpleNamespace(), private_evidence=evidence)
    assert not bodies
    evidence.close()


def test_private_oracle_predicates_preserve_actual_failure_and_original_submit_once(fx):
    task = suite.TASKS[2]
    result = {"summary": "missing reference"}
    value = suite.private_oracle_metadata(task, fx, result, None, False, False)
    assert value["predicates"] == {"original_oracle": False, "oracle_available": True,
                                   "exactly_one_committed_workspace": False, "exactly_one_delivered_attempt": False,
                                   "final_reference_in_returned_answer": False}
    assert "Submit once" in task.goal(fx)


def test_gate_rejects_custody_loss_and_cross_trial_binding():
    selected = suite.TASKS[:1]
    t = selected[0]
    rows = [{"task": t.id, "category": t.cat, "repetition": 1, "run_id": "owned-run", "model": reviewed_browser_profile()["roles"]["actor"]["model"],
             "outcome": "done", "reason": None, "passed": True, "oracle": True, "independent_verification": True,
             "cleanup_verified": True, "browser_process_gone": True, "browser_started": True, "forbidden_requests": 0,
             "qa_transport": "headless_cdp_qa", "source_scope": "browser", "source_digest": suite.source_digest(scope="browser"),
             "catalog": suite.CATALOG, "harness_source_digest": suite.fingerprint(), "expected_human_handoff": False,
             "human_handoff_verified": False, "service_tier_requested": "default", "action_cache_enabled": False,
             "synthetic": True, "counts_as_real_user": False, "account_created": False}]
    row = rows[0]
    row.update(diagnostic_requested=True, diagnostic_custody_verified=True, diagnostic_error_code=None,
               diagnostic_trial_id="a" * 32, diagnostic_binding_sha256="b" * 64,
               diagnostic_manifest_sha256="c" * 64, diagnostic_result_sha256="d" * 64, diagnostic_calls=[],
               diagnostic_binding={k: row[k] for k in ("run_id", "task", "repetition", "source_digest", "harness_source_digest")})
    assert suite.gate(rows, selected, 1)["accepted"] is True
    row["diagnostic_binding"]["task"] = "another_task"
    assert suite.gate(rows, selected, 1)["accepted"] is False
    row["diagnostic_binding"]["task"] = selected[0].id
    row["diagnostic_custody_verified"] = False
    assert suite.gate(rows, selected, 1)["accepted"] is False


@pytest.mark.parametrize("failed", [False, True])
def test_whole_owned_harness_links_final_result_and_cleanup_and_custody_loss_fails(monkeypatch, failed):
    """Actual local server/source oracle + recorder; stub Chrome/model only."""
    model_name = reviewed_browser_profile()["roles"]["actor"]["model"]
    live = []
    original = suite.HeldoutFixture
    def fixture(uid):
        value = original(uid)
        live.append(value)
        return value
    session = SimpleNamespace(closed=False, proc=SimpleNamespace(poll=lambda: 0), _group_gone=True)
    class Factory:
        def __init__(self, *a, **kw):
            self.session = session
    class Mac(BrowserMac):
        def close(self):
            session.closed = True
    monkeypatch.setattr(suite, "HeldoutFixture", fixture)
    monkeypatch.setattr("desktop_runtime.cu_ai_browser.ChromeFactory", Factory)
    monkeypatch.setattr("desktop_runtime.cu_ai_browser.BrowserMac", Mac)
    def model(goal, *, mac, private_evidence, **kw):
        fx = live[-1]
        mac.current_url = fx.url("workspace/ready")
        private_evidence.admit(goal, mac)
        response = httpx.post(fx.url("workspace/create"), data={"name": fx.workspace_name, "region": fx.region, "timezone": fx.timezone})
        assert response.status_code == 303
        invocation = private_evidence.request("PRIVATE EXACT INPUT", "PRIVATE OBSERVED CONTEXT", model=model_name,
                                             phase="terminal_answer_verification", current_url=mac.current_url)
        private_evidence.verdict(invocation, (True, "PRIVATE CONFIRMED"))
        return {"outcome": "done", "summary": fx.final_reference, "reason": "", "cleanup_verified": True,
                "steps": 4, "model_calls": 1, "output_tokens": 3}
    monkeypatch.setattr(suite.trial, "run_task", model)
    if failed:
        def final_failure(self, *a):
            raise PrivateEvidenceError("qa_private_evidence_write_failed")
        monkeypatch.setattr(PrivateTrialEvidence, "finalize", final_failure)
    row = suite.run_one(suite.TASKS[2], model_name, suite.source_digest(scope="browser"),
                        suite.fingerprint(), "f" * 32, 1, private_diagnostics=True)
    assert row["oracle"] is True and row["cleanup_verified"] is True and row["browser_process_gone"] is True
    assert len(live[0].posts) == len(live[0].valid["workspace"]) == 1
    assert row["passed"] is not failed
    assert row["diagnostic_custody_verified"] is not failed
    assert suite.gate([row], suite.TASKS[2:3], 1, require_private=True)["accepted"] is not failed
    if failed:
        assert row["failure_class"] == "diagnostic_custody_unavailable"
    else:
        assert len(row["diagnostic_calls"]) == 1
        public = json.dumps(row)
        assert "PRIVATE" not in public and live[0].final_reference not in public


def test_private_metadata_rejects_unlinked_calls_malformed_shapes_and_required_capture(fx, tmp_path):
    evidence = recorder(fx, tmp_path)
    evidence.verdict(call(evidence, fx), (False, "PRIVATE"))
    evidence.finalize({}, {})
    row = {**evidence.public_metadata(), **evidence.public_metadata()["diagnostic_binding"]}
    assert PrivateTrialEvidence.metadata_valid(row)
    for field, value in (("call_id", "other:1"), ("verdict_sha256", None), ("user_bytes", True),
                         ("readback_bytes", 5001), ("phase", []), ("error_code", [])):
        changed = json.loads(json.dumps(row))
        changed["diagnostic_calls"][0][field] = value
        assert not PrivateTrialEvidence.metadata_valid(changed)
    selected = suite.TASKS[:1]
    assert suite.gate([], selected, 1, require_private=True)["accepted"] is False


def test_explicit_goal_or_native_or_debug_scope_never_logs(fx, tmp_path, monkeypatch):
    evidence = recorder(fx, tmp_path)
    mac = BrowserMac(factory=lambda: None)
    with pytest.raises(PrivateEvidenceError, match="scope_invalid"):
        evidence.admit("different goal", mac)
    evidence.close()
    evidence = recorder(fx, tmp_path)
    monkeypatch.setenv("CU_TRIAL_DEBUG", "PRIVATE RAW LOG PATH")
    with pytest.raises(PrivateEvidenceError, match="scope_invalid"):
        evidence.admit(suite.TASKS[2].goal(fx), mac)
    evidence.close()
