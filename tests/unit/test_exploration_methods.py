"""Each method uses unrelated business and consumer fixtures and real runner state."""
import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from tests.company_os_harness import local_owner_clients
from tests.unit.test_exploration import exploration_market, market, seed, venture
from tests.unit.test_autonomous_experiments import credit_meter, save_market_context
from tests.unit.test_outreach_experiment_splits import contact, journey_db, outreach_case


@pytest.fixture
def method_outreach_case(outreach_case):
    from app.core.local_mode import is_local
    from app.tenancy import bind

    if is_local():
        from app.core.providers_local import save_settings
        from app.core.local_secrets import update_settings

        save_settings(provider="openai_compatible", base_url="http://127.0.0.1:11434/v1", models={"chat": "fixture-chat"})
        update_settings("capabilities", {"send_email": True}, {})
    with bind(workspace_id=outreach_case["wid"], venture_id=outreach_case["vid"], actor=outreach_case["actor"]):
        yield outreach_case


@pytest.mark.parametrize("missing", ["who", "problem", "playbook"])
def test_too_small_next_step_asks_only_for_missing_input(exploration_market, monkeypatch, missing):
    from app.chat.actions import disabled_reason
    from app.chat.execute import execute_action
    from app.experiments import activity, exploration
    from app.state import MEM
    from app.chat import customers
    from tests.unit.experiments.test_acceptance_exploration import _action

    _client, vid, _wid, view, *_rest = exploration_market
    options = [{"statement": f"You could try message {index} for {view['icp']['who']}.",
        "audience": view["icp"]["who"], "problem": view["icp"]["problem"]} for index in range(6)]
    first = exploration.create_round(vid, kind="value_proposition", options=options[:3])
    for option in first["options"]:
        activity.record(vid, _action(option["id"], action="reply_received", evidence_level="market_response",
            outcome={"events": 1, "positive_reply_rate": 1.0}))
    resolved = exploration.resolve_round(vid, first["id"], next_options=options[3:])
    if missing == "playbook":
        view["playbooks"] = []
    else:
        view["icp"].pop(missing)
        save_market_context(vid, view)
    monkeypatch.setattr(exploration.autonomous, "select_runnable", lambda *_args: [])
    step = resolved["actions"][0]
    following = asyncio.run(execute_action(vid, step["action"], payload=step["payload"]))
    assert following["id"] == resolved["next_round"]["id"] and following["kind"] == "question"
    assert not following["action_started"] and following["run_ids"] == []
    assert all(not option["experiment_ids"] for option in following["options"])
    action, = following["actions"]
    assert action["action"] == "chat.prefill" and disabled_reason(action["action"]) is None
    assert action["text"] == {"who": "My audience is ", "problem": "The problem is ", "playbook": "Use this email opening: "}[missing]
    if missing != "playbook":
        assert MEM[customers._ASKED.format(vid)]["field"] == missing


def test_segments_queue_all_scans_and_wait_for_every_worker(exploration_market, credit_meter, monkeypatch):
    from app.core.local_mode import is_local
    from app.db.supymem_models import Experiment
    from app.experiments import exploration, methods
    from app.state import db

    _client, vid, _wid, *_rest = exploration_market
    seed(exploration_market)
    round_ = asyncio.run(exploration.start_round(vid, "segment"))
    result = asyncio.run(exploration.act(vid, "experiment.exploration.start", {"round_id": round_["id"]}))
    assert len(result["run_ids"]) == (3 if is_local() else 9)
    with db()() as session:
        rows = list(session.scalars(select(Experiment).where(Experiment.venture_id == vid,
            Experiment.id.in_([identifier for option in exploration.get_round(vid, round_["id"])["options"] for identifier in option["experiment_ids"]]))))
        assert len(rows) == 9
        if is_local():
            assert sum(row.state == "running" for row in rows) == 3
            assert sum(row.state == "proposed" for row in rows) == 6
        assert {json.loads(row.context)["spec"]["scan"] for row in rows} == set(methods.SEGMENT_SCANS)
        for option in round_["options"]:
            specs = [json.loads(row.context)["spec"] for row in rows if json.loads(row.context)["spec"]["option_id"] == option["id"]]
            assert len(specs) == 3 and all(spec["icp"]["who"] in option["statement"] or spec["icp"]["who"].startswith("Group ") for spec in specs)
    again = asyncio.run(exploration.act(vid, "experiment.exploration.start", {"round_id": round_["id"]}))
    assert again["run_ids"] == result["run_ids"]
    card = exploration.get_round(vid, round_["id"])
    assert not card["ended_at"]
    assert all("buying_intent" in row["missing_data"] for row in card["comparison"]["options"])
    with db()() as session:
        row = session.get(Experiment, card["options"][0]["experiment_ids"][0])
        context = json.loads(row.context)
        row.state = "concluded"
        row.context = json.dumps({**context, "result": {"sources": [], "measured": {"events": 0}}})
        session.commit()
    exploration._refresh(vid)
    assert not exploration.get_round(vid, round_["id"])["options"][0]["finished"]


def test_value_propositions_bind_comparable_groups_and_keep_review(method_outreach_case, monkeypatch):
    from app.autopilot import guard, outreach
    from app.db.models import GtmContact
    from app.db.supymem_models import Experiment
    from app.experiments import activity, exploration, methods, outreach_tests
    from core.clock import utcnow

    outreach_case = method_outreach_case
    vid, view = outreach_case["vid"], outreach_case["snapshot"]["journey"]
    monkeypatch.setattr(exploration, "get_policy", lambda _key: {"exploration_v1": True})
    view["allow_sending"] = False
    round_ = exploration.create_round(vid, kind="value_proposition", options=[
        {"statement": f"You could try message {index} for {view['icp']['who']} to address {view['icp']['problem']}.",
            "audience": view["icp"]["who"], "problem": view["icp"]["problem"]} for index in range(3)])
    pair = [round_["options"][index]["id"] for index in (0, 2)]
    spec, = exploration._prepare_runs(vid, round_["id"], None, option_ids=pair)
    assert spec["option_ids"] == pair
    assert len({arm["params"]["playbook"] for arm in spec["variants"]}) == 1
    assert spec["approval"] == "each_send" and "source_disclosure" in spec["compliance"]
    assert not view["allow_sending"]
    people = [contact(outreach_case, index) for index in range(6)]
    if outreach_case["consumer"]:
        assert outreach_tests._assign(spec, {}) == {}
        with outreach_case["factory"]() as session:
            for identifier in people:
                person = session.get(GtmContact, identifier)
                person.contact_provenance = "founder_import"
                person.enrichment_json = json.dumps({**json.loads(person.enrichment_json),
                    "founder_import": {"lawful_basis_attested": True, "email_check": "mx"}})
            session.commit()
    monkeypatch.setattr(outreach, "_foreign_contact", lambda *_args: False)
    assignments = outreach_tests._assign(spec, {})
    assert list(assignments.values()).count("arm_0") == list(assignments.values()).count("arm_1") == 3
    monkeypatch.setattr(guard, "get_settings", lambda _vid: {"send_max_per_day": 40})
    assert methods.send_plan(spec)["daily_limit"] == 40
    monkeypatch.setattr(guard, "get_settings", lambda _vid: {"send_max_per_day": 0})
    assert methods.send_plan(spec)["daily_limit"] == guard.DEFAULT_SEND_MAX
    monkeypatch.setattr(guard, "get_settings", lambda _vid: {"send_max_per_day": 40})
    monkeypatch.setattr(outreach, "send_slots_used_today", lambda _vid: 39)
    assert outreach_tests.send_block(vid, {"experiment_id": spec["id"], "variant": "arm_0"}) == "experiment_not_running"
    monkeypatch.setattr(outreach, "send_slots_used_today", lambda _vid: 40)
    assert outreach_tests.send_block(vid, {"experiment_id": spec["id"], "variant": "arm_0"}) == "experiment_daily_cap"
    source_body = "Hi,\n\nCould this help?\n\nYour email address is listed publicly on example.org.\n" + outreach.OPT_OUT + "\nSender address"
    saved = []
    drafts = [{"doc_id": str(index), "experiment_id": spec["id"], "variant": arm["key"],
        "state": "draft", "day": 1, "body": source_body, "review_state": "draft"} for index, arm in enumerate(spec["variants"])]
    monkeypatch.setattr(outreach, "drafts", lambda *_args: drafts)
    monkeypatch.setattr(outreach, "_save_draft", lambda _vid, draft, **_kwargs: saved.append(deepcopy(draft)))
    methods.prepare_messages(spec)
    assert len(saved) == 2 and saved[0]["body"] != saved[1]["body"]
    assert all(source_body.split("\n\n")[-1] in draft["body"] and draft["state"] == "draft" for draft in saved)
    methods.prepare_messages(spec)
    assert len(saved) == 2
    receipts = activity.list_activity(vid)
    assert len(receipts) == 2 and all(item["action"] == "draft" for item in receipts)
    assert {item["option_id"] for item in receipts} == set(pair)
    drafts[0]["body"] = source_body
    methods.prepare_messages(spec)
    assert len(saved) == 3 and spec["variants"][0]["params"]["value_proposition"] in saved[-1]["body"]
    with outreach_case["factory"]() as session:
        row = session.get(Experiment, spec["id"])
        row.state, row.started_at = "running", utcnow()
        row.context = json.dumps({**json.loads(row.context), "assignments": assignments})
        session.commit()
    monkeypatch.setattr(outreach, "send_slots_used_today", lambda _vid: 0)
    tampered = {**drafts[0], "contact_id": next(person for person, variant in assignments.items() if variant == "arm_0"),
        "body": source_body}
    assert outreach_tests.send_block(vid, tampered) == "experiment_message_changed"
    card = exploration.get_round(vid, round_["id"])
    assert card["comparison"]["too_small"]
    assert card["send_plan"]["remaining"] == spec["min_sample"]
    assert methods.send_plan(card["options"][0]["spec"], sent=10)["remaining"] == 10
    assert card["comparison"]["next_step"]["payload"]["extend"] is True
    with pytest.raises(ValueError, match="two distinct"):
        exploration._prepare_runs(vid, round_["id"], None, option_ids=[])


def test_manual_methods_stay_copyable_and_do_not_queue(exploration_market):
    from app.experiments import exploration

    _client, vid, _wid, view, *_rest = exploration_market
    options = [{"statement": f"You could try route {index} for {view['icp']['who']}.",
        "audience": view["icp"]["who"], "problem": view["icp"]["problem"], "kind": kind}
        for index, kind in enumerate(("linkedin", "community", "community"))]
    card = exploration.create_round(vid, kind="segment", options=options)
    assert exploration._prepare_runs(vid, card["id"], None) == []
    public = exploration.list_rounds(vid)["rounds"][-1]
    assert [option["method"] for option in public["options"]] == ["linkedin_draft", "community_post_draft", "community_post_draft"]
    assert all(option["draft"] and option["constraints"] and not option["experiment_ids"] for option in public["options"])
    assert "thanks for connecting" not in public["options"][0]["draft"].lower()
    for kind in ("linkedin", "community"):
        drafts = [{"statement": f"You could try {kind} option {index} for {view['icp']['who']}.",
            "audience": view["icp"]["who"], "problem": view["icp"]["problem"]} for index in range(3)]
        channel = exploration.create_round(vid, kind=kind, options=drafts)
        assert exploration._prepare_runs(vid, channel["id"], None) == []
        assert all(option["method"] == exploration.methods.METHODS[kind] and option["draft"]
            for option in exploration.get_round(vid, channel["id"])["options"])


def test_approach_review_uses_existing_lens_and_rejects_invented_sources(exploration_market, monkeypatch):
    from app.experiments import exploration, methods
    from app.gstack import prompts

    _client, vid, wid, view, *_rest = exploration_market
    seed(exploration_market, "approach")
    round_ = asyncio.run(exploration.start_round(vid, "approach"))
    specs = exploration._prepare_runs(vid, round_["id"], None)
    assert len(specs) == 3 and all(spec["approach_review"] for spec in specs)
    monkeypatch.setattr(prompts, "ceo_review_brief", lambda **_kwargs: "Scope reduction review lens.")
    cited = "https://example.org/recorded"
    async def review(messages, **kwargs):
        assert "Scope reduction review lens" in messages[0]["content"]
        assert kwargs["purpose"] == "experiment.approach_review"
        assert view["icp"]["problem"] in messages[1]["content"]
        response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps({"flags": [
            {"kind": "overcomplexity", "summary": "You could test the simpler workflow first.", "source_urls": [cited]}]})))])
        await kwargs["before_meter_exit"](response, {"actual_micro": 7})
        return response
    monkeypatch.setattr(methods.metered, "llm", review)
    result = {"sources": [{"url": cited, "quote": view["icp"]["problem"]}]}
    saved = []
    async def checkpoint(result, _receipt):
        saved.append(deepcopy(result))
    reviewed = asyncio.run(methods.review_approach(specs[0], result, "run", checkpoint))
    assert reviewed["review_flags"][0]["assessment"] is True
    assert reviewed["actual_micro"] == 7 and saved[-1] == reviewed
    cited = "https://example.org/not-recorded"
    rejected = asyncio.run(methods.review_approach(specs[0], result, "run", checkpoint))
    assert rejected["sources"] == result["sources"] and rejected["review_error"]
    assert "review_flags" not in rejected and rejected["actual_micro"] == 7


def test_comparison_preserves_sources_and_counts_arm_receipts_once(exploration_market):
    from app.experiments import compare

    _client, _vid, _wid, view, *_rest = exploration_market
    option = {"id": "option", "kind": "segment", "method": "desk_research", "spec": {"min_sample": 2}}
    source = {"url": "https://example.org/source", "quote": view["icp"]["problem"]}
    receipt = {"id": 1, "option_id": "option", "action": "compare", "tool": "experiment_results",
        "evidence_summary": "", "text": "Checked your results", "evidence_level": "desk_research",
        "outcome": {"status": "succeeded", "scan": "demand", "sources": [source]}}
    row = compare.option_result(option, [receipt, {**receipt, "id": 2,
        "outcome": {**receipt["outcome"], "scan": "reviews"}}])
    assert row["research"]["pain"] == [source] and row["research"]["need"] == [source]
    assert row["research"]["buying_intent"] is None and "scan:community" in row["missing_data"]
    receipt["outcome"] = {"status": "succeeded", "experiment_id": "email_test",
        "arm_result": {"sent": 4, "replies": 2, "positive_replies": 1, "booked": 2}}
    receipt["evidence_level"] = "market_response"
    row = compare.option_result(option, [receipt, {**receipt, "id": 2}])
    assert row["counts"]["sent"] == 4 and row["counts"]["positive_replies"] == 1
    assert row["counts"]["meetings"] == 2 and "2 meetings booked" in row["buying_intent"]
    option["spec"]["metric"] = "booking_rate"
    assert compare.option_result(option, [receipt])["response_rate"] == .5
