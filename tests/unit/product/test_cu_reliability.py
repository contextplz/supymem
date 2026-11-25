import pytest

from desktop_runtime import cu_reliability as cu


def test_normalize_entry_text():
    assert cu.normalize_entry_text("  https://example.org/\n") == "https://example.org/"
    assert cu.normalize_entry_text("a\tb") == "a b"
    for blank in ("", " \n\t", None):
        with pytest.raises(ValueError):
            cu.normalize_entry_text(blank)


@pytest.mark.parametrize("expected,observed,ok", [
    ("https://www.google.com", "https://www.google.com", True),
    ("https://www.google.com", "ahttps://www.google.com", False),  # dwo_gzmnbvkwvvce
    ("https://www.google.com", "www.google.com/", True),  # Safari drops scheme, adds slash
    ("example.org", "https://example.org/", True),
    ("Hello world", "hello world", True),
    ("Hello", "Hello world", True),  # inline autocomplete suffix
    ("Hello", "Hello" + "x" * 100, False),
    ("Hello", "", False),
    ("Hello", None, False),
    ("Hello", "Goodbye", False),
])
def test_field_value_decision(expected, observed, ok):
    assert cu.field_value_matches(expected, observed) is ok


def test_field_is_clear():
    assert cu.field_is_clear("") and cu.field_is_clear("  ")
    assert not cu.field_is_clear("old") and not cu.field_is_clear(None)


def test_repeated_type_loop_detection():
    typed = {"operation": "type", "label": "Smart Search"}
    click = {"operation": "click", "label": "Smart Search"}
    assert not cu.repeated_type_on_field([typed], "Smart Search")
    assert cu.repeated_type_on_field([typed, typed], "Smart Search")
    assert not cu.repeated_type_on_field([typed, typed], "Other field")
    assert not cu.repeated_type_on_field([typed, click], "Smart Search")
    assert not cu.repeated_type_on_field([click, typed, typed][:2], "Smart Search")


def test_wait_until_waits_for_bridge_then_gives_up():
    now = [0.0]
    ready_at = 20.0  # bridge warm-up
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    assert cu.wait_until(lambda: now[0] >= ready_at, timeout=30, interval=1,
                         sleep=sleep, clock=lambda: now[0])
    assert now[0] == 20.0 and len(sleeps) == 20
    now[0] = 0.0
    assert not cu.wait_until(lambda: False, timeout=5, interval=1, sleep=sleep, clock=lambda: now[0])
    assert now[0] == 5.0
    assert cu.wait_until(lambda: True, timeout=0)  # always checks once


def test_retry_transient_is_bounded_and_only_for_known_codes():
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise RuntimeError("desktop_cua_session_unavailable")
        return "ok"

    assert cu.retry_transient(flaky, sleep=lambda _s: None) == "ok"
    calls.clear()

    def always():
        calls.append(1)
        raise RuntimeError("desktop_cua_session_unavailable")

    with pytest.raises(RuntimeError, match="session_unavailable"):
        cu.retry_transient(always, attempts=3, sleep=lambda _s: None)
    assert len(calls) == 3
    calls.clear()

    def permanent():
        calls.append(1)
        raise RuntimeError("desktop_cua_host_permissions_missing")

    with pytest.raises(RuntimeError):
        cu.retry_transient(permanent, sleep=lambda _s: None)
    assert len(calls) == 1


@pytest.mark.parametrize("outcome,failure,driver,expected", [
    ("verified", None, None, None),
    ("repeated_action_stopped", None, None, cu.CU_REPEATED_ACTION),
    ("execution_requires_reconciliation", "desktop_cua_session_unavailable", None, cu.CU_SESSION_UNAVAILABLE),
    ("execution_requires_reconciliation", "desktop_cua_host_permissions_missing", None, cu.CU_PERMISSION_MISSING),
    ("execution_requires_reconciliation", "computer_use_effect_outcome_uncertain",
     "desktop_cua_effect_uncertain", cu.CU_EFFECT_UNVERIFIED),
    ("execution_requires_reconciliation", "computer_use_effect_outcome_uncertain",
     "cu_field_value_mismatch", cu.CU_FIELD_VALUE_MISMATCH),
    ("reviewed_window_changed_before_start", "desktop_window_identity_changed", None, cu.CU_WINDOW_CHANGED),
    ("cancelled", None, None, cu.CU_STOPPED),
])
def test_reason_code(outcome, failure, driver, expected):
    assert cu.reason_code(outcome, failure, driver) == expected
