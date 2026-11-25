"""Real offline Chrome exercises grant-scoped tabs, frames and CSV custody."""
from contextlib import contextmanager
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import shutil
from types import SimpleNamespace
from urllib.parse import parse_qsl

import pytest

from coding_agents.cu_ai_loop import Action
from coding_agents.jev_decision import Candidate
from coding_agents.owned_browser import OwnedBrowser, cuai_document_target
from core.browser_qualification_catalog import fixture_contract, fixture_transition, new_fixture_state
from tests.unit.product.test_browser_qualification_runtime import claimed_gateway, fixture_http_response


@contextmanager
def catalog_browser(tmp_path, task_id, *, grant_change=None, response_change=None, factory=False):
    gateway, original, _, status, receipt, calls = claimed_gateway()
    snapshot = new_fixture_state(task_id, "abcdef1234", "/public/browser-qualification/" + original.nonce)
    contract = fixture_contract(snapshot)
    grant = replace(original, task_id=task_id, catalog_digest=contract['catalog_digest'],
        seed_digest=contract['seed_digest'], rules=contract['rules'],
        allowed_operations=contract['allowed_operations'], max_tabs=contract['max_tabs'],
        frames=contract['frames'], downloads=contract['downloads'])
    if grant_change:
        grant = grant_change(grant)
    order = gateway.request('GET', '/order', None)
    order.update(browser_qualification_grant=grant.to_dict(),
        browser_qualification_trial_binding=grant.trial_binding().to_dict())
    gateway.browser_qualification_binding = grant.trial_binding()
    gateway.browser_qualification_grant_digest = grant.grant_digest
    status.update({key:getattr(grant,key) for key in ('task_id','catalog_digest','seed_digest')})
    status.update(grant_digest=grant.grant_digest, completed_rules=[], rules={rule.rule_id:{
        'request_count':0,'committed_count':0,'protected':False} for rule in grant.rules})
    status['read_prerequisites'] = status['completed_rules']
    events, effects, blocked = [], [], []
    def admit(effect, digest):
        effects.append(effect)
        return gateway.admit_effect(effect['invocation'], effect['observation'], digest)
    browser = OwnedBrowser(order_id=grant.order_id, goal=contract['goal'], origins=[grant.origin],
        authorize=lambda *_:True, admit_effect=admit, cuai_mode=True, general_mode=True,
        profile_dir=tmp_path / 'chrome', qualification_grant=grant.to_dict(),
        qualification_current=gateway.qualification_current, qualification_gateway=gateway)
    if factory:
        from desktop_runtime.cu_ai_browser import ChromeFactory
        browser._factory = ChromeFactory(tmp_path/'owned-factory',account_id='usr_qualification_fixture',
            profile_email='',headless=True)
        browser._factory_headless = True
    actual_route = browser._route
    def local_route(route):
        def fetched(**kwargs):
            nonlocal snapshot
            request = route.request
            body = (json.loads(request.post_data_buffer) if request.headers.get('content-type','').startswith('application/json')
                else dict(parse_qsl(request.post_data_buffer.decode(),keep_blank_values=True))) if request.method == 'POST' else {}
            rule = grant.request_rule(url=request.url,method=request.method,
                submitted_fields=list(body.items()), completed_rules=status['completed_rules'])
            transition = fixture_transition(snapshot,rule,body)
            snapshot, response = transition.state, transition.response
            events.append((request.method, request.url, response.status))
            if response_change:
                response = response_change(response,rule)
            if request.method == 'POST':
                payload = next(value for _,path,value in reversed(calls) if path.endswith('/bind-qualification'))
                assert kwargs['headers']['X-Supymem-Qualification-Request'] == payload['qualification']['request_id']
                mutated = response.state_change == 'commit'
                receipt.update(payload['qualification'], invocation_id=payload['invocation_id'],
                    effect_digest=payload['effect_digest'],method='POST',path=rule.path,query=rule.query,
                    body_digest=hashlib.sha256(request.post_data_buffer).hexdigest(),
                    response_digest=hashlib.sha256(response.body).hexdigest(),status_code=response.status,
                    response_size_bytes=len(response.body),response_mime_type=response.media_type.split(';',1)[0].strip().lower(),
                    state='committed' if mutated else 'rejected_before_mutation',mutated=mutated)
                status['rules'][rule.rule_id]['committed_count'] += int(mutated)
            status['rules'][rule.rule_id]['request_count'] += 1
            if response.status < 400 and rule.rule_id not in status['completed_rules']:
                status['completed_rules'].append(rule.rule_id)
            return fixture_http_response(gateway,grant,rule,response.body,status=response.status,
                mime_type=response.media_type,headers=response.headers,
                request_id=payload['qualification']['request_id'] if request.method=='POST' else None)
        def aborted(*args):
            blocked.append(route.request.url)
            route.abort(*args)
        actual_route(SimpleNamespace(request=route.request,fetch=fetched,fulfill=route.fulfill,abort=aborted))
    browser._route = local_route
    try:
        if factory:
            from desktop_runtime import cu_browser
            from desktop_runtime.cu_browser.chrome import CHROME_BIN
            executable = next((path for path in (CHROME_BIN,shutil.which('google-chrome'),
                shutil.which('chromium'),shutil.which('chromium-browser')) if path and Path(path).is_file()),None)
            assert executable is not None,'real Chrome executable required for factory transport'
            session_class = cu_browser.ChromeSession
            # The product targetsMac; LinuxCI supplies its real installed Chrome
            # through ChromeSession's existing executable argument only.
            with pytest.MonkeyPatch.context() as patch:
                patch.setattr(cu_browser,'ChromeSession',lambda profile,**options:
                    session_class(profile,chrome_bin=executable,**options))
                browser.start(contract['start_url'])
            assert isinstance(browser._factory.session,session_class)
            assert browser._factory.session.chrome_bin==executable
        else:
            browser.start(contract['start_url'])
        yield SimpleNamespace(browser=browser,grant=grant,status=status,effects=effects,events=events,
            blocked=blocked,snapshot=lambda:snapshot)
    finally:
        download_directory = browser._qualification_download_dir
        browser.close()
        if download_directory is not None:
            assert not Path(download_directory.name).exists()


def target_link(browser, observation, suffix):
    return next(control.id for control in observation.controls
        if str(browser._control_meta[control.id].get('href') or '').endswith(suffix))


def test_new_tab_and_switch_use_exact_owned_custody_and_preserve_page_history(tmp_path):
    with catalog_browser(tmp_path,'multitab') as fixture:
        browser = fixture.browser
        observation = browser.observe()
        first_tab = browser.qualification_context(observation)['tabs'][0]['id']
        url = fixture.grant.origin + next(rule.path for rule in fixture.grant.rules if rule.path.endswith('/compare/b'))
        action = Action('new_tab',text=url)
        target = cuai_document_target(browser.last_load_evidence,action)
        browser.tab_action(Candidate('new_tab',target,observation.digest()),'bai_newtab_a0',action)
        observation = browser.observe()
        context = browser.qualification_context(observation)
        assert len(context['tabs']) == 2 and browser._page.url == url
        with pytest.raises(PermissionError):
            browser.tab_action(Candidate('switch_tab','t'+'f'*32,observation.digest()),'bai_foreign_a0',
                Action('switch_tab',element_id='t'+'f'*32))
        browser.tab_action(Candidate('switch_tab',first_tab,observation.digest()),'bai_switch_a0',
            Action('switch_tab',element_id=first_tab))
        observation = browser.observe()
        assert observation.location.endswith('/compare/a') and browser.verified_load(observation)
        notes = browser.cuai_readback(observation)
        assert '/compare/a' in notes and '/compare/b' in notes
        assert [item['operation'] for item in fixture.effects] == ['new_tab','switch_tab']


def test_catalog_new_tab_link_keeps_initial_popup_request_load_witness(tmp_path):
    with catalog_browser(tmp_path,'new_tab_link') as fixture:
        browser = fixture.browser
        observation = browser.observe()
        target = target_link(browser,observation,'/compare/a')
        candidate = Candidate('click',target,observation.digest())
        browser.qualification_proposal('bai_popup_a0',candidate,[])
        browser.click(candidate,'bai_popup_a0')
        observation = browser.observe()
        assert observation.location.endswith('/compare/a') and browser.verified_load(observation)
        assert len(browser.qualification_context(observation)['tabs']) == 2


def test_unreviewed_script_popup_is_closed_before_any_route_can_continue(tmp_path):
    with catalog_browser(tmp_path,'new_tab_link') as fixture:
        browser = fixture.browser
        browser._page.evaluate("url => window.open(url)",fixture.grant.origin + fixture.grant.rules[-1].path)
        browser._page.wait_for_timeout(150)
        assert browser._uncertain and len(browser._context.pages) == 1
        assert len(fixture.events) == 1


def test_reviewed_iframe_fact_has_source_custody_and_unreviewed_frame_is_blocked(tmp_path):
    with catalog_browser(tmp_path,'iframe') as fixture:
        browser = fixture.browser
        observation = browser.observe()
        context = browser.qualification_context(observation)
        assert context['frames'] == [{key:getattr(fixture.grant.frames[0],key)
            for key in ('document_path','parent_document_path','frame_id')}]
        notes = browser.cuai_readback(observation)
        assert 'Frame ' + fixture.grant.frames[0].frame_id in notes
        assert 'Badge' in notes or 'badge' in notes
        initial_count = len(fixture.events)
        browser._page.evaluate("url => { const f=document.createElement('iframe'); f.id='foreign-frame'; f.src=url; document.body.append(f); }",
            fixture.grant.origin + fixture.grant.frames[0].document_path)
        browser._page.wait_for_timeout(150)
        assert len(fixture.events) == initial_count and fixture.blocked
        observation = browser.observe()
        assert len(browser.qualification_context(observation)['frames']) == 1
        frame = next(frame for frame in browser._page.frames if frame.url.endswith('/frame-inner'))
        frame.evaluate("document.body.id='foreign-document'")
        observation = browser.observe()
        assert browser.qualification_context(observation)['frames'] == []
        assert not any(fact.startswith('Frame ') for fact in browser._cuai_current['source']['facts'])


def test_hover_only_control_reveals_link_without_click_authority(tmp_path):
    def hover_grant(grant):
        return replace(grant,allowed_operations=tuple(dict.fromkeys(grant.allowed_operations+('hover',))))
    with catalog_browser(tmp_path,'multitab',grant_change=hover_grant) as fixture:
        browser = fixture.browser
        browser._page.evaluate("""url => { const holder=document.createElement('div');holder.innerHTML=
            '<style>#q-support a{display:none}#q-support:hover a{display:block}</style>'+
            '<div id=q-support tabindex=0>Support<a href="'+url+'">Help</a></div>';document.body.append(holder); }""",
            fixture.grant.origin+fixture.grant.rules[-1].path)
        observation = browser.observe()
        target = next(control.id for control in observation.controls if control.label == 'Support')
        with pytest.raises(PermissionError):
            browser.click(Candidate('click',target,observation.digest()),'bai_badclick_a0')
        browser.hover(Candidate('hover',target,observation.digest()),'bai_hover_a0')
        observation = browser.observe()
        assert 'Help' in [control.label for control in observation.controls]
        assert fixture.effects[-1]['operation'] == 'hover'


def test_exact_catalog_csv_is_saved_privately_and_returned_as_bounded_metadata(tmp_path):
    with catalog_browser(tmp_path,'download') as fixture:
        browser = fixture.browser
        observation = browser.observe()
        target = target_link(browser,observation,'/download')
        url = browser._control_meta[target]['href']
        browser.click(Candidate('navigate',target,observation.digest()),'bai_csv_a0',navigate_url=url)
        evidence = browser.last_qualification_download
        assert evidence['sha256'] == fixture.grant.downloads[0].sha256
        assert evidence['size_bytes'] == fixture.grant.downloads[0].max_bytes
        assert evidence['media_type'] == 'text/csv' and evidence['rows_excluding_header'] == 2
        assert set(Path(browser._qualification_download_dir.name).iterdir()) == {
            Path(browser._qualification_download_dir.name)/fixture.grant.downloads[0].filename}
        observation = browser.observe()
        assert browser.verified_load(observation)
        notes = browser.cuai_readback(observation)
        assert evidence['sha256'] in notes and browser._qualification_download_dir.name not in notes
        assert not browser._qualification_download_pending


@pytest.mark.parametrize('factory',[False,True])
def test_long_csv_known_503_recovery_does_not_freeze_or_mutate_and_then_saves_exact_file(tmp_path,factory):
    with catalog_browser(tmp_path,'l06_recover_from_error_page',factory=factory) as fixture:
        browser = fixture.browser
        observation = browser.observe()
        target = target_link(browser,observation,'/reports/q-download')
        url = browser._control_meta[target]['href']
        browser.click(Candidate('navigate',target,observation.digest()),'bai_report_1_a0',navigate_url=url)
        for attempt in (2,3):
            observation = browser.observe()
            assert browser.qualification_recoverable_load(observation) and not browser.verified_load(observation)
            assert not observation.controls and not observation.reviewed_text
            assert '503' in browser.cuai_readback(observation)
            assert browser.qualification_context(observation)['controls'] == []
            action = Action('navigate',text=url)
            browser.document_action(Candidate('navigate',cuai_document_target(browser.last_load_evidence,action),
                observation.digest()),f'bai_report_{attempt}_a0',action)
        evidence = browser.last_qualification_download
        assert evidence['sha256'] == fixture.grant.downloads[0].sha256
        assert evidence['size_bytes'] == fixture.grant.downloads[0].max_bytes
        assert not browser._uncertain and not browser._qualification_download_pending
        assert [event[2] for event in fixture.events if event[1].endswith('/reports/q-download')] == [503,503,200]
        assert all(event[0] == 'GET' for event in fixture.events)
        observation = browser.observe()
        action = Action('navigate',text=fixture.grant.origin+next(rule.path for rule in fixture.grant.rules if rule.path.endswith('/reports')))
        browser.document_action(Candidate('navigate',cuai_document_target(browser.last_load_evidence,action),
            observation.digest()),'bai_reports_return_a0',action)
        observation = browser.observe()
        assert browser.verified_load(observation) and evidence['sha256'] in browser.cuai_readback(observation)


@pytest.mark.parametrize('change',['mime','body','filename'])
def test_download_rejects_other_mime_bytes_or_filename_and_deletes_native_artifact(tmp_path,change):
    from coding_agents.computer_use_gate import EffectUncertain
    def altered(response,rule):
        if response.media_type != 'text/csv':
            return response
        return replace(response,media_type='text/plain' if change=='mime' else response.media_type,
            body=response.body+b'bad' if change=='body' else response.body,
            headers=(('Content-Disposition','attachment; filename="other.csv"'),) if change=='filename' else response.headers)
    with catalog_browser(tmp_path,'download',response_change=altered) as fixture:
        browser = fixture.browser
        observation = browser.observe()
        target = target_link(browser,observation,'/download')
        with pytest.raises(EffectUncertain):
            browser.click(Candidate('navigate',target,observation.digest()),'bai_badcsv_a0',
                navigate_url=browser._control_meta[target]['href'])
        assert browser._uncertain and browser.last_qualification_download is None


@pytest.mark.parametrize('task_id',['form_valid','form_recover'])
def test_real_catalog_form_roundtrip_joins_303_and_known_rejection_once(tmp_path,task_id):
    with catalog_browser(tmp_path,task_id) as fixture:
        browser = fixture.browser
        post_rules = [rule for rule in fixture.grant.rules if rule.method == 'POST']
        for index,rule in enumerate(post_rules):
            if index:
                observation = browser.observe()
                assert browser.qualification_recoverable_load(observation) and not observation.controls
                action = Action('navigate',text=fixture.grant.origin+rule.document_path)
                browser.document_action(Candidate('navigate',cuai_document_target(browser.last_load_evidence,action),
                    observation.digest()),'bai_form_recover_a0',action)
            for field in rule.fields:
                if field.input_value is None:
                    continue
                observation = browser.observe()
                target = next(control.id for control in observation.controls
                    if 'type' in control.operations and browser._control_meta[control.id]['source_control_id'] == field.control_id) if any(
                        'type' in control.operations and browser._control_meta[control.id]['source_control_id'] == field.control_id
                        for control in observation.controls) else None
                if target is not None:
                    browser.type_text(Candidate('type',target,observation.digest(),
                        value_index=observation.reviewed_text.index(field.input_value)),f'bai_form_fill_{index}_a0',field.input_value)
            observation = browser.observe()
            target = next(control.id for control in observation.controls
                if browser._control_meta[control.id]['source_control_id'] == rule.control_id)
            candidate = Candidate('click',target,observation.digest())
            invocation = f'bai_form_roundtrip_{index}_a0'
            browser.qualification_proposal(invocation,candidate,[rule.rule_id])
            browser.click(candidate,invocation)
            receipt = browser.last_qualification_response
            assert receipt['status_code'] == (400 if rule.rejected_retry_limit else 303)
            assert receipt['mutated'] is (not bool(rule.rejected_retry_limit))
        observation = browser.observe()
        assert browser.verified_load(observation) and observation.location.endswith('/form/done')
        assert len([event for event in fixture.events if event[0] == 'POST']) == len(post_rules)
