import json
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta

import pytest
from sqlalchemy import event, select

from tests.unit.product.test_device_orders import orders as orders
from tests.unit.tasks.test_device import NOW


DOMAINS = ("Inventory alerts for warehouse teams", "Trail notes for weekend hikers")


def _venture(service, title):
    from app.db.models import Person, Venture, Workspace

    with service.db()() as session:
        session.add(Person(id="founder", display_name="You", created_at=service.utcnow()))
        session.add(Workspace(id="w_one", owner_id="founder", kind="venture", name=title,
            entry_mode="idea", created_at=service.utcnow(), updated_at=service.utcnow()))
        session.add(Venture(id="v_one", workspace_id="w_one", name=title, raw_idea=title,
            stage="draft", created_at=service.utcnow(), updated_at=service.utcnow()))
        session.commit()


def test_twenty_workspaces_claim_concurrently_and_sweep_uses_distinct_locks(orders, monkeypatch):
    from app.build import device_stop
    from app.credits import _txn
    from app.db.models import CreditAccount, CreditReservation, Person, Venture, Workspace
    from app.db.supymem_models import DesktopDevice
    from app.db.task_models import Task, TaskRun, TaskStep
    from app.tasks import device
    from app.tasks.contracts import Budget
    from app.tenancy import bind

    service, caller, manifest = orders
    monkeypatch.setattr(_txn, "utcnow", lambda: NOW)
    monkeypatch.setattr(service, "utcnow", lambda: NOW)
    monkeypatch.setattr(service, "assert_venture_access", lambda vid, sub: (vid, "ws_" + vid.removeprefix("ven_")))
    with service.db()() as session:
        engine = session.get_bind()

    def writer_wait(connection, _record):
        connection.execute("PRAGMA busy_timeout=60000")

    # ponytail: SQLite serializes writers; verify parallel transactions on PostgreSQL in deployment.
    event.listen(engine, "connect", writer_wait)
    engine.dispose()
    moment = NOW.replace(tzinfo=None)
    with service.db()() as session:
        for index in range(20):
            title = DOMAINS[index % 2]
            session.add(Person(id=f"usr_{index}", display_name="You", created_at=moment))
            session.add(Workspace(id=f"ws_{index}", owner_id=f"usr_{index}", kind="venture", name=title,
                entry_mode="idea", created_at=moment, updated_at=moment))
            session.add(Venture(id=f"ven_{index}", workspace_id=f"ws_{index}", name=title, raw_idea=title,
                stage="draft", created_at=moment, updated_at=moment))
            session.add(DesktopDevice(id=f"dev_{index}", person_id=f"usr_{index}", name="Your Mac", platform="macos"))
            session.add(CreditAccount(id=f"cra_{index}", workspace_id=f"ws_{index}", venture_id=f"ven_{index}",
                scope="venture", granted_micro=10000, spent_micro=0, reserved_micro=0, status="active",
                created_at=moment, updated_at=moment))
            session.add(Task(id=f"tsk_{index}", workspace_id=f"ws_{index}", venture_id=f"ven_{index}",
                owner_person_id=f"usr_{index}", title=title, goal=title, state="working",
                budget_json=Budget().model_dump_json(), deadline_at=moment + timedelta(minutes=20),
                created_at=moment, updated_at=moment))
            session.add(TaskRun(id=f"trn_{index}", task_id=f"tsk_{index}", run_key="initial", state="working",
                started_at=moment, deadline_at=moment + timedelta(minutes=20)))
            session.add(TaskStep(id=f"tst_{index}", run_id=f"trn_{index}", position=0, title=title,
                skill="device.inspect", runs_on="device", state="submitted",
                deadline_at=moment + timedelta(minutes=20)))
        session.commit()
    approved = []
    for index in range(20):
        owner = replace(caller, sub=f"usr_{index}", device_id=f"dev_{index}")
        review = manifest.model_copy(update={"title": DOMAINS[index % 2], "instruction": DOMAINS[index % 2]})
        order = service.prepare(f"ven_{index}", owner, review)
        args = (f"ven_{index}", owner, order["id"], order["review_digest"])
        service.transition(*args, "approve")
        approved.append(args)
    barrier = threading.Barrier(20)
    keys = []
    original = device._serialize

    def capture(session, key):
        keys.append(key)
        return original(session, key)

    monkeypatch.setattr(device, "_serialize", capture)

    def claim(index):
        barrier.wait(timeout=20)
        claimed = service.transition(*approved[index], "claim")
        with service.db()() as session, bind(workspace_id=f"ws_{index}",
                venture_id=f"ven_{index}", actor=f"usr_{index}"):
            result = device.claim_step(session, f"tsk_{index}", f"tst_{index}", f"dev_{index}", now=NOW)
            session.commit()
            return claimed, result

    with ThreadPoolExecutor(max_workers=20) as workers:
        results = list(workers.map(claim, range(20)))
    assert all(claimed["state"] == "claimed" and result["accepted"] for claimed, result in results)
    assert len({result["lease_token"] for _, result in results}) == 20
    assert len({claimed["execution_id"] for claimed, _ in results}) == 20
    expected = {f"task-device:tsk_{index}" for index in range(20)}
    assert set(keys) == expected
    keys.clear()
    with service.db()() as session:
        assert all(session.get(CreditAccount, f"cra_{index}").reserved_micro == 1000 for index in range(20))
        assert len(device.sweep_stale_leases(session, now=NOW + timedelta(seconds=61))) == 20
        session.commit()
    assert set(keys) == expected

    def receipt(index):
        barrier.wait(timeout=20)
        return device_stop.record(*approved[index][:3], execution_id=results[index][0]["execution_id"],
            evidence_digest=f"{index:064x}", processes_stopped=True)

    with ThreadPoolExecutor(max_workers=20) as workers:
        stopped = list(workers.map(receipt, range(20)))
    assert all(body["state"] == "stopped" for body in stopped)
    with service.db()() as session:
        assert all(session.get(CreditAccount, f"cra_{index}").reserved_micro == 0 for index in range(20))
        assert all(session.get(CreditReservation, body["reservation_id"]).status == "settled" for body in stopped)


@pytest.mark.hosted
@pytest.mark.parametrize("title", DOMAINS)
def test_offline_mac_notifies_once_releases_expired_execution_and_keeps_uncertainty(orders, monkeypatch, title):
    from app.build import device_invocations
    from app.db.models import ChatMessage, CreditAccount, CreditReservation, VentureDocument

    monkeypatch.setenv("DEVICE_ORDER_HOLD_MAX_AGE_SECONDS", "600")
    service, caller, manifest = orders
    _venture(service, title)
    manifest.title, manifest.instruction, manifest.duration_seconds = title, f"Read the page about {title}", 300
    order = service.prepare("v_one", caller, manifest)
    args = ("v_one", caller, order["id"], order["review_digest"])
    service.transition(*args, "approve")
    claimed = service.transition(*args, "claim")
    live = service.heartbeat("v_one", caller, order["id"], service.DeviceHeartbeat(execution_id=claimed["execution_id"]))
    cutoff = datetime.fromisoformat(live["lease_expires_at"]) + timedelta(seconds=1)
    monkeypatch.setattr(service, "utcnow", lambda: cutoff)
    assert service.sweep_device_leases(cutoff) == 1
    assert service.sweep_device_leases(cutoff) == 0
    waiting = service.retrieve("v_one", caller, order["id"])
    assert waiting["state"] == "waiting_for_device"
    with pytest.raises(ValueError, match="not_executing"):
        device_invocations.check_current("v_one", caller, order["id"])
    with service.db()() as session:
        messages = session.scalars(select(ChatMessage).where(ChatMessage.kind == "mac_task_stopped")).all()
        assert len(messages) == 1
        assert "Your coding tool stopped" in messages[0].body_md
        assert "Then choose Retry." in messages[0].body_md
        actions = json.loads(messages[0].actions_json)
        assert {"Retry", "Do it another way"} <= {action["label"] for action in actions}
        alternative = next(action for action in actions if action["label"] == "Do it another way")
        assert alternative["payload"]["step"] == "mac.task"
        row = session.get(VentureDocument, order["id"])
        body = json.loads(row.provenance_json)
        body["invocations"] = {"lost": {"state": "admitted", "quote_micro": 25, "effect": {"state": "uncertain"}}}
        row.provenance_json = json.dumps(body)
        session.commit()
    cutoff = datetime.fromisoformat(claimed["claimed_at"]) + timedelta(seconds=601)
    monkeypatch.setattr(service, "utcnow", lambda: cutoff)
    with pytest.raises(ValueError, match="lease_closed"):
        service.heartbeat("v_one", caller, order["id"], service.DeviceHeartbeat(execution_id=claimed["execution_id"]))
    assert service.sweep_device_leases(cutoff) == 1
    assert service.sweep_device_leases(cutoff) == 0
    saved = service.retrieve("v_one", caller, order["id"])
    assert saved["state"] == "cancelled" and saved["lease_reconciliation"] == {
        "reconciled_at": cutoff.isoformat(), "actual_usage_unconfirmed": True,
        "effect_outcome_unconfirmed": True, "process_cleanup_proven": False}
    with service.db()() as session:
        assert session.get(VentureDocument, order["id"]).lease_expires_at is None
        hold = session.get(CreditReservation, saved["reservation_id"])
        assert hold.status == "settled" and hold.settled_micro == 25
        account = session.get(CreditAccount, hold.account_id)
        assert account.reserved_micro == 0 and account.spent_micro == 25
        assert list(service._active_orders(session, "v_one", manifest.task_id, cutoff)) == []
    assert service.prepare("v_one", caller, manifest)["state"] == "pending"


@pytest.mark.parametrize("title", DOMAINS)
def test_every_failed_outcome_receipt_posts_plain_guidance_with_next_steps(orders, monkeypatch, title):
    from app.build import device_stop
    from app.chat import mac_stop
    from app.db.models import ChatMessage
    from desktop_runtime.cu_reliability import outcome_code

    service, caller, manifest = orders
    _venture(service, title)
    manifest.title, manifest.instruction = title, f"Read the page about {title}"
    codes = {outcome_code(code) for code in ("blocked", "timeout", "auth_lost", "model_unavailable", "needs_founder")}
    for index, code in enumerate(sorted(codes)):
        order = service.prepare("v_one", caller, manifest)
        args = ("v_one", caller, order["id"], order["review_digest"])
        service.transition(*args, "approve")
        claimed = service.transition(*args, "claim")
        reason = "cu_stopped" if code in {"model_unavailable", "timeout"} else "cu_unmapped_detail" if index % 2 else None
        receipt = dict(execution_id=claimed["execution_id"], evidence_digest="a" * 64,
            processes_stopped=True, outcome_code=code, reason_code=reason)
        with monkeypatch.context() as first_delivery:
            if index == 0:
                first_delivery.setattr(mac_stop, "post_mac_stopped", lambda *args, **kwargs: False)
            stopped = device_stop.record("v_one", caller, order["id"], **receipt)
        assert stopped["state"] == "stopped" and stopped["outcome_code"] == code
        assert device_stop.record("v_one", caller, order["id"], **receipt)["state"] == "stopped"
        with service.db()() as session:
            messages = session.scalars(select(ChatMessage).where(ChatMessage.kind == "mac_task_stopped")).all()
            assert len(messages) == index + 1
            message = max(messages, key=lambda message: message.created_at)
            assert "cu_" not in message.body_md and "\u2014" not in message.body_md
            actions = json.loads(message.actions_json)
            assert {"Retry", "Do it another way", "Explain in detail"} <= {action["label"] for action in actions}
            explanation = next(action for action in actions if action["label"] == "Explain in detail")
            assert explanation["payload"]["code"] == f"mac_{code}"
