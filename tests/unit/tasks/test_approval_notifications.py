"""Saved approval notices qualify delivery; no notification or email is sent."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from tests.company_os_harness import load_company_os_app


@pytest.fixture
def approvals(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LLM_OFFLINE", "1")
    load_company_os_app()
    from app import desktop_notices
    from app.approvals import service
    from app.db.models import Approval, Base, Person, Venture, Workspace, WorkspaceMember
    from app.db.supymem_models import Notice
    from core.clock import set_clock
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[m.__table__ for m in (
        Approval, Person, Venture, Workspace, WorkspaceMember, Notice)])
    factory = sessionmaker(engine, expire_on_commit=False)
    now = datetime(2026, 10, 8, 12, tzinfo=timezone.utc)
    clock = [now]
    set_clock(lambda: clock[0])
    monkeypatch.setattr(service, "db", lambda: factory)
    monkeypatch.setattr(service, "audit", lambda *a, **kw: None)
    monkeypatch.setattr(desktop_notices, "db", lambda: factory)
    with factory() as session:
        session.add(Person(id="per_notification", display_name="Synthetic", created_at=now))
        session.add(Workspace(id="wsp_notification", owner_id="per_notification", name="Synthetic",
            kind="company", entry_mode="idea", created_at=now, updated_at=now))
        session.add(WorkspaceMember(id="wmb_notification", workspace_id="wsp_notification", person_id="per_notification",
            role="owner", can_approve=True, created_at=now))
        session.commit()
    yield factory, service, desktop_notices, clock
    set_clock(None)
    engine.dispose()


def request(d):
    return d[1].request_approval(workspace_id="wsp_notification", kind="external_comms",
        subject_ref="reviewed_subject", summary="Private saved preview", requested_by="agt_synthetic")


def pending(d):
    return d[2].pending(workspace_ids={"wsp_notification"}, person_id="per_notification")


def test_initial_and_halfway_delivery_are_committed_once_and_project_current_state(approvals):
    from app.db.supymem_models import Notice
    factory, service, _, clock = approvals
    view = request(approvals)
    first = pending(approvals)
    assert view.notified_count == 1 and len(first) == 1
    assert first[0]["current_state_verified"] is True
    assert "Private saved preview" not in str(first)
    clock[0] += timedelta(hours=37)
    assert service.sweep_notifications() == [(view.id, "per_notification")]
    assert service.sweep_notifications() == []
    assert len(pending(approvals)) == 1  # same approval is projected once
    with factory() as session:
        rows = session.scalars(select(Notice)).all()
        assert len(rows) == 2 and all(r.person_id == "per_notification" for r in rows)
    assert approvals[2].pending(workspace_ids={"wsp_notification"}, person_id="per_other") == []


@pytest.mark.parametrize("status", ["approved", "rejected", "withdrawn", "expired"])
def test_old_notices_disappear_after_a_decision(approvals, status):
    from app.db.models import Approval
    view = request(approvals)
    with approvals[0]() as session:
        session.get(Approval, view.id).status = status
        session.commit()
    assert pending(approvals) == []


def test_no_reminder_or_notice_survives_expiry_even_before_expiry_sweep(approvals):
    from app.db.models import Approval
    view = request(approvals)
    approvals[3][0] += timedelta(hours=73)
    assert approvals[1].sweep_notifications() == []
    assert pending(approvals) == []
    with approvals[0]() as session:
        assert session.get(Approval, view.id).notified_count == 1


def test_revoked_approval_authority_cannot_receive_a_saved_or_new_reminder(approvals):
    from app.db.models import WorkspaceMember
    request(approvals)
    with approvals[0]() as session:
        session.get(WorkspaceMember, "wmb_notification").can_approve = False
        session.commit()
    approvals[3][0] += timedelta(hours=37)
    assert approvals[1].sweep_notifications() == []
    assert pending(approvals) == []
