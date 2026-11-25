"""OT-14: network distance is non-monotonic. More mutuals is not better."""

from __future__ import annotations

from tests.company_os_harness import load_company_os_app

load_company_os_app()

from app.gtm.relationships.score import network_score


def test_network_curve_peak_and_tails():
    assert network_score(None) == 1
    assert network_score(0) == 1
    assert network_score(1) == 4
    assert network_score(3) == 4
    assert network_score(5) == 4
    assert network_score(6) == 3
    assert network_score(15) == 3
    assert network_score(16) == 2
    assert network_score(49) == 2
    assert network_score(50) == 1
    assert network_score(199) == 1
    assert network_score(200) == 0
    assert network_score(250) == 0

    assert network_score(3) > network_score(0)
    assert network_score(3) > network_score(250)
    assert network_score(0) < 4
    assert network_score(250) < 4


def test_linear_min4_would_fail_this_curve():
    """A linear min(4, mutuals) implementation fails OT-14."""

    def linear(n: int | None) -> int:
        if n is None:
            return 0
        return min(4, n)

    assert linear(0) != network_score(0) or linear(250) != network_score(250)
    assert linear(3) == 3
    assert network_score(3) == 4
    assert linear(250) == 4
    assert network_score(250) == 0
    assert network_score(3) > network_score(0)
    assert network_score(3) > network_score(250)
