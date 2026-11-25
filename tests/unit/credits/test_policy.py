"""Policy reads and cache invalidation."""

from __future__ import annotations


def test_seeded_values_read_correctly(credits_env) -> None:
    from app.credits.policy import (
        default_venture_credits,
        dual_control_threshold_micro,
        enforcement_enabled,
        grant_cap_micro,
        reservation_ttl_seconds,
        web_fetch_credits,
        web_search_credits,
    )
    from app.credits.units import MAX_GRANT_MICRO, credits_to_micro

    assert default_venture_credits() == 5000
    assert reservation_ttl_seconds() == 900
    assert enforcement_enabled() is True
    assert web_search_credits() == 2
    assert web_fetch_credits() == 1
    assert dual_control_threshold_micro() == credits_to_micro(50_000)
    assert grant_cap_micro("support") == MAX_GRANT_MICRO
    assert grant_cap_micro("finance") == MAX_GRANT_MICRO
    assert grant_cap_micro("read_only") == MAX_GRANT_MICRO
    assert grant_cap_micro("super_admin") == MAX_GRANT_MICRO


def test_set_policy_is_visible_immediately(credits_env) -> None:
    from app.credits.policy import (
        default_venture_credits,
        get_int_policy,
        set_policy,
    )

    set_policy("default_venture_credits", 42, updated_by="test-admin")
    assert default_venture_credits() == 42
    assert get_int_policy("default_venture_credits", 500) == 42
