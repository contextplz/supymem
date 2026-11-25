"""Owned PG + real isolated Chrome + actual Core phase/effect gates; provider replies stay local."""
import json
from types import SimpleNamespace
from urllib.parse import urlsplit
from uuid import uuid4

import pytest

from tests.integration.test_postgres_auth_security import pg_database as pg_database  # noqa: F401
from tests.integration.test_postgres_journey_jobs import journey_db as journey_db  # noqa: F401
from tests.integration.test_postgres_browser_qualification import seeded_trial
from tests.unit.product.test_device_browser_ai import response


@pytest.mark.parametrize('task',['download','l06_recover_from_error_page'])
def test_real_browser_csv_notes_cross_core_actor_verify(journey_db,monkeypatch,tmp_path,task):
    from app.build import device_browser_ai as ai,device_effects as effects,device_invocations as calls
    from coding_agents.jev_decision import Candidate
    from coding_agents.owned_browser import OwnedBrowser
    from desktop_runtime.cu_owned_browser_adapter import CuOwnedBrowserAdapter
    from desktop_runtime.device_gateway import WorkOrderGateway
    import threading
    import time
    d=journey_db
    if task=='download':
        from app.build import browser_qualification
        token_hex=browser_qualification.secrets.token_hex
        monkeypatch.setattr(browser_qualification.secrets,'token_hex',lambda size:
            'c10e99f57d45c106b22fab4575236503064ccad1b5787507' if size==24 else token_hex(size))
    # Keep the card-shaped owned nonce regression; this happy-path CSV needs
    # a reproducible non-sensitive filename. Restore the catalog hook before
    # the browser and its threads start; no production guard is changed.
    with monkeypatch.context() as fixture_seed:
        if task=='download':
            from core import browser_qualification_catalog as catalog
            new_fixture_state=catalog.new_fixture_state
            fixture_seed.setattr(catalog,'new_fixture_state',lambda task_id,seed,base_path:
                new_fixture_state(task_id,'ab12cd34ef56ab78' if task_id=='download' else seed,base_path))
        service,orders,caller,_,grant,args=seeded_trial(d,monkeypatch,task_id=task)
    saved=orders.retrieve(*args)
    profile=saved['manifest']['general_browser_verification']['execution_profile']
    def request(method,path,body):
        if path.endswith('/authority'):
            calls.check_current(*args)
            return {'current':True}
        if '/browser-qualification/' in path:
            if '/requests/' in path:
                return service.request_status(d['vid'],caller,grant.trial_id,path.rsplit('/',1)[-1])
            return service.order_status(d['vid'],caller,grant.trial_id,grant.order_id)
        if path.endswith('/effects/admit'):
            return {'admitted_now':effects.admit(*args,invocation_id=body['invocation_id'],
                observation_digest=body['observation_digest'],effect_digest=body['effect_digest'])}
        assert method=='GET'
        return orders.retrieve(*args)
    gateway=WorkOrderGateway(venture_id=d['vid'],order_id=grant.order_id,device_id=grant.device_id,
        account_id=grant.actor_id,request=request)
    gateway.claimed=True
    gateway.execution_id=saved['execution_id']
    gateway.review_digest=grant.review_digest
    gateway.browser_cuai_profile_digest=grant.profile_digest
    admitted=[]
    def admit(effect,sha):
        allowed=gateway.admit_effect(effect['invocation'],effect['observation'],sha)
        if allowed:
            admitted.append((effect['invocation'],sha))
        return allowed
    browser=OwnedBrowser(order_id=grant.order_id,goal=saved['manifest']['instruction'],origins=[grant.origin],
        authorize=lambda *_:bool(gateway.current()),admit_effect=admit,general_mode=True,cuai_mode=True,
        bounded_reads=True,profile_dir=tmp_path/'isolated-chrome',qualification_grant=grant.to_dict(),
        qualification_current=gateway.qualification_current,qualification_gateway=gateway)
    actual_route=browser._route
    source_requests=[]
    def offline_route(route):
        def fetch(**kwargs):
            assert kwargs['max_redirects']==kwargs['max_retries']==0 and route.request.method=='GET'
            parsed=urlsplit(route.request.url)
            result,ident=service.receive_fixture_request(grant.nonce,path=parsed.path,query=parsed.query,method='GET')
            source_requests.append((route.request.url,result.status))
            headers={key.lower():value for key,value in result.headers}
            headers.update({'content-type':result.media_type,'content-length':str(len(result.body)),
                'x-supymem-qualification-request':ident})
            service.record_response_delivery(grant.nonce,ident)
            return SimpleNamespace(status=result.status,headers=headers,body=lambda:result.body,dispose=lambda:None)
        actual_route(SimpleNamespace(request=route.request,fetch=fetch,fulfill=route.fulfill,abort=route.abort))
    browser._route=offline_route
    adapter=CuOwnedBrowserAdapter(browser=browser,gateway=gateway,profile=profile,goal=browser.goal,
        stop=threading.Event(),deadline=time.monotonic()+120)
    action={}
    provider_calls=[]
    def provider(body):
        provider_calls.append(body)
        reply=({'passed':True,'reason':'Exact emitted CSV source'} if action.get('phase')=='verify' else
            {'actions':[action['value']],'memory':'','subgoal_done':False})
        return response(json.dumps(reply))
    monkeypatch.setattr(ai,'_provider_call',provider)
    def phase(phase_name,observed,value=None):
        action.update(phase=phase_name,value=value)
        context=adapter._context(observed,claim='Saved exact reviewed CSV' if phase_name=='verify' else '')
        load={key:browser.last_load_evidence[key] for key in (
            'qualified','session_id','page_id','document_id','sequence','location','observation_digest',
            'ready_state','visible_text','visibility_scope','response')}
        if browser.last_load_evidence.get('kind'):
            load['kind']=browser.last_load_evidence['kind']
        packet=ai.BrowserAIBody(invocation_id='bai_'+uuid4().hex,phase=phase_name,
            execution_profile_digest=grant.profile_digest,checkpoint_digest='e'*64,
            observation=observed,current_load=load,context=context)
        return ai.run(*args,request=packet)
    def navigate(observed,url):
        result=phase('actor',observed,{'op':'navigate','text':url})
        proposal=result['effect_proposals'][0]
        candidate=Candidate(**proposal['candidate'])
        browser.qualification_proposal(proposal['invocation_id'],candidate,[])
        if str(candidate.target).startswith('document_'):
            from coding_agents.cu_ai_loop import Action
            browser.document_action(candidate,proposal['invocation_id'],Action('navigate',text=url))
        else:
            browser.click(candidate,proposal['invocation_id'],navigate_url=url)
        ident,sha=admitted[-1]
        effects.receipt(*args,invocation_id=ident,effect_digest=sha,outcome='returned')
        child=orders.retrieve(*args)['invocations'][ident]
        assert child['qualification_read_url']==url and child['execution_id']==saved['execution_id']
        return browser.observe()
    try:
        browser.start(saved['manifest']['general_browser_verification']['start_url'])
        observed=browser.observe()
        url=grant.origin+next(rule.path for rule in grant.rules if rule.rule_id==grant.downloads[0].rule_id)
        observed=navigate(observed,url)
        if task=='l06_recover_from_error_page':
            assert browser.qualification_recoverable_load(observed) and browser.last_qualification_download is None
            observed=navigate(observed,url)
            assert browser.qualification_recoverable_load(observed)
            observed=navigate(observed,url)
            assert browser.last_qualification_download is not None
            observed=navigate(observed,saved['manifest']['general_browser_verification']['start_url'])
        assert browser.verified_load(observed)
        notes=browser.cuai_readback(observed)
        assert '"download"' in notes
        if task=='l06_recover_from_error_page':
            assert '"failed_download"' in notes
        answer='Saved exact reviewed CSV with '+str(browser.last_qualification_download['rows_excluding_header'])+' rows.'
        phase('actor',observed,{'op':'done','summary':answer})
        verified=phase('verify',browser.observe())
        assert json.loads(verified['reply'])['passed'] is True and verified['task_verified'] is False
        assert [status for uri,status in source_requests if uri==url]==([200] if task=='download' else [503,503,200])
        assert len(provider_calls)==(3 if task=='download' else 6)
    finally:
        browser.close()
