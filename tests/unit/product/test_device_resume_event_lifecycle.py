"""Actual pause/Event lifecycle: Continue survives publication-to-wait scheduling."""
from types import SimpleNamespace

from desktop_runtime import cu_ai_engine as engine
from desktop_runtime.device_worker import DeviceWorker
from tests.unit.product.test_cu_ai_worker import (
    FakeMac, Gateway, MANIFEST, worker,
    # Register the actual shared autouse fixture in this module too.
    _owned_worker_checkpoint_directory as _owned_worker_checkpoint_directory,
)


def test_owned_continue_delivered_after_publication_before_wait_is_not_lost():
    owned = DeviceWorker(SimpleNamespace(order_id="dwo_owned"), lambda receipt: None)
    owned.continue_event.clear()  # episode owner initializes before publishing
    owned.progress("needs_you", pause_kind="captcha")
    owned._record_step(kind="pause", pause_kind="captcha")
    assert owned.continue_founder_handoff()
    assert engine.wait_for_human("captcha", "Complete verification", FakeMac(), owned.cancelled,
        owned.continue_event, notify=lambda text: None, timeout=0.01) == "continue"
    assert owned.last["task_verified"] is False


def test_actual_worker_initializes_each_episode_before_publishing(monkeypatch):
    from desktop_runtime import peekaboo_driver
    def unavailable(*args, **kwargs):
        raise peekaboo_driver.DriverUnavailable("desktop_driver_not_installed")
    # Exercise a clean CI host: this test must never depend on a real GUI driver.
    monkeypatch.setattr(peekaboo_driver, "_binary", unavailable)
    gateway = Gateway([])
    owned, saved = worker(monkeypatch, gateway, FakeMac())
    monkeypatch.setattr(owned, "_notify_needs_you", lambda *args: None)
    original_record = owned._record_step
    observed = []
    def record(**values):
        original_record(**values)
        if values.get("kind") == "pause":
            # Old episode's Event must be cleared before a new pause is visible.
            assert not owned.continue_event.is_set()
            assert owned.last["state"] == "needs_you"
            observed.append(id(owned))
            assert owned.continue_founder_handoff()
    monkeypatch.setattr(owned, "_record_step", record)
    original_wait = engine.wait_for_human
    def wait(*args, **kwargs):
        return original_wait(*args, **kwargs, timeout=0.01)
    monkeypatch.setattr(engine, "wait_for_human", wait)
    def run(*args, pause, **kwargs):
        assert pause("captcha", "Complete verification", None) == "continue"
        assert pause("login", "Sign in", None) == "continue"
        return SimpleNamespace(outcome="stopped", reason="test stops before effects", steps=0, seconds=0)
    monkeypatch.setattr(engine, "run_loop", run)
    owned.run_ai_loop({"state": "approved"}, MANIFEST)
    assert observed == [id(owned), id(owned)]
    assert owned.last["task_verified"] is False
    assert gateway.receipt["task_verified"] is False


def test_stop_wins_over_already_delivered_continue():
    owned = DeviceWorker(SimpleNamespace(order_id="dwo_owned"), lambda receipt: None)
    owned.progress("needs_you", pause_kind="captcha")
    assert owned.continue_founder_handoff()
    owned.cancel()
    assert engine.wait_for_human("captcha", "Complete verification", FakeMac(), owned.cancelled,
        owned.continue_event, notify=lambda text: None, timeout=0.01) == "stopped"
    assert owned.last["task_verified"] is False
