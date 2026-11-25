"""Founder spend view (flag spend_view_v1): ledger sums, per-experiment caps, tenancy, flag off."""
from __future__ import annotations

import asyncio
import json
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from tests.company_os_harness import load_company_os_app
from tests.credit_fixtures import ensure_test_wallet
from tests.unit.credits.test_gate import local_meter_env as local_meter_env

DOMAINS = {"a": "Dental clinic demand scan", "b": "Family meal planning demand scan"}


@pytest.mark.parametrize("domain", ["Fleet maintenance", "Community gardening"])
def test_local_missing_experiment_returns_saved_experiment_next_step(local_meter_env, monkeypatch, domain):
    from fastapi import HTTPException
    from app.api import spend_api
    from app.credits import spend

    monkeypatch.setattr(spend_api, "_scope", lambda *args: "workspace")
    monkeypatch.setattr(spend_api, "role_of", lambda *args: None)

    def missing(*args, **kwargs):
        raise KeyError("private experiment")

    monkeypatch.setattr(spend, "set_cap", missing)
    with pytest.raises(HTTPException) as error:
        spend_api.set_experiment_cap(domain, "missing", {"cap_micro": 1000}, "owner")
    assert error.value.status_code == 404
    assert error.value.detail["error"]["message"] == "Open your venture's Experiments screen, choose a saved experiment, then set its spending limit again."


@pytest.mark.parametrize("domain", ["Fleet maintenance", "Community gardening"])
def test_local_settings_spend_is_monthly_across_ventures_without_contact_or_wallet_data(local_meter_env, monkeypatch, domain):
    from fastapi import FastAPI
    from app.api import spend_api
    from app.core.local_secrets import update_settings
    from app.credits import metered, spend

    update_settings("spend", {"monthly_spend_cap": "10"}, {})

    def no_scoped_or_wallet_reads(*args):
        pytest.fail("global local spending must not require a venture or read a wallet")

    monkeypatch.setattr(spend_api, "_scope", no_scoped_or_wallet_reads)
    monkeypatch.setattr(spend, "_rows", no_scoped_or_wallet_reads)
    monkeypatch.setattr(spend, "_reservations", no_scoped_or_wallet_reads)
    for venture_id, cost in ((domain, 125), (domain + " other", 250), (None, 375)):
        metered.call_sync(lambda: domain, purpose="research.search", estimate_micro=cost,
                          workspace_id="workspace", venture_id=venture_id)
    app = FastAPI()
    app.include_router(spend_api.router)
    with TestClient(app) as client:
        response = client.get("/local/spend")
    assert response.status_code == 200
    report = response.json()
    assert set(report) == {"month", "month_cost_usd_micro", "monthly_cap_usd_micro"}
    assert report["month_cost_usd_micro"] == 750
    assert report["monthly_cap_usd_micro"] == 10_000_000
    assert domain not in response.text
    assert "credit" not in response.text.lower()


@pytest.mark.parametrize("domain", ["Fleet maintenance", "Community gardening"])
def test_local_settings_spend_failure_has_next_step_and_is_unavailable_when_hosted(local_meter_env, monkeypatch, domain):
    from fastapi import FastAPI
    from app.api import spend_api
    from app.credits import local_meter, spend
    from app.credits.errors import CreditsUnavailable

    def unavailable():
        raise CreditsUnavailable(domain)

    monkeypatch.setattr(local_meter, "snapshot", unavailable)
    app = FastAPI()
    app.include_router(spend_api.router)
    with TestClient(app) as client:
        response = client.get("/local/spend")
        assert response.status_code == 503
        assert "Keys and connections" in response.text and "Refresh spending" in response.text
        assert domain not in response.text
        monkeypatch.setattr(spend_api, "is_local", lambda: False)
        monkeypatch.setattr(spend, "enabled", lambda: True)
        assert client.get("/local/spend").status_code == 404


@pytest.mark.parametrize("domain", ["Fleet maintenance", "Community gardening"])
def test_local_spend_api_uses_meter_without_wallet_or_policy(local_meter_env, monkeypatch, domain):
    from fastapi import FastAPI
    from app.api import spend_api
    from app.core.local_secrets import update_settings
    from app.credits import metered, spend

    update_settings("spend", {"monthly_spend_cap": "10"}, {})
    scopes = []

    def scope(venture_id, subject):
        scopes.append((venture_id, subject))
        return "workspace"

    def no_wallet(*args):
        pytest.fail("local spend API must not read wallet entries or holds")

    monkeypatch.setattr(spend_api, "_scope", scope)
    monkeypatch.setattr(spend, "_rows", no_wallet)
    monkeypatch.setattr(spend, "_reservations", no_wallet)
    monkeypatch.setattr(spend, "_experiments", lambda *args: [])
    for purpose, cost in (("chat.reply", 125), ("research.search", 250), ("content.writer", 375)):
        metered.call_sync(lambda: domain, purpose=purpose, estimate_micro=cost,
                          workspace_id="workspace", venture_id=domain)
    app = FastAPI()
    app.include_router(spend_api.router)
    with TestClient(app) as client:
        response = client.get(f"/ventures/{domain}/spend", headers={"X-Subject": "owner"})
    assert response.status_code == 200
    report = response.json()
    assert scopes == [(domain, "owner")]
    assert report["month_cost_usd_micro"] == 750
    assert report["monthly_cap_usd_micro"] == 10_000_000
    assert report["by_feature_usd_micro"] == {"chat": 125, "research": 250, "content": 375}
    assert "credit" not in response.text.lower()


@pytest.mark.parametrize("domain", ["Fleet maintenance", "Community gardening"])
def test_local_experiment_limit_counts_pending_spend_without_confirmation(local_meter_env, monkeypatch, domain):
    from types import SimpleNamespace
    from app.credits import metered, spend

    row = SimpleNamespace(id="experiment", state="running", hypothesis=domain,
        context=json.dumps({"run_id": "run", "spend_cap_micro": 1000}))
    monkeypatch.setattr(spend, "_owned", lambda *args: row)
    metered.call_sync(lambda: domain, purpose="experiment.research", workspace_id="workspace", venture_id=domain,
                      request_id="run:search", estimate_micro=500)

    def timeout():
        raise TimeoutError()

    with pytest.raises(TimeoutError):
        metered.call_sync(timeout, purpose="experiment.research", workspace_id="workspace", venture_id=domain,
                          request_id="run:writer", estimate_micro=300)
    assert spend.experiment_spent(None, "workspace", domain, row) == 800
    assert spend.experiment_spent(None, "other", domain, row) == 0
    blocked = spend.cap_block("workspace", domain, row.id, 201, domain)
    assert blocked["kind"] == "experiment_blocked" and blocked["actions"] == []
    assert "Ask to raise or remove" in blocked["body_md"]
    assert json.loads(row.context)["spend_paused"] is True


def _flag(on: bool) -> None:
    from app.credits.policy import invalidate, set_policy

    set_policy("feature_flags", {"spend_view_v1": on}, updated_by="test")
    invalidate()


def _spend(wallet, workspace_id, venture_id, micro, purpose, request_id):
    from app.credits.reserve import reserve, settle

    hold = reserve(account_id=wallet.id, amount_micro=micro, purpose=purpose, workspace_id=workspace_id,
                   venture_id=venture_id, request_id=request_id)
    settle(hold, actual_micro=micro)


def _founder(key: str, scale: int):
    from app.db.supymem_models import Experiment
    from app.state import db
    from core.clock import utcnow

    vid = f"ven_spend_{key}"
    wallet = ensure_test_wallet(vid)
    ws = "ws_wallet:" + vid
    with db()() as session:
        session.add(Experiment(id=f"exp_{key}", workspace_id=ws, venture_id=vid, kind="venture_test",
            hypothesis=DOMAINS[key], state="running", started_at=utcnow(), context=json.dumps(
                {"runner": "system", "run_id": f"exr_{key}", "spec": {"title": DOMAINS[key]}})))
        session.commit()
    for micro, purpose, request in ((200_000, "experiment.demand", f"exr_{key}"),
                                    (50_000, "experiment.approach_review", f"exr_{key}:approach"),
                                    (30_000, "research.deep", f"res_{key}"), (10_000, "venture.chat.assistant", f"chat_{key}"),
                                    (5_000, "outreach.draft_batch", f"out_{key}"), (7_000, "build.model", f"bld_{key}"),
                                    (1_000, "connections.status", f"con_{key}")):
        _spend(wallet, ws, vid, micro * scale, purpose, request)
    return vid, ws


@pytest.fixture()
def founders(credits_env):
    return _founder("a", 1), _founder("b", 3)


def test_view_sums_ledger_by_category_without_cross_founder_leak(founders):
    from app.credits import spend

    (va, wa), (vb, wb) = founders
    _flag(True)
    view = spend.view(wa, va)
    expected = {"experiment": 250_000, "research": 30_000, "chat": 10_000, "outreach": 5_000, "builds": 7_000, "other": 1_000}
    for period in ("week", "month"):
        assert view[period]["by_category"] == expected
        assert view[period]["total_micro"] == 303_000
    assert [(row["id"], row["title"], row["spent_micro"], row["cap_micro"], row["status"]) for row in view["experiments"]] == [
        ("exp_a", DOMAINS["a"], 250_000, None, "running")]
    other = spend.view(wb, vb)
    assert other["month"]["total_micro"] == 909_000 and [row["id"] for row in other["experiments"]] == ["exp_b"]
    assert spend.view(wa, vb)["month"]["total_micro"] == 0  # another founder's venture under my workspace reads nothing
    with pytest.raises(KeyError):
        spend.set_cap(wa, va, "exp_b", 1_000_000)

    main = load_company_os_app()
    with TestClient(main.create_app()) as client:
        own = client.get(f"/ventures/{va}/spend", headers={"X-Subject": "per_wallet:" + va})
        assert own.status_code == 200 and own.json()["month"]["by_category"] == expected
        assert client.get(f"/ventures/{vb}/spend", headers={"X-Subject": "per_wallet:" + va}).status_code == 404
        put = client.put(f"/ventures/{vb}/spend/experiments/exp_b/cap", json={"cap_micro": 1},
                         headers={"X-Subject": "per_wallet:" + va})
        assert put.status_code == 404
        put = client.put(f"/ventures/{va}/spend/experiments/exp_a/cap", json={"cap_micro": 0},
                         headers={"X-Subject": "per_wallet:" + va})
        assert put.status_code == 422
        put = client.put(f"/ventures/{va}/spend/experiments/exp_a/cap", json={"cap_micro": 400_000},
                         headers={"X-Subject": "per_wallet:" + va})
        assert put.status_code == 200 and put.json() == {"id": "exp_a", "cap_micro": 400_000, "spent_micro": 250_000,
                                                         "reserved_micro": 0, "remaining_micro": 150_000,
                                                         "status": "running"}
    assert spend.view(wb, vb)["experiments"][0]["cap_micro"] is None


@pytest.mark.parametrize("key", DOMAINS)
@pytest.mark.parametrize("stop_after_lease", [False, True], ids=["queued", "leased"])
def test_stop_fences_queued_worker_before_paid_admission(credits_env, monkeypatch, key, stop_after_lease):
    from types import SimpleNamespace

    from app import main
    from app.chat.conversations import bind_conversation, bind_job_conversation, create
    from app.core.jobs import FatalError, lease
    from app.credits import spend
    from app.db.supymem_models import Experiment
    from app.experiments import autonomous
    from app.state import db
    from app.tenancy import bind

    scale = 1 if key == "a" else 3
    venture_id, workspace_id = _founder(key, scale)
    experiment_id = f"exp_{key}"
    subject = "per_wallet:" + venture_id
    _flag(True)
    spec = {"id": experiment_id, "workspace_id": workspace_id, "venture_id": venture_id,
            "title": DOMAINS[key], "hypothesis": DOMAINS[key], "fingerprint": "fp",
            "ceiling_micro": 40_000 * scale, "scan": "demand", "provider": "test"}
    with db()() as session:
        row = session.get(Experiment, experiment_id)
        row.context = json.dumps({**json.loads(row.context), "spec": spec})
        session.commit()
    monkeypatch.setattr(autonomous, "available", lambda venture_id: [dict(spec)])
    monkeypatch.setattr(autonomous, "_provider", lambda: SimpleNamespace(name="test"))
    admissions = []

    async def intercept(function, **kwargs):
        admissions.append(kwargs)
        raise AssertionError("Stopped work reached paid admission; no provider was called.")

    monkeypatch.setattr(autonomous.metered, "call", intercept)
    with bind(workspace_id=workspace_id, venture_id=venture_id, actor=subject):
        conversation = create(venture_id, idempotency_key="stop-regression", title=DOMAINS[key])
        with bind_conversation(venture_id, conversation["id"]):
            reply = asyncio.run(autonomous.run(venture_id, {"experiment_id": experiment_id,
                "fingerprint": spec["fingerprint"], "ceiling_micro": spec["ceiling_micro"]}))
        assert reply["kind"] == "experiment_running"
        with TestClient(main.create_app()) as client:
            if not stop_after_lease:
                stopped = client.put(f"/ventures/{venture_id}/spend/experiments/{experiment_id}/cap",
                    json={"stop": True}, headers={"X-Subject": subject})
            with db()() as session:
                jobs = lease(session, "spend-stop-worker", kinds=[autonomous.KIND], n=1)
                session.commit()
            assert len(jobs) == 1
            job = jobs[0]
            assert job.id == reply["run_id"] and job.payload["_chat_conversation"]["conversation_id"] == conversation["id"]
            if stop_after_lease:
                stopped = client.put(f"/ventures/{venture_id}/spend/experiments/{experiment_id}/cap",
                    json={"stop": True}, headers={"X-Subject": subject})
            assert stopped.status_code == 200 and stopped.json()["status"] == "stopped"
        payload = {**job.payload, "_job_id": job.id, "_worker_id": job.worker_id, "_job_attempt": job.attempts}
        failure = None
        with bind_job_conversation(payload, venture_id=venture_id, workspace_id=workspace_id):
            try:
                asyncio.run(autonomous.run_job(payload))
            except FatalError as error:
                failure = str(error)
    assert admissions == []
    assert failure == "experiment_stopped"
    with db()() as session:
        context = json.loads(session.get(Experiment, experiment_id).context)
    assert context["spend_stopped"] and not context["worker_started"]
    assert spend.view(workspace_id, venture_id)["experiments"][0]["spent_micro"] == 250_000 * scale


@pytest.mark.parametrize("key", DOMAINS)
def test_lifetime_spend_survives_long_gap_retry(credits_env, monkeypatch, key):
    from app.credits import spend
    from app.db.supymem_models import Experiment
    from app.experiments import autonomous
    from app.state import db
    from core import clock

    scale = 1 if key == "a" else 3
    experiment_id = f"exp_{key}"
    old_start = clock.utcnow() - timedelta(days=400)
    with monkeypatch.context() as past:
        past.setattr(clock, "_clock", lambda: old_start)
        venture_id, workspace_id = _founder(key, scale)
    _flag(True)
    with db()() as session:
        row = session.get(Experiment, experiment_id)
        row.context = json.dumps({**json.loads(row.context), "claimed": True, "retryable": True})
        session.commit()
    spend.set_cap(workspace_id, venture_id, experiment_id, 300_000 * scale)
    spec = {"id": experiment_id, "workspace_id": workspace_id, "venture_id": venture_id,
            "title": DOMAINS[key], "hypothesis": DOMAINS[key], "fingerprint": "fp", "ceiling_micro": 40_000 * scale}
    monkeypatch.setattr(autonomous, "available", lambda venture_id: [dict(spec)])
    reply = asyncio.run(autonomous.run(venture_id, {"experiment_id": experiment_id,
        "fingerprint": "fp", "ceiling_micro": spec["ceiling_micro"]}))
    assert reply["kind"] == "experiment_running"
    with db()() as session:
        row = session.get(Experiment, experiment_id)
        assert row.started_at.replace(tzinfo=None) > old_start.replace(tzinfo=None) + timedelta(days=399)
        run_id = json.loads(row.context)["run_id"]
    wallet = ensure_test_wallet(venture_id)
    _spend(wallet, workspace_id, venture_id, 30_000 * scale, "experiment.demand", run_id + ":retry")
    saved = spend.set_cap(workspace_id, venture_id, experiment_id, 300_000 * scale)
    assert saved["spent_micro"] == 280_000 * scale
    assert saved["remaining_micro"] == 20_000 * scale
    with db()() as session:
        assert spend.experiment_spent(session, workspace_id, venture_id,
            session.get(Experiment, experiment_id)) == 280_000 * scale
    report = spend.view(workspace_id, venture_id)
    experiment = report["experiments"][0]
    assert experiment["spent_micro"] == 280_000 * scale
    assert experiment["spent_week_micro"] == experiment["spent_month_micro"] == 30_000 * scale
    assert report["week"]["total_micro"] == report["month"]["total_micro"] == 30_000 * scale
    assert spend.cap_block(workspace_id, venture_id, experiment_id, 40_000 * scale, DOMAINS[key])["kind"] == "confirmation"


@pytest.mark.parametrize("key", DOMAINS)
def test_held_reservations_reduce_remaining_without_becoming_spend(founders, key):
    from types import SimpleNamespace

    from app.credits import spend
    from app.credits.pools import create_pool
    from app.credits.reserve import protect_outcome, release, reserve, settle, sweep_expired
    from app.db.models import CreditAccount, CreditReservation
    from app.state import db
    from core.clock import utcnow

    venture_id, workspace_id = founders[0 if key == "a" else 1]
    other_venture, other_workspace = founders[1 if key == "a" else 0]
    scale = 1 if key == "a" else 3
    wallet = ensure_test_wallet(venture_id)
    _flag(True)
    holds = {}
    for suffix, amount in (("active", 50_000), ("unknown", 20_000), ("released", 25_000),
                          ("settled", 10_000), ("expired", 15_000)):
        holds[suffix] = reserve(account_id=wallet.id, amount_micro=amount * scale, purpose="experiment.demand",
            workspace_id=workspace_id, venture_id=venture_id, request_id=f"exr_{key}:{suffix}")
    protect_outcome(holds["unknown"])
    release(holds["released"])
    settle(holds["settled"], actual_micro=10_000 * scale)
    with db()() as session:
        row = create_pool(session, account=session.get(CreditAccount, wallet.id),
            run=SimpleNamespace(id=f"build_{key}", workspace_id=workspace_id, venture_id=venture_id,
                                runtime_version=1, reservation_id=None, closed_at=None),
            reservation_id=f"pool_{key}", amount_micro=100_000 * scale,
            expires_at=utcnow() + timedelta(minutes=15))
        pool_id = row.id
        for suffix in ("active", "unknown", "expired"):
            row = session.get(CreditReservation, holds[suffix].reservation_id)
            row.created_at = utcnow() - timedelta(days=400)
            if suffix != "active":
                row.expires_at = utcnow() - timedelta(days=1)
        row = session.get(CreditReservation, pool_id)
        row.remaining_micro = 40_000 * scale
        session.add(CreditReservation(id=f"pool_child_{key}", account_id=wallet.id, parent_id=row.id,
            amount_micro=60_000 * scale, status="open", purpose="build.model", workspace_id=workspace_id,
            venture_id=venture_id, request_id=f"build_{key}", created_at=utcnow(), expires_at=row.expires_at))
        session.commit()
    assert sweep_expired() == 1
    for scoped_workspace, scoped_venture in ((workspace_id, other_venture), (other_workspace, venture_id)):
        reserve(account_id=wallet.id, amount_micro=90_000 * scale, purpose="experiment.demand",
            workspace_id=scoped_workspace, venture_id=scoped_venture, request_id=f"exr_{key}:foreign")
    experiment_id = f"exp_{key}"
    saved = spend.set_cap(workspace_id, venture_id, experiment_id, 330_000 * scale)
    assert saved["spent_micro"] == 260_000 * scale
    assert saved["reserved_micro"] == 70_000 * scale
    assert saved["remaining_micro"] == 0 and saved["status"] == "at_cap"
    report = spend.view(workspace_id, venture_id)
    assert report["reserved"]["total_micro"] == 170_000 * scale
    assert report["reserved"]["by_category"]["experiment"] == 70_000 * scale
    assert report["reserved"]["by_category"]["builds"] == 100_000 * scale
    assert report["week"]["total_micro"] == report["month"]["total_micro"] == 313_000 * scale
    assert report["experiments"][0]["reserved_micro"] == 70_000 * scale
    assert spend.view(other_workspace, venture_id)["reserved"]["total_micro"] == 0
    stopped = spend.set_cap(workspace_id, venture_id, experiment_id, None, stop=True)
    assert stopped["status"] == "stopped" and stopped["reserved_micro"] == 70_000 * scale
    blocked = spend.cap_block(workspace_id, venture_id, experiment_id, 40_000 * scale, DOMAINS[key])
    assert blocked["kind"] == "experiment_blocked" and "Work already in progress" in blocked["body_md"]
    spend.set_cap(workspace_id, venture_id, experiment_id, 360_000 * scale)
    assert spend.cap_block(workspace_id, venture_id, experiment_id, 40_000 * scale, DOMAINS[key])["kind"] == "confirmation"
    release(holds["active"])
    settle(holds["unknown"], actual_micro=12_000 * scale)
    saved = spend.set_cap(workspace_id, venture_id, experiment_id, 360_000 * scale)
    assert saved["spent_micro"] == 272_000 * scale
    assert saved["reserved_micro"] == 0 and saved["remaining_micro"] == 88_000 * scale
    assert spend.cap_block(workspace_id, venture_id, experiment_id, 40_000 * scale, DOMAINS[key]) is None
    assert spend.view(workspace_id, venture_id)["reserved"]["total_micro"] == 100_000 * scale


@pytest.mark.parametrize("key", DOMAINS)
def test_spend_defaults_on_without_bypassing_access_checks(founders, key):
    from app.credits import spend
    from app.credits.policy import set_policy

    venture_id, _ = founders[0 if key == "a" else 1]
    set_policy("feature_flags", {}, "test")
    assert spend.enabled()
    main = load_company_os_app()
    with TestClient(main.create_app()) as client:
        assert client.get(f"/ventures/{venture_id}/spend", headers={"X-Subject": "per_wallet:" + venture_id}).status_code == 200
        assert client.get(f"/ventures/{venture_id}/spend", headers={"X-Subject": "per_wallet:unknown"}).status_code == 404


@pytest.mark.parametrize("key", DOMAINS)
def test_cap_pauses_run_silently_then_requested_limit_change_or_stop(founders, monkeypatch, key):
    from app.chat import experiments
    from app.credits import spend
    from app.experiments import autonomous

    va, wa = founders[0 if key == "a" else 1]
    experiment_id = "exp_" + key
    _flag(True)
    spend.set_cap(wa, va, experiment_id, 300_000)
    spec = {"id": experiment_id, "workspace_id": wa, "venture_id": va, "title": DOMAINS[key], "hypothesis": DOMAINS[key],
            "fingerprint": "fp", "ceiling_micro": 100_000}
    monkeypatch.setattr(autonomous, "available", lambda venture_id: [dict(spec)])
    before = spend.view(wa, va)
    card = asyncio.run(autonomous.run(va, {"experiment_id": experiment_id, "fingerprint": "fp", "ceiling_micro": 100_000}))
    assert card["kind"] == "confirmation"
    assert card["body_md"] == f"Your saved experiment limit stops {DOMAINS[key]}. Change the limit or stop this experiment, then choose what you want to run next."
    assert spend.view(wa, va)["week"] == before["week"]
    assert spend.view(wa, va)["reserved"] == before["reserved"]
    raise_action, stop_action = card["actions"]
    assert raise_action["action"] == stop_action["action"] == "experiment.spend_cap"
    assert raise_action["confirmation"]["resulting_state"] == {"experiment_id": experiment_id, "cap_micro": 1_000_000}
    assert raise_action["label"] == "Change experiment limit" and stop_action["label"] == "Stop it"
    assert "$" not in json.dumps([card["body_md"], raise_action["label"], *[
        [action["confirmation"]["interpretation"], action["confirmation"]["consequences"]] for action in card["actions"]]])
    assert spend.view(wa, va)["experiments"][0]["status"] == "paused_at_cap"

    raised = asyncio.run(experiments.execute(va, "experiment.spend_cap", raise_action["confirmation"]["resulting_state"], None))
    assert raised["body_md"] == "Your experiment setting is saved. Run it again when you are ready."
    assert spend.cap_block(wa, va, experiment_id, 100_000, DOMAINS[key]) is None
    assert spend.view(wa, va)["experiments"][0]["status"] == "running"

    stopped = asyncio.run(experiments.execute(va, "experiment.spend_cap", stop_action["confirmation"]["resulting_state"], None))
    assert stopped["body_md"].startswith("You stopped this experiment.")
    blocked = asyncio.run(autonomous.run(va, {"experiment_id": experiment_id, "fingerprint": "fp", "ceiling_micro": 100_000}))
    assert blocked["kind"] == "experiment_blocked" and spend.view(wa, va)["experiments"][0]["status"] == "stopped"


@pytest.mark.parametrize("key", DOMAINS)
def test_flag_off_is_unchanged(founders, monkeypatch, key):
    from app.api.exploration_read import autopilot_view
    from app.chat import experiments
    from app.credits import spend
    from app.db.supymem_models import Experiment
    from app.experiments import autonomous
    from app.state import db

    va, wa = founders[0 if key == "a" else 1]
    experiment_id = f"exp_{key}"
    _flag(False)
    assert not spend.enabled()
    baseline = json.dumps(autopilot_view(va, wa)).encode()
    _flag(True)
    spend.set_cap(wa, va, experiment_id, 1)
    on = autopilot_view(va, wa)
    spent = 250_000 if key == "a" else 750_000
    assert on["experiments"][0]["spend"] == {"spent_micro": spent, "cap_micro": 1, "status": "at_cap"}
    with db()() as session:
        row = session.get(Experiment, experiment_id)
        context = json.loads(row.context)
        context.update(claimed=True, job_id=f"job_{key}", spend_stopped=True, spend_paused=True)
        row.context = json.dumps(context)
        session.commit()
        saved_context = row.context
    _flag(False)
    monkeypatch.setattr(spend, "view", lambda *args: pytest.fail("Flag-off reads must not query spend"))
    off = autopilot_view(va, wa)
    assert "\"spend\":" not in json.dumps(off)
    assert json.dumps(off).encode() == baseline
    assert spend.cap_block(wa, va, experiment_id, 100_000, DOMAINS[key]) is None
    context = {"run_id": f"exr_{key}", "spend_run_ids": [f"exr_old_{key}"]}
    baseline_context = json.dumps(context).encode()
    spend.record_run(context, f"exr_new_{key}")
    assert json.dumps(context).encode() == baseline_context
    for payload in ({"experiment_id": experiment_id, "stop": True},
                    {"experiment_id": experiment_id, "cap_micro": 1_000_000}, {}):
        response = asyncio.run(experiments.execute(va, "experiment.spend_cap", payload, None))
        assert json.dumps(response).encode() == json.dumps(experiments._reply(
            "Changing an experiment cap here is not available. Nothing was changed.")).encode()
    spec = {"id": experiment_id, "workspace_id": wa, "venture_id": va, "title": DOMAINS[key],
            "hypothesis": DOMAINS[key], "fingerprint": "fp", "ceiling_micro": 100_000}
    monkeypatch.setattr(autonomous, "available", lambda venture_id: [dict(spec)])
    payload = {"experiment_id": experiment_id, "fingerprint": "fp", "ceiling_micro": 100_000}
    response = asyncio.run(autonomous.run(va, payload))
    assert json.dumps(response).encode() == json.dumps({**autonomous._reply(
        f"Your {DOMAINS[key].lower()} is queued. Check your experiments for progress; I will post the result here.", kind="experiment_running"),
        "run_id": f"job_{key}"}).encode()
    spec.update(runner="outreach_split", variants=[], catalog_key="E24")
    response = asyncio.run(autonomous.run(va, {**payload, "variants": []}))
    assert json.dumps(response).encode() == json.dumps(autonomous._reply(
        "Your experiment is already running. Review the saved drafts through your usual approval flow.",
        kind="experiment_running")).encode()
    main = load_company_os_app()
    with TestClient(main.create_app()) as client:
        not_found = client.get(f"/ventures/{va}/spend")
        assert not_found.status_code == 404
        for headers in ({}, {"X-Subject": "per_wallet:" + va}, {"X-Subject": "per_wallet:unknown"}):
            assert client.get(f"/ventures/{va}/spend", headers=headers).content == not_found.content
            for content in (b'{"cap_micro": 1000000}', b'{"stop": true}', b'{}', b'[]', b'{', b''):
                response = client.put(f"/ventures/{va}/spend/experiments/{experiment_id}/cap", content=content,
                                      headers={**headers, "Content-Type": "application/json"})
                assert response.status_code == 404
                assert response.content == not_found.content
    with db()() as session:
        assert session.get(Experiment, experiment_id).context == saved_context
