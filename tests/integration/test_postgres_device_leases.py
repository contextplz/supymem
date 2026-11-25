import json
from dataclasses import replace
from datetime import datetime, timedelta

import pytest
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from tests.integration.test_postgres_device_orders import (
    pg_database as pg_database, journey_db as journey_db, explicit_native_admission as explicit_native_admission, prepare,
)


def claim(journey_db):
    service, caller, order = prepare(journey_db, duration_seconds=300)
    args = (journey_db["vid"], caller, order["id"], order["review_digest"])
    service.transition(*args, "approve")
    return service, caller, service.transition(*args, "claim")


def test_postgres_lease_lifecycle(journey_db, monkeypatch):
    from app.build import device_invocations, device_stop
    from app.db.models import CreditReservation, VentureDocument

    service, caller, order = claim(journey_db)
    vid = journey_db["vid"]
    receipt = service.DeviceHeartbeat(execution_id=order["execution_id"], step=1, percent=20, last_action="observing")
    live = service.heartbeat(vid, caller, order["id"], receipt)
    cutoff = datetime.fromisoformat(live["lease_expires_at"]) + timedelta(seconds=1)
    monkeypatch.setattr(service, "utcnow", lambda: cutoff)
    assert service.sweep_device_leases(cutoff) == 1
    assert service.sweep_device_leases(cutoff) == 0
    assert service.retrieve(vid, caller, order["id"])["state"] == "waiting_for_device"
    with journey_db["factory"]() as session:
        assert session.get(CreditReservation, order["reservation_id"]).status == "reconciling"
    with pytest.raises(KeyError):
        service.heartbeat(vid, replace(caller, device_id="dev_other"), order["id"], receipt)
    resumed = service.heartbeat(vid, caller, order["id"], receipt)
    assert resumed["state"] == "claimed" and resumed["execution_id"] == order["execution_id"]
    device_invocations.check_current(vid, caller, order["id"])
    stopped = device_stop.record(vid, caller, order["id"], execution_id=order["execution_id"],
                                 evidence_digest="b" * 64, processes_stopped=True)
    assert stopped["state"] == "stopped"
    with journey_db["factory"]() as session:
        hold = session.get(CreditReservation, order["reservation_id"])
        assert hold.status == "settled" and hold.settled_micro == 0
        assert session.get(VentureDocument, order["id"]).lease_expires_at is None


@pytest.mark.parametrize("unknown", [False, True])
def test_postgres_max_age_releases_remainder_using_existing_settlement(journey_db, monkeypatch, unknown):
    from app.db.models import CreditAccount, CreditReservation, VentureDocument

    service, caller, order = claim(journey_db)
    with journey_db["factory"]() as session:
        row = session.get(VentureDocument, order["id"])
        body = json.loads(row.provenance_json)
        body["invocations"] = {"call": {"state": "uncertain" if unknown else "completed",
                                        "actual_micro": 25, "quote_micro": 100}}
        row.provenance_json = json.dumps(body)
        session.commit()
    cutoff = datetime.fromisoformat(order["claimed_at"]) + timedelta(hours=24)
    monkeypatch.setattr(service, "utcnow", lambda: cutoff)
    assert service.sweep_device_leases(cutoff) == 1
    assert service.sweep_device_leases(cutoff) == 0
    with journey_db["factory"]() as session:
        hold = session.get(CreditReservation, order["reservation_id"])
        account = session.get(CreditAccount, hold.account_id)
        assert hold.status == "settled" and hold.settled_micro == (100 if unknown else 25)
        assert account.reserved_micro == 0 and account.spent_micro == hold.settled_micro


def test_postgres_old_client_long_default_lease(journey_db):
    service, caller, order = claim(journey_db)
    started = datetime.fromisoformat(order["claimed_at"])
    assert datetime.fromisoformat(order["lease_expires_at"]) - started == timedelta(hours=2)
    assert service.sweep_device_leases(started + timedelta(minutes=90)) == 0
    assert service.retrieve(journey_db["vid"], caller, order["id"])["state"] == "claimed"


def test_postgres_heartbeat_sweeper_race_cannot_lose_renewal(journey_db, monkeypatch):
    service, caller, order = claim(journey_db)
    cutoff = datetime.fromisoformat(order["lease_expires_at"]) + timedelta(seconds=1)
    monkeypatch.setattr(service, "utcnow", lambda: cutoff)
    barrier = Barrier(2)
    def sweep():
        barrier.wait(timeout=5)
        service.sweep_device_leases(cutoff)
    def renew():
        barrier.wait(timeout=5)
        service.heartbeat(journey_db["vid"], caller, order["id"], service.DeviceHeartbeat(execution_id=order["execution_id"]))
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(sweep), pool.submit(renew)]
        for future in futures:
            future.result(timeout=10)
    assert service.retrieve(journey_db["vid"], caller, order["id"])["state"] == "claimed"
