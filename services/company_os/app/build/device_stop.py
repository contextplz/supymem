"""Internal native termination receipt and atomic credit reconciliation.

Not exposed as an HTTP endpoint until an authenticated native owner supplies
actual process-exit evidence. Stopped means the executor ended, not that its
venture task succeeded. An ambiguous exit or provider/effect receipt retains
the hold and prevents another execution of the same task.
"""
import json
import re
from datetime import datetime, timedelta, timezone

from app.build import device_invocations as calls
from app.build import device_orders as orders
from app.core.local_mode import is_local
from app.credits._txn import naive_utc
from app.credits.ledger import append
from app.db.models import CreditReservation


def _release_qualification(session, account, body):
    if body.get("state") == "stopped" and body.get("settlement_ledger_id"):
        binding = orders._qualification_binding(body)
        if binding is not None:
            from app.build import browser_qualification as qualification
            qualification.release_order_allocation(session, account, body, binding)


def _settle_execution(session, account, body, hold, charged, now, evidence_digest):
    if hold is None:
        if (not is_local() or body.get("budget_source") != "local" or body.get("reservation_id")
                or charged < 0 or charged > body["manifest"]["credit_ceiling_micro"]):
            raise ValueError("device_stop_budget_inconsistent")
        body.update(state="stopped", closed_at=now.isoformat(), settled_micro=charged)
        return
    if (charged > hold.amount_micro or charged < 0 or account.reserved_micro < hold.amount_micro
            or account.spent_micro + charged + account.reserved_micro - hold.amount_micro > account.granted_micro):
        raise ValueError("device_stop_budget_inconsistent")
    account.reserved_micro -= hold.amount_micro
    account.spent_micro += charged
    account.version += 1
    account.updated_at = naive_utc(now)
    entry = append(session, account=account, kind="spend", amount_micro=-charged,
        reason="settled device execution", actor_kind="system", actor_id="system",
        idempotency_key=f"settle:{hold.id}", purpose=hold.purpose,
        reservation_id=hold.id, request_id=body["id"],
        meta={"execution_id": body["execution_id"], "evidence_digest": evidence_digest,
              "hold_micro": hold.amount_micro})
    hold.status, hold.settled_micro, hold.settled_at = "settled", charged, naive_utc(now)
    body.update(state="stopped", closed_at=now.isoformat(), settled_micro=charged,
                settlement_ledger_id=entry.id)


def _reconcilable_navigation(body, invocations):
    manifest = body.get("manifest", {})
    uncertain = [value for value in invocations
        if value.get("effect", {}).get("state") == "uncertain"]
    return (len(uncertain) == 1
        and manifest.get("applications") == ["com.google.Chrome"]
        and set(manifest.get("actions", [])) <= {"browser.read", "browser.interact"}
        and bool(manifest.get("browser_verification", {}).get("expected_locations"))
        and uncertain[0].get("proposal", {}).get("operation") == "click")


def _reconcilable_desktop_text(body, invocations):
    """Close a stopped Mac text action without asserting its visual outcome.

    The native stop receipt proves this execution ended; the effect receipt
    intentionally remains uncertain. Only one reviewed text-entry effect may
    use this path, and no task-success or no-mutation assertion is made.
    """
    manifest = body.get("manifest") or {}
    verification = manifest.get("desktop_verification") or {}
    uncertain = [value for value in invocations
        if value.get("effect", {}).get("state") == "uncertain"]
    return (len(uncertain) == 1
        and verification.get("operation") == "desktop_goal_v1"
        and bool(verification.get("reviewed_text"))
        and manifest.get("actions") == ["desktop.read", "desktop.interact"]
        and uncertain[0].get("proposal", {}).get("operation") == "type")


def _reconcilable_desktop_click(body, invocations):
    """Settle one stopped click only after the founder reviews its uncertainty.

    This never verifies whether the click landed or permits replay. The saved
    acknowledgement is bound to the exact native stop and effect digest.
    """
    manifest = body.get("manifest") or {}
    verification = manifest.get("desktop_verification") or {}
    uncertain = [value for value in invocations
        if value.get("effect", {}).get("state") == "uncertain"]
    stop = body.get("native_stop") or {}
    ack = body.get("uncertain_desktop_click_ack") or {}
    return (body.get("state") == "cancelled"
        and len(uncertain) == 1
        and verification.get("operation") == "desktop_goal_v1"
        and manifest.get("actions") == ["desktop.read", "desktop.interact"]
        and uncertain[0].get("proposal", {}).get("operation") == "click"
        and stop.get("processes_stopped") is True
        and ack.get("execution_id") == stop.get("execution_id")
        and ack.get("evidence_digest") == stop.get("evidence_digest")
        and ack.get("effect_digest") == uncertain[0].get("effect", {}).get("digest"))


def acknowledge_uncertain_desktop_click(venture_id, caller, order_id):
    """Save a founder's explicit review of an ambiguous, stopped Mac click."""
    now = orders.utcnow()
    with orders.db()() as session:
        row, body, _ = calls._locked(session, venture_id, caller, order_id,
                                      receipt_only=True, settled_stop=True)
        invocations = list((body.get("invocations") or {}).values())
        uncertain = [value for value in invocations
            if value.get("effect", {}).get("state") == "uncertain"]
        stop = body.get("native_stop") or {}
        if (body.get("state") != "cancelled"
                or body.get("stop_reconciliation_required") is not True
                or stop.get("processes_stopped") is not True
                or len(uncertain) != 1
                or uncertain[0].get("proposal", {}).get("operation") != "click"
                or (body.get("manifest") or {}).get("desktop_verification", {}).get("operation") != "desktop_goal_v1"
                or (body.get("manifest") or {}).get("actions") != ["desktop.read", "desktop.interact"]):
            raise ValueError("device_desktop_click_ack_unavailable")
        marker = {"execution_id": stop["execution_id"],
                  "evidence_digest": stop["evidence_digest"],
                  "effect_digest": uncertain[0]["effect"]["digest"]}
        previous = body.get("uncertain_desktop_click_ack")
        if previous is not None:
            if any(previous.get(key) != value for key, value in marker.items()):
                raise ValueError("device_desktop_click_ack_conflict")
            return body
        body["uncertain_desktop_click_ack"] = {
            **marker, "actor_id": body["actor_id"], "acknowledged_at": now.isoformat()}
        row.provenance_json, row.updated_at = json.dumps(body), now
        session.commit()
        return body


def _verification_kind(manifest):
    loop = manifest.get("coding_loop_verification")
    if (isinstance(loop, dict) and loop.get("operation") == "workspace_loop_v1"
            and (loop.get("profile") == "web_app_vercel_v1"
                 or is_local() and (loop.get("profile"), loop.get("check_recipe")) in (
                     ("static_landing_v1", "static_landing_checks_v1"), ("workspace_utility_v1", "workspace_utility_checks_v1"))
                    and loop.get("phase") == "implement")
            and loop.get("phase") in {"plan", "implement"}):
        return "workspace_loop_v1"
    deploy = manifest.get("deploy_verification")
    if isinstance(deploy, dict) and deploy.get("operation") == "workspace_deploy_v1" and deploy.get("provider") == "vercel":
        return "workspace_deploy_v1"
    for key, operation in (
        ("terminal_verification", "git_status_v1"),
        ("app_navigation_verification", "open_venture_screen_v1"),
        ("inspection_verification", "workspace_inspect_v1"),
    ):
        value = manifest.get(key)
        if isinstance(value, dict) and value.get("operation") == operation:
            return operation
    if isinstance(manifest.get("browser_verification"), dict):
        return "expected_browser_locations_v1"
    desktop = manifest.get("desktop_verification")
    if (isinstance(desktop, dict) and desktop.get("operation") == "desktop_goal_v1"
            and desktop.get("expected_visible_text")):
        return "desktop_observed_text_v1"
    return None


def _verification_evidence(body):
    kind = _verification_kind(body.get("manifest") or {})
    if kind in {"git_status_v1", "open_venture_screen_v1", "workspace_inspect_v1"}:
        return True
    if kind == "workspace_loop_v1":
        iterations = body.get("iterations") or []
        expected = "plan_ready" if body["manifest"]["coding_loop_verification"]["phase"] == "plan" else "pass"
        return bool(iterations and iterations[-1].get("execution_id") == body.get("execution_id")
            and iterations[-1].get("outcome") == expected)
    if kind == "workspace_deploy_v1":
        receipt = body.get("deploy_receipt") or {}
        return (receipt.get("execution_id") == body.get("execution_id")
            and receipt.get("status") in {"deployed", "protected"})
    invocations = body.get("invocations") or {}
    return bool(kind and invocations and all(isinstance(value, dict)
        and value.get("state") == "completed"
        and re.fullmatch(r"[a-f0-9]{64}", str(value.get("request_digest", "")))
        and type(value.get("actual_micro")) is int and value["actual_micro"] >= 0
        and (not value.get("effect") or value["effect"].get("state") in {"returned", "not_dispatched"})
        for value in invocations.values()))


def record(venture_id, caller, order_id, *, execution_id, evidence_digest, processes_stopped,
           task_verified=None, reason_code=None, outcome_code=None):
    if (not isinstance(execution_id, str) or not re.fullmatch(r"dex_[A-Za-z0-9]+", execution_id)
            or not isinstance(evidence_digest, str) or not re.fullmatch(r"[a-f0-9]{64}", evidence_digest)
            or type(processes_stopped) is not bool
            or (task_verified is not None and type(task_verified) is not bool)
            or (reason_code is not None and not re.fullmatch(r"cu_[a-z_]{1,40}", str(reason_code)))
            or outcome_code not in {None, "ok", "blocked", "timeout", "auth_lost", "model_unavailable", "needs_founder"}
            or outcome_code == "ok" and task_verified is not True):
        raise ValueError("device_stop_invalid")
    now = orders.utcnow()
    with orders.db()() as session:
        row, body, account = calls._locked(session, venture_id, caller, order_id,
                                          receipt_only=True, settled_stop=True)
        if body.get("execution_id") != execution_id or not body.get("claimed_at"):
            raise ValueError("device_stop_unbound")
        kind = _verification_kind(body.get("manifest") or {}) if task_verified is True else None
        if task_verified is True and (not processes_stopped or not kind or not _verification_evidence(body)
                or body.get("inference_reconciliation_required")):
            raise ValueError("device_stop_verification_invalid")
        receipt = {"execution_id": execution_id, "evidence_digest": evidence_digest,
                   "processes_stopped": processes_stopped}
        if task_verified is True:
            receipt.update(task_verified=True, verification_kind=kind)
        prior = body.get("native_stop")
        cleanup_reconciliation = (isinstance(prior, dict)
            and prior.get("execution_id") == execution_id
            and prior.get("processes_stopped") is False
            and processes_stopped is True
            and body.get("stop_reconciliation_required") is True
            and body.get("state") in {"claimed", "waiting_for_device", "cancelled"})
        if prior is not None and prior != receipt and not cleanup_reconciliation:
            raise ValueError("device_stop_conflict")
        hold = session.get(CreditReservation, body.get("reservation_id", ""), with_for_update=True)
        if hold is not None and hold.status == "settled" or hold is None and body.get("state") == "stopped":
            if prior != receipt or body.get("state") != "stopped" or not body.get("closed_at"):
                raise ValueError("device_stop_settlement_conflict")
            session.commit()
            if body.get("stop_reason_code") or body.get("outcome_code") not in {None, "ok"}:
                _post_stopped(venture_id, order_id, body)
            return body
        body["native_stop"] = receipt
        if outcome_code is not None and (task_verified is True or "outcome_code" not in body):
            body["outcome_code"] = outcome_code
        invocations = list(body.get("invocations", {}).values())
        reconciled_navigation = _reconcilable_navigation(body, invocations)
        reconciled_desktop_text = _reconcilable_desktop_text(body, invocations)
        reconciled_desktop_click = _reconcilable_desktop_click(body, invocations)
        ready = (processes_stopped and not body.get("inference_reconciliation_required")
                 and all(value.get("state") == "completed"
                         and (not value.get("effect") or value["effect"].get("state") in {"returned", "not_dispatched"}
                              or ((reconciled_navigation or reconciled_desktop_text or reconciled_desktop_click)
                                  and value["effect"].get("state") == "uncertain"))
                         for value in invocations))
        if ready:
            body.pop("stop_reconciliation_required", None)
            charged = sum(value["actual_micro"] for value in invocations)
            _settle_execution(session, account, body, hold, charged, now, evidence_digest)
            if reason_code and task_verified is not True:  # kept out of native_stop so retries stay idempotent
                body["stop_reason_code"] = reason_code
            if reconciled_navigation:
                body["navigation_effect_reconciliation"] = {
                    "kind": "reviewed_navigation_outcome_unverified",
                    "reconciled_at": now.isoformat(),
                    "effect_replayed": False,
                }
            if reconciled_desktop_text:
                body["desktop_text_effect_reconciliation"] = {
                    "kind": "reviewed_text_effect_outcome_unverified",
                    "reconciled_at": now.isoformat(),
                    "effect_replayed": False,
                }
            if reconciled_desktop_click:
                body["desktop_click_effect_reconciliation"] = {
                    "kind": "founder_reviewed_click_outcome_unverified",
                    "reconciled_at": now.isoformat(),
                    "acknowledged_at": body["uncertain_desktop_click_ack"]["acknowledged_at"],
                    "effect_replayed": False,
                }
        else:
            body["stop_reconciliation_required"] = True
        _release_qualification(session, account, body)
        if body.get("closed_at"):
            row.lease_expires_at = None
        row.provenance_json, row.updated_at = json.dumps(body), now
        from app.analytics.observability import stage_computer_outcome

        stage_computer_outcome(session, body)
        session.commit()
        if body.get("stop_reason_code") or body.get("outcome_code") not in {None, "ok"}:
            _post_stopped(venture_id, order_id, body)
        elif body.get("state") == "stopped" and processes_stopped and task_verified is not True:
            from app.chat.mac_stop import post_mac_finished

            post_mac_finished(venture_id, order_id, (body.get("manifest") or {}).get("title"))
        return body


def _post_stopped(venture_id, order_id, body):
    from app.chat.mac_stop import post_mac_stopped

    post_mac_stopped(venture_id, order_id, body.get("stop_reason_code"),
                     (body.get("manifest") or {}).get("title"),
                     outcome_code=body.get("outcome_code") or "needs_founder")


def settle_cancelled_unknown_usage(venture_id, caller, order_id, *, execution_id, evidence_digest):
    """Close a proven-stopped task by charging the reserved quote for unknown calls.

    This never assigns an invented provider actual or verifies an action. The
    founder already reviewed the order ceiling; unresolved calls are charged
    only at their per-call admitted upper bound after the provider deadline.
    """
    if (not isinstance(execution_id, str) or not re.fullmatch(r"dex_[A-Za-z0-9]+", execution_id)
            or not isinstance(evidence_digest, str) or not re.fullmatch(r"[a-f0-9]{64}", evidence_digest)):
        raise ValueError("device_unknown_usage_reconciliation_invalid")
    now = orders.utcnow()
    with orders.db()() as session:
        row, body, account = calls._locked(session, venture_id, caller, order_id,
                                          receipt_only=True, settled_stop=True)
        hold = session.get(CreditReservation, body.get("reservation_id", ""), with_for_update=True)
        if hold is not None and hold.status == "settled" or hold is None and body.get("state") == "stopped":
            if (body.get("state") != "stopped" or
                    body.get("unknown_usage_reconciliation", {}).get("evidence_digest") != evidence_digest or
                    body.get("execution_id") != execution_id):
                raise ValueError("device_unknown_usage_reconciliation_conflict")
            return body
        stop = {"execution_id": execution_id, "evidence_digest": evidence_digest,
                "processes_stopped": True}
        invocations = body.get("invocations") or {}
        if (body.get("state") != "cancelled" or body.get("execution_id") != execution_id
                or body.get("native_stop") != stop or body.get("stop_reconciliation_required") is not True
                or body.get("inference_reconciliation_required")
                or not isinstance(invocations, dict) or not invocations):
            raise ValueError("device_unknown_usage_reconciliation_unavailable")
        unknown = []
        uncertain_effects = []
        charged = 0
        for invocation_id, value in invocations.items():
            effect = value.get("effect") or {}
            if effect and effect.get("state") not in {"returned", "not_dispatched"}:
                if (effect.get("state") not in {"admitted", "uncertain"}
                        or (body.get("manifest") or {}).get("actions") != ["desktop.read", "desktop.interact"]
                        or (body.get("manifest") or {}).get("desktop_verification", {}).get("operation") != "desktop_goal_v1"):
                    raise ValueError("device_unknown_usage_reconciliation_unavailable")
                uncertain_effects.append(invocation_id)
            if value.get("state") == "completed":
                actual = value.get("actual_micro")
                if type(actual) is not int or actual < 0:
                    raise ValueError("device_unknown_usage_reconciliation_unavailable")
                charged += actual
            elif value.get("state") in {"calling", "uncertain"}:
                started = datetime.fromisoformat(value["started_at"])
                if started.tzinfo is None:
                    started = started.replace(tzinfo=timezone.utc)
                if now < started + timedelta(seconds=60):
                    raise ValueError("device_unknown_usage_reconciliation_too_early")
                quote = value.get("quote_micro")
                if type(quote) is not int or quote < 1:
                    raise ValueError("device_unknown_usage_reconciliation_unavailable")
                charged += quote
                unknown.append(invocation_id)
            else:
                raise ValueError("device_unknown_usage_reconciliation_unavailable")
        if (not unknown and not uncertain_effects or charged > body["manifest"]["credit_ceiling_micro"]
                or hold is not None and (account.reserved_micro < hold.amount_micro
                    or account.spent_micro + charged + account.reserved_micro - hold.amount_micro > account.granted_micro)):
            raise ValueError("device_unknown_usage_reconciliation_unavailable")
        ledger_id = None
        if hold is None:
            _settle_execution(session, account, body, None, charged, now, evidence_digest)
        else:
            account.reserved_micro -= hold.amount_micro
            account.spent_micro += charged
            account.version += 1
            account.updated_at = naive_utc(now)
            entry = append(session, account=account, kind="spend", amount_micro=-charged,
                reason="settled stopped device execution with unknown call ceiling",
                actor_kind="system", actor_id="system", idempotency_key=f"settle:{hold.id}",
                purpose=hold.purpose, reservation_id=hold.id, request_id=order_id,
                meta={"execution_id": execution_id, "evidence_digest": evidence_digest,
                      "hold_micro": hold.amount_micro, "unknown_call_count": len(unknown),
                      "actual_usage_unconfirmed": bool(unknown),
                      "effect_outcome_unconfirmed": bool(uncertain_effects),
                      "uncertain_effect_count": len(uncertain_effects)})
            hold.status, hold.settled_micro, hold.settled_at = "settled", charged, naive_utc(now)
            ledger_id = entry.id
        body.pop("stop_reconciliation_required", None)
        body.update(state="stopped", closed_at=now.isoformat(), settled_micro=charged,
                    settlement_ledger_id=ledger_id,
                    unknown_usage_reconciliation={"kind": "admitted_quote_upper_bound",
                        "evidence_digest": evidence_digest, "call_count": len(unknown),
                        "uncertain_effect_count": len(uncertain_effects),
                        "reconciled_at": now.isoformat(), "actual_usage_unconfirmed": bool(unknown),
                        "effect_outcome_unconfirmed": bool(uncertain_effects),
                        "effect_replayed": False})
        _release_qualification(session, account, body)
        row.provenance_json, row.updated_at = json.dumps(body), now
        session.commit()
        return body


def reconcile_startup_failure(venture_id, caller, order_id, *, execution_id, evidence_digest):
    """Close a cancelled claim that failed before its first provider invocation.

    This is deliberately narrower than a general stop override.  It accepts only
    the exact earlier negative stop receipt, after the founder has cancelled the
    order, and only when the server ledger proves that no model call or browser
    effect was ever admitted.  We therefore settle zero usage without claiming
    that an unobserved legacy child process was positively inventoried.
    """
    if (not isinstance(execution_id, str) or not re.fullmatch(r"dex_[A-Za-z0-9]+", execution_id)
            or not isinstance(evidence_digest, str) or not re.fullmatch(r"[a-f0-9]{64}", evidence_digest)):
        raise ValueError("device_startup_reconciliation_invalid")
    now = orders.utcnow()
    with orders.db()() as session:
        row, body, account = calls._locked(session, venture_id, caller, order_id,
                                          receipt_only=True, settled_stop=True,
                                          cancelled_recovery=True)
        prior = body.get("native_stop")
        hold = session.get(CreditReservation, body.get("reservation_id", ""), with_for_update=True)
        if hold is not None and hold.status == "settled" or hold is None and body.get("state") == "stopped":
            if (body.get("state") != "stopped" or body.get("settled_micro") != 0
                    or body.get("startup_reconciliation", {}).get("evidence_digest") != evidence_digest
                    or body.get("execution_id") != execution_id):
                raise ValueError("device_startup_reconciliation_conflict")
            return body
        if (body.get("execution_id") != execution_id or body.get("state") != "cancelled"
                or not body.get("cancelled_at") or not isinstance(prior, dict)
                or prior != {"execution_id": execution_id, "evidence_digest": evidence_digest,
                             "processes_stopped": False}
                or body.get("invocations") or body.get("inference_reconciliation_required")):
            raise ValueError("device_startup_reconciliation_unavailable")
        if body.get("device_id") != caller.device_id:
            # A rotated device cannot attest that the old executor was stopped.
            # Wait beyond its bounded lifetime before accepting the old native
            # pre-inference receipt; no new execution can be admitted meanwhile.
            claimed = datetime.fromisoformat(body["claimed_at"])
            if claimed.tzinfo is None:
                claimed = claimed.replace(tzinfo=timezone.utc)
            duration = int(body.get("manifest", {}).get("duration_seconds") or 0)
            if duration <= 0 or now < claimed + timedelta(seconds=duration + 60):
                raise ValueError("device_startup_reconciliation_unavailable")
        if hold is not None and (hold.status != "reconciling" or account.reserved_micro < hold.amount_micro
                or account.spent_micro + account.reserved_micro - hold.amount_micro > account.granted_micro):
            raise ValueError("device_startup_reconciliation_budget_inconsistent")
        ledger_id = None
        if hold is None:
            _settle_execution(session, account, body, None, 0, now, evidence_digest)
        else:
            account.reserved_micro -= hold.amount_micro
            account.version += 1
            account.updated_at = naive_utc(now)
            entry = append(session, account=account, kind="spend", amount_micro=0,
                reason="reconciled pre-inference device startup failure", actor_kind="system", actor_id="system",
                idempotency_key=f"settle:{hold.id}", purpose=hold.purpose,
                reservation_id=hold.id, request_id=order_id,
                meta={"execution_id": execution_id, "evidence_digest": evidence_digest,
                      "hold_micro": hold.amount_micro, "reconciliation": "pre_inference_startup_failure"})
            hold.status, hold.settled_micro, hold.settled_at = "settled", 0, naive_utc(now)
            ledger_id = entry.id
        body.pop("stop_reconciliation_required", None)
        body.update(state="stopped", closed_at=now.isoformat(), settled_micro=0,
                    settlement_ledger_id=ledger_id,
                    startup_reconciliation={"kind": "pre_inference_startup_failure",
                                            "evidence_digest": evidence_digest,
                                            "reconciled_at": now.isoformat(),
                                            "process_cleanup_proven": False})
        _release_qualification(session, account, body)
        row.provenance_json, row.updated_at = json.dumps(body), now
        session.commit()
        return body
