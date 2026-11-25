"""Migrated PostgreSQL general profile retirement preserves bounded receipts."""
import pytest

from tests.integration.test_postgres_auth_security import pg_database as pg_database  # noqa: PLC0414
from tests.integration.test_postgres_device_orders import journey_db as journey_db  # noqa: PLC0414
from tests.integration.test_postgres_device_orders import prepare


@pytest.mark.parametrize("stage", ["pending", "approved", "claimed"])
@pytest.mark.parametrize("origin", ["https://supply.example.test", "https://recipes.example.test"])
def test_general_default_off_blocks_saved_authority_without_erasing_receipts(journey_db, monkeypatch, stage, origin):
    from app.build import device_effects as effects, device_invocations as calls
    from app.db.models import CreditAccount
    from coding_agents.jev_decision import Candidate
    d = journey_db
    svc, caller, baseline = prepare(d)
    svc.transition(d["vid"], caller, baseline["id"], baseline["review_digest"], "cancel")
    manifest = svc.Manifest.model_validate({**baseline["manifest"], "applications": ["com.google.Chrome"], "origins": [origin],
        "actions": ["browser.read", "browser.interact"], "general_browser_verification": {
            "operation": "general_goal_v1", "start_url": origin + "/"}})
    monkeypatch.setenv(svc.GENERAL_BROWSER_EXECUTION_ENV, "1")
    order = svc.prepare(d["vid"], caller, manifest)
    args = (d["vid"], caller, order["id"])
    if stage in {"approved", "claimed"}:
        order = svc.transition(*args, order["review_digest"], "approve")
    if stage == "claimed":
        order = svc.transition(*args, order["review_digest"], "claim")
        calls.admit(*args, invocation_id="owned_call", request_digest="a" * 64, quote_micro=100)
        calls.record_result(*args, invocation_id="owned_call", actual_micro=42)
        effects.save_proposal(*args, invocation_id="owned_call", candidate=Candidate("click", "c0", "c" * 64))
        assert effects.admit(*args, invocation_id="owned_call", observation_digest="c" * 64, effect_digest="e" * 64)
    before = svc.retrieve(*args)
    with d["factory"]() as session:
        balance = [(a.reserved_micro, a.spent_micro) for a in session.query(CreditAccount).all()]
    monkeypatch.delenv(svc.GENERAL_BROWSER_EXECUTION_ENV)
    with pytest.raises(ValueError, match="^device_general_browser_execution_unavailable$"):
        svc.prepare(d["vid"], caller, manifest, recover_active=True)
    for action in ("approve", "claim"):
        with pytest.raises(ValueError, match="^device_general_browser_execution_unavailable$"):
            svc.transition(*args, order["review_digest"], action)
    if stage == "claimed":
        with pytest.raises(ValueError, match="^device_general_browser_execution_unavailable$"):
            calls.check_current(*args)
        with pytest.raises(ValueError, match="^device_general_browser_execution_unavailable$"):
            calls.admit(*args, invocation_id="new_call", request_digest="b" * 64, quote_micro=100)
        with pytest.raises(ValueError, match="^device_general_browser_execution_unavailable$"):
            effects.admit(*args, invocation_id="owned_call", observation_digest="c" * 64, effect_digest="e" * 64)
    assert svc.retrieve(*args) == before
    with d["factory"]() as session:
        assert balance == [(a.reserved_micro, a.spent_micro) for a in session.query(CreditAccount).all()]
    if stage == "claimed":
        effects.receipt(*args, invocation_id="owned_call", effect_digest="e" * 64, outcome="uncertain")
        calls.record_result(*args, invocation_id="owned_call", actual_micro=42)
        assert svc.retrieve(*args)["invocations"]["owned_call"]["effect"]["state"] == "uncertain"
    assert svc.transition(*args, order["review_digest"], "cancel")["state"] == "cancelled"
