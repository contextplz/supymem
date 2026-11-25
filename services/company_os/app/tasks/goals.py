"""Flagged serial goals using existing admissions and source-owned receipts."""
from datetime import timedelta, timezone
import hashlib
import json
import re
from zoneinfo import ZoneInfo
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr
from sqlalchemy import or_, select

from app.db.models import Job, Venture
from app.db.task_models import Task, TaskEvent, TaskRun, TaskStep
from app.tasks.contracts import Budget, Contract

SYSTEM = """Plan at most 12 serial steps with distinct, literal subgoals. Use only the supplied skills and their typed inputs. Never invent an identifier, approval, receipt or result. output_bindings maps a documented output field to a unique binding name. An input may reference a verified earlier output using {"binding": "name"}; never reference a future step. Use deterministic receipt/count checks. Separate preparation from each exact action approval. No contact with people, spending money, paid ads, publishing, deletion or LinkedIn automation. Do not claim calls were booked from drafts or search results. Return the supplied JSON schema. Use one_time and trigger {} unless the founder explicitly requested a schedule/event. Unknown inputs need your clarification. Keep founder copy plain, speak to you, and omit internal notes."""


def enabled():
    from app.core.local_mode import is_local
    if is_local():
        from app.core.local_capabilities import enabled as capability_enabled
        return capability_enabled("background_work")
    from app.credits.policy import get_policy
    flags = get_policy("feature_flags")
    return isinstance(flags, dict) and flags.get("goals_v1") is True


class Binding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    binding: StrictStr = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")


class Inputs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class FindInputs(Inputs):
    query: Annotated[StrictStr, Field(max_length=1500)] | Binding = ""


class DraftInputs(Inputs):
    contact_id: Annotated[StrictStr, Field(min_length=1, max_length=100)] | Binding


class ExperimentInputs(Inputs):
    experiment_id: Annotated[StrictStr, Field(min_length=1, max_length=200)] | Binding


class RunInputs(ExperimentInputs):
    fingerprint: Annotated[StrictStr, Field(min_length=1, max_length=200)] | Binding
    ceiling_micro: Annotated[StrictInt, Field(ge=0, le=500_000)] | Binding


class CallsInputs(Inputs):
    target_count: StrictInt = Field(ge=1, le=1000)


MODELS = {"gtm.find": FindInputs, "outreach.draft": DraftInputs,
          "experiment.prepare": ExperimentInputs, "experiment.run": RunInputs,
          "tasks.booking_link": Inputs, "tasks.customer_calls": CallsInputs}
OUTPUTS = {"gtm.find": ("journey_id", "contact_id"), "outreach.draft": ("draft_id",),
           "experiment.prepare": ("experiment_id", "fingerprint", "ceiling_micro"),
           "experiment.run": ("experiment_id",), "tasks.booking_link": ("booking_link",),
           "tasks.customer_calls": ("booking_count",)}
OUTPUT_FIELDS = sorted({field for fields in OUTPUTS.values() for field in fields})


def is_action(name):
    return name in MODELS


def catalog():
    from app.chat.actions import beta_blocked, disabled_reason, get_action
    from app.tasks.skills import Skill
    result = {}
    for name, model in MODELS.items():
        registry_name = "experiment.booking.readout" if name.startswith("tasks.") else name
        action = get_action(registry_name)
        if disabled_reason(registry_name) or beta_blocked(registry_name):
            continue
        result[name] = Skill(name, name.replace(".", " ").replace("_", " "), tuple(model.model_fields),
            OUTPUTS[name], "free" if name.startswith("tasks.") else "metered", frozenset(),
            consequential=action.consequential)
    return result


def planning_catalog():
    from app.tasks.skills import SKILLS
    return {**{name: skill for name, skill in SKILLS.items() if not skill.consequential}, **catalog()}


def input_schemas():
    result = []
    for model in MODELS.values():
        schema = model.model_json_schema()
        definitions = schema.pop("$defs", {})
        def expand(node):
            if isinstance(node, dict):
                if "$ref" in node:
                    return expand(definitions[node["$ref"].rsplit("/", 1)[1]])
                return {key: expand(value) for key, value in node.items()}
            if isinstance(node, list):
                return [expand(value) for value in node]
            return node
        result.append(expand(schema))
    return result


def validate_step(step, earlier):
    from app.secrets.detect import looks_like_secret
    if (not step.subgoal or looks_like_secret(step.subgoal) or step.runs_on != "server"
            or len(step.output_bindings) > len(OUTPUTS[step.skill])):
        raise ValueError("Choose a specific, safe server subgoal for each step.")
    known = {binding for source in earlier for binding in source.output_bindings.values()}
    values = MODELS[step.skill].model_validate(step.inputs).model_dump(exclude_none=True)
    for value in values.values():
        if isinstance(value, dict):
            if value["binding"] not in known:
                raise ValueError("Inputs must use verified earlier-step outputs.")
        elif isinstance(value, str) and looks_like_secret(value):
            raise ValueError("Keep credentials out of task inputs.")
    for field, binding in step.output_bindings.items():
        if (field not in OUTPUTS[step.skill] or not isinstance(binding, str)
                or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", binding)
                or binding in known):
            raise ValueError("Choose unique names for documented step outputs.")
        known.add(binding)
    step.inputs = values


def validate_plan(contract):
    # shortcut: plans are fixed, add replanning after receipt and approval recovery is proven.
    if any(is_action(step.skill) or step.subgoal for step in contract.steps):
        if any(step.subgoal is not None and not step.subgoal.strip() for step in contract.steps):
            raise ValueError("Choose a specific result for each step in your goal plan.")
        if any(is_action(step.skill) for step in contract.steps) and contract.done_test.get("type") == "rubric":
            raise ValueError("Use deterministic result checks for these goal actions.")
        if contract.kind != "one_time":
            raise ValueError("Goal plans currently run once. Use existing skills for recurring tasks.")
        seen = set()
        for step in contract.steps:
            key = json.dumps([step.skill, step.inputs, (step.subgoal or contract.goal).strip().casefold()], sort_keys=True)
            if key in seen:
                raise ValueError("Repeated actions with the same inputs would redo your work.")
            seen.add(key)


def customer_calls_template(goal, *, timezone, now):
    match = re.fullmatch(r"(?:please\s+)?book\s+(\d{1,4})\s+customer\s+calls\s+this\s+week[.!]?", goal.strip(), re.I)
    if match is None:
        return None
    count = int(match[1])
    CallsInputs(target_count=count)
    return Contract(title=goal[:160], goal=goal, estimate_seconds=86400,
        budget=Budget(wall_seconds=86400), steps=[
            {"title": "Read your booking link", "subgoal": "Read your saved booking link without changing it.",
             "skill": "tasks.booking_link", "output_bindings": {"booking_link": "booking_link"}},
            {"title": "Find people you can speak with", "subgoal": "Find customer leads for your saved audience without sending anything.",
             "skill": "gtm.find", "output_bindings": {"contact_id": "customer_contact"}},
            {"title": "Prepare your invitation for review", "subgoal": "Prepare an invitation draft for your review. Do not send it.",
             "skill": "outreach.draft", "inputs": {"contact_id": {"binding": "customer_contact"}}},
            {"title": "Check your confirmed calls", "subgoal": f"Verify {count} distinct confirmed customer calls this week.",
             "skill": "tasks.customer_calls", "inputs": {"target_count": count}}])


def goal_deadline(goal, zone, now, estimate):
    if not re.fullmatch(r"(?:please\s+)?book\s+\d{1,4}\s+customer\s+calls\s+this\s+week[.!]?", goal.strip(), re.I):
        return estimate
    local = (now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now).astimezone(ZoneInfo(zone))
    end = (local + timedelta(days=7 - local.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    return end.astimezone(timezone.utc).replace(tzinfo=None)


def metadata(spec):
    return {"_goal_subgoal": spec.get("subgoal"), "_goal_outputs": spec.get("output_bindings", {})}


def guard(session, task, run, **charges):
    from app.tasks import service
    scope = json.loads(task.scope_json)
    if not scope.get("_goals_v1"):
        return True
    planning_cost = scope.get("_planning", {}).get("billing", {}).get("usage", {}).get("actual_micro", 0)
    if type(planning_cost) is not int or planning_cost < 0:
        return False
    limits = Budget.model_validate_json(task.budget_json)
    runs = session.scalars(select(TaskRun).where(TaskRun.task_id == task.id)).all()
    return (enabled() and len(runs) == 1 and service.naive_utc() < run.deadline_at
        and service.naive_utc() < service.naive_utc(run.started_at) + timedelta(seconds=limits.wall_seconds)
        and len(scope["steps"]) <= 12
        and all(sum(getattr(item, used) + getattr(item, reserved, 0) for item in runs) + charges.get(charge, 0)
            + (planning_cost if charge == "cost_micro" else 0) <= getattr(limits, limit)
            for used, reserved, charge, limit in (("cost_micro", "reserved_cost_micro", "cost_micro", "cost_micro"),
                ("tokens_used", "reserved_tokens", "tokens", "tokens"), ("tool_calls", "_none", "tool_calls", "tool_calls"),
                ("attempts", "_none", "attempts", "attempts"))))


def _receipt(session, task, run, step):
    if step.job_id:
        receipt = verify_job(session, step.skill, {**json.loads(step.inputs_json), "_job_id": step.job_id},
            workspace_id=task.workspace_id, venture_id=task.venture_id)
    else:
        saved = session.scalar(select(TaskEvent).where(TaskEvent.task_id == task.id, TaskEvent.run_id == run.id,
            TaskEvent.step_id == step.id, TaskEvent.kind == "goal_receipt"))
        if not saved:
            raise ValueError("Your earlier step does not have a saved receipt.")
        receipt = verify_sync(session, task, step, json.loads(saved.data_json))
    stamp = json.loads(step.inputs_json)
    original = stamp.get("_goal_verified_outputs")
    if original is not None and any(receipt["outputs"].get(field) != original.get(field) for field in stamp.get("_goal_outputs", {})):
        return result(False)
    return receipt


def resolve_inputs(session, task, run, step):
    from app.tasks.checks import passes
    stamp = json.loads(step.inputs_json)
    inputs = dict(stamp.get("_goal_inputs", {key: value for key, value in stamp.items() if not key.startswith("_")}))
    earlier = session.scalars(select(TaskStep).where(TaskStep.run_id == run.id,
        TaskStep.position < step.position).order_by(TaskStep.position)).all()
    for key, value in inputs.items():
        if not isinstance(value, dict):
            continue
        binding = Binding.model_validate(value).binding
        sources = [(source, field) for source in earlier for field, name in json.loads(source.inputs_json).get("_goal_outputs", {}).items() if name == binding]
        if len(sources) != 1 or sources[0][0].state != "completed":
            raise ValueError("Your earlier step must finish before its output can be used.")
        source, field = sources[0]
        receipt = _receipt(session, task, run, source)
        if not receipt["verified"] or not passes(json.loads(source.done_test_json), receipt["evidence"]):
            raise ValueError("Your earlier result could not be verified.")
        inputs[key] = receipt["outputs"][field]
    values = MODELS[step.skill].model_validate(inputs).model_dump(exclude_none=True)
    if any(isinstance(value, dict) for value in values.values()):
        raise ValueError("Your step inputs could not be resolved.")
    from app.secrets.detect import looks_like_secret
    if any(isinstance(value, str) and looks_like_secret(value) for value in values.values()):
        raise ValueError("Keep credentials out of task inputs.")
    return values


def fingerprint(action, inputs):
    return hashlib.sha256(json.dumps([action, inputs], sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def record_attempt(session, task, run, step, action, inputs):
    from app.tasks import service
    key = fingerprint(action, inputs)
    attempts = session.scalars(select(TaskEvent).where(TaskEvent.task_id == task.id, TaskEvent.kind == "goal_action")).all()
    if any(json.loads(item.data_json)["fingerprint"] == key for item in attempts):
        raise ValueError("This action already ran with the same inputs. Review its saved result.")
    if not service.guard(session, task, run, attempts=1):
        raise ValueError("Your goal reached its deadline or agreed work limit.")
    run.attempts += 1
    service.event(session, task, "goal_action", "Your next step is starting.", run_id=run.id, step_id=step.id,
        data={"action": action, "inputs": inputs, "fingerprint": key})


def scan_spec(venture_id, experiment_id):
    from app.experiments import autonomous
    spec = next((item for item in autonomous.available(venture_id) if item["id"] == experiment_id), None)
    if not spec or spec.get("runner") != "scan" or spec.get("scan") not in {"demand", "competitors", "reviews"}:
        raise ValueError("Choose a public-source scan that contacts nobody.")
    return spec


def result(verified, evidence=(), outputs=None, pending=False):
    return {"verified": verified, "evidence": list(evidence), "outputs": outputs or {}, "pending": pending}


def verify_job(session, kind, payload, *, workspace_id, venture_id):
    from app.tasks import service, skills
    job = session.get(Job, payload.get("_job_id"))
    venture = session.get(Venture, venture_id)
    expected = "gtm.journey.prepare" if kind == "gtm.find" else kind
    if (not enabled() or not job or not venture or venture.workspace_id != workspace_id
            or (job.workspace_id, job.venture_id, job.kind) != (workspace_id, venture_id, expected)):
        return result(False)
    task, run, step = service.job_step(session, job.id)
    if not step or step.skill != kind or not json.loads(task.scope_json).get("_goals_v1"):
        return result(False)
    if job.status not in {"succeeded", "done"}:
        return result(False, pending=job.status in {"scheduled", "queued", "pending", "leased", "running", "retry_wait"})
    saved = json.loads(job.payload_json)
    if kind == "gtm.find":
        from app.db.models import VentureDocument
        source = session.get(VentureDocument, saved.get("journey_id"))
        provenance = json.loads(source.provenance_json or "{}") if source else {}
        if (not source or source.venture_id != venture_id
                or (provenance.get("job_id"), provenance.get("run_id")) != (job.id, saved.get("prepare_id"))):
            return result(False)
        receipt = skills.verify_result(session, expected, {**saved, "_job_id": job.id}, workspace_id=workspace_id, venture_id=venture_id)
        from app.db.models import GtmContact
        from app.gtm.reachable import contact_seed
        leads = (provenance.get("reachable_leads") or []) + (provenance.get("industry_experts") or [])
        emails = {seed["email"].strip().lower() for lead in leads if (seed := contact_seed(lead))}
        contacts = session.scalars(select(GtmContact).where(GtmContact.workspace_id == workspace_id,
            GtmContact.source.startswith(f"venture:{venture_id}:", autoescape=True),
            GtmContact.email.in_(emails)).order_by(GtmContact.id)).all()
        evidence = [{**item, "prospect_count": len(contacts)} if "prospect_count" in item else item for item in receipt["evidence"]]
        return {**receipt, "verified": receipt["verified"] and bool(contacts), "evidence": evidence,
            "outputs": {"journey_id": source.id, "contact_id": contacts[0].id if contacts else None}}
    if kind == "experiment.run":
        from app.db.supymem_models import Experiment
        row = session.get(Experiment, saved.get("experiment_id"))
        context = json.loads(row.context) if row else {}
        spec = context.get("spec", {})
        if (not row or (row.workspace_id, row.venture_id) != (workspace_id, venture_id)
                or context.get("job_id") != job.id or not context.get("result") or context.get("error")
                or row.state != "concluded" or row.ended_at is None
                or spec.get("runner") != "scan" or spec.get("scan") not in {"demand", "competitors", "reviews"}):
            return result(False)
        return result(True, [{"ref": "experiment:" + row.id, "state": "completed"}], {"experiment_id": row.id})
    return result(False)


def verify_sync(session, task, step, saved):
    from app.experiments.outreach_tests import booking_link
    if step.skill == "tasks.booking_link":
        link = booking_link(task.venture_id, session=session)
        return result(bool(link and saved.get("booking_link") == link), [{"ref": "venture:" + task.venture_id}], {"booking_link": link})
    if step.skill == "experiment.prepare":
        spec = scan_spec(task.venture_id, saved["experiment_id"])
        outputs = {field: spec[field] for field in OUTPUTS[step.skill] if field != "experiment_id"}
        outputs["experiment_id"] = spec["id"]
        return result(outputs == saved, [{"ref": "experiment:" + spec["id"], "state": "prepared"}], outputs)
    if step.skill == "outreach.draft":
        from app.autopilot import outreach
        drafts = outreach.drafts(task.venture_id, "draft", "manual")
        draft = next((item for item in drafts if item.get("doc_id") == saved.get("draft_id")
                      and item.get("contact_id") == json.loads(step.inputs_json).get("contact_id")), None)
        return result(bool(draft), [{"ref": "venture_document:" + saved["draft_id"], "draft_count": 1}], saved)
    if step.skill == "tasks.customer_calls":
        return customer_calls_receipt(session, task, step)
    return result(False)


def customer_calls_receipt(session, task, step):
    from app.db.models import GtmContact, GtmTouch
    from app.experiments.autonomous import _uncertain_claim
    from app.experiments.outreach_tests import booked_reply
    from app.tasks import service
    from app.tenancy import bind
    if enabled() and step.state == "completed":
        saved = session.scalar(select(TaskEvent).where(TaskEvent.task_id == task.id, TaskEvent.run_id == step.run_id,
            TaskEvent.step_id == step.id, TaskEvent.kind == "goal_booking_verified"))
        if saved:
            return json.loads(saved.data_json)
    run = session.get(TaskRun, step.run_id)
    zone = ZoneInfo(json.loads(task.scope_json).get("_goal_timezone", "UTC"))
    local = service.naive_utc(run.started_at).replace(tzinfo=timezone.utc).astimezone(zone)
    start = (local - timedelta(days=local.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    start_utc = start.astimezone(timezone.utc).replace(tzinfo=None)
    end_utc = (start + timedelta(days=7)).astimezone(timezone.utc).replace(tzinfo=None)
    from app.connections.bookings import read_bookings
    with bind(workspace_id=task.workspace_id, venture_id=task.venture_id, actor=task.owner_person_id), service.step_context(run.id, step.id):
        signal = read_bookings(session, task.venture_id, start_utc, end_utc,
            feature_flag="goals_v1", person_id=task.owner_person_id)
    if signal and signal["connected"]:
        count = len({(booking["starts_at"], booking["ends_at"]) for booking in signal["bookings"]})
        target = json.loads(step.inputs_json)["target_count"]
        receipt = result(signal["available"] and count >= target,
            [{"ref": "venture:" + task.venture_id, "booking_count": count, "bookings_checked": signal["available"]}],
            {"booking_count": count}, pending=not signal["available"] or count < target)
        if signal.get("connection_action"):
            receipt["connection_action"] = signal["connection_action"]
        return receipt
    touches = session.scalars(select(GtmTouch).join(GtmContact, GtmContact.id == GtmTouch.contact_id).where(
        GtmContact.workspace_id == task.workspace_id, GtmTouch.workspace_id == task.workspace_id,
        GtmTouch.venture_id == task.venture_id, GtmTouch.direction == "in", GtmTouch.simulated == 0,
        or_(GtmTouch.classification.is_(None), GtmTouch.classification != "automated"), GtmTouch.created_at >= start_utc,
        GtmTouch.created_at < end_utc, GtmTouch.created_at <= service.naive_utc()).order_by(GtmTouch.created_at, GtmTouch.id)).all()
    sent = session.scalars(select(GtmTouch).where(GtmTouch.workspace_id == task.workspace_id,
        GtmTouch.venture_id == task.venture_id, GtmTouch.direction == "out", GtmTouch.simulated == 0,
        GtmTouch.sent_at.is_not(None))).all()
    contacts = set()
    for touch in touches:
        if not any(item.contact_id == touch.contact_id and service.naive_utc(item.sent_at) <= service.naive_utc(touch.created_at) for item in sent):
            continue
        body = re.split(r"(?im)^\s*(?:>|On .+wrote:|From:|-----Original Message-----)", touch.body_text or "", maxsplit=1)[0]
        if re.search(r"\b(?:cancel\w*|declin\w*|not booked|never booked)\b", body, re.I):
            contacts.discard(touch.contact_id)
        elif not _uncertain_claim(body) and booked_reply(body, subject=touch.subject or ""):
            contacts.add(touch.contact_id)
    count = len(contacts)
    target = json.loads(step.inputs_json)["target_count"]
    receipt = result(count >= target, [{"ref": "venture:" + task.venture_id, "booking_count": count}],
        {"booking_count": count}, pending=count < target)
    if signal and signal.get("connection_action"):
        receipt["connection_action"] = signal["connection_action"]
    return receipt


def finish(session, task, run, step, receipt):
    from app.tasks import service
    from app.tasks.checks import passes
    from app.tasks.locks import lock_step
    task, run, step = lock_step(session, step.id)
    if task.state in service.TERMINAL | service.WAITING or run.state in service.TERMINAL | service.WAITING:
        return
    step.evidence_json = json.dumps(receipt["evidence"])
    if not service.guard(session, task, run):
        service.block(session, task, {"class": "needs_input", "reason": "Your goal reached its deadline or agreed work limit.",
            "unblock_action": "Review your saved progress and choose a new goal."}, run=run, step=step)
        return
    if receipt["verified"] and passes(json.loads(step.done_test_json), receipt["evidence"]):
        stamp = json.loads(step.inputs_json)
        required = stamp.get("_goal_outputs", {})
        if any(receipt["outputs"].get(field) is None for field in required):
            service.block(session, task, {"class": "needs_input", "reason": "Your step did not produce the output needed by the next step.",
                "unblock_action": "Review your saved result and provide the missing input."}, run=run, step=step)
            return
        stamp["_goal_verified_outputs"] = receipt["outputs"]
        step.inputs_json = json.dumps(stamp)
        if step.skill == "tasks.customer_calls":
            service.event(session, task, "goal_booking_verified", "Your booked customer calls are verified.",
                run_id=run.id, step_id=step.id, data=receipt)
        step.state, step.finished_at = "completed", service.naive_utc()
        service.event(session, task, "step_finished", "Verified: " + step.title, run_id=run.id, step_id=step.id)
        service.complete_run(session, task, run)
    elif receipt.get("pending"):
        step.state = "verifying"
        if receipt.get("connection_action"):
            from app.chat.conversations import job_receipt_conversation
            from app.chat.store import append_message
            job = session.get(Job, run.control_job_id)
            if job:
                with job_receipt_conversation(json.loads(job.payload_json),
                        venture_id=task.venture_id, workspace_id=task.workspace_id) as allowed:
                    if allowed:
                        append_message(task.venture_id, role="assistant", kind="text",
                            body_md="Connect your booking account so I can check your booked customer calls. Your goal stays open. Nothing is changed in your account.",
                            actions=[receipt["connection_action"]], idempotency_key="goal-booking-connection:" + step.id)
    else:
        service.block(session, task, {"class": "needs_input", "reason": "Your saved result could not be verified.",
            "unblock_action": "Review the saved result before continuing."}, run=run, step=step)


async def dispatch(task_id, run_id, step_id, inputs):
    from app.tasks import service, usage
    from app.tasks.locks import lock_step
    from app.state import db
    from app.tenancy import bind
    from app.chat.execute import execute_action
    with db()() as session:
        task, run, step = lock_step(session, step_id)
        if not service.guard(session, task, run):
            raise ValueError("Your goal reached its deadline or agreed work limit.")
        skill, vid, wid, actor = step.skill, task.venture_id, task.workspace_id, task.owner_person_id
        routine = json.loads(task.scope_json).get("routine_id")
    with bind(workspace_id=wid, venture_id=vid, actor=actor), service.step_context(run_id, step_id), usage.run_context(run_id, step_id=step_id):
        if skill == "gtm.find":
            from app.chat.execute import find_customers_reply
            from app.chat.intent import linkedin_people_request
            if linkedin_people_request(inputs.get("query", "")):
                raise ValueError("Choose a public-source customer search. LinkedIn automation is not available for goals.")
            reply = await find_customers_reply(vid, inputs.get("query", ""), start_requested=False)
        elif skill == "outreach.draft":
            reply = await execute_action(vid, skill, payload={**inputs, "review_only": True, "run": False}, actor_subject=actor)
        elif skill == "experiment.run":
            from app.experiments import autonomous
            scan_spec(vid, inputs["experiment_id"])
            reply = autonomous.prepare(vid, inputs["experiment_id"])
        else:
            reply = None
        if reply is not None:
            cards = [item for item in reply.get("actions", []) if isinstance(item.get("confirmation"), dict)
                     and item["confirmation"]["action"] == skill]
            if len(cards) != 1:
                raise ValueError("Your next step needs a fresh exact action review.")
            from app.core.local_mode import is_local
            if is_local() and routine and skill in {"gtm.find", "outreach.draft"}:
                from app.tasks.routines import admit_step
                with db()() as session:
                    task, run, step = lock_step(session, step_id)
                    if not service.guard(session, task, run) or not admit_step(session, task, run, step):
                        raise ValueError("Your routine changed. Review its saved request in your private chat before retrying.")
                    record_attempt(session, task, run, step, skill, inputs)
                    session.commit()
                reply = await execute_action(vid, skill, payload=cards[0]["confirmation"]["resulting_state"], actor_subject=actor)
                with db()() as session:
                    task, run, step = lock_step(session, step_id)
                    job_id = reply.get("job_id") or reply.get("run_id")
                    job = session.get(Job, job_id) if job_id else None
                    if job:
                        service.attach_job(session, job)
                    if step.job_id:
                        if task.state not in service.TERMINAL | service.WAITING:
                            step.state = "working"
                        session.commit()
                        service.after_job(session, step.job_id)
                    elif skill == "outreach.draft" and reply.get("kind") not in {"needs_you", "confirmation"} and reply.get("action_started") is not False:
                        from app.autopilot import outreach
                        draft = next((item for item in outreach.drafts(vid, "draft", "manual")
                                      if item.get("contact_id") == inputs["contact_id"]), None)
                        if not draft:
                            raise ValueError("Your draft has no saved receipt. Review your saved drafts in Find customers before retrying.")
                        saved = {"draft_id": draft["doc_id"]}
                        service.event(session, task, "goal_receipt", "Your draft is saved for review. Nothing was sent.",
                                      run_id=run.id, step_id=step.id, data=saved)
                        finish(session, task, run, step, verify_sync(session, task, step, saved))
                    else:
                        service.block(session, task, {"class": "needs_input", "reason": reply.get("body_md") or "Your preparation did not produce a saved result.",
                            "unblock_action": "Review your saved results in your private chat before retrying your routine."}, run=run, step=step)
                    session.commit()
                return
            await park(step_id, reply, cards[0])
            return
        with db()() as session:
            task, run, step = lock_step(session, step_id)
            record_attempt(session, task, run, step, skill, inputs)
            session.commit()
        receipt = None
        if skill == "tasks.customer_calls":
            with db()() as session:
                receipt = customer_calls_receipt(session, session.get(Task, task_id), session.get(TaskStep, step_id))
        with db()() as session:
            task, run, step = lock_step(session, step_id)
            if skill == "tasks.booking_link":
                from app.experiments.outreach_tests import booking_link
                saved = {"booking_link": booking_link(vid, session=session)}
            elif skill == "experiment.prepare":
                spec = scan_spec(vid, inputs["experiment_id"])
                saved = {"experiment_id": spec["id"], "fingerprint": spec["fingerprint"], "ceiling_micro": spec["ceiling_micro"]}
            else:
                saved = {}
            service.event(session, task, "goal_receipt", "Your result is saved.", run_id=run.id, step_id=step.id, data=saved)
            finish(session, task, run, step, receipt if receipt is not None else verify_sync(session, task, step, saved))
            session.commit()


async def park(step_id, reply, card):
    from app.chat.store import append_message
    from app.state import db
    from app.tasks import service
    from app.tasks.locks import lock_step
    with db()() as session:
        task, run, step = lock_step(session, step_id)
        routine = json.loads(task.scope_json).get("routine_id")
        if routine:
            from app.tasks.routines import enabled as routines_enabled
            if not routines_enabled():
                service.cancel(session, task)
                session.commit()
                return
        if not service.guard(session, task, run):
            raise ValueError("Your goal reached its deadline or agreed work limit.")
        binding = {"task_id": task.id, "run_id": run.id, "step_id": step.id}
        confirmation = {**card["confirmation"], "resulting_state": {**card["confirmation"]["resulting_state"], "_goal_step": binding}}
        saved = append_message(task.venture_id, role="assistant", body_md=reply["body_md"], kind="confirmation",
            actions=[{**card, "confirmation": confirmation}], idempotency_key="goal-review:" + step.id)
        inputs = json.loads(step.inputs_json)
        inputs["_goal_approval"] = {"message_id": saved["id"], "action": confirmation["action"], "payload": confirmation["resulting_state"]}
        step.inputs_json = json.dumps(inputs)
        service.block(session, task, {"class": "needs_approval", "reason": "Your next step is ready for exact review.",
            "unblock_action": "Review and accept the saved confirmation in your private chat."}, run=run, step=step)
        venture_id, actor = task.venture_id, task.owner_person_id
        session.commit()
    if routine:
        from app.chat.confirmations import auto_dispatch
        await auto_dispatch(venture_id, saved, actor)


async def refresh_booking_check(payload):
    from app.state import db
    from app.tasks import service, usage
    from app.tasks.locks import lock_step
    from app.tenancy import bind
    if not enabled():
        return
    with db()() as session:
        step = session.get(TaskStep, payload["_booking_step_id"])
        run = session.get(TaskRun, payload["task_run_id"])
        task = session.get(Task, run.task_id) if run else None
        job = session.get(Job, payload.get("_job_id"))
        if (not step or not run or not task or not job or step.run_id != run.id
                or step.skill != "tasks.customer_calls" or not json.loads(task.scope_json).get("_goals_v1")
                or (task.workspace_id, task.venture_id) != (payload.get("workspace_id"), payload.get("venture_id"))
                or json.loads(job.payload_json).get("_booking_step_id") != step.id
                or (job.workspace_id, job.venture_id, job.kind, job.id) != (task.workspace_id, task.venture_id, "tasks.execute", run.control_job_id)):
            raise PermissionError("task_scope_mismatch")
        if step.state != "verifying" or not service.guard(session, task, run):
            return
        with bind(workspace_id=task.workspace_id, venture_id=task.venture_id, actor=task.owner_person_id), \
                service.step_context(run.id, step.id), usage.run_context(run.id, step_id=step.id):
            receipt = customer_calls_receipt(session, task, step)
    with db()() as session:
        task, run, step = lock_step(session, step.id)
        if step.state == "verifying":
            finish(session, task, run, step, receipt)
        session.commit()


def reconcile(session):
    from app.tasks import service
    from app.tasks.locks import lock_step
    from app.connections.bookings import PROVIDERS
    from app.db.models import ContentOAuthState
    for step_id in session.scalars(select(TaskStep.id).where(TaskStep.skill == "tasks.customer_calls",
            TaskStep.state == "verifying", TaskStep.job_id.is_(None)).limit(100)).all():
        task, run, step = lock_step(session, step_id)
        if not json.loads(task.scope_json).get("_goals_v1"):
            continue
        if not service.guard(session, task, run):
            if task.state not in service.TERMINAL | service.WAITING:
                service.block(session, task, {"class": "needs_input", "reason": "Your goal reached its deadline or agreed work limit.",
                    "unblock_action": "Review your saved progress and choose a new goal."}, run=run, step=step)
            continue
        connected = session.scalar(select(ContentOAuthState.id).where(ContentOAuthState.workspace_id == task.workspace_id,
            ContentOAuthState.actor_subject == task.owner_person_id, ContentOAuthState.platform.in_(PROVIDERS),
            ContentOAuthState.auth_method == "composio", ContentOAuthState.outcome == "connected",
            ContentOAuthState.consumed_at.is_not(None), ContentOAuthState.connected_account_id.is_not(None)))
        if connected:
            control = session.get(Job, run.control_job_id)
            if not control or control.status not in {"scheduled", "queued", "pending", "leased", "running", "retry_wait"}:
                bucket = int(service.naive_utc().replace(tzinfo=timezone.utc).timestamp()) // 300
                run.control_job_id = service.enqueue_owned(session, task, "tasks.execute",
                    {"task_run_id": run.id, "_booking_step_id": step.id},
                    dedupe_key=f"goal-bookings:{step.id}:{bucket}", max_attempts=1)
            continue
        finish(session, task, run, step, customer_calls_receipt(session, task, step))
