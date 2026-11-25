"""Use existing workflow admission, never dispatch a publish/send by model choice."""
import json

from sqlalchemy import select

from app.db.task_models import TaskStep
from app.state import db
from app.tasks import service
from app.tasks.locks import lock_run, lock_step
from app.tasks.contracts import blocker, consequential_blocker


async def execute(payload):
    if payload.get("reasoning_v1") is True:
        from app.tasks.reasoning import run_job
        await run_job(payload)
        return
    if payload.get("_booking_step_id"):
        from app.tasks import goals
        await goals.refresh_booking_check(payload)
        return
    factory = db()
    with factory() as session:
        from app.db.session import serialize_writes
        task, run = lock_run(session, payload["task_run_id"])
        serialize_writes(session, key="task-admission:" + payload["task_run_id"])
        if not task or (task.workspace_id, task.venture_id) != (payload.get("workspace_id"), payload.get("venture_id")):
            raise PermissionError("task_scope_mismatch")
        if task.state in service.TERMINAL | service.WAITING or run.state in service.TERMINAL | {"queued"}:
            return
        from app.tasks import routines
        if json.loads(task.scope_json).get("routine_id") and not routines.enabled():
            service.cancel(session, task)
            session.commit()
            return
        steps = [lock_step(session, identifier)[2] for identifier in session.scalars(
            select(TaskStep.id).where(TaskStep.run_id == run.id).order_by(TaskStep.position)).all()]
        from app.tasks import goals
        goal_plan = json.loads(task.scope_json).get("_goals_v1")
        for candidate in steps:
            if candidate.state == "admitting" and not candidate.job_id:
                if goal_plan:
                    service.block(session, task, {"class": "needs_input", "reason": "Your earlier step was interrupted before its result was saved.",
                        "unblock_action": "Review its saved progress before starting a new goal."}, run=run, step=candidate)
                    session.commit()
                    return
                stamp = json.loads(candidate.inputs_json)
                if stamp.get("_admission_job_id") == payload.get("_job_id") and int(payload.get("_job_attempt", 1)) > int(stamp.get("_admission_attempt", 0)):
                    candidate.state = "submitted"  # These admissions do DB writes only; outbox links are atomic.
        if any(step.state in {"working", "verifying", "admitting"} or step.job_id and step.state == "submitted" for step in steps):
            return
        step = next((item for item in steps if item.state != "completed"), None) if goal_plan else next((item for item in steps if item.state == "submitted" and not item.job_id), None)
        if goal_plan and step is not None and (step.state != "submitted" or step.job_id):
            return
        if step is None:
            service.complete_run(session, task, run)
            session.commit()
            return
        if routines.hold_step(session, task, run):
            session.commit()
            return
        if not service.guard(session, task, run):
            service.block(session, task, blocker("budget_exhausted"), run=run, step=step)
            session.commit()
            return
        from app.tasks.routines import admit_step
        if not admit_step(session, task, run, step):
            service.block(session, task, {"class": "needs_approval", "reason": "Your routine needs your approval before this step can run.",
                "unblock_action": "Review this exact action or allow eligible preparation in your routine."}, run=run, step=step)
            session.commit()
            return
        step.state = "admitting"
        stamp = json.loads(step.inputs_json)
        if goal_plan and goals.is_action(step.skill):
            try:
                inputs = goals.resolve_inputs(session, task, run, step)
            except (ValueError, KeyError, TypeError):
                service.block(session, task, {"class": "needs_input", "reason": "Your next step needs a verified earlier result or a missing input.",
                    "unblock_action": "Review the saved results and clarify this step."}, run=run, step=step)
                session.commit()
                return
            stamp.update(inputs, _goal_inputs={key: value for key, value in stamp.items() if not key.startswith("_")})
        stamp.update(_admission_job_id=payload.get("_job_id"), _admission_attempt=payload.get("_job_attempt", 1))
        step.inputs_json = json.dumps(stamp)
        inputs, skill, run_id, step_id = json.loads(step.inputs_json), step.skill, run.id, step.id
        vid, wid = task.venture_id, task.workspace_id
        task_id, original_goal = task.id, task.goal
        from app.tasks.skills import get_skill
        registered = get_skill(skill)
        if registered is None:
            from app.core.local_mode import is_local
            if is_local():
                from app.tasks.contracts import StepContract
                from app.tasks.planner import repair_preparation
                scope = json.loads(task.scope_json)
                saved = StepContract.model_validate(scope["steps"][step.position])
                if repair_preparation(saved):
                    scope["steps"][step.position] = saved.model_dump()
                    task.scope_json = json.dumps(scope)
                    skill = step.skill = saved.skill
                    registered = get_skill(skill)
        if registered is None:
            from app.tasks.planner import blocked_skill_message
            message = blocked_skill_message(skill)
            service.block(session, task, {"class": "needs_input", "reason": message or "Your saved task includes a step you cannot run here.",
                "unblock_action": message or "In your private chat, cancel this task and ask me to replan your requested work as research, a draft, or a summary."}, run=run, step=step)
            session.commit()
            return
        if registered.consequential and not (goal_plan and goals.is_action(skill)):
            service.block(session, task, consequential_blocker(skill), run=run, step=step)
            session.commit()
            return
        session.commit()
    # Existing admissions own their transactions and provider/effect custody.
    try:
        with service.step_context(run_id, step_id):
            if goal_plan and goals.is_action(skill):
                await goals.dispatch(task_id, run_id, step_id, {key: value for key, value in inputs.items() if not key.startswith("_")})
                return
            if goal_plan and skill != "task.run":
                with factory() as session:
                    task, run, checkpoint = lock_step(session, step_id)
                    goals.record_attempt(session, task, run, checkpoint, skill, {key: value for key, value in inputs.items() if not key.startswith("_")})
                    session.commit()
            if skill == "tasks.reply_draft":
                from app.tasks.drafts import draft_reply
                result = await draft_reply(task_id=task_id, run_id=run_id, step_id=step_id, workspace_id=wid, venture_id=vid)
                with factory() as session:
                    owned_task, owned_run, checkpoint = lock_step(session, step_id)
                    if result["verified"]:
                        checkpoint.evidence_json = json.dumps(result["evidence"])
                        from app.tasks.checks import passes
                        if not passes(json.loads(checkpoint.done_test_json), result["evidence"]):
                            service.block(session, owned_task, {"class": "impossible", "reason": "The draft did not meet the agreed result check.",
                                "unblock_action": "Review the saved draft and adjust the request."}, run=owned_run, step=checkpoint)
                        elif owned_task.state != "canceled":
                            checkpoint.state, checkpoint.finished_at = "completed", service.naive_utc()
                            service.event(session, owned_task, "step_finished", "Your draft is saved for review. Nothing was sent.", run_id=run_id, step_id=step_id)
                            service.complete_run(session, owned_task, owned_run)
                    elif result.get("blocker"):
                        service.block(session, owned_task, result["blocker"], run=owned_run, step=checkpoint)
                    session.commit()
                return
            if skill == "tasks.waitlist_summary":
                from app.tasks.reports import write_report
                with factory() as session:
                    owned_task, owned_run, checkpoint = lock_step(session, step_id)
                    if owned_task.state in service.TERMINAL | service.WAITING or owned_run.state in service.TERMINAL | service.WAITING:
                        return
                    result = write_report(session, task_id=task_id, run_id=run_id, workspace_id=wid, venture_id=vid)
                    checkpoint.evidence_json = json.dumps(result["evidence"])
                    checkpoint.state = "completed"
                    checkpoint.finished_at = service.naive_utc()
                    checkpoint.strategy_id = "quiet" if result.get("quiet") else "default"
                    service.complete_run(session, owned_task, owned_run)
                    session.commit()
                return
            if skill == "research.deep":
                from app.research.service import start
                result = start(vid, request_key=f"task:{run_id}:{step_id}", expected_revision_id=inputs.get("revision_id"))
            elif skill == "pagebuilder.build":
                # A generic planner contract is not a reviewed build quote.
                # Keep the existing quote/build review as the unblocking action.
                raise ValueError("pagebuilder_confirmation_required")
            elif skill == "gtm.journey.prepare":
                from app.gtm.journey import prepare
                result = prepare(wid, vid, authorize=True)
                if result.get("state") == "needs_input":
                    raise ValueError("gtm_input_required")
            elif skill == "task.run":
                goal = inputs.get("_goal_subgoal") or original_goal
                if goal_plan:
                    with factory() as session:
                        task, run, checkpoint = lock_step(session, step_id)
                        goals.record_attempt(session, task, run, checkpoint, skill, {"subgoal": goal, **{key: value for key, value in inputs.items() if not key.startswith("_")}})
                        session.commit()
                from app.planning.execute import start_planned_task
                if not inputs.get("task_id"):
                    from app.planning.store import enqueue_task
                    from app.tasks.planner import work_goal
                    from app.planning.execute import work_kind
                    goal = work_goal(goal)
                    import re
                    tags = ["research"] if re.match(r"(?i)^(?:find|compare|audit|research|investigate|analy[sz]e|review|discover|list|identify)\b", goal) else []
                    if work_kind(goal, tags, original_goal) == "research":
                        from app.org.characters import canonical_role, encode_assignee
                        from app.org.summary import resolve_org
                        researcher = next((agent for agent in resolve_org(vid).get("agents", [])
                            if canonical_role(str(agent.get("role") or "")) == "researcher"), None)
                        if researcher is None:
                            raise ValueError("researcher_input_required")
                        tags.append(encode_assignee(researcher))
                    planned = enqueue_task(vid, {"title": goal[:160], "detail": goal if goal_plan else original_goal, "why_now": "You requested this task.",
                        "tags": tags, "est_minutes": 15, "runnable": True}, source="handoff:" + run_id + (":" + step_id if goal_plan else ""))
                    inputs["task_id"] = planned["id"]
                from app.db.models import PlannedTask
                with factory() as session:
                    task, run, step = lock_step(session, step_id)
                    if task.state in service.TERMINAL | service.WAITING or run.state in service.TERMINAL | service.WAITING:
                        return
                    planned = session.get(PlannedTask, inputs["task_id"])
                    def normalize(text):
                        return " ".join((text or "").strip().casefold().split())
                    if (not planned or planned.venture_id != vid or planned.workspace_id not in {None, wid}
                            or normalize(inputs.get("_goal_subgoal") or task.goal) not in {normalize(planned.title), normalize(planned.detail)}):
                        raise ValueError("task_goal_input_required")
                result = start_planned_task(vid, inputs["task_id"])
            else:
                raise ValueError("skill_input_required")
            if result.get("job_id"):
                from app.db.models import Job
                with factory() as session:
                    task, run, checkpoint = lock_step(session, step_id)
                    admitted = session.get(Job, result["job_id"])
                    if admitted is None:
                        raise ValueError("task_result_unavailable")
                    service.attach_job(session, admitted)
                    if checkpoint.job_id is None:
                        service.block(session, task, {"class": "needs_input",
                            "reason": "This work already has a saved task in progress.",
                            "unblock_action": "Review the existing task before starting it again."},
                            run=run, step=checkpoint)
                    service.after_job(session, admitted.id) if admitted.status in {"succeeded", "done"} else None
                    session.commit()
            else:
                with factory() as session:
                    task, run, checkpoint = lock_step(session, step_id)
                    if not checkpoint.job_id:
                        service.block(session, task, {"class": "needs_input",
                            "reason": "The existing workflow needs a saved result checked before this task can continue.",
                            "unblock_action": "Open the saved task and review its progress."},
                            run=run, step=checkpoint)
                        session.commit()
    except Exception as exc:
        # Known workflow codes only. Unknown provider/exception text stays out of UI.
        code = getattr(exc, "code", None) or (str(exc) if isinstance(exc, ValueError) else "task_admission_failed")
        from fastapi import HTTPException
        from app.core.local_mode import is_local

        setup_message = None
        if is_local() and isinstance(exc, HTTPException) and isinstance(exc.detail, dict):
            code = exc.detail.get("code") or code
            setup_message = exc.detail.get("message")
        import logging
        from app.analytics.error_events import closed_code
        logging.getLogger(__name__).warning("task_admission_failed", extra={
            "task_id": task_id, "error_code": closed_code(code.partition(":")[0])})
        with factory() as session:
            task, run, step = lock_step(session, step_id)
            reason = blocker(code)
            if setup_message:
                reason = {"class": "needs_input", "reason": setup_message, "unblock_action": setup_message}
            if code == "task_job_already_linked":
                reason = {"class": "needs_input", "reason": "This work already has a saved task in progress.", "unblock_action": "Review the existing task before starting it again."}
            if code == "task_goal_input_required":
                reason = {"class": "needs_input", "reason": "The selected saved work has a different goal.", "unblock_action": "Choose work that matches this request, or create a new task."}
            if code == "planned_task_input_required":
                reason = {"class": "needs_input", "reason": "Which saved work item should this task run?", "unblock_action": "Choose the saved work item on the Tasks page."}
            if code == "researcher_input_required":
                reason = {"class": "needs_input", "reason": "Research for this venture is not ready to start.",
                          "unblock_action": "Open Now and finish setting up this venture's research."}
            if code == "gtm_input_required":
                reason = {"class": "needs_input", "reason": "Who should this task find customers for?", "unblock_action": "Confirm the target audience on the Customers page."}
            service.block(session, task, reason, run=run, step=step)
            session.commit()
