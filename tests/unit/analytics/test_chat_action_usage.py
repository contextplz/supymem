"""Chat action telemetry describes dispatch, not prompt content or job success."""

from __future__ import annotations

import asyncio

import pytest

from tests.company_os_harness import load_company_os_app
from tests.unit.gtm.test_journey import journey_db as journey_db


def test_authenticated_dispatch_records_closed_tool_and_feature(monkeypatch):
    load_company_os_app()
    from app.analytics import chat_usage
    from app.chat import execute

    recorded = []
    monkeypatch.setattr(chat_usage, "emit", lambda name, **kwargs: recorded.append((name, kwargs)))

    async def handler(_venture_id, _action_name, *, payload, actor_subject=None):
        assert payload == {"query": "private founder text"}
        assert actor_subject is None
        return {"kind": "text", "body_md": "private answer"}

    monkeypatch.setattr(execute, "_execute_action", handler)
    with chat_usage.attributed_chat_action("person-1", "workspace-1", "venture-1"):
        asyncio.run(execute.execute_action("venture-1", "research", payload={"query": "private founder text"}))
    assert [name for name, _ in recorded] == ["chat.tool_invoked", "feature.used"]
    assert recorded[0][1]["props"] == {"source": "chat", "tool": "research", "feature": "research", "outcome": "returned"}
    assert recorded[1][1]["props"] == {"source": "chat", "feature": "research", "tool": "research"}
    assert all("private" not in repr(row) for row in recorded)

    asyncio.run(execute.execute_action("venture-1", "research", payload={"query": "private founder text"}))
    assert len(recorded) == 2  # no authenticated request context


@pytest.mark.parametrize("audience", ["Warehouse stock planning for distributors", "Weekend hiking for families"])
def test_draft_rewrite_is_registered_dispatched_and_attributed(journey_db, monkeypatch, audience):
    from app.analytics import chat_usage
    from app.analytics.feature_catalog import ACTION_SLUG
    from app.chat import customers, execute
    from app.chat.actions import disabled_reason, get_action
    from app.db.models import Venture

    data = journey_db
    from app.core.local_mode import is_local
    if is_local():
        from app.core.providers_local import save_settings
        save_settings(provider="openai", base_url="https://provider.example.invalid/v1",
                      models={"chat": "gpt-6.1-sol"}, api_key="fixture-model-key")
    with data["factory"]() as session:
        session.get(Venture, data["vid"]).raw_idea = audience
        session.commit()
    action = get_action("gtm.draft_rewrite")
    assert action.endpoint == "gtm.journey.rewrite_drafts"
    assert action.governed_capability == "cap.write.work_start"
    assert action.required_permission == "request_work"
    assert disabled_reason(action.name) is None
    assert ACTION_SLUG[action.name] == "outreach"
    assert chat_usage.ACTION_FEATURE[action.name] == "gtm"
    recorded = []
    called = []
    payload = {"numbers": [1, 2]}
    monkeypatch.setattr(chat_usage, "emit", lambda name, **kwargs: recorded.append((name, kwargs)))

    async def rewrite(venture_id, action_name, request, actor):
        called.append((venture_id, action_name, request, actor))
        return {"kind": "text", "body_md": f"Your drafts for {audience} need review. Nothing was sent."}

    monkeypatch.setattr(customers, "execute", rewrite)
    with chat_usage.attributed_chat_action(data["actor"], data["wid"], data["vid"]):
        reply = asyncio.run(execute.execute_action(data["vid"], action.name, payload=payload,
                                                  actor_subject=data["actor"]))
    assert called == [(data["vid"], action.name, payload, data["actor"])]
    assert audience in reply["body_md"]
    assert [name for name, _ in recorded] == ["chat.tool_invoked", "feature.used"]
    expected = {"source": "chat", "tool": action.name, "feature": "gtm"}
    assert recorded[0][1] == {"person_id": data["actor"], "workspace_id": data["wid"], "venture_id": data["vid"],
                              "props": {**expected, "outcome": "returned"}}
    assert recorded[1][1]["props"] == expected
    assert audience not in repr(recorded) and "numbers" not in repr(recorded)


def test_failure_and_unimplemented_action_do_not_claim_feature_use(monkeypatch):
    load_company_os_app()
    from app.analytics import chat_usage
    from app.chat import execute

    recorded = []
    monkeypatch.setattr(chat_usage, "emit", lambda name, **kwargs: recorded.append((name, kwargs)))

    async def failed(_venture_id, _action_name, *, payload, actor_subject=None):
        assert actor_subject is None
        raise RuntimeError("private failure detail")

    monkeypatch.setattr(execute, "_execute_action", failed)
    with chat_usage.attributed_chat_action("person-1", "workspace-1", "venture-1"):
        with pytest.raises(RuntimeError):
            asyncio.run(execute.execute_action("venture-1", "research"))
        with pytest.raises(RuntimeError):
            asyncio.run(execute.execute_action("venture-1", "idea.confirm"))
        with pytest.raises(RuntimeError):
            asyncio.run(execute.execute_action("other-venture", "research"))
    # A registered action outside the closed feature map is still counted as a
    # tool call (feature "chat", never a feature use); an other-venture call is not.
    assert [name for name, _ in recorded] == ["chat.tool_invoked", "chat.tool_invoked"]
    assert recorded[0][1]["props"]["outcome"] == "raised"
    assert recorded[1][1]["props"] == {"source": "chat", "tool": "idea.confirm", "feature": "chat", "outcome": "raised"}
    assert "private failure detail" not in repr(recorded)


def test_closed_mapping_only_names_registered_actions():
    load_company_os_app()
    from app.analytics.chat_usage import ACTION_FEATURE
    from app.chat.actions import REGISTRY

    assert set(ACTION_FEATURE) <= set(REGISTRY)
    assert ACTION_FEATURE["idea.answer"] == "idea"
    assert "idea.confirm" in REGISTRY and "idea.confirm" not in ACTION_FEATURE
    for name in ("site.publish", "waitlist.enable", "plan.quote", "plan.resources_none", "build.prepare",
                 "build.app.create", "work.start", "experiment.start", "experiment.record", "mac.task.prepare",
                 "resume", "blocker.explain", "job.retry", "outreach.draft"):
        assert name in ACTION_FEATURE, name


def test_saved_direct_gtm_read_counts_both_chat_route_and_gtm_without_prompt(monkeypatch):
    load_company_os_app()
    from app.analytics import chat_usage

    recorded = []
    monkeypatch.setattr(chat_usage, "emit", lambda name, **kwargs: recorded.append((name, kwargs)))
    with chat_usage.attributed_chat_action("person-1", "workspace-1", "venture-1"):
        chat_usage.record_served_chat_read("venture-1", "gtm_review")
        chat_usage.record_served_chat_read("venture-1", "open_screen.analytics_read")
        chat_usage.record_served_chat_read("venture-1", "start_product_build")
        chat_usage.record_served_chat_read("other-venture", "gtm_review")
    assert [name for name, _ in recorded] == ["chat.tool_invoked", "feature.used"] * 2
    assert recorded[0][1]["props"] == {"source": "chat", "tool": "gtm_review",
                                       "feature": "gtm", "outcome": "returned"}
    assert recorded[1][1]["props"] == {"source": "chat", "tool": "gtm_review",
                                       "feature": "gtm"}
    assert recorded[3][1]["props"] == {"source": "chat", "tool": "open_screen.analytics_read",
                                       "feature": "analytics"}
    assert all(row[1]["person_id"] == "person-1" for row in recorded)


@pytest.mark.parametrize("raises", [False, True])
def test_unpublish_dispatch_has_website_usage_without_claiming_hosting_success(monkeypatch, raises):
    load_company_os_app()
    from app.analytics import chat_usage
    from app.chat import execute

    recorded = []
    monkeypatch.setattr(chat_usage, "emit", lambda name, **kwargs: recorded.append((name, kwargs)))
    payload = {"app_id": "private-app", "deployment_id": "private-deployment", "version": 7}

    async def handler(venture_id, action_name, *, payload, actor_subject=None):
        assert venture_id == "venture-1" and action_name == "site.unpublish"
        assert payload["deployment_id"] == "private-deployment" and actor_subject == "person-1"
        if raises:
            raise RuntimeError("private provider detail")
        return {"kind": "text", "body_md": "Unpublishing is queued; offline is not yet verified."}

    monkeypatch.setattr(execute, "_execute_action", handler)
    with chat_usage.attributed_chat_action("person-1", "workspace-1", "venture-1"):
        if raises:
            with pytest.raises(RuntimeError):
                asyncio.run(execute.execute_action("venture-1", "site.unpublish", payload=payload, actor_subject="person-1"))
        else:
            reply = asyncio.run(execute.execute_action("venture-1", "site.unpublish", payload=payload, actor_subject="person-1"))
            assert "not yet verified" in reply["body_md"]
    assert [name for name, _ in recorded] == (["chat.tool_invoked"] if raises else ["chat.tool_invoked", "feature.used"])
    assert recorded[0][1] == {"person_id": "person-1", "workspace_id": "workspace-1", "venture_id": "venture-1",
        "props": {"source": "chat", "tool": "site.unpublish", "feature": "website", "outcome": "raised" if raises else "returned"}}
    if not raises:
        assert recorded[1][1]["props"] == {"source": "chat", "feature": "website", "tool": "site.unpublish"}
    assert "private" not in repr(recorded) and "offline" not in repr(recorded)
