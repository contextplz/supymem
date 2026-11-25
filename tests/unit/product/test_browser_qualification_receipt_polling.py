"""Known source responses wait for delivery acknowledgement without repeating HTTP."""
from pathlib import Path

import pytest

from coding_agents.computer_use_gate import EffectUncertain
from coding_agents.jev_decision import Candidate
from tests.unit.product.test_browser_qualification_capabilities import catalog_browser, target_link


def submit_form(browser,rule):
    for field in rule.fields:
        observation = browser.observe()
        target = next(control.id for control in observation.controls
            if browser._control_meta[control.id]['source_control_id']==field.control_id)
        browser.type_text(Candidate('type',target,observation.digest(),
            value_index=observation.reviewed_text.index(field.input_value)),'bai_poll_fill_a0',field.input_value)
    observation = browser.observe()
    target = next(control.id for control in observation.controls
        if browser._control_meta[control.id]['source_control_id']==rule.control_id)
    candidate = Candidate('click',target,observation.digest())
    browser.qualification_proposal('bai_poll_submit_a0',candidate,[rule.rule_id])
    browser.click(candidate,'bai_poll_submit_a0')


def test_actual_303_waits_for_exact_post_delivery_before_reviewed_redirect_get(tmp_path):
    with catalog_browser(tmp_path,'form_valid') as fixture:
        browser = fixture.browser
        gateway = browser._qualification_gateway
        actual = gateway.request
        polls = []
        def delayed(method,path,body):
            receipt = actual(method,path,body)
            if '/requests/' in path and receipt.get('method')=='POST':
                receipt['server_response_sent'] = len(polls)>=2
                polls.append(receipt['server_response_sent'])
                if receipt['server_response_sent'] is False:
                    assert not any(url.endswith('/form/done') for _,url,_ in fixture.events)
                    assert browser.last_qualification_response is None and browser._qualification_pending is not None
            return receipt
        gateway.request = delayed
        submit_form(browser,next(rule for rule in fixture.grant.rules if rule.method=='POST'))
        assert polls==[False,False,True]
        assert len([event for event in fixture.events if event[0]=='POST'])==1
        assert fixture.events[-1][1].endswith('/form/done')
        assert browser.last_qualification_response['status_code']==303 and not browser._uncertain


@pytest.mark.parametrize('field,value',[('response_digest','f'*64),('response_size_bytes',1),
    ('response_mime_type','application/json')])
def test_unsent_known_post_metadata_mismatch_cannot_be_polled_into_permission(tmp_path,field,value):
    with catalog_browser(tmp_path,'form_valid') as fixture:
        browser = fixture.browser
        gateway = browser._qualification_gateway
        actual = gateway.request
        polls = []
        def mismatch(method,path,body):
            receipt = actual(method,path,body)
            if '/requests/' in path and receipt.get('method')=='POST':
                receipt.update(server_response_sent=False,**{field:value})
                polls.append(receipt)
            return receipt
        gateway.request = mismatch
        with pytest.raises(EffectUncertain):
            submit_form(browser,next(rule for rule in fixture.grant.rules if rule.method=='POST'))
        assert len(polls)==1 and browser._uncertain and browser.last_qualification_response is None
        assert len([event for event in fixture.events if event[0]=='POST'])==1
        assert not any(url.endswith('/form/done') for _,url,_ in fixture.events)


@pytest.mark.parametrize('task_id',['download','l06_recover_from_error_page'])
def test_known_get_pending_delivery_preserves_native_file_or_failed_read_until_sent(tmp_path,task_id):
    native_paths = []
    with catalog_browser(tmp_path,task_id) as fixture:
        browser = fixture.browser
        gateway = browser._qualification_gateway
        actual = gateway.qualification_read_receipt
        polls = []
        def delayed(response,*,allow_pending=False):
            assert allow_pending is True
            saved = gateway._fixture_read_receipts[response['request_id']]
            saved['server_response_sent'] = len(polls)>=2
            receipt = actual(response,allow_pending=allow_pending)
            pending = browser._qualification_download_pending
            if receipt['server_response_sent'] is False:
                assert pending is not None and browser.last_qualification_download is None
                assert browser.last_qualification_read_response is None
                assert browser._qualification_download_dir is None
                if task_id=='download':
                    native_paths.append(pending['download'].path())
                    assert native_paths[-1].is_file()
            polls.append(receipt['server_response_sent'])
            return receipt
        gateway.qualification_read_receipt = delayed
        observation = browser.observe()
        target = target_link(browser,observation,'/download' if task_id=='download' else '/reports/q-download')
        browser.click(Candidate('navigate',target,observation.digest()),'bai_pending_get_a0',
            navigate_url=browser._control_meta[target]['href'])
        assert polls==[False,False,True] and not browser._qualification_download_pending and not browser._uncertain
        assert len(fixture.events)==2
        if task_id=='download':
            assert browser.last_qualification_download is not None
            assert all(not path.exists() for path in native_paths)
        else:
            assert browser.last_qualification_download is None and browser.last_qualification_read_response['status']==503


def test_unsent_get_exhaustion_never_reports_success_or_repeats_source_http(tmp_path):
    paths = []
    with catalog_browser(tmp_path,'download') as fixture:
        browser = fixture.browser
        gateway = browser._qualification_gateway
        actual = gateway.qualification_read_receipt
        polls = []
        def unsent(response,*,allow_pending=False):
            assert allow_pending is True
            saved = gateway._fixture_read_receipts[response['request_id']]
            saved['server_response_sent'] = False
            receipt = actual(response,allow_pending=allow_pending)
            path = browser._qualification_download_pending['download'].path()
            assert path.is_file()
            paths.append(path)
            polls.append(receipt)
            return receipt
        gateway.qualification_read_receipt = unsent
        observation = browser.observe()
        target = target_link(browser,observation,'/download')
        with pytest.raises(EffectUncertain):
            browser.click(Candidate('navigate',target,observation.digest()),'bai_unsent_get_a0',
                navigate_url=browser._control_meta[target]['href'])
        assert 0<len(polls)<=20 and len(fixture.events)==2
        assert browser._uncertain and browser.last_qualification_download is None
        assert browser._qualification_download_pending is not None
        with pytest.raises(PermissionError):
            browser.observe()
    assert paths and all(not Path(path).exists() for path in paths)


def test_pending_source_metadata_mismatch_refuses_on_first_poll(tmp_path):
    with catalog_browser(tmp_path,'download') as fixture:
        browser = fixture.browser
        gateway = browser._qualification_gateway
        actual = gateway.qualification_read_receipt
        polls = []
        def mismatch(response,*,allow_pending=False):
            saved = gateway._fixture_read_receipts[response['request_id']]
            saved.update(server_response_sent=False,response_digest='f'*64)
            polls.append(response)
            return actual(response,allow_pending=allow_pending)
        gateway.qualification_read_receipt = mismatch
        observation = browser.observe()
        target = target_link(browser,observation,'/download')
        with pytest.raises(EffectUncertain):
            browser.click(Candidate('navigate',target,observation.digest()),'bai_bad_pending_a0',
                navigate_url=browser._control_meta[target]['href'])
        assert len(polls)==1 and len(fixture.events)==2
        assert browser._uncertain and browser.last_qualification_download is None
