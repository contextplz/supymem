"""Pause feedback distinguishes Continue, observed gate clearing and invalid hooks."""
from types import SimpleNamespace

import pytest

from desktop_runtime import cu_ai_engine as eng


@pytest.mark.parametrize('result,phrase', [
    ('continue', 'clicked Continue'), ('resolved', 'no longer showed the human gate')])
def test_pause_feedback_does_not_claim_founder_completed_verification(result, phrase):
    rows = []
    state = SimpleNamespace(started=0, same_screen=1, same_action=1, history=[])
    verdict = eng._pause(state, lambda *a: result, 'captcha',
                         SimpleNamespace(frontmost='Owned app'), lambda **row: rows.append(row))
    assert verdict == 'resumed'
    assert phrase in state.history[-1]
    assert 'Re-observe' in state.history[-1]
    assert 'founder completed' not in state.history[-1]
    assert rows[-1]['outcome'] == 'resumed_' + result


def test_unknown_pause_result_is_held_without_private_hook_body():
    rows = []
    state = SimpleNamespace(started=0, same_screen=1, same_action=1, history=[])
    verdict = eng._pause(state, lambda *a: 'private transport body', 'captcha',
                         SimpleNamespace(frontmost='Owned app'), lambda **row: rows.append(row))
    assert verdict == ('failed', 'pause_result_invalid')
    assert 'private transport body' not in repr(rows)
    assert not state.history
