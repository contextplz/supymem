"""Saved public evidence yields only opt-in ideas in unrelated markets."""
import asyncio
import json

import pytest
from tests.company_os_harness import local_owner_clients

from tests.unit.test_autonomous_experiments import credit_meter, run_jobs, venture
from tests.unit.test_market_beliefs import base_market, market, set_local_experiments


@pytest.fixture
def scan_case(market, monkeypatch):
    from app.credits import policy
    from app.experiments import autonomous

    _client, venture_id, workspace_id, view, *_rest = market
    monkeypatch.setattr(policy, "get_policy", lambda _key, _default=None: {"experiments_autopilot_v1": True})
    monkeypatch.setattr(autonomous, "get_policy", policy.get_policy)
    set_local_experiments(True)
    who, problem = (("restaurant owners", "reconciling supplier invoices") if view["icp"]["mode"] == "company"
                    else ("aquarium keepers", "maintaining water chemistry"))
    return venture_id, workspace_id, view["icp"], who, problem


def save_scan(case, sources, *, suffix="first", **overrides):
    from app.db.supymem_models import Experiment
    from app.experiments import ledger
    from app.state import db
    from core.clock import utcnow

    venture_id, workspace_id, icp, _who, _problem = case
    identifier = "exp_spinoff_" + suffix
    with db()() as session:
        spec = {"id": identifier, "venture_id": venture_id, "workspace_id": workspace_id, "scan": "demand",
                "icp": icp, "direction": ledger.current_direction(session, venture_id)}
        spec.update(overrides.pop("spec", {}))
        context = {"runner": "system", "spec": spec, "result": {"sources": sources,
                   "insight": "Your saved public observations.", "outcome": "inconclusive"}}
        context.update(overrides.pop("context", {}))
        session.add(Experiment(id=identifier, venture_id=overrides.pop("venture_id", venture_id),
            workspace_id=overrides.pop("workspace_id", workspace_id), kind=ledger.KIND,
            hypothesis="Your public complaint scan", state=overrides.pop("state", "concluded"),
            ended_at=utcnow(), context=json.dumps(context), **overrides))
        session.commit()
    return identifier


def source(who, problem, *, site="accounts.example", path="one", marker="struggle with"):
    return {"url": f"https://{site}/{path}", "title": "A public account",
            "quote": f"{who} {marker} {problem}."}


def test_spinoffs_read_auxiliary_state_without_holding_the_writer(scan_case, monkeypatch):
    from app.db.models import VentureDocument
    from app.db.session import serialize_writes
    from app.db.supymem_models import Experiment
    from app.experiments import ledger, spinoffs
    from app.state import db

    venture_id, workspace_id, _icp, who, problem = scan_case
    identifier = save_scan(scan_case, [source(who, problem)])
    original = ledger.loop_state
    changed = [False]

    def state(selected):
        with db()() as session:
            serialize_writes(session, key="auxiliary-state:" + selected)
            if changed[0]:
                row = session.get(Experiment, identifier)
                context = json.loads(row.context)
                context["result"]["sources"] = []
                row.context = json.dumps(context)
                session.commit()
        return original(selected)

    monkeypatch.setattr(ledger, "loop_state", state)
    assert len(spinoffs.consume(venture_id, workspace_id, identifier)) == 1
    with db()() as session:
        document = session.query(VentureDocument).filter_by(venture_id=venture_id, kind=spinoffs.KIND).one()
        before = document.provenance_json
    changed[0] = True
    assert spinoffs.consume(venture_id, workspace_id, identifier) == []
    with db()() as session:
        assert session.get(VentureDocument, document.id).provenance_json == before


def test_spinoffs_reject_questions_hypotheticals_conditionals_and_negations(scan_case):
    from app.db.models import ChatMessage, VentureDocument
    from app.experiments import spinoffs
    from app.state import db

    venture_id, workspace_id, _icp, who, problem = scan_case
    quotes = [f"{who} struggle with how to handle {problem}?",
              f"{who} struggle with {problem} in hypothetical cases.",
              f"{who} struggle with {problem} when demand grows.",
              f"{who} struggle with {problem} unless routines change.",
              f"{who} struggle with {problem} if routines change.",
              f"{who} struggle with {problem} without errors.",
              f"{who} don’t need help with {problem}.",
              f"Suppose this happens. {who} struggle with {problem}.",
              f"{who} struggle with {problem}; assume this happens."]
    sources = [{**source(who, problem, path=str(index)), "quote": quote}
               for index, quote in enumerate(quotes)]
    identifier = save_scan(scan_case, sources, spec={"scan": "search_demand"})
    assert spinoffs.consume(venture_id, workspace_id, identifier) == []
    with db()() as session:
        assert session.query(VentureDocument).filter_by(kind=spinoffs.KIND).count() == 0
        assert session.query(ChatMessage).filter_by(kind="confirmation").count() == 0
    asserted = source(who, problem, path="asserted")
    identifier = save_scan(scan_case, [asserted], suffix="asserted")
    proposals = spinoffs.consume(venture_id, workspace_id, identifier)
    assert len(proposals) == 1 and proposals[0]["score"] == 1
    assert proposals[0]["evidence_strength"] == proposals[0]["reach"] == 1
    assert [item["quote"] for item in proposals[0]["evidence"]] == [asserted["quote"]]


def test_saved_scans_cluster_score_and_confirm_without_creating_ventures(scan_case, monkeypatch):
    from app.analytics.feature_catalog import ACTION_SLUG
    from app.chat import experiments
    from app.db.models import ChatMessage, Venture, VentureDocument
    from app.db.supymem_models import EntryChatProposal, Experiment
    from app.experiments import ledger, spinoffs
    from app.state import db

    venture_id, workspace_id, icp, who, problem = scan_case
    sources = [source(who, problem), source(who, problem, path="two"),
               source(icp["who"], icp["problem"]), source(who, problem, path="unasserted", marker="could struggle with")]
    identifier = save_scan(scan_case, sources)
    save_scan(scan_case, [sources[0], source(who.upper(), problem.upper(), site="stories.example")], suffix="second",
              spec={"option_id": "an_alternative", "icp": {"who": who, "problem": problem}})
    with db()() as session:
        before = {row.id: row.raw_idea for row in session.query(Venture)}
        before_scans = {row.id: row.context for row in session.query(Experiment)}
    proposals = spinoffs.consume(venture_id, workspace_id, identifier)
    assert len(proposals) == 1
    proposal = proposals[0]
    assert proposal["who"] == who and proposal["problem"] == problem
    assert proposal["score"] == 6 and proposal["evidence_strength"] == 3 and proposal["reach"] == 2
    assert len(proposal["evidence"]) == 3 and "public complaint scan" in proposal["first_experiment"]
    assert "Nothing is sent or published" in proposal["first_experiment"]
    assert {item["quote"].lower() for item in proposal["evidence"]} == {source(who, problem)["quote"].lower()}
    state = ledger.loop_state(venture_id)
    with db()() as session:
        document = session.query(VentureDocument).filter_by(kind=spinoffs.KIND).one()
        assert json.loads(document.provenance_json)["proposals"] == proposals
        message = session.query(ChatMessage).filter_by(kind="confirmation").one()
        actions = json.loads(message.actions_json)
        assert len(actions) == 1 and actions[0]["action"] == "experiment.spinoff"
        assert ACTION_SLUG[actions[0]["action"]] == "experiments"
        payload = actions[0].get("payload") or actions[0]["confirmation"]["resulting_state"]
        candidate = {field: payload[field] for field in ("kind", "text", "idea")}
        assert payload == experiments._decision_payload(venture_id, "experiment.spinoff", state, candidate)
        assert payload["idea"] == proposal["idea"] and payload["parent_venture_id"] == venture_id
        assert "market size" in message.body_md and source(who, problem)["quote"] in message.body_md
        assert "\u2014" not in message.body_md
        assert session.query(EntryChatProposal).count() == 0
        assert {row.id: row.raw_idea for row in session.query(Venture)} == before
        assert {row.id: row.context for row in session.query(Experiment)} == before_scans
    assert spinoffs.consume(venture_id, workspace_id, identifier) == proposals
    assert spinoffs.consume(venture_id, workspace_id, "exp_spinoff_second") == proposals
    with db()() as session:
        assert session.query(VentureDocument).filter_by(kind=spinoffs.KIND).count() == 1
        assert session.query(ChatMessage).filter_by(kind="confirmation").count() == 1

    confirmed = []

    async def confirm(received_venture, received_payload, actor):
        confirmed.append((received_venture, received_payload, actor))
        return experiments._reply("You can review your separate venture.", kind="experiment_spinoff")

    monkeypatch.setattr(experiments, "_spinoff_run", confirm)
    with pytest.raises(ValueError, match="experiment_decision_invalid"):
        asyncio.run(experiments.decision_run(venture_id, "experiment.spinoff", {**payload, "evidence": []}, "local"))
    assert not confirmed
    reply = asyncio.run(experiments.decision_run(venture_id, "experiment.spinoff", payload, "local"))
    assert reply["kind"] == "experiment_spinoff" and len(confirmed) == 1


@pytest.mark.parametrize("flags", [None, {}, {"experiments_autopilot_v1": False},
    {"experiments_autopilot_v1": 1}, {"experiments_autopilot_v1": "true"}])
def test_spinoffs_flag_off_does_not_read_or_write(scan_case, monkeypatch, flags):
    from app.credits import policy
    from app.experiments import autonomous, spinoffs

    venture_id, workspace_id, _icp, who, problem = scan_case
    identifier = save_scan(scan_case, [source(who, problem)])
    monkeypatch.setattr(policy, "get_policy", lambda _key: flags)
    set_local_experiments(False)

    def unexpected(*_args, **_kwargs):
        pytest.fail("Flag-off proposals must not read scans or perform work")

    monkeypatch.setattr(autonomous, "_snapshot", unexpected)
    monkeypatch.setattr(spinoffs, "db", unexpected)
    assert spinoffs.consume(venture_id, workspace_id, identifier) == []


def test_spinoff_policy_failure_does_not_fail_the_finished_scan(scan_case, monkeypatch, caplog):
    from app.credits import policy
    from app.experiments import spinoffs

    venture_id, workspace_id, _icp, who, problem = scan_case
    identifier = save_scan(scan_case, [source(who, problem)])

    def unavailable(_key):
        raise RuntimeError("policy_store_unavailable")

    from app.core.local_mode import is_local
    monkeypatch.setattr(spinoffs.ledger if is_local() else policy,
        "autopilot_enabled" if is_local() else "get_policy", lambda *args: unavailable(None))
    assert asyncio.run(spinoffs.on_scan_completed({"id": identifier, "venture_id": venture_id,
        "workspace_id": workspace_id, "scan": "demand"})) == []
    assert any(getattr(record, "action", None) == "experiment.spinoffs.propose"
        and record.error_type == "RuntimeError" for record in caplog.records)


def test_spinoffs_ignore_unfinished_failed_cross_scope_and_unsourced_results(scan_case):
    from app.db.models import VentureDocument
    from app.experiments import spinoffs
    from app.state import db

    venture_id, workspace_id, icp, who, problem = scan_case
    evidence = [source(who, problem)]
    invalid = [save_scan(scan_case, evidence, suffix="running", state="running"),
               save_scan(scan_case, evidence, suffix="foreign", venture_id="ven_someone_else"),
               save_scan(scan_case, evidence, suffix="workspace", workspace_id="ws_someone_else"),
               save_scan(scan_case, evidence, suffix="old", spec={"direction": "an_old_direction"}),
               save_scan(scan_case, evidence, suffix="failed", context={"error": "scan_failed"}),
               save_scan(scan_case, evidence, suffix="outreach", spec={"scan": "outreach"}),
               save_scan(scan_case, evidence, suffix="pending", context={"result": {}, "pending_result": {"sources": evidence}})]
    for identifier in invalid:
        assert spinoffs.consume(venture_id, workspace_id, identifier) == []
    unusable = [source(icp["who"], icp["problem"]), {"url": "https://accounts.example/missing"},
                {**evidence[0], "url": "javascript:alert(1)"}, {**evidence[0], "url": "https://user:secret@accounts.example/"},
                source(who, problem, marker="do not struggle with"),
                {**evidence[0], "quote": f"I struggle with {problem}."}]
    assert spinoffs._proposals([{"id": "exp_unusable", "sources": unusable}], icp) == []
    identifier = save_scan(scan_case, unusable, suffix="unusable")
    assert spinoffs.consume(venture_id, workspace_id, identifier) == []
    identifier = save_scan(scan_case, evidence, suffix="valid")
    assert spinoffs.consume(venture_id, workspace_id, identifier) == []
    with db()() as session:
        assert session.query(VentureDocument).filter_by(kind=spinoffs.KIND).count() == 0


def test_spinoffs_bound_rank_and_keep_literal_workaround_evidence(scan_case):
    from app.db.models import ChatMessage
    from app.experiments import spinoffs
    from app.state import db

    venture_id, workspace_id, _icp, who, problem = scan_case
    shifts = ["morning", "evening", "night", "weekend", "holiday", "summer", "winter", "spring"]
    sources = [source(who, f"{problem} during {shift} shifts", path=shift) for shift in shifts]
    sources += [source(who, problem, path="manual", marker="use a notebook to"),
                source(who, problem, site="more.example", path="manual", marker="use a notebook to"),
                {"url": "https://accounts.example/firstperson", "title": "A first-person account",
                 "quote": f"As {who}, I struggle with {problem}."}]
    identifier = save_scan(scan_case, sources)
    proposals = spinoffs.consume(venture_id, workspace_id, identifier)
    assert len(proposals) == 5 and proposals[0]["score"] == 6
    assert proposals[0]["problem"] == problem
    assert any("use a notebook" in item["quote"] for item in proposals[0]["evidence"])
    assert [item["score"] for item in proposals] == sorted((item["score"] for item in proposals), reverse=True)
    assert spinoffs.consume(venture_id, workspace_id, identifier) == proposals
    with db()() as session:
        actions = json.loads(session.query(ChatMessage).filter_by(kind="confirmation").one().actions_json)
        assert len({action["label"] for action in actions}) == 5


def test_spinoffs_keep_saved_quotes_literal_in_citations(scan_case):
    from app.db.models import ChatMessage
    from app.db.supymem_models import Experiment
    from app.experiments import spinoffs
    from app.state import db

    venture_id, workspace_id, _icp, who, problem = scan_case
    evidence = source(who, problem + " using a manual log \u2014 again today")
    identifier = save_scan(scan_case, [evidence])
    with db()() as session:
        before = session.get(Experiment, identifier).context
    proposals = spinoffs.consume(venture_id, workspace_id, identifier)
    assert proposals[0]["evidence"][0]["quote"] == evidence["quote"]
    with db()() as session:
        message = session.query(ChatMessage).filter_by(kind="confirmation").one()
        citations = json.loads(message.citations_json)
        assert next(item["quote"] for item in citations if item.get("url") == evidence["url"]) == evidence["quote"]
        assert session.get(Experiment, identifier).context == before


def test_worker_consumes_only_committed_successful_public_scan_results(scan_case, market, credit_meter, monkeypatch):
    from app.db.supymem_models import Experiment
    from app.experiments import autonomous, spinoffs
    from app.state import db

    venture_id, _workspace_id, _icp, who, problem = scan_case
    consumed = []
    consume = spinoffs.consume

    def after_commit(received_venture, received_workspace, experiment_id):
        with db()() as session:
            row = session.get(Experiment, experiment_id)
            assert row.state == "concluded" and json.loads(row.context)["result"]["sources"]
        consumed.append(experiment_id)
        return consume(received_venture, received_workspace, experiment_id)

    async def scan(_spec, _provider):
        return {"sources": [source(who, problem)], "insight": "Your public scan finished.",
                "outcome": "inconclusive", "measured": {}, "actual_micro": 0}

    monkeypatch.setattr(spinoffs, "consume", after_commit)
    monkeypatch.setattr(autonomous, "_scan", scan)
    spec = next(item for item in autonomous.available(venture_id) if item["scan"] == "demand")
    action = autonomous.prepare(venture_id, spec["id"])["actions"][0]
    payload = action.get("payload") or action["confirmation"]["resulting_state"]
    asyncio.run(autonomous.run(venture_id, payload))
    run_jobs()
    assert consumed == [spec["id"], spec["id"]]
    assert consume(venture_id, spec["workspace_id"], spec["id"])[0]["who"] == who
