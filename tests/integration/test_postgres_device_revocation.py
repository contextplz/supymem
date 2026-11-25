"""Real row locks and revocation behavior for native logout."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from tests.integration.test_postgres_auth_security import (
    concurrent,
    gateway as gateway,
    pg_database as pg_database,
)


def test_concurrent_device_revoke_is_idempotent_and_stops_access(gateway):
    _, devices, user, engine = gateway
    device = devices.create_device(person_id=user.id, name="Concurrent logout Mac")
    token = devices.rotate_refresh(device, previous=None)
    access = devices.issue_access_jwt(user.id, device)
    assert devices.load_bearer(access).subject == user.id
    assert devices.load_bearer(access).device_id == device
    assert all(concurrent(lambda: devices.revoke_device(device, token)))
    assert devices.load_bearer(access) is None
    assert devices.device_person(device) is None
    with engine.connect() as connection:
        assert connection.execute(text(
            "SELECT count(*) FROM security_events WHERE kind='device_revoked' AND detail LIKE :device"
        ), {"device": f"%{device}%"}).scalar_one() == 1


def test_foreign_refresh_does_not_change_device_state(gateway):
    _, devices, user, engine = gateway
    first = devices.create_device(person_id=user.id, name="First Mac")
    second = devices.create_device(person_id=user.id, name="Second Mac")
    devices.rotate_refresh(first, previous=None)
    other_refresh = devices.rotate_refresh(second, previous=None)
    assert not devices.revoke_device(first, other_refresh)
    with engine.connect() as connection:
        assert connection.execute(text(
            "SELECT revoked_at FROM desktop_devices WHERE id=:device"
        ), {"device": first}).scalar_one() is None


@pytest.mark.parametrize("zone", ["UTC", "Asia/Kolkata", "America/Los_Angeles"])
def test_postgres_retry_window_does_not_extend_and_honors_utc(gateway, monkeypatch, zone):
    from sqlalchemy import event
    users, devices, user, engine = gateway
    pg_engine = users._pg_engine()
    def set_zone(connection, *_args):
        with connection.cursor() as cursor:
            cursor.execute("SELECT set_config('TimeZone', %s, false)", (zone,))
        connection.commit()
    event.listen(pg_engine, "checkout", set_zone)
    try:
        device = devices.create_device(person_id=user.id, name="Fixed retry window Mac")
        clock = devices._now
        at = datetime(2026, 10, 7, 1, 2, 3, 456789, tzinfo=timezone.utc)
        monkeypatch.setattr(devices, "_now", lambda: at)
        initial = devices.rotate_refresh(device, previous=None)
        second = devices.rotate_refresh(device, previous=initial)
        monkeypatch.setattr(devices, "_now", lambda: at + timedelta(seconds=600))
        assert devices.rotate_refresh(device, previous=initial) == second
        with engine.connect() as connection:
            row = connection.execute(text("SELECT * FROM desktop_devices WHERE id=:id"), {"id": device}).mappings().one()
            assert row["previous_refresh_token_hash"] == users._hash_token(initial)
            assert row["refresh_token_hash"] == users._hash_token(second)
            assert row["refresh_rotated_at"] == at.replace(tzinfo=None)
            assert initial not in str(dict(row)) and second not in str(dict(row))
            assert connection.execute(text(
                "SELECT severity FROM security_events WHERE kind='refresh_retry_grace' AND detail LIKE :device"
            ), {"device": f"%{device}%"}).scalars().all() == ["info"]
        monkeypatch.setattr(devices, "_now", lambda: at + timedelta(seconds=601))
        with pytest.raises(ValueError, match="invalid_refresh"):
            devices.rotate_refresh(device, previous=initial)
        assert devices.device_person(device) == user.id
        monkeypatch.setattr(devices, "_now", clock)
        access = devices.issue_access_jwt(user.id, device, refresh_token=second)
        assert devices.load_bearer(access).subject == user.id
        monkeypatch.setattr(devices, "_now", lambda: at + timedelta(seconds=601))
        with pytest.raises(ValueError, match="invalid_refresh"):
            devices.rotate_refresh(device, previous=initial)
        with pytest.raises(ValueError, match="token_reuse"):
            devices.rotate_refresh(device, previous=second)
        assert devices.device_person(device) is None
    finally:
        event.remove(pg_engine, "checkout", set_zone)


@pytest.mark.parametrize("elapsed", [0, 601])
def test_postgres_logout_accepts_previous_token_after_lost_reply(gateway, monkeypatch, elapsed):
    _, devices, user, _ = gateway
    device = devices.create_device(person_id=user.id, name="Lost refresh logout Mac")
    first = devices.rotate_refresh(device, previous=None)
    devices.rotate_refresh(device, previous=first)
    later = devices._now() + timedelta(seconds=elapsed)
    monkeypatch.setattr(devices, "_now", lambda: later)
    assert devices.revoke_device(device, first)
    assert devices.device_person(device) is None
