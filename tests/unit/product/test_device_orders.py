"""Work-order persistence and state admission, not native executor acceptance."""
import json
from dataclasses import replace

import pytest
from pydantic import ValidationError

from tests.unit.product.test_computer_use_jobs import _client


@pytest.fixture(autouse=True)
def explicit_native_admission(monkeypatch):
    monkeypatch.setenv("SUPYMEM_MODE", "hosted")
    # These durable native scope tests intentionally exercise the opt-in path.
    monkeypatch.setenv("DEVICE_DESKTOP_EXECUTION_ENABLED", "1")
    original = _client
    def client_with_owned_device(*args):
        client = original(*args)
        from app.build import device_orders as service
        from app.workspaces.resolve import LOCAL_PERSON_ID
        _pilot_device(service, monkeypatch, LOCAL_PERSON_ID)
        return client
    monkeypatch.setattr(__import__(__name__, fromlist=["_client"]), "_client", client_with_owned_device)


def _pilot_device(service, monkeypatch, actor):
    from app.build.device_desktop_pilot import ALLOWLIST_ENV
    from app.db.supymem_models import DesktopDevice
    monkeypatch.setenv(ALLOWLIST_ENV, json.dumps([[actor, "dev_one"]]))
    with service.db()() as session:
        row = session.get(DesktopDevice, "dev_one")
        if row is None:
            row = DesktopDevice(id="dev_one", person_id=actor, name="Synthetic owned Mac", platform="macos")
            session.add(row)
        row.person_id = actor
        row.app_version = "0.1.84"
        session.commit()


@pytest.fixture
def orders(tmp_path, monkeypatch):
    monkeypatch.setenv("SUPYMEM_MODE", "hosted")
    # This fixture is imported by the separate provider/admission test modules.
    monkeypatch.setenv("DEVICE_DESKTOP_EXECUTION_ENABLED", "1")
    _client(tmp_path, monkeypatch)
    from app.admin.authn import CallerContext
    from app.build import device_orders as service
    _pilot_device(service, monkeypatch, "founder")
    from app.db.models import CreditAccount
    with service.db()() as session:
        session.add(CreditAccount(id="cra_device_test", workspace_id="w_one", venture_id="v_one",
            scope="venture", granted_micro=10000, spent_micro=0, reserved_micro=0, status="active",
            created_at=service.utcnow(), updated_at=service.utcnow()))
        session.commit()
    from app.build import device_desktop_pause
    monkeypatch.setattr(device_desktop_pause, '_lock_workspace', lambda *args: None)
    caller = CallerContext("founder", "", "user", "/test", "POST", 9999999999, "test", device_id="dev_one")
    # This unit fixture isolates durable transitions. Membership enforcement uses
    # the existing assert_venture_access boundary and needs separate API tests.
    monkeypatch.setattr(service, "assert_venture_access", lambda vid, sub: (vid, "w_one"))
    monkeypatch.setattr(service, "subject_person_id", lambda sub: sub)
    monkeypatch.setattr(service, "_lock_venture", lambda *args: None)
    monkeypatch.setattr(service, "authorize", lambda *args, **kwargs: None)
    monkeypatch.setattr(service, "_task_snapshot", lambda *args, **kwargs: {"task_id": "task_one", "plan_id": "approved_plan"})
    manifest = service.Manifest(task_id="task_one", title="Review product", instruction="Read the approved page",
        applications=["com.apple.Safari"], origins=["https://example.test"], actions=["browser.read"],
        credit_ceiling_micro=1000, duration_seconds=60)
    return service, caller, manifest


@pytest.mark.parametrize("domain", ["Factory safety checklists", "Family cycling planner"])
def test_local_owner_has_stable_device_authority_without_hosted_registration(tmp_path, monkeypatch, domain):
    from fastapi import HTTPException
    from core import local_security
    from secrets import token_hex

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    client = _client(tmp_path, monkeypatch)
    from app.admin.authn import CallerContext
    from app.workspaces.resolve import LOCAL_PERSON_ID
    response = client.post("/ventures", json={"raw_idea": domain, "start": False})
    assert response.status_code == 201, response.text
    venture_id = response.json()["id"]
    from app.build import device_orders as svc
    from app.db.supymem_models import DesktopDevice
    with svc.db()() as session:
        session.delete(session.get(DesktopDevice, "dev_one"))
        session.commit()
    caller = CallerContext(LOCAL_PERSON_ID, "", "user", "/test", "GET", 0, "")
    scope = svc._scope(venture_id, caller)
    assert scope[1] == LOCAL_PERSON_ID and scope[2]
    assert svc._scope(venture_id, caller) == scope
    listed = client.get(f"/ventures/{venture_id}/device-work-orders")
    assert listed.status_code == 200 and listed.json() == {"orders": []}
    with svc.db()() as session:
        assert session.get(DesktopDevice, "dev_one") is None
    rotated_secret = token_hex(32).encode()
    monkeypatch.setattr(local_security, "_proxy_secret", lambda: rotated_secret)
    assert svc._scope(venture_id, caller)[2] != scope[2]
    assert client.get(f"/ventures/{venture_id}/device-work-orders").status_code in {401, 403}
    with pytest.raises((KeyError, PermissionError, HTTPException)):
        svc._scope(venture_id, replace(caller, sub="other_owner"))
    with pytest.raises(PermissionError):
        svc._scope(venture_id, replace(caller, legacy=True))
    with pytest.raises((KeyError, HTTPException)):
        svc._scope("missing_venture", caller)


@pytest.mark.parametrize("domain", ["Supplier invoice reviews", "Family recipe journals"])
@pytest.mark.parametrize("ending", ["stop", "unknown", "startup", "lease"])
def test_local_device_execution_without_a_wallet_keeps_custody(orders, monkeypatch, domain, ending):
    from datetime import datetime, timedelta
    from sqlalchemy import select

    from app.build import device_invocations as calls, device_stop
    from app.db.models import CreditAccount, CreditReservation

    svc, caller, manifest = orders
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    with svc.db()() as session:
        session.delete(session.get(CreditAccount, "cra_device_test"))
        session.commit()
    manifest.title = domain
    order = svc.prepare("v_one", caller, manifest)
    args = ("v_one", caller, order["id"])
    with pytest.raises(ValueError):
        svc.transition(*args, order["review_digest"], "claim")
    approved = svc.transition(*args, order["review_digest"], "approve")
    claimed = svc.transition(*args, approved["review_digest"], "claim")
    assert not claimed.get("reservation_id")
    with pytest.raises(ValueError):
        svc.transition(*args, claimed["review_digest"], "claim")
    for other in (replace(caller, sub="other"), replace(caller, device_id="other")):
        with pytest.raises(KeyError):
            calls.check_current("v_one", other, order["id"])
    with pytest.raises(ValueError, match="device_invocation_budget_exhausted"):
        calls.admit(*args, invocation_id="over_budget", request_digest="a" * 64, quote_micro=1001)
    receipt = {"execution_id": claimed["execution_id"], "evidence_digest": "b" * 64,
               "processes_stopped": True}
    if ending != "startup":
        call = calls.admit(*args, invocation_id="local_call", request_digest="a" * 64, quote_micro=100)
        assert call["admitted_now"]
        assert not calls.admit(*args, invocation_id="local_call", request_digest="a" * 64,
                               quote_micro=100)["admitted_now"]
        if ending == "stop":
            calls.record_result(*args, invocation_id="local_call", actual_micro=42)
            stopped = device_stop.record(*args, **receipt)
            assert stopped["state"] == "stopped" and stopped["settled_micro"] == 42
            assert device_stop.record(*args, **receipt) == stopped
        elif ending == "unknown":
            svc.transition(*args, claimed["review_digest"], "cancel")
            pending = device_stop.record(*args, **receipt)
            assert pending["stop_reconciliation_required"]
            with pytest.raises(ValueError, match="too_early"):
                device_stop.settle_cancelled_unknown_usage(*args, execution_id=receipt["execution_id"],
                                                         evidence_digest=receipt["evidence_digest"])
            monkeypatch.setattr(svc, "utcnow", lambda: datetime.fromisoformat(call["started_at"]) + timedelta(seconds=61))
            stopped = device_stop.settle_cancelled_unknown_usage(*args, execution_id=receipt["execution_id"],
                                                               evidence_digest=receipt["evidence_digest"])
            assert stopped["state"] == "stopped" and stopped["settled_micro"] == 100
            assert stopped["unknown_usage_reconciliation"]["actual_usage_unconfirmed"]
            assert not stopped["unknown_usage_reconciliation"]["effect_replayed"]
            assert device_stop.settle_cancelled_unknown_usage(*args, execution_id=receipt["execution_id"],
                                                            evidence_digest=receipt["evidence_digest"]) == stopped
        else:
            cutoff = datetime.fromisoformat(claimed["claimed_at"]) + timedelta(seconds=svc.device_hold_max_age_seconds() + 1)
            assert svc.sweep_device_leases(cutoff) == 1
            assert svc.sweep_device_leases(cutoff) == 0
            saved = svc.retrieve(*args)
            assert saved["state"] == "cancelled" and saved["settled_micro"] == 100
            assert saved["lease_reconciliation"]["actual_usage_unconfirmed"]
            assert not saved["lease_reconciliation"]["process_cleanup_proven"]
    else:
        svc.transition(*args, claimed["review_digest"], "cancel")
        receipt["processes_stopped"] = False
        pending = device_stop.record(*args, **receipt)
        assert pending["stop_reconciliation_required"]
        stopped = device_stop.reconcile_startup_failure(*args, execution_id=receipt["execution_id"],
                                                       evidence_digest=receipt["evidence_digest"])
        assert stopped["state"] == "stopped" and stopped["settled_micro"] == 0
        assert not stopped["startup_reconciliation"]["process_cleanup_proven"]
        assert device_stop.reconcile_startup_failure(*args, execution_id=receipt["execution_id"],
                                                    evidence_digest=receipt["evidence_digest"]) == stopped
    with pytest.raises(ValueError):
        calls.check_current(*args)
    with svc.db()() as session:
        assert not session.scalar(select(CreditAccount.id))
        assert not session.scalar(select(CreditReservation.id))


def test_exact_device_review_single_claim_and_cancel(orders):
    svc, caller, manifest = orders
    order = svc.prepare("v_one", caller, manifest)
    oid, digest = order["id"], order["review_digest"]
    with pytest.raises(ValueError):
        svc.transition("v_one", caller, oid, digest, "claim")
    with pytest.raises(ValueError):
        svc.transition("v_one", caller, oid, "different", "approve")
    for other in [replace(caller, device_id="dev_two"), replace(caller, sub="other")]:
        with pytest.raises(KeyError):
            svc.transition("v_one", other, oid, digest, "approve")
    with pytest.raises(KeyError):
        svc.transition("v_two", caller, oid, digest, "approve")
    assert svc.transition("v_one", caller, oid, digest, "approve")["state"] == "approved"
    claimed = svc.transition("v_one", caller, oid, digest, "claim")
    assert claimed["manifest"] == manifest.model_dump(exclude_none=True)
    with pytest.raises(ValueError):
        svc.transition("v_one", caller, oid, digest, "claim")
    assert svc.transition("v_one", caller, oid, digest, "cancel")["state"] == "cancelled"
    with pytest.raises(ValueError):
        svc.transition("v_one", caller, oid, digest, "claim")


def test_founder_mac_request_can_start_before_a_plan_exists(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    venture = client.post("/ventures", json={"raw_idea": "Private Mac control QA", "start": False})
    assert venture.status_code == 201
    vid = venture.json()["id"]
    from app.admin.authn import CallerContext
    from app.build import device_orders as svc, device_invocations as calls
    from app.credits.accounts import ensure_account

    ensure_account(vid)
    caller = CallerContext("founder", "", "user", "/test", "POST", 9999999999,
                           "test", device_id="dev_one")
    instruction = ("Use Brave to open https://example.org/ and report the page title. "
                   "Do not type private data or visit https://other.example/.")
    order = svc.prepare_desktop_from_chat(
        vid, caller, instruction,
        "com.brave.Browser", 123, "123456789", 44, "Search")
    assert order["task_snapshot"]["kind"] == "founder_mac_request"
    from desktop_runtime.cu_reviewed_scope import ReviewedDesktopScope
    scope = ReviewedDesktopScope.from_manifest(order['manifest'])
    assert order["manifest"]["desktop_verification"]["delivery_mode"] == "background"
    assert order["manifest"]["desktop_verification"]["founder_goal"] == instruction
    reviewed_text = order["manifest"]["desktop_verification"]["reviewed_text"]
    assert "https://example.org/" in reviewed_text
    assert "private data" not in reviewed_text
    assert "https://other.example/" not in reviewed_text
    assert "Control runs in the background" in order["manifest"]["instruction"]
    assert "may bring" not in order["manifest"]["instruction"]
    assert scope.pairs == ((123,44),)
    import copy
    old_signed_manifest = copy.deepcopy(order['manifest'])
    old_signed_manifest['desktop_verification']['delivery_mode'] = 'foreground_fallback'
    with pytest.raises(ValueError, match='desktop_scope_unsupported'):
        ReviewedDesktopScope.from_manifest(old_signed_manifest)
    assert svc._digest({**order,'manifest':old_signed_manifest}) != order['review_digest']
    with pytest.raises(ValueError, match="device_task_not_ready"):
        svc.prepare(vid, caller, svc.Manifest(task_id=order["manifest"]["task_id"],
            title="Unrelated work", instruction="Read a page", applications=["com.apple.Safari"],
            origins=[], actions=["browser.read"], credit_ceiling_micro=1000,
            duration_seconds=60))
    for action in ("approve", "claim"):
        svc.transition(vid, caller, order["id"], order["review_digest"], action)
    calls.check_current(vid, caller, order["id"])


def test_direct_use_my_mac_request_stays_pending_until_exact_review_approval(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    venture = client.post("/ventures", json={"raw_idea": "Read-only Mac task QA", "start": False})
    assert venture.status_code == 201
    vid = venture.json()["id"]
    from app.admin.authn import CallerContext
    from app.build import device_orders as svc, device_invocations as calls
    from app.credits.accounts import ensure_account

    ensure_account(vid)
    caller = CallerContext("founder", "", "user", "/test", "POST", 9999999999,
                           "test", device_id="dev_one")
    instruction = ("Use my Mac to open Chrome and describe the current window. "
                   "This is read-only: do not edit, submit forms, or send messages.")
    assert not svc.browser_task_request(instruction)
    order = svc.prepare_from_chat(vid, caller, instruction, "com.google.Chrome", 77,
                                  "123456789", 8, "New Tab")
    assert order["state"] == "pending"
    assert order["id"] and order["review_digest"]
    assert order["manifest"]["desktop_verification"]["founder_goal"] == instruction
    assert order["manifest"]["applications"] == ["com.google.Chrome"]
    assert order["manifest"]["desktop_verification"]["window_id"] == 8
    assert order["manifest"]["desktop_verification"]["process_start_identity"] == "123456789"
    with pytest.raises(ValueError):
        calls.check_current(vid, caller, order["id"])
    with pytest.raises(ValueError):
        svc.transition(vid, caller, order["id"], order["review_digest"], "claim")
    with pytest.raises(ValueError):
        svc.transition(vid, caller, order["id"], "historical-review-digest", "approve")
    assert svc.retrieve(vid, caller, order["id"])["state"] == "pending"


def test_condition_first_named_app_request_prepares_exact_review_card(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    venture = client.post("/ventures", json={"raw_idea": "Conditional Mac task QA", "start": False})
    assert venture.status_code == 201
    vid = venture.json()["id"]
    from app.admin.authn import CallerContext
    from app.build import device_orders as svc
    from app.credits.accounts import ensure_account

    ensure_account(vid)
    caller = CallerContext("founder", "", "user", "/test", "POST", 9999999999,
                           "test", device_id="dev_one")
    instruction = ("If TextEdit is showing an Open dialog, click Cancel and report which window "
                   "remains. If not, only describe what you see. Do not open or edit a document.")
    order = svc.prepare_from_chat(vid, caller, instruction, "com.apple.TextEdit", 123,
                                  "123456789", 44, "TextEdit — Open")
    assert order["state"] == "pending"
    assert order["review_digest"]
    manifest = order["manifest"]
    assert manifest["applications"] == ["com.apple.TextEdit"]
    assert manifest["actions"] == ["desktop.read", "desktop.interact"]
    assert manifest["desktop_verification"]["founder_goal"] == instruction
    assert manifest["desktop_verification"]["window_id"] == 44
    assert manifest["desktop_verification"]["window_title"] == "TextEdit — Open"


def test_explicit_desktop_end_condition_is_review_bound_but_not_typed(tmp_path, monkeypatch):
    _client(tmp_path, monkeypatch)
    from app.build import device_orders, device_stop

    monkeypatch.setattr(device_orders, "_scope", lambda *_args: ("w_one", "founder", "dev_one"))
    monkeypatch.setattr(device_orders, "prepare", lambda _vid, _caller, manifest, **_kwargs: {
        "manifest": manifest.model_dump(exclude_none=True),
    })
    instruction = 'Open an installed app, work through two steps, then verify the exact visible result "Completed".'
    assert device_orders._desktop_expected_visible_text(instruction) == "Completed"
    prepared = device_orders.prepare_desktop_from_chat(
        "v_one", object(), instruction, "com.example.app", 123, "123456", 44, "Example")
    verification = prepared["manifest"]["desktop_verification"]
    assert verification["expected_visible_text"] == "Completed"
    assert "Completed" not in verification["reviewed_text"]
    assert device_orders._desktop_expected_visible_text(
        'Do not verify the exact visible result "Completed"') is None
    assert device_orders._desktop_expected_visible_text(
        'Verify the exact visible result "One" and confirm the exact visible text "Two"') is None
    assert device_stop._verification_kind({"desktop_verification": {
        "operation": "desktop_goal_v1", "expected_visible_text": "Completed"}}) == "desktop_observed_text_v1"
    assert device_stop._verification_kind({"desktop_verification": {
        "operation": "desktop_goal_v1"}}) is None


@pytest.mark.parametrize("invocations", [{}, {"empty": {}}, {"unfinished": {
    "state": "calling", "request_digest": "b" * 64, "actual_micro": 0}},
    {"uncertain": {"state": "completed", "request_digest": "b" * 64, "actual_micro": 0,
        "effect": {"state": "uncertain"}}}])
def test_verified_stop_requires_completed_execution_evidence(orders, invocations):
    from app.build import device_stop
    from app.db.models import VentureDocument

    svc, caller, manifest = orders
    reviewed = svc.Manifest.model_validate({**manifest.model_dump(exclude_none=True),
        "browser_verification": {"expected_locations": ["https://example.test"]}})
    order = svc.prepare("v_one", caller, reviewed)
    for action in ("approve", "claim"):
        order = svc.transition("v_one", caller, order["id"], order["review_digest"], action)
    with svc.db()() as session:
        row = session.get(VentureDocument, order["id"])
        body = json.loads(row.provenance_json)
        body["invocations"] = invocations
        row.provenance_json = json.dumps(body)
        session.commit()
    before = svc.retrieve("v_one", caller, order["id"])
    with pytest.raises(ValueError, match="device_stop_verification_invalid"):
        device_stop.record("v_one", caller, order["id"], execution_id=order["execution_id"],
            evidence_digest="a" * 64, processes_stopped=True, task_verified=True)
    assert svc.retrieve("v_one", caller, order["id"]) == before


def test_completed_execution_evidence_allows_idempotent_verified_stop(orders, monkeypatch):
    monkeypatch.delenv("SUPYMEM_MODE", raising=False)
    from app.build import device_invocations, device_stop

    svc, caller, manifest = orders
    reviewed = svc.Manifest.model_validate({**manifest.model_dump(exclude_none=True),
        "browser_verification": {"expected_locations": ["https://example.test"]}})
    order = svc.prepare("v_one", caller, reviewed)
    for action in ("approve", "claim"):
        order = svc.transition("v_one", caller, order["id"], order["review_digest"], action)
    device_invocations.admit("v_one", caller, order["id"], invocation_id="verified_call",
        request_digest="b" * 64, quote_micro=1)
    device_invocations.record_result("v_one", caller, order["id"], invocation_id="verified_call", actual_micro=0)
    receipt = {"execution_id": order["execution_id"], "evidence_digest": "a" * 64,
        "processes_stopped": True, "task_verified": True}
    first = device_stop.record("v_one", caller, order["id"], **receipt)
    second = device_stop.record("v_one", caller, order["id"], **receipt)
    assert first == second and first["state"] == "stopped"
    assert first["native_stop"]["task_verified"] is True
    from app.db.models import AnalyticsEvent
    with svc.db()() as session:
        rows = session.query(AnalyticsEvent).filter_by(event_name="agent.computer_outcome").all()
        assert len(rows) == 1 and json.loads(rows[0].props_json) == {"outcome": "ok"}


def test_empty_execution_can_stop_without_verifying_the_goal(orders, monkeypatch):
    monkeypatch.delenv("SUPYMEM_MODE", raising=False)
    from app.build import device_stop

    svc, caller, manifest = orders
    order = svc.prepare("v_one", caller, manifest)
    for action in ("approve", "claim"):
        order = svc.transition("v_one", caller, order["id"], order["review_digest"], action)
    stopped = device_stop.record("v_one", caller, order["id"], execution_id=order["execution_id"],
        evidence_digest="a" * 64, processes_stopped=True, task_verified=False)
    assert stopped["state"] == "stopped"
    assert stopped["native_stop"].get("task_verified") is not True
    from app.db.models import AnalyticsEvent
    with svc.db()() as session:
        rows = session.query(AnalyticsEvent).filter_by(event_name="agent.computer_outcome").all()
        assert len(rows) == 1 and json.loads(rows[0].props_json) == {"outcome": "needs_founder"}


def test_readonly_workspace_inspection_cannot_grant_an_edit(orders):
    svc, _caller, _manifest = orders
    valid = {"task_id": "task_one", "title": "Read workspace", "instruction": "List files only",
        "applications": ["com.companyos.app"], "origins": [], "actions": ["editor.read"],
        "inspection_verification": {"operation": "workspace_inspect_v1", "provider": "cursor",
            "workspace_slug": "venture-local", "read_scope": "venture_workspace", "write_scope": "none"},
        "credit_ceiling_micro": 10000, "duration_seconds": 180}
    assert svc.Manifest(**valid).inspection_verification.write_scope == "none"
    for patch in [
        {"actions": ["editor.read", "editor.edit"]},
        {"actions": ["editor.edit"]},
        {"applications": ["com.todesktop.230313mzl4w4u92"]},
        {"inspection_verification": {**valid["inspection_verification"], "provider": "codex"}},
        {"inspection_verification": {**valid["inspection_verification"], "write_scope": "venture_workspace"}},
    ]:
        with pytest.raises(ValidationError):
            svc.Manifest(**{**valid, **patch})


def test_app_self_navigation_requires_exact_own_bundle_and_route(orders):
    svc, _caller, _manifest = orders
    valid = {"task_id": "task_one", "title": "Open Builds", "instruction": "Navigate only Supymem",
        "applications": ["com.companyos.app"], "origins": [], "actions": ["app.navigate"],
        "app_navigation_verification": {"operation": "open_venture_screen_v1", "route": "builds"},
        "credit_ceiling_micro": 10000, "duration_seconds": 120}
    assert svc.Manifest(**valid).app_navigation_verification.route == "builds"
    for patch in [
        {"applications": ["com.google.Chrome"]},
        {"actions": ["app.navigate", "browser.read"]},
        {"origins": ["https://app.supymem.com"]},
        {"app_navigation_verification": {"operation": "open_venture_screen_v1", "route": "people"}},
        {"app_navigation_verification": None},
    ]:
        with pytest.raises(ValidationError):
            svc.Manifest(**{**valid, **patch})


def test_expired_and_nondevice_orders_cannot_authorize(orders):
    svc, caller, manifest = orders
    for other in [replace(caller, device_id=None), replace(caller, legacy=True), replace(caller, role="admin")]:
        with pytest.raises(PermissionError):
            svc.prepare("v_one", other, manifest)
    order = svc.prepare("v_one", caller, manifest)
    from app.db.models import VentureDocument
    with svc.db()() as session:
        row = session.get(VentureDocument, order["id"])
        saved = json.loads(row.provenance_json)
        saved["expires_at"] = "2000-01-01T00:00:00"
        row.provenance_json = json.dumps(saved)
        session.commit()
    with pytest.raises(ValueError, match="expired"):
        svc.transition("v_one", caller, order["id"], order["review_digest"], "approve")


@pytest.mark.parametrize("patch", [{"applications": ["*"]}, {"origins": ["https://x.test/path"]},
    {"origins": ["https://user:secret@x.test"]}, {"origins": ["https://*.test"]},
    {"actions": ["anything"]}, {"credit_ceiling_micro": -1}, {"duration_seconds": 1801},
    {"browser_verification": {"expected_locations": ["https://outside.test/"]}},
    {"browser_verification": {"expected_locations": ["http://example.test/"]}},
    {"argv": ["sh", "-c", "anything"]}])
def test_unbounded_manifest_rejected(orders, patch):
    svc, _, manifest = orders
    with pytest.raises(ValidationError):
        svc.Manifest.model_validate({**manifest.model_dump(), **patch})


def test_terminal_manifest_is_one_fixed_owned_workspace_operation(orders):
    svc, _, manifest = orders
    valid = {**manifest.model_dump(), "applications": ["com.companyos.app"], "origins": [],
        "actions": ["terminal.execute"], "terminal_verification": {
            "operation": "git_status_v1", "workspace_slug": "labhire"}}
    parsed = svc.Manifest.model_validate(valid)
    assert parsed.terminal_verification.operation == "git_status_v1"
    for patch in [
        {"terminal_verification": {"operation": "shell_anything", "workspace_slug": "labhire"}},
        {"terminal_verification": {"operation": "git_status_v1", "workspace_slug": "../other"}},
        {"terminal_verification": None},
        {"origins": ["https://example.test"]},
    ]:
        with pytest.raises(ValidationError):
            svc.Manifest.model_validate({**valid, **patch})
    assert svc._workspace_slug("Lab Hire!", "ven_abc123") == "lab-hire-abc123"
    assert len(svc._workspace_slug("x" * 100, "ven_abc123")) <= 64


def test_coding_manifest_binds_provider_and_owned_workspace(orders):
    svc, _, manifest = orders
    valid = {**manifest.model_dump(), "applications": ["com.companyos.app"], "origins": [],
        "actions": ["editor.edit", "terminal.execute"], "coding_verification": {
            "operation": "workspace_edit_v1", "provider": "codex", "workspace_slug": "labhire",
            "read_scope": "device_filesystem", "write_scope": "venture_workspace",
            "command_network": "disabled"}}
    parsed = svc.Manifest.model_validate(valid)
    assert parsed.coding_verification.provider == "codex"
    for patch in [
        {"coding_verification": {"operation": "shell_anything", "provider": "codex",
                                 "workspace_slug": "labhire", "read_scope": "device_filesystem",
                                 "write_scope": "venture_workspace", "command_network": "disabled"}},
        {"coding_verification": {"operation": "workspace_edit_v1", "provider": "unknown",
                                 "workspace_slug": "labhire", "read_scope": "device_filesystem",
                                 "write_scope": "venture_workspace", "command_network": "disabled"}},
        {"coding_verification": {"operation": "workspace_edit_v1", "provider": "codex",
                                 "workspace_slug": "../other", "read_scope": "device_filesystem",
                                 "write_scope": "venture_workspace", "command_network": "disabled"}},
        {"coding_verification": {"operation": "workspace_edit_v1", "provider": "codex",
                                 "workspace_slug": "labhire", "read_scope": "venture_workspace",
                                 "write_scope": "venture_workspace", "command_network": "disabled"}},
        {"coding_verification": None},
        {"actions": ["editor.edit"]},
    ]:
        with pytest.raises(ValidationError):
            svc.Manifest.model_validate({**valid, **patch})


@pytest.mark.parametrize("value", ["not-json", "{}", '["", "valid"]', '[1]'])
def test_coding_acceptance_requires_nonempty_string_list(orders, value):
    svc, _, _ = orders
    with pytest.raises(ValueError, match="device_coding_task_invalid"):
        svc._acceptance_criteria(value)
    assert svc._acceptance_criteria(None) == []
    assert svc._acceptance_criteria('["A real acceptance criterion"]') == ["A real acceptance criterion"]


def test_changed_manifest_requires_review_and_expiry_does_not_prevent_cancel(orders):
    svc, caller, manifest = orders
    order = svc.prepare("v_one", caller, manifest)
    from app.db.models import VentureDocument
    with svc.db()() as session:
        row = session.get(VentureDocument, order["id"])
        saved = json.loads(row.provenance_json)
        saved["manifest"]["instruction"] = "Different work"
        row.provenance_json = json.dumps(saved)
        session.commit()
    with pytest.raises(ValueError, match="review_changed"):
        svc.transition("v_one", caller, order["id"], order["review_digest"], "approve")
    with svc.db()() as session:
        row = session.get(VentureDocument, order["id"])
        saved = json.loads(row.provenance_json)
        saved["expires_at"] = "2000-01-01T00:00:00"
        row.provenance_json = json.dumps(saved)
        session.commit()
    assert svc.transition("v_one", caller, order["id"], order["review_digest"], "cancel")["state"] == "cancelled"


def test_duplicate_preparation_reuses_review_but_claimed_cancel_cannot_restart(orders):
    svc, caller, manifest = orders
    order = svc.prepare("v_one", caller, manifest)
    assert svc.prepare("v_one", caller, manifest) == order
    with pytest.raises(svc.ActiveDeviceOrder, match="active_order") as blocked:
        svc.prepare("v_one", replace(caller, device_id="dev_other"), manifest)
    assert blocked.value.order["id"] == order["id"]
    args = ("v_one", caller, order["id"], order["review_digest"])
    svc.transition(*args, "approve")
    svc.transition(*args, "claim")
    svc.transition(*args, "cancel")
    with pytest.raises(ValueError, match="active_order"):
        svc.prepare("v_one", caller, manifest)


def test_reconnect_reads_saved_state_without_renewing_or_replaying(orders, monkeypatch):
    from datetime import timedelta
    svc, caller, manifest = orders
    order = svc.prepare("v_one", caller, manifest)
    args = ("v_one", caller, order["id"], order["review_digest"])
    assert svc.retrieve(*args[:3]) == order
    svc.transition(*args, "approve")
    claimed = svc.transition(*args, "claim")
    assert svc.retrieve(*args[:3]) == claimed
    future = svc.utcnow() + timedelta(hours=25)
    monkeypatch.setattr(svc, "utcnow", lambda: future)
    # Recovery still shows the original deadline and claim after its expiry.
    assert svc.retrieve(*args[:3]) == claimed
    with pytest.raises(KeyError):
        svc.retrieve("v_one", replace(caller, device_id="dev_other"), order["id"])
    with pytest.raises(ValueError, match="expired"):
        svc.transition(*args, "claim")
    cancelled = svc.transition(*args, "cancel")
    assert svc.retrieve(*args[:3]) == cancelled
    assert cancelled["claimed_at"] == claimed["claimed_at"]
    # A rotated device can observe this cancelled claim, never execute it.
    assert svc.retrieve("v_one", replace(caller, device_id="dev_other"), order["id"]) == cancelled
    with pytest.raises(KeyError):
        svc.retrieve("v_one", replace(caller, sub="other"), order["id"])
    with pytest.raises(KeyError):
        svc.retrieve("v_two", caller, order["id"])
    with pytest.raises(PermissionError):
        svc.retrieve("v_one", replace(caller, device_id=None), order["id"])


def test_reconnected_owner_can_cancel_unclaimed_approval_without_inheriting_it(orders):
    svc, caller, manifest = orders
    order = svc.prepare("v_one", caller, manifest)
    svc.transition("v_one", caller, order["id"], order["review_digest"], "approve")
    reconnected = replace(caller, device_id="dev_other")
    saved = svc.retrieve("v_one", reconnected, order["id"])
    assert saved["state"] == "approved"
    with pytest.raises(KeyError):
        svc.transition("v_one", reconnected, order["id"], order["review_digest"], "claim")
    with pytest.raises(KeyError):
        svc.transition("v_one", replace(reconnected, sub="other"), order["id"], order["review_digest"], "cancel")
    cancelled = svc.transition("v_one", reconnected, order["id"], order["review_digest"], "cancel")
    assert cancelled["state"] == "cancelled"
    assert svc.prepare("v_one", reconnected, manifest)["state"] == "pending"


def test_approval_reserves_ceiling_and_cancel_only_releases_unclaimed_work(orders):
    from app.db.models import CreditAccount, CreditReservation
    svc, caller, manifest = orders
    order = svc.prepare("v_one", caller, manifest)
    args = ("v_one", caller, order["id"], order["review_digest"])
    approved = svc.transition(*args, "approve")
    with svc.db()() as session:
        assert session.get(CreditAccount, "cra_device_test").reserved_micro == 1000
        assert session.get(CreditReservation, approved["reservation_id"]).status == "active"
    svc.transition(*args, "cancel")
    with svc.db()() as session:
        assert session.get(CreditAccount, "cra_device_test").reserved_micro == 0
        assert session.get(CreditReservation, approved["reservation_id"]).status == "released"
    second = svc.prepare("v_one", caller, manifest)
    args = ("v_one", caller, second["id"], second["review_digest"])
    approved = svc.transition(*args, "approve")
    svc.transition(*args, "claim")
    svc.transition(*args, "cancel")
    with svc.db()() as session:
        assert session.get(CreditAccount, "cra_device_test").reserved_micro == 1000
        assert session.get(CreditReservation, approved["reservation_id"]).status == "reconciling"


def test_insufficient_budget_rolls_back_approval(orders):
    from app.credits.errors import InsufficientCredits
    from app.db.models import CreditAccount
    svc, caller, manifest = orders
    order = svc.prepare("v_one", caller, manifest)
    with svc.db()() as session:
        session.get(CreditAccount, "cra_device_test").granted_micro = 999
        session.commit()
    with pytest.raises(InsufficientCredits):
        svc.transition("v_one", caller, order["id"], order["review_digest"], "approve")
    assert svc.retrieve("v_one", caller, order["id"]) == order
    with svc.db()() as session:
        assert session.get(CreditAccount, "cra_device_test").reserved_micro == 0


def test_call_admission_retains_uncertain_usage_and_never_replays(orders):
    from app.build import device_invocations as calls
    svc, caller, manifest = orders
    order = svc.prepare("v_one", caller, manifest)
    args = ("v_one", caller, order["id"])
    transition = (*args, order["review_digest"])
    svc.transition(*transition, "approve")
    svc.transition(*transition, "claim")
    quoted = {"invocation_id": "call_one", "request_digest": "a" * 64, "quote_micro": 700}
    assert calls.admit(*args, **quoted)["admitted_now"] is True
    assert calls.admit(*args, **quoted)["admitted_now"] is False
    with pytest.raises(ValueError, match="conflict"):
        calls.admit(*args, **{**quoted, "request_digest": "b" * 64})
    calls.record_result(*args, invocation_id="call_one", actual_micro=None)
    with pytest.raises(ValueError, match="exhausted"):
        calls.admit(*args, **{**quoted, "invocation_id": "call_two", "quote_micro": 500})
    calls.record_result(*args, invocation_id="call_one", actual_micro=400)
    assert calls.admit(*args, **{**quoted, "invocation_id": "call_two", "quote_micro": 500})["admitted_now"]
    svc.transition(*transition, "cancel")
    # A late result is retained; cancelling does not free ambiguous usage.
    calls.record_result(*args, invocation_id="call_two", actual_micro=450)
    with pytest.raises(ValueError, match="not_executing"):
        calls.admit(*args, **{**quoted, "invocation_id": "call_three", "quote_micro": 1})


def test_provider_overage_stops_further_device_calls(orders):
    from app.build import device_invocations as calls
    svc, caller, manifest = orders
    order = svc.prepare("v_one", caller, manifest)
    args = ("v_one", caller, order["id"])
    for action in ("approve", "claim"):
        svc.transition(*args, order["review_digest"], action)
    calls.admit(*args, invocation_id="call_one", request_digest="a" * 64, quote_micro=100)
    result = calls.record_result(*args, invocation_id="call_one", actual_micro=150)
    assert result["actual_micro"] == 100 and result["provider_actual_micro"] == 150
    with pytest.raises(ValueError, match="unavailable"):
        calls.admit(*args, invocation_id="call_two", request_digest="a" * 64, quote_micro=1)


def test_classify_device_chat_intent_covers_supported_and_ordinary():
    from app.build import device_orders as service

    assert service.classify_device_chat_intent(
        "Open Builds in Supymem. Do not modify data."
    ) is None
    assert service.classify_device_chat_intent(
        "Open analytics in Supymem and tell me what the founder dashboard actually shows. "
        "Answer only; do not start work."
    ) is None
    assert service.classify_device_chat_intent(
        "Open Builds in Supymem and click Delete"
    ) == "desktop"
    assert service.classify_device_chat_intent(
        "Open Builds in Supymem and report what build evidence is saved for LabHire. "
        "Do not start or modify any build."
    ) is None
    assert service.classify_device_chat_intent(
        "Inspect the workspace with Cursor, read-only; do not edit files."
    ) == "inspection"
    assert service.classify_device_chat_intent(
        "Check git status in the terminal for the venture workspace."
    ) == "terminal"
    assert service.classify_device_chat_intent(
        "Open the browser and check the live site page and waitlist."
    ) == "public_site"
    assert service.classify_device_chat_intent(
        "Use Codex to implement the current approved build task"
    ) == "coding"
    assert service.classify_device_chat_intent("What is next for this venture?") is None
    assert service.classify_device_chat_intent(
        "What was the outcome of the latest approved Supymem Mac task? Did it open Builds, "
        "was that verified, and how many credits were settled? Do not start another task."
    ) is None
    assert service.classify_device_chat_intent("Open Notes and type a reminder") == "desktop"
    assert service.classify_device_chat_intent(
        "Open Calculator. Press its visible Clear control once, then report the display."
    ) == "desktop"
    assert service.classify_device_chat_intent(
        "Open Calculator. What does the Clear control do?"
    ) is None
    assert service.classify_device_chat_intent("Will you open Notes and type a reminder?") == "desktop"
    assert service.classify_device_chat_intent(
        "If TextEdit is showing an Open dialog, click Cancel and report which window remains. "
        "If not, only describe what you see. Do not open or edit a document."
    ) == "desktop"
    assert service.classify_device_chat_intent(
        "If TextEdit is showing an Open dialog, should I click Cancel?"
    ) is None
    assert service.classify_device_chat_intent(
        "Open https://www.reddit.com/r/cscareerquestions/ in the browser and verify its waitlist page"
    ) == "public_site"
    assert service.classify_device_chat_intent(
        "Inspect the workspace with Cursor, but do not edit files."
    ) == "inspection"
    assert service._readonly_inspection_provider(
        "inspect cursor then edit the repo"
    ) is None
    assert service.classify_device_chat_intent(
        "Use Cursor to implement the landing page"
    ) == "coding"
    assert "Supported tasks" in service.UNSUPPORTED_MAC_TASK_MESSAGE


def test_device_chat_intent_parity_fixture_matches_server_classifier():
    """Shared JSON fixture keeps Console and Python classifiers from drifting."""
    import json
    from pathlib import Path

    from app.build import device_orders as service

    fixture_path = Path(__file__).resolve().parents[2] / "fixtures" / "device_chat_intent_parity.json"
    fixture = json.loads(fixture_path.read_text())
    phrases = fixture["phrases"]
    assert len(phrases) >= 15
    for phrase in phrases:
        text = phrase["text"]
        expected = phrase["server_intent"]
        assert service.classify_device_chat_intent(text) == expected, text
        # Client reviewable must never be broader than the server.
        if phrase["client_reviewable"]:
            assert expected is not None, text


@pytest.mark.parametrize("instruction", [
    "Use my Mac for a read-only background browser task. Open https://example.test and compare three steps.",
    "Could you please use this Mac for a read only browser task: open https://example.test?",
    "Prepare a Mac task: use the Mac for a readonly browser task, do not change anything.",
])
def test_readonly_browser_direct_prepare_has_no_scope_or_preparation_effect(orders, monkeypatch, instruction):
    service, caller, _ = orders
    def unexpected(*_args, **_kwargs):
        raise AssertionError("unsupported read-only policy must refuse before scope or preparation")
    monkeypatch.setattr(service, "_scope", unexpected)
    monkeypatch.setattr(service, "prepare_general_browser_from_chat", unexpected)
    monkeypatch.setattr(service, "prepare_desktop_from_chat", unexpected)
    assert service.classify_device_chat_intent(instruction) == "readonly_browser"
    with pytest.raises(ValueError, match="^device_read_only_browser_task_unavailable$"):
        service.prepare_from_chat("v_one", caller, instruction, "com.google.Chrome", 77, "123", 10)


def test_desktop_reviewed_text_never_authorizes_prohibited_content(orders):
    service, _caller, _manifest = orders
    instruction = (
        "On this Mac, open Safari, go to https://example.org/, and report the heading. "
        "Do not sign in, type private data, or visit https://other.example/.")
    values = service._desktop_reviewed_text(instruction)
    assert "https://example.org/" in values
    assert "private data" not in values
    assert "https://other.example/" not in values
    assert service._desktop_reviewed_text('Do not type "a secret" into Notes') == []


def test_prepare_from_chat_refuses_unclassified_and_foreign_urls(orders):
    svc, caller, _manifest = orders
    with pytest.raises(ValueError, match="device_desktop_app_required"):
        svc.prepare_from_chat("v_one", caller, "Open Notes and type a reminder")
    with pytest.raises(ValueError, match="device_chat_intent_unsupported"):
        svc.prepare_from_chat("v_one", caller, "What is next for this venture?")
    with pytest.raises(ValueError, match="device_chat_intent_unsupported"):
        svc.prepare_from_chat(
            "v_one",
            caller,
            "Open https://www.reddit.com/r/cscareerquestions/ in the browser and verify its waitlist page",
        )


def test_late_approval_keeps_a_start_window(orders, monkeypatch):
    from datetime import timedelta
    svc, caller, manifest = orders
    order = svc.prepare("v_one", caller, manifest)
    start = svc.utcnow()
    monkeypatch.setattr(svc, "utcnow", lambda: start + timedelta(hours=23, minutes=59))
    approved = svc.transition("v_one", caller, order["id"], order["review_digest"], "approve")
    assert approved["expires_at"] > (start + timedelta(hours=24, minutes=8)).isoformat()
    assert approved["review_digest"] == svc._digest(approved)
    monkeypatch.setattr(svc, "utcnow", lambda: start + timedelta(hours=24, minutes=2))
    claimed = svc.transition("v_one", caller, order["id"], approved["review_digest"], "claim")
    assert claimed["state"] == "claimed"


def test_approved_order_still_expires_after_its_start_window(orders, monkeypatch):
    from datetime import timedelta
    svc, caller, manifest = orders
    order = svc.prepare("v_one", caller, manifest)
    approved = svc.transition("v_one", caller, order["id"], order["review_digest"], "approve")
    later = svc.utcnow() + timedelta(hours=25)
    monkeypatch.setattr(svc, "utcnow", lambda: later)
    with pytest.raises(ValueError, match="expired"):
        svc.transition("v_one", caller, order["id"], approved["review_digest"], "claim")


def test_desktop_budget_grows_with_subgoals_and_stays_inside_native_bounds():
    from coding_agents.jev_decision import desktop_budget

    assert desktop_budget("Open the page and read the title", []) == (24, 900)
    steps, seconds = desktop_budget(
        'Open TextEdit. Type "Hello" then save it. Close the window; reopen the file. '
        "Check the text. Open Notes", ["Hello", "notes.txt"])
    assert 24 < steps <= 48 and 900 < seconds <= 1800
    assert desktop_budget("; ".join(f"step {i}" for i in range(40)), ["a"] * 8) == (48, 1800)


def test_zero_cost_hold_is_narrow_and_claim_requires_binding(orders):
    from app.build import device_budget
    from datetime import datetime, timezone
    manifest = {"credit_ceiling_micro": 0,
        "applications": ["com.companyos.app"], "origins": [],
        "actions": ["editor.edit", "terminal.execute"],
        "coding_verification": {"operation": "workspace_edit_v1", "provider": "codex",
            "write_scope": "venture_workspace", "command_network": "disabled"}}
    assert device_budget._subscription_coding(manifest)
    with pytest.raises(ValueError, match="device_budget_not_reserved"):
        device_budget.apply(None, None, {"manifest": manifest}, "claim", datetime.now(timezone.utc))
    for change in ({"origins": ["https://example.org"]}, {"actions": ["browser.read"]},
                   {"coding_verification": {"operation": "workspace_edit_v1", "provider": "other"}},
                   {"coding_verification": []}):
        assert not device_budget._subscription_coding({**manifest, **change})


@pytest.mark.parametrize("prefix", ["/Users/fixture-projectgooty", "~"])
def test_three_literal_lines_and_full_save_path_survive_prepared_review(orders, monkeypatch, prefix):
    svc, caller, _manifest = orders
    path = prefix + "/Documents/SupymemCUTest/supymem-note-2026-10-02.txt"
    instruction = (
        "On my Mac, open TextEdit and type these three lines in a new plain text document: "
        "Supymem test note; Gyms lose members quietly; Check-ins keep them. "
        f"Save as {path} and report the saved contents. Do not overwrite an existing file."
    )
    monkeypatch.setattr(svc, "prepare", lambda _vid, _caller, manifest, **_kwargs: {
        "manifest": manifest.model_dump(exclude_none=True)})
    order = svc.prepare_desktop_from_chat("v_one", caller, instruction,
        "com.apple.TextEdit", 123, "123456789", 44, "Untitled")
    manifest = order["manifest"]
    assert manifest["desktop_verification"]["reviewed_text"] == [
        "Supymem test note\nGyms lose members quietly\nCheck-ins keep them.", path]
    assert manifest["desktop_verification"]["document_text_index"] == 0
    assert manifest["desktop_verification"]["founder_goal"] == instruction
    assert manifest["applications"] == ["com.apple.TextEdit"]
    assert manifest["actions"] == ["desktop.read", "desktop.interact"]
    assert manifest["origins"] == []


def test_counted_lines_require_exact_bounded_content_and_respect_prohibition(orders):
    svc, _caller, _manifest = orders
    assert svc._desktop_reviewed_text(
        "Do not type these three lines: One; Two; Three. Save as /tmp/private.txt") == []
    for instruction in [
        "Type these three lines: One; Two. Save as /tmp/note.txt",
        "Type these three lines: One; ; Three. Save as /tmp/note.txt",
        "Type these three lines: " + "x" * 321 + "; Two; Three. Save as /tmp/note.txt",
    ]:
        with pytest.raises(ValueError, match="device_desktop_lines_ambiguous"):
            svc._desktop_reviewed_text(instruction)
    assert svc._desktop_reviewed_text("Go to https://example.org/") == ["https://example.org/"]


def test_counted_lines_do_not_silently_drop_a_save_path_when_capacity_is_full(orders):
    svc, _caller, _manifest = orders
    with pytest.raises(ValueError, match="device_desktop_lines_ambiguous"):
        svc._desktop_reviewed_text(
            "Type these eight lines: One; Two; Three; Four; Five; Six; Seven; Eight. Save as /tmp/note.txt")


@pytest.mark.parametrize("path", [
    "~/Documents/Test Notes/note.txt", "/Users/Some Person/Test Notes/note.txt",
    "~/Documents/Test Notes/note.txt.",
])
def test_quoted_save_path_is_one_exact_candidate_including_spaces_and_punctuation(orders, path):
    svc, _caller, _manifest = orders
    assert svc._desktop_reviewed_text(f'Save as "{path}" and report the saved contents.') == [path]
    assert svc._desktop_reviewed_text(f'Save as “{path}” and report the saved contents.') == [path]


def test_coding_manifest_includes_saved_nonsecret_service_guidance(orders, monkeypatch):
    svc, caller, _ = orders
    from app.build import native_static_recipe
    monkeypatch.setattr(native_static_recipe, 'reviewed_recipe', lambda *args, **kwargs: 'Reviewed static recipe\n')
    from app.chat.service_guides import select_service
    with svc.db()() as session:
        session.add(svc.Venture(id='v_one', workspace_id='w_one', name='Reviewed landing', raw_idea='Landing',
            stage='draft', created_at=svc.utcnow(), updated_at=svc.utcnow()))
        session.commit()
    select_service('v_one', {'category': 'hosting', 'provider': 'netlify'})
    monkeypatch.setattr(svc, '_coding_task_snapshot', lambda *args: {
        'task_id': 'task_one', 'title': 'Reviewed landing', 'description': 'Use approved copy',
        'acceptance': '["Visible reviewed offer"]', 'artifact_profile': 'static_landing_v1',
        'landing_expectations': {'contract': {'profile': 'static_landing_v1', 'product_name': 'Offer',
            'required_text': ['Reviewed offer'], 'actions': []}, 'criteria': []}})
    monkeypatch.setattr(svc, 'prepare', lambda vid, caller, manifest, **kwargs: manifest.model_dump())
    reviewed = svc.prepare_coding_task_from_chat('v_one', caller, 'Build my landing page with Codex')
    text = reviewed['instruction']
    assert 'Netlify' in text and 'DEPLOY.md only' in text
    assert 'future product work only' in text and 'Do not create Python routes' in text
    assert 'Provide reviewed database migrations' not in text
    assert 'Do not publish, deploy, communicate externally' in text
@pytest.mark.parametrize("bundle,index,text", [
    ("com.apple.Safari", 0, "One\nTwo\nThree"),
    ("com.apple.TextEdit", None, "One\nTwo\nThree"),
    ("com.apple.TextEdit", 1, "One\nTwo\nThree"),
    ("com.apple.TextEdit", False, "One\nTwo\nThree"),
    ("com.apple.TextEdit", 0, "One\nTwo"),
    ("com.apple.TextEdit", 0, "One\t\nTwo\nThree"),
])
def test_document_profile_rejects_other_app_index_and_controls(orders, bundle, index, text):
    svc, _, _ = orders
    with pytest.raises(ValueError):
        svc.DesktopVerification(operation="desktop_goal_v1", application_bundle=bundle,
            process_id=123, process_start_identity="123456789", window_id=44,
            reviewed_text=[text], document_text_index=index)


def test_only_explicit_bounded_three_lines_join_for_textedit_review(orders):
    svc, _, _ = orders
    message = "Type these three lines: One; Two; Three. Save as /tmp/note.txt"
    assert svc._desktop_reviewed_text(message) == ["One", "Two", "Three.", "/tmp/note.txt"]
    assert svc._desktop_reviewed_text(message, application_bundle="com.apple.Safari") == svc._desktop_reviewed_text(message)
    assert svc._desktop_reviewed_text(message, application_bundle="com.apple.TextEdit") == ["One\nTwo\nThree.", "/tmp/note.txt"]
    assert len(svc._desktop_reviewed_text("Type three lines: " + "x" * 310 + "; Two; Three", application_bundle="com.apple.TextEdit")[0]) == 320
    with pytest.raises(ValueError, match="device_desktop_lines_ambiguous"):
        svc._desktop_reviewed_text("Type three lines: " + "x" * 311 + "; Two; Three", application_bundle="com.apple.TextEdit")


@pytest.mark.parametrize('focus', [
    'Use Codex to build current approved task. Prepare its exact coding card for review; do not execute until card approved.',
    'Use Codex for the approved landing task, but edit documentation-only CODING_CARD.md; do not implement HTML even after approval.',
])
def test_prepared_coding_card_separates_conditional_review_gate_without_widening_focus(orders, monkeypatch, focus):
    svc, caller, _ = orders
    from app.build import native_static_recipe
    monkeypatch.setattr(native_static_recipe, 'reviewed_recipe', lambda *args, **kwargs: 'Reviewed static recipe\n')
    with svc.db()() as session:
        session.add(svc.Venture(id='v_one', workspace_id='w_one', name='Reviewed landing', raw_idea='Landing',
            stage='draft', created_at=svc.utcnow(), updated_at=svc.utcnow()))
        session.commit()
    monkeypatch.setattr(svc, '_coding_task_snapshot', lambda *args: {
        'task_id': 'task_one', 'title': 'Reviewed landing', 'description': 'Only approved concept copy',
        'acceptance': '["Visible reviewed offer"]', 'artifact_profile': 'static_landing_v1',
        'landing_expectations': {'contract': {'profile': 'static_landing_v1', 'product_name': 'Offer',
            'required_text': ['Reviewed offer'], 'actions': []}, 'criteria': []}})
    monkeypatch.setattr(svc, 'prepare', lambda vid, caller, manifest, **kwargs: manifest.model_dump())
    manifest = svc.prepare_coding_task_from_chat('v_one', caller, focus)
    assert f"Founder's requested focus for this reviewed run: {focus}" in manifest['instruction']
    assert 'held until the founder approves it and the runtime claims it' in manifest['instruction']
    assert 'documentation-only requests remain documentation-only' in manifest['instruction']
    assert 'Other founder decisions, source review, QA and publication approvals remain separate' in manifest['instruction']
    assert 'workspace-root index.html' in manifest['instruction']
    assert 'Reviewed static recipe' in manifest['instruction']
    assert 'Do not publish, deploy, communicate externally' in manifest['instruction']
    assert manifest['credit_ceiling_micro'] == 0
    assert manifest['actions'] == ['editor.edit', 'terminal.execute']
    assert manifest['origins'] == []


@pytest.mark.parametrize('host', ['inventory.example', 'gardening.example'])
@pytest.mark.parametrize('measurement', ['ready', 'off', 'needs_setup'])
def test_local_static_recipe_is_a_connected_tool_measurement_brief(monkeypatch, host, measurement):
    from types import SimpleNamespace
    from app.build import native_static_recipe, subscription_static_baseline
    from app.hosting import revisions
    from app.product_analytics import service

    monkeypatch.setenv('SUPYMEM_MODE', 'local')
    app = SimpleNamespace(id='app_owned', workspace_id='wid', venture_id='vid', deleted_at=None)
    session = SimpleNamespace(get=lambda model, key: app)
    monkeypatch.setattr(subscription_static_baseline, 'assert_current', lambda *args: None)
    monkeypatch.setattr(service, 'enabled', lambda name: measurement != 'off')
    tag = f'<script data-supymem-analytics="1">publicReference("https://{host}/e")</script>'
    monkeypatch.setattr(revisions, '_public_analytics_script', lambda app_id: tag if measurement == 'ready' else '')
    snapshot = {'artifact_profile': 'static_landing_v1', 'publication_baseline': {'app_id': app.id}}
    brief = native_static_recipe.reviewed_recipe(session, snapshot, workspace_id='wid', venture_id='vid')
    assert 'your connected coding tool' in brief
    assert 'before source is frozen' in brief
    assert 'never rewrite frozen source' in brief
    if measurement == 'ready':
        assert brief.count(tag) == 1
    else:
        assert 'omit measurement scripts' in brief
        assert 'sole analytics script' not in brief
        if measurement == 'needs_setup':
            assert 'Settings > Keys and connections > Publishing' in brief
            assert 'Do not claim that this preview records visits or signups' in brief
        else:
            assert 'Page measurement is off' in brief


@pytest.mark.parametrize('scope', ['foreign_workspace', 'foreign_venture', 'deleted', 'missing', 'baseline_changed', 'owned'])
def test_native_static_recipe_only_uses_exact_owned_current_app(monkeypatch, scope):
    from types import SimpleNamespace
    from app.build import native_static_recipe, subscription_static_baseline
    from app.hosting import revisions
    app = SimpleNamespace(id='app_owned', workspace_id='wid', venture_id='vid', deleted_at=None)
    if scope == 'foreign_workspace':
        app.workspace_id = 'other'
    if scope == 'foreign_venture':
        app.venture_id = 'other'
    if scope == 'deleted':
        app.deleted_at = True
    session = SimpleNamespace(get=lambda model, key: None if scope == 'missing' else app)
    calls = []
    def baseline(session, app, frozen):
        if scope == 'baseline_changed':
            raise ValueError('subscription_static_publication_baseline_changed')
        assert frozen['app_id'] == app.id
    monkeypatch.setattr(subscription_static_baseline, 'assert_current', baseline)
    monkeypatch.setattr(revisions, '_public_analytics_script', lambda app_id: calls.append(app_id) or '<script data-supymem-analytics="1">public-ingest-fixture</script>')
    snapshot = {'artifact_profile': 'static_landing_v1', 'publication_baseline': {'app_id': 'app_owned'}}
    if scope == 'owned':
        result = native_static_recipe.reviewed_recipe(session, snapshot, workspace_id='wid', venture_id='vid')
        assert calls == ['app_owned'] and result.count('data-supymem-analytics="1"') == 1
        assert 'target="_blank"' in result and 'rel="noopener noreferrer"' in result
        assert 'do not claim working automation' in result
    else:
        with pytest.raises(ValueError):
            native_static_recipe.reviewed_recipe(session, snapshot, workspace_id='wid', venture_id='vid')
        assert calls == []
    assert native_static_recipe.reviewed_recipe(session, {}, workspace_id='wid', venture_id='vid') == ''


@pytest.mark.parametrize('pause_at', ['prepare', 'approve', 'claim'])
def test_explicit_device_pause_refuses_only_desktop_grants_and_keeps_history(orders, pause_at):
    from app.build import device_desktop_pause as pause
    svc, caller, browser = orders
    desktop = svc.Manifest(task_id='task_one', title='Reviewed desktop', instruction='Read selected TextEdit window',
        applications=['com.apple.TextEdit'], origins=[], actions=['desktop.read', 'desktop.interact'],
        credit_ceiling_micro=1000, duration_seconds=60, desktop_verification={
            'operation': 'desktop_goal_v1', 'application_bundle': 'com.apple.TextEdit',
            'process_id': 123, 'process_start_identity': '123456789', 'window_id': 44, 'window_title': 'Note'})
    body = None
    if pause_at != 'prepare':
        body = svc.prepare('v_one', caller, desktop)
        if pause_at == 'claim':
            body = svc.transition('v_one', caller, body['id'], body['review_digest'], 'approve')
    assert pause.set_pause('v_one', caller, True)['paused'] is True
    assert pause.status('v_one', caller)['paused'] is True
    for other in [replace(caller, device_id='another_device'), replace(caller, sub='another_actor')]:
        assert pause.status('v_one', other)['paused'] is False
    with pytest.raises(ValueError, match='device_desktop_paused'):
        if pause_at == 'prepare':
            svc.prepare('v_one', caller, desktop)
        else:
            svc.transition('v_one', caller, body['id'], body['review_digest'], pause_at)
    if body:
        assert svc.retrieve('v_one', caller, body['id'])['state'] == body['state']
        assert svc.transition('v_one', caller, body['id'], body['review_digest'], 'cancel')['state'] == 'cancelled'
    # Browser inspection remains reviewable; CLI contracts have no desktop flag.
    assert svc.prepare('v_one', caller, browser)['state'] == 'pending'
    with svc.db()() as session:
        for manifest in [{}, {'coding_verification': {'operation': 'workspace_edit_v1'}},
                         {'terminal_verification': {'operation': 'git_status_v1'}}]:
            pause.assert_available(session, 'w_one', 'founder', 'dev_one', manifest)
    assert pause.set_pause('v_one', caller, False)['paused'] is False
    assert pause.status('v_one', caller)['paused'] is False


def test_desktop_pause_strict_body_and_native_actor_device_boundary(orders):
    from app.build import device_desktop_pause as pause
    svc, caller, _ = orders
    with pytest.raises(ValueError, match='device_desktop_pause_invalid'):
        pause.set_pause('v_one', caller, 'true')
    with pytest.raises(PermissionError, match='authenticated_device_required'):
        pause.set_pause('v_one', replace(caller, device_id=None), True)
    from app.api.device_orders_api import DesktopPauseBody
    for body in [{'paused': 'true'}, {'paused': True, 'device_id': 'another'}, {'paused': True, 'actor_id': 'another'}]:
        with pytest.raises(ValueError):
            DesktopPauseBody.model_validate(body)


def _multi_surface_manifest(svc):
    surfaces = [dict(application_bundle="com.apple.TextEdit", process_id=101,
        process_start_identity="1001", window_id=11, window_title="Owned text"),
        dict(application_bundle="com.apple.Notes", process_id=202,
        process_start_identity="2002", window_id=22, window_title="Owned note")]
    return dict(task_id="task_one", title="Explicit windows", instruction="Copy the reviewed text",
        applications=[s['application_bundle'] for s in surfaces], origins=[],
        actions=["desktop.read", "desktop.interact"], credit_ceiling_micro=0, duration_seconds=60,
        desktop_verification=dict(**surfaces[0], operation="desktop_goal_v2", reviewed_surfaces=surfaces))


def test_explicit_multisurface_schema_binds_primary_apps_and_each_identity(orders):
    import copy
    svc, _, _ = orders
    data = _multi_surface_manifest(svc)
    manifest = svc.Manifest(**data)
    assert manifest.desktop_verification.operation == "desktop_goal_v2"
    assert len(manifest.desktop_verification.reviewed_surfaces) == 2
    for mutate in (
        lambda d: d['desktop_verification'].update(window_id=999),
        lambda d: d.update(applications=['com.apple.TextEdit']),
        lambda d: d['desktop_verification']['reviewed_surfaces'][1].update(process_id=101),
        lambda d: d['desktop_verification']['reviewed_surfaces'].append(d['desktop_verification']['reviewed_surfaces'][0]),
        lambda d: d['desktop_verification'].update(delivery_mode='foreground_fallback'),
        lambda d: d['desktop_verification']['reviewed_surfaces'][1].update(allow_launch=True),
        lambda d: d['desktop_verification'].update(operation='desktop_goal_v1'),
    ):
        changed = copy.deepcopy(data)
        mutate(changed)
        with pytest.raises(ValidationError):
            svc.Manifest(**changed)


def test_multisurface_default_off_before_any_order_and_digest_binds_selected_windows(orders, monkeypatch):
    svc, caller, _ = orders
    manifest = svc.Manifest(**_multi_surface_manifest(svc))
    monkeypatch.delenv('SUPYMEM_REVIEWED_DESKTOP_V2', raising=False)
    from app.db.models import VentureDocument
    from sqlalchemy import select
    with pytest.raises(ValueError, match='device_desktop_multisurface_unavailable'):
        svc.prepare('v_one', caller, manifest)
    with svc.db()() as session:
        assert not session.scalar(select(VentureDocument.id).where(VentureDocument.kind == svc.KIND))
    monkeypatch.setenv('SUPYMEM_REVIEWED_DESKTOP_V2', '1')
    order = svc.prepare('v_one', caller, manifest)
    assert order['manifest']['desktop_verification']['reviewed_surfaces'] == [s.model_dump() for s in manifest.desktop_verification.reviewed_surfaces]
    import copy
    changed = copy.deepcopy(order)
    changed['manifest']['desktop_verification']['reviewed_surfaces'][1]['window_id'] = 999
    assert svc._digest(changed) != order['review_digest']


def test_founder_explicit_multisurface_request_persists_every_selected_surface(tmp_path, monkeypatch):
    monkeypatch.delenv("SUPYMEM_MODE", raising=False)
    client = _client(tmp_path, monkeypatch)
    vid = client.post('/ventures', json={'raw_idea':'Owned multi-window review', 'start':False}).json()['id']
    from app.build import device_orders as svc
    from app.credits.accounts import ensure_account
    ensure_account(vid)
    surfaces = [svc.ReviewedDesktopSurface(**surface) for surface in _multi_surface_manifest(svc)['desktop_verification']['reviewed_surfaces']]
    monkeypatch.setenv('SUPYMEM_REVIEWED_DESKTOP_V2', '1')
    from core.assertion import sign_assertion
    secret = 'multisurface-route-fixture-over-thirty-two-characters'
    monkeypatch.setenv('COS_INTERNAL_ASSERTION_SECRET', secret)
    path = f'/ventures/{vid}/device-work-orders/from-chat'
    signed = sign_assertion(secret, sub='founder', email='', role='user', path=path, method='POST', device_id='dev_one')
    refused = client.post(path, json={'instruction':'Copy only between the selected windows',
        'reviewed_surfaces':[surface.model_dump() for surface in surfaces]}, headers={'X-COS-Assertion':signed})
    assert refused.status_code == 409
    assert refused.json()['detail']['code'] == 'device_desktop_verification_required'
    from app.db.models import VentureDocument
    from sqlalchemy import select
    with svc.db()() as session:
        assert not session.scalar(select(VentureDocument.id).where(VentureDocument.kind == svc.KIND))
    signed = sign_assertion(secret, sub='founder', email='', role='user', path=path, method='POST', device_id='dev_one')
    response = client.post(path, json={'instruction':'Copy only between the selected windows; confirm the exact visible result "Reviewed result"',
        'reviewed_surfaces':[surface.model_dump() for surface in surfaces]}, headers={'X-COS-Assertion':signed})
    assert response.status_code == 200, response.text
    order = response.json()
    assert order['task_snapshot']['kind'] == 'founder_mac_request'
    assert order['manifest']['desktop_verification']['operation'] == 'desktop_goal_v2'
    assert order['manifest']['desktop_verification']['reviewed_surfaces'] == [surface.model_dump() for surface in surfaces]
    assert order['manifest']['applications'] == [surface.application_bundle for surface in surfaces]
    assert order['manifest']['desktop_verification']['delivery_mode'] == 'background'
    assert order['manifest']['desktop_verification']['expected_visible_text'] == 'Reviewed result'
    import copy
    changed = copy.deepcopy(order)
    changed['manifest']['desktop_verification']['expected_visible_text'] = 'Another result'
    assert svc._digest(changed) != order['review_digest']


@pytest.mark.parametrize("goal,document,expected", [
    ('Type these three lines: First; Second; Third.', 'First\nSecond\nThird.', None),
    ('Confirm the exact visible result "Ready"', None, 'Ready'),
])
def test_multisurface_chat_persists_supported_primary_predicates(orders,monkeypatch,goal,document,expected):
    svc,caller,_=orders
    monkeypatch.setenv('SUPYMEM_REVIEWED_DESKTOP_V2','1')
    surfaces=[svc.ReviewedDesktopSurface(**row) for row in _multi_surface_manifest(svc)['desktop_verification']['reviewed_surfaces']]
    captured=[]
    monkeypatch.setattr(svc,'prepare',lambda _v,_c,manifest,**kw: captured.append(manifest.model_dump(exclude_none=True)) or {'manifest':captured[-1]})
    result=svc.prepare_multisurface_from_chat('v_one',caller,goal,surfaces)['manifest']
    spec=result['desktop_verification']
    assert spec.get('expected_visible_text')==expected
    assert spec.get('document_text_index')==(0 if document else None)
    if document:
        assert spec['reviewed_text'][0]==document
    assert spec['process_id']==surfaces[0].process_id and spec['window_id']==surfaces[0].window_id
    assert result['duration_seconds']==svc.desktop_budget(goal,spec['reviewed_text'])[1]


@pytest.mark.parametrize('goal', [
    'Copy only between the selected windows',
    'Do not confirm the exact visible result "Ready"',
    'Confirm the exact visible result "One"; confirm the exact visible result "Two"',
    'Type these three lines: First; Second; Third. Report contents. Confirm the exact visible result "One"; confirm the exact visible result "Two"',
])
def test_multisurface_chat_refuses_unsupported_or_ambiguous_predicates_before_prepare(orders,monkeypatch,goal):
    svc,caller,_=orders
    monkeypatch.setenv('SUPYMEM_REVIEWED_DESKTOP_V2','1')
    surfaces=[svc.ReviewedDesktopSurface(**row) for row in _multi_surface_manifest(svc)['desktop_verification']['reviewed_surfaces']]
    calls=[]
    monkeypatch.setattr(svc,'prepare',lambda *args,**kw: calls.append(args))
    with pytest.raises(ValueError,match='device_desktop_verification_required'):
        svc.prepare_multisurface_from_chat('v_one',caller,goal,surfaces)
    assert calls==[]


def _modal_policy(svc):
    primary=_multi_surface_manifest(svc)['desktop_verification']
    return {'operation':'exact_parent_children_v1','parents':[
        {key:primary[key] for key in ('application_bundle','process_id','process_start_identity','window_id')}]}


def test_modal_review_is_explicit_bound_and_default_off_before_order(orders,monkeypatch):
    svc,caller,_=orders
    data=_multi_surface_manifest(svc)
    data['desktop_verification']['modal_policy']=_modal_policy(svc)
    manifest=svc.Manifest.model_validate(data)
    monkeypatch.setenv('SUPYMEM_REVIEWED_DESKTOP_V2','1')
    monkeypatch.delenv('SUPYMEM_REVIEWED_MODAL_CHILDREN',raising=False)
    with pytest.raises(ValueError,match='device_desktop_modal_policy_unavailable'):
        svc.prepare('v_one',caller,manifest)
    from app.db.models import VentureDocument
    from sqlalchemy import select
    with svc.db()() as session:
        assert not session.scalar(select(VentureDocument.id).where(VentureDocument.kind==svc.KIND))
    monkeypatch.setenv('SUPYMEM_REVIEWED_MODAL_CHILDREN','1')
    # Official 0.33.3 cannot honor modal_parents; a flag is not artifact proof.
    with pytest.raises(ValueError,match='device_desktop_modal_policy_unavailable'):
        svc.prepare('v_one',caller,manifest)
    with svc.db()() as session:
        assert not session.scalar(select(VentureDocument.id).where(VentureDocument.kind==svc.KIND))


@pytest.mark.parametrize('mutation',['pid','start','window','bundle','duplicate','extra','recursive'])
def test_modal_policy_never_grants_undeclared_or_ambiguous_roots(orders,mutation):
    svc,_,_=orders
    data=_multi_surface_manifest(svc)
    policy=_modal_policy(svc)
    if mutation=='pid':
        policy['parents'][0]['process_id']=999
    if mutation=='start':
        policy['parents'][0]['process_start_identity']='999'
    if mutation=='window':
        policy['parents'][0]['window_id']=999
    if mutation=='bundle':
        policy['parents'][0]['application_bundle']='com.example.Decoy'
    if mutation=='duplicate':
        policy['parents'].append(policy['parents'][0].copy())
    if mutation=='extra':
        policy['parents'][0]['all_windows']=True
    if mutation=='recursive':
        policy['operation']='all_descendants'
    data['desktop_verification']['modal_policy']=policy
    with pytest.raises(ValueError):
        svc.Manifest.model_validate(data)
