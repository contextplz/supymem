"""Provider-free real DOM reference identity and the admission interval."""

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from playwright.sync_api import Error as PlaywrightError

from coding_agents.jev_decision import Candidate
from coding_agents.owned_browser import OwnedBrowser, BrowserPreDispatchRefused
from coding_agents.computer_use_gate import EffectUncertain
from desktop_runtime import device_worker, cu_owned_browser_adapter
from tests.unit.product.test_cu_owned_browser_adapter import (
    Authority, Browser, Tracker, actor, gateway,
)


@pytest.fixture
def owned_page(tmp_path):
    hits = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            hits.append(self.path)
            body = (b'<h1>Owned reference page</h1>'
                    b'<a id="first" href="/wanted">Continue</a>'
                    b'<a id="second" href="/decoy">Continue</a>'
                    b'<label>Name<input name="name"></label>')
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f"http://127.0.0.1:{server.server_address[1]}"
    browser = OwnedBrowser(order_id="dwo_reference_boundary", goal="Read the owned source",
        origins=[origin], authorize=lambda *_: True, admit_effect=lambda *_: True,
        general_mode=True, cuai_mode=True, profile_dir=tmp_path / "profile",
        allow_loopback_for_tests=True, fill_values={"name": "Ada"})
    try:
        browser.start(origin + "/")
        yield browser, hits, origin
    finally:
        browser.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def first_candidate(browser):
    observation = browser.observe()
    target = next(identity for identity, (_, info) in browser._handles.items()
                  if info["href"].endswith("/wanted"))
    return Candidate("click", target, observation.digest())


def test_duplicate_labels_reorder_keeps_exact_original_dom_handle(owned_page):
    browser, hits, origin = owned_page
    candidate = first_candidate(browser)
    browser._page.locator("#first").evaluate("e => e.parentElement.append(e)")
    browser.click(candidate, "bai_reference_a0")
    observed = browser.observe()
    assert observed.location == origin + "/wanted"
    assert "/wanted" in hits and "/decoy" not in hits


@pytest.mark.parametrize("replacement", ["clone", "same_url_document"])
def test_lost_dom_binding_never_retargets_same_label_or_same_url(owned_page, replacement):
    browser, hits, _origin = owned_page
    candidate = first_candidate(browser)
    admissions = []
    browser.admit_effect = lambda *args: admissions.append(args) or True
    if replacement == "clone":
        browser._page.locator("#first").evaluate("e => e.replaceWith(e.cloneNode(true))")
    else:
        browser._page.reload(wait_until="load")
    # A detached same-document handle is rejected explicitly; replacement of
    # the whole JS execution context can instead raise Playwright's closed
    # transport error. Both must happen before effect admission.
    with pytest.raises((PermissionError, PlaywrightError)):
        browser.click(candidate, "bai_lost_reference_a0")
    assert admissions == []
    assert "/wanted" not in hits and "/decoy" not in hits


@pytest.mark.parametrize("change", ["href", "label", "clone", "document"])
def test_destination_changed_during_core_admission_is_not_delivered(owned_page, change):
    browser, hits, _origin = owned_page
    candidate = first_candidate(browser)
    admissions = []

    def admit(effect, digest):
        admissions.append((effect, digest))
        if change == 'document':
            browser._page.reload(wait_until='load')
        else:
            browser._page.locator("#first").evaluate({
                'href': "e => e.href='/decoy'",
                'label': "e => e.textContent='Changed action'",
                'clone': "e => e.replaceWith(e.cloneNode(true))",
            }[change])
        return True

    browser.admit_effect = admit
    refused = False
    try:
        browser.click(candidate, "bai_admission_changed_a0")
    except PermissionError:
        refused = True
    else:
        # Finish the existing dispatched navigation so the failed assertion
        # retains an actual client/server witness rather than timing a click.
        browser.observe()
    assert len(admissions) == 1
    assert admissions[0][0]["control"]["href"].endswith("/wanted")
    assert refused, {"requested_path": "/wanted", "received_paths": list(hits)}
    assert "/wanted" not in hits and "/decoy" not in hits


def test_nonempty_field_during_admission_preserves_changed_value_without_typing(owned_page):
    browser, _hits, _origin = owned_page
    observation = browser.observe()
    target = next(control.id for control in observation.controls if 'type' in control.operations)
    admissions = []

    def admit(effect, digest):
        admissions.append((effect, digest))
        browser._page.locator('input').evaluate("e=>e.value='Changed source value'")
        return True

    browser.admit_effect = admit
    with pytest.raises(BrowserPreDispatchRefused) as error:
        browser.type_text(Candidate('type', target, observation.digest(), value_index=0),
                          'bai_changed_field_a0', 'Ada')
    assert error.value.pre_dispatch_code == 'browser_pre_dispatch_target_changed'
    assert len(admissions) == 1 and browser._cuai_inputs == []
    assert browser._page.locator('input').input_value() == 'Changed source value'


def test_retired_authority_after_admission_never_starts_input(owned_page):
    browser, hits, _origin = owned_page
    candidate = first_candidate(browser)

    def admit(_effect, _digest):
        browser.authorize = lambda *_: False
        return True

    browser.admit_effect = admit
    with pytest.raises(BrowserPreDispatchRefused) as error:
        browser.click(candidate, 'bai_retired_a0')
    assert error.value.pre_dispatch_code == 'browser_pre_dispatch_scope_retired'
    assert browser._cuai_inputs == [] and '/wanted' not in hits and '/decoy' not in hits


@pytest.mark.parametrize('operation', ['click', 'type'])
@pytest.mark.parametrize('receipt', [True, False, 'unknown'])
def test_source_no_dispatch_receipt_stops_and_restores_only_confirmed_hold(
    monkeypatch, operation, receipt,
):
    authority = Authority([
        {'subgoals': ['Use the reviewed control']},
        actor(operation, element_id='c0', **({'text': 'Ada'} if operation == 'type' else {})),
    ])
    original = authority.request

    def request(method, path, body):
        if path.endswith('/effects/receipt'):
            authority.effects.append(('receipt', body))
            if receipt == 'unknown':
                raise OSError('QA receipt transport unavailable')
            return {'recorded': receipt}
        return original(method, path, body)

    authority.request = request
    browsers, adapters = [], []

    class RefusingBrowser(Browser):
        def _deliver(self, candidate, invocation, operation):
            effect = {'invocation': invocation, 'observation': candidate.observation_digest,
                      'operation': operation, 'control': {'tag': 'BUTTON', 'label': 'Continue',
                      'form': False, 'typed': operation == 'type'}}
            assert self.admit_effect(effect, 'e' * 64) is True
            raise BrowserPreDispatchRefused('browser_pre_dispatch_target_changed')

    class RecordingAdapter(cu_owned_browser_adapter.CuOwnedBrowserAdapter):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            adapters.append(self)

    def browser(**kwargs):
        value = RefusingBrowser(**kwargs)
        browsers.append(value)
        return value

    monkeypatch.setattr(device_worker, 'OwnedBrowser', browser)
    monkeypatch.setattr(device_worker, 'OwnedProcesses', Tracker)
    monkeypatch.setattr(device_worker, 'report_error', lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cu_owned_browser_adapter, 'CuOwnedBrowserAdapter', RecordingAdapter)
    worker = device_worker.DeviceWorker(gateway(authority), lambda _value: None)
    worker._notify_terminal = lambda *_args, **_kwargs: None
    worker.run()
    assert len(authority.broker.calls) == 2 and browsers[0].closed
    assert browsers[0].deliveries == [] and worker.last['task_verified'] is False
    assert [kind for kind, _ in authority.effects] == ['admit', 'receipt']
    saved = authority.effects[-1][1]
    assert saved['outcome'] == 'not_dispatched'
    assert saved['pre_dispatch_code'] == 'browser_pre_dispatch_target_changed'
    assert saved['invocation_id'].endswith('_a0')
    assert adapters[0].pending_dispatch is (receipt is not True)
    assert worker.last['evidence']['pending_dispatch'] is (receipt is not True)
    assert worker.current_effect is None if receipt is True else worker.current_effect is not None


def test_unknown_started_effect_never_becomes_no_dispatch_or_replay(monkeypatch):
    authority = Authority([{'subgoals': ['Use reviewed control']}, actor('click', element_id='c0')])
    browsers = []

    class UnknownBrowser(Browser):
        def _deliver(self, candidate, invocation, operation):
            effect = {'invocation': invocation, 'observation': candidate.observation_digest,
                      'operation': operation, 'control': {'tag': 'BUTTON', 'label': 'Continue',
                      'form': False}}
            assert self.admit_effect(effect, 'e' * 64) is True
            self.deliveries.append((operation, invocation))
            raise EffectUncertain('Unknown admitted delivery')

    def browser(**kwargs):
        value = UnknownBrowser(**kwargs)
        browsers.append(value)
        return value

    monkeypatch.setattr(device_worker, 'OwnedBrowser', browser)
    monkeypatch.setattr(device_worker, 'OwnedProcesses', Tracker)
    monkeypatch.setattr(device_worker, 'report_error', lambda *_args, **_kwargs: None)
    worker = device_worker.DeviceWorker(gateway(authority), lambda _value: None)
    worker._notify_terminal = lambda *_args, **_kwargs: None
    worker.run()
    assert len(authority.broker.calls) == 2 and browsers[0].closed
    assert len(browsers[0].deliveries) == 1
    assert authority.effects[-1][1]['outcome'] == 'uncertain'
    assert 'pre_dispatch_code' not in authority.effects[-1][1]
    assert worker.last['evidence']['pending_dispatch'] is True
    assert worker.current_effect is not None


def test_legacy_route_keeps_admitted_hold_without_fabricating_server_receipt():
    authority = Authority([])
    worker = device_worker.DeviceWorker(gateway(authority), lambda _value: None)
    worker.current_effect = ('din_legacy', 'e' * 64)
    worker._browser_input_refused(BrowserPreDispatchRefused(
        'browser_pre_dispatch_target_changed'), None, 'click')
    assert worker.current_effect == ('din_legacy', 'e' * 64)
    assert authority.effects == [] and authority.requests == []
