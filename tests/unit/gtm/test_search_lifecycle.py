"""Customer searches keep their confirmed inputs and leave durable chat outcomes."""
import asyncio
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from tests.unit.gtm.test_journey import journey_db as journey_db, _expire_search_replay
from tests.unit.gtm.test_journey_signals import _leased_payload


@pytest.fixture(params=[
    ("Independent bicycle repair workshops in Ireland", "Keep repair appointments organized", "b2b"),
    ("Family caregivers in Australia", "Coordinate shared care routines", "consumer"),
])
def search_domain(journey_db, monkeypatch, request):
    from app.db.models import Venture
    from app.db.supymem_models import IdeaRevision, IdeaSession
    from app.gtm import journey
    from core.clock import utcnow

    data = journey_db | dict(zip(("audience", "problem", "business_model"), request.param))
    monkeypatch.setattr(journey, "_icp_seed", data["stored_seed"])
    now = utcnow()
    with data["factory"]() as session:
        session.get(Venture, data["vid"]).raw_idea = f"{data['problem']}\nAudience: {data['audience']}"
        session.add(IdeaSession(id="search_idea", workspace_id=data["wid"], venture_id=data["vid"],
            case="know", state="confirmed", current_revision_id="search_revision", confirmed_revision_id="search_revision",
            created_at=now, updated_at=now))
        session.flush()
        session.add(IdeaRevision(id="search_revision", session_id="search_idea", workspace_id=data["wid"],
            venture_id=data["vid"], rev=1, source="founder", summary=data["problem"],
            structured=json.dumps({"customer": data["audience"], "problem": data["problem"],
                                   "business_model": data["business_model"]}), created_at=now))
        session.commit()
    return data


def test_search_start_follows_your_request(search_domain, monkeypatch):
    from datetime import timedelta
    from app.chat import store
    from app.chat.store import append_message, list_messages
    from app.gtm import journey
    from core.clock import utcnow

    data = search_domain
    search = journey.update_icp(data["wid"], data["vid"], data["audience"],
        problem=data["problem"], recruitment_opt_in=True)
    now = utcnow()
    monkeypatch.setattr(store, "utcnow", lambda: now)
    request = append_message(data["vid"], role="user", body_md=f"Find people for {data['audience']}")
    now += timedelta(seconds=1)
    started = append_message(data["vid"], role="assistant", kind="gtm_search_progress",
        body_md="Your customer search is starting. You can check progress below.",
        citations=[{"gtm_search_progress": {"job_id": search["job_id"]}},
                   {"job_started": {"job_id": search["job_id"]}}])
    messages = [item["id"] for item in list_messages(data["vid"])]
    assert messages.index(request["id"]) < messages.index(started["id"])
    assert started["reply_to_message_id"] == request["id"]


def test_search_status_replies_follow_your_request_and_show_current_work(search_domain, monkeypatch):
    from datetime import timedelta
    from tests.unit.test_local_mode import _owner_client as TestClient
    from app import main
    from app.chat import store
    from app.chat.store import append_message, list_messages
    from app.db.models import Job
    from app.gtm import journey
    from core.clock import utcnow

    data = search_domain
    search = journey.update_icp(data["wid"], data["vid"], data["audience"],
        problem=data["problem"], recruitment_opt_in=True)
    monkeypatch.setattr("app.admin.authn.verify_caller", lambda *args, **kwargs: SimpleNamespace(sub=data["actor"]))
    with TestClient(main.create_app()) as client:
        client.headers["X-Subject"] = data["actor"]
        now = utcnow()
        monkeypatch.setattr(store, "utcnow", lambda: now)
        queued = client.post(f"/ventures/{data['vid']}/chat/confirm", json={"action": "gtm.status", "idempotency_key": "queued-status"})
        assert queued.status_code == 200, queued.text
        from app.core.local_mode import is_local
        assert ("Your customer search is queued. Check progress here in a moment." if is_local() else "has not started yet") in queued.json()["body_md"]
        with data["factory"]() as session:
            job = session.get(Job, search["job_id"])
            job.status = "leased"
            row = journey._row(session, data["vid"])
            document = json.loads(row.provenance_json)
            document["_runtime"].update(progress_step="judging", progress_percent=55,
                started_at=(now - timedelta(minutes=10)).isoformat())
            journey._store(session, row, document)
            session.commit()
        now += timedelta(minutes=10)
        request = append_message(data["vid"], role="user", body_md="What are you doing now?")
        now += timedelta(seconds=1)
        running = client.post(f"/ventures/{data['vid']}/chat/confirm", json={"action": "gtm.status", "idempotency_key": "running-status"})
        assert running.status_code == 200, running.text
        assert "Checking fit (55%)" in running.json()["body_md"]
        assert "has not started yet" not in running.json()["body_md"]
        assert running.json()["reply_to_message_id"] == request["id"]
        assert running.json()["id"] != queued.json()["id"]
        replay = client.post(f"/ventures/{data['vid']}/chat/confirm", json={"action": "gtm.status", "idempotency_key": "running-status"})
        assert replay.status_code == 200 and replay.json()["id"] == running.json()["id"]
        messages = [item["id"] for item in list_messages(data["vid"])]
        assert messages.index(request["id"]) < messages.index(running.json()["id"])
        with data["factory"]() as session:
            session.get(Job, search["job_id"]).status = "failed_dead"
            session.get(Job, search["job_id"]).last_error = "public_search_unavailable:private-provider-detail"
            session.commit()
        failed = client.post(f"/ventures/{data['vid']}/chat/confirm", json={"action": "gtm.status", "idempotency_key": "failed-status"})
        assert failed.status_code == 200, failed.text
        assert "stopped" in failed.json()["body_md"] and "private-provider-detail" not in failed.json()["body_md"]
        assert any(action.get("action") == "gtm.find" and action.get("payload", {}).get("retry") is True
            for action in failed.json()["actions"])
        with data["factory"]() as session:
            session.get(Job, search["job_id"]).status = "succeeded"
            document["state"] = "needs_input"
            document["error_code"] = "no_public_candidates"
            journey._store(session, journey._row(session, data["vid"]), document)
            session.commit()
        finished = client.post(f"/ventures/{data['vid']}/chat/confirm", json={"action": "gtm.status", "idempotency_key": "finished-status"})
        assert finished.status_code == 200, finished.text
        assert "has not started yet" not in finished.json()["body_md"]
        assert finished.json()["gtm_search_progress"]["state"] == "needs_input"
        assert finished.json()["actions"]


def test_identical_running_search_returns_existing_job(search_domain):
    from datetime import timedelta
    from app.core.local_mode import is_local
    from app.db.models import Job, Workspace
    from app.gtm import journey
    from core.clock import utcnow

    data = search_domain
    arguments = {"problem": data["problem"], "recruitment_opt_in": True}
    first = journey.update_icp(data["wid"], data["vid"], data["audience"], **arguments)
    assert first["job_id"]
    for state, phase in (("scheduled", "queued"), ("running", "running")):
        with data["factory"]() as session:
            job = session.get(Job, first["job_id"])
            job.status = state
            row = journey._row(session, data["vid"])
            document = json.loads(row.provenance_json)
            document["state"] = phase
            document["error_code"] = None
            journey._store(session, row, document)
            original = row.provenance_json
            session.commit()
        assert journey.prepare(data["wid"], data["vid"], retry=True)["job_id"] == first["job_id"]
        assert journey.update_icp(data["wid"], data["vid"], data["audience"], **arguments)["job_id"] == first["job_id"]
        assert journey.update_icp(data["wid"], data["vid"], data["audience"],
            search_queries=first["icp"]["search_queries"], note=" ", **arguments)["job_id"] == first["job_id"]
        with data["factory"]() as session:
            assert len(list(session.scalars(select(Job).where(Job.kind == "gtm.journey.prepare")))) == 1
            assert journey._row(session, data["vid"]).provenance_json == original
            assert not session.get(Job, first["job_id"]).cancel_requested
    with data["factory"]() as session:
        job = session.get(Job, first["job_id"])
        job.status, job.finished_at = "succeeded", utcnow()
        session.commit()
    changed = journey.prepare(data["wid"], data["vid"], retry=True, note="Prioritize independently published contact routes")
    assert changed["job_id"] != first["job_id"]
    with data["factory"]() as session:
        job = session.get(Job, changed["job_id"])
        job.status, job.finished_at = "succeeded", utcnow() - timedelta(seconds=job.timeout_s + 1)
        row = journey._row(session, data["vid"])
        document = json.loads(row.provenance_json)
        document["state"] = "ready"
        journey._store(session, row, document)
        session.commit()
    latest = journey.prepare(data["wid"], data["vid"], retry=True)
    assert latest["job_id"] != changed["job_id"]
    with data["factory"]() as session:
        session.get(Workspace, data["wid"]).tenancy = "managed"
        session.commit()
    blocked = journey.update_icp(data["wid"], data["vid"], data["audience"], **arguments)
    if is_local():
        assert blocked["state"] == "queued" and blocked["processing_blocked_reason"] is None
    else:
        assert blocked["state"] == "needs_input" and blocked["processing_blocked_reason"] == "aup_required"
    assert blocked["job_id"] == latest["job_id"]
    with data["factory"]() as session:
        assert len(list(session.scalars(select(Job).where(Job.kind == "gtm.journey.prepare")))) == 3


@pytest.mark.parametrize("audience", ["Scheduling for dental practices", "Meal planning for home cooks"])
def test_waiting_search_copy_keeps_the_reason_without_claiming_live_percentage(audience):
    from tests.company_os_harness import load_company_os_app
    load_company_os_app()
    from app.chat import customers
    from app.core.local_mode import is_local

    view = {"state": "queued", "job_id": "saved_search", "discovery_started": True, "icp": {"who": audience}}
    reply = customers.render("venture", view)
    queued = "Your customer search is queued." if is_local() else "Starting soon. Your search is saved but has not started yet."
    assert queued in reply["body_md"]
    assert audience in reply["body_md"] and "worker" not in reply["body_md"]
    assert ".." not in reply["body_md"]
    view["state"] = "running"
    view["research_progress"] = {"state": "queued", "percent": 45,
        "step_label": "Starting soon. Waiting before trying again after a temporary problem."}
    reply = customers.render("venture", view)
    assert "temporary problem" in reply["body_md"] and "(45%)" not in reply["body_md"]
    view["research_progress"].update(state="running", step_label="Searching public sources")
    assert "Searching public sources (45%)" in customers.render("venture", view)["body_md"]


def test_people_search_results_report_shortfalls_even_with_other_public_sources(search_domain, monkeypatch):
    from app.chat import customers

    data = search_domain
    monkeypatch.setattr(customers, "_send_section", lambda *args: ("", []))
    view = {"state": "ready", "job_id": "shortfall-search", "discovery_started": True,
        "icp": {"who": data["audience"], "business_model": data["business_model"]},
        "search_target": {"target_type": "person", "requested_count": 5},
        "channels": [{"name": "Public discussion group", "url": "https://community.example.org/discussions"}]}
    for found in (3, 0):
        view["reachable_leads"] = [{"id": f"public-person-{index}", "kind": "person", "name": f"Person {index}",
            "role": data["audience"], "source_url": f"https://profiles.example.org/person-{index}"} for index in range(found)]
        reply = customers.render(data["vid"], view)
        assert f"You found {found} of 5 people" in reply["body_md"]
        assert reply["actions"] or "Try a broader" in reply["body_md"] or "Review each public source" in reply["body_md"]
    view.update(state="idle", job_id=None, discovery_started=False)
    assert "You found 0 of 5 people" not in customers.render(data["vid"], view)["body_md"]


def test_page_refresh_keeps_queued_and_running_search_snapshot_and_founder_edit_restarts(search_domain, monkeypatch):
    from app.core.jobs import FatalError, complete_job, fail_job
    from app.core.local_mode import is_local
    from app.credits import metered
    from app.db.models import ChatMessage, Job, Venture
    from app.db.supymem_models import IdeaRevision, IdeaSession
    from app.gtm import journey
    from app.ventures import idea_page, state_brief
    from core.clock import utcnow

    data = search_domain
    queued = journey.update_icp(data["wid"], data["vid"], data["audience"], problem=data["problem"], recruitment_opt_in=True)
    assert queued["state"] == "queued"
    assert queued["icp"]["problem_revision_id"] == "search_revision"
    payload = _leased_payload(data)
    calls = []

    async def completion(*args, **kwargs):
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps({
            "summary": "Generated summary of saved work", "problem": "A paraphrased problem from saved work",
            "customers": {"current": ["A generated audience description"]}})))])

    async def discover(who, *args, problem, **kwargs):
        calls.append((who, problem))
        return []

    monkeypatch.setattr(metered, "llm", completion)
    monkeypatch.setattr(state_brief, "get_venture_state_brief", lambda *args, **kwargs: {"idea": {"audience": data["audience"]}})
    monkeypatch.setattr(idea_page, "experiments", lambda *args: [])
    monkeypatch.setattr(journey, "_discover", discover)
    monkeypatch.setattr(journey, "_draft_ready", lambda *args: 0)
    for state in ("queued", "running"):
        with data["factory"]() as session:
            row = journey._row(session, data["vid"])
            saved = json.loads(row.provenance_json)
            saved["state"] = state
            journey._store(session, row, saved)
            session.commit()
        asyncio.run(idea_page.refresh({"venture_id": data["vid"]}))
        with data["factory"]() as session:
            idea = session.get(IdeaSession, "search_idea")
            assert idea.current_revision_id == "search_revision"
            assert session.get(Venture, data["vid"]).raw_idea == f"{data['problem']}\nAudience: {data['audience']}"
            assert journey._prepare_fence(session, payload)[1]["icp"]["problem"] == data["problem"]
        assert journey.status(data["wid"], data["vid"])["icp"]["who"] == data["audience"]

    with data["factory"]() as session:
        idea = session.get(IdeaSession, "search_idea")
        session.add(IdeaRevision(id="generated_revision", session_id=idea.id, workspace_id=data["wid"],
            venture_id=data["vid"], rev=2, source="agent", summary="Updated generated copy",
            structured=json.dumps({"customer": "Generated audience copy", "problem": "Generated problem copy"}), created_at=utcnow()))
        idea.current_revision_id, idea.state = "generated_revision", "reviewing"
        session.commit()
    assert journey.prepare(data["wid"], data["vid"], retry=True)["icp"]["problem"] == data["problem"]
    asyncio.run(journey.run_prepare(payload))
    with data["factory"]() as session:
        complete_job(session, payload["_job_id"], worker_id=payload["_worker_id"], attempt=payload["_job_attempt"])
        session.commit()
        assert session.get(Job, payload["_job_id"]).status == "succeeded"
        notices = list(session.scalars(select(ChatMessage).where(ChatMessage.venture_id == data["vid"])))
        assert len(notices) == 1
        notice = notices[0]
        if is_local():
            assert "You found 0 contacts. You have 0 email drafts. Nothing was sent." in notice.body_md
            assert "Search again" in notice.body_md
            assert any(action.get("action") == "gtm.find" and action["payload"]["retry"] is True
                       for action in json.loads(notice.actions_json))
        else:
            assert "found nothing usable" in notice.body_md
    assert calls == [(data["audience"], data["problem"])]

    _expire_search_replay(data, payload["_job_id"])
    journey.prepare(data["wid"], data["vid"], retry=True)
    old = _leased_payload(data)
    journey._journal(old).save_operation(old["journey_id"], "unsettled-search", {"state": "uncertain", "usage": {"reservation_id": "still-held"}})
    audience, problem = data["audience"] + " in rural areas", data["problem"] + " with fewer repeated steps"
    restarted = journey.update_icp(data["wid"], data["vid"], audience, problem=problem, recruitment_opt_in=True)
    assert restarted["state"] == "queued" and restarted["job_id"] != old["_job_id"]
    assert (restarted["icp"]["who"], restarted["icp"]["problem"]) == (audience, problem)
    with data["factory"]() as session:
        saved = json.loads(journey._row(session, data["vid"]).provenance_json)
        assert saved["_runtime"]["operations"]["unsettled-search"]["usage"]["reservation_id"] == "still-held"
        assert fail_job(session, old["_job_id"], FatalError("gtm_prepare_superseded"), worker_id=old["_worker_id"], attempt=old["_job_attempt"]) == "cancelled"
        session.commit()
        assert len(list(session.scalars(select(ChatMessage).where(ChatMessage.kind == "gtm_search_failed")))) == 0


def test_failed_search_reports_reason_retry_and_one_unsolicited_chat_notice(search_domain):
    from app.chat import customers, milestone_hooks
    from app.chat.execute import execute_action
    from app.chat.store import append_message
    from app.core.jobs import FatalError, _watchdog_failure, fail_job
    from app.db.models import ChatMessage, Job
    from app.gtm import journey

    data = search_domain
    journey.update_icp(data["wid"], data["vid"], data["audience"], problem=data["problem"], recruitment_opt_in=True)
    payload = _leased_payload(data)
    code = "gtm_problem_confirmation_changed"
    with data["factory"]() as session:
        assert fail_job(session, payload["_job_id"], FatalError(code), worker_id=payload["_worker_id"], attempt=payload["_job_attempt"]) == "failed_dead"
        session.commit()
        _watchdog_failure(session, session.get(Job, payload["_job_id"]))
        milestone_hooks.content_failed(session, data["vid"], "gtm.journey.prepare", payload["_job_id"], code)
        session.commit()
        notices = list(session.scalars(select(ChatMessage).where(ChatMessage.kind == "gtm_search_failed")))
        assert len(notices) == 1
        assert "Your customer search stopped" in notices[0].body_md and "saved idea was updated" in notices[0].body_md
        actions = json.loads(notices[0].actions_json)
        assert {action["label"] for action in actions} >= {"Retry", "Do it another way"}
    for status in ("failed", "failed_dead", "poison", "cancelled"):
        with data["factory"]() as session:
            session.get(Job, payload["_job_id"]).status = status
            session.commit()
        view = journey.status(data["wid"], data["vid"])
        assert view["state"] == "failed" and view["error_code"] == code
        assert view["research_progress"]["step_label"] == "Search stopped"
        assert "Your customer search stopped" in view["message"]
        reply = asyncio.run(customers.execute(data["vid"], "gtm.status", {}, data["actor"]))
        assert "Queued" not in reply["body_md"] and "gtm_problem_confirmation_changed" not in reply["body_md"]
        assert reply["actions"][0]["label"] == "Retry"
        assert reply["actions"][1]["label"] == "Do it another way"
        checked = append_message(data["vid"], role="assistant", body_md=reply["body_md"], kind=reply["kind"],
            citations=reply["citations"], actions=reply["actions"], idempotency_key="failed-check-" + status)
        assert checked["id"] == notices[0].id
    retry = reply["actions"][0]["payload"]
    assert not reply["actions"][0].get("confirmation")
    assert retry["retry"] is True
    result = asyncio.run(execute_action(data["vid"], "gtm.find", payload=retry, actor_subject=data["actor"]))
    from app.core.local_mode import is_local
    label = "Your customer search is queued. Check progress here in a moment." if is_local() else "Starting soon. Your work is saved but has not started yet."
    assert label in result["body_md"] and "(0%)" not in result["body_md"]


@pytest.mark.parametrize("search_domain", [
    ("Warehouse supervisors in Norway", "Schedule loading bay appointments", "b2b"),
    ("Amateur stargazers in Chile", "Organize local night-sky outings", "consumer"),
], indirect=True)
def test_progress_taps_answer_each_request_and_polled_stages_remain_live(search_domain, monkeypatch):
    from tests.unit.test_local_mode import _owner_client as TestClient
    from app import main
    from app.chat import customers
    from app.chat.store import append_message
    from app.core.jobs import complete_job
    from app.db.models import ChatMessage, Job
    from app.gtm import journey
    from app.gtm.progress import compute_progress

    data = search_domain
    journey.update_icp(data["wid"], data["vid"], data["audience"], problem=data["problem"], recruitment_opt_in=True)
    monkeypatch.setattr("app.admin.authn.verify_caller", lambda *args, **kwargs: SimpleNamespace(sub=data["actor"]))
    with TestClient(main.create_app()) as client:
        client.headers["X-Subject"] = data["actor"]
        messages = [client.post(f"/ventures/{data['vid']}/chat/confirm", json={"action": "gtm.status", "idempotency_key": f"progress-tap-{index}"}) for index in range(3)]
        assert all(response.status_code == 200 for response in messages), [response.text for response in messages]
        assert len({response.json()["id"] for response in messages}) == 3
        assert messages[0].json()["citations"] == []
        reference = messages[0].json()["gtm_search_progress"]
        assert reference["audience"] == data["audience"]
        initial_work = client.get(f"/ventures/{data['vid']}/chat/work").json()
        assert initial_work["research_progress"]["job_id"] == reference["job_id"]
        from app.core.local_mode import is_local
        label = "Your customer search is queued. Check progress here in a moment." if is_local() else "Starting soon. Your work is saved but has not started yet."
        assert initial_work["research_progress"]["step_label"] == label
        assert label in messages[0].json()["body_md"] and "(0%)" not in messages[0].json()["body_md"]
        payload = _leased_payload(data)
        ids = {response.json()["id"] for response in messages}
        for stage, label in (("searching", "Searching public sources"), ("judging", "Checking fit"), ("contacting", "Finding contact routes"), ("drafting", "Writing drafts")):
            if is_local() and stage == "searching":
                label = "Searching..."
            def advance(row, saved, session):
                saved["state"] = "running"
            journey._journal(payload).mutate(payload["journey_id"], payload, advance)
            if stage == "contacting":
                async def published_route():
                    return "A published contact route on the source page"
                asyncio.run(journey._signal_call(payload, "contact:public.example", "https://public.example/contact", published_route))
            else:
                journey._progress(payload, stage, [stage + "-operation"])
            reply = client.post(f"/ventures/{data['vid']}/chat/confirm", json={"action": "gtm.status"})
            again = client.post(f"/ventures/{data['vid']}/chat/confirm", json={"action": "gtm.status"})
            assert reply.status_code == again.status_code == 200
            assert reply.json()["id"] != again.json()["id"]
            assert label.rstrip(".") in reply.json()["body_md"] and label.rstrip(".") in again.json()["body_md"]
            live = client.get(f"/ventures/{data['vid']}/chat/work").json()["research_progress"]
            assert live["job_id"] == reference["job_id"] and live["run_id"] == reference["run_id"]
            assert live["step_label"] == label
            assert isinstance(live["percent"], int) and 0 <= live["percent"] <= 99
            ids.update((reply.json()["id"], again.json()["id"]))
        with data["factory"]() as session:
            session.get(Job, payload["_job_id"]).status = "retry_wait"
            session.commit()
            saved = json.loads(journey._row(session, data["vid"]).provenance_json)
            assert compute_progress(saved, session.get(Job, payload["_job_id"]))["step_label"] == "Starting soon. Your work is ready to try again."
            session.get(Job, payload["_job_id"]).status = "leased"
            session.commit()
            complete_job(session, payload["_job_id"], worker_id=payload["_worker_id"], attempt=payload["_job_attempt"])
            saved["state"] = "needs_input"
            saved["error_code"] = "no_public_candidates"
            journey._store(session, journey._row(session, data["vid"]), saved)
            session.commit()
        run_id = saved["run_id"]
        finished_work = client.get(f"/ventures/{data['vid']}/chat/work").json()
        assert finished_work["research_progress"]["state"] == "done"
        assert finished_work["research_progress"]["percent"] == 100
        assert reference["job_id"] not in {item["id"] for item in finished_work["items"]}
        from app.chat.conversations import create
        other = create(data["vid"], idempotency_key="another-search-conversation")
        assert client.get(f"/ventures/{data['vid']}/chat/work", params={"conversation_id": other["id"]}).json()["research_progress"] is None
        completed = asyncio.run(customers.execute(data["vid"], "gtm.status", {}, data["actor"]))
        assert completed["citations"][0]["gtm_search_progress"]["run_id"] == run_id
        assert customers.post_results(data["vid"], run_id)
        assert not customers.post_results(data["vid"], run_id)
        done = client.post(f"/ventures/{data['vid']}/chat/confirm", json={"action": "gtm.status"})
        again = client.post(f"/ventures/{data['vid']}/chat/confirm", json={"action": "gtm.status"})
        assert done.status_code == again.status_code == 200 and done.json()["id"] == again.json()["id"]
        with data["factory"]() as session:
            notices = list(session.scalars(select(ChatMessage).where(ChatMessage.kind != "gtm_search_progress")))
            assert len(notices) == 1 and done.json()["id"] == notices[0].id
        journey.prepare(data["wid"], data["vid"], retry=True)
        fresh = client.post(f"/ventures/{data['vid']}/chat/confirm", json={"action": "gtm.status"})
        assert fresh.status_code == 200 and fresh.json()["id"] not in ids
        unrelated = append_message(data["vid"], role="user", body_md="Check the saved search")
        repeated = append_message(data["vid"], role="user", body_md="Check the saved search")
        assert unrelated["id"] != repeated["id"]
        malformed = append_message(data["vid"], role="assistant", kind="gtm_search_failed", body_md="Your saved work is kept.",
            citations=[{"gtm_search_progress": {"job_id": 42, "run_id": "invalid", "state": []}}])
        assert malformed["citations"] == []
    with data["factory"]() as session:
        bubbles = list(session.scalars(select(ChatMessage).where(ChatMessage.kind == "gtm_search_progress")))
        assert len(bubbles) == 12
