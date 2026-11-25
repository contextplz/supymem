import json
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from tests.unit.product.test_device_orders import orders as orders


def claimed_order(orders):
    service, caller, manifest = orders
    manifest.duration_seconds = 300
    order = service.prepare("v_one", caller, manifest)
    args = ("v_one", caller, order["id"], order["review_digest"])
    service.transition(*args, "approve")
    return service, caller, service.transition(*args, "claim")


def test_claim_heartbeat_wait_resume_complete(orders, monkeypatch):
    from app.build import device_invocations, device_stop
    from app.db.models import CreditAccount, CreditReservation

    service, caller, claimed = claimed_order(orders)
    receipt = service.DeviceHeartbeat(execution_id=claimed["execution_id"], step=2, percent=10,
                                      last_action="observing")
    live = service.heartbeat("v_one", caller, claimed["id"], receipt)
    assert live["progress"] == {"step": 2, "percent": 10, "last_action": "observing"}
    cutoff = datetime.fromisoformat(live["lease_expires_at"]) + timedelta(seconds=1)
    monkeypatch.setattr(service, "utcnow", lambda: cutoff)
    assert service.sweep_device_leases(cutoff) == 1
    assert service.sweep_device_leases(cutoff) == 0
    assert service.retrieve("v_one", caller, claimed["id"])["state"] == "waiting_for_device"
    with service.db()() as session:
        assert session.get(CreditAccount, "cra_device_test").reserved_micro == 1000
        assert session.get(CreditReservation, claimed["reservation_id"]).status == "reconciling"
    resumed = service.heartbeat("v_one", caller, claimed["id"], receipt)
    assert resumed["state"] == "claimed" and resumed["execution_id"] == claimed["execution_id"]
    device_invocations.check_current("v_one", caller, claimed["id"])
    stopped = device_stop.record("v_one", caller, claimed["id"], execution_id=claimed["execution_id"],
                                 evidence_digest="a" * 64, processes_stopped=True)
    assert stopped["state"] == "stopped"
    with service.db()() as session:
        assert session.get(CreditAccount, "cra_device_test").reserved_micro == 0


@pytest.mark.parametrize("title", ["Stock reports for purchasing teams", "Garden logs for home growers"])
@pytest.mark.parametrize("renew_first", [False, True])
def test_heartbeat_and_sweeper_share_order_lock_and_recheck_expiry(orders, monkeypatch, title, renew_first):
    from app.db import session as sessions

    service, caller, manifest = orders
    manifest.title = title
    service, caller, claimed = claimed_order(orders)
    cutoff = datetime.fromisoformat(claimed["lease_expires_at"]) + timedelta(seconds=1)
    monkeypatch.setattr(service, "utcnow", lambda: cutoff)
    reached_lock, release_lock = threading.Event(), threading.Event()
    serialize = sessions.serialize_writes
    keys = []

    def serialize_sweep(session, *, key=None):
        keys.append((threading.current_thread().name, key))
        if threading.current_thread().name.startswith("lease-sweep"):
            reached_lock.set()
            assert release_lock.wait(timeout=60)
        serialize(session, key=key)

    monkeypatch.setattr(sessions, "serialize_writes", serialize_sweep)
    receipt = service.DeviceHeartbeat(execution_id=claimed["execution_id"])
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="lease-sweep") as pool:
        future = pool.submit(service.sweep_device_leases, cutoff)
        try:
            assert reached_lock.wait(timeout=60)
            if renew_first:
                renewed = service.heartbeat("v_one", caller, claimed["id"], receipt)
            release_lock.set()
            assert future.result(timeout=60) == (0 if renew_first else 1)
            if not renew_first:
                renewed = service.heartbeat("v_one", caller, claimed["id"], receipt)
        finally:
            release_lock.set()
    lease_key = "device-lease:" + claimed["id"]
    assert any(name.startswith("lease-sweep") and key == lease_key for name, key in keys)
    assert (threading.current_thread().name, lease_key) in keys
    saved = service.retrieve("v_one", caller, claimed["id"])
    assert saved["state"] == "claimed" and not saved.get("lease_reconciliation")
    assert saved["lease_expires_at"] == renewed["lease_expires_at"]
    assert datetime.fromisoformat(saved["lease_expires_at"]) == cutoff + timedelta(seconds=90)


def test_old_client_keeps_long_lease_and_max_age_settles_once(orders, monkeypatch):
    from app.db.models import CreditAccount, CreditReservation

    service, caller, claimed = claimed_order(orders)
    start = datetime.fromisoformat(claimed["claimed_at"])
    assert datetime.fromisoformat(claimed["lease_expires_at"]) - start == timedelta(hours=2)
    assert service.sweep_device_leases(start + timedelta(minutes=90)) == 0
    assert service.retrieve("v_one", caller, claimed["id"])["state"] == "claimed"
    monkeypatch.setenv("DEVICE_ORDER_HOLD_MAX_AGE_SECONDS", "10800")
    cutoff = start + timedelta(hours=3)
    monkeypatch.setattr(service, "utcnow", lambda: cutoff)
    assert service.sweep_device_leases(cutoff) == 1
    assert service.sweep_device_leases(cutoff) == 0
    saved = service.retrieve("v_one", caller, claimed["id"])
    assert saved["state"] == "cancelled" and saved["lease_reconciliation"]["process_cleanup_proven"] is False
    with service.db()() as session:
        assert session.get(CreditAccount, "cra_device_test").reserved_micro == 0
        assert session.get(CreditReservation, claimed["reservation_id"]).settled_micro == 0
    with pytest.raises(ValueError):
        service.heartbeat("v_one", caller, claimed["id"], service.DeviceHeartbeat(execution_id=claimed["execution_id"]))


def test_heartbeat_binding_progress_and_ambiguous_resume(orders):
    from app.db.models import VentureDocument

    service, caller, claimed = claimed_order(orders)
    receipt = service.DeviceHeartbeat(execution_id=claimed["execution_id"], resume=True)
    for other in (replace(caller, device_id="dev_other"), replace(caller, sub="other")):
        with pytest.raises(KeyError):
            service.heartbeat("v_one", other, claimed["id"], receipt)
    for packet in ({"last_action": "password=secret"}, {"step": -1}, {"percent": 101}, {"text": "secret"}):
        with pytest.raises(ValueError):
            service.DeviceHeartbeat(execution_id=claimed["execution_id"], **packet)
    with service.db()() as session:
        row = session.get(VentureDocument, claimed["id"])
        body = json.loads(row.provenance_json)
        body.update(state="waiting_for_device", invocations={"one": {"state": "completed", "actual_micro": 10,
                    "effect": {"state": "uncertain"}}})
        row.provenance_json = json.dumps(body)
        session.commit()
    with pytest.raises(ValueError, match="requires_reconciliation"):
        service.heartbeat("v_one", caller, claimed["id"], receipt)


@pytest.mark.parametrize("actual", [-1, 10001, None])
def test_max_age_never_clamps_or_invents_invalid_usage(orders, actual):
    from app.db.models import CreditAccount, CreditReservation, VentureDocument

    service, caller, claimed = claimed_order(orders)
    with service.db()() as session:
        row = session.get(VentureDocument, claimed["id"])
        body = json.loads(row.provenance_json)
        body["invocations"] = {"call": {"state": "completed", "actual_micro": actual}}
        row.provenance_json = json.dumps(body)
        session.commit()
    cutoff = datetime.fromisoformat(claimed["claimed_at"]) + timedelta(hours=24)
    with pytest.raises(ValueError, match="device_stop_budget_inconsistent"):
        service.sweep_device_leases(cutoff)
    with service.db()() as session:
        hold = session.get(CreditReservation, claimed["reservation_id"])
        account = session.get(CreditAccount, hold.account_id)
        assert hold.status == "reconciling" and account.reserved_micro == hold.amount_micro
        assert account.spent_micro == 0


@pytest.mark.parametrize("unknown", [False, True])
def test_max_age_settles_confirmed_or_admitted_upper_bound_once(orders, unknown):
    from app.db.models import CreditAccount, CreditReservation, VentureDocument

    service, caller, claimed = claimed_order(orders)
    with service.db()() as session:
        row = session.get(VentureDocument, claimed["id"])
        body = json.loads(row.provenance_json)
        body["invocations"] = {"call": {"state": "uncertain" if unknown else "completed",
                                        "actual_micro": 25, "quote_micro": 100}}
        row.provenance_json = json.dumps(body)
        session.commit()
    cutoff = datetime.fromisoformat(claimed["claimed_at"]) + timedelta(hours=24)
    assert service.sweep_device_leases(cutoff) == 1
    assert service.sweep_device_leases(cutoff) == 0
    with service.db()() as session:
        hold = session.get(CreditReservation, claimed["reservation_id"])
        account = session.get(CreditAccount, hold.account_id)
        assert hold.status == "settled" and hold.settled_micro == (100 if unknown else 25)
        assert account.reserved_micro == 0 and account.spent_micro == hold.settled_micro


def test_expired_lease_denies_effects_before_maintenance(orders):
    from app.build import device_invocations
    from types import SimpleNamespace

    service, caller, claimed = claimed_order(orders)
    cutoff = datetime.fromisoformat(claimed["claimed_at"]) + timedelta(seconds=30)
    claimed["lease_expires_at"] = cutoff.isoformat()
    with service.db()() as session, pytest.raises(ValueError, match="not_executing"):
        device_invocations._check_executing(session, "v_one", claimed, SimpleNamespace(status="active"), cutoff)


def test_wall_clock_gap_revokes_worker_authority(monkeypatch):
    from types import SimpleNamespace
    from desktop_runtime.device_worker import DeviceWorker

    worker = DeviceWorker(SimpleNamespace(order_id="dwo_sleep", current=lambda: None), lambda _: None)
    worker.deadline, worker.server_deadline = 1000, 200
    monkeypatch.setattr("desktop_runtime.device_worker.time.monotonic", lambda: 100)
    monkeypatch.setattr("desktop_runtime.device_worker.time.time", lambda: 199)
    assert worker.authorize("dwo_sleep", "session")
    monkeypatch.setattr("desktop_runtime.device_worker.time.time", lambda: 10000)
    assert not worker.authorize("dwo_sleep", "session")
    worker.retire_activity()


def test_sleep_gap_also_blocks_ai_action(monkeypatch):
    from types import SimpleNamespace
    from desktop_runtime.device_worker import DeviceWorker

    worker = DeviceWorker(SimpleNamespace(order_id="dwo_sleep"), lambda _: None)
    admitted = []
    worker._ai_scope = SimpleNamespace(admit=lambda *args: admitted.append(args))
    worker.deadline, worker.server_deadline = 1000, 200
    monkeypatch.setattr("desktop_runtime.device_worker.time.monotonic", lambda: 100)
    monkeypatch.setattr("desktop_runtime.device_worker.time.time", lambda: 10000)
    assert worker._admit_ai_action("click", "observation") == "device_execution_expired"
    assert admitted == []
    worker.retire_activity()


def test_heartbeat_thread_runs_during_blocked_step_and_stops():
    from types import SimpleNamespace
    from desktop_runtime.device_worker import DeviceWorker

    received = threading.Event()
    packets = []
    def heartbeat(**packet):
        packets.append(packet)
        received.set()
    gateway = SimpleNamespace(order_id="dwo_thread", claim=lambda: {"claimed_at": "2026-10-09T00:00:00+00:00"},
                              heartbeat=heartbeat)
    worker = DeviceWorker(gateway, lambda _: None)
    worker._claim({}, {"duration_seconds": 900})
    assert received.wait(1)
    assert packets[0] == {"step": 0, "percent": 0, "last_action": "starting"}
    worker.progress("stopped")
    worker._heartbeat_thread.join(timeout=1)
    assert not worker._heartbeat_thread.is_alive()
    worker.retire_activity()


def test_max_age_configuration_is_enforced_after_waiting(orders, monkeypatch):
    from app.db.models import CreditReservation

    service, caller, order = claimed_order(orders)
    start = datetime.fromisoformat(order["claimed_at"])
    live = service.heartbeat("v_one", caller, order["id"], service.DeviceHeartbeat(execution_id=order["execution_id"]))
    assert service.sweep_device_leases(datetime.fromisoformat(live["lease_expires_at"]) + timedelta(seconds=1)) == 1
    monkeypatch.setenv("DEVICE_ORDER_HOLD_MAX_AGE_SECONDS", "120")
    assert service.sweep_device_leases(start + timedelta(seconds=120)) == 1
    with service.db()() as session:
        assert session.get(CreditReservation, order["reservation_id"]).status == "settled"


def test_status_projection_names_sleeping_mac(orders, monkeypatch):
    from app.db.models import VentureDocument
    from app.ventures.run_status import _project_recent_mac_task

    service, caller, order = claimed_order(orders)
    live = service.heartbeat("v_one", caller, order["id"], service.DeviceHeartbeat(execution_id=order["execution_id"]))
    cutoff = datetime.fromisoformat(live["lease_expires_at"]) + timedelta(seconds=1)
    monkeypatch.setattr(service, "utcnow", lambda: cutoff)
    service.sweep_device_leases(cutoff)
    result = {"state": "idle", "changed_at": None}
    with service.db()() as session:
        _project_recent_mac_task(result, "v_one", session.get(VentureDocument, order["id"]))
    assert result["state"] == "waiting_for_device"
    assert result["activity"] == service.WAITING_FOR_DEVICE_MESSAGE


def test_gateway_resumes_without_reclaiming():
    from desktop_runtime.device_gateway import WorkOrderGateway

    calls = []
    saved = {"id": "dwo_resume", "venture_id": "ven_resume", "device_id": "dev_resume",
             "state": "waiting_for_device", "review_digest": "a" * 64, "execution_id": "dex_resume"}
    def request(method, path, packet):
        calls.append((method, path, packet))
        if path.endswith("/capabilities"):
            return {"available": True}
        if path.endswith("/heartbeat"):
            assert packet == {"execution_id": "dex_resume", "resume": True}
            return {**saved, "state": "claimed"}
        return saved
    gateway = WorkOrderGateway(venture_id="ven_resume", order_id="dwo_resume", device_id="dev_resume", request=request)
    assert gateway.claim()["execution_id"] == "dex_resume"
    assert gateway.claimed and not any(path.endswith("/claim") for _, path, _ in calls)


def test_resume_preserves_journal_but_refuses_ambiguous_process(tmp_path, monkeypatch):
    from desktop_runtime import device_journal

    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    scope = ("https://example.test", "founder", "dev_resume")
    device_journal.create(scope, "ven_resume", "dwo_resume")
    receipt = {"state": "observing", "step": 3}
    device_journal.save(scope, "ven_resume", "dwo_resume", receipt)
    device_journal.create(scope, "ven_resume", "dwo_resume", resume=True)
    assert device_journal.read(scope, "ven_resume", "dwo_resume") == receipt
    device_journal.save(scope, "ven_resume", "dwo_resume", {"state": "running_coding"})
    with pytest.raises(ValueError, match="requires_reconciliation"):
        device_journal.create(scope, "ven_resume", "dwo_resume", resume=True)


def test_pending_checkpoint_dispatch_refuses_before_resume_heartbeat(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from desktop_runtime.device_worker import DeviceWorker

    monkeypatch.setenv("SUPYMEM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SUPYMEM_CU_ENGINE", "ai_loop")
    checkpoint = tmp_path / "cu_checkpoints" / "dwo_resume.json"
    checkpoint.parent.mkdir()
    checkpoint.write_text(json.dumps({"pending_dispatch": True}))
    attempted = []
    worker = DeviceWorker(SimpleNamespace(order_id="dwo_resume", claim=lambda: attempted.append(True)), lambda _: None)
    with pytest.raises(ValueError, match="requires_reconciliation"):
        worker._claim({"state": "waiting_for_device"}, {"desktop_verification": {}, "duration_seconds": 900})
    assert attempted == []
    worker.retire_activity()


def test_maintenance_tick_sweeps_device_leases(orders, monkeypatch):
    from contextlib import nullcontext
    from app.build import device_orders
    from app.worker import main
    from app.core import jobs
    from app.credits import reserve
    from app import state

    ticks = []
    monkeypatch.setattr(state, "db", lambda: lambda: nullcontext(type("Session", (), {"commit": lambda _: None})()))
    monkeypatch.setattr(jobs, "reclaim_stale", lambda _: 0)
    monkeypatch.setattr(jobs, "expire_overdue_paid_journey", lambda _: 0)
    monkeypatch.setattr(reserve, "sweep_expired", lambda: ticks.append("credits"))
    monkeypatch.setattr(device_orders, "sweep_device_leases", lambda: ticks.append("devices"))
    main._run_maintenance()
    assert ticks == ["credits", "devices"]


def test_migration_roundtrip_backfills_lease_and_preserves_document_history(tmp_path):
    import importlib.util
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import create_engine, inspect, text

    path = "services/company_os/app/db/migrations/versions/s043deviceleases.py"
    spec = importlib.util.spec_from_file_location("device_lease_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine(f"sqlite:///{tmp_path}/migration.db")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE venture_documents (id TEXT PRIMARY KEY, kind TEXT, provenance_json TEXT)"))
        body = {"state": "claimed", "claimed_at": "2026-10-09T00:00:00+00:00"}
        connection.execute(text("INSERT INTO venture_documents VALUES ('dwo_old','device_work_order_v1',:body)"),
                           {"body": json.dumps(body)})
        connection.execute(text("CREATE INDEX ix_history ON venture_documents (kind)"))
        before_columns = [{**column, "type": str(column["type"])}
                          for column in inspect(connection).get_columns("venture_documents")]
        before_indexes = inspect(connection).get_indexes("venture_documents")
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
        row = connection.execute(text("SELECT lease_expires_at, provenance_json FROM venture_documents")).one()
        assert datetime.fromisoformat(row[0]).hour == 2
        assert json.loads(row[1]) == {**body, "lease_expires_at": "2026-10-09T02:00:00+00:00"}
        with Operations.context(MigrationContext.configure(connection)):
            migration.downgrade()
        assert [{**column, "type": str(column["type"])}
                for column in inspect(connection).get_columns("venture_documents")] == before_columns
        assert inspect(connection).get_indexes("venture_documents") == before_indexes
        assert connection.scalar(text("SELECT provenance_json FROM venture_documents")) == row[1]
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
        assert connection.execute(text("SELECT lease_expires_at, provenance_json FROM venture_documents")).one() == row
    engine.dispose()
