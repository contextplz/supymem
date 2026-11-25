"""Controlled baseline invocation custody; no live models or native GUI."""
import copy
import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts/desktop"))
import cu_ai_trial as trial
import cu_browser_suite as suite
import cu_acceptance
from cu_acceptance import evaluate, source_digest
from cu_trial_private_evidence import PrivateEvidenceError, PrivateTrialEvidence
from desktop_runtime.cu_ai_engine import Observation
from desktop_runtime.cu_ai_browser import BrowserMac
from desktop_runtime.cu_ai_browser import ChromeFactory
from desktop_runtime.cu_browser.chrome import CHROME_BIN
from coding_agents.cu_ai_loop import Action
from coding_agents.cu_ai_provider import OPENAI_ENDPOINT


@pytest.fixture
def historical_profile():
    return {"backend": "cuai", "completion_policy": "browser_semantic_review_v1", "max_provider_calls": 64,
        "max_request_bytes": 65536, "policy_version": 1, "version": "browser_cuai_text_v1",
        "pricing_digest": "deb0f50f682218f1462632140b85e7d1764c72ab760a1dda51c880d81b286e90",
        "roles": {phase: {"provider": "openai", "model": "gpt-5.6-luna", "reasoning": "none",
            "service_tier": "default", "max_output_tokens": 700} for phase in ("plan", "actor", "verify")}}


def install(monkeypatch, tmp_path, *, wrong_effect=False, capture_failure=False, final_failure=False):
    bodies, instances, stops = [], [], []
    for key in ("CU_TRIAL_DEBUG", "CU_TRIAL_PRIORITY", "CU_BROWSER_CACHE"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("OPENAI_DEVICE_VISION_API_KEY", "AUTH_SENTINEL_NEVER_CAPTURE")

    client = httpx.Client

    def transport(request):
        assert request.url == OPENAI_ENDPOINT
        assert request.headers["Authorization"] == "Bearer AUTH_SENTINEL_NEVER_CAPTURE"
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"status": "completed", "output": [{"content": [{"type": "output_text", "text": '{"passed":true,"reason":"PRIVATE VERDICT"}'}]}], "usage": {"input_tokens": 5, "output_tokens": 3}})

    def Client(**kwargs):
        assert kwargs["trust_env"] is False and kwargs["follow_redirects"] is False
        return client(transport=httpx.MockTransport(transport), **kwargs)

    class Recorder(PrivateTrialEvidence):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=tmp_path, **kwargs)
            instances.append(self)
            if capture_failure:
                self.MAX_FILE_BYTES = 1
        def finalize(self, *args):
            if final_failure:
                raise OSError("PRIVATE OS DETAIL")
            return super().finalize(*args)

    def engine(goal, decide, mac, stop, *, verify, log, verifier_diagnostic, **kwargs):
        origin = goal.split()[1].removesuffix("/prefs")
        body = {"country": "Portugal", "news": "yes", "plan": "free" if wrong_effect else "pro"}
        request = Request(origin + "/prefs", data=urlencode(body).encode(), method="POST")
        with urlopen(request, timeout=3) as response:
            assert response.status == 200
        mac.current_url = origin + "/prefs/done"
        obs = Observation(frontmost="Chrome (CDP)", window_title="PRIVATE WINDOW", elements=[], png=b"", snapshot_id="snapshot", notes="PRIVATE PAGE")
        obs.browser_snapshot_id, obs.browser_tab_state = "owned-snapshot", (1, "owned-tab")
        try:
            verdict = verify("PRIVATE ANSWER", obs, "PRIVATE EXACT READBACK")
        except Exception:
            stops.append(stop.is_set())
        else:
            verifier_diagnostic({"step": 4, "passed": verdict[0], "reason": verdict[1], "code": None})
        return SimpleNamespace(outcome="done", reason="", summary="PRIVATE ANSWER", steps=4, log=[])

    monkeypatch.setattr(trial.httpx, "Client", Client)
    monkeypatch.setattr(trial, "run_loop", engine)
    monkeypatch.setattr(suite, "PrivateTrialEvidence", Recorder)
    import desktop_runtime.cu_ai_browser as browser
    monkeypatch.setattr(browser, "ChromeFactory", lambda *a, **k: SimpleNamespace(session=None))
    return bodies, instances, stops


def run(monkeypatch, tmp_path, **kwargs):
    bodies, instances, stops = install(monkeypatch, tmp_path, **kwargs)
    task = next(t for t in suite.TASKS if t.id == "select_prefs")
    row = suite.run_one(task, None, "gpt-5.6-luna", run_id="a" * 32, repetition=1,
                        base_digest=source_digest(scope="browser"), private_diagnostics=True)
    return row, bodies, instances[0], stops


@pytest.mark.parametrize("model", ["gpt-5.6-luna", "gpt-6-luna"])
def test_baseline_provider_stub_uses_real_stream_transport(monkeypatch, tmp_path, model):
    bodies, instances, stops = install(monkeypatch, tmp_path)

    def engine(goal, decide, mac, stop, *, verify, **kwargs):
        obs = Observation(frontmost="Chrome (CDP)", window_title="PRIVATE WINDOW", elements=[],
                          png=b"", snapshot_id="snapshot", notes="PRIVATE PAGE")
        assert verify("PRIVATE ANSWER", obs, "PRIVATE EXACT READBACK")[0] is True
        return SimpleNamespace(outcome="done", reason="", summary="PRIVATE ANSWER", steps=1, log=[])

    monkeypatch.setattr(trial, "run_loop", engine)
    result = trial.run_task("Controlled provider transport check", mac=BrowserMac(factory=lambda: None), model=model)
    assert result["model_calls"] == len(bodies) == 1
    assert bodies[0]["model"] == model and bodies[0]["max_output_tokens"] == 300
    assert result["cleanup_verified"] is True
    assert not instances and not stops


def test_actual_baseline_runner_exact_call_result_and_oracle_custody(monkeypatch, tmp_path, capsys, historical_profile):
    monkeypatch.setattr(cu_acceptance, "reviewed_browser_profile", lambda: historical_profile)
    row, bodies, evidence, _ = run(monkeypatch, tmp_path)
    assert row["passed"] is True and row["cleanup_verified"] is True
    assert row["diagnostic_scope"] == "controlled_baseline"
    assert row["calls"] == len(bodies) == 1
    request = json.loads((evidence.path / "verify-0001-request.json").read_text())
    assert request["user_text"] == bodies[0]["input"][1]["content"][0]["text"]
    assert request["system_prompt"] == bodies[0]["input"][0]["content"]
    assert request["readback_context"] == "PRIVATE EXACT READBACK"
    final = json.loads((evidence.path / "result.json").read_text())
    assert final["oracle"]["original_oracle"] is True
    assert len(final["oracle"]["posts"]) == len(final["oracle"]["request_receipts"]) == 1
    assert final["result"]["engine_result"]["summary"] == "PRIVATE ANSWER"
    manifest = json.loads((evidence.path / "manifest.json").read_text())
    for file in manifest["files"]:
        assert hashlib.sha256((evidence.path / file["name"]).read_bytes()).hexdigest() == file["sha256"]
    assert PrivateTrialEvidence.metadata_valid(row)
    exported = json.dumps(row)
    assert "PRIVATE" not in exported and "AUTH_SENTINEL" not in exported and str(evidence.path) not in exported
    assert all(b"AUTH_SENTINEL" not in p.read_bytes() for p in evidence.path.iterdir())
    printed = capsys.readouterr().out
    assert f"PRIVATE_EVIDENCE select_prefs repetition=1 {evidence.path}" in printed
    assert "PRIVATE ANSWER" not in printed and "PRIVATE VERDICT" not in printed
    assert evaluate([row], ["select_prefs"], suite="browser", repetitions=1, require_private=True)["accepted"]


def test_captured_false_effect_is_not_saved_by_positive_verifier(monkeypatch, tmp_path):
    row, bodies, evidence, _ = run(monkeypatch, tmp_path, wrong_effect=True)
    assert len(bodies) == 1 and row["oracle"] is False and row["passed"] is False
    assert row["diagnostic_custody_verified"] is True
    assert json.loads((evidence.path / "result.json").read_text())["oracle"]["original_oracle"] is False


def test_opt_in_does_not_change_existing_bounded_provider_request_or_add_calls(monkeypatch, tmp_path):
    bodies, instances, _ = install(monkeypatch, tmp_path)
    fx = suite.Fx("abcdef12").start()
    mac = BrowserMac(factory=lambda: None)
    goal = next(t.task for t in suite.TASKS if t.id == "select_prefs").replace("{base}", fx.base)
    try:
        plain = trial.run_task(goal, mac=mac)
        private = suite.PrivateTrialEvidence(fx, goal, run_id="a" * 32, task="select_prefs", repetition=1,
                                            source_digest="b" * 64, harness_digest="c" * 64,
                                            fixture_scope="controlled_baseline")
        captured = trial.run_task(goal, mac=mac, private_evidence=private)
        assert len(instances) == 1 and len(bodies) == 2
        assert plain["model_calls"] == captured["model_calls"] == 1
        assert bodies[0] == bodies[1]
        private.finalize(captured, {"original_oracle": True})
    finally:
        fx.close()


@pytest.mark.parametrize("failure", ["capture_failure", "final_failure"])
def test_private_loss_fails_strict_and_keeps_cleanup(monkeypatch, tmp_path, failure):
    row, bodies, _, stops = run(monkeypatch, tmp_path, **{failure: True})
    assert row["passed"] is False and row["failure_class"] == "diagnostic_custody_unavailable"
    assert row["diagnostic_custody_verified"] is False and row["cleanup_verified"] is True
    if failure == "capture_failure":
        assert not bodies and stops == [True]
    else:
        assert len(bodies) == 1


def test_public_capture_refused_before_fixture_provider_and_key(monkeypatch, tmp_path):
    monkeypatch.setattr(suite, "Fx", lambda *a: pytest.fail("public capture constructed a fixture"))
    row = suite.run_one(suite.LIVE[0], None, "gpt-5.6-luna", run_id="a" * 32,
                        base_digest="b" * 64, private_diagnostics=True)
    assert row["passed"] is False and row["diagnostic_error_code"] == "qa_private_evidence_scope_invalid"
    monkeypatch.setattr(sys, "argv", ["suite", "--private-diagnostics", "--live", "--cat", "live", "--key-file", str(tmp_path / "missing")])
    with pytest.raises(SystemExit) as error:
        suite.main()
    assert error.value.code == 2


@pytest.mark.parametrize("wide", [False, True])
def test_private_capture_uses_shared_verifier_bounds_without_dropping_custody(wide, tmp_path):
    from coding_agents.cu_verification_context import (
        MAX_CONTEXT_BYTES, MAX_REQUEST_BYTES, WIDE_CONTEXT_BYTES, WIDE_REQUEST_BYTES,
    )
    fx = suite.Fx("abcdef12")
    fx.httpd = SimpleNamespace(server_address=("127.0.0.1", 12345))
    fx.thread = SimpleNamespace(is_alive=lambda: True)
    evidence = PrivateTrialEvidence(fx, "Read " + fx.base + "/source", run_id="a" * 32,
        task="source_read", repetition=1, source_digest="b" * 64, harness_digest="c" * 64,
        directory=tmp_path, fixture_scope="controlled_baseline")
    request_bound = WIDE_REQUEST_BYTES if wide else MAX_REQUEST_BYTES
    context_bound = WIDE_CONTEXT_BYTES if wide else MAX_CONTEXT_BYTES
    prefix = "MULTI-PAGE TASK\n" if wide else ""
    user = prefix + "U" * (request_bound - len(prefix))
    context = "界" * (context_bound // 3) + "C" * (context_bound % 3)
    call_id = evidence.request(user, context, model="gpt-6-luna", phase="whole_task_verification",
        current_url=fx.base + "/source", snapshot_id="owned-snapshot", tab_state=(1, "owned-tab"),
        system_prompt="PRIVATE VERIFIER RULES")
    evidence.verdict(call_id, (True, "Independent source match"))
    evidence.engine_verdict({"step": 7, "passed": True, "reason": "Independent source match"})
    evidence.finalize({"summary": "PRIVATE ANSWER"}, {"original_oracle": True})
    request = json.loads((evidence.path / "verify-0001-request.json").read_text())
    assert request["user_text"] == user and request["readback_context"] == context
    assert request["observed_snapshot_id"] == "owned-snapshot" and request["observed_tab_state"] == [1, "owned-tab"]
    manifest = json.loads((evidence.path / "manifest.json").read_text())
    assert manifest["calls"][0]["wide_context"] is wide
    assert manifest["calls"][0]["user_bytes"] == request_bound
    assert manifest["calls"][0]["readback_bytes"] == context_bound
    for entry in manifest["files"]:
        assert hashlib.sha256((evidence.path / entry["name"]).read_bytes()).hexdigest() == entry["sha256"]
    row = {**evidence.public_metadata(), **evidence.public_metadata()["diagnostic_binding"]}
    assert PrivateTrialEvidence.metadata_valid(row)
    for field, value in (("user_bytes", request_bound + 1), ("readback_bytes", context_bound + 1), ("wide_context", 1)):
        changed = copy.deepcopy(row)
        changed["diagnostic_calls"][0][field] = value
        assert not PrivateTrialEvidence.metadata_valid(changed)
    legacy = copy.deepcopy(row)
    del legacy["diagnostic_calls"][0]["wide_context"]
    assert PrivateTrialEvidence.metadata_valid(legacy) is (not wide)
    assert "PRIVATE ANSWER" not in json.dumps(row) and "PRIVATE VERIFIER RULES" not in json.dumps(row)


@pytest.mark.parametrize("field", ["user", "readback"])
def test_private_wide_capture_still_refuses_over_bound_input(field, tmp_path):
    from coding_agents.cu_verification_context import WIDE_CONTEXT_BYTES, WIDE_REQUEST_BYTES
    fx = suite.Fx("abcdef12")
    fx.httpd = SimpleNamespace(server_address=("127.0.0.1", 12345))
    fx.thread = SimpleNamespace(is_alive=lambda: True)
    evidence = PrivateTrialEvidence(fx, "Read " + fx.base + "/source", run_id="a" * 32,
        task="source_read", repetition=1, source_digest="b" * 64, harness_digest="c" * 64,
        directory=tmp_path, fixture_scope="controlled_baseline")
    user = "MULTI-PAGE TASK\n" + "U" * WIDE_REQUEST_BYTES if field == "user" else "MULTI-PAGE TASK\n"
    context = "C" * (WIDE_CONTEXT_BYTES + 1) if field == "readback" else ""
    with pytest.raises(PrivateEvidenceError, match="schema_invalid"):
        evidence.request(user, context, model="gpt-6-luna", phase="whole_task_verification", current_url=fx.base + "/source")
    assert not (evidence.path / "verify-0001-request.json").exists()
    evidence.close()


def test_private_path_prints_and_summary_stays_content_free_without_network(monkeypatch, tmp_path, capsys):
    def start(fixture):
        fixture.httpd = SimpleNamespace(server_address=('127.0.0.1', 12345))
        fixture.thread = SimpleNamespace(is_alive=lambda: True)
        fixture._base = 'http://127.0.0.1:12345'
        return fixture
    monkeypatch.setattr(suite.Fx, 'start', start)
    monkeypatch.setattr(suite.Fx, 'close', lambda self: None)
    def run_task(goal, **kwargs):
        evidence = kwargs['private_evidence']
        call = evidence.request('PRIVATE INPUT', 'PRIVATE READBACK', model='gpt-6-luna',
                                phase='whole_task_verification', current_url=evidence.origin + '/app',
                                system_prompt='PRIVATE VERIFIER RULES')
        evidence.verdict(call, (False, 'PRIVATE REASON'))
        return {'outcome':'failed', 'reason':'needs_founder', 'summary':'PRIVATE ANSWER'}
    monkeypatch.setattr(trial, 'run_task', run_task)
    row = suite.run_one(next(t for t in suite.TASKS if t.id == 'todos'), None, 'gpt-6-luna',
                        run_id='a' * 32, base_digest='b' * 64, private_diagnostics=True)
    printed = capsys.readouterr().out
    line = next(line for line in printed.splitlines() if line.startswith('PRIVATE_EVIDENCE '))
    path = Path(line.split(' ', 3)[3])
    assert path.joinpath('manifest.json').is_file() and row['diagnostic_custody_verified']
    assert json.loads(path.joinpath('verify-0001-request.json').read_text())['system_prompt'] == 'PRIVATE VERIFIER RULES'
    assert 'PRIVATE ANSWER' not in printed and 'PRIVATE ANSWER' not in json.dumps(row)
    assert 'PRIVATE VERIFIER RULES' not in printed and 'PRIVATE VERIFIER RULES' not in json.dumps(row)


@pytest.mark.parametrize("variant", ["public_goal", "uppercase_public_goal", "public_request", "stopped", "unknown_scope"])
def test_baseline_scope_remains_exact_live_owned_origin(tmp_path, variant):
    fx = suite.Fx("abcdef12").start()
    goal = "At " + fx.base + "/prefs save preferences."
    kwargs = dict(run_id="a" * 32, task="select_prefs", repetition=1,
                  source_digest="b" * 64, harness_digest="c" * 64,
                  directory=tmp_path, fixture_scope="controlled_baseline")
    try:
        if variant == "public_request":
            recorder = PrivateTrialEvidence(fx, goal, **kwargs)
            with pytest.raises(PrivateEvidenceError, match="scope_invalid"):
                recorder.request("private", "private", model="m", phase="whole_task_verification", current_url="https://example.com")
            recorder.close()
        else:
            if variant in {"public_goal", "uppercase_public_goal"}:
                goal += " Open " + ("HTTPS" if variant == "uppercase_public_goal" else "https") + "://example.com."
            elif variant == "stopped":
                fx.close()
            else:
                kwargs["fixture_scope"] = "unknown"
            with pytest.raises(PrivateEvidenceError, match="scope_invalid"):
                PrivateTrialEvidence(fx, goal, **kwargs)
    finally:
        if fx.thread.is_alive():
            fx.close()


def test_gate_rejects_missing_rebound_reused_or_public_custody(monkeypatch, tmp_path, historical_profile):
    monkeypatch.setattr(cu_acceptance, "reviewed_browser_profile", lambda: historical_profile)
    row, _, _, _ = run(monkeypatch, tmp_path)
    plain = {k: v for k, v in row.items() if not k.startswith("diagnostic_")}
    assert not evaluate([plain], ["select_prefs"], suite="browser", repetitions=1, require_private=True)["accepted"]
    corrupt = copy.deepcopy(row)
    corrupt["diagnostic_binding"]["task"] = "wrong"
    assert not evaluate([corrupt], ["select_prefs"], suite="browser", repetitions=1)["accepted"]
    reused = copy.deepcopy(row)
    reused["repetition"] = reused["diagnostic_binding"]["repetition"] = 2
    assert not evaluate([row, reused], ["select_prefs"], suite="browser", repetitions=2)["accepted"]
    public = copy.deepcopy(row)
    public["task"] = public["diagnostic_binding"]["task"] = suite.LIVE[0].id
    assert not evaluate([public], [public["task"]], suite="browser", repetitions=1)["accepted"]
    malformed = copy.deepcopy(row)
    malformed["diagnostic_trial_id"] = {"unknown": "not an ID"}
    assert not evaluate([malformed], ["select_prefs"], suite="browser", repetitions=1)["accepted"]
    # Combined gates can require controlled custody while keeping public uncaptured.
    public = {k: v for k, v in plain.items() if k != "harness_source_digest"}
    public["task"] = suite.LIVE[0].id
    assert evaluate([row, public], ["select_prefs", public["task"]], suite="browser", repetitions=1, require_private=True)["accepted"]


def test_browser_source_digest_includes_private_recorder_bytes(tmp_path):
    scripts = tmp_path / "scripts/desktop"
    scripts.mkdir(parents=True)
    helper = scripts / "cu_trial_private_evidence.py"
    helper.write_text("first")
    before = source_digest(tmp_path, scope="browser")
    helper.write_text("second")
    assert source_digest(tmp_path, scope="browser") != before


@pytest.mark.parametrize("relative", ["config/browser_cuai_profile.json", "config/llm_pricing.yaml"])
def test_browser_source_digest_binds_reviewed_profile_and_pricing(tmp_path, relative):
    path = tmp_path / relative
    path.parent.mkdir(parents=True)
    path.write_text("first")
    before = source_digest(tmp_path, scope="browser")
    path.write_text("second")
    assert source_digest(tmp_path, scope="browser") != before


def test_reviewed_browser_profile_is_the_active_config():
    from cu_acceptance import reviewed_browser_profile
    profile = reviewed_browser_profile()
    active = json.loads((Path(__file__).parents[2] / "config/browser_cuai_profile.json").read_text())
    assert profile == active
    assert {role["model"] for role in profile["roles"].values()} == {"gpt-6-luna"}
    assert {role["provider"] for role in profile["roles"].values()} == {"openai"}


def test_controlled_gate_rejects_historical_model_under_active_reviewed_profile(historical_profile, monkeypatch):
    row = {"task": "select_prefs", "repetition": 1, "run_id": "owned-run", "source_digest": "a" * 64,
        "model": historical_profile["roles"]["actor"]["model"], "qa_transport": "owned_qa", "passed": True,
        "cleanup_verified": True, "oracle": True, "independent_verification": True,
        "browser_process_gone": True, "forbidden_requests": 0}
    with monkeypatch.context() as historical:
        historical.setattr(cu_acceptance, "reviewed_browser_profile", lambda: historical_profile)
        assert evaluate([row], [row["task"]], suite="browser", repetitions=1)["accepted"]
    rejected = evaluate([row], [row["task"]], suite="browser", repetitions=1)
    assert not rejected["accepted"] and rejected["faults"] == ["unreviewed_browser_model"]
    row["model"] = cu_acceptance.reviewed_browser_profile()["roles"]["actor"]["model"]
    assert evaluate([row], [row["task"]], suite="browser", repetitions=1)["accepted"]


BLANK_REFUSALS = ["not_admitted", "late_admission", "unowned_backend", "copied_profile", "closed_session",
                  "foreign_process", "stale_snapshot", "wrong_tab", "stale_document", "nonempty_page",
                  "omitted_history", "foreign_history", "foreign_request", "foreign_context",
                  "uppercase_request", "uppercase_context", "uppercase_history", "uppercase_navigation",
                  "missing_download", "missing_receipt", "failed_navigation", "foreign_factory"]


@pytest.fixture(scope="module")
def owned_blank_download(tmp_path_factory):
    if not Path(CHROME_BIN).exists():
        pytest.skip("requires fresh owned headless Chrome; no model calls")
    root = tmp_path_factory.mktemp("private-blank-custody")
    class FilesFixture(suite.Fx):
        def get(self, handler, path, query):
            if path == "/files/export.csv":
                return handler.out(200, self.csv, "text/csv", [("Content-Disposition", "attachment; filename=report.csv")])
            return super().get(handler, path, query)
    fx = FilesFixture("abcdef12").start()
    factory = ChromeFactory(root / "browser", profile_email="")
    mac = BrowserMac(factory=factory)
    goal = "Download the file from " + fx.base + "/files/export.csv and report when saved."
    evidence = {}
    def make():
        return PrivateTrialEvidence(fx, goal, run_id="a" * 32, task="download", repetition=1,
                                    source_digest="b" * 64, harness_digest="c" * 64,
                                    directory=root, fixture_scope="controlled_baseline")
    session = None
    try:
        for name in ["positive", "uppercase_owned", "after_close"] + BLANK_REFUSALS:
            evidence[name] = make()
            if name not in {"not_admitted", "late_admission"}:
                evidence[name].admit(goal, mac)
        initial = mac.observe()
        session = factory.session
        mac.act(Action(op="navigate", text=fx.base + "/files/export.csv"), initial)
        obs = mac.observe()
        assert mac.current_url == "about:blank" and not obs.elements
        assert len(mac.b.downloads) == fx.hit_count("/files/export.csv") == 1
        assert Path(mac.b.downloads[0]).read_bytes() == fx.csv
        evidence["late_admission"].admit(goal, mac)
        yield fx, mac, obs, evidence, goal
    finally:
        mac.close()
        assert session is not None and session.closed and session._group_gone and session.proc.poll() is not None
        if "after_close" in evidence:
            with pytest.raises(PrivateEvidenceError, match="scope_invalid"):
                evidence["after_close"].request(goal, "", model="no-model", phase="whole_task_verification",
                                                 current_url="about:blank", snapshot_id=getattr(mac.last, "browser_snapshot_id", None),
                                                 tab_state=getattr(mac.last, "browser_tab_state", None))
        for recorder in evidence.values():
            recorder.close()
        fx.close()
        assert not fx.thread.is_alive()


def blank_request(recorder, mac, obs, goal, *, user=None, context=None, snapshot=None, tabs=None):
    return recorder.request(goal if user is None else user, obs.notes if context is None else context,
                            model="no-model", phase="whole_task_verification", current_url=mac.current_url,
                            snapshot_id=obs.browser_snapshot_id if snapshot is None else snapshot,
                            tab_state=obs.browser_tab_state if tabs is None else tabs)


def test_actual_owned_blank_download_capture_preserves_exact_input_and_negative_verdict(owned_blank_download, capsys):
    fx, mac, obs, recorders, goal = owned_blank_download
    recorder = recorders["positive"]
    call_id = blank_request(recorder, mac, obs, goal)
    value = json.loads((recorder.path / "verify-0001-request.json").read_text())
    assert value["user_text"] == goal and value["readback_context"] == obs.notes
    assert value["current_page_custody"]["kind"] == "owned_blank_after_stored_download"
    assert value["observed_snapshot_id"] == obs.browser_snapshot_id
    recorder.verdict(call_id, (False, "PRIVATE NEGATIVE VERDICT"))
    recorder.finalize({"outcome": "needs_you"}, {"original_oracle": False})
    assert recorder.public_metadata()["diagnostic_calls"][0]["passed"] is False
    assert fx.hit_count("/files/export.csv") == len(mac.b.downloads) == 1
    assert "PRIVATE" not in json.dumps(recorder.public_metadata()) and not capsys.readouterr().out


def test_uppercase_owned_url_control_keeps_exact_bounded_bytes(owned_blank_download):
    fx, mac, obs, recorders, goal = owned_blank_download
    recorder = recorders["uppercase_owned"]
    user = goal + " Source: " + fx.base.replace("http://", "HTTP://") + "/files/export.csv"
    call_id = blank_request(recorder, mac, obs, goal, user=user)
    value = json.loads((recorder.path / "verify-0001-request.json").read_text())
    assert value["user_text"] == user
    recorder.verdict(call_id, (False, "PRIVATE UNKNOWN"))
    recorder.finalize({"outcome": "needs_you"}, {"original_oracle": False})


@pytest.mark.parametrize("variant", BLANK_REFUSALS)
def test_about_blank_alone_never_authorizes_private_capture(owned_blank_download, monkeypatch, variant):
    _, mac, obs, recorders, goal = owned_blank_download
    recorder = recorders[variant]
    backend, factory, session = mac.b, mac.factory, mac.factory.session
    options = {}
    if variant == "unowned_backend":
        monkeypatch.setattr(mac, "b", SimpleNamespace())
    elif variant == "copied_profile":
        monkeypatch.setattr(factory, "profile_email", "account@example.invalid")
    elif variant == "closed_session":
        monkeypatch.setattr(session, "closed", True)
    elif variant == "foreign_process":
        monkeypatch.setattr(session, "_owned_process", object())
    elif variant == "stale_snapshot":
        options["snapshot"] = "0" * 32
    elif variant == "wrong_tab":
        options["tabs"] = (2, obs.browser_tab_state[1])
    elif variant == "stale_document":
        monkeypatch.setattr(backend, "_indexed_generation", backend._indexed_generation + 1)
    elif variant == "nonempty_page":
        monkeypatch.setitem(mac.seen, "about:blank", "PRIVATE UNKNOWN DOCUMENT")
    elif variant == "omitted_history":
        monkeypatch.setattr(mac, "history_omitted", True)
    elif variant == "foreign_history":
        monkeypatch.setitem(mac.seen, "https://example.com", "PRIVATE FOREIGN PAGE")
    elif variant in {"foreign_request", "foreign_context"}:
        options["user" if variant == "foreign_request" else "context"] = "https://example.com PRIVATE"
    elif variant in {"uppercase_request", "uppercase_context"}:
        options["user" if variant == "uppercase_request" else "context"] = "HTTPS://example.com PRIVATE"
    elif variant == "uppercase_history":
        monkeypatch.setitem(mac.seen, "HTTPS://example.com", "PRIVATE FOREIGN PAGE")
    elif variant == "uppercase_navigation":
        monkeypatch.setattr(mac, "did", mac.did + ["navigate HTTPS://example.com/file.csv -> about:blank"])
    elif variant == "missing_download":
        monkeypatch.setattr(backend, "downloads", [])
    elif variant == "missing_receipt":
        monkeypatch.setattr(backend, "_action_receipts", [])
    elif variant == "failed_navigation":
        monkeypatch.setitem(backend._action_receipts[-1], "status", "error")
    elif variant == "foreign_factory":
        monkeypatch.setattr(mac, "factory", object())
    with pytest.raises(PrivateEvidenceError, match="scope_invalid"):
        blank_request(recorder, mac, obs, goal, **options)
    assert not (recorder.path / "verify-0001-request.json").exists()
    assert recorder.public_metadata()["diagnostic_calls"] == []
