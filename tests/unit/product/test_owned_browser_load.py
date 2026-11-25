"""Actual Chrome main-document load proof; no providers or native GUI."""
from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import pytest

from coding_agents.owned_browser import OwnedBrowser


@pytest.fixture
def site():
    state = {'same_status': 200, 'root_status': 200, 'root_blank': False, 'visits': []}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass
        def do_GET(self):
            path = urlsplit(self.path).path
            state['visits'].append(path)
            status = state['root_status'] if path == '/' else state['same_status'] if path == '/same' else {'/error': 404, '/server-error': 500, '/empty-status': 204}.get(path, 200)
            if path == '/redirect':
                self.send_response(302)
                self.send_header('Location', '/good')
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            body = (b'' if path in {'/blank', '/empty-status'} or path == '/' and state['root_blank'] else
                    b'<span hidden>Only hidden content</span>' if path == '/hidden' else
                    b'<h1>Visible source page</h1><a href="/good">Continue</a>')
            if path == '/popup':
                body = b'<h1>Start page</h1><a href="/good" target="_blank">Continue</a>'
            if path == '/opacity':
                body = b'<body style="opacity:0"><h1>Invisible text</h1></body>'
            if path == '/body-hidden':
                body = b'<body style="display:none"><h1>Invisible text</h1></body>'
            if path == '/clipped':
                body = b'<div style="height:0;overflow:hidden"><h1>Entirely clipped text</h1></div>'
            if path == '/visible-scroll':
                body = b'<div style="height:100px;overflow:auto"><h1>Visible text line</h1></div>'
            if path == '/transparent':
                body = b'<body style="color:rgba(0,0,0,0)"><h1>Transparent text</h1></body>'
            if path == '/transparent-fill':
                body = b'<h1 style="-webkit-text-fill-color:transparent">Transparent text</h1>'
            if path == '/error-frame':
                status, body = 500, b'<h1>Visible error</h1><iframe src="/good"></iframe>'
            if path == '/slow':
                self.send_response(200)
                self.send_header('Content-Type', 'text/html')
                self.send_header('Content-Length', '100000')
                self.end_headers()
                self.wfile.write(b'<h1>Partial visible content</h1>')
                self.wfile.flush()
                return  # Deliberately incomplete source response.
            self.send_response(status)
            self.send_header('Content-Type', 'text/html')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f'http://127.0.0.1:{server.server_address[1]}'
    yield base, state
    server.shutdown()
    server.server_close()


@pytest.fixture
def browser(site, tmp_path):
    pytest.importorskip('playwright.sync_api')
    value = OwnedBrowser(order_id='dwo_load_qa', goal='Confirm the reviewed page visibly loads',
        origins=[site[0]], authorize=lambda *_: True, admit_effect=lambda *_: True,
        profile_dir=tmp_path / 'profile', allow_loopback_for_tests=True)
    yield value
    value.close()


@pytest.mark.parametrize('path,expected', [('/good', True), ('/error', False),
    ('/server-error', False), ('/blank', False), ('/hidden', False),
    ('/error-frame', False), ('/opacity', False), ('/body-hidden', False),
    ('/clipped', False), ('/visible-scroll', True), ('/transparent', False), ('/transparent-fill', False)])
def test_actual_current_main_document_status_and_visible_content(browser, site, path, expected):
    browser.start(site[0] + path)
    browser._page.wait_for_load_state('load')
    observation = browser.observe()
    assert browser.verified_load(observation) is expected
    assert browser.last_load_evidence['response']['status'] == (500 if path in {'/server-error', '/error-frame'} else 404 if path == '/error' else 200)


def test_ordinary_same_origin_redirect_is_refused_before_destination(browser, site):
    with pytest.raises(RuntimeError, match='^owned_browser_navigation_network$'):
        browser.start(site[0] + '/redirect')
    assert site[1]['visits'] == ['/redirect']
    assert browser.last_read_refusal == 'browser_read_redirect_unavailable'
    assert browser._uncertain is True
    assert browser.last_load_evidence is None
    assert browser._page is browser._context is browser._browser is browser._playwright is None


def test_prior_success_same_url_is_not_reused_after_http_error(browser, site):
    url = site[0] + '/same'
    browser.start(url)
    browser._page.wait_for_load_state('load')
    prior = browser.observe()
    assert browser.verified_load(prior) is True
    prior_sequence = browser.last_load_evidence['sequence']
    site[1]['same_status'] = 500
    browser._page.reload(wait_until='load')
    assert browser.verified_load(prior) is False
    current = browser.observe()
    assert browser.verified_load(current) is False
    assert browser.last_load_evidence['sequence'] > prior_sequence


def test_spa_location_change_cannot_reuse_original_http_success(browser, site):
    browser.start(site[0] + '/good')
    browser._page.wait_for_load_state('load')
    prior = browser.observe()
    assert browser.verified_load(prior) is True
    browser._page.evaluate("history.pushState({},'', '/other')")
    assert browser.verified_load(prior) is False
    current = browser.observe()
    assert browser.verified_load(current) is False


def test_source_document_nonce_and_omitted_receipt_fail_closed(browser, site):
    browser.start(site[0] + '/good')
    browser._page.wait_for_load_state('load')
    current = browser.observe()
    assert browser.verified_load(current) is True
    key = browser._load_document_key
    assert browser._page.evaluate('key => {try {document[key]="fake"}catch(e){};return document[key]}', key) == browser.last_load_evidence['document_id']
    browser._load_pages[browser._page]['response'] = None
    assert browser.verified_load(current) is False


def test_incomplete_main_document_cannot_qualify(browser, site):
    try:
        browser.start(site[0] + '/slow')
        observation = browser.observe()
        assert browser.verified_load(observation) is False
    except RuntimeError as error:
        assert str(error).startswith('owned_browser_navigation_')


def test_popup_initial_navigation_uses_actual_context_request_custody(browser, site):
    from coding_agents.jev_decision import Candidate
    browser.start(site[0] + '/popup')
    observation = browser.observe()
    control = next(c for c in observation.controls if c.label == 'Continue')
    browser.click(Candidate('click', control.id, observation.digest()), 'din_popup')
    current = browser.observe()
    assert current.location == site[0] + '/good'
    assert browser.verified_load(current) is True


@pytest.mark.parametrize('status,blank,expected', [(200, False, True), (404, False, False), (200, True, False)])
def test_actual_worker_load_gate_without_test_only_load_wait(site, tmp_path, monkeypatch, status, blank, expected):
    from coding_agents.jev_decision import Candidate
    from desktop_runtime import device_worker
    site[1].update(root_status=status, root_blank=blank)

    class Gateway:
        order_id, venture_id = 'dwo_actual_load', 'ven_owned_qa'
        execution_id, claim_attempted = None, False
        decisions = 0
        def read(self):
            return {'state': 'approved', 'manifest': {'applications': ['com.google.Chrome'],
                'origins': [site[0]], 'actions': ['browser.read', 'browser.interact'],
                'duration_seconds': 30, 'instruction': 'Confirm the reviewed page visibly loads',
                'browser_verification': {'expected_locations': [site[0] + '/']}}}
        def claim(self):
            self.claim_attempted, self.execution_id = True, 'dex_ownedload'
        def current(self):
            return self.read()
        def decide(self, _invocation, observation):
            self.decisions += 1
            return Candidate('done', None, observation.digest(), True)
        def stop_receipt(self, _digest, stopped, *, task_verified=False):
            assert stopped is True and task_verified is expected
            return {'state': 'stopped'}

    actual_browser = device_worker.OwnedBrowser
    monkeypatch.setattr(device_worker, 'OwnedBrowser', lambda **kwargs:
        actual_browser(**kwargs, allow_loopback_for_tests=True, profile_dir=tmp_path / 'actual-profile'))
    gateway = Gateway()
    worker = device_worker.DeviceWorker(gateway, lambda _row: None)
    worker._notify_terminal = lambda *_args, **_kwargs: None
    worker.run()
    assert worker.last['task_verified'] is expected
    assert worker.last['evidence']['outcome'] == ('verified' if expected else 'outcome_review_required')
    assert gateway.decisions == (0 if expected else 1)
    assert len(worker.last['evidence']['verified_documents']) == (1 if expected else 0)


def test_route_tolerates_a_request_whose_frame_detached_but_not_other_errors():
    from types import SimpleNamespace
    browser = OwnedBrowser.__new__(OwnedBrowser)

    def route(error):
        def fail(*_args):
            raise RuntimeError(error)
        return SimpleNamespace(request=SimpleNamespace(method="POST", resource_type="document", url="https://x.test/"),
                               continue_=fail, abort=fail)

    browser._route(route("Route.abort: Frame for this navigation request is not available"))   # swallowed
    with pytest.raises(RuntimeError, match="Target closed"):
        browser._route(route("Target closed"))
