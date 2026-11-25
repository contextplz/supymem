"""Founder emergency Stop: Core cancels the order once, records a receipt, holds unknown effects."""
import json

import pytest

from tests.unit.product.test_device_orders import explicit_native_admission as explicit_native_admission
from tests.unit.product.test_device_orders import orders as orders


def _claimed(svc, caller, manifest):
    order = svc.prepare("v_one", caller, manifest)
    args = ("v_one", caller, order["id"], order["review_digest"])
    svc.transition(*args, "approve")
    svc.transition(*args, "claim")
    return order


def test_stop_cancels_claimed_order_records_receipt_and_is_idempotent(orders):
    from app.db.models import CreditAccount
    svc, caller, manifest = orders
    order = _claimed(svc, caller, manifest)
    first = svc.emergency_stop("v_one", caller, order["id"], "menu")
    assert first["state"] == "cancelled"
    assert first["stopped_by_user"]["source"] == "menu"
    again = svc.emergency_stop("v_one", caller, order["id"], "cursor")
    assert again["stopped_by_user"] == first["stopped_by_user"]  # first receipt wins
    with svc.db()() as session:  # credits stay reconciling like any claimed cancel
        assert session.get(CreditAccount, "cra_device_test").reserved_micro == 1000
    with pytest.raises(ValueError):
        svc.transition("v_one", caller, order["id"], order["review_digest"], "claim")


def test_stop_releases_unclaimed_order_and_normalizes_unknown_source(orders):
    svc, caller, manifest = orders
    order = svc.prepare("v_one", caller, manifest)
    stopped = svc.emergency_stop("v_one", caller, order["id"], "<script>")
    assert stopped["state"] == "cancelled" and stopped["stopped_by_user"]["source"] == "app"


def test_stop_holds_unknown_effects_and_never_replays_them(orders):
    from app.db.models import VentureDocument
    svc, caller, manifest = orders
    order = _claimed(svc, caller, manifest)
    with svc.db()() as session:
        row = session.get(VentureDocument, order["id"])
        body = json.loads(row.provenance_json)
        body["invocations"] = {
            "inv_unknown": {"effect": {"state": "admitted"}},
            "inv_uncertain": {"effect": {"state": "uncertain"}},
            "inv_done": {"effect": {"state": "returned"}},
        }
        row.provenance_json = json.dumps(body)
        session.commit()
    stopped = svc.emergency_stop("v_one", caller, order["id"])
    assert stopped["stopped_by_user"]["held_effects"] == ["inv_uncertain", "inv_unknown"]
    states = {k: v["effect"]["state"] for k, v in stopped["invocations"].items()}
    assert states == {"inv_unknown": "admitted", "inv_uncertain": "uncertain", "inv_done": "returned"}
    from app.build import device_effects
    with pytest.raises((ValueError, KeyError, PermissionError)):  # a cancelled order admits nothing
        device_effects.admit("v_one", caller, order["id"], invocation_id="inv_unknown",
                             observation_digest="a" * 64, effect_digest="b" * 64)


def test_stop_after_the_mac_already_closed_the_order_only_adds_the_receipt(orders):
    from app.db.models import VentureDocument
    svc, caller, manifest = orders
    order = _claimed(svc, caller, manifest)
    with svc.db()() as session:
        row = session.get(VentureDocument, order["id"])
        body = json.loads(row.provenance_json)
        body["state"] = "stopped"
        row.provenance_json = json.dumps(body)
        session.commit()
    stopped = svc.emergency_stop("v_one", caller, order["id"], "shortcut")
    assert stopped["state"] == "stopped" and stopped["stopped_by_user"]["source"] == "shortcut"
