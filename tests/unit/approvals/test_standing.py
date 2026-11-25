from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from tests.company_os_harness import load_company_os_app


@pytest.fixture(scope="module")
def standing_app(tmp_path_factory):
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path_factory.mktemp("standing-app")))
        load_company_os_app()
        from app.approvals import service, standing

        yield service, standing


@pytest.fixture
def policy(tmp_path, monkeypatch, standing_app):
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    from app.core.local_secrets import update_settings

    update_settings("capabilities", {"routines": True}, {})
    monkeypatch.setattr(standing_app[1], "get_policy", lambda key: {"approvals_policy_v1": True})
    service, standing = standing_app
    from app.db.models import Approval, Base, ChatConversation, ChatMessage, EventLog, Person, Venture, Workspace, WorkspaceMember
    from app.db.supymem_models import Notice
    from core.clock import set_clock

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[model.__table__ for model in (
        Approval, ChatConversation, ChatMessage, EventLog, Person, Venture, Workspace, WorkspaceMember, Notice)])
    factory = sessionmaker(engine, expire_on_commit=True)
    clock = [datetime(2026, 10, 30, tzinfo=timezone.utc)]
    set_clock(lambda: clock[0])
    monkeypatch.setattr(standing, "db", lambda: factory)
    monkeypatch.setattr(service, "db", lambda: factory)
    monkeypatch.setattr(service, "audit", lambda *args, **kwargs: None)
    domains = [("fleet", "Maintenance records for logistics operators"),
               ("hiking", "Weekend trail guides for families")]
    with factory() as session:
        for domain, audience in domains:
            founder, workspace, venture = f"per_{domain}", f"wsp_{domain}", f"ven_{domain}"
            session.add(Person(id=founder, display_name=audience, created_at=clock[0]))
            session.add(Workspace(id=workspace, owner_id=founder, name=audience, kind="company",
                entry_mode="idea", created_at=clock[0], updated_at=clock[0]))
            session.add(WorkspaceMember(id=f"wmb_{domain}", workspace_id=workspace,
                person_id=founder, role="owner", can_approve=1, created_at=clock[0]))
            session.add(Venture(id=venture, workspace_id=workspace, name=audience, raw_idea=audience,
                stage="idea", created_at=clock[0], updated_at=clock[0]))
            session.add(ChatConversation(id=f"cnv_{domain}", venture_id=venture, workspace_id=workspace,
                creator_person_id=founder, visibility="private", title=audience,
                creation_key=domain, created_at=clock[0], updated_at=clock[0]))
            session.add(ChatMessage(id=f"msg_{domain}", venture_id=venture, workspace_id=workspace,
                conversation_id=f"cnv_{domain}", role="assistant", body_md="Review your saved action.",
                created_at=clock[0], actions_json=json.dumps([{"confirmation": {"action": "research.start",
                    "resulting_state": {"ceiling_micro": 5, "task_allowance_micro": 10}}}])))
        session.add(WorkspaceMember(id="wmb_shared", workspace_id="wsp_fleet", person_id="per_hiking",
            role="owner", can_approve=1, created_at=clock[0]))
        session.commit()
    yield standing, service, factory, clock
    set_clock(None)
    engine.dispose()


def scope(domain):
    return dict(workspace_id=f"wsp_{domain}", venture_id=f"ven_{domain}", founder_id=f"per_{domain}")


@pytest.mark.parametrize("domain", ["fleet", "hiking"])
@pytest.mark.parametrize("outcome", ["sent", "unknown", "rejected", "reserving"])
def test_local_mail_keeps_exact_approval_and_send_fences_without_a_wallet(policy, monkeypatch, domain, outcome):
    from app.approvals import external
    from app.core.local_secrets import update_settings
    from app.db.models import Approval
    from core.clock import utcnow

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    update_settings("smtp", {"host": "smtp.example.invalid", "port": 587,
        "user": "owner@example.invalid", "sender_name": "Owner", "postal_address": "123 Example Road"},
        {"password": "synthetic"})
    update_settings("capabilities", {"send_email": True}, {})
    monkeypatch.setattr(external, "db", lambda: policy[2])
    monkeypatch.setattr(external, "audit", lambda *args, **kwargs: None)
    monkeypatch.setattr(external, "execution_enabled", lambda *_: True)
    monkeypatch.setattr(external, "get_policy", lambda *_: pytest.fail("Your SMTP send needs no administrator rate"))
    monkeypatch.setattr("app.credits.accounts.get_account", lambda *_: pytest.fail("Your SMTP send needs no wallet"))
    target = {"venture_id": f"ven_{domain}", "draft_id": f"draft_{domain}", "to": f"owner@{domain}.example"}
    approval_id = f"approval_{domain}"
    with policy[2]() as session:
        session.add(Approval(id=approval_id, workspace_id=f"wsp_{domain}", kind="external_comms",
            subject_ref=target["draft_id"], summary="Send your saved draft", risk="high", requested_by=f"per_{domain}",
            status="approved", created_at=utcnow(), expires_at=utcnow() + timedelta(days=1),
            detail_json=json.dumps({"external_action": "gtm_email_send",
                "execution_digest": external.action_digest("gtm_email_send", target), "target": target})))
        session.commit()
    calls = []

    def send():
        calls.append(outcome)
        if outcome == "unknown":
            raise TimeoutError()
        if outcome == "rejected":
            raise external.DefinitiveProviderFailure("Your SMTP server rejected this send")
        return {"provider_message_id": "offline-receipt"}

    kwargs = dict(workspace_id=f"wsp_{domain}", approval_id=approval_id, action="gtm_email_send",
        provider="smtp", target=target, invoke=send)
    assert external.outbound_quote("gtm_email_send", "smtp")["credit_estimate_micro"] == 0
    with pytest.raises(external.OutboundExecutionError) as wrong_workspace:
        external.execute_metered_external_action(**{**kwargs, "workspace_id": "wsp_other"})
    assert wrong_workspace.value.code == "external_approval_workspace_mismatch"
    with pytest.raises(external.OutboundExecutionError, match="content mismatch"):
        external.execute_metered_external_action(**{**kwargs, "target": {**target, "to": "other@example.invalid"}})
    assert not calls
    if outcome == "reserving":
        external._set_execution(approval_id, {"state": "reserving"})
        with pytest.raises(external.OutboundExecutionError, match="Open your Sent mailbox") as blocked:
            external.execute_metered_external_action(**kwargs)
        assert blocked.value.code == "external_execution_recovery_required"
        assert not calls
        return
    result = external.execute_metered_external_action(**kwargs)
    assert result.status == {"sent": "succeeded", "unknown": "recovery_required", "rejected": "failed_definitive"}[outcome]
    assert result.reservation_id is None
    with pytest.raises(external.OutboundExecutionError):
        external.execute_metered_external_action(**kwargs)
    assert calls == [outcome]
    if outcome == "unknown":
        from app.db.models import CreditReservation

        with policy[2]() as session:
            CreditReservation.__table__.create(session.get_bind())
        external._set_execution(approval_id, {"reservation_id": "foreign-reservation"})
        with pytest.raises(external.OutboundExecutionError, match="credit mismatch"):
            external.resolve_outreach_execution(workspace_id=f"wsp_{domain}", venture_id=f"ven_{domain}",
                draft_id=target["draft_id"], approval_id=approval_id, outcome="not_sent", actor_subject=f"per_{domain}")
        external._set_execution(approval_id, {"reservation_id": None})
        external.resolve_outreach_execution(workspace_id=f"wsp_{domain}", venture_id=f"ven_{domain}",
            draft_id=target["draft_id"], approval_id=approval_id, outcome="not_sent", actor_subject=f"per_{domain}")
        with pytest.raises(external.OutboundExecutionError):
            external.execute_metered_external_action(**kwargs)
        assert calls == [outcome]


def request(policy, domain, action, **overrides):
    return policy[0].auto_approve(**{**scope(domain), "action": action, "kind": "workflow_write",
        "subject_ref": f"task:{domain}", "summary": "Read saved documents", "requested_by": "agt_reader", **overrides})


@pytest.mark.parametrize("mode", ["hosted", "local"])
def test_policy_flag_defaults_off_and_ignores_legacy_environment(policy, monkeypatch, mode):
    standing = policy[0]
    monkeypatch.setenv("SUPYMEM_MODE", mode)
    monkeypatch.setenv("APPROVALS_POLICY_V1", "1")
    for flags in (None, {}, {"approvals_policy_v1": False}, {"approvals_policy_v1": "true"},
                  {"approvals_policy_v1": 1}):
        monkeypatch.setattr(standing, "get_policy", lambda key: flags)
        assert standing.enabled() is (mode == "local")
        for domain in ("fleet", "hiking"):
            assert request(policy, domain, standing.StandingAction("documents.read", f"folder:{domain}", 0, 10)) is None
    monkeypatch.setattr(standing, "get_policy", lambda key: {"approvals_policy_v1": True})
    assert standing.enabled() is True
    if mode == "local":
        from app.core.local_secrets import update_settings

        update_settings("capabilities", {"routines": False}, {})
        assert standing.enabled() is False
        for domain in ("fleet", "hiking"):
            assert request(policy, domain, standing.StandingAction("documents.read", f"folder:{domain}", 0, 10)) is None


@pytest.mark.parametrize("domain", ["fleet", "hiking"])
def test_revocation_expiry_and_atomic_audits(policy, domain):
    from app.db.models import EventLog
    from core.clock import as_utc

    standing, service, factory, clock = policy
    action = standing.StandingAction("documents.read", f"folder:{domain}", 5, 10)
    grant = standing.grant(**scope(domain), action=action, label="Read these saved documents")
    assert as_utc(grant.expires_at) == clock[0] + timedelta(days=30)
    approved = service.request_approval(**scope(domain), standing_action=action, kind="workflow_write",
        subject_ref=f"task:{domain}", summary="Read saved documents", requested_by="agt_reader", window_hours=1,
        detail={"offered_at": clock[0]})
    assert approved.status == "approved" and approved.decided_by == scope(domain)["founder_id"]
    assert approved.detail["standing_approval_id"] == grant.id
    assert approved.detail["offered_at"] == str(clock[0])
    assert as_utc(datetime.fromisoformat(approved.expires_at)) == clock[0] + timedelta(hours=1)
    service.decide(grant.id, decision="withdrawn", decided_by=scope(domain)["founder_id"])
    assert request(policy, domain, action) is None
    fresh = standing.grant(**scope(domain), action=action, label="Read these saved documents")
    clock[0] += timedelta(days=30)
    assert request(policy, domain, action) is None
    with factory() as session:
        audits = session.scalars(select(EventLog).order_by(EventLog.seq)).all()
    assert [event.topic for event in audits] == ["approval.standing_granted", "approval.auto_approved",
                                                "approval.standing_revoked", "approval.standing_granted"]
    assert [event.actor_id for event in audits] == [scope(domain)["founder_id"], "standing_policy",
                                                  scope(domain)["founder_id"], scope(domain)["founder_id"]]
    assert all(event.on_behalf_of == scope(domain)["founder_id"] and event.venture_id == scope(domain)["venture_id"]
               and event.ts is not None for event in audits)
    assert json.loads(audits[1].payload_json) == {"standing_approval_id": grant.id,
        "requested_by": "agt_reader", "action_type": action.action_type, "scope": action.scope}
    assert fresh.id != grant.id


@pytest.mark.parametrize("action_type", ["first_send", "gtm.send", "social_post", "payments.purchase",
    "spend_money", "account.change", "deploy_production", "build.start", "form.submit",
    "connections.connect", "connections.disconnect", "delete", "delete_foreign_resource", "unknown"])
def test_hard_rules_never_grant_or_auto_approve(policy, action_type):
    from app.approvals.service import ApprovalError
    from app.db.models import Approval, EventLog

    for domain in ("fleet", "hiking"):
        action = policy[0].StandingAction(action_type, f"scope:{domain}", 0, 10)
        with pytest.raises(ApprovalError, match="each time"):
            policy[0].grant(**scope(domain), action=action, label="Saved scope")
        assert request(policy, domain, action) is None
    with policy[2]() as session:
        assert session.scalar(select(Approval.id)) is None
        assert session.scalar(select(EventLog.id)) is None


@pytest.mark.parametrize("domain", ["fleet", "hiking"])
def test_current_send_and_spend_limits_fail_closed(policy, domain):
    from app.approvals.service import ApprovalError

    standing = policy[0]
    followup = standing.StandingAction("outreach.followup", f"sequence:{domain}", 5, 10,
        first_send=False, sequence_approved=True, sent_today=2, daily_cap=3)
    assert followup.eligible()
    for unsafe in (replace(followup, first_send=True), replace(followup, sequence_approved=False),
                   replace(followup, sent_today=3), replace(followup, daily_cap=None),
                   replace(followup, credit_cost_micro=11), replace(followup, task_allowance_micro=None),
                   replace(followup, credit_cost_micro=True), replace(followup, scope="")):
        assert not unsafe.eligible()
        with pytest.raises(ApprovalError, match="each time"):
            standing.grant(**scope(domain), action=unsafe, label="Saved scope")
        assert request(policy, domain, unsafe) is None
    read = standing.StandingAction("documents.read", f"folder:{domain}", 5, 10)
    standing.grant(**scope(domain), action=read, label="Read these documents")
    assert request(policy, domain, replace(read, credit_cost_micro=11)) is None
    for kind in ("production_deploy", "external_comms", "spend_increase", "credit_reserve_large", "autonomy_change"):
        assert request(policy, domain, read, kind=kind) is None


def test_standing_grants_never_cross_founders_workspaces_ventures_or_scopes(policy):
    from app.approvals.service import ApprovalError
    from app.db.models import Venture
    from app.tenancy import bind

    standing, service, factory, clock = policy
    with factory() as session:
        for domain in ("fleet", "hiking"):
            session.add(Venture(id=f"ven_{domain}_other", workspace_id=scope(domain)["workspace_id"],
                name=f"Another {domain} venture", raw_idea=f"Another {domain} venture", stage="idea",
                created_at=clock[0], updated_at=clock[0]))
        session.commit()
    action = standing.StandingAction("documents.read", "shared-folder", 0, 10)
    grants = [standing.grant(**scope(domain), action=action, label=f"Read {domain} documents")
              for domain in ("fleet", "hiking")]
    for domain, foreign in (("fleet", grants[1]), ("hiking", grants[0])):
        with bind(workspace_id=scope(domain)["workspace_id"], venture_id=scope(domain)["venture_id"],
                  actor=scope(domain)["founder_id"]):
            views = service.list_approvals(scope(domain)["workspace_id"], status="standing")
            assert all(view.expires_at.endswith("+00:00") for view in views)
            assert [item.id for item in views] == [
                grants[0 if domain == "fleet" else 1].id]
            assert service.get_approval(foreign.id) is None
            with pytest.raises(ApprovalError) as rejected:
                service.decide(foreign.id, decision="withdrawn", decided_by=scope(domain)["founder_id"])
            assert rejected.value.code == "not_found"
            with pytest.raises(ApprovalError):
                standing.revoke(foreign.id, scope(domain)["founder_id"])
        assert request(policy, domain, replace(action, scope="another-folder")) is None
        assert request(policy, domain, action, venture_id=f"ven_{domain}_other") is None
        assert request(policy, domain, replace(action, action_type="browse.public")) is None
    with bind(workspace_id="wsp_fleet", venture_id="ven_fleet", actor="per_hiking"):
        assert service.list_approvals("wsp_fleet", status="standing") == []
        assert service.list_approvals("wsp_fleet", status=None) == []
        assert service.get_approval(grants[0].id) is None
        with pytest.raises(ApprovalError) as rejected:
            service.decide(grants[0].id, decision="withdrawn", decided_by="per_hiking")
        assert rejected.value.code == "not_found"
        assert standing.auto_approve(**{**scope("fleet"), "founder_id": "per_hiking"}, action=action,
            kind="workflow_write", subject_ref="task:shared", summary="Read documents", requested_by="agt_reader") is None
        with pytest.raises(ApprovalError):
            standing.grant(**scope("fleet"), action=action, label="Forged founder")
    standing.revoke(grants[0].id, "per_fleet")
    with bind(workspace_id="wsp_fleet", venture_id="ven_fleet", actor="per_hiking"):
        assert service.list_approvals("wsp_fleet", status=None) == []
        assert service.get_approval(grants[0].id) is None
        with pytest.raises(ApprovalError) as rejected:
            service.decide(grants[0].id, decision="withdrawn", decided_by="per_hiking")
        assert rejected.value.code == "not_found"


@pytest.mark.parametrize("founder", [None, "", "founder", "local", "anonymous", "system"])
def test_standing_grants_reject_shared_or_unnamed_identity(policy, founder):
    from app.approvals.service import ApprovalError
    from app.authz.workspace import LOCAL_PERSON_ID
    from app.db.models import Person, WorkspaceMember

    standing, _, factory, clock = policy
    with factory() as session:
        session.add(Person(id=LOCAL_PERSON_ID, display_name="Legacy account", created_at=clock[0]))
        for domain in ("fleet", "hiking"):
            session.add(WorkspaceMember(id=f"wmb_local_{domain}", workspace_id=scope(domain)["workspace_id"],
                person_id=LOCAL_PERSON_ID, role="owner", can_approve=1, created_at=clock[0]))
        session.commit()
    action = standing.StandingAction("documents.read", "folder", 0, 10)
    for domain in ("fleet", "hiking"):
        with pytest.raises(ApprovalError):
            standing.grant(**{**scope(domain), "founder_id": founder}, action=action, label="Read documents")


@pytest.mark.parametrize("domain", ["fleet", "hiking"])
def test_card_choices_use_saved_private_scope_and_deny_blocks_start(policy, domain, monkeypatch):
    from app.approvals.service import ApprovalError
    from app.chat import confirmations
    from app.chat.conversations import ConversationScope
    from app.db.models import Approval, ChatMessage
    from app.tenancy import bind

    standing, service, factory, _ = policy
    choice = {"choice": "always", "venture_id": scope(domain)["venture_id"],
              "message_id": f"msg_{domain}", "action": "research.start", "scope": "forged-broad-scope"}
    with bind(workspace_id=scope(domain)["workspace_id"], venture_id=scope(domain)["venture_id"],
              actor=scope(domain)["founder_id"]):
        view = service.request_approval(workspace_id=scope(domain)["workspace_id"], kind="scope_grant",
            requested_by=scope(domain)["founder_id"], subject_ref="untrusted", summary="Untrusted summary",
            detail={"standing_policy_v1": choice})
        assert view.status == "standing" and view.detail["scope"] != choice["scope"]
        with pytest.raises(ApprovalError):
            standing.choose_card(workspace_id=scope(domain)["workspace_id"], founder_id=scope(domain)["founder_id"],
                detail={**choice, "action": "social_post"})
        standing.choose_card(workspace_id=scope(domain)["workspace_id"], founder_id=scope(domain)["founder_id"],
            detail={**choice, "choice": "deny"})
        with pytest.raises(ApprovalError, match="no longer"):
            standing.choose_card(workspace_id=scope(domain)["workspace_id"], founder_id=scope(domain)["founder_id"], detail=choice)
    with factory() as session:
        card = json.loads(session.get(ChatMessage, f"msg_{domain}").actions_json)[0]["confirmation"]
        assert card["state"] == "denied"
        assert len(session.scalars(select(Approval)).all()) == 2
    monkeypatch.setattr(confirmations, "db", lambda: factory)
    monkeypatch.setattr(confirmations, "resolve", lambda venture: ConversationScope(
        f"cnv_{domain}", scope(domain)["venture_id"], scope(domain)["workspace_id"], scope(domain)["founder_id"], "private"))
    with pytest.raises(ValueError, match="chat_request_in_progress"):
        confirmations.mark(scope(domain)["venture_id"], f"msg_{domain}", "research.start", "starting")


@pytest.mark.parametrize("domain", ["fleet", "hiking"])
@pytest.mark.parametrize("mode", ["hosted", "local"])
def test_flag_off_keeps_confirmation_and_approval_behavior(policy, monkeypatch, domain, mode):
    from app.chat import conversations
    from app.chat.conversations import ConversationScope
    from app.chat.actions import get_action
    from app.chat.confirm import ConfirmationCard
    from app.tenancy import bind

    standing, service, _, _ = policy
    monkeypatch.setenv("SUPYMEM_MODE", mode)
    monkeypatch.setattr(conversations, "current_conversation", lambda: ConversationScope(
        f"cnv_{domain}", scope(domain)["venture_id"], scope(domain)["workspace_id"], scope(domain)["founder_id"], "private"))
    card = ConfirmationCard.for_action(get_action("research.start"), interpretation="Read saved context")
    with bind(workspace_id=scope(domain)["workspace_id"], venture_id=scope(domain)["venture_id"],
              actor=scope(domain)["founder_id"]):
        on = card.as_action()
        monkeypatch.setattr(standing, "get_policy", lambda key: {})
        off = card.as_action()
        assert on["confirmation"].pop("approval_policy")["eligible"] is False
        if mode == "local":
            assert off["confirmation"].pop("approval_policy")["eligible"] is False
        assert on == off
        assert service.list_approvals(scope(domain)["workspace_id"], status="standing") == []
    action = standing.StandingAction("documents.read", "folder", 0, 10)
    assert request(policy, domain, action) is None
    view = service.request_approval(**scope(domain), kind="workflow_write", subject_ref="task",
        summary="Review saved work", requested_by="agt_reader",
        standing_action=action)
    assert view.status == "pending" and view.notified_count == 1


@pytest.mark.parametrize("domain", ["fleet", "hiking"])
def test_audit_failure_rolls_back_grants_and_auto_decisions(policy, monkeypatch, domain):
    from app.db.models import Approval

    standing, _, factory, _ = policy
    action = standing.StandingAction("browse.public", "public-sites", 0, 10)
    original = standing._audit
    def fail(*args, **kwargs):
        raise RuntimeError("audit unavailable")
    monkeypatch.setattr(standing, "_audit", fail)
    with pytest.raises(RuntimeError, match="audit unavailable"):
        standing.grant(**scope(domain), action=action, label="Browse public sites")
    with factory() as session:
        assert session.scalar(select(Approval.id)) is None
    monkeypatch.setattr(standing, "_audit", original)
    standing.grant(**scope(domain), action=action, label="Browse public sites")
    monkeypatch.setattr(standing, "_audit", fail)
    with pytest.raises(RuntimeError, match="audit unavailable"):
        request(policy, domain, action)
    with factory() as session:
        assert len(session.scalars(select(Approval)).all()) == 1


@pytest.mark.parametrize("domain", ["fleet", "hiking"])
def test_execution_decision_rolls_back_with_task_admission(policy, domain):
    from app.approvals.service import STANDING_DECISION_KIND
    from app.db.models import Approval, EventLog

    standing, _, factory, _ = policy
    action = standing.StandingAction("documents.read", f"folder:{domain}", 5, 10)
    grant = standing.grant(**scope(domain), action=action, label="Read your saved documents")
    with factory() as session:
        decision = request(policy, domain, action, grant_id=grant.id, session=session)
        assert decision.status == "approved" and decision.decided_by == scope(domain)["founder_id"]
        session.flush()
        session.rollback()
    with factory() as session:
        assert session.scalar(select(Approval.id).where(Approval.kind == STANDING_DECISION_KIND)) is None
        assert session.scalar(select(EventLog.id).where(EventLog.topic == "approval.auto_approved")) is None
