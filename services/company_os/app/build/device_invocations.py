"""Internal device-call admission within the already reserved order ceiling.

Quotes and usage must come from the trusted model controller, never a public
request's asserted price. This module performs no provider I/O and exposes no API.
"""

import json
import re
from datetime import datetime, timedelta

from app.build import device_budget, device_orders as orders
from app.core.local_mode import is_local
from app.db.models import CreditReservation, VentureDocument


def _locked(
    session,
    venture_id,
    caller,
    order_id,
    *,
    receipt_only=False,
    settled_stop=False,
    cancelled_recovery=False,
):
    if receipt_only:
        # Internal accounting of an already admitted provider call must survive
        # access revocation. It grants no read endpoint, new spend or effect.
        if caller.legacy or not caller.device_id or caller.role != "user":
            raise PermissionError("authenticated_device_required")
        existing = session.get(VentureDocument, order_id)
        if (
            existing is None
            or existing.kind != orders.KIND
            or existing.venture_id != venture_id
        ):
            raise KeyError("device_order_not_found")
        saved = json.loads(existing.provenance_json or "{}")
        wid, actor, device = (
            saved.get("workspace_id"),
            orders.subject_person_id(caller.sub),
            caller.device_id,
        )
        same_device = saved.get("device_id") == device
        recovery_state = (
            saved.get("state") == "cancelled" and bool(saved.get("claimed_at"))
        ) or (
            saved.get("state") == "stopped"
            and saved.get("startup_reconciliation", {}).get("kind")
            == "pre_inference_startup_failure"
        )
        if (
            not wid
            or saved.get("actor_id") != actor
            or (not same_device and not (cancelled_recovery and recovery_state))
        ):
            raise KeyError("device_order_not_found")
    else:
        wid, actor, device = orders._scope(venture_id, caller)
        if is_local():
            existing = session.get(VentureDocument, order_id)
            if existing is None or existing.kind != orders.KIND or existing.venture_id != venture_id:
                raise KeyError("device_order_not_found")
            saved = json.loads(existing.provenance_json or "{}")
    account = device_budget.lock_account(session, wid, venture_id,
        reservation_id=saved.get("reservation_id") if receipt_only or is_local() else None)
    orders._lock_venture(session, wid, venture_id)
    if not receipt_only:
        orders.authorize(
            session, workspace_id=wid, venture_id=venture_id, actor=actor, mutate=True
        )
    row, body = orders._saved_order(
        session,
        venture_id,
        order_id,
        wid,
        actor,
        saved.get("device_id") if receipt_only and cancelled_recovery else device,
        lock=True,
    )
    hold = session.get(
        CreditReservation, body.get("reservation_id", ""), with_for_update=True
    )
    local_budget = is_local() and body.get("budget_source") == "local" and not body.get("reservation_id")
    if not local_budget and (
        account is None
        or hold is None
        or hold.account_id != account.id
        or hold.request_id != order_id
        or hold.purpose != "device.work_order"
        or hold.is_pool
        or hold.status
        not in (
            {"reconciling", "settled"}
            if receipt_only and settled_stop
            else {"reconciling"}
        )
        or hold.workspace_id != wid
        or hold.amount_micro != body["manifest"]["credit_ceiling_micro"]
    ):
        raise ValueError("device_invocation_budget_unavailable")
    if body.get("review_digest") != orders._digest(body):
        raise ValueError("device_order_review_changed")
    return row, body, account


def _check_executing(session, venture_id, body, account, now):
    # Recheck even for a previously claimed card or a restarted native worker.
    # Receipt-only settlement deliberately does not use this admission path.
    orders.assert_native_desktop_available(body["manifest"])
    from app.build.device_desktop_pilot import assert_available as assert_pilot
    assert_pilot(session, body["actor_id"], body["device_id"], body["manifest"])
    orders.assert_general_browser_available(body["manifest"], body.get("actor_id"))
    orders.assert_coding_loop_available(body["manifest"], body.get("actor_id"))
    if (
        account is not None and account.status != "active"
        or body.get("inference_reconciliation_required")
        or body.get("native_stop")
    ):
        raise ValueError("device_invocation_budget_unavailable")
    if body["state"] != "claimed":
        raise ValueError("device_order_not_executing")
    if body.get("lease_expires_at") and now >= datetime.fromisoformat(body["lease_expires_at"]):
        raise ValueError("device_order_not_executing")
    if now >= datetime.fromisoformat(body["claimed_at"]) + timedelta(seconds=orders.device_hold_max_age_seconds()):
        raise ValueError("device_execution_expired")
    deadline = orders.execution_deadline(body)
    if now >= deadline:
        raise ValueError("device_execution_expired")
    if body["task_snapshot"] != orders._review_task_snapshot(
        session, body["workspace_id"], venture_id, body["manifest"],
        actor=body["actor_id"], device=body["device_id"]
    ):
        raise ValueError("device_order_task_changed")


def check_current(venture_id, caller, order_id):
    """Recheck current authority after inference; still not an effect grant."""
    with orders.db()() as session:
        _, body, account = _locked(session, venture_id, caller, order_id)
        _check_executing(session, venture_id, body, account, orders.utcnow())


def admit(
    venture_id,
    caller,
    order_id,
    *,
    invocation_id,
    request_digest,
    quote_micro,
    browser_phase=None,
):
    """Persist a one-use call admission before network I/O. Never replay I/O."""
    if (
        not isinstance(invocation_id, str)
        or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", invocation_id)
        or not isinstance(request_digest, str)
        or not re.fullmatch(r"[a-f0-9]{64}", request_digest)
        or type(quote_micro) is not int
        or quote_micro < 1
    ):
        raise ValueError("device_invocation_invalid")
    now = orders.utcnow()
    with orders.db()() as session:
        row, body, account = _locked(session, venture_id, caller, order_id)
        _check_executing(session, venture_id, body, account, now)
        calls = body.setdefault("invocations", {})
        profile = (body["manifest"].get("general_browser_verification") or {}).get(
            "execution_profile"
        )
        if profile is not None:
            from app.build import device_browser_ai as browser_ai

            checked = browser_ai.assert_configuration(profile)
            if (
                not isinstance(browser_phase, dict)
                or browser_phase.get("phase") not in {"plan", "actor", "verify"}
                or browser_phase.get("profile_digest")
                != browser_ai.profile_digest(checked)
                or "bounded_reads" in browser_phase and browser_phase["bounded_reads"] is not True
            ):
                raise ValueError("device_browser_ai_request_invalid")
            if (
                body.get("browser_ai_terminal_only")
                and browser_phase["phase"] == "actor"
                and not browser_phase.get("terminal_only")
            ):
                raise ValueError("device_browser_ai_terminal_effect_refused")
            if any(c.get("state") in {"calling", "uncertain"} for c in calls.values()):
                raise ValueError("device_effect_requires_reconciliation")
            providers = [
                c
                for c in calls.values()
                if c.get("source_kind") == browser_ai.SOURCE_KIND
            ]
            provider_count = sum(len(c.get("provider_attempts") or []) or (2 if c.get("fallback_reason") else 1)
                                 for c in providers)
            allowance = 2 if getattr(checked.roles, browser_phase["phase"]).provider == "azure" else 1
            if provider_count + allowance > checked.max_provider_calls:
                raise ValueError("device_invocation_limit")
            if (
                browser_phase["phase"] == "actor"
                and sum(c.get("phase") == "actor" for c in providers)
                >= body["manifest"]["general_browser_verification"]["max_steps"]
            ):
                raise ValueError("device_invocation_limit")
            previous = body.get("browser_ai_source")
            current = browser_phase["load"]
            previous_page = body.get("browser_ai_pages", {}).get(current["page_id"])
            if previous and (
                previous["session_id"] != current["session_id"]
                or (
                    previous_page
                    and (
                        current["sequence"] < previous_page["sequence"]
                        or current["sequence"] == previous_page["sequence"]
                        and current["document_id"] != previous_page["document_id"]
                        or current["sequence"] > previous_page["sequence"]
                        and current["document_id"] == previous_page["document_id"]
                    )
                )
            ):
                raise ValueError("device_browser_ai_request_invalid")
            identity = {
                key: current[key] for key in ("page_id", "sequence", "location")
            }
            prior_document = body.get("browser_ai_documents", {}).get(
                current["document_id"]
            )
            if prior_document is not None and prior_document != identity:
                raise ValueError("device_browser_ai_request_invalid")
        prior = calls.get(invocation_id)
        if prior:
            if (prior["request_digest"], prior["quote_micro"]) != (
                request_digest,
                quote_micro,
            ):
                raise ValueError("device_invocation_conflict")
            return {**prior, "admitted_now": False}
        if any(
            call.get("effect", {}).get("state") in {"admitted", "uncertain"}
            for call in calls.values()
        ):
            raise ValueError("device_effect_requires_reconciliation")
        used = sum(
            call.get("actual_micro", call["quote_micro"]) for call in calls.values()
        )
        if used + quote_micro > body["manifest"]["credit_ceiling_micro"]:
            raise ValueError("device_invocation_budget_exhausted")
        # Long AI-loop desktop tasks make one call per step; the founder-reviewed credit ceiling still bounds spend.
        if profile is None and len(calls) >= (
            400 if (body["manifest"].get("desktop_verification") or {}) else 128
        ):
            raise ValueError("device_invocation_limit")
        call = {
            "id": invocation_id,
            "request_digest": request_digest,
            "quote_micro": quote_micro,
            "state": "calling",
            "started_at": now.isoformat(),
        }
        if profile is not None:
            call.update(
                source_kind=browser_ai.SOURCE_KIND,
                phase=browser_phase["phase"],
                profile_digest=browser_phase["profile_digest"],
                checkpoint_digest=browser_phase["checkpoint_digest"],
                terminal_only=browser_phase.get("terminal_only") is True,
                **({"bounded_reads": True} if browser_phase.get("bounded_reads") is True else {}),
            )
            # Admission supersedes older actor proposals, including the interval
            # after their paid result but before their effect child is saved.
            body["browser_ai_latest_invocation_id"] = invocation_id
            body["browser_ai_source"] = dict(browser_phase["load"])
            body.setdefault("browser_ai_pages", {})[current["page_id"]] = {
                "sequence": current["sequence"],
                "document_id": current["document_id"],
            }
            body.setdefault("browser_ai_documents", {})[current["document_id"]] = (
                identity
            )
            if browser_phase.get("terminal_only") is True:
                body["browser_ai_terminal_only"] = True
        calls[invocation_id] = call
        binding = orders._qualification_binding(body)
        if binding is not None:
            from app.build import browser_qualification as qualification
            qualification.reserve_attempt(session, account, body, binding, invocation_id=invocation_id)
        row.provenance_json, row.updated_at = json.dumps(body), now
        session.commit()
        return {**call, "admitted_now": True}


def admit_zero_provider_proposal(
    venture_id,
    caller,
    order_id,
    *,
    invocation_id,
    request_digest,
    review_digest,
    candidate,
    source_summary,
):
    """Atomic source-issued typed navigation proposal; no provider or fake usage.

    This internal controller path preserves the same exact account/task/lease
    locks and durable one-use proposal consumed by device_effects.admit.
    """
    from dataclasses import asdict
    from app.build import device_browser_navigation as navigation

    if (
        not isinstance(invocation_id, str)
        or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", invocation_id)
        or not isinstance(request_digest, str)
        or not re.fullmatch(r"[a-f0-9]{64}", request_digest)
        or candidate.operation != "click"
        or not candidate.target
    ):
        raise ValueError("device_invocation_invalid")
    now = orders.utcnow()
    with orders.db()() as session:
        row, body, account = _locked(session, venture_id, caller, order_id)
        _check_executing(session, venture_id, body, account, now)
        if (
            not navigation.supported(body["manifest"])
            or body["review_digest"] != review_digest
        ):
            raise ValueError(navigation.UNAVAILABLE)
        invocations = body.setdefault("invocations", {})
        if invocation_id in invocations:
            raise ValueError("device_decision_already_attempted")
        if any(
            call.get("state") in {"calling", "uncertain"}
            or call.get("effect", {}).get("state") in {"admitted", "uncertain"}
            for call in invocations.values()
        ):
            raise ValueError("device_effect_requires_reconciliation")
        if len(invocations) >= 128:
            raise ValueError("device_invocation_limit")
        if (
            sum(
                call.get("actual_micro", call["quote_micro"])
                for call in invocations.values()
            )
            > body["manifest"]["credit_ceiling_micro"]
        ):
            raise ValueError("device_invocation_budget_exhausted")
        previous = body.get("typed_navigation_progress")
        if previous and (
            previous["session_id"] != source_summary["session_id"]
            or source_summary["progress_count"] <= previous["progress_count"]
            or source_summary["document_id"] == previous["document_id"]
            or (
                source_summary["page_id"] == previous["page_id"]
                and source_summary["sequence"] <= previous["sequence"]
            )
        ):
            raise ValueError(navigation.UNAVAILABLE)
        invocations[invocation_id] = {
            "id": invocation_id,
            "request_digest": request_digest,
            "source_kind": navigation.SOURCE_KIND,
            "no_provider_io": True,
            "quote_micro": 0,
            "provider_actual_micro": 0,
            "actual_micro": 0,
            "state": "completed",
            "started_at": now.isoformat(),
            "updated_at": now.isoformat(),
            "proposal": asdict(candidate),
            "source_summary": dict(source_summary),
        }
        body["typed_navigation_progress"] = dict(source_summary)
        row.provenance_json, row.updated_at = json.dumps(body), now
        session.commit()


def record_result(
    venture_id,
    caller,
    order_id,
    *,
    invocation_id,
    actual_micro,
    usage=None,
    force_hold=False,
    provider=None,
    fallback_reason=None,
    unknown_provider_micro=0,
    provider_attempts=None,
):
    """Trusted receipt: None means uncertain, retaining the entire call ceiling.

    Legacy late known receipts may resolve uncertainty after cancellation.
    Browser CUAI uncertainty is held permanently; it has no late-release path.
    Proven zero-output Azure timeout fallback retains that provider's ceiling
    separately from the known OpenAI usage, without granting a call replay.
    This does not settle the account or prove the native process has stopped.
    """
    if actual_micro is not None and (type(actual_micro) is not int or actual_micro < 0):
        raise ValueError("device_usage_invalid")
    expected_attempts = [{"provider": "azure", "state": "usage_unconfirmed", "held_micro": unknown_provider_micro}]
    if actual_micro is not None or isinstance(provider_attempts, list) and len(provider_attempts) == 2:
        expected_attempts.append({"provider": "openai", "state": "known" if actual_micro is not None else "usage_unconfirmed"})
    if (type(unknown_provider_micro) is not int or unknown_provider_micro < 0
            or unknown_provider_micro and fallback_reason != "azure_pre_output_timeout"
            or provider_attempts is not None and provider_attempts != expected_attempts
            or bool(unknown_provider_micro) != (provider_attempts is not None)):
        raise ValueError("device_usage_invalid")
    if provider is not None and (provider not in {"openai", "azure"} or actual_micro is None and not unknown_provider_micro):
        raise ValueError("device_usage_invalid")
    if fallback_reason is not None:
        from coding_agents.cu_ai_provider import FALLBACK_REASONS
        if (not isinstance(fallback_reason, str) or fallback_reason not in FALLBACK_REASONS or provider != "openai"):
            raise ValueError("device_usage_invalid")
    if usage is not None:
        if (
            actual_micro is None
            or not isinstance(usage, dict)
            or set(usage) != {"model", "input_tokens", "output_tokens"}
            or not isinstance(usage["model"], str)
            or not re.fullmatch(r"[A-Za-z0-9._/-]{1,100}", usage["model"])
            or any(
                type(usage[key]) is not int or not 0 <= usage[key] <= 1000000
                for key in ("input_tokens", "output_tokens")
            )
        ):
            raise ValueError("device_usage_invalid")
        usage = dict(usage)
    with orders.db()() as session:
        row, body, account = _locked(session, venture_id, caller, order_id, receipt_only=True)
        call = body.get("invocations", {}).get(invocation_id)
        if call is None:
            raise KeyError("device_invocation_not_found")
        browser_ai = call.get("source_kind") == "browser_cuai_text_v1"
        if (provider is not None or fallback_reason is not None or provider_attempts is not None) and not browser_ai:
            raise ValueError("device_usage_invalid")
        if browser_ai and call["state"] == "uncertain":
            raise ValueError("device_usage_conflict")
        if call["state"] == "completed":
            if actual_micro != call["provider_actual_micro"] or (
                usage is not None and usage != call.get("usage")
            ) or (provider is not None and provider != call.get("provider")) or (
                fallback_reason is not None and fallback_reason != call.get("fallback_reason")
            ) or (unknown_provider_micro != call.get("unknown_provider_micro", 0)
                  or provider_attempts != call.get("provider_attempts")):
                raise ValueError("device_usage_conflict")
            return call
        if actual_micro is None:
            call["state"] = "uncertain"
            if provider_attempts is not None:
                call.update(provider_attempts=provider_attempts, unknown_provider_micro=unknown_provider_micro,
                            provider=provider, fallback_reason=fallback_reason)
            if browser_ai:
                body["inference_reconciliation_required"] = True
        else:
            call.update(
                state="completed",
                provider_actual_micro=actual_micro,
                actual_micro=(
                    max(actual_micro + unknown_provider_micro, call["quote_micro"])
                    if browser_ai and force_hold
                    else actual_micro + unknown_provider_micro
                    if browser_ai
                    else min(actual_micro, call["quote_micro"])
                ),
            )
            if usage is not None:
                call["usage"] = usage
            if provider is not None:
                call["provider"] = provider
            if fallback_reason is not None:
                call["fallback_reason"] = fallback_reason
            if provider_attempts is not None:
                # ponytail: unknown Azure usage is charged at its admitted ceiling; invoice reconciliation can replace the bound.
                call.update(provider_attempts=provider_attempts, unknown_provider_micro=unknown_provider_micro)
            if force_hold or actual_micro + unknown_provider_micro > call["quote_micro"]:
                body["inference_reconciliation_required"] = True
        call["updated_at"] = orders.utcnow().isoformat()
        binding = orders._qualification_binding(body)
        if binding is not None:
            from app.build import browser_qualification as qualification
            qualification.settle_attempt(session, account, body, binding, invocation_id=invocation_id)
        row.provenance_json, row.updated_at = json.dumps(body), orders.utcnow()
        session.commit()
        return call


def save_browser_actor_proposal(
    venture_id, caller, order_id, *, invocation_id, candidate, qualification_rule_ids=None,qualification_read_url=None
):
    """Source-issued first actor action child; other phases cannot produce effects."""
    from dataclasses import asdict
    from app.build.device_browser_ai import profile_digest

    with orders.db()() as session:
        row, body, account = _locked(session, venture_id, caller, order_id)
        _check_executing(session, venture_id, body, account, orders.utcnow())
        invocations = body.get("invocations", {})
        parent = invocations.get(invocation_id)
        profile = (body["manifest"].get("general_browser_verification") or {}).get("execution_profile") or {}
        allowed = set() if orders.general_browser_read_only(body["manifest"]) else {"click", "type"}
        binding = orders._qualification_binding(body)
        owned_fixture = False
        if binding is not None:
            from app.build import browser_qualification as qualification
            from core.browser_qualification import OwnedFixtureGrant
            _,qualification_trial=qualification._authority(session, account, body, binding)
            if isinstance(binding, OwnedFixtureGrant):
                owned_fixture = True
                allowed = set(binding.allowed_operations) - {"wait"}
                if "select" in allowed:
                    allowed.add("type")
                if "check" in allowed:
                    allowed.add("click")
            if qualification_rule_ids is not None:
                if (not isinstance(binding, OwnedFixtureGrant) or not isinstance(qualification_rule_ids, (tuple, list))
                        or any(not isinstance(value, str) for value in qualification_rule_ids)
                        or len(set(qualification_rule_ids)) != len(qualification_rule_ids)
                        or not set(qualification_rule_ids) <= {rule.rule_id for rule in binding.rules if rule.method == "POST"}):
                    raise ValueError("browser_qualification_binding_invalid")
        elif qualification_rule_ids is not None:
            raise ValueError("browser_qualification_binding_invalid")
        if not owned_fixture and (parent and parent.get("bounded_reads") is True
                or profile.get("version") == "browser_cuai_text_v2" and profile.get("policy_version") == 2):
            allowed.update({"navigate", "scroll", "extract"})
        if (
            not parent
            or parent.get("source_kind") != "browser_cuai_text_v1"
            or parent.get("profile_digest") != profile_digest(profile)
            or parent.get("phase") != "actor"
            or parent["state"] != "completed"
            or body.get("browser_ai_latest_invocation_id") != invocation_id
            or any(
                value.get("state") in {"calling", "uncertain"}
                for value in invocations.values()
            )
            or parent.get("terminal_only")
            or body.get("browser_ai_terminal_only")
            or candidate.operation not in allowed
            or not candidate.target
        ):
            raise ValueError("device_effect_proposal_unavailable")
        if qualification_read_url is not None:
            if not owned_fixture or candidate.operation not in {'click','navigate'} or not isinstance(qualification_read_url,str):
                raise ValueError('device_effect_proposal_unavailable')
            receipts=[item for item in qualification_trial['requests'].values() if item['order_id']==body['id']]
            if any(item['state'] in {'pending','unknown'} or item.get('server_response_sent') is not True for item in receipts):
                raise ValueError('device_effect_proposal_unavailable')
            read_rule=binding.request_rule(url=qualification_read_url,method='GET',
                completed_rules=qualification.completed_rules(binding,receipts))
            if (sum(item['rule_id']==read_rule.rule_id for item in receipts)>=read_rule.max_requests
                    or orders.general_browser_read_only(body['manifest']) and any(
                        download.rule_id==read_rule.rule_id for download in binding.downloads)):
                raise ValueError('device_effect_proposal_unavailable')
        reviewed_read_url = None
        if (candidate.operation == "navigate" and isinstance(candidate.target, str)
                and candidate.target.startswith("document_") and not owned_fixture):
            from coding_agents.cu_ai_loop import Action
            from coding_agents.owned_browser import cuai_document_target
            source = body.get("browser_ai_source") or {}
            matches = [url for url in (body["manifest"]["general_browser_verification"].get("read_urls") or [])
                if cuai_document_target(source, Action(op="navigate", text=url)) == candidate.target]
            if (not orders.general_browser_read_only(body["manifest"]) or parent.get("bounded_reads") is not True
                    or source.get("qualified") is not True or len(matches) != 1):
                raise ValueError("device_effect_proposal_unavailable")
            reviewed_read_url = matches[0]
        child_id = invocation_id + "_a0"
        if child_id in invocations:
            raise ValueError("device_proposal_conflict")
        invocations[child_id] = {
            "id": child_id,
            "source_kind": "browser_cuai_actor_effect_v1",
            "parent_invocation_id": invocation_id,
            "profile_digest": parent["profile_digest"],
            "request_digest": parent["request_digest"],
            "state": "completed",
            "quote_micro": 0,
            "actual_micro": 0,
            "provider_actual_micro": 0,
            "no_provider_io": True,
            "proposal": asdict(candidate),
        }
        if qualification_rule_ids is not None:
            invocations[child_id]["proposal"]["qualification_rule_ids"] = list(qualification_rule_ids)
        if reviewed_read_url is not None:
            invocations[child_id]["read_url"] = reviewed_read_url
        if qualification_read_url is not None:
            invocations[child_id].update(qualification_read_url=qualification_read_url,execution_id=body['execution_id'])
        row.provenance_json, row.updated_at = json.dumps(body), orders.utcnow()
        session.commit()
        return child_id
