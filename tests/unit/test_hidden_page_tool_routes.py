"""Hidden local page entries must use the owner's existing device build route."""

from types import SimpleNamespace

import pytest

from tests.company_os_harness import load_company_os_app


@pytest.fixture
def route(monkeypatch, tmp_path):
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    load_company_os_app()
    from app.authz import workspace
    from app.build import device_orders
    from app.core import local_capabilities, local_owner
    from app import coding_agents_consent
    from app.firstlook import compose
    from app.idealab import slate
    from app.landing import service
    from desktop_runtime import agent_status

    calls = []
    monkeypatch.setattr(local_owner, "ensure_owner", lambda: SimpleNamespace(id="owner", email=""))
    monkeypatch.setattr(workspace, "assert_venture_access", lambda venture, actor: (venture, "workspace"))
    monkeypatch.setattr(local_capabilities, "enabled", lambda name: True)
    monkeypatch.setattr(coding_agents_consent, "consents", lambda actor: {"codex": True})
    monkeypatch.setattr(agent_status, "statuses", lambda: [{"name": "codex", "installed": True,
        "signed_in": True, "available": True, "subscription_login": True}])

    def prepare(venture, caller, instruction, **kwargs):
        calls.append((venture, caller, instruction))
        return {"id": "dwo_test", "state": "pending", "review_digest": "review"}

    def transition(venture, caller, order, digest, action):
        assert (order, digest, action) == ("dwo_test", "review", "approve")
        return {"id": order, "state": "approved"}

    def forbidden(*args, **kwargs):
        raise AssertionError("Internal page source generation was reached")

    monkeypatch.setattr(device_orders, "prepare_coding_task_from_chat", prepare)
    monkeypatch.setattr(device_orders, "transition", transition)
    monkeypatch.setattr(service, "ensure_site", lambda *args: {"id": "site", "app_id": "app",
        "state": "draft", "current_version_id": None})
    monkeypatch.setattr(service, "_site_dir", forbidden)
    monkeypatch.setattr(compose, "compose_html", forbidden)
    monkeypatch.setattr(slate, "_teaser_html", forbidden)
    return calls


@pytest.mark.parametrize("idea,audience", [
    ("Receiving audit logs", "Warehouse operations managers"),
    ("Weekend pottery lessons", "Families learning ceramics"),
])
@pytest.mark.parametrize("entry", ["landing", "specialist", "teaser"])
def test_local_hidden_page_uses_connected_device_order(route, monkeypatch, idea, audience, entry):
    from app.gtm.god.specialists import run_specialist
    from app.idealab import slate
    from app.landing.service import generate_and_publish

    if entry == "landing":
        result = generate_and_publish("venture", "workspace", idea)
        assert result["state"] != "live"
        assert result["current_version_id"] is None
    elif entry == "specialist":
        result = run_specialist("site", workspace_id="workspace", venture_id="venture",
            plan={"idea": idea}, artifacts={"icp": {"who": audience}})
        assert result["status"] != "done"
        assert "site" not in result.get("artifacts", {})
    else:
        view = SimpleNamespace(id="opportunity", workspace_id="desk", title=idea,
            one_liner=idea, first_customer=audience, wedge="One useful workflow",
            why_now="Repeated requests", kill_criteria="No recurring need", evidence=[],
            promoted_venture_id="venture", to_dict=lambda: {"id": "opportunity", "teaser_path": None})
        monkeypatch.setattr(slate, "get_opportunity", lambda identifier: view)
        result = slate.generate_teaser("opportunity", actor_id="owner")
        assert result["teaser_path"] is None

    assert len(route) == 1
    venture, caller, instruction = route[0]
    assert venture == "venture"
    assert caller.sub == "owner"
    assert "codex" in instruction
    assert idea in instruction
    if entry != "landing":
        assert audience in instruction
    assert "Open Builds" in (result.get("message") or result.get("human_instruction"))


@pytest.mark.parametrize("entry", ["landing", "specialist", "teaser"])
def test_disconnected_hidden_page_names_connect_step(route, monkeypatch, entry):
    from app import coding_agents_consent
    from app.gtm.god.specialists import run_specialist
    from app.idealab import slate
    from app.landing.service import generate_and_publish
    from fastapi import HTTPException

    monkeypatch.setattr(coding_agents_consent, "consents", lambda actor: {})
    monkeypatch.setattr(slate, "get_opportunity", lambda identifier: SimpleNamespace(
        id="opportunity", promoted_venture_id="venture", title="A requested teaser",
        one_liner="A small idea", first_customer="Interested people", wedge="One workflow",
        why_now="Repeated requests", kill_criteria="No recurring need", evidence=[]))
    with pytest.raises(HTTPException) as error:
        if entry == "landing":
            generate_and_publish("venture", "workspace", "A requested page")
        elif entry == "specialist":
            run_specialist("site", workspace_id="workspace", venture_id="venture",
                plan={"idea": "A requested page"}, artifacts={})
        else:
            slate.generate_teaser("opportunity", actor_id="owner")
    assert "Settings > Keys and connections > Coding tools" in error.value.detail["message"]
    assert route == []


@pytest.mark.parametrize("missing", ["installed", "signed_in", "available", "subscription_login"])
def test_page_build_requires_real_subscription_status(route, monkeypatch, missing):
    from app.landing.service import generate_and_publish
    from desktop_runtime import agent_status
    from fastapi import HTTPException

    status = {"name": "codex", "installed": True, "signed_in": True,
        "available": True, "subscription_login": True, missing: False}
    monkeypatch.setattr(agent_status, "statuses", lambda: [status])
    with pytest.raises(HTTPException, match="409"):
        generate_and_publish("venture", "workspace", "A requested page")
    assert route == []


def test_page_build_rejects_wrong_workspace_before_admission(route):
    from app.landing.service import generate_and_publish
    from fastapi import HTTPException

    with pytest.raises(HTTPException, match="404"):
        generate_and_publish("venture", "another-workspace", "A requested page")
    assert route == []


def test_page_build_keeps_owner_capability_switch(route, monkeypatch):
    from app.core import local_capabilities
    from app.landing.service import generate_and_publish
    from fastapi import HTTPException

    monkeypatch.setattr(local_capabilities, "enabled", lambda name: False)
    monkeypatch.setattr(local_capabilities, "blocked_message", lambda name: "Open Settings > Capabilities and enable building.")
    with pytest.raises(HTTPException) as error:
        generate_and_publish("venture", "workspace", "A requested page")
    assert "Settings > Capabilities" in error.value.detail["message"]
    assert route == []


@pytest.mark.parametrize("failure", [ValueError("device_task_not_ready"), KeyError("device_task_not_found")])
def test_page_admission_failure_has_next_step_without_internal_fallback(route, monkeypatch, failure):
    from app.build import device_orders
    from app.landing.service import generate_and_publish
    from fastapi import HTTPException

    def reject(*args, **kwargs):
        raise failure

    monkeypatch.setattr(device_orders, "prepare_coding_task_from_chat", reject)
    with pytest.raises(HTTPException) as error:
        generate_and_publish("venture", "workspace", "A requested page")
    assert "Open Builds" in error.value.detail["message"]
    assert route == []


def test_unpromoted_teaser_has_saved_scope_next_step(route, monkeypatch):
    from app.idealab import slate
    from fastapi import HTTPException

    monkeypatch.setattr(slate, "get_opportunity", lambda identifier: SimpleNamespace(
        id="opportunity", promoted_venture_id=None, title="A requested teaser",
        one_liner="A small idea", first_customer="Interested people", wedge="One workflow",
        why_now="Repeated requests", kill_criteria="No recurring need", evidence=[]))
    with pytest.raises(HTTPException) as error:
        slate.generate_teaser("opportunity", actor_id="owner")
    assert "Open Idea Lab, start a venture" in error.value.detail["message"]
    assert route == []


def test_page_build_does_not_return_money_or_internal_receipts(route, monkeypatch):
    from app.build import device_orders
    from app.landing.service import generate_and_publish

    monkeypatch.setattr(device_orders, "prepare_coding_task_from_chat", lambda *args, **kwargs:
        {"id": "dwo_test", "state": "stopped", "credit_ceiling_micro": 123,
         "native_stop": {"task_verified": False}})
    result = generate_and_publish("venture", "workspace", "A requested page")
    assert result["build"] == {"id": "dwo_test", "state": "stopped"}
    assert result["state"] != "live"
    assert result["current_version_id"] is None
