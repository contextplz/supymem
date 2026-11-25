"""Stage machine: illegal transitions raise; table-driven."""

from __future__ import annotations

import pytest

from tests.company_os_harness import load_company_os_app

load_company_os_app()

from app.core.stages import TRANSITIONS, can_transition, progress, reach_at_least, transition  # noqa: E402
from core.enums import VentureStage  # noqa: E402
from core.errors import IllegalTransition  # noqa: E402


def test_legal_happy_path() -> None:
    s = VentureStage.DRAFT
    for nxt in (
        VentureStage.CLASSIFYING,
        VentureStage.CLARIFYING,
        VentureStage.CHARTER_REVIEW,
        VentureStage.ORG_PLANNING,
        VentureStage.FORK_CHOICE,
        VentureStage.BUILDING,
        VentureStage.READINESS_REVIEW,
        VentureStage.HOSTING,
        VentureStage.LIVE,
    ):
        assert can_transition(s, nxt)
        s = transition(s, nxt)
    assert s == VentureStage.LIVE


def test_illegal_transition_raises() -> None:
    with pytest.raises(IllegalTransition):
        transition(VentureStage.DRAFT, VentureStage.LIVE)


def test_machine_is_a_table() -> None:
    assert isinstance(TRANSITIONS, dict)
    assert VentureStage.DRAFT in TRANSITIONS
    # archived is terminal
    assert TRANSITIONS[VentureStage.ARCHIVED] == frozenset()


def test_any_can_pause_or_archive_from_draft() -> None:
    assert can_transition(VentureStage.DRAFT, VentureStage.PAUSED)
    assert can_transition(VentureStage.DRAFT, VentureStage.ARCHIVED)


def test_progress_allows_a_proven_forward_journey_but_never_regresses() -> None:
    assert progress(VentureStage.DRAFT, VentureStage.BUILDING) is VentureStage.BUILDING
    assert progress(VentureStage.BUILDING, VentureStage.READINESS_REVIEW) is VentureStage.READINESS_REVIEW
    with pytest.raises(IllegalTransition):
        progress(VentureStage.LIVE, VentureStage.BUILDING)


def test_reach_at_least_preserves_later_straight_path_stage() -> None:
    assert (
        reach_at_least(VentureStage.READINESS_REVIEW, VentureStage.BUILDING)
        is VentureStage.READINESS_REVIEW
    )
    assert reach_at_least(VentureStage.HOSTING, VentureStage.BUILDING) is VentureStage.HOSTING
    assert reach_at_least(VentureStage.LIVE, VentureStage.BUILDING) is VentureStage.LIVE
