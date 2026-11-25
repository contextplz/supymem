"""An unconfirmed dispatched effect cannot be erased by SDK/readback exceptions."""
import threading
from types import SimpleNamespace

import pytest

from coding_agents.cu_ai_loop import Caps
from desktop_runtime.cu_ai_cua import CuaError, CuaMac, session_caller
from desktop_runtime.cu_ai_engine import FileCheckpoint, NeedsHuman, run_loop
from desktop_runtime.cua_session import AI_LOOP_TOOLS, CuaSession, CuaSessionError
from tests.unit.test_cu_ai_loop import FakeMac, act, reply


@pytest.mark.parametrize('error', [CuaSessionError('desktop_cua_transport_unavailable'),
    CuaSessionError('desktop_cua_response_invalid'), RuntimeError('PRIVATE provider detail'),
    RuntimeError('desktop_cua_session_unavailable')])
def test_actual_session_caller_transport_throw_has_unconfirmed_phase(error):
    calls = []
    def call(*args, **kwargs):
        calls.append(True)
        raise error
    session = SimpleNamespace(call=call, close=lambda: None)
    with pytest.raises(NeedsHuman) as raised:
        session_caller(session)('press_key', {'key': 'return'})
    assert raised.value.dispatch_state == 'unconfirmed' and calls == [True]
    assert 'PRIVATE' not in str(raised.value) + raised.value.detail


def test_actual_session_known_pre_dispatch_refusal_remains_resumable():
    session = object.__new__(CuaSession)
    session.tools, session._failure = AI_LOOP_TOOLS, 'owned closed session'
    with pytest.raises(NeedsHuman) as raised:
        session_caller(session)('press_key', {'key': 'return'})
    assert raised.value.dispatch_state == 'not_dispatched'


def test_actual_readonly_transport_exception_is_replannable():
    session = SimpleNamespace(call=lambda *a, **kw: (_ for _ in ()).throw(
        RuntimeError('PRIVATE response')), close=lambda: None)
    with pytest.raises(CuaError, match='driver_readonly_failed'):
        session_caller(session)('get_window_state', {'pid': 10, 'window_id': 1})


@pytest.mark.parametrize('stop_after_dispatch', [False, True])
def test_generic_sdk_throw_holds_checkpoint_without_second_mutation(tmp_path, stop_after_dispatch):
    mac = FakeMac([('a', [])])
    stop = threading.Event()
    calls = []
    def dispatched(action, obs):
        calls.append(action.op)
        if stop_after_dispatch:
            stop.set()
        raise RuntimeError('PRIVATE sdk failure after input')
    mac.act = dispatched
    replies = iter([reply(act('key', chord='cmd+s')), reply(act('key', chord='cmd+s')), reply(act('done'))])
    checkpoint = FileCheckpoint(tmp_path / 'owned-checkpoint.json')
    result = run_loop('synthetic goal', lambda p: (next(replies), {}), mac, stop,
                      checkpoint=checkpoint, caps=Caps(max_steps=4, explicit=True), verify=lambda *a: (True, ''))
    assert calls == ['key']
    assert result.reason == ('stopped_by_founder' if stop_after_dispatch else 'effect_reconciliation_required')
    assert checkpoint.load()['pending_dispatch'] is True
    assert 'PRIVATE' not in str(result.log) + result.reason
    resumed = run_loop('synthetic goal', lambda p: pytest.fail('pending input cannot call the model'), mac,
                       threading.Event(), checkpoint=checkpoint, caps=Caps(max_steps=4, explicit=True))
    assert resumed.reason == 'checkpoint_dispatch_requires_reconciliation'
    assert calls == ['key']


def test_actual_cua_set_value_readback_throw_cannot_dispatch_next_mutation(tmp_path):
    calls = []
    session = SimpleNamespace(call=lambda tool, args, **kw: calls.append(tool) or
        SimpleNamespace(isError=False, structuredContent={'effect': 'confirmed', 'verified': True}, content=[]), close=lambda: None)
    native = CuaMac(session_caller(session))
    native.pid, native.app, native.ax_window, native.shot_window = 10, 'TextEdit', 1, 1
    elements = [{'id': 'e1', 'ax_role': 'AXTextArea', 'label': 'owned synthetic body'}]
    mac = FakeMac([('a', elements)])
    original_observe = mac.observe
    def observe():
        obs = original_observe()
        native._observation = obs
        native.tokens = {'e1': 'owned-token'}
        return obs
    mac.observe = observe
    mac.act = native.act
    def failed_readback():
        raise CuaError('driver_readonly_failed')
    native.observe = failed_readback
    replies = iter([reply(act('type', element_id='e1', text='synthetic value')),
                    reply(act('key', chord='cmd+s')), reply(act('done'))])
    checkpoint = FileCheckpoint(tmp_path / 'owned-checkpoint.json')
    result = run_loop('synthetic goal', lambda p: (next(replies), {}), mac, threading.Event(),
                      checkpoint=checkpoint, caps=Caps(max_steps=4, explicit=True), verify=lambda *a: (True, ''))
    assert calls == ['set_value']
    assert result.reason == 'effect_reconciliation_required'
    assert checkpoint.load()['pending_dispatch'] is True


@pytest.mark.parametrize('known_no_dispatch', [False, True])
def test_actual_caller_continue_distinguishes_transport_uncertainty_from_admission(tmp_path, known_no_dispatch):
    attempts, effects, pauses = [], [], []
    def call(tool, args, **kwargs):
        attempts.append(tool)
        if len(attempts) == 1:
            raise CuaSessionError('desktop_cua_session_unavailable' if known_no_dispatch else 'desktop_cua_transport_unavailable')
        effects.append(tool)
        return SimpleNamespace(isError=False, structuredContent={'effect': 'confirmed', 'verified': True}, content=[])
    caller = session_caller(SimpleNamespace(call=call, close=lambda: None))
    mac = FakeMac([('a', [])])
    def act_native(action, obs):
        caller('press_key', {'key': 'return'})
        return 'pressed return'
    mac.act = act_native
    def pause(*args, **kwargs):
        pauses.append(True)
        return 'continue'
    replies = iter([reply(act('key', chord='return')), reply(act('key', chord='return')), reply(act('done'))])
    checkpoint = FileCheckpoint(tmp_path / 'owned-checkpoint.json')
    result = run_loop('synthetic goal', lambda p: (next(replies), {}), mac, threading.Event(), pause=pause,
                      checkpoint=checkpoint, caps=Caps(max_steps=4, explicit=True), verify=lambda *a: (True, ''))
    assert pauses == [True]
    if known_no_dispatch:
        assert attempts == ['press_key', 'press_key'] and effects == ['press_key']
        assert result.outcome == 'done' and checkpoint.load() is None
    else:
        assert attempts == ['press_key'] and effects == []
        assert result.reason == 'effect_reconciliation_required' and checkpoint.load()['pending_dispatch'] is True


def test_source_local_bad_element_is_no_effect_and_reobserves_before_retry():
    from tests.unit.test_cu_ai_cua import opened
    mac, driver = opened()
    proposals = iter([reply(act('click', element_id='e999')), reply(act('key', chord='return')), reply(act('done'))])
    result = run_loop('synthetic goal', lambda p: (next(proposals), {}), mac, threading.Event(),
                      caps=Caps(max_steps=4, explicit=True), verify=lambda *a: (True, ''))
    inputs = [tool for tool, args in driver.calls if tool in {'click', 'press_key', 'hotkey'}]
    assert inputs == ['press_key'] and result.outcome == 'done'
    assert sum(tool == 'get_window_state' for tool, args in driver.calls) >= 2


def test_native_closed_no_effect_receipt_can_reobserve_without_human_retry():
    from tests.unit.test_cu_ai_cua import FakeDriver, opened
    class Driver(FakeDriver):
        def __call__(self, tool, args):
            if tool == 'click':
                self.calls.append((tool, args))
                return {'status': 'refused', 'effect': 'refused', 'verified': False,
                        'refusal': {'code': 'element_outside_target_window'}}
            return super().__call__(tool, args)
    mac, driver = opened(Driver())
    proposals = iter([reply(act('click', element_id='e1')), reply(act('key', chord='return')), reply(act('done'))])
    result = run_loop('synthetic goal', lambda p: (next(proposals), {}), mac, threading.Event(),
                      caps=Caps(max_steps=4, explicit=True), verify=lambda *a: (True, ''))
    assert [tool for tool, args in driver.calls if tool in {'click', 'press_key'}] == ['click', 'press_key']
    assert result.outcome == 'done'
    assert sum(tool == 'get_window_state' for tool, args in driver.calls) >= 2


def test_generic_sdk_cannot_claim_no_effect_with_an_arbitrary_attribute():
    mac = FakeMac([('a', [])])
    calls = []
    error = RuntimeError('private exception')
    error.no_effect = True
    def dispatched(action, obs):
        calls.append(action.op)
        raise error
    mac.act = dispatched
    proposals = iter([reply(act('key', chord='return')), reply(act('key', chord='return'))])
    result = run_loop('synthetic goal', lambda p: (next(proposals), {}), mac, threading.Event())
    assert calls == ['key'] and result.reason == 'effect_reconciliation_required'


@pytest.mark.parametrize('receipt', [
    {'status': 'refused', 'effect': 'refused', 'verified': True},
    {'status': 'refused', 'effect': 'refused', 'verified': False, 'suspected_noop': True},
    {'status': 'refused', 'verified': False},
    {'status': 'refused', 'effect': 'partial', 'verified': False},
])
def test_contradictory_or_incomplete_refusal_cannot_release_dispatch(receipt, tmp_path):
    from tests.unit.test_cu_ai_cua import FakeDriver, opened
    class Driver(FakeDriver):
        def __call__(self, tool, args):
            if tool == 'click':
                self.calls.append((tool, args))
                return receipt
            return super().__call__(tool, args)
    mac, driver = opened(Driver())
    proposals = iter([reply(act('click', element_id='e1')), reply(act('key', chord='return'))])
    checkpoint = FileCheckpoint(tmp_path / 'owned-checkpoint.json')
    result = run_loop('synthetic goal', lambda p: (next(proposals), {}), mac, threading.Event(),
                      caps=Caps(max_steps=4, explicit=True), checkpoint=checkpoint)
    assert [tool for tool, args in driver.calls if tool in {'click', 'press_key'}] == ['click']
    assert result.outcome != 'done' and checkpoint.load()['pending_dispatch'] is True
