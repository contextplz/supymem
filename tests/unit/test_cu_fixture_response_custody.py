"""Actual HTTP response recording synchronization never repeats delivery."""
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts/desktop'))
from cu_browser_suite import Fx, wizard_verification


@pytest.fixture
def barrier_fixture():
    flushed, release = threading.Event(), threading.Event()
    class Fixture(Fx):
        def post(self, handler, path, body):
            if path == '/chain/3':
                original = handler.wfile.flush
                def after_actual_flush():
                    original()
                    flushed.set()
                    release.wait(3)
                handler.wfile.flush = after_actual_flush
            return super().post(handler, path, body)
    fixture = Fixture('abcdef12').start()
    try:
        with httpx.Client(trust_env=False, timeout=2) as client:
            for step in (1, 2, 3):
                body = {} if step == 1 else {'prev': fixture.chain_codes[str(step - 1)]}
                assert client.post(fixture.base + '/chain/' + str(step), data=body).status_code == 200
        assert flushed.wait(1)
        with fixture.lock:
            assert len(fixture.posts) == 3 and fixture.post_receipts[-1]['response'] is None
            assert fixture.post_receipts[-1]['chain']['accepted'] is True
        yield fixture, release
    finally:
        release.set()
        fixture.close()
        fixture.thread.join(timeout=2)
        assert not fixture.thread.is_alive()


def test_actual_http_client_can_finish_before_receipt_then_bounded_observer_gets_same_delivery(barrier_fixture):
    fixture, release = barrier_fixture
    timer = threading.Timer(.04, release.set)
    timer.start()
    result = wizard_verification(fixture, {'summary': fixture.chain_codes['3']})
    timer.join(timeout=1)
    assert result['passed'] is True and result['qualified_steps'] == [1, 2, 3]
    assert result['delivered_attempts'] == len(fixture.posts) == 3
    assert fixture.post_receipts[-1]['response']['complete'] is True


def test_actual_http_receipt_timeout_stays_unknown_without_replay(barrier_fixture):
    fixture, _ = barrier_fixture
    start = time.monotonic()
    posts, receipts = fixture.delivered_posts(timeout=.03)
    assert .02 <= time.monotonic() - start < .5
    assert len(posts) == 3 and receipts[-1]['response'] is None
    result = wizard_verification(fixture, {'summary': fixture.chain_codes['3']})
    assert result['passed'] is False and result['failure_code'] == 'wizard_receipt_missing_or_unknown'
    assert len(fixture.posts) == 3 and fixture.post_receipts[-1]['response'] is None


@pytest.mark.parametrize('state', ['missing', 'failed', 'incomplete'])
def test_unknown_or_failed_source_receipt_is_not_inflight_and_is_never_waited_into_success(state):
    fixture = Fx('abcdef34')
    receipt = {'request_id': 1, 'response': None, 'handler_error': None}
    if state == 'failed':
        receipt['handler_error'] = 'BrokenPipeError'
    elif state == 'incomplete':
        receipt['response'] = {'complete': False}
    fixture.posts = [('/chain/1', {})]
    fixture.post_receipts = [receipt]
    if state == 'failed':
        fixture._pending_response_recording.add(1)
    start = time.monotonic()
    posts, receipts = fixture.delivered_posts(timeout=.5)
    assert time.monotonic() - start < .1
    assert posts == fixture.posts and receipts == fixture.post_receipts
    assert wizard_verification(fixture, {'summary': fixture.chain_codes['3']})['passed'] is False


@pytest.mark.parametrize('timeout', [True, -1, 2, float('nan'), float('inf'), '1'])
def test_observation_budget_is_closed(timeout):
    with pytest.raises(ValueError, match='fixture_response_observation_bound_invalid'):
        Fx('abcdef56').delivered_posts(timeout=timeout)


@pytest.mark.parametrize('record', [None, {'request_id': {}}, {'request_id': True}])
def test_malformed_receipt_cannot_be_promoted_by_inflight_observer(record):
    fixture = Fx('abcdef78')
    fixture.posts = [('/chain/1', {})]
    fixture.post_receipts = [record]
    fixture._pending_response_recording.add(1)
    assert fixture.delivered_posts() == (fixture.posts, fixture.post_receipts)
    assert wizard_verification(fixture, {'summary': fixture.chain_codes['3']})['passed'] is False
