"""Portfolio summaries avoid per-venture projections and preserve pause truth."""
import json
from datetime import timedelta
from time import perf_counter

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from tests.company_os_harness import load_company_os_app


@pytest.fixture
def portfolio(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/list.db")
    load_company_os_app()
    from app import state
    from app.db.models import Base, Venture, Workspace
    from app.ventures import lifecycle
    from core.clock import utcnow

    engine = create_engine(f"sqlite:///{tmp_path}/list.db")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine)
    monkeypatch.setattr(state, "db", lambda: factory)
    monkeypatch.setattr(lifecycle, "db", lambda: factory)
    now = utcnow()
    with factory() as session:
        for index in range(20):
            workspace = f"workspace_{index}"
            session.add(Workspace(id=workspace, kind="venture", name="Portfolio", entry_mode="idea",
                owner_id="founder", settings_json=json.dumps({"venture_pause": {
                    "paused": True, "paused_reason": "hold spend", "paused_at": now.isoformat(),
                }}) if index == 0 else "{}", created_at=now, updated_at=now))
            session.add(Venture(id=f"venture_{index}", workspace_id=workspace, name=f"Portfolio {index}",
                raw_idea="Saved idea", stage="draft", created_at=now, updated_at=now))
        session.add(Venture(id="foreign", workspace_id="foreign_workspace", name="Foreign", raw_idea="Private",
            stage="draft", created_at=now, updated_at=now))
        session.commit()
    lifecycle._pause_file("venture_1", create=True).write_text(json.dumps({"paused": True,
        "paused_reason": "file fallback", "paused_at": now.isoformat()}))
    queries = []

    def record_query(connection, cursor, statement, parameters, context, executemany):
        queries.append(statement)

    event.listen(engine, "before_cursor_execute", record_query)
    yield lifecycle, factory, {f"workspace_{index}" for index in range(20)}, queries
    engine.dispose()


def test_twenty_summaries_use_two_queries_without_secondary_projections(portfolio, monkeypatch):
    lifecycle, factory, scope, queries = portfolio

    def refuse(*args):
        pytest.fail("summary list must not heal names or compute live projections")

    monkeypatch.setattr(lifecycle, "_heal_names", refuse)
    monkeypatch.setattr(lifecycle, "_ser_active", refuse)
    started = perf_counter()
    items = lifecycle.list_active(workspace_ids=scope, summary=True)
    elapsed = perf_counter() - started
    assert len(items) == 20
    assert len(queries) == 2
    assert elapsed < 1
    by_id = {item["id"]: item for item in items}
    assert by_id["venture_0"]["paused_reason"] == "hold spend"
    assert by_id["venture_1"]["paused_reason"] == "file fallback"
    assert by_id["venture_2"]["paused"] is False
    assert all("agents" not in item and "status" not in item for item in items)
    print(f"20 venture summaries: {elapsed:.4f}s, {len(queries)} queries")
    queries.clear()
    assert lifecycle.list_active(workspace_ids=set(), summary=True) == []
    assert not queries


def test_deleted_summaries_keep_retention_and_workspace_boundaries(portfolio):
    lifecycle, factory, scope, queries = portfolio
    from app.db.models import Venture
    from core.clock import utcnow

    with factory() as session:
        for venture in session.query(Venture):
            venture.archived_at = utcnow() - timedelta(days=8 if venture.id == "venture_2" else 1)
        session.commit()
    queries.clear()
    items = lifecycle.list_deleted(workspace_ids=scope, summary=True)
    assert len(items) == 19
    assert len(queries) == 2
    assert all(item["recoverable"] and item["days_left"] >= 0 and item["purge_after"] for item in items)
    assert all(item["id"] not in {"foreign", "venture_2"} for item in items)


def test_profile_full_projection_against_twenty_summaries(portfolio, monkeypatch):
    lifecycle, factory, scope, queries = portfolio
    from app import state
    from app.onboarding import state as onboarding_state
    from app.org import heartbeat, summary

    monkeypatch.setattr(state, "MEM", {})
    for module in (onboarding_state, heartbeat, summary):
        monkeypatch.setattr(module, "db", lambda: factory)
    started = perf_counter()
    full = lifecycle.list_active(workspace_ids=scope)
    full_elapsed, full_queries = perf_counter() - started, len(queries)
    assert len(full) == 20
    queries.clear()
    started = perf_counter()
    light = lifecycle.list_active(workspace_ids=scope, summary=True)
    light_elapsed = perf_counter() - started
    assert len(light) == 20 and len(queries) == 2
    assert full_queries >= 20 * 7
    assert light_elapsed < 1
    assert {item["id"]: item["paused"] for item in light} == {item["id"]: item["paused"] for item in full}
    print(f"20 ventures: full={full_queries} queries/{full_elapsed:.4f}s; summary={len(queries)} queries/{light_elapsed:.4f}s")


def test_existing_list_consumers_still_receive_full_projection(portfolio, monkeypatch):
    lifecycle, factory, scope, queries = portfolio
    monkeypatch.setattr(lifecycle, "_heal_names", lambda *args: False)
    monkeypatch.setattr(lifecycle, "_ser_active", lambda row, settings=None: {"id": row.id, "agents": {"hired": 3}, "status": {"code": "setup"}})
    items = lifecycle.list_active(workspace_ids=scope)
    assert len(items) == 20
    assert all(item["agents"]["hired"] == 3 and item["status"]["code"] == "setup" for item in items)


def test_list_routes_pass_opt_in_summary_and_authorized_workspaces(monkeypatch):
    load_company_os_app()
    from app.api import ventures

    monkeypatch.setattr(ventures, "accessible_workspace_ids", lambda subject: {subject})
    for route, method in [(ventures.list_ventures, "list_active"), (ventures.list_deleted_ventures, "list_deleted")]:
        monkeypatch.setattr(ventures.lifecycle, method, lambda **args: [args])
        assert route(x_subject="owner", summary=True)["items"] == [{"workspace_ids": {"owner"}, "summary": True}]
        assert route(x_subject="owner")["items"] == [{"workspace_ids": {"owner"}, "summary": False}]


@pytest.mark.parametrize("summary", [True, False])
def test_cards_use_one_workspace_settings_read_for_the_whole_list(portfolio, monkeypatch, summary):
    lifecycle, factory, scope, queries = portfolio
    from app.db.models import Workspace
    from app.workspaces import service

    with factory() as session:
        session.get(Workspace, "workspace_0").settings_json = json.dumps({
            "card_type": "hardware", "card_color": "green", "card_art": "8",
        })
        session.get(Workspace, "workspace_1").settings_json = json.dumps({
            "card_type": [], "card_color": {}, "card_art": "../../private",
        })
        session.commit()
    monkeypatch.setattr(lifecycle, "_heal_names", lambda *args: False)
    monkeypatch.setattr(lifecycle, "_ser_active_scoped", lambda row: lifecycle._venture_fields(row))
    monkeypatch.setattr(service, "get_workspace", lambda *args: pytest.fail("card list must not fetch individual workspaces"))
    queries.clear()
    items = {item["id"]: item for item in lifecycle.list_active(workspace_ids=scope, summary=summary)}
    assert len(queries) == 2
    assert len(items) == 20
    assert "foreign" not in items
    assert {key: items["venture_0"][key] for key in ("card_type", "card_color", "card_art")} == {
        "card_type": "hardware", "card_color": "green", "card_art": "8",
    }
    assert all(items["venture_1"][key] is None for key in ("card_type", "card_color", "card_art"))
