"""Tool-backed experiment regressions in unrelated business and consumer markets."""
import asyncio
import json
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace

import pytest

from tests.unit.test_composio_toolkit_catalog import scopes
from tests.unit.test_crm_survey_evidence import evidence_case


@pytest.fixture
def tool_case(evidence_case, monkeypatch):
    from app.experiments import autonomous, ledger, tools

    case = evidence_case
    monkeypatch.setattr(tools, "get_policy", lambda _key: {"experiments_tools_v2": case.get("tools_on", True)})
    scope = case["scope"]
    from app.state import db
    from app.db.supymem_models import FounderApp
    from core.clock import utcnow
    with db()() as session:
        direction = ledger.current_direction(session, scope["venture"])
        session.add(FounderApp(id="tool_app", venture_id=scope["venture"], workspace_id=scope["workspace"],
            slug="tool-app", name=scope["domain"], kind="web", created_at=utcnow(), updated_at=utcnow()))
        session.commit()
    snapshot = {"venture_id": scope["venture"], "workspace_id": scope["workspace"], "direction": direction,
        "stage": "draft", "journey": {"icp": {"who": scope["domain"], "problem": "finding enough interested customers"}},
        "waitlist": {"published_site": {"url": "https://published.example/test"}}, "app_id": "tool_app"}
    case["snapshot"] = snapshot
    case["specs"] = tools.specs(snapshot)
    monkeypatch.setattr(autonomous, "available", lambda _venture: case["specs"])
    monkeypatch.setattr(autonomous, "select_runnable", lambda *_args: deepcopy(case["specs"]))
    monkeypatch.setattr(autonomous, "_provider", lambda: None)
    monkeypatch.setattr(autonomous, "_snapshot", lambda _venture: deepcopy(snapshot))
    return case


def save_spec(case, tool):
    from app.db.supymem_models import Experiment
    from app.experiments import ledger
    from app.state import db

    spec = next(spec for spec in case["specs"] if spec["tool"] == tool)
    with db()() as session:
        session.add(Experiment(id=spec["id"], venture_id=spec["venture_id"], workspace_id=spec["workspace_id"],
            kind=ledger.KIND, hypothesis=spec["hypothesis"], state="proposed", context=json.dumps({"runner": "system", "spec": spec})))
        session.commit()
    return spec


def test_registry_feeds_designer_and_chat_and_discovers_registered_tools(tool_case, monkeypatch):
    from app.chat import assistant
    from app.connections import toolkits
    from app.experiments import beliefs, exploration, tools

    case = tool_case
    monkeypatch.setitem(toolkits.TOOLKITS, "new_connector", {"name": "New Connector", "goals": ["Read responses"]})
    monkeypatch.setitem(tools.READ_RUNNERS, "new_connector", tools.READ_RUNNERS["typeform"])
    registry = tools.capabilities(case["scope"]["venture"])
    entries = {item["slug"]: item for item in registry["tools"]}
    assert entries["new_connector"]["runners"] == ["tool_evidence"]
    assert not entries["new_connector"]["runnable"] and entries["new_connector"]["requires_connection"]
    assert entries["hubspot"]["runnable"] and entries["typeform"]["runnable"]
    assert entries["google_analytics"]["runners"] == ["app.connections.traffic.readout"]
    assert entries["google_search_console"]["runners"] == ["app.connections.traffic.readout"]
    assert entries["google_calendar"]["runners"] == ["app.connections.bookings.read_bookings"]
    assert "account_id" not in json.dumps(registry) and case["scope"]["owner"] not in json.dumps(registry)
    monkeypatch.setattr("app.autopilot.llm.brief_text", lambda _venture: case["scope"]["domain"])
    monkeypatch.setattr("app.lead_library.service.match", lambda *_args, **_kwargs: {"total": 0, "filters": {}, "items": []})
    _, inputs = beliefs.context(case["scope"]["venture"])
    assert inputs["experiment_capabilities"] == registry
    messages = assistant.build_messages(question="What tools can you use?", brief={"venture_id": case["scope"]["venture"], "_rendered": case["scope"]["domain"]}, history=[])
    assert "New Connector" in messages[1]["content"] and "tool_evidence" in messages[1]["content"]
    options = [{"statement": "You can test " + case["scope"]["domain"] + " with option " + str(index),
                "required_tool": "typeform", "tool_reason": "You can read answers from your exact form."} for index in range(3)]
    drafts = exploration._drafts(exploration.Kind.DEMAND_TEST, options)
    round_ = exploration._append_round({}, exploration.Kind.DEMAND_TEST, drafts, case["snapshot"]["direction"])
    assert all(option["method"] == "tool_evidence" and option["required_tool"] == "typeform" for option in round_["options"])
    linkedin = exploration._drafts(exploration.Kind.LINKEDIN, [{"statement": option["statement"]} for option in options])
    assert all(draft["required_tool"] == "linkedin" for draft in linkedin)
    from app.db.models import ContentOAuthState
    from app.state import db
    from sqlalchemy import select
    with db()() as session:
        account = session.scalar(select(ContentOAuthState).where(ContentOAuthState.workspace_id == case["scope"]["workspace"],
            ContentOAuthState.platform == "typeform"))
        account.provider_user_id = "content:" + case["scope"]["workspace"]
        session.commit()
    migrated = {item["slug"]: item for item in tools.capabilities(case["scope"]["venture"])["tools"]}
    assert not migrated["typeform"]["runnable"] and migrated["typeform"]["needs_reconnect"]
    spec = save_spec(case, "typeform")
    assert tools.prepare(spec["venture_id"], spec)["actions"][0]["payload"]["query"] == "reconnect Typeform"
    case["tools_on"] = False
    assert tools.capabilities(case["scope"]["venture"]) is None and tools.specs(case["snapshot"]) == []
    _, inputs = beliefs.context(case["scope"]["venture"])
    assert "experiment_capabilities" not in inputs
    messages = assistant.build_messages(question="What tools can you use?", brief={"venture_id": case["scope"]["venture"], "_rendered": case["scope"]["domain"]}, history=[])
    assert "New Connector" not in messages[1]["content"]
    for value in (None, 1, "true"):
        case["tools_on"] = value
        assert not tools.enabled()


@pytest.mark.parametrize("tool", ("reddit", "linkedin", "typeform", "hubspot"))
def test_designer_proposes_disconnected_best_tool_with_one_connect_and_signup_steps(tool_case, monkeypatch, tool):
    from app.credits import metered
    from app.experiments import belief_planner, beliefs, tools

    case, scope = tool_case, tool_case["scope"]
    inputs = {"icp": case["snapshot"]["journey"]["icp"], "experiment_capabilities": tools.capabilities(scope["venture"])}
    reason = "You can measure responses from " + scope["domain"] + " in this test."
    async def llm(messages, **kwargs):
        assert "including disconnected tools" in messages[0]["content"]
        assert "required_tool" in json.dumps(kwargs["response_schema"])
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps({"beliefs": [{
            "type": "channel", "statement": "You can reach " + scope["domain"] + " through this channel.",
            "confidence": 0.2, "required_tool": tool, "tool_reason": reason}]})))])
    monkeypatch.setattr(metered, "llm", llm)
    generated = asyncio.run(beliefs._draft(scope["venture"], inputs, parent={"id": "parent", "type": "channel", "statement": "A different prior claim"}))
    plan = belief_planner.build(generated, case["specs"], {"journey": {}, "experiments": []})
    assert plan["waiting"][0]["tool"] == tool and not plan["tests"]
    reply = tools.connection_reply(tool, reason)
    assert len(reply["actions"]) == 1 and reply["actions"][0]["action"] == "connections.connect"
    assert reason in reply["body_md"] and "Don't have an account? I'll walk you through it" in reply["body_md"]
    assert all(f"{step}. " in reply["body_md"] for step in range(1, 6))
    assert "Do not send credentials here" in reply["body_md"] and "—" not in reply["body_md"]


@pytest.mark.parametrize("mode", ["default", "local"])
def test_connect_services_resolve_configured_traffic_and_catalog_names(tool_case, monkeypatch, mode):
    from app.chat import connect
    from app.connections import hub, toolkits

    case = tool_case
    monkeypatch.setattr(connect, "_is_connected", lambda *_args: False)
    monkeypatch.setattr(connect, "_save_watch", lambda *_args, **_kwargs: None)
    connected = []
    def start(_workspace, slug, **_kwargs):
        connected.append(slug)
        return {"ok": True, "redirect_url": "https://connect.example/" + slug}
    monkeypatch.setattr(hub, "connect_provider", start)
    names = {"Google Analytics": "google_analytics", "Google Search Console": "google_search_console", "HubSpot": "hubspot", "Typeform": "typeform"}
    if mode == "local":
        from app.core.local_secrets import update_settings

        monkeypatch.setenv("SUPYMEM_MODE", "local")
        update_settings("composio", {"auth_configs": {slug: "ac_" + slug for slug in names.values()}}, {"api_key": "saved-test-key"})
        update_settings("capabilities", {"connected_apps": True}, {})
    else:
        for slug in names.values():
            monkeypatch.setenv(toolkits.TOOLKITS[slug]["auth_config_env"], "ac_" + slug)
    for name, slug in names.items():
        assert connect.service_from("connect " + name) == slug
        reply = connect.handle(case["scope"]["venture"], "connect " + name)
        assert reply["actions"][0]["href"] == "https://connect.example/" + slug
    assert connected == list(names.values())
    case["tools_on"] = False
    if mode == "default":
        for slug in names.values():
            monkeypatch.delenv(toolkits.TOOLKITS[slug]["auth_config_env"])
    assert all(connect.service_from("connect " + name) == (slug if mode == "local" or slug in connect.SERVICES else None)
               for name, slug in names.items())


def test_wizard_runner_matches_saved_page_request_and_flag_off_is_unchanged(tool_case):
    from app.experiments import belief_planner

    spec = {"scan": "page_test", "page_kind": "request", "state": "running"}
    assert belief_planner.runner(spec) == "wizard_tests"
    assert belief_planner._portfolio_lane(spec) == "response"
    tool_case["tools_on"] = False
    assert belief_planner.runner(spec) == "page_request"


def test_crm_test_ingests_only_chosen_contacts_with_its_own_baseline(tool_case):
    from app.connections import crm
    from app.db.supymem_models import Experiment
    from app.experiments import ledger, tools
    from app.state import db

    case = tool_case
    spec = save_spec(case, "hubspot")
    with pytest.raises(ValueError, match="Only you"):
        tools.link(spec["venture_id"], {"experiment_id": spec["id"], "contact_ids": ["known"]}, None)
    with pytest.raises(ValueError, match="scope_invalid"):
        tools.link(spec["venture_id"], {"experiment_id": spec["id"], "contact_ids": ["known"]}, case["other"]["owner"])
    with pytest.raises(ValueError, match="out_of_scope"):
        tools.link(spec["venture_id"], {"experiment_id": spec["id"], "contact_ids": ["foreign"]}, case["scope"]["owner"])
    tools.link(spec["venture_id"], {"experiment_id": spec["id"], "contact_ids": ["known"]}, case["scope"]["owner"])
    first = tools.readout(spec["venture_id"], {"experiment_id": spec["id"]})
    assert first["kind"] == "experiment_result", first
    with db()() as session:
        baseline = ledger._ctx(session.get(Experiment, spec["id"]))["crm_snapshot"]
    crm.snapshot(spec["venture_id"])
    with db()() as session:
        assert ledger._ctx(session.get(Experiment, spec["id"]))["crm_snapshot"] == baseline
    case["stage"] = "evaluation"
    result = crm.snapshot(spec["venture_id"], experiment_id=spec["id"])
    assert (result["contact_count"], result["deal_count"], result["progress_count"]) == (1, 1, 1)
    assert result["scope"] == "experiment_contacts" and result["experiment_id"] == spec["id"]
    with db()() as session:
        chosen = ledger._ctx(session.get(Experiment, spec["id"]))
        other = ledger._ctx(session.get(Experiment, case["experiment"]))
    assert chosen["crm_snapshot"]["progressed"] == ["deal_remote_known"] and len(other["crm_snapshot"]["deals"]) == 2
    assert all(call[0] in {"GET", "EXECUTE"} for call in case["calls"])
    assert all(meter["payer"] == "founder" for meter in case["meters"])
    with db()() as session:
        row = session.get(Experiment, spec["id"])
        context = ledger._ctx(row)
        context["crm_snapshot"]["account_id"] = "previous_account"
        context["crm_snapshot"]["observed_from"] = "2020-01-01T00:00:00+00:00"
        row.context = json.dumps(context)
        session.commit()
    reset = crm.snapshot(spec["venture_id"], experiment_id=spec["id"])
    assert reset["progress_count"] == 0 and reset["observed_from"] != "2020-01-01T00:00:00+00:00"
    case["active"] = False
    reconnect = tools.readout(spec["venture_id"], {"experiment_id": spec["id"]})
    assert len(reconnect["actions"]) == 1 and reconnect["actions"][0]["payload"]["query"] == "reconnect HubSpot"


@pytest.mark.parametrize("platform", ("hubspot", "typeform"))
def test_tool_connections_use_founder_identity_without_autopilot(tool_case, monkeypatch, platform):
    from app.content import composio_social
    from app.content.oauth import attempts
    from app.experiments import tools

    case = tool_case
    case["flag"] = False
    monkeypatch.setattr(composio_social, "ensure_auth_config", lambda _platform: "ac_" + platform)
    monkeypatch.setattr(attempts, "initiating_actor", lambda _workspace: case["scope"]["owner"])
    assert tools.enabled()
    connected = composio_social.start_connect(workspace_id=case["scope"]["workspace"], platform=platform)
    assert connected["composio_user_id"] == case["scope"]["owner"]
    case["tools_on"] = False
    connected = composio_social.start_connect(workspace_id=case["scope"]["workspace"], platform=platform)
    assert connected["composio_user_id"] == "content:" + case["scope"]["workspace"]


def test_survey_test_counts_only_chosen_form_and_observation_window(tool_case):
    from app.connections import surveys
    from app.db.supymem_models import Experiment
    from app.experiments import ledger, tools
    from app.state import db
    from core.clock import utcnow

    case = tool_case
    spec = save_spec(case, "typeform")
    surveys.link_form(spec["venture_id"], spec["id"], "form_one", case["scope"]["owner"])
    old = deepcopy(case["responses"]["form_one"]["items"][0])
    new = {**deepcopy(old), "response_id": "new_response", "submitted_at": utcnow().isoformat()}
    case["responses"]["form_one"] = {"items": [old, new], "total_items": 2, "page_count": 1}
    reply = tools.readout(spec["venture_id"], {"experiment_id": spec["id"]})
    assert reply["kind"] == "experiment_result", reply
    with db()() as session:
        result = ledger._ctx(session.get(Experiment, spec["id"]))["result"]
    assert result["measured"] == {"events": 1, "response_count": 1} and result["outcome"] == "inconclusive"
    calls = [call for call in case["calls"] if call[0] == "EXECUTE"]
    assert len(calls) == 1 and calls[0][2]["arguments"]["form_id"] == "form_one"
    assert result["attribution"] == {"experiment_id": spec["id"], "tool": "typeform"}
    assert "private@example.test" not in json.dumps(result)


@pytest.mark.parametrize("platform", ("reddit", "linkedin"))
def test_social_requires_published_approved_post_and_attributes_replies_and_clicks(tool_case, monkeypatch, platform):
    from app.connections import crm
    from app.content import publication
    from app.db.models import ContentPost
    from app.db.supymem_models import AppEvent
    from app.experiments import channel_tests, tools
    from app.state import db
    from core.clock import utcnow

    case = tool_case
    spec = save_spec(case, platform)
    link = channel_tests.tagged_link(spec["site_url"], utm_source=platform, utm_medium="social", utm_campaign=spec["id"])
    post = ContentPost(id="chosen_post", workspace_id=spec["workspace_id"], topic_id="topic", platform=platform,
        body=case["scope"]["domain"] + " " + link, state="draft", dry_run=False,
        created_at=utcnow(), updated_at=utcnow())
    monkeypatch.setattr(publication, "_post", lambda _session, _workspace, _venture, identifier: (post, None) if identifier == post.id else (_ for _ in ()).throw(ValueError("wrong post")))
    tools.link(spec["venture_id"], {"experiment_id": spec["id"], "post_id": post.id}, case["scope"]["owner"])
    with pytest.raises(ValueError, match="must be published"):
        tools.observe(spec)
    assert post.state == "draft" and not case["calls"]
    published = utcnow() - timedelta(hours=1)
    post.state, post.external_id, post.external_url, post.published_at = "published", "t3_post" if platform == "reddit" else "urn:li:share:post", "https://post.example/exact", published
    with db()() as session:
        for source, campaign, timestamp, path in ((platform, spec["id"], utcnow(), "/test"), ("other", spec["id"], utcnow(), "/test"),
                (platform, "another_test", utcnow(), "/test"), (platform, spec["id"], published - timedelta(hours=1), "/test"),
                (platform, spec["id"], utcnow(), "/unrelated")):
            session.add(AppEvent(app_id=spec["app_id"], workspace_id=spec["workspace_id"], name="landing_view", ts=timestamp,
                props=json.dumps({"utm_source": source, "utm_medium": "social", "utm_campaign": campaign, "path": path})))
        session.commit()
    calls = []
    def reader(_venture, slug, collect):
        assert slug == platform
        def read(operation, values):
            calls.append((operation, values))
            if platform == "linkedin":
                return {"id": post.external_id, "comment_count": 2}
            return {"comments": [{"id": "reply_one", "link_id": post.external_id}, {"id": "reply_two", "link_id": post.external_id}]}
        return {"status": "connected", **collect(read, spec["workspace_id"])}
    monkeypatch.setattr(crm, "_reader", reader)
    result = tools.observe(spec)
    assert result["measured"] == {"events": 2, "replies": 2, "clicks": 1}
    assert result["attribution"]["experiment_id"] == spec["id"] and result["observed_from"] == published.isoformat()
    assert calls[0][0] == "post_replies" and calls[0][1]["post_id"] == ("post" if platform == "reddit" else post.external_id)
    post.dry_run = True
    with pytest.raises(ValueError, match="must be published"):
        tools.observe(spec)
