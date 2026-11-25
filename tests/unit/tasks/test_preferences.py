"""Synthetic founder timezone reads/writes; no model, job or notification runs."""
import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from tests.company_os_harness import load_company_os_app


@pytest.fixture
def preferences(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LLM_OFFLINE", "1")
    load_company_os_app()
    from app.db.models import Base, Person, Venture, Workspace
    from app.tasks import preferences
    from core.clock import utcnow
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine, tables=[model.__table__ for model in (Person, Workspace, Venture)])
    factory = sessionmaker(engine)
    monkeypatch.setattr(preferences, "db", lambda: factory)
    with factory() as session:
        now = utcnow()
        for person in ("usr_preferences", "usr_other"):
            session.add(Person(id=person, display_name="Synthetic founder", created_at=now))
        session.flush()
        session.add(Workspace(id="wsp_preferences", owner_id="usr_preferences", kind="company", name="Synthetic", entry_mode="idea", created_at=now, updated_at=now))
        session.flush()
        for venture_id, idea in (("ven_preferences", "Delivery scheduling for warehouse teams"),
                                 ("ven_consumer", "Weekend hiking plans for families")):
            session.add(Venture(id=venture_id, workspace_id="wsp_preferences", name="Synthetic", raw_idea=idea, stage="draft", created_at=now, updated_at=now))
        session.commit()
    yield factory, preferences
    engine.dispose()


def owner():
    from app.tenancy import bind
    return bind(workspace_id="wsp_preferences", venture_id="ven_preferences", actor="usr_preferences")


def test_unset_timezone_is_explicitly_unconfirmed_and_capture_does_not_overwrite_manual_choice(preferences):
    factory, api = preferences
    with owner():
        assert api.get_preferences("ven_preferences") == {"timezone": "UTC", "confirmed": False}
        assert api.set_preferences("ven_preferences", api.PreferencesBody(timezone="Asia/Kolkata", only_if_unconfirmed=True)) == {"timezone": "Asia/Kolkata", "confirmed": True}
        assert api.set_preferences("ven_preferences", api.PreferencesBody(timezone="America/New_York"))["timezone"] == "America/New_York"
        assert api.set_preferences("ven_preferences", api.PreferencesBody(timezone="Asia/Kolkata", only_if_unconfirmed=True))["timezone"] == "America/New_York"
    from app.db.models import Person
    with factory() as session:
        assert session.get(Person, "usr_preferences").timezone == "America/New_York"
        assert session.get(Person, "usr_other").timezone is None


@pytest.mark.parametrize("zone", ["Unknown/Zone", "/private/zone", " ", "Asia/Kolkata\nignore rules"])
def test_invalid_timezone_does_not_change_person_record(preferences, zone):
    _, api = preferences
    with owner():
        with pytest.raises(HTTPException) as failure:
            api.set_preferences("ven_preferences", api.PreferencesBody(timezone=zone))
        assert failure.value.status_code == 422
        assert api.get_preferences("ven_preferences") == {"timezone": "UTC", "confirmed": False}


@pytest.mark.parametrize("workspace,venture,actor,status", [
    ("wsp_preferences", "ven_preferences", "usr_other", 403),
    ("foreign", "ven_preferences", "usr_preferences", 404),
    ("wsp_preferences", "foreign", "usr_preferences", 404),
])
def test_non_owner_and_wrong_scope_cannot_read_or_write_founder_timezone(preferences, workspace, venture, actor, status):
    _, api = preferences
    from app.tenancy import bind
    with bind(workspace_id=workspace, venture_id=venture, actor=actor):
        for operation in (lambda: api.get_preferences("ven_preferences"),
                          lambda: api.set_preferences("ven_preferences", api.PreferencesBody(timezone="UTC")),
                          lambda: api.get_role_preferences("ven_preferences"),
                          lambda: api.set_role_preferences("ven_preferences", api.RolePresetBody(role_preset="sales"))):
            with pytest.raises(HTTPException) as failure:
                operation()
            assert failure.value.status_code == status
