import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from tests.company_os_harness import load_company_os_app


@pytest.fixture
def home(tmp_path, monkeypatch):
    load_company_os_app()
    from app import state
    from app.api.company_home_api import router
    from app.authz import workspace
    from app.db.models import GtmContact, GtmTouch, HumanTask, Job, Person, PlannedTask, Venture, VentureDocument, Workspace, WorkspaceMember
    from app.db.supymem_models import BuildRun, ExecutionPlan, FounderApp, ResearchDossier
    from app.db.task_models import Task
    from app.ventures import company_home
    from core.clock import utcnow

    engine = create_engine(f"sqlite:///{tmp_path}/home.db")
    models = [Person, Workspace, WorkspaceMember, Venture, VentureDocument, ResearchDossier, ExecutionPlan, FounderApp, BuildRun, PlannedTask, Task, HumanTask, GtmContact, GtmTouch, Job]
    for model in models:
        model.__table__.create(engine)
    factory = sessionmaker(engine)
    for module in (state, workspace, company_home):
        monkeypatch.setattr(module, "db", lambda: factory)
    now = utcnow()
    with factory() as session:
        for owner, workspace_id, venture_id in [("usr_home", "wsp_home", "ven_home"), ("usr_other", "wsp_other", "ven_other")]:
            session.add(Person(id=owner, display_name=owner, created_at=now))
            session.add(Workspace(id=workspace_id, kind="company", name=workspace_id, entry_mode="idea", owner_id=owner, created_at=now, updated_at=now))
            session.add(Venture(id=venture_id, workspace_id=workspace_id, name=venture_id, raw_idea="Saved idea", stage="draft", created_at=now, updated_at=now))
        session.commit()
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        yield SimpleNamespace(client=client, factory=factory, now=now, document=VentureDocument, models=models)
    engine.dispose()


def request(home, method, path, subject="usr_home", **kwargs):
    return home.client.request(method, f"/ventures/ven_home/{path}", headers={"X-Subject": subject} if subject else {}, **kwargs)


def test_pins_persist_idempotently_and_do_not_replace_other_documents(home):
    assert request(home, "GET", "ui-pins").json() == {"venture_id": "ven_home", "pins": []}
    for name in ("Research", "Product & Engineering", "Research"):
        result = request(home, "POST", "ui-pins", json={"department": name, "pinned": True})
        assert result.status_code == 200
    assert result.json()["pins"] == ["Research", "Product & Engineering"]
    for _ in range(2):
        assert request(home, "POST", "ui-pins", json={"department": "Research", "pinned": False}).json()["pins"] == ["Product & Engineering"]
    assert request(home, "GET", "ui-pins").headers["cache-control"] == "private, no-store"
    with home.factory() as session:
        rows = list(session.scalars(select(home.document)))
        assert len(rows) == 1
        assert rows[0].kind == "company_ui_pins"
        assert json.loads(rows[0].body_md) == ["Product & Engineering"]


@pytest.mark.parametrize("path", ["ui-pins", "ui-archive", "ui-archive/private"])
def test_private_reads_require_identity_and_workspace_ownership(home, path):
    assert request(home, "GET", path, subject=None).status_code == 401
    assert request(home, "GET", path, subject="usr_other").status_code == 404
    assert request(home, "POST", "ui-pins", subject="usr_other", json={"department": "Research", "pinned": True}).status_code == 404


@pytest.mark.parametrize("body", [{"department": "", "pinned": True}, {"department": "bad\nname", "pinned": True}, {"department": "Research", "pinned": "true"}, {"department": "Research", "pinned": True, "venture_id": "ven_other"}])
def test_pin_input_validation(home, body):
    assert request(home, "POST", "ui-pins", json=body).status_code == 422
    assert request(home, "GET", "ui-pins").json()["pins"] == []


def test_corrupt_saved_pins_are_preserved_with_page_list_fallback(home):
    with home.factory() as session:
        session.add(home.document(id="company_ui_pins:ven_home", venture_id="ven_home", kind="company_ui_pins", title="Pins", body_md="corrupt", created_at=home.now, updated_at=home.now))
        session.commit()
    response = request(home, "POST", "ui-pins", json={"department": "Research", "pinned": True})
    assert response.status_code == 409
    assert "page list" in response.json()["detail"]
    with home.factory() as session:
        assert session.get(home.document, "company_ui_pins:ven_home").body_md == "corrupt"


def test_archives_are_real_dated_scoped_records_and_attachment_chunks(home):
    from app.db.models import Job, PlannedTask
    from app.db.supymem_models import BuildRun, FounderApp, ResearchDossier
    with home.factory() as session:
        session.add(ResearchDossier(id="report_real", workspace_id="wsp_home", venture_id="ven_home", kind="market_research", state="done", summary="Saved report", created_at=home.now))
        session.add(FounderApp(id="app_real", workspace_id="wsp_home", venture_id="ven_home", slug="real-home", name="Real product", kind="static", state="draft", created_at=home.now, updated_at=home.now))
        session.add(BuildRun(id="build_real", app_id="app_real", workspace_id="wsp_home", venture_id="ven_home", trigger="founder", agent="builder", state="completed", created_at=home.now))
        session.add(PlannedTask(id="task_real", workspace_id="wsp_home", venture_id="ven_home", title="Interview buyers", why_now="Learn", status="open", created_at=home.now, updated_at=home.now))
        session.add(Job(id="job_real", workspace_id="wsp_home", venture_id="ven_home", kind="research.deep", payload_json='{"secret":"not displayed"}', status="scheduled", created_at=home.now))
        for identity, venture, kind, text, provenance in [
            ("recap_real", "ven_home", "recap", "Saved knowledge", None),
            ("hidden_internal", "ven_home", "journey_overrides", "Never expose pipeline config", None),
            ("other_private", "ven_other", "recap", "Other founder", None),
            ("file_real:1", "ven_home", "founder_evidence", "Second chunk", {"source_kind": "file", "evidence_id": "file_real", "chunk": 1}),
            ("file_real:0", "ven_home", "founder_evidence", "First chunk", {"source_kind": "file", "evidence_id": "file_real", "chunk": 0}),
        ]:
            session.add(home.document(id=identity, venture_id=venture, kind=kind, title="Real saved work", body_md=text, provenance_json=json.dumps(provenance), created_at=home.now, updated_at=home.now))
        session.commit()
    response = request(home, "GET", "ui-archive")
    assert response.status_code == 200, response.text
    archive = response.json()
    assert archive["open_work"] == 2
    items = {item["id"]: item for item in archive["items"]}
    assert set(items) == {"report_real", "app_real", "build_real", "task_real", "job_real", "recap_real", "file_real:0"}
    assert all(item["created_at"] for item in items.values())
    assert items["report_real"]["kind"] == "market_research"
    assert items["build_real"]["agent_role"] == "builder"
    assert items["report_real"]["href"].endswith("research?dossier=report_real")
    assert items["build_real"]["href"].endswith("builds?app=app_real&run=build_real")
    assert request(home, "GET", "ui-archive/file_real:0").json()["text"] == "First chunk\n\nSecond chunk"
    assert request(home, "GET", "ui-archive/recap_real").json()["text"] == "Saved knowledge"
    assert request(home, "GET", "ui-archive/hidden_internal").status_code == 404
    assert request(home, "GET", "ui-archive/other_private").status_code == 404


def test_empty_archive_is_honest(home):
    assert request(home, "GET", "ui-archive").json() == {"venture_id": "ven_home", "open_work": 0, "items": []}


def test_human_tasks_supply_real_integration_filters_and_remain_venture_scoped(home):
    from app.db.models import HumanTask
    with home.factory() as session:
        for venture in ("ven_home", "ven_other"):
            session.add(HumanTask(id=f"human:{venture}", venture_id=venture, title="Connect the saved inbox", why="Approved replies", provider="gmail", category="connection", status="open", priority=1, blocks_json="[]", steps_json="[]", expects_json="[]", created_at=home.now, updated_at=home.now))
        session.commit()
    result = request(home, "GET", "ui-archive").json()
    assert result["open_work"] == 1
    assert [item["id"] for item in result["items"]] == ["human:ven_home"]
    assert result["items"][0]["integration"] == "gmail"
    assert result["items"][0]["href"] == "/v/ven_home/tasks"
