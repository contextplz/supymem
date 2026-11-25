"""Durable one-use native effects bound to a metered server-produced proposal.

This does not call a browser and is not a public grant endpoint. The native
broker must enforce its own exact live target, network policy and consequential
confirmation before invoking admission. Never use a model response as consent.
"""

from dataclasses import asdict
import json
import re

from app.build import device_invocations as calls, device_orders as orders


def save_proposal(venture_id, caller, order_id, *, invocation_id, candidate):
    value = asdict(candidate)
    with orders.db()() as session:
        row, body, account = calls._locked(session, venture_id, caller, order_id)
        calls._check_executing(session, venture_id, body, account, orders.utcnow())
        invocation = body.get("invocations", {}).get(invocation_id)
        if not invocation or invocation["state"] != "completed":
            raise ValueError("device_proposal_usage_unverified")
        if invocation.get("source_kind") == "browser_cuai_text_v1":
            raise ValueError("device_effect_proposal_unavailable")
        prior = invocation.get("proposal")
        if prior is not None and prior != value:
            raise ValueError("device_proposal_conflict")
        invocation["proposal"] = value
        row.provenance_json, row.updated_at = json.dumps(body), orders.utcnow()
        session.commit()


def admit(
    venture_id, caller, order_id, *, invocation_id, observation_digest, effect_digest, qualification=None
):
    """Consume a known reviewed effect proposal once before native I/O; ambiguity is final.

    Returns False on exact replay, never permission to repeat the effect. This
    internal primitive expects the broker to have independently checked consent;
    its hashes identify an effect but do not classify its consequences as safe.
    """
    if any(
        not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value)
        for value in (observation_digest, effect_digest)
    ):
        raise ValueError("device_effect_digest_invalid")
    with orders.db()() as session:
        row, body, account = calls._locked(session, venture_id, caller, order_id)
        calls._check_executing(session, venture_id, body, account, orders.utcnow())
        invocation = body.get("invocations", {}).get(invocation_id)
        proposal = invocation.get("proposal") if invocation else None
        desktop = body["manifest"].get("desktop_verification")
        general = body["manifest"].get("general_browser_verification") or {}
        cuai = general.get("execution_profile") is not None
        cuai_child = (
            invocation
            and invocation.get("source_kind") == "browser_cuai_actor_effect_v1"
        )
        parent = body.get("invocations", {}).get(invocation.get("parent_invocation_id"), {}) if invocation else {}
        source_reads = bool(cuai_child and parent.get("bounded_reads") is True
            and parent.get("state") == "completed" and parent.get("phase") == "actor"
            and parent.get("source_kind") == "browser_cuai_text_v1"
            and parent.get("profile_digest") == invocation.get("profile_digest"))
        if cuai_child:
            from app.build.device_browser_ai import profile_digest
            current_profile_digest = profile_digest(general.get("execution_profile"))
        if cuai_child and (
            invocation.get("state") != "completed"
            or parent.get("state") != "completed"
            or parent.get("phase") != "actor"
            or parent.get("source_kind") != "browser_cuai_text_v1"
            or invocation.get("parent_invocation_id")
            != body.get("browser_ai_latest_invocation_id")
            or parent.get("profile_digest") != current_profile_digest
            or invocation.get("profile_digest") != parent.get("profile_digest")
            or any(
                value.get("state") in {"calling", "uncertain"}
                for value in body.get("invocations", {}).values()
            )
        ):
            raise ValueError("device_effect_proposal_unavailable")
        cuai_type = bool(
            cuai_child
            and proposal
            and proposal.get("operation") == "type"
            and general.get("allow_type")
            and type(proposal.get("value_index")) is int
            and 0 <= proposal["value_index"] < len(general.get("fill_values", {}))
        )
        desktop_effect = bool(
            desktop
            and "desktop.interact" in body["manifest"]["actions"]
            and proposal
            and proposal.get("operation") in {"click", "type", "key", "scroll"}
            and (
                proposal.get("operation") != "type"
                or (
                    type(proposal.get("value_index")) is int
                    and 0
                    <= proposal["value_index"]
                    < len(desktop.get("reviewed_text") or [])
                )
            )
        )
        browser_effect = bool(
            not desktop
            and ("browser.interact" in body["manifest"]["actions"]
                 or orders.general_browser_read_only(body["manifest"]) and cuai_child
                 and (source_reads or general.get("execution_profile", {}).get("version") == "browser_cuai_text_v2")
                 and proposal and proposal.get("operation") in {"navigate", "scroll", "extract"})
            and proposal
            and (proposal.get("operation") == "click" or cuai_type
                 or cuai_child and (source_reads
                     or general.get("execution_profile", {}).get("version") == "browser_cuai_text_v2"
                     and general.get("execution_profile", {}).get("policy_version") == 2)
                 and proposal.get("operation") in {"navigate", "scroll", "extract"})
            and (not cuai or cuai_child)
        )
        binding = orders._qualification_binding(body)
        if invocation and invocation.get("read_url") is not None:
            from coding_agents.cu_ai_loop import Action
            from coding_agents.owned_browser import cuai_document_target
            reviewed_url = invocation["read_url"]
            source = body.get("browser_ai_source") or {}
            if (not source_reads or not orders.general_browser_read_only(body["manifest"])
                    or reviewed_url not in (general.get("read_urls") or [])
                    or source.get("qualified") is not True or proposal.get("operation") != "navigate"
                    or proposal.get("target") != cuai_document_target(source, Action(op="navigate", text=reviewed_url))):
                raise ValueError("device_effect_proposal_unavailable")
        qualified_request = None
        if binding is not None:
            from app.build import browser_qualification as qualification_service
            from core.browser_qualification import OwnedFixtureGrant, request_binding_digest
            qualification_service._authority(session, account, body, binding)
            if isinstance(binding, OwnedFixtureGrant):
                allowed = set(binding.allowed_operations) - {"wait"}
                if "select" in allowed:
                    allowed.add("type")
                if "check" in allowed:
                    allowed.add("click")
                browser_effect = bool(cuai_child and proposal and proposal.get("operation") in allowed)
            if qualification is not None:
                if (not isinstance(binding, OwnedFixtureGrant) or not proposal or not isinstance(qualification, dict)
                        or set(qualification) != {"grant_digest", "rule_id", "request_id", "request_digest"}
                        or qualification["grant_digest"] != binding.grant_digest
                        or qualification["rule_id"] not in proposal.get("qualification_rule_ids", [])):
                    raise ValueError("browser_qualification_binding_invalid")
                rule = next((value for value in binding.rules if value.rule_id == qualification["rule_id"] and value.method == "POST"), None)
                if rule is None or qualification["request_digest"] != request_binding_digest(binding, rule, qualification["request_id"]):
                    raise ValueError("browser_qualification_binding_invalid")
                qualified_request = dict(qualification)
        elif qualification is not None:
            raise ValueError("browser_qualification_binding_invalid")
        if (
            not proposal
            or not proposal.get("target")
            or proposal.get("observation_digest") != observation_digest
            or not (desktop_effect or browser_effect)
        ):
            raise ValueError("device_effect_proposal_unavailable")
        if invocation.get("effect"):
            if (invocation["effect"]["digest"] != effect_digest
                    or invocation["effect"].get("qualification") != qualified_request):
                raise ValueError("device_effect_conflict")
            return False
        if any(
            value.get("effect", {}).get("state") in {"admitted", "uncertain"}
            for value in body.get("invocations", {}).values()
        ):
            raise ValueError("device_effect_requires_reconciliation")
        invocation["effect"] = {
            "digest": effect_digest,
            "state": "admitted",
            "admitted_at": orders.utcnow().isoformat(),
        }
        if qualified_request is not None:
            invocation["effect"]["qualification"] = qualified_request
        row.provenance_json, row.updated_at = json.dumps(body), orders.utcnow()
        session.commit()
        return True


def bind_qualification(venture_id, caller, order_id, *, invocation_id, effect_digest, qualification):
    """Bind the paused exact request once on its already admitted UI effect."""
    from core.browser_qualification import OwnedFixtureGrant, request_binding_digest
    from app.build import browser_qualification as qualification_service
    with orders.db()() as session:
        row, body, account = calls._locked(session, venture_id, caller, order_id)
        calls._check_executing(session, venture_id, body, account, orders.utcnow())
        binding = orders._qualification_binding(body)
        if not isinstance(binding, OwnedFixtureGrant):
            raise ValueError("browser_qualification_binding_invalid")
        qualification_service._authority(session, account, body, binding)
        invocation = body.get("invocations", {}).get(invocation_id)
        proposal = invocation.get("proposal") if invocation else None
        effect = invocation.get("effect") if invocation else None
        parent = body.get("invocations", {}).get(invocation.get("parent_invocation_id")) if invocation else None
        if (not effect or effect.get("digest") != effect_digest or effect.get("state") != "admitted"
                or invocation.get("source_kind") != "browser_cuai_actor_effect_v1"
                or invocation.get("parent_invocation_id") != body.get("browser_ai_latest_invocation_id")
                or invocation.get("state") != "completed" or invocation.get("profile_digest") != binding.profile_digest
                or not parent or parent.get("state") != "completed" or parent.get("phase") != "actor"
                or parent.get("source_kind") != "browser_cuai_text_v1" or parent.get("profile_digest") != binding.profile_digest
                or any(value.get("state") in {"calling", "uncertain"} for value in body.get("invocations", {}).values())
                or not proposal or not isinstance(qualification, dict)
                or set(qualification) != {"grant_digest", "rule_id", "request_id", "request_digest"}
                or qualification["grant_digest"] != binding.grant_digest
                or qualification["rule_id"] not in proposal.get("qualification_rule_ids", [])):
            raise ValueError("browser_qualification_effect_unavailable")
        rule = next((value for value in binding.rules if value.rule_id == qualification["rule_id"] and value.method == "POST"), None)
        if rule is None or qualification["request_digest"] != request_binding_digest(binding, rule, qualification["request_id"]):
            raise ValueError("browser_qualification_binding_invalid")
        previous = effect.get("qualification")
        if previous is not None:
            if previous != qualification:
                raise ValueError("browser_qualification_effect_binding_changed")
            return False
        effect["qualification"] = dict(qualification)
        row.provenance_json, row.updated_at = json.dumps(body), orders.utcnow()
        session.commit()
        return True


PRE_DISPATCH_CODES = frozenset({"desktop_fresh_observation_required"})


def _attested_pre_dispatch_code(code: str | None) -> bool:
    return (
        code in PRE_DISPATCH_CODES
        or isinstance(code, str)
        and re.fullmatch(r"desktop_pre_dispatch_[a-z0-9_]{1,60}", code) is not None
    )


BROWSER_PRE_DISPATCH_CODES = frozenset(
    {
        "browser_pre_dispatch_fresh_observation_required",
        "browser_pre_dispatch_target_changed",
        "browser_pre_dispatch_scope_retired",
    }
)


def receipt(
    venture_id,
    caller,
    order_id,
    *,
    invocation_id,
    effect_digest,
    outcome,
    pre_dispatch_code=None,
):
    if outcome not in {"returned", "uncertain", "not_dispatched"}:
        raise ValueError("device_effect_outcome_invalid")
    if (
        outcome == "not_dispatched"
        and not (
            _attested_pre_dispatch_code(pre_dispatch_code)
            or pre_dispatch_code in BROWSER_PRE_DISPATCH_CODES
        )
        or outcome != "not_dispatched"
        and pre_dispatch_code is not None
    ):
        raise ValueError("device_effect_pre_dispatch_code_invalid")
    with orders.db()() as session:
        row, body, account = calls._locked(
            session, venture_id, caller, order_id, receipt_only=True
        )
        invocation = body.get("invocations", {}).get(invocation_id)
        if outcome == "not_dispatched":
            allowed = (
                body.get("manifest", {}).get("desktop_verification")
                and _attested_pre_dispatch_code(pre_dispatch_code)
                or invocation
                and invocation.get("source_kind") == "browser_cuai_actor_effect_v1"
                and pre_dispatch_code in BROWSER_PRE_DISPATCH_CODES
            )
            if not allowed:
                raise ValueError("device_effect_pre_dispatch_code_invalid")
        effect = invocation.get("effect") if invocation else None
        if not effect or effect["digest"] != effect_digest:
            raise ValueError("device_effect_receipt_unbound")
        if effect.get("qualification"):
            from app.build import browser_qualification as qualification_service
            binding = orders._qualification_binding(body)
            _, trial = qualification_service._authority(session, account, body, binding, receipt_only=True)
            expected = effect["qualification"]
            delivered = trial["requests"].get(expected["request_id"])
            if delivered is not None and (any(delivered.get(key) != value for key, value in expected.items())
                    or delivered.get("effect_digest") != effect_digest
                    or delivered.get("invocation_id") != invocation_id
                    or delivered.get("execution_id") != body.get("execution_id")):
                raise ValueError("browser_qualification_binding_invalid")
            if outcome == "returned" and (not delivered or delivered.get("state") not in {"committed", "rejected_before_mutation"}
                    or delivered.get("server_response_sent") is not True):
                raise ValueError("browser_qualification_request_protected")
            if outcome == "not_dispatched" and delivered is not None:
                raise ValueError("browser_qualification_request_protected")
        if effect["state"] not in {"admitted", outcome} and not (
            effect["state"] == "uncertain" and outcome == "not_dispatched"
        ):
            raise ValueError("device_effect_receipt_conflict")
        if (
            effect["state"] == "uncertain"
            and outcome == "not_dispatched"
            and (body.get("state") != "cancelled" or not body.get("native_stop"))
        ):
            raise ValueError("device_effect_receipt_conflict")
        if (
            effect["state"] == "not_dispatched"
            and effect.get("pre_dispatch_code") != pre_dispatch_code
        ):
            raise ValueError("device_effect_receipt_conflict")
        effect.update(state=outcome, returned_at=orders.utcnow().isoformat())
        if pre_dispatch_code is not None:
            effect["pre_dispatch_code"] = pre_dispatch_code
        row.provenance_json, row.updated_at = json.dumps(body), orders.utcnow()
        session.commit()
