"""Regression checks for the imported GTM/content live-execution boundary."""

from __future__ import annotations

import json

from sqlalchemy import select

from tests.company_os_harness import load_company_os_app

load_company_os_app()


def test_channel_flag_cannot_enable_live_email_without_global_opt_in(monkeypatch):
    monkeypatch.setenv("CHANNELS_DRY_RUN", "false")
    monkeypatch.delenv("SUPYMEM_OUTBOUND_EXECUTION_ENABLED", raising=False)

    from app.gtm.supymem.pipeline import _dry_run

    assert _dry_run() is True


def test_channel_flag_cannot_enable_live_content_without_global_opt_in(monkeypatch):
    monkeypatch.setenv("CONTENT_DRY_RUN", "false")
    monkeypatch.setenv("CHANNELS_DRY_RUN", "false")
    monkeypatch.delenv("SUPYMEM_OUTBOUND_EXECUTION_ENABLED", raising=False)

    from app.content.platforms.base import dry_run_enabled

    assert dry_run_enabled() is True


def test_external_approval_is_bound_and_one_time(monkeypatch, tmp_path):
    from core.clock import utcnow
    from core.ids import new_id

    dbfile = tmp_path / "approval.db"
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{dbfile}")
    monkeypatch.setenv("DB_URL", f"sqlite:///{dbfile}")
    monkeypatch.setenv("SUPYMEM_OUTBOUND_EXECUTION_ENABLED", "true")

    from app.approvals.external import action_digest, consume_external_approval
    from app.db.models import Approval, Base
    from app.state import db, reset_db_factory

    reset_db_factory()
    factory = db()
    with factory() as session:
        Base.metadata.create_all(session.get_bind())
        target = {"recipient": "ada@example.com", "body": "Hello Ada"}
        session.add(
            Approval(
                id=new_id("apr"),
                workspace_id="ws_a",
                kind="external_comms",
                subject_ref="gtm_email",
                summary="Send one reviewed email",
                detail_json=json.dumps(
                    {
                        "external_action": "gtm_email_send",
                        "execution_digest": action_digest("gtm_email_send", target),
                    }
                ),
                risk="high",
                requested_by="person_requester",
                status="approved",
                expires_at=utcnow().replace(year=utcnow().year + 1),
                created_at=utcnow(),
            )
        )
        session.commit()
        approval_id = session.scalar(select(Approval.id))

    assert consume_external_approval(
        workspace_id="ws_a",
        approval_id=approval_id,
        action="gtm_email_send",
        target=target,
    ) == (False, "external_execution_requires_metered_executor")


def test_external_approval_refuses_changed_copy(monkeypatch, tmp_path):
    from core.clock import utcnow
    from core.ids import new_id

    dbfile = tmp_path / "approval_changed_copy.db"
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{dbfile}")
    monkeypatch.setenv("DB_URL", f"sqlite:///{dbfile}")
    monkeypatch.setenv("SUPYMEM_OUTBOUND_EXECUTION_ENABLED", "true")

    from app.approvals.external import action_digest, consume_external_approval
    from app.db.models import Approval, Base
    from app.state import db, reset_db_factory

    reset_db_factory()
    factory = db()
    with factory() as session:
        Base.metadata.create_all(session.get_bind())
        approved = {"recipient": "ada@example.com", "body": "Reviewed copy"}
        session.add(
            Approval(
                id="apr_changed",
                workspace_id="ws_a",
                kind="external_comms",
                subject_ref="gtm_email",
                summary="Send one reviewed email",
                detail_json=json.dumps(
                    {
                        "external_action": "gtm_email_send",
                        "execution_digest": action_digest("gtm_email_send", approved),
                    }
                ),
                risk="high",
                requested_by="person_requester",
                status="approved",
                expires_at=utcnow().replace(year=utcnow().year + 1),
                created_at=utcnow(),
            )
        )
        session.commit()

    allowed, reason = consume_external_approval(
        workspace_id="ws_a",
        approval_id="apr_changed",
        action="gtm_email_send",
        target={"recipient": "ada@example.com", "body": "Changed copy"},
    )
    assert not allowed
    assert reason == "external_execution_requires_metered_executor"
