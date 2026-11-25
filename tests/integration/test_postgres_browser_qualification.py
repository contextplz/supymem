"""Signed source custody on migrated owned PG; no native/provider I/O or acceptance claims."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta
import json
from threading import Barrier, Event

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.integration.test_postgres_auth_security import pg_database as pg_database  # noqa: F401
from tests.integration.test_postgres_journey_jobs import journey_db as journey_db  # noqa: F401
from tests.unit.product.test_device_browser_ai import configure


def seeded_trial(d, monkeypatch, *, task_id="form_valid", claim=True):
    from app.admin.authn import CallerContext
    from app.build import browser_qualification as qualification, device_budget, device_orders as orders
    from app.db.models import CreditAccount, VentureDocument
    from app.db.supymem_models import DesktopDevice
    from core.browser_qualification import OwnedFixtureGrant
    from core.browser_qualification_catalog import CATALOG_DIGEST
    from core.canonical import digest

    configure(monkeypatch)
    device_id = "dev_" + d["actor"].rsplit("_", 1)[1]
    monkeypatch.setenv(qualification.ENABLED_ENV, "1")
    monkeypatch.setenv(qualification.ALLOWLIST_ENV, d["actor"])
    now = orders.utcnow()
    with d["factory"]() as session:
        session.add(DesktopDevice(id=device_id, person_id=d["actor"], name="Synthetic source test Mac", platform="macos", app_version="0.1.84"))
        session.add(CreditAccount(id="cra_" + device_id, workspace_id=d["wid"], venture_id=d["vid"], scope="venture",
            granted_micro=20_000_000, spent_micro=0, reserved_micro=0, status="active", created_at=now, updated_at=now))
        session.commit()
    caller = CallerContext(d["actor"], "", "user", "/test", "POST", 9999999999, "synthetic", device_id=device_id)
    trial = qualification.start_trial(d["vid"], caller, catalog_digest=CATALOG_DIGEST,
        seed_digest=digest({"synthetic_form": "source"}), expires_at=int((orders.utcnow() + timedelta(hours=1)).timestamp()))
    order = qualification.prepare_task(d["vid"], caller, trial["trial_id"], task_id)
    assert order["state"] == "pending" and not order.get("claimed_at")
    assert qualification.prepare_task(d["vid"], caller, trial["trial_id"], task_id) == order
    grant = OwnedFixtureGrant.from_dict(order["browser_qualification_grant"])
    args = (d["vid"], caller, order["id"])
    if not claim:
        return qualification, orders, caller, order, grant, args
    with d["factory"]() as session:
        account = device_budget.lock_account(session, d["wid"], d["vid"])
        orders._lock_venture(session, d["wid"], d["vid"])
        row = session.get(VentureDocument, order["id"], with_for_update=True)
        body = json.loads(row.provenance_json)
        qualification.allocate_order(session, account, body, grant)
        session.commit()
    for action in ("approve", "claim"):
        orders.transition(*args, grant.review_digest, action)
    return qualification, orders, caller, order, grant, args


def test_controller_prepares_exact_all_36_pending_reviews_without_execution_or_seed_reset(journey_db, monkeypatch):
    from app.db.models import CreditAccount, VentureDocument
    from core.browser_qualification_catalog import CATALOG, fixture_goal
    from core.browser_qualification import OwnedFixtureGrant, QualificationTrialBinding
    d = journey_db
    svc, orders, caller, first, grant, args = seeded_trial(d, monkeypatch, claim=False)
    orders.transition(*args, first["review_digest"], "cancel")
    for task in CATALOG:
        if task.task_id == "form_valid":
            continue
        order = svc.prepare_task(d["vid"], caller, grant.trial_id, task.task_id)
        assert order["state"] == "pending" and not order.get("claimed_at") and not order.get("invocations")
        with d["factory"]() as session:
            trial = json.loads(session.get(VentureDocument, grant.trial_id).provenance_json)
            snapshot = trial["task_states"][task.task_id]["initial"]
            assert order["manifest"]["instruction"] == fixture_goal(snapshot)
            assert order["manifest"]["general_browser_verification"]["max_steps"] == task.max_steps
        if task.task_id == "l01_research_synthesis":
            binding = QualificationTrialBinding.from_dict(order["browser_qualification_trial_binding"])
            assert not hasattr(binding, "origin") and "browser_qualification_grant" not in order
            assert order["manifest"]["general_browser_verification"]["start_url"] == "https://pypi.org/"
            assert order["manifest"]["actions"] == ["browser.read"]
        else:
            OwnedFixtureGrant.from_dict(order["browser_qualification_grant"])
        assert orders._digest(order) == order["review_digest"]
        orders.transition(d["vid"], caller, order["id"], order["review_digest"], "cancel")
    with d["factory"]() as session:
        trial = json.loads(session.get(VentureDocument, grant.trial_id).provenance_json)
        assert len(trial["task_states"]) == len(trial["task_seeds"]) == len(trial["task_orders"]) == 36
        account = session.scalar(select(CreditAccount).where(CreditAccount.venture_id == d["vid"]))
        assert account.spent_micro == account.reserved_micro == 0
    with pytest.raises(ValueError, match="browser_qualification_task_already_prepared"):
        svc.prepare_task(d["vid"], caller, grant.trial_id, "form_valid")


def source_effect(d, grant, args, *, request_id="source_submit_once", invocation_id="source_effect", effect_digest="a" * 64):
    """Trusted controller journal fixture; this helper never runs a model/native action."""
    from app.build import device_invocations as calls
    from core.browser_qualification import request_binding_digest
    rule = next(rule for rule in grant.rules if rule.method == "POST")
    with d["factory"]() as session:
        row, body, _ = calls._locked(session, *args)
        parent_id = invocation_id + "_parent"
        body["browser_ai_latest_invocation_id"] = parent_id
        body.setdefault("invocations", {})[parent_id] = {"id": parent_id, "state": "completed", "phase": "actor",
            "source_kind": "browser_cuai_text_v1", "profile_digest": grant.profile_digest, "quote_micro": 0}
        body["invocations"][invocation_id] = {"id": invocation_id, "state": "completed", "source_kind": "browser_cuai_actor_effect_v1",
            "no_provider_io": True, "quote_micro": 0, "parent_invocation_id": parent_id, "profile_digest": grant.profile_digest,
            "effect": {"state": "admitted", "digest": effect_digest, "qualification": {"grant_digest": grant.grant_digest,
                "rule_id": rule.rule_id, "request_id": request_id, "request_digest": request_binding_digest(grant, rule, request_id)}}}
        row.provenance_json = json.dumps(body)
        session.commit()
        return {"X-Supymem-Qualification-Request": request_id, "X-Supymem-Qualification-Invocation": invocation_id,
            "X-Supymem-Qualification-Effect": effect_digest, "X-Supymem-Qualification-Execution": body["execution_id"]}


def test_signed_trial_read_is_current_device_owned_and_default_off(journey_db, monkeypatch):
    from app.api.browser_qualification_api import router
    from app.db.supymem_models import DesktopDevice
    from core.assertion import sign_assertion
    d = journey_db
    svc, _, caller, _, grant, _ = seeded_trial(d, monkeypatch)
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    path = f"/ventures/{d['vid']}/browser-qualification/{grant.trial_id}"
    secret = "synthetic-browser-qualification-signed-source-32bytes"
    monkeypatch.setenv("COS_INTERNAL_ASSERTION_SECRET", secret)

    def headers(actor=d["actor"], device=caller.device_id):
        return {"X-COS-Assertion": sign_assertion(secret, sub=actor, email="", role="user", path=path, method="GET", device_id=device)}

    assert client.get(path).status_code == 401
    visible = client.get(path, headers=headers())
    assert visible.status_code == 200 and visible.json()["ceiling_micro_usd"] == 10_000_000
    assert client.get(path, headers=headers(actor="usr_foreign")).status_code == 404
    assert client.get(path, headers=headers(device="dev_foreign")).status_code == 409
    with d["factory"]() as session:
        session.get(DesktopDevice, caller.device_id).revoked_at = datetime.utcnow()
        session.commit()
    assert client.get(path, headers=headers()).status_code == 409
    monkeypatch.delenv(svc.ENABLED_ENV)
    assert client.get(path, headers=headers()).status_code == 404


def test_durable_attempt_reservations_survive_unknown_restart_and_contention_without_credit_charge(journey_db, monkeypatch):
    from app.build import device_invocations as calls
    from app.db.models import CreditAccount, CreditLedgerEntry, VentureDocument
    from core.canonical import digest
    d = journey_db
    svc, orders, caller, order, grant, args = seeded_trial(d, monkeypatch)

    def reserve(identifier, amount):
        with d["factory"]() as session:
            row, body, account = calls._locked(session, *args)
            # Trusted controller journal fixture only. No provider request is dispatched.
            body.setdefault("invocations", {})[identifier] = {"id": identifier, "source_kind": "browser_cuai_text_v1",
                "state": "calling", "phase": "actor", "profile_digest": grant.profile_digest,
                "request_digest": digest(identifier), "quote_micro": amount}
            admitted = svc.reserve_attempt(session, account, body, grant, invocation_id=identifier)
            row.provenance_json = json.dumps(body)
            session.commit()
            return admitted

    assert reserve("unknown_one", 300_000)
    first = svc.status(d["vid"], caller, grant.trial_id)
    assert first["protected_unknown_micro_usd"] == 300_000 and first["allocated_remaining_micro_usd"] == 200_000
    with pytest.raises(ValueError, match="browser_qualification_usd_limit"):
        reserve("would_exceed", 300_000)
    barrier = Barrier(2)

    def competing(identifier):
        barrier.wait(timeout=5)
        try:
            return reserve(identifier, 200_000)
        except ValueError as exc:
            return str(exc)

    with ThreadPoolExecutor(max_workers=2) as workers:
        result = list(workers.map(competing, ["parallel_one", "parallel_two"]))
    assert result.count(True) == 1 and result.count("browser_qualification_usd_limit") == 1
    restarted = svc.status(d["vid"], caller, grant.trial_id)  # Fresh database session, no local budget object.
    assert restarted["protected_unknown_micro_usd"] == 500_000 and restarted["attempted_calls"] == 2
    with d["factory"]() as session:
        row, body, account = calls._locked(session, *args)
        assert svc.settle_attempt(session, account, body, grant, invocation_id="unknown_one") is False
        hold_before = (account.spent_micro, account.reserved_micro)
        body["invocations"]["unknown_one"].update(state="completed", provider_actual_micro=100_000,
            usage={"model": "gpt-6-sol", "input_tokens": 100, "output_tokens": 10})
        assert svc.settle_attempt(session, account, body, grant, invocation_id="unknown_one") is True
        assert svc.settle_attempt(session, account, body, grant, invocation_id="unknown_one") is False
        assert (account.spent_micro, account.reserved_micro) == hold_before
        assert session.scalars(select(CreditLedgerEntry).where(CreditLedgerEntry.account_id == account.id)).all() == []
        row.provenance_json = json.dumps(body)
        session.commit()
    after = svc.status(d["vid"], caller, grant.trial_id)
    assert after["known_micro_usd"] == 100_000 and after["protected_unknown_micro_usd"] == 200_000
    assert after["allocated_remaining_micro_usd"] == 200_000
    # The $10 aggregate also reserves prospective order allocations, not just one order.
    with d["factory"]() as session:
        account = session.scalar(select(CreditAccount).where(CreditAccount.venture_id == d["vid"]))
        trial_row = session.get(VentureDocument, grant.trial_id)
        trial = json.loads(trial_row.provenance_json)
        trial["allocations"]["synthetic_other_reviewed_orders"] = {"ceiling_micro_usd": 9_500_000, "remaining_micro_usd": 9_500_000}
        trial_row.provenance_json = json.dumps(trial)
        session.commit()
    assert svc.status(d["vid"], caller, grant.trial_id)["remaining_micro_usd"] == 0
    from app.build import device_budget
    from core.ids import new_id
    with pytest.raises(ValueError, match="browser_qualification_usd_limit"), d["factory"]() as session:
        account = device_budget.lock_account(session, d["wid"], d["vid"])
        orders._lock_venture(session, d["wid"], d["vid"])
        original = json.loads(session.get(VentureDocument, grant.order_id).provenance_json)
        pending = dict(original, id=new_id("dwo"), state="pending")
        binding = replace(grant.trial_binding(), order_id=pending["id"])
        pending["browser_qualification_scope"] = svc.scope_policy(binding)
        pending["review_digest"] = orders._digest(pending)
        binding = replace(binding, review_digest=pending["review_digest"])
        now = orders.utcnow()
        session.add(VentureDocument(id=pending["id"], venture_id=d["vid"], kind=orders.KIND,
            title="Synthetic pending allocation boundary", provenance_json=json.dumps(pending), created_at=now, updated_at=now))
        session.flush()
        changed_seed = replace(binding, seed_digest="f" * 64)
        altered = dict(pending, browser_qualification_scope=svc.scope_policy(changed_seed))
        altered["review_digest"] = orders._digest(altered)
        changed_seed = replace(changed_seed, review_digest=altered["review_digest"])
        with pytest.raises(ValueError, match="browser_qualification_task_seed_changed"):
            svc.seal_trial_binding(session, account, altered, changed_seed)
        svc.seal_trial_binding(session, account, pending, binding)
        svc.allocate_order(session, account, pending, binding)
    assert svc.status(d["vid"], caller, grant.trial_id)["remaining_micro_usd"] == 0


def test_fixture_submit_requires_current_one_use_durable_effect_and_payment_has_no_mutation(journey_db, monkeypatch):
    from app.api.browser_qualification_api import router
    from app.db.models import VentureDocument
    d = journey_db
    svc, _, caller, _, grant, args = seeded_trial(d, monkeypatch)
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    path = grant.rules[0].path
    assert client.get(path).status_code == 200
    fields = {field.name: field.value for field in grant.rules[1].fields}
    with d["factory"]() as session:
        saved_trial = json.loads(session.get(VentureDocument, grant.trial_id).provenance_json)
        completed_rules = {value["rule_id"] for value in saved_trial["requests"].values() if value["state"] == "committed"}
        assert [value["source_sequence"] for value in saved_trial["requests"].values()] == [1]
        assert grant.rules[0].rule_id in completed_rules
        from core.browser_qualification import OwnedFixtureGrant
        saved_grant = OwnedFixtureGrant.from_dict(saved_trial["grants"][grant.order_id])
        assert saved_grant.request_rule(url=grant.origin + path, method="POST", submitted_fields=tuple(fields.items()), completed_rules=completed_rules).rule_id == grant.rules[1].rule_id
    assert client.post(path, data=fields).status_code == 409
    request_id, invocation_id, effect_digest = "source_submit_once", "source_effect", "a" * 64
    headers = source_effect(d, grant, args, request_id=request_id, invocation_id=invocation_id, effect_digest=effect_digest)
    submitted = client.post(path, data=fields, headers=headers, follow_redirects=False)
    assert submitted.status_code == 303 and submitted.headers["location"] == path + "/done", submitted.text
    assert client.post(path, data=fields, headers=headers).status_code == 409
    receipt = svc.request_status(d["vid"], caller, grant.trial_id, request_id)
    assert receipt["state"] == "committed" and receipt["mutated"] and receipt["status_code"] == 303
    assert receipt["server_response_sent"] is True
    assert receipt["source_sequence"] == 2
    assert receipt["effect_digest"] == effect_digest
    done = client.get(path + "/done")
    assert done.status_code == 200 and b"Received" in done.content
    with d["factory"]() as session:
        trial = json.loads(session.get(VentureDocument, grant.trial_id).provenance_json)
        assert sum(value["mutated"] for value in trial["requests"].values()) == 1
        assert sorted(value["source_sequence"] for value in trial["requests"].values()) == [1, 2, 3]
    refusal = replace(grant, task_id="pay_wall", rules=(grant.rules[0],), allowed_operations=("navigate", "extract"))
    with pytest.raises(ValueError):
        refusal.request_rule(url=refusal.origin + path, method="POST", submitted_fields=tuple(fields.items()), completed_rules=(grant.rules[0].rule_id,))


def test_signed_effect_retirement_between_request_admission_and_handler_preserves_unknown(journey_db, monkeypatch):
    from app.api.browser_qualification_api import router
    from app.api.device_orders_api import router as device_router
    from app.db.models import VentureDocument
    from core.assertion import sign_assertion
    d = journey_db
    svc, _, caller, _, grant, args = seeded_trial(d, monkeypatch)
    app = FastAPI()
    app.include_router(router)
    app.include_router(device_router)
    client = TestClient(app)
    path = grant.rules[0].path
    assert client.get(path).status_code == 200
    headers = source_effect(d, grant, args)
    fields = {field.name: field.value for field in next(rule for rule in grant.rules if rule.method == "POST").fields}
    admitted, resume = Event(), Event()
    original_lock = svc.device_budget.lock_account
    calls = [0]

    def pause_second_lock(*arguments, **options):
        calls[0] += 1
        if calls[0] == 2:
            admitted.set()
            assert resume.wait(timeout=10)
        return original_lock(*arguments, **options)

    monkeypatch.setattr(svc.device_budget, "lock_account", pause_second_lock)
    secret = 's' * 48
    monkeypatch.setenv("COS_INTERNAL_ASSERTION_SECRET", secret)
    with ThreadPoolExecutor(max_workers=1) as worker:
        posted = worker.submit(client.post, path, data=fields, headers=headers, follow_redirects=False)
        assert admitted.wait(timeout=10)
        receipt_path = f"/ventures/{d['vid']}/device-work-orders/{grant.order_id}/effects/receipt"
        token = sign_assertion(secret, sub=d["actor"], email="", role="user", path=receipt_path, method="POST", device_id=caller.device_id)
        retired = client.post(receipt_path, headers={"X-COS-Assertion": token}, json={
            "invocation_id": headers["X-Supymem-Qualification-Invocation"], "effect_digest": headers["X-Supymem-Qualification-Effect"],
            "outcome": "uncertain"})
        assert retired.status_code == 200, retired.text
        resume.set()
        result = posted.result(timeout=10)
    assert result.status_code == 409 and result.json()["detail"]["code"] == "browser_qualification_effect_retired"
    receipt = svc.request_status(d["vid"], caller, grant.trial_id, headers["X-Supymem-Qualification-Request"])
    assert receipt["state"] == "unknown" and receipt["mutated"] is False and receipt["server_response_sent"] is False
    assert client.post(path, data=fields, headers=headers).status_code == 409
    with d["factory"]() as session:
        trial = json.loads(session.get(VentureDocument, grant.trial_id).provenance_json)
        assert trial["task_states"]["form_valid"]["current"]["state"]["posts"] == []


def test_source_commit_order_includes_known_503_reads_before_recovered_csv(journey_db, monkeypatch):
    from app.api.browser_qualification_api import router
    d = journey_db
    svc, _, caller, _, grant, _ = seeded_trial(d, monkeypatch, task_id="l06_recover_from_error_page")
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    assert client.get(grant.rules[0].path).status_code == 200
    rule = next(rule for rule in grant.rules if rule.path.endswith("/reports/q-download"))
    for sequence, status in enumerate((503, 503, 200), 2):
        response = client.get(rule.path)
        assert response.status_code == status
        receipt = svc.request_status(d["vid"], caller, grant.trial_id, response.headers["X-Supymem-Qualification-Request"])
        assert receipt["source_sequence"] == sequence and receipt["server_response_sent"] is True
        assert receipt["state"] == ("failed_read" if status == 503 else "committed") and receipt["mutated"] is False


def test_redirect_read_never_turns_delayed_server_completion_into_later_post_authority(journey_db, monkeypatch):
    from app.api.browser_qualification_api import router
    d = journey_db
    svc, _, caller, _, grant, args = seeded_trial(d, monkeypatch, task_id="wizard")
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    first_post = next(rule for rule in grant.rules if rule.method == "POST")
    next_post = next(rule for rule in grant.rules if rule.method == "POST" and rule.rule_id != first_post.rule_id)
    assert client.get(grant.rules[0].path).status_code == 200
    headers = source_effect(d, grant, args)
    original_complete = svc.record_response_delivery
    delayed = []

    def delayed_first_response(nonce, request_id):
        if request_id == headers["X-Supymem-Qualification-Request"]:
            delayed.append((nonce, request_id))
            return
        return original_complete(nonce, request_id)

    monkeypatch.setattr(svc, "record_response_delivery", delayed_first_response)
    first = client.post(first_post.path, data={field.name: field.value for field in first_post.fields}, headers=headers, follow_redirects=False)
    assert first.status_code == 200 and delayed
    passive = client.get(next_post.document_path)
    assert passive.status_code == 200
    view = svc.order_status(d["vid"], caller, grant.trial_id, grant.order_id)
    assert first_post.rule_id not in view["completed_rules"] and view["rules"][first_post.rule_id]["protected"]
    assert client.post(next_post.path, data={field.name: field.value for field in next_post.fields}).status_code == 409
    receipt = svc.request_status(d["vid"], caller, grant.trial_id, headers["X-Supymem-Qualification-Request"])
    assert receipt["state"] == "committed" and receipt["mutated"] is True and receipt["server_response_sent"] is False
    original_complete(*delayed[0])
    assert first_post.rule_id in svc.order_status(d["vid"], caller, grant.trial_id, grant.order_id)["completed_rules"]
