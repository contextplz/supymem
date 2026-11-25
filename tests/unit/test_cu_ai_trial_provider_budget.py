"""The QA allowance must guard real request plumbing, without provider calls."""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import httpx

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "desktop"))
import cu_ai_trial as trial  # noqa: E402
from coding_agents.cu_ai_loop import BROWSER_MARKER  # noqa: E402


@pytest.fixture
def plumbing(monkeypatch):
    import cu_verifier_artifact
    attempts, clients = [], []
    monkeypatch.setattr(trial, "QA_PROVIDER_BUDGET", None)
    monkeypatch.setenv("OPENAI_DEVICE_VISION_API_KEY", "test-placeholder")
    for name in ("CU_TRIAL_PRIORITY", "CU_TRIAL_DEBUG", "CU_BROWSER_CACHE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(trial, "FileCheckpoint", lambda _: SimpleNamespace(clear=lambda: None))
    monkeypatch.setattr(cu_verifier_artifact, "PrivateVerifierArtifact", lambda: SimpleNamespace(
        close=lambda: None, record=lambda _: None, metadata=lambda: {}))
    observation = SimpleNamespace(frontmost="Chrome (browser)", window_title="Owned", elements=[],
                                  notes=BROWSER_MARKER, png=b"")
    control = {"error": None, "usage_valid": True, "usage_exceeds": False,
               "response_model": "gpt-6-sol", "invalid_total": False}

    class Client:
        def __init__(self, **_kwargs):
            self.closed = False
            clients.append(self)

        def post(self, _url, *, json, headers):
            # Bodies sent to the provider are the already-reserved ordinary
            # builders' bodies; the guard never inserts task/oracle evidence.
            attempts.append(json)
            if control["error"]:
                raise control["error"]
            properties = json["text"]["format"]["schema"]["properties"]
            reply = ({"subgoals": ["Inspect"]} if "subgoals" in properties else
                     {"passed": True, "reason": "Source matches"} if "passed" in properties else
                     {"actions": [{"op": "done", "summary": "Observed"}], "memory": "", "subgoal_done": False})
            value = {"model": control["response_model"], "status": "completed",
                     "usage": {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120},
                     "output": [{"type": "message", "content": [{"type": "output_text", "text": __import__("json").dumps(reply)}]}]}
            if not control["usage_valid"]:
                value.pop("usage")
            if control["usage_exceeds"]:
                value["usage"] = {"input_tokens": 1_000_000, "output_tokens": 20, "total_tokens": 1_000_020}
            if control["invalid_total"]:
                value["usage"]["total_tokens"] = 1
            return SimpleNamespace(status_code=200, content=b"stub", json=lambda: value)

        def close(self):
            self.closed = True

    monkeypatch.setattr(trial.httpx, "Client", Client)
    def read_response(client, url, headers, body):
        response = client.post(url, headers=headers, json=body)
        try:
            raw = __import__('json').dumps(response.json()).encode() if response.content else b''
        except ValueError:
            raw = b'not json'
        return response.status_code, raw
    monkeypatch.setattr(trial, "read_response", read_response)

    def engine(_goal, decide, _mac, _stop, *, planner, verify, **_kwargs):
        result = SimpleNamespace(outcome="done", reason="", summary="Observed", steps=1, log=[])
        try:
            planner("Owned fixture task", "", "start", observation)
            decide({"goal": "Owned fixture task", "observation": observation})
            verify("Observed", observation, "")
        except Exception:
            result.outcome, result.reason = "failed", "model_unavailable"
        return result

    monkeypatch.setattr(trial, "run_loop", engine)
    return SimpleNamespace(attempts=attempts, clients=clients, control=control,
                           run=lambda: trial.run_task("Owned fixture task", model="gpt-6-sol", mac=object()))


def test_all_three_phase_requests_reserve_before_provider_and_settle(plumbing):
    guard = trial.configure_provider_budget("gpt-6-sol", max_calls=3, max_estimated_usd="10")
    result = plumbing.run()
    assert result["outcome"] == "done"
    assert [body["max_output_tokens"] for body in plumbing.attempts] == [600, 700, 300]
    assert result["provider_budget_attempted_calls"] == result["provider_budget_confirmed_calls"] == 3
    assert result["provider_cost_usage_complete"] is True
    assert guard.snapshot()["confirmed"] == 3 and guard.snapshot()["attempted_calls"] == 3
    assert result["input_tokens"] == 300 and result["output_tokens"] == 60
    assert all(client.closed for client in plumbing.clients)


def test_subgoal_verifier_uses_same_paid_allowance_without_whole_task_claim(plumbing, monkeypatch):
    observation = SimpleNamespace(frontmost='Chrome (browser)', window_title='Owned', elements=[], notes=BROWSER_MARKER, png=b'')
    def engine(_goal, _decide, _mac, _stop, *, verify_subgoal, **kwargs):
        passed, _ = verify_subgoal('Fill reviewed code', observation, '')
        return SimpleNamespace(outcome='failed', reason='remaining_work', summary='', steps=1, log=[])
    monkeypatch.setattr(trial, 'run_loop', engine)
    guard = trial.configure_provider_budget('gpt-6-sol', max_calls=1, max_estimated_usd='10')
    result = plumbing.run()
    from coding_agents.cu_ai_loop import SUBGOAL_VERIFY_PROMPT
    assert len(plumbing.attempts) == 1 and plumbing.attempts[0]['input'][0]['content'] == SUBGOAL_VERIFY_PROMPT
    assert guard.snapshot()['confirmed'] == 1 and result['outcome'] == 'failed'


def test_verifier_call_denied_before_provider_does_not_waive_failure(plumbing):
    guard = trial.configure_provider_budget("gpt-6-sol", max_calls=2, max_estimated_usd="10")
    result = plumbing.run()
    assert result["outcome"] == "failed" and len(plumbing.attempts) == 2
    assert result["provider_budget_denial"] == "qa_provider_call_limit"
    assert guard.snapshot()["attempted_calls"] == 2
    assert all(client.closed for client in plumbing.clients)


def test_same_guard_covers_later_phase_without_reset(plumbing):
    guard = trial.configure_provider_budget("gpt-6-sol", max_calls=4, max_estimated_usd="10")
    first, second = plumbing.run(), plumbing.run()
    assert first["outcome"] == "done" and second["outcome"] == "failed"
    assert len(plumbing.attempts) == 4 and trial.QA_PROVIDER_BUDGET is guard
    assert guard.snapshot()["confirmed"] == 4
    with pytest.raises(ValueError, match="already_configured"):
        trial.configure_provider_budget("gpt-6-sol", max_calls=4, max_estimated_usd="10")


def test_network_exception_keeps_full_unknown_reservation(plumbing):
    guard = trial.configure_provider_budget("gpt-6-sol", max_calls=1, max_estimated_usd="10")
    plumbing.control["error"] = RuntimeError("simulated_transport_failure")
    result = plumbing.run()
    snapshot = guard.snapshot()
    assert result["outcome"] == "failed" and len(plumbing.attempts) == 1
    assert snapshot["unknown"] == 1 and snapshot["confirmed"] == snapshot["in_flight"] == 0
    assert float(snapshot["committed_estimated_usd"]) > 0
    assert result["provider_cost_usage_complete"] is False
    assert result["provider_budget_attempted_calls"] == 1 and result["model_calls"] == 0


def test_trial_never_replays_a_provider_timeout_even_if_engine_asks_again(plumbing, monkeypatch):
    import httpx
    from types import SimpleNamespace
    guard = trial.configure_provider_budget("gpt-6-sol", max_calls=5, max_estimated_usd="10")
    plumbing.control["error"] = httpx.ReadTimeout("outcome unknown")
    def engine(goal, decide, mac, stop, *, planner, **kwargs):
        for attempt in range(2):
            with pytest.raises((httpx.ReadTimeout, RuntimeError)):
                planner(goal, "", "retry", None)
        return SimpleNamespace(outcome="failed", reason="model_unavailable", summary="", steps=0, log=[])
    monkeypatch.setattr(trial, "run_loop", engine)
    result = plumbing.run()
    assert len(plumbing.attempts) == 1 and guard.snapshot()["unknown"] == 1
    assert result["provider_budget_attempted_calls"] == 1 and result["providers"] == []
    assert result["provider_calls"][0]["kind"] == "timeout"
    assert result["provider_calls"][0]["timeout_phase"] == "read"


@pytest.mark.parametrize('limit', [1, 4])
def test_azure_timeout_fallback_records_both_paid_attempts(plumbing, monkeypatch, limit):
    from coding_agents.cu_ai_provider import NoOutputReadTimeout
    monkeypatch.setenv('AZURE_OPENAI_CUAI_API_KEY', 'synthetic-azure')
    original = trial.read_response
    def read_response(client, url, headers, body):
        if 'api-key' in headers and not plumbing.attempts:
            plumbing.attempts.append(body)
            raise NoOutputReadTimeout('private timeout')
        return original(client, url, headers, body)
    monkeypatch.setattr(trial, 'read_response', read_response)
    guard = trial.configure_provider_budget('gpt-6-sol', max_calls=limit, max_estimated_usd='10')
    result = trial.run_task('Owned fixture task', model='gpt-6-sol', mac=object(), provider='azure',
                            endpoint='https://example.openai.azure.com/openai/v1', deployment='gpt-6-sol')
    assert guard.snapshot()['unknown'] == 1
    assert result['cost_micro_credits'] is None and result['provider_usage_unconfirmed'] is True
    assert result['provider_cost_usage_complete'] is False
    assert result['provider_calls'][0]['fallback_reason'] == 'azure_pre_output_timeout'
    if limit == 4:
        assert result['outcome'] == 'done' and guard.snapshot()['confirmed'] == 3
        assert result['provider_calls'][1]['provider'] == 'openai'
        assert result['provider_calls'][1]['fallback_reason'] == 'azure_pre_output_timeout'
        assert result['provider_budget_attempted_calls'] == 4
    else:
        assert result['outcome'] == 'failed' and len(plumbing.attempts) == 1
        assert result['provider_budget_denial'] == 'qa_provider_call_limit'


@pytest.mark.parametrize("error,kind", [(httpx.ConnectError("private-key private-content"), "connect"),
                                       (httpx.WriteTimeout("private-key private-content"), "timeout"),
                                       (httpx.ReadError("private-key private-content"), "transport")])
def test_provider_failure_diagnostics_are_content_free(plumbing, capsys, error, kind):
    plumbing.control["error"] = error
    result = plumbing.run()
    row = result["provider_calls"][0]
    assert row["kind"] == kind and row["elapsed_ms"] >= 0 and row["request_bytes"] > 0
    assert row["images"] == 0 and row["reasoning"] == "none"
    assert "private-key" not in capsys.readouterr().out and "private-content" not in str(row)


@pytest.mark.parametrize("status,invalid", [(400, False), (429, False), (503, False), (200, True)])
def test_provider_http_and_json_diagnostics_survive_missing_azure_fallback_key(plumbing, monkeypatch, capsys, status, invalid):
    monkeypatch.delenv("OPENAI_DEVICE_VISION_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("AZURE_OPENAI_CUAI_API_KEY", "private-azure-key")
    class Client:
        def __init__(self, **kwargs):
            pass
        def post(self, *args, **kwargs):
            def value():
                if invalid:
                    raise ValueError("private-response")
                return {"error": {"message": "private-response", "code": "content_filter"}}
            return SimpleNamespace(status_code=status, content=b"private-response", json=value)
        def close(self):
            pass
    monkeypatch.setattr(trial.httpx, "Client", Client)
    result = trial.run_task("Owned fixture task", model="gpt-6-sol", mac=object(), provider="azure",
                            endpoint="https://example.openai.azure.com/openai/v1", deployment="gpt-6-sol")
    assert result["provider_calls"][0]["http_status"] == status
    assert result["provider_calls"][0]["kind"] == ("invalid_json" if invalid else "http")
    output = capsys.readouterr().out
    assert "private-response" not in output and "private-azure-key" not in output
    assert ("openai_fallback_key_missing" in output) is (not invalid)


@pytest.mark.parametrize("output", [False, True])
def test_openai_400_records_machine_fields_and_output_state_without_replay(plumbing, monkeypatch, capsys, output):
    calls = []
    def read_response(client, url, headers, body):
        calls.append(body)
        value = {"error": {"code": "invalid_request_error", "message": "PRIVATE ERROR BODY"}}
        if output:
            value["usage"] = {"input_tokens": 10}
        return 400, __import__('json').dumps(value).encode()
    monkeypatch.setattr(trial, "read_response", read_response)
    guard = trial.configure_provider_budget("gpt-6-sol", max_calls=3, max_estimated_usd="10")
    result = plumbing.run()
    assert result["outcome"] == "failed" and len(calls) == 1
    row = result["provider_calls"][0]
    assert row["http_status"] == 400 and row["error_code"] == "invalid_request_error"
    assert row["response_output_present"] is output
    assert guard.snapshot()["confirmed"] == 0 and guard.snapshot()["attempted_calls"] == 1
    assert "PRIVATE ERROR BODY" not in str(result["provider_calls"]) + capsys.readouterr().out


def test_azure_filter_diagnostics_and_fallback_receipts_are_content_free(plumbing, monkeypatch, capsys):
    monkeypatch.setenv('AZURE_OPENAI_CUAI_API_KEY', 'private-azure-key')
    original = trial.httpx.Client
    calls = []
    def client(**kwargs):
        instance = original(**kwargs)
        send = instance.post
        def post(url, **kwargs):
            calls.append(url)
            if len(calls) == 1:
                error = {'error': {'code': 'content_filter', 'message': 'private-response', 'innererror': {
                    'code': 'ResponsibleAIPolicyViolation', 'content_filter_result': {
                        'jailbreak': {'filtered': True, 'detected': True, 'severity': 'private-response'},
                        'private-response': {'filtered': True}}}}}
                return SimpleNamespace(status_code=400, content=b'private-response', json=lambda: error)
            return send(url, **kwargs)
        instance.post = post
        return instance
    monkeypatch.setattr(trial.httpx, 'Client', client)
    result = trial.run_task('Owned fixture task', model='gpt-6-sol', mac=object(), provider='azure',
                            endpoint='https://example.openai.azure.com/openai/v1', deployment='gpt-6-sol')
    assert result['outcome'] == 'done' and len(calls) == 4
    assert result['providers'] == ['openai', 'azure', 'azure']
    first, fallback = result['provider_calls'][:2]
    assert first['error_code'] == 'content_filter' and first['innererror_code'] == 'ResponsibleAIPolicyViolation'
    assert first['content_filter_result'] == {'jailbreak': {'filtered': True, 'detected': True}}
    assert first['fallback_reason'] == fallback['fallback_reason'] == 'azure_content_filter'
    output = capsys.readouterr().out
    assert 'provider_call_failed' in output and 'ResponsibleAIPolicyViolation' in output
    for secret in ('private-response', 'private-azure-key', 'test-placeholder'):
        assert secret not in output and secret not in str(result['provider_calls'])


def test_trial_azure_all_phases_and_fallback_are_metered_once(plumbing, monkeypatch):
    monkeypatch.setenv("AZURE_OPENAI_CUAI_API_KEY", "synthetic-azure")
    guard = trial.configure_provider_budget("gpt-6-sol", max_calls=3, max_estimated_usd="10")
    result = trial.run_task("Owned fixture task", model="gpt-6-sol", mac=object(), provider="azure",
                            endpoint="https://example.openai.azure.com/openai/v1", deployment="gpt-6-sol")
    assert result["outcome"] == "done" and result["providers"] == ["azure"] * 3
    assert guard.snapshot()["confirmed"] == 3
    assert all("service_tier" not in body for body in plumbing.attempts)
    assert all(row["images"] == 0 and row["reasoning"] == "none"
               and row["service_tier"] == row["truncation"] == "omitted_or_other"
               and row["request_bytes"] == len(httpx.Request("POST", "https://example.test", json=body).content)
               for row, body in zip(result["provider_calls"], plumbing.attempts))


def test_complete_reply_without_usage_remains_held_not_free(plumbing):
    guard = trial.configure_provider_budget("gpt-6-sol", max_calls=3, max_estimated_usd="10")
    plumbing.control["usage_valid"] = False
    result = plumbing.run()
    assert len(plumbing.attempts) == 3 and guard.snapshot()["unknown"] == 3
    assert result["provider_cost_usage_complete"] is False
    assert result["cost_micro_credits"] is None  # Unknown usage cannot be presented as a zero cost.
    assert result["confirmed_usage_cost_micro_credits"] == 0
    assert float(result["provider_budget"]["committed_estimated_usd"]) > 0


def test_priority_is_refused_before_provider_and_does_not_return_done(plumbing, monkeypatch):
    trial.configure_provider_budget("gpt-6-sol", max_calls=3, max_estimated_usd="10")
    monkeypatch.setenv("CU_TRIAL_PRIORITY", "1")
    result = plumbing.run()
    assert result["outcome"] == "failed" and plumbing.attempts == []
    assert result["provider_budget_denial"] == "qa_provider_budget_request_unsupported"


@pytest.mark.parametrize("invalid", ["wrong_model", "invalid_total"])
def test_unconfirmed_provider_counts_are_not_reported_as_confirmed_cost(plumbing, invalid):
    guard = trial.configure_provider_budget("gpt-6-sol", max_calls=3, max_estimated_usd="10")
    if invalid == "wrong_model":
        plumbing.control["response_model"] = "gpt-5.6-luna"
    else:
        plumbing.control["invalid_total"] = True
    result = plumbing.run()
    assert guard.snapshot()["unknown"] == result["provider_budget_attempted_calls"] == 3
    assert result["provider_budget_confirmed_calls"] == result["confirmed_usage_cost_micro_credits"] == 0
    assert result["cost_micro_credits"] is None and result["provider_cost_usage_complete"] is False
    assert result["input_tokens"] == result["output_tokens"] == 0
    assert float(guard.snapshot()["committed_estimated_usd"]) > 0


def test_interruption_keeps_reservation_and_closes_client(plumbing):
    guard = trial.configure_provider_budget("gpt-6-sol", max_calls=3, max_estimated_usd="10")
    plumbing.control["error"] = KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        plumbing.run()
    assert guard.snapshot()["unknown"] == 1 and guard.snapshot()["confirmed"] == 0
    assert all(client.closed for client in plumbing.clients)


def test_invalid_guard_is_refused_before_http_client_creation(plumbing, monkeypatch):
    monkeypatch.setattr(trial, "QA_PROVIDER_BUDGET", object())
    with pytest.raises(ValueError, match="qa_provider_budget_invalid"):
        plumbing.run()
    assert plumbing.clients == [] and plumbing.attempts == []


def test_usage_bound_breach_refuses_response_before_model_action(plumbing):
    guard = trial.configure_provider_budget("gpt-6-sol", max_calls=3, max_estimated_usd="10")
    plumbing.control["usage_exceeds"] = True
    result = plumbing.run()
    assert result["outcome"] == "failed" and len(plumbing.attempts) == 1
    assert result["provider_budget_denial"] == "qa_provider_budget_usage_exceeded_bound"
    assert guard.snapshot()["usage_bound_breached"] is True
    assert result["provider_cost_usage_complete"] is False


def test_three_real_cli_phase_functions_share_one_allowance(plumbing, monkeypatch, tmp_path):
    import cu_browser_suite as baseline
    import cu_browser_heldout_suite as heldout
    import cu_acceptance
    monkeypatch.setattr(cu_acceptance, "source_digest", lambda **_: "source")
    first = SimpleNamespace(id="owned", cat="controlled")
    public = SimpleNamespace(id="public", cat="live")
    varied = SimpleNamespace(id="varied", cat="controlled", refuse=False)
    monkeypatch.setattr(baseline, "TASKS", [first])
    monkeypatch.setattr(baseline, "LIVE", [public])
    monkeypatch.setattr(heldout, "TASKS", [varied])

    def row(task, *_, **__):
        result = plumbing.run()
        return {"passed": result["outcome"] == "done", "task": task.id, "outcome": result["outcome"],
                "reason": result["reason"], "steps": result["steps"], "seconds": 0, "step_ms": [], "parts": [],
                "repetition": 1, "provider_budget": result["provider_budget"]}

    monkeypatch.setattr(baseline, "run_one", row)
    monkeypatch.setattr(heldout, "run_one", row)
    monkeypatch.setattr(sys, "argv", ["baseline", "--model", "gpt-6-sol", "--provider-budget-usd", "10",
                                    "--provider-budget-calls", "9", "--out", str(tmp_path / "controlled.jsonl")])
    assert baseline.main() == 0
    guard = trial.QA_PROVIDER_BUDGET
    monkeypatch.setattr(sys, "argv", ["public", "--model", "gpt-6-sol", "--cat", "live", "--live",
                                    "--out", str(tmp_path / "public.jsonl")])
    assert baseline.main() == 0
    monkeypatch.setattr(sys, "argv", ["heldout", "--model", "gpt-6-sol", "--runs", "1", "--out", str(tmp_path / "heldout.jsonl")])
    assert heldout.main() == 1  # The minimal test rows correctly fail the unchanged strict acceptance gate.
    assert trial.QA_PROVIDER_BUDGET is guard and guard.snapshot()["attempted_calls"] == 9
    assert len(plumbing.attempts) == 9
    monkeypatch.setattr(sys, "argv", ["reset", "--model", "gpt-6-sol", "--provider-budget-usd", "10",
                                    "--out", str(tmp_path / "reset.jsonl")])
    with pytest.raises(SystemExit) as error:
        baseline.main()
    assert error.value.code == 2 and len(plumbing.attempts) == 9


def test_budget_helper_bytes_are_in_browser_source_fingerprint(tmp_path):
    from cu_acceptance import source_digest
    folder = tmp_path / "scripts" / "desktop"
    folder.mkdir(parents=True)
    helper = folder / "cu_provider_budget.py"
    helper.write_text("first\n")
    first = source_digest(tmp_path, scope="browser")
    helper.write_text("second\n")
    assert source_digest(tmp_path, scope="browser") != first
