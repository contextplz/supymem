"""Managed static lifecycle v2: purpose-specific cleanup, never legacy repricing.

All helpers mutate an already locked account→run→pool context; callers commit
with the current job lease. No cloud/provider I/O or runtime activation here.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_CEILING
import json
import math

from app.credits import pools
from app.credits._txn import naive_utc
from app.core.fsm import advance
from .model_proxy import BuildModelError, digest

INCLUDED = {"policy_id": "static_landing_included_v1", "founder_price_micro": 0, "basis": "platform_included",
            "aws_actual_cost": None, "source_zip_max_bytes": 33554432, "report_evidence_max_bytes": 10485760,
            "log_max_bytes_per_task": 5242880, "retention_days": 30, "internet_egress": False}


def managed(spec):
    return spec.get("lifecycle_version") in {2, 3}


def validate_runtime(runtime):
    """A well-formed test/review policy is not an accepted production runtime."""
    try:
        from .budgets import MANAGED_TOOL_NAMES, model_pricing
        config = runtime["model_config"]
        price = model_pricing("DeepSeek-V4-Pro")
        if (runtime["model"] != "DeepSeek-V4-Pro" or config.get("model") != "DeepSeek-V4-Pro"
                or config.get("provider") != "azure_foundry" or config.get("source") != "platform_default"
                or not price or config.get("pricing") != price):
            raise ValueError("build_model_pricing_unverified")
        parts = [runtime[k] for k in ("model_ceiling_micro", "builder_compute_ceiling_micro", "qa_compute_ceiling_micro")]
        bounds = runtime["deadlines"]
        if (runtime.get("lifecycle_version") != 2 or any(type(v) is not int or v <= 0 for v in parts)
                or runtime["compute_ceiling_micro"] != sum(parts[1:]) or runtime["ceiling_micro"] != sum(parts)
                or runtime["builder_profile"] != runtime["fargate_profile"] or runtime["artifact_ceiling_micro"] != 0 or runtime["included_overhead"] != INCLUDED
                or runtime["runtime_policy"].get("tool_names") != MANAGED_TOOL_NAMES
                or set(bounds) != {"queue_seconds", "builder_seconds", "builder_stop_seconds", "qa_seconds", "qa_stop_seconds", "margin_seconds"}
                or any(type(value) is not int or not 1 <= value <= 3600 for value in bounds.values())):
            raise ValueError()
        for purpose in ("builder", "qa"):
            pricing = runtime["task_pricing"][purpose]
            validate_rate(pricing, runtime[purpose + "_profile"])
            seconds = bounds[purpose + "_seconds"] + bounds[purpose + "_stop_seconds"]
            cap = math.ceil(Decimal(pricing["micro_per_second"]) * max(60, seconds))
            if runtime[purpose + "_compute_ceiling_micro"] != cap:
                raise ValueError()
    except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
        raise BuildModelError("build_lifecycle_policy_invalid") from exc
    return runtime


def validate_rate(rate, profile):
    value = Decimal(rate["micro_per_second"])
    observed, expires = (datetime.fromisoformat(rate[key]) for key in ("observed_at", "expires_at"))
    now = datetime.now(timezone.utc)
    if (not value.is_finite() or value <= 0 or rate["minimum_seconds"] != 60 or rate["currency"] != "USD"
            or rate["unit"] != "task_second" or rate["profile_digest"] != digest(profile)
            or rate["region"] != profile["region"] or rate["cpu"] != 2048 or rate["memory_mb"] != 4096
            or rate["architecture"] != profile.get("architecture", "X86_64") or not rate["source"].startswith("https://")
            or not rate["policy_id"] or observed.tzinfo is None or expires.tzinfo is None or not observed <= now < expires):
        raise ValueError("build_compute_rate_invalid")


def initial(runtime, now):
    validate_runtime(runtime)
    bounds = runtime["deadlines"]
    return {"phase": "builder_provisioning", "queue_deadline": (now + timedelta(seconds=bounds["queue_seconds"])).isoformat(),
            "total_deadline": (now + timedelta(seconds=sum(bounds.values()))).isoformat(),
            "builder": {"cleanup_state": "not_started"}, "qa_required": True, "review": {"status": "not_ready"}}


def allocate(session, ctx, spec):
    runtime = validate_runtime(spec["runtime"])
    holds = {}
    for purpose in ("builder", "qa"):
        rid = "crv_" + digest({"run": ctx.run.id, "generation": spec["generation"], "purpose": purpose})[:40]
        pools.allocate(session, ctx, reservation_id=rid, amount_micro=runtime[purpose + "_compute_ceiling_micro"],
                       purpose="build.compute." + purpose, provider="aws")
        holds[purpose] = rid
    spec["lifecycle"]["holds"] = holds
    spec["lifecycle"]["builder"]["cleanup_state"] = "pending"
    return {"compute": holds["builder"]}


def deadline_reason(spec):
    if spec.get("lifecycle_version") == 3:
        from app.build.subscription_static_controller import deadline_reason as source_deadline
        return source_deadline(spec)
    now = naive_utc()
    lifecycle, bounds = spec["lifecycle"], spec["runtime"]["deadlines"]
    if now >= datetime.fromisoformat(lifecycle["total_deadline"]):
        return "build_runtime_deadline"
    launch = spec.get("launch", {})
    if not launch.get("first_attempt_at") and now >= datetime.fromisoformat(lifecycle["queue_deadline"]):
        return "build_queue_deadline"
    builder = lifecycle["builder"]
    if launch.get("first_attempt_at") and builder.get("cleanup_state") != "complete":
        if now >= datetime.fromisoformat(launch["first_attempt_at"]) + timedelta(seconds=bounds["builder_seconds"] + (bounds["builder_stop_seconds"] if builder.get("normal_stop_intent") else 0)):
            return "build_runtime_deadline"
    return None


def close_admission(spec):
    spec["builder_admission_closed_at"] = spec.get("builder_admission_closed_at") or naive_utc().isoformat()
    spec["lifecycle"]["phase"] = "builder_draining"


def retire(session, ctx, spec, final_sdk):
    capture = spec.get("artifact_capture", {})
    if not spec.get("builder_admission_closed_at") or not spec.get("artifact_verified") or capture.get("state") != "stored":
        raise BuildModelError("build_normal_completion_unverified")
    intent = {"command_id": spec["managed_command"]["id"], "command_digest": spec["managed_command"]["body_digest"],
              "source_artifact_id": ctx.run.artifact_id, "source_receipt_digest": digest(capture), "final_sdk": final_sdk,
              "generation": spec["generation"]}
    builder = spec["lifecycle"]["builder"]
    if builder.get("normal_stop_intent") and builder["normal_stop_intent"] != intent:
        raise BuildModelError("build_normal_completion_conflict")
    builder["normal_stop_intent"] = intent
    spec["lifecycle"]["phase"] = spec["phase"] = "builder_stopping"
    if ctx.run.state == "running":
        advance(session, ctx.run, "syncing_artifacts", actor="build", reason="Source captured and final model receipts verified")


def terminal_observation(observation, expected_arn):
    if observation.get("state") != "STOPPED" or observation.get("task_arn") != expected_arn:
        raise BuildModelError("build_terminal_identity_invalid")
    return {key: observation.get(key) for key in ("task_arn", "state", "pull_started_at", "stopped_at", "stop_code")}


def settle_task(session, ctx, spec, purpose, observation, expected_arn):
    """Known usage only. Repeated observations cannot change an accepted bill."""
    result = terminal_observation(observation, expected_arn)
    slot = spec["lifecycle"].setdefault(purpose, {})
    prior = slot.get("terminal_observation")
    if prior:
        # AWS observations may initially omit billable timestamps. Only fill
        # absent fields; never rewrite a known terminal identity or accepted bill.
        if any(value is not None and result.get(key) != value for key, value in prior.items()):
            raise BuildModelError("build_terminal_observation_conflict")
    slot.update(cleanup_state="complete", terminal_observation=result)
    if slot.get("settlement_receipt"):
        return
    pricing = spec["runtime"]["task_pricing"][purpose]
    try:
        start, stop = (datetime.fromisoformat(result[key]) for key in ("pull_started_at", "stopped_at"))
        rate = Decimal(pricing["micro_per_second"])
        if start.tzinfo is None or stop.tzinfo is None or stop < start or not rate.is_finite() or rate <= 0:
            raise ValueError()
        # The approved snapshot remains valid for settlement after its expiry;
        # expiry prevents new work, not charging already observed past usage.
        seconds = max(pricing["minimum_seconds"], math.ceil((stop - start).total_seconds()))
        amount = int((rate * seconds).quantize(Decimal(1), rounding=ROUND_CEILING))
    except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
        raise BuildModelError("build_compute_usage_unverified") from exc
    paid = pools.settle_child(session, ctx, reservation_id=spec["lifecycle"]["holds"][purpose], actual_micro=amount,
        metadata={"task_arn": expected_arn, "task_purpose": purpose, "observation_digest": digest(result),
                  "pricing_digest": digest(pricing), "billed_seconds": seconds, "source": pricing["source"]})
    slot["settlement_receipt"] = {"reservation_id": spec["lifecycle"]["holds"][purpose], "ledger_id": paid.ledger_id,
                                  "raw_micro": amount, "billed_seconds": seconds, "pricing_digest": digest(pricing)}
    if ctx.pool.status == "reconciling":
        revised = json.loads(ctx.run.spec)
        spec.update({key: revised[key] for key in ("credentials_revoked", "reconciliation_required", "reconciliation_reason") if key in revised})


def release_unlaunched_qa(session, ctx, spec):
    if spec["lifecycle"].get("qa_id"):
        raise BuildModelError("build_qa_launch_requires_reconciliation")
    pools.release_child(session, ctx, reservation_id=spec["lifecycle"]["holds"]["qa"], no_external_call=True)
    spec["lifecycle"]["qa"] = {"cleanup_state": "complete", "no_external_call": True}


def cleanup_complete(spec):
    lifecycle = spec.get("lifecycle", {})
    return all(lifecycle.get(purpose, {}).get("cleanup_state") == "complete"
               and (lifecycle[purpose].get("terminal_observation", {}).get("state") == "STOPPED"
                    or lifecycle[purpose].get("no_external_call") is True) for purpose in ("builder", "qa"))


def never_started(spec):
    for purpose in ("builder", "qa"):
        spec["lifecycle"][purpose] = {"cleanup_state": "complete", "no_external_call": True}
