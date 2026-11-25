"""Synthetic DB fixtures exercise real wallet/ledger code, with no provider calls."""
from datetime import timedelta

import pytest


def founder(person_id, venture_id, workspace_id):
    from app.credits._txn import naive_utc, session_factory
    from app.db.models import Person, Venture, Workspace
    now = naive_utc()
    with session_factory()() as session:
        if not session.get(Person, person_id):
            session.add(Person(id=person_id, display_name="Test founder", created_at=now))
            session.flush()
        if not session.get(Workspace, workspace_id):
            session.add(Workspace(id=workspace_id, owner_id=person_id, kind="venture", name="Test",
                                  entry_mode="idea", created_at=now, updated_at=now))
            session.flush()
        session.add(Venture(id=venture_id, workspace_id=workspace_id, name="Test", raw_idea="Test", stage="draft", gtm_order=0, daily_token_budget=10000, created_at=now, updated_at=now))
        session.commit()


def test_one_signup_grant_across_ventures_and_consumer(credits_env):
    from app.credits.accounts import ensure_account, wallet_for
    founder("per_one", "ven_one", "ws_one")
    founder("per_one", "ven_two", "ws_two")
    one = ensure_account("ven_one")
    two = ensure_account("ven_two")
    consumer = ensure_account("consumer:test", owner_person_id="per_one")
    assert one.id == two.id == consumer.id == wallet_for("per_one").id
    assert one.scope == "person" and one.venture_id is None and one.workspace_id is None
    assert wallet_for("per_one").granted_micro == 50_000_000


def test_acting_member_spends_own_wallet_viewer_uses_owner(credits_env):
    from app.credits.accounts import account_for_venture, wallet_for
    from app.credits._txn import naive_utc, session_factory
    from app.db.models import Person, WorkspaceMember
    from app.tenancy import bind
    founder("per_owner", "ven_shared", "ws_shared")
    with session_factory()() as session:
        session.add_all([Person(id="per_actor", display_name="Actor", created_at=naive_utc()),
                         Person(id="per_viewer", display_name="Viewer", created_at=naive_utc())])
        session.flush()
        session.add_all([WorkspaceMember(id="member_actor", workspace_id="ws_shared", person_id="per_actor", role="contributor", created_at=naive_utc()),
                         WorkspaceMember(id="member_viewer", workspace_id="ws_shared", person_id="per_viewer", role="viewer", created_at=naive_utc())])
        session.commit()
    owner = wallet_for("per_owner")
    actor = wallet_for("per_actor")
    assert account_for_venture("ven_shared", acting_person_id="per_actor").id == actor.id
    assert account_for_venture("ven_shared", acting_person_id="per_viewer").id == owner.id
    assert account_for_venture("ven_shared", acting_person_id="per_stranger").id == owner.id
    with bind(workspace_id="ws_shared", venture_id="ven_shared", actor="per_actor"):
        assert account_for_venture("ven_shared").id == actor.id


def legacy(session, person, vid, wid, account_id, grant=1000):
    from app.credits._txn import naive_utc
    from app.credits.ledger import append
    from app.db.models import CreditAccount
    row = CreditAccount(id=account_id, scope="venture", owner_person_id=person, venture_id=vid, workspace_id=wid,
        granted_micro=grant, spent_micro=0, reserved_micro=0, status="active", version=0,
        created_at=naive_utc(), updated_at=naive_utc())
    session.add(row)
    session.flush()
    append(session, account=row, kind="grant", amount_micro=grant, reason="legacy grant",
           actor_kind="system", actor_id="system", idempotency_key="initial:" + vid)
    return row


def test_migration_conserves_balance_hashes_and_is_idempotent(credits_env):
    from app.credits.accounts import wallet_for
    from app.credits._txn import naive_utc, session_factory
    from app.credits.ledger import verify_chain
    from app.credits.migration import migrate_wallets
    from app.db.models import CreditReservation
    founder("per_old", "ven_old_a", "ws_old")
    founder("per_old", "ven_old_b", "ws_old")
    with session_factory()() as session:
        a = legacy(session, "per_old", "ven_old_a", "ws_old", "cra_old_a", grant=4000)
        legacy(session, "per_old", "ven_old_b", "ws_old", "cra_old_b", grant=6000)
        a.reserved_micro = 500
        session.add(CreditReservation(id="crv_stale", account_id=a.id, amount_micro=500, status="active",
            purpose="test", clamped=0, created_at=naive_utc(), expires_at=naive_utc() - timedelta(seconds=1)))
        session.commit()
    with session_factory()() as session:
        result = migrate_wallets(session)
        session.commit()
    assert result == {"wallets": 1, "transferred_micro": 10000, "expired_holds": 1, "deferred_accounts": []}
    wallet = wallet_for("per_old")
    assert wallet.granted_micro == 10000
    assert wallet.spent_micro == wallet.reserved_micro == 0
    for account_id in (wallet.id, "cra_old_a", "cra_old_b"):
        assert verify_chain(account_id) == (True, None)
    with session_factory()() as session:
        rerun = migrate_wallets(session)
        session.commit()
        assert session.get(CreditReservation, "crv_stale").status == "expired"
    assert rerun["transferred_micro"] == 0
    assert wallet_for("per_old").granted_micro == 10000


def test_migration_defers_uncertain_hold_without_moving_its_value(credits_env):
    from app.credits._txn import naive_utc, session_factory
    from app.credits.accounts import wallet_for
    from app.credits.policy import wallet_phase
    from app.credits.migration import migrate_wallets
    from app.db.models import CreditAccount, CreditReservation
    founder("per_pending", "ven_pending", "ws_pending")
    with session_factory()() as session:
        a = legacy(session, "per_pending", "ven_pending", "ws_pending", "cra_pending")
        a.reserved_micro = 100
        session.add(CreditReservation(id="crv_pending", account_id=a.id, amount_micro=100, status="reconciling",
            purpose="test", clamped=0, created_at=naive_utc(), expires_at=naive_utc() - timedelta(days=1)))
        session.commit()
    with session_factory()() as session:
        result = migrate_wallets(session)
        session.commit()
        assert result["transferred_micro"] == 0
        assert result["deferred_accounts"] == [{"account_id": "cra_pending", "holds": [
            {"id": "crv_pending", "status": "reconciling", "amount_micro": 100}]}]
        a = session.get(CreditAccount, "cra_pending")
        assert a.granted_micro == 1000 and a.reserved_micro == 100 and a.status == "active"
    assert wallet_phase() == "person"
    assert wallet_for("per_pending").granted_micro == 0


def test_exempt_grant_is_50000_once(credits_env, monkeypatch):
    from app.credits.accounts import wallet_for
    founder("per_exempt", "ven_exempt", "ws_exempt")
    monkeypatch.setenv("SUPYMEM_VENTURE_LIMIT_EXEMPT_PERSON_IDS", "per_exempt")
    assert wallet_for("per_exempt").granted_micro == 500_000_000
    assert wallet_for("per_exempt").granted_micro == 500_000_000


def test_missing_wallet_refuses_platform_call_by_default(credits_env, monkeypatch):
    from app.credits.errors import CreditsUnavailable
    from app.credits.gate import charge_sync
    monkeypatch.delenv("LLM_CREDITS_REQUIRE_GATE", raising=False)
    with pytest.raises(CreditsUnavailable):
        with charge_sync(workspace_id="missing", purpose="test", estimate_micro=100):
            pytest.fail("provider must not run")


def test_third_venture_is_allowed(credits_env):
    from app.ventures.quota import assert_can_create_venture
    for n in range(3):
        founder("per_many", f"ven_many_{n}", "ws_many")
        assert_can_create_venture("per_many")
