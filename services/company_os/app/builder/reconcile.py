"""Credit-safe recovery without cloud or model I/O.

Unknown outcomes stay held until an explicit receipt arrives or bounded platform
absorption releases the founder after managed runtime cleanup.
"""
import json
from datetime import timedelta

from sqlalchemy import select

from app.builder.model_proxy import _settle, reconcile_stale_calls
from app.credits import pools
from app.credits._txn import naive_utc
from app.db.models import CreditReservation, Job
from app.db.supymem_models import BuildEvent, BuildModelCall
from app.state import db


PLATFORM_ABSORPTION_SECONDS = 300


def _absorb_stale_platform_calls(session, ctx, rows, spec, *, now):
    """Refund founders after a stopped platform call cannot produce a receipt.

    The provider request is never retried.  We wait until all runtime cleanup
    is proven, retain the uncertainty on the call, and emit a durable audit
    event showing that Supymem accepted the possible upstream cost.
    """
    from app.builder import lifecycle
    config = spec.get("model_config", {})
    if (not lifecycle.managed(spec) or not lifecycle.cleanup_complete(spec)
            or not spec.get("credentials_revoked")
            or (config.get("source"), config.get("provider"), config.get("model"))
                != ("platform_default", "azure_foundry", "DeepSeek-V4-Pro")):
        return 0
    cutoff = now - timedelta(seconds=PLATFORM_ABSORPTION_SECONDS)
    absorbed = 0
    for row in rows:
        if row.state != "uncertain" or naive_utc(row.updated_at) > cutoff:
            continue
        pools.absorb_platform_child(session, ctx, reservation_id=row.reservation_id)
        row.state, row.settled_at, row.updated_at = "released", now, now
        row.error_code = row.error_code or "build_provider_outcome_uncertain"
        ctx.run.event_seq += 1
        session.add(BuildEvent(build_run_id=ctx.run.id, seq=ctx.run.event_seq, ts=now,
            kind="provider_cost_absorbed", payload=json.dumps({
                "message": "Supymem absorbed an unverified provider charge; founder credits were released.",
                "invocation_id": row.id, "maximum_micro": row.ceiling_micro,
                "reason": row.error_code,
            }, sort_keys=True)))
        absorbed += 1
    return absorbed


def reconcile_held_builds(limit=200, now=None):
    """Invoked by the existing credit sweep independently of the desktop.

    Settle stored usage; release only allocated calls that never reached calling;
    close a never-leased launch atomically. A leased/cloud task or an uncertain
    provider request requires the external controller's observed cleanup/usage.
    """
    reconcile_stale_calls()
    with db()() as session:
        ids = list(session.scalars(select(CreditReservation.id).where(CreditReservation.is_pool.is_(True),
            CreditReservation.status == "reconciling").order_by(CreditReservation.created_at).limit(limit)))
    now = naive_utc(now)
    result = {"reviewed": 0, "closed": 0, "pending": 0, "absorbed": 0}
    for pool_id in ids:
        with db()() as session:
            ctx = pools.lock_pool(session, pool_id)
            if ctx.pool.status != "reconciling":
                continue
            rows = session.scalars(select(BuildModelCall).where(BuildModelCall.build_run_id == ctx.run.id)
                                   .order_by(BuildModelCall.sequence).with_for_update()).all()
            for row in rows:
                if row.state == "responded":
                    _settle(session, ctx, row)
                elif row.state == "allocated":
                    pools.release_child(session, ctx, reservation_id=row.reservation_id, no_external_call=True)
                    row.state, row.updated_at = "released", naive_utc()
                    row.error_code = "build_cancelled_before_provider"
            spec = json.loads(ctx.run.spec)
            result["absorbed"] += _absorb_stale_platform_calls(session, ctx, rows, spec, now=now)
            job = session.get(Job, ctx.run.job_id, with_for_update=True) if ctx.run.job_id else None
            if job and job.status == "scheduled" and job.attempts == 0 and spec.get("cleanup_state") == "not_started":
                job.status, job.finished_at = "cancelled", naive_utc()
                from app.builder.lifecycle import managed, never_started
                if managed(spec):
                    never_started(spec)
                spec["cleanup_state"] = "complete"
                ctx.run.spec = json.dumps(spec, sort_keys=True)
            outstanding = session.scalar(select(CreditReservation.id).where(CreditReservation.parent_id == pool_id,
                CreditReservation.status.in_(pools.OPEN_CHILD)).limit(1))
            result["reviewed"] += 1
            if spec.get("lifecycle_version") == 2:
                from app.builder.finalization import finish
                if finish(session, ctx, spec):
                    result["closed"] += 1
                else:
                    result["pending"] += 1
            elif spec.get("cleanup_state") == "complete" and not outstanding:
                if ctx.run.state == "cancelling":
                    from app.core.fsm import advance
                    advance(session, ctx.run, "cancelled", reason="build_usage_and_cleanup_reconciled")
                ctx.run.finished_at = ctx.run.finished_at or naive_utc()
                pools.close_pool(session, ctx)
                result["closed"] += 1
            else:
                result["pending"] += 1
            session.commit()
    return result
