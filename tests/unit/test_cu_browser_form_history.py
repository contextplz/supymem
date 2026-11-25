"""Whole multi-step source fields remain inputs, never acceptance or model claims."""
import copy
import json
from types import SimpleNamespace

import pytest

from coding_agents.cu_ai_loop import Action, verify_text
from coding_agents.cu_verification_context import compose
from desktop_runtime.cu_ai_browser import BrowserMac
from desktop_runtime.cu_browser.dom import format_form_state


def form(value, label='Reviewed field', **omitted):
    return format_form_state({'v': 1, 'controls': [{'label': label, 'type': 'text', 'value': value}],
                              'omitted': {'controls': 0, 'labels': 0, 'values': 0, **omitted}})


class SourceBackend:
    def __init__(self):
        self.page = SimpleNamespace(url='https://owned.example/first')
        self.snapshot_id = 'a' * 32
        self.scope = 'b' * 32
        self.fields = form('FIRST-REVIEWED-VALUE')
        self.actions, self.responses = [], []
        self.status, self.no_effect, self.mismatch = 'ok', False, False
        self.form_scope = 'form:0:0'
        self.document_scope = 'main'

    def owned_tab_state(self):
        return (1, self.scope)

    def completion_snapshot_current(self, snapshot_id, url):
        return self.snapshot_id == snapshot_id and self.page.url == url

    def completion_evidence(self):
        return copy.deepcopy({'v': 1, 'source': 'owned_browser_backend',
                              'actions': self.actions, 'responses': self.responses})

    def form_state(self):
        return self.fields

    def observe(self):
        self.snapshot_id = f'{len(self.actions) + 1:032x}'
        return SimpleNamespace(url=self.page.url, title='Source result', text='Current owned result', tabs=[],
                               captcha=False, elements=[{'i': 0, 'tag': 'button', 'type': 'submit',
                                                        'form_scope': self.form_scope, 'document_scope': self.document_scope, 'submits_form': True,
                                                        'label': 'Save'}])

    def act(self, payload):
        seq = len(self.actions) + 1
        receipt = {'seq': seq, 'page_scope': self.scope, 'action': payload['action'],
                   'status': self.status, 'element_index': payload['index'] + int(self.mismatch)}
        self.actions.append(receipt)
        if self.status == 'ok':
            self.responses.append({'action_seq': seq, 'page_scope': self.scope, 'method': 'POST',
                                   'status': 303, 'resource': 'document'})
            self.page.url = self.page.url + '/result'
        return {'status': self.status, 'no_effect': self.no_effect, 'error': 'source_static_error', 'seconds': .001}


def submit(mac):
    observed = mac.observe()
    return mac.act(Action('click', element_id='b0'), observed)


@pytest.mark.parametrize('stages', [2, 3])
def test_source_required_flags_survive_multiple_delivered_forms_into_verifier_context(stages):
    from coding_agents.cu_ai_loop import verify_text
    from coding_agents.cu_verification_context import compose, is_wide, WIDE_CONTEXT_BYTES, WIDE_REQUEST_BYTES
    backend = SourceBackend()
    mac = BrowserMac(backend)
    for step in range(stages):
        backend.page.url = 'https://owned.example/stage/' + str(step)
        backend.fields = format_form_state({'v': 1, 'controls': [
            {'label': 'Reference', 'type': 'text', 'value': 'SOURCE-REFERENCE', 'required': False},
            {'label': 'Optional annotation', 'type': 'text', 'value': '', 'required': False},
        ], 'omitted': {'controls': 0, 'labels': 0, 'values': 0}})
        submit(mac)
    observed = mac.observe()
    value, text = history(mac)
    assert len(value['entries']) == stages and value['omitted_entries'] == 0
    for entry in value['entries']:
        assert entry['delivery'] == 'input_delivered' and entry['fields_qualified'] is True
        assert entry['fields']['controls'][1] == {
            'label': 'Optional annotation', 'type': 'text', 'value': '', 'required': False}
    owned = 'SOURCE-OWNED BROWSER EVIDENCE\n' + json.dumps(mac.completion_evidence())
    context = compose([], text, owned, '')
    contextual, _ = json.JSONDecoder().raw_decode(context.split('FORM INPUT HISTORY', 1)[1].split('\n', 1)[1])
    assert contextual == value
    assert 'SOURCE-REFERENCE' in context and 'omitted content' not in context
    request = verify_text('Submit every stage using source references; leave optional annotations blank', 'Finished',
                          observed.frontmost, observed.window_title, observed.elements, context, observed.notes)
    assert len(context.encode()) <= WIDE_CONTEXT_BYTES and len(request.encode()) <= WIDE_REQUEST_BYTES
    assert is_wide(text) == (stages == 3)
    assert len(backend.actions) == stages and len(backend.responses) == stages


def history(mac):
    text = mac.browser_readback()
    head = 'FORM INPUT HISTORY (chronological main-document fields before clicks; historical input, not submitted payload or acceptance proof):\n'
    assert head in text
    value, _ = json.JSONDecoder().raw_decode(text.split(head, 1)[1])
    assert len(text.encode()) <= (4200 if 'MULTI-PAGE TASK' in text else 2500)
    return value, text


def test_two_distinct_form_inputs_coexist_with_source_delivery_chronology():
    backend = SourceBackend()
    mac = BrowserMac(backend)
    submit(mac)
    backend.page.url = 'https://owned.example/second'
    backend.fields = form('SECOND-REVIEWED-VALUE')
    submit(mac)
    observed = mac.observe()
    value, text = history(mac)
    assert 'FIRST-REVIEWED-VALUE' in text and 'SECOND-REVIEWED-VALUE' in text
    entries = value['entries']
    assert [e['action_seq'] for e in entries] == [1, 2]
    assert [e['source_url'] for e in entries] == ['https://owned.example/first', 'https://owned.example/second']
    assert entries[0]['result_url'] == 'https://owned.example/first/result'
    assert all(e['delivery'] == 'input_delivered' and e['responses'][0]['status'] == 303 for e in entries)
    request = verify_text('Complete both reviewed forms', 'Saved', observed.frontmost, observed.window_title,
                          observed.elements, compose([], text, '', ''), observed.notes)
    assert 'FIRST-REVIEWED-VALUE' in request and 'SECOND-REVIEWED-VALUE' in request
    assert len(request.encode()) <= 10240


def test_repeated_same_url_and_form_keep_distinct_attempts():
    backend = SourceBackend()
    mac = BrowserMac(backend)
    for value in ('EARLIER-EXACT', 'LATER-EXACT'):
        backend.page.url = 'https://owned.example/same'
        backend.fields = form(value)
        submit(mac)
    mac.observe()
    value, _ = history(mac)
    assert len(value['entries']) == 2
    assert [e['action_seq'] for e in value['entries']] == [1, 2]
    assert value['entries'][0]['fields']['controls'][0]['value'] == 'EARLIER-EXACT'
    assert value['entries'][1]['fields']['controls'][0]['value'] == 'LATER-EXACT'


@pytest.mark.parametrize('change', ['none', 'accepted', 'server_error', 'unqualified', 'uncertain',
                                   'foreign_page', 'foreign_form', 'stale_observation', 'stale_snapshot'])
def test_validation_handoff_feedback_requires_current_qualified_rejection(change):
    class Rejecting(SourceBackend):
        def act(self, payload):
            result = super().act(payload)
            self.responses[-1]['status'] = 422
            self.page.url = 'https://owned.example/first'
            return result
        def observe(self):
            observed = super().observe()
            observed.elements.append({'i': 1, 'tag': 'input', 'label': 'Reference', 'type': 'text',
                'validation': 'Use the specified format', 'form_scope': self.form_scope, 'document_scope': 'main'})
            return observed
    backend = Rejecting()
    mac = BrowserMac(backend)
    assert mac.form_validation_feedback(mac.observe()) == ''
    submit(mac)
    observed = mac.observe()
    entry = mac._form_history[-1]
    if change == 'accepted':
        entry['responses'][0]['status'] = 200
    elif change == 'server_error':
        entry['responses'][0]['status'] = 500
    elif change == 'unqualified':
        entry['fields_qualified'] = False
    elif change == 'uncertain':
        entry['delivery'] = 'delivery_unconfirmed'
    elif change == 'foreign_page':
        entry['page_scope'] = 'c' * 32
    elif change == 'foreign_form':
        entry['form_scope'] = 'form:9:9'
    elif change == 'stale_observation':
        mac.observe()
    elif change == 'stale_snapshot':
        backend.snapshot_id = 'c' * 32
    feedback = mac.form_validation_feedback(observed)
    assert bool(feedback) == (change == 'none')
    if feedback:
        assert 'Reference: Use the specified format' in feedback and len(feedback) < 1400
    assert len(backend.actions) == 1


def test_rejected_form_blocks_identical_values_but_allows_specific_correction():
    from desktop_runtime.cu_ai_cua import CuaError
    class Rejecting(SourceBackend):
        def act(self, payload):
            result = super().act(payload)
            self.responses[-1]['status'] = 422
            self.page.url = 'https://owned.example/first'
            return result
    backend = Rejecting()
    mac = BrowserMac(backend)
    submit(mac)
    with pytest.raises(CuaError, match='identical rejected form values') as captured:
        submit(mac)
    assert captured.value.no_effect is True and len(backend.actions) == 1
    assert 'already rejected' in captured.value.validation_feedback
    backend.fields = form('CORRECTED-REVIEWED-VALUE')
    submit(mac)
    assert len(backend.actions) == 2


def test_rejected_form_stops_after_two_corrected_attempts_without_more_delivery():
    from desktop_runtime.cu_ai_engine import NeedsHuman
    class Rejecting(SourceBackend):
        def act(self, payload):
            result = super().act(payload)
            self.responses[-1]['status'] = 400
            self.page.url = 'https://owned.example/first'
            return result
    backend = Rejecting()
    mac = BrowserMac(backend)
    for value in ('INITIAL', 'CORRECTION-ONE', 'CORRECTION-TWO'):
        backend.fields = form(value)
        submit(mac)
    backend.fields = form('CORRECTION-THREE')
    with pytest.raises(NeedsHuman, match='field still rejects') as captured:
        submit(mac)
    assert captured.value.dispatch_state == 'not_dispatched' and len(backend.actions) == 3


def test_omitted_fields_cannot_prove_an_identical_rejected_submission():
    backend = SourceBackend()
    backend.fields = format_form_state({'v': 1, 'controls': [{'label': 'Reviewed field', 'type': 'text',
                                    'value': '', 'value_omitted': True}],
                                    'omitted': {'controls': 0, 'labels': 0, 'values': 1}})
    mac = BrowserMac(backend)
    submit(mac)
    backend.responses[-1]['status'] = 422
    backend.page.url = 'https://owned.example/first'
    submit(mac)
    assert len(backend.actions) == 2


def test_rejection_budget_survives_whole_history_row_eviction():
    from desktop_runtime.cu_ai_engine import NeedsHuman
    class Rejecting(SourceBackend):
        def act(self, payload):
            source = self.page.url
            result = super().act(payload)
            self.responses[-1]['status'] = 422
            self.page.url = source
            return result
    backend = Rejecting()
    mac = BrowserMac(backend)
    for value in ('INITIAL', 'CORRECTION-ONE', 'CORRECTION-TWO'):
        backend.fields = form(value)
        submit(mac)
    for index in range(9):
        backend.page.url = f'https://owned.example/other-{index}'
        submit(mac)
    backend.page.url = 'https://owned.example/first'
    backend.fields = form('CORRECTION-THREE')
    with pytest.raises(NeedsHuman):
        submit(mac)
    assert len(backend.actions) == 12


def test_omitted_fields_preserve_conservative_rejection_cap_without_more_delivery():
    from desktop_runtime.cu_ai_engine import NeedsHuman
    class Rejecting(SourceBackend):
        def act(self, payload):
            result = super().act(payload)
            self.responses[-1]['status'] = 422
            self.page.url = 'https://owned.example/first'
            return result
    backend = Rejecting()
    backend.fields = form('bounded', controls=1)
    mac = BrowserMac(backend)
    for _ in range(3):
        submit(mac)
    with pytest.raises(NeedsHuman) as error:
        submit(mac)
    assert error.value.dispatch_state == 'not_dispatched' and len(backend.actions) == 3


def test_correction_budget_is_per_changed_field_not_all_form_attempts():
    class Rejecting(SourceBackend):
        def act(self, payload):
            result = super().act(payload)
            self.responses[-1]['status'] = 422
            self.page.url = 'https://owned.example/first'
            return result
    def fields(code, email):
        return format_form_state({'v': 1, 'controls': [{'label': 'Code', 'type': 'text', 'value': code},
                   {'label': 'Email', 'type': 'email', 'value': email}],
                   'omitted': {'controls': 0, 'labels': 0, 'values': 0}})
    backend = Rejecting()
    mac = BrowserMac(backend)
    for code, email in [('initial', 'first'), ('correction1', 'first'), ('correction2', 'first'),
                        ('correction2', 'second'), ('correction2', 'third')]:
        backend.fields = fields(code, email)
        submit(mac)
    assert len(backend.actions) == 5
    backend.fields = fields('correction2', 'fourth')
    from desktop_runtime.cu_ai_engine import NeedsHuman
    with pytest.raises(NeedsHuman):
        submit(mac)
    assert len(backend.actions) == 5


def test_corrected_values_change_route_and_do_not_exhaust_other_field_retries():
    from desktop_runtime.cu_ai_retries import RetryGuard, route
    backend = SourceBackend()
    mac = BrowserMac(backend)
    mac.submit_key = lambda action, obs: backend.fields if action.op == 'click' else None
    obs = mac.observe()
    guard = RetryGuard()
    rejected_key, family = route(Action('click', element_id='b0'), obs, mac)
    for _ in range(3):
        guard.failed(rejected_key, family, 'rejected')
    backend.fields = form('SPECIFIC-CORRECTED-VALUE')
    corrected_key, _ = route(Action('click', element_id='b0'), obs, mac)
    assert guard.rejected(rejected_key) and not guard.rejected(corrected_key)
    obs.elements += [{'id': 'b1', 'ax_role': 'input', 'name': 'email'}, {'id': 'b2', 'ax_role': 'input', 'name': 'code'}]
    email_key, _ = route(Action('type', element_id='b1', text='unchanged'), obs, mac)
    code_key, _ = route(Action('type', element_id='b2', text='unchanged'), obs, mac)
    corrected_code_key, _ = route(Action('type', element_id='b2', text='corrected'), obs, mac)
    assert len({email_key, code_key, corrected_code_key}) == 3


@pytest.mark.parametrize('handoff', [False, True])
def test_engine_corrects_specific_field_after_rejection_without_exhausting_other_routes(handoff):
    import threading
    from desktop_runtime.cu_ai_engine import run_loop
    class Recovering(SourceBackend):
        def __init__(self):
            super().__init__()
            self.values = ['', '', '']
            self.page.evaluate = lambda *args: list(self.values)
        def observe(self):
            self.fields = format_form_state({'v': 1, 'controls': [
                {'label': label, 'type': 'text', 'value': value}
                for label, value in zip(('Name', 'Email', 'Code'), self.values)],
                'omitted': {'controls': 0, 'labels': 0, 'values': 0}})
            result = super().observe()
            result.text = 'Saved' if self.submit_accepted else 'Code must contain six uppercase letters or digits'
            result.elements += [{'i': index + 1, 'tag': 'input', 'name': label, 'type': 'text', 'value': value,
                'validation': 'Code must contain six uppercase letters or digits' if label == 'Code' and not self.submit_accepted else '',
                'form_scope': self.form_scope, 'document_scope': self.document_scope}
                for index, (label, value) in enumerate(zip(('Name', 'Email', 'Code'), self.values))]
            return result
        def form_state(self):
            return format_form_state({'v': 1, 'controls': [
                {'label': label, 'type': 'text', 'value': value}
                for label, value in zip(('Name', 'Email', 'Code'), self.values)],
                'omitted': {'controls': 0, 'labels': 0, 'values': 0}})
        submit_accepted = False
        def act(self, payload):
            if payload['action'] == 'type':
                self.values[payload['index'] - 1] = payload['text']
                self.actions.append({'seq': len(self.actions) + 1, 'page_scope': self.scope, 'action': 'type',
                    'status': 'ok', 'element_index': payload['index']})
                return {'status': 'ok', 'seconds': .001}
            result = super().act(payload)
            self.submit_accepted = self.values[2] == 'ABC123'
            if not self.submit_accepted:
                self.responses[-1]['status'] = 422
                self.page.url = 'https://owned.example/first'
            return result
    backend = Recovering()
    mac = BrowserMac(backend)
    replies = iter([
        {'actions': [{'op': 'type', 'element_id': 'b1', 'text': 'Ada'},
                     {'op': 'type', 'element_id': 'b2', 'text': 'ada@example.test'},
                     {'op': 'type', 'element_id': 'b3', 'text': 'abc123'}, {'op': 'click', 'element_id': 'b0'}]},
        {'actions': [{'op': 'type', 'element_id': 'b2', 'text': 'ada@example.test'}, {'op': 'click', 'element_id': 'b0'}]},
        *([{'actions': [{'op': 'needs_you', 'summary': 'Should I change the Code to uppercase?'}]}] if handoff else []),
        {'actions': [{'op': 'type', 'element_id': 'b3', 'text': 'ABC123'}, {'op': 'click', 'element_id': 'b0'}]},
        {'actions': [{'op': 'done', 'summary': 'Saved with corrected code'}]},
    ])
    def decide(payload):
        if len(backend.responses) == 1:
            assert 'six uppercase' in payload['observation'].notes
            if len(backend.actions) == 5:
                assert 'Code' in str(payload['history']) and 'abc123' in str(payload['history'])
                assert 'already rejected' in str(payload['history']) and 'six uppercase' in str(payload['history'])
        return {**next(replies), 'memory': '', 'subgoal_done': False}, {}
    result = run_loop('Fill and submit Name Ada, Email ada@example.test, Code abc123; correct any validation errors', decide, mac, threading.Event(),
                      verify=lambda *args: (backend.submit_accepted, 'Source accepted corrected form'))
    assert result.outcome == 'done', (result.reason, result.log, backend.actions)
    assert [row['status'] for row in backend.responses] == [422, 303]
    assert [row['element_index'] for row in backend.actions if row['action'] == 'type'] == [1, 2, 3, 2, 3]
    assert sum(row.get('outcome') == 'form_validation_retry_requested' for row in result.log) == int(handoff)
    assert not any(row.get('outcome') == 'needs_human_raised' for row in result.log)
    assert 'abc123' not in str(result.log)


@pytest.mark.parametrize('status,no_effect,expected,qualified', [('error', True, 'not_dispatched', False),
                                                               ('error', False, 'delivery_unconfirmed', True),
                                                               ('refused', True, 'not_dispatched', False)])
def test_failure_never_promotes_field_snapshot_to_accepted_delivery(status, no_effect, expected, qualified):
    from desktop_runtime.cu_ai_cua import CuaError
    from desktop_runtime.cu_ai_engine import NeedsHuman
    backend = SourceBackend()
    backend.status, backend.no_effect = status, no_effect
    mac = BrowserMac(backend)
    with pytest.raises((CuaError, NeedsHuman)):
        submit(mac)
    mac.observe()
    value, text = history(mac)
    entry = value['entries'][0]
    assert entry['delivery'] == expected and entry['fields_qualified'] is qualified
    assert ('FIRST-REVIEWED-VALUE' in text) is qualified
    assert entry['responses'] == []
    assert 'acceptance proof' in text


def test_mismatched_source_action_does_not_qualify_preclick_values():
    backend = SourceBackend()
    backend.mismatch = True
    mac = BrowserMac(backend)
    submit(mac)
    mac.observe()
    value, text = history(mac)
    assert value['entries'][0]['delivery'] == 'correlation_unavailable'
    assert value['entries'][0]['fields_qualified'] is False
    assert 'FIRST-REVIEWED-VALUE' not in text


def test_iframe_form_never_qualifies_main_document_fields_as_its_inputs():
    backend = SourceBackend()
    backend.form_scope = 'form:0:0'
    backend.document_scope = 'frame'
    mac = BrowserMac(backend)
    submit(mac)
    mac.observe()
    value, text = history(mac)
    assert value['entries'][0]['fields_qualified'] is False
    assert 'FIRST-REVIEWED-VALUE' not in text


def test_unknown_or_sensitive_field_shape_never_enters_history():
    backend = SourceBackend()
    backend.fields = json.dumps({'v': 1, 'controls': [{'label': 'Password', 'type': 'password', 'value': 'DUMMY_SECRET'}],
                                 'omitted': {'controls': 0, 'labels': 0, 'values': 0}})
    mac = BrowserMac(backend)
    submit(mac)
    mac.observe()
    value, text = history(mac)
    assert not value['entries'][0]['fields_qualified'] and 'DUMMY_SECRET' not in text


def test_bounds_drop_whole_snapshots_and_explicitly_report_omitted_attempts():
    backend = SourceBackend()
    mac = BrowserMac(backend)
    values = ['whole-' + str(n) + '-' + '界' * 65 for n in range(12)]
    for n, value in enumerate(values):
        backend.page.url = f'https://owned.example/form-{n}'
        backend.fields = form(value)
        submit(mac)
    mac.observe()
    data, text = history(mac)
    assert data['omitted_entries'] > 0
    assert data['omitted_entries'] + len(data['entries']) == len(values)
    assert len(data['entries']) <= 8 and 'omitted content is not verified' in text
    for entry in data['entries']:
        assert entry['fields']['controls'][0]['value'] in values
    assert values[-1] in text


def test_reset_changed_backend_and_lost_current_custody_clear_history_authority():
    backend = SourceBackend()
    mac = BrowserMac(backend)
    submit(mac)
    mac.observe()
    assert 'FIRST-REVIEWED-VALUE' in history(mac)[1]
    backend.scope = 'c' * 32
    assert 'FIRST-REVIEWED-VALUE' not in mac.browser_readback()
    mac.reset_completion_evidence()
    mac.observe()
    assert 'FIRST-REVIEWED-VALUE' not in mac.browser_readback()
    submit(mac)
    mac.observe()
    mac.b = SourceBackend()
    assert 'FIRST-REVIEWED-VALUE' not in mac.browser_readback()


@pytest.fixture
def owned_workflow(tmp_path):
    from pathlib import Path
    from desktop_runtime.cu_browser import BrowserBackend, ChromeSession
    from desktop_runtime.cu_browser.chrome import CHROME_BIN
    from scripts.desktop.cu_tasks.fixture_server import FixtureServer
    if not Path(CHROME_BIN).exists():
        pytest.skip('requires owned headless Chrome')
    class Fixture(FixtureServer):
        def get(self, h, path, q):
            if path == '/first':
                h.page('First', "<form method=post action=/first/save><label>Reviewed name <input name=name></label>"
                               "<input type=password name=password value=DUMMY_PASSWORD>"
                               "<input name=card_number value=DUMMY_CARD><button>Save first</button></form>")
            elif path == '/first/result':
                h.page('First saved', '<h1>First saved</h1><a href=/second>Continue</a>')
            elif path == '/second':
                h.page('Second', "<form method=post action=/second/save><label>Reviewed region <select name=region>"
                                "<option>North</option><option>West</option></select></label>"
                                "<label><input type=checkbox name=digest value=yes checked>Digest</label><button>Save second</button></form>")
            elif path == '/second/result':
                h.page('Completed', '<h1>Source saved both steps</h1>')
            elif path == '/frame-parent':
                h.page('Frame', f'<iframe src="{self.base}/frame"></iframe>')
            elif path == '/frame':
                h.page('Frame inputs', '<form method=post action=/frame/save><label>Frame value<input name=frame value=FRAME_ONLY></label><button>Save frame</button></form>')
            else:
                h.page('Result', '<h1>Source result</h1>')
        def post(self, h, path, body):
            target = '/first/result' if path == '/first/save' else '/second/result' if path == '/second/save' else '/frame/result'
            h.out(303, '', extra=[('Location', target)])
    fixture = Fixture('OwnedHistory').start()
    chrome = ChromeSession(tmp_path / 'profile', headless=True)
    backend = None
    try:
        backend = BrowserBackend(chrome.start(), download_dir=tmp_path / 'downloads', owned_context=True)
        yield fixture, backend, BrowserMac(backend)
    finally:
        if backend:
            backend.close()
        chrome.stop()
        fixture.close()
        fixture.thread.join(timeout=2)
        assert chrome.closed and chrome.proc.poll() is not None and chrome._group_gone
        assert not fixture.thread.is_alive()


def element(mac, predicate):
    observed = mac.observe()
    return observed, next(e for e in observed.elements if predicate(e))


def test_actual_owned_http_two_forms_keep_exact_values_and_correlated303_without_more_requests(owned_workflow):
    fixture, backend, mac = owned_workflow
    mac.act(Action('navigate', text=fixture.base + '/first'), None)
    observed, target = element(mac, lambda e: e.get('name') == 'name')
    mac.act(Action('type', element_id=target['id'], text='SOURCE-NAME-EXACT'), observed)
    observed, target = element(mac, lambda e: e.get('type') == '' and e['ax_role'] == 'button')
    mac.act(Action('click', element_id=target['id']), observed)
    observed, target = element(mac, lambda e: e['ax_role'] == 'a')
    mac.act(Action('click', element_id=target['id']), observed)
    observed, target = element(mac, lambda e: e.get('name') == 'region')
    mac.act(Action('type', element_id=target['id'], text='West'), observed)
    observed, target = element(mac, lambda e: e.get('type') == 'checkbox')
    mac.act(Action('click', element_id=target['id']), observed)
    observed, target = element(mac, lambda e: e['ax_role'] == 'button')
    mac.act(Action('click', element_id=target['id']), observed)
    mac.observe()
    before = len(fixture.posts)
    data, text = history(mac)
    assert len(fixture.posts) == before == 2
    assert fixture.posts[0][1]['name'] == 'SOURCE-NAME-EXACT'
    assert fixture.posts[1][1] == {'region': 'West'}
    assert 'SOURCE-NAME-EXACT' in text and 'West' in text and 'unchecked' in text
    assert 'DUMMY_PASSWORD' not in text and 'DUMMY_CARD' not in text
    assert len(data['entries']) == 2 and data['omitted_entries'] == 0
    assert all(e['fields_qualified'] and e['delivery'] == 'input_delivered' for e in data['entries'])
    assert all(any(r['method'] == 'POST' and r['status'] == 303 for r in e['responses']) for e in data['entries'])


def test_actual_iframe_basezero_does_not_claim_main_document_fields(owned_workflow):
    fixture, backend, mac = owned_workflow
    mac.act(Action('navigate', text=fixture.base + '/frame-parent'), None)
    observed, target = element(mac, lambda e: e['ax_role'] == 'button')
    assert target['document_scope'] == 'frame' and target['form_scope'] == 'form:0:0'
    mac.act(Action('click', element_id=target['id']), observed)
    mac.observe()
    data, text = history(mac)
    assert data['entries'][0]['fields_qualified'] is False and 'FRAME_ONLY' not in text
    assert len(fixture.posts) == 1


def test_actual_stale_same_url_document_refusal_cannot_qualify_new_document_fields(owned_workflow):
    from desktop_runtime.cu_ai_cua import CuaError
    fixture, backend, mac = owned_workflow
    mac.act(Action('navigate', text=fixture.base + '/first'), None)
    observed, target = element(mac, lambda e: e['ax_role'] == 'button')
    backend.page.reload(wait_until='domcontentloaded')
    backend.page.locator('input[name=name]').fill('NEW-DOCUMENT-UNQUALIFIED')
    with pytest.raises(CuaError) as error:
        mac.act(Action('click', element_id=target['id']), observed)
    assert error.value.no_effect is True
    mac.observe()
    text = mac.browser_readback()
    assert 'FORM INPUT HISTORY' not in text  # Refused before even taking an input snapshot.
    assert 'NEW-DOCUMENT-UNQUALIFIED' not in text and fixture.posts == []


def test_actual_delivered_input_followed_by_timeout_retains_unknown_without_replay(owned_workflow, monkeypatch):
    from desktop_runtime.cu_ai_cua import CuaError
    fixture, backend, mac = owned_workflow
    mac.act(Action('navigate', text=fixture.base + '/first'), None)
    observed, target = element(mac, lambda e: e.get('name') == 'name')
    mac.act(Action('type', element_id=target['id'], text='KNOWN-INPUT-UNKNOWN-DELIVERY'), observed)
    observed, target = element(mac, lambda e: e['ax_role'] == 'button')
    original = backend._do
    def delivered_then_timeout(kind, action):
        result = original(kind, action)
        if kind == 'click':
            raise RuntimeError('source_timeout_after_delivery')
        return result
    monkeypatch.setattr(backend, '_do', delivered_then_timeout)
    with pytest.raises(CuaError) as error:
        mac.act(Action('click', element_id=target['id']), observed)
    assert error.value.no_effect is not True
    mac.observe()
    data, text = history(mac)
    assert len(fixture.posts) == 1
    assert data['entries'][0]['delivery'] == 'delivery_unconfirmed'
    assert data['entries'][0]['fields_qualified'] is True
    assert 'KNOWN-INPUT-UNKNOWN-DELIVERY' in text


def test_unavailable_preclick_source_ledger_never_qualifies_observed_values():
    backend = SourceBackend()
    original = backend.completion_evidence
    calls = 0
    def unknown_first_ledger():
        nonlocal calls
        calls += 1
        return None if calls == 1 else original()
    backend.completion_evidence = unknown_first_ledger
    mac = BrowserMac(backend)
    submit(mac)
    mac.observe()
    data, text = history(mac)
    assert data['entries'][0]['fields_qualified'] is False
    assert data['entries'][0]['delivery'] == 'correlation_unavailable'
    assert 'FIRST-REVIEWED-VALUE' not in text


def test_whole_field_omission_remains_explicit_inside_retained_snapshot():
    backend = SourceBackend()
    backend.fields = format_form_state({'v': 1, 'controls': [{'label': 'Reviewed field', 'type': 'text',
                                      'value': '', 'value_omitted': True}],
                                      'omitted': {'controls': 0, 'labels': 0, 'values': 1}})
    mac = BrowserMac(backend)
    submit(mac)
    mac.observe()
    data, text = history(mac)
    assert data['entries'][0]['fields']['omitted']['values'] == 1
    assert 'form fields omitted; omitted content is not verified' in text
    assert data['entries'][0]['fields']['controls'][0]['value'] == ''
