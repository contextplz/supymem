"""v9 founder journey: research -> one-click page -> customers; the plan is optional and never loops."""
import json
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from tests.company_os_harness import load_company_os_app
from tests.unit.pagebuilder import test_api

setup = test_api.setup

FACTS = dict(workspace_id="w1", gtm_entry=False, active_jobs=False, idea_state="confirmed", idea_confirmed=True,
             personal=False, plan_state=None, dossier_state="complete", research_report_ready=True, products=0, accepted_build=False,
             accepted_landing=False, product_attempt=False, accepted_product=False, page_build=None)


@pytest.fixture
def ns(monkeypatch, tmp_path):
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    load_company_os_app()
    from app.core.local_secrets import update_settings
    from app.core.providers_local import save_settings
    from app import coding_agents_consent
    from desktop_runtime import agent_status
    from app.chat import execute
    from app.planning import store
    from app.ventures import next_step, state_brief

    save_settings(provider="openai_compatible", base_url="http://127.0.0.1:1234", models={"chat": "test-model"})
    update_settings("capabilities", {"landing_pages": True}, {})
    monkeypatch.setattr(coding_agents_consent, "consents", lambda actor: {"codex": True})
    monkeypatch.setattr(agent_status, "statuses", lambda: [{"name": "codex", "installed": True,
        "signed_in": True, "subscription_login": True, "available": True}])
    monkeypatch.setattr(execute, "research_needs_start", lambda _v: True)
    monkeypatch.setattr(store, "list_current_tasks", lambda _v: [])
    monkeypatch.setattr(state_brief, "_website_section", lambda *_a: {"live": False, "waitlist_enabled": False})
    return next_step


def founder(ns, **over):
    return ns._resolve("v1", "/v/v1", {**FACTS, **over}, page_first=True)


@pytest.mark.parametrize("audience", ["warehouse dispatchers", "weekend gardeners"])
@pytest.mark.parametrize("report_ready", [False, True])
def test_failed_page_does_not_claim_missing_research(monkeypatch, audience, report_ready):
    load_company_os_app()
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    from app.pagebuilder import api
    from app.ventures import next_step
    monkeypatch.setattr(api, "enabled", lambda: True)
    facts = {**FACTS, "idea_summary": audience, "page_build": "failed", "research_report_ready": report_ready}
    step = next_step._page_step("v1", "/v/v1", facts)
    assert step
    assert ("research are saved" in step["detail"]) is report_ready
    assert "Website" in step["detail"] and "Retry" in step["detail"]
    from app.tasks.contracts import blocker
    stopped = blocker("pagebuilder_tool_build_stopped:" + audience)
    assert "research are saved" not in str(stopped)
    assert "Website" in stopped["unblock_action"] and "Retry" in stopped["unblock_action"]


def test_page_is_one_click_without_cost_then_customers_without_plan(ns):
    step = founder(ns)
    assert (step["key"], step["cta_label"], step["href"]) == ("site.build", "Build the landing page and explore customers", "/v/v1/site?build=1")
    assert not any(word in str(step).lower() for word in ("cost", "credit", "estimate", "approve"))
    again = founder(ns, page_build="failed")
    assert (again["key"], again["title"], again["href"]) == ("site.build", "Build your landing page again", "/v/v1/site?build=1")
    running = founder(ns, page_build="running", active_jobs=True)
    assert (running["key"], running["href"]) == ("site.building", "/v/v1/site")
    for plan in ({}, {"plan_state": "founder_review"}, {"plan_state": "draft", "plan_has_proposal": False}):
        nxt = founder(ns, page_build="ready", **plan)
        assert (nxt["key"], nxt["chat_action"]) == ("gtm.outreach", "outreach.draft"), plan


def test_automation_and_owner_switch_off_keep_the_plan_order(ns, monkeypatch):
    from app.core.local_secrets import update_settings
    auto = lambda **over: ns._resolve("v1", "/v/v1", {**FACTS, **over}, page_first=False)  # noqa: E731
    assert auto()["key"] == "plan.generate"
    assert auto(plan_state="founder_review")["key"] == "plan.review"
    assert auto(plan_state="draft", plan_has_proposal=False)["key"] == "plan.retry"
    monkeypatch.setenv("SUPYMEM_PAGEBUILDER_ENABLED", "0")
    assert founder(ns, page_build="ready", plan_state="founder_review")["key"] == "gtm.outreach"
    update_settings("capabilities", {"landing_pages": False}, {})
    assert founder(ns, page_build="ready", plan_state="founder_review")["key"] == "plan.review"


def test_research_only_path_stops_at_the_verdict(ns):
    step = ns._founder_resolve("v1", "/v/v1", {**FACTS, "intake_path": "research"})
    assert step["key"] == "research.review"
    assert ns._founder_resolve("v1", "/v/v1", {**FACTS, "intake_path": "research", "dossier_state": "partial",
                                                "page_build": "ready"})["key"] == "research.review"


@pytest.mark.parametrize("text,intent", [
    ("build my website", "pagebuilder.build"), ("build my page", "pagebuilder.build"),
    ("skip the plan and build my website", "pagebuilder.build"), ("make the landing page", "pagebuilder.build"),
    ("build my landing page and go to gtm", "pagebuilder.build"),
    ("get customers", "gtm_review"), ("find my first customers", "gtm_review"), ("go to gtm", "gtm_review"),
])
def test_chat_build_and_gtm_requests_never_route_to_plan(text, intent):
    load_company_os_app()
    from app.chat.intent import classify_intent
    if intent == "gtm_review":
        from app.entry import chat_start
        assert chat_start._classify(text, "v1")[0] == "gtm"
    else:
        assert classify_intent(text) == intent


def test_chat_build_reply_is_page_action_not_plan_review(ns, monkeypatch):
    from app.chat import router
    monkeypatch.setattr(ns, "_facts", lambda _v: {**FACTS})
    reply = router._one_click_page_reply("v1", "build my website and go to gtm")
    assert reply["actions"][0] == {"label": "Build your landing page", "action": "pagebuilder.build",
                                    "payload": {"prepare": True}, "style": "primary"}
    assert reply["actions"][1]["href"] == "/gtm?venture=v1"
    monkeypatch.setattr(ns, "_facts", lambda _v: {**FACTS, "idea_confirmed": False})
    assert router._one_click_page_reply("v1", "build my website") is None


def test_failed_jobs_name_one_clear_next_action(ns):
    from app.ventures.run_status import _failed_action
    assert _failed_action("research.deep", "/v/v1") == {"label": "Run research again", "href": "/v/v1/research"}
    assert _failed_action("plan.compile", "/v/v1") == {"label": "Build your landing page", "href": "/v/v1/site?build=1"}
    assert _failed_action("pagebuilder.build", "/v/v1")["href"] == "/v/v1/site?build=1"
    assert _failed_action("unknown.kind", "/v/v1")["label"] != "See error"


def test_saved_page_build_only_advances_its_confirmed_revision_and_workspace(setup, monkeypatch):
    from app.autopilot.authority import founder_scope
    from app.db.models import Job
    from app.db.supymem_models import IdeaRevision, IdeaSession, ResearchDossier, ResearchFinding
    from app.ventures import next_step, state_brief
    from core.clock import utcnow

    d, now = setup, utcnow()
    monkeypatch.setattr(state_brief, "_website_section", lambda *_a: {"live": False, "waitlist_enabled": False})
    with d["factory"]() as session:
        session.add(Job(id="page_old", kind="pagebuilder.build", workspace_id=d["wid"], venture_id=d["vid"],
                        payload_json=json.dumps({"revision_id": d["rid"]}), status="succeeded", created_at=now))
        session.commit()
    assert next_step._facts(d["vid"])["page_build"] == "ready"

    revised = "idv_corrected"
    with d["factory"]() as session:
        session.add(IdeaRevision(id=revised, session_id=d["sid"], workspace_id=d["wid"], venture_id=d["vid"],
            rev=2, source="founder", summary="Corrected idea", structured="{}", created_at=now))
        idea = session.get(IdeaSession, d["sid"])
        idea.current_revision_id = idea.confirmed_revision_id = revised
        session.add(ResearchDossier(id="research_corrected", workspace_id=d["wid"], venture_id=d["vid"],
            revision_id=revised, kind="deep", state="complete",
            summary=json.dumps({"review": {"overview": "Saved research for the corrected idea."}}), created_at=now))
        session.add(ResearchFinding(id="corrected_source", dossier_id="research_corrected", workspace_id=d["wid"],
            venture_id=d["vid"], category="source", title="Observed source", source="web", url="https://example.test/source",
            evidence=json.dumps({"observed_at": now.isoformat(), "evidence_kind": "web"}), created_at=now))
        session.add(Job(id="page_foreign", kind="pagebuilder.build", workspace_id="another-workspace", venture_id=d["vid"],
            payload_json=json.dumps({"revision_id": revised}), status="succeeded", created_at=now + timedelta(seconds=2)))
        session.commit()
    with founder_scope(d["vid"], d["actor"]):
        assert next_step._facts(d["vid"])["page_build"] is None
        assert next_step.next_step(d["vid"], page_first=True)["key"] == "site.build"

    with d["factory"]() as session:
        session.add(Job(id="page_current", kind="pagebuilder.build", workspace_id=d["wid"], venture_id=d["vid"],
            payload_json=json.dumps({"revision_id": revised}), status="scheduled", created_at=now + timedelta(seconds=1)))
        session.commit()
    with founder_scope(d["vid"], d["actor"]):
        assert next_step._facts(d["vid"])["page_build"] == "running"
        assert next_step.next_step(d["vid"], page_first=True)["key"] == "site.building"
    with d["factory"]() as session:
        session.get(Job, "page_current").status = "succeeded"
        session.commit()
    with founder_scope(d["vid"], d["actor"]):
        assert next_step._facts(d["vid"])["page_build"] == "ready"
        assert next_step.next_step(d["vid"], page_first=True)["key"] == "gtm.outreach"


def _client():
    from fastapi import FastAPI
    from app.api.pagebuilder_api import router
    app = FastAPI()
    app.include_router(router, prefix="/api")
    return TestClient(app)


def test_one_click_build_quotes_and_approves_in_one_request_with_capped_retries(setup):
    from app.db.models import Job
    from app.pagebuilder import api
    d, headers = setup, {"X-Subject": setup["actor"]}
    url = f"/api/ventures/{d['vid']}/pagebuilder"
    with _client() as client:
        profile = client.get(url + "/profile", headers=headers)
        assert profile.status_code == 200 and profile.json()["task_id"] is None  # no plan needed
        first = client.post(url + "/build", headers=headers)
        assert first.status_code == 202 and first.json()["variant_count"] == 1
        assert client.post(url + "/build", headers=headers).json()["job_id"] == first.json()["job_id"]
        jobs = {first.json()["job_id"]}
        # Failed attempts no longer spend a delivered version; the separate total-attempt ceiling still holds.
        for _ in range(api.ATTEMPTS_PER_REVISION - 1):
            with d["factory"]() as session:
                for job in session.scalars(select(Job).where(Job.kind == api.KIND)):
                    job.status = "failed_dead"
                session.commit()
            attempt = client.post(url + "/build", headers=headers)
            assert attempt.status_code == 202 and attempt.json()["state"] == "queued"
            jobs.add(attempt.json()["job_id"])
        with d["factory"]() as session:
            for job in session.scalars(select(Job).where(Job.kind == api.KIND)):
                job.status = "failed_dead"
            session.commit()
        capped = client.post(url + "/build", headers=headers)
        assert capped.status_code == 409 and capped.json()["detail"]["code"] == "pagebuilder_attempt_limit"
        with d["factory"]() as session:
            assert session.scalar(select(func.count()).select_from(Job).where(Job.kind == api.KIND)) == api.ATTEMPTS_PER_REVISION
        assert len(jobs) == api.ATTEMPTS_PER_REVISION
        assert client.post(url + "/build", headers={"X-Subject": "someone-else"}).status_code in {403, 404}


def test_known_clarification_codes_have_plain_copy():
    load_company_os_app()
    from app.chat.blockers import error_copy

    assert "usage limit" in error_copy("clarification_insufficient_credits")
    assert error_copy("clarification_context_limit")
