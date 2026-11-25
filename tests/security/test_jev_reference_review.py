"""Advisory inference is authenticated, budgeted and never an action dispatcher."""
import copy
import json
from concurrent.futures import ThreadPoolExecutor
import pytest
from sqlalchemy import select
from tests.security.test_connector_oauth_boundary import client as client
from tests.security.test_durable_journey_boundary import request, venture


@pytest.fixture
def evaluator(client, monkeypatch):
    from app.knowledge import jev
    monkeypatch.setenv('JEV_REFERENCE_REVIEW_ENABLED','1')
    monkeypatch.setenv('TYPESAFE_API_KEY','test-secret-not-for-network')
    calls=[]
    def provider(body):
        calls.append(body)
        labels=list(body['questions']['reference']['criteria'])
        return {'choice':'case_1','probabilities':{k:float(k=='case_1') for k in labels},
                'usage':{'input_tokens':500,'output_tokens':50},'estimated_provider_cost_usd':'0.000021'}
    monkeypatch.setattr(jev,'provider_call',provider)
    return jev,calls


def test_review_authority_cache_citations_and_receipt(client,evaluator):
    jev,calls=evaluator
    vid,wid,_=venture(client)
    path=f'/ventures/{vid}/knowledge/review'
    query='Developers never connect API; hands on setup and onboarding'
    assert request(client,'POST',path,subject='usr_bob',json={'query':query}).status_code==404
    assert client.post(path,json={'query':query}).status_code in {401,403}
    assert calls==[]
    a=request(client,'POST',path,json={'query':query})
    assert a.status_code==200,a.text
    assert a.json()['advisory']['assessment']=='selected'
    assert a.json()['advisory']['founder_credits_charged']==0
    assert a.json()['items'][0]['id']==a.json()['advisory']['selected_id']
    assert a.json()['items'][0]['fields'][0]['citation']
    b=request(client,'POST',path,json={'query':query})
    assert b.json()['advisory']['cached'] is True and len(calls)==1
    assert request(client,'POST',path,json={'query':query,'model':'unbounded-model'}).status_code==422
    from app.db.models import EventLog
    from app.state import db
    with db()() as session:
        receipts=list(session.scalars(select(EventLog).where(EventLog.topic.like('knowledge.jev.%'))))
        assert len(receipts)==2
        assert all((r.workspace_id,r.venture_id)==(wid,vid) for r in receipts)
        assert all(query not in r.payload_json and 'test-secret' not in r.payload_json for r in receipts)
    c=request(client,'POST',path,json={'query':query+' changed'})
    assert c.json()['advisory']['reason']=='allowance_or_cooldown' and len(calls)==1


def test_timeout_and_receipt_failure_prevent_repeat_spend(client,evaluator,monkeypatch):
    jev,calls=evaluator
    vid,wid,_=venture(client)
    path=f'/ventures/{vid}/knowledge/review'
    def timeout(body):
        calls.append(body)
        raise TimeoutError('do not expose query or credential')
    monkeypatch.setattr(jev,'provider_call',timeout)
    assert request(client,'POST',path,json={'query':'poor photos listings'}).json()['advisory']['assessment']=='unavailable'
    assert request(client,'POST',path,json={'query':'poor photos listings'}).json()['advisory']['assessment']=='unavailable'
    assert len(calls)==1
    assert calls[0]['model']==jev.MODEL
    assert calls[0]['questions']['reference']['instructions']==jev.INSTRUCTIONS


def test_no_admission_receipt_means_no_provider_request(client,evaluator,monkeypatch):
    jev,calls=evaluator
    vid,wid,_=venture(client)
    monkeypatch.setattr(jev,'_event',lambda *args,**kwargs: (_ for _ in ()).throw(RuntimeError('storage down')))
    result=request(client,'POST',f'/ventures/{vid}/knowledge/review',json={'query':'manual developer API onboarding'})
    assert result.json()['advisory']['reason']=='receipt_unavailable' and calls==[]


def test_five_founders_get_distinct_receipts_and_global_cap(client,evaluator,monkeypatch):
    jev,calls=evaluator
    scopes=[venture(client,subject=f'usr_founder_{i}') for i in range(5)]
    def run(i):
        vid,wid,_=scopes[i]
        return request(client,'POST',f'/ventures/{vid}/knowledge/review',subject=f'usr_founder_{i}',json={'query':'manual developer API onboarding'})
    with ThreadPoolExecutor(max_workers=5) as pool:
        results=list(pool.map(run,range(5)))
    assert all(r.status_code==200 and r.json()['advisory']['assessment']=='selected' for r in results)
    assert len(calls)==5
    monkeypatch.setattr(jev,'GLOBAL_DAILY_LIMIT',5)
    vid,_,_=venture(client,subject='usr_sixth')
    r=request(client,'POST',f'/ventures/{vid}/knowledge/review',subject='usr_sixth',json={'query':'manual developer API onboarding'})
    assert r.json()['advisory']['reason']=='allowance_or_cooldown' and len(calls)==5


def test_response_validation_unknown_choices_model_usage_and_probability(client):
    from app.knowledge import jev,pmf
    body=jev.request_body('poor photos listings',pmf.search('poor photos listings',limit=6,kind='historical_claim'))
    expected=body['questions']['reference']['criteria']
    value={'model':jev.MODEL,'answers':{'reference':{'type':'choice','choice':'none',
        'probabilities':{k:float(k=='none') for k in expected}}},'usage':{'input_tokens':100,'output_tokens':10}}
    assert jev.validate_response(value,body)['choice']=='none'
    mutations=[lambda x:x.update(model='jev-latest'),lambda x:x.update(answers=[]),
      lambda x:x['answers']['reference'].update(choice='execute_payment'),
      lambda x:x['answers']['reference']['probabilities'].update(none=float('nan')),
      lambda x:x['answers']['reference']['probabilities'].update(none=True),
      lambda x:x['answers']['reference']['probabilities'].update(none=.4),
      lambda x:x['usage'].update(input_tokens=999999),lambda x:x.update(usage=[])]
    for mutate in mutations:
        bad=copy.deepcopy(value);mutate(bad)
        with pytest.raises(ValueError):jev.validate_response(bad,body)


def test_disabled_model_keeps_local_retrieval_available(client,evaluator,monkeypatch):
    jev,calls=evaluator
    monkeypatch.delenv('JEV_REFERENCE_REVIEW_ENABLED')
    vid,_,_=venture(client)
    r=request(client,'POST',f'/ventures/{vid}/knowledge/review',json={'query':'poor photos listings'})
    assert r.status_code==200 and r.json()['items'] and r.json()['advisory']['reason']=='not_enabled'
    assert calls==[]


def test_provider_transport_is_fixed_bounded_and_validates_response(client, monkeypatch):
    import httpx
    from app.knowledge import jev,pmf
    monkeypatch.setenv('TYPESAFE_API_KEY','unit-only-key')
    body=jev.request_body('poor photos listings',pmf.search('poor photos listings',limit=6,kind='historical_claim'))
    requests=[]
    def respond(req):
        requests.append(req)
        assert str(req.url)==jev.ENDPOINT and req.headers['authorization']=='Bearer unit-only-key'
        assert 'unit-only-key' not in req.content.decode()
        labels=body['questions']['reference']['criteria']
        return httpx.Response(200,json={'model':jev.MODEL,'answers':{'reference':{'type':'choice','choice':'none',
            'probabilities':{k:float(k=='none') for k in labels}}},'usage':{'input_tokens':100,'output_tokens':10,'unexpected':'not retained'}})
    original=httpx.Client
    def factory(**kwargs):
        assert kwargs['follow_redirects'] is False and kwargs['trust_env'] is False
        return original(transport=httpx.MockTransport(respond),**kwargs)
    monkeypatch.setattr(jev.httpx,'Client',factory)
    result=jev.provider_call(body)
    assert result['choice']=='none' and len(requests)==1 and 'unexpected' not in result['usage']


def test_lost_result_receipt_does_not_retry_provider(client,evaluator,monkeypatch):
    jev,calls=evaluator
    vid,wid,_=venture(client)
    original=jev._event
    def event(session, **kwargs):
        if kwargs['topic']=='knowledge.jev.result':raise RuntimeError('storage down')
        original(session,**kwargs)
    monkeypatch.setattr(jev,'_event',event)
    path=f'/ventures/{vid}/knowledge/review'
    a=request(client,'POST',path,json={'query':'poor photos listings'})
    b=request(client,'POST',path,json={'query':'poor photos listings'})
    assert a.json()['advisory']['reason']=='result_receipt_unavailable'
    assert b.json()['advisory']['reason']=='already_attempted' and len(calls)==1


@pytest.mark.parametrize('choice,probability,assessment', [
    ('none', 1., 'no_close_match'), ('case_1', .8, 'uncertain'),
])
def test_abstention_keeps_original_reference_order(client,evaluator,monkeypatch,choice,probability,assessment):
    jev,calls=evaluator
    from app.knowledge import pmf
    vid,_,_=venture(client)
    query='business sales pricing willingness to pay for enterprise software'
    original=pmf.recommend(query,limit=6,kind='historical_claim')
    def response(body):
        labels=body['questions']['reference']['criteria']
        return {'choice':choice,'probabilities':{k:probability if k==choice else (1-probability)/(len(labels)-1) for k in labels},
            'usage':{'input_tokens':100,'output_tokens':10},'estimated_provider_cost_usd':'0.0000042'}
    monkeypatch.setattr(jev,'provider_call',response)
    result=request(client,'POST',f'/ventures/{vid}/knowledge/review',json={'query':query}).json()
    assert result['advisory']['assessment']==assessment and result['advisory']['selected_id'] is None
    assert [r['id'] for r in result['items']]==[r['id'] for r in original[:3]]


def test_concurrent_duplicate_requests_make_one_provider_call(client,evaluator):
    jev,calls=evaluator
    vid,_,_=venture(client)
    def run(_):
        return request(client,'POST',f'/ventures/{vid}/knowledge/review',json={'query':'manual developer API onboarding'})
    with ThreadPoolExecutor(max_workers=5) as pool:
        results=list(pool.map(run,range(5)))
    assert all(r.status_code==200 for r in results)
    assert len(calls)==1
    assert any(r.json()['advisory']['assessment']=='selected' for r in results)


def test_all_workbook_content_keeps_playbooks_and_cases(client):
    from app.knowledge import pmf
    vid, _, _ = venture(client)
    path = f'/ventures/{vid}/knowledge'
    query = 'buyer price interview'
    expected = pmf.search(query, limit=6, kind=None)
    assert any(row['kind'] == 'playbook_guidance' for row in expected)
    assert any(row['kind'].startswith('historical_claim') for row in expected)

    # The UI's All workbook content choice omits kind entirely. Explicit
    # kind= is accepted as the same unfiltered choice for API callers.
    for params in ({'q': query, 'limit': 6}, {'q': query, 'kind': '', 'limit': 6}):
        response = request(client, 'GET', path, params=params)
        assert response.status_code == 200, response.text
        assert [row['id'] for row in response.json()['items']] == [row['id'] for row in expected]
