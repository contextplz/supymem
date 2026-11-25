"""Actual source readback and Core phase gates, with local saved receipt/state doubles only."""
from contextlib import nullcontext
from dataclasses import replace
import csv
import hashlib
import io
import json
from types import SimpleNamespace

import pytest

from coding_agents.jev_decision import Observation
from coding_agents.owned_browser import OwnedBrowser,cuai_document_target
from coding_agents.cu_ai_loop import Action
from core.browser_qualification import request_binding_digest
from core.browser_qualification_catalog import fixture_contract,fixture_transition,new_fixture_state
from core.canonical import digest
from tests.unit.product.test_browser_qualification_runtime import fixture_order
from tests.unit.product.test_device_browser_ai import configure,response


@pytest.fixture(autouse=True)
def company_os():
    from tests.company_os_harness import load_company_os_app
    load_company_os_app(reset=False)


def emitted_source(tmp_path,monkeypatch,*,task='download',failed=False):
    from app.build import device_browser_ai as ai,browser_qualification as service
    profile=configure(monkeypatch)
    original,body=fixture_order()
    initial=new_fixture_state(task,'abcdef1234','/public/browser-qualification/'+original.nonce)
    contract=fixture_contract(initial)
    grant=replace(original,task_id=task,profile_digest=ai.profile_digest(profile),catalog_digest=contract['catalog_digest'],
        seed_digest=contract['seed_digest'],rules=contract['rules'],allowed_operations=contract['allowed_operations'],
        downloads=contract['downloads'])
    body.update(state='claimed',execution_id='dex_notes',browser_qualification_grant=grant.to_dict(),
        browser_qualification_scope={})
    body.pop('browser_qualification_trial_binding',None)
    body['manifest'].update(task_id='founder_browser_ven_one',title='Reviewed CSV',applications=['com.google.Chrome'],
        credit_ceiling_micro=500000,duration_seconds=600,instruction=contract['goal'],origins=[grant.origin],
        actions=['browser.read','browser.interact'])
    body['manifest']['general_browser_verification'].update(execution_profile=profile.model_dump(),allow_type=False,
        fill_values={},start_url=contract['start_url'],operation='general_goal_v1',max_steps=32)
    current,receipts=initial,{}
    def apply(rule):
        nonlocal current
        transition=fixture_transition(current,rule)
        current,result=transition.state,transition.response
        ident='bqr_notes_'+str(len(receipts))
        receipt={key:getattr(grant,key) for key in ('trial_id','order_id','workspace_id','venture_id','actor_id','device_id')}
        receipt.update(grant_digest=grant.grant_digest,execution_id=body['execution_id'],request_id=ident,rule_id=rule.rule_id,
            request_digest=request_binding_digest(grant,rule,ident),method='GET',path=rule.path,query=rule.query,
            state='failed_read' if result.status>=400 else 'committed',status_code=result.status,mutated=False,
            server_response_sent=True,response_digest=hashlib.sha256(result.body).hexdigest(),
            response_size_bytes=len(result.body),response_mime_type=result.media_type.split(';',1)[0].strip().lower())
        receipts[ident]=receipt
        return receipt,result
    start=next(rule for rule in grant.rules if grant.origin+rule.path==contract['start_url'])
    apply(start)
    read=next(rule for rule in grant.rules if rule.rule_id==grant.downloads[0].rule_id)
    attempts=1 if failed or task=='download' else 3
    outcomes=[apply(read) for _ in range(attempts)]
    wire,result=outcomes[-1]
    location=grant.origin+read.path if failed else contract['start_url']
    observation=Observation(revision='source_notes',application='com.google.Chrome',location=location,goal=contract['goal'],
        context='503 Service unavailable' if failed else 'Report source',controls=[],reviewed_text=[],permitted=True,
        confirmation_pending=False,prior_effect_uncertain=False)
    browser=OwnedBrowser(order_id=grant.order_id,goal=contract['goal'],origins=[grant.origin],authorize=lambda *_:True,
        admit_effect=lambda *_:pytest.fail('No native input'),general_mode=True,cuai_mode=True,profile_dir=tmp_path/'unused',
        qualification_grant=grant.to_dict(),qualification_current=lambda:grant)
    class Page:
        url=location
    browser._page=Page()
    load={'qualified':not failed,'session_id':browser.session_id,'page_id':'b'*32,'document_id':'c'*32,'sequence':1,
        'location':location,'observation_digest':observation.digest(),'ready_state':'complete','visible_text':True,
        'visibility_scope':'bounded_dom_text_geometry','response':{'url':location,'status':503 if failed else 200,'sequence':1,'complete':True}}
    error=None
    if task!='download':
        error_wire=outcomes[0][0]
        error={'url':grant.origin+read.path,'status':503,'complete':True,'sequence':1,'request_id':error_wire['request_id'],
            'sha256':error_wire['response_digest'],'size_bytes':error_wire['response_size_bytes'],'mime_type':'text/html'}
        browser.last_qualification_read_response=error
    if failed:
        load['kind']='qualification_error'
        load['response']=error
    browser.last_load_evidence=load
    browser._load_pages[browser._page]={'page_id':load['page_id'],'sequence':1,'committed_sequence':1,
        'document_id':load['document_id'],'response':load['response']}
    browser._observation=observation
    entry={key:load[key] for key in ('session_id','page_id','document_id','sequence','location','observation_digest')}
    entry['source']={'facts':['503 Service unavailable' if failed else 'Report source'],'fields':[],
        'omitted':{'facts':0,'fields':0,'values':0},'reads':[],'reads_omitted':0}
    browser._cuai_current=entry
    before=digest('source before download')
    child_id='bai_'+'d'*32+'_a0'
    parent_id=child_id.removesuffix('_a0')
    effect_digest=digest('admitted exact CSV input')
    body['invocations']={parent_id:{'id':parent_id,'state':'completed','phase':'actor','source_kind':'browser_cuai_text_v1',
        'profile_digest':grant.profile_digest},child_id:{'id':child_id,'state':'completed','source_kind':'browser_cuai_actor_effect_v1',
        'parent_invocation_id':parent_id,'profile_digest':grant.profile_digest,'no_provider_io':True,'actual_micro':0,'provider_actual_micro':0,
        'execution_id':body['execution_id'],'qualification_read_url':grant.origin+read.path,
        'proposal':{'operation':'navigate','target':'c0','observation_digest':before},
        'effect':{'state':'returned','digest':effect_digest}}}
    if not failed:
        browser.last_qualification_download={'rule_id':read.rule_id,'url':grant.origin+read.path,'filename':grant.downloads[0].filename,
            'media_type':'text/csv','size_bytes':len(result.body),'sha256':hashlib.sha256(result.body).hexdigest(),
            'rows_excluding_header':len(list(csv.reader(io.StringIO(result.body.decode()))))-1,'invocation_id':child_id,
            'effect_digest':effect_digest,'observation_digest':before,'delivery':'input_delivered','request_id':wire['request_id']}
    notes=browser.cuai_readback(observation)
    request=ai.BrowserAIBody(invocation_id='bai_phase',phase='actor',execution_profile_digest=grant.profile_digest,
        checkpoint_digest='e'*64,observation=observation,current_load=load,
        context={'notes':notes,'qualification':{'grant_digest':grant.grant_digest},'bounded_reads':True})
    trial={'requests':receipts,'task_states':{task:{'current':current}}}
    monkeypatch.setattr(service,'_authority',lambda *args:(None,trial))
    monkeypatch.setattr(ai.calls,'_check_executing',lambda *args:None)
    def validate():
        service.validate_runtime_context(None,None,body,request.context.qualification,observation=observation,
            current_url=location,current_load=request.current_load,source_notes=request.context.notes)
    return SimpleNamespace(ai=ai,service=service,body=body,grant=grant,profile=profile,request=request,
        observation=observation,trial=trial,browser=browser,validate=validate,child_id=child_id,wire=wire)


@pytest.mark.parametrize('task',['download','l06_recover_from_error_page'])
def test_actual_emitted_csv_notes_pass_real_core_actor_and_verifier_scoping(tmp_path,monkeypatch,task):
    source=emitted_source(tmp_path,monkeypatch,task=task)
    source.validate()
    for phase in ('actor','verify'):
        source.request.phase=phase
        source.request.context.claim='Saved the exact reviewed CSV'
        profile,observation=source.ai.scoped(source.body,source.request)
        body=source.ai.request_body(source.body,source.request,profile,observation)
        assert 'rows_excluding_header' in json.dumps(body)
    assert source.request.context.qualification._recovery_only is False


def test_actual_failed_download_notes_keep_bounded_recovery_and_real_core_retry_candidate(tmp_path,monkeypatch):
    source=emitted_source(tmp_path,monkeypatch,task='l06_recover_from_error_page',failed=True)
    source.validate()
    profile,observation=source.ai.scoped(source.body,source.request)
    body=source.ai.request_body(source.body,source.request,profile,observation)
    url=source.grant.origin+source.grant.rules[-1].path
    candidate=source.ai._candidate(json.dumps({'actions':[{'op':'navigate','text':url}],'memory':'','subgoal_done':False}),
        observation,source.body['manifest']['general_browser_verification'],False,current_load=source.request.current_load.model_dump(),
        fixture=source.grant,qualification=source.request.context.qualification)
    assert candidate.target==cuai_document_target(source.browser.last_load_evidence,Action('navigate',text=url))
    assert any(url in variant['properties'].get('text',{}).get('enum',[]) for variant in
        body['text']['format']['schema']['properties']['actions']['items']['anyOf'])
    with pytest.raises(ValueError):
        source.ai._candidate(json.dumps({'actions':[{'op':'done','summary':'Saved'}],'memory':'','subgoal_done':False}),
            observation,source.body['manifest']['general_browser_verification'],False,current_load=source.request.current_load.model_dump(),
            fixture=source.grant,qualification=source.request.context.qualification)


@pytest.mark.parametrize('field,value',[('rows_excluding_header',True),('size_bytes',True),('rows_excluding_header',999),
    ('filename','other.csv'),('path','/private/file.csv'),('effect_digest','f'*64),('url','https://foreign.invalid/')])
def test_csv_metadata_cannot_supply_unknown_paths_wrong_bytes_or_unrelated_input(tmp_path,monkeypatch,field,value):
    source=emitted_source(tmp_path,monkeypatch)
    notes=json.loads(source.request.context.notes)
    notes['download'][field]=value
    source.request.context.notes=json.dumps(notes)
    with pytest.raises(ValueError):
        source.validate()


@pytest.mark.parametrize('field,value',[('state','pending'),('state','unknown'),('server_response_sent',False),
    ('execution_id','foreign'),('response_digest','f'*64),('response_mime_type','text/plain'),('response_size_bytes',True)])
def test_csv_source_receipt_custody_must_be_exact_and_delivered(tmp_path,monkeypatch,field,value):
    source=emitted_source(tmp_path,monkeypatch)
    source.wire[field]=value
    with pytest.raises(ValueError):
        source.validate()


def test_unrelated_returned_navigation_child_cannot_claim_the_csv(tmp_path,monkeypatch):
    source=emitted_source(tmp_path,monkeypatch)
    source.body['invocations'][source.child_id]['qualification_read_url']=source.request.current_load.location
    with pytest.raises(ValueError):
        source.validate()


@pytest.mark.parametrize('text',['4111111111111111','sk-'+'a'*32])
def test_valid_artifact_custody_never_exempts_decoded_page_content(tmp_path,monkeypatch,text):
    source=emitted_source(tmp_path,monkeypatch)
    notes=json.loads(source.request.context.notes)
    notes['current']['source']['facts'].append(text)
    source.request.context.notes=json.dumps(notes)
    source.validate()
    with pytest.raises(ValueError):
        source.ai.scoped(source.body,source.request)


def test_scoped_artifact_metadata_cannot_bypass_locked_core_join(tmp_path,monkeypatch):
    source=emitted_source(tmp_path,monkeypatch)
    with pytest.raises(ValueError):
        source.ai.scoped(source.body,source.request)


def phase_gateway(source,monkeypatch,action=None):
    events=[]
    ai=source.ai
    row=SimpleNamespace(provenance_json='',updated_at=None)
    session=SimpleNamespace(commit=lambda:events.append('commit'))
    monkeypatch.setattr(ai.orders,'retrieve',lambda *args:source.body)
    monkeypatch.setattr(ai.orders,'db',lambda:lambda:nullcontext(session))
    monkeypatch.setattr(ai.calls,'_locked',lambda *args:(row,source.body,None))
    def admit(*args,**kwargs):
        ident=kwargs['invocation_id']
        phase=kwargs['browser_phase']['phase']
        source.body.setdefault('invocations',{})[ident]={'id':ident,'state':'calling','phase':phase,
            'source_kind':'browser_cuai_text_v1','profile_digest':source.grant.profile_digest,'request_digest':digest(ident)}
        source.body['browser_ai_latest_invocation_id']=ident
        source.body['browser_ai_source']=source.request.current_load.model_dump()
        events.append('admit')
        return {'admitted_now':True}
    def returned(*args,**kwargs):
        source.body['invocations'][kwargs['invocation_id']]['state']='completed'
        events.append('result')
    monkeypatch.setattr(ai.calls,'admit',admit)
    monkeypatch.setattr(ai.calls,'record_result',returned)
    monkeypatch.setattr(ai.calls,'check_current',lambda *args:None)
    def provider(body):
        events.append('provider_stub')
        reply=({'passed':True,'reason':'Exact source'} if source.request.phase=='verify' else
            {'actions':[action or {'op':'done','summary':'Saved exact CSV'}],'memory':'','subgoal_done':False})
        return response(json.dumps(reply))
    monkeypatch.setattr(ai,'_provider_call',provider)
    return events


@pytest.mark.parametrize('phase',['actor','verify'])
def test_emitted_csv_notes_reach_actual_core_phase_admission_without_real_provider_io(tmp_path,monkeypatch,phase):
    source=emitted_source(tmp_path,monkeypatch,task='l06_recover_from_error_page')
    source.request.phase=phase
    source.request.context.claim='Saved exact CSV'
    events=phase_gateway(source,monkeypatch)
    result=source.ai.run(source.grant.venture_id,object(),source.grant.order_id,request=source.request)
    assert result['reply'] and events==['admit','provider_stub','result']
    assert source.request.context.qualification._recovery_only is False


def test_failed_csv_retry_crosses_actual_core_candidate_and_persists_server_derived_url(tmp_path,monkeypatch):
    source=emitted_source(tmp_path,monkeypatch,task='l06_recover_from_error_page',failed=True)
    url=source.grant.origin+source.grant.rules[-1].path
    events=phase_gateway(source,monkeypatch,{'op':'navigate','text':url})
    result=source.ai.run(source.grant.venture_id,object(),source.grant.order_id,request=source.request)
    assert result['effect_proposals'][0]['candidate']['operation']=='navigate'
    child=source.body['invocations'][result['effect_proposals'][0]['invocation_id']]
    assert child['qualification_read_url']==url and child['execution_id']==source.body['execution_id']
    assert events==['admit','provider_stub','result','commit']
    assert source.request.context.qualification._recovery_only is True


def test_invalid_csv_custody_stops_actual_core_before_admission_or_provider_stub(tmp_path,monkeypatch):
    source=emitted_source(tmp_path,monkeypatch)
    source.wire['execution_id']='foreign'
    events=phase_gateway(source,monkeypatch)
    with pytest.raises(ValueError):
        source.ai.run(source.grant.venture_id,object(),source.grant.order_id,request=source.request)
    assert events==[]


@pytest.mark.parametrize('rule_id',['unrelated_rule','4111111111111111','sk-'+'a'*32],
    ids=['arbitrary-rule','pan-shaped-rule','escaped-secret-rule'])
def test_csv_rule_metadata_must_match_saved_source_before_provider(tmp_path,monkeypatch,rule_id):
    source=emitted_source(tmp_path,monkeypatch)
    notes=json.loads(source.request.context.notes)
    notes['download']['rule_id']=rule_id
    source.request.context.notes=json.dumps(notes).replace('sk-',r'\u0073\u006b\u002d')
    assert json.loads(source.request.context.notes)['download']['rule_id']==rule_id
    events=phase_gateway(source,monkeypatch)
    with pytest.raises(ValueError):
        source.ai.run(source.grant.venture_id,object(),source.grant.order_id,request=source.request)
    assert source.request.context.qualification._source_artifacts_digest is None
    assert events==[]


@pytest.mark.parametrize('field,value',[('qualification_read_url',None),('profile_digest','f'*64),
    ('execution_id','foreign'),('actual_micro',1),('no_provider_io',False),('state','calling')])
def test_csv_child_custody_cannot_be_borrowed_or_relaxed(tmp_path,monkeypatch,field,value):
    source=emitted_source(tmp_path,monkeypatch)
    source.body['invocations'][source.child_id][field]=value
    with pytest.raises(ValueError):
        source.validate()


@pytest.mark.parametrize('state',['admitted','not_dispatched','uncertain'])
def test_csv_notes_require_returned_native_input(tmp_path,monkeypatch,state):
    source=emitted_source(tmp_path,monkeypatch)
    source.body['invocations'][source.child_id]['effect']['state']=state
    with pytest.raises(ValueError):
        source.validate()


def test_failed_download_cannot_borrow_an_unrelated_reviewed_failed_get(tmp_path,monkeypatch):
    source=emitted_source(tmp_path,monkeypatch,task='l06_recover_from_error_page',failed=True)
    unrelated=next(row for row in source.trial['requests'].values() if row['request_id']!=source.wire['request_id'])
    unrelated.update(state='failed_read',status_code=503,response_mime_type='text/html')
    notes=json.loads(source.request.context.notes)
    notes['failed_download'].update(request_id=unrelated['request_id'],url=source.grant.origin+unrelated['path'],
        sha256=unrelated['response_digest'],size_bytes=unrelated['response_size_bytes'])
    source.request.context.notes=json.dumps(notes)
    with pytest.raises(ValueError):
        source.validate()


def test_current_failed_download_sequence_must_match_current_error_proof(tmp_path,monkeypatch):
    source=emitted_source(tmp_path,monkeypatch,task='l06_recover_from_error_page',failed=True)
    notes=json.loads(source.request.context.notes)
    notes['failed_download']['sequence']=2
    source.request.context.notes=json.dumps(notes)
    with pytest.raises(ValueError):
        source.validate()


def test_artifact_notes_cannot_change_after_locked_custody_validation(tmp_path,monkeypatch):
    source=emitted_source(tmp_path,monkeypatch)
    source.validate()
    notes=json.loads(source.request.context.notes)
    notes['download']['rows_excluding_header']+=1
    source.request.context.notes=json.dumps(notes)
    with pytest.raises(ValueError):
        source.ai.scoped(source.body,source.request)


def test_failed_download_history_never_permits_current_error_verifier(tmp_path,monkeypatch):
    source=emitted_source(tmp_path,monkeypatch,task='l06_recover_from_error_page',failed=True)
    events=phase_gateway(source,monkeypatch)
    source.request.phase='verify'
    with pytest.raises(ValueError):
        source.ai.run(source.grant.venture_id,object(),source.grant.order_id,request=source.request)
    assert events==[]
