"""Migrated PostgreSQL stage/history and actual resolver parity; offline evidence."""
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from tests.integration.test_postgres_auth_security import pg_database as pg_database  # noqa: F401
from tests.integration.test_postgres_journey_jobs import journey_db as journey_db  # noqa: F401


def test_saved_site_audit_routes_research_then_customers_without_rebuild(journey_db):
    from app.db.models import Job, VentureDocument
    from app.tenancy import bind
    from app.ventures import stage, next_step
    from core.clock import utcnow
    from tests.planning_fixtures import seed_research
    from app.db.supymem_models import IdeaRevision, IdeaSession

    d, now = journey_db, utcnow()
    with d["factory"]() as session:
        session.get(Job, d["job_id"]).status = "cancelled"
        session.add(VentureDocument(id=f"external_site:{d['vid']}", venture_id=d["vid"], kind="external_site",
            title="Imported site", path="https://booking.example.com", body_md=json.dumps({"status": "verified", "audit": {"recommendations": ["Check the signup form"]}}),
            created_at=now, updated_at=now))
        session.commit()
    with bind(workspace_id=d["wid"], venture_id=d["vid"], actor=d["actor"]):
        before = stage.reassess(d["vid"])
        assert before["stage"] == "page_live" and not before["confirmed_by_founder"]
        confirmed = stage.confirm_stage(d["vid"], "page_live")
        assert confirmed["history"][-1]["source"] == "founder_confirmation"
        assert next_step.next_step(d["vid"])["key"] == "research.start"
        assert next_step.next_step(d["vid"]) == next_step.founder_next_step(d["vid"])
        seed_research(d)  # Synthetic dossier is explicitly offline test evidence.
        assert next_step.next_step(d["vid"])["key"] == "gtm.outreach"
        assert next_step.next_step(d["vid"]) == next_step.founder_next_step(d["vid"])
        with d["factory"]() as session:
            session.add(IdeaRevision(id="idv_changed_direction", workspace_id=d["wid"], venture_id=d["vid"],
                session_id=d["sid"], rev=2, source="founder", structured=json.dumps({"offer": "Lesson bookings"}), created_at=now))
            session.flush()
            idea = session.get(IdeaSession, d["sid"])
            idea.current_revision_id = idea.confirmed_revision_id = "idv_changed_direction"
            session.commit()
        assert next_step.next_step(d["vid"])["key"] == "research.start"  # Older direction's report cannot skip research.
    with d["factory"]() as session:
        saved = json.loads(session.get(VentureDocument, f"founder_stage:{d['vid']}").body_md)
        assert saved["stage"] == "page_live" and saved["confirmed_by_founder"]
        assert any(evidence["source"] == "external_site" and evidence["link"] == "https://booking.example.com" for evidence in saved["evidence"])


def test_real_signup_reassesses_while_test_signup_does_not(journey_db):
    from app.db.supymem_models import VentureWaitlist, VentureWaitlistEntry
    from app.tenancy import bind
    from app.ventures import stage
    from core.clock import utcnow

    d, now = journey_db, utcnow()
    with d["factory"]() as session:
        session.add(VentureWaitlist(venture_id=d["vid"], workspace_id=d["wid"], enabled=True,
            consent_version="test", enabled_by=d["actor"], updated_at=now))
        session.flush()
        session.add(VentureWaitlistEntry(id="test_signup", workspace_id=d["wid"], venture_id=d["vid"],
            email="test@example.com", consent_version="test", consent_text="Test evidence", is_test=True, created_at=now))
        session.commit()
    with bind(workspace_id=d["wid"], venture_id=d["vid"], actor=d["actor"]):
        assert stage.reassess(d["vid"])["stage"] == "idea"
        with d["factory"]() as session:
            session.add(VentureWaitlistEntry(id="real_signup", workspace_id=d["wid"], venture_id=d["vid"],
                email="founder-test@example.com", consent_version="test", consent_text="Test consent", is_test=False, created_at=now))
            session.commit()
        record = stage.reassess(d["vid"])
        assert record["stage"] == "traction" and record["history"][-1]["previous"] == "idea"
        assert record["message"] == "You're now at Traction."


def test_worker_custody_is_revalidated_after_actual_advisory_wait(journey_db, monkeypatch):
    from app.authz.workspace import ensure_subject_person
    from app.db.models import VentureDocument, Workspace
    from app.db.session import serialize_writes
    from app.db.supymem_models import VentureWaitlist, VentureWaitlistEntry
    from app.tenancy import bind
    from app.ventures import stage
    from core.clock import utcnow

    d, waiting = journey_db, Event()
    next_owner = ensure_subject_person("usr_stage_new_owner")
    with d["factory"]() as session:
        session.add(VentureWaitlist(venture_id=d["vid"], workspace_id=d["wid"], enabled=True,
            consent_version="test", enabled_by=d["actor"], updated_at=utcnow()))
        session.flush()
        session.add(VentureWaitlistEntry(id="entry_custody", venture_id=d["vid"], workspace_id=d["wid"],
            email="qa@example.test", consent_version="test", consent_text="Synthetic consent", is_test=False, created_at=utcnow()))
        session.commit()
    original = stage.serialize_writes
    def lock(session, **kwargs):
        waiting.set()
        return original(session, **kwargs)
    monkeypatch.setattr(stage, "serialize_writes", lock)
    def worker():
        with bind(workspace_id=d["wid"], venture_id=d["vid"], actor=d["actor"]):
            return stage.reassess_evidence(d["vid"], "waitlist.signup:entry_custody", d["wid"])
    with ThreadPoolExecutor(max_workers=1) as pool, d["factory"]() as blocker:
        serialize_writes(blocker, key="founder-stage:" + d["vid"])
        future = pool.submit(worker)
        assert waiting.wait(5), "Worker must reach the actual held stage advisory lock"
        try:
            with d["factory"]() as transfer:
                transfer.get(Workspace, d["wid"]).owner_id = next_owner
                transfer.commit()
        finally:
            blocker.rollback()
        with pytest.raises(PermissionError, match="stage_reassessment_scope_mismatch"):
            future.result(timeout=10)
    with d["factory"]() as session:
        assert session.get(VentureDocument, "founder_stage:" + d["vid"]) is None
