"""General pre-submit field custody: lossless bounded values and explicit unknowns."""
import html
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from coding_agents.cu_ai_loop import Action, verify_text
from coding_agents.cu_verification_context import compose
from desktop_runtime.cu_ai_browser import BrowserMac
from desktop_runtime.cu_browser import BrowserBackend, ChromeSession
from desktop_runtime.cu_browser.chrome import CHROME_BIN
from desktop_runtime.cu_browser.dom import format_form_state


def source(controls, **omitted):
    return {'v': 1, 'controls': controls, 'omitted': {'controls': 0, 'labels': 0, 'values': 0, **omitted}}


def test_dom_form_readback_preserves_required_flags_and_blank_optional_fields_without_browser():
    import subprocess
    from desktop_runtime.cu_browser.dom import FORM_STATE_JS
    script = r'''
    const field = (name, required, value) => ({tagName:'INPUT',type:'text',name,required,value,
      labels:[],getAttribute:()=>'',getBoundingClientRect:()=>({width:100,height:30})});
    global.document = {querySelectorAll:()=>[
      field('reference',true,'SOURCE-REFERENCE'),field('annotation',false,''),field('requested',false,'')]};
    console.log(JSON.stringify((FORM_FUNCTION)()));
    '''.replace('FORM_FUNCTION', FORM_STATE_JS)
    output = subprocess.run(['node', '-e', script], capture_output=True, text=True, timeout=10, check=True)
    controls = json.loads(format_form_state(json.loads(output.stdout)))['controls']
    assert controls == [
        {'label': 'reference', 'type': 'text', 'value': 'SOURCE-REFERENCE', 'required': True},
        {'label': 'annotation', 'type': 'text', 'value': '', 'required': False},
        {'label': 'requested', 'type': 'text', 'value': '', 'required': False},
    ]


def test_long_labels_and_values_within_bound_survive_exactly_without_url_competition():
    fields = [{'label': 'Requested project ' + 'L' * 65, 'type': 'text', 'value': 'Reviewed value-' + 'V' * 50},
              {'label': 'Region', 'type': 'select', 'value': 'North'},
              {'label': 'Time zone', 'type': 'select', 'value': 'Asia/Kolkata'}]
    text = format_form_state(source(fields))
    assert json.loads(text)['controls'] == fields
    assert len(text.encode()) <= 900 and 'omitted content' not in text
    class Backend:
        def __init__(self):
            self.page = SimpleNamespace(url='https://owned.example/' + 'u' * 80)
        def form_state(self):
            return text
        def observe(self):
            return SimpleNamespace(url=self.page.url, title='Review', text='Source saved', tabs=[], captcha=False,
                                   elements=[{'i': 0, 'tag': 'button', 'type': 'submit', 'label': 'Save'}])
        def act(self, payload):
            return {'status': 'ok', 'seconds': .001}
    backend = Backend()
    mac = BrowserMac(backend)
    obs = mac.observe()
    mac.act(Action('click', element_id='b0'), obs)
    mac.observe()
    assert mac.last_form_state == text and mac.last_form_url == backend.page.url[:70]
    readback = mac.browser_readback()
    for field in fields:
        assert field['label'] in readback and field['value'] in readback
    assert 'FORM CLICK RESULT URL' in readback and len(readback.encode()) <= 2500
    context = compose([], readback, '', '')
    request = verify_text('Create the reviewed source record', 'Saved', obs.frontmost, obs.window_title, obs.elements, context, obs.notes)
    assert all(field['value'] in request for field in fields)
    assert len(context.encode()) <= 5000 and len(request.encode()) <= 10240
    mac.reset_completion_evidence()
    assert mac.last_form_state == mac.last_form_url == ''


def test_utf8_budget_drops_whole_fields_and_marks_omissions_instead_of_partial_values():
    controls = [{'label': f'Field {n}', 'type': 'text', 'value': ('界' * 100) + f'-tail-{n}'} for n in range(12)]
    text = format_form_state(source(controls))
    data = json.loads(text.splitlines()[0])
    assert data['omitted']['controls'] > 0 and len(text.encode()) <= 900
    assert len(data['controls']) + data['omitted']['controls'] == len(controls)
    for row in data['controls']:
        assert row in controls and row['value'].endswith('-tail-' + row['label'].split()[-1])
    assert 'omitted content is not verified' in text


@pytest.mark.parametrize('value', [None, [], {'v': True, 'controls': [], 'omitted': {'controls': 0, 'labels': 0, 'values': 0}},
                                     source([{'label': 'Password', 'type': 'password', 'value': 'DUMMY_SECRET'}]),
                                     source([{'label': 'Field', 'type': 'text', 'value': 'X' * 257}]),
                                     source([{'label': 'Field', 'type': 'text', 'value': '', 'value_omitted': True}]),
                                     source([], controls_extra=1)])
def test_unknown_malformed_and_unmarked_truncation_never_become_complete_fields(value):
    text = format_form_state(value)
    assert text == 'FORM STATE unavailable; omitted content is not verified'
    assert 'DUMMY_SECRET' not in text and 'X' * 20 not in text


def test_marked_oversized_value_remains_unknown_without_claiming_a_prefix_match():
    text = format_form_state(source([{'label': 'Field', 'type': 'text', 'value': '', 'value_omitted': True}], values=1))
    assert json.loads(text.splitlines()[0])['controls'][0]['value'] == ''
    assert 'omitted content is not verified' in text


@pytest.mark.parametrize('required', [None, 0, 1, 'false', [], {}])
def test_required_flag_accepts_only_source_booleans(required):
    assert format_form_state(source([{'label': 'Field', 'type': 'text', 'value': '', 'required': required}])) == (
        'FORM STATE unavailable; omitted content is not verified')


@pytest.fixture
def backend(tmp_path):
    if not Path(CHROME_BIN).exists():
        pytest.skip('requires owned headless Chrome')
    chrome = ChromeSession(tmp_path / 'profile', headless=True)
    browser = None
    try:
        browser = BrowserBackend(chrome.start(), download_dir=tmp_path / 'downloads')
        yield browser
    finally:
        try:
            if browser:
                browser.close()
        finally:
            chrome.stop()
        assert chrome.closed and chrome.proc.poll() is not None and chrome._group_gone


def test_actual_dom_compacts_select_labels_preserves_long_values_and_filters_private_controls(backend):
    label = 'Reviewed long source label ' + 'N' * 55
    value = 'Observed exact source value ' + 'V' * 45
    backend.page.set_content(f"<form><label>{html.escape(label)}<input name=reviewed value='{value}'></label>"
                             "<label>Region <select><option>North</option><option selected>West</option><option>East</option></select></label>"
                             "<label>Time zone <select><option>UTC</option><option>Europe/Lisbon</option><option selected>Asia/Kolkata</option></select></label>"
                             "<input type=password name=password value=DUMMY_PASSWORD>"
                             "<input name=card_number value=DUMMY_CARD><input name=cvv value=DUMMY_CVV>"
                             "<input type=hidden value=DUMMY_HIDDEN><label><input type=checkbox checked>Digest</label></form>")
    backend.observe()
    state = backend.form_state()
    controls = json.loads(state)['controls']
    assert controls[:3] == [{'label': label, 'type': 'text', 'value': value, 'required': False},
                            {'label': 'Region', 'type': 'select', 'value': 'West', 'required': False},
                            {'label': 'Time zone', 'type': 'select', 'value': 'Asia/Kolkata', 'required': False}]
    assert controls[-1] == {'label': 'Digest', 'type': 'checkbox', 'value': 'checked', 'required': False}
    assert 'DUMMY_' not in state and len(state.encode()) <= 900
    assert backend.completion_evidence()['actions'] == []


def test_actual_dom_file_input_reports_chosen_file_name_only(backend, tmp_path):
    upload = tmp_path / 'avatar.png'
    upload.write_bytes(b'DUMMY_FILE_CONTENT')
    backend.page.set_content("<form><label>Avatar <input type=file name=avatar></label></form>")
    backend.page.set_input_files('input[type=file]', str(upload))
    backend.observe()
    state = backend.form_state()
    assert json.loads(state)['controls'] == [{'label': 'Avatar', 'type': 'file', 'value': 'avatar.png', 'required': False}]
    assert 'DUMMY_' not in state and str(tmp_path) not in state


def test_actual_dom_oversized_values_labels_and_many_controls_are_explicit_unknowns(backend):
    label, value = 'L' * 161, 'V' * 257
    backend.page.set_content(f"<label>{label}<input value='{value}'></label>" +
                             ''.join(f"<label>Field {n}<input value='value-{n}'></label>" for n in range(40)))
    backend.observe()
    state = backend.form_state()
    data = json.loads(state.splitlines()[0])
    assert data['controls'][0]['label'] == data['controls'][0]['value'] == ''
    assert data['controls'][0]['label_omitted'] is data['controls'][0]['value_omitted'] is True
    assert data['omitted']['labels'] == data['omitted']['values'] == 1 and data['omitted']['controls'] > 8
    assert 'omitted content is not verified' in state and len(state.encode()) <= 900
    assert label not in state and value not in state


def test_actual_dom_multiple_select_never_presents_first_selected_value_as_complete(backend):
    backend.page.set_content("<label>Selected sources <select multiple><option selected>Source A</option>"
                             "<option selected>Source B</option></select></label>")
    backend.observe()
    state = backend.form_state()
    data = json.loads(state.splitlines()[0])
    assert data['controls'] == [{'label': 'Selected sources', 'type': 'select', 'value': '', 'required': False, 'value_omitted': True}]
    assert data['omitted']['values'] == 1 and 'omitted content is not verified' in state
    assert 'Source A' not in state and 'Source B' not in state


def test_actual_dom_hidden_label_descendants_never_enter_readback(backend):
    backend.page.set_content("<label>Region<span hidden>DUMMY_HIDDEN</span><span style='display:none'>DUMMY_CSS</span>"
                             "<span style='visibility:hidden'>DUMMY_INVISIBLE</span><span aria-hidden=true>DUMMY_ARIA</span>"
                             "<script>/* DUMMY_SCRIPT */</script><style>/* DUMMY_STYLE */</style><noscript>DUMMY_NOSCRIPT</noscript>"
                             "<select><option>East</option><option selected>West</option></select></label>")
    backend.observe()
    state = backend.form_state()
    assert json.loads(state)['controls'] == [{'label': 'Region', 'type': 'select', 'value': 'West', 'required': False}]
    assert 'DUMMY_' not in state


def test_form_readback_error_is_explicit_and_retains_no_stale_values():
    browser = object.__new__(BrowserBackend)
    browser.page = SimpleNamespace(evaluate=lambda _: (_ for _ in ()).throw(RuntimeError('PRIVATE VALUE')))
    assert browser.form_state() == 'FORM STATE unavailable; omitted content is not verified'
