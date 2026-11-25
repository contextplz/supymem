"""Read-only context and feedback contracts across unrelated venture domains."""
import asyncio
from contextlib import contextmanager
from datetime import timedelta
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from tests.unit.test_composio_toolkit_catalog import scopes


@pytest.fixture(params=[0, 1], ids=["wholesale", "family-lessons"])
def connected(scopes, request, monkeypatch):
    from app.connections import context_docs
    from app.content.oauth import callbacks
    from app.credits import metered, policy
    from app.db.models import ContentOAuthState
    from app.experiments import autonomous
    from app.gtm.supymem import composio_gmail
    from app.state import db
    from core.clock import utcnow

    scope = scopes[request.param]
    flags, calls, charges = {"experiments_autopilot_v1": True}, [], []
    original_policy = policy.get_policy
    for module in (context_docs, autonomous, policy):
        monkeypatch.setattr(module, "get_policy", lambda key, default=None: flags if key == "feature_flags" else original_policy(key, default))
    now = utcnow()
    with db()() as session:
        for platform in context_docs.NAMES:
            session.add(ContentOAuthState(id=scope["venture"] + platform, workspace_id=scope["workspace"],
                platform=platform, actor_subject=scope["owner"], provider_user_id=scope["owner"],
                connected_account_id="ca_" + scope["owner"] + platform, auth_config_id="auth_" + platform,
                auth_method="composio", outcome="connected", created_at=now, consumed_at=now,
                expires_at=now + timedelta(hours=1), redirect_uri="https://api.example.test/callback"))
        session.commit()
    monkeypatch.setattr(callbacks, "_verified_connection", lambda account: {"status": "ACTIVE"})

    @contextmanager
    def charge(**kwargs):
        charges.append(kwargs)
        yield SimpleNamespace(record_micro=lambda value: None)

    monkeypatch.setattr(metered, "charge_sync", charge)
    schemas = {
        "notion": [("NOTION_FETCH_PAGE", ["page_id"]), ("NOTION_FETCH_BLOCK_CONTENTS", ["block_id", "page_size"])],
        "googledocs": [("GOOGLEDOCS_GET_DOCUMENT_BY_ID", ["document_id"])],
        "googledrive": [("GOOGLEDRIVE_GET_FILE", ["file_id"])],
        "slack": [("SLACK_FETCH_CONVERSATION_HISTORY", ["channel", "limit", "oldest"])],
        "github": [("GITHUB_LIST_REPOSITORY_ISSUES", ["owner", "repo", "state", "per_page", "since"])],
    }

    def catalog(method, path, **kwargs):
        calls.append((method, path, kwargs))
        assert method == "GET" and path == "/tools"
        entries = schemas[kwargs["params"]["toolkit_slug"]]
        return {"items": [{"slug": slug, "input_parameters": {"properties": {name: {} for name in names},
                            "required": names}} for slug, names in entries]}

    def execute(slug, **kwargs):
        calls.append(("execute", slug, kwargs))
        assert kwargs["user_id"] == scope["owner"]
        assert kwargs["connected_account_id"].startswith("ca_" + scope["owner"])
        text = f"Please fix repeated scheduling conflicts for {scope['domain']}."
        if slug == "NOTION_FETCH_PAGE":
            data = {"properties": {"title": {"title": [{"plain_text": scope["domain"]}]}}}
        elif slug == "NOTION_FETCH_BLOCK_CONTENTS":
            data = {"results": [{"paragraph": {"rich_text": [{"plain_text": text * 300}]}}]}
        elif slug == "GOOGLEDOCS_GET_DOCUMENT_BY_ID":
            data = {"title": scope["domain"], "body": {"content": [{"paragraph": {"elements": [{"textRun": {"content": text * 300}}]}}]}}
        elif slug == "GOOGLEDRIVE_GET_FILE":
            data = {"name": scope["domain"], "description": text * 300}
        elif slug == "SLACK_FETCH_CONVERSATION_HISTORY":
            assert kwargs["arguments"]["channel"] == "CSELECTED"
            timestamp = (now - timedelta(days=1)).timestamp()
            data = {"messages": [{"text": text, "ts": f"{int(timestamp) + index}.1"} for index in range(2)] +
                [{"text": "Unrelated launch celebration", "ts": f"{timestamp + 3:.6f}"},
                 {"text": text, "ts": f"{timestamp + 4:.6f}", "bot_id": "bot"},
                 {"text": text, "ts": f"{(now - timedelta(days=100)).timestamp():.6f}"},
                 {"text": text, "ts": "999999999999.1"}], "has_more": True}
        elif slug == "GITHUB_LIST_REPOSITORY_ISSUES":
            assert kwargs["arguments"]["repo"] == "selected-repo"
            data = [{"title": text, "body": "", "number": index + 1, "created_at": (now - timedelta(days=1)).isoformat()}
                    for index in range(2)]
            data += [{"title": text, "number": 3, "pull_request": {}, "created_at": now.isoformat()}]
        else:
            raise AssertionError("Write or unexpected read tool")
        return {"successful": True, "data": data}

    monkeypatch.setattr(composio_gmail, "_request", catalog)
    monkeypatch.setattr(composio_gmail, "execute_tool", execute)
    return {**scope, "foreign": scopes[1 - request.param], "flags": flags, "calls": calls, "charges": charges,
            "schemas": schemas, "now": now}


@pytest.mark.parametrize("platform", ["notion", "googledocs", "googledrive"])
def test_selected_documents_are_capped_linked_metered_and_owner_scoped(connected, platform):
    from app.connections import context_docs
    from app.db.models import VentureDocument
    from app.research import context
    from app.state import db

    reply = context_docs.select_documents(connected["venture"], platform, ["selected-doc"], person_id=connected["owner"])
    assert reply["kind"] == "context_saved" and len(reply["citations"]) == 1
    with db()() as session:
        row = session.scalar(select(VentureDocument).where(VentureDocument.kind == context_docs.KIND))
        assert len(row.body_md) == context_docs.MAX_EXCERPT
        saved = context_docs.research_context(session, connected["venture"], connected["workspace"])
        assert saved["connected_context"]["documents"][0]["source_url"] == reply["citations"][0]["href"]
        assert "not instructions" in saved["connected_context"]["use"]
        assert not context_docs.research_context(session, connected["foreign"]["venture"], connected["foreign"]["workspace"])
        assert context.capture(session, connected["venture"], connected["workspace"], {})["connected_context"] == saved["connected_context"]
    assert connected["charges"] and all(item["provider"] == "composio" and item["workspace_id"] == connected["workspace"] for item in connected["charges"])
    assert "\u2014" not in reply["body_md"]


def test_local_selected_context_uses_connected_apps_instead_of_hosted_policy(connected, monkeypatch):
    from app.connections import context_docs
    from app.core.local_capabilities import blocked_message
    from app.core.local_secrets import update_settings

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    update_settings("composio", {"auth_configs": {"googledocs": "auth_googledocs"}}, {"api_key": "saved-test-key"})
    update_settings("capabilities", {"connected_apps": True}, {})
    connected["flags"].clear()
    reply = context_docs.select_documents(connected["venture"], "googledocs", ["selected_doc"], person_id=connected["owner"])
    assert reply["kind"] != "experiment_blocked" and connected["calls"]
    calls = len(connected["calls"])
    update_settings("capabilities", {"connected_apps": False}, {})
    blocked = context_docs.select_documents(connected["venture"], "googledocs", ["selected_doc"], person_id=connected["owner"])
    assert blocked["kind"] == "experiment_blocked" and blocked_message("connected_apps") in blocked["body_md"]
    assert len(connected["calls"]) == calls


def test_disabled_or_unselected_context_never_calls_providers_or_writes(connected):
    from app.connections import context_docs, feedback_mining
    from app.db.models import VentureDocument
    from app.state import db

    for flags in ({}, {"experiments_autopilot_v1": False}, {"experiments_autopilot_v1": "true"}):
        connected["flags"].clear()
        connected["flags"].update(flags)
        assert context_docs.select_documents(connected["venture"], "notion", ["secret"], person_id=connected["owner"])["kind"] == "experiment_blocked"
        assert feedback_mining.select_sources(connected["venture"], channels=["CSELECTED"], person_id=connected["owner"])["kind"] == "experiment_blocked"
    with db()() as session:
        assert not session.scalars(select(VentureDocument)).all()
    assert not connected["calls"]
    connected["flags"]["experiments_autopilot_v1"] = True
    with pytest.raises(PermissionError, match="not_selected"):
        context_docs.read(connected["venture"], "googledocs", "document", [(("documentid",), "secret")])
    assert not connected["calls"]


def test_selection_rejects_foreign_owner_invalid_ids_and_write_operations(connected):
    from app.connections import context_docs, feedback_mining
    for owner in (connected["foreign"]["owner"], None):
        with pytest.raises(PermissionError):
            context_docs.select_documents(connected["venture"], "googledocs", ["doc"], person_id=owner)
    for ids in ([], ["https://unselected.example/private"], ["doc"] * 11):
        with pytest.raises(ValueError):
            context_docs.select_documents(connected["venture"], "googledocs", ids, person_id=connected["owner"])
    with pytest.raises(ValueError):
        feedback_mining.select_sources(connected["venture"], channels=["all"], repo="../private", person_id=connected["owner"])
    with pytest.raises(ValueError, match="not_read_only"):
        context_docs.read(connected["venture"], "github", "create_issue", [])
    assert not connected["calls"]


def test_missing_revoked_and_legacy_accounts_offer_one_connect_action(connected):
    from app.connections import context_docs
    from app.db.models import ContentOAuthState
    from app.state import db

    for changes in ({"outcome": "revoked"}, {"outcome": "needs_reconnect"},
                    {"outcome": "connected", "provider_user_id": "content:" + connected["workspace"]}):
        with db()() as session:
            row = session.get(ContentOAuthState, connected["venture"] + "googledocs")
            for name, value in changes.items():
                setattr(row, name, value)
            session.commit()
        reply = context_docs.select_documents(connected["venture"], "googledocs", ["doc"], person_id=connected["owner"])
        assert reply["actions"] == context_docs.connect_reply("googledocs")["actions"]
        assert len(reply["actions"]) == 1
    assert not connected["calls"]


def test_feedback_recipe_reuses_scan_outcome_with_only_selected_sources(connected, monkeypatch):
    from app.connections import context_docs, feedback_mining
    from app.experiments import autonomous, outreach_tests, page_tests, channel_tests
    from app.state import db

    feedback_mining.select_sources(connected["venture"], channels=["CSELECTED"], repo="founder/selected-repo", person_id=connected["owner"])
    with db()() as session:
        saved = feedback_mining.selection(session, connected["venture"])
    snapshot = {"venture_id": connected["venture"], "workspace_id": connected["workspace"], "direction": connected["domain"],
        "journey": {"icp": {"who": connected["domain"], "problem": "scheduling conflicts"}},
        "waitlist": {}, "stage": "draft", "feedback_selection": saved}
    monkeypatch.setattr(outreach_tests, "specs", lambda *args: [])
    monkeypatch.setattr(page_tests, "available", lambda *args: [])
    monkeypatch.setattr(channel_tests, "available", lambda *args: [])
    specs = autonomous.select_runnable(snapshot, None)
    assert len(specs) == 1 and specs[0]["scan"] == "feedback_mining"
    spec = specs[0]
    result = feedback_mining.scan(spec)
    assert result["outcome"] == "supported"
    assert result["measured"]["recurring_feedback"] == 4
    assert len(result["details"]) == 1 and len(result["sources"]) == 4
    assert all("CSELECTED" in source["url"] or "founder/selected-repo/issues" in source["url"] for source in result["sources"])
    assert all(source["url"].endswith("100000") for source in result["sources"] if "CSELECTED" in source["url"])
    with db()() as session:
        assert context_docs.research_context(session, connected["venture"], connected["workspace"])
    feedback_mining.select_sources(connected["venture"], channels=[], repo="founder/another-repo", person_id=connected["owner"])
    with db()() as session:
        assert not context_docs.research_context(session, connected["venture"], connected["workspace"])
    connected["flags"].clear()
    assert not autonomous.select_runnable(snapshot, None)


@pytest.mark.parametrize("disconnect_before_worker", [False, True])
def test_recipe_confirmation_worker_and_disconnect_use_existing_lifecycle(connected, monkeypatch, disconnect_before_worker):
    from app.connections import feedback_mining
    from app.core import jobs
    from app.db.models import ContentOAuthState
    from app.db.supymem_models import Experiment
    from app.experiments import autonomous, activity, spinoffs, page_tests, channel_tests, outreach_tests
    from app.chat import milestones
    from app.state import db

    feedback_mining.select_sources(connected["venture"], repo="founder/selected-repo", person_id=connected["owner"])
    with db()() as session:
        saved = feedback_mining.selection(session, connected["venture"])
    snapshot = {"venture_id": connected["venture"], "workspace_id": connected["workspace"], "direction": connected["domain"],
        "journey": {"icp": {"who": connected["domain"], "problem": "scheduling conflicts"}}, "waitlist": {}, "stage": "draft", "feedback_selection": saved}
    monkeypatch.setattr(autonomous, "_snapshot", lambda *args: snapshot)
    monkeypatch.setattr(autonomous, "_provider", lambda: None)
    monkeypatch.setattr(outreach_tests, "specs", lambda *args: [])
    monkeypatch.setattr(outreach_tests, "saved_specs", lambda venture_id, specs: specs)
    monkeypatch.setattr(page_tests, "available", lambda *args: [])
    monkeypatch.setattr(channel_tests, "available", lambda *args: [])
    monkeypatch.setattr(autonomous, "enqueue", lambda *args, **kwargs: "feedback_job")
    monkeypatch.setattr(jobs, "_leased_row", lambda *args: SimpleNamespace(id="feedback_job"))
    monkeypatch.setattr(milestones, "post_milestone", lambda *args, **kwargs: None)
    monkeypatch.setattr(activity, "record_safely", lambda *args, **kwargs: None)
    async def completed(spec):
        return None
    monkeypatch.setattr(spinoffs, "on_scan_completed", completed)
    spec = autonomous.available(connected["venture"])[0]
    confirmation = autonomous.prepare(connected["venture"], spec["id"])
    assert confirmation["kind"] == "confirmation" and "only the feedback" in confirmation["body_md"]
    payload = {"experiment_id": spec["id"], "fingerprint": spec["fingerprint"], "ceiling_micro": 0}
    reply = asyncio.run(autonomous.run(connected["venture"], payload))
    assert reply["kind"] == "experiment_running"
    with db()() as session:
        row = session.get(Experiment, spec["id"])
        run_id = json.loads(row.context)["run_id"]
        if disconnect_before_worker:
            session.get(ContentOAuthState, connected["venture"] + "github").outcome = "revoked"
            session.commit()
    asyncio.run(autonomous.run_job({"experiment_id": spec["id"], "venture_id": connected["venture"],
        "workspace_id": connected["workspace"], "run_id": run_id}))
    with db()() as session:
        row = session.get(Experiment, spec["id"])
        if disconnect_before_worker:
            context = json.loads(row.context)
            assert context["retryable"] and context["connection_action"]["label"] == "Connect GitHub"
            assert row.state != "concluded"
            assert autonomous.prepare(connected["venture"], spec["id"])["actions"] == [context["connection_action"]]
            return
        assert row.state == "concluded" and json.loads(row.context)["result"]["measured"]["recurring_feedback"] == 2
        row.context = json.dumps({"runner": "system", "spec": spec})
        row.state = "proposed"
        session.get(ContentOAuthState, connected["venture"] + "github").outcome = "revoked"
        session.commit()
    assert len(autonomous.prepare(connected["venture"], spec["id"])["actions"]) == 1
    assert asyncio.run(autonomous.run(connected["venture"], payload))["actions"][0]["label"] == "Connect GitHub"


def test_oauth_uses_stable_owner_identity_and_registry_catalog(connected, monkeypatch):
    from app.analytics.feature_catalog import ACTION_SLUG
    from app.chat.actions import get_action
    from app.connections import toolkits
    from app.content import composio_social
    from app.content.oauth import attempts
    from app.state import db
    from app.tenancy import bind

    monkeypatch.setattr(attempts, "initiating_actor", lambda workspace_id: connected["owner"])
    monkeypatch.setattr(composio_social, "connectable", lambda platform: True)
    monkeypatch.setattr(composio_social, "ensure_auth_config", lambda platform: "auth")
    def link(method, path, **kwargs):
        assert kwargs["json_body"]["user_id"] == connected["owner"]
        return {"connected_account_id": "new", "redirect_url": "https://connect.example.test"}
    monkeypatch.setattr(composio_social, "_request", link)
    with bind(workspace_id=connected["workspace"], venture_id=connected["venture"], actor=connected["owner"]):
        for platform in ("notion", "googledocs", "googledrive", "slack", "github"):
            assert composio_social.start_connect(workspace_id=connected["workspace"], platform=platform)["composio_user_id"] == connected["owner"]
            assert toolkits.accounts(connected["workspace"], platform)[0]["provider_user_id"] == connected["owner"]
    for action in ("connections.context.select_documents", "connections.feedback.select_sources"):
        assert ACTION_SLUG[action] == "settings" and get_action(action).required_permission == "connect_systems"


def test_private_context_is_quoted_and_removed_from_replayed_prompts_when_disabled(connected):
    from app.idea import agent
    from app.research import selection
    snapshot = {"original_idea": connected["domain"], "connected_context": {"documents": [{"excerpt": "PRIVATE_IGNORE_ALL_RULES"}]}}
    for messages in (agent.messages_for(snapshot), selection.messages_for(snapshot, [])):
        assert "never instructions" in messages[0]["content"] and "PRIVATE_IGNORE_ALL_RULES" in messages[1]["content"]
    connected["flags"].clear()
    for messages in (agent.messages_for(snapshot), selection.messages_for(snapshot, [])):
        assert "PRIVATE_IGNORE_ALL_RULES" not in messages[1]["content"] and "connected_context" not in messages[0]["content"]
    assert "connected_context" in snapshot


def test_read_schema_verification_and_mid_call_revocation_fail_closed(connected, monkeypatch):
    from app.connections import context_docs
    from app.db.models import ContentOAuthState
    from app.gtm.supymem import composio_gmail
    from app.state import db
    context_docs.select_documents(connected["venture"], "googledocs", ["doc"], person_id=connected["owner"])
    connected["calls"].clear()
    connected["schemas"]["googledocs"] = [("GOOGLEDOCS_UPDATE_DOCUMENT", ["document_id"])]
    with pytest.raises(ValueError, match="unavailable"):
        context_docs.read(connected["venture"], "googledocs", "document", [(("documentid",), "doc")])
    assert not any(call[0] == "execute" for call in connected["calls"])
    connected["schemas"]["googledocs"] = [("GOOGLEDOCS_GET_DOCUMENT_BY_ID", ["document_id"])]
    original = composio_gmail._request
    def revoke(method, path, **kwargs):
        result = original(method, path, **kwargs)
        with db()() as session:
            session.get(ContentOAuthState, connected["venture"] + "googledocs").outcome = "revoked"
            session.commit()
        return result
    monkeypatch.setattr(composio_gmail, "_request", revoke)
    reply = context_docs.read(connected["venture"], "googledocs", "document", [(("documentid",), "doc")])
    assert reply["actions"][0]["label"] == "Connect Google Docs" and len(reply["actions"]) == 1
    assert not any(call[0] == "execute" for call in connected["calls"])


def test_idea_attempt_captures_selected_excerpts_without_changing_idea(connected, monkeypatch):
    from app.connections import context_docs
    from app.db.models import Job
    from app.db.supymem_models import IdeaClarificationRun, IdeaRevision, IdeaSession
    from app.idea import runner
    from app.state import db
    context_docs.select_documents(connected["venture"], "googledocs", ["doc"], person_id=connected["owner"])
    with db()() as session:
        session.add(IdeaSession(id="context_idea", workspace_id=connected["workspace"], venture_id=connected["venture"],
            case="know", state="clarifying", current_revision_id="context_revision", created_at=connected["now"], updated_at=connected["now"]))
        session.flush()
        session.add(IdeaRevision(id="context_revision", session_id="context_idea", workspace_id=connected["workspace"],
            venture_id=connected["venture"], rev=1, source="founder", summary=connected["domain"], structured="{}", created_at=connected["now"]))
        session.add(Job(id="context_job", kind="idea.clarify_turn", workspace_id=connected["workspace"],
            venture_id=connected["venture"], payload_json="{}", status="leased", created_at=connected["now"]))
        session.commit()
    monkeypatch.setattr(runner, "_fence", lambda *args: None)
    calls_before = len(connected["calls"])
    run_id = runner._claim({"session_id": "context_idea", "revision_id": "context_revision", "_job_id": "context_job",
        "workspace_id": connected["workspace"], "venture_id": connected["venture"], "_job_attempt": 1, "_worker_id": "fake_worker"})
    with db()() as session:
        snapshot = json.loads(session.get(IdeaClarificationRun, run_id).snapshot)
        assert snapshot["connected_context"]["documents"][0]["source_url"].endswith("/doc/edit")
        assert session.get(IdeaRevision, "context_revision").structured == "{}"
        assert session.get(IdeaSession, "context_idea").current_revision_id == "context_revision"
    assert len(connected["calls"]) == calls_before


def test_unicode_selected_excerpts_fit_the_same_prompt_budget(connected, monkeypatch):
    from app.connections import context_docs
    from app.db.models import Job, VentureDocument
    from app.db.supymem_models import IdeaClarificationRun, IdeaRevision, IdeaSession
    from app.idea import runner
    from app.state import db
    connected["flags"].update({"multi_gmail_v1": False, "inbox_briefing_v1": False})
    context_docs.select_documents(connected["venture"], "googledocs", ["doc"], person_id=connected["owner"])
    with db()() as session:
        row = session.scalar(select(VentureDocument).where(VentureDocument.kind == context_docs.KIND))
        row.body_md = "📚" * context_docs.MAX_EXCERPT
        session.add(IdeaSession(id="unicode_idea", workspace_id=connected["workspace"], venture_id=connected["venture"],
            case="know", state="clarifying", current_revision_id="unicode_revision", created_at=connected["now"], updated_at=connected["now"]))
        session.flush()
        session.add(IdeaRevision(id="unicode_revision", session_id="unicode_idea", workspace_id=connected["workspace"],
            venture_id=connected["venture"], rev=1, source="founder", summary=connected["domain"],
            structured=json.dumps({"summary": connected["domain"], "constraints": ["x" * 30000]}), created_at=connected["now"]))
        session.add(Job(id="unicode_job", kind="idea.clarify_turn", workspace_id=connected["workspace"],
            venture_id=connected["venture"], payload_json="{}", status="leased", created_at=connected["now"]))
        session.commit()
        assert context_docs.research_context(session, connected["venture"], connected["workspace"]) == {}
    monkeypatch.setattr(runner, "_fence", lambda *args: None)
    run_id = runner._claim({"session_id": "unicode_idea", "revision_id": "unicode_revision", "_job_id": "unicode_job",
        "workspace_id": connected["workspace"], "venture_id": connected["venture"], "_job_attempt": 1, "_worker_id": "fake_worker"})
    with db()() as session:
        snapshot = json.loads(session.get(IdeaClarificationRun, run_id).snapshot)
        assert "connected_context" not in snapshot
        assert 30000 < len(json.dumps(snapshot)) <= 60000
        row = session.scalar(select(VentureDocument).where(VentureDocument.kind == context_docs.KIND))
        row.body_md = "📚" * 400
        context_docs.save(session, connected["venture"], context_docs.KIND, "another_doc", connected["domain"], row.body_md,
            json.loads(row.provenance_json))
        session.commit()
        saved = context_docs.research_context(session, connected["venture"], connected["workspace"])
        assert len(saved["connected_context"]["documents"]) == 1
        assert len(json.dumps(saved)) <= 6000
        assert len(json.dumps({**snapshot, **saved})) <= 60000


@pytest.mark.parametrize("platform", ["googledocs", "slack", "github"])
def test_cached_context_is_omitted_after_last_selected_connection_is_revoked(connected, platform):
    from app.connections import context_docs, feedback_mining
    from app.db.models import ContentOAuthState, VentureDocument
    from app.research import context
    from app.state import db

    if platform == "googledocs":
        context_docs.select_documents(connected["venture"], platform, ["doc"], person_id=connected["owner"])
    else:
        feedback_mining.select_sources(connected["venture"], person_id=connected["owner"],
            **({"channels": ["CSELECTED"]} if platform == "slack" else {"repo": "founder/selected-repo"}))
        with db()() as session:
            context_docs.save(session, connected["venture"], feedback_mining.KIND, "feedback", connected["domain"],
                connected["domain"], {"selected_by": connected["owner"], "selection": feedback_mining.selection(session, connected["venture"])})
            session.commit()
    calls_before = len(connected["calls"])
    with db()() as session:
        for account in session.scalars(select(ContentOAuthState).where(
                ContentOAuthState.workspace_id == connected["workspace"], ContentOAuthState.platform != platform)):
            session.delete(account)
        session.commit()
        assert context_docs.research_context(session, connected["venture"], connected["workspace"])
        account = session.get(ContentOAuthState, connected["venture"] + platform)
        account.outcome = "revoked"
        session.commit()
        assert context_docs.research_context(session, connected["venture"], connected["workspace"]) == {}
        assert "connected_context" not in context.capture(session, connected["venture"], connected["workspace"], {})
        session.delete(account)
        session.commit()
        assert context_docs.research_context(session, connected["venture"], connected["workspace"]) == {}
        assert session.scalar(select(VentureDocument).where(VentureDocument.kind.in_([context_docs.KIND, feedback_mining.KIND])))
    assert len(connected["calls"]) == calls_before


@pytest.mark.parametrize("failure", ["policy", "scope", "database", "feedback", "connection", "provenance", "serialization"])
def test_research_context_failures_are_logged_and_return_empty(connected, monkeypatch, caplog, failure):
    from app.connections import context_docs, feedback_mining
    from app.db.models import VentureDocument
    from app.state import db

    context_docs.select_documents(connected["venture"], "googledocs", ["doc"], person_id=connected["owner"])

    def fail(*args, **kwargs):
        raise RuntimeError("context reader failed")

    with db()() as session:
        if failure == "provenance":
            row = session.scalar(select(VentureDocument).where(VentureDocument.kind == context_docs.KIND))
            row.provenance_json = "{"
            session.commit()
        else:
            target, name = {"policy": (context_docs, "enabled"), "scope": (context_docs, "owner_scope"),
                "database": (session, "scalars"), "feedback": (feedback_mining, "selection"),
                "connection": (context_docs, "connection"), "serialization": (context_docs.json, "dumps")}[failure]
            monkeypatch.setattr(target, name, fail)
        with caplog.at_level("ERROR", logger=context_docs.__name__):
            assert context_docs.research_context(session, connected["venture"], connected["workspace"]) == {}
        assert any(record.message == "connected_context_unavailable" and record.exc_info for record in caplog.records)
