from __future__ import annotations

import pytest
from tests.company_os_harness import load_company_os_app

load_company_os_app()

from app.evolution.peer_review import review


def test_self_review_impossible():
    with pytest.raises(PermissionError):
        review("p1", author_id="a1", reviewer_id="a1")
    r = review("p1", author_id="a1", reviewer_id="a2")
    assert r.reviewed_by_agent_id == "a2"
