"""Migrated PostgreSQL intake; public-page and research evidence are offline fixtures."""
from tests.integration.test_postgres_auth_security import pg_database as pg_database  # noqa: F401
from tests.integration.test_postgres_journey_jobs import journey_db as journey_db  # noqa: F401
from tests.integration.test_postgres_entry_scope import entry as entry  # noqa: F401


def test_live_site_commit_records_asset_research_then_gtm(entry, monkeypatch):
    import json
    from sqlalchemy import select
    from app.db.models import Job
    from app.db.supymem_models import IdeaSession, ResearchDossier
    from app.state import db
    from app.chat.recovery import external_site
    from app.intake.smart import IntakeResult
    from app.tenancy import bind
    from app.ventures.next_step import founder_next_step
    d, request = entry
    result = IntakeResult(path="gtm", confidence=0.95,
        extracted={"idea": "Booking software for rehearsal studios", "stage": "live", "existing_site_url": "https://bookflow.example.com"},
        founder_line="Let's get customers for your live product.", button_label="Get customers").to_dict()
    async def interpret(*args, **kwargs):
        return result
    monkeypatch.setattr("app.intake.application.interpret", interpret)
    monkeypatch.setattr("app.intake.assets._read_site", lambda _: {"status": "verified",
        "audit": {"recommendations": ["Check the signup form"]}, "note": "Synthetic public-page read for PG persistence."})
    proposal = request("/entry/chat/preview", {"text": "Our live booking product is at https://bookflow.example.com", "request_id": "pg-intake"}).json()["proposal"]
    response = request("/entry/chat/confirm", {"proposal_id": proposal["id"]})
    assert response.status_code == 200, response.text
    receipt = response.json()
    with bind(workspace_id=receipt["workspace_id"], venture_id=receipt["venture_id"], actor=d["actor"]):
        assert external_site(receipt["venture_id"])["url"] == "https://bookflow.example.com"
        running = founder_next_step(receipt["venture_id"])
        assert running["key"] == "research.running" and running["chat_action"] is None
        with db()() as session:
            job = session.scalar(select(Job).where(Job.venture_id == receipt["venture_id"], Job.kind == "research.deep"))
            assert job and job.workspace_id == receipt["workspace_id"]
            payload = json.loads(job.payload_json)
            assert payload["_actor_subject"] == d["actor"]
            idea = session.scalar(select(IdeaSession).where(IdeaSession.venture_id == receipt["venture_id"]))
            assert idea.state == "confirmed" and payload["revision_id"] == idea.confirmed_revision_id
            from core.clock import utcnow
            job.status, job.finished_at = "succeeded", utcnow()
            session.add(ResearchDossier(id="rsd_stage_intake", workspace_id=receipt["workspace_id"], venture_id=receipt["venture_id"],
                revision_id=idea.confirmed_revision_id, kind="deep", state="partial", budget_calls=1, calls_used=1,
                summary=json.dumps({"coverage_gaps": ["Synthetic offline dossier for routing"]}), created_at=utcnow(), completed_at=utcnow()))
            session.commit()
        assert founder_next_step(receipt["venture_id"])["key"] == "gtm.outreach"
    assert request("/entry/chat/confirm", {"proposal_id": proposal["id"]}).json() == receipt


def test_small_talk_clarifications_cancel_without_creating_a_venture(entry):
    from sqlalchemy import func, select
    from app.db.models import Venture
    from app.db.supymem_models import EntryChatProposal
    d, request = entry
    with d["factory"]() as session:
        baseline = session.scalar(select(func.count()).select_from(Venture))
    ids = []
    for index, text in enumerate(["hello", "hello\nClarification: how are you", "hello\nClarification: New idea"]):
        response = request("/entry/chat/preview", {"text": text, "request_id": f"greeting-{index}"})
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["status"] == "clarify" and len(result["onboarding_choices"] or result["choices"]) == 3
        ids.append(result["id"])
        assert request(f"/entry/chat/proposals/{result['id']}/cancel").json()["status"] == "cancelled"
    with d["factory"]() as session:
        assert session.scalar(select(func.count()).select_from(Venture)) == baseline
        assert all(session.get(EntryChatProposal, proposal_id).state == "cancelled" for proposal_id in ids)


def test_name_requests_have_distinct_idea_based_results_and_durable_replay(entry):
    d, request = entry
    response = request("/entry/chat/preview", {"text": "A booking calendar for independent tutors", "request_id": "name-proposal"})
    assert response.status_code == 200, response.text
    proposal = response.json()["proposal"]
    path = f"/entry/chat/proposals/{proposal['id']}/name"
    names = []
    for index in range(3):
        response = request(path, {"request_id": f"name-{index}"})
        assert response.status_code == 200, response.text
        name = response.json()["name"]
        assert name.casefold() not in {prior.casefold() for prior in names}
        assert "booking" in name.lower() or "calendar" in name.lower()
        assert request(path, {"request_id": f"name-{index}"}).json()["name"] == name
        names.append(name)
