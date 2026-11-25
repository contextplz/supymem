"""Real isolated Chrome follows only exact reviewed GET URLs across two test origins."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading

import pytest

from coding_agents.cu_ai_loop import Action
from coding_agents.computer_use_gate import EffectUncertain
from coding_agents.jev_decision import Candidate
from coding_agents.owned_browser import OwnedBrowser, cuai_document_target


@contextmanager
def reviewed_browser(tmp_path, *, with_urls=True, sensitive=""):
    visits, effects = [], []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass
        def do_GET(self):
            visits.append((self.headers.get('Host'), self.path))
            if self.path == '/redirect':
                self.send_response(302)
                self.send_header('Location', '/unreviewed')
                self.end_headers()
                return
            body = ('<!doctype html><link rel=icon href="data:,"><h1>Public source</h1><p>License: Apache-2.0</p>'
                '<form method=get action=/search><input name=q><button>Search</button></form>' + sensitive).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    first = f'http://127.0.0.1:{server.server_port}'
    second = f'http://localhost:{server.server_port}'
    urls = [first + '/project/requests/', second + '/repo/requests', first + '/redirect']
    browser = OwnedBrowser(order_id='dwo_reviewed_urls', goal='Read the reviewed public sources', origins=[first, second],
        authorize=lambda *_: True, admit_effect=lambda effect, digest: effects.append(effect) or True,
        general_mode=True, cuai_mode=True, bounded_reads=True, read_only=True,
        read_urls=urls if with_urls else [], profile_dir=tmp_path/'owned-profile', allow_loopback_for_tests=True)
    try:
        browser.start(first + '/start')
        yield browser, urls, visits, effects
    finally:
        browser.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def navigate(browser, url, invocation):
    observation = browser.observe()
    action = Action(op='navigate', text=url)
    candidate = Candidate('navigate', cuai_document_target(browser.last_load_evidence, action), observation.digest())
    browser.document_action(candidate, invocation, action)
    return browser.observe()


def test_exact_reviewed_urls_bootstrap_and_cross_origin_without_controls_or_input(tmp_path):
    with reviewed_browser(tmp_path) as (browser, urls, visits, effects):
        observation = browser.observe()
        assert browser.verified_load(observation) and not observation.confirmation_pending
        assert browser.cuai_navigation_targets(observation) == []  # No fabricated DOM links.
        for index, url in enumerate([urls[0], urls[1], urls[0]]):
            observation = navigate(browser, url, f'bai_read_{index}_a0')
            assert browser.verified_load(observation) and observation.location == url
            assert not observation.confirmation_pending and browser.pause_kind is None
        assert [effect['read_url'] for effect in effects] == [urls[0], urls[1], urls[0]]
        assert all(effect['operation'] == 'navigate' for effect in effects)
        assert [path for _, path in visits] == ['/start', '/project/requests/', '/repo/requests', '/project/requests/']
        assert len(browser._context.pages) == 1
        for operation in ('click', 'type'):
            with pytest.raises(PermissionError):
                if operation == 'click':
                    browser.click(Candidate('click', 'c0', observation.digest()), 'bai_submit_a0')
                else:
                    browser.type_text(Candidate('type', 'c0', observation.digest()), 'bai_type_a0', 'requests')
        assert len(effects) == 3


@pytest.mark.parametrize('with_urls', [False, True])
def test_unreviewed_url_is_refused_before_dispatch_even_inside_reviewed_origin(tmp_path, with_urls):
    with reviewed_browser(tmp_path, with_urls=with_urls) as (browser, urls, visits, effects):
        with pytest.raises(PermissionError):
            navigate(browser, urls[0].replace('/project/requests/', '/unreviewed'), 'bai_bad_a0')
        if not with_urls:
            with pytest.raises(PermissionError):
                navigate(browser, urls[0], 'bai_unmarked_a0')
        assert effects == [] and len(visits) == 1


def test_reviewed_url_redirect_never_acquires_an_unreviewed_destination(tmp_path):
    with reviewed_browser(tmp_path) as (browser, urls, visits, effects):
        with pytest.raises(EffectUncertain):
            navigate(browser, urls[2], 'bai_redirect_a0')
        assert len(effects) == 1 and '/unreviewed' not in [path for _, path in visits]
        assert browser._uncertain


@pytest.mark.parametrize('sensitive,pause', [
    ('<input type=password name=password>', 'password'),
    ('<input name=code autocomplete=one-time-code>', 'otp_2fa'),
    ('<input name=cardnumber autocomplete=cc-number>', 'payment'),
    ('<label><input type=checkbox name=terms>Accept terms</label>', 'terms'),
    ('<p>Complete the CAPTCHA</p>', 'captcha'),
])
def test_public_submit_presence_does_not_suppress_sensitive_handoff(tmp_path, sensitive, pause):
    with reviewed_browser(tmp_path, sensitive=sensitive) as (browser, urls, visits, effects):
        observation = browser.observe()
        assert observation.confirmation_pending and browser.pause_kind == pause
        with pytest.raises(PermissionError):
            navigate(browser, urls[0], 'bai_sensitive_a0')
        assert effects == [] and len(visits) == 1
