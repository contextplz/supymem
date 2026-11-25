"""Core's terminal actor parser preserves explicit failure while closing effect routes."""
import json
import threading
import time

import pytest

from desktop_runtime.cu_owned_browser_adapter import CuOwnedBrowserAdapter
from tests.unit.product.test_cu_owned_browser_adapter import Browser,Broker,actor,profile


@pytest.fixture
def ai():
    from tests.company_os_harness import load_company_os_app
    load_company_os_app(reset=False)
    from app.build import device_browser_ai
    return device_browser_ai


def test_rejected_done_then_terminal_fail_roundtrips_real_core_parser_and_preserves_answer(ai):
    class CoreParsingBroker(Broker):
        def browser_ai_call(self,invocation,phase,observation,**kwargs):
            result=super().browser_ai_call(invocation,phase,observation,**kwargs)
            if phase=='actor':
                assert ai._candidate(result['reply'],observation,{'execution_profile':profile(),
                    'allow_type':True,'fill_values':{'name':'Ada'}},kwargs['context']['terminal_only'],
                    current_load=kwargs['current_load']) is None
            return result
    summary='I could not verify this answer against the reviewed source.'
    browser=Browser()
    broker=CoreParsingBroker([{'subgoals':['Read the offer']},actor('done',summary='The price is $7'),
        {'passed':False,'reason':'The source shows $23'},actor('fail',summary=summary)])
    adapter=CuOwnedBrowserAdapter(browser=browser,gateway=broker,profile=profile(),goal=browser.goal,
        stop=threading.Event(),deadline=time.monotonic()+60)
    proposal=adapter.decide(browser.observe())
    assert proposal.terminal_operation=='fail' and proposal.summary==summary
    assert proposal.action.summary==adapter.answer==summary
    assert [phase for phase,_,_ in broker.calls]==['plan','actor','verify','actor']
    assert broker.calls[-1][2]['context']['terminal_only'] is True
    assert adapter.terminal_only and not adapter.pending_dispatch and browser.deliveries==[]


@pytest.mark.parametrize('action',[{'op':'click','element_id':'c0'},
    {'op':'type','element_id':'c0','text':'Ada'}])
def test_terminal_only_still_refuses_otherwise_valid_effect_before_child_proposal(ai,action):
    browser=Browser()
    observation=browser.observe()
    general={'execution_profile':profile(),'allow_type':True,'fill_values':{'name':'Ada'}}
    reply=json.dumps({'actions':[action],'memory':'','subgoal_done':False})
    assert ai._candidate(reply,observation,general,False) is not None
    with pytest.raises(ValueError,match='device_browser_ai_terminal_effect_refused'):
        ai._candidate(reply,observation,general,True)
    assert browser.deliveries==[]
