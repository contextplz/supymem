"""Role focus stays venture-scoped across business and consumer products."""
import json

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from tests.unit.tasks.test_preferences import owner, preferences as preferences


@pytest.mark.parametrize("venture_id,other_id", [
    ("ven_preferences", "ven_consumer"), ("ven_consumer", "ven_preferences"),
])
def test_presets_change_only_focus_and_suggestions_for_the_selected_venture(preferences, venture_id, other_id):
    factory, api = preferences
    from app.authz.roles import ROLES
    from app.db.models import Person, Venture, Workspace
    from app.tasks.role_presets import read_role_preset
    from app.tenancy import bind
    safety = {"spending_limit": 12, "sending_approval_required": True, "unrelated": {"keep": "saved"}}
    with factory() as session:
        workspace = session.get(Workspace, "wsp_preferences")
        workspace.settings_json = json.dumps(safety)
        before_workspace = (workspace.owner_id, workspace.autonomy_level, workspace.observe_level)
        venture = session.get(Venture, venture_id)
        before_venture = (venture.raw_idea, venture.autonomy_level, venture.daily_token_budget, venture.active_charter_id)
        session.commit()
    before_roles = ROLES.copy()
    with bind(workspace_id="wsp_preferences", venture_id=venture_id, actor="usr_preferences"):
        assert api.get_role_preferences(venture_id)["role_preset"] == "chief_of_staff"
        seen = set()
        for preset in ("sales", "product", "chief_of_staff"):
            result = api.set_role_preferences(venture_id, api.RolePresetBody(role_preset=preset))
            assert result["role_preset"] == preset
            assert set(result) == {"role_preset", "label", "focus", "suggestions"}
            seen.add((result["focus"], result["suggestions"]))
            with factory() as session:
                assert read_role_preset(session, venture_id) == result
        assert len(seen) == 3
        result["focus"] = "Caller mutation"
        assert api.get_role_preferences(venture_id)["focus"] != "Caller mutation"
        api.set_role_preferences(venture_id, api.RolePresetBody(role_preset="sales"))
    with bind(workspace_id="wsp_preferences", venture_id=other_id, actor="usr_preferences"):
        assert api.get_role_preferences(other_id)["role_preset"] == "chief_of_staff"
        api.set_role_preferences(other_id, api.RolePresetBody(role_preset="product"))
    with factory() as session:
        workspace = session.get(Workspace, "wsp_preferences")
        assert json.loads(workspace.settings_json) == {**safety, "charlie_role_presets": {venture_id: "sales", other_id: "product"}}
        assert (workspace.owner_id, workspace.autonomy_level, workspace.observe_level) == before_workspace
        venture = session.get(Venture, venture_id)
        assert (venture.raw_idea, venture.autonomy_level, venture.daily_token_budget, venture.active_charter_id) == before_venture
        assert session.get(Person, "usr_preferences").timezone is None
    assert ROLES == before_roles
    with owner():
        assert api.set_preferences("ven_preferences", api.PreferencesBody(timezone="Europe/London"))["confirmed"]
        assert api.get_role_preferences("ven_preferences")["role_preset"] == ("sales" if venture_id == "ven_preferences" else "product")


@pytest.mark.parametrize("venture_id", ["ven_preferences", "ven_consumer"])
def test_role_endpoint_rejects_invalid_presets_and_permission_fields(preferences, venture_id):
    _, api = preferences
    from app.tenancy import bind
    application = FastAPI()
    application.include_router(api.router)
    with bind(workspace_id="wsp_preferences", venture_id=venture_id, actor="usr_preferences"), TestClient(application) as client:
        path = f"/ventures/{venture_id}/task-preferences/role-preset"
        for body in ({}, {"role_preset": "owner"}, {"role_preset": None}, {"role_preset": []},
                     {"role_preset": "sales", "autonomy_level": "L4"},
                     {"role_preset": "product", "spending_limit": 100000},
                     {"role_preset": "sales", "send": True}):
            assert client.patch(path, json=body).status_code == 422
        assert client.get(path).json()["role_preset"] == "chief_of_staff"
        assert client.patch(path, json={"role_preset": "product"}).json()["role_preset"] == "product"


@pytest.mark.parametrize("venture_id", ["ven_preferences", "ven_consumer"])
@pytest.mark.parametrize("blocked", ["workspace_archived", "venture_archived", "custody_changed", "blocked"])
def test_role_reader_and_writer_keep_custody_and_archive_checks(preferences, venture_id, blocked):
    factory, api = preferences
    from app.db.models import Venture, Workspace
    from app.tasks.role_presets import read_role_preset
    from app.tenancy import bind
    from core.clock import utcnow
    with factory() as session:
        workspace = session.get(Workspace, "wsp_preferences")
        if blocked == "workspace_archived":
            workspace.archived_at = utcnow()
        elif blocked == "venture_archived":
            session.get(Venture, venture_id).archived_at = utcnow()
        elif blocked == "custody_changed":
            workspace.owner_id = "usr_other"
        else:
            workspace.settings_json = '{"blocked": true}'
        session.commit()
    with bind(workspace_id="wsp_preferences", venture_id=venture_id, actor="usr_preferences"):
        with factory() as session:
            for operation in (lambda: read_role_preset(session, venture_id),
                              lambda: api.set_role_preferences(venture_id, api.RolePresetBody(role_preset="sales"))):
                with pytest.raises(HTTPException) as failure:
                    operation()
                assert failure.value.status_code == (403 if blocked == "custody_changed" else 404)


@pytest.mark.parametrize("venture_id", ["ven_preferences", "ven_consumer"])
@pytest.mark.parametrize("saved", [None, [], "invalid_preset"])
def test_malformed_saved_preferences_are_not_overwritten(preferences, venture_id, saved):
    factory, api = preferences
    from app.db.models import Workspace
    from app.tenancy import bind
    presets = {venture_id: "owner"} if saved == "invalid_preset" else saved
    raw = json.dumps({"charlie_role_presets": presets, "spending_limit": 12})
    with factory() as session:
        session.get(Workspace, "wsp_preferences").settings_json = raw
        session.commit()
    with bind(workspace_id="wsp_preferences", venture_id=venture_id, actor="usr_preferences"):
        with pytest.raises(HTTPException) as failure:
            api.get_role_preferences(venture_id)
        assert failure.value.status_code == 409
        if saved != "invalid_preset":
            with pytest.raises(HTTPException) as failure:
                api.set_role_preferences(venture_id, api.RolePresetBody(role_preset="sales"))
            assert failure.value.status_code == 409
        with factory() as session:
            assert session.get(Workspace, "wsp_preferences").settings_json == raw
