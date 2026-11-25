"""Executable choices share Website admission and save only private previews."""
from __future__ import annotations

import asyncio
import json
import math

import pytest

from tests.unit.pagebuilder.test_api import setup as pagebuilder_setup


@pytest.fixture(name="setup")
def choices_setup(pagebuilder_setup, monkeypatch):
    from app.db.supymem_models import ResearchDossier, ResearchFinding
    from app.suggestions import proposals
    from core.clock import utcnow

    data = pagebuilder_setup
    with data["factory"]() as session:
        now = utcnow()
        session.add(ResearchDossier(id="choice_research", workspace_id=data["wid"], venture_id=data["vid"],
            revision_id=data["rid"], kind="deep", state="complete",
            summary=json.dumps({"review": {"overview": "Saved market alternatives."}}),
            created_at=now, completed_at=now))
        session.add(ResearchFinding(id="choice_source", dossier_id="choice_research", workspace_id=data["wid"],
            venture_id=data["vid"], category="source", title="Public alternatives", source="web",
            url="https://example.test/alternatives", evidence=json.dumps({"evidence_kind": "web", "observed_at": now.isoformat()}),
            created_at=now))
        session.commit()
    monkeypatch.setattr(proposals, "god_actions", lambda: None)
    return data


def cards(data):
    from app.suggestions import service
    from app.tenancy import bind

    with bind(workspace_id=data["wid"], venture_id=data["vid"], actor=data["actor"]):
        return {service.get(identifier).action_kind: identifier for identifier in service.generate(data["vid"])}


def test_choice_offers_both_real_completing_capabilities(setup, monkeypatch):
    from app.autopilot import actions
    from app.pagebuilder import api
    from app.suggestions import proposals, service
    from app.tenancy import bind
    from app.ventures.next_step import next_step

    data = setup
    with bind(workspace_id=data["wid"], venture_id=data["vid"], actor=data["actor"]):
        step = next_step(data["vid"])
        assert step["key"] == "site.build" and not step.get("chat_action")
        assert {choice["choice"] for choice in step["alternatives"]} == {"build", "skip"}
    monkeypatch.setattr(proposals, "god_actions", lambda: actions)
    offered = cards(data)
    assert set(offered) == {"pagebuilder.build", "outreach.draft"}
    row = service.get(offered["pagebuilder.build"])
    assert row.title == "Build a private landing page preview"
    assert row.est_credits == math.ceil(api.estimate(data["vid"], data["actor"])["credits"])
    assert service.public(row)["result_href"] == f'/v/{data["vid"]}/site'
    assert service.generate(data["vid"]) == []


def test_outreach_choice_without_people_offers_customer_search_not_empty_drafts(setup):
    from app.autopilot.outreach import drafts
    from app.gtm import journey
    from app.suggestions import service

    data = setup
    identifier = cards(data)["outreach.draft"]
    result = asyncio.run(service.accept(identifier, data["actor"]))
    assert result["result_state"] == "needs_you"
    assert result["result_message"].startswith("No people to write to yet.")
    assert result["result_href"] == f'/gtm?venture={data["vid"]}&step=review'
    saved = drafts(data["vid"])
    assert saved == []
    assert journey.status(data["wid"], data["vid"], authorize=False)["drafts"] == []


@pytest.mark.parametrize("change", ["quote", "credits", "idea", "research", "running", "ready"])
@pytest.mark.parametrize("operation", ["list", "accept"])
def test_choice_rechecked_before_list_and_accept(setup, monkeypatch, change, operation):
    from app.db.models import CreditAccount, Job
    from app.db.supymem_models import IdeaSession, ResearchDossier
    from app.pagebuilder import api
    from app.suggestions import service

    data = setup
    identifier = cards(data)["pagebuilder.build"]
    if change == "quote":
        original = api.resources
        monkeypatch.setattr(api, "resources", lambda *args: {**original(*args), "resource_digest": "changed"})
    elif change in {"running", "ready"}:
        job_id = api.build(data["vid"], data["actor"])["job_id"]
        if change == "ready":
            monkeypatch.setattr(api, "_candidate_current", lambda doc: True)
            with data["factory"]() as session:
                job = session.get(Job, job_id)
                payload = json.loads(job.payload_json)
                payload["_runtime"]["state"] = "ready"
                job.status, job.payload_json = "succeeded", api.dump(payload)
                session.commit()
    else:
        with data["factory"]() as session:
            if change == "credits":
                account = session.get(CreditAccount, data["account"].id)
                account.granted_micro = account.spent_micro
            elif change == "idea":
                session.get(IdeaSession, data["sid"]).state = "reviewing"
            else:
                session.get(ResearchDossier, "choice_research").state = "pending"
            session.commit()
    monkeypatch.setattr(service, "_execute_accepted", lambda *_: pytest.fail("stale preview was executed"))
    if operation == "list":
        assert identifier not in {item["id"] for item in service.list_for({data["wid"]})["items"]}
        assert service.get(identifier).status == "expired"
    else:
        assert asyncio.run(service.accept(identifier, data["actor"]))["result_state"] == "noop"


@pytest.mark.parametrize("state", ["ready", "queued", "invalid", "failed"])
def test_preview_accept_queues_existing_builder_and_waits_for_private_result(setup, monkeypatch, state):
    from app.db.models import Job, SiteVersion
    from app.pagebuilder import api
    from app.suggestions import service

    data = setup
    identifier = cards(data)["pagebuilder.build"]
    result = asyncio.run(service.accept(identifier, data["actor"]))
    assert result["result_state"] == "running"
    assert "Nothing is published" in result["result_message"]
    job_id, = json.loads(service.get(identifier).result_ref)["jobs"]
    assert "pagebuilder.build" not in cards(data)
    with data["factory"]() as session:
        job = session.get(Job, job_id)
        payload = json.loads(job.payload_json)
        assert job.kind == api.KIND and payload["variant_count"] == 1
        assert payload["revision_id"] == data["rid"] and payload["_actor_subject"] == data["actor"]
        assert payload["_runtime"]["quote"]["ceiling_micro"] <= service.get(identifier).est_credits * 10000
        payload["_runtime"]["state"] = "ready" if state in {"ready", "invalid"} else state
        job.status, job.payload_json = "succeeded", api.dump(payload)
        session.commit()
    if state == "ready":
        monkeypatch.setattr(api, "_candidate_current", lambda doc: True)
    service._reconcile({data["vid"]})
    final = service.public(service.get(identifier))
    assert final["result_state"] == ("done" if state == "ready" else "needs_you")
    assert final["result_href"] == f'/v/{data["vid"]}/site'
    if state == "ready":
        assert final["result_message"] == "Private landing page preview ready"
    with data["factory"]() as session:
        assert not session.query(SiteVersion).filter_by(venture_id=data["vid"], published=True).first()


def test_preview_execution_rechecks_exact_quote_and_requires_human_approval(setup, monkeypatch):
    from fastapi import HTTPException
    from app.autopilot import guard
    from app.chat.execute import execute_action
    from app.pagebuilder import api

    data = setup
    quote = api.estimate(data["vid"], data["actor"])
    assert guard.check(data["vid"], "pagebuilder.build", quote, settings={"god_mode": True}).reason == "action_not_allowed"
    monkeypatch.setattr(api, "_start", lambda *args, **kwargs: pytest.fail("changed quote admitted"))
    for payload in ({}, {**quote, "ceiling_micro": 0}, {**quote, "quote_fingerprint": "a" * 64}):
        with pytest.raises(HTTPException) as error:
            asyncio.run(execute_action(data["vid"], "pagebuilder.build", payload=payload, actor_subject=data["actor"]))
        assert error.value.detail["code"] == "pagebuilder_quote_changed"


def test_quote_is_rechecked_inside_admission_before_reusing_a_job(setup, monkeypatch):
    from fastapi import HTTPException
    from app.chat.execute import execute_action
    from app.pagebuilder import api

    data = setup
    quote = api.estimate(data["vid"], data["actor"])
    job = api.build(data["vid"], data["actor"])
    resources, calls = api.resources, []
    def changing_resources(*args):
        calls.append(True)
        result = resources(*args)
        return result if len(calls) == 1 else {**result, "resource_digest": "changed_during_admission"}
    monkeypatch.setattr(api, "resources", changing_resources)
    with pytest.raises(HTTPException) as error:
        asyncio.run(execute_action(data["vid"], "pagebuilder.build", payload=quote, actor_subject=data["actor"]))
    assert error.value.detail["code"] == "pagebuilder_quote_changed"
    assert api.status(data["vid"], data["actor"], job["job_id"])["state"] == "queued"


def test_approved_preview_finishes_with_real_private_worker_artifacts(setup, monkeypatch):
    from app.builder.artifact_store import S3ArtifactStore
    from app.core.jobs import lease
    from app.pagebuilder import api
    from app.pagebuilder.worker import run_one
    from app.suggestions import service
    from tests.unit.pagebuilder.test_api import install_pipeline_stub
    from tests.unit.pagebuilder.test_native_reference import Store

    data = setup
    store = Store()
    monkeypatch.setattr(S3ArtifactStore, "configured", lambda: store)
    install_pipeline_stub(monkeypatch)
    identifier = cards(data)[api.KIND]
    assert asyncio.run(service.accept(identifier, data["actor"]))["result_state"] == "running"
    with data["factory"]() as session:
        job, = lease(session, "suggestion-preview-worker", kinds=[api.KIND], n=1)
        session.commit()
    asyncio.run(run_one(data["factory"], job))
    preview = api.result(data["vid"], data["actor"], job.id)
    assert preview["state"] == "ready" and preview["publish_ready"] is False
    assert preview["source_url"] and preview["variants"][0]["screenshots"]
    service._reconcile({data["vid"]})
    result = service.public(service.get(identifier))
    assert result["result_state"] == "done" and result["result_message"] == "Private landing page preview ready"
