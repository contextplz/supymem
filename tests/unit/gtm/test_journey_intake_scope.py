"""Eligible maintenance never starts discovery without the founder's consent."""
from __future__ import annotations

import asyncio
import json

import pytest
from sqlalchemy import select

from tests.unit.gtm.test_journey import journey_db as journey_db


def _saved_context(data, audience, problem):
    from app.core.local_mode import is_local

    if not is_local():
        return
    from app.db.models import Venture
    from app.ventures import founder_facts

    with data["factory"]() as session:
        founder_facts.save(session, session.get(Venture, data["vid"]), {
            "audience": founder_facts._fact(audience, "review", 1),
            "problem": founder_facts._fact(problem, "review", 1),
        })
        session.commit()


@pytest.mark.parametrize("audience", ["Dental clinic owners", "Parents arranging weekend outings"])
@pytest.mark.parametrize("eligible", [False, True])
def test_prepare_without_requested_work_releases_its_local_writer_lock(journey_db, monkeypatch, audience, eligible):
    from sqlalchemy import event, func
    from app.db.models import Job, Venture
    from app.gtm import journey

    data = journey_db
    with data["factory"]() as session:
        session.get(Venture, data["vid"]).raw_idea = "Planning for " + audience
        session.commit()
    monkeypatch.setattr(journey, "_discovery_eligible", lambda *args: eligible)
    engine = data["factory"].kw["bind"]
    def bound_wait(connection, record):
        connection.execute("PRAGMA busy_timeout=100")
    event.listen(engine, "connect", bound_wait)
    try:
        result = journey.prepare(data["wid"], data["vid"], authorize=False)
        assert not result["discovery_started"]
        with data["factory"]() as session:
            assert session.scalar(select(func.count()).select_from(Job)) == 0
    finally:
        event.remove(engine, "connect", bound_wait)


@pytest.mark.parametrize("audience,problem", [
    ("Freight invoice coordinators", "Invoice corrections require duplicate entry"),
    ("Families planning dinners", "Weekly menu changes cause wasted groceries"),
])
def test_reviewed_problem_matches_preserve_its_founder_entered_source(journey_db, audience, problem):
    from app.core.local_mode import is_local
    from app.gtm import journey

    _saved_context(journey_db, audience, problem)
    base = journey._icp(journey_db["vid"])
    if not is_local():
        base.update(who=audience, problem=problem, problem_basis="From your clarification answer.")
    saved = base | {"problem_basis": "From the problem you entered.", "confirmed": True, "chat_confirmed": True}
    restored = journey._journey_icp(journey_db["vid"], saved, base=base)
    assert restored["problem"] == problem
    assert restored["problem_basis"] == "From the problem you entered."


@pytest.mark.parametrize("recorded_intake,sibling_state,expected_count", [
    (True, "ordinary", 1),
    (False, "ordinary", 2),
    (True, "enrolled", 2),
    (True, "published", 2),
])
def test_maintainer_scopes_intake_without_losing_saved_or_published_enrollment(
    journey_db, monkeypatch, recorded_intake, sibling_state, expected_count,
):
    from app.db.models import Job, Venture, VentureDocument, Workspace
    from app.gtm import journey
    from app.tenancy import bind
    from core.clock import utcnow

    d = journey_db
    sibling = "ven_intake_sibling"
    monkeypatch.setattr(journey.outreach, "_brief", lambda vid: {"idea": {"audience": "Independent bakery owners"}})
    monkeypatch.setattr("app.ventures.state_brief._website_section", lambda wid, vid: {
        "live": sibling_state == "published" and vid == sibling,
    })
    with d["factory"]() as session:
        settings = {"entry_goal": "gtm"}
        if recorded_intake:
            settings["intake_venture_id"] = d["vid"]
        session.get(Workspace, d["wid"]).settings_json = json.dumps(settings)
        session.add(Venture(id=sibling, workspace_id=d["wid"], name="Sibling venture",
            raw_idea="Bakery inventory", stage="draft", created_at=utcnow(), updated_at=utcnow()))
        if sibling_state == "enrolled":
            journey._row(session, sibling, create=True)
        session.commit()

    with bind(workspace_id=d["wid"], venture_id=None, actor=d["actor"]):
        result = asyncio.run(journey.maintain_all())
    expected = {d["vid"]} | ({sibling} if expected_count == 2 else set())
    assert set(result["prepared"]) == expected
    with d["factory"]() as session:
        rows = list(session.scalars(select(VentureDocument).where(VentureDocument.kind == journey.KIND)))
        jobs = list(session.scalars(select(Job).where(Job.kind == "gtm.journey.prepare")))
        assert {row.venture_id for row in rows} == ({sibling} if sibling_state == "enrolled" else set())
        assert jobs == []


def test_maintainer_prepares_and_polls_a_founder_started_journey(journey_db, monkeypatch):
    from app.gtm import journey
    from app.db.models import Job
    d, polled, prepared = journey_db, [], []
    monkeypatch.setattr(journey.outreach, "_brief", lambda vid: {"idea": {"audience": "Independent bakery owners", "problem": "Bakery phone orders", "confirmed": True}})
    _saved_context(d, "Independent bakery owners", "Bakery phone orders")
    from tests.unit.gtm.test_journey import _mail

    _mail(d)
    assert not journey.status(d["wid"], d["vid"])["discovery_started"]
    assert not journey.prepare(d["wid"], d["vid"], authorize=False)["discovery_started"]
    view = journey.prepare(d["wid"], d["vid"])
    real_prepare = journey.prepare
    def prepare(wid, vid, **kwargs):
        prepared.append((wid, vid, kwargs))
        return real_prepare(wid, vid, **kwargs)
    monkeypatch.setattr(journey, "prepare", prepare)
    async def poll(vid):
        polled.append(vid)
    monkeypatch.setattr(journey.outreach, "poll_replies", poll)
    monkeypatch.setattr(journey.outreach, "schedule_followups", lambda vid: None)
    assert asyncio.run(journey.maintain_all())["prepared"] == [d["vid"]]
    assert prepared == [(d["wid"], d["vid"], {"authorize": False})]
    assert polled == [d["vid"]]
    with d["factory"]() as session:
        assert [job.id for job in session.scalars(select(Job))] == [view["job_id"]]


@pytest.mark.parametrize("audience,consumer", [("Independent dental clinic owners", False), ("Bangalore parents", True)])
def test_one_explicit_start_confirms_recorded_audience_and_opt_in(journey_db, monkeypatch, audience, consumer):
    from app.db.models import Job, VentureDocument
    from app.gtm import journey
    d = journey_db
    monkeypatch.setattr(journey.outreach, "_brief", lambda vid: {"idea": {
        "audience": audience, "problem": "Missed appointments", "confirmed": True}})
    _saved_context(d, audience, "Missed appointments")
    assert not journey.status(d["wid"], d["vid"])["discovery_started"]
    assert not journey.prepare(d["wid"], d["vid"], authorize=False)["discovery_started"]
    with d["factory"]() as session:
        assert not session.scalar(select(VentureDocument.id).where(VentureDocument.kind == journey.KIND))
    started = journey.prepare(d["wid"], d["vid"], note="Avoid existing customers", recruitment_opt_in=consumer)
    assert started["state"] == "queued" and started["icp"]["confirmed"] and started["discovery_started"]
    assert started["recruitment_opt_in"] == consumer and started["discovery_note"] == "Avoid existing customers"
    repeated = journey.prepare(d["wid"], d["vid"], recruitment_opt_in=consumer)
    assert repeated["job_id"] == started["job_id"]
    with d["factory"]() as session:
        assert list(session.scalars(select(Job.id))) == [started["job_id"]]


def test_explicit_start_still_requires_saved_problem_context(journey_db, monkeypatch):
    from app.db.models import Job, Venture
    from app.gtm import journey
    d = journey_db
    monkeypatch.setattr(journey.outreach, "_brief", lambda vid: {"idea": {"audience": "Bangalore parents"}})
    with d["factory"]() as session:
        session.get(Venture, d["vid"]).raw_idea = ""
        session.commit()
    view = journey.prepare(d["wid"], d["vid"], recruitment_opt_in=True)
    assert view["state"] == "needs_input" and not view["job_id"] and not view["icp"]["confirmed"]
    with d["factory"]() as session:
        assert not session.scalar(select(Job.id))


def test_confirmed_revision_without_problem_uses_saved_offer(journey_db):
    from app.db.models import Job, VentureDocument
    from app.db.supymem_models import IdeaRevision, IdeaSession
    from app.gtm import journey
    from core.clock import utcnow
    d, now = journey_db, utcnow()
    with d["factory"]() as session:
        session.add(IdeaSession(id="ids_missing_problem", workspace_id=d["wid"], venture_id=d["vid"], case="know", state="confirmed",
            current_revision_id="idv_missing_problem", confirmed_revision_id="idv_missing_problem", confirmed_by_person_id=d["actor"],
            confirmed_at=now, created_at=now, updated_at=now))
        session.flush()
        session.add(IdeaRevision(id="idv_missing_problem", session_id="ids_missing_problem", workspace_id=d["wid"], venture_id=d["vid"], rev=1,
            source="founder", structured=json.dumps({"offer": "Bakery order board"}), created_at=now))
        session.commit()
    view = journey.update_icp(d["wid"], d["vid"], "Independent bakery owners")
    assert view["state"] == "queued" and view["icp"]["problem"] == "Bakery order board" and view["job_id"]
    assert view["icp"]["problem_basis"] == "From your saved description."
    with d["factory"]() as session:
        assert session.scalar(select(VentureDocument.id).where(VentureDocument.kind == journey.KIND))
        assert session.scalar(select(Job.id).where(Job.kind == "gtm.journey.prepare"))


@pytest.fixture()
def brewbox(journey_db, monkeypatch):
    from app.db.models import Venture, VentureDocument
    from app.gtm import journey
    from core.clock import utcnow
    data = journey_db
    monkeypatch.setattr(journey.outreach, "_brief", lambda vid: {})
    with data["factory"]() as session:
        venture = session.get(Venture, data["vid"])
        venture.name = "BrewBox"
        venture.raw_idea = "Let's grow what's working: 140 paying subscribers."
        session.add(VentureDocument(id=f"intake_decision:{data['vid']}", venture_id=data["vid"], kind="intake_decision",
            title="Saved intake", body_md=json.dumps({"path": "gtm", "extracted": {
                "idea": "Home espresso enthusiasts struggle to find fresh coffee subscriptions", "audience": "Home espresso enthusiasts",
                "has_customers": True, "stage": "customers", "existing_site_url": "https://brewbox.example"}}),
            provenance_json="{}", created_at=utcnow(), updated_at=utcnow()))
        session.commit()
    return data


def test_existing_business_without_idea_session_can_start_and_edit_saved_problem(brewbox, monkeypatch):
    from app.api import gtm_god_ops
    from app.db.models import Job
    from app.db.supymem_models import IdeaSession
    from app.gtm import journey
    from tests.unit.gtm.test_journey import _work
    data = brewbox
    _saved_context(data, "Home espresso enthusiasts", "Home espresso enthusiasts struggle to find fresh coffee subscriptions")
    before = journey.status(data["wid"], data["vid"])
    assert before["icp"]["problem"] == "Home espresso enthusiasts struggle to find fresh coffee subscriptions"
    assert before["icp"]["who"] == "Home espresso enthusiasts"
    from app.core.local_mode import is_local
    assert before["icp"]["problem_basis"] == ("From your clarification answer." if is_local() else "From your saved description.")
    assert before["can_prepare"] and not before["discovery_started"] and before["customer_start"]
    assert "BrewBox" in before["customer_start"]["referral_draft"]
    with data["factory"]() as session:
        assert not session.scalar(select(IdeaSession.id))
        assert not session.scalar(select(Job.id))
    monkeypatch.setattr(gtm_god_ops, "_auth_workspace", lambda *args: data["wid"])
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app = FastAPI()
    app.include_router(gtm_god_ops.router)
    client = TestClient(app)
    assert client.get(f"/api/v1/ventures/{data['vid']}/gtm/journey").status_code == 200
    started = journey.prepare(data["wid"], data["vid"], recruitment_opt_in=True)
    assert started["state"] == "queued" and started["customer_start"] and started["discovery_started"]
    captured = []
    async def discover(who, queries, **kwargs):
        captured.append((who, kwargs["problem"]))
        return []
    monkeypatch.setattr(journey, "_discover", discover)
    _work(data, started)
    assert captured == [("Home espresso enthusiasts", before["icp"]["problem"])]
    assert journey.status(data["wid"], data["vid"])["customer_start"]
    edited = client.patch(f"/api/v1/ventures/{data['vid']}/gtm/journey/icp", json={"who": before["icp"]["who"],
        "problem": "  Finding fresh coffee that suits a home espresso machine  ", "recruitment_opt_in": True})
    assert edited.status_code == 200
    assert edited.json()["state"] == "queued"
    assert edited.json()["icp"]["problem"] == "Finding fresh coffee that suits a home espresso machine"
    assert edited.json()["icp"]["problem_basis"] == "From the problem you entered."
    _work(data, edited.json())
    again = journey.update_icp(data["wid"], data["vid"], before["icp"]["who"], search_queries=["espresso community"])
    assert again["icp"]["problem"] == edited.json()["icp"]["problem"]


@pytest.mark.parametrize("intake,raw,brief,expected", [
    ({"idea": "Saved coffee offer", "offer": "Other offer"}, "Raw description", {}, "Saved coffee offer"),
    ({"idea": " ", "offer": "Coffee delivery"}, "Raw description", {}, "Coffee delivery"),
    ({}, "Raw description", {"idea": {"raw_idea": "Brief description"}}, "Raw description"),
    ({}, "", {"idea": {"raw_idea": "Brief description"}}, "Brief description"),
    ({}, "", {"research": {"summary": "Saved research summary"}}, "Saved research summary"),
    ({}, "", {}, ""),
])
def test_problem_fallback_prefers_fullest_description_before_research(brewbox, monkeypatch, intake, raw, brief, expected):
    from app.core.local_mode import is_local
    from app.db.models import Venture, VentureDocument
    from app.gtm import journey
    data = brewbox
    monkeypatch.setattr(journey.outreach, "_brief", lambda vid: brief)
    with data["factory"]() as session:
        session.get(Venture, data["vid"]).raw_idea = raw
        session.get(VentureDocument, f"intake_decision:{data['vid']}").body_md = json.dumps({"path": "gtm", "extracted": intake})
        session.commit()
    if is_local() and not raw and not any(str(intake.get(field) or "").strip() for field in ("idea", "offer")):
        expected = ""
    assert journey._icp(data["vid"])["problem"] == expected


def test_confirmed_problem_takes_precedence_over_intake_description(brewbox):
    from app.db.supymem_models import IdeaRevision, IdeaSession
    from app.gtm import journey
    from core.clock import utcnow
    data, now = brewbox, utcnow()
    with data["factory"]() as session:
        session.add(IdeaSession(id="idea_brewbox", workspace_id=data["wid"], venture_id=data["vid"], case="know", state="confirmed",
            current_revision_id="revision_brewbox", confirmed_revision_id="revision_brewbox", created_at=now, updated_at=now))
        session.flush()
        session.add(IdeaRevision(id="revision_brewbox", session_id="idea_brewbox", workspace_id=data["wid"], venture_id=data["vid"],
            rev=1, source="founder", structured=json.dumps({"audience": "Home espresso enthusiasts", "problem": "Choosing fresh espresso beans"}), created_at=now))
        session.commit()
    view = journey.status(data["wid"], data["vid"])
    assert view["icp"]["problem"] == "Choosing fresh espresso beans"
    assert view["icp"]["problem_basis"] == "From your saved idea."
    assert view["icp"]["problem_revision_id"] == "revision_brewbox"


def test_shared_existing_customers_eligibility_includes_intake_stage(brewbox):
    from app.db.models import Venture, VentureDocument
    from app.gtm import journey
    data = brewbox
    with data["factory"]() as session:
        session.get(Venture, data["vid"]).raw_idea = "Coffee subscriptions"
        session.get(VentureDocument, f"intake_decision:{data['vid']}").body_md = json.dumps({"extracted": {"stage": "customers"}})
        session.commit()
    assert journey._customer_start(data["vid"], "Home espresso enthusiasts")


@pytest.mark.parametrize("problem", ["", "   ", "x" * 2001, 42])
def test_problem_edit_validation_does_not_start_discovery(brewbox, problem):
    from app.gtm import journey
    data = brewbox
    with pytest.raises(ValueError, match="gtm_invalid_input"):
        journey.update_icp(data["wid"], data["vid"], "Home espresso enthusiasts", problem=problem)
    assert not journey.status(data["wid"], data["vid"])["discovery_started"]


def test_missing_audience_does_not_discard_a_founder_supplied_problem(brewbox):
    from app.db.models import VentureDocument
    from app.gtm import journey
    data = brewbox
    with data["factory"]() as session:
        row = journey._row(session, data["vid"], create=True)
        row.provenance_json = json.dumps({"icp": {"who": "", "problem": "Finding fresh beans for home espresso", "problem_basis": "Founder supplied"}})
        session.get(VentureDocument, f"intake_decision:{data['vid']}").body_md = json.dumps({"extracted": {"has_customers": True}})
        session.commit()
    view = journey.status(data["wid"], data["vid"])
    assert view["icp"]["who"] == "" and view["icp"]["needs_input"]
    assert view["icp"]["problem"] == "Finding fresh beans for home espresso"
    assert view["icp"]["problem_basis"] == "From the problem you entered."


@pytest.mark.parametrize("with_problem", [False, True])
def test_postgres_journey_fixture_seeds_revision_without_updates(journey_db, tmp_path, monkeypatch, with_problem):
    from types import SimpleNamespace
    from sqlalchemy import event
    from tests.integration.test_postgres_gtm_journey import GTM_IDEA
    from tests.integration.test_postgres_journey_jobs import journey_db as pg_journey_db
    engine = journey_db["factory"].kw["bind"]
    def append_only(connection, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("UPDATE IDEA_REVISIONS"):
            pytest.fail("Idea revisions are append-only")
    event.listen(engine, "before_cursor_execute", append_only)
    seeded = pg_journey_db.__wrapped__((str(engine.url), engine), tmp_path, monkeypatch,
        SimpleNamespace(param=GTM_IDEA) if with_problem else SimpleNamespace())
    try:
        data = next(seeded)
        from app.db.supymem_models import IdeaRevision, IdeaSession
        with data["factory"]() as session:
            revision, idea = session.get(IdeaRevision, data["rid"]), session.get(IdeaSession, data["sid"])
            assert json.loads(revision.structured) == {"offer": "Bakery order board", "desired_outcome": "First paying bakery"} | (GTM_IDEA if with_problem else {})
            assert idea.state == "confirmed" and idea.current_revision_id == idea.confirmed_revision_id == revision.id
    finally:
        seeded.close()
        event.remove(engine, "before_cursor_execute", append_only)


def test_maintainer_skips_never_started_venture_without_preparing_or_polling(journey_db, monkeypatch):
    from app.gtm import journey
    from app.db.models import Job, VentureDocument
    d = journey_db
    monkeypatch.setattr(journey, "prepare", lambda *args, **kwargs: pytest.fail("Discovery needs founder consent"))
    async def poll(vid):
        pytest.fail("Never-started ventures must not be polled")
    monkeypatch.setattr(journey.outreach, "poll_replies", poll)
    assert not journey.status(d["wid"], d["vid"])["discovery_started"]
    assert asyncio.run(journey.maintain_all()) == {"prepared": []}
    with d["factory"]() as session:
        assert not session.scalar(select(VentureDocument.id).where(VentureDocument.kind == journey.KIND))
        assert not session.scalar(select(Job.id).where(Job.kind == "gtm.journey.prepare"))


@pytest.mark.parametrize("scoped", [True, False])
def test_maintain_tick_prepares_onboarding_and_live_deployment_scopes_only(journey_db, monkeypatch, scoped):
    from app.db.models import Job, Venture, VentureDocument, Workspace
    from app.db.supymem_models import Deployment, FounderApp
    from app.gtm import journey
    from app.tenancy import bind
    from app.workspaces.service import create_workspace
    from core.clock import utcnow

    d, now = journey_db, utcnow()
    published_workspace = create_workspace(kind="company", name="Published", owner_id=d["actor"])
    unrelated_workspace = create_workspace(kind="company", name="Unrelated", owner_id=d["actor"])
    archived_workspace = create_workspace(kind="company", name="Archived", owner_id=d["actor"])
    monkeypatch.setattr(journey.outreach, "_brief", lambda vid: {"idea": {"audience": "Independent bakery owners"}})
    monkeypatch.setattr(journey.outreach, "_send_one", lambda *args: pytest.fail("Maintenance never sends"))
    with d["factory"]() as session:
        session.get(Workspace, d["wid"]).settings_json = json.dumps({"entry_goal": "gtm"})
        archived = session.get(Workspace, archived_workspace.id)
        archived.settings_json, archived.archived_at = json.dumps({"entry_goal": "gtm"}), now
        for vid, wid, archived_at in (("ven_published", published_workspace.id, None),
                                     ("ven_unrelated", unrelated_workspace.id, None),
                                     ("ven_archived", d["wid"], now),
                                     ("ven_archived_workspace", archived_workspace.id, None)):
            session.add(Venture(id=vid, workspace_id=wid, name="Maintenance fixture", raw_idea="Bakery orders",
                stage="draft", archived_at=archived_at, created_at=now, updated_at=now))
        session.flush()
        session.add(FounderApp(id="app_published", workspace_id=published_workspace.id, venture_id="ven_published",
            slug="maintenance-fixture", name="Published page", kind="website", state="live", created_at=now, updated_at=now))
        session.flush()
        session.add(Deployment(id="dep_published", app_id="app_published", workspace_id=published_workspace.id,
            venture_id="ven_published", build_artifact_id="owned-artifact", target="supymem_static", state="live",
            version=1, url="https://owned-product.example", started_at=now, live_at=now))
        session.commit()
    with bind(workspace_id=d["wid"] if scoped else "none", venture_id=d["vid"] if scoped else None, actor=d["actor"]):
        result = asyncio.run(journey.maintain_all())
    expected = {d["vid"]} | (set() if scoped else {"ven_published"})
    assert set(result["prepared"]) == expected
    with d["factory"]() as session:
        assert not session.scalar(select(VentureDocument.id).where(VentureDocument.kind == journey.KIND))
        jobs = list(session.scalars(select(Job).where(Job.kind == "gtm.journey.prepare")))
        assert jobs == []
