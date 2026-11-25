import asyncio
import json
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from tests.unit.gtm.test_journey import _mail, journey_db


@pytest.fixture(params=[
    ("Route notebook", "freight coordinators in the US", "lost dispatch assignments", "US", ["Tampa", "Raleigh"], ["regional freight teams", "independent freight coordinators"]),
    ("Garden schedule", "hobby gardeners in the UK", "missed watering schedules", "GB", ["Leeds", "York"], ["balcony gardeners", "allotment gardeners"]),
])
def outreach_case(journey_db, monkeypatch, request):
    from app.autopilot import outreach
    from app.credits.accounts import ensure_account
    from app.db.models import CreditAccount, Venture
    from app.experiments import autonomous, ledger
    from app.gtm import journey
    from app.tenancy import bind
    from core.clock import utcnow
    from core.ids import new_id

    name, who, problem, country, cities, segments = request.param
    case = {**journey_db, "vid": new_id("ven"), "country": country, "consumer": country == "GB", "name": name}
    icp = {"who": who, "problem": problem, "competitors": [], "offer": "", "needs_input": False,
        "country": country, "cities": cities, "segments": segments, "excluded_segments": [segments[1]],
        "company_size": "3-30",
        "customer_type": "B2C" if case["consumer"] else "B2B",
        "confirmed": True, "chat_confirmed": True}
    with case["factory"]() as session:
        now = utcnow()
        session.add(Venture(id=case["vid"], workspace_id=case["wid"], name=name,
            raw_idea=f"A notebook for {who} to prevent {problem}.", stage="draft", created_at=now, updated_at=now))
        session.commit()
        row = journey._row(session, case["vid"], create=True)
        row.provenance_json = json.dumps({"icp": icp, "playbooks": [{"key": "value_first", "title": "Value first"}], "state": "ready"})
        session.commit()
        direction = ledger.current_direction(session, case["vid"])
    account = ensure_account(case["vid"], workspace_id=case["wid"], owner_person_id=case["actor"])
    with case["factory"]() as session:
        if account is None:
            now = utcnow()
            account = CreditAccount(id=new_id("cra"), scope="venture", venture_id=case["vid"],
                workspace_id=case["wid"], owner_person_id=case["actor"], granted_micro=0,
                spent_micro=0, reserved_micro=0, status="active", version=0,
                created_at=now, updated_at=now)
            session.add(account)
            session.flush()
            session.expunge(account)
        source_account = session.get(CreditAccount, case["account"]) if case["account"] else None
        session.get(CreditAccount, account.id).granted_micro = source_account.granted_micro if source_account else 0
        case["account"] = account.id
        session.commit()
    _mail(case)
    from app.core.local_mode import is_local
    from app.core.local_secrets import update_settings
    if is_local():
        update_settings("capabilities", {"send_email": True}, {})
        from app.core.local_capabilities import enabled
        from app.gtm import local_mail
        monkeypatch.setattr(local_mail, "enabled", enabled)
    monkeypatch.setattr(outreach, "site_url", lambda vid: None)
    monkeypatch.setattr(journey, "_icp_seed", lambda vid: {"idea": {"audience": who, "problem": problem, "customer_type": icp["customer_type"], "confirmed": True}})
    monkeypatch.setattr(outreach, "_brief", lambda vid: {"idea": {"audience": who, "problem": problem, "customer_type": icp["customer_type"], "confirmed": True}})
    snapshot = {"venture_id": case["vid"], "workspace_id": case["wid"], "direction": direction,
        "stage": "draft", "journey": {"icp": icp, "playbooks": [{"key": "value_first"}]},
        "segments": segments, "waitlist": {}, "app_id": None}
    monkeypatch.setattr(autonomous, "_snapshot", lambda vid: snapshot if vid == case["vid"] else None)
    case.update(icp=icp, snapshot=snapshot)
    with bind(workspace_id=case["wid"], venture_id=case["vid"], actor=case["actor"]):
        yield case


def proposal(case, catalog="E19"):
    from app.credits import metered
    from app.experiments import autonomous, outreach_tests

    base = {key: case["snapshot"][key] for key in ("venture_id", "workspace_id", "direction")}
    base["icp"] = case["icp"]
    provider = SimpleNamespace(name="firecrawl", search_micro=metered.price_micro("firecrawl_search"),
        scrape_micro=metered.price_micro("firecrawl_scrape"))
    spec = next(item for item in outreach_tests.specs(case["snapshot"], base, provider) if item["catalog_key"] == catalog)
    spec["fingerprint"] = autonomous._fingerprint(spec)
    spec["id"] = "exp_" + spec["fingerprint"][:24]
    return spec


def test_outreach_failure_keeps_the_manual_draft_action_label(outreach_case):
    from app.experiments import autonomous, outreach_tests

    spec = {**proposal(outreach_case), "error": "Your search could not finish. Choose a next step."}
    action = autonomous.failure_reply(spec)["actions"][0]
    assert action == outreach_tests.manual_action(spec)
    assert action["label"] == "Prepare drafts for me" and action["action"] == "outreach.draft"


def contact(case, index, *, audience=None, source=None):
    from app.db.models import GtmContact
    from app.gtm.relationships.store import upsert_company
    from core.clock import utcnow
    from core.ids import new_id

    domain = f"lead{index}.example"
    url = source or f"https://{domain}/contact"
    with case["factory"]() as session:
        company = upsert_company(session, workspace_id=case["wid"], name=f"Public team {index}", domain=domain, evidence_url=url)
        now = utcnow()
        person = GtmContact(id=new_id("gtmc"), workspace_id=case["wid"], company_id=company.id,
            email=f"hello@{domain}", first_name="Rowan", email_confidence="risky", recipient_geo=case["country"],
            lawful_basis="consent", lawful_basis_at=now, lawful_basis_note="Explicitly agreed to this research.",
            source=f"venture:{case['vid']}:public", created_at=now, stage_changed_at=now,
            enrichment_json=json.dumps({"found_at": url, "contact_source_url": url, "business": f"Public team {index}",
                "company_text": audience or case["icp"]["who"], "country": case["country"]}))
        session.add(person)
        session.commit()
        return person.id


def save_experiment(case, spec, assignments, *, claimed=True):
    from app.db.supymem_models import Experiment
    from app.experiments import ledger
    from core.clock import utcnow

    with case["factory"]() as session:
        session.add(Experiment(id=spec["id"], workspace_id=case["wid"], venture_id=case["vid"], kind=ledger.KIND,
            hypothesis=spec["hypothesis"], state="running" if claimed else "proposed", started_at=utcnow() if claimed else None,
            context=json.dumps({"runner": "system", "spec": spec, "assignments": assignments, "claimed": claimed, "actual_micro": 0})))
        session.commit()


def draft(case, spec, person, variant, *, day=1):
    from app.autopilot import outreach
    from app.db.models import GtmContact

    with case["factory"]() as session:
        email = session.get(GtmContact, person).email

    ident = outreach._save_draft(case["vid"], {"contact_id": person, "to": email,
        "subject": "Research question", "body": "Hi Rowan,\n\nCould you describe a recent incident?", "channel": "email", "day": day,
        "state": "draft", "experiment_id": spec["id"], "variant": variant, "playbook": "value_first"})
    with case["factory"]() as session:
        from app.db.models import VentureDocument
        return json.loads(session.get(VentureDocument, ident).provenance_json) | {"doc_id": ident}


def test_variant_tags_survive_both_draft_paths_delivery_recovery_and_human_rollup(outreach_case, monkeypatch):
    from app.autopilot import outreach
    from app.db.models import GtmTouch
    from app.experiments import autonomous
    from app.gtm import journey
    from core.clock import utcnow
    from core.ids import new_id

    case, spec = outreach_case, proposal(outreach_case)
    people = [contact(case, index) for index in range(2)]
    assignments = dict(zip(people, [arm["key"] for arm in spec["variants"]]))
    save_experiment(case, spec, assignments)
    made = journey._draft_ready_admitted(case["wid"], case["vid"], review_only=True, experiment={**spec, "assignments": assignments})
    assert made == 2
    if not case["consumer"]:
        for saved in outreach.drafts(case["vid"], "draft"):
            assert saved["honesty_json"]["experiment_id"] == spec["id"]
            assert saved["honesty_json"]["variant"] == assignments[saved["contact_id"]]
        old = outreach.drafts(case["vid"], "draft")
        rewritten = journey.rewrite_drafts(case["wid"], case["vid"], [item["doc_id"] for item in old])
        assert len(rewritten) == 2
        assert {item["playbook"] for item in outreach.drafts(case["vid"], "draft")} == {"value_first", "worst_week"}
    saved = [draft(case, spec, person, assignments[person]) for person in people]
    for item in saved:
        assert item["honesty_json"] == {"playbook": "value_first", "experiment_id": spec["id"], "variant": item["variant"], "day": 1}
        with case["factory"]() as session:
            outreach._record_sent(session, case["vid"], case["wid"], item | {"honesty_json": json.dumps(item["honesty_json"])}, {"provider_message_id": new_id("msg")}, evidence="Founder checked Sent")
            session.commit()
    with case["factory"]() as session:
        for person, kind, text in [(people[0], "human_neutral", "Last time this happened I lost an entire afternoon."),
                (people[0], "human_neutral", "The same incident cost us two hours."),
                (people[1], "automated", "I am out of office."), (people[1], "human_neutral", "Automatic reply: I am away from the office.")]:
            session.add(GtmTouch(id=new_id("gtmtch"), workspace_id=case["wid"], venture_id=case["vid"], contact_id=person,
                direction="in", simulated=0, channel="email", classification=kind, body_text=text, created_at=utcnow()))
        session.add(GtmTouch(id=new_id("gtmtch"), workspace_id=case["wid"], venture_id=case["vid"], contact_id=people[0],
            direction="out", simulated=0, channel="email", sent_at=utcnow(), created_at=utcnow(), honesty_json="Unrelated legacy note"))
        session.commit()
    result = autonomous._observe(spec)
    assert [(arm["sent"], arm["replies"]) for arm in result["by_variant"]] == [(1, 1), (1, 0)]
    assert result["outcome"] == "inconclusive"
    assert all("\u2014" not in str(value) for value in result.values())
    metadata_only = {key: value for key, value in saved[0].items() if key not in {"experiment_id", "variant", "doc_id"}}
    identifier = outreach._save_draft(case["vid"], metadata_only | {"state": "draft"})
    mirrored = next(item for item in outreach.drafts(case["vid"]) if item["doc_id"] == identifier)
    assert mirrored["experiment_id"] == spec["id"] and mirrored["variant"] == assignments[people[0]]
    from app.experiments.outreach_tests import tags
    assert tags({"honesty_json": json.dumps(mirrored["honesty_json"])}) == {"experiment_id": spec["id"], "variant": assignments[people[0]]}
    samples = [contact(case, index + 200) for index in range(20)]
    incident = "Last time this happened we lost an entire afternoon fixing the schedule."
    for sample_size, treatment_text, replied_indices, expected in ((20, incident, {0, 10, 11}, "supported"),
            (20, "Imagine if that worked. In theory it might help.", {0, 10, 11}, "refuted"),
            (19, incident, {0, 10, 11}, "inconclusive"), (20, incident, {0, 1, 10}, "refuted")):
        threshold_spec, now = {**spec, "id": new_id("exp")}, utcnow() - timedelta(
            days=spec.get("response_window_days", spec["max_duration_days"]))
        with case["factory"]() as session:
            for index, person in enumerate(samples[:sample_size]):
                variant = spec["variants"][index // 10]["key"]
                session.add(GtmTouch(id=new_id("gtmtch"), workspace_id=case["wid"], venture_id=case["vid"], contact_id=person,
                    direction="out", simulated=0, channel="email", sent_at=now, created_at=now,
                    honesty_json=json.dumps({"experiment_id": threshold_spec["id"], "variant": variant, "day": 1})))
                if index in replied_indices:
                    session.add(GtmTouch(id=new_id("gtmtch"), workspace_id=case["wid"], venture_id=case["vid"], contact_id=person,
                        direction="in", simulated=0, channel="email", classification="human_neutral",
                        body_text=incident if index == 0 else treatment_text, created_at=now))
            session.commit()
        measured = autonomous._observe(threshold_spec)
        assert measured["outcome"] == expected
        if expected == "supported":
            assert measured["measured"]["reply_rate_ratio"] == 2
        for catalog in ("E08", "E09", "E39"):
            comparison = {**threshold_spec, "catalog_key": catalog,
                "success_threshold": {**spec["success_threshold"], "value": 2},
                "failure_threshold": {**spec["failure_threshold"], "value": 2}}
            expected_split = "inconclusive" if sample_size < 20 else "refuted" if catalog == "E39" and 1 in replied_indices else "supported"
            assert autonomous._observe(comparison)["outcome"] == expected_split


def test_worst_week_is_research_only_and_keeps_every_compliance_line(outreach_case, monkeypatch):
    from app.autopilot import outreach
    from app.db.models import GtmContact
    from app.gtm import playbooks
    from app.gtm.relationships.email_compliance import complete_body, source_line, sender_address
    from app.credits import metered
    from app.chat import experiments
    from app.experiments import autonomous

    case = outreach_case
    person = contact(case, 50)
    message = playbooks.messages({"company": "Public team"}, "worst_week", case["name"],
        intro=f"I'm Rowan, building {case['name']}: a helpful product.", audience=case["icp"]["who"], problem=case["icp"]["problem"])
    assert case["name"] not in message["email"] and "building" not in message["email"]
    assert case["name"] not in playbooks.messages({}, "worst_week", case["name"])["email"]
    assert case["icp"]["problem"] in message["email"] and message["email"].count("?") == 1
    async def forbidden(*args, **kwargs):
        pytest.fail("The research-only template must not be turned into a pitch by a model")
    monkeypatch.setattr(metered, "llm", forbidden)
    assert asyncio.run(playbooks.compose(case["wid"], case["vid"], message)) == message
    with case["factory"]() as session:
        person = session.get(GtmContact, person)
        body = complete_body(session, case["wid"], person, message["email"], outreach.founder_signature(case["vid"]))
        assert source_line(person) in body and "Your email address is listed publicly on" in body
        assert outreach.OPT_OUT in body and sender_address(session, case["wid"]) in body
        assert not playbooks.copy_problem(body) and "\u2014" not in body
    spec = proposal(case)
    monkeypatch.setattr(autonomous, "available", lambda vid: [spec])
    named = experiments.named_prepare(case["vid"], f"Run {spec['title'].lower()}")
    from app.core.local_mode import is_local
    if is_local():
        assert named["kind"] == "experiment_result"
        assert named["actions"][0]["action"] == "experiment.run"
        assert named["actions"][0]["payload"]["variants"] == spec["variants"]
        assert not any(action.get("confirmation") for action in named["actions"])
        assert "credit" not in named["body_md"].lower()
    else:
        assert named["kind"] == "confirmation" and named["actions"][0]["confirmation"]["resulting_state"]["variants"] == spec["variants"]


@pytest.mark.parametrize("catalog", ["E08", "E09", "E39"])
def test_scoped_arm_search_confirmation_balance_caps_and_manual_failure(outreach_case, monkeypatch, catalog):
    from app.autopilot import outreach
    from app.db.models import Venture, VentureDocument
    from app.db.supymem_models import Experiment
    from app.experiments import autonomous, outreach_tests
    from app.signals.research import ScrapedText, SearchResults
    from core.clock import utcnow

    case, spec, calls = outreach_case, proposal(outreach_case, catalog), []
    original = deepcopy(case["icp"])
    class Provider:
        name = "firecrawl"

        async def search(self, query):
            calls.append(("search", query))
            arm = next(arm for arm in spec["variants"] if arm["params"]["queries"][0] == query or arm["params"]["queries"][1] == query)
            return SearchResults([{"url": f"https://{arm['key'].replace('_', '')}.example/contact", "title": arm["params"]["who"]}],
                provider_rows=1, rejected_shape=0, rejected_missing_url=0, usage={"firecrawl_self_hosted": True})

        async def scrape(self, url):
            calls.append(("scrape", url))
            arm = next(arm for arm in spec["variants"] if arm["key"].replace("_", "") in url)
            params = arm["params"]
            size = f"{params['min_size']} employees" if "min_size" in params else params.get("size", "")
            return ScrapedText(f"{params['who']} {params.get('city', '')} {size}. {case['icp']['who']}. Contact hello@{arm['key'].replace('_', '')}.example.", metadata={}, usage={"firecrawl_self_hosted": True})

    result = asyncio.run(outreach_tests._search(spec, Provider()))
    assert len([call for call in calls if call[0] == "search"]) == 4
    assert all(result["buckets"][arm["key"]] for arm in spec["variants"])
    assert result["actual_micro"] <= spec["search_ceiling_micro"] and case["icp"] == original
    if catalog == "E08":
        mismatched = deepcopy(spec)
        mismatched["variants"][0]["params"]["city"] = spec["variants"][1]["params"]["city"]
        rejected = asyncio.run(outreach_tests._search(mismatched, Provider()))
        assert not rejected["buckets"][spec["variants"][0]["key"]]
    card = outreach_tests.confirmation(spec)
    state = card["actions"][0]["confirmation"]["resulting_state"]
    consequences = " ".join(card["actions"][0]["confirmation"]["consequences"])
    assert state["variants"] == spec["variants"] and "10 first emails per arm" in consequences
    assert '15 per day' in consequences and 'consent' in consequences and ('credit' not in consequences.lower())
    assert all(arm["label"] in consequences for arm in spec["variants"])
    save_experiment(case, spec, {})
    with pytest.raises(ValueError, match="experiment_confirmation_changed"):
        asyncio.run(outreach_tests.run(spec, {**state, "variants": []}))
    first, second = contact(case, 90), contact(case, 91)
    assignments = {first: "arm_0", second: "arm_1"}
    with case["factory"]() as session:
        row = session.get(Experiment, spec["id"])
        row.context = json.dumps({**json.loads(row.context), "assignments": assignments})
        session.commit()
    left, right = draft(case, spec, first, "arm_0"), draft(case, spec, second, "arm_1")
    monkeypatch.setattr(outreach, "send_slots_used_today", lambda vid: 0)
    assert outreach_tests.send_block(case["vid"], left) is None
    outreach._save_draft(case["vid"], left | {"state": "delivery_unconfirmed"}, left["doc_id"])
    assert outreach_tests.send_block(case["vid"], left) == "experiment_arm_balance"
    assert outreach_tests.send_block(case["vid"], right) is None
    assert outreach_tests.send_block("missing_venture", right) == "experiment_not_running"
    with case["factory"]() as session:
        session.get(Venture, case["vid"]).stage = "paused"
        session.commit()
    assert outreach_tests.send_block(case["vid"], right) == "experiment_not_running"
    with case["factory"]() as session:
        session.get(Venture, case["vid"]).stage = "draft"
        session.commit()
    monkeypatch.setattr(outreach, "send_slots_used_today", lambda vid: 15)
    assert outreach_tests.send_block(case["vid"], right) == "experiment_daily_cap"
    monkeypatch.setattr(outreach, "send_slots_used_today", lambda vid: 0)
    with case["factory"]() as session:
        row = session.get(Experiment, spec["id"])
        context = json.loads(row.context)
        context["actual_micro"] = spec["ceiling_micro"]
        row.context = json.dumps(context)
        session.commit()
    from app.core.local_mode import is_local
    assert outreach_tests.send_block(case["vid"], right) == (None if is_local() else "experiment_credit_ceiling")
    if is_local():
        assert right["experiment_send_micro"] == 0
    with case["factory"]() as session:
        row = session.get(Experiment, spec["id"])
        context = json.loads(row.context)
        context["actual_micro"] = spec["ceiling_micro"] + 1
        row.context = json.dumps(context)
        session.commit()
    assert outreach_tests.send_block(case["vid"], right) == "experiment_credit_ceiling"
    pending = [{**left, "doc_id": f"left{index}"} for index in range(3)] + [{**right, "doc_id": f"right{index}"} for index in range(3)]
    assert [item["variant"] for item in outreach_tests.balanced_pending(pending)] == ["arm_0", "arm_1"] * 3
    failure = autonomous.failure_reply({**spec, "error": "Search did not finish safely."})
    manual = failure["actions"][0]
    assert manual["label"] == "Prepare drafts for me" and manual["action"] == "outreach.draft"
    with case["factory"]() as session:
        assert session.get(VentureDocument, left["doc_id"]).provenance_json


@pytest.mark.parametrize("catalog", ["E19", "E08", "E09", "E39"])
def test_e19_worker_waits_for_approval_then_observes_or_offers_manual(outreach_case, monkeypatch, catalog):
    from app.core.jobs import complete_job, lease
    from app.db.supymem_models import Experiment
    from app.experiments import autonomous, outreach_tests
    from core.clock import utcnow

    case, spec = outreach_case, proposal(outreach_case, catalog)
    people = [contact(case, index + 100) for index in range(4)]
    monkeypatch.setattr(autonomous, "_provider", lambda: SimpleNamespace(name="firecrawl"))
    if catalog != "E19":
        from app.credits import metered
        async def search(spec, provider):
            return {"buckets": {arm["key"]: [{"email": f"hello@lead{index + 100}.example", "source_url": f"https://lead{index + 100}.example/contact", "website": f"https://lead{index + 100}.example"}
                for index in range(arm_index * 2, arm_index * 2 + 2)] for arm_index, arm in enumerate(spec["variants"])}, "actual_micro": 6}
        async def call(callback, **kwargs):
            assert kwargs["purpose"] == "experiment.outreach_split"
            assert kwargs["estimate_micro"] == spec["search_ceiling_micro"] < spec["ceiling_micro"]
            assert kwargs["venture_id"] == case["vid"] and kwargs["workspace_id"] == case["wid"]
            result = await callback()
            await kwargs["before_meter_exit"](result, {})
            return result
        monkeypatch.setattr(outreach_tests, "_search", search)
        monkeypatch.setattr(metered, "call", call)
    monkeypatch.setattr(autonomous, "available", lambda vid: [spec])
    save_experiment(case, spec, {}, claimed=False)
    state = outreach_tests.confirmation(spec)["actions"][0]["confirmation"]["resulting_state"]
    response = asyncio.run(autonomous.run(case["vid"], state))
    assert response["kind"] == "experiment_running"
    def dispatch(now=None):
        with case["factory"]() as session:
            jobs = lease(session, "outreach-test-worker", kinds=[autonomous.KIND], now=now)
            session.commit()
        assert len(jobs) == 1
        job = jobs[0]
        payload = {**job.payload, "_job_id": job.id, "_worker_id": job.worker_id, "_job_attempt": job.attempts}
        asyncio.run(autonomous.run_job(payload))
        asyncio.run(autonomous.run_job(payload))
        with case["factory"]() as session:
            complete_job(session, job.id, worker_id=job.worker_id, attempt=job.attempts)
            session.commit()
        return payload
    dispatch()
    with case["factory"]() as session:
        context = json.loads(session.get(Experiment, spec["id"]).context)
    if case["consumer"]:
        assert context["error"] and context["outreach_state"] == "blocked"
        assert not context.get("assignments")
    else:
        assert context["prepared"] and context["outreach_state"] == "waiting_data"
        assert set(context["assignments"]) == set(people)
        assert list(context["assignments"].values()).count("arm_0") == 2
        future = utcnow() + timedelta(days=8)
        monkeypatch.setattr(outreach_tests, "utcnow", lambda: future)
        dispatch(future)
        with case["factory"]() as session:
            row = session.get(Experiment, spec["id"])
            context = json.loads(row.context)
            from app.core.local_mode import is_local
            if is_local():
                assert row.state == "running" and row.outcome is None
                result = context["observed_result"]
                assert result["outcome"] == "inconclusive" and result["response_window_open"]
                assert "first real send" in result["insight"]
            else:
                assert row.state == "concluded" and row.outcome == "inconclusive"
                result = context["result"]
            assert all(arm["sent"] == 0 for arm in result["by_variant"])
    from app.chat import experiments
    from app.experiments import ledger
    listed_spec = {**spec, "state": context["outreach_state"], "error": context.get("error"), "result": context.get("result")}
    monkeypatch.setattr(autonomous, "available", lambda vid: [listed_spec])
    listed = experiments.list_reply(case["vid"])
    assert listed["kind"] == "experiments"
    if case["consumer"]:
        assert any(action["label"] == "Prepare drafts for me" for action in listed["actions"])
    from app.core.local_mode import is_local
    expected_states = {"blocked", "waiting_data"} if is_local() else {"blocked", "concluded"}
    assert any(row["state"] in expected_states for row in ledger.ranking_inputs(case["vid"])["experiments"])


def test_outreach_runner_ranking_uses_email_volume_not_scan_confidence(outreach_case):
    from app.experiments import engine

    spec = proposal(outreach_case, "E08")
    risks = [{"question_code": "Q2", "risk": 4}]
    assert engine.days(spec) == 7
    assert engine.score(spec, risks, {"emails": 20}) == pytest.approx(5 * engine.score(spec, risks, {"emails": 0}))
    changed = {**spec, "variants": [{**arm, "label": arm["label"] + " updated"} for arm in spec["variants"]]}
    assert engine.input_fingerprint(changed) != engine.input_fingerprint(spec)
    assert engine.input_fingerprint({**changed, "runner": "scan"}) == engine.input_fingerprint({**spec, "runner": "scan"})
