"""Confirmed choices change gates, never completion evidence or tenant scope."""
from __future__ import annotations

import asyncio
import pytest
from fastapi import HTTPException
from sqlalchemy import select

from tests.unit.gtm.test_journey import journey_db as base_journey_db


@pytest.fixture
def journey_db(base_journey_db):
    from app.core.local_mode import is_local

    if is_local():
        from app.core.local_secrets import update_settings
        from app.core.providers_local import save_settings

        save_settings(provider="openai_compatible", base_url="http://127.0.0.1:1234", models={"chat": "test-model"})
        update_settings("capabilities", {"background_work": True}, {})
    return base_journey_db


def _confirm(data, action, payload):
    from app.chat.execute import execute_action
    reply = asyncio.run(execute_action(data["vid"], action, payload=payload, actor_subject=data["actor"]))
    args = reply["actions"][0]["confirmation"]["resulting_state"]
    return asyncio.run(execute_action(data["vid"], action, payload=args, actor_subject=data["actor"])), args


@pytest.mark.parametrize("idea", ["Stock alerts for independent repair shops", "Planting reminders for home gardeners"])
def test_local_build_setup_keeps_a_next_step_without_starting_work(journey_db, idea):
    from app.chat.execute import execute_action
    from app.db.models import Job, Venture

    data = journey_db
    with data["factory"]() as session:
        session.get(Venture, data["vid"]).raw_idea = idea
        session.commit()
        before = set(session.scalars(select(Job.id).where(Job.kind != "idea.page_refresh")))
    reply = asyncio.run(execute_action(data["vid"], "build.prepare", actor_subject=data["actor"]))
    assert reply["body_md"] and reply["actions"]
    assert all(action.get("action") != "build.start" for action in reply["actions"])
    with data["factory"]() as session:
        assert set(session.scalars(select(Job.id).where(Job.kind != "idea.page_refresh"))) == before


@pytest.mark.parametrize("idea", ["Stock alerts for independent repair shops", "Planting reminders for home gardeners"])
def test_local_next_moves_with_every_action_blocked_opens_setup_without_starting_work(journey_db, monkeypatch, idea):
    from app import coding_agents_consent
    from app.chat import actions, next_moves
    from app.db.models import Job, Venture
    from desktop_runtime import agent_status

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setattr(coding_agents_consent, "consents", lambda actor: {"codex": True})
    monkeypatch.setattr(agent_status, "statuses", lambda: [{"name": "codex", "installed": True,
        "signed_in": True, "subscription_login": True, "available": True}])
    data = journey_db
    with data["factory"]() as session:
        session.get(Venture, data["vid"]).raw_idea = idea
        session.commit()
    _confirm(data, "journey.jump_to", {"step": "find_customers", "why": "Use the saved idea."})
    monkeypatch.setattr(actions, "disabled_reason", lambda action: "Open Settings > Keys and connections and save your model-provider key.")
    with data["factory"]() as session:
        before = set(session.scalars(select(Job.id).where(Job.kind != "idea.page_refresh")))
    reply = next_moves.compose(data["vid"])
    assert [action["href"] for action in reply["actions"]] == ["/settings/local"]
    assert "Settings > Keys and connections" in reply["body_md"] and "model-provider key" in reply["body_md"]
    assert not reply["citations"] and all("action" not in action for action in reply["actions"])
    with data["factory"]() as session:
        assert set(session.scalars(select(Job.id).where(Job.kind != "idea.page_refresh"))) == before


def test_jump_records_skips_and_unblocks_without_research_or_sends(journey_db):
    from app.core.local_mode import is_local
    from app.chat.execute import execute_action
    from app.db.models import Job
    from app.db.supymem_models import IdeaSession
    from app.gtm.journey import _icp
    from app.research.dossier import load_validation_dossier
    from app.ventures.journey_overrides import read
    from app.ventures.next_step import next_step
    data = journey_db
    _confirm(data, "venture.set_website", {"url": "https://bakery.example", "why": "I already have a website."})
    card = asyncio.run(execute_action(data["vid"], "journey.jump_to", actor_subject=data["actor"],
        payload={"step": "find_customers", "why": "I insist on finding customers without research."}))
    assert not read(data["vid"])["skipped"], "A card alone cannot skip anything"
    args = card["actions"][0]["confirmation"]["resulting_state"]
    result = asyncio.run(execute_action(data["vid"], "journey.jump_to", payload=args, actor_subject=data["actor"]))
    choices = read(data["vid"])
    assert set(choices["skipped"]) == {"clarification", "validation", "research", "landing_page"}
    assert choices["skipped"]["research"]["who"] == data["actor"]
    assert choices["skipped"]["research"]["when"]
    assert "insist" in choices["skipped"]["research"]["why"]
    assert next_step(data["vid"])["key"] == ("venture.context" if is_local() else "gtm.outreach")
    assert "Lower confidence" in next_step(data["vid"])["detail"]
    assert _icp(data["vid"])["website_url"] == "https://bakery.example"
    assert "Lower confidence" in _icp(data["vid"])["problem_basis"]
    markdown, context = load_validation_dossier(data["vid"])
    assert markdown is None and context["research_skipped"]
    assert "bakery" in context["summary"].lower()
    with data["factory"]() as session:
        assert session.scalar(select(IdeaSession).where(IdeaSession.venture_id == data["vid"])).state == "confirmed"
        assert not session.scalar(select(Job.id).where(Job.venture_id == data["vid"], Job.kind != "idea.page_refresh"))
    assert asyncio.run(execute_action(data["vid"], "journey.jump_to", payload=args, actor_subject=data["actor"])) == result
    assert len(read(data["vid"])["history"]) == 2, "Confirmation retry is idempotent"
    _confirm(data, "journey.restore_step", {"step": "validation", "why": "I want to validate after all."})
    assert "validation" not in read(data["vid"])["skipped"]
    with data["factory"]() as session:
        latest = session.scalar(select(IdeaSession).where(IdeaSession.venture_id == data["vid"])
                                .order_by(IdeaSession.created_at.desc()))
        assert latest.state == "reviewing", "Restored validation needs a real review, not fabricated completion"
    assert next_step(data["vid"])["key"] == ("venture.context" if is_local() else "idea.review_validation")
    _confirm(data, "journey.restore_step", {"step": "clarification", "why": "Restore questions too."})
    with data["factory"]() as session:
        latest = session.scalar(select(IdeaSession).where(IdeaSession.venture_id == data["vid"])
                                .order_by(IdeaSession.created_at.desc()))
        assert latest.state == "clarifying"


def test_override_rejects_wrong_workspace_and_tampered_confirmation(journey_db):
    from app.chat.execute import execute_action
    from app.tenancy import bind
    from app.ventures.journey_overrides import read
    data = journey_db
    with bind(workspace_id="another-workspace", venture_id=data["vid"], actor=data["actor"]):
        with pytest.raises(PermissionError, match="scope_mismatch"):
            asyncio.run(execute_action(data["vid"], "journey.skip_step", actor_subject=data["actor"],
                payload={"step": "research", "why": "skip"}))
    with pytest.raises(HTTPException) as denied:
        asyncio.run(execute_action(data["vid"], "journey.skip_step", actor_subject="usr_intruder",
            payload={"step": "research", "why": "skip"}))
    assert denied.value.status_code == 404
    with pytest.raises(KeyError):
        asyncio.run(execute_action(data["vid"], "journey.skip_step", actor_subject=data["actor"],
            payload={"proposal_id": "forged"}))
    assert not read(data["vid"])["skipped"]


@pytest.mark.parametrize("payload", [
    {"step": "payments", "why": "skip"}, {"step": "research", "why": ""},
    {"step": "research", "why": "skip", "workspace_id": "other"},
])
def test_invalid_skip_input(journey_db, payload):
    from app.chat.execute import execute_action
    with pytest.raises(ValueError):
        asyncio.run(execute_action(journey_db["vid"], "journey.skip_step", actor_subject=journey_db["actor"], payload=payload))


def test_skip_clarification_preserves_validation_then_restore(journey_db):
    from app.db.supymem_models import IdeaSession
    from app.ventures.journey_overrides import read
    data = journey_db
    _confirm(data, "journey.skip_step", {"step": "clarification", "why": "Use the existing summary."})
    with data["factory"]() as session:
        idea = session.scalar(select(IdeaSession).where(IdeaSession.venture_id == data["vid"]))
        assert idea.state == "reviewing" and not idea.confirmed_revision_id
    assert "validation" not in read(data["vid"])["skipped"]
    _confirm(data, "journey.restore_step", {"step": "clarification", "why": "Ask me the questions."})
    assert not read(data["vid"])["skipped"]


def test_skipping_validation_alone_advances_only_a_clarified_idea(journey_db):
    from app.db.supymem_models import IdeaSession
    from app.ventures.journey_overrides import read
    data = journey_db
    _confirm(data, "journey.skip_step", {"step": "clarification", "why": "Use summary."})
    _confirm(data, "journey.restore_step", {"step": "clarification", "why": "Finish questions first."})
    _confirm(data, "journey.skip_step", {"step": "validation", "why": "No validation."})
    with data["factory"]() as session:
        idea = session.scalar(select(IdeaSession).where(IdeaSession.venture_id == data["vid"])
                              .order_by(IdeaSession.updated_at.desc()))
        assert idea.state == "clarifying", "Skipping validation must not silently skip unfinished clarification"
        idea.state = "reviewing"
        session.commit()
    _confirm(data, "journey.skip_step", {"step": "validation", "why": "Use the now clarified summary."})
    with data["factory"]() as session:
        idea = session.scalar(select(IdeaSession).where(IdeaSession.venture_id == data["vid"])
                              .order_by(IdeaSession.updated_at.desc()))
        assert idea.state == "confirmed"
    assert "clarification" not in read(data["vid"])["skipped"]


def test_skipping_only_research_does_not_claim_unfinished_idea_is_done(journey_db):
    from app.ventures.next_step import _founder_resolve
    data = journey_db
    _confirm(data, "journey.skip_step", {"step": "research", "why": "No research."})
    facts = {"idea_confirmed": False, "idea_state": "clarifying", "active_jobs": False,
        "gtm_entry": False, "personal": False, "workspace_id": data["wid"],
        "dossier_state": None, "plan_state": None, "journey_overrides": {"skipped": {"research": {}}, "target": "landing_page"}}
    assert _founder_resolve(data["vid"], f"/v/{data['vid']}", facts)["key"] == "idea.clarify"


def test_skipped_research_does_not_enqueue_on_idea_confirmation(journey_db):
    from app.db.models import Job
    from app.db.supymem_models import IdeaSession
    from app.idea.service import confirm
    data = journey_db
    _confirm(data, "journey.skip_step", {"step": "clarification", "why": "Use my summary."})
    _confirm(data, "journey.skip_step", {"step": "research", "why": "No paid research."})
    with data["factory"]() as session:
        idea = session.scalar(select(IdeaSession).where(IdeaSession.venture_id == data["vid"]))
        sid, rid = idea.id, idea.current_revision_id
    result = confirm(sid, revision_id=rid, person_id=data["actor"])
    assert result["dossier_job_id"] is None
    with data["factory"]() as session:
        assert not session.scalar(select(Job.id).where(Job.venture_id == data["vid"], Job.kind == "research.deep"))


def test_chat_parsing_keeps_quoted_or_negated_skips_inert(journey_db):
    from app.ventures.journey_overrides import parse
    assert parse("skip research because I already know the market")[0] == "journey.skip_step"
    assert parse("skip to find customers")[0] == "journey.jump_to"
    assert parse("restore research")[0] == "journey.restore_step"
    assert parse("I already have a website: https://bakery.example")[0] == "venture.set_website"
    assert parse('"skip research"') is None
    assert parse("do not skip research") is None


def test_chat_recap_never_calls_skipped_checks_completed(journey_db):
    from app.chat.journey import _done_list
    assert _done_list({"idea": {"confirmed": True}, "research": {"present": True},
        "next_step": {"journey_overrides": {"skipped": {"clarification": {}, "validation": {}, "research": {}}}}}) == []


def test_website_rejects_credentials_and_script_urls(journey_db):
    from app.chat.execute import execute_action
    for url in ("javascript:alert(1)", "https://user:secret@example.com", "https://example.com bad"):
        with pytest.raises(ValueError, match="invalid_website"):
            asyncio.run(execute_action(journey_db["vid"], "venture.set_website", actor_subject=journey_db["actor"],
                payload={"url": url, "why": "existing website"}))


@pytest.mark.parametrize("audience,problem", [
    ("Independent hardware repair shops in Germany", "Schedule repair appointments"),
    ("Home gardeners in Canada", "Track seasonal planting routines"),
])
def test_discovery_worker_accepts_overridden_current_context_not_stale_revision(journey_db, audience, problem):
    from app.core.jobs import lease
    from app.db.models import Job
    from app.db.supymem_models import IdeaSession
    from app.gtm import journey
    from app.idea.service import append_revision
    data = journey_db
    _confirm(data, "journey.jump_to", {"step": "find_customers", "why": "Find customers with existing context."})
    _confirm(data, "journey.restore_step", {"step": "validation", "why": "Validate separately later."})
    journey.update_icp(data["wid"], data["vid"], audience, problem=problem, recruitment_opt_in=True)
    with data["factory"]() as session:
        jobs = lease(session, "override-worker", kinds=["gtm.journey.prepare"], n=1)
        assert jobs
        job = jobs[0]
        payload = {**job.payload, "workspace_id": data["wid"], "venture_id": data["vid"],
            "_job_id": job.id, "_worker_id": job.worker_id, "_job_attempt": job.attempts}
        session.commit()
    with data["factory"]() as session:
        assert journey._prepare_fence(session, payload)[1]["icp"]["confirmed"]
        idea = session.scalar(select(IdeaSession).where(IdeaSession.venture_id == data["vid"])
                              .order_by(IdeaSession.updated_at.desc()))
        append_revision(session, idea, {"problem": "A corrected problem"}, source="founder", summary="Correction")
        session.commit()
    with data["factory"]() as session:
        assert session.get(Job, payload["_job_id"]).status == "leased"
        assert journey._prepare_fence(session, payload)[1]["icp"]["problem"] == problem


STEPS = ("clarification", "validation", "research", "landing_page", "find_customers")


@pytest.mark.parametrize("source,target", [(source, target) for position, source in enumerate(STEPS)
                                          for target in STEPS[position + 1:]])
@pytest.mark.parametrize("saved_context", ["empty", "website", "idea"])
def test_every_skip_to_later_step_has_usable_downstream_responses(journey_db, monkeypatch, source, target, saved_context):
    from types import SimpleNamespace
    from app.api import research_api, tasks_api
    from app.chat import context, ground, memory_commands, plan_chain, router, store
    from app.chat.execute import execute_action
    from app.core.local_mode import is_local
    from app.db.models import Venture
    from app.gtm import journey
    from app.memory import task_context
    from app import memory
    from app.pagebuilder.api import snapshot as page_snapshot
    from app.planning import execution, summarize
    from app.state import MEM
    from app.ventures.current_idea import current_idea
    from app.ventures.journey_overrides import read
    from app.ventures.next_step import next_step
    from app.ventures.state_brief import get_venture_state_brief

    from app.ventures import founder_memory, state_brief
    monkeypatch.setattr(state_brief, "MEM", MEM)

    async def missing_memory(*args, **kwargs):
        raise RuntimeError("not ingested")

    missing = SimpleNamespace(name="postgres", answer=missing_memory, context_pack=missing_memory, recall=missing_memory)
    monkeypatch.setattr(ground, "get_memory", lambda: missing)
    monkeypatch.setattr(task_context, "get_memory", lambda: missing)
    monkeypatch.setattr(founder_memory, "get_memory", lambda: missing)
    monkeypatch.setattr(memory, "get_memory", lambda: missing)
    monkeypatch.setattr(journey, "_icp_seed", lambda vid: {"idea": {}, "research": {}})
    data = journey_db
    with data["factory"]() as session:
        session.get(Venture, data["vid"]).raw_idea = ""
        session.commit()
    for key, value in (("org", []), ("charter", []), ("questions", [None, {}]), ("answers", [])):
        MEM[f"{key}:{data['vid']}"] = value
    if saved_context == "idea":
        with data["factory"]() as session:
            session.get(Venture, data["vid"]).raw_idea = "Keep bakery orders in one place"
            session.commit()
    if saved_context == "website":
        _confirm(data, "venture.set_website", {"url": "https://bakery.example", "why": "Use my existing site."})
    _confirm(data, "journey.skip_step", {"step": source, "why": f"Skip {source} without prerequisites."})
    _confirm(data, "journey.jump_to", {"step": target, "why": f"I insist on {target}."})
    assert read(data["vid"])["target"] == target
    step = next_step(data["vid"])
    assert step["href"] and step["detail"] and "Lower confidence" in step["detail"]
    ctx = context.assemble(data["vid"])
    assert ctx.limits
    if saved_context != "idea":
        assert not ctx.venture["raw_idea"], "Unknown scope must not become an invented idea"
    brief = get_venture_state_brief(data["vid"], force=True)
    assert brief["next_step"]["href"] and brief["journey_overrides"]["skipped"]
    meta = research_api.get_dossier_meta(data["vid"])
    assert not meta["available"] and meta["limitations"]
    assert "Research not run" in research_api.download_dossier_md(data["vid"]).body.decode()
    assert tasks_api.list_venture_tasks(data["vid"])["note"]
    MEM[summarize._cache_key(data["vid"], summarize._slug("Draft my next task"))] = {"body_md": "Queued for execution."}
    assert "draft work description" in asyncio.run(summarize.summarize_task(data["vid"], title="Draft my next task"))["body_md"]
    with data["factory"]() as session:
        inputs = execution.inputs(session, data["wid"], data["vid"], include_builder_runtime=False)
        assert inputs["context"]["research_gaps"] and inputs["dossier_id"] is None
        assert inputs["context"]["institutional_memory"]["availability"] == "unavailable"
        assert bool(current_idea(session.get(Venture, data["vid"]), session=session)) == (saved_context == "idea")
        if "validation" in read(data["vid"])["skipped"]:
            if is_local() and saved_context != "idea":
                with pytest.raises(HTTPException) as blocked_page:
                    page_snapshot(session, data["vid"], data["wid"])
                assert blocked_page.value.status_code == 422
                assert "Idea screen" in blocked_page.value.detail and "describe" in blocked_page.value.detail
            else:
                _, page = page_snapshot(session, data["vid"], data["wid"])
                assert page["research"]["limitations"] and page["idea"]
    actions = ("research", "research.start", "leads", "build.prepare", "why", "brain_brief", "work.start", "status")
    for action in actions:
        reply = asyncio.run(execute_action(data["vid"], action, actor_subject=data["actor"]))
        assert reply["body_md"] and reply.get("actions"), action
    answer = asyncio.run(ground.answer_from_memory(data["vid"], "Why did we choose this price?"))
    assert answer["abstained"] and not answer["citations"] and "memory is unavailable" in answer["body_md"]
    message = store.append_message(data["vid"], role="user", body_md="List saved venture memories")
    listed = asyncio.run(memory_commands.dispatch(data["vid"], message["id"]))
    if is_local():
        assert listed["body_md"] == memory_commands.STORAGE_RECOVERY
    else:
        assert listed["body_md"].startswith("Saved memory is unavailable;")
        assert "No memory was changed." in listed["body_md"] and memory_commands.HELP in listed["body_md"]
    assert not listed["citations"]
    prepared = journey.prepare(data["wid"], data["vid"])
    assert prepared["state"] == "needs_input" and prepared["message"] and not prepared["prospects"]
    drafts = journey.draft_for_review(data["vid"])
    assert drafts["body_md"].startswith("No people to write to yet.")
    assert drafts["actions"][0]["action"] == "gtm.find"
    status = journey.status(data["wid"], data["vid"])
    assert status["drafts"] == [] and status["results"]["sent"] == 0
    assert router._product_build_reply(data["vid"], brief, data["actor"])["actions"]
    assert plan_chain.next_task_reply(data["vid"], data["actor"])["body_md"]
    assert tasks_api.plan_tasks(data["vid"])["tasks"]
    card = asyncio.run(execute_action(data["vid"], "outreach.draft", payload={"run": False, "review_only": True}, actor_subject=data["actor"]))
    assert card["kind"] == "confirmation" and card["actions"][0]["confirmation"]["resulting_state"]["review_only"]
    _check_downstream_admission(data, monkeypatch, has_topic=saved_context != "empty")


@pytest.mark.parametrize("name,website", [("Warehouse stock alerts", "https://inventory.example"),
    ("Family hiking journal", "https://hiking.example")], ids=["b2b", "consumer"])
def test_local_page_brief_does_not_invent_your_idea_from_your_website(journey_db, monkeypatch, name, website):
    from app.db.models import Venture
    from app.pagebuilder.api import snapshot

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    data = journey_db
    with data["factory"]() as session:
        venture = session.get(Venture, data["vid"])
        venture.name, venture.raw_idea = name, ""
        session.commit()
    _confirm(data, "venture.set_website", {"url": website, "why": "Use my existing site."})
    _confirm(data, "journey.skip_step", {"step": "clarification", "why": "Skip questions."})
    _confirm(data, "journey.skip_step", {"step": "validation", "why": "Skip market checks."})
    with data["factory"]() as session:
        with pytest.raises(HTTPException) as blocked_page:
            snapshot(session, data["vid"], data["wid"])
    assert blocked_page.value.status_code == 422
    assert "Idea screen" in blocked_page.value.detail and "describe" in blocked_page.value.detail


def _check_downstream_admission(data, monkeypatch, *, has_topic):
    import json
    from app.core.local_mode import is_local
    from app.api import research_api, tasks_api
    from app.chat.execute import execute_action
    from app.core.jobs import complete_job, lease
    from app.db.models import CreditAccount, Job
    from app.db.supymem_models import IdeaSession
    from app.planning import compiler, execution
    from app.research import execution as research_execution

    if is_local():
        from app.ventures import startup_review

        current = startup_review.get(data["vid"])
        while current["question"]:
            current = startup_review.skip_question(data["vid"], current["revision"], current["question"]["field"])
        startup_review._mutate(data["vid"], current["revision"], "priorities", value="Review my build plan")

    before = tasks_api.list_venture_tasks(data["vid"])
    assert before["replan_available"]
    if not before["items"] and not before["running"]:  # the empty Work state always has one working action
        from app.ventures.next_step import founder_next_step
        assert before["empty_action"]["label"] in {founder_next_step(data["vid"])["cta_label"], "Review your plan"}
    replanned = tasks_api.replan_tasks(data["vid"])
    assert replanned["tasks"] and "Lower confidence" in replanned["note"]
    with data["factory"]() as session:
        idea = session.scalar(select(IdeaSession).where(IdeaSession.venture_id == data["vid"])
                              .order_by(IdeaSession.updated_at.desc(), IdeaSession.created_at.desc(), IdeaSession.id.desc()))
        state = idea.state if idea else None
    started = research_api.regenerate_dossier(data["vid"], research_api.ResearchStartBody(request_key="skip-research-click"))
    if not has_topic:
        assert started["kind"] == "needs_you" and started["actions"][0]["label"] == "Add research topic"
        assert started["actions"][0]["href"].endswith("/idea")
        with data["factory"]() as session:
            assert not session.scalar(select(Job.id).where(Job.kind == "research.deep"))
    else:
        assert started["job_id"]
        assert research_api.regenerate_dossier(data["vid"], research_api.ResearchStartBody(request_key="skip-research-click")) == started
        reply = asyncio.run(execute_action(data["vid"], "research.start", actor_subject=data["actor"]))
        assert reply["action_started"] and reply["run_id"] == started["job_id"]
        assert "I scheduled your research." in reply["body_md"]
        assert "Nothing is built or published." in reply["body_md"]
        with data["factory"]() as session:
            job = lease(session, "skip-research-worker", kinds=["research.deep"])[0]
            assert job.id == started["job_id"]
            payload = {**job.payload, "_job_id": job.id, "_worker_id": job.worker_id, "_job_attempt": job.attempts}
            session.commit()
        did = research_execution.claim(payload, kind="deep", budget=8)
        snapshot = json.loads(research_execution.read(did).summary)["_runtime"]["snapshot"]
        assert any("Lower confidence" in note for note in snapshot["limitations"])
        assert snapshot.get("summary") or snapshot.get("existing_site")
        with data["factory"]() as session:
            complete_job(session, job.id, worker_id=job.worker_id, attempt=job.attempts)
            session.commit()
    with data["factory"]() as session:
        idea = session.scalar(select(IdeaSession).where(IdeaSession.venture_id == data["vid"])
                              .order_by(IdeaSession.updated_at.desc(), IdeaSession.created_at.desc(), IdeaSession.id.desc()))
        assert idea.state == (state or "open"), "Starting research must not fabricate idea confirmation"
        revision_id = idea.current_revision_id

    requests = _plan_transport(monkeypatch)
    assert compiler.generation_quote(data["vid"], data["wid"])["sufficient_now"]
    started = compiler.compile_plan(data["vid"], data["wid"], revision_id)
    assert compiler.compile_plan(data["vid"], data["wid"], revision_id) == started
    with data["factory"]() as session:
        job = lease(session, "skip-plan-worker", kinds=["plan.compile"])[0]
        assert job.id == started["job_id"]
        payload = {**job.payload, "_job_id": job.id, "_worker_id": job.worker_id, "_job_attempt": job.attempts}
        session.commit()
    plan = compiler.run_compile(payload)
    assert plan["state"] == "founder_review" and plan["reviewable"] and not plan["stale_dependencies"]
    assert not plan["usage"]["has_unsettled"]
    if data["account"] is None:
        from app.credits import local_meter
        assert plan["usage"]["credits_micro"] == 0
        assert local_meter.snapshot(workspace_id=data["wid"], venture_id=data["vid"])["month_cost_usd_micro"] > 0
    else:
        assert plan["usage"]["credits_micro"] > 0
    assert len(requests) == 1
    assert compiler.run_compile(payload)["id"] == plan["id"] and len(requests) == 1
    with data["factory"]() as session:
        saved = json.loads(execution.read(plan["id"]).structured)
        assert saved["_runtime"]["operations"]["plan"]["state"] == "completed"
        if data["account"] is None:
            assert session.scalar(select(CreditAccount.id)) is None
        else:
            assert session.get(CreditAccount, data["account"]).reserved_micro == 0
    approved = compiler.approve_plan(plan["id"], person_id=data["actor"], review_token=plan["review_token"])
    assert approved["plan"]["state"] == "approved"
    assert tasks_api.replan_tasks(data["vid"])["plan_id"] == plan["id"]


def _plan_transport(monkeypatch, *, uncertain=False):
    import json
    from app.chat.recovery import _skeleton
    from app.core.local_mode import is_local
    from llm_gateway import provider_foundry, sole
    from openai import _base_client
    httpx = getattr(_base_client, "httpx", None) or _base_client.httpx2
    monkeypatch.setenv("LLM_CASSETTE_MODE", "off")
    for name, value in {"LLM_PROFILE": "prod", "LLM_OFFLINE": "0", "LLM_PROVIDER": "azure_foundry",
        "LLM_API_KEY": "offline", "LLM_BASE_URL": "https://offline.services.ai.azure.com/api/projects/offline",
        "LLM_DEPLOYMENT_PRO": "DeepSeek-V4-Pro", "LLM_DEPLOYMENT_FLASH": "DeepSeek-V4-Pro",
        "MODEL_ROUTING_OVERRIDES": "{}"}.items():
        monkeypatch.setenv(name, value)
    if is_local():
        from app.core.providers_local import save_settings
        save_settings(provider="azure_openai", base_url="https://offline.services.ai.azure.com/api/projects/offline",
            api_key="synthetic-plan-key", models={"chat": "DeepSeek-V4-Pro", "planning": "DeepSeek-V4-Pro"})
    proposal = _skeleton("A provisional plan using saved context", "Skipped checks are assumptions, not evidence.")
    requests = []

    def respond(request):
        requests.append(request)
        if uncertain:
            raise httpx.ReadTimeout("Synthetic lost provider response", request=request)
        return httpx.Response(200, json={"id": "chat_skip_plan", "object": "chat.completion", "created": 1,
            "model": "DeepSeek-V4-Pro", "choices": [{"index": 0, "finish_reason": "stop",
                "message": {"role": "assistant", "content": json.dumps(proposal)}}], "service_tier": None,
            "usage": {"completion_tokens": 2, "prompt_tokens": 6, "total_tokens": 8,
                "completion_tokens_details": None, "prompt_tokens_details": None,
                "audio_prompt_tokens": 0}})

    real_client = provider_foundry.DefaultAsyncHttpxClient
    monkeypatch.setattr(provider_foundry, "DefaultAsyncHttpxClient", lambda **kwargs: real_client(
        **{**kwargs, "transport": httpx.MockTransport(respond)}))
    monkeypatch.setattr(sole, "_primary_chat", None)
    return requests


@pytest.mark.parametrize("step", STEPS)
@pytest.mark.parametrize("has_topic", [False, True])
def test_each_single_skip_keeps_real_research_and_plan_admission_usable(journey_db, monkeypatch, step, has_topic):
    from app.db.models import Venture
    if not has_topic:
        with journey_db["factory"]() as session:
            session.get(Venture, journey_db["vid"]).raw_idea = ""
            session.commit()
    _confirm(journey_db, "journey.skip_step", {"step": step, "why": "Use whatever context is saved."})
    _check_downstream_admission(journey_db, monkeypatch, has_topic=has_topic)


@pytest.mark.parametrize("state,source,confirmed", [("clarifying", "founder_override", False),
    ("reviewing", "founder", False), ("reviewing", "founder_override", True)])
def test_journey_skip_cannot_bypass_pending_idea_review(journey_db, state, source, confirmed):
    from app.db.models import Job
    from app.db.supymem_models import IdeaSession
    from app.idea.service import append_revision
    from app.research.service import ResearchPreflightBlocked, start

    data = journey_db
    _confirm(data, "journey.skip_step", {"step": "clarification", "why": "Use saved context."})
    with data["factory"]() as session:
        idea = session.scalar(select(IdeaSession).where(IdeaSession.venture_id == data["vid"]))
        idea.state = state
        if confirmed:
            idea.confirmed_revision_id = idea.current_revision_id
        append_revision(session, idea, {"summary": "A corrected topic"}, source=source, summary="A corrected topic")
        session.commit()
    with pytest.raises(ResearchPreflightBlocked, match="research_confirmation_required"):
        start(data["vid"], request_key="pending-review")
    with data["factory"]() as session:
        assert not session.scalar(select(Job.id).where(Job.venture_id == data["vid"], Job.kind == "research.deep"))


def test_skipped_research_worker_completes_and_rejects_a_changed_revision(journey_db, monkeypatch):
    import json
    from app.core.jobs import FatalError, lease
    from app.db.supymem_models import IdeaSession
    from app.idea.service import append_revision
    from app.research import deep, execution, service
    from app.ventures.next_step import current_research_report
    monkeypatch.setenv("RESEARCH_KEYLESS", "0")
    data = journey_db
    _confirm(data, "journey.skip_step", {"step": "clarification", "why": "Research with saved context."})
    started = service.start(data["vid"], phase="demand", request_key="worker-research-click")
    with data["factory"]() as session:
        job = lease(session, "research-worker", kinds=["research.deep"])[0]
        assert job.id == started["job_id"]
        payload = {**job.payload, "_job_id": job.id, "_worker_id": job.worker_id, "_job_attempt": job.attempts}
        session.commit()
    deep.run_deep(payload)
    dossier = execution.read(execution.identifier(job.id))
    assert dossier.state == "partial" and dossier.completed_at
    assert json.loads(dossier.summary)["coverage_gaps"]
    with data["factory"]() as session:
        idea = session.get(IdeaSession, job.payload["session_id"])
        assert idea.state == "reviewing" and not idea.confirmed_revision_id
        append_revision(session, idea, {"summary": "A corrected topic"}, source="founder", summary="A corrected topic")
        session.commit()
    with pytest.raises(service.ResearchPreflightBlocked, match="research_confirmation_required"):
        service.start(data["vid"], expected_revision_id=job.payload["revision_id"])
    with pytest.raises(FatalError, match="research_confirmation_required"):
        execution.claim(payload, kind="deep", budget=8)
    with data["factory"]() as session:
        assert current_research_report(session, session.get(IdeaSession, job.payload["session_id"])) is None


def test_skipped_plan_uncertain_provider_call_is_never_replayed(journey_db, monkeypatch):
    import json
    from app.core.jobs import FatalError, lease
    from app.db.models import CreditAccount
    from app.planning import compiler, execution
    data = journey_db
    _confirm(data, "journey.jump_to", {"step": "find_customers", "why": "Plan from saved context."})
    requests = _plan_transport(monkeypatch, uncertain=True)
    compiler.start(data["vid"], data["wid"])
    with data["factory"]() as session:
        job = lease(session, "uncertain-plan-worker", kinds=["plan.compile"])[0]
        payload = {**job.payload, "_job_id": job.id, "_worker_id": job.worker_id, "_job_attempt": job.attempts}
        session.commit()
    for _ in range(2):
        # An uncertain call ends the job with a founder recovery action; it is never redialled.
        plan = compiler.run_compile(payload)
        assert plan["generation_state"] == "failed" and plan["generation_recovery"]
        assert len(requests) == 1
    doc = json.loads(execution.read(execution.identifier(job.id)).structured)
    assert doc["_runtime"]["operations"]["plan"]["state"] == "uncertain"
    with data["factory"]() as session:
        if data["account"] is None:
            from app.credits import local_meter
            assert session.scalar(select(CreditAccount.id)) is None
            usage = local_meter.snapshot(workspace_id=data["wid"], venture_id=data["vid"], run_id=plan["id"])
            assert usage["month_cost_usd_micro"] > 0
            assert sum(row["unconfirmed_calls"] for row in usage["tasks"]) == 1
            assert sum(row["prompt_tokens"] + row["completion_tokens"] for row in usage["tasks"]) == 0
        else:
            wallet = session.get(CreditAccount, data["account"])
            assert wallet.reserved_micro > 0 and wallet.spent_micro == 0


def test_missing_topic_after_skip_can_use_a_later_saved_description(journey_db, monkeypatch):
    from app.db.models import Venture
    from app.planning.execution import inputs
    from app.research.service import start
    data = journey_db
    with data["factory"]() as session:
        session.get(Venture, data["vid"]).raw_idea = ""
        session.commit()
    _confirm(data, "journey.jump_to", {"step": "find_customers", "why": "Work with what is saved."})
    assert start(data["vid"])["kind"] == "needs_you"
    with data["factory"]() as session:
        session.get(Venture, data["vid"]).raw_idea = "Bakery order board"
        session.commit()
    assert start(data["vid"])["job_id"]
    with data["factory"]() as session:
        assert inputs(session, data["wid"], data["vid"], include_builder_runtime=False)["context"]["idea"]["summary"] == "Bakery order board"


def test_skipped_research_chat_offer_keeps_confirmation_and_assumptions(journey_db, monkeypatch):
    from app.chat import context, router
    from app.state import MEM
    monkeypatch.setattr(router, "MEM", MEM)
    data = journey_db
    _confirm(data, "journey.skip_step", {"step": "clarification", "why": "Research from my saved idea."})
    reply = asyncio.run(router._prepare_intent(data["vid"], context.assemble(data["vid"]), "research",
        "Show research from saved context", None, actor_subject=data["actor"]))
    assert reply["kind"] == "confirmation"
    assert reply["actions"][0]["action"] == "research.start"
    assert "assumptions" in reply["body_md"] and "confirm the idea" not in reply["body_md"]


def test_skipped_starter_plan_is_reviewable_without_research_memory_or_inventory(journey_db, monkeypatch):
    from tests.idea_fixtures import install_provider
    from app.chat.recovery import create_plan
    from app.planning.compiler import get_plan
    from app.memory import task_context
    calls = install_provider(monkeypatch)
    data = journey_db
    _confirm(data, "journey.jump_to", {"step": "find_customers", "why": "Work from what exists."})
    monkeypatch.setattr(task_context, "for_task", lambda *args, **kwargs: {
        "availability": "unavailable", "statements": [], "omitted_count": 0})
    calls_before = len(calls)
    plan = get_plan(create_plan(data["vid"], data["wid"], provenance="template"))
    assert plan["state"] == "founder_review" and plan["reviewable"]
    assert not plan["stale_dependencies"]
    assert any("memory is unavailable" in gap for gap in plan["assumptions"])
    assert not next(milestone for milestone in plan["milestones"] if milestone["kind"] == "landing")["included"]
    assert len(calls) == calls_before


def test_memory_unavailable_does_not_swallow_wrong_workspace(journey_db):
    from app.chat.ground import answer_from_memory
    from app.tenancy import bind
    with bind(workspace_id="foreign", venture_id=journey_db["vid"], actor=journey_db["actor"]):
        with pytest.raises(PermissionError, match="scope_mismatch"):
            asyncio.run(answer_from_memory(journey_db["vid"], "What is saved?"))


@pytest.mark.parametrize("snapshot", [{}, {"summary": "", "customer": None},
                                     {"existing_site": {"url": "https://bakery.example"}}])
def test_research_worker_missing_context_keeps_an_honest_partial_result(journey_db, monkeypatch, snapshot):
    import json
    from types import SimpleNamespace
    from app.research import deep, execution
    from app.research.prior_art import runner
    document = {"_runtime": {"snapshot": snapshot}}
    row = SimpleNamespace(state="pending", summary=json.dumps(document), workspace_id=journey_db["wid"], completed_at=None)
    monkeypatch.setattr(execution, "claim", lambda *args, **kwargs: "dossier")
    monkeypatch.setattr(execution, "read", lambda *args: row)
    def save(did, payload, update):
        update(row, document, None)
    monkeypatch.setattr(execution, "mutate", save)
    monkeypatch.setattr(runner, "enabled", lambda: False)
    monkeypatch.setattr(deep, "inherit_feedback", lambda *args: None)
    monkeypatch.setattr(deep, "_web_provider", lambda: None)
    monkeypatch.setattr(deep, "_github", lambda *args: {"items": [], "incomplete": False})
    monkeypatch.setattr(deep, "_store_sources", lambda *args: None)
    monkeypatch.setattr(deep, "sources_for", lambda *args: [])
    deep.run_deep({"phase": "demand"})
    assert row.state == "partial" and row.completed_at
    assert document["review"] is None and document["coverage_gaps"]
    assert not deep.queries_for({}, "all")
