from __future__ import annotations

from tests.company_os_harness import load_company_os_app

load_company_os_app()

from app.evolution.archive import archive, clear_archive, has_similar_failure


def test_regressed_not_retried():
    clear_archive()
    hyp = {"kind": "org_replace", "subject": "op"}
    archive({"hypothesis": str(hyp), "predicted_metric": "engagement_rate", "tier": "org", "outcome": "regressed"})
    assert has_similar_failure(hyp) is True
