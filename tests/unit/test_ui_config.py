from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from tests.company_os_harness import load_company_os_app


@pytest.fixture
def ui(tmp_path, monkeypatch):
    monkeypatch.delenv("SUPYMEM_MODE", raising=False)
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/ui.db")
    monkeypatch.setenv("LLM_OFFLINE", "1")
    monkeypatch.delenv("COMPANY_OS_REQUIRE_SIGNED_CALLER", raising=False)
    main = load_company_os_app()
    from app import ui_config
    from app.api.ui_config_api import router
    from app.credits import policy
    from app.db.models import Base, CreditPolicy

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine, tables=[CreditPolicy.__table__])
    factory = sessionmaker(engine)
    monkeypatch.setattr(policy, "session_factory", lambda: factory)
    policy.invalidate()
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        yield SimpleNamespace(client=client, service=ui_config, policy=policy, factory=factory,
                              model=CreditPolicy, main=main)
    policy.invalidate()
    engine.dispose()


def test_default_chat_and_only_global_policy_applies_to_every_founder(ui, monkeypatch):
    monkeypatch.setenv("FOUNDER_UI_ALLOWLIST", "usr_ui_a, alice@example.test")
    ui.policy.set_policy("founder_ui:usr_ui_a", "classic", "old override")
    for flags, expected in [(None, "chat"), ({}, "chat"), ({"founder_ui": "classic"}, "classic"),
                            ({"founder_ui": "home"}, "home"), ({"founder_ui": "chat"}, "chat"),
                            ({"founder_ui": "invalid"}, "chat"), ({"founder_ui": True}, "chat"), ("classic", "chat")]:
        if flags is not None:
            ui.policy.set_policy("feature_flags", flags, "admin")
        ui.policy.invalidate()
        for subject in ("usr_ui_a", "usr_ui_b", "alice@example.test", "new_founder"):
            response = ui.client.get("/ui-config", headers={"X-Subject": subject})
            assert response.status_code == 200
            assert response.json() == {"founder_ui": expected}
            assert response.headers["cache-control"] == "no-store"
    assert ui.policy.get_policy("founder_ui:usr_ui_a") == "classic"
    for subject in (None, "", " ", "anonymous", "system"):
        assert ui.client.get("/ui-config", headers={"X-Subject": subject} if subject is not None else {}).status_code == 401


def test_goal_flag_is_strict_boolean_for_unrelated_founders_without_exposing_other_flags(ui):
    subjects = ("usr_equipment_inspections", "usr_family_meals")
    for flags, layout, enabled in [(None, "chat", False), ({}, "chat", False),
                                   ({"goals_v1": False}, "chat", False), ({"goals_v1": True}, "chat", True),
                                   ({"goals_v1": 1}, "chat", False), ({"goals_v1": "true"}, "chat", False),
                                   ("goals_v1", "chat", False),
                                   ({"founder_ui": "classic", "goals_v1": True, "private_flag": True}, "classic", True)]:
        if flags is not None:
            ui.policy.set_policy("feature_flags", flags, "admin")
        ui.policy.invalidate()
        for subject in subjects:
            response = ui.client.get("/ui-config", headers={"X-Subject": subject})
            assert response.status_code == 200
            assert response.json() == ({"founder_ui": layout, "goals_v1": True} if enabled else {"founder_ui": layout})
            assert response.headers["cache-control"] == "no-store"


def test_founder_config_is_read_only_and_cannot_change_global_flags(ui, monkeypatch):
    from app.admin import deps
    from app.admin.router import router
    ui.client.app.include_router(router)
    monkeypatch.setattr(deps, "verify_caller", lambda request, required=True: SimpleNamespace(role="user", email=""))
    monkeypatch.setattr(deps, "audit_record", lambda **kwargs: None)
    flags = {"existing_flag": True, "founder_ui": "classic"}
    ui.policy.set_policy("feature_flags", flags, "admin")
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        response = ui.client.request(method, "/ui-config", headers={"X-Subject": "usr_ui_a"}, json={"founder_ui": "home"})
        assert response.status_code == 405
    assert ui.client.get("/admin/ops/flags").status_code == 403
    assert ui.client.put("/admin/ops/flags", json={"flags": {"founder_ui": "home"}}).status_code == 403
    assert ui.policy.get_policy("feature_flags") == flags
    with ui.factory() as session:
        assert [row.key for row in session.query(ui.model).all()] == ["feature_flags"]


def test_admin_flags_preserve_boolean_contract_and_validate_layout(ui):
    from app.admin.router import FlagsBody, admin_get_flags, admin_put_flags
    context = SimpleNamespace(admin=SimpleNamespace(email="admin@example.test"))
    assert admin_get_flags(context)["flags"]["founder_ui"] == "chat"
    for layout in ("chat", "classic", "home"):
        flags = {"existing_flag": False, "founder_ui": layout}
        assert admin_put_flags(FlagsBody(flags=flags), context) == {"flags": flags}
        assert admin_get_flags(context) == {"flags": flags}
        ui.policy.invalidate()
        for subject in ("usr_ui_a", "usr_ui_b"):
            assert ui.client.get("/ui-config", headers={"X-Subject": subject}).json() == {"founder_ui": layout}
    for invalid in ({"existing_flag": "home"}, {"existing_flag": "chat"}, {"founder_ui": True}, {"founder_ui": "invalid"}):
        with pytest.raises(ValueError):
            FlagsBody(flags=invalid)


def test_new_charlie_destinations_resolve_to_real_pages(ui):
    from app.chat.intent import COMPANY_SCREEN_PATHS, requested_venture_screen
    for text, path in [("open all ventures", "/"), ("open new venture", "/new"), ("open company connections", "/connections")]:
        assert COMPANY_SCREEN_PATHS[requested_venture_screen(text)] == path


def test_production_middleware_uses_verified_identity_not_spoofed_subject(ui, monkeypatch):
    from app.admin import authn
    from app.api import ui_config_api
    app = ui.main.create_app()
    subjects = []
    def read_config(subject):
        subjects.append(subject)
        return ui.service.ui_config(subject)
    monkeypatch.setattr(ui_config_api, "ui_config", read_config)
    monkeypatch.setattr(authn, "require_signed_caller", lambda: True)
    monkeypatch.setattr(authn, "verify_caller", lambda request: SimpleNamespace(sub="usr_ui_a", role="user", email=""))
    client = TestClient(app)
    response = client.get("/ui-config", headers={"X-Subject": "usr_ui_b"})
    assert response.status_code == 200, response.text
    assert response.json() == {"founder_ui": "chat"}
    assert subjects == ["usr_ui_a"]
    monkeypatch.setattr(authn, "verify_caller", lambda request: SimpleNamespace(sub="", role="user", email=""))
    assert client.get("/ui-config", headers={"X-Subject": "usr_ui_b"}).status_code == 401
