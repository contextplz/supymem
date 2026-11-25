"""Every legal FSM edge passes; every other pair is illegal (doc 05 §3, 24 §3)."""

from __future__ import annotations

import pytest

from tests.company_os_harness import load_company_os_app

load_company_os_app()

from app.core.fsm import MACHINES, can_transition, legal_edges, states_of, transition
from core.errors import IllegalTransition


@pytest.mark.parametrize("machine", sorted(MACHINES))
def test_legal_edges_pass(machine: str) -> None:
    for src, dst in legal_edges(machine):
        assert can_transition(machine, src, dst), (machine, src, dst)


@pytest.mark.parametrize("machine", sorted(MACHINES))
def test_illegal_edges_rejected(machine: str) -> None:
    allowed = legal_edges(machine)
    states = states_of(machine)
    illegal = [
        (src, dst)
        for src in states
        for dst in states
        if src != dst and (src, dst) not in allowed
    ]
    assert illegal, machine
    for src, dst in illegal:
        assert not can_transition(machine, src, dst), (machine, src, dst)


def test_thirteen_plus_task_machine_registered() -> None:
    required = {
        "venture",
        "idea_session",
        "execution_plan",
        "plan_milestone",
        "plan_task",
        "app",
        "build_run",
        "deployment",
        "agent_run",
        "job",
        "approval",
        "credit_reservation",
        "app_domain",
        "experiment",
    }
    assert required <= set(MACHINES)


def test_transition_mutates_and_rejects() -> None:
    class _E:
        __fsm__ = "idea_session"
        state = "open"
        id = "idea_test"
        venture_id = None

    e = _E()
    transition(None, e, "recon", actor="test", reason="start")
    assert e.state == "recon"
    with pytest.raises(IllegalTransition):
        transition(None, e, "confirmed", actor="test", reason="skip")


def test_job_legacy_status_maps() -> None:
    assert can_transition("job", "pending", "leased")
    assert can_transition("job", "running", "succeeded")


def test_venture_legacy_stage_maps() -> None:
    assert can_transition("venture", "draft", "ideating")
    assert can_transition("venture", "clarifying", "researching")
    assert not can_transition("venture", "created", "live")
