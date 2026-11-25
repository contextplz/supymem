"""Actual isolated Chrome canaries; redirects must never contact a foreign server."""
import base64
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import shutil
import threading

import pytest

from coding_agents.computer_use_gate import EffectUncertain
from coding_agents.jev_decision import Candidate
from coding_agents.owned_browser import OwnedBrowser

PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9ZQmcAAAAASUVORK5CYII=')


@contextmanager
def canary_browser(tmp_path, transport):
    foreign_hits, approved_hits, effects = [], [], []
    class Foreign(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass
        def do_GET(self):
            foreign_hits.append(self.path)
            self.send_response(200)
            self.send_header('Content-Length', '0')
            self.end_headers()
    foreign = ThreadingHTTPServer(('127.0.0.1', 0), Foreign)
    class Approved(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass
        def do_GET(self):
            approved_hits.append(self.path)
            if self.path.startswith('/redirect'):
                self.send_response(302)
                self.send_header('Location', f'http://127.0.0.1:{foreign.server_port}/canary')
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            if self.path == '/good.css':
                body, mime = b'body { color: rgb(1,2,3); }', 'text/css'
            elif self.path == '/good.js':
                body, mime = b'window.goodScriptLoaded=true;', 'text/javascript'
            elif self.path == '/good.png':
                body, mime = PNG, 'image/png'
            else:
                redirects = ('<link rel=stylesheet href=/redirect-css><script src=/redirect-script></script>'
                    '<img src=/redirect-image>') if self.path == '/resources' else ''
                body = ('<!doctype html><link rel=icon href="data:,"><link rel=stylesheet href=/good.css>'
                    '<script src=/good.js></script><img id=good src=/good.png>' + redirects
                    + '<h1>Approved source</h1><a href=/same>Same source</a><a href=/redirect-document>Redirect source</a>').encode()
                mime = 'text/html'
            self.send_response(200)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    approved = ThreadingHTTPServer(('127.0.0.1', 0), Approved)
    servers = [foreign, approved]
    threads = [threading.Thread(target=server.serve_forever, daemon=True) for server in servers]
    for thread in threads:
        thread.start()
    root = f'http://127.0.0.1:{approved.server_port}'
    factory = None
    if transport == 'factory':
        from desktop_runtime.cu_ai_browser import ChromeFactory
        factory = ChromeFactory(tmp_path/'owned-factory', account_id='usr_canary_fixture', profile_email='', headless=True)
    browser = OwnedBrowser(order_id='dwo_canary', goal='Read approved source', origins=[root], authorize=lambda *_: True,
        admit_effect=lambda effect, digest: effects.append(effect) or True, general_mode=True, cuai_mode=True,
        bounded_reads=True, read_only=True, chrome_factory=factory,
        profile_dir=None if factory else tmp_path/'owned-profile', allow_loopback_for_tests=True)
    try:
        if factory:
            from desktop_runtime import cu_browser
            from desktop_runtime.cu_browser.chrome import CHROME_BIN
            executable = next((path for path in (CHROME_BIN, shutil.which('google-chrome'), shutil.which('chromium'),
                shutil.which('chromium-browser')) if path and Path(path).is_file()), None)
            assert executable is not None, 'Actual installed Chrome required for factory transport'
            session_type = cu_browser.ChromeSession
            with pytest.MonkeyPatch.context() as patch:
                patch.setattr(cu_browser, 'ChromeSession', lambda profile, **kwargs:
                    session_type(profile, chrome_bin=executable, **kwargs))
                browser.start(root + '/start')
        else:
            browser.start(root + '/start')
        yield browser, root, foreign_hits, approved_hits, effects
    finally:
        try:
            browser.close()
        finally:
            for server in servers:
                server.shutdown()
                server.server_close()
            for thread in threads:
                thread.join(timeout=1)


@pytest.mark.parametrize('transport', ['persistent', 'factory'])
def test_offered_link_redirect_has_zero_foreign_hits_and_no_qualified_destination(tmp_path, transport):
    with canary_browser(tmp_path, transport) as (browser, root, foreign_hits, approved_hits, effects):
        observation = browser.observe()
        assert browser.verified_load(observation)
        link = next(link for link in browser.cuai_navigation_targets(observation) if link['href'] == root + '/redirect-document')
        try:
            browser.click(Candidate('navigate', link['id'], observation.digest()), 'bai_canary_a0', navigate_url=link['href'])
            browser._page.wait_for_timeout(250)
        except (PermissionError, EffectUncertain):
            pass
        assert '/redirect-document' in approved_hits and foreign_hits == [] and len(effects) == 1
        assert browser._uncertain and browser.last_read_refusal == 'browser_read_redirect_unavailable'
        assert browser.last_load_evidence is None or browser.last_load_evidence['qualified'] is False


@pytest.mark.parametrize('transport', ['persistent', 'factory'])
def test_subresource_redirects_have_zero_foreign_hits_while_normal_resources_work(tmp_path, transport):
    with canary_browser(tmp_path, transport) as (browser, root, foreign_hits, approved_hits, effects):
        browser._page.goto(root + '/resources', wait_until='load')
        browser._page.wait_for_function('window.goodScriptLoaded===true && document.querySelector("#good").naturalWidth===1')
        observation = browser.observe()
        assert browser.verified_load(observation) and not browser._uncertain
        assert all(path in approved_hits for path in ('/redirect-css', '/redirect-script', '/redirect-image'))
        assert all(path in approved_hits for path in ('/good.css', '/good.js', '/good.png'))
        assert browser._page.evaluate('getComputedStyle(document.body).color') == 'rgb(1, 2, 3)'
        assert foreign_hits == [] and effects == []


@pytest.mark.parametrize('transport', ['persistent', 'factory'])
def test_normal_same_origin_offered_link_still_has_a_true_current_load(tmp_path, transport):
    with canary_browser(tmp_path, transport) as (browser, root, foreign_hits, approved_hits, effects):
        observation = browser.observe()
        link = next(link for link in browser.cuai_navigation_targets(observation) if link['href'] == root + '/same')
        browser.click(Candidate('navigate', link['id'], observation.digest()), 'bai_same_a0', navigate_url=link['href'])
        browser._page.wait_for_load_state('load')
        observation = browser.observe()
        assert browser.verified_load(observation) and observation.location == root + '/same'
        assert foreign_hits == [] and '/same' in approved_hits and len(effects) == 1
