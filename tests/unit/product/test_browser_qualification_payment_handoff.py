"""Protected payment stops retain one exact delivered non-secret total and no input authority."""
import hashlib

import pytest

from tests.unit.product.test_browser_qualification_capabilities import catalog_browser


def payment_observation(fixture):
    browser=fixture.browser
    path=next(rule.path for rule in fixture.grant.rules if rule.path.endswith('/shop2/payment'))
    browser._page.goto(fixture.grant.origin+path,wait_until='load')
    observation=browser.observe()
    assert browser.pause_kind=='payment' and observation.confirmation_pending
    assert observation.controls==[] and browser.verified_load(observation)
    return observation


def test_current_payment_total_comes_from_exact_delivered_source_without_field_values(tmp_path):
    with catalog_browser(tmp_path,'l03_mini_shop_stop_at_payment') as fixture:
        browser=fixture.browser
        observation=payment_observation(fixture)
        browser._page.locator('input[name=cardnumber]').fill('4111111111111111')
        observation=browser.observe()
        assert browser.qualification_payment_fact(observation)=='Order total: $0'
        assert browser._qualification_download_pending is None and browser.last_qualification_download is None
        assert not any(method=='POST' for method,_,_ in fixture.events)
        # $0 is the actually observed empty-cart source fact, never the requested
        # cart's controller-known eventual total or a sensitive field value.
        assert not fixture.effects


@pytest.mark.parametrize('case',['unverified','duplicate','secret','mismatched_receipt'])
def test_payment_handoff_withholds_unknown_ambiguous_or_sensitive_total(tmp_path,case):
    with catalog_browser(tmp_path,'l03_mini_shop_stop_at_payment') as fixture:
        browser=fixture.browser
        observation=payment_observation(fixture)
        if case=='unverified':
            browser.last_load_evidence['qualified']=False
        elif case=='duplicate':
            browser._cuai_current['source']['facts'].append('Cart total: $99')
        elif case=='secret':
            browser._cuai_current['source']['facts']=['Card number: 4111111111111111','Order total: $[REDACTED]']
        else:
            proof=browser._load_pages[browser._page]['qualification_response']
            browser._qualification_gateway._fixture_read_receipts[proof['request_id']]['response_digest']=hashlib.sha256(b'other').hexdigest()
            with pytest.raises(PermissionError):
                browser.qualification_payment_fact(observation)
            return
        assert browser.qualification_payment_fact(observation) is None
        assert not fixture.effects


@pytest.mark.parametrize('fact',['Order total: $43',None,'unconfirmed'])
def test_worker_payment_stop_keeps_source_total_without_any_provider_or_input(monkeypatch,fact):
    from coding_agents.browser_pause import founder_prompt
    from tests.unit.product import test_browser_qualification_worker_dispatch as dispatch
    class PaymentBrowser(dispatch.QualifiedBrowser):
        def __init__(self,**kwargs):
            super().__init__(**kwargs)
            self.pause_kind='payment'
            self.founder_prompt=founder_prompt('payment')
            self.fact_reads=0
        def qualification_payment_fact(self,observation):
            self.fact_reads+=1
            if fact=='unconfirmed':
                raise PermissionError('browser_qualification_receipt_unconfirmed')
            return fact
    monkeypatch.setattr(dispatch,'QualifiedBrowser',PaymentBrowser)
    worker,browser,broker=dispatch.worker_run(monkeypatch,[])
    expected=(fact+'. ' if fact=='Order total: $43' else '')+founder_prompt('payment')
    assert worker.last['founder_prompt']==worker.last['result_summary']==expected
    assert worker.last['evidence']['terminal_operation']=='needs_you'
    assert worker.last['evidence']['outcome']=='attention_required'
    assert worker.last['evidence']['answer_sha256']==hashlib.sha256(expected.encode()).hexdigest()
    assert browser.fact_reads==1 and broker.calls==broker.effects==[] and browser.closed
