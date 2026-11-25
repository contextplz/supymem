"""The approved belief loop is domain-independent and never creates research facts."""
import asyncio
import json
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace

import pytest
from tests.company_os_harness import local_owner_clients


@pytest.fixture
def hosted_wallet(monkeypatch):
    monkeypatch.delenv("SUPYMEM_MODE", raising=False)


def set_local_experiments(on):
    from app.core.local_mode import is_local
    if is_local():
        from app.core.local_secrets import update_settings
        update_settings("capabilities", {"run_for_me_experiments": on}, {})


def enable_local_mail():
    from app.core.local_mode import is_local
    if is_local():
        from app.core.local_secrets import update_settings
        from secrets import token_urlsafe
        update_settings("smtp", {"host": "smtp.example.test", "user": "owner@example.test",
            "sender_name": "Your founder", "postal_address": "18 Oak Road, Test City, US"},
            {"password": token_urlsafe(24)})
        update_settings("capabilities", {"send_email": True}, {})

from tests.unit.test_autonomous_experiments import market as base_market, venture


@pytest.fixture
def market(base_market):
    from app.db.models import Venture
    from app.experiments import autonomous
    from app.state import db
    from app.ventures import founder_facts
    from app.ventures.current_idea import current_revision
    from tests.company_os_harness import append_idea_revision

    _client, venture_id, _workspace_id, view, *_rest = base_market
    with db()() as session:
        venture = session.get(Venture, venture_id)
        founder_facts.save(session, venture, {
            field: {"value": value, "quote": value, "source": "clarification", "confidence": 1.0}
            for field, value in (("audience", view["icp"]["who"]), ("problem", view["icp"]["problem"]))})
        revision = current_revision(venture, session)
        append_idea_revision(session, revision.id, structured=json.dumps({
            **json.loads(revision.structured), "customer": view["icp"]["who"],
            "problem": view["icp"]["problem"], "_questions_skipped": True}))
        session.commit()
    snapshot = autonomous._snapshot(venture_id)
    assert snapshot["journey"]["icp"]["who"] == view["icp"]["who"]
    assert snapshot["journey"]["icp"]["problem"] == view["icp"]["problem"]
    return base_market


def drafts(view):
    from app.experiments.beliefs import TYPES
    who, problem = view["icp"]["who"], view["icp"]["problem"]
    return {"beliefs": [{"type": kind, "statement": f"Your {who} have testable {kind} hypothesis {index} about {problem}.",
                         "confidence": 0.2} for index, kind in enumerate((*TYPES, "who"))]}


def model_stub(monkeypatch, view):
    from app.experiments import beliefs, ledger
    from app.state import db
    calls = []

    async def complete(messages, **kwargs):
        calls.append((messages, kwargs))
        inputs = json.loads(messages[-1]["content"])
        parent = inputs.get("parent")
        output = {"beliefs": [{"type": parent["type"], "statement": f"Your narrower {view['icp']['who']} segment has a different testable hypothesis.",
            "confidence": 0.3, "audience": "Focused " + view["icp"]["who"], "problem": view["icp"]["problem"]}]} if parent else drafts(view)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(output)))])

    monkeypatch.setattr(beliefs.metered, "llm", complete)
    def context(vid):
        with db()() as session:
            direction = ledger.current_direction(session, vid)
        return {"venture_id": vid, "direction": direction}, {
            "idea_and_research": view["icp"]["problem"], "icp": deepcopy(view["icp"]),
            "lead_library": {"facets": {"audience": view["icp"]["who"]}, "total": 0}}
    monkeypatch.setattr(beliefs, "context", context)
    return calls


def test_planning_failure_keeps_saved_beliefs_and_a_truthful_retry(market, monkeypatch, caplog):
    from app.chat import market_beliefs
    from app.experiments import belief_planner, beliefs

    _, venture_id, _, view, *_ = market
    model_stub(monkeypatch, view)
    error = RuntimeError("private idea or credentials")

    def unavailable(*args, **kwargs):
        raise error

    monkeypatch.setattr(belief_planner, "available", unavailable)
    payload = {"max_emails_per_day": 0}  # a requested card explains its failure; the automatic one stays silent
    reply = asyncio.run(market_beliefs.prepare(venture_id, payload))
    assert "credits" not in reply["body_md"].lower() and "model" not in reply["body_md"].lower()
    assert "nothing was launched" in reply["body_md"].lower()
    assert reply["actions"] == [{"label": "Retry", "action": "experiment.beliefs.prepare", "payload": payload, "retry": True}]
    assert len(beliefs.load(venture_id)["beliefs"]) == 8
    assert any(getattr(record, "venture_id", None) == venture_id and record.error_type == "RuntimeError" for record in caplog.records)
    assert "private idea or credentials" not in caplog.text


def spec(venture_id, workspace_id, direction, kind, index, *, cost=1):
    from app.experiments import autonomous, belief_planner
    scan = belief_planner.RUNNERS[kind][0]
    result = {"id": f"exp_belief_{index}", "venture_id": venture_id, "workspace_id": workspace_id,
        "direction": direction, "scan": scan, "runner": "scan", "state": "proposed", "title": f"Your {kind} test",
        "icp": {},
        "hypothesis": f"Your market has a measurable {kind} signal {index}.", "question": belief_planner.QUESTIONS[kind],
        "method": "Read public sources and compare your saved threshold.", "approval": "once",
        "metric": "signal", "min_sample": 5, "ceiling_micro": cost,
        "success_threshold": {"op": ">=", "value": 3, "min_events": 5},
        "failure_threshold": {"op": "<", "value": 1, "min_events": 5}, "max_duration_days": 1}
    result["fingerprint"] = autonomous._fingerprint(result)
    return result


def save_plan(venture_id, workspace_id, items, specs, *, ceiling=None):
    from app.db.supymem_models import Experiment
    from app.experiments import belief_planner, beliefs, ledger
    from app.state import db
    plan = belief_planner.build(items, specs, {}, ceiling_micro=ceiling)
    with db()() as session:
        saved = beliefs.row(session, venture_id, create=True)
        context = json.loads(saved.context)
        context.update(beliefs=items, plan=plan)
        saved.context = json.dumps(context)
        for item in specs:
            session.add(Experiment(id=item["id"], venture_id=venture_id, workspace_id=workspace_id,
                kind=ledger.KIND, hypothesis=item["hypothesis"], state="proposed",
                context=json.dumps({"runner": "system", "spec": item})))
        session.commit()
    return plan


def test_generation_is_metered_typed_isolated_and_not_research(market, monkeypatch):
    from app.experiments import beliefs, ledger
    from app.state import db
    from app.db.models import Venture
    from core.clock import utcnow
    _client, vid, _wid, view, *_rest = market
    calls = model_stub(monkeypatch, view)
    generated = asyncio.run(beliefs.generate(vid))
    assert len(generated["beliefs"]) == 8
    assert {item["type"] for item in generated["beliefs"]} == set(beliefs.TYPES)
    assert all(item["status"] == "to_test" and not item["evidence"] for item in generated["beliefs"])
    assert asyncio.run(beliefs.generate(vid))["beliefs"] == generated["beliefs"]
    assert len(calls) == 1
    assert calls[0][1]["purpose"] == "experiment.beliefs" and calls[0][1]["venture_id"] == vid
    assert view["icp"]["who"] in calls[0][0][-1]["content"]
    assert "lead_library" in calls[0][0][-1]["content"]
    assert ledger.list_for_venture(vid) == []
    with db()() as session:
        session.add(Venture(id="unrelated", workspace_id=market[2], name="Another venture", raw_idea="A separate idea",
            stage="idea", created_at=utcnow(), updated_at=utcnow()))
        session.commit()
    assert beliefs.load("unrelated")["beliefs"] == []
    from tests.unit.test_autonomous_experiments import confirm_saved_idea
    from tests.company_os_harness import append_idea_revision
    from app.ventures.current_idea import current_revision

    confirm_saved_idea("unrelated")
    with db()() as session:
        revision = current_revision(session.get(Venture, "unrelated"), session)
        append_idea_revision(session, revision.id, structured=json.dumps({"_questions_skipped": True}))
        session.commit()
    original_call = beliefs.metered.llm
    async def change_idea(messages, **kwargs):
        response = await original_call(messages, **kwargs)
        monkeypatch.setattr(ledger, "current_direction", lambda _session, _vid: "changed-direction")
        return response
    monkeypatch.setattr(beliefs.metered, "llm", change_idea)
    with pytest.raises(ValueError, match="idea changed"):
        asyncio.run(beliefs.generate("unrelated"))
    assert beliefs.load("unrelated")["beliefs"] == []


def test_measured_status_rule_preserves_thresholds_and_history(market):
    from app.experiments.beliefs import update
    _client, vid, wid, view, *_rest = market
    belief = {"id": "belief", "type": "pain", "statement": view["icp"]["problem"], "status": "testing", "evidence": []}
    for index, (value, events, expected) in enumerate(((3, 5, "confirmed"), (0, 5, "disproven"), (2, 5, "weak"), (3, 1, "weak"), (float("nan"), 5, "weak"), (3, float("nan"), "weak"))):
        experiment = spec(vid, wid, "direction", "pain", index)
        changed = update(belief, experiment, {"signal": value, "events": events}, date="2026-10-27T00:00:00+00:00")
        assert changed["status"] == expected
        assert changed["evidence"][-1]["experiment_id"] == experiment["id"]
        assert changed["evidence"][-1]["threshold"]["min_events"] == 5
        assert update(changed, experiment, {"signal": value, "events": events})["evidence"] == changed["evidence"]
        json.dumps(changed, allow_nan=False)
        belief = changed
    assert len(belief["evidence"]) == 6
    waiting = update(belief, spec(vid, wid, "direction", "pain", 7), {"signal": 0, "events": 5}, verdict="inconclusive")
    assert waiting["status"] == "weak"


def test_planner_covers_every_type_and_ranks_risk_then_cost(market):
    from app.experiments import belief_planner, beliefs
    _client, vid, wid, view, *_rest = market
    items = [{"id": f"belief_{index}", **item, "status": "to_test"} for index, item in enumerate(drafts(view)["beliefs"][:7])]
    items[1]["confidence"] = 0.0
    specs = [spec(vid, wid, "direction", item["type"], index) for index, item in enumerate(items)]
    plan = belief_planner.build(items, specs + [{**specs[1], "id": "expensive", "ceiling_micro": 100}], {})
    assert len(plan["tests"]) == 7
    assert plan["tests"][0]["belief_id"] == items[1]["id"]
    assert plan["ceiling_micro"] == 7 and plan["max_emails_per_day"] == 15 and plan["days"] == 7
    assert set(belief_planner.RUNNERS) == set(beliefs.TYPES)
    assert all(test["sample_size"] == 5 and test["success_threshold"] and test["failure_threshold"] for test in plan["tests"])
    assert len(belief_planner.build(items, specs, {}, ceiling_micro=1)["tests"]) == 1
    for bad in (16, -1, True):
        with pytest.raises(ValueError):
            belief_planner.build(items, specs, {}, max_emails_per_day=bad)
    specs[1]["approval"] = "each_publish"
    assert all(test["experiment_id"] != specs[1]["id"] for test in belief_planner.build(items, specs, {})["tests"])
    price_test = {**specs[2], "scan": "page_test", "page_kind": "pricing", "runner": "observe",
                  "state": "running", "approval": "each_publish", "ceiling_micro": 0}
    observing = belief_planner.build([items[2]], [price_test], {})
    assert observing["tests"][0]["state"] == "running" and observing["ceiling_micro"] == 0
    email_test = {**specs[4], "runner": "outreach_split", "approval": "once", "catalog_key": "E19", "min_sample": 20}
    assert not belief_planner.build([items[4]], [email_test], {})["tests"]
    consent = {"journey": {"allow_sending": True, "auto_approve_first_emails": True}}
    assert belief_planner.build([items[4]], [email_test], consent)["tests"]
    assert not belief_planner.build([items[4]], [email_test], consent, max_emails_per_day=1)["tests"]


def test_planner_allocates_risky_research_response_and_challenges(market, monkeypatch):
    enable_local_mail()
    from app.core.local_mode import is_local
    from app.experiments import belief_planner
    _client, vid, wid, view, *_rest = market
    items = [{"id": f"belief_{index}", **item, "status": "to_test"}
             for index, item in enumerate(drafts(view)["beliefs"][:7])]
    items[1]["confidence"] = 0.0
    items.extend({**items[1], "id": f"extra_pain_{index}", "confidence": 0.8} for index in range(3))
    scans = [{**spec(vid, wid, "direction", item["type"], index), "belief_ids": [item["id"]]}
             for index, item in enumerate(items)]
    response = {**scans[4], "id": "response", "runner": "outreach_split", "scan": "outreach_split",
                "catalog_key": "E19", "approval": "each_send", "min_sample": 20, "ceiling_micro": 2}
    challenges = [{**scans[index], "id": f"challenge_{index}", "scan": "redteam",
                   "catalog_key": "E37_red_team", "belief_ids": [items[index]["id"]], "ceiling_micro": 2}
                  for index in (7, 8, 9)]
    inputs = {"journey": {**view, "allow_sending": True, "auto_approve_first_emails": True}}
    flags = {}
    monkeypatch.setattr(belief_planner, "get_policy", lambda _key: flags, raising=False)
    set_local_experiments(False)
    baseline = belief_planner.build(items, scans + [response, *challenges], inputs)
    if not is_local():
        assert [test["experiment_id"] for test in baseline["tests"]] == [
            scans[items.index(item)]["id"] for item in sorted(items,
                key=lambda item: (-belief_planner.engine.IMPACT[belief_planner.QUESTIONS[item["type"]]] *
                                  (1 - item["confidence"]), item["id"]))]
    flags["experiments_autopilot_v1"] = True
    set_local_experiments(True)
    plan = belief_planner.build(items, scans + [response, *challenges], inputs)
    selected = [test["experiment_id"] for test in plan["tests"]]
    if is_local():
        assert [test["experiment_id"] for test in baseline["tests"]] == selected
    assert selected[:3] == [scans[1]["id"], "response", "challenge_7"]
    assert set(selected) >= {"challenge_7", "challenge_8", "challenge_9"}
    assert sum(ident.startswith("challenge_") for ident in selected) / len(selected) == 0.3
    assert len(selected) == len(set(selected)) == len(items)
    assert len({test["belief_id"] for test in plan["tests"]}) == len(items)
    assert plan["ceiling_micro"] == sum(test["ceiling_micro"] for test in plan["tests"])
    bounded = belief_planner.build(items, scans + [response, *challenges], inputs, ceiling_micro=5)
    assert [test["experiment_id"] for test in bounded["tests"]] == selected[:3]
    assert not belief_planner.build(items, challenges, inputs, ceiling_micro=1)["tests"]
    observed = {**scans[2], "id": "observed", "scan": "page_test", "page_kind": "pricing",
                "runner": "observe", "state": "running", "approval": "each_publish", "ceiling_micro": 0}
    assert belief_planner.build(items, scans + [observed, *challenges], inputs)["tests"][1]["experiment_id"] == "observed"
    scoped = [{**items[7], "audience": view["icp"]["who"], "problem": view["icp"]["problem"]}]
    available = belief_planner.available(vid, scoped)
    assert any(candidate.get("scan") == "redteam" and candidate.get("belief_ids") == [scoped[0]["id"]]
               and candidate["icp"]["who"] == view["icp"]["who"] for candidate in available)


def test_planner_admits_each_send_only_with_existing_email_consent(market, monkeypatch):
    enable_local_mail()
    from app.core.local_mode import is_local
    from app.experiments import belief_planner
    _client, vid, wid, view, *_rest = market
    item = {"id": "message", **drafts(view)["beliefs"][4], "status": "to_test"}
    email = {**spec(vid, wid, "direction", "message", 0), "runner": "outreach_split",
             "scan": "outreach_split", "approval": "each_send", "catalog_key": "E19", "min_sample": 20}
    inputs = {"journey": {**view, "allow_sending": True, "auto_approve_first_emails": True}}
    flags = {}
    monkeypatch.setattr(belief_planner, "get_policy", lambda _key: flags, raising=False)
    set_local_experiments(False)
    assert bool(belief_planner.build([item], [email], inputs)["tests"]) is is_local()
    flags["experiments_autopilot_v1"] = "true"
    assert bool(belief_planner.build([item], [email], inputs)["tests"]) is is_local()
    flags["experiments_autopilot_v1"] = True
    set_local_experiments(True)
    assert belief_planner.build([item], [email], inputs)["tests"][0]["experiment_id"] == email["id"]
    for key in ("allow_sending", "auto_approve_first_emails"):
        assert not belief_planner.build([item], [email], {"journey": {**inputs["journey"], key: False}})["tests"]
    for allowance in (0, 1):
        assert not belief_planner.build([item], [email], inputs, max_emails_per_day=allowance)["tests"]
    assert not belief_planner.build([item], [email], inputs, ceiling_micro=0)["tests"]
    assert not belief_planner.build([item], [{**email, "belief_ids": ["other"]}], inputs)["tests"]
    assert not belief_planner.build([item], [{**email, "approval": "each_publish"}], inputs)["tests"]
    manual = {**email, "runner": "scan", "scan": "demand"}
    pain = {**item, "type": "pain"}
    assert not belief_planner.build([pain], [manual], inputs)["tests"]
    monkeypatch.setattr(belief_planner.engine, "blocked_reason", lambda *_args: "Your test needs to wait.")
    assert not belief_planner.build([item], [email], inputs)["tests"]


def test_planner_who_uses_emitted_audience_catalogs(market, monkeypatch):
    enable_local_mail()
    from app.core.local_mode import is_local
    from app.experiments import belief_planner, outreach_tests
    _client, vid, wid, view, *_rest = market
    item = {"id": "who", **drafts(view)["beliefs"][0], "status": "to_test"}
    inputs = {"journey": {**view, "allow_sending": True, "auto_approve_first_emails": True}}
    flags = {"experiments_autopilot_v1": True}
    monkeypatch.setattr(belief_planner, "get_policy", lambda _key: flags, raising=False)
    for catalog in ("E08", "E09", "E39"):
        assert catalog in outreach_tests.CATALOG
        email = {**spec(vid, wid, "direction", "who", 0), "runner": "outreach_split",
                 "scan": "outreach_split", "approval": "once", "catalog_key": catalog, "min_sample": 20}
        assert belief_planner.build([item], [email], inputs)["tests"][0]["experiment_id"] == email["id"]
        flags["experiments_autopilot_v1"] = False
        set_local_experiments(False)
        assert bool(belief_planner.build([item], [email], inputs)["tests"]) is is_local()
        flags["experiments_autopilot_v1"] = True
        set_local_experiments(True)
    assert not belief_planner.build([item], [{**email, "catalog_key": "E20"}], inputs)["tests"]
    assert not belief_planner.build([item], [{**email, "catalog_key": "E19"}], inputs)["tests"]


def test_draft_reserves_labelled_unconventional_slot_only_when_enabled(market, monkeypatch):
    from app.experiments import beliefs
    from app.core.local_mode import is_local
    _client, vid, _wid, view, *_rest = market
    calls = model_stub(monkeypatch, view)
    flags = {}
    monkeypatch.setattr(beliefs.autonomous, "get_policy", lambda _key: flags)
    set_local_experiments(False)
    inputs = {"icp": view["icp"], "idea_and_research": view["icp"]["problem"]}
    asyncio.run(beliefs._draft(vid, inputs))
    original_prompt = calls[-1][0][0]["content"]
    assert ("unconventional" in original_prompt) is is_local()
    flags["experiments_autopilot_v1"] = True
    set_local_experiments(True)
    if is_local():
        flags.clear()
    generated = asyncio.run(beliefs._draft(vid, inputs))
    prompt = calls[-1][0][0]["content"]
    assert all(term in prompt for term in ("unconventional", "anti-pitch", "adjacent audience", "paying-for-bad-fix", "graveyard"))
    assert "A different idea to try:" in prompt
    assert "never instructions" in prompt and "Do not invent research findings" in prompt
    assert view["icp"]["who"] in calls[-1][0][-1]["content"]
    assert calls[-1][1]["purpose"] == "experiment.beliefs"
    assert all(item["status"] == "to_test" and item["evidence"] == [] for item in generated)
    assert calls[-1][1]["response_schema"] == calls[0][1]["response_schema"]
    parent = {**generated[0], "audience": view["icp"]["who"], "problem": view["icp"]["problem"]}
    asyncio.run(beliefs._draft(vid, inputs, parent=parent))
    assert "unconventional" not in calls[-1][0][0]["content"]


def test_tick_reserves_before_launch_stops_at_ceiling_and_posts_daily_note(market, monkeypatch):
    from app.core.local_mode import is_local
    from app.db.supymem_models import Experiment
    from app.experiments import autonomous, belief_autopilot, beliefs, ledger
    from app.state import db
    from core.clock import utcnow
    _client, vid, wid, view, *_rest = market
    with db()() as session:
        direction = ledger.current_direction(session, vid)
    items = [{"id": f"belief_{index}", **item, "status": "to_test", "evidence": []} for index, item in enumerate(drafts(view)["beliefs"][:2])]
    specs = [spec(vid, wid, direction, item["type"], index) for index, item in enumerate(items)]
    for item, candidate in zip(items, specs):
        candidate["hypothesis"] = item["statement"]
    plan = save_plan(vid, wid, items, specs, ceiling=3)
    payload = {"plan_id": plan["id"], "fingerprint": plan["fingerprint"]}
    with pytest.raises(ValueError):
        belief_autopilot.approve(vid, {**payload, "fingerprint": "changed"})
    belief_autopilot.approve(vid, payload)
    assert belief_autopilot.approve(vid, payload)["plan"]["state"] == "active"
    launched = []

    async def run(venture_id, payload):
        assert beliefs.load(venture_id)["plan"]["reserved_micro"] >= 1
        launched.append(payload["experiment_id"])
        with db()() as session:
            experiment = session.get(Experiment, payload["experiment_id"])
            context = json.loads(experiment.context)
            context["result"] = {"measured": {"signal": 3, "events": 5}}
            experiment.state, experiment.context = "concluded", json.dumps(context)
            session.commit()

    monkeypatch.setattr(autonomous, "run", run)
    monkeypatch.setattr(autonomous, "available", lambda _vid: deepcopy(specs))
    monkeypatch.setattr(ledger, "ranking_inputs", lambda _vid: {})
    if is_local():
        set_local_experiments(False)
        asyncio.run(belief_autopilot.tick(vid))
        assert launched == []
        set_local_experiments(True)
    asyncio.run(belief_autopilot.tick(vid))
    assert len(launched) == 1
    belief_autopilot._admit_followup(vid, plan["id"], 1)
    asyncio.run(belief_autopilot.tick(vid))
    current = beliefs.load(vid)
    assert len(launched) == 2 and current["plan"]["state"] == "budget_exhausted"
    assert current["plan"]["reserved_micro"] == 3
    asyncio.run(belief_autopilot.tick(vid))
    assert len(launched) == 2
    assert any(item["status"] == "confirmed" for item in current["beliefs"])
    tomorrow = utcnow() + timedelta(days=1)
    note = belief_autopilot.daily_note(vid, now=tomorrow)
    assert ("market-test budget" if is_local() else "credit ceiling") in note and "Your market tests" in note
    assert belief_autopilot.daily_note(vid, now=tomorrow) is None
    with pytest.raises(Exception, match="budget is exhausted"):
        belief_autopilot.reserve(current["plan"], 1)


def test_followups_narrow_or_replace_without_mutating_prior_evidence(market, monkeypatch):
    from app.experiments import belief_autopilot, beliefs
    from app.state import db
    from core.clock import utcnow
    _client, vid, wid, view, *_rest = market
    model_stub(monkeypatch, view)
    for index, status in enumerate(("confirmed", "disproven")):
        parent = {"id": f"parent_{index}", "type": "who", "statement": view["icp"]["who"], "status": status,
                  "evidence": [{"experiment_id": f"completed_{index}", "metric": "signal", "value": 3 if index == 0 else 0,
                                "threshold": {"op": ">=", "value": 3}, "date": "2026-10-27", "events": 5}]}
        save_plan(vid, wid, [parent], [], ceiling=10)
        with db()() as session:
            saved = beliefs.row(session, vid)
            context = json.loads(saved.context)
            context["plan"].update(state="active", deadline=(utcnow() + timedelta(days=7)).isoformat())
            saved.context = json.dumps(context)
            session.commit()
        asyncio.run(belief_autopilot._narrow(vid, beliefs.load(vid)))
        updated = beliefs.load(vid)["beliefs"]
        assert len(updated) == 2 and updated[0]["evidence"] == parent["evidence"]
        assert updated[1]["status"] == "to_test" and updated[1]["parent_id"] == parent["id"]
        assert updated[1]["statement"] != parent["statement"] and updated[1]["evidence"] == []
        from app.experiments import belief_planner
        updated[1]["audience"] = "Focused " + view["icp"]["who"]
        updated[1]["problem"] = view["icp"]["problem"]
        candidates = belief_planner.available(vid, updated)
        scoped = [test for test in candidates if updated[1]["id"] in test.get("belief_ids", [])]
        assert scoped and all(test["icp"]["who"] == updated[1]["audience"] for test in scoped)


def test_failed_launch_keeps_belief_untested_and_blocks_automatic_retry(market, monkeypatch):
    set_local_experiments(True)
    from app.experiments import autonomous, belief_autopilot, beliefs, ledger
    from app.state import db
    _client, vid, wid, view, *_rest = market
    with db()() as session:
        direction = ledger.current_direction(session, vid)
    item = {"id": "unstarted", "type": "pain", "confidence": 0.2, "statement": view["icp"]["problem"], "status": "to_test", "evidence": []}
    experiment = spec(vid, wid, direction, "pain", 1)
    plan = save_plan(vid, wid, [item], [experiment], ceiling=1)
    belief_autopilot.approve(vid, {"plan_id": plan["id"], "fingerprint": plan["fingerprint"]})
    attempts = []
    async def fail(_vid, _payload):
        attempts.append(_vid)
        raise RuntimeError("Your provider is unavailable")
    monkeypatch.setattr(autonomous, "run", fail)
    monkeypatch.setattr(autonomous, "available", lambda _vid: [deepcopy(experiment)])
    monkeypatch.setattr(ledger, "ranking_inputs", lambda _vid: {})
    asyncio.run(belief_autopilot.tick(vid))
    asyncio.run(belief_autopilot.tick(vid))
    current = beliefs.load(vid)
    assert attempts == [vid]
    assert current["beliefs"][0]["status"] == "to_test" and current["beliefs"][0]["evidence"] == []
    assert current["plan"]["tests"][0]["state"] == "error"
    assert current["plan"]["reserved_micro"] == 1


def test_card_and_api_keep_untested_beliefs_separate_and_send_stop_is_scoped(market, monkeypatch):
    from app.chat import market_beliefs
    from app.experiments import autonomous, belief_autopilot, beliefs, ledger
    from app.state import db
    from app.autopilot import outreach
    from app.chat.intent import classify_intent
    client, vid, wid, view, *_rest = market
    enable_local_mail()
    model_stub(monkeypatch, view)
    assert classify_intent("Show my market beliefs and prepare a seven-day test plan.") == "experiment_list"
    monkeypatch.setattr(ledger, "ranking_inputs", lambda _vid: {})
    reply = asyncio.run(market_beliefs.prepare(vid))
    assert reply["kind"] == "market_beliefs"
    assert "no evidence" not in reply["body_md"].lower() and "\u2014" not in reply["body_md"]
    from app.core.local_mode import is_local
    action = reply["actions"][0]
    confirmation = ({"action": action["action"], "interpretation": reply["body_md"], "resulting_state": action["payload"]}
        if is_local() else action["confirmation"])
    assert confirmation["action"] == "experiment.beliefs.approve"
    assert "Choose Run your market tests" in confirmation["interpretation"] if is_local() else "7 days" in confirmation["interpretation"]
    belief_autopilot.approve(vid, confirmation["resulting_state"])
    response = client.get(f"/ventures/{vid}/market-map")
    assert response.status_code == 200 and len(response.json()["beliefs"]) == 8
    assert ledger.list_for_venture(vid) == []
    current = beliefs.load(vid)
    test = current["plan"]["tests"][0]
    with db()() as session:
        saved = beliefs.row(session, vid)
        current["plan"]["tests"][0]["state"] = "running"
        saved.context = json.dumps(current)
        session.commit()
    monkeypatch.setattr(outreach, "send_slots_used_today", lambda _vid: 15)
    with db()() as session:
        assert belief_autopilot.send_block(session, vid, {}) is None
        from app.db.supymem_models import Experiment
        linked = {"belief_plan_id": current["plan"]["id"],
            "spec": json.loads(session.get(Experiment, test["experiment_id"]).context)["spec"]}
        assert belief_autopilot.send_block(session, vid, linked) == "experiment_daily_cap"
    belief_autopilot.stop(vid)
    with db()() as session:
        assert belief_autopilot.send_block(session, vid, linked) == "experiment_plan_stopped"


def test_standing_plan_renews_refills_only_approved_scope_and_revokes(market, monkeypatch):
    enable_local_mail()
    set_local_experiments(True)
    from app.chat import market_beliefs
    from app.credits.policy import set_policy
    from app.db.supymem_models import Experiment
    from app.experiments import autonomous, belief_autopilot, beliefs, ledger
    from app.state import db
    from core.clock import utcnow

    _client, vid, wid, view, *_rest = market
    set_policy("feature_flags", {"experiments_autopilot_v1": True}, updated_by="test")
    with db()() as session:
        direction = ledger.current_direction(session, vid)
    items = [{"id": f"standing_{index}", "type": "who", "confidence": 0.2,
              "statement": view["icp"]["who"], "status": "to_test", "evidence": []} for index in range(3)]
    specs = [spec(vid, wid, direction, "who", index, cost=10) for index in range(3)]
    for candidate in specs:
        candidate["icp"] = deepcopy(view["icp"])
        candidate["fingerprint"] = autonomous._fingerprint(candidate)
    specs[2]["icp"]["who"] = "Another segment of " + view["icp"]["who"]
    specs[2]["belief_ids"] = [items[2]["id"]]
    specs[1]["belief_ids"] = [items[1]["id"]]
    save_plan(vid, wid, items[:1], specs[:1])

    async def generate(_vid):
        return beliefs.load(_vid)

    monkeypatch.setattr(beliefs, "generate", generate)
    monkeypatch.setattr(ledger, "ranking_inputs", lambda _vid: {})
    monkeypatch.setattr(autonomous, "available", lambda _vid: deepcopy(specs[:1]))
    reply = asyncio.run(market_beliefs.prepare(vid, {"weekly_budget_micro": 20, "max_emails_per_day": 15}))
    from app.core.local_mode import is_local
    action = reply["actions"][0]
    card = ({"interpretation": reply["body_md"], "resulting_state": action["payload"],
        "consequences": [reply["body_md"]]} if is_local() else action["confirmation"])
    assert "Choose Run your market tests" in card["interpretation"] if is_local() else "renew every 7 days" in card["interpretation"]
    if is_local():
        assert "repeat every seven days" in card["interpretation"] and "Stop new market tests" in card["interpretation"]
        assert not action.get("confirmation")
    assert "credit" not in card["interpretation"].lower() and "15 per day" in card["interpretation"]
    assert beliefs.load(vid)["plan"]["weekly_budget_micro"] == 20
    assert any(view["icp"]["who"] in consequence for consequence in card["consequences"])
    with pytest.raises(ValueError, match="email cap"):
        belief_autopilot.validate_standing({**beliefs.load(vid)["plan"], "max_emails_per_day": 16})
    with db()() as session:
        saved = beliefs.row(session, vid)
        context = json.loads(saved.context)
        original = deepcopy(context)
        context["plan"]["weekly_budget_micro"] += 1
        saved.context = json.dumps(context)
        session.commit()
    with pytest.raises(ValueError, match="changed"):
        belief_autopilot.approve(vid, card["resulting_state"])
    with db()() as session:
        saved = beliefs.row(session, vid)
        saved.context = json.dumps(original)
        session.commit()
    carry = deepcopy(original["plan"])
    carry.update(state="active", deadline=(utcnow() - timedelta(days=8)).isoformat(), reserved_micro=20,
                 calls=[{"reserved_micro": 10, "finished": True}])
    carry["tests"][0].update(reserved_micro=10, actual_micro=2)
    belief_autopilot._renew(carry, utcnow())
    assert carry["reserved_micro"] == 10 and carry["deadline"] > utcnow().isoformat()
    belief_autopilot.approve(vid, card["resulting_state"])
    with db()() as session:
        saved = beliefs.row(session, vid)
        context = json.loads(saved.context)
        context["beliefs"].extend(items[1:])
        context["plan"].update(state="budget_exhausted", reserved_micro=20,
            deadline=(utcnow() - timedelta(seconds=1)).isoformat(),
            calls=[{"request_id": "prior_followup", "reserved_micro": 10, "actual_micro": 10, "finished": True}])
        context["plan"]["tests"][0].update(state="done", reserved_micro=10, actual_micro=10)
        saved.context = json.dumps(context)
        session.commit()
    launched = []

    async def run(_vid, payload):
        launched.append(payload["experiment_id"])
        with db()() as session:
            experiment = session.get(Experiment, payload["experiment_id"])
            experiment.state, experiment.started_at = "running", utcnow()
            session.commit()

    monkeypatch.setattr(autonomous, "run", run)
    monkeypatch.setattr(autonomous, "available", lambda _vid: deepcopy(specs))
    set_policy("feature_flags", {"experiments_autopilot_v1": False}, updated_by="test")
    set_local_experiments(False)
    asyncio.run(belief_autopilot.tick(vid))
    assert not launched and beliefs.load(vid)["plan"]["reserved_micro"] == 20
    set_policy("feature_flags", {"experiments_autopilot_v1": True}, updated_by="test")
    set_local_experiments(True)
    asyncio.run(belief_autopilot.tick(vid))
    current = beliefs.load(vid)
    assert launched == [specs[1]["id"]]
    assert current["plan"]["state"] == "active" and current["plan"]["reserved_micro"] == 10
    assert current["plan"]["deadline"] > utcnow().isoformat()
    assert all(test["experiment_id"] != specs[2]["id"] for test in current["plan"]["tests"])
    linked = {"belief_plan_id": current["plan"]["id"], "spec": specs[1]}
    set_policy("feature_flags", {"experiments_autopilot_v1": False}, updated_by="test")
    set_local_experiments(False)
    with db()() as session:
        assert belief_autopilot.send_block(session, vid, linked) == "experiment_plan_stopped"
    set_policy("feature_flags", {"experiments_autopilot_v1": True}, updated_by="test")
    set_local_experiments(True)
    stopped = belief_autopilot.stop(vid)
    assert stopped["plan"]["revoked_at"]
    assert belief_autopilot._claim(vid, specs[2]) is None
    with db()() as session:
        assert belief_autopilot.send_block(session, vid, linked) == "experiment_plan_stopped"
    asyncio.run(belief_autopilot.tick(vid, now=utcnow() + timedelta(days=8)))
    assert launched == [specs[1]["id"]] and beliefs.load(vid)["plan"]["state"] == "stopped"


@pytest.mark.parametrize("observer", ["page_test", "channel_test", "outreach_split"])
def test_scoped_readout_is_durable_before_beliefs_and_never_synthetic(market, monkeypatch, observer):
    from sqlalchemy import select
    from app.core.local_mode import is_local
    from app.credits.policy import set_policy
    from app.db.models import ActivityLine
    from app.db.supymem_models import Experiment, ExperimentObservation
    from app.experiments import autonomous, belief_autopilot, beliefs, channel_tests, ledger, page_tests, service
    from app.state import db
    from core.clock import utcnow

    _client, vid, wid, view, *_rest = market
    with db()() as session:
        direction = ledger.current_direction(session, vid)
    item = {"id": "observed_belief", "type": "who", "confidence": 0.2,
            "statement": view["icp"]["who"], "status": "to_test", "evidence": []}
    candidate = spec(vid, wid, direction, "who", 0, cost=0)
    candidate["hypothesis"] = item["statement"]
    save_plan(vid, wid, [item], [candidate], ceiling=10)
    candidate.update(scan=observer, runner="outreach_split" if observer == "outreach_split" else "scan", state="running")
    now = utcnow()
    with db()() as session:
        saved = beliefs.row(session, vid)
        context = json.loads(saved.context)
        context["plan"].update(state="active", approved_at=now.isoformat(), deadline=(now + timedelta(days=7)).isoformat())
        context["plan"]["tests"][0]["state"] = "running"
        saved.context = json.dumps(context)
        experiment = session.get(Experiment, candidate["id"])
        experiment.state, experiment.started_at = "running", now
        experiment.context = json.dumps({"runner": "system", "spec": candidate})
        foreign = {**candidate, "id": "exp_foreign", "venture_id": "ven_other", "workspace_id": "ws_other"}
        session.add(Experiment(id=foreign["id"], venture_id=foreign["venture_id"], workspace_id=foreign["workspace_id"],
            kind=ledger.KIND, hypothesis="Private hypothesis", state="running", context=json.dumps({"runner": "system", "spec": foreign})))
        session.commit()
    observed = []
    measured = {"signal": None, "events": 0} if observer == "channel_test" else {"signal": 3, "events": 5}
    verdict = "supported" if observer == "page_test" else "inconclusive"

    def observe(specification):
        observed.append(specification["id"])
        return {"outcome": verdict, "measured": measured, "insight": "Your recorded events were checked against your saved threshold.", "sources": [], "actual_micro": 0}

    module = {"page_test": page_tests, "channel_test": channel_tests, "outreach_split": autonomous.outreach_tests}[observer]
    monkeypatch.setattr(module, "observe", observe)
    payload = {"venture_id": vid, "workspace_id": wid, "experiment_ids": [candidate["id"], "exp_foreign"]}
    set_policy("feature_flags", {}, updated_by="test")
    set_local_experiments(False)
    if not is_local():
        assert service.run_evaluate(payload) == {"evaluated": 0}
    assert service.run_evaluate({}) == {"evaluated": 0}
    with db()() as session:
        assert list(session.scalars(select(ExperimentObservation))) == []
        assert session.get(Experiment, candidate["id"]).result is None
    set_policy("feature_flags", {"experiments_autopilot_v1": True}, updated_by="test")
    with pytest.raises(PermissionError):
        service.run_evaluate({**payload, "workspace_id": "ws_other"})
    assert service.run_evaluate(payload) == {"evaluated": 1}
    if observer == "outreach_split":
        with db()() as session:
            experiment = session.get(Experiment, candidate["id"])
            assert experiment.state == "running"
            interim = json.loads(experiment.context)
            assert not interim.get("result") and interim["observed_result"]["measured"] == measured
        verdict = "supported"
        assert service.run_evaluate(payload) == {"evaluated": 1}
    if observer == "channel_test":
        with db()() as session:
            assert session.get(Experiment, candidate["id"]).state == "running"
        payload["now"] = now + timedelta(days=2)
        assert service.run_evaluate(payload) == {"evaluated": 1}
    assert service.run_evaluate(payload) == {"evaluated": 1}
    assert observed == [candidate["id"]] * (1 if observer == "page_test" else 2)
    set_local_experiments(True)
    update = beliefs.update

    def checked_update(*args, **kwargs):
        with db()() as session:
            experiment = session.get(Experiment, candidate["id"])
            assert experiment.state == "concluded" and experiment.ended_at is not None
            assert experiment.metric == "signal"
            assert experiment.result == measured["signal"] and experiment.outcome == verdict
            assert json.loads(experiment.context)["result"]["measured"] == measured
            if observer == "outreach_split":
                finished = json.loads(experiment.context)
                assert finished["outreach_state"] == "concluded" and "observed_result" not in finished
                assert "actual_micro" not in finished["result"]
            assert len(list(session.scalars(select(ActivityLine).where(ActivityLine.kind == "experiment_readout")))) == (1 if observer == "page_test" else 2)
            assert session.get(Experiment, "exp_foreign").result is None
        return update(*args, **kwargs)

    monkeypatch.setattr(beliefs, "update", checked_update)
    monkeypatch.setattr(module, "observe", lambda *_args: pytest.fail("A saved final result must not be collected again"))
    monkeypatch.setattr(autonomous, "available", lambda _vid: [])
    monkeypatch.setattr(ledger, "ranking_inputs", lambda _vid: {})

    async def no_followup(*_args):
        return None

    monkeypatch.setattr(belief_autopilot, "_narrow", no_followup)
    asyncio.run(belief_autopilot.tick(vid))
    current = beliefs.load(vid)
    assert current["beliefs"][0]["status"] == ("weak" if observer == "channel_test" else "confirmed")
    assert current["plan"]["tests"][0]["state"] == "done"


def test_local_plan_receipts_require_settlement_and_exact_scope(market, monkeypatch):
    from app.credits import local_meter
    from app.credits.gate import charge_sync
    from app.experiments import belief_autopilot
    from app.state import db

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    _client, venture_id, workspace_id, *_rest = market
    with db()() as session:
        with charge_sync(workspace_id=workspace_id, venture_id=venture_id, purpose="experiment.demand",
                estimate_micro=10, provider="firecrawl", request_id="local_parent:search") as meter:
            assert belief_autopilot._receipt_total(session, workspace_id, venture_id, ["local_parent"]) is None
            meter.record_micro(2)
            assert belief_autopilot._receipt_total(session, workspace_id, venture_id, ["local_parent"]) is None
        assert belief_autopilot._receipt_total(session, workspace_id, venture_id, ["local_parent"]) == 2
        assert belief_autopilot._receipt_total(session, workspace_id, venture_id, ["local_parent", "local_parent:search"]) == 2
        assert belief_autopilot._receipt_total(session, "another_workspace", venture_id, ["local_parent"]) is None
        assert belief_autopilot._receipt_total(session, workspace_id, "another_venture", ["local_parent"]) is None
        assert belief_autopilot._receipt_total(session, workspace_id, venture_id, ["local_parent"], account_id="another_account") is None
        with charge_sync(workspace_id=workspace_id, venture_id=venture_id, purpose="experiment.demand",
                estimate_micro=10, provider="firecrawl", request_id="local_parent:unknown"):
            pass
        assert belief_autopilot._receipt_total(session, workspace_id, venture_id, ["local_parent"]) is None
        with charge_sync(workspace_id=workspace_id, venture_id=venture_id, purpose="experiment.demand",
                estimate_micro=10, provider="firecrawl", request_id="local_rejected") as rejected:
            rejected.definitive_failure = True
        assert belief_autopilot._receipt_total(session, workspace_id, venture_id, ["local_rejected"]) == 0
        with local_meter._database() as connection:
            row = connection.execute("SELECT call_id, receipt_json FROM calls WHERE request_id = ?", ("local_parent:search",)).fetchone()
            receipt = {**json.loads(row["receipt_json"]), "workspace_id": "another_workspace"}
            connection.execute("UPDATE calls SET receipt_json = ? WHERE call_id = ?", (json.dumps(receipt), row["call_id"]))
        assert belief_autopilot._receipt_total(session, workspace_id, venture_id, ["local_parent:search"]) is None


@pytest.mark.parametrize("wallet_mode", ["hosted", "local"])
def test_plan_reservations_release_only_settled_scoped_receipts_and_are_idempotent(hosted_wallet, market, monkeypatch, wallet_mode):
    from app.autopilot import outreach
    from app.credits.accounts import account_for_venture
    from app.credits.gate import charge_sync
    from app.credits.policy import set_policy
    from app.credits.reserve import reserve, settle
    from app.db.models import Approval, Job
    from app.db.supymem_models import Experiment
    from app.experiments import belief_autopilot, beliefs, ledger
    from app.state import db
    from core.clock import utcnow

    if wallet_mode == "local":
        from app.core.providers_local import save_settings

        monkeypatch.setenv("SUPYMEM_MODE", "local")
        save_settings(provider="openai_compatible", base_url="http://127.0.0.1:1234/v1",
            models={"chat": "local-test-model"})
        set_local_experiments(True)

        def reserve(*, amount_micro, **binding):
            charge = charge_sync(estimate_micro=amount_micro, **binding)
            return charge, charge.__enter__()

        def settle(hold, *, actual_micro):
            charge, meter = hold
            meter.record_micro(actual_micro)
            charge.__exit__(None, None, None)

    _client, vid, wid, view, *_rest = market
    set_policy("feature_flags", {"experiments_autopilot_v1": True}, updated_by="test")
    with db()() as session:
        direction = ledger.current_direction(session, vid)
    items = [{"id": f"receipt_{index}", "type": kind, "confidence": 0.2,
              "statement": view["icp"]["problem"], "status": "to_test", "evidence": []}
             for index, kind in enumerate(("who", "pain"))]
    specs = [spec(vid, wid, direction, item["type"], index, cost=10) for index, item in enumerate(items)]
    plan = save_plan(vid, wid, items, specs, ceiling=20)
    belief_autopilot.approve(vid, {"plan_id": plan["id"], "fingerprint": plan["fingerprint"]})
    holds = []
    account = account_for_venture(vid)
    assert (account is None) == (wallet_mode == "local")
    account_id = account.id if account else None
    for candidate in specs:
        assert belief_autopilot._claim(vid, candidate)
        belief_autopilot._launched(vid, candidate["id"])
        request_id = "run_" + candidate["id"]
        holds.append(reserve(account_id=account_id, amount_micro=10, purpose="experiment.demand", provider="firecrawl",
            workspace_id=wid, venture_id=vid, request_id=request_id))
        with db()() as session:
            experiment = session.get(Experiment, candidate["id"])
            context = json.loads(experiment.context)
            context.update(run_id=request_id, result={"actual_micro": 0, "measured": {"events": 0}})
            experiment.context, experiment.state = json.dumps(context), "concluded"
            session.commit()
    assert belief_autopilot._read_results(vid, utcnow())["plan"]["reserved_micro"] == 20
    settle(holds[0], actual_micro=2)
    with db()() as session:
        experiment = session.get(Experiment, specs[0]["id"])
        context = json.loads(experiment.context)
        context["job_id"] = "job_receipt"
        experiment.context = json.dumps(context)
        session.add(Job(id="job_receipt", kind="experiment.run", workspace_id=wid, venture_id=vid,
            status="running", payload_json="{}", created_at=utcnow()))
        session.commit()
    assert belief_autopilot._read_results(vid, utcnow())["plan"]["reserved_micro"] == 20
    with db()() as session:
        session.get(Job, "job_receipt").status = "succeeded"
        session.commit()
    current = belief_autopilot._read_results(vid, utcnow())
    assert current["plan"]["reserved_micro"] == 12 and current["plan"]["state"] == "active"
    assert "actual_micro" not in next(test for test in current["plan"]["tests"] if test["experiment_id"] == specs[1]["id"])
    assert belief_autopilot._read_results(vid, utcnow())["plan"]["reserved_micro"] == 12
    settle(holds[1], actual_micro=3)
    draft = {"experiment_id": specs[1]["id"], "state": "delivery_unconfirmed", "delivery_approval_id": "approval_receipt"}
    monkeypatch.setattr(outreach, "drafts", lambda _vid: [draft])
    with db()() as session:
        experiment = session.get(Experiment, specs[1]["id"])
        context = json.loads(experiment.context)
        context["spec"]["runner"] = "outreach_split"
        experiment.context = json.dumps(context)
        session.add(Approval(id="approval_receipt", workspace_id=wid, kind="external", subject_ref="draft_receipt",
            summary="Your approved email", detail_json=json.dumps({"external_execution": {"request_id": "outbound_receipt"}}),
            requested_by="local", status="approved", expires_at=utcnow() + timedelta(days=1), created_at=utcnow()))
        session.commit()
    assert belief_autopilot._read_results(vid, utcnow())["plan"]["reserved_micro"] == 12
    delivery = reserve(account_id=account_id, amount_micro=2, purpose="outbound:gtm_email_send", provider="composio_gmail",
        workspace_id=wid, venture_id=vid, request_id="outbound_receipt")
    draft["state"] = "sent"
    assert belief_autopilot._read_results(vid, utcnow())["plan"]["reserved_micro"] == 12
    settle(delivery, actual_micro=2)
    assert belief_autopilot._read_results(vid, utcnow())["plan"]["reserved_micro"] == 7
    request_id = f"{plan['id']}:parent:followup"
    belief_autopilot._admit_followup(vid, plan["id"], 5, request_id=request_id, account_id=account_id)
    hold = reserve(account_id=account_id, amount_micro=5, purpose="experiment.beliefs", provider="test_model",
        workspace_id=wid, venture_id=vid, request_id=request_id)
    settle(hold, actual_micro=1)
    with db()() as session:
        saved = beliefs.row(session, vid)
        context = json.loads(saved.context)
        context["plan"]["calls"][0]["finished"] = True
        saved.context = json.dumps(context)
        session.commit()
    assert belief_autopilot._read_results(vid, utcnow())["plan"]["reserved_micro"] == 8
    assert belief_autopilot._read_results(vid, utcnow())["plan"]["reserved_micro"] == 8
    with charge_sync(workspace_id=wid, venture_id=vid, account_id=account_id,
            purpose="experiment.free", estimate_micro=0, provider="local", request_id="free_receipt") as meter:
        meter.record_micro(0)
    with db()() as session:
        assert belief_autopilot._receipt_total(session, wid, vid, ["free_receipt"], account_id=account_id) == 0
        assert belief_autopilot._receipt_total(session, "ws_other", vid, ["free_receipt"], account_id=account_id) is None
