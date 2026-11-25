"""Actual owned headless Chrome/local-fixture regressions, no model or desktop GUI."""
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import shutil

import pytest

from coding_agents.cu_ai_loop import Action, Caps
from desktop_runtime.cu_ai_browser import BrowserMac
from desktop_runtime.cu_browser import BrowserBackend, ChromeSession
from desktop_runtime.cu_browser.chrome import CHROME_BIN


@pytest.fixture
def owned_browser(tmp_path):
    binary = CHROME_BIN if Path(CHROME_BIN).exists() else shutil.which('google-chrome') or shutil.which('chromium')
    if binary is None:
        pytest.skip('owned headless Chrome unavailable')
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_GET(self):
            body = b'<title>Synthetic owned tab</title><input id="value"><p>Owned source</p>'
            self.send_response(200)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    chrome = ChromeSession(tmp_path / 'owned-profile', headless=True, chrome_bin=binary)
    backend = None
    try:
        chrome.start()
        backend = BrowserBackend(chrome.endpoint, download_dir=tmp_path / 'downloads', owned_context=True)
        yield backend, f'http://127.0.0.1:{server.server_address[1]}', chrome
    finally:
        try:
            if backend is not None:
                backend.close()
        finally:
            try:
                chrome.stop()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(2)
                assert not thread.is_alive()


def test_actual_dsl_new_switch_tabs_preserve_exact_input_recipient(owned_browser):
    b, url, _ = owned_browser
    mac = BrowserMac(b)
    mac.act(Action('navigate', text=url + '/one'), None)
    first = mac.observe()
    mac.act(Action('type', element_id='b0', text='first owned document'), first)
    # Tab operations require a fresh browser snapshot just like element input.
    first = mac.observe()
    mac.act(Action('new_tab', text=url + '/two'), first)
    assert len(b.context.pages) == 2
    with pytest.raises(RuntimeError, match='fresh_browser_observation_required'):
        mac.act(Action('type', element_id='b0', text='stale must not type'), first)
    second = mac.observe()
    mac.act(Action('type', element_id='b0', text='second owned document'), second)
    second = mac.observe()
    first_token = next(token for token, binding in b._tab_bindings.items() if binding[0].url == url + '/one')
    mac.act(Action('switch_tab', element_id=first_token), second)
    assert mac.observe().notes.startswith('PAGE URL: ' + url + '/one')
    assert b.page.locator('#value').input_value() == 'first owned document'
    other = next(page for page in b.context.pages if page is not b.page)
    assert other.locator('#value').input_value() == 'second owned document'
    assert len(b.completion_evidence()['actions']) >= 5


def test_actual_closed_token_never_switches_to_replacement_tab(owned_browser):
    b, url, _ = owned_browser
    assert b.act({'action': 'navigate', 'url': url + '/first'})['status'] == 'ok'
    b.observe()
    assert b.act({'action': 'new_tab', 'url': url + '/second'})['status'] == 'ok'
    b.observe()
    token, target = next((token, binding[0]) for token, binding in b._tab_bindings.items()
                         if binding[0] is not b.page)
    target.close()
    replacement = b.context.new_page()
    replacement.goto(url + '/replacement')
    active = b.page
    assert b.act({'action': 'switch_tab', 'tab_id': token})['status'] == 'error'
    assert b.page is active
    assert replacement.locator('#value').input_value() == ''


def test_actual_borrowed_cdp_attach_and_unsafe_urls_refuse_before_creation(owned_browser, tmp_path):
    b, url, chrome = owned_browser
    b.observe()
    initial_count = len(b.context.pages)
    for destination in ('file:///tmp/private', 'javascript:alert(1)', 'data:text/html,x', 'https://user:pass@example.invalid/'):
        assert b.act({'action': 'new_tab', 'url': destination})['status'] == 'refused'
        assert len(b.context.pages) == initial_count
    results, errors = [], []
    def probe_borrowed():
        borrowed = None
        try:
            borrowed = BrowserBackend(chrome.endpoint, download_dir=tmp_path / 'borrowed-downloads')
            assert all(row['id'] is None for row in borrowed.observe().tabs)
            for action in ({'action': 'new_tab', 'url': url}, {'action': 'switch_tab', 'tab': 0},
                           {'action': 'close_tab', 'tab': 0}):
                results.append(borrowed.act(action)['status'])
        except Exception as error:
            errors.append(type(error).__name__)
        finally:
            if borrowed is not None:
                borrowed.close()  # disconnect only; owned Chrome remains alive
    borrower = threading.Thread(target=probe_borrowed, daemon=True)
    borrower.start()
    borrower.join(20)
    assert not borrower.is_alive() and not errors
    assert results == ['refused', 'refused', 'refused']
    assert len(b.context.pages) == initial_count
    assert b.observe().title is not None


def test_actual_engine_deterministic_tab_operations_keep_independent_goal_verifier(owned_browser, monkeypatch):
    from desktop_runtime.cu_ai_engine import run_loop
    from desktop_runtime import agent_cursor
    b, url, _ = owned_browser
    mac = BrowserMac(b)
    monkeypatch.setattr(agent_cursor, 'emit', lambda *a, **kw: None)  # no visible cursor feed
    actions = iter([{'op': 'navigate', 'text': url + '/one'},
                    {'op': 'type', 'element_id': 'b0', 'text': 'engine first'},
                    {'op': 'new_tab', 'text': url + '/two'},
                    {'op': 'type', 'element_id': 'b0', 'text': 'engine second'}, 'switch',
                    {'op': 'done', 'summary': 'Two owned inputs independently checked'}])
    def decide(payload):
        action = next(actions)
        if action == 'switch':
            # The actual observation published these exact tokens; no live tab index.
            token = next(token for token, binding in b._tab_bindings.items() if binding[0].url == url + '/one')
            action = {'op': 'switch_tab', 'element_id': token}
        return {'actions': [action], 'subgoal_done': False, 'memory': ''}, {}
    def verify(*args):
        values = {page.url: page.locator('#value').input_value() for page in b.context.pages}
        return (values == {url + '/one': 'engine first', url + '/two': 'engine second'} and b.page.url == url + '/one', 'independent exact owned inputs')
    result = run_loop('Use two owned tabs and retain different input values', mac=mac, decide=decide,
                      stop=threading.Event(), verify=verify, caps=Caps(max_steps=10, max_seconds=30))
    assert result.outcome == 'done', result.reason
    assert verify()[0]
