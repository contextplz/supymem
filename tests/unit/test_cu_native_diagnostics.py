"""Closed native receipt diagnostics; no driver or UI calls."""
from types import SimpleNamespace

import pytest

from desktop_runtime.cu_ai_cua import CuaError, session_caller
from desktop_runtime.cu_ai_engine import NeedsHuman


class PrivateContent:
    def __iter__(self):
        pytest.fail('private MCP error content accessed')


def test_error_diagnostic_precedes_hold_and_never_reads_private_blocks():
    rows = []
    result = SimpleNamespace(isError=True, content=PrivateContent(), structuredContent={
        'status': 'refused', 'effect': 'refused', 'verified': False,
        'refusal': {'code': 'element_outside_target_window', 'reason': 'private AX prose'},
        'window_title': 'private title'})
    session = SimpleNamespace(call=lambda *a, **kw: result, close=lambda: None)
    with pytest.raises(NeedsHuman):
        session_caller(session, diagnostic=rows.append)('click', {'text': 'private input'})
    assert rows == [{'tool': 'click', 'is_error': True, 'status': 'refused', 'effect': 'refused',
                     'verified': False, 'code': 'element_outside_target_window'}]


def test_unknown_error_fields_and_sink_crash_do_not_mask_uncertainty():
    rows = []
    result = SimpleNamespace(isError=True, content=PrivateContent(), structuredContent={
        'status': 'private', 'effect': 'private', 'verified': 'private', 'code': 'private-AX-code'})
    session = SimpleNamespace(call=lambda *a, **kw: result, close=lambda: None)
    with pytest.raises(CuaError, match='driver_readonly_failed'):
        session_caller(session, diagnostic=rows.append)('get_window_state', {})
    assert rows == [{'tool': 'get_window_state', 'is_error': True, 'status': 'unknown',
                     'effect': 'unknown', 'verified': None, 'code': 'withheld'}]
    def broken(_):
        raise ValueError('private sink failure')
    with pytest.raises(NeedsHuman):
        session_caller(session, diagnostic=broken)('click', {})


def test_actual_qa_factory_routes_closed_diagnostic_to_native_caller(monkeypatch):
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts/desktop'))
    import cu_ai_trial as trial
    import desktop_runtime.cu_qa_direct_session as direct
    import desktop_runtime.cu_ai_browser as browser
    rows = []
    result = SimpleNamespace(isError=True, structuredContent={'refusal': {'code': 'facility_unavailable'}},
                             content=PrivateContent())
    monkeypatch.setattr(direct, 'QADirectSession', lambda *a, **kw: SimpleNamespace(
        call=lambda *a, **kw: result, close=lambda: None))
    monkeypatch.setattr(trial, 'CuaMac', lambda caller, **kw: SimpleNamespace(call=caller, close=caller.close))
    monkeypatch.setattr(trial, 'PeekabooMac', lambda *a: None)
    monkeypatch.setattr(browser, 'BrowserMac', lambda **kw: None)
    monkeypatch.setattr(browser, 'RoutedMac', lambda native, page: native)
    mac = trial.make_mac('cua', qa_direct=True, native_diagnostic=rows.append)
    with pytest.raises(NeedsHuman):
        mac.call('click', {})
    mac.close()
    assert rows[0]['code'] == 'facility_unavailable'
    assert rows[0]['tool'] == 'click'


@pytest.mark.parametrize('number,name,want', [(-25204, 'cannot_complete', -25204),
    (-25211, 'api_disabled', -25211), (-25204, 'private native label', None),
    (True, 'failure', None), (42, 'cannot_complete', None), (-25215, 'unknown', None)])
def test_typed_native_ax_diagnostic_exact_numeric_category_pair_only(number, name, want):
    from desktop_runtime.cu_ai_cua import native_diagnostic
    result = SimpleNamespace(isError=True, content=PrivateContent(), structuredContent={
        'status': 'failed', 'code': 'ax_action_failed', 'effect': 'unverifiable', 'verified': False,
        'stage': 'ax_perform_action', 'ax_error_code': number, 'ax_error': name, 'body': 'private AX label'})
    row = native_diagnostic('click', result)
    assert row['code'] == 'ax_action_failed'
    assert row['effect'] == 'unverifiable' and row['verified'] is False
    assert row['ax_error_code'] == want
    assert row['ax_error'] == name if want is not None else row['ax_error'] == 'withheld'
    assert row['stage'] == 'ax_perform_action'
    assert 'private' not in str(row)



def _closed_probe_sample():
    return {'stage':'capture_preflight','reason':'sheets_unavailable','sample_index':0,
        'target_cg_present':None,'target_cg_owned':None,'parent_cg_owned':None,'parent_identity_exact':None,
        'sheets_available':False,'sheets_count':None,'target_sheet_listed':None,'ax_error_code':-25205}


def test_successful_ack_retains_closed_capture_failure_diagnostic_without_confirmation():
    rows=[]
    result=SimpleNamespace(isError=False, content=[], structuredContent={
        'effect':'unverifiable','verified':False,'window_title':'private title','probe_diagnostics':{
            'protocol':'supymem_press_probe_metadata_v1','truncated':False,'samples':[_closed_probe_sample()]}})
    session=SimpleNamespace(call=lambda *a,**kw:result,close=lambda:None)
    returned=session_caller(session,diagnostic=rows.append)('click',{'text':'private input'})
    assert returned['verified'] is False
    row=rows[0]
    assert row['verified'] is False and row['effect']=='unverifiable'
    assert row['probe_diagnostics']['samples'][0]['ax_error_code']==-25205
    assert row['probe_diagnostics']['samples'][0]['reason']=='sheets_unavailable'
    assert 'private' not in str(row)  # Only closed fields enter the sink.


@pytest.mark.parametrize('tamper',[
    lambda v:v.update(body='private native content'),
    lambda v:v['samples'][0].update(window_title='private title'),
    lambda v:v['samples'][0].update(reason='private AX prose'),
    lambda v:v['samples'][0].update(ax_error_code=True),
    lambda v:v['samples'][0].update(ax_error_code=42),
    lambda v:v['samples'][0].update(sheets_count=33),
    lambda v:v['samples'][0].update(parent_identity_exact='private identity'),
    lambda v:v['samples'].extend([_closed_probe_sample() for _ in range(16)]),
])
def test_probe_unknown_or_private_fields_are_denied_whole(tamper):
    from desktop_runtime.cu_ai_cua import native_diagnostic
    value={'protocol':'supymem_press_probe_metadata_v1','truncated':False,'samples':[_closed_probe_sample()]}
    tamper(value)
    result=SimpleNamespace(isError=False,content=PrivateContent(),structuredContent={'verified':False,'effect':'unverifiable','probe_diagnostics':value})
    row=native_diagnostic('click',result)
    assert 'probe_diagnostics' not in row and 'private' not in str(row)


def test_post_ack_unknown_ownership_stays_unknown_and_samples_stay_ordered():
    from desktop_runtime.cu_ai_cua import native_diagnostic
    sample=_closed_probe_sample()
    sample.update(stage='after_ack_sample',reason='postcondition_unconfirmed',sample_index=1,ax_error_code=None,
        target_cg_present=False,target_cg_owned=False,parent_cg_owned=True,sheets_available=None)
    result=SimpleNamespace(isError=False,content=PrivateContent(),structuredContent={'verified':False,'effect':'unverifiable',
        'probe_diagnostics':{'protocol':'supymem_press_probe_metadata_v1','truncated':False,'samples':[sample]}})
    row=native_diagnostic('click',result)
    got=row['probe_diagnostics']['samples'][0]
    assert got['parent_identity_exact'] is None and got['sheets_available'] is None
    assert got['target_cg_present'] is False and row['verified'] is False


def test_canonical_probe_metadata_survives_without_legacy_verified_or_private_content():
    from desktop_runtime.cu_ai_cua import native_diagnostic
    value = {'effect': 'unverifiable', 'route': 'accessibility', 'delivery': {'mode': 'background'},
        'summary': 'private native label', 'probe_diagnostics': {
            'protocol': 'supymem_press_probe_metadata_v1', 'truncated': False,
            'samples': [_closed_probe_sample()]}}
    result = SimpleNamespace(isError=False, content=PrivateContent(), structuredContent=value)
    row = native_diagnostic('click', result)
    assert row['effect'] == 'unverifiable' and row['verified'] is None
    assert row['probe_diagnostics']['samples'][0]['ax_error_code'] == -25205
    assert 'private' not in str(row)
    # The canonical effect stays unconfirmed; diagnostic availability adds no task proof.
    result.content = []
    session = SimpleNamespace(call=lambda *a, **kw: result, close=lambda: None)
    returned = session_caller(session)('click', {})
    assert returned['effect'] == 'unverifiable' and 'verified' not in returned
