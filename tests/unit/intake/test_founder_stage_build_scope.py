"""SQLite build-source facts; no provider, QA execution or publication claim."""
from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from tests.company_os_harness import append_idea_revision
from tests.unit.intake.test_founder_stage import stages as stages  # noqa: F401


@pytest.fixture()
def build_facts(stages, monkeypatch):
    from app.authz.workspace import ensure_subject_person
    from app.db.models import Venture
    from app.db.supymem_models import IdeaRevision, IdeaSession
    from app.state import db
    from app.tenancy import bind
    from app.ventures import next_step
    from app.workspaces.service import create_workspace
    from core.clock import utcnow

    actor = ensure_subject_person("usr_build_fact_scope")
    workspace = create_workspace(kind="company", name="Build fact scope", owner_id=actor)
    other = create_workspace(kind="company", name="Other build owner", owner_id=actor)
    now, vid, sid, rid = utcnow(), "ven_build_scope", "ids_build_scope", "idv_build_current"
    with db()() as session:
        session.add_all([
            Venture(id=vid, workspace_id=workspace.id, name="Booking", raw_idea="Booking software", stage="draft", created_at=now, updated_at=now),
            Venture(id="ven_build_other", workspace_id=other.id, name="Other booking", raw_idea="Other product", stage="draft", created_at=now, updated_at=now),
        ])
        session.flush()
        session.add(IdeaSession(id=sid, workspace_id=workspace.id, venture_id=vid, case="know", state="confirmed",
            current_revision_id=rid, confirmed_revision_id=rid, confirmed_at=now, confirmed_by_person_id=actor, created_at=now, updated_at=now))
        session.flush()
        session.add_all([
            IdeaRevision(id=rid, workspace_id=workspace.id, venture_id=vid, session_id=sid, rev=2, source="founder", structured="{}", created_at=now),
            IdeaRevision(id="idv_build_old", workspace_id=workspace.id, venture_id=vid, session_id=sid, rev=1, source="founder", structured="{}", created_at=now),
        ])
        session.commit()
    # Leave the build, app and idea reads real; unrelated projections are off.
    monkeypatch.setattr(next_step, "_coding_tool", lambda _: False)
    monkeypatch.setattr("app.firstlook.service.landing_exists", lambda *_: False)
    monkeypatch.setattr("app.chat.recovery.external_site", lambda _: None)
    monkeypatch.setattr(stages, "get_stage", lambda _: {"stage": "idea", "confirmed_by_founder": False})
    monkeypatch.setattr("app.experiments.ledger.loop_state", lambda _: {"ready": False, "open": []})
    with bind(workspace_id=workspace.id, venture_id=vid, actor=actor):
        yield {"wid": workspace.id, "other_wid": other.id, "vid": vid, "sid": sid, "rid": rid,
               "now": now, "factory": db(), "facts": lambda: next_step._facts(vid)}


def _save_build(d, fault=None):
    from app.db.models import Venture, Workspace
    from app.db.supymem_models import BuildRun, FounderApp, IdeaSession

    app_wid = d["other_wid"] if fault == "app_workspace" else d["wid"]
    app_vid = "ven_build_other" if fault == "app_venture" else d["vid"]
    inputs = {"product_context": {"revision_id": "idv_build_old" if fault == "historical_revision" else d["rid"]}}
    if fault == "missing_provenance":
        inputs = {}
    elif fault == "correction_lineage_is_not_idea":
        inputs = {"revision": {"id": d["rid"], "revision_id": d["rid"]}}
    elif fault == "idea_alias_is_not_provenance":
        inputs = {"idea": {"id": d["rid"]}}
    spec = {"artifact_profile": "static_landing_v1", "inputs": inputs,
            "qa_state": "failed" if fault == "qa_failed" else "passed",
            "lifecycle": {"review": {"status": "pending" if fault == "review_pending" else "accepted"}}}
    with d["factory"]() as session:
        idea = session.get(IdeaSession, d["sid"])
        if fault == "unconfirmed_idea":
            idea.state, idea.confirmed_revision_id, idea.confirmed_at = "reviewing", None, None
        elif fault == "current_revision_changed":
            idea.current_revision_id = "idv_build_old"
        elif fault == "idea_workspace":
            idea.workspace_id = d["other_wid"]
        revision_changes = {}
        if fault == "revision_workspace":
            revision_changes["workspace_id"] = d["other_wid"]
        elif fault == "revision_venture":
            revision_changes["venture_id"] = "ven_build_other"
        elif fault == "revision_session":
            session.add(IdeaSession(id="ids_build_other", workspace_id=d["other_wid"], venture_id="ven_build_other",
                case="know", state="clarifying", created_at=d["now"], updated_at=d["now"]))
            session.flush()
            revision_changes["session_id"] = "ids_build_other"
        if revision_changes:
            d["rid"] = append_idea_revision(session, d["rid"], **revision_changes).id
            inputs["product_context"]["revision_id"] = d["rid"]
        if fault == "venture_archived":
            session.get(Venture, d["vid"]).archived_at = d["now"]
        elif fault == "stage_archived":
            session.get(Venture, d["vid"]).stage = "archived"
        elif fault == "workspace_archived":
            session.get(Workspace, d["wid"]).archived_at = d["now"]
        session.add(FounderApp(id="app_build_scope", workspace_id=app_wid, venture_id=app_vid, slug="build-source-fixture",
            name="Private landing", kind="static", deleted_at=d["now"] if fault == "deleted_app" else None,
            created_at=d["now"], updated_at=d["now"]))
        session.flush()
        session.add(BuildRun(id="bld_build_scope", app_id="app_build_scope",
            workspace_id=d["other_wid"] if fault == "run_workspace" else d["wid"], venture_id=d["vid"],
            runtime_version=0 if fault == "legacy_runtime" else 1, trigger="founder", agent="builder",
            state="succeeded", closed_at=None if fault == "open_run" else d["now"],
            created_at=d["now"], spec=json.dumps(spec)))
        session.commit()


@pytest.mark.parametrize("requeued", [False, True])
def test_active_page_job_overrides_failed_history(build_facts, requeued):
    from app.db.models import Job
    from app.ventures import next_step
    from datetime import timedelta

    data = build_facts
    with data["factory"]() as session:
        session.add(Job(id="page_old", workspace_id=data["wid"], venture_id=data["vid"],
            kind="pagebuilder.build", status="scheduled" if requeued else "failed_dead",
            created_at=data["now"], scheduled_for=data["now"] + timedelta(seconds=20),
            payload_json=json.dumps({"revision_id": data["rid"]})))
        session.add(Job(id="page_new", workspace_id=data["wid"], venture_id=data["vid"],
            kind="pagebuilder.build", status="failed_dead" if requeued else "scheduled",
            created_at=data["now"] + timedelta(seconds=10),
            payload_json=json.dumps({"revision_id": data["rid"]})))
        session.commit()
    facts = data["facts"]()
    assert facts["page_build"] == "running"
    facts["landing_build"] = {"state": "failed", "page": {"available": False}}
    assert next_step._founder_resolve(data["vid"], f"/v/{data['vid']}", facts)["key"] == "site.building"


def test_current_accepted_landing_has_exact_revision_and_remains_private(build_facts):
    from app.db.supymem_models import Deployment

    d = build_facts
    _save_build(d)
    facts = d["facts"]()
    assert facts["idea_confirmed"] and facts["accepted_current_landing"]
    assert facts["accepted_landing"] and facts["accepted_build"]
    assert not facts["build_review_pending"]
    with d["factory"]() as session:
        assert not session.scalar(select(Deployment.id))


@pytest.mark.parametrize("state", ["queued", "running", "syncing_artifacts"])
@pytest.mark.parametrize("idea", ["Equipment inspections for warehouse teams", "Shared garden plans for families"])
def test_active_landing_run_marks_page_work_without_a_pagebuilder_job(build_facts, state, idea):
    from app.db.models import Venture
    from app.db.supymem_models import BuildRun
    from app.ventures.next_step import founder_next_step
    d = build_facts
    _save_build(d)
    with d["factory"]() as session:
        session.get(Venture, d["vid"]).raw_idea = idea
        run = session.get(BuildRun, "bld_build_scope")
        run.state, run.closed_at = state, None
        session.commit()
    assert d["facts"]()["page_build"] == "running"
    assert founder_next_step(d["vid"])["key"] == "site.building"


@pytest.mark.parametrize("fault", ["app_workspace", "app_venture", "deleted_app", "historical_revision", "current_revision_changed"])
def test_unrelated_active_landing_cannot_replace_current_next_step(build_facts, fault):
    from app.db.supymem_models import BuildRun
    d = build_facts
    _save_build(d, fault)
    with d["factory"]() as session:
        run = session.get(BuildRun, "bld_build_scope")
        run.state, run.closed_at = "running", None
        session.commit()
    assert d["facts"]()["page_build"] != "running"


def test_dossier_reads_use_saved_customer_signal_and_clean_product_summary(build_facts):
    from app.db.models import VentureDocument
    from app.db.supymem_models import ResearchDossier, ResearchFinding
    from app.research.service import get_dossier, export_markdown
    d = build_facts
    raw = '[Skip to main content](https://coffee.example/#main) [Coffee subscriptions](https://coffee.example/coffee). Freshly roasted beans.'
    with d["factory"]() as session:
        session.add(VentureDocument(id=f'intake_decision:{d["vid"]}', venture_id=d["vid"], kind="intake_decision", title="Intake",
            body_md=json.dumps({"extracted": {"has_customers": True}}), created_at=d["now"], updated_at=d["now"]))
        session.add(ResearchDossier(id="dossier_fixture", workspace_id=d["wid"], venture_id=d["vid"], revision_id=d["rid"],
            kind="deep", state="partial", created_at=d["now"], summary=json.dumps({"prior_art": {}})))
        session.flush()
        session.add(ResearchFinding(id="finding_fixture", dossier_id="dossier_fixture", workspace_id=d["wid"], venture_id=d["vid"],
            category="source", title="Coffee subscriptions", url="https://coffee.example", source="synthetic", summary=raw,
            evidence=json.dumps({"evidence_kind": "yc_company", "prior_art": {"source_kind": "yc_company"}}), created_at=d["now"]))
        session.commit()
    body = get_dossier("dossier_fixture")
    assert body["has_customers"]
    assert body["summary"]["verdict"]["title"] == "Grow what's already working"
    assert "acquisition channel" in body["summary"]["verdict"]["why_not"][0]
    built = body["summary"]["prior_art"]["companies"][0]["what_they_built"]
    assert "](" not in built["quote"] and built["quote"].endswith("Coffee subscriptions. Freshly roasted beans.")
    assert built["description"] == "Coffee subscriptions. Freshly roasted beans."
    assert "Coffee subscriptions. Freshly roasted beans." in export_markdown(d["vid"], "dossier_fixture")["markdown"]


def test_active_research_receipt_precedes_an_older_finished_dossier(build_facts):
    from app.db.models import Job
    from app.db.supymem_models import ResearchDossier
    from app.ventures.next_step import founder_next_step
    d = build_facts
    with d["factory"]() as session:
        session.add(ResearchDossier(id="finished_fixture", workspace_id=d["wid"], venture_id=d["vid"], revision_id=d["rid"],
            kind="deep", state="complete", created_at=d["now"], summary="{}"))
        session.add(Job(id="job_research_fixture", workspace_id=d["wid"], venture_id=d["vid"], kind="research.deep",
            status="pending", payload_json="{}", created_at=d["now"]))
        session.commit()
    facts = d["facts"]()
    assert facts["dossier_state"] == "complete" and facts["active_research"]
    assert founder_next_step(d["vid"])["key"] == "research.running"


@pytest.mark.parametrize("fault", [
    "historical_revision", "missing_provenance", "correction_lineage_is_not_idea", "idea_alias_is_not_provenance",
    "unconfirmed_idea", "current_revision_changed", "run_workspace", "app_workspace", "app_venture", "deleted_app",
    "qa_failed", "review_pending", "open_run", "legacy_runtime",
    "revision_workspace", "revision_venture", "revision_session", "idea_workspace",
    "venture_archived", "stage_archived", "workspace_archived",
])
def test_unqualified_build_history_cannot_prove_current_landing(build_facts, fault):
    d = build_facts
    _save_build(d, fault)
    facts = d["facts"]()
    assert not facts["accepted_current_landing"], fault
    if fault in {"historical_revision", "missing_provenance", "correction_lineage_is_not_idea", "idea_alias_is_not_provenance"}:
        # Historical acceptance remains available to its existing consumers;
        # it does not prove the new, current-direction publication priority.
        assert facts["accepted_landing"] and facts["accepted_build"]


def _synthetic_world_build(d, monkeypatch):
    """Real SQLite idea custody, synthetic build callback; no QA or BuildRun."""
    import asyncio
    import time
    from tests.integration.godmode_world import FakeClock, World

    world = World(vid=d["vid"], wid=d["wid"], owner="usr_build_fact_scope", clock=FakeClock(d["now"]), mp=monkeypatch)
    monotonic = time.monotonic
    world.install()  # The real fact adapter re-reads the persisted idea rows.
    monkeypatch.setattr(time, "monotonic", monotonic)
    queued = []
    def finish_synthetic_build(kind, done, **_):
        assert kind == "build.run"
        queued.append(d["facts"]()["accepted_current_landing"])
        done()  # Synthetic state transition only, never a worker or QA receipt.
    monkeypatch.setattr(world, "enqueue_job", finish_synthetic_build)
    asyncio.run(world.execute(d["vid"], "build.start", actor_subject=world.owner))
    assert queued == [False]
    assert world.state["builds"][0]["state"] == "review"
    assert d["facts"]()["accepted_current_landing"] is False
    return world


def _assert_no_actual_build_or_publication(d):
    from app.db.supymem_models import BuildRun, Deployment, QaRun
    with d["factory"]() as session:
        for model in (BuildRun, QaRun, Deployment):
            assert not session.scalar(select(model.id))


def test_synthetic_world_current_flag_waits_for_explicit_founder_review(build_facts, monkeypatch):
    d = build_facts
    world = _synthetic_world_build(d, monkeypatch)
    captured = world.state["builds"][0]
    assert captured["revision_id"] == d["rid"]
    assert (captured["workspace_id"], captured["venture_id"], captured["artifact_profile"]) == (d["wid"], d["vid"], "static_landing_v1")
    world.founder_review("accept")
    assert captured["origin"] == "founder" and d["facts"]()["accepted_current_landing"]
    assert not world.state["site_live"] and world.review_called_by_godmode == 0
    _assert_no_actual_build_or_publication(d)


@pytest.mark.parametrize("fault", ["current_revision_changed", "unconfirmed_idea", "foreign_revision_workspace",
                                    "moved_venture", "archived_venture", "archived_stage", "archived_workspace"])
def test_synthetic_world_current_flag_rechecks_real_idea_custody(build_facts, monkeypatch, fault):
    from app.db.models import Venture, Workspace
    from app.db.supymem_models import IdeaSession

    d = build_facts
    world = _synthetic_world_build(d, monkeypatch)
    world.founder_review("accept")
    assert d["facts"]()["accepted_current_landing"]
    with d["factory"]() as session:
        idea = session.get(IdeaSession, d["sid"])
        if fault == "current_revision_changed":
            idea.current_revision_id = idea.confirmed_revision_id = "idv_build_old"
        elif fault == "unconfirmed_idea":
            idea.state = "reviewing"
        elif fault == "foreign_revision_workspace":
            d["rid"] = append_idea_revision(session, d["rid"], workspace_id=d["other_wid"]).id
        elif fault == "moved_venture":
            session.get(Venture, d["vid"]).workspace_id = d["other_wid"]
        elif fault == "archived_venture":
            session.get(Venture, d["vid"]).archived_at = d["now"]
        elif fault == "archived_stage":
            session.get(Venture, d["vid"]).stage = "archived"
        elif fault == "archived_workspace":
            session.get(Workspace, d["wid"]).archived_at = d["now"]
        session.commit()
    assert d["facts"]()["accepted_current_landing"] is False
    # Historical acceptance stays visible only in the same captured scope.
    assert d["facts"]()["accepted_landing"] is (fault != "moved_venture")
    _assert_no_actual_build_or_publication(d)


def test_synthetic_world_missing_or_foreign_capture_cannot_prove_current_landing(build_facts, monkeypatch):
    d = build_facts
    world = _synthetic_world_build(d, monkeypatch)
    world.founder_review("accept")
    captured = world.state["builds"][0]
    baseline = dict(captured)
    for field, value in (("revision_id", None), ("workspace_id", d["other_wid"]),
                         ("venture_id", "ven_build_other"), ("artifact_profile", "founder_product_v1")):
        captured.clear()
        captured.update(baseline | {field: value})
        assert d["facts"]()["accepted_current_landing"] is False, field
        assert d["facts"]()["accepted_landing"] is (field == "revision_id"), field
    _assert_no_actual_build_or_publication(d)


def _synthetic_world_before_confirm(d, monkeypatch):
    import time
    from tests.integration.godmode_world import FakeClock, World

    world = World(vid=d["vid"], wid=d["wid"], owner="usr_build_fact_scope", clock=FakeClock(d["now"]), mp=monkeypatch)
    monotonic = time.monotonic
    world.install()
    monkeypatch.setattr(time, "monotonic", monotonic)
    monkeypatch.setattr(world, "enqueue_job", lambda kind, done, **_: done())
    return world


def test_synthetic_world_confirm_persists_owned_revision_before_build_capture(build_facts, monkeypatch):
    import asyncio
    from app.db.supymem_models import IdeaSession

    d = build_facts
    with d["factory"]() as session:
        idea = session.get(IdeaSession, d["sid"])
        idea.state, idea.confirmed_revision_id, idea.confirmed_at = "reviewing", None, None
        session.commit()
    world = _synthetic_world_before_confirm(d, monkeypatch)
    assert world.current_confirmed_revision() is None
    world.idea_confirm(d["vid"], actor_subject=world.owner)
    with d["factory"]() as session:
        idea = session.get(IdeaSession, d["sid"])
        assert idea.state == "confirmed" and idea.confirmed_revision_id == d["rid"] and idea.confirmed_at
    asyncio.run(world.execute(d["vid"], "build.start", actor_subject=world.owner))
    assert world.state["builds"][0]["revision_id"] == d["rid"]
    assert d["facts"]()["accepted_current_landing"] is False
    world.founder_review("accept")
    assert d["facts"]()["accepted_current_landing"]
    _assert_no_actual_build_or_publication(d)


@pytest.mark.parametrize("fault", ["foreign_current_revision", "missing_current_revision"])
def test_synthetic_world_confirm_rejects_changed_source_without_binding(build_facts, monkeypatch, fault):
    from app.db.supymem_models import IdeaRevision, IdeaSession

    d = build_facts
    with d["factory"]() as session:
        idea = session.get(IdeaSession, d["sid"])
        idea.state, idea.confirmed_revision_id, idea.confirmed_at = "reviewing", None, None
        if fault == "foreign_current_revision":
            d["rid"] = append_idea_revision(session, d["rid"], workspace_id=d["other_wid"]).id
        else:
            idea.current_revision_id = "idv_missing"
        session.commit()
    world = _synthetic_world_before_confirm(d, monkeypatch)
    with pytest.raises(RuntimeError, match="fixture_idea_source_changed"):
        world.idea_confirm(d["vid"], actor_subject=world.owner)
    with d["factory"]() as session:
        idea = session.get(IdeaSession, d["sid"])
        assert idea.state == "reviewing" and idea.confirmed_revision_id is None and idea.confirmed_at is None
    assert world.current_confirmed_revision() is None
    _assert_no_actual_build_or_publication(d)


def test_synthetic_world_foreign_build_argument_cannot_capture_own_revision(build_facts, monkeypatch):
    import asyncio

    d = build_facts
    world = _synthetic_world_before_confirm(d, monkeypatch)
    assert world.current_confirmed_revision() == d["rid"]
    asyncio.run(world.execute("ven_build_other", "build.start", actor_subject=world.owner))
    captured = world.state["builds"][0]
    assert captured["revision_id"] is None and captured["venture_id"] == "ven_build_other"
    world.founder_review("accept")
    assert d["facts"]()["accepted_current_landing"] is False
    assert d["facts"]()["accepted_landing"] is False
    from app.ventures import next_step
    foreign = next_step._facts("ven_build_other")
    assert foreign["accepted_current_landing"] is False and foreign["accepted_landing"] is False
    _assert_no_actual_build_or_publication(d)
