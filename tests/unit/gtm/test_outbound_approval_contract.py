"""Exact external-action approval contract, without a real provider call."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from sqlalchemy import select

from tests.company_os_harness import load_company_os_app

load_company_os_app()


def _fresh_db(monkeypatch, tmp_path):
    monkeypatch.setenv("SUPYMEM_MODE", "hosted")
    dbfile = tmp_path / "external_review.db"
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{dbfile}")
    monkeypatch.setenv("DB_URL", f"sqlite:///{dbfile}")
    from app.credits import policy
    from app.db.models import Base
    from app.state import db, reset_db_factory

    reset_db_factory()
    factory = db()
    with factory() as session:
        Base.metadata.create_all(session.get_bind())
    policy.invalidate()
    return factory


def test_ops_routes_leave_database_transactions_in_services():
    import ast
    import inspect
    from pathlib import Path
    from app.api import ops_api

    tree = ast.parse(Path(inspect.getfile(ops_api)).read_text())
    assert not [node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
        and node.module and node.module.startswith(("app.db", "app.state", "sqlalchemy"))]
    assert not [node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute) and node.attr == "commit"]


@pytest.mark.parametrize("domain", ["warehouse-maintenance.example", "weekend-pottery.example"])
def test_local_draft_review_returns_saved_critic_without_publication(monkeypatch, tmp_path, domain):
    from fastapi import HTTPException
    from app.api import ops_api
    from app.db.models import Approval, ContentBrief, ContentPost, ContentTopic
    from core.clock import utcnow

    factory = _fresh_db(monkeypatch, tmp_path)
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setattr(ops_api, "_require_api_key", lambda *_: None)
    monkeypatch.setattr(ops_api, "_workspace", lambda *_: "review-workspace")
    with factory() as session:
        session.add(ContentBrief(id="review-brief", workspace_id="review-workspace", created_at=utcnow()))
        session.flush()
        session.add(ContentTopic(id="review-topic", brief_id="review-brief", workspace_id="review-workspace",
            angle=domain, created_at=utcnow()))
        session.flush()
        session.add(ContentPost(id="review-post", topic_id="review-topic", workspace_id="review-workspace",
            platform="linkedin", state="draft", body=f"A practical note about {domain}",
            critic_json=json.dumps({"claims_reviewed": True, "unsupported_claims": []}),
            created_at=utcnow(), updated_at=utcnow()))
        session.commit()
    result = ops_api.content_post_review_request("review-post")
    assert result["critic"]["claims_reviewed"] is True and result["state"] == "draft"
    assert result["body"] == f"A practical note about {domain}" and not result.get("approval")
    with factory() as session:
        assert not session.scalar(select(Approval.id))
        assert session.get(ContentPost, "review-post").published_at is None
    monkeypatch.setattr(ops_api, "_workspace", lambda *_: "another-workspace")
    with pytest.raises(HTTPException) as error:
        ops_api.content_post_review_request("review-post")
    assert error.value.status_code == 404


def test_propose_review_approve_consume_once_and_audit(monkeypatch, tmp_path):
    factory = _fresh_db(monkeypatch, tmp_path)
    monkeypatch.setenv("SUPYMEM_OUTBOUND_EXECUTION_ENABLED", "true")
    from app.approvals import service
    from app.approvals.external import consume_external_approval, propose_external_approval
    from app.db.models import EventLog, Person, Workspace, WorkspaceMember
    from core.clock import utcnow

    with factory() as session:
        session.add(
            Person(
                id="per_founder", display_name="Founder", email="founder@example.com", created_at=utcnow()
            )
        )
        session.add(
            Workspace(
                id="ws_review", kind="company", name="Review workspace", entry_mode="idea",
                owner_id="per_founder", tenancy="hosted", autonomy_level="L1", observe_level="O0",
                created_at=utcnow(), updated_at=utcnow(),
            )
        )
        session.add(
            WorkspaceMember(
                id="wsm_founder",
                workspace_id="ws_review",
                person_id="per_founder",
                role="owner",
                can_approve=1,
                created_at=utcnow(),
            )
        )
        session.commit()

    target = {
        "queue_id": "gtmjq_1",
        "recipient": "ada@example.com",
        "sequence_day": 1,
        "subject": "A specific subject",
        "body": "A specific reviewed body",
    }
    proposed = propose_external_approval(
        workspace_id="ws_review",
        subject_ref="gtmjq_1",
        action="gtm_email_send",
        target=target,
        summary="Send reviewed email to ada@example.com",
        review={"recipient": "ada@example.com", "body": target["body"]},
    )
    assert proposed["status"] == "pending"
    assert proposed["detail"]["external_action"] == "gtm_email_send"
    assert proposed["detail"]["execution_digest"]

    decided = service.decide(
        proposed["id"], decision="approved", decided_by="per_founder", surface="desktop"
    )
    assert decided.status == "approved"
    assert consume_external_approval(
        workspace_id="ws_review",
        approval_id=proposed["id"],
        action="gtm_email_send",
        target=target,
    ) == (False, "external_execution_requires_metered_executor")

    with factory() as session:
        topics = set(session.scalars(select(EventLog.topic)).all())
    assert {"approval.requested", "approval.approved"} <= topics


def test_foreign_workspace_cannot_review_or_consume_queue(monkeypatch, tmp_path):
    factory = _fresh_db(monkeypatch, tmp_path)
    monkeypatch.setenv("SUPYMEM_OUTBOUND_EXECUTION_ENABLED", "true")
    from app.approvals.external import action_digest, consume_external_approval, gtm_email_review
    from app.db.models import Approval, CreditAccount, GtmContact, GtmEmailAccount, GtmEmailSendQueue
    from core.clock import utcnow

    now = utcnow()
    with factory() as session:
        session.add_all(
            [
                GtmContact(
                    id="contact_a", workspace_id="ws_a", email="ada@example.com",
                    email_confidence="safe", lawful_basis="consent", relationship_stage="new",
                    stage_changed_at=now, sequence_state_json=json.dumps({"1": {"subject": "s", "body": "b"}}),
                    sequence_kind="cold", created_at=now,
                ),
                GtmEmailAccount(
                    id="mail_a", workspace_id="ws_a", app_user_id="user_a", provider="gmail",
                    email="mail@example.com", composio_user_id="comp_a", connected_account_id="conn_a",
                    daily_send_limit=10, sent_today=0, status="active", created_at=now, updated_at=now,
                ),
                GtmEmailSendQueue(
                    id="gtmjq_a", contact_id="contact_a", email_account_id="mail_a", sequence_day=1,
                    status="queued", scheduled_for=now, attempts=0, idempotency_key="queue-a",
                    created_at=now, updated_at=now,
                ),
            ]
        )
        session.commit()

    with pytest.raises(ValueError, match="outbound_queue_not_found"):
        gtm_email_review("ws_b", "gtmjq_a")
    target = {"queue_id": "gtmjq_a", "recipient": "ada@example.com", "sequence_day": 1, "subject": "s", "body": "b"}
    with factory() as session:
        session.add(
            Approval(
                id="apr_a", workspace_id="ws_a", kind="external_comms", subject_ref="gtmjq_a",
                summary="review", detail_json=json.dumps({"external_action": "gtm_email_send", "execution_digest": action_digest("gtm_email_send", target)}),
                risk="high", requested_by="agt_review", status="approved", expires_at=now.replace(year=now.year + 1), created_at=now,
            )
        )
        session.commit()
    assert consume_external_approval(
        workspace_id="ws_b", approval_id="apr_a", action="gtm_email_send", target=target
    ) == (False, "external_execution_requires_metered_executor")


@pytest.mark.parametrize("basis", ["consent", "legitimate_interest"])
@pytest.mark.parametrize("rejected", [False, True])
def test_mocked_provider_runs_only_after_exact_approval(monkeypatch, tmp_path, basis, rejected):
    factory = _fresh_db(monkeypatch, tmp_path)
    monkeypatch.setenv("SUPYMEM_OUTBOUND_EXECUTION_ENABLED", "true")
    monkeypatch.setenv("CHANNELS_DRY_RUN", "false")
    from app.approvals.external import action_digest
    from app.db.models import Approval, CreditAccount, GtmContact, GtmEmailAccount, GtmEmailSendQueue, Person, Workspace
    from app.gtm.supymem.pipeline import _send_one
    from app.gtm.relationships.unsubscribe import unsubscribe_url
    from core.clock import utcnow

    now = utcnow()
    target = {
        "queue_id": "gtmjq_send", "recipient": "ada@example.com", "sequence_day": 1,
        "subject": "Reviewed subject", "body": "Reviewed body\n\nYour email address is listed publicly on example.com.\n\nmail@example.com\n\n"
        'If this is not relevant, just reply "no thanks" and I will not email you again.\n\nBusiness outreach · 1 Fixture Street, New York, NY 10001',
    }
    target["body"] += "\n\nStop these emails: " + unsubscribe_url("ws_send", "contact_send", target["recipient"])
    with factory() as session:
        session.add_all(
            [
                Person(id="per_send", display_name="Send founder", email="send@example.test", created_at=now),
                Workspace(id="ws_send", kind="company", name="Send workspace", entry_mode="idea",
                    owner_id="per_send", tenancy="hosted", autonomy_level="L1", observe_level="O0",
                    created_at=now, updated_at=now),
                GtmContact(
                    id="contact_send", workspace_id="ws_send", email="ada@example.com", email_confidence="safe",
                    lawful_basis=basis, relationship_stage="new", stage_changed_at=now,
                    recipient_geo="US", lawful_basis_at=now, lawful_basis_note="The contact agreed by email.",
                    enrichment_json=json.dumps({"found_at": "https://example.com/contact"}),
                    sequence_state_json=json.dumps({"1": {"subject": target["subject"], "body": target["body"]}}),
                    sequence_kind="cold", created_at=now,
                ),
                GtmEmailAccount(
                    id="mail_send", workspace_id="ws_send", app_user_id="user_send", provider="gmail",
                    email="mail@example.com", composio_user_id="comp_send", connected_account_id="conn_send",
                    daily_send_limit=10, sent_today=0, status="active", created_at=now, updated_at=now,
                ),
                GtmEmailSendQueue(
                    id="gtmjq_send", contact_id="contact_send", email_account_id="mail_send", sequence_day=1,
                    status="queued", scheduled_for=now, attempts=1, idempotency_key="queue-send",
                    created_at=now, updated_at=now,
                ),
                Approval(
                    id="apr_send", workspace_id="ws_send", kind="external_comms", subject_ref="gtmjq_send",
                    summary="review", detail_json=json.dumps({"external_action": "gtm_email_send", "execution_digest": action_digest("gtm_email_send", target)}),
                    risk="high", requested_by="agt_review", status="approved", expires_at=now.replace(year=now.year + 1), created_at=now,
                ),
                CreditAccount(
                    id="cra_send", scope="person", venture_id=None, workspace_id=None,
                    owner_person_id="per_send", granted_micro=2_000_000, spent_micro=0, reserved_micro=0,
                    status="active", version=0, created_at=now, updated_at=now,
                ),
            ]
        )
        session.commit()

    from app.gtm.relationships.service import _settings
    with factory() as session:
        settings = _settings(session, "ws_send")
        settings.sender_postal_address, settings.send_enabled = "1 Fixture Street, New York, NY 10001", True
        session.commit()

    from app.credits.policy import set_policy

    set_policy(
        "outbound_provider_rates",
        {
            "version": "test-composio-gmail-v1",
            "rates": {
                "gtm_email_send:composio_gmail": {
                    "action": "gtm_email_send", "provider": "composio_gmail", "price_usd": "0.01",
                    "currency": "USD", "source": "test contract",
                }
            },
        },
        "test-admin",
    )

    sent_calls: list[dict[str, str]] = []
    def execute_gmail(slug, **kwargs):
        if slug == "GMAIL_SEND_EMAIL":
            args = kwargs["arguments"]
            sent_calls.append({"to": args["recipient_email"], "body": args["body"]})
            if rejected:
                from app.gtm.supymem.composio_gmail import DefiniteFailure
                raise DefiniteFailure("Mailbox daily cap reached. Resets at 2026-10-18T00:00:00 UTC.")
            return {"data": {"id": "mock-1", "threadId": "thread-1"}}
        assert slug == "GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID"
        return {"data": {"payload": {"headers": [{"name": "Message-ID", "value": "<mock-1@example.com>"}]}}}
    with patch("app.gtm.localization.experiments.execution_context", return_value={"mode": "control", "allowed": True}), patch(
        "app.gtm.supymem.composio_gmail.configured", return_value=True
    ), patch(
        "app.gtm.supymem.composio_gmail.execute_tool", side_effect=execute_gmail,
    ):
        result = _send_one("gtmjq_send", simulated=False, approval_id="apr_send")

    assert result["skipped"] is rejected
    if rejected:
        assert "daily cap reached. Resets at" in result["error"]
        with factory() as session:
            account = session.get(CreditAccount, "cra_send")
            assert account.spent_micro == 0 and account.reserved_micro == 0
            assert session.get(GtmEmailSendQueue, "gtmjq_send").status == "queued"
    assert len(sent_calls) == 1
    assert sent_calls[0]["to"] == "ada@example.com"
    assert sent_calls[0]["body"] == target["body"]


def test_late_suppression_blocks_review_and_approved_send(monkeypatch, tmp_path):
    factory = _fresh_db(monkeypatch, tmp_path)
    from app.approvals.external import gtm_email_review
    from app.db.models import GtmContact, GtmEmailAccount, GtmEmailSendQueue, GtmSuppression
    from app.gtm.supymem.pipeline import _send_one
    from core.clock import utcnow

    now = utcnow()
    with factory() as session:
        session.add_all([
            GtmContact(
                id="contact_late", workspace_id="ws_pilot", email="Ada@Example.com",
                email_confidence="safe", lawful_basis="consent", relationship_stage="new",
                stage_changed_at=now, sequence_state_json=json.dumps({"1": {"subject": "s", "body": "b"}}),
                sequence_kind="cold", created_at=now,
            ),
            GtmEmailAccount(
                id="mail_late", workspace_id="ws_pilot", app_user_id="user_pilot", provider="gmail",
                email="founder@example.com", composio_user_id="comp_pilot", connected_account_id="conn_pilot",
                daily_send_limit=10, sent_today=0, status="active", created_at=now, updated_at=now,
            ),
            GtmEmailSendQueue(
                id="gtmjq_late", contact_id="contact_late", email_account_id="mail_late",
                sequence_day=1, status="queued", scheduled_for=now, attempts=0,
                idempotency_key="queue-late", created_at=now, updated_at=now,
            ),
            GtmSuppression(
                id="sup_foreign", workspace_id="ws_other", email="ada@example.com",
                source="opt_out", created_at=now,
            ),
        ])
        session.commit()
    assert gtm_email_review("ws_pilot", "gtmjq_late")[0]["recipient"] == "Ada@Example.com"

    with factory() as session:
        session.add(GtmSuppression(
            id="sup_pilot", workspace_id="ws_pilot", email="ada@example.com",
            source="opt_out", created_at=now,
        ))
        session.commit()
    with pytest.raises(ValueError, match="outbound_recipient_suppressed"):
        gtm_email_review("ws_pilot", "gtmjq_late")
    with patch("app.gtm.supymem.composio_gmail.send_gmail_email") as send:
        result = _send_one("gtmjq_late", simulated=False, approval_id="previously_approved")
    assert result["skipped"] is True and result["reason"] == "stopped"
    send.assert_not_called()
    with factory() as session:
        assert session.get(GtmEmailSendQueue, "gtmjq_late").status == "skipped"


def _configure_outbound_rate() -> None:
    from app.credits.policy import set_policy

    set_policy(
        "outbound_provider_rates",
        {
            "version": "test-composio-gmail-v1",
            "rates": {
                "gtm_email_send:composio_gmail": {
                    "action": "gtm_email_send",
                    "provider": "composio_gmail",
                    "price_usd": "0.01",
                    "currency": "USD",
                    "source": "test contract",
                }
            },
        },
        "test-admin",
    )


def _seed_metered_approval(factory, *, granted_micro: int = 2_000_000, recipient: str = "ada@example.com"):
    from app.approvals.external import action_digest
    from app.db.models import Approval, CreditAccount, Person, Workspace
    from core.clock import utcnow

    target = {
        "queue_id": "gtmjq_metered",
        "recipient": recipient,
        "sequence_day": 1,
        "subject": "Reviewed subject",
        "body": "Reviewed body",
    }
    now = utcnow()
    with factory() as session:
        session.add_all(
            [
                Person(id="per_metered", display_name="Metered founder", email="metered@example.test", created_at=now),
                Workspace(id="ws_metered", kind="company", name="Metered workspace", entry_mode="idea",
                    owner_id="per_metered", tenancy="hosted", autonomy_level="L1", observe_level="O0",
                    created_at=now, updated_at=now),
                CreditAccount(
                    id="cra_metered", scope="person", venture_id=None, workspace_id=None,
                    owner_person_id="per_metered", granted_micro=granted_micro, spent_micro=0, reserved_micro=0,
                    status="active", version=0, created_at=now, updated_at=now,
                ),
                Approval(
                    id="apr_metered", workspace_id="ws_metered", kind="external_comms", subject_ref="gtmjq_metered",
                    summary="review", detail_json=json.dumps({"external_action": "gtm_email_send", "execution_digest": action_digest("gtm_email_send", target)}),
                    risk="high", requested_by="agt_review", status="approved", expires_at=now.replace(year=now.year + 1), created_at=now,
                ),
            ]
        )
        session.commit()
    return target


@pytest.fixture
def execution_contract(monkeypatch, tmp_path, request):
    """Exercise the real executor with an enabled local owner or hosted pricing."""
    factory = _fresh_db(monkeypatch, tmp_path)
    monkeypatch.setenv("SUPYMEM_OUTBOUND_EXECUTION_ENABLED", "true")
    from app.core.local_mode import is_local

    if "runtime" in request.fixturenames:
        _execution_runtime(monkeypatch, request.getfixturevalue("runtime"))
    if is_local():
        _execution_runtime(monkeypatch, "local")
        monkeypatch.delenv("SUPYMEM_OUTBOUND_EXECUTION_ENABLED")
    else:
        _configure_outbound_rate()
    return factory


@pytest.mark.parametrize("runtime", [pytest.param("hosted", marks=pytest.mark.hosted_only), "local"])
@pytest.mark.parametrize("recipient", ["planner@freight.example", "member@gardening.example"])
def test_metered_success_settles_once_and_replay_never_calls_provider(execution_contract, recipient, runtime):
    from app.core.local_mode import is_local

    factory = execution_contract
    provider = "smtp" if is_local() else "composio_gmail"
    target = _seed_metered_approval(factory, recipient=recipient, granted_micro=0 if is_local() else 2_000_000)
    from app.approvals.external import OutboundExecutionError, execute_metered_external_action
    from app.db.models import CreditAccount, CreditLedgerEntry

    calls: list[str] = []
    result = execute_metered_external_action(
        workspace_id="ws_metered", approval_id="apr_metered", action="gtm_email_send",
        provider=provider, target=target,
        invoke=lambda: calls.append("send") or {"provider_message_id": "mock-1"},
    )
    assert result.status == "succeeded"
    assert calls == ["send"]
    with factory() as session:
        account = session.get(CreditAccount, "cra_metered")
        spends = list(session.scalars(select(CreditLedgerEntry).where(CreditLedgerEntry.account_id == "cra_metered")))
    assert account is not None and account.spent_micro == (0 if is_local() else 10_000) and account.reserved_micro == 0
    if is_local():
        assert spends == []
    else:
        assert len(spends) == 1 and spends[0].purpose == "outbound:gtm_email_send"
    with pytest.raises(OutboundExecutionError, match="already consumed"):
        execute_metered_external_action(
            workspace_id="ws_metered", approval_id="apr_metered", action="gtm_email_send",
            provider=provider, target=target, invoke=lambda: calls.append("replay"),
        )
    assert calls == ["send"]


@pytest.mark.parametrize("recipient", ["planner@freight.example", "member@gardening.example"])
def test_local_requested_smtp_needs_no_wallet_or_admin_rate(monkeypatch, tmp_path, recipient):
    from app.approvals import external
    from app.core.local_secrets import update_settings
    from app.credits import gate, local_meter
    from app.db.models import CreditAccount

    factory = _fresh_db(monkeypatch, tmp_path)
    target = _seed_metered_approval(factory, recipient=recipient, granted_micro=0)
    with factory() as session:
        session.delete(session.get(CreditAccount, "cra_metered"))
        session.commit()
    _execution_runtime(monkeypatch, "local")
    monkeypatch.delenv("SUPYMEM_OUTBOUND_EXECUTION_ENABLED", raising=False)
    update_settings("spend", {"monthly_spend_cap": "0"}, {})

    def no_wallet(*args, **kwargs):
        pytest.fail("your requested SMTP send must not need a wallet or admin price")

    for module, names in ((external, ("get_policy", "account_for_workspace", "reserve", "settle")),
            (gate, ("enforcement_enabled", "resolve_charge_target", "reserve", "append"))):
        for name in names:
            monkeypatch.setattr(module, name, no_wallet)
    calls = []
    options = {"workspace_id": "ws_metered", "action": "gtm_email_send", "provider": "smtp",
        "target": target, "invoke": lambda: calls.append("send") or {"provider_message_id": "receipt"}}
    with pytest.raises(external.OutboundExecutionError):
        external.execute_metered_external_action(approval_id=None, **options)
    assert calls == []
    assert external.execute_metered_external_action(approval_id="apr_metered", **options).status == "succeeded"
    with pytest.raises(external.OutboundExecutionError, match="already consumed"):
        external.execute_metered_external_action(approval_id="apr_metered", **options)
    assert calls == ["send"]
    assert local_meter.snapshot()["month_cost_usd_micro"] == 0


@pytest.mark.parametrize("runtime", [pytest.param("hosted", marks=pytest.mark.hosted_only), "local"])
@pytest.mark.parametrize("recipient", ["planner@freight.example", "member@gardening.example"])
def test_definitive_failure_refunds_and_timeout_requires_recovery(execution_contract, recipient, runtime):
    from app.core.local_mode import is_local

    factory = execution_contract
    provider = "smtp" if is_local() else "composio_gmail"
    target = _seed_metered_approval(factory, recipient=recipient, granted_micro=0 if is_local() else 2_000_000)
    from app.approvals.external import (
        DefinitiveProviderFailure,
        OutboundExecutionError,
        execute_metered_external_action,
    )
    from app.db.models import Approval, CreditAccount

    calls = []

    def reject():
        calls.append("reject")
        raise DefinitiveProviderFailure("invalid recipient")

    def timeout():
        calls.append("timeout")
        raise TimeoutError("provider timeout")

    failed = execute_metered_external_action(
        workspace_id="ws_metered", approval_id="apr_metered", action="gtm_email_send",
        provider=provider, target=target,
        invoke=reject,
    )
    assert failed.status == "failed_definitive"
    with factory() as session:
        account = session.get(CreditAccount, "cra_metered")
    assert account is not None and account.spent_micro == 0 and account.reserved_micro == 0

    # A fresh exact approval is needed after a conclusive provider rejection.
    with factory() as session:
        row = session.get(Approval, "apr_metered")
        assert row is not None
        row.id = "apr_timeout"
        row.detail_json = json.dumps({"external_action": "gtm_email_send", "execution_digest": __import__("app.approvals.external", fromlist=["action_digest"]).action_digest("gtm_email_send", target)})
        session.add(row)
        session.commit()
    unknown = execute_metered_external_action(
        workspace_id="ws_metered", approval_id="apr_timeout", action="gtm_email_send",
        provider=provider, target=target,
        invoke=timeout,
    )
    assert unknown.status == "recovery_required"
    with factory() as session:
        account = session.get(CreditAccount, "cra_metered")
    assert account is not None and account.reserved_micro == (0 if is_local() else 10_000)
    from datetime import timedelta
    from core.clock import utcnow
    from app.credits.reserve import sweep_expired
    sweep_expired(utcnow() + timedelta(days=2))
    with factory() as session:
        assert session.get(CreditAccount, "cra_metered").reserved_micro == (0 if is_local() else 10_000)
    with pytest.raises(OutboundExecutionError) as blocked:
        execute_metered_external_action(
            workspace_id="ws_metered", approval_id="apr_timeout", action="gtm_email_send",
            provider=provider, target=target, invoke=lambda: calls.append("replay"),
        )
    assert blocked.value.code == "external_execution_recovery_required"
    if is_local():
        assert str(blocked.value) == "Open your Sent mailbox and check this email's delivery before trying again."
    assert calls == ["reject", "timeout"]


@pytest.mark.parametrize("recipient", ["planner@freight.example", "member@gardening.example"])
def test_definitive_reconnect_resumes_exact_approval_without_replaying_success(execution_contract, recipient):
    from app.approvals.external import OutboundExecutionError, ProviderReconnectRequired, execute_metered_external_action
    from app.core.local_mode import is_local

    factory = execution_contract
    provider = "smtp" if is_local() else "composio_gmail"
    target = _seed_metered_approval(factory, recipient=recipient, granted_micro=0 if is_local() else 2_000_000)
    calls = []

    def disconnected():
        calls.append("reconnect")
        raise ProviderReconnectRequired("Open Keys and connections and reconnect your mail.")

    paused = execute_metered_external_action(workspace_id="ws_metered", approval_id="apr_metered",
        action="gtm_email_send", provider=provider, target=target, invoke=disconnected)
    assert paused.status == "paused"
    with pytest.raises(OutboundExecutionError, match="content mismatch"):
        execute_metered_external_action(workspace_id="ws_metered", approval_id="apr_metered",
            action="gtm_email_send", provider=provider, target={**target, "body": "Changed after approval"},
            invoke=lambda: calls.append("edited send"))
    resumed = execute_metered_external_action(workspace_id="ws_metered", approval_id="apr_metered",
        action="gtm_email_send", provider=provider, target=target, invoke=lambda: calls.append("send") or {"provider_message_id": "resumed"})
    assert resumed.status == "succeeded"
    with pytest.raises(OutboundExecutionError, match="already consumed"):
        execute_metered_external_action(workspace_id="ws_metered", approval_id="apr_metered",
            action="gtm_email_send", provider=provider, target=target, invoke=lambda: calls.append("replay"))
    assert calls == ["reconnect", "send"]
    if is_local():
        from app.credits.local_meter import snapshot
        assert snapshot(workspace_id="ws_metered")["tasks"][0]["unconfirmed_calls"] == 0


@pytest.mark.parametrize("recipient", ["planner@freight.example", "member@gardening.example"])
@pytest.mark.parametrize("outcome", ["sent", "not_sent"])
def test_local_uncertain_delivery_resolves_only_its_saved_scope_without_sending_again(execution_contract, monkeypatch, recipient, outcome):
    from app.approvals import external
    from app.db.models import Approval, Venture
    from core.clock import utcnow

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    _execution_runtime(monkeypatch, "local")
    monkeypatch.setattr(external, "capability_enabled", lambda name: name == "send_email")
    factory = execution_contract
    target = _seed_metered_approval(factory, recipient=recipient, granted_micro=0)
    target.pop("queue_id")
    target.update(venture_id="ven_metered", draft_id="draft_metered")
    with factory() as session:
        session.add(Venture(id="ven_metered", workspace_id="ws_metered", name="Delivery check",
            raw_idea=f"Invite people at {recipient.rsplit('@', 1)[1]}", stage="draft", created_at=utcnow(), updated_at=utcnow()))
        session.get(Approval, "apr_metered").detail_json = json.dumps({"external_action": "gtm_email_send",
            "execution_digest": external.action_digest("gtm_email_send", target), "target": target})
        session.commit()
    calls = []

    def timeout():
        calls.append("send")
        raise TimeoutError("provider timeout")

    attempt = external.execute_metered_external_action(workspace_id="ws_metered", approval_id="apr_metered",
        action="gtm_email_send", provider="smtp", target=target, invoke=timeout)
    assert attempt.status == "recovery_required"
    from app.credits.local_meter import snapshot
    receipts = snapshot(workspace_id="ws_metered", venture_id="ven_metered")["tasks"]
    assert len(receipts) == 1 and receipts[0]["unconfirmed_calls"] == 1
    for workspace_id, venture_id in [("foreign_workspace", "ven_metered"), ("ws_metered", "foreign_venture")]:
        with pytest.raises(external.OutboundExecutionError, match="approval mismatch"):
            external.resolve_outreach_execution(workspace_id=workspace_id, venture_id=venture_id,
                draft_id="draft_metered", approval_id="apr_metered", outcome=outcome, actor_subject="per_metered")
    external.resolve_outreach_execution(workspace_id="ws_metered", venture_id="ven_metered",
        draft_id="draft_metered", approval_id="apr_metered", outcome=outcome, actor_subject="per_metered")
    with factory() as session:
        detail = json.loads(session.get(Approval, "apr_metered").detail_json)
    execution = detail["external_execution"]
    assert execution["state"] == ("succeeded" if outcome == "sent" else "failed_definitive")
    assert execution["delivery_resolution"]["outcome"] == outcome
    blocked = "already consumed" if outcome == "sent" else "requires new review"
    with pytest.raises(external.OutboundExecutionError, match=blocked):
        external.execute_metered_external_action(workspace_id="ws_metered", approval_id="apr_metered",
            action="gtm_email_send", provider="smtp", target=target, invoke=lambda: calls.append("replay"))
    assert calls == ["send"]


@pytest.mark.parametrize("recipient", ["planner@freight.example", "member@gardening.example"])
def test_local_smtp_does_not_make_non_email_actions_unpriced(execution_contract, monkeypatch, recipient):
    from app.approvals import external
    from app.db.models import Approval

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setattr(external, "capability_enabled", lambda name: True)
    factory = execution_contract
    target = _seed_metered_approval(factory, recipient=recipient, granted_micro=0)
    with factory() as session:
        session.get(Approval, "apr_metered").detail_json = json.dumps({"external_action": "content_publish",
            "execution_digest": external.action_digest("content_publish", target)})
        session.commit()
    calls = []
    with pytest.raises(external.OutboundExecutionError):
        external.execute_metered_external_action(workspace_id="ws_metered", approval_id="apr_metered",
            action="content_publish", provider="smtp", target=target, invoke=lambda: calls.append("publish"))
    assert calls == []


@pytest.mark.hosted_only
def test_insufficient_credits_does_not_call_provider(monkeypatch, tmp_path):
    factory = _fresh_db(monkeypatch, tmp_path)
    monkeypatch.setenv("SUPYMEM_OUTBOUND_EXECUTION_ENABLED", "true")
    _configure_outbound_rate()
    target = _seed_metered_approval(factory, granted_micro=9_999)
    from app.approvals.external import OutboundExecutionError, execute_metered_external_action

    calls: list[str] = []
    with pytest.raises(OutboundExecutionError, match="required") as excinfo:
        execute_metered_external_action(
            workspace_id="ws_metered", approval_id="apr_metered", action="gtm_email_send",
            provider="composio_gmail", target=target,
            invoke=lambda: calls.append("provider"),
        )
    assert excinfo.value.code == "insufficient_credits"
    assert not calls


@pytest.mark.parametrize("runtime", [pytest.param("hosted", marks=pytest.mark.hosted_only), "local"])
def test_metered_execution_rejects_an_edited_draft(monkeypatch, tmp_path, runtime):
    factory = _fresh_db(monkeypatch, tmp_path)
    monkeypatch.setenv("SUPYMEM_OUTBOUND_EXECUTION_ENABLED", "true")
    _configure_outbound_rate()
    target = _seed_metered_approval(factory)
    _execution_runtime(monkeypatch, runtime)
    from app.approvals.external import OutboundExecutionError, execute_metered_external_action

    with pytest.raises(OutboundExecutionError, match="content mismatch") as excinfo:
        execute_metered_external_action(
            workspace_id="ws_metered", approval_id="apr_metered", action="gtm_email_send",
            provider="composio_gmail", target={**target, "body": "edited after review"},
            invoke=lambda: (_ for _ in ()).throw(AssertionError("provider must not run")),
        )
    assert excinfo.value.code == "external_approval_content_mismatch"


def _execution_runtime(monkeypatch, runtime):
    if runtime == "local":
        from app.core.local_secrets import update_settings
        monkeypatch.setenv("SUPYMEM_MODE", "local")
        update_settings("smtp", {"host": "127.0.0.1", "port": 587, "user": "owner@sender.example",
            "sender_name": "Local owner", "postal_address": "12 Main Street, Toronto, Canada"},
            {"password": "synthetic-test-password"})
        update_settings("capabilities", {"send_email": True}, {})


@pytest.mark.parametrize("audience", ["Factory purchasing teams", "Parents choosing weekend activities"])
@pytest.mark.parametrize("outcome", ["sent", "not_sent"])
def test_local_ambiguous_send_resolution_never_replays_or_uses_a_wallet(monkeypatch, tmp_path, audience, outcome):
    from app.approvals.external import action_digest, execute_metered_external_action, resolve_outreach_execution, OutboundExecutionError
    from app.db.models import Approval, CreditAccount, Venture
    from core.clock import utcnow

    factory = _fresh_db(monkeypatch, tmp_path)
    target = _seed_metered_approval(factory) | {"venture_id": "ven_local_resolution", "draft_id": "draft_local_resolution"}
    with factory() as session:
        now = utcnow()
        session.add(Venture(id=target["venture_id"], workspace_id="ws_metered", name=audience,
            raw_idea=audience, stage="draft", created_at=now, updated_at=now))
        session.get(Approval, "apr_metered").detail_json = json.dumps({"external_action": "gtm_email_send",
            "execution_digest": action_digest("gtm_email_send", target), "target": target})
        session.commit()
    _execution_runtime(monkeypatch, "local")
    calls = []
    def lost_receipt():
        calls.append("send")
        raise TimeoutError("Synthetic lost receipt")
    result = execute_metered_external_action(workspace_id="ws_metered", approval_id="apr_metered",
        action="gtm_email_send", provider="composio_gmail", target=target, invoke=lost_receipt)
    assert result.status == "recovery_required" and calls == ["send"]
    for _ in range(2):
        resolve_outreach_execution(workspace_id="ws_metered", venture_id=target["venture_id"],
            draft_id=target["draft_id"], approval_id="apr_metered", outcome=outcome, actor_subject="per_metered")
    with pytest.raises(OutboundExecutionError, match="already consumed" if outcome == "sent" else "new review"):
        execute_metered_external_action(workspace_id="ws_metered", approval_id="apr_metered",
            action="gtm_email_send", provider="composio_gmail", target=target, invoke=lost_receipt)
    with factory() as session:
        account = session.get(CreditAccount, "cra_metered")
        assert account.spent_micro == account.reserved_micro == 0
    assert calls == ["send"]
