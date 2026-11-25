"""Independent adversarial GTM checks. No live research or email provider."""
from __future__ import annotations

import asyncio

import pytest

from tests.company_os_harness import load_company_os_app
from tests.unit.intake.test_contact_import import _commit, _preview, journey_db


@pytest.fixture()
def journey_env(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/journey-abuse.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COMPANY_OS_WORKING_MEMORY_PATH", str(tmp_path / "memory.db"))
    monkeypatch.setenv("LLM_OFFLINE", "true")
    monkeypatch.setenv("SANDBOX_MODE", "true")
    monkeypatch.setenv("SUPYMEM_OUTBOUND_EXECUTION_ENABLED", "false")
    load_company_os_app()
    from app import state
    from app.autopilot import outreach
    from app.db.models import Person, Venture, Workspace
    from app.gtm import journey
    from core.clock import utcnow

    state.reset_db_factory()
    now = utcnow()
    with state.db()() as session:
        session.add(Person(id="per_abuse_fixture", display_name="Fixture Founder", created_at=now))
        session.commit()
        session.add(Workspace(id="ws_abuse_fixture", kind="company", name="Fixture workspace", entry_mode="existing_company",
            owner_id="per_abuse_fixture", created_at=now, updated_at=now))
        session.add(Venture(id="ven_abuse_fixture", workspace_id="ws_abuse_fixture", name="Fixture Product", raw_idea="A customer interview note tool",
            stage="idea", created_at=now, updated_at=now))
        session.commit()
    monkeypatch.setattr(journey, "authority", lambda *a, **kw: "per_abuse_fixture")
    monkeypatch.setattr(outreach, "site_url", lambda v: None)
    monkeypatch.setattr(outreach, "_brief", lambda vid: {"idea": {"audience": "founders running customer interviews", "problem": "customer interviews", "confirmed": True}})
    monkeypatch.setattr(journey, "_icp_seed", lambda vid: outreach._brief(vid))
    return journey, "ws_abuse_fixture", "ven_abuse_fixture"


def _contact():
    return {"name": "Fixture Avery", "company": "Fixture Company", "email": "avery@fixture.example",
        "source_url": "https://fixture.example/contact", "country": "CA", "lawful_basis": "consent",
        "lawful_basis_note": "Synthetic fixture express permission for this test only."}


@pytest.mark.parametrize("url", ["javascript:alert(1)", "http://127.0.0.1/secret", "http://169.254.169.254/latest/meta-data", "https://user:password@fixture.example/"])
def test_source_abuse_cannot_create_prospect(journey_env, url):
    journey, wid, vid = journey_env
    with pytest.raises(ValueError, match="gtm_source_url_invalid"):
        journey.add_prospect(wid, vid, {**_contact(), "source_url": url})
    assert journey.status(wid, vid)["prospects"] == []


def test_cross_scope_contact_update_cannot_change_permission(journey_env):
    journey, wid, vid = journey_env
    view = journey.add_prospect(wid, vid, _contact())
    cid = view["prospects"][0]["id"]
    with pytest.raises(ValueError, match="gtm_not_found"):
        journey.patch_prospect("ws_foreign_fixture", vid, cid, {"country": "US"})
    assert journey.status(wid, vid)["prospects"][0]["country"] == "CA"
    with pytest.raises(ValueError, match="gtm_not_found"):
        journey.status("ws_foreign_fixture", vid, authorize=False)


@pytest.mark.parametrize("audience,problem", [("warehouse planners", "late shipments"),
                                             ("families planning trips", "finding accessible routes")])
def test_review_edit_cannot_retain_approval_or_reuse_old_digest(journey_db, monkeypatch, audience, problem):
    from app.autopilot import outreach
    from app.gtm import journey

    wid, vid = journey_db["wid"], journey_db["vid"]
    monkeypatch.setattr(outreach, "_brief", lambda _vid: {"idea": {
        "audience": audience, "problem": problem, "confirmed": True}})
    contact = _contact()
    preview = _preview(journey_db, monkeypatch,
        f"name,email,company,country\n{contact['name']},{contact['email']},{contact['company']},{contact['country']}")
    imported = _commit(journey_db, preview, lawful_basis_note=contact["lawful_basis_note"])
    assert imported["imported"] == 1 and imported["drafts_status"] == "not_requested"
    assert outreach.drafts(vid) == []
    journey.draft_for_review(vid, contact_id=imported["contact_ids"][0])
    view = journey.status(wid, vid)
    draft = view["drafts"][0]
    view = journey.patch_draft(wid, vid, draft["doc_id"], {"decision": "approved", "expected_revision": draft["revision"]})
    old = view["batches"][0]
    view = journey.patch_draft(wid, vid, draft["doc_id"], {
        "expected_revision": view["drafts"][0]["revision"], "body": draft["body"] + "\n\nThank you.", "decision": "approved"})
    assert view["drafts"][0]["review_state"] == "draft"
    assert all(b["approved_count"] == 0 for b in view["batches"])
    monkeypatch.setattr(outreach, "_send_one", lambda *_args:
                        pytest.fail("An edited draft must not be sent using old approval."))
    with pytest.raises(ValueError, match="gtm_approval_changed"):
        asyncio.run(journey.send(wid, vid, {"batch_id": old["batch_id"], "draft_ids": old["draft_ids"],
            "approval_digest": old["approval_digest"], "cap": old["cap"]}))


@pytest.mark.parametrize("country,basis", [("", "consent"), ("CA", "none"), ("DE", "legitimate_interest")])
def test_missing_or_disallowed_permission_does_not_generate_draft(journey_env, country, basis):
    journey, wid, vid = journey_env
    view = journey.add_prospect(wid, vid, {**_contact(), "country": country, "lawful_basis": basis})
    assert not view["prospects"][0]["eligible"]
    assert view["drafts"] == []


@pytest.mark.parametrize("audience,problem", [
    ("Procurement managers", "Supplier approvals are scattered across email threads"),
    ("Parents arranging school carpools", "Changing schedules cause missed pickups"),
])
def test_worker_cannot_run_with_another_workers_lease(journey_env, audience, problem):
    journey, wid, vid = journey_env
    from app.core import jobs
    from app.state import db
    from tests.company_os_harness import configure_local_test_model

    configure_local_test_model()
    journey.update_icp(wid, vid, audience, problem=problem, recruitment_opt_in=True)
    with db()() as session:
        claimed = jobs.lease(session, "actual-fixture-worker", kinds=["gtm.journey.prepare"])
        session.commit()
    job = claimed[0]
    with pytest.raises(jobs.FatalError, match="gtm_prepare_scope_or_lease_invalid"):
        asyncio.run(journey.run_prepare({**job.payload, "workspace_id": wid, "venture_id": vid,
            "_job_id": job.id, "_worker_id": "foreign-fixture-worker", "_job_attempt": job.attempts}))
    assert journey.status(wid, vid)["state"] == "queued"
