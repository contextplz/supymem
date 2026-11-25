"""An explicit source capability enables bounded reads with the existing v1 profile."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading

import pytest

from coding_agents.cu_ai_loop import Action
from coding_agents.jev_decision import Candidate
from coding_agents.owned_browser import OwnedBrowser, cuai_document_target


@contextmanager
def source_browser(tmp_path, *, bounded_reads=False, read_only=False):
    admissions, visits = [], []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*_):
            pass
        def do_GET(self):
            visits.append(self.path)
            body = (f'<h1>Source {self.path}</h1><p>Seats: 23</p>'
                '<a href="/two">Second source</a>'
                f'<a href="http://localhost:{self.server.server_port}/never">Other reviewed origin</a>'
                '<p hidden>HIDDEN_REFERENCE</p>'
                '<form><p>PRIVATE_FORM_REFERENCE</p><input value="PRIVATE_INPUT"></form>'
                + '<p>Ordinary readable filler</p>'*80).encode()
            self.send_response(200)
            self.send_header('Content-Type','text/html')
            self.send_header('Content-Length',str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    server = ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread = threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    root = f'http://127.0.0.1:{server.server_port}'
    browser = OwnedBrowser(order_id='dwo_founder_reads',goal='Compare source seats',
        origins=[root,f'http://localhost:{server.server_port}'],authorize=lambda *_:True,
        admit_effect=lambda effect,digest:admissions.append(effect) or True,
        general_mode=True,cuai_mode=True,bounded_reads=bounded_reads,read_only=read_only,
        cuai_version='browser_cuai_text_v1',profile_dir=tmp_path/'owned-profile',allow_loopback_for_tests=True)
    try:
        browser.start(root+'/one')
        yield browser,admissions,visits
    finally:
        browser.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


@pytest.mark.parametrize('read_only',[False,True])
def test_v1_explicit_bounded_reads_runs_observed_navigation_extract_and_scroll(tmp_path,read_only):
    with source_browser(tmp_path,bounded_reads=True,read_only=read_only) as (browser,admissions,visits):
        observation = browser.observe()
        links = browser.cuai_navigation_targets(observation)
        assert len(links)==1 and links[0]['href'].endswith('/two')
        foreign = next(control.id for control in observation.controls if control.label=='Other reviewed origin')
        with pytest.raises(PermissionError):
            browser.click(Candidate('navigate',foreign,observation.digest()),'bai_foreign_a0',
                navigate_url=browser._control_meta[foreign]['href'])
        browser.click(Candidate('navigate',links[0]['id'],observation.digest()),'bai_navigate_a0',navigate_url=links[0]['href'])
        observation = browser.observe()
        assert observation.location.endswith('/two') and browser.verified_load(observation)
        for invocation,action in [('bai_extract_a0',Action('extract',text='Seats')),
                ('bai_scroll_a0',Action('scroll',direction='down',amount=3))]:
            target = cuai_document_target(browser.last_load_evidence,action)
            browser.document_action(Candidate(action.op,target,observation.digest()),invocation,action)
            observation = browser.observe()
        readback = json.loads(browser.cuai_readback(observation))
        assert readback['schema_version']==2 and readback['current']['source']['reads'][0]['passages']==['Seats: 23']
        assert browser._page.evaluate('scrollY')>0 and len(browser.cuai_readback(observation).encode())<=5000
        assert all(value not in json.dumps(readback) for value in ('HIDDEN_REFERENCE','PRIVATE_INPUT'))
        assert [effect['operation'] for effect in admissions]==['navigate','extract','scroll']
        assert visits==['/one','/two']
        assert all(not browser._read_op_allowed(op) for op in ('new_tab','switch_tab','hover','download'))


def test_v1_default_retains_schema1_and_refuses_read_dispatch_before_admission(tmp_path):
    with source_browser(tmp_path) as (browser,admissions,visits):
        observation = browser.observe()
        assert json.loads(browser.cuai_readback(observation))['schema_version']==1
        with pytest.raises(PermissionError):
            browser.cuai_navigation_targets(observation)
        target = next(control.id for control in observation.controls if control.label=='Second source')
        with pytest.raises(PermissionError):
            browser.click(Candidate('navigate',target,observation.digest()),'bai_unavailable_a0',
                navigate_url=browser._control_meta[target]['href'])
        for action in (Action('extract',text='Seats'),Action('scroll',direction='down',amount=1)):
            with pytest.raises(PermissionError):
                browser.document_action(Candidate(action.op,cuai_document_target(browser.last_load_evidence,action),
                    observation.digest()),'bai_unavailable_a0',action)
        assert admissions==[] and visits==['/one']


@pytest.mark.parametrize('value',[None,0,1,'true'])
def test_bounded_reads_rejects_nonliteral_boolean_before_browser_start(tmp_path,value):
    with pytest.raises(PermissionError,match='browser_cuai_scope_invalid'):
        OwnedBrowser(order_id='dwo_invalid_reads',goal='Read',origins=['https://owned.example'],
            authorize=lambda *_:True,admit_effect=lambda *_:pytest.fail('No effect'),general_mode=True,
            cuai_mode=True,bounded_reads=value,profile_dir=tmp_path/'unused')
    assert not (tmp_path/'unused').exists()


def test_bounded_reads_requires_cuai_mode_before_browser_start(tmp_path):
    with pytest.raises(PermissionError,match='browser_cuai_scope_invalid'):
        OwnedBrowser(order_id='dwo_invalid_reads',goal='Read',origins=['https://owned.example'],
            authorize=lambda *_:True,admit_effect=lambda *_:pytest.fail('No effect'),bounded_reads=True,
            profile_dir=tmp_path/'unused')
    assert not (tmp_path/'unused').exists()
