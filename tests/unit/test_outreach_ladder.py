"""OT-16: ask ladder — illegal skips refused."""

from __future__ import annotations

from tests.company_os_harness import load_company_os_app

load_company_os_app()

from app.gtm.relationships.ladder import can_transition


def test_none_to_call_15_is_illegal():
    result = can_transition("none", "call_15")
    assert result.ok is False


def test_sequential_connect_ok():
    assert can_transition("none", "connect").ok is True
    assert can_transition("connect", "question").ok is True
    assert can_transition("question", "value_return").ok is True
    assert can_transition("value_return", "call_15").ok is True


def test_drop_from_anywhere():
    assert can_transition("connect", "dropped").ok is True


def test_call_15_needs_value_return_and_sku():
    from app.gtm.relationships.ladder import call_15_allowed

    skip = call_15_allowed(from_rung="none", has_value_return=True, sku_approved=True)
    assert skip.ok is False
    no_sku = call_15_allowed(
        from_rung="value_return", has_value_return=True, sku_approved=False
    )
    assert no_sku.ok is False
    assert no_sku.reason == "need_approved_sku"
    no_vr = call_15_allowed(
        from_rung="value_return", has_value_return=False, sku_approved=True
    )
    assert no_vr.ok is False
    ok = call_15_allowed(
        from_rung="value_return", has_value_return=True, sku_approved=True
    )
    assert ok.ok is True
