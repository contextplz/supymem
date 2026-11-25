"""Original-task explicit mutation counts remain safe even when the actor errs."""
import threading

import pytest

from coding_agents.cu_ai_loop import Action, Caps
from coding_agents.cu_exact_button_budget import exact_button_budgets
from desktop_runtime.cu_ai_cua import CuaError
from desktop_runtime.cu_ai_engine import Observation, run_loop


GOAL = 'Open the app and press the Increment button exactly 5 times.'


class CounterMac:
    def __init__(self, *, ambiguous=False, fail=None):
        self.count = 0
        self.ambiguous, self.fail = ambiguous, fail
        self.attempts = 0

    def observe(self):
        elements = [{'id': 'b0', 'ax_role': 'button', 'label': 'Increment', 'focused': True},
                    {'id': 'b1', 'ax_role': 'button', 'label': 'Unrelated', 'focused': False}]
        if self.ambiguous:
            elements.append({'id': 'b2', 'ax_role': 'button', 'label': 'Increment'})
        obs = Observation(str(self.count).encode(), elements, 'Chrome (browser)', 'Owned task app', '', False, None)
        obs.notes = f'PAGE URL: https://owned.example/app\nPAGE TEXT: Current count: {self.count}'
        return obs

    def frontmost(self):
        return ('Chrome (CDP)', 0)

    def focused_role(self):
        return None

    def act(self, action, obs):
        self.attempts += 1
        if self.fail == 'before' and self.attempts == 1:
            raise CuaError('synthetic admission refusal', no_effect=True)
        if action.element_id == 'b0':
            self.count += 2 if action.op == 'double_click' else 1
        if self.fail == 'after':
            raise CuaError('synthetic post-input uncertainty')
        return 'click ok (0.01s)'


def actor_click(payload):
    if 'TERMINAL_ANSWER_ONLY' in payload['hint']:
        return {'actions': [{'op': 'done', 'summary': 'The final count is 5.'}]}, {}
    return {'actions': [{'op': 'click', 'element_id': 'b0'}]}, {}


def run(mac, actor=actor_click, verify=None, **kwargs):
    return run_loop(GOAL, actor, mac, threading.Event(), caps=Caps(max_steps=12, max_seconds=60, explicit=True),
                    verify=verify or (lambda claim, obs, evidence: (mac.count == 5, 'exact independent count check')), **kwargs)


def test_actor_proposing_sixth_click_is_refused_and_cannot_exceed_original_exact_count():
    mac = CounterMac()
    decisions = []
    def actor(payload):
        decisions.append(True)
        return {'actions': [{'op': 'click', 'element_id': 'b0'}]}, {}
    result = run(mac, actor)
    assert result.outcome == 'failed' and result.reason == 'terminal_answer_action_refused'
    assert mac.count == mac.attempts == 5 and len(decisions) == 6
    assert any(row.get('outcome') == 'done_claimed' and row.get('verified') is True for row in result.log)


def test_rejected_whole_task_verifier_never_opens_another_budget_slot():
    mac = CounterMac()
    result = run(mac, verify=lambda *args: (False, 'synthetic remaining work missing'))
    assert result.outcome != 'done' and mac.count == mac.attempts == 5
    assert any(row.get('outcome') == 'exact_repetition_limit_reached' for row in result.log)


def test_same_named_ambiguous_controls_are_rejected_before_any_input():
    mac = CounterMac(ambiguous=True)
    result = run(mac)
    assert result.outcome != 'done' and mac.attempts == 0
    assert any(row.get('outcome') == 'exact_repetition_recipient_ambiguous' for row in result.log)


def test_uncertain_dispatched_input_holds_checkpoint_and_never_retries():
    mac = CounterMac(fail='after')
    saved = []
    checkpoint = type('Checkpoint', (), {'load': lambda self: None, 'save': lambda self, row: saved.append(row),
                                         'clear': lambda self: None})()
    result = run(mac, checkpoint=checkpoint)
    assert result.reason == 'effect_reconciliation_required' and mac.count == mac.attempts == 1
    assert saved[-1]['pending_dispatch'] is True and saved[-1]['exact_button_budgets'][0]['used'] == 1


def test_proven_pre_dispatch_failure_can_release_only_its_reserved_slot():
    mac = CounterMac(fail='before')
    result = run(mac)
    assert result.outcome == 'done' and mac.count == 5 and mac.attempts == 6


def test_original_task_budget_does_not_charge_other_observed_control():
    budget = exact_button_budgets(GOAL)[0]
    elements = CounterMac().observe().elements
    assert budget.admission(Action('click', element_id='b1'), elements) == (0, None)
    assert budget.admission(Action('key', chord='Return'), elements) == (1, None)
    budget.used = 5
    assert budget.admission(Action('key', chord='Return'), elements)[1] == 'exact_repetition_limit_reached'
    assert budget.admission(Action('double_click', element_id='b0'), elements)[1] == 'exact_repetition_limit_reached'


def test_no_page_or_model_text_can_create_original_task_count_authority():
    assert exact_button_budgets('Read the current counter value.') == []
    budgets = exact_button_budgets('Click "Add tab" exactly 2 times; click Next button exactly 3 times.')
    assert [(b.label, b.limit) for b in budgets] == [('add tab', 2), ('next', 3)]
    with pytest.raises(ValueError):
        exact_button_budgets('Click Next exactly 2 times; click Next exactly 3 times.')


@pytest.mark.parametrize('used', [3, 5])
def test_checkpoint_resume_preserves_used_quota_and_verifies_before_another_input(used):
    mac = CounterMac()
    mac.count = used
    from coding_agents.cu_ai_loop import LoopState
    from desktop_runtime.cu_ai_retries import RetryGuard
    saved = {**LoopState().snapshot(GOAL), 'pending_dispatch': False, 'route_retries': RetryGuard().snapshot(),
             'exact_button_budgets': [{'label':'increment','limit':5,'used':used}]}
    checkpoint = type('Checkpoint', (), {'load':lambda self:saved, 'save':lambda self,row:None,
                                         'clear':lambda self:None})()
    result = run(mac, checkpoint=checkpoint)
    assert result.outcome == 'done' and mac.attempts == 5 - used


def test_old_checkpoint_without_explicit_budget_cannot_reset_quota():
    from coding_agents.cu_ai_loop import LoopState
    saved = {**LoopState().snapshot(GOAL), 'pending_dispatch': False, 'route_retries': {}}
    checkpoint = type('Checkpoint', (), {'load':lambda self:saved})()
    mac = CounterMac()
    result = run(mac, checkpoint=checkpoint)
    assert result.reason == 'checkpoint_action_budget_unreconciled' and mac.attempts == 0


def test_stop_between_reservation_and_backend_call_restores_proven_unused_quota():
    mac, stop, saved = CounterMac(), threading.Event(), []
    def save(row):
        saved.append(row)
        if row['pending_dispatch']:
            stop.set()
    checkpoint = type('Checkpoint', (), {'load':lambda self:None, 'save':lambda self,row:save(row),
                                         'clear':lambda self:None})()
    result = run_loop(GOAL, actor_click, mac, stop, checkpoint=checkpoint, verify=lambda *args:(True,''))
    assert result.outcome == 'stopped' and mac.attempts == 0
    assert saved[-1]['pending_dispatch'] is False and saved[-1]['exact_button_budgets'][0]['used'] == 0


def test_time_cap_between_reservation_and_backend_call_restores_unused_quota(monkeypatch):
    from desktop_runtime import cu_ai_engine as engine
    mac, saved, expired = CounterMac(), [], [False]
    clock = engine.time.monotonic
    monkeypatch.setattr(engine.time, 'monotonic', lambda:clock() + (120 if expired[0] else 0))
    def save(row):
        saved.append(row)
        if row['pending_dispatch']:
            expired[0] = True
    checkpoint = type('Checkpoint', (), {'load':lambda self:None, 'save':lambda self,row:save(row),
                                         'clear':lambda self:None})()
    result = run(mac, checkpoint=checkpoint)
    assert result.outcome == 'capped' and mac.attempts == 0
    assert saved[-1]['pending_dispatch'] is False and saved[-1]['exact_button_budgets'][0]['used'] == 0


def test_zero_original_task_authorization_cannot_deliver_any_button_input():
    budget = exact_button_budgets('Click Increment button exactly 0 times.')[0]
    assert budget.limit == 0
    assert budget.admission(Action('click', element_id='b0'), CounterMac().observe().elements)[1] == 'exact_repetition_limit_reached'


def test_coordinate_and_unknown_keyboard_recipient_cannot_bypass_named_limit():
    budget = exact_button_budgets(GOAL)[0]
    elements = CounterMac().observe().elements
    assert budget.admission(Action('click', x=1, y=2), elements)[1] == 'exact_repetition_requires_observed_control_id'
    elements[0]['focused'] = False
    assert budget.admission(Action('key', chord='Return'), elements)[1] == 'exact_repetition_keyboard_recipient_unknown'


@pytest.mark.parametrize('chord', ['Return', ' return ', 'ENTER', ' Space ', 'cmd+Return'])
def test_dispatcher_activation_aliases_share_the_same_maximum_input_budget(chord):
    budget = exact_button_budgets(GOAL)[0]
    elements = CounterMac().observe().elements
    assert budget.admission(Action('key', chord=chord), elements) == (1, None)
    budget.used = 5
    assert budget.admission(Action('key', chord=chord), elements)[1] == 'exact_repetition_limit_reached'


@pytest.mark.parametrize('quotes', [('“','”'), ('‘','’'), ('"','"'), ("'","'")])
def test_balanced_control_name_quotes_preserve_exact_observed_recipient(quotes):
    budget = exact_button_budgets(f'Click {quotes[0]}Increment{quotes[1]} exactly 0 times.')[0]
    assert budget.label == 'increment' and budget.limit == 0
    assert budget.admission(Action('click', element_id='b0'), CounterMac().observe().elements)[1] == 'exact_repetition_limit_reached'


def test_unbalanced_control_name_quotes_fail_closed():
    with pytest.raises(ValueError):
        exact_button_budgets('Click “Increment exactly 5 times.')
