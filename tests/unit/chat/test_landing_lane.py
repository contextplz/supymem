"""Landing pages are always built by Claude Code / Codex: the firstlook template lane is retired, even via env."""

import re

import pytest

from tests.company_os_harness import append_idea_revision, load_company_os_app
from tests.unit.pagebuilder import test_api

setup = test_api.setup


@pytest.mark.parametrize("idea,audience", [
    ("Manage service appointments for commercial building managers", "commercial building managers"),
    ("Organize weekend cycling trips for families", "families planning cycling trips"),
])
def test_disabled_landing_builder_keeps_the_page_specific_next_step(setup, monkeypatch, idea, audience):
    import asyncio
    import json
    from app.chat import router
    from app.chat.context import ChatContext
    from app.db.models import Job, Venture
    from app.tenancy import bind

    data = setup
    monkeypatch.setenv("SUPYMEM_PAGEBUILDER_ENABLED", "0")
    with data["factory"]() as session:
        session.get(Venture, data["vid"]).raw_idea = idea
        data["rid"] = append_idea_revision(session, data["rid"],
            summary=idea, structured=json.dumps({"audience": audience})).id
        session.commit()
    ctx = ChatContext(venture={"id": data["vid"], "workspace_id": data["wid"]})
    with bind(workspace_id=data["wid"], venture_id=data["vid"], actor=data["actor"]):
        reply = asyncio.run(router._dispatch_intent(data["vid"], ctx, "start_product_build",
            "Build my landing page.", None, data["actor"]))
    assert reply["kind"] == "needs_you" and not reply["action_started"]
    assert "Your landing page builder is not available here" in reply["body_md"]
    assert "test your idea without a page" in reply["body_md"]
    assert reply["actions"] == [{"label": "Record your interview", "action": "chat.prefill",
                                "payload": {"text": "Record my customer interview: "}}]
    assert not re.search(r"\b(?:credits?|app|research)\b", reply["body_md"], re.I)
    with data["factory"]() as session:
        assert session.query(Job).count() == 0


@pytest.mark.parametrize("idea,audience", [
    ("Track equipment inspections for commercial building managers", "commercial building managers"),
    ("Organize weekend cycling trips for families", "families planning cycling trips"),
])
@pytest.mark.parametrize("text,starts", [
    ("Build me a landing page with a waitlist", True),
    ("Could you build my page with a waitlist?", True),
    ("Would you build my landing page with a waitlist?", True),
    ("How do I build a landing page with a waitlist?", False),
    ("Can I build a waitlist-only landing page?", False),
])
def test_requested_page_starts_once_with_server_hold_but_questions_do_not(setup, monkeypatch, idea, audience, text, starts):
    import asyncio
    import json
    from sqlalchemy import select
    from app.chat import router
    from app.chat.context import ChatContext
    from app.credits.accounts import get_account
    from app.credits.policy import person_wallet_enabled
    from app.db.models import CreditReservation, Job, Venture
    from app.pagebuilder import api, runtime
    from app.tenancy import bind
    from llm_gateway import sole

    data = setup
    monkeypatch.setattr(api, "publish_wake", lambda: None)
    with data["factory"]() as session:
        session.get(Venture, data["vid"]).raw_idea = idea
        data["rid"] = append_idea_revision(session, data["rid"],
            summary=idea, structured=json.dumps({"audience": audience})).id
        session.commit()
    ctx = ChatContext(venture={"id": data["vid"], "workspace_id": data["wid"]})
    with bind(workspace_id=data["wid"], venture_id=data["vid"], actor=data["actor"]):
        reply = asyncio.run(router._dispatch_intent(data["vid"], ctx, "start_product_build", text, None, data["actor"]))
        if starts:
            assert reply["kind"] != "confirmation" and reply.get("run_id")
            repeated = asyncio.run(router._dispatch_intent(data["vid"], ctx, "start_product_build", text, None, data["actor"]))
            assert repeated.get("run_id", reply["run_id"]) == reply["run_id"]
        else:
            assert reply["actions"] and not reply.get("run_id")
    assert person_wallet_enabled()
    with data["factory"]() as session:
        jobs = list(session.scalars(select(Job).where(Job.venture_id == data["vid"], Job.kind == api.KIND)))
        holds = list(session.scalars(select(CreditReservation).where(CreditReservation.venture_id == data["vid"])))
        assert len(jobs) == int(starts) and not holds
        if starts:
            saved = json.loads(jobs[0].payload_json)["_runtime"]
            assert saved["quote"]["ceiling_micro"] > 0
            assert saved["input"]["waitlist_url"] and saved["input"]["waitlist_consent_text"]
    assert get_account(data["vid"]).spent_micro == get_account(data["vid"]).reserved_micro == 0
    if starts:
        calls = []

        async def transmit(**kwargs):
            calls.append(kwargs)
            assert 0 < get_account(data["vid"]).reserved_micro <= saved["quote"]["ceiling_micro"]
            return test_api.response()

        monkeypatch.setattr(sole, "transmit", transmit)
        started, payload = test_api.claimed(data)
        assert started["job_id"] == reply["run_id"]
        call = runtime.job_model_call(started["job_id"], payload)
        assert call("code", idea) == call("code", idea)
        assert len(calls) == 1 and get_account(data["vid"]).spent_micro > 0
        assert get_account(data["vid"]).reserved_micro == 0
    assert not any(word in reply["body_md"].lower() for word in ("credit", "ceiling", "quote", "confirm"))
    assert "—" not in reply["body_md"]


@pytest.mark.parametrize("idea", ["help people choose a gym", "help pottery studios fill classes"])
def test_unconfirmed_landing_page_request_is_friendly_and_keeps_idea_action(monkeypatch, idea):
    monkeypatch.setenv("SUPYMEM_MODE", "hosted")
    load_company_os_app()
    from app.chat.router import _product_build_reply

    reply = _product_build_reply("ven_x", {"idea": {"confirmed": False, "name": idea}},
                                 hint="build my landing page")
    assert reply["body_md"] == ("Your landing page needs one step first: confirm your idea. "
                                "It takes a minute. Nothing was started.")
    assert reply["actions"] == [{"label": "Confirm the idea", "href": "/v/ven_x/idea", "style": "primary"}]


def test_landing_lane_is_build_even_when_env_says_firstlook(monkeypatch):
    load_company_os_app()
    from app.chat.site_actions import landing_lane

    monkeypatch.setenv("SUPYMEM_LANDING_LANE", "firstlook")
    assert landing_lane() == "build"


def test_resources_never_offer_a_firstlook_capability(monkeypatch):
    load_company_os_app()
    from app.planning import resources

    monkeypatch.setenv("SUPYMEM_LANDING_LANE", "firstlook")
    assert not hasattr(resources, "landing_lane") and not hasattr(resources, "LANDING_CAPABILITY")


@pytest.mark.asyncio
async def test_build_my_landing_page_never_offers_a_template_page(monkeypatch):
    monkeypatch.delenv("SUPYMEM_MODE", raising=False)
    load_company_os_app()
    from app.chat import router
    from app.chat.context import ChatContext

    monkeypatch.setenv("SUPYMEM_LANDING_LANE", "firstlook")
    monkeypatch.setattr(router, "_planned_landing_card", lambda *_a: None)
    monkeypatch.setattr(router, "_product_build_reply", lambda *_a, **_k: {"kind": "navigation", "actions": []})
    monkeypatch.setattr("app.ventures.state_brief.get_venture_state_brief", lambda *_a, **_k: {})
    ctx = ChatContext(venture={"id": "ven_x", "workspace_id": "wsp_x"})
    reply = await router._dispatch_intent("ven_x", ctx, "start_product_build", "Build my landing page", None)
    assert reply == {"kind": "navigation", "actions": []}  # the real build path, never a site.rewrite template card


def test_no_new_template_page_without_an_existing_one(monkeypatch):
    monkeypatch.setenv("SUPYMEM_MODE", "hosted")
    load_company_os_app()
    from app.firstlook import service

    monkeypatch.setattr(service.versions_mod, "get_latest", lambda *_a, **_k: None)
    with pytest.raises(service.FirstLookError) as exc:
        service.assert_existing_page("wsp_x", "ven_x")
    assert exc.value.code == "firstlook_retired"
    monkeypatch.setattr(service.versions_mod, "get_latest", lambda *_a, **_k: object())
    service.assert_existing_page("wsp_x", "ven_x")  # an existing page may still be rewritten


@pytest.fixture(params=["Warehouse stock checks for wholesalers", "Weekend pottery classes for families"])
def local_page_request(request, monkeypatch, tmp_path):
    import hashlib
    import sys
    from types import SimpleNamespace
    from fastapi import HTTPException

    load_company_os_app()
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    from app.build import device_orders
    from app.chat import execute, site_actions
    from app.core import local_capabilities
    from app.firstlook import service, versions
    from app.pagebuilder import api, local_publish

    state = {"connected": True, "orders": [], "configured": [], "idea": request.param}
    source = tmp_path / "saved.html"
    source.write_text("<html><body><h1>Saved heading</h1></body></html>")
    legacy = SimpleNamespace(id="saved-version", workspace_id="workspace", venture_id="venture", version=1,
        path=str(source), sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        copy={"headline": "Saved heading"}, archetype="service", stage="first_look")
    monkeypatch.setattr(site_actions, "_venture", lambda venture_id: ("workspace", request.param, request.param))
    monkeypatch.setattr(site_actions, "_saved_page_builds", lambda *args: [])
    monkeypatch.setattr(versions, "get_latest", lambda *args, **kwargs: legacy)
    monkeypatch.setattr(versions, "validate_binding", lambda *args: None)
    monkeypatch.setattr("app.tenancy.current_or_none", lambda: SimpleNamespace(actor="owner", workspace_id="workspace", venture_id="venture"))
    snapshot = local_capabilities.capability
    monkeypatch.setattr(local_capabilities, "capability", lambda name:
        {"state": "ready"} if name in {"landing_pages", "build_with_my_tool"} else snapshot(name))
    from app.chat import product_build_request
    monkeypatch.setattr(product_build_request, "local_product_provider",
        lambda *args: "codex" if state["connected"] else None, raising=False)
    monkeypatch.setattr(local_publish, "build_test_preview", lambda *args: pytest.fail("internal preview generator ran"))
    monkeypatch.setattr("app.core.local_mode.is_test_mode", lambda: True)
    monkeypatch.setattr("app.core.local_mode.is_sample_workspace", lambda wid: True)

    def require_connected(actor):
        assert actor == "owner"
        if not state["connected"]:
            raise HTTPException(409, detail={"code": "pagebuilder_coding_tool_required", "message":
                "Connect your coding tool in Settings > Keys and connections > Coding tools, then retry your page request."})
        return "codex"

    monkeypatch.setitem(sys.modules, "app.pagebuilder.tool", SimpleNamespace(require_connected=require_connected))
    monkeypatch.setattr("app.core.local_owner.current_owner", lambda: SimpleNamespace(id="owner"))

    def prepare(venture_id, caller, brief, **kwargs):
        import json

        assert state["connected"] and venture_id == "venture"
        if brief.split("\n", 1)[-1].startswith("{"):
            payload = json.loads(brief.split("\n", 1)[1])
            kwargs["source_version_id"] = payload["source"]["id"]
        state["orders"].append({"brief": brief, **kwargs})
        return {"id": "tool-order", "state": "approved", "review_digest": "review"}

    monkeypatch.setattr(device_orders, "prepare_coding_task_from_chat", prepare)

    def start(venture_id, actor, **kwargs):
        prepare(venture_id, actor, "Build only the private landing page for " + state["idea"])
        return {"job_id": "page-job", "state": "pending"}

    def rewrite(venture_id, actor, *, new_version_of, instruction):
        prepare(venture_id, actor, instruction + "\nConfirmed idea: " + state["idea"], source_job_id=new_version_of)
        return {"job_id": "edit-job", "state": "pending"}

    monkeypatch.setattr(api, "estimate", lambda *args: {"ready": True, "quote_fingerprint": "quote", "ceiling_micro": 100})
    monkeypatch.setattr(api, "_start", start)
    monkeypatch.setattr(api, "rewrite", rewrite)
    monkeypatch.setattr(service, "generate_first_look", lambda *args, **kwargs: pytest.fail("internal firstlook generator ran"))
    monkeypatch.setattr(site_actions, "_saved_rewrite_reply", lambda *args: site_actions._reply(
        "Your coding tool is editing your saved page. Review it when checks finish; nothing public changed."))
    monkeypatch.setattr("app.autopilot.receipts.queued_job", lambda job_id: None)
    return state, execute, site_actions, legacy, source


@pytest.mark.parametrize("connected", [True, False])
def test_local_chat_page_build_uses_tool_admission_not_sample_generator(local_page_request, connected):
    state, execute, _, _, _ = local_page_request
    state["connected"] = connected
    reply = execute._build_landing_page("venture", {"prepare": True}, "owner")
    if connected:
        assert len(state["orders"]) == 1 and state["idea"] in state["orders"][0]["brief"]
        assert reply["run_id"] == "page-job" and "coding tool" in reply["body_md"]
    else:
        assert not state["orders"] and reply["action_started"] is False
        assert "Settings > Keys and connections > Coding tools" in reply["body_md"]
        assert reply["actions"][0]["href"] == "/settings/local#coding-tools"


@pytest.mark.asyncio
@pytest.mark.parametrize("connected", [True, False])
@pytest.mark.parametrize("source_kind", ["builder", "legacy"])
async def test_local_chat_page_rewrite_routes_fenced_source_to_tool(local_page_request, monkeypatch, connected, source_kind):
    import base64
    import json
    from types import SimpleNamespace

    state, _, site_actions, _, source = local_page_request
    state["connected"] = connected
    before = source.read_bytes()
    instruction = "Change the headline for " + state["idea"] + "; keep the consent and existing form unchanged."
    if source_kind == "builder":
        saved = SimpleNamespace(id="saved-job", payload_json=json.dumps({"_runtime": {"state": "ready"},
            "result": {"bundles": [{"files": {"index.html": base64.b64encode(before).decode()}}]}}))
        monkeypatch.setattr(site_actions, "_saved_page_builds", lambda *args: [saved])
    reply = await site_actions._rewrite("venture", {"instruction": instruction}, "owner")
    assert source.read_bytes() == before
    if connected:
        assert len(state["orders"]) == 1 and instruction in state["orders"][0]["brief"]
        assert state["orders"][0]["source_job_id" if source_kind == "builder" else "source_version_id"] == (
            "saved-job" if source_kind == "builder" else "saved-version")
        assert "coding tool" in reply["body_md"] and "Your saved landing page changed" not in reply["body_md"]
        if source_kind == "legacy":
            assert reply["actions"][0]["payload"]["order_id"] == "tool-order"
    else:
        assert not state["orders"] and reply["action_started"] is False
        assert "Settings > Keys and connections > Coding tools" in reply["body_md"]


def test_local_chat_edit_preflight_analyzes_intent_without_replacing_html(local_page_request):
    state, _, site_actions, _, source = local_page_request
    before = source.read_bytes()
    reply = site_actions.route("venture", "site.rewrite", "Change the headline for " + state["idea"])
    assert source.read_bytes() == before and not state["orders"]
    assert reply["actions"][0]["action"] == "site.rewrite"
    assert not reply["actions"][0].get("confirmation")


@pytest.mark.parametrize("connected", [True, False])
def test_local_chat_publish_builds_missing_page_with_tool_not_sample(local_page_request, monkeypatch, connected):
    state, _, site_actions, _, _ = local_page_request
    state["connected"] = connected
    from app.conversion import waitlist

    monkeypatch.setattr("app.api.pagebuilder_api.profile", lambda *args, **kwargs: {"current": None})
    monkeypatch.setattr(site_actions, "_waitlist_state", lambda venture_id: {"enabled": False})
    monkeypatch.setattr(waitlist, "configure", lambda *args, **kwargs: state["configured"].append(kwargs))
    monkeypatch.setattr(site_actions, "_remember_publish", lambda job_id: None)
    reply = site_actions.publish_page("venture", "owner")
    if connected:
        assert len(state["orders"]) == 1 and state["idea"] in state["orders"][0]["brief"]
        assert len(state["configured"]) == 1 and "coding tool" in reply["body_md"]
        assert "Nothing is public yet" in reply["body_md"]
    else:
        assert not state["orders"] and not state["configured"] and reply["action_started"] is False
        assert "Settings > Keys and connections > Coding tools" in reply["body_md"]


@pytest.mark.asyncio
async def test_local_chat_legacy_edit_rejects_changed_saved_source(local_page_request):
    state, _, site_actions, _, source = local_page_request
    source.write_text("<html><body>Unverified changed source</body></html>")
    reply = await site_actions._rewrite("venture", {"instruction": "Change the headline for " + state["idea"]}, "owner")
    assert not state["orders"] and reply["kind"] == "needs_you"
    assert "Choose Review your saved page below" in reply["body_md"]
    assert any(action.get("action") == "site.preview" for action in reply["actions"])


@pytest.mark.asyncio
@pytest.mark.parametrize("connected", [True, False])
async def test_local_chat_charter_refinement_uses_tool_and_never_claims_completion(local_page_request, monkeypatch, connected):
    state, execute, _, _, _ = local_page_request
    state["connected"] = connected
    monkeypatch.setattr(execute, "_refine_site_context", lambda venture_id:
        ("workspace", state["idea"], {"content": {"one_liner": state["idea"]}}, state["idea"]))
    reply = await execute._refine_site("venture")
    if connected:
        assert len(state["orders"]) == 1 and state["idea"] in state["orders"][0]["brief"]
        assert "coding tool" in reply["body_md"] and "Rebuilt" not in reply["body_md"]
        assert reply["device_order"]["id"] == "tool-order"
    else:
        assert not state["orders"] and reply["action_started"] is False
        assert "Settings > Keys and connections > Coding tools" in reply["body_md"]
