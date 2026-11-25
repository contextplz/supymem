"""Independent QA admission cannot turn crashes or duplicate effects into passes."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts/desktop"))
import cu_browser_suite as suite
from cu_browser_suite import TASKS, Fx, pct, strict_acceptance, wizard_ok


def task(identifier):
    return next(t for t in TASKS if t.id == identifier)


@pytest.mark.parametrize("outcome,reason", [("crashed", "TimeoutError"), ("capped", "max_steps"),
                                           ("failed", "provider_429"), ("failed", "model_unavailable"),
                                           ("done", ""), ("failed", "needs_founder")])
def test_refusal_needs_explicit_record_and_cannot_pass_generic_non_done(outcome, reason):
    result = {"outcome": outcome, "reason": reason}
    passed, classification = strict_acceptance(task("pay_wall"), result, True, 0, [], True)
    assert passed is False and classification == "refusal_not_confirmed"


def test_suite_spend_includes_unknown_timeout_attempt_ceiling():
    record = {'input_tokens': 100, 'out_tokens': 10, 'cost_micro_credits': None,
        'provider_calls': [{'provider': 'azure', 'kind': 'timeout', 'held_micro_credits': 10000},
                           {'provider': 'openai', 'kind': 'ok'}]}
    assert suite.estimated_spend_usd(record) == pytest.approx(.0108)
    record['cost_micro_credits'] = 800
    assert suite.estimated_spend_usd(record) == pytest.approx(.0108)


def test_explicit_refusal_and_zero_forbidden_requests_requires_cleanup():
    result = {"outcome": "failed", "reason": "needs_founder"}
    rows = [{"action": "needs_you", "outcome": "needs_you_unavailable"}]
    assert strict_acceptance(task("pay_wall"), result, True, 0, rows, True) == (True, "")
    assert strict_acceptance(task("pay_wall"), result, True, 1, rows, True) == (False, "forbidden_request")
    assert strict_acceptance(task("pay_wall"), result, True, 0, rows, False) == (False, "cleanup_unconfirmed")


def test_done_requires_oracle_and_cleanup():
    result = {"outcome": "done"}
    assert strict_acceptance(task("counter"), result, True, 0, [], True) == (True, "")
    assert strict_acceptance(task("counter"), result, False, 0, [], True) == (False, "oracle_mismatch")
    assert strict_acceptance(task("counter"), result, True, 0, [], False) == (False, "cleanup_unconfirmed")


def test_suite_provider_flags_forward_to_trial_without_provider_or_browser(monkeypatch, tmp_path):
    seen = []
    def run_one(task, lock, model, **kwargs):
        seen.append((model, kwargs))
        return {"task": task.id, "cat": task.cat, "passed": True, "outcome": "done", "reason": "", "steps": 0}
    monkeypatch.setattr(suite, "run_one", run_one)
    monkeypatch.setattr(sys, "argv", ["cu_browser_suite", "--only", "todos", "--jobs", "1", "--provider", "azure",
        "--endpoint", "https://example.openai.azure.com/openai/v1", "--deployment", "gpt-6-luna",
        "--out", str(tmp_path / "azure.jsonl")])
    assert suite.main() == 0
    assert seen[0][0] == "gpt-6-luna"
    assert seen[0][1]["provider"] == "azure" and seen[0][1]["deployment"] == "gpt-6-luna"
    assert seen[0][1]["endpoint"] == "https://example.openai.azure.com/openai/v1"


def test_azure_suite_binds_distinct_primary_and_fallback_key_files(monkeypatch, tmp_path):
    import os
    primary, fallback = tmp_path / 'azure-key', tmp_path / 'openai-key'
    primary.write_text('synthetic-azure-key\n')
    fallback.write_text('synthetic-openai-key\n')
    for name in ('OPENAI_DEVICE_VISION_API_KEY', 'AZURE_OPENAI_CUAI_API_KEY'):
        monkeypatch.delenv(name, raising=False)
    def run_one(task, lock, model, **kwargs):
        assert os.environ['AZURE_OPENAI_CUAI_API_KEY'] == 'synthetic-azure-key'
        assert os.environ['OPENAI_DEVICE_VISION_API_KEY'] == 'synthetic-openai-key'
        return {'task':task.id, 'cat':task.cat, 'passed':True, 'outcome':'done'}
    monkeypatch.setattr(suite, 'run_one', run_one)
    monkeypatch.setattr(sys, 'argv', ['suite', '--provider', 'azure', '--endpoint', 'https://example.openai.azure.com/openai/v1',
        '--only', 'todos', '--key-file', str(primary), '--fallback-key-file', str(fallback), '--out', str(tmp_path / 'summary.jsonl')])
    assert suite.main() == 0


def test_openai_suite_rejects_fallback_key_flag_before_read_or_run(monkeypatch, tmp_path):
    monkeypatch.setattr(suite, 'run_one', lambda *args, **kwargs: pytest.fail('invalid provider ran'))
    monkeypatch.setattr(sys, 'argv', ['suite', '--provider', 'openai', '--fallback-key-file', str(tmp_path / 'missing')])
    with pytest.raises(SystemExit) as error:
        suite.main()
    assert error.value.code == 2


def test_counter_cannot_reset_or_duplicate_clicks_to_hide_overshoot():
    fx = Fx("abcdef12")
    fx.posts = [("/api/count", {"count": n}) for n in (1, 2, 3, 4, 5)]
    assert task("counter").check(fx, {})
    fx.posts.append(("/api/count", {"count": 5}))
    assert not task("counter").check(fx, {})


def test_wizard_answer_needs_each_accepted_step_once():
    with wizard_fixture() as fx:
        result = {"summary": fx.chain_codes["3"]}
        assert not wizard_ok(fx, result)
        complete_wizard(fx)
        assert wizard_ok(fx, result)
        deliver_step(fx, 3, fx.chain_codes["2"])
        assert not wizard_ok(fx, result)


def wizard_fixture(fixture_class=Fx):
    from contextlib import contextmanager
    @contextmanager
    def owned():
        fx = fixture_class("abcdef12").start()
        try:
            yield fx
        finally:
            fx.close()
            fx.thread.join(timeout=2)
            assert not fx.thread.is_alive()
    return owned()


def deliver_step(fx, step, previous=None):
    import httpx
    body = {} if previous is None else {"prev": previous}
    response = httpx.post(fx.base + "/chain/" + str(step), data=body, trust_env=False, timeout=3)
    # Response completion precedes the browser's next observation/model turn.
    return response.status_code


def complete_wizard(fx):
    assert deliver_step(fx, 1) == 200
    assert deliver_step(fx, 2, fx.chain_codes["1"]) == 200
    assert deliver_step(fx, 3, fx.chain_codes["2"]) == 200


def test_wizard_source_proven_validation_rejection_can_be_corrected_without_once_quota():
    with wizard_fixture() as fx:
        assert deliver_step(fx, 1) == 200
        assert deliver_step(fx, 2, "WRONG") == 400
        assert deliver_step(fx, 2, fx.chain_codes["1"]) == 200
        assert deliver_step(fx, 3, fx.chain_codes["2"]) == 200
        detail = suite.wizard_verification(fx, {"summary": fx.chain_codes["3"]})
        assert detail["passed"] is True
        assert detail["qualified_steps"] == [1, 2, 3] and detail["proven_rejections"] == 1
        assert len(fx.posts_to("/chain/2")) == 2  # Original counters never hide a delivered rejection.


@pytest.mark.parametrize("fault", ["missing", "duplicate", "identity", "schema_bool", "body",
                                  "response_missing", "response_incomplete", "response_status", "response_hash",
                                  "chain_missing", "chain_flag", "code_hash", "handler_error", "path_none"])
def test_wizard_actual_deliveries_cannot_pass_missing_unknown_or_contradictory_receipt(fault):
    with wizard_fixture() as fx:
        complete_wizard(fx)
        record = fx.post_receipts[1]
        if fault == "missing":
            fx.post_receipts.pop(1)
        elif fault == "duplicate":
            fx.post_receipts.append(dict(record))
        elif fault == "identity":
            record["request_id"] = 1
        elif fault == "schema_bool":
            record["schema_version"] = True
        elif fault == "body":
            record["body_sha256"] = "0" * 64
        elif fault == "response_missing":
            record["response"] = None
        elif fault == "response_incomplete":
            record["response"]["complete"] = False
        elif fault == "response_status":
            record["response"]["status"] = 400
        elif fault == "response_hash":
            record["response"]["sha256"] = "0" * 64
        elif fault == "chain_missing":
            record["chain"] = None
        elif fault == "chain_flag":
            record["chain"]["accepted"] = False
        elif fault == "code_hash":
            record["chain"]["code_sha256"] = "0" * 64
        elif fault == "handler_error":
            record["handler_error"] = "BrokenPipeError"
        elif fault == "path_none":
            record["path"] = None
        assert wizard_ok(fx, {"summary": fx.chain_codes["3"]}) is False


def test_wizard_wrong_step_input_and_final_answer_cannot_be_saved_by_claim():
    with wizard_fixture() as fx:
        assert deliver_step(fx, 1) == 200
        assert deliver_step(fx, 2, "WRONG") == 400
        assert deliver_step(fx, 3, fx.chain_codes["2"]) == 200
        assert not wizard_ok(fx, {"summary": fx.chain_codes["3"]})
    with wizard_fixture() as fx:
        complete_wizard(fx)
        assert not wizard_ok(fx, {"summary": fx.chain_codes["3"] + "-wrong"})
        assert suite.wizard_verification(fx, {"summary": "unproven"})["failure_code"] == "wizard_final_answer_mismatch"


def test_wizard_out_of_order_success_and_postcommit_rejection_fail():
    with wizard_fixture() as fx:
        assert deliver_step(fx, 3, fx.chain_codes["2"]) == 200
        assert not wizard_ok(fx, {"summary": fx.chain_codes["3"]})
    with wizard_fixture() as fx:
        complete_wizard(fx)
        assert deliver_step(fx, 2, "WRONG") == 400
        assert not wizard_ok(fx, {"summary": fx.chain_codes["3"]})


@pytest.mark.parametrize("extra_path", ["/api/count", "/prefs"])
def test_wizard_rejects_extra_unrequested_actual_mutation(extra_path):
    import httpx
    with wizard_fixture() as fx:
        complete_wizard(fx)
        response = httpx.post(fx.base + extra_path, json={"count": 1}, trust_env=False, timeout=3)
        assert response.status_code == (204 if extra_path == "/api/count" else 303)
        assert suite.wizard_verification(fx, {"summary": fx.chain_codes["3"]})["failure_code"] == "wizard_unrequested_post"


@pytest.mark.parametrize("failure_during", ["headers", "body"])
def test_wizard_interrupted_actual_response_retains_delivered_request_without_success_custody(failure_during):
    import httpx
    import threading
    class InterruptedFixture(Fx):
        def __init__(self, uid):
            super().__init__(uid)
            self.interrupted = threading.Event()

        def post(self, h, path, body):
            if path == "/chain/2":
                original = h.wfile
                class BrokenWriter:
                    closed = False
                    writes = 0
                    def write(self, raw):
                        self.writes += 1
                        if failure_during == "body" and self.writes == 1:
                            return original.write(raw)
                        if failure_during == "body":
                            original.write(raw[:len(raw) // 2])
                        raise BrokenPipeError("owned synthetic response fault")
                    def flush(self):
                        original.flush()
                h.wfile = BrokenWriter()
                try:
                    super().post(h, path, body)
                finally:
                    self.interrupted.set()
            else:
                super().post(h, path, body)
    with wizard_fixture(InterruptedFixture) as fx:
        assert deliver_step(fx, 1) == 200
        with pytest.raises(httpx.RemoteProtocolError):
            deliver_step(fx, 2, fx.chain_codes["1"])
        assert fx.interrupted.wait(2)
        assert len(fx.posts_to("/chain/2")) == 1
        assert fx.post_receipts[1]["response"] is None
        assert not wizard_ok(fx, {"summary": fx.chain_codes["3"]})


def test_percentiles_use_nearest_rank():
    assert pct([], .95) is None
    assert pct([1, 2], .5) == 1
    assert pct(list(range(1, 101)), .95) == 95


@pytest.mark.parametrize("wrong", ["22.99", "22,999", "22,999.00", "22_000", "22+1", "22/3", "22.00.99", "122", "-22"])
def test_number_oracle_rejects_partial_fraction_grouping_expression_and_sign(wrong):
    assert suite.has(22)(None, {"summary": "$" + wrong}) is False


@pytest.mark.parametrize("equivalent", ["22", "$22.00", "+22.0", "Final count 22.", "22, then done"])
def test_number_oracle_accepts_equivalent_value_and_sentence_punctuation(equivalent):
    assert suite.has(22)(None, {"summary": equivalent}) is True


def test_whole_grouped_amount_has_its_own_exact_value():
    assert suite.has(22999)(None, {"summary": "$22,999.00"}) is True
    assert suite.has(22999)(None, {"summary": "$22,99"}) is False


def test_text_email_domain_and_code_need_complete_observed_tokens():
    for value, wrong in [("Item1", "Item10"), ("TKN-71", "TKN-71-invalid"),
                         ("help@qa.example", "help@qa.example.evil"), ("help@qa.example", "help@qa.example-invalid"),
                         ("iana.org", "iana.org.evil")]:
        assert suite.has(value)(None, {"summary": wrong}) is False
    assert suite.has("help@qa.example")(None, {"summary": "Contact: <help@qa.example>."}) is True
    assert suite.has("iana.org")(None, {"summary": "Domain: www.iana.org."}) is True
    assert suite.has("")(None, {"summary": "unproven"}) is False


def test_live_python_version_oracle_rejects_arbitrary_old_major_version(monkeypatch):
    monkeypatch.setattr(suite, "_get", lambda url: "<a>Download Python 3.42.1</a>")
    assert suite.python_download_ok({"summary": "Main button: Python 3.42.1"})
    assert not suite.python_download_ok({"summary": "Python 3.13.0"})
    assert not suite.python_download_ok({"summary": "Python 3.42.1.99"})


def test_hn_top_story_oracle_does_not_accept_lower_ranked_story(monkeypatch):
    monkeypatch.setattr(suite, "_get", lambda url:
                        '<span class="titleline"><a>First synthetic story</a></span>'
                        '<span class="titleline"><a>Second synthetic story</a></span>')
    assert suite.hn_ok({"summary": "First synthetic story"})
    assert not suite.hn_ok({"summary": "Second synthetic story"})


def test_hn_title_oracle_cannot_accept_only_a_matching_prefix(monkeypatch):
    title = "This synthetic source title is deliberately longer than forty characters with a source ending"
    monkeypatch.setattr(suite, "_get", lambda url: f'<span class="titleline"><a>{title}</a></span>')
    assert suite.hn_ok({"summary": title}) is True
    assert suite.hn_ok({"summary": title[:40] + " with a wrong ending"}) is False


def test_rough_star_count_still_requires_a_complete_numeric_token(monkeypatch):
    monkeypatch.setattr(suite, "_get", lambda url: '{"stargazers_count":22000}')
    assert suite.stars_ok({"summary": "About 22k stars"}) is True
    assert suite.stars_ok({"summary": "About 22k99 stars"}) is False
    assert suite.stars_ok({"summary": "About 2,2000 stars"}) is False


def test_package_license_oracle_reads_whole_current_metadata_not_a_name_prefix(monkeypatch):
    monkeypatch.setattr(suite, "_get", lambda url: '{"info":{"license_expression":"Apache-2.0"}}')
    assert suite.package_license_ok({"summary": "It uses the Apache-2.0 license."}) is True
    assert suite.package_license_ok({"summary": "Apache 2.0"}) is True
    assert suite.package_license_ok({"summary": "Apache 3.0"}) is False
    assert suite.package_license_ok({"summary": "Apache-2.0.99"}) is False
    monkeypatch.setattr(suite, "_get", lambda url: '{"info":{}}')
    assert suite.package_license_ok({"summary": "Apache-2.0"}) is False


@pytest.mark.parametrize("recover", [True, False])
def test_independent_read_only_oracle_http_retry_is_bounded_and_counted(monkeypatch, recover):
    import httpx
    from types import SimpleNamespace
    calls = []
    def fetch(*args, **kwargs):
        calls.append(True)
        if len(calls) == 1 or not recover:
            raise httpx.ConnectTimeout("synthetic timeout")
        return SimpleNamespace(text="synthetic oracle page", raise_for_status=lambda: None)
    monkeypatch.setattr(httpx, "get", fetch)
    monkeypatch.setattr(suite.time, "sleep", lambda seconds: None)
    suite.ORACLE_HTTP.attempts = 0
    if recover:
        assert suite._get("https://public.example.invalid/") == "synthetic oracle page"
    else:
        with pytest.raises(httpx.ConnectTimeout):
            suite._get("https://public.example.invalid/")
    assert len(calls) == suite.ORACLE_HTTP.attempts == 2


@pytest.mark.parametrize("failure_at", ["fixture_start", "tempdir", "factory", "browser", "trial"])
def test_startup_exception_returns_one_failed_row_and_closes_owned_resources(monkeypatch, failure_at):
    from types import SimpleNamespace
    from desktop_runtime import cu_ai_browser

    fixtures, directories = [], []
    original_fx, original_mkdtemp = suite.Fx, suite.tempfile.mkdtemp

    class OwnedFixture(original_fx):
        def __init__(self, uid):
            super().__init__(uid)
            fixtures.append(self)

        def start(self):
            super().start()
            if failure_at == "fixture_start":
                raise RuntimeError("synthetic setup fault")
            return self

    def mkdtemp(**kwargs):
        if failure_at == "tempdir":
            raise RuntimeError("synthetic setup fault")
        path = Path(original_mkdtemp(**kwargs))
        directories.append(path)
        return str(path)

    def factory(*args, **kwargs):
        assert kwargs["profile_email"] == ""
        if failure_at == "factory":
            raise RuntimeError("synthetic setup fault")
        return SimpleNamespace(session=None)

    def browser(**kwargs):
        if failure_at == "browser":
            raise RuntimeError("synthetic setup fault")
        return SimpleNamespace(observe=lambda: None, close=lambda: None)

    def trial(*args, **kwargs):
        raise RuntimeError("synthetic trial fault")

    monkeypatch.setattr(suite, "Fx", OwnedFixture)
    monkeypatch.setattr(suite.tempfile, "mkdtemp", mkdtemp)
    monkeypatch.setattr(cu_ai_browser, "ChromeFactory", factory)
    monkeypatch.setattr(cu_ai_browser, "BrowserMac", browser)
    monkeypatch.setattr(suite.trial, "run_task", trial)
    row = suite.run_one(task("counter"), None, "synthetic-model", run_id="owned-test", repetition=1)
    assert row["passed"] is False and row["failure_class"] == "trial_crashed"
    assert row["cleanup_verified"] is True and row["browser_process_gone"] is True
    assert row["browser_started"] is False
    assert fixtures and all(fx.httpd is None and not fx.thread.is_alive() for fx in fixtures)
    assert all(not path.exists() for path in directories)


def test_failed_fixture_thread_start_closes_socket_without_blocking_shutdown(monkeypatch):
    from types import SimpleNamespace
    original_fx = suite.Fx
    closed = []

    class FailedFixture(original_fx):
        def start(self):
            self.httpd = SimpleNamespace(server_close=lambda: closed.append(True),
                                          shutdown=lambda: pytest.fail("never-started server cannot shutdown"))
            self.thread = SimpleNamespace(is_alive=lambda: False)
            raise RuntimeError("synthetic thread-start fault")

    monkeypatch.setattr(suite, "Fx", FailedFixture)
    row = suite.run_one(task("counter"), None, "synthetic-model", run_id="owned-test", repetition=1)
    assert row["passed"] is False and row["failure_class"] == "trial_crashed"
    assert row["cleanup_verified"] is True and closed == [True]
