from __future__ import annotations

from tests.company_os_harness import load_company_os_app

load_company_os_app()

from app.evolution.propose import Proposal, validate_proposal_shape


def test_missing_fields_rejected():
    p = Proposal(
        id="p",
        tier="config",
        hypothesis="",
        evidence=[],
        predicted_metric="",
        predicted_delta=0,
        rollback={},
        authored_by_agent_id="a",
    )
    assert validate_proposal_shape(p) is False
    p.hypothesis = "h"
    p.evidence = ["e"]
    p.predicted_metric = "m"
    p.rollback = {"undo": True}
    assert validate_proposal_shape(p) is True
