"""Synthetic SQLite transactions prove stage hooks follow real committed evidence."""
from __future__ import annotations

import hashlib
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from tests.company_os_harness import load_company_os_app


@pytest.fixture()
def evidence(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/evidence.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LLM_OFFLINE", "1")
    load_company_os_app()
    from app.authz.workspace import ensure_subject_person
    from app.conversion import waitlist
    from app.db.models import GtmContact, GtmTouch, Venture
    from app.db.supymem_models import VentureWaitlist, VentureWaitlistEntry
    from app.gtm.supymem import pipeline
    from app.state import db
    from app.ventures import stage
    from app.workspaces.service import create_workspace
    from core.clock import utcnow

    actor = ensure_subject_person("synthetic_stage_hook_founder")
    workspace = create_workspace(kind="company", name="Synthetic evidence fixture", owner_id=actor)
    factory, vid, contact, now = db(), "ven_evidence_hook", "gtc_evidence_hook", utcnow()
    with factory() as session:
        session.add(Venture(id=vid, workspace_id=workspace.id, name="Fixture", raw_idea="Synthetic booking product",
                            stage="draft", created_at=now, updated_at=now))
        session.add(GtmContact(id=contact, workspace_id=workspace.id, email="contact@fixture.invalid",
                               created_at=now, stage_changed_at=now))
        session.flush()
        session.add(VentureWaitlist(venture_id=vid, workspace_id=workspace.id, enabled=True,
            enabled_by=actor, consent_version=waitlist.VERSION, updated_at=now))
        session.add(GtmTouch(id="gtmtch_owned_send", workspace_id=workspace.id, venture_id=vid,
            contact_id=contact, provider_thread_id="owned-thread", direction="out", channel="email",
            simulated=0, sent_at=now, created_at=now))
        session.commit()
    events = []
    def after_evidence(owner, event_key):
        # A second session cannot see uncommitted rows. The key carries only
        # the committed record identity, never email, provider body or thread.
        prefix, identity = event_key.rsplit(":", 1)
        model = VentureWaitlistEntry if prefix == "waitlist.signup" else GtmTouch
        assert prefix in {"waitlist.signup", "gtm.reply"}
        with factory() as session:
            row = session.get(model, identity)
            assert row is not None and row.venture_id == owner == vid and row.workspace_id == workspace.id
            if model is VentureWaitlistEntry:
                assert row.is_test is False
            else:
                assert row.simulated == 0 and row.direction == "in" and row.classification.startswith("human_")
        events.append(("stage", event_key))
    monkeypatch.setattr(stage, "after_evidence", after_evidence, raising=False)
    monkeypatch.setattr(pipeline.learning, "record", lambda *args, **kwargs: events.append(("learning", None)))
    payload = {"email": "signup@sample-business.example", "consent": "yes", "consent_version": waitlist.VERSION,
               "consent_digest": hashlib.sha256(waitlist.consent_text("Fixture").encode()).hexdigest()}
    return SimpleNamespace(waitlist=waitlist, pipeline=pipeline, stage=stage, factory=factory,
        vid=vid, wid=workspace.id, contact=contact, payload=payload, events=events)


def test_real_signup_hook_observes_commit_and_duplicate_does_not_replay(evidence):
    e = evidence
    assert e.waitlist.capture(e.vid, e.payload, with_new=True) == ({"received": True, "email_verified": False}, True)
    assert len(e.events) == 1 and e.events[0][1].startswith("waitlist.signup:wle_")
    assert e.waitlist.capture(e.vid, e.payload, with_new=True) == ({"received": True, "email_verified": False}, False)
    assert len(e.events) == 1


def test_synthetic_waitlist_probe_never_changes_stage(evidence):
    from app.db.supymem_models import VentureWaitlist
    from core.clock import utcnow
    e, token = evidence, "synthetic-probe-token"
    with e.factory() as session:
        row = session.get(VentureWaitlist, e.vid)
        row.probe_hash = hashlib.sha256(token.encode()).hexdigest()
        row.probe_expires_at = utcnow() + timedelta(seconds=60)
        session.commit()
    payload = {**e.payload, "email": "signup@qa.invalid", "test_token": token}
    assert e.waitlist.capture(e.vid, payload, with_new=True)[1] is False
    assert e.events == []


def test_waitlist_scope_failure_never_calls_stage(evidence):
    from app.db.models import Venture
    e = evidence
    with e.factory() as session:
        session.get(Venture, e.vid).workspace_id = "foreign-workspace"
        session.commit()
    with pytest.raises(e.waitlist.WaitlistError, match="waitlist_not_found"):
        e.waitlist.capture(e.vid, e.payload)
    assert e.events == []


@pytest.mark.parametrize("classification", ["human_positive", "human_neutral", "human_negative"])
def test_attributed_human_reply_hooks_commit_before_learning_and_not_on_replay(evidence, classification):
    e = evidence
    args = dict(venture_id=e.vid, provider_thread_id="owned-thread", provider_message_id="provider-reply",
                classification=classification, body="Synthetic reply fixture")
    assert e.pipeline.apply_reply(e.contact, **args) is True
    assert [event[0] for event in e.events] == ["stage", "learning"]
    assert e.events[0][1].startswith("gtm.reply:gtmtch_")
    assert e.pipeline.apply_reply(e.contact, **args) is False
    assert len(e.events) == 2


@pytest.mark.parametrize("classification,attributed", [("automated", True), ("unclassified", True), ("human_neutral", False)])
def test_automatic_unknown_or_unattributed_reply_does_not_change_stage(evidence, classification, attributed):
    e = evidence
    args = dict(venture_id=e.vid, provider_thread_id="owned-thread", provider_message_id="provider-reply") if attributed else {}
    assert e.pipeline.apply_reply(e.contact, classification=classification, **args) is True
    assert e.events == [("learning", None)]


@pytest.mark.parametrize("failure", ["missing_contact", "foreign_workspace", "foreign_thread", "ambiguous_thread"])
def test_reply_failed_attribution_never_calls_stage(evidence, failure):
    from app.db.models import GtmTouch, Venture
    from core.clock import utcnow
    e = evidence
    args = dict(venture_id=e.vid, provider_thread_id="owned-thread", provider_message_id="provider-reply")
    if failure == "missing_contact":
        assert e.pipeline.apply_reply("gtc_missing", **args) is False
    else:
        if failure == "foreign_workspace":
            with e.factory() as session:
                session.get(Venture, e.vid).workspace_id = "foreign-workspace"
                session.commit()
        elif failure == "foreign_thread":
            args["provider_thread_id"] = "foreign-thread"
        else:
            with e.factory() as session:
                session.add(GtmTouch(id="gtmtch_unattributed_send", workspace_id=e.wid, venture_id=None,
                    contact_id=e.contact, provider_thread_id="owned-thread", direction="out", channel="email",
                    simulated=0, sent_at=utcnow(), created_at=utcnow()))
                session.commit()
        with pytest.raises(ValueError, match="reply_(?:venture|thread)_scope"):
            e.pipeline.apply_reply(e.contact, **args)
    assert e.events == []


@pytest.mark.parametrize("flow", ["waitlist", "reply"])
def test_commit_failure_rolls_back_evidence_and_never_calls_stage(evidence, monkeypatch, flow):
    from app.db.models import GtmTouch
    from app.db.supymem_models import VentureWaitlistEntry
    e = evidence
    def failed_commit():
        raise RuntimeError("synthetic_commit_failure")
    def factory():
        session = e.factory()
        monkeypatch.setattr(session, "commit", failed_commit)
        return session
    module = e.waitlist if flow == "waitlist" else e.pipeline
    monkeypatch.setattr(module, "db", lambda: factory)
    with pytest.raises(RuntimeError, match="synthetic_commit_failure"):
        if flow == "waitlist":
            e.waitlist.capture(e.vid, e.payload)
        else:
            e.pipeline.apply_reply(e.contact, venture_id=e.vid, provider_thread_id="owned-thread", provider_message_id="provider-reply")
    assert e.events == []
    with e.factory() as session:
        model = VentureWaitlistEntry if flow == "waitlist" else GtmTouch
        query = select(model.id).where(model.venture_id == e.vid)
        if flow == "reply":
            query = query.where(GtmTouch.direction == "in")
        assert session.scalar(query) is None


@pytest.mark.parametrize("flow", ["waitlist", "reply"])
def test_hook_exception_cannot_change_original_success(evidence, monkeypatch, flow):
    e = evidence
    def failed_hook(*args):
        raise RuntimeError("synthetic_hook_failure")
    monkeypatch.setattr(e.stage, "after_evidence", failed_hook)
    if flow == "waitlist":
        assert e.waitlist.capture(e.vid, e.payload) == {"received": True, "email_verified": False}
    else:
        assert e.pipeline.apply_reply(e.contact, venture_id=e.vid, provider_thread_id="owned-thread", provider_message_id="provider-reply") is True
        assert e.events == [("learning", None)]
