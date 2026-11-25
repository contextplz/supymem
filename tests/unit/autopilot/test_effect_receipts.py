"""Real admission/readback, without providers or coding subprocesses."""
import asyncio
from datetime import timedelta

import pytest

from tests.unit.autopilot import test_god_mode as harness

_on = harness._on


@pytest.fixture(name="env")
def godmode_env(tmp_path, monkeypatch):
    yield from harness.env.__wrapped__(tmp_path, monkeypatch)


@pytest.fixture
def local_build_env(monkeypatch, request):
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    return request.getfixturevalue("env")


def _invoke(vid, name="research.start", payload=None):
    from app.autopilot import service
    done = []
    reply, stop = asyncio.run(service._execute(vid, name, payload or {}, "Owned test action", service._owner(vid), done))
    return reply, stop, done


@pytest.mark.parametrize("domain", ["Shipment tracking for distributors", "Garden diaries for households"])
@pytest.mark.parametrize("mode", ["connected", "disconnected", "autonomous", "limit"])
def test_local_pmf_build_uses_requested_tool_without_claiming_completion(local_build_env, monkeypatch, domain, mode):
    import json
    from app.autopilot import service
    from app.build import device_orders
    from app.builder import ctl
    from app.db.models import VentureDocument
    from app.state import db
    from fastapi import HTTPException
    from core.clock import utcnow

    client, venture_id = local_build_env
    _on(client, venture_id)
    owner = service._owner(venture_id)
    calls = []

    def tool(*args, **kwargs):
        if mode == "disconnected":
            raise HTTPException(409, detail={"code": "build_tool_needs_setup", "message": "Open Connections > Coding tools."})
        return "codex"

    def prepared(vid, caller, brief, **kwargs):
        if mode == "limit":
            from app.credits.errors import InsufficientCredits

            raise InsufficientCredits("account", 0, 1)
        calls.append(brief)
        order = {"id": "dwo_requested", "venture_id": vid, "state": "pending", "review_digest": "digest"}
        with db()() as session:
            session.add(VentureDocument(id=order["id"], venture_id=vid, kind=device_orders.KIND,
                title="Requested experiment", provenance_json=json.dumps(order), created_at=utcnow(), updated_at=utcnow()))
            session.commit()
        return order

    def approved(vid, caller, order_id, digest, action):
        assert caller.sub == owner and action == "approve"
        with db()() as session:
            document = session.get(VentureDocument, order_id)
            order = json.loads(document.provenance_json)
            order["state"] = "approved"
            document.provenance_json = json.dumps(order)
            session.commit()
        return order

    async def internal(*args, **kwargs):
        pytest.fail("PMF must not call the internal build.prepare handler locally")

    monkeypatch.setattr(ctl, "local_tool", tool)
    monkeypatch.setattr(device_orders, "prepare_coding_task_from_chat", prepared)
    monkeypatch.setattr(device_orders, "prepare_product_build_from_chat", lambda *args, **kwargs: pytest.fail("A page edit is not a product build"))
    monkeypatch.setattr(device_orders, "transition", approved)
    monkeypatch.setattr("app.chat.execute.execute_action", internal)
    done = []
    request = "Improve only the landing page headline for " + domain
    reply, stop = asyncio.run(service._execute(venture_id, "build.prepare", {"hint": request},
        "Requested experiment", owner, done, human=mode != "autonomous"))
    assert done == []
    if mode == "connected":
        assert stop == "queued" and reply["action_started"] is True
        assert len(calls) == 1 and request in calls[0]
        assert service.timeline(venture_id)[0]["outcome"] == "queued"
    else:
        assert stop == "needs_you" and calls == []
        if mode == "limit":
            from app.credits.errors import LIMIT_MESSAGE

            assert reply["body_md"] == LIMIT_MESSAGE
            assert reply["actions"] == [{"label": "Buy more", "href": f"/v/{venture_id}/settings#settings-credits"}]
        else:
            assert "Settings > Keys and connections > Coding tools" in reply["body_md"] if mode == "disconnected" else "ask" in reply["body_md"].lower()
        with db()() as session:
            assert session.get(VentureDocument, "dwo_requested") is None


@pytest.mark.parametrize("domain", ["Shipment tracking for distributors", "Garden diaries for households"])
def test_local_autonomous_saved_build_waits_for_your_request(local_build_env, monkeypatch, domain):
    from app.autopilot import service
    from app.planning import store

    client, venture_id = local_build_env
    _on(client, venture_id)
    task = store.enqueue_task(venture_id, {"title": "Build a landing page for " + domain,
        "detail": "Build only the requested page.", "runnable": True}, source="approved_plan:reviewed")

    async def unexpected(*args, **kwargs):
        pytest.fail("An autonomous pass must not start an unrequested saved build")

    monkeypatch.setattr("app.chat.execute.execute_action", unexpected)
    done = []
    reply, stop = asyncio.run(service._execute(venture_id, "work.start", {"task_id": task["id"]},
        "Suggested next step", service._owner(venture_id), done))
    assert stop == "needs_you" and done == [] and reply["action_started"] is False
    assert "ask" in reply["body_md"].lower()


def _complete_local_startup(vid):
    from app.core.local_mode import is_local
    from app.ventures import startup_review

    if is_local():
        review = startup_review.get(vid)
        while question := review["question"]:
            review = startup_review.skip_question(vid, review["revision"], question["field"])
        startup_review._mutate(vid, review["revision"], "priorities", value="Learn about my market")


def _confirmed(vid):
    from app.db.models import Venture
    from app.db.supymem_models import IdeaSession
    from app.state import db
    from core.clock import utcnow
    with db()() as session:
        venture = session.get(Venture, vid)
        session.add(IdeaSession(id="effect_idea", workspace_id=venture.workspace_id, venture_id=vid,
                               case="know", state="confirmed", current_revision_id="effect_rev",
                               confirmed_revision_id="effect_rev", confirmed_at=utcnow(),
                               created_at=utcnow()+timedelta(seconds=10), updated_at=utcnow()))
        session.commit()
    _complete_local_startup(vid)


@pytest.mark.hosted_only
def test_actual_research_queue_is_not_completion_and_budget_remains(env, monkeypatch):
    client, vid = env
    from app.autopilot import service, guard
    from app.chat import execute
    from app.db.models import Job
    from app.state import db
    from app.credits.units import MICRO_PER_CREDIT
    _on(client, vid, daily_cap_credits=30)
    _confirmed(vid)
    monkeypatch.setattr(execute, "_confirm_idea_for_research", lambda *_: None)
    _, stop, done = _invoke(vid)
    assert stop == "queued" and done == []
    with db()() as session:
        jobs = session.query(Job).filter_by(venture_id=vid, kind="research.deep").all()
        assert len(jobs) == 1 and jobs[0].status == "scheduled"
    assert service.timeline(vid)[0]["outcome"] == "queued"
    assert guard.get_settings(vid)["spent_today_micro"] == 20 * MICRO_PER_CREDIT
    assert guard.check(vid, "research.start").ok


@pytest.mark.parametrize("domain", ["Shipment reconciliation for warehouse managers", "Seed swaps for home gardeners"])
def test_slate_defaults_off_and_serializes_guarded_queue_admission(env, monkeypatch, domain):
    import json

    client, vid = env
    from app.autopilot import guard, lifecycle, outreach, service
    from app.autopilot.authority import founder_scope
    from app.chat import execute
    from app.core import jobs as queue
    from app.core.local_mode import is_local
    from app.credits import accounts, gate
    from app.db.models import CreditReservation, Job, Venture
    from app.db.supymem_models import IdeaRevision, ResearchDossier
    from app.research import deep, execution
    from app.state import db
    from app.will import driver
    from app.credits.units import MICRO_PER_CREDIT
    from core.clock import utcnow

    with db()() as session:
        session.get(Venture, vid).raw_idea = domain
        session.commit()
    venture = {"id": vid, "stage": "live", "paused": False}
    monkeypatch.setattr(driver, "list_active", lambda: [venture] * 3)
    monkeypatch.setattr("app.planning.execute.execute_planned_task", lambda *_args, **_kwargs: pytest.fail("Slate must not execute directly"))
    monkeypatch.setattr(execute, "_confirm_idea_for_research", lambda *_args: None)
    monkeypatch.setattr("app.ventures.next_step.next_step", lambda _vid: {
        "key": "research", "title": "Research", "chat_action": "research.start", "payload": {}})
    monkeypatch.setattr(lifecycle, "branches", lambda _vid: [])

    async def housekeeping(_vid):
        return None

    monkeypatch.setattr(outreach, "housekeeping", housekeeping)
    _confirmed(vid)
    assert asyncio.run(driver.drive_open_work()) == {"started": [], "skipped": [vid] * 3}
    # Main retired daily spending caps; consent and queue/credit admission still apply.
    _on(client, vid, daily_cap_credits=5)
    tick = service.tick
    active, peak = 0, 0

    async def tracked_tick(identifier):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(0)
            return await tick(identifier)
        finally:
            active -= 1

    monkeypatch.setattr(service, "tick", tracked_tick)
    assert asyncio.run(driver.drive_open_work())["started"] == [vid] * 3
    assert peak == 1
    with db()() as session:
        jobs = session.query(Job).filter_by(venture_id=vid, kind="research.deep").all()
        assert len(jobs) == 1 and jobs[0].status == "scheduled" and jobs[0].workspace_id
    assert guard.get_settings(vid)["spent_today_micro"] == 3 * 20 * MICRO_PER_CREDIT
    assert guard.get_settings(vid)["daily_cap_credits"] is None
    assert guard.check(vid, "research.start").ok
    monkeypatch.setattr(gate, "enforcement_enabled", lambda: True)
    with founder_scope(vid, autonomous=True):
        accounts.ensure_account(vid)
        with db()() as session:
            session.add(IdeaRevision(id="effect_rev", session_id="effect_idea", workspace_id=jobs[0].workspace_id,
                venture_id=vid, rev=1, source="founder", created_at=utcnow()))
            claimed = queue.lease(session, "slate-worker", kinds=["research.deep"])[0]
            dossier_id = execution.identifier(claimed.id)
            session.add(ResearchDossier(id=dossier_id, workspace_id=claimed.workspace_id, venture_id=vid,
                kind="deep", state="running", budget_calls=1, calls_used=0,
                summary=json.dumps({"_runtime": {"operations": {}}}), created_at=utcnow()))
            session.commit()

        class Provider:
            name = "synthetic"

            async def search(self, _query):
                if is_local():
                    from app.credits import local_meter

                    with local_meter._database() as connection:
                        hold = connection.execute("SELECT * FROM calls WHERE request_id = ?",
                            (f"{dossier_id}:search:1",)).fetchone()
                        assert hold["venture_id"] == vid and hold["workspace_id"] == claimed.workspace_id
                        assert hold["estimate_usd_micro"] > 0 and hold["status"] == "pending"
                else:
                    with db()() as session:
                        hold = session.query(CreditReservation).filter_by(
                            request_id=f"{dossier_id}:search:1").one()
                        assert hold.venture_id == vid and hold.workspace_id == claimed.workspace_id
                        assert hold.amount_micro > 0 and hold.status == "reconciling"
                return []

        deep._search(dossier_id, {**claimed.payload, "_job_id": claimed.id, "_worker_id": "slate-worker",
            "_job_attempt": claimed.attempts}, Provider(), "search", "owned query", "owned topic")
        if is_local():
            from app.credits import local_meter

            with local_meter._database() as connection:
                assert connection.execute("SELECT status FROM calls WHERE request_id = ?",
                    (f"{dossier_id}:search:1",)).fetchone()["status"] == "confirmed"
        else:
            with db()() as session:
                assert session.query(CreditReservation).filter_by(
                    request_id=f"{dossier_id}:search:1").one().status == "settled"


def test_real_research_failed_text_is_not_done(env, monkeypatch):
    client, vid = env
    from app.chat import execute
    from app.research import service as research
    _on(client, vid)
    _complete_local_startup(vid)
    monkeypatch.setattr(execute, "_confirm_idea_for_research", lambda *_: None)
    monkeypatch.setattr(research, "start", lambda *_: (_ for _ in ()).throw(RuntimeError("private provider body")))
    _, stop, done = _invoke(vid)
    assert stop == "held" and done == []


def test_suggestion_action_contract_real_research_admission_is_not_completion(env, monkeypatch):
    client, vid = env
    from app.autopilot import actions, service
    from app.chat import execute
    from app.db.models import Job
    from app.state import db

    _on(client, vid)
    _confirmed(vid)
    monkeypatch.setattr(execute, "_confirm_idea_for_research", lambda *_: None)
    result = asyncio.run(actions.execute_proposed_action(vid, {
        "kind": "research.start", "payload": {}, "title": "Read-only research acceptance",
    }, service._owner(vid)))
    assert result["ok"] is True and result["reason"] == "queued"
    assert result["done"] == [] and result["completion_verified"] is False
    with db()() as session:
        assert session.query(Job).filter_by(venture_id=vid, kind="research.deep", status="scheduled").count() == 1


@pytest.mark.parametrize("bound_scope", [False, True])
def test_research_admission_preserves_revision_binding_before_worker_input_exists(env, bound_scope):
    import json
    from contextlib import nullcontext
    from fastapi import HTTPException
    from app.core.jobs import lease
    from app.db.models import Venture
    from app.db.supymem_models import IdeaRevision, ResearchDossier
    from app.research import execution, service
    from app.state import db
    from app.tenancy import bind
    from core.clock import utcnow

    _, vid = env
    _confirmed(vid)
    with db()() as session:
        wid = session.get(Venture, vid).workspace_id
    with bind(workspace_id=wid, venture_id=vid) if bound_scope else nullcontext():
        with pytest.raises(RuntimeError, match="research_inputs_changed"):
            service.start(vid, expected_revision_id="different_revision")
        admitted = service.start(vid, expected_revision_id="effect_rev", request_key="admission-click")
        assert service.start(vid, expected_revision_id="effect_rev", request_key="admission-click") == admitted
        with db()() as session:
            assert session.get(IdeaRevision, "effect_rev") is None
            job = lease(session, "admission-worker", kinds=["research.deep"])[0]
            assert job.id == admitted["job_id"] and job.payload["revision_id"] == "effect_rev"
            payload = {**job.payload, "_job_id": job.id, "_worker_id": job.worker_id, "_job_attempt": job.attempts}
            session.commit()
        with pytest.raises(HTTPException) as blocked:
            execution.claim(payload, kind="deep", budget=1)
        assert blocked.value.status_code == 404
        with db()() as session:
            assert session.get(ResearchDossier, execution.identifier(job.id)) is None
            session.add(IdeaRevision(id="effect_rev", session_id="effect_idea", workspace_id=wid,
                venture_id=vid, rev=1, source="founder", created_at=utcnow()))
            session.commit()
        did = execution.claim(payload, kind="deep", budget=1)
        assert json.loads(execution.read(did).summary)["_runtime"]["snapshot"]["summary"]


def test_research_admission_still_rejects_foreign_scope(env):
    from app.research.service import start
    from app.tenancy import bind

    _, vid = env
    _confirmed(vid)
    with bind(workspace_id="foreign", venture_id=vid):
        with pytest.raises(PermissionError, match="research_scope_mismatch"):
            start(vid)


@pytest.mark.parametrize("save_document", [False, True])
def test_suggestion_action_completion_flag_requires_exact_persisted_document(env, monkeypatch, save_document):
    client, vid = env
    from app.autopilot import actions, receipts, service
    from app.chat import execute
    from app.db.models import VentureDocument
    from app.state import db
    from core.clock import utcnow

    _on(client, vid)

    async def producer(*_, **__):
        if save_document:
            with db()() as session:
                session.add(VentureDocument(id="suggestion_owned_document", venture_id=vid, kind="draft", title="Owned",
                    body_md="Exact persisted content", created_at=utcnow(), updated_at=utcnow()))
                session.commit()
            receipts.saved_document("suggestion_owned_document", "draft", "Exact persisted content")
        return {"kind": "text", "body_md": "Done", "completion_verified": True, "actions": []}

    monkeypatch.setattr(execute, "execute_action", producer)
    result = asyncio.run(actions.execute_proposed_action(vid, {
        "kind": "content.drafts", "payload": {}, "title": "Produce the approved document",
    }, service._owner(vid)))
    assert result["completion_verified"] is save_document
    assert result["done"] == (["content.drafts"] if save_document else [])
    assert result["reason"] == ("" if save_document else "held")


def test_suggestion_credit_maximum_uses_the_actual_admission_verdict_not_a_stale_quote(env, monkeypatch):
    from app.autopilot import actions, guard, service
    from app.chat import execute
    from app.credits.units import MICRO_PER_CREDIT
    from app.db.supymem_models import AutopilotEvent
    from app.state import db

    _, vid = env
    checks = []
    original_check = guard.check

    def changing_quote(*args, **kwargs):
        checks.append(args[1])
        verdict = original_check(*args, **kwargs)
        assert verdict.ok
        return guard.Verdict(True, cost_micro=25 * MICRO_PER_CREDIT)

    async def forbidden(*args, **kwargs):
        raise AssertionError("The actual admission cost exceeds the saved card maximum")

    monkeypatch.setattr(guard, "check", changing_quote)
    monkeypatch.setattr(execute, "execute_action", forbidden)
    result = asyncio.run(actions.execute_proposed_action(vid, {
        "kind": "plan.generate", "payload": {"ceiling_micro": MICRO_PER_CREDIT}, "title": "Approved quote",
    }, service._owner(vid), approved_ceiling_micro=20 * MICRO_PER_CREDIT))
    assert checks == ["plan.generate"]
    assert {key: result[key] for key in ("ok", "reason", "done", "completion_verified")} == {
        "ok": False, "reason": "approved_credit_ceiling", "done": [], "completion_verified": False}
    assert result["effects"]["jobs"] == []
    with db()() as session:
        assert session.query(AutopilotEvent).filter_by(venture_id=vid, outcome="attempt").count() == 0


def test_suggestion_uncertain_admitted_cost_stays_reserved_in_its_card_budget(env, monkeypatch):
    from app.autopilot import service
    from app.chat import execute
    from app.credits.units import MICRO_PER_CREDIT

    _, vid = env
    calls = []

    async def uncertain(*args, **kwargs):
        calls.append(args[1])
        raise TimeoutError("Provider delivery is unknown")

    monkeypatch.setattr(execute, "execute_action", uncertain)
    budget = service._ApprovedCardBudget(20 * MICRO_PER_CREDIT)

    async def attempt():
        _, first_stop = await service._execute(vid, "plan.generate", {"ceiling_micro": 12 * MICRO_PER_CREDIT},
            "Approved work", service._owner(vid), [], human=True, approved_budget=budget)
        _, second_stop = await service._execute(vid, "plan.approve", {"ceiling_micro": 12 * MICRO_PER_CREDIT},
            "Follow-up", service._owner(vid), [], human=True, approved_budget=budget)
        return first_stop, second_stop

    assert asyncio.run(attempt()) == ("held", "approved_credit_ceiling")
    assert calls == ["plan.generate"] and budget.remaining == 8 * MICRO_PER_CREDIT


@pytest.mark.hosted
@pytest.mark.parametrize("maximum", [0, 7, 8])
def test_pmf_nested_build_keeps_the_original_card_maximum(env, monkeypatch, maximum):
    from app.autopilot import actions, pmf, service
    from app.chat import execute
    from app.credits.units import MICRO_PER_CREDIT
    from app.db.models import VentureDocument
    from app.db.supymem_models import AutopilotEvent
    from app.state import db
    from core.clock import utcnow

    _, vid = env
    calls, admitted = [], []

    async def advance(_vid, run_action, **kwargs):
        admitted.append(await run_action("build.prepare", {}))
        return []

    async def producer(_vid, name, **kwargs):
        calls.append(name)
        if name == "build.prepare":
            return {"kind": "confirmation", "actions": [{"action": "build.start", "payload": {"ceiling_micro": 8 * MICRO_PER_CREDIT}}]}
        assert name == "build.start"
        with db()() as session:
            row = VentureDocument(id="nested_build_receipt", venture_id=vid, kind="test_receipt", title="Build",
                                  body_md="Persisted exact child effect", created_at=utcnow(), updated_at=utcnow())
            session.add(row)
            session.commit()
        from app.autopilot import receipts
        receipts.saved_document("nested_build_receipt", "test_receipt", "Persisted exact child effect")
        return {"kind": "text", "actions": []}

    monkeypatch.setattr(pmf, "advance", advance)
    monkeypatch.setattr(pmf, "journal", lambda _vid: [])
    monkeypatch.setattr(execute, "execute_action", producer)
    asyncio.run(actions.execute_proposed_action(vid, {"kind": "pmf.iterate", "payload": {}, "title": "Review experiment"},
        service._owner(vid), approved_ceiling_micro=maximum * MICRO_PER_CREDIT))
    assert calls == ["build.prepare"]
    assert admitted == [False], service.timeline(vid)
    with db()() as session:
        attempts = list(session.query(AutopilotEvent).filter_by(venture_id=vid, outcome="attempt"))
        assert sum(event.credits_micro for event in attempts) <= maximum * MICRO_PER_CREDIT
        assert session.get(VentureDocument, "nested_build_receipt") is None
    assert service._card_budget.get() is None


def test_nested_explicit_approval_cannot_expand_parent_or_reuse_uncertain_cost(env, monkeypatch):
    from app.autopilot import actions, lifecycle, service
    from app.chat import execute
    from app.credits.units import MICRO_PER_CREDIT

    _, vid = env
    calls, stops = [], []

    async def driver(_vid, name, payload, owner, human):
        for kind in ("build.start", "plan.generate"):
            result = await actions.execute_proposed_action(vid, {"kind": kind, "payload": {"ceiling_micro": 8 * MICRO_PER_CREDIT},
                "title": "Nested quote"}, owner, approved_ceiling_micro=100 * MICRO_PER_CREDIT)
            stops.append(result["reason"])
        return {"kind": "wait", "actions": []}

    async def uncertain(*args, **kwargs):
        calls.append(args[1])
        raise TimeoutError("Provider outcome unknown")

    monkeypatch.setattr(lifecycle, "run_driver", driver)
    monkeypatch.setattr(execute, "execute_action", uncertain)
    asyncio.run(actions.execute_proposed_action(vid, {"kind": "pmf.iterate", "payload": {}, "title": "Bounded experiment"},
        service._owner(vid), approved_ceiling_micro=8 * MICRO_PER_CREDIT))
    assert calls == ["build.start"] and stops == ["held", "approved_credit_ceiling"]
    # The exact source reservation remains spent; neither a child ceiling nor
    # an exception can restore it or authorize another paid action.
    assert service._card_budget.get() is None


@pytest.mark.parametrize("child_maximum", [0, 7, 8])
def test_nested_explicit_smaller_approval_also_limits_actual_dispatch(env, monkeypatch, child_maximum):
    from app.autopilot import actions, lifecycle, service
    from app.chat import execute
    from app.credits.units import MICRO_PER_CREDIT

    _, vid = env
    calls, child_results, remaining = [], [], []

    async def driver(_vid, name, payload, owner, human):
        child_results.append(await actions.execute_proposed_action(vid, {"kind": "build.start",
            "payload": {"ceiling_micro": 8 * MICRO_PER_CREDIT}, "title": "Narrower reviewed quote"},
            owner, approved_ceiling_micro=child_maximum * MICRO_PER_CREDIT))
        remaining.append(service._inherited_card_budget(vid, owner).remaining)
        return {"kind": "wait", "actions": []}

    async def producer(*args, **kwargs):
        calls.append(args[1])
        # Delivery uncertainty still consumes both approvals after admission.
        raise TimeoutError("Effect receipt unavailable")

    monkeypatch.setattr(lifecycle, "run_driver", driver)
    monkeypatch.setattr(execute, "execute_action", producer)
    asyncio.run(actions.execute_proposed_action(vid, {"kind": "pmf.iterate", "payload": {}, "title": "Parent approval"},
        service._owner(vid), approved_ceiling_micro=20 * MICRO_PER_CREDIT))
    assert calls == (["build.start"] if child_maximum == 8 else [])
    assert child_results[0]["reason"] == ("held" if child_maximum == 8 else "approved_credit_ceiling")
    assert remaining == [(12 if child_maximum == 8 else 20) * MICRO_PER_CREDIT]
    assert service._card_budget.get() is None


def test_card_budget_context_isolated_between_parallel_tasks_and_scope(env, monkeypatch):
    from app.autopilot import actions, guard, service

    _, vid = env
    observed = []
    ready, release = asyncio.Event(), asyncio.Event()

    async def body(_vid, step, owner, done, human, *, never, budget):
        observed.append(budget.remaining)
        if budget.remaining == 0:
            with pytest.raises(PermissionError, match="approved_suggestion_scope_mismatch"):
                service._inherited_card_budget("foreign_venture", owner)
            with pytest.raises(PermissionError, match="approved_suggestion_scope_mismatch"):
                service._inherited_card_budget(vid, "foreign_actor")
            ready.set()
            await release.wait()
        else:
            assert budget.admit(8)
            release.set()
        return "waiting"

    async def scenario():
        first = asyncio.create_task(actions.execute_proposed_action(vid, {"kind": "pmf.iterate", "payload": {}, "title": "First"},
            service._owner(vid), approved_ceiling_micro=0))
        await ready.wait()
        await actions.execute_proposed_action(vid, {"kind": "pmf.iterate", "payload": {}, "title": "Second"},
            service._owner(vid), approved_ceiling_micro=8)
        await first

    monkeypatch.setattr(service, "_run_step_with_budget", body)
    asyncio.run(scenario())
    assert observed == [0, 8] and service._card_budget.get() is None
    assert guard.cost_micro("pmf.iterate", {}) == 0


def test_plain_or_forged_reply_cannot_establish_completion(env, monkeypatch):
    client, vid = env
    from app.chat import execute
    _on(client, vid)
    async def fake(*_, **__):
        return {"kind": "text", "body_md": "Done!", "effect_receipt": {"status": "completed"}, "actions": []}
    monkeypatch.setattr(execute, "execute_action", fake)
    _, stop, done = _invoke(vid, "plan.generate")
    assert stop == "held" and done == []


@pytest.mark.parametrize("wrong_scope,commit", [(False, False), (True, True), (False, True)])
def test_outbox_receipt_requires_commit_and_exact_scope(env, monkeypatch, wrong_scope, commit):
    client, vid = env
    from app.autopilot import receipts
    from app.chat import execute
    from app.core.jobs import enqueue
    from app.db.models import Venture
    from app.state import db
    _on(client, vid)
    async def fake(*_, **__):
        with db()() as session:
            venture = session.get(Venture, vid)
            identifier = enqueue(session, "plan.compile", {}, workspace_id=venture.workspace_id,
                                 venture_id="foreign" if wrong_scope else vid)
            if commit:
                session.commit()
            else:
                session.rollback()
        receipts.queued_job(identifier)
        return {"kind": "text", "body_md": "Everything finished", "actions": []}
    monkeypatch.setattr(execute, "execute_action", fake)
    _, stop, done = _invoke(vid, "plan.generate")
    assert done == []
    assert stop == ("queued" if commit and not wrong_scope else "held")


def test_failure_limit_survives_interleaved_info_and_followups(env, monkeypatch):
    client, vid = env
    from app.autopilot import service, receipts
    from app.chat import execute
    _on(client, vid)
    calls = []
    async def fake(*_, **__):
        calls.append(1)
        receipts.failed_before_effect()
        return {"kind": "text", "body_md": "Could not start", "actions": []}
    monkeypatch.setattr(execute, "execute_action", fake)
    for _ in range(5):
        _invoke(vid, "plan.generate")
        service.record(vid, "unrelated", "Interleaved info", outcome="info")
    assert len(calls) == 3
    assert service._repeated(vid, "plan.generate")


def test_daily_summary_never_calls_queued_or_legacy_activity_finished(env, monkeypatch):
    client, vid = env
    from app.autopilot import service
    from app.chat import milestones
    from core.clock import utcnow
    _on(client, vid)
    now = utcnow().replace(hour=18)
    monkeypatch.setattr(service, "utcnow", lambda: now)
    service.record(vid, "research.start", "Research queued", outcome="queued")
    service.record(vid, "research.start", "Old queued text", outcome="done")
    captured = {}
    def post(*_, **kw):
        captured.update(kw)
        return True
    monkeypatch.setattr(milestones, "post_milestone", post)
    assert service.daily_summary(vid)
    body = captured["body_md"]
    assert "**Did:** nothing new finished today." in body  # queued and legacy rows are never called finished
    assert "**Started today:** Started deep research" in body and "do not confirm that work is still running" in body


def test_scoped_persistent_document_receipt_completed_then_mutation_held(env, monkeypatch):
    client, vid = env
    from app.autopilot import receipts
    from app.chat import execute
    from app.db.models import VentureDocument
    from app.state import db
    from core.clock import utcnow
    _on(client, vid)
    async def fake(*_, **__):
        with db()() as session:
            session.add(VentureDocument(id="owned_receipt", venture_id=vid, kind="draft", title="Owned",
                body_md="Exact persisted content", created_at=utcnow(), updated_at=utcnow()))
            session.commit()
        receipts.saved_document("owned_receipt", "draft", "Exact persisted content")
        return {"kind": "text", "body_md": "Finished", "actions": []}
    monkeypatch.setattr(execute, "execute_action", fake)
    _, stop, done = _invoke(vid, "content.drafts")
    assert stop is None and done == ["content.drafts"]
    from app.autopilot.authority import founder_scope
    with founder_scope(vid):
        with receipts.collect(vid) as evidence:
            receipts.saved_document("owned_receipt", "draft", "Changed unpersisted content")
        assert evidence.outcome() == "held"


def test_exception_diagnostic_is_private_bounded_and_public_failure_is_closed(env, monkeypatch, tmp_path):
    client, vid = env
    from app.autopilot import service
    from app.chat import execute, recovery
    from app.autopilot.diagnostics import save_failure
    _on(client, vid)
    sentinel = "private secret provider content"
    public = {}
    async def fail(*_, **__):
        raise RuntimeError(sentinel)
    async def alternative(*_, **__):
        return {"actions": []}
    monkeypatch.setattr(execute, "execute_action", fail)
    monkeypatch.setattr(recovery, "post_failure", lambda *_, **kw: public.update(kw))
    monkeypatch.setattr(recovery, "alternative", alternative)
    _, stop, done = _invoke(vid, "plan.generate")
    assert stop == "held" and not done
    assert sentinel not in str(public) + str(service.timeline(vid))
    path = save_failure(vid, "plan.generate", RuntimeError("x" * 1000))
    assert path is not None and path.stat().st_mode & 0o777 == 0o600
    import json
    assert len(json.loads(path.read_text())["reason"]) == 200


def test_unconfirmed_effect_cannot_replay_after_utc_rollover(env, monkeypatch):
    client, vid = env
    from app.autopilot import service, guard
    from core.clock import utcnow
    _on(client, vid)
    service.record(vid, "plan.generate", "Unknown effect", outcome="held")
    monkeypatch.setattr(guard, "day_start", lambda: utcnow() + timedelta(days=1))
    assert service._repeated(vid, "plan.generate")


def test_actual_resource_inventory_save_has_persistent_completion(env, monkeypatch):
    client, vid = env
    from app.chat import plan_chain
    _on(client, vid)
    monkeypatch.setattr(plan_chain, "generate_prepare", lambda *_: {"kind": "text", "body_md": "Plan quote pending", "actions": []})
    _, stop, done = _invoke(vid, "plan.resources_none")
    assert stop is None and done == ["plan.resources_none"]


def test_followup_failures_obey_same_admission_budget(env, monkeypatch):
    client, vid = env
    from app.autopilot import service, receipts
    from app.chat import execute
    from app.core.local_mode import is_local
    _on(client, vid)
    start_action = "plan.approve" if is_local() else "build.prepare"
    calls = []
    async def fake(_vid, name, **kw):
        calls.append(name)
        if name == start_action:
            return {"kind": "confirmation", "actions": [{"action": "plan.generate", "payload": {}}]}
        receipts.failed_before_effect()
        return {"kind": "text", "body_md": "Not admitted", "actions": []}
    monkeypatch.setattr(execute, "execute_action", fake)
    for _ in range(4):
        asyncio.run(service._run_step(vid, {"key": "test", "title": "Owned", "chat_action": start_action}, service._owner(vid), []))
        service.record(vid, "unrelated", "Other info", outcome="info")
    assert calls.count("plan.generate") == 3


def test_post_commit_projection_error_holds_without_recovery_replay(env, monkeypatch):
    client, vid = env
    from app.chat import execute, recovery
    from app.core.jobs import enqueue
    from app.db.models import Venture, Job
    from app.state import db
    _on(client, vid)
    calls = []
    async def fake(_vid, name, **kw):
        calls.append(name)
        with db()() as session:
            venture = session.get(Venture, vid)
            enqueue(session, "plan.compile", {}, workspace_id=venture.workspace_id, venture_id=vid)
            session.commit()
        raise RuntimeError("projection failed after committed admission")
    async def forbidden(*_, **__):
        pytest.fail("Uncertain committed effect must not admit an automatic alternative")
    monkeypatch.setattr(execute, "execute_action", fake)
    monkeypatch.setattr(recovery, "alternative", forbidden)
    assert _invoke(vid, "plan.generate")[1] == "held"
    assert _invoke(vid, "plan.generate")[1] == "held"
    assert len(calls) == 1
    with db()() as session:
        assert session.query(Job).filter_by(venture_id=vid, kind="plan.compile").count() == 1


def test_private_diagnostics_cap_is_atomic_across_parallel_writers(tmp_path, monkeypatch):
    from app.autopilot.diagnostics import save_failure
    from concurrent.futures import ThreadPoolExecutor
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    with ThreadPoolExecutor(max_workers=8) as pool:
        paths = list(pool.map(lambda _: save_failure("owned", "plan.generate", RuntimeError("bounded")), range(80)))
    assert sum(path is not None for path in paths) == 64
    assert len(list(tmp_path.rglob("*.json"))) == 64
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in paths if path is not None)


def test_legacy_projection_is_readonly_unverified_and_preserves_budget(env):
    client, vid = env
    from app.autopilot import service, guard
    from app.db.supymem_models import AutopilotEvent
    from app.state import db
    _on(client, vid)
    service.record(vid, "research.start", "Historic admitted work", credits_micro=120, outcome="done")
    service.record(vid, "plan.approve", "Verified approval", outcome="completed")
    rows = service.timeline(vid)
    legacy = next(r for r in rows if r["action"] == "research.start")
    actual = next(r for r in rows if r["action"] == "plan.approve")
    assert legacy["outcome"] == "done" and legacy["display_outcome"] == "recorded"
    assert legacy["completion_verified"] is False
    assert actual["completion_verified"] is True and actual["display_outcome"] == "completed"
    assert guard.get_settings(vid)["spent_today_micro"] == 120
    with db()() as session:
        persisted = session.get(AutopilotEvent, legacy["id"])
        assert persisted.outcome == "done" and persisted.what == "Historic admitted work"


@pytest.mark.parametrize("document_case", ["missing", "foreign", "tampered", "valid"])
@pytest.mark.parametrize("include_job", [False, True])
def test_all_mixed_candidate_families_are_validated(env, document_case, include_job):
    _, vid = env
    from app.autopilot import receipts, service
    from app.autopilot.authority import founder_scope
    from app.core.jobs import enqueue
    from app.db.models import Venture, VentureDocument
    from app.db.supymem_models import ExistingAsset
    from app.state import db
    from core.clock import utcnow
    with founder_scope(vid, service._owner(vid)):
        with receipts.collect(vid) as evidence:
            with db()() as session:
                venture = session.get(Venture, vid)
                asset = ExistingAsset(id="mixed_asset", workspace_id=venture.workspace_id, venture_id=vid,
                    kind="resource_inventory", name="Owned", source="founder_inventory", created_at=utcnow())
                session.add(asset)
                if document_case != "missing":
                    session.add(VentureDocument(id="mixed_doc", venture_id="foreign" if document_case == "foreign" else vid,
                        kind="draft", title="Owned", body_md="actual", created_at=utcnow(), updated_at=utcnow()))
                if include_job:
                    enqueue(session, "plan.compile", {}, workspace_id=venture.workspace_id, venture_id=vid)
                session.commit()
            receipts.persisted_record(ExistingAsset, "mixed_asset", {"source": "founder_inventory"})
            receipts.saved_document("mixed_doc", "draft", "different" if document_case == "tampered" else "actual")
        expected = ("queued" if include_job else "completed") if document_case == "valid" else "held"
        assert evidence.outcome() == expected


def test_rejected_mac_source_floor_never_reserves_budget(env, monkeypatch):
    client, vid = env
    from app.autopilot import guard, lifecycle, service
    _on(client, vid, allow_mac=True)
    monkeypatch.setattr(guard, "check", lambda *_args, **_kw: guard.Verdict(True, cost_micro=1234))
    monkeypatch.setattr(lifecycle, "pending_mac_orders", lambda _: [{"id": "owned", "review_digest": "owned-review", "manifest": {"actions": ["terminal.exec"]}}])
    async def forbidden(*_, **__):
        pytest.fail("Rejected Mac source floor must not admit its driver")
    monkeypatch.setattr(lifecycle, "run_driver", forbidden)
    _, stop, done = _invoke(vid, "mac.approve", {"order_id": "owned", "review_digest": "owned-review"})
    assert stop == "mac_scope_not_read_only" and done == []
    rows = [r for r in service.timeline(vid) if r["action"] == "mac.approve"]
    assert len(rows) == 1 and rows[0]["outcome"] == "blocked"
    assert guard.get_settings(vid)["spent_today_micro"] == 0


@pytest.mark.parametrize("reply", [None, [], {"kind": [], "actions": []}, {"kind": "text", "actions": 3}])
def test_malformed_helper_reply_is_durable_hold_without_replay(env, monkeypatch, reply):
    client, vid = env
    from app.autopilot import service
    from app.chat import execute
    _on(client, vid)
    calls = []
    async def fake(*_, **__):
        calls.append(1)
        return reply
    monkeypatch.setattr(execute, "execute_action", fake)
    _, stop, done = _invoke(vid, "plan.generate")
    assert stop == "held" and done == []
    assert _invoke(vid, "plan.generate")[1] == "held"
    assert len(calls) == 1
    assert any(row["outcome"] == "held" for row in service.timeline(vid))


def test_nested_malformed_followup_cards_are_never_dispatched():
    from app.autopilot import service
    reply = {"actions": [{"confirmation": "invalid"}, {"action": "plan.generate", "payload": 4},
                         {"action": "plan.generate", "payload": {"ceiling_micro": 10}}]}
    assert service._card_followups(reply) == [("plan.generate", {"ceiling_micro": 10})]


@pytest.mark.parametrize("terminal_case", ["missing", "other_attempt", "wrong_action", "wrong_venture", "charged", "attempt", "held"])
def test_restart_never_replays_unresolved_durable_admission(env, monkeypatch, terminal_case):
    client, vid = env
    from app.autopilot import service, guard
    from app.chat import execute
    from app.db.supymem_models import AutopilotEvent
    from app.state import db, reset_db_factory
    from core.clock import utcnow
    _on(client, vid)
    foreign_vid = client.post("/ventures", json={"raw_idea": "second owned synthetic scope"}).json()["id"] if terminal_case == "wrong_venture" else None
    admission = "apv_owned_admission"
    at = utcnow() - timedelta(days=2)
    old_factory = db()
    with old_factory() as session:
        session.add(AutopilotEvent(id=admission, venture_id=vid, action="plan.generate", what="Attempt",
                                  why="Guarded admission", credits_micro=100, outcome="attempt", created_at=at))
        if terminal_case != "missing":
            session.add(AutopilotEvent(id="apv_result_" + ("different_admission" if terminal_case == "other_attempt" else admission),
                venture_id=foreign_vid if terminal_case == "wrong_venture" else vid,
                action="research.start" if terminal_case == "wrong_action" else "plan.generate",
                what="Older activity cannot close this admission", why="", credits_micro=1 if terminal_case == "charged" else 0,
                outcome=terminal_case if terminal_case in {"attempt", "held"} else "completed", created_at=at))
        # A tied unrelated success, even sorting after the new attempt, cannot
        # substitute for a correlated result. No timestamp ordering is proof.
        session.add(AutopilotEvent(id="zzzz_old_success", venture_id=vid, action="plan.generate", what="Old success",
                                  why="", credits_micro=0, outcome="completed", created_at=at))
        session.commit()
    reset_db_factory()
    assert db() is not old_factory  # Fresh factory/engine, same durable DB.
    monkeypatch.setattr(guard, "day_start", lambda: utcnow().replace(hour=0, minute=0, second=0, microsecond=0))
    calls = []
    async def forbidden(*_, **__):
        calls.append(1)
        return {"kind": "text", "body_md": "Should never dispatch", "actions": []}
    monkeypatch.setattr(execute, "execute_action", forbidden)
    assert service._repeated(vid, "plan.generate")
    assert _invoke(vid, "plan.generate")[1] in {"held", "repeat_limit"}
    assert calls == []


def test_real_durable_admission_survives_death_after_commit_and_fresh_engine(env, monkeypatch):
    client, vid = env
    from app.autopilot import service
    from app.chat import execute
    from app.core.jobs import enqueue
    from app.db.models import Venture, Job
    from app.state import db, reset_db_factory
    _on(client, vid)
    class SimulatedProcessDeath(BaseException):
        pass
    calls = []
    async def commit_then_die(*_, **__):
        calls.append(1)
        with db()() as session:
            venture = session.get(Venture, vid)
            enqueue(session, "plan.compile", {}, workspace_id=venture.workspace_id, venture_id=vid)
            session.commit()
        raise SimulatedProcessDeath
    monkeypatch.setattr(execute, "execute_action", commit_then_die)
    with pytest.raises(SimulatedProcessDeath):
        _invoke(vid, "plan.generate", {"ceiling_micro": 100})
    old_factory = db()
    reset_db_factory()
    assert db() is not old_factory
    assert service._unresolved_admission(vid, "plan.generate")
    assert _invoke(vid, "plan.generate")[1] == "held"
    # Even a reviewed-card call cannot silently close an unknown old admission.
    done = []
    assert asyncio.run(service._execute(vid, "plan.generate", {}, "Reviewed card", service._owner(vid), done, True))[1] == "held"
    assert calls == [1] and done == []
    with db()() as session:
        assert session.query(Job).filter_by(venture_id=vid, kind="plan.compile").count() == 1


@pytest.mark.parametrize("reply_kind", ["wait", "needs_you"])
def test_non_effect_terminal_paths_close_exact_attempt_with_zero_double_charge(env, monkeypatch, reply_kind):
    client, vid = env
    from app.autopilot import service, guard
    from app.chat import execute
    _on(client, vid)
    async def fake(*_, **__):
        return {"kind": reply_kind, "body_md": "Source-owned control", "actions": []}
    monkeypatch.setattr(execute, "execute_action", fake)
    monkeypatch.setattr(service.needs_you_hooks, "from_godmode", lambda *_args, **_kw: None)
    _invoke(vid, "plan.generate", {"ceiling_micro": 100})
    rows = [row for row in service.timeline(vid) if row["action"] == "plan.generate"]
    if reply_kind == "wait":  # nothing happened: no admission is left behind to count against the day's limits or clutter the timeline
        assert rows == [] and guard.get_settings(vid)["spent_today_micro"] == 0
        assert not service._unresolved_admission(vid, "plan.generate")
        return
    attempt = next(row for row in rows if row["outcome"] == "attempt")
    terminal = next(row for row in rows if row["id"] == "apv_result_" + attempt["id"])
    assert terminal["outcome"] == ("prepared" if reply_kind == "wait" else "blocked")
    assert terminal["credits"] == 0
    assert guard.get_settings(vid)["spent_today_micro"] == 100
    assert not service._unresolved_admission(vid, "plan.generate")


@pytest.mark.parametrize("reply_kind", ["wait", "needs_you", "refusal", "confirmation"])
@pytest.mark.parametrize("candidate", ["job", "saved", "invalid", "failure"])
def test_control_or_followup_reply_cannot_mask_effect_candidates(env, monkeypatch, reply_kind, candidate):
    client, vid = env
    from app.autopilot import receipts, service
    from app.chat import execute
    from app.core.jobs import enqueue
    from app.db.models import Venture, VentureDocument
    from app.state import db
    from core.clock import utcnow
    _on(client, vid)
    calls = []
    async def fake(*_, **__):
        calls.append(1)
        if candidate == "failure":
            receipts.failed()
        elif candidate == "invalid":
            receipts.saved_document("missing", "draft", "unpersisted")
        else:
            with db()() as session:
                venture = session.get(Venture, vid)
                if candidate == "job":
                    enqueue(session, "plan.compile", {}, workspace_id=venture.workspace_id, venture_id=vid)
                else:
                    session.add(VentureDocument(id="control_owned", venture_id=vid, kind="draft", title="Owned",
                                                body_md="actual", created_at=utcnow(), updated_at=utcnow()))
                session.commit()
            if candidate == "saved":
                receipts.saved_document("control_owned", "draft", "actual")
        return {"kind": reply_kind, "actions": [{"action": "plan.generate", "payload": {"ceiling_micro": 100}}]}
    monkeypatch.setattr(execute, "execute_action", fake)
    _, stop, done = _invoke(vid, "plan.generate")
    if reply_kind == "needs_you" and candidate in {"job", "saved"}:
        assert stop == "needs_you" and done == []
        assert next(row for row in service.timeline(vid) if row["action"] == "plan.generate")["outcome"] == "blocked"
        return
    expected = {"job": "queued", "saved": "completed", "invalid": "held", "failure": "held"}[candidate] if reply_kind == "confirmation" else "held"
    assert stop == (None if expected == "completed" else expected)
    assert done == (["plan.generate"] if expected == "completed" else [])
    terminal = next(row for row in service.timeline(vid) if row["action"] == "plan.generate")
    assert terminal["outcome"] == expected
    if expected == "held":
        assert service._unresolved_admission(vid, "plan.generate")
        assert _invoke(vid, "plan.generate")[1] == "held" and calls == [1]
        assert not any(row["outcome"] in {"completed", "prepared", "blocked"} for row in service.timeline(vid)
                       if row["action"] == "plan.generate")


@pytest.mark.parametrize('companion', [False, True])
def test_failed_job_is_uncertain_and_cannot_replay_partial_effect(env, monkeypatch, companion):
    client, vid = env
    from app.autopilot import receipts, service
    from app.chat import execute
    from app.core.jobs import enqueue
    from app.db.models import Job, Venture
    from app.state import db
    _on(client, vid)
    calls = harness._fake_chat(monkeypatch, [])
    # _fake_chat installs the real persisted-document fixture executor.
    original = execute.execute_action
    async def partial(*args, **kwargs):
        with db()() as session:
            venture = session.get(Venture, vid)
            jid = enqueue(session, 'plan.compile', {}, workspace_id=venture.workspace_id, venture_id=vid)
            session.flush()
            session.get(Job, jid).status = 'failed_dead'
            session.commit()
        receipts.queued_job(jid)
        if companion:
            return await original(*args, **kwargs)
        calls.append('failed-job')
        return {'kind': 'text', 'body_md': 'Done', 'actions': []}
    monkeypatch.setattr(execute, 'execute_action', partial)
    assert _invoke(vid, 'plan.generate')[1:] == ('held', [])
    assert service._unresolved_admission(vid, 'plan.generate')
    assert _invoke(vid, 'plan.generate')[1:] == ('held', [])
    assert len(calls) == 1


def test_common_fence_excludes_cards_pmf_and_child_tasks(env, monkeypatch):
    client, vid = env
    from app.autopilot import actions, service
    _on(client, vid)
    calls = harness._fake_chat(monkeypatch, [])
    actor = service._owner(vid)
    proposal = {'kind': 'plan.generate', 'payload': {}, 'title': 'Owned test'}
    async def trial():
        with service._tick_fence(vid) as locked:
            assert locked
            result = await actions.execute_proposed_action(vid, proposal, actor)
            assert result['reason'] == 'already_running'
            assert (await service.pmf_iterate(vid, actor, human=True))['kind'] == 'wait'
        with service._admission_fence(vid, actor) as locked:
            assert locked
            with service._admission_fence(vid, actor) as nested:
                assert nested
            with service._admission_fence(vid, 'different-actor') as foreign:
                assert not foreign
            with service._admission_fence('distinct-venture', actor) as independent:
                assert independent
            async def child():
                with service._admission_fence(vid, actor) as acquired:
                    return acquired
            assert not await asyncio.create_task(child())
        assert calls == [] and not any(e["outcome"] == "attempt" for e in service.timeline(vid))
    asyncio.run(trial())


def test_concurrent_cards_have_one_durable_effect_and_reservation(env, monkeypatch):
    client, vid = env
    from app.autopilot import actions, service
    from app.chat import execute
    _on(client, vid)
    calls = harness._fake_chat(monkeypatch, [])
    original = execute.execute_action
    proposal = {'kind': 'plan.generate', 'payload': {}, 'title': 'Owned test'}
    async def trial():
        entered, release = asyncio.Event(), asyncio.Event()
        async def delayed(*args, **kwargs):
            entered.set()
            await release.wait()
            return await original(*args, **kwargs)
        monkeypatch.setattr(execute, 'execute_action', delayed)
        first = asyncio.create_task(actions.execute_proposed_action(vid, proposal, service._owner(vid)))
        await asyncio.wait_for(entered.wait(), 5)
        try:
            second = await actions.execute_proposed_action(vid, proposal, service._owner(vid))
            assert second['reason'] == 'already_running'
        finally:
            release.set()
        assert (await first)['ok']
    asyncio.run(trial())
    assert len(calls) == 1
    events = service.timeline(vid)
    assert sum(e['outcome'] == 'attempt' for e in events) == 1
    assert sum(e['outcome'] == 'completed' for e in events) == 1


@pytest.mark.parametrize('companion', ['persisted', 'active_job', 'succeeded_job'])
def test_failed_queue_never_masks_committed_companions(env, companion):
    _, vid = env
    from app.autopilot import receipts, service
    from app.autopilot.authority import founder_scope
    from app.core.jobs import enqueue
    from app.db.models import Job, Venture
    from app.db.supymem_models import ExistingAsset
    from app.state import db
    from core.clock import utcnow
    with founder_scope(vid, service._owner(vid)):
        with receipts.collect(vid) as evidence:
            with db()() as session:
                venture = session.get(Venture, vid)
                failed = enqueue(session, 'plan.compile', {'owned_test': 'failed'},
                    workspace_id=venture.workspace_id, venture_id=vid)
                session.flush()
                session.get(Job, failed).status = 'failed_dead'
                if companion == 'persisted':
                    session.add(ExistingAsset(id='partial_asset', workspace_id=venture.workspace_id,
                        venture_id=vid, kind='resource_inventory', name='Owned',
                        source='founder_inventory', created_at=utcnow()))
                else:
                    other = enqueue(session, 'plan.compile', {'owned_test': 'companion'},
                        workspace_id=venture.workspace_id, venture_id=vid)
                    session.flush()
                    session.get(Job, other).status = 'scheduled' if companion == 'active_job' else 'succeeded'
                session.commit()
            if companion == 'persisted':
                receipts.persisted_record(ExistingAsset, 'partial_asset', {'source': 'founder_inventory'})
        assert evidence.outcome() == 'held'


@pytest.mark.parametrize("exception", [ValueError("unknown_failure"), RuntimeError("research_confirmation_required")])
def test_unreported_committed_job_then_exception_is_held_not_replayed(env, monkeypatch, exception):
    client, vid = env
    from app.autopilot import receipts, service
    from app.chat import execute
    from app.research import service as research
    from app.core.jobs import enqueue
    from app.db.models import Job, Venture
    from app.state import db
    _on(client, vid)
    _complete_local_startup(vid)
    calls = []
    monkeypatch.setattr(execute, '_confirm_idea_for_research', lambda *_: None)
    monkeypatch.setattr(receipts, 'queued_job', lambda _: None)
    def partial(_vid):
        calls.append(1)
        with db()() as session:
            venture = session.get(Venture, vid)
            enqueue(session, 'research.deep', {}, workspace_id=venture.workspace_id, venture_id=vid)
            session.commit()
        raise exception  # neither arbitrary exception nor its text proves preflight
    monkeypatch.setattr(research, 'start', partial)
    assert _invoke(vid)[1:] == ('held', [])
    assert _invoke(vid)[1:] == ('held', [])
    assert len(calls) == 1 and service._unresolved_admission(vid, 'research.start')
    with db()() as session:
        assert session.query(Job).filter_by(venture_id=vid, kind='research.deep').count() == 1


def test_actual_research_confirmation_preflight_is_recoverable(env, monkeypatch):
    client, vid = env
    from app.autopilot import service
    from app.chat import execute
    from app.db.models import Job
    from app.state import db
    _on(client, vid)
    monkeypatch.setattr(execute, '_confirm_idea_for_research', lambda *_: None)
    assert _invoke(vid)[1:] == ('needs_you', [])
    assert not service._unresolved_admission(vid, 'research.start')
    with db()() as session:
        assert session.query(Job).filter_by(venture_id=vid, kind='research.deep').count() == 0
    _confirmed(vid)
    assert _invoke(vid)[1:] == ('queued', [])
    assert not service._unresolved_admission(vid, 'research.start')
    with db()() as session:
        assert session.query(Job).filter_by(venture_id=vid, kind='research.deep').count() == 1
