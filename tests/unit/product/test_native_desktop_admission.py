"""Generic native beta input is unavailable independently of other device routes."""
import copy

import pytest
from sqlalchemy import select

from tests.unit.product.test_device_orders import orders as orders
from tests.unit.product.test_device_ai_step import ai_order as ai_order


def close_native_gate(monkeypatch):
    from app.build import device_orders
    from app.core.local_mode import is_local
    from app.core.local_secrets import update_settings

    monkeypatch.delenv(device_orders.DESKTOP_EXECUTION_ENV, raising=False)
    if is_local():
        update_settings("capabilities", {"background_work": False}, {})


def desktop_manifest(svc):
    return svc.Manifest(task_id="task_one", title="Reviewed native window",
        instruction="Press the reviewed shortcut in the existing native window.",
        applications=["com.apple.TextEdit"], origins=[],
        actions=["desktop.read", "desktop.interact"], credit_ceiling_micro=1000,
        duration_seconds=60, desktop_verification={"operation": "desktop_goal_v1",
            "application_bundle": "com.apple.TextEdit", "process_id": 123,
            "process_start_identity": "123456", "window_id": 44,
            "window_title": "Owned document", "delivery_mode": "background",
            "max_steps": 4, "reviewed_text": []})


@pytest.mark.parametrize("value", [None, "", "0", "true", "enabled", " 1", "1 "])
def test_default_and_nonexact_flags_refuse_before_recipe_card_or_credit_hold(orders, monkeypatch, value):
    svc, caller, _ = orders
    close_native_gate(monkeypatch)
    if value is None:
        monkeypatch.delenv(svc.DESKTOP_EXECUTION_ENV, raising=False)
    else:
        monkeypatch.setenv(svc.DESKTOP_EXECUTION_ENV, value)
    monkeypatch.setattr(svc, "_desktop_reviewed_text", lambda *_a, **_k: pytest.fail("recipe ran"))
    monkeypatch.setattr(svc, "_scope", lambda *_a: pytest.fail("authority/storage preparation ran"))
    with pytest.raises(ValueError, match="^device_desktop_execution_unavailable$"):
        svc.prepare_desktop_from_chat("v_one", caller, "In TextEdit press cmd+s.",
            "com.apple.TextEdit", 123, "123456", 44, "Owned document")
    with pytest.raises(ValueError, match="^device_desktop_execution_unavailable$"):
        svc.prepare("v_one", caller, desktop_manifest(svc))
    from app.db.models import CreditAccount, CreditReservation, VentureDocument
    with svc.db()() as session:
        assert not session.scalar(select(VentureDocument.id).where(VentureDocument.kind == svc.KIND))
        assert not session.scalar(select(CreditReservation.id))
        account = session.get(CreditAccount, "cra_device_test")
        assert (account.reserved_micro, account.spent_micro) == (0, 0)


@pytest.mark.parametrize("descriptor", [
    {"applications": ["com.apple.TextEdit"], "actions": ["browser.read"]},
    {"applications": ["com.google.Chrome"], "actions": ["browser.read", "browser.interact"],
     "general_browser_verification": {"operation": "general_goal_v1"}},
    {"actions": ["editor.edit", "terminal.execute"], "coding_verification": {"offline_profile": "static_html_v1"}},
    {"actions": ["terminal.execute"], "terminal_verification": {"operation": "git_status_v1"}},
])
def test_saved_descriptor_not_app_name_or_instruction_defines_native_boundary(monkeypatch, descriptor):
    from app.build import device_orders as svc
    monkeypatch.delenv(svc.DESKTOP_EXECUTION_ENV, raising=False)
    assert not svc.native_desktop_manifest(descriptor)
    svc.assert_native_desktop_available(descriptor)


@pytest.mark.parametrize("descriptor", [
    {"desktop_verification": {}, "actions": ["browser.read"]},
    {"desktop_verification": {"operation": "desktop_goal_v1"}, "actions": []},
    {"actions": ["desktop.read"]}, {"actions": ["desktop.interact"]},
])
def test_legacy_descriptors_cannot_erase_native_classification(monkeypatch, descriptor):
    from app.build import device_orders as svc
    close_native_gate(monkeypatch)
    with pytest.raises(ValueError, match="^device_desktop_execution_unavailable$"):
        svc.assert_native_desktop_available(descriptor)


@pytest.mark.parametrize("stage", ["pending", "approved", "claimed"])
def test_legacy_native_review_cannot_approve_claim_or_reenter_after_gate_retires(orders, monkeypatch, stage):
    svc, caller, _ = orders
    order = svc.prepare("v_one", caller, desktop_manifest(svc))
    args = ("v_one", caller, order["id"])
    if stage in {"approved", "claimed"}:
        svc.transition(*args, order["review_digest"], "approve")
    if stage == "claimed":
        svc.transition(*args, order["review_digest"], "claim")
    before = copy.deepcopy(svc.retrieve(*args))
    close_native_gate(monkeypatch)
    for action in ("approve", "claim"):
        with pytest.raises(ValueError, match="^device_desktop_execution_unavailable$"):
            svc.transition(*args, order["review_digest"], action)
    if stage == "claimed":
        from app.build import device_invocations as calls
        with pytest.raises(ValueError, match="^device_desktop_execution_unavailable$"):
            calls.check_current(*args)
        with pytest.raises(ValueError, match="^device_desktop_execution_unavailable$"):
            calls.admit(*args, invocation_id="restart", request_digest="a" * 64, quote_micro=100)
    assert svc.retrieve(*args) == before
    assert svc.transition(*args, order["review_digest"], "cancel")["state"] == "cancelled"


def test_previously_admitted_effect_is_not_replayed_but_receipts_survive_disabled_native_gate(orders, monkeypatch):
    from app.build import device_effects as effects, device_invocations as calls
    from coding_agents.jev_decision import Candidate
    svc, caller, _ = orders
    order = svc.prepare("v_one", caller, desktop_manifest(svc))
    args = ("v_one", caller, order["id"])
    for action in ("approve", "claim"):
        svc.transition(*args, order["review_digest"], action)
    calls.admit(*args, invocation_id="effect", request_digest="a" * 64, quote_micro=100)
    calls.record_result(*args, invocation_id="effect", actual_micro=42)
    effects.save_proposal(*args, invocation_id="effect", candidate=Candidate("key", "c0", "b" * 64))
    assert effects.admit(*args, invocation_id="effect", observation_digest="b" * 64, effect_digest="c" * 64)
    monkeypatch.delenv(svc.DESKTOP_EXECUTION_ENV)
    before = copy.deepcopy(svc.retrieve(*args))
    with pytest.raises(ValueError, match="^device_desktop_execution_unavailable$"):
        effects.admit(*args, invocation_id="effect", observation_digest="b" * 64, effect_digest="c" * 64)
    assert svc.retrieve(*args) == before
    effects.receipt(*args, invocation_id="effect", effect_digest="c" * 64, outcome="uncertain")
    calls.record_result(*args, invocation_id="effect", actual_micro=42)
    saved = svc.retrieve(*args)["invocations"]["effect"]
    assert saved["effect"]["state"] == "uncertain" and saved["actual_micro"] == 42
    assert svc.transition(*args, order["review_digest"], "cancel")["state"] == "cancelled"


@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_reviewed_static_subscription_coding_still_prepares_claims_and_has_authority_with_native_gate_off(orders, monkeypatch, provider):
    from app.build import device_invocations as calls
    from tests.unit.product.test_native_coding_offline_profile import coding_manifest, static_snapshot
    svc, caller, _ = orders
    monkeypatch.delenv(svc.DESKTOP_EXECUTION_ENV)
    monkeypatch.setattr(svc, "_task_snapshot", lambda *_a, **_k: static_snapshot())
    manifest = coding_manifest(svc)
    manifest.coding_verification.provider = provider
    order = svc.prepare("v_one", caller, manifest)
    args = ("v_one", caller, order["id"])
    for action in ("approve", "claim"):
        svc.transition(*args, order["review_digest"], action)
    calls.check_current(*args)
    assert svc.retrieve(*args)["manifest"]["coding_verification"]["offline_profile"] == "static_html_v1"


def test_isolated_browser_prepare_claim_and_fresh_model_admission_are_preserved(orders, monkeypatch):
    from app.build import device_invocations as calls
    from app.db.models import VentureDocument
    svc, caller, manifest = orders
    monkeypatch.delenv(svc.DESKTOP_EXECUTION_ENV)
    monkeypatch.delenv(svc.GENERAL_BROWSER_EXECUTION_ENV, raising=False)
    browser = svc.Manifest.model_validate({**manifest.model_dump(exclude_none=True),
        "applications": ["com.google.Chrome"], "actions": ["browser.read", "browser.interact"],
        "general_browser_verification": {"operation": "general_goal_v1", "start_url": "https://example.test/"}})
    with pytest.raises(ValueError, match="^device_general_browser_execution_unavailable$"):
        svc.prepare("v_one", caller, browser)
    with svc.db()() as session:
        assert not session.scalar(select(VentureDocument.id).where(VentureDocument.kind == svc.KIND))
    # This positive exercises the explicitly reviewed opt-in route, while the
    # independent generic-native boundary remains unavailable.
    monkeypatch.setenv(svc.GENERAL_BROWSER_EXECUTION_ENV, "1")
    order = svc.prepare("v_one", caller, browser)
    args = ("v_one", caller, order["id"])
    for action in ("approve", "claim"):
        svc.transition(*args, order["review_digest"], action)
    calls.check_current(*args)
    assert calls.admit(*args, invocation_id="browser", request_digest="a" * 64, quote_micro=100)["admitted_now"]


@pytest.mark.parametrize("marker", [None, False, "true", 1])
def test_old_or_unqualified_general_capability_cannot_claim(marker):
    from desktop_runtime.device_gateway import WorkOrderGateway

    requests = []
    review = {"id": "dwo_one", "venture_id": "ven_one", "device_id": "dev_one",
              "actor_id": "usr_fixture", "state": "approved", "review_digest": "a" * 64,
              "manifest": {"general_browser_verification": {"operation": "general_goal_v1"}}}

    def request(method, path, body):
        requests.append((method, path))
        assert method == "GET", "An unqualified capability must refuse before any claim or effect"
        if path.endswith("/capabilities"):
            capabilities = {"available": True}
            if marker is not None:
                capabilities["general_browser_execution_v1"] = marker
            return capabilities
        return copy.deepcopy(review)

    gateway = WorkOrderGateway(request=request, venture_id="ven_one", order_id="dwo_one",
                               device_id="dev_one", account_id="usr_fixture")
    with pytest.raises(PermissionError, match="^native_execution_unavailable$"):
        gateway.claim()
    assert not gateway.claim_attempted and not gateway.claimed
    assert [method for method, _path in requests] == ["GET", "GET"]


@pytest.mark.parametrize("kind", ["plan", "step", "verify"])
def test_retired_native_gate_denies_model_provider_without_new_invocation(ai_order, monkeypatch, kind):
    from tests.unit.product.test_device_ai_step import CTX
    svc, caller, order, ai, sent = ai_order
    monkeypatch.delenv(svc.DESKTOP_EXECUTION_ENV)
    monkeypatch.setattr(ai, "_deepseek", lambda *_a: pytest.fail("planner provider called"))
    before = copy.deepcopy(svc.retrieve("v_one", caller, order["id"]))
    with pytest.raises(ValueError, match="^device_desktop_execution_unavailable$"):
        ai.run("v_one", caller, order["id"], invocation_id="retired_" + kind,
               kind=kind, context=CTX)
    assert sent == [] and svc.retrieve("v_one", caller, order["id"]) == before
