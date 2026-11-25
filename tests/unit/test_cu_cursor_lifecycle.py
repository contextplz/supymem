"""Cursor hints terminate on engine escape; hints never prove task success."""
import threading
from types import SimpleNamespace

import pytest

from desktop_runtime import cu_ai_engine as eng


@pytest.mark.parametrize("exception", [RuntimeError, KeyboardInterrupt])
def test_unexpected_checkpoint_escape_ends_cursor_and_preserves_exception(monkeypatch, exception):
    events = []
    monkeypatch.setattr(eng.agent_cursor, 'emit', lambda kind, *a, **kw: events.append(kind))
    error = exception('private checkpoint detail')
    def fail():
        raise error
    with pytest.raises(exception) as caught:
        eng.run_loop('owned task', lambda _: pytest.fail('no model after failed load'),
                     SimpleNamespace(), threading.Event(), checkpoint=SimpleNamespace(load=fail))
    assert caught.value is error
    assert events == ['begin', 'end']


def test_preexisting_stop_terminates_cursor_without_observation_or_success(monkeypatch):
    events = []
    monkeypatch.setattr(eng.agent_cursor, 'emit', lambda kind, *a, **kw: events.append(kind))
    stop = threading.Event()
    stop.set()
    result = eng.run_loop('owned task', lambda _: pytest.fail('no model after stop'),
                         SimpleNamespace(observe=lambda: pytest.fail('no observe after stop')), stop)
    assert result.outcome == 'stopped'
    assert events == ['begin', 'end']


def test_reset_evidence_failure_still_terminates_hint_without_false_success(monkeypatch):
    events = []
    monkeypatch.setattr(eng.agent_cursor, 'emit', lambda kind, *a, **kw: events.append(kind))
    def reset():
        raise OSError('private reset detail')
    result = eng.run_loop('owned task', lambda _: pytest.fail('no model after failed reset'),
                         SimpleNamespace(reset_completion_evidence=reset), threading.Event())
    assert result.outcome == 'failed'
    assert result.reason == 'completion_evidence_reset_failed'
    assert events == ['begin', 'end']
