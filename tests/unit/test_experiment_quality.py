import asyncio
import json
from copy import deepcopy
from datetime import timedelta
from functools import partial

import pytest
from tests.company_os_harness import local_owner_clients

from tests.unit.test_autonomous_experiments import market, venture
from tests.unit.test_page_experiments import connected_coding_tool


@pytest.mark.parametrize("sample", [False, True], ids=["owner", "sample"])
def test_owner_observations_do_not_become_samples_in_log_delivery_mode(market, monkeypatch, sample):
    from app.db.models import Workspace
    from app.experiments import ledger
    from app.state import db
    from core.clock import utcnow

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("EMAIL_PROVIDER", "log")

    def mark_workspace(as_sample):
        with db()() as session:
            workspace = session.get(Workspace, market[2])
            workspace.settings_json = json.dumps({**json.loads(workspace.settings_json or "{}"), "sample": as_sample})
            session.commit()

    mark_workspace(sample)
    audience = market[3]["icp"]["who"]
    identifier, = ledger.create_from_tests(market[1], "chat", [{"hypothesis": f"{audience} will try your offer"}])
    report = f"I interviewed five {audience} and recorded their answers in anonymized notes today."
    provenance = {"source": "Your anonymized notes", "method": "Interviews", "observed_at": utcnow().isoformat()}
    record = partial(ledger.record_result, identifier, market[1], True, report, "supported",
        "Your reported interviews need another test.", sample_size=5, provenance=provenance)
    before = ledger.get(identifier, market[1])
    mark_workspace(not sample)
    with pytest.raises(KeyError, match="experiment"):
        record()
    assert ledger.get(identifier, market[1]) == before
    mark_workspace(sample)
    assert record()["state"] == "concluded"
    saved = ledger.get(identifier, market[1])
    assert saved["result_text"] == ("Sample result, not market evidence: " + report if sample else report)
    assert saved["ran"] is (not sample)
    assert saved["outcome"] == (None if sample else "supported")
    assert saved.get("sample_size") == (None if sample else 5)
    assert saved.get("provenance") == (None if sample else provenance)
    assert bool(saved.get("sample")) == sample
    assert saved["evidence_type"] == ("sample" if sample else "founder_reported")
    assert saved["evidence_label"] == ("Sample, not market evidence" if sample else "reported by you")


@pytest.mark.parametrize("saved_spec", [None, [], "invalid"])
def test_page_test_rejects_nonobject_saved_spec_without_changing_data(market, saved_spec):
    from app.db.supymem_models import Experiment
    from app.experiments import ledger, page_tests
    from app.state import db

    context = json.dumps({"page_test": True, "spec": saved_spec})
    with db()() as session:
        row = Experiment(id="exp_invalid_spec", venture_id=market[1], workspace_id=market[2],
            kind=ledger.KIND, hypothesis=market[3]["icp"]["problem"], state="proposed", context=context)
        session.add(row)
        session.commit()
        with pytest.raises(ValueError, match="page_test_not_found"):
            page_tests._row(session, market[1], row.id)
        assert row.context == context and row.state == "proposed" and row.started_at is None


@pytest.fixture
def quality_clock(market, monkeypatch):
    from app.core.local_mode import is_local
    from app.credits import policy
    from app.experiments import service
    from core import clock

    now = [clock.utcnow()]
    flags = {"experiments_quality_v1": True, "experiments_autopilot_v1": True, "exploration_v1": True}
    original = policy.get_policy
    monkeypatch.setattr(policy, "get_policy", lambda key, *args: flags if key == "feature_flags" else original(key, *args))
    monkeypatch.setattr(service, "get_policy", policy.get_policy)
    monkeypatch.setattr(clock, "_clock", lambda: now[0])
    if is_local():
        from app.core.local_secrets import update_settings
        from secrets import token_urlsafe
        update_settings("smtp", {"host": "smtp.example.test", "user": "owner@example.test",
            "sender_name": "Your founder", "postal_address": "18 Oak Road, Test City, US"},
            {"password": token_urlsafe(24)})
        update_settings("capabilities", {"send_email": True}, {})
    return now, flags


def outreach_spec(market):
    from app.experiments import autonomous, ledger
    from app.state import db

    _client, vid, wid, view, *_rest = market
    with db()() as session:
        direction = ledger.current_direction(session, vid)
    spec = {"id": "exp_quality_outreach", "venture_id": vid, "workspace_id": wid, "direction": direction,
        "runner": "outreach_split", "scan": "outreach_split", "catalog_key": "E19", "variable": "angle",
        "icp": deepcopy(view["icp"]), "hypothesis": "Your audience replies to a concrete question.",
        "title": "Reply comparison", "method": "Compare recorded human replies.",
        "variants": [{"key": "arm_0", "label": "Current opening", "params": {"playbook": "value_first"}},
            {"key": "arm_1", "label": "Concrete question", "params": {"playbook": "worst_week"}}],
        "metric": "reply_rate_ratio", "min_sample": 20, "min_sample_per_arm": 10, "max_duration_days": 7,
        "response_window_days": 7, "checkpoint": {"after_sample": 10, "wait_days": 2, "max_response_rate": .05},
        "success_threshold": {"op": ">=", "value": 1.5, "min_events": 20, "metric": "qualified_reply_rate_ratio"},
        "failure_threshold": {"op": "<", "value": 1.5, "min_events": 20, "metric": "qualified_reply_rate_ratio"},
        "ceiling_micro": 2000, "email_micro": 100, "search_ceiling_micro": 0, "requires": ["outreach", "gmail"],
        "runner_params": {"playbooks": ["value_first", "worst_week"], "pages_per_arm": 0},
        "send_plan": {"daily_limit": 10}, "founder_minutes": 0, "question": "Q4", "state": "proposed"}
    spec["fingerprint"] = autonomous._fingerprint(spec)
    return spec


@pytest.mark.parametrize("spend_enabled", [False, True])
@pytest.mark.parametrize("quality_enabled", [False, True])
def test_outreach_run_preserves_spend_and_quality_independently(
        market, quality_clock, monkeypatch, spend_enabled, quality_enabled):
    from app.credits import spend
    from app.db.supymem_models import Experiment
    from app.experiments import autonomous, ledger
    from app.state import db

    _now, flags = quality_clock
    flags.update(spend_view_v1=spend_enabled, experiments_quality_v1=quality_enabled)
    spec = outreach_spec(market)
    context = {"spec": spec, "runner": "system", "run_id": "exr_previous", "spend_run_ids": ["exr_initial"]}
    with db()() as session:
        session.add(Experiment(id=spec["id"], venture_id=market[1], workspace_id=market[2], kind=ledger.KIND,
            hypothesis=spec["hypothesis"], state="proposed", context=json.dumps(context)))
        session.commit()
    monkeypatch.setattr(autonomous, "available", lambda *_args: [deepcopy(spec)])
    payload = {"experiment_id": spec["id"], "fingerprint": spec["fingerprint"],
        "ceiling_micro": spec["ceiling_micro"], "variants": spec["variants"]}
    spend.set_cap(market[2], market[1], spec["id"], spec["ceiling_micro"] - 1)
    from app.core.local_mode import is_local
    if spend_enabled or is_local():
        assert asyncio.run(autonomous.run(market[1], payload))["kind"] == ("experiment_blocked" if is_local() else "confirmation")
        with db()() as session:
            row = session.get(Experiment, spec["id"])
            saved = json.loads(row.context)
            assert row.state == "proposed" and row.started_at is None
            assert saved["run_id"] == "exr_previous" and "job_id" not in saved
            assert saved["spend_run_ids"] == ["exr_initial"] and "checkpoint_approved" not in saved
        spend.set_cap(market[2], market[1], spec["id"], spec["ceiling_micro"])
    reply = asyncio.run(autonomous.run(market[1], payload))
    assert reply["kind"] == "experiment_running"
    with db()() as session:
        row = session.get(Experiment, spec["id"])
        saved = json.loads(row.context)
        assert row.state == "running" and row.started_at is not None
        assert saved["claimed"] is True and saved["job_id"] == reply["run_id"]
        assert saved["run_id"] not in {"exr_initial", "exr_previous"}
        assert saved["spend_run_ids"] == (sorted(["exr_initial", "exr_previous", saved["run_id"]])
            if spend_enabled or is_local() else ["exr_initial"])
        assert saved.get("checkpoint_approved") is (True if quality_enabled or is_local() else None)
        saved_context = row.context
    assert asyncio.run(autonomous.run(market[1], payload))["kind"] == "experiment_running"
    with db()() as session:
        assert session.get(Experiment, spec["id"]).context == saved_context


def test_disabling_quality_keeps_persisted_parent_checkpoint_pause(market, quality_clock, monkeypatch):
    from app.db.supymem_models import Experiment
    from app.db.models import Venture
    from app.experiments import ledger, outreach_tests
    from app.state import db
    now, flags = quality_clock
    spec = outreach_spec(market)
    with db()() as session:
        session.add(Experiment(id=spec["id"], venture_id=market[1], workspace_id=market[2],
            kind=ledger.KIND, hypothesis=spec["hypothesis"], state="running", started_at=now[0],
            context=json.dumps({"spec": spec, "checkpoint_paused": True})))
        session.commit()
    draft = {"experiment_id": spec["id"], "variant": "arm_0"}
    assert outreach_tests.send_block(market[1], draft) == "experiment_checkpoint_review_required"
    flags["experiments_quality_v1"] = False
    assert outreach_tests.send_block(market[1], draft) == "experiment_checkpoint_review_required"
    with db()() as session:
        assert json.loads(session.get(Experiment, spec["id"]).context)["checkpoint_paused"] is True
        session.add(Venture(id="ven_checkpoint_other", workspace_id=market[2], name="Separate scope",
            raw_idea="A separate project", stage="draft", created_at=now[0], updated_at=now[0]))
        session.commit()
    monkeypatch.setattr("app.autopilot.outreach.send_slots_used_today", lambda *_: 0)
    monkeypatch.setattr("app.autopilot.guard.get_settings", lambda *_: {"send_max_per_day": 10})
    assert outreach_tests.send_block("ven_checkpoint_other", draft) == "experiment_not_running"


def add_touch(session, market, identifier, experiment_id, sent_at, *, variant="arm_0", simulated=0):
    from app.db.models import GtmContact, GtmTouch

    _client, vid, wid, *_rest = market
    session.add(GtmContact(id="contact_" + identifier, workspace_id=wid, email=identifier + "@example.com",
        source=f"venture:{vid}:public", created_at=sent_at, stage_changed_at=sent_at))
    session.flush()
    session.add(GtmTouch(id="touch_" + identifier, contact_id="contact_" + identifier, workspace_id=wid,
        venture_id=vid, direction="out", channel="email", simulated=simulated, sent_at=sent_at, created_at=sent_at,
        message_id="real_" + identifier, honesty_json=json.dumps({"experiment_id": experiment_id, "variant": variant, "day": 1})))


def test_quality_launch_requires_published_page_and_real_distribution(market, quality_clock, monkeypatch, connected_coding_tool):
    from sqlalchemy import select
    from app.core.jobs import complete_job, lease
    from app.db.supymem_models import Experiment
    from app.experiments import autonomous, belief_autopilot, channel_tests, page_tests
    from app.state import db
    from app.core.local_mode import is_local

    _client, vid, _wid, _view, landing, _provider, calls, *_rest = market
    now, flags = quality_clock
    landing.update(enabled=True, published_site={"url": "https://your-site.example/"})
    public = [None]
    monkeypatch.setattr(page_tests, "published_url", lambda *_args: public[0])
    if is_local():
        from app.db.session import serialize_writes
        for runner, payload in [
            (page_tests, {"kind": "headlines", "variants": [
                {"key": "first", "label": "First", "headline": "A first opening"},
                {"key": "second", "label": "Second", "headline": "A second opening"}]}),
            (channel_tests, {"kind": "channels"}),
        ]:
            spec = runner._spec(vid, payload)
            runner.configure(vid, {**payload, "fingerprint": spec["fingerprint"]})
            with db()() as session:
                row = session.get(Experiment, spec["id"])
                before = row.context
            with db()() as writer:
                serialize_writes(writer, key="page-observation:" + vid)
                result = runner.observe(spec)
                assert result["outcome"] == "inconclusive" and not result["actual_micro"]
                assert writer.get(Experiment, spec["id"]).context == before
            assert not calls
        return
    for index, (runner, payload) in enumerate([
        (page_tests, {"kind": "headlines", "variants": [
            {"key": "first", "label": "First", "headline": "A first opening"},
            {"key": "second", "label": "Second", "headline": "A second opening"}]}),
        (channel_tests, {"kind": "channels"}),
        (page_tests, {"kind": "survey"}),
        (channel_tests, {"kind": "referral"}),
    ]):
        public[0] = None
        spec = runner._spec(vid, payload)
        configured_at = now[0]
        reply = runner.configure(vid, {**payload, "fingerprint": spec["fingerprint"]})
        assert "ready to launch" in reply["body_md"]
        blocked = asyncio.run(autonomous.run(vid, {"experiment_id": spec["id"], "fingerprint": spec["fingerprint"], "ceiling_micro": spec["ceiling_micro"]}))
        assert blocked["kind"] != "experiment_running" and "ready to launch" in blocked["body_md"]
        with db()() as session:
            row = session.get(Experiment, spec["id"])
            assert row.state == "proposed" and row.started_at is None
            assert json.loads(row.context)["quality_job_id"]
            add_touch(session, market, f"simulated_{index}", row.id, configured_at, simulated=1)
            session.commit()
        now[0] += timedelta(days=12)
        assert runner.observe(spec)["launch_state"] == "ready to launch"
        with db()() as session:
            jobs = lease(session, "quality-test-worker", kinds=["experiment.beliefs.tick"], n=10)
            session.commit()
        assert jobs
        for job in jobs:
            asyncio.run(belief_autopilot.run_job({**job.payload, "_job_id": job.id, "_worker_id": job.worker_id, "_job_attempt": job.attempts}))
            with db()() as session:
                complete_job(session, job.id, worker_id=job.worker_id, attempt=job.attempts)
                session.commit()
        public[0] = landing["published_site"]["url"]
        assert runner.observe(spec)["launch_state"] == "ready to launch"
        ready = runner.readout(vid, {"experiment_id": spec["id"]})
        assert ready["actions"] and "ready to launch" in ready["body_md"]
        now[0] += timedelta(days=1)
        exposed_at = now[0]
        with db()() as session:
            add_touch(session, market, f"real_{index}", spec["id"], exposed_at)
            session.commit()
        runner.observe(spec)
        with db()() as session:
            row = session.get(Experiment, spec["id"])
            context = json.loads(row.context)
            assert row.state == "running" and context["first_exposure_at"] == exposed_at.isoformat()
            assert context["prepared_at"] == exposed_at.isoformat()
            assert context["distribution_cohort"] == [f"contact_real_{index}"]
            row.state = "aborted"
            for candidate in session.scalars(select(Experiment).where(Experiment.venture_id == vid,
                    Experiment.state == "proposed")):
                candidate.state = "aborted"
            session.commit()
    flags.pop("experiments_quality_v1")
    spec = page_tests._spec(vid, {"kind": "pricing", "variants": [{"key": "price", "label": "Price", "price": "$10 per month"}]})
    page_tests.configure(vid, {"kind": "pricing", "variants": spec["variants"], "fingerprint": spec["fingerprint"]})
    with db()() as session:
        assert session.get(Experiment, spec["id"]).state == "running"
    assert not calls


def test_quality_verdict_waits_for_each_contacts_response_window(market, quality_clock):
    from app.db.supymem_models import Experiment
    from app.experiments import ledger, outreach_tests
    from app.experiments.service import run_evaluate
    from app.state import db

    now, flags = quality_clock
    spec = outreach_spec(market)
    start = now[0]
    with db()() as session:
        session.add(Experiment(id=spec["id"], venture_id=market[1], workspace_id=market[2], kind=ledger.KIND,
            hypothesis=spec["hypothesis"], state="running", started_at=start,
            context=json.dumps({"runner": "system", "spec": spec})))
        for index in range(20):
            add_touch(session, market, f"maturity_{index}", spec["id"], start + timedelta(days=3 if index == 19 else 0), variant=f"arm_{index % 2}")
        session.commit()
    now[0] = start + timedelta(days=3)
    flags["experiments_quality_v1"] = False
    from app.core.local_mode import is_local
    assert outreach_tests.observe(spec)["outcome"] == ("inconclusive" if is_local() else "refuted")
    flags["experiments_quality_v1"] = True
    early = outreach_tests.observe(spec)
    assert early["outcome"] == "inconclusive" and early["readable_at"] == (start + timedelta(days=10)).isoformat()
    assert "too early to tell" in early["insight"] and early["readable_at"] in early["insight"]
    run_evaluate({"venture_id": market[1], "workspace_id": market[2], "now": now[0]})
    with db()() as session:
        assert session.get(Experiment, spec["id"]).outcome == "inconclusive"
    now[0] = start + timedelta(days=10) - timedelta(seconds=1)
    assert outreach_tests.observe(spec)["outcome"] == "inconclusive"
    now[0] += timedelta(seconds=1)
    mature = outreach_tests.observe(spec)
    assert mature["outcome"] == "refuted" and mature["mature_contacts"] == 20
    assert not mature["response_window_open"]


def test_quality_evidence_matches_runner_and_manual_reports_require_provenance(market, quality_clock, monkeypatch):
    from app.db.supymem_models import Experiment
    from app.experiments import autonomous, belief_autopilot, beliefs, exploration, ledger
    from app.state import db

    now, flags = quality_clock
    spec = {**outreach_spec(market), "scan": "demand", "runner": "scan", "metric": "observed_complaints",
        "hypothesis": "Your public source sample contains observed complaints.", "variants": [],
        "success_threshold": {"op": ">=", "value": 3, "min_events": 3},
        "failure_threshold": {"op": "<", "value": 1, "min_events": 3}}
    monkeypatch.setattr(autonomous, "select_runnable", lambda *_args: [deepcopy(spec)])
    strong = f"People in {market[3]['icp']['who']} will pay for your product."
    round_ = exploration.create_round(market[1], kind="market_gap", options=[
        {"statement": strong + f" Variant {index}.", "audience": market[3]["icp"]["who"], "problem": market[3]["icp"]["problem"]}
        for index in range(3)])
    prepared = exploration._prepare_runs(market[1], round_["id"], round_["options"][0]["id"])
    measured, = prepared
    assert measured["hypothesis"] == spec["hypothesis"] and measured["requested_hypothesis"].startswith(strong)
    assert measured["evidence_binding"]["evidence_type"] == "desk_research"
    assert "observed_complaints" in measured["evidence_binding"]["metrics"]
    bound = next(item for item in beliefs.load(market[1])["beliefs"] if item["id"] == measured["belief_ids"][0])
    assert bound["evidence_bindings"][0]["measured_hypothesis"] == spec["hypothesis"]
    with db()() as session:
        experiment = session.get(Experiment, measured["id"])
        context = json.loads(experiment.context)
        context["result"] = {"outcome": "supported", "measured": {"events": 5, "observed_complaints": 5}}
        experiment.state, experiment.context = "concluded", json.dumps(context)
        saved = beliefs.row(session, market[1])
        context = json.loads(saved.context)
        context["plan"] = {"state": "active", "ceiling_micro": 10000, "reserved_micro": 0,
            "deadline": (now[0] + timedelta(days=7)).isoformat(), "tests": [{"experiment_id": measured["id"],
                "belief_id": bound["id"], "state": "running", "deadline": (now[0] + timedelta(days=7)).isoformat(),
                "success_threshold": measured["success_threshold"], "failure_threshold": measured["failure_threshold"]}]}
        saved.context = json.dumps(context)
        session.commit()
    reviewed = belief_autopilot._read_results(market[1], now[0])
    belief = next(item for item in reviewed["beliefs"] if item["id"] == bound["id"])
    assert belief["status"] == "weak" and belief["evidence"][-1]["evidence_type"] == "desk_research"
    monkeypatch.setattr(autonomous, "select_runnable", lambda *_args: [outreach_spec(market)])
    pair = exploration.create_round(market[1], kind="value_proposition", options=[
        {"statement": f"You could try message {index} for {market[3]['icp']['who']}.",
            "audience": market[3]["icp"]["who"], "problem": market[3]["icp"]["problem"]} for index in range(3)])
    compared, = exploration._prepare_runs(market[1], pair["id"], None,
        option_ids=[option["id"] for option in pair["options"][:2]])
    for option in pair["options"][:2]:
        belief = next(item for item in beliefs.load(market[1])["beliefs"] if item["id"] == option["belief_id"])
        assert belief["evidence_bindings"][-1]["metrics"] == ["positive_reply_rate"]
        assert belief["evidence_bindings"][-1]["experiment_id"] == compared["id"]
    identifier, = ledger.create_from_tests(market[1], "chat", [{"hypothesis": strong}])
    from app.core.local_mode import is_local
    if is_local():
        reported = ledger.record_result(identifier, market[1], True, "I interviewed people. They said yes.",
            "supported", "A signal", complete=False)
        assert reported["state"] == "running" and reported["evidence_type"] == "founder_reported"
        assert "not independently verified" in reported["learning"]
    else:
        with pytest.raises(ValueError, match="sample size"):
            ledger.record_result(identifier, market[1], True, "People said yes", "supported", "A signal")
    for sample in (True, 0, -1, float("nan")):
        with pytest.raises(ValueError, match="positive sample size"):
            ledger.record_result(identifier, market[1], True, "I interviewed three people.", "supported", "A signal", sample_size=sample,
                provenance={"source": "Your call notes", "method": "Interview", "observed_at": now[0].isoformat()})
    report = {"result_text": "Three answered yes in the interviews.", "sample_size": 3,
        "provenance": {"source": "Your call notes", "method": "Interview", "observed_at": now[0].isoformat()}}
    saved = ledger.record_result(identifier, market[1], True, json.dumps(report), "supported", "A reported signal")
    assert saved["state"] == "concluded" and saved["outcome"] == "supported"
    assert saved["sample_size"] == 3 and saved["evidence_label"] == "reported by you"
    assert saved["provenance"] == report["provenance"]
    flags["experiments_quality_v1"] = False
    legacy, = ledger.create_from_tests(market[1], "chat", [{"hypothesis": "A separate " + strong}])
    saved = ledger.record_result(legacy, market[1], True, "I interviewed people. They said yes", "supported", "A signal")
    if is_local():
        assert saved["state"] == "running" and saved["outcome"] is None and saved["needs_evidence"]
    else:
        assert saved["outcome"] == "supported"


def test_quality_checkpoint_pauses_and_requires_reviewed_version_within_budget(market, quality_clock, monkeypatch):
    from app.autopilot import outreach
    from app.db.supymem_models import Experiment
    from app.experiments import autonomous, belief_autopilot, belief_planner, beliefs, ledger, outreach_tests
    from app.state import db

    now, flags = quality_clock
    spec = outreach_spec(market)
    start = now[0]
    approvals, launches = [], []
    async def skip_narrow(*_args):
        return None
    async def launch(*_args):
        launches.append(True)
    queued = {"beliefs": [], "plan": {"id": "quality_queued", "state": "active",
        "tests": [{"experiment_id": spec["id"], "state": "queued"}]}}
    with monkeypatch.context() as patch:
        patch.setattr(beliefs, "load", lambda *_args: queued)
        patch.setattr(belief_autopilot, "_read_results", lambda *_args: queued)
        patch.setattr(belief_autopilot, "_narrow", skip_narrow)
        patch.setattr(belief_autopilot, "_plan_followups", lambda *_args: queued)
        patch.setattr(belief_autopilot, "daily_note", lambda *_args, **_kwargs: None)
        patch.setattr(belief_planner, "available", lambda *_args: [spec])
        patch.setattr(autonomous, "run", launch)
        import app.chat.milestones as milestones
        patch.setattr(milestones, "post_milestone", lambda *_args, **kwargs: approvals.append(kwargs))
        asyncio.run(belief_autopilot.tick(market[1]))
    from app.core.local_mode import is_local
    assert not launches
    if is_local():
        assert not approvals
    else:
        assert len(approvals) == 1 and approvals[0]["kind"] == "confirmation"
        assert approvals[0]["actions"][0]["action"] == "experiment.run"
        assert "holding further sends" in approvals[0]["actions"][0]["confirmation"]["consequences"][-1]
    held = [{"state": "sent", "experiment_id": spec["id"], "variant": f"arm_{index % 2}", "experiment_send_micro": 100}
        for index in range(10)]
    monkeypatch.setattr(outreach, "drafts", lambda _venture, *states:
        deepcopy([draft for draft in held if not states or draft["state"] in states]))
    with db()() as session:
        session.add(Experiment(id=spec["id"], venture_id=market[1], workspace_id=market[2], kind=ledger.KIND,
            hypothesis=spec["hypothesis"], state="running", started_at=start,
            context=json.dumps({"runner": "system", "spec": spec, "checkpoint_approved": True, "belief_plan_id": "quality_plan",
                "assignments": {"contact_future": "arm_0"}})))
        for index in range(10):
            add_touch(session, market, f"checkpoint_{index}", spec["id"], start, variant=f"arm_{index % 2}")
        saved = beliefs.row(session, market[1], create=True)
        saved.context = json.dumps({"beliefs": [{"id": "quality_belief", "statement": spec["hypothesis"], "status": "testing"}],
            "plan": {"id": "quality_plan", "state": "active", "deadline": (start + timedelta(days=21)).isoformat(),
                "ceiling_micro": 2000, "reserved_micro": 2000, "max_emails_per_day": 10,
                "tests": [{"belief_id": "quality_belief", "experiment_id": spec["id"], "state": "running", "ceiling_micro": 2000,
                    "reserved_micro": 2000, "deadline": (start + timedelta(days=21)).isoformat()}]}})
        session.commit()
    assert outreach_tests.review_checkpoint(spec, outreach_tests.observe(spec)) is None
    now[0] += timedelta(days=2)
    result = outreach_tests.observe(spec)
    assert result["outcome"] == "inconclusive" and result["response_window_open"]
    assert outreach_tests.send_block(market[1], {"experiment_id": spec["id"], "variant": "arm_0", "contact_id": "contact_future"}) == "experiment_checkpoint_waiting"
    flags["experiments_quality_v1"] = False
    from app.core.local_mode import is_local
    if not is_local():
        assert outreach_tests.review_checkpoint(spec, result) is None
    flags["experiments_quality_v1"] = True
    card = outreach_tests.review_checkpoint(spec, result)
    assert card["kind"] == "confirmation" and len(card["actions"]) == 1
    with db()() as session:
        parent = session.get(Experiment, spec["id"])
        context = json.loads(parent.context)
        alternative = context["checkpoint_alternative"]
        assert context["checkpoint_paused"] and context["spec"]["variants"] == spec["variants"]
        assert alternative["arm_version"] == 2 and alternative["ceiling_micro"] == 1000
        assert session.get(Experiment, alternative["id"]).state == "proposed"
    assert outreach_tests.send_block(market[1], {"experiment_id": spec["id"], "variant": "arm_0"}) == "experiment_checkpoint_review_required"
    assert outreach_tests.review_checkpoint(spec, result) == card
    held.append({**held[0], "experiment_send_micro": 100})
    with pytest.raises(ValueError, match="remaining budget changed"):
        asyncio.run(outreach_tests.run(alternative, {"fingerprint": alternative["fingerprint"],
            "ceiling_micro": alternative["ceiling_micro"], "variants": alternative["variants"]}))
    held.pop()
    reply = asyncio.run(outreach_tests.run(alternative, {"fingerprint": alternative["fingerprint"],
        "ceiling_micro": alternative["ceiling_micro"], "variants": alternative["variants"]}))
    assert reply["kind"] == "experiment_running"
    with db()() as session:
        assert session.get(Experiment, spec["id"]).state == "aborted"
        assert session.get(Experiment, alternative["id"]).state == "running"
    plan = beliefs.load(market[1])["plan"]
    assert plan["reserved_micro"] == 2000 and sum(test["reserved_micro"] for test in plan["tests"]) == 2000
    assert len(plan["tests"]) == 2
