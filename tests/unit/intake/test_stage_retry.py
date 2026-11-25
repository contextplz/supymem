"""Real SQLite source/outbox state; no provider, worker or public-effect claim."""
import json

import pytest
from sqlalchemy import select

from tests.company_os_harness import load_company_os_app


@pytest.fixture()
def event(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/stage-retry.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    load_company_os_app()
    from app.authz.workspace import ensure_subject_person
    from app.db.models import Venture
    from app.db.supymem_models import VentureWaitlist, VentureWaitlistEntry
    from app.state import db
    from app.ventures import stage
    from app.workspaces.service import create_workspace
    from core.clock import utcnow
    owner = ensure_subject_person("usr_stage_retry")
    workspace = create_workspace(kind="company", name="Stage retry", owner_id=owner)
    now, vid = utcnow(), "ven_stage_retry"
    with db()() as session:
        session.add(Venture(id=vid, workspace_id=workspace.id, name="Booking", raw_idea="Booking software",
                            stage="draft", autonomy_level="L1", created_at=now, updated_at=now))
        session.flush()
        session.add(VentureWaitlist(venture_id=vid, workspace_id=workspace.id, enabled=True,
            consent_version="test", enabled_by=owner, updated_at=now))
        session.flush()
        session.add(VentureWaitlistEntry(id="entry_retry", venture_id=vid, workspace_id=workspace.id,
            email="qa@example.test", consent_version="test", consent_text="Synthetic consent", is_test=False, created_at=now))
        session.commit()
    return stage, vid, workspace.id, owner, "waitlist.signup:entry_retry"


def test_committed_evidence_persists_without_queue_and_read_is_stable(event):
    from app.db.models import Job, VentureDocument
    from app.state import db
    stage, vid, _, _, key = event
    assert stage.after_evidence(vid, key) is True
    assert stage.get_stage(vid)["stage"] == "traction"
    with db()() as session:
        assert session.get(VentureDocument, "founder_stage:" + vid)
        assert not list(session.scalars(select(Job)))


def test_failure_queues_once_with_actual_owner_and_no_private_destination(event, monkeypatch):
    from app.chat import conversations
    from app.db.models import Job
    from app.state import db
    from app.tenancy import bind, current
    stage, vid, wid, owner, key = event
    def unavailable(_):
        raise RuntimeError("Synthetic exception detail must not be exported")
    monkeypatch.setattr(stage, "reassess", unavailable)
    foreign_private = conversations.ConversationScope("private_fixture", vid, wid, "usr_other", "private")
    token = conversations._CURRENT.set(foreign_private)
    try:
        with bind(workspace_id=wid, venture_id=vid, actor="usr_other"):
            assert stage.after_evidence(vid, key) is False
            assert stage.after_evidence(vid, key) is False
            assert current().actor == "usr_other"
            assert conversations.current_conversation() == foreign_private
    finally:
        conversations._CURRENT.reset(token)
    with db()() as session:
        jobs = list(session.scalars(select(Job)))
        assert len(jobs) == 1
        row, payload = jobs[0], json.loads(jobs[0].payload_json)
        assert (row.kind, row.venture_id, row.workspace_id, row.max_attempts) == ("milestone.reassess", vid, wid, 2)
        assert payload["_actor_subject"] == owner and payload["event_key"] == key
        assert payload.get("_chat_conversation") is None
        assert row.dedupe_key == "stage.evidence:" + key


def test_worker_rechecks_scope_current_owner_and_source_before_persistence(event):
    from app.db.models import VentureDocument
    from app.state import db
    from app.tenancy import bind
    stage, vid, wid, owner, key = event
    with bind(workspace_id=wid, venture_id=vid, actor="usr_other"), pytest.raises(PermissionError):
        stage.reassess_evidence(vid, key, wid)
    with bind(workspace_id=wid, venture_id=vid, actor=owner), pytest.raises(ValueError):
        stage.reassess_evidence(vid, key, "foreign_workspace")
    with db()() as session:
        assert session.get(VentureDocument, "founder_stage:" + vid) is None
    with bind(workspace_id=wid, venture_id=vid, actor=owner):
        assert stage.reassess_evidence(vid, key, wid)["stage"] == "traction"


@pytest.mark.parametrize("change", ["owner", "blocked", "archived"])
def test_custody_is_rechecked_after_wait_before_stage_write(event, monkeypatch, change):
    from app.authz.workspace import ensure_subject_person
    from app.db.models import VentureDocument, Workspace
    from app.state import db
    from app.tenancy import bind
    from core.clock import utcnow
    stage, vid, wid, owner, key = event
    ensure_subject_person("usr_new_owner")
    original = stage.serialize_writes
    def waited(session, **kwargs):
        # Actual committed custody changes at the lock boundary, not a stubbed owner result.
        with db()() as concurrent:
            row = concurrent.get(Workspace, wid)
            if change == "owner":
                row.owner_id = "usr_new_owner"
            elif change == "blocked":
                row.settings_json = '{"blocked":true}'
            else:
                row.archived_at = utcnow()
            concurrent.commit()
        return original(session, **kwargs)
    monkeypatch.setattr(stage, "serialize_writes", waited)
    with bind(workspace_id=wid, venture_id=vid, actor=owner), pytest.raises((ValueError, PermissionError)):
        stage.reassess_evidence(vid, key, wid)
    with db()() as session:
        assert session.get(VentureDocument, "founder_stage:" + vid) is None


@pytest.mark.parametrize("key", ["waitlist.signup:missing", "gtm.reply:missing", "publication:missing", "unknown:entry_retry", "bad"])
def test_invalid_events_cannot_write_or_queue(event, key):
    from app.db.models import Job, VentureDocument
    from app.state import db
    stage, vid, _, _, _ = event
    assert stage.after_evidence(vid, key) is False
    with db()() as session:
        assert session.get(VentureDocument, "founder_stage:" + vid) is None
        assert not list(session.scalars(select(Job)))


def test_test_signup_is_not_evidence(event):
    from app.db.supymem_models import VentureWaitlistEntry
    from app.state import db
    stage, vid, _, _, key = event
    with db()() as session:
        session.get(VentureWaitlistEntry, "entry_retry").is_test = True
        session.commit()
    assert stage.after_evidence(vid, key) is False


def test_retry_source_validation_requires_attributed_actual_human_reply(event):
    from app.db.models import GtmContact, GtmTouch
    from app.state import db
    from core.clock import utcnow
    stage, vid, wid, owner, _ = event
    with db()() as session:
        session.add(GtmContact(id="contact_reply", workspace_id=wid, email="qa@example.test",
            source=f"venture:{vid}:founder_import", created_at=utcnow(), stage_changed_at=utcnow()))
        session.flush()
        session.add(GtmTouch(id="touch_reply", contact_id="contact_reply", workspace_id=wid, venture_id=vid,
            direction="in", classification="human_positive", simulated=0, created_at=utcnow()))
        session.commit()
    assert stage.evidence_context(vid, "gtm.reply:touch_reply")["owner_person_id"] == owner
    for field, value in (("classification", "automated"), ("simulated", 1), ("venture_id", None), ("workspace_id", "foreign")):
        identity = "touch_invalid_" + field
        values = dict(id=identity, contact_id="contact_reply", workspace_id=wid, venture_id=vid,
                      direction="in", classification="human_positive", simulated=0, created_at=utcnow())
        values[field] = value
        with db()() as session:
            session.add(GtmTouch(**values))
            session.commit()
        with pytest.raises(ValueError):
            stage.evidence_context(vid, "gtm.reply:" + identity)


def test_retry_source_validation_requires_live_public_reviewed_source(event):
    from app.db.supymem_models import Deployment, FounderApp
    from app.state import db
    from core.clock import utcnow
    stage, vid, wid, _, _ = event
    with db()() as session:
        session.add(FounderApp(id="app00001", workspace_id=wid, venture_id=vid, slug="stage-fixture", name="Stage fixture",
            kind="static", created_at=utcnow(), updated_at=utcnow()))
        session.flush()
        session.add(Deployment(id="dep00001", app_id="app00001", workspace_id=wid, venture_id=vid,
            target="supymem_static", state="live", version=1, build_artifact_id="synthetic_artifact",
            live_at=utcnow(), url="https://synthetic.supymem.com"))
        session.commit()
    assert stage.evidence_context(vid, "publication:dep00001")["workspace_id"] == wid
    for field, value in (("state", "pending"), ("target", "private_runtime"), ("build_artifact_id", None), ("live_at", None)):
        with db()() as session:
            row = session.get(Deployment, "dep00001")
            before = getattr(row, field)
            setattr(row, field, value)
            session.commit()
        with pytest.raises(ValueError):
            stage.evidence_context(vid, "publication:dep00001")
        with db()() as session:
            setattr(session.get(Deployment, "dep00001"), field, before)
            session.commit()
    with db()() as session:
        session.get(FounderApp, "app00001").deleted_at = utcnow()
        session.commit()
    with pytest.raises(ValueError):
        stage.evidence_context(vid, "publication:dep00001")


def test_queue_failure_does_not_erase_original_source_or_expose_exception(event, monkeypatch, caplog):
    from app.core import jobs
    from app.db.supymem_models import VentureWaitlistEntry
    from app.state import db
    stage, vid, _, _, key = event
    def unavailable(*_, **__):
        raise RuntimeError("Synthetic private failure detail")
    monkeypatch.setattr(stage, "reassess", unavailable)
    monkeypatch.setattr(jobs, "enqueue", unavailable)
    assert stage.after_evidence(vid, key) is False
    assert "Synthetic private failure detail" not in caplog.text
    with db()() as session:
        assert session.get(VentureWaitlistEntry, "entry_retry")
