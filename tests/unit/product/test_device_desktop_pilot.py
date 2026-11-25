"""Exact current-device pilot admission; no native, provider or production calls."""
import copy
import json
from dataclasses import replace

import pytest
from sqlalchemy import select

from tests.unit.product.test_device_orders import orders as orders
from tests.unit.product.test_native_desktop_admission import desktop_manifest


@pytest.mark.parametrize("raw", [None, "", "true", "{}", '["founder"]', '[["founder","*"]]',
    '[["founder","dev_one"],null]', '[["founder","dev_one",true]]', '[[" founder","dev_one"]]'])
def test_default_or_malformed_pair_configuration_never_prepares(orders, monkeypatch, raw):
    from app.build import device_desktop_pilot as pilot
    from app.db.models import CreditReservation, VentureDocument
    svc, caller, _ = orders
    if raw is None:
        monkeypatch.delenv(pilot.ALLOWLIST_ENV)
    else:
        monkeypatch.setenv(pilot.ALLOWLIST_ENV, raw)
    with pytest.raises(ValueError, match="^device_desktop_execution_unavailable$"):
        svc.prepare("v_one", caller, desktop_manifest(svc))
    with svc.db()() as session:
        assert not session.scalar(select(VentureDocument.id).where(VentureDocument.kind == svc.KIND))
        assert not session.scalar(select(CreditReservation.id))


def test_pair_and_global_flag_are_both_required(orders, monkeypatch):
    from app.build import device_desktop_pilot as pilot
    svc, caller, _ = orders
    for pairs in ([["other", "dev_one"]], [["founder", "other"]], [["founder", "dev_one"]]):
        monkeypatch.setenv(pilot.ALLOWLIST_ENV, json.dumps(pairs))
        if pairs == [["founder", "dev_one"]]:
            monkeypatch.setenv(svc.DESKTOP_EXECUTION_ENV, "0")
        with pytest.raises(ValueError, match="^device_desktop_execution_unavailable$"):
            svc.prepare("v_one", caller, desktop_manifest(svc))


@pytest.mark.parametrize("change", ["missing", "foreign", "revoked", "platform", "old", "unknown"])
def test_configured_pair_requires_current_owned_mac(orders, change):
    from app.db.supymem_models import DesktopDevice
    svc, caller, _ = orders
    with svc.db()() as session:
        row = session.get(DesktopDevice, caller.device_id)
        if change == "missing":
            session.delete(row)
        elif change == "foreign":
            row.person_id = "other"
        elif change == "revoked":
            row.revoked_at = svc.utcnow()
        elif change == "platform":
            row.platform = "windows"
        else:
            row.app_version = "0.1.83" if change == "old" else None
        session.commit()
    with pytest.raises(ValueError, match="^device_desktop_execution_unavailable$"):
        svc.prepare("v_one", caller, desktop_manifest(svc))


def test_pair_retirement_rechecked_on_approval_claim_and_new_execution(orders, monkeypatch):
    from app.build import device_desktop_pilot as pilot, device_invocations as calls
    svc, caller, _ = orders
    order = svc.prepare("v_one", caller, desktop_manifest(svc))
    args = ("v_one", caller, order["id"])
    for action in ("approve", "claim"):
        monkeypatch.setenv(pilot.ALLOWLIST_ENV, "[]")
        before = copy.deepcopy(svc.retrieve(*args))
        with pytest.raises(ValueError, match="^device_desktop_execution_unavailable$"):
            svc.transition(*args, before["review_digest"], action)
        assert svc.retrieve(*args) == before
        monkeypatch.setenv(pilot.ALLOWLIST_ENV, '[["founder","dev_one"]]')
        svc.transition(*args, before["review_digest"], action)
    calls.admit(*args, invocation_id="settle_only", request_digest="a" * 64, quote_micro=100)
    monkeypatch.setenv(pilot.ALLOWLIST_ENV, "[]")
    with pytest.raises(ValueError, match="^device_desktop_execution_unavailable$"):
        calls.check_current(*args)
    with pytest.raises(ValueError, match="^device_desktop_execution_unavailable$"):
        calls.admit(*args, invocation_id="new", request_digest="b" * 64, quote_micro=100)
    # Retirement cannot strand already admitted accounting or founder cancellation.
    calls.record_result(*args, invocation_id="settle_only", actual_micro=42)
    assert svc.transition(*args, svc.retrieve(*args)["review_digest"], "cancel")["state"] == "cancelled"


def test_device_revocation_and_saved_review_rechecked_after_claim(orders):
    from app.build import device_invocations as calls
    from app.db.supymem_models import DesktopDevice
    svc, caller, _ = orders
    order = svc.prepare("v_one", caller, desktop_manifest(svc))
    args = ("v_one", caller, order["id"])
    with pytest.raises(ValueError, match="device_order_review_changed"):
        svc.transition(*args, "0" * 64, "approve")
    for action in ("approve", "claim"):
        svc.transition(*args, svc.retrieve(*args)["review_digest"], action)
    with pytest.raises(KeyError):
        calls.check_current("v_one", replace(caller, sub="other"), order["id"])
    with pytest.raises(KeyError):
        calls.check_current("v_one", replace(caller, device_id="other"), order["id"])
    with svc.db()() as session:
        session.get(DesktopDevice, caller.device_id).revoked_at = svc.utcnow()
        session.commit()
    with pytest.raises(ValueError, match="^device_desktop_execution_unavailable$"):
        calls.check_current(*args)
