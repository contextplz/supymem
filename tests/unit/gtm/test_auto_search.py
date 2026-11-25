import asyncio
from datetime import timedelta

import pytest
from tests.unit.gtm.test_journey import journey_db, _mail, _work
from tests.unit.gtm.test_reviewed_ports import safety_runtime as safety_runtime


@pytest.fixture(params=[("Dental clinic owners in the US", "Manage appointment cancellations"),
                       ("Security operations teams", "Investigate infrastructure alerts")], ids=["local", "b2b"])
def auto_journey(journey_db, monkeypatch, request):
    from app.autopilot import outreach
    from app.db.models import Job
    from app.gtm import journey

    data = journey_db | {"audience": request.param[0], "problem": request.param[1]}
    monkeypatch.setattr(outreach, "_brief", lambda vid: {"idea": {
        "audience": data["audience"], "problem": data["problem"], "confirmed": True}, "research": {}})
    journey.update_icp(data["wid"], data["vid"], data["audience"], problem=data["problem"], start=False)
    view = journey.prepare(data["wid"], data["vid"])
    with data["factory"]() as session:
        session.get(Job, view["job_id"]).status = "succeeded"
        row = journey._row(session, data["vid"])
        saved = journey._json(row.provenance_json)
        saved.update(state="ready", seen_lead_urls=["https://already.example"])
        journey._store(session, row, saved)
        session.commit()
    journey.update_settings(data["wid"], data["vid"], {"auto_search": {"enabled": True, "target": 7}})
    with data["factory"]() as session:
        row = journey._row(session, data["vid"])
        saved = journey._json(row.provenance_json)
        saved["auto_search"]["last_run_at"] = (journey.utcnow() - timedelta(days=3)).isoformat()
        journey._store(session, row, saved)
        session.commit()
    return data


@pytest.mark.parametrize("case,backlog,cap,expected", [
    ("below_cap", 14, 0, True), ("full_day", 15, 0, False),
    ("custom_below", 1, 2, True), ("custom_full", 2, 2, False),
    ("ineligible", 15, 0, True), ("running", 0, 0, False),
    ("spacing", 0, 0, False), ("credits", 0, 0, False),
    ("inactive", 0, 0, False), ("empty_runs", 0, 0, False), ("paused", 0, 0, False),
])
def test_auto_search_replenishes_eligible_backlog_and_stops_once_for_credits(auto_journey, monkeypatch, case, backlog, cap, expected):
    from sqlalchemy import select
    from app.autopilot import guard
    from app.chat import customers
    from app.chat.store import list_messages
    from app.core.local_mode import is_local
    from app.db.models import CreditAccount, GtmContact, Job
    from app.db.supymem_models import AutopilotSetting
    from app.gtm import journey

    data = auto_journey
    local = is_local()
    if local:
        assert data["account"] is None
    _mail(data)
    now = journey.utcnow()
    monkeypatch.setattr(journey, "utcnow", lambda: now)
    if case == "paused":
        monkeypatch.setattr("app.ventures.lifecycle.is_paused", lambda vid: True)
    journey._ingest(data["wid"], data["vid"], [{"website": f"https://backlog{index}.example", "enriched": True,
        "email": f"hello@backlog{index}.example", "organization": f"Public business {index}", "country": "US"}
        for index in range(backlog)])
    if cap:
        with data["factory"]() as session:
            session.add(AutopilotSetting(venture_id=data["vid"], send_max_per_day=cap, updated_at=now))
            session.commit()
    with data["factory"]() as session:
        row = journey._row(session, data["vid"])
        saved = journey._json(row.provenance_json)
        saved["auto_search"].update(last_run_at=(now - timedelta(hours=journey.AUTO_SEARCH_MIN_INTERVAL_HOURS)).isoformat())
        if case == "ineligible":
            contact = session.scalar(select(GtmContact))
            contact.recipient_geo = "DE"
            contact.lawful_basis = "none"
            session.flush()
            assert journey._block(session, data["wid"], contact) is not None
        elif case == "running":
            session.get(Job, saved["job_id"]).status = "leased"
        elif case == "spacing":
            saved["auto_search"]["last_run_at"] = (now - timedelta(hours=journey.AUTO_SEARCH_MIN_INTERVAL_HOURS, microseconds=-1)).isoformat()
        elif case == "credits":
            if not local:
                session.get(CreditAccount, data["account"]).granted_micro = saved["auto_search"]["ceiling_micro"] - 1
            saved["auto_search"]["last_run_at"] = now.isoformat()
        elif case == "inactive":
            if local:
                expected = True
            else:
                session.get(CreditAccount, data["account"]).status = "paused"
        elif case == "empty_runs":
            saved["auto_search"]["empty_runs"] = 2
        session.commit()
        assert journey._auto_search_due(session, row, saved, data["wid"], data["vid"]) is expected
        assert guard.get_settings(data["vid"])["allow_send"] is False
        if case == "credits" and not local:
            assert saved["auto_search"]["stop_reason"] == "credits_low"
            assert saved["auto_search"]["enabled"] is False
            assert journey._auto_search_due(session, row, saved, data["wid"], data["vid"]) is False
        elif case in {"credits", "inactive"} and local:
            assert saved["auto_search"]["enabled"] is True
            assert saved["auto_search"].get("stop_reason") is None
        journey._store(session, row, saved)
        session.commit()
    if case == "credits":
        notices = [message for message in list_messages(data["vid"]) if "You have reached your usage limit" in message.get("body_md", "")]
        assert len(notices) == (0 if local else 1)
        if not local:
            assert 'credit' not in notices[0]['body_md'].lower() and 'Continue finding customers' in notices[0]['body_md']
            assert "—" not in notices[0]["body_md"]
    description = customers._auto_search_choice(saved["auto_search"])[1]
    assert f"{journey.AUTO_SEARCH_MIN_INTERVAL_HOURS} hours apart" in description
    assert "less than a day of eligible contacts" in description


@pytest.mark.parametrize("block", [None, "first_run", "not_due", "running", "target", "credits", "paused"])
def test_maintenance_starts_only_due_funded_searches(auto_journey, monkeypatch, block):
    from app.autopilot import guard, outreach
    from app.core.local_mode import is_local
    from app.db.models import CreditAccount, GtmCompany, GtmContact, GtmTouch, Job
    from app.gtm import journey

    data = auto_journey
    monkeypatch.setattr(outreach, "poll_replies", lambda vid: asyncio.sleep(0))
    monkeypatch.setattr(outreach, "schedule_followups", lambda vid: None)
    with data["factory"]() as session:
        row = journey._row(session, data["vid"])
        saved = journey._json(row.provenance_json)
        previous = saved["job_id"]
        if block == "first_run":
            saved.pop("job_id")
            saved.pop("discovery_opt_in", None)
        elif block == "not_due":
            saved["auto_search"]["last_run_at"] = journey.utcnow().isoformat()
        elif block == "running":
            session.get(Job, previous).status = "pending"
        elif block == "paused":
            saved["auto_search"]["enabled"] = False
        elif block == "credits" and not is_local():
            session.get(CreditAccount, data["account"]).granted_micro = journey.SEARCH_CEILING_MICRO - 1
        elif block == "target":
            for index in range(7):
                session.add(GtmCompany(id=f"business-{index}", workspace_id=data["wid"],
                    canonical_name=f"Business {index}", domain=f"seen{index}.example", created_at=journey.utcnow()))
            session.flush()
            for index in range(8):
                contact = GtmContact(id=f"contact-{index}", workspace_id=data["wid"], email=f"hello{index}@seen.example",
                    company_id=f"business-{index % 7}", source=f"venture:{data['vid']}:public",
                    stage_changed_at=journey.utcnow(), created_at=journey.utcnow())
                session.add(contact)
                for day in (1, 4):
                    session.add(GtmTouch(id=f"touch-{index}-{day}", workspace_id=data["wid"], venture_id=data["vid"],
                        contact_id=contact.id, direction="out", channel="email", simulated=0,
                        sent_at=journey.utcnow(), created_at=journey.utcnow()))
            session.flush()
            assert journey._contacted_businesses(session, data["wid"], data["vid"]) == 7
        journey._store(session, row, saved)
        session.commit()
    asyncio.run(journey.maintain_all())
    assert guard.get_settings(data["vid"])["allow_send"] is False
    view = journey.status(data["wid"], data["vid"])
    starts = block in {None, "first_run"} or block == "credits" and is_local()
    assert (view["job_id"] != previous) == starts
    with data["factory"]() as session:
        saved = journey._json(journey._row(session, data["vid"]).provenance_json)
    if starts:
        assert saved["_runtime"]["city_offset"] == 3
        assert "https://already.example" in saved["seen_lead_urls"]
        assert saved["_runtime"]["auto_run"]["ceiling_micro"] == journey.SEARCH_CEILING_MICRO
        asyncio.run(journey.maintain_all())
        assert journey.status(data["wid"], data["vid"])["job_id"] == view["job_id"]
    if block == "target":
        from app.chat.store import list_messages
        asyncio.run(journey.maintain_all())
        notices = [item for item in list_messages(data["vid"]) if "contact target" in item.get("body_md", "")]
        assert len(notices) == 1 and "7 of 7" in notices[0]["body_md"]
        action = notices[0]["actions"][0]
        if is_local():
            assert "confirmation" not in action and "ceiling_micro" not in action["payload"]["auto_search"]
            choice = action["payload"]
        else:
            choice = action["confirmation"]["resulting_state"]
        assert choice["auto_search"]["target"] > 7
    if block is None:
        with data["factory"]() as session:
            session.get(Job, view["job_id"]).status = "succeeded"
            row = journey._row(session, data["vid"])
            saved = journey._json(row.provenance_json)
            saved["auto_search"]["last_run_at"] = (journey.utcnow() - timedelta(days=3)).isoformat()
            journey._store(session, row, saved)
            session.commit()
        manual = journey.prepare(data["wid"], data["vid"], retry=True)
        with data["factory"]() as session:
            session.get(Job, manual["job_id"]).status = "succeeded"
            session.commit()
        asyncio.run(journey.maintain_all())
        assert journey.status(data["wid"], data["vid"])["job_id"] == manual["job_id"]


@pytest.mark.hosted_only
def test_auto_runs_report_new_leads_and_stop_after_two_empty_runs(auto_journey, monkeypatch):
    from app.autopilot import outreach
    from app.chat.store import list_messages
    from app.credits import metered
    from app.gtm import journey

    data = auto_journey
    batches = [[{"website": "https://fresh.example", "source_url": "https://fresh.example", "enriched": True,
                 "market_role": "customer", "organization": "New business"}], [], []]
    async def discover(*args, payload, **kwargs):
        await metered.call(lambda: asyncio.sleep(0, result={}), purpose="gtm.discovery.search", provider="firecrawl",
            workspace_id=data["wid"], venture_id=data["vid"], estimate_micro=100,
            request_id=payload["prepare_id"] + ":search", actual_micro=lambda response: 23)
        return batches.pop(0)
    def draft(wid, vid):
        if len(batches) != 2:
            return 0
        outreach._save_draft(vid, {"to": "hello@fresh.example", "contact_id": "fresh-contact", "day": 1,
            "channel": "email", "subject": "Question", "body": "A draft", "state": "draft"})
        return 1
    monkeypatch.setattr(journey, "_discover", discover)
    monkeypatch.setattr(journey, "_draft_ready", draft)
    for round_number in range(3):
        view = journey.prepare(data["wid"], data["vid"], authorize=False, auto_search=True)
        _work(data, view)
        view = journey.status(data["wid"], data["vid"])
        assert view["auto_search"]["runs"] == round_number + 1
        assert view["auto_search"]["empty_runs"] == max(0, round_number)
        with data["factory"]() as session:
            row = journey._row(session, data["vid"])
            saved = journey._json(row.provenance_json)
            assert saved["_runtime"]["auto_run"]["credits_used_micro"] == 23
            if round_number == 0:
                assert saved["_runtime"]["auto_run"]["new_businesses"] == 1
                assert saved["_runtime"]["auto_run"]["emails_queued"] == 1
            saved["auto_search"]["last_run_at"] = (journey.utcnow() - timedelta(days=3)).isoformat()
            journey._store(session, row, saved)
            session.commit()
    assert not view["auto_search"]["enabled"]
    previous = view["job_id"]
    assert journey.prepare(data["wid"], data["vid"], authorize=False, auto_search=True)["job_id"] == previous
    notices = list_messages(data["vid"])
    assert len([item for item in notices if "Your automatic search found" in item.get("body_md", "")]) == 3
    stopped = [item for item in notices if "Two searches in a row" in item.get("body_md", "")]
    assert len(stopped) == 1 and "1 new businesses" in stopped[0]["body_md"]
    assert stopped[0]["actions"][0]["confirmation"]["action"] == "gtm.auto_search"


def test_requested_automation_and_search_commands_apply_target_and_cap(auto_journey):
    from app.autopilot import guard
    from app.chat import customers
    from app.chat.execute import execute_action
    from app.core.local_mode import is_local
    from app.gtm import journey

    data = auto_journey
    _mail(data)
    if is_local():
        from app.core.local_secrets import update_settings

        update_settings("capabilities", {"background_work": True}, {})
    name, match = customers.command(data["vid"], "automate my emails")
    offer = asyncio.run(customers.run_command(data["vid"], name, match, "", data["actor"]))
    assert not any(action.get("confirmation") for action in offer["actions"])
    assert "7 businesses" in offer["body_md"] and "4 hours apart" in offer["body_md"]
    assert 'credit' not in offer['body_md'].lower()
    assert "—" not in offer["body_md"]
    assert guard.get_settings(data["vid"])["allow_send"]
    for text in ("stop finding new customers", "resume finding customers", "keep finding customers until 19"):
        name, match = customers.command(data["vid"], text)
        reply = asyncio.run(customers.run_command(data["vid"], name, match, text, data["actor"]))
        assert not any(action.get("confirmation") for action in reply["actions"])
        view = journey.status(data["wid"], data["vid"])
        assert view["auto_search"]["enabled"] == (name != "stop_search")
        assert guard.get_settings(data["vid"])["allow_send"]
    assert view["auto_search"]["target"] == 19
    assert journey.update_settings(data["wid"], data["vid"], {"auto_search": {"target": 1_000_001}})["auto_search"]["target"] == 1_000_001
    with pytest.raises(ValueError, match="gtm_auto_search_invalid"):
        journey.update_settings(data["wid"], data["vid"], {"auto_search": {"target": True}})
    with data["factory"]() as session:
        session.delete(journey._row(session, data["vid"]))
        session.commit()
    first = customers.automate_card()["actions"][0]["confirmation"]
    asyncio.run(execute_action(data["vid"], "gtm.automate", payload=first["resulting_state"], actor_subject=data["actor"]))
    view = journey.status(data["wid"], data["vid"])
    assert not view["auto_search"]["enabled"]
    assert view["auto_approve_first_emails"]
    search = customers.auto_search_card({"target": 23})
    payload = search["payload"] if is_local() else search["confirmation"]["resulting_state"]
    if is_local():
        assert "confirmation" not in search and "ceiling_micro" not in payload["auto_search"]
    asyncio.run(execute_action(data["vid"], "gtm.auto_search", payload=payload, actor_subject=data["actor"]))
    view = journey.status(data["wid"], data["vid"])
    assert view["auto_search"]["enabled"] and view["auto_search"]["target"] == 23
    assert view["auto_search"]["ceiling_micro"] == journey.SEARCH_CEILING_MICRO


@pytest.mark.parametrize("payer", [None, "founder"], ids=["platform", "byok"])
@pytest.mark.parametrize("pause", [False, True], ids=["cap", "pause"])
def test_auto_search_credit_ceiling_covers_repeated_calls_and_overages(auto_journey, monkeypatch, payer, pause, safety_runtime):
    from app.credits import metered
    from app.core.local_mode import is_local
    from app.gtm import journey

    data = auto_journey
    calls = []
    if pause:
        monkeypatch.setattr("app.ventures.lifecycle.is_paused", lambda vid: bool(calls))
    async def discover(*args, payload, **kwargs):
        async def provider():
            calls.append(True)
            return {}
        for attempt in range(3):
            await metered.call(provider, purpose="gtm.discovery.search", provider="firecrawl",
                workspace_id=data["wid"], venture_id=data["vid"], estimate_micro=journey.SEARCH_CEILING_MICRO // 2,
                request_id="same-search-request", payer=payer, actual_micro=lambda result: journey.SEARCH_CEILING_MICRO)
        return []
    monkeypatch.setattr(journey, "_discover", discover)
    monkeypatch.setattr(journey, "_draft_ready", lambda wid, vid: 0)
    view = journey.prepare(data["wid"], data["vid"], authorize=False, auto_search=True)
    _work(data, view)
    expected_calls = 1 if pause else 2
    assert len(calls) == expected_calls
    with data["factory"]() as session:
        saved = journey._json(journey._row(session, data["vid"]).provenance_json)
        run = saved["_runtime"]["auto_run"]
    assert len(run["requests"]) == expected_calls
    assert run["credits_used_micro"] == (journey.SEARCH_CEILING_MICRO // 2 * expected_calls if payer is None and not is_local() else 0)
    assert saved["auto_search"]["empty_runs"] == 0


def test_auto_search_varies_local_cities_and_b2b_queries(auto_journey, monkeypatch):
    from app.core.jobs import lease
    from app.core.local_mode import is_local
    from app.db.models import Job
    from app.gtm import journey

    data = auto_journey
    monkeypatch.setenv("FIRECRAWL_API_KEY", "offline-test")
    async def plan(payload, stage, context):
        return {"queries": [{"query": data["audience"] + " website", "intent": "business"},
                            {"query": data["audience"] + " contact", "intent": "business"}]}
    async def public_call(*args, **kwargs):
        return []
    monkeypatch.setattr(journey, "_discovery_model", plan)
    monkeypatch.setattr(journey, "_signal_call", public_call)
    queries = []
    for round_number in (1, 2):
        journey.prepare(data["wid"], data["vid"], authorize=False, auto_search=True)
        with data["factory"]() as session:
            jobs = lease(session, "auto-query-test", kinds=["gtm.journey.prepare"], n=1)
            session.commit()
        job = jobs[0]
        payload = job.payload | {"_job_id": job.id, "_worker_id": "auto-query-test", "_job_attempt": job.attempts}
        asyncio.run(journey._discover(data["audience"], problem=data["problem"], payload=payload))
        with data["factory"]() as session:
            row = journey._row(session, data["vid"])
            saved = journey._json(row.provenance_json)
            queries.append([item["query"] for item in saved["discovery_plan"] if item["intent"] == "business"])
            saved["auto_search"].update(runs=round_number, last_run_at=(journey.utcnow() - timedelta(days=3)).isoformat())
            saved.update(state="ready", force_discovery=False)
            session.get(Job, job.id).status = "succeeded"
            journey._store(session, row, saved)
            session.commit()
    assert queries[0] != queries[1]
    if is_local():
        assert all(any(data["audience"].casefold() in query.casefold() for query in batch) for batch in queries)
        assert all("Houston" not in query and "Atlanta" not in query for batch in queries for query in batch)
    elif journey._local_business_category(data["audience"]):
        assert any("Houston" in query for query in queries[0])
        assert any("Atlanta" in query for query in queries[1])
    else:
        assert any("companies directory" in query for query in queries[0])
        assert any("industry association members" in query for query in queries[1])
