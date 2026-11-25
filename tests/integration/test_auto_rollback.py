from __future__ import annotations

from tests.company_os_harness import load_company_os_app

load_company_os_app()

from app.evolution.archive import clear_archive, has_similar_failure, list_archive
from app.evolution.watch import check_non_degradation


def test_regression_triggers_auto_rollback():
    clear_archive()
    promos = [{
        "id": "promo1",
        "status": "observing",
        "proposal_id": "prop1",
        "target_kind": "config",
        "baseline": {"metric": "engagement_rate", "value": 0.5},
        "hypothesis": "{'kind': 'config_change'}",
    }]
    actions = check_non_degradation(promos, {"engagement_rate": 0.1, "_samples": 100})
    assert actions
    assert promos[0]["status"] == "rolled_back"
    assert any(a.get("outcome") == "regressed" for a in list_archive())
