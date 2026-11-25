"""Synthetic staged wallet rollout; no provider, deployment or environment cutover."""
import json

import pytest
from sqlalchemy import select


@pytest.fixture
def founder(credits_env):
    from tests.unit.credits.test_founder_wallet import founder as seed
    seed("per_bridge", "ven_bridge", "ws_bridge")
    seed("per_bridge", "ven_bridge_other", "ws_bridge")
    return "per_bridge", "ven_bridge", "ws_bridge"


def phase_marker(phase, session=None):
    """Test-only equivalent of the migration's private DB update."""
    from app.credits._txn import naive_utc, session_factory
    from app.credits.policy import WALLET_PHASE_KEY
    from app.db.models import CreditPolicy
    if session is None:
        with session_factory()() as owned:
            phase_marker(phase, owned)
            owned.commit()
        return
    row = session.get(CreditPolicy, WALLET_PHASE_KEY)
    if phase is None:
        if row:
            session.delete(row)
    elif row:
        row.value_json = json.dumps(phase)
    else:
        session.add(CreditPolicy(key=WALLET_PHASE_KEY, value_json=json.dumps(phase),
                                 updated_at=naive_utc(), updated_by="synthetic-migration"))
    session.flush()


def test_legacy_phase_gives_one_new_starter_and_keeps_separate_custody(founder, monkeypatch):
    from app.credits._txn import session_factory
    from app.credits.accounts import ensure_account, get_account, wallet_for
    from app.credits.errors import CreditsUnavailable
    from app.credits.policy import wallet_phase
    from app.credits.units import credits_to_micro
    from app.db.models import CreditAccount, CreditLedgerEntry

    person, venture, workspace = founder
    phase_marker("legacy")
    monkeypatch.setenv("SUPYMEM_FOUNDER_WALLET_PHASE", "person")
    first = ensure_account(venture, workspace_id=workspace, owner_person_id=person)
    second = ensure_account("ven_bridge_other", workspace_id=workspace, owner_person_id=person)
    assert wallet_phase() == "legacy"
    assert first.id != second.id and first.scope == second.scope == "venture"
    assert first.granted_micro == credits_to_micro(5000)
    assert second.granted_micro == 0
    assert ensure_account(venture).id == get_account(venture).id == first.id
    assert wallet_for(person).id in {first.id, second.id}
    with pytest.raises(CreditsUnavailable):
        wallet_for("per_no_legacy_account")
    with session_factory()() as session:
        assert session.scalar(select(CreditAccount.id).where(CreditAccount.scope == "person")) is None
        grants = list(session.scalars(select(CreditLedgerEntry.idempotency_key)))
        assert sorted(grants) == ["initial:ven_bridge"]


def test_missing_marker_with_legacy_rows_preserves_launch_topup(founder):
    from app.credits._txn import naive_utc, session_factory
    from app.credits.accounts import ensure_account
    from app.credits.grants import grant
    from app.credits.ledger import verify_chain
    from app.credits.policy import WALLET_PHASE_KEY, wallet_phase
    from app.credits.units import credits_to_micro
    from app.db.models import CreditAccount, CreditLedgerEntry, CreditPolicy

    person, venture, workspace = founder
    phase_marker(None)
    now = naive_utc()
    with session_factory()() as session:
        session.add(CreditAccount(id="cra_bridge_old", scope="venture", owner_person_id=person,
                                 venture_id=venture, workspace_id=workspace, status="active",
                                 granted_micro=0, spent_micro=0, reserved_micro=0, version=0,
                                 created_at=now, updated_at=now))
        session.commit()
    grant(account_id="cra_bridge_old", amount_micro=credits_to_micro(500), reason="Synthetic pre-launch grant",
          actor_kind="system", actor_id="test", idempotency_key="initial:" + venture)
    assert wallet_phase() == "legacy"
    assert ensure_account(venture).granted_micro == credits_to_micro(5000)
    assert ensure_account(venture).granted_micro == credits_to_micro(5000)
    with session_factory()() as session:
        assert session.get(CreditPolicy, WALLET_PHASE_KEY) is None
        assert list(session.scalars(select(CreditLedgerEntry.idempotency_key).order_by(CreditLedgerEntry.seq))) == [
            "initial:ven_bridge", "launch-allowance:ven_bridge:5000"]
    assert verify_chain("cra_bridge_old") == (True, None)


def test_fresh_database_bootstraps_marker_only_when_ensuring_wallet(founder):
    from app.credits._txn import session_factory
    from app.credits.accounts import ensure_account, wallet_for
    from app.credits.policy import WALLET_PHASE_KEY, wallet_phase
    from app.credits.units import credits_to_micro
    from app.db.models import CreditPolicy

    person, venture, _ = founder
    phase_marker(None)
    assert wallet_phase() == "person"
    with session_factory()() as session:
        assert session.get(CreditPolicy, WALLET_PHASE_KEY) is None
    wallet = ensure_account(venture)
    assert wallet.scope == "person" and wallet.granted_micro == credits_to_micro(5000)
    assert ensure_account("ven_bridge_other").id == wallet_for(person).id == wallet.id
    with session_factory()() as session:
        assert session.get(CreditPolicy, WALLET_PHASE_KEY).value_json == '"person"'


def test_legacy_workspace_lookup_preserves_accounts_without_venture_rows(credits_env):
    from app.credits._txn import naive_utc, session_factory
    from app.credits.accounts import account_for_workspace, ensure_account, founder_snapshot_for_workspace, get_account
    from app.db.models import Person, Venture, Workspace

    phase_marker("legacy")
    # Old first-look quotations may own a workspace-tagged account before a Venture exists.
    with session_factory()() as session:
        now = naive_utc()
        session.add(Person(id="per_firstlook", display_name="Synthetic owner", created_at=now))
        session.flush()
        session.add(Workspace(id="ws_firstlook_synthetic", owner_id="per_firstlook", kind="venture",
                              name="First look", entry_mode="idea", created_at=now, updated_at=now))
        session.commit()
    account = ensure_account("firstlook:synthetic", workspace_id="ws_firstlook_synthetic")
    assert account.scope == "venture"
    assert account_for_workspace("ws_firstlook_synthetic").id == get_account("firstlook:synthetic").id == account.id
    assert founder_snapshot_for_workspace("ws_firstlook_synthetic")["available"] == 5000
    with session_factory()() as session:
        assert session.get(Venture, "firstlook:synthetic") is None
    phase_marker("person")
    assert account_for_workspace("ws_firstlook_synthetic") is None
    assert get_account("firstlook:synthetic") is None
    assert founder_snapshot_for_workspace("ws_firstlook_synthetic") is None


def test_same_bridge_resolves_person_wallets_after_transactional_transfer(founder, monkeypatch):
    from app.credits._txn import naive_utc, session_factory
    from app.credits.accounts import account_can_spend, account_for_venture, ensure_account, get_account, wallet_for
    from app.credits.migration import migrate_wallets
    from app.credits.policy import get_policy, person_wallet_enabled, wallet_phase
    from app.credits.units import credits_to_micro
    from app.db.models import CreditAccount, CreditLedgerEntry, Person, WorkspaceMember
    from app.tenancy import bind

    person, venture, workspace = founder
    phase_marker("legacy")
    # Authentic pre-policy legacy balances remain transferable; new ventures
    # no longer manufacture this second historical allowance.
    from tests.unit.credits.test_founder_wallet import legacy
    with session_factory()() as session:
        legacy(session, person, venture, workspace, "cra_historical_one", grant=credits_to_micro(5000))
        legacy(session, person, "ven_bridge_other", workspace, "cra_historical_two", grant=credits_to_micro(5000))
        session.commit()
    old = ensure_account(venture, workspace_id=workspace, owner_person_id=person)
    ensure_account("ven_bridge_other", workspace_id=workspace, owner_person_id=person)
    with session_factory()() as session:
        session.add(Person(id="per_bridge_actor", display_name="Synthetic contributor", created_at=naive_utc()))
        session.flush()
        session.add(WorkspaceMember(id="member_bridge_actor", person_id="per_bridge_actor", workspace_id=workspace,
                                    role="contributor", created_at=naive_utc()))
        session.commit()
    assert account_for_venture(venture, acting_person_id="per_bridge_actor").id == old.id
    get_policy("default_venture_credits")  # Prime the unrelated editable-policy cache.
    with session_factory()() as session:
        assert wallet_phase(session) == "legacy"
        migrate_wallets(session)
        phase_marker("person", session)
        assert wallet_phase(session) == "person" and person_wallet_enabled(session)
        session.commit()
    monkeypatch.setenv("SUPYMEM_FOUNDER_WALLET_PHASE", "legacy")
    monkeypatch.setenv("FOUNDER_WALLET_PHASE", "legacy")
    wallet = wallet_for(person)
    assert wallet.scope == "person" and wallet.granted_micro == credits_to_micro(10000)
    assert ensure_account(venture).id == ensure_account("ven_bridge_other").id == get_account(venture).id == wallet.id
    actor = wallet_for("per_bridge_actor")
    assert actor.granted_micro == credits_to_micro(5000)
    assert account_for_venture(venture, acting_person_id="per_bridge_actor").id == actor.id
    with bind(workspace_id=workspace, venture_id=venture, actor="per_bridge_actor"):
        assert get_account(venture).id == actor.id
    with session_factory()() as session:
        source = session.get(CreditAccount, old.id)
        assert not account_can_spend(session, source, workspace, venture)
        assert account_can_spend(session, source, workspace, venture, receipt_only=True)
        assert session.scalar(select(CreditLedgerEntry.id).where(
            CreditLedgerEntry.idempotency_key == "initial:person:" + person)) is None
    assert wallet_phase() == "person"


@pytest.mark.parametrize("value", ["legacy", "person"])
def test_public_policy_cannot_flip_the_private_wallet_phase(founder, value):
    from app.credits.policy import WALLET_PHASE_KEY, get_policy, set_policy, wallet_phase

    phase_marker("person")
    with pytest.raises(ValueError, match="drained financial migration"):
        set_policy(WALLET_PHASE_KEY, value, updated_by="synthetic_admin")
    assert wallet_phase() == "person"
    assert get_policy(WALLET_PHASE_KEY) is None


def test_invalid_phase_marker_refuses_billing(founder):
    from app.credits.accounts import ensure_account, get_account
    from app.credits.errors import CreditsUnavailable
    from app.credits.policy import wallet_phase

    phase_marker("invalid")
    for read in (wallet_phase, lambda: get_account(founder[1]), lambda: ensure_account(founder[1])):
        with pytest.raises(CreditsUnavailable, match="wallet phase needs reconciliation"):
            read()


def test_persisted_consumer_starter_prevents_new_venture_grant_even_without_owner_field(founder):
    from datetime import timedelta
    from app.credits._txn import naive_utc, session_factory
    from app.credits.accounts import ensure_account
    from app.credits.ledger import verify_chain
    from app.credits.units import credits_to_micro
    from app.db.models import ConsumerUser, CreditAccount, CreditLedgerEntry

    person, venture, workspace = founder
    phase_marker("legacy")
    now = naive_utc()
    with session_factory()() as session:
        session.add(ConsumerUser(id="consumer_bridge", cognito_sub="sub_bridge", email="bridge@example.invalid",
            person_id=person, credit_account_id=None, plan="trial", plan_started_at=now,
            plan_expires_at=now + timedelta(days=14), status="active", created_at=now, updated_at=now))
        session.commit()
    consumer = ensure_account("consumer:sub_bridge", owner_person_id=person)
    assert consumer.granted_micro == credits_to_micro(5000)
    with session_factory()() as session:
        session.get(ConsumerUser, "consumer_bridge").credit_account_id = consumer.id
        # Historical consumer linkage remains authoritative when owner backfill
        # was absent; an extra venture cannot reset that funded entitlement.
        session.get(CreditAccount, consumer.id).owner_person_id = None
        before = list(session.execute(select(CreditLedgerEntry.id, CreditLedgerEntry.entry_hash)))
        session.commit()
    account = ensure_account(venture, workspace_id=workspace, owner_person_id=person)
    assert account.id != consumer.id and account.scope == "venture" and account.granted_micro == 0
    with session_factory()() as session:
        assert list(session.execute(select(CreditLedgerEntry.id, CreditLedgerEntry.entry_hash))) == before
        assert session.get(CreditAccount, consumer.id).granted_micro == credits_to_micro(5000)
    assert verify_chain(consumer.id) == (True, None)


def test_zero_funded_additional_account_cannot_regrant_through_ensure_snapshot_or_worker_call(founder):
    from app.credits._txn import session_factory
    from app.credits.accounts import ensure_account, founder_snapshot, get_account
    from app.credits.reserve import reserve, settle
    from app.credits.units import credits_to_micro
    from app.db.models import CreditAccount, CreditLedgerEntry, CreditReservation

    person, venture, workspace = founder
    phase_marker("legacy")
    funded = ensure_account(venture, workspace_id=workspace, owner_person_id=person)
    empty = ensure_account("ven_bridge_other", workspace_id=workspace, owner_person_id=person)
    paid = reserve(account_id=funded.id, amount_micro=300, purpose="synthetic.completed",
                   workspace_id=workspace, venture_id=venture, request_id="completed_bridge")
    settle(paid, actual_micro=300)
    protected = reserve(account_id=funded.id, amount_micro=700, purpose="synthetic.unknown",
                        workspace_id=workspace, venture_id=venture, request_id="protected_bridge")
    with session_factory()() as session:
        before = list(session.execute(select(CreditLedgerEntry.id, CreditLedgerEntry.entry_hash)))
        hold_before = (session.get(CreditReservation, protected.reservation_id).status,
                       session.get(CreditReservation, protected.reservation_id).amount_micro)
    for _ in range(3):
        assert ensure_account("ven_bridge_other").id == empty.id  # Existing worker self-heal shape.
        assert ensure_account("ven_bridge_other", workspace_id=workspace, owner_person_id=person).granted_micro == 0
        assert founder_snapshot("ven_bridge_other", workspace_id=workspace, owner_person_id=person)["available"] == 0
        assert get_account("ven_bridge_other").id == empty.id
    with session_factory()() as session:
        assert list(session.execute(select(CreditLedgerEntry.id, CreditLedgerEntry.entry_hash))) == before
        original = session.get(CreditAccount, funded.id)
        assert (original.granted_micro, original.spent_micro, original.reserved_micro) == (credits_to_micro(5000), 300, 700)
        assert session.get(CreditAccount, empty.id).granted_micro == 0
        hold = session.get(CreditReservation, protected.reservation_id)
        assert (hold.status, hold.amount_micro) == hold_before == ("active", 700)


def test_separate_founder_receives_first_legacy_starter_independently(founder):
    from app.credits.accounts import ensure_account
    from app.credits.units import credits_to_micro
    from tests.unit.credits.test_founder_wallet import founder as seed

    phase_marker("legacy")
    first = ensure_account(founder[1])
    seed("per_independent", "ven_independent", "ws_independent")
    other = ensure_account("ven_independent", workspace_id="ws_independent", owner_person_id="per_independent")
    assert first.id != other.id
    assert first.granted_micro == other.granted_micro == credits_to_micro(5000)


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("claim", ["owner", "workspace"])
def test_foreign_owner_or_workspace_claim_refuses_before_account_or_grant(founder, existing, claim):
    from app.credits._txn import session_factory
    from app.credits.accounts import ensure_account
    from app.credits.errors import CreditsUnavailable
    from app.db.models import CreditAccount, CreditLedgerEntry
    from tests.unit.credits.test_founder_wallet import founder as seed

    person, venture, workspace = founder
    phase_marker("legacy")
    seed("per_foreign", "ven_foreign", "ws_foreign")
    if existing:
        ensure_account(venture, workspace_id=workspace, owner_person_id=person)
    with session_factory()() as session:
        rows = list(session.execute(select(CreditAccount.id, CreditAccount.owner_person_id,
            CreditAccount.workspace_id, CreditAccount.granted_micro, CreditAccount.spent_micro, CreditAccount.reserved_micro)))
        ledger = list(session.execute(select(CreditLedgerEntry.id, CreditLedgerEntry.entry_hash)))
    with pytest.raises(CreditsUnavailable, match="mismatch"):
        ensure_account(venture, workspace_id="ws_foreign" if claim == "workspace" else workspace,
                       owner_person_id="per_foreign" if claim == "owner" else person)
    with session_factory()() as session:
        assert list(session.execute(select(CreditAccount.id, CreditAccount.owner_person_id,
            CreditAccount.workspace_id, CreditAccount.granted_micro, CreditAccount.spent_micro, CreditAccount.reserved_micro))) == rows
        assert list(session.execute(select(CreditLedgerEntry.id, CreditLedgerEntry.entry_hash))) == ledger


@pytest.mark.parametrize("claimed_owner", [False, True])
def test_unknown_ownerless_resource_never_mints_starter_from_supplied_person(founder, claimed_owner):
    from app.credits._txn import session_factory
    from app.credits.accounts import ensure_account
    from app.db.models import CreditAccount, CreditLedgerEntry

    phase_marker("legacy")
    account = ensure_account("unknown:synthetic", workspace_id="ws_missing",
                             owner_person_id=founder[0] if claimed_owner else None)
    assert account.granted_micro == account.spent_micro == account.reserved_micro == 0
    assert ensure_account("unknown:synthetic").granted_micro == 0
    with session_factory()() as session:
        assert session.get(CreditAccount, account.id).granted_micro == 0
        assert session.scalar(select(CreditLedgerEntry.id)) is None


def test_third_venture_uses_own_zero_account_and_cannot_spend_other_workspace_funds(founder):
    from app.credits._txn import session_factory
    from app.credits.accounts import account_can_spend, ensure_account, get_account
    from app.db.models import CreditAccount, CreditLedgerEntry
    from tests.unit.credits.test_founder_wallet import founder as seed

    person, venture, workspace = founder
    phase_marker("legacy")
    funded = ensure_account(venture, workspace_id=workspace, owner_person_id=person)
    ensure_account("ven_bridge_other", workspace_id=workspace, owner_person_id=person)
    seed(person, "ven_third", "ws_third")
    third = ensure_account("ven_third", workspace_id="ws_third", owner_person_id=person)
    assert get_account("ven_third").id == third.id != funded.id
    assert third.granted_micro == 0
    with session_factory()() as session:
        original = session.get(CreditAccount, funded.id)
        empty = session.get(CreditAccount, third.id)
        assert not account_can_spend(session, original, "ws_third", "ven_third")
        assert account_can_spend(session, empty, "ws_third", "ven_third")
        assert not account_can_spend(session, empty, workspace, venture)
        assert list(session.scalars(select(CreditLedgerEntry.idempotency_key))) == ["initial:" + venture]


def test_empty_historical_account_keeps_first_entitlement_from_later_empty_account(founder):
    from datetime import timedelta
    from app.credits._txn import naive_utc, session_factory
    from app.credits.accounts import ensure_account
    from app.credits.units import credits_to_micro
    from app.db.models import CreditAccount, CreditLedgerEntry

    person, venture, workspace = founder
    phase_marker("legacy")
    now = naive_utc()
    with session_factory()() as session:
        session.add(CreditAccount(id="cra_old_empty", scope="venture", owner_person_id=person,
            venture_id=venture, workspace_id=workspace, status="active", granted_micro=0,
            spent_micro=0, reserved_micro=0, version=0, created_at=now - timedelta(days=1), updated_at=now))
        session.commit()
    later = ensure_account("ven_bridge_other", workspace_id=workspace, owner_person_id=person)
    assert later.granted_micro == 0
    first = ensure_account(venture, workspace_id=workspace, owner_person_id=person)
    assert first.id == "cra_old_empty" and first.granted_micro == credits_to_micro(5000)
    assert ensure_account("ven_bridge_other").granted_micro == 0
    with session_factory()() as session:
        assert list(session.scalars(select(CreditLedgerEntry.idempotency_key))) == ["initial:" + venture]


def test_historical_workspace_funding_remains_consumed_after_owner_backfill_gap_and_revocation(founder):
    from app.credits._txn import session_factory
    from app.credits.accounts import ensure_account
    from app.credits.grants import revoke
    from app.credits.ledger import verify_chain
    from app.credits.units import credits_to_micro
    from app.db.models import CreditAccount, CreditLedgerEntry
    from tests.unit.credits.test_founder_wallet import legacy

    person, venture, workspace = founder
    phase_marker("legacy")
    with session_factory()() as session:
        old = legacy(session, person, venture, workspace, "cra_old_no_owner", grant=credits_to_micro(5000))
        old.owner_person_id = None
        session.commit()
    revoke(account_id="cra_old_no_owner", amount_micro=credits_to_micro(5000), reason="Synthetic full revocation",
           actor_id="synthetic_admin", idempotency_key="revoke:historical_bridge")
    with session_factory()() as session:
        before = list(session.execute(select(CreditLedgerEntry.id, CreditLedgerEntry.entry_hash)))
    extra = ensure_account("ven_bridge_other", workspace_id=workspace, owner_person_id=person)
    assert extra.granted_micro == 0
    assert ensure_account("ven_bridge_other").granted_micro == 0
    with session_factory()() as session:
        original = session.get(CreditAccount, "cra_old_no_owner")
        assert original.owner_person_id is None
        assert (original.granted_micro, original.spent_micro, original.reserved_micro) == (credits_to_micro(5000), credits_to_micro(5000), 0)
        assert list(session.execute(select(CreditLedgerEntry.id, CreditLedgerEntry.entry_hash))) == before
    assert verify_chain("cra_old_no_owner") == (True, None)


def test_person_exempt_allowance_remains_once_per_person_without_count_exemption(founder, monkeypatch):
    from app.credits._txn import session_factory
    from app.credits.accounts import ensure_account
    from app.credits.units import credits_to_micro
    from app.db.models import CreditLedgerEntry

    person, venture, _ = founder
    phase_marker("person")
    monkeypatch.setenv("SUPYMEM_VENTURE_LIMIT_EXEMPT_PERSON_IDS", person)
    first = ensure_account(venture)
    second = ensure_account("ven_bridge_other")
    assert first.id == second.id and first.granted_micro == second.granted_micro == credits_to_micro(50000)
    with session_factory()() as session:
        before = list(session.execute(select(CreditLedgerEntry.id, CreditLedgerEntry.entry_hash)))
        assert sorted(session.scalars(select(CreditLedgerEntry.idempotency_key))) == [
            "grant:exempt-50k:" + person, "initial:person:" + person]
    assert ensure_account(venture).granted_micro == credits_to_micro(50000)
    with session_factory()() as session:
        assert list(session.execute(select(CreditLedgerEntry.id, CreditLedgerEntry.entry_hash))) == before
