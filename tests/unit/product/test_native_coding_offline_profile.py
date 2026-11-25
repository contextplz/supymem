"""Offline admission is bound source data; invalid cards cause no local effects."""
import copy
import json

import pytest
from sqlalchemy import select

from tests.unit.product.test_device_orders import orders as orders


def static_snapshot():
    return {"task_id": "task_one", "plan_id": "approved_plan", "milestone_id": "milestone_owned",
            "title": "Reviewed static offer", "description": "Implement exact copy",
            "acceptance": '["The reviewed offer is visible"]',
            "artifact_profile": "static_landing_v1",
            "landing_expectations": {"contract": {
                "profile": "static_landing_v1", "product_name": "Offline offer",
                "required_text": ["The reviewed offer"], "actions": []},
                "criteria": [{"criterion": 0, "text_indices": [0], "action_ids": []}]}}


def coding_manifest(svc):
    return svc.Manifest(task_id="task_one", title="Build reviewed static offer",
        instruction="Implement the exact approved static offer; no packages or framework build step.",
        applications=["com.companyos.app"], origins=[], actions=["editor.edit", "terminal.execute"],
        credit_ceiling_micro=0, duration_seconds=60,
        coding_verification={"operation": "workspace_edit_v1", "provider": "codex",
            "workspace_slug": "offline-offer", "read_scope": "device_filesystem",
            "write_scope": "venture_workspace", "command_network": "disabled",
            "offline_profile": "static_html_v1"})


@pytest.mark.parametrize("offer", ["Factory inventory checks", "Weekend garden layouts"])
@pytest.mark.parametrize("case", ["static", "product", "invalid_authority"])
def test_requested_build_uses_profile_caps_without_losing_authority_validation(orders, monkeypatch, offer, case):
    svc, caller, _manifest = orders
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setattr(svc, "_scope", lambda *args: ("w_one", caller.sub, "dev_one"))
    monkeypatch.setattr(svc, "assert_coding_loop_available", lambda *args: None)

    class ReachedPersistence(Exception):
        pass

    def persistence():
        raise ReachedPersistence

    monkeypatch.setattr(svc, "db", persistence)
    descriptor = {"operation": "workspace_loop_v1", "provider": "claude", "workspace_slug": "reviewed-offer",
        "profile": "static_landing_v1", "check_recipe": "static_landing_checks_v1", "phase": "implement",
        "network": "none", "read_scope": "venture_workspace", "write_scope": "venture_workspace",
        "max_iterations": 2, "max_wall_seconds": 120, "per_call_seconds": 60, "max_turns": 24}
    if case == "product":
        descriptor.update(profile="web_app_vercel_v1", check_recipe="web_app_vercel_checks_v1",
            plan_order_id="dwo_plan", plan_digest="b" * 64)
    manifest = svc.Manifest(task_id="task_one", title=offer, instruction="Build only the requested offer.",
        applications=["com.companyos.app"], origins=[], actions=["editor.edit", "terminal.execute"],
        credit_ceiling_micro=0, duration_seconds=120, coding_loop_verification=descriptor)
    authorization = {"request_sha256": "a" * 64, "caps": svc.CodingLoopCaps().model_dump()}
    if case == "invalid_authority":
        authorization["request_sha256"] = "invalid"
    expected = ReachedPersistence if case == "static" else ValueError
    with pytest.raises(expected) as caught:
        svc.prepare("v_one", caller, manifest, requested_build=authorization)
    if case != "static":
        assert str(caught.value) == "device_coding_loop_scope_invalid"


@pytest.mark.parametrize("offer", ["Factory inventory checks", "Weekend garden layouts"])
@pytest.mark.parametrize("mode", ["local", "hosted"])
@pytest.mark.parametrize("size", [10001, 12000, 12001])
@pytest.mark.parametrize("page_brief", [True, False])
def test_coding_focus_uses_the_local_limit_before_any_scope_lookup(orders, monkeypatch, offer, mode, size, page_brief):
    svc, caller, _manifest = orders
    monkeypatch.setenv("SUPYMEM_MODE", mode)

    class ReachedScope(Exception):
        pass

    def scope(*args):
        raise ReachedScope

    monkeypatch.setattr(svc, "_scope", scope)
    prefix = ("Build or edit only your requested landing page using Codex.\n" if page_brief else "Use Codex to edit ") + offer + ": "
    instruction = prefix + "x" * (size - len(prefix))
    expected = ReachedScope if mode == "local" and size <= 12000 else ValueError
    with pytest.raises(expected) as caught:
        svc.prepare_coding_task_from_chat("v_one", caller, instruction)
    if expected is ValueError:
        assert str(caught.value) == "device_coding_focus_too_long"


@pytest.mark.parametrize("offer", ["Factory inventory checks", "Weekend garden layouts"])
def test_complete_local_coding_brief_rejects_oversized_saved_context_before_prepare(orders, monkeypatch, offer):
    svc, caller, _manifest = orders
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setattr(svc, "_scope", lambda *args: ("w_one", caller.sub, "dev_one"))
    snapshot = {**static_snapshot(), "title": offer, "description": "x" * 12000}
    monkeypatch.setattr(svc, "_coding_task_snapshot", lambda *args: snapshot)
    from app.build import native_static_recipe
    monkeypatch.setattr(native_static_recipe, "reviewed_recipe", lambda *args, **kwargs: "Reviewed static recipe")
    with svc.db()() as session:
        session.add(svc.Venture(id="v_one", workspace_id="w_one", name=offer, raw_idea=offer,
            stage="draft", created_at=svc.utcnow(), updated_at=svc.utcnow()))
        session.commit()

    def prepare(*args, **kwargs):
        pytest.fail("Oversized instructions reached work-order persistence")

    monkeypatch.setattr(svc, "prepare", prepare)
    with pytest.raises(ValueError, match="^device_coding_focus_too_long$"):
        svc.prepare_coding_task_from_chat("v_one", caller, "Use Codex to edit " + offer)


@pytest.mark.parametrize("profile", [None, "founder_product_v1", "native_python_product_source_v1"])
@pytest.mark.parametrize("hint", ["web_app.nextjs", "api.fastapi", "realtime.websocket", "build.run"])
def test_framework_or_untyped_task_never_creates_card_or_reservation(orders, monkeypatch, profile, hint):
    svc, caller, _ = orders
    snapshot = {**static_snapshot(), "tool_hints": json.dumps([hint])}
    snapshot["artifact_profile"] = profile
    monkeypatch.setattr(svc, "_task_snapshot", lambda *_args, **_kwargs: snapshot)
    with pytest.raises(ValueError, match="^device_coding_offline_profile_unavailable$"):
        svc.prepare("v_one", caller, coding_manifest(svc))
    from app.db.models import CreditReservation, VentureDocument
    with svc.db()() as session:
        assert not session.scalar(select(VentureDocument.id).where(VentureDocument.kind == svc.KIND))
        assert not session.scalar(select(CreditReservation.id))


@pytest.mark.parametrize("hint", ["web_app.nextjs", "api.fastapi", "realtime.websocket"])
def test_chat_framework_refusal_precedes_recipe_and_card(orders, monkeypatch, hint):
    svc, caller, _ = orders
    with svc.db()() as session:
        session.add(svc.Venture(id="v_one", workspace_id="w_one", name="Framework request",
            raw_idea="Reviewed product", stage="draft", created_at=svc.utcnow(), updated_at=svc.utcnow()))
        session.commit()
    monkeypatch.setattr(svc, "_coding_task_snapshot", lambda *_args: {
        **static_snapshot(), "artifact_profile": None, "tool_hints": json.dumps([hint])})
    def forbidden(*_args, **_kwargs):
        raise AssertionError("unprovisioned task must refuse before recipe/card")
    from app.build import native_static_recipe
    monkeypatch.setattr(native_static_recipe, "reviewed_recipe", forbidden)
    monkeypatch.setattr(svc, "prepare", forbidden)
    with pytest.raises(ValueError, match="^device_coding_offline_profile_unavailable$"):
        svc.prepare_coding_task_from_chat("v_one", caller, "Use Codex to build the approved framework task")


@pytest.mark.parametrize("stage", ["approve", "claim"])
def test_legacy_card_cannot_gain_execution_but_can_be_cancelled(orders, monkeypatch, stage):
    svc, caller, _ = orders
    snapshot = static_snapshot()
    monkeypatch.setattr(svc, "_task_snapshot", lambda *_args, **_kwargs: snapshot)
    body = svc.prepare("v_one", caller, coding_manifest(svc))
    if stage == "claim":
        body = svc.transition("v_one", caller, body["id"], body["review_digest"], "approve")
    from app.db.models import CreditReservation, VentureDocument
    with svc.db()() as session:
        row = session.get(VentureDocument, body["id"])
        legacy = json.loads(row.provenance_json)
        legacy["manifest"]["coding_verification"].pop("offline_profile")
        legacy["review_digest"] = svc._digest(legacy)
        row.provenance_json = json.dumps(legacy)
        session.commit()
    with pytest.raises(ValueError, match="^device_coding_offline_profile_unavailable$"):
        svc.transition("v_one", caller, legacy["id"], legacy["review_digest"], stage)
    saved = svc.retrieve("v_one", caller, legacy["id"])
    assert saved["state"] == ("approved" if stage == "claim" else "pending")
    assert not saved.get("claimed_at") and not saved.get("execution_id")
    cancelled = svc.transition("v_one", caller, legacy["id"], legacy["review_digest"], "cancel")
    assert cancelled["state"] == "cancelled"
    with svc.db()() as session:
        holds = list(session.scalars(select(CreditReservation)))
        assert not holds if stage == "approve" else len(holds) == 1 and holds[0].status == "released"


def test_bound_static_contract_keeps_zero_cost_review_and_claim(orders, monkeypatch):
    svc, caller, _ = orders
    snapshot = static_snapshot()
    monkeypatch.setattr(svc, "_task_snapshot", lambda *_args, **_kwargs: snapshot)
    body = svc.prepare("v_one", caller, coding_manifest(svc))
    assert body["task_snapshot"] == snapshot
    assert body["manifest"]["coding_verification"]["offline_profile"] == "static_html_v1"
    for stage in ("approve", "claim"):
        body = svc.transition("v_one", caller, body["id"], body["review_digest"], stage)
    assert body["state"] == "claimed" and body["execution_id"]


@pytest.mark.parametrize("offer", ["Stock alerts for repair shops", "Planting reminders for home gardeners"])
def test_local_subscription_coding_keeps_stop_custody_without_a_hosted_wallet(orders, monkeypatch, offer):
    from app.build import device_stop
    from app.coding_agents_consent import CodingAgentConsent, ensure_schema
    from app.db.models import CreditAccount, CreditReservation, VentureDocument
    from app.workspaces.resolve import LOCAL_PERSON_ID
    from desktop_runtime import agent_status
    from dataclasses import replace

    svc, caller, _ = orders
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    caller = replace(caller, sub=LOCAL_PERSON_ID)
    monkeypatch.setattr(agent_status, "statuses", lambda: [{"name": "codex", "installed": True,
        "signed_in": True, "subscription_login": True, "available": True}])
    snapshot = static_snapshot()
    snapshot["title"] = offer
    monkeypatch.setattr(svc, "_task_snapshot", lambda *_args, **_kwargs: snapshot)
    with svc.db()() as session:
        ensure_schema(session.get_bind())
        session.add(CodingAgentConsent(id="native_consent", founder_id=caller.sub, provider="codex", consented_at=svc.utcnow()))
        session.delete(session.get(CreditAccount, "cra_device_test"))
        session.commit()
    body = svc.prepare("v_one", caller, coding_manifest(svc))
    for stage in ("approve", "claim"):
        body = svc.transition("v_one", caller, body["id"], body["review_digest"], stage)
    stopped = device_stop.record("v_one", caller, body["id"], execution_id=body["execution_id"],
        evidence_digest="a" * 64, processes_stopped=True)
    assert stopped["state"] == "stopped"
    assert "reservation_id" not in body
    with svc.db()() as session:
        saved = json.loads(session.get(VentureDocument, body["id"]).provenance_json)
        assert saved["budget_source"] == "local" and not saved.get("reservation_id")
        assert saved["workspace_id"] == "w_one" and saved["venture_id"] == "v_one"
        assert saved["execution_id"] == body["execution_id"]
        assert session.scalar(select(CreditReservation.id).where(CreditReservation.request_id == body["id"])) is None
        assert session.scalar(select(CreditAccount.id).where(CreditAccount.scope == "device", CreditAccount.workspace_id == "w_one")) is None


def test_native_offline_refusal_copy_is_actionable_without_claiming_missing_permissions(orders):
    from app.api.device_orders_api import CONFLICTS, _conflict_detail, call
    from fastapi import HTTPException
    code = "device_coding_offline_profile_unavailable"
    assert code in CONFLICTS
    message = _conflict_detail(code)["message"]
    assert "plain HTML, CSS and JavaScript" in message
    assert "Historical coding cards need a fresh supported review" in message
    assert "reserve credits" in message and "permission" not in message.lower()
    def reject():
        raise ValueError(code)
    with pytest.raises(HTTPException) as refused:
        call(reject)
    assert refused.value.status_code == 409
    assert refused.value.detail == {"code": code, "message": message}
    svc, _, _ = orders
    assert any("approved static landing" in item for item in svc.SUPPORTED_MAC_TASKS)
    assert not any("current approved coding task" in item for item in svc.SUPPORTED_MAC_TASKS)


@pytest.mark.parametrize("fault", ["missing_profile", "untyped", "personal", "wrong_contract", "broken_contract", "framework"])
def test_fresh_native_workspace_refuses_before_any_provider_or_claim(orders, monkeypatch, tmp_path, fault):
    svc, _, _ = orders
    from desktop_runtime import device_worker
    saved = {"state": "approved", "task_snapshot": static_snapshot(),
             "manifest": coding_manifest(svc).model_dump(exclude_none=True)}
    if fault == "missing_profile":
        saved["manifest"]["coding_verification"].pop("offline_profile")
    elif fault == "untyped":
        saved["task_snapshot"].pop("artifact_profile")
    elif fault == "personal":
        saved["task_snapshot"] = {"kind": "personal_build", "revision_id": "owned_revision"}
    elif fault == "wrong_contract":
        saved["task_snapshot"]["landing_expectations"]["contract"]["profile"] = "founder_product_v1"
    elif fault == "broken_contract":
        saved["task_snapshot"]["landing_expectations"] = []
    else:
        saved["task_snapshot"]["tool_hints"] = '["web_app.nextjs"]'
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path / "data"))
    def forbidden(*_args, **_kwargs):
        raise AssertionError("refusal must precede tracker/provider construction and filesystem effects")
    monkeypatch.setattr(device_worker, "OwnedCoding", forbidden)
    monkeypatch.setattr(device_worker, "OwnedProcesses", forbidden)
    class Gateway:
        order_id, venture_id = "dwo_offline", "ven_offline"
        claim_attempted, execution_id = False, None
        def read(self):
            return copy.deepcopy(saved)
        claim = forbidden
    worker = device_worker.DeviceWorker(Gateway(), lambda _receipt: None)
    worker.run()
    assert worker.last["state"] == "not_started"
    assert worker.last["failure_code"] == "coding_offline_profile_unavailable"
    assert not worker.gateway.claim_attempted
    assert not (tmp_path / "Documents").exists()


@pytest.mark.parametrize("hint", ["web_app.nextjs", "api.fastapi", "realtime.websocket", "unprovisioned.framework"])
def test_static_label_cannot_override_a_framework_capability(orders, monkeypatch, hint):
    svc, caller, _ = orders
    snapshot = {**static_snapshot(), "tool_hints": json.dumps([hint])}
    monkeypatch.setattr(svc, "_task_snapshot", lambda *_args, **_kwargs: snapshot)
    with pytest.raises(ValueError, match="^device_coding_offline_profile_unavailable$"):
        svc.prepare("v_one", caller, coding_manifest(svc))


@pytest.mark.parametrize("account_id", [None, "owned_account"])
def test_worker_notices_carry_only_the_pinned_gateway_actor(monkeypatch, account_id):
    from types import SimpleNamespace
    from desktop_runtime import device_worker
    seen = []
    def capture(**body):
        seen.append(body)
    monkeypatch.setattr(device_worker.local_notices, "push_task_finished", capture)
    monkeypatch.setattr(device_worker.local_notices, "push_needs_you", capture)
    gateway = SimpleNamespace(order_id="dwo_notice", venture_id="ven_notice", claim_attempted=True,
                              execution_id="dex_notice", report_needs_you=lambda *_args: None)
    if account_id is not None:
        gateway.account_id = account_id
    worker = device_worker.DeviceWorker(gateway, lambda _receipt: None)
    worker._notify_terminal("cancelled", manifest={"title": "Owned task"})
    worker._notify_needs_you("Owned input required")
    assert len(seen) == 2
    assert [item["actor_id"] for item in seen] == [account_id, account_id]
    assert [item["order_id"] for item in seen] == ["dwo_notice", "dwo_notice"]
