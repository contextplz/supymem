"""An approved budgeted loop; all effects remain with existing experiment runners."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from textwrap import shorten

from sqlalchemy import select

from app.autopilot.authority import founder_scope
from app.chat.recovery import log_founder_exception
from app.credits.errors import CreditsUnavailable
from app.credits.policy import get_policy
from app.db.models import Approval, CreditLedgerEntry, CreditReservation, Job
from app.db.session import serialize_writes
from app.db.supymem_models import Experiment
from app.experiments import autonomous, belief_planner, beliefs, engine, ledger, methods
from app.state import db
from app.tenancy import current_or_none
from core.clock import as_utc, utcnow



def enabled():
    from app.core.local_mode import is_local

    if is_local():
        return autonomous._local_enabled("run_for_me_experiments")
    flags = get_policy("feature_flags")
    return isinstance(flags, dict) and flags.get("experiments_autopilot_v1") is True


def validate_standing(plan):
    if (plan.get("renewal") != "weekly_until_revoked" or plan.get("expires_at") is not None or
            type(plan.get("weekly_budget_micro")) is not int or plan["weekly_budget_micro"] < 0 or
            type(plan.get("max_emails_per_day")) is not int or not 0 <= plan["max_emails_per_day"] <= 15 or
            any(not isinstance(plan.get(field), list) or not plan[field] or
                any(type(value) is not str for value in plan[field])
                for field in ("allowed_methods", "allowed_audiences")) or
            sum(test["ceiling_micro"] for test in plan["tests"] if test["state"] == "queued") > plan["weekly_budget_micro"]):
        raise ValueError("Your standing plan needs a weekly budget, saved methods and audiences, and an email cap from 0 to 15.")


def _allowed(plan, spec):
    if not plan.get("renewal"):
        return True
    return (enabled() and not plan.get("revoked_at") and
        type(plan.get("max_emails_per_day")) is int and 0 <= plan["max_emails_per_day"] <= 15 and
        belief_planner.runner(spec) in plan["allowed_methods"] and
        spec.get("icp", {}).get("who", "") in plan["allowed_audiences"] and
        spec.get("direction") == plan["direction"] and
        (spec.get("runner") != "outreach_split" or plan["max_emails_per_day"] > 0))


def reserve(plan, amount):
    if type(amount) is not int or amount < 0 or plan["reserved_micro"] + amount > plan["ceiling_micro"]:
        raise CreditsUnavailable("Your market experiment budget is exhausted.")
    plan["reserved_micro"] += amount
    if amount and plan["reserved_micro"] == plan["ceiling_micro"]:
        plan["state"] = "budget_exhausted"


def approve(venture_id, payload):
    with db()() as session:
        serialize_writes(session, key="market-beliefs:" + venture_id)
        if enabled():
            serialize_writes(session, key="market-experiments:" + venture_id)
        saved = beliefs.row(session, venture_id)
        context = json.loads(saved.context) if saved else {}
        plan = context.get("plan") or {}
        if plan.get("id") != payload.get("plan_id") or plan.get("fingerprint") != payload.get("fingerprint"):
            raise ValueError("Your market plan changed. Review a fresh approval card.")
        if plan.get("renewal"):
            if not enabled():
                raise ValueError("Your standing plan is unavailable. Review a fresh approval card.")
            if plan["state"] == "draft":
                validate_standing(plan)
                fingerprint = autonomous._fingerprint({key: value for key, value in plan.items() if key != "fingerprint"})
                if fingerprint != plan["fingerprint"]:
                    raise ValueError("Your market plan changed. Review a fresh approval card.")
        if plan["state"] != "draft":
            return context
        now = utcnow()
        plan.update(state="active", approved_at=now.isoformat(), deadline=(now + timedelta(days=7)).isoformat())
        if plan.get("renewal"):
            plan["period_started_at"] = now.isoformat()
        for test in plan["tests"]:
            test["deadline"] = plan["deadline"]
            experiment = session.get(Experiment, test["experiment_id"])
            if not experiment or experiment.venture_id != venture_id or experiment.workspace_id != saved.workspace_id:
                raise ValueError("Your planned experiment is unavailable.")
            experiment_context = json.loads(experiment.context)
            spec = experiment_context.get("spec") or {}
            if spec.get("fingerprint") != test["fingerprint"] or spec.get("ceiling_micro") != test["ceiling_micro"]:
                raise ValueError("Your planned experiment changed. Review a fresh approval card.")
            if not _allowed(plan, spec):
                raise ValueError("Your planned experiment is outside your approved methods or audiences.")
            if plan.get("renewal") and test["state"] == "running":
                start = experiment_context.get("prepared_at") or experiment.started_at or now
                start = datetime.fromisoformat(start) if isinstance(start, str) else start
                test["deadline"] = (as_utc(start) + timedelta(days=spec.get("max_duration_days", 7))).isoformat()
            experiment.context = json.dumps({**experiment_context, "belief_plan_id": plan["id"]})
            if ledger.quality_enabled():
                binding = methods.evidence_binding(spec)
                for belief in context["beliefs"]:
                    if belief["id"] == test["belief_id"]:
                        belief["evidence_bindings"] = [*belief.get("evidence_bindings", []), binding]
            if test["state"] == "running":
                for belief in context["beliefs"]:
                    if belief["id"] == test["belief_id"]:
                        belief["status"] = "testing"
        saved.context = json.dumps(context)
        session.commit()
    return context


def approve_checkpoint(session, parent_context, experiment_context, spec):
    plan_id = parent_context.get("belief_plan_id")
    if not plan_id:
        return
    saved = beliefs.row(session, spec["venture_id"])
    context = json.loads(saved.context) if saved else {}
    plan = context.get("plan") or {}
    parent = next((test for test in plan.get("tests", []) if test["experiment_id"] == spec["checkpoint_parent_id"]), None)
    if (plan.get("id") != plan_id or plan.get("state") not in {"active", "budget_exhausted"}
            or utcnow() >= as_utc(datetime.fromisoformat(plan["deadline"])) or not parent or not _allowed(plan, spec)
            or spec["ceiling_micro"] > parent.get("reserved_micro", parent["ceiling_micro"])):
        raise ValueError("Your alternative is outside your remaining approved plan budget.")
    allocation = parent.get("reserved_micro", parent["ceiling_micro"])
    parent.update(state="done", reserved_micro=allocation - spec["ceiling_micro"], ended_at=utcnow().isoformat())
    plan["tests"].append({**parent, "experiment_id": spec["id"], "state": "running", "result": None,
        "fingerprint": spec["fingerprint"], "ceiling_micro": spec["ceiling_micro"], "reserved_micro": spec["ceiling_micro"],
        "success_threshold": spec["success_threshold"], "failure_threshold": spec["failure_threshold"],
        "started_at": utcnow().isoformat(), "deadline": plan["deadline"]})
    for key in ("ended_at", "actual_micro", "request_ids", "checkpoint_paused"):
        plan["tests"][-1].pop(key, None)
    experiment_context["belief_plan_id"] = plan_id
    saved.context = json.dumps(context)


def stop(venture_id):
    with db()() as session:
        serialize_writes(session, key="market-beliefs:" + venture_id)
        saved = beliefs.row(session, venture_id)
        context = json.loads(saved.context) if saved else {}
        if context.get("plan"):
            context["plan"]["state"] = "stopped"
            if context["plan"].get("renewal"):
                context["plan"]["revoked_at"] = utcnow().isoformat()
            saved.context = json.dumps(context)
            session.commit()
    return context


def send_block(session, venture_id, experiment_context):
    plan_id = experiment_context.get("belief_plan_id")
    if not plan_id:
        return None
    saved = beliefs.row(session, venture_id)
    plan = json.loads(saved.context).get("plan") if saved else None
    if (not plan or plan["id"] != plan_id or plan["state"] not in {"active", "budget_exhausted"}
            or utcnow() >= as_utc(datetime.fromisoformat(plan["deadline"]))
            or not _allowed(plan, experiment_context["spec"])):
        return "experiment_plan_stopped"
    test = next((test for test in plan["tests"] if test["experiment_id"] == experiment_context["spec"]["id"]), None)
    if not test or test["state"] not in {"launching", "running"}:
        return "experiment_plan_stopped"
    from app.autopilot.outreach import send_slots_used_today
    if send_slots_used_today(venture_id) >= plan["max_emails_per_day"]:
        return "experiment_daily_cap"
    return None


def daily_note(venture_id, *, now=None):
    from app.chat.store import append_message

    now = as_utc(now or utcnow())
    context = beliefs.load(venture_id)
    plan = context.get("plan")
    if not plan or not plan.get("approved_at"):
        return None
    date = now.date().isoformat()
    if date in plan["daily_notes"]:
        return None
    updates = [item for item in context["beliefs"] if item.get("evidence") and item["evidence"][-1]["date"][:10] == date]
    lines = []
    for item in updates[:3]:
        evidence = item["evidence"][-1]
        value = evidence["value"]
        lines.append(f"{shorten(item['statement'], width=140, placeholder='...')} ({item['status']}; {evidence['metric'].replace('_', ' ')}: "
                     f"{value if value is not None else 'still being measured'}, {evidence['events']} events).")
    counts = {status: sum(item["status"] == status for item in context["beliefs"]) for status in ("confirmed", "testing", "weak", "disproven")}
    body = "Your market tests today: " + (" ".join(lines) if lines else
        ", ".join(f"{count} {status}" for status, count in counts.items()) + ".")
    errors = sum(test["state"] == "error" for test in plan["tests"])
    if errors:
        body += f" Your {errors} failed test(s) need recovery; those beliefs are still to test."
    if plan["state"] == "budget_exhausted":
        from app.core.local_mode import is_local
        body += (" Your market-test budget for this period has been reached. Open your market map to review the saved results. Choose Stop new market tests before requesting a different plan."
            if is_local() else " Your approved credit ceiling has been reached. New tests are stopped.")
    elif plan["state"] == "complete":
        body += " Your seven-day plan is complete. Review your market map for the next tests."
    else:
        body += " You can review the evidence and next tests in your market map."
    append_message(venture_id, role="assistant", body_md=body, kind="market_beliefs_daily",
        idempotency_key=f"market-beliefs:{plan['id']}:{date}",
        actions=[{"label": "Open your market map", "href": f"/v/{venture_id}/market-map"}])
    with db()() as session:
        serialize_writes(session, key="market-beliefs:" + venture_id)
        saved = beliefs.row(session, venture_id)
        fresh = json.loads(saved.context)
        if fresh.get("plan", {}).get("id") == plan["id"] and date not in fresh["plan"]["daily_notes"]:
            fresh["plan"]["daily_notes"].append(date)
            saved.context = json.dumps(fresh)
            session.commit()
    return body


def _receipt_total(session, workspace_id, venture_id, request_ids, *, account_id=None):
    if not request_ids:
        return None
    from app.core.local_mode import is_local
    if is_local():
        from app.credits import local_meter
        from app.credits.gate import reconcile_receipt
        calls = {}
        with local_meter._database() as connection:
            for request_id in request_ids:
                if not isinstance(request_id, str) or not request_id:
                    return None
                rows = connection.execute("SELECT * FROM calls WHERE workspace_id = ? AND venture_id = ? "
                    "AND (request_id = ? OR substr(request_id, 1, length(?) + 1) = ? || ':')",
                    (workspace_id, venture_id, request_id, request_id, request_id)).fetchall()
                if not rows:
                    return None
                calls.update({row["call_id"]: row for row in rows})
        total = 0
        for row in calls.values():
            cost = row["cost_usd_micro"]
            if row["status"] not in {"confirmed", "unspent"} or type(cost) is not int or cost < 0:
                return None
            try:
                usage = json.loads(row["receipt_json"])
                binding = {name: row[name] for name in ("workspace_id", "venture_id", "request_id", "purpose", "provider", "model")}
                if (not isinstance(usage, dict) or usage.get("local_call_id") != row["call_id"]
                        or any(name not in usage or usage[name] != value for name, value in binding.items())
                        or account_id is not None and usage.get("account_id") != account_id):
                    return None
                if row["status"] == "unspent":
                    usage = {**usage, "definitive_unspent": True}
                reconcile_receipt(usage, expected_binding=binding)
            except (ValueError, TypeError, CreditsUnavailable):
                return None
            total += cost
        return total
    total = 0
    for request_id in request_ids:
        holds = list(session.scalars(select(CreditReservation).where(
            (CreditReservation.request_id == request_id) | CreditReservation.request_id.startswith(request_id + ":", autoescape=True),
            CreditReservation.workspace_id == workspace_id, CreditReservation.venture_id == venture_id)))
        passthrough = list(session.scalars(select(CreditLedgerEntry).where(
            (CreditLedgerEntry.request_id == request_id) | CreditLedgerEntry.request_id.startswith(request_id + ":", autoescape=True),
            CreditLedgerEntry.reservation_id.is_(None), CreditLedgerEntry.kind == "spend")))
        if not holds and not passthrough:
            return None
        for entry in passthrough:
            meta = json.loads(entry.meta_json or "{}")
            if (entry.amount_micro != 0 or account_id and entry.account_id != account_id or
                    meta.get("passthrough") is not True or meta.get("source") not in {"byok", "free"} or
                    (meta.get("workspace_id"), meta.get("venture_id")) != (workspace_id, venture_id) or
                    any(type(meta.get(name)) is not int or meta[name] < 0
                        for name in ("actual_micro", "provider_actual_micro", "estimate_micro", "cached_tokens"))):
                return None
        for hold in holds:
            if hold.is_pool or hold.parent_id or account_id and hold.account_id != account_id:
                return None
            entry = session.scalar(select(CreditLedgerEntry).where(CreditLedgerEntry.idempotency_key == "settle:" + hold.id))
            if hold.status == "released" and entry is None:
                continue
            if (hold.status != "settled" or type(hold.settled_micro) is not int or hold.settled_micro < 0 or
                    not entry or entry.kind != "spend" or entry.amount_micro != -hold.settled_micro or
                    entry.reservation_id != hold.id or entry.account_id != hold.account_id or
                    any(getattr(entry, name) != getattr(hold, name) for name in ("request_id", "purpose", "provider", "model"))):
                return None
            meta = json.loads(entry.meta_json or "{}")
            if (meta.get("workspace_id"), meta.get("venture_id")) != (workspace_id, venture_id):
                return None
            total += hold.settled_micro
    return total


def _reconcile(session, saved, context):
    plan = context["plan"]
    allocations = list(plan.get("calls", []))
    for test in plan["tests"]:
        if test["state"] not in {"done", "error"} or "reserved_micro" not in test or "actual_micro" in test:
            continue
        experiment = session.get(Experiment, test["experiment_id"])
        if not experiment or (experiment.workspace_id, experiment.venture_id) != (saved.workspace_id, saved.venture_id):
            continue
        observed = json.loads(experiment.context)
        if not observed.get("run_id"):
            continue
        job = session.get(Job, observed["job_id"]) if observed.get("job_id") else None
        if job and job.status not in {"done", "succeeded", "failed", "failed_dead", "poison", "cancelled"}:
            continue
        from app.core.local_mode import is_local
        requests = observed.get("request_ids") if is_local() else None
        if requests is not None and not isinstance(requests, list):
            continue
        requests = list(requests) if requests else [observed["run_id"]]
        if observed.get("spec", {}).get("runner") == "outreach_split":
            from app.autopilot import outreach
            drafts = [draft for draft in outreach.drafts(saved.venture_id)
                      if autonomous.outreach_tests.tags(draft)["experiment_id"] == experiment.id]
            if any(draft.get("state") in {"delivery_pending", "delivery_unconfirmed"} for draft in drafts):
                continue
            missing = False
            for draft in drafts:
                if draft.get("state") != "sent":
                    continue
                approval = session.get(Approval, draft.get("delivery_approval_id")) if draft.get("delivery_approval_id") else None
                detail = json.loads(approval.detail_json or "{}") if approval else {}
                request_id = detail.get("external_execution", {}).get("request_id")
                if not request_id or approval.workspace_id != saved.workspace_id:
                    missing = True
                    break
                requests.append(request_id)
            if missing:
                continue
        test["request_ids"] = list(set(requests))
        allocations.append(test)
    for allocation in allocations:
        if "actual_micro" in allocation or not allocation.get("finished", allocation.get("state") in {"done", "error"}):
            continue
        actual = _receipt_total(session, saved.workspace_id, saved.venture_id,
            allocation.get("request_ids", [allocation.get("request_id")]) if allocation.get("request_ids") or allocation.get("request_id") else [],
            account_id=allocation.get("account_id"))
        if actual is None:
            continue
        plan["reserved_micro"] += actual - allocation["reserved_micro"]
        allocation["actual_micro"] = actual
    if plan["state"] == "active" and plan["reserved_micro"] >= plan["ceiling_micro"]:
        plan["state"] = "budget_exhausted"
    if plan["state"] == "budget_exhausted" and plan["reserved_micro"] < plan["ceiling_micro"]:
        plan["state"] = "active"


def _renew(plan, now):
    deadline = as_utc(datetime.fromisoformat(plan["deadline"]))
    if now < deadline or plan.get("revoked_at") or plan["state"] not in {"active", "budget_exhausted"}:
        return
    periods = (now - deadline) // timedelta(days=7) + 1
    plan["period_started_at"] = (deadline + timedelta(days=7 * (periods - 1))).isoformat()
    plan["deadline"] = (deadline + timedelta(days=7 * periods)).isoformat()
    plan["reserved_micro"] = sum(item["reserved_micro"] for item in [*plan["tests"], *plan.get("calls", [])]
        if "reserved_micro" in item and "actual_micro" not in item)
    plan["state"] = "active" if plan["reserved_micro"] < plan["ceiling_micro"] else "budget_exhausted"


def _read_results(venture_id, now):
    with db()() as session:
        serialize_writes(session, key="market-beliefs:" + venture_id)
        saved = beliefs.row(session, venture_id)
        context = json.loads(saved.context)
        plan = context["plan"]
        for test in plan["tests"]:
            if test["state"] != "running":
                continue
            experiment = session.get(Experiment, test["experiment_id"])
            if not experiment or (experiment.venture_id, experiment.workspace_id) != (venture_id, saved.workspace_id):
                continue
            observed = json.loads(experiment.context)
            spec, result = observed["spec"], observed.get("result")
            if ledger.quality_enabled():
                if observed.get("checkpoint_paused"):
                    test["checkpoint_paused"] = True
                    continue
                if spec.get("runner") == "outreach_split" and not observed.get("error") and experiment.state != "concluded":
                    result = autonomous.outreach_tests.observe(spec)
                    if result.get("response_window_open") and result["outcome"] == "inconclusive":
                        test.update(readable_at=result.get("readable_at"), result=result)
                        continue
                    if experiment.state == "concluded":
                        observed["result"] = result
                        experiment.context = json.dumps(observed)
                        experiment.outcome, experiment.learnings = result["outcome"], result["insight"]
                elif (spec.get("scan") in {"page_test", "channel_test"} and not observed.get("first_exposure_at")
                        and experiment.state != "concluded"):
                    test["launch_state"] = "ready to launch"
                    continue
            expired = now >= as_utc(datetime.fromisoformat(test["deadline"]))
            if not enabled() and experiment.state == "running" and spec.get("runner") == "outreach_split":
                result = autonomous.outreach_tests.observe(spec)
            elif not enabled() and experiment.state == "running" and spec.get("scan") == "page_test":
                from app.experiments.page_tests import observe
                result = observe(spec)
            elif not enabled() and experiment.state == "running" and spec.get("scan") == "channel_test":
                from app.experiments.channel_tests import observe
                result = observe(spec)
            measured = (result or {}).get("measured") or {"events": 0}
            verdict = (result or {}).get("outcome")
            decisive = autonomous.outcome(spec, measured) != "inconclusive" and verdict != "inconclusive"
            if enabled() and experiment.state != "concluded" and not observed.get("error"):
                continue
            if not (experiment.state == "concluded" or expired or decisive or observed.get("error")):
                continue
            test["state"] = "error" if observed.get("error") else "done"
            if observed.get("error"):
                test["error"] = observed["error"]
            test["ended_at"] = now.isoformat()
            test["result"] = result
            for index, belief in enumerate(context["beliefs"]):
                if belief["id"] == test["belief_id"]:
                    if ledger.quality_enabled():
                        binding = methods.evidence_binding(spec)
                        belief["evidence_bindings"] = [item for item in belief.get("evidence_bindings", [])
                            if item["experiment_id"] != spec["id"]] + [binding]
                        if belief["statement"] != spec.get("hypothesis"):
                            verdict = "inconclusive"
                    context["beliefs"][index] = {**belief, "status": "to_test"} if observed.get("error") else beliefs.update(
                        belief, {**spec, "success_threshold": test["success_threshold"], "failure_threshold": test["failure_threshold"]},
                        measured, date=now.isoformat(), verdict=verdict)
                    if ledger.quality_enabled() and context["beliefs"][index].get("evidence"):
                        context["beliefs"][index]["evidence"][-1]["evidence_type"] = binding["evidence_type"]
        if plan.get("renewal") and enabled():
            _renew(plan, now)
        elif now >= as_utc(datetime.fromisoformat(plan["deadline"])):
            plan["state"] = "complete"
        if enabled():
            _reconcile(session, saved, context)
        saved.context = json.dumps(context)
        session.commit()
    return context


def _claim(venture_id, spec):
    with db()() as session:
        serialize_writes(session, key="market-beliefs:" + venture_id)
        saved = beliefs.row(session, venture_id)
        if saved is None:
            return None
        context = json.loads(saved.context)
        plan = context["plan"]
        if plan["state"] != "active" or utcnow() >= as_utc(datetime.fromisoformat(plan["deadline"])):
            return None
        if not _allowed(plan, spec):
            return None
        if any(test["state"] == "launching" for test in plan["tests"]):
            return None
        test = next((test for test in plan["tests"] if test["experiment_id"] == spec["id"] and test["state"] == "queued"), None)
        if not test or test["fingerprint"] != spec["fingerprint"] or test["ceiling_micro"] != spec["ceiling_micro"]:
            return None
        try:
            reserve(plan, test["ceiling_micro"])
        except CreditsUnavailable:
            plan["state"] = "budget_exhausted"
            saved.context = json.dumps(context)
            session.commit()
            return None
        test["state"] = "launching"
        if enabled():
            from app.credits.accounts import account_for_venture
            account = account_for_venture(venture_id, session=session)
            test.update(reserved_micro=test["ceiling_micro"], account_id=account.id if account else None)
            if plan.get("renewal"):
                test["deadline"] = (utcnow() + timedelta(days=spec.get("max_duration_days", 7))).isoformat()
        saved.context = json.dumps(context)
        session.commit()
    return test


def _launched(venture_id, test_id, *, error=None):
    with db()() as session:
        serialize_writes(session, key="market-beliefs:" + venture_id)
        saved = beliefs.row(session, venture_id)
        context = json.loads(saved.context)
        test = next(test for test in context["plan"]["tests"] if test["experiment_id"] == test_id)
        test.update(state="error" if error else "running", started_at=utcnow().isoformat())
        if error:
            test["error"] = "Your test needs recovery before it can restart. No automatic retry is made."
        for belief in context["beliefs"]:
            if belief["id"] == test["belief_id"]:
                belief["status"] = "to_test" if error else "testing"
        saved.context = json.dumps(context)
        session.commit()


def _admit_followup(venture_id, plan_id, amount, *, request_id=None, account_id=None):
    with db()() as session:
        serialize_writes(session, key="market-beliefs:" + venture_id)
        saved = beliefs.row(session, venture_id)
        context = json.loads(saved.context)
        plan = context["plan"]
        if (plan["id"] != plan_id or plan["state"] != "active" or utcnow() >= as_utc(datetime.fromisoformat(plan["deadline"]))
                or plan.get("renewal") and (not enabled() or plan.get("revoked_at"))):
            raise CreditsUnavailable("Your market plan is not active.")
        queued = sum(test["ceiling_micro"] for test in plan["tests"] if test["state"] == "queued")
        if plan["reserved_micro"] + queued + amount > plan["ceiling_micro"]:
            raise CreditsUnavailable("Your remaining budget is reserved for the tests you approved.")
        reserve(plan, amount)
        if enabled() and request_id:
            plan.setdefault("calls", []).append({"request_id": request_id, "account_id": account_id,
                "reserved_micro": amount, "finished": False})
        saved.context = json.dumps(context)
        session.commit()


async def _narrow(venture_id, context):
    plan = context["plan"]
    if plan.get("renewal") and (not enabled() or plan.get("revoked_at")):
        return
    parent = next((item for item in context["beliefs"] if item["status"] in {"confirmed", "disproven"}
                   and not item.get("followup_attempted")), None)
    queued = sum(test["ceiling_micro"] for test in plan["tests"] if test["state"] == "queued")
    if not parent or plan["reserved_micro"] + queued >= plan["ceiling_micro"]:
        return
    with db()() as session:
        serialize_writes(session, key="market-beliefs:" + venture_id)
        saved = beliefs.row(session, venture_id)
        fresh = json.loads(saved.context)
        matched = next(item for item in fresh["beliefs"] if item["id"] == parent["id"])
        if matched.get("followup_attempted"):
            return
        # ponytail: one model attempt per parent, retry only after reconciling its provider receipt.
        matched["followup_attempted"] = True
        saved.context = json.dumps(fresh)
        session.commit()
    try:
        drafts = await beliefs.followup(venture_id, parent,
            admit=lambda account, request, amount: _admit_followup(venture_id, plan["id"], amount,
                request_id=request, account_id=account),
            request_id=f"{plan['id']}:{parent['id']}:followup")
    except Exception as exc:
        log_founder_exception(exc, venture_id, "experiment.beliefs.followup")
        drafts = []
    with db()() as session:
        serialize_writes(session, key="market-beliefs:" + venture_id)
        saved = beliefs.row(session, venture_id)
        fresh = json.loads(saved.context)
        if fresh["plan"]["id"] == plan["id"]:
            fresh["beliefs"].extend(drafts)
            for call in fresh["plan"].get("calls", []):
                if call["request_id"] == f"{plan['id']}:{parent['id']}:followup":
                    call["finished"] = True
            saved.context = json.dumps(fresh)
            session.commit()


def _plan_followups(venture_id, experiments, inputs, now):
    from app.core.local_mode import is_local

    with db()() as session:
        serialize_writes(session, key="market-beliefs:" + venture_id)
        if enabled():
            serialize_writes(session, key="market-experiments:" + venture_id)
        saved = beliefs.row(session, venture_id)
        context = json.loads(saved.context)
        plan = context["plan"]
        if (plan["state"] != "active" or any(test["state"] == "launching" for test in plan["tests"])
                or plan.get("renewal") and (not enabled() or plan.get("revoked_at"))):
            return context
        used_beliefs = {test["belief_id"] for test in plan["tests"]}
        used_experiments = {test["experiment_id"] for test in plan["tests"]}
        if is_local():
            def planned_inputs(spec):
                return spec.get("catalog_key"), spec.get("hypothesis"), engine.input_fingerprint(spec)

            selected = [engine.spec(row) for row in inputs.get("experiments", []) if row["id"] in used_experiments]
            used_inputs = {planned_inputs(spec) for spec in selected}
            experiments = [spec for spec in experiments if planned_inputs(spec) not in used_inputs]
        candidates = [item for item in context["beliefs"] if (plan.get("renewal") or item.get("parent_id")) and item["id"] not in used_beliefs]
        extra = belief_planner.build(candidates, [spec for spec in experiments if spec["id"] not in used_experiments and _allowed(plan, spec)], inputs,
            ceiling_micro=max(0, plan["ceiling_micro"] - plan["reserved_micro"] - sum(test["ceiling_micro"] for test in plan["tests"] if test["state"] == "queued")),
            max_emails_per_day=plan["max_emails_per_day"], now=now)
        for test in extra["tests"]:
            test["deadline"] = plan["deadline"]
            experiment = session.get(Experiment, test["experiment_id"])
            if experiment is None:
                spec = next(spec for spec in experiments if spec["id"] == test["experiment_id"])
                experiment = Experiment(id=spec["id"], venture_id=venture_id, workspace_id=saved.workspace_id,
                    kind=ledger.KIND, hypothesis=spec["hypothesis"], action=spec["method"], state="proposed",
                    context=json.dumps({"runner": "system", "spec": spec}))
                session.add(experiment)
            if (experiment.venture_id, experiment.workspace_id) != (venture_id, saved.workspace_id):
                raise ValueError("Your planned experiment is unavailable.")
            experiment.context = json.dumps({**json.loads(experiment.context), "belief_plan_id": plan["id"]})
            plan["tests"].append(test)
        saved.context = json.dumps(context)
        session.commit()
    return context


async def tick(venture_id, *, now=None):
    from app.core.local_mode import is_local
    from app.ventures.current_idea import intake_pending

    if is_local() and not enabled():
        return
    now = as_utc(now or utcnow())
    if not autonomous._snapshot(venture_id):
        return
    context = beliefs.load(venture_id)
    if not context.get("plan") or context["plan"]["state"] not in {"active", "budget_exhausted"}:
        return
    if context["plan"].get("renewal") and not enabled():
        return
    if enabled():
        from app.experiments.service import run_evaluate
        snapshot = autonomous._snapshot(venture_id)
        await asyncio.to_thread(run_evaluate, {"venture_id": venture_id, "workspace_id": snapshot["workspace_id"],
            "experiment_ids": [test["experiment_id"] for test in context["plan"]["tests"] if test["state"] == "running"], "now": now})
    context = await asyncio.to_thread(_read_results, venture_id, now)
    plan = context["plan"]
    if plan["state"] == "active" and not await asyncio.to_thread(intake_pending, venture_id):
        await _narrow(venture_id, context)
        experiments = await asyncio.to_thread(belief_planner.available, venture_id, beliefs.load(venture_id)["beliefs"])
        inputs = await asyncio.to_thread(ledger.ranking_inputs, venture_id)
        context = await asyncio.to_thread(_plan_followups, venture_id, experiments, inputs, now)
        plan = context["plan"]
        for test in plan["tests"]:
            if test["state"] != "queued":
                continue
            spec = next((spec for spec in experiments if spec["id"] == test["experiment_id"]), None)
            if ledger.quality_enabled() and spec and spec.get("checkpoint_parent_id"):
                continue
            if not spec or spec.get("state") != "proposed" or engine.blocked_reason(spec, inputs.get("experiments", []), now):
                continue
            if ledger.quality_enabled() and spec.get("checkpoint") and not is_local():
                from app.chat.milestones import post_milestone
                from app.experiments.outreach_tests import confirmation
                card = confirmation(spec)
                with db()() as session:
                    post_milestone(venture_id, key=plan["id"] + ":checkpoint-approval:" + spec["id"],
                        kind="confirmation", requested=True, body_md=card["body_md"],
                        citations=[], actions=card["actions"], session=session)
                    session.commit()
                break
            if spec.get("runner") == "outreach_split" and not is_local():
                from app.gtm import journey
                settings = journey.status(spec["workspace_id"], venture_id, authorize=False)
                if not settings.get("allow_sending") or not settings.get("auto_approve_first_emails"):
                    continue
            claimed = _claim(venture_id, spec)
            if not claimed:
                continue
            try:
                await autonomous.run(venture_id, {"experiment_id": spec["id"], "fingerprint": spec["fingerprint"],
                    "ceiling_micro": spec["ceiling_micro"], **({"variants": spec["variants"]} if spec.get("runner") == "outreach_split" else {})})
            except Exception as error:
                _launched(venture_id, spec["id"], error=error)
                log_founder_exception(error, venture_id, "experiment.beliefs.launch")
            else:
                _launched(venture_id, spec["id"])
            break
    await asyncio.to_thread(daily_note, venture_id, now=now)


async def run_job(_payload):
    if _payload.get("experiment_id") and not ledger.quality_enabled():
        return
    if ledger.quality_enabled() and _payload.get("experiment_id"):
        from app.core.jobs import FatalError, _leased_row
        from app.experiments import channel_tests, page_tests
        with db()() as session:
            job = _leased_row(session, _payload.get("_job_id"), _payload.get("_worker_id"), _payload.get("_job_attempt"))
            row = session.get(Experiment, _payload["experiment_id"])
            context = json.loads(row.context) if row else {}
            if (not job or not row or row.kind != ledger.KIND or
                    (row.venture_id, row.workspace_id) != (_payload.get("venture_id"), _payload.get("workspace_id"))
                    or (row.venture_id, row.workspace_id) != (job.venture_id, job.workspace_id)
                    or json.loads(job.payload_json).get("experiment_id") != row.id or context.get("runner") != "system"
                    or context.get("spec", {}).get("scan") not in {"page_test", "channel_test"}):
                raise FatalError("experiment_observation_scope_changed")
            if context.get("quality_job_id") != job.id or row.state in {"concluded", "aborted"}:
                return
            observer = page_tests if context["spec"]["scan"] == "page_test" else channel_tests
        with founder_scope(row.venture_id, autonomous=True):
            reply = await asyncio.to_thread(observer.readout, row.venture_id, {"experiment_id": row.id})
        with db()() as session:
            row = session.get(Experiment, _payload["experiment_id"])
            context = json.loads(row.context)
            from app.chat.milestones import post_milestone
            post_milestone(row.venture_id, key="experiment-quality:" + row.id + ":" + autonomous._fingerprint(reply),
                kind=reply["kind"], requested=True, body_md=reply["body_md"], citations=reply.get("citations", []),
                actions=reply["actions"], session=session)
            if row.state == "proposed" or not context.get("first_exposure_at"):
                page_tests.schedule_observation(session, row, context, utcnow() + timedelta(days=1))
                row.context = json.dumps(context)
                session.commit()
            elif context.get("result", {}).get("response_window_open") and context["result"].get("readable_at"):
                page_tests.schedule_observation(session, row, context, datetime.fromisoformat(context["result"]["readable_at"]))
                row.context = json.dumps(context)
                session.commit()
            else:
                session.commit()
        return
    scope = current_or_none()
    with db()() as session:
        query = select(Experiment.venture_id).where(Experiment.kind == ledger.KIND,
            Experiment.id.like("beliefs_%"))
        if scope and scope.workspace_id != "none":
            query = query.where(Experiment.workspace_id == scope.workspace_id)
            if scope.venture_id:
                query = query.where(Experiment.venture_id == scope.venture_id)
        ids = list(set(session.scalars(query)))
    for venture_id in ids:
        try:
            with founder_scope(venture_id, autonomous=True):
                await tick(venture_id)
        except Exception as exc:
            log_founder_exception(exc, venture_id, "experiment.beliefs.tick")
