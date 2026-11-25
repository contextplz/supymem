"""Charged-consumption rewards and private, person-scoped referral nudges."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from tests.company_os_harness import load_company_os_app


@pytest.fixture(params=["RIVER234", "STONE678"])
def program(request, tmp_path, monkeypatch):
    monkeypatch.delenv("SUPYMEM_MODE", raising=False)
    load_company_os_app()
    from app import state
    from app.db.models import Base, Person, CreditAccount, CreditPolicy, Workspace, Venture, ChatConversation
    from app.db.supymem_models import BetaCode, BetaCodeRedemption, DesktopDevice, ReferralProgramState
    from app.credits import ledger
    from app.credits._txn import naive_utc
    from app.credits.units import credits_to_micro
    from app.analytics import usage
    from app.consumer import referrals
    from app.chat import milestones
    engine = create_engine(f"sqlite:///{tmp_path}/rewards.db", connect_args={"check_same_thread": False, "timeout": 30})
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    original_db = state.db
    for module in list(sys.modules.values()):
        if module and getattr(module, "__name__", "").startswith("app.") and getattr(module, "db", None) is original_db:
            monkeypatch.setattr(module, "db", lambda: factory)
    clock = [datetime(2026, 10, 24, 12, tzinfo=timezone.utc)]
    monkeypatch.setattr(ledger, "naive_utc", lambda value=None: naive_utc(value or clock[0]))
    monkeypatch.setattr(usage, "utcnow", lambda: clock[0])
    monkeypatch.setattr(referrals, "utcnow", lambda: clock[0])
    monkeypatch.setattr(milestones, "utcnow", lambda: clock[0])
    monkeypatch.setattr(milestones, "manager_speaker", lambda _: {"name": "Charlie", "title": "Manager"})
    code = request.param
    with factory() as session:
        session.add_all([Person(id=person, display_name="Founder", email=f"{person}@private.test",
            timezone="America/Los_Angeles" if person == "usr_friend" else "UTC",
            created_at=clock[0] - timedelta(hours=1)) for person in ("usr_referrer", "usr_friend", "usr_member", "usr_new")])
        session.add(ReferralProgramState(id=1, grandfather_before=clock[0] - timedelta(days=1)))
        session.add(CreditPolicy(key="founder_wallet_phase", value_json='"person"', updated_at=naive_utc(clock[0]), updated_by="test"))
        session.add(BetaCode(code=code, owner_person_id="usr_referrer", label="Personal", active=True, max_uses=2,
            created_at=clock[0] - timedelta(hours=1)))
        session.flush()
        session.add(BetaCodeRedemption(person_id="usr_friend", user_id="usr_friend", code=code,
            created_at=clock[0] - timedelta(minutes=30)))
        session.add(DesktopDevice(id="dev_friend", person_id="usr_friend", name="Friend Mac", platform="macos"))
        session.add(DesktopDevice(id="dev_member", person_id="usr_member", name="Member Mac", platform="macos"))
        for person in ("usr_referrer", "usr_friend", "usr_member"):
            session.add(CreditAccount(id=person, scope="person", owner_person_id=person,
                granted_micro=credits_to_micro(50000), spent_micro=0, reserved_micro=0, version=0, status="active",
                created_at=naive_utc(clock[0]), updated_at=naive_utc(clock[0])))
        for venture in ("ven_first", "ven_second"):
            workspace = "wsp_" + venture
            session.add(Workspace(id=workspace, kind="company", name="Company", entry_mode="idea", owner_id="usr_friend",
                created_at=clock[0], updated_at=clock[0]))
            session.add(Venture(id=venture, workspace_id=workspace, name="Company", raw_idea="Idea", stage="draft",
                created_at=clock[0], updated_at=clock[0]))
            session.add(ChatConversation(id="chat_" + venture, workspace_id=workspace, venture_id=venture,
                creator_person_id="usr_member", visibility="private", title="Conversation", creation_key=venture,
                created_at=clock[0], updated_at=clock[0]))
        session.add(CreditAccount(id="legacy", scope="venture", venture_id="ven_first", workspace_id="wsp_ven_first",
            granted_micro=credits_to_micro(50000), spent_micro=0, reserved_micro=0, version=0, status="active",
            created_at=naive_utc(clock[0]), updated_at=naive_utc(clock[0])))
        session.commit()
    counter = [0]

    def write(credits, *, kind="spend", account_id="usr_friend", commit=True, reason="test receipt"):
        from app.db.session import serialize_writes
        with factory() as session:
            serialize_writes(session, key=account_id)
            account = session.get(CreditAccount, account_id)
            amount = credits_to_micro(credits)
            if amount < 0:
                account.spent_micro -= amount
            else:
                account.granted_micro += amount
            counter[0] += 1
            entry = ledger.append(session, account=account, kind=kind, amount_micro=amount, reason=reason,
                actor_kind="system", actor_id=None, idempotency_key=f"test:{counter[0]}")
            if commit:
                session.commit()
            return entry.id

    yield factory, referrals, usage, clock, code, write
    engine.dispose()


@pytest.mark.parametrize("domain", ["warehouse-routing.example", "family-cooking.example"])
def test_local_wallet_keeps_qualified_referral_pending_until_recovery(program, monkeypatch, domain):
    factory, referrals, usage, clock, code, write = program
    from app.db.models import CreditAccount
    from app.db.supymem_models import BetaCode, BetaCodeRedemption

    with factory() as session:
        session.get(BetaCode, code).label = domain
        before = session.get(CreditAccount, "usr_referrer").granted_micro
        session.commit()
    usage.record_process_heartbeat("usr_friend", "dev_friend", "test-build")
    write(-2000)
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    with factory() as session:
        assert referrals.reward(session, "usr_friend") is False
        receipt = session.get(BetaCodeRedemption, "usr_friend")
        assert receipt.qualified_at is not None and receipt.reward_ledger_id is None
        assert session.get(CreditAccount, "usr_referrer").granted_micro == before
    assert referrals.sweep_rewards() == 0
    monkeypatch.delenv("SUPYMEM_MODE")
    assert referrals.sweep_rewards() == 1
    assert referrals.sweep_rewards() == 0


def test_threshold_rollback_recovery_and_concurrent_sweeps(program):
    factory, referrals, usage, clock, code, write = program
    from app.db.models import CreditAccount, CreditLedgerEntry
    from app.db.supymem_models import BetaCode, BetaCodeRedemption
    from app.credits.units import credits_to_micro
    usage.record_process_heartbeat("usr_friend", "dev_friend", "test-build")
    write(-1999)
    with factory() as session:
        assert session.get(BetaCodeRedemption, "usr_friend").qualified_at is None
        assert referrals.summary(session, "usr_referrer")["referrals"][0]["used_credits"] == 1999
    write(-1, commit=False)
    assert referrals.sweep_rewards() == 0
    write(-1)
    with factory() as session:
        assert session.get(BetaCodeRedemption, "usr_friend").qualified_at is not None
        assert referrals.reward(session, "usr_friend")
        session.rollback()
    with factory() as session:
        assert session.get(BetaCodeRedemption, "usr_friend").reward_ledger_id is None
        assert session.get(CreditAccount, "usr_referrer").granted_micro == credits_to_micro(50000)
        session.get(BetaCodeRedemption, "usr_friend").qualified_at = None
        session.commit()
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(pool.map(lambda _: referrals.sweep_rewards(), range(2))) == 1
    assert referrals.sweep_rewards() == 0
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(CreditLedgerEntry).where(
            CreditLedgerEntry.idempotency_key == "referral-reward:usr_friend")) == 1
        assert session.get(CreditAccount, "usr_referrer").granted_micro == credits_to_micro(52000)
        summary = referrals.summary(session, "usr_referrer")
        assert summary["code"] == code and summary["share_url"].endswith("?ref=" + code)
        assert summary["referrals"][0]["status"] == "rewarded"
        assert summary["referrals"][0]["rewarded_at"].endswith("+00:00")
        assert "@private.test" not in str(summary) and "email" not in str(summary)
        legacy_code = "CLOUD234" if code == "RIVER234" else "FIELD678"
        session.add(BetaCode(code=legacy_code, owner_person_id="usr_referrer", label="Legacy", active=True,
            max_uses=None, created_at=clock[0]))
        session.flush()
        session.add(BetaCodeRedemption(person_id="usr_member", user_id="usr_member", code=legacy_code,
            created_at=clock[0] + timedelta(minutes=1)))
        summary = referrals.summary(session, "usr_referrer")
        assert summary["code"] == code
        assert {item["referral_id"] for item in summary["referrals"]} == {"usr_friend", "usr_member"}


def test_install_cutoff_adjustments_byok_and_unrelated_members(program):
    factory, referrals, usage, clock, code, write = program
    from app.db.supymem_models import BetaCodeRedemption
    write(-3000)
    clock[0] += timedelta(hours=1)
    with pytest.raises(Exception) as denied:
        usage.record_process_heartbeat("usr_friend", "dev_member", None)
    assert denied.value.status_code == 403
    usage.record_process_heartbeat("usr_friend", "dev_friend", None)
    with factory() as session:
        session.get(BetaCodeRedemption, "usr_friend").created_at = clock[0] + timedelta(minutes=30)
        session.commit()
    write(-500)
    clock[0] += timedelta(hours=1)
    for kind, amount in (("grant", 4000), ("refund", 3000), ("adjust", -4000), ("revoke", -2000), ("release", 1000)):
        write(amount, kind=kind)
    write(-4000, kind="adjust", reason="migrate_out")
    write(4000, kind="adjust", reason="migrate_in")
    write(0)
    write(-2000, account_id="usr_member")
    write(-1000, account_id="legacy")
    write(-999)
    with factory() as session:
        receipt = session.get(BetaCodeRedemption, "usr_friend")
        assert referrals.used_micro(session, receipt) == 19990000
        assert receipt.qualified_at is None
        first = receipt.installed_at
    clock[0] += timedelta(hours=1)
    usage.record_process_heartbeat("usr_friend", "dev_friend", None)
    with factory() as session:
        assert session.get(BetaCodeRedemption, "usr_friend").installed_at == first
    write(-1, account_id="legacy")
    assert referrals.sweep_rewards() == 1


def test_claim_replay_self_legacy_usage_and_private_summary(program):
    factory, referrals, usage, clock, code, write = program
    from app.api import beta_api
    from app.db.supymem_models import ReferralAdmission
    app = FastAPI()
    app.include_router(beta_api.router)
    client = TestClient(app)
    path = "/beta/referrals/claim"
    assert client.post(path, json={"code": code}, headers={"X-Subject": "usr_referrer"}).json() == {"detail": {"code": "self_invitation"}}
    for _ in range(2):
        response = client.post(path, json={"code": code.lower()}, headers={"X-Subject": "usr_new"})
        assert response.status_code == 200 and response.json() == {"ok": True, "referral_id": "usr_new"}
    assert client.post(path, json={"code": "OTHER789"}, headers={"X-Subject": "usr_new"}).status_code == 409
    assert client.post(path, json={"code": code, "person_id": "usr_new"}, headers={"X-Subject": "usr_member"}).status_code == 422
    assert client.get("/beta/referrals").status_code == 422
    for blank in ("", "   "):
        assert client.get("/beta/referrals", headers={"X-Subject": blank}).status_code == 401
        assert client.post(path, json={"code": code}, headers={"X-Subject": blank}).status_code == 401
    response = client.get("/beta/referrals", headers={"X-Subject": "usr_referrer"})
    assert response.status_code == 200 and len(response.json()["referrals"]) == 2
    assert "email" not in response.text and "@private.test" not in response.text
    assert client.get("/beta/referrals", headers={"X-Subject": "usr_member"}).json()["referrals"] == []
    with factory() as session:
        assert session.get(ReferralAdmission, "usr_member") is None
        session.add(ReferralAdmission(person_id="usr_member", created_at=clock[0], legacy=True))
        session.commit()
    assert client.post(path, json={"code": code}, headers={"X-Subject": "usr_member"}).json() == {"detail": {"code": "ineligible"}}


def _engaged(factory, clock, person="usr_member", ventures=("ven_first", "ven_second")):
    """Nudges go only to founders who have used Supymem: an account over a day old and a few messages."""
    from app.db.models import ChatMessage, Person
    with factory() as session:
        session.get(Person, person).created_at = clock[0] - timedelta(days=2)
        for venture in ventures:
            for index in range(3):
                session.add(ChatMessage(id=f"msg_{venture}_{index}", venture_id=venture, workspace_id="wsp_" + venture,
                    role="user", kind="text", body_md="hello", created_at=clock[0] - timedelta(hours=2)))
        session.commit()


def test_person_scoped_72_hour_nudge_current_chat_and_rollback(program, monkeypatch):
    factory, referrals, usage, clock, code, write = program
    _engaged(factory, clock)
    from app.chat.conversations import bind_conversation
    from app.tenancy import bind
    from app.db.models import ChatMessage
    from app.db.supymem_models import PersonReferralCode
    from app.chat.proactive import NUDGE, DAILY_NUDGES

    def nudge(venture):
        with bind(workspace_id="wsp_" + venture, venture_id=venture, actor="usr_member"):
            with bind_conversation(venture, "chat_" + venture) as conversation:
                return referrals.nudge(venture, "usr_member"), conversation.id

    assert nudge("ven_first")[0]
    assert not nudge("ven_second")[0]
    clock[0] += timedelta(hours=72) - timedelta(seconds=1)
    assert not nudge("ven_second")[0]
    clock[0] += timedelta(seconds=1)
    with monkeypatch.context() as patch:
        def fail_commit(_session):
            raise RuntimeError("commit failed")
        with bind(workspace_id="wsp_ven_second", venture_id="ven_second", actor="usr_member"):
            with bind_conversation("ven_second", "chat_ven_second"):
                patch.setattr(factory.class_, "commit", fail_commit)
                with pytest.raises(RuntimeError, match="commit failed"):
                    referrals.nudge("ven_second", "usr_member")
    posted, conversation_id = nudge("ven_second")
    assert posted
    with factory() as session:
        rows = list(session.scalars(select(ChatMessage).where(ChatMessage.kind == NUDGE).order_by(ChatMessage.created_at)))
        assert len(rows) == 2 and rows[-1].conversation_id == conversation_id
        assert '"action": "referrals.show"' in rows[-1].actions_json
        assert session.get(PersonReferralCode, "usr_member").last_nudged_at is not None
    clock[0] += timedelta(days=3, hours=10)
    assert not nudge("ven_first")[0]
    clock[0] += timedelta(hours=14)
    with factory() as session:
        last_nudged = session.get(PersonReferralCode, "usr_member").last_nudged_at
        for index in range(DAILY_NUDGES):
            session.add(ChatMessage(id=f"cap_{index}", venture_id="ven_first", workspace_id="wsp_ven_first",
                conversation_id="chat_ven_first", role="assistant", kind=NUDGE, body_md="Existing nudge",
                idempotency_key=f"cap:{index}", created_at=clock[0]))
        session.commit()
    assert not nudge("ven_first")[0]
    with factory() as session:
        assert session.get(PersonReferralCode, "usr_member").last_nudged_at == last_nudged


@pytest.mark.parametrize("idea", ["Factory inspection planning", "Family music lessons"])
def test_chat_open_never_interrupts_onboarding_with_referral_invites(program, monkeypatch, idea):
    factory, referrals, usage, clock, code, write = program
    _engaged(factory, clock)
    from app.chat import proactive
    from app.chat.conversations import bind_conversation
    from app.tenancy import bind
    from app.db.models import ChatMessage, Venture
    from app.db.supymem_models import PersonReferralCode
    opened = []
    monkeypatch.setattr(proactive, "claim_brief", lambda venture: opened.append(venture))
    with factory() as session:
        for venture in ("ven_first", "ven_second"):
            session.get(Venture, venture).raw_idea = idea
        session.commit()
    for venture in ("ven_first", "ven_second"):
        with bind(workspace_id="wsp_" + venture, venture_id=venture, actor="usr_member"):
            with bind_conversation(venture, "chat_" + venture):
                for stage in ("draft", "idea", "research", "build"):
                    with factory() as session:
                        session.get(Venture, venture).stage = stage
                        session.commit()
                    proactive.on_chat_open(venture)
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(ChatMessage).where(ChatMessage.kind == proactive.NUDGE)) == 0
        assert session.get(PersonReferralCode, "usr_member") is None
    assert opened == [venture for venture in ("ven_first", "ven_second") for _ in range(4)]


def test_unavailable_wallet_does_not_block_other_rewards_and_recovers(program, monkeypatch):
    factory, referrals, usage, clock, code, write = program
    from app.credits import accounts
    from app.credits.errors import CreditsUnavailable
    from app.db.supymem_models import BetaCode, BetaCodeRedemption
    pending_code = "CLOUD234" if code == "RIVER234" else "FIELD678"
    with factory() as session:
        session.get(BetaCode, code).owner_person_id = "usr_new"
        session.add(BetaCode(code=pending_code, owner_person_id="usr_referrer", label="Personal", active=True,
            max_uses=2, created_at=clock[0]))
        session.flush()
        session.add(BetaCodeRedemption(person_id="usr_member", user_id="usr_member", code=pending_code,
            created_at=clock[0] - timedelta(minutes=30)))
        session.commit()
    for person, device in (("usr_friend", "dev_friend"), ("usr_member", "dev_member")):
        usage.record_process_heartbeat(person, device, None)
        write(-2000, account_id=person)
    original = accounts.wallet_for
    unavailable = [True]

    def wallet_for(person_id, *, transaction=None):
        if person_id == "usr_new" and unavailable[0]:
            raise CreditsUnavailable("wallet is temporarily unavailable")
        return original(person_id, transaction=transaction)

    monkeypatch.setattr(accounts, "wallet_for", wallet_for)
    assert referrals.sweep_rewards() == 1
    with factory() as session:
        assert session.get(BetaCodeRedemption, "usr_friend").reward_ledger_id is None
        assert session.get(BetaCodeRedemption, "usr_member").reward_ledger_id is not None
    unavailable[0] = False
    assert referrals.sweep_rewards() == 1
    assert referrals.sweep_rewards() == 0


def test_a_broken_referral_check_never_blocks_a_spend(program, monkeypatch):
    factory, referrals, usage, clock, code, write = program
    from app.db.models import CreditLedgerEntry
    usage.record_process_heartbeat("usr_friend", "dev_friend", "test-build")

    def broken(session, account):
        raise RuntimeError("referral bookkeeping bug")
    with monkeypatch.context() as patch:  # scoped: the fixture's own patches stay in place
        patch.setattr(referrals, "qualify_spend", broken)
        write(-2000)  # the spend itself must still be recorded
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(CreditLedgerEntry).where(
            CreditLedgerEntry.kind == "spend")) >= 1
    assert referrals.sweep_rewards() == 1  # the sweep re-checks qualification and still pays once


def test_new_or_inactive_founders_are_never_asked_to_refer(program):
    factory, referrals, usage, clock, code, write = program
    from app.chat.conversations import bind_conversation
    from app.tenancy import bind

    def nudge():
        with bind(workspace_id="wsp_ven_first", venture_id="ven_first", actor="usr_member"):
            with bind_conversation("ven_first", "chat_ven_first"):
                return referrals.nudge("ven_first", "usr_member")
    assert not nudge()  # account one hour old, no messages
    _engaged(factory, clock, ventures=())
    assert not nudge()  # old enough, but has not used this venture yet
    _engaged(factory, clock, ventures=("ven_first",))
    assert nudge()
