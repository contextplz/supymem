"""Actual worker/Chrome and direct transport regression without provider calls."""
from __future__ import annotations

import importlib
import json
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def suite(monkeypatch):
    from tests.company_os_harness import load_company_os_app
    load_company_os_app()
    monkeypatch.syspath_prepend(str(ROOT / 'scripts/desktop'))
    module = importlib.import_module('cu_jev_suite')
    monkeypatch.setenv('TYPESAFE_API_KEY', 'owned-qa-test-not-a-provider-key')
    return module


@pytest.fixture
def reviewed_general_authority(suite, monkeypatch):
    """Explicit simulated opt-in; the legacy authority remains unadvertised."""
    original = suite.FakeAuthority

    class ReviewedGeneralAuthority(original):
        def request(self, method, path, body):
            result = super().request(method, path, body)
            general = self.manifest.get('general_browser_verification') or {}
            if (method == 'GET' and path.endswith('/capabilities')
                    and general.get('operation') == 'general_goal_v1'):
                return {**result, 'general_browser_execution_v1': True}
            return result

    monkeypatch.setattr(suite, 'FakeAuthority', ReviewedGeneralAuthority)


def answer(body, *, operation=None):
    choices = {}
    for name, question in body['questions'].items():
        criteria = question['criteria']
        if name == 'operation':
            selected = operation or ('click' if any(label in body['state']['goal']
                for label in body['questions'].get('click_target', {}).get('criteria', {}).values()
                if label != 'No suitable unambiguous target.') else 'done')
        elif name == 'click_target':
            selected = next((key for key, label in criteria.items()
                             if key != 'none' and label in body['state']['goal']), 'none')
        else:
            selected = 'none'
        choices[name] = {'type': 'choice', 'choice': selected,
            'probabilities': {key: 1.0 if key == selected else 0.0 for key in criteria}}
    return {'model': 'jev-1.13.0', 'answers': choices,
            'usage': {'input_tokens': 500, 'output_tokens': 100}}


def provider(suite, tmp_path, handler=None, *, max_calls=200):
    from cu_jev_provider_budget import SharedJevBudget
    calls = []

    def transport(request):
        calls.append(request)
        body = json.loads(request.content)
        assert request.url == suite.core_jev.ENDPOINT
        return handler(body) if handler else httpx.Response(200, json=answer(body))

    def factory(**kwargs):
        return httpx.Client(transport=httpx.MockTransport(transport), **kwargs)

    recorder = suite.PrivateCalls(tmp_path / 'private')
    budget = SharedJevBudget(max_calls=max_calls, max_estimated_usd='1')
    return suite.GuardedProvider(budget, recorder, client_factory=factory), calls


def test_actual_owned_worker_typed_url_navigation_and_cleanup(suite, tmp_path):
    pytest.importorskip('playwright.sync_api')
    p, calls = provider(suite, tmp_path)
    result = suite.run_case('navigation_3', 1, p, source={'source_sha256': 'test-source'})
    assert result['passed'] is True
    assert result['outcome'] == 'verified' and result['worker_task_verified'] is True
    assert result['decisions'] == result['effects'] == len(calls) == 2
    assert result['all_effects_returned'] is True
    assert result['posts'] == 0 and result['cleanup'] is True
    assert result['core_authority'] == 'owned_fake_not_production'
    assert all(item['usage_confirmed'] for item in p.receipts)


def test_general_done_stays_review_required_not_task_verified(suite, tmp_path, reviewed_general_authority):
    pytest.importorskip('playwright.sync_api')
    p, calls = provider(suite, tmp_path)
    result = suite.run_case('general_done_review', 1, p, source={'source_sha256': 'test-source'})
    assert result['passed'] is True
    assert result['outcome'] == 'outcome_review_required'
    assert result['worker_task_verified'] is False and len(calls) == 2


@pytest.mark.parametrize('case', ['login_creds', 'captcha_wall'])
def test_actual_human_gate_preserves_original_no_submit_oracle(suite, tmp_path, case, reviewed_general_authority):
    pytest.importorskip('playwright.sync_api')
    p, calls = provider(suite, tmp_path)
    result = suite.run_case(case, 1, p, source={'source_sha256': 'test-source'})
    assert result['passed'] is True
    assert result['worker_task_verified'] is False and result['effects'] == result['posts'] == 0
    assert result['outcome'] == 'cancelled_or_expired' and calls == []


@pytest.mark.parametrize('marker', [None, False, 'true', 1])
def test_unadvertised_general_refuses_before_claim_browser_and_provider(suite, tmp_path, monkeypatch, marker):
    p, calls = provider(suite, tmp_path)
    manifest = {'instruction': 'Read the reviewed page',
        'applications': ['com.google.Chrome'], 'origins': ['https://example.test'],
        'actions': ['browser.read', 'browser.interact'], 'duration_seconds': 60,
        'general_browser_verification': {'operation': 'general_goal_v1',
            'start_url': 'https://example.test', 'max_steps': 2, 'fill_values': {}}}
    authority = suite.FakeAuthority(manifest, p, 'unadvertised-general')
    requests, browser_calls = [], []

    def request(method, path, body):
        requests.append((method, path))
        result = authority.request(method, path, body)
        if method == 'GET' and path.endswith('/capabilities') and marker is not None:
            return {**result, 'general_browser_execution_v1': marker}
        return result

    def forbidden_browser(**kwargs):
        browser_calls.append(kwargs)
        raise AssertionError('Unadvertised general order opened a browser')

    monkeypatch.setattr(suite.worker_module, 'OwnedBrowser', forbidden_browser)
    monkeypatch.setattr(suite.worker_module, 'report_error', lambda *_args, **_kwargs: None)
    gateway = suite.WorkOrderGateway(venture_id='ven_qa', order_id=authority.order,
        device_id='dev_qa', request=request)
    worker = suite.worker_module.DeviceWorker(gateway, lambda _receipt: None)
    worker._notify_needs_you = lambda *_args, **_kwargs: None
    worker._notify_terminal = lambda *_args, **_kwargs: None
    worker.run()
    assert gateway.claim_attempted is False and authority.state == 'approved'
    assert worker.last['state'] == 'not_started' and worker.last['task_verified'] is False
    assert all(method == 'GET' for method, _path in requests)
    assert browser_calls == calls == [] and authority.effects == {}
    assert p.budget.snapshot()['attempted_calls'] == 0


def test_premature_done_cannot_pass_typed_url_oracle(suite, tmp_path):
    pytest.importorskip('playwright.sync_api')
    p, calls = provider(suite, tmp_path, lambda body: httpx.Response(200, json=answer(body, operation='done')))
    result = suite.run_case('navigation_3', 1, p, source={'source_sha256': 'test-source'})
    assert result['passed'] is False and result['independent_verification'] is False
    assert result['outcome'] == 'outcome_review_required' and result['worker_task_verified'] is False
    assert result['effects'] == 0 and len(calls) == 1 and result['cleanup'] is True


def test_actual_worker_failure_never_calls_installed_diagnostic_sink(suite, tmp_path, monkeypatch):
    pytest.importorskip('playwright.sync_api')
    installed_sink_calls = []
    def installed_sink(*args, **kwargs):
        installed_sink_calls.append((args, kwargs))
    monkeypatch.setattr(suite.worker_module, 'report_error', installed_sink)
    p, calls = provider(suite, tmp_path, lambda _body: httpx.Response(503, json={}))
    result = suite.run_case('navigation_2', 1, p, source={'source_sha256': 'test-source'})
    assert result['passed'] is False and result['worker_task_verified'] is False
    assert result['cleanup'] is True and result['effects'] == 0 and len(calls) == 1
    assert result['qa_diagnostic_error_count'] == 1 and installed_sink_calls == []
    assert suite.worker_module.report_error is installed_sink  # Restored even after failure.
    assert p.budget.snapshot()['unknown'] == 1 and p.budget.snapshot()['confirmed'] == 0


@pytest.mark.parametrize('failure', ['http_error', 'blank'])
def test_loaded_url_alone_does_not_pass_source_content_oracle(suite, tmp_path, monkeypatch, failure):
    pytest.importorskip('playwright.sync_api')
    original = suite.NavigationFixture

    def fixture(uid, pages):
        value = original(uid, pages)
        (value.failed_paths if failure == 'http_error' else value.blank_paths).add(value.paths[-1])
        return value

    monkeypatch.setattr(suite, 'NavigationFixture', fixture)
    p, calls = provider(suite, tmp_path)
    result = suite.run_case('navigation_2', 1, p, source={'source_sha256': 'test-source'})
    assert result['passed'] is False and result['independent_verification'] is False
    assert result['cleanup'] is True
    if failure == 'http_error':
        # The old actual 404 false-positive is sealed separately. Current
        # worker completion requires its fresh successful document witness.
        assert result['worker_task_verified'] is False and result['outcome'] == 'outcome_review_required'
    else:
        assert result['worker_task_verified'] is False


def test_started_fixture_is_closed_on_setup_refusal(suite, monkeypatch):
    fixtures = []
    original = suite.NavigationFixture

    def fixture(uid, pages):
        value = original(uid, pages)
        fixtures.append(value)
        return value

    class Refuse:
        def bind(self, *_args):
            raise ValueError('setup-refused')

    monkeypatch.setattr(suite, 'NavigationFixture', fixture)
    with pytest.raises(ValueError, match='setup-refused'):
        suite.run_case('navigation_2', 1, Refuse(), source={'source_sha256': 'test-source'})
    assert fixtures[0].httpd is None


def test_fixture_bound_socket_closes_when_thread_start_fails(suite, monkeypatch):
    fixtures = []
    original = suite.NavigationFixture

    def fixture(uid, pages):
        value = original(uid, pages)
        fixtures.append(value)
        return value

    monkeypatch.setattr(suite, 'NavigationFixture', fixture)
    monkeypatch.setattr(suite.worker_module.threading.Thread, 'start',
                        lambda _self: (_ for _ in ()).throw(RuntimeError('thread-start-refused')))
    with pytest.raises(RuntimeError, match='thread-start-refused'):
        suite.run_case('navigation_2', 1, None, source={'source_sha256': 'test-source'})
    assert fixtures[0].httpd is None


def bound_call(suite, tmp_path, handler, *, max_calls=200):
    p, calls = provider(suite, tmp_path, handler, max_calls=max_calls)
    fx = suite.NavigationFixture('abcd1234', 2).start()
    goal = 'Visit page'
    p.bind('trial', fx, goal)
    body = suite.jev_decision.request(suite.jev_decision.Observation(
        revision='rev', application='com.google.Chrome', location=fx.base + '/', goal=goal,
        context='Owned test page', controls=[], permitted=True,
        confirmation_pending=False, prior_effect_uncertain=False))
    return p, calls, fx, body


@pytest.mark.parametrize('status,value', [(503, {}), (200, {'model': 'other', 'usage': {'input_tokens': 1, 'output_tokens': 1}})])
def test_unknown_response_is_held_before_candidate_use(suite, tmp_path, status, value):
    p, calls, fx, body = bound_call(suite, tmp_path, lambda _: httpx.Response(status, json=value))
    try:
        with pytest.raises(ValueError):
            p.call(body, trial_id='trial')
        snap = p.budget.snapshot()
        assert snap['attempted_calls'] == snap['unknown'] == 1
        assert snap['confirmed'] == 0
        assert p.receipts[0]['usage_confirmed'] is False and len(calls) == 1
    finally:
        fx.close()


def test_reservation_prevents_second_http_attempt_and_no_retry(suite, tmp_path):
    p, calls, fx, body = bound_call(suite, tmp_path, lambda b: httpx.Response(200, json=answer(b)), max_calls=1)
    try:
        p.call(body, trial_id='trial')
        with pytest.raises(RuntimeError):
            p.call(body, trial_id='trial')
        assert len(calls) == 1 and p.budget.snapshot()['attempted_calls'] == 1
    finally:
        fx.close()


def test_foreign_scope_refuses_before_reservation_or_http(suite, tmp_path):
    p, calls, fx, body = bound_call(suite, tmp_path, lambda b: httpx.Response(200, json=answer(b)))
    try:
        body['state']['location'] = 'https://example.com/'
        with pytest.raises(ValueError, match='qa_capture_fixture_scope_changed'):
            p.call(body, trial_id='trial')
        assert calls == [] and p.budget.snapshot()['attempted_calls'] == 0
    finally:
        fx.close()


def test_private_payload_has_no_auth_and_manifest_hashes_exact_bytes(suite, tmp_path):
    p, calls, fx, body = bound_call(suite, tmp_path, lambda b: httpx.Response(200, json=answer(b)))
    try:
        p.call(body, trial_id='trial')
        value = json.loads((p.recorder.directory / p.recorder.calls[0]['file']).read_bytes())
        assert value['request'] == body and value['transport_complete'] is True
        assert 'Authorization' not in json.dumps(value)
        assert 'owned-qa-test-not-a-provider-key' not in json.dumps(value)
        raw = suite.base64.b64decode(value['response_bytes_base64'])
        assert value['response_sha256'] == suite.hashlib.sha256(raw).hexdigest()
        assert json.loads(raw)['model'] == 'jev-1.13.0'
        manifest_sha = p.recorder.seal()
        assert manifest_sha == suite.hashlib.sha256((p.recorder.directory / 'manifest.json').read_bytes()).hexdigest()
        assert (p.recorder.directory.stat().st_mode & 0o777) == 0o500
    finally:
        fx.close()


def test_private_existing_directory_and_symlink_file_fail_closed(suite, tmp_path):
    directory = tmp_path / 'existing'
    directory.mkdir()
    with pytest.raises(FileExistsError):
        suite.PrivateCalls(directory)
    p = suite.PrivateCalls(tmp_path / 'new')
    (p.directory / 'manifest.tmp').symlink_to(tmp_path / 'victim')
    with pytest.raises(FileExistsError):
        p.seal()
    assert not (tmp_path / 'victim').exists()


def test_live_refusal_precedes_key_capture_and_provider(suite, tmp_path):
    with pytest.raises(ValueError, match='qa_live_run_requires_explicit_approval'):
        suite.main(['--key-file', str(tmp_path / 'missing-key'), '--output', str(tmp_path / 'out'),
                    '--private-directory', str(tmp_path / 'private')])
    assert not (tmp_path / 'private').exists()


def test_second_live_admission_cannot_reset_process_allowance(suite, tmp_path, monkeypatch):
    monkeypatch.setattr(suite, 'LIVE_RUN_ADMITTED', True)
    with pytest.raises(ValueError, match='qa_live_run_already_admitted'):
        suite.main(['--approved-live-run', '--key-file', str(tmp_path / 'missing-key'),
            '--output', str(tmp_path / 'out'), '--private-directory', str(tmp_path / 'private')])
    assert not (tmp_path / 'private').exists()


def test_live_coordinator_stops_after_third_failed_row_and_seals_same_guard(suite, tmp_path, monkeypatch):
    guards = []
    invocations = []
    monkeypatch.setattr(suite, 'LIVE_RUN_ADMITTED', False)
    monkeypatch.setattr(suite, 'source_receipt', lambda: {'source_sha256': 'owned-test-source'})
    monkeypatch.setattr(suite, 'read_key', lambda _path: 'owned-fake-key-never-sent')
    monkeypatch.setenv('TYPESAFE_API_KEY', 'restore-owned-test-value')
    class NoHttpProvider:
        def __init__(self, budget, recorder):
            self.budget, self.recorder, self.receipts = budget, recorder, []
            guards.append(budget)
    monkeypatch.setattr(suite, 'GuardedProvider', NoHttpProvider)
    body = suite.jev_decision.request(suite.jev_decision.Observation(
        revision='owned-test', application='com.google.Chrome', location='https://owned.example.test/',
        goal='Owned QA no-network budget reservation', context='Bounded owned test', controls=[],
        permitted=True, confirmation_pending=False, prior_effect_uncertain=False))
    def fake_run(case, repetition, provider, *, source):
        invocations.append((case, repetition))
        provider.budget.hold(provider.budget.reserve(body))
        return {'id': case, 'repetition': repetition, 'passed': len(invocations) == 2}
    monkeypatch.setattr(suite, 'run_case', fake_run)
    output = tmp_path / 'rows.jsonl'
    argv = ['--approved-live-run', '--key-file', str(tmp_path / 'fake-key'),
        '--output', str(output), '--private-directory', str(tmp_path / 'private')]
    assert suite.main(argv) == 1
    final = json.loads(output.with_suffix('.jsonl.receipt.json').read_bytes())
    assert invocations == [('navigation_2', 1), ('navigation_2', 2),
                           ('navigation_3', 1), ('navigation_3', 2)]
    assert final['completed_rows'] == 4 and final['failed_rows'] == 3
    assert final['passed_rows'] == 1 and final['full_catalog_accepted'] is False
    assert final['stop_checkpoint']['code'] == 'qa_three_failed_rows'
    assert final['stop_checkpoint']['completed_rows'] == 4
    assert final['stop_checkpoint']['failed_rows'] == 3
    assert suite.datetime.fromisoformat(final['stop_checkpoint']['at_utc']).utcoffset().total_seconds() == 0
    assert final['provider_budget']['attempted_calls'] == final['provider_budget']['unknown'] == 4
    assert final['provider_budget'] == final['stop_checkpoint']['provider_budget'] == guards[0].snapshot()
    assert suite.hashlib.sha256((tmp_path / 'private/manifest.json').read_bytes()).hexdigest() == final['private_manifest_sha256']
    assert suite.os.environ['TYPESAFE_API_KEY'] == 'restore-owned-test-value'
    with pytest.raises(ValueError, match='qa_live_run_already_admitted'):
        suite.main(argv)
    assert len(guards) == 1  # Failed-run continuation cannot reset or reconfigure.


@pytest.mark.parametrize('raw', [b'not-json', b'\xff', b'x' * 65537])
def test_malformed_and_oversize_transport_never_release_hold(suite, tmp_path, raw):
    p, calls, fx, body = bound_call(suite, tmp_path, lambda _: httpx.Response(200, content=raw))
    try:
        with pytest.raises((ValueError, UnicodeDecodeError)):
            p.call(body, trial_id='trial')
        assert len(calls) == 1
        assert p.budget.snapshot()['unknown'] == 1
        assert p.budget.snapshot()['confirmed'] == 0
        payload = json.loads((p.recorder.directory / p.recorder.calls[0]['file']).read_bytes())
        captured = suite.base64.b64decode(payload['response_bytes_base64'])
        if len(raw) <= 65536:
            assert captured == raw
        else:
            assert len(captured) <= 65536 and payload['transport_complete'] is False
    finally:
        fx.close()


def test_cancelled_partial_stream_permanently_holds_attempt(suite, tmp_path):
    class CancelStream(httpx.SyncByteStream):
        def __iter__(self):
            yield b'{"partial":'
            raise KeyboardInterrupt

    p, calls, fx, body = bound_call(suite, tmp_path,
        lambda _: httpx.Response(200, stream=CancelStream()))
    try:
        with pytest.raises(KeyboardInterrupt):
            p.call(body, trial_id='trial')
        assert len(calls) == 1 and p.budget.snapshot()['unknown'] == 1
        payload = json.loads((p.recorder.directory / p.recorder.calls[0]['file']).read_bytes())
        assert suite.base64.b64decode(payload['response_bytes_base64']) == b'{"partial":'
        assert payload['transport_complete'] is False
    finally:
        fx.close()


def test_private_capture_loss_prevents_candidate_return(suite, tmp_path, monkeypatch):
    p, calls, fx, body = bound_call(suite, tmp_path, lambda b: httpx.Response(200, json=answer(b)))
    monkeypatch.setattr(p.recorder, 'write', lambda *_: (_ for _ in ()).throw(OSError('capture-failed')))
    try:
        with pytest.raises(OSError, match='capture-failed'):
            p.call(body, trial_id='trial')
        assert len(calls) == 1 and p.budget.snapshot()['confirmed'] == 1
        assert p.receipts == []  # Paid known usage does not imply returned action/QA capture.
    finally:
        fx.close()
