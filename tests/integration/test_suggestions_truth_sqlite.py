import json

import pytest
from sqlalchemy import select

from tests.unit.suggestions.test_suggestions import env as shared_env
from tests.unit.suggestions.test_truthful_suggestions import proposal


@pytest.fixture(name="env")
def suggestions_env(tmp_path, monkeypatch):
    yield from shared_env.__wrapped__(tmp_path, monkeypatch)


def test_completing_card_simulation_on_sqlite(env, monkeypatch):
    from app.autopilot import service
    from app.db.models import Job, Venture
    from app.state import db
    from core import clock
    from tests.integration.godmode_world import FakeClock, World
    from tests.integration.test_postgres_godmode_simulation import (
        test_suggestion_cards_are_executable_and_tapping_one_really_runs_it as run_scenario,
    )

    _, vid = env
    with db()() as session:
        wid = session.get(Venture, vid).workspace_id
        for job in session.scalars(select(Job).where(Job.venture_id == vid)):
            job.status = "succeeded"
        session.commit()
    fake_clock = FakeClock(clock.utcnow())
    monkeypatch.setattr(clock, "_clock", fake_clock)
    world = World(vid=vid, wid=wid, owner=service._owner(vid), clock=fake_clock, mp=monkeypatch)
    world.install()
    run_scenario(world)


def test_live_scoped_pagination_expires_stale_cards(env, monkeypatch):
    from app.db.models import Venture
    from app.db.supymem_models import Suggestion
    from app.state import db
    from app.suggestions import service
    from core.clock import utcnow

    _, vid = env
    monkeypatch.setattr(service.proposals, "propose_next_actions", lambda *_: [proposal()])
    binding = service._state_binding(vid)
    with db()() as session:
        wid = session.get(Venture, vid).workspace_id
        for index in range(61):
            session.add(Suggestion(id=f"page_{index:03}", venture_id=vid, title="Research", action_kind="research.start",
                payload_json=json.dumps({"_suggestion_state": binding}), dedupe_key=str(index), created_at=utcnow()))
        session.add(Suggestion(id="stale", venture_id=vid, title="Navigation", action_kind="gtm.diagnosis",
                               dedupe_key="stale", created_at=utcnow()))
        session.commit()
    first = service.list_for({wid}, venture_id=vid)
    second = service.list_for({wid}, venture_id=vid, offset=first["next_offset"])
    assert first["new"] == 61 and len(first["items"]) == 50 and len(second["items"]) == 11
    assert len({item["id"] for item in first["items"] + second["items"]}) == 61
    assert service.get("stale").status == "expired"


@pytest.mark.parametrize("terminal,status", [("succeeded", "done"), ("failed", "needs_you"), ("cancelled", "needs_you")])
def test_saved_job_reconciles_to_truth(env, terminal, status):
    from app.db.models import Job, Venture
    from app.db.supymem_models import Suggestion
    from app.db.supymem_models import ResearchDossier
    from app.research.execution import identifier
    from app.state import db
    from app.suggestions import service
    from core.clock import utcnow

    _, vid = env
    with db()() as session:
        wid = session.get(Venture, vid).workspace_id
        session.add(Job(id="truth_job", kind="research.deep", venture_id=vid, workspace_id=wid,
                        payload_json="{}", status="running", created_at=utcnow()))
        session.add(Suggestion(id="truth_card", venture_id=vid, title="Research", action_kind="research.start",
            status="running", dedupe_key="truth", created_at=utcnow(),
            result_ref=json.dumps({"jobs": ["truth_job"], "href": f"/v/{vid}/research"})))
        session.commit()
    service._reconcile({vid})
    assert service.get("truth_card").status == "running"
    with db()() as session:
        session.get(Job, "truth_job").status = terminal
        if terminal == "succeeded":
            session.add(ResearchDossier(id=identifier("truth_job"), venture_id=vid, workspace_id=wid,
                                       kind="deep", state="complete", created_at=utcnow(), summary="{}"))
        session.commit()
    service._reconcile({vid})
    assert service.get("truth_card").status == status
    assert service.public(service.get("truth_card"))["result_href"] == f"/v/{vid}/research"


def test_job_success_without_saved_result_needs_review(env):
    from app.db.models import Job, Venture
    from app.db.supymem_models import Suggestion
    from app.state import db
    from app.suggestions import service
    from core.clock import utcnow

    _, vid = env
    with db()() as session:
        wid = session.get(Venture, vid).workspace_id
        session.add(Job(id="no_result_job", kind="research.deep", venture_id=vid, workspace_id=wid,
                        payload_json="{}", status="succeeded", created_at=utcnow()))
        session.add(Suggestion(id="no_result_card", venture_id=vid, title="Research", action_kind="research.start",
            status="running", dedupe_key="no_result", created_at=utcnow(),
            result_ref=json.dumps({"jobs": ["no_result_job"], "href": f"/v/{vid}/research"})))
        session.commit()
    service._reconcile({vid})
    assert service.get("no_result_card").status == "needs_you"


def test_successful_waitlist_and_experiment_writes_have_owned_receipts(env):
    from app.autopilot import receipts, service
    from app.autopilot.authority import founder_scope
    from app.conversion import waitlist
    from app.db.models import Venture
    from app.db.supymem_models import Experiment
    from app.experiments import ledger
    from app.state import db

    _, vid = env
    with db()() as session:
        wid = session.get(Venture, vid).workspace_id
        session.add(Experiment(id="receipt_experiment", venture_id=vid, workspace_id=wid,
                               kind=ledger.KIND, state="proposed", hypothesis="Test one message"))
        session.commit()
    with founder_scope(vid, service._owner(vid)):
        with receipts.collect(vid) as evidence:
            waitlist.configure(vid, wid, enabled=True, actor=service._owner(vid))
            ledger.start("receipt_experiment", vid)
    assert evidence.outcome() == "completed"
    assert {model.__name__ for model, _, _ in evidence.persisted} == {"VentureWaitlist", "Experiment"}


@pytest.mark.parametrize("ready", [False, True])
def test_pagebuilder_queue_success_requires_private_preview_evidence(env, monkeypatch, ready):
    from app.db.models import Job, Venture
    from app.db.supymem_models import Suggestion
    from app.pagebuilder import api
    from app.state import db
    from app.suggestions import service
    from core.clock import utcnow

    _, vid = env
    with db()() as session:
        wid = session.get(Venture, vid).workspace_id
        session.add(Job(id="preview_job", kind=api.KIND, workspace_id=wid, venture_id=vid,
            status="succeeded", created_at=utcnow(), payload_json=json.dumps({
                "variant_count": 1, "result": {}, "_runtime": {"state": "ready", "stage": "ready",
                "progress": 1, "calls_used": 0, "operations": {}, "quote": {"ceiling_micro": 10000, "payer": "platform"}}})))
        session.add(Suggestion(id="preview_card", venture_id=vid, action_kind=api.KIND, title="Private preview",
            status="running", created_at=utcnow(), dedupe_key="preview", result_ref=json.dumps({"jobs": ["preview_job"]})))
        session.commit()
    if ready:
        monkeypatch.setattr(api, "_candidate_current", lambda doc: True)
    service._reconcile({vid})
    result = service.public(service.get("preview_card"))
    assert result["result_state"] == ("done" if ready else "needs_you")
    assert result["result_href"] == f"/v/{vid}/site"
    if ready:
        assert result["result_message"] == "Private landing page preview ready"
