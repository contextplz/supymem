import pytest

from coding_agents.computer_use_gate import ComputerUseGate, EffectUncertain


def fixture(**overrides):
    calls, admitted = [], set()
    def driver(args, **kwargs):
        calls.append((args, kwargs))
        return {"observed": True}
    def admit(order, session, invocation, digest):
        key = (order, session, invocation)
        if key in admitted:
            return False
        admitted.add(key)
        return True
    gate = ComputerUseGate(order_id="order_one", session_id="session_one", driver=driver,
        **{"authorize": lambda *args: True, "admit_effect": admit, **overrides})
    return gate, calls, admitted


@pytest.mark.parametrize("missing", ["authorize", "admit_effect"])
def test_no_missing_host_callback_can_default_allow_even_an_observation(missing):
    gate, calls, _ = fixture(**{missing: None})
    with pytest.raises(PermissionError, match="gate_missing"):
        gate.invoke("one", {"action": "capture", "app": "com.apple.Safari"})
    assert calls == []


@pytest.mark.parametrize("args", [{"action": "capture"}, {"action": "capture", "app": "screen"},
    {"action": "list_apps", "app": "com.apple.Safari"},
    {"action": "capture", "app": "com.apple.Safari", "window_id": 5},
    {"action": "click", "app": "com.apple.Safari", "auto_approve": True}])
def test_implicit_or_alternative_targets_cannot_escape_scope(args):
    gate, calls, _ = fixture()
    with pytest.raises(ValueError):
        gate.invoke("one", args)
    assert calls == []


def test_scope_checked_for_reads_and_rechecked_after_effect_admission():
    checks = []
    def authorize(*args):
        checks.append(args)
        return len(checks) == 1
    gate, calls, admitted = fixture(authorize=authorize)
    with pytest.raises(PermissionError, match="scope_denied"):
        gate.invoke("one", {"action": "click", "app": "com.apple.Safari", "element_index": 4})
    assert not calls and len(admitted) == 1 and len(checks) == 2


def test_authorizer_cannot_mutate_the_admitted_driver_payload():
    def authorize(order, session, args):
        args["app"] = "com.apple.Terminal"
        return True
    gate, calls, _ = fixture(authorize=authorize)
    gate.invoke("one", {"action": "capture", "app": "com.apple.Safari"})
    assert calls[0][0]["app"] == "com.apple.Safari"
    assert calls[0][1] == {"session_id": "session_one"}


def test_ambiguous_effect_is_not_replayed():
    gate, _, admitted = fixture()
    count = []
    def driver(*args, **kwargs):
        count.append(1)
        raise TimeoutError("private backend response")
    gate.driver = driver
    args = {"action": "click", "app": "com.apple.Safari", "element_index": 4}
    with pytest.raises(EffectUncertain, match="outcome_uncertain"):
        gate.invoke("one", args)
    with pytest.raises(PermissionError, match="not_admitted"):
        gate.invoke("one", args)
    assert len(count) == len(admitted) == 1


def test_effect_failure_carries_bounded_sanitized_driver_detail_only_when_provided():
    gate, _, _ = fixture()
    error = RuntimeError("desktop_cua_effect_uncertain_select")
    error.detail = "minimized_or_hidden_window: raw key input refused " + "x" * 400
    def driver(*_a, **_k):
        raise error
    gate.driver = driver
    with pytest.raises(EffectUncertain) as raised:
        gate.invoke("one", {"action": "click", "app": "com.apple.Safari", "element_index": 4})
    assert raised.value.driver_code == "desktop_cua_effect_uncertain_select"
    assert raised.value.driver_detail.startswith("minimized_or_hidden_window") and len(raised.value.driver_detail) == 200
    gate.driver = lambda *_a, **_k: (_ for _ in ()).throw(TimeoutError("private"))
    with pytest.raises(EffectUncertain) as raised:
        gate.invoke("two", {"action": "click", "app": "com.apple.Safari", "element_index": 4})
    assert raised.value.driver_detail is None


def test_capture_preserves_bounded_native_failure_code_without_driver_details():
    gate, _, _ = fixture()
    gate.driver = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        RuntimeError("desktop_cua_reviewed_window_missing"))
    with pytest.raises(RuntimeError, match="^desktop_cua_reviewed_window_missing$"):
        gate.invoke("one", {"action": "capture", "app": "com.apple.Safari"})
    gate.driver = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        RuntimeError("private path /Users/founder/secret"))
    with pytest.raises(RuntimeError, match="^computer_use_observation_failed$"):
        gate.invoke("two", {"action": "capture", "app": "com.apple.Safari"})


@pytest.mark.parametrize("override", [
    {"delivery_mode": "foreground"}, {"bring_to_front": True},
    {"raise_window": True}, {"delivery_mode": None}, {"bring_to_front": "false"},
])
def test_background_gate_cannot_be_escalated_by_model_or_general_approval(override):
    gate, calls, admitted = fixture()
    with pytest.raises(PermissionError, match="foreground_permission_required"):
        gate.invoke("one", {"action": "click", "app": "com.apple.Safari", **override})
    assert not calls and not admitted


def test_background_policy_is_bound_to_admission_and_driver():
    observed = []
    gate, calls, _ = fixture(authorize=lambda order, session, args: observed.append(args) or True)
    gate.invoke("one", {"action": "click", "app": "com.apple.Safari", "element": 4})
    assert len(observed) == 2
    for args in [*observed, calls[0][0]]:
        assert args["delivery_mode"] == "background"
        assert args["bring_to_front"] is False
        assert args["raise_window"] is False


@pytest.mark.parametrize("idea", ["Inventory reconciliation for distributors", "Plant care reminders for families"])
def test_onboarding_queues_computer_use_only_when_native_execution_is_enabled(monkeypatch, idea):
    import types
    from unittest import mock
    from tests.company_os_harness import load_company_os_app

    load_company_os_app()
    from app.onboarding.steps import build
    from app.core.local_mode import is_local
    from app.core.local_secrets import update_settings

    ctx = types.SimpleNamespace(venture=types.SimpleNamespace(id="v1", raw_idea=idea))
    if is_local():
        update_settings("capabilities", {"background_work": False}, {})
    for value in (None, "", "true", "0"):
        if value is None:
            monkeypatch.delenv("DEVICE_DESKTOP_EXECUTION_ENABLED", raising=False)
        else:
            monkeypatch.setenv("DEVICE_DESKTOP_EXECUTION_ENABLED", value)
        with mock.patch("app.build.computer_use.enqueue_computer_use") as enqueue:
            build._queue_computer_use(ctx, "Co", "one liner")
        enqueue.assert_not_called()
    monkeypatch.setenv("DEVICE_DESKTOP_EXECUTION_ENABLED", "1")
    if is_local():
        update_settings("capabilities", {"background_work": True}, {})
    with mock.patch("app.build.computer_use.enqueue_computer_use") as enqueue:
        build._queue_computer_use(ctx, "Co", "one liner")
    enqueue.assert_called_once()
