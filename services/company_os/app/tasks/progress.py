"""Bounded research strategy changes from actual tool observations."""
from __future__ import annotations

import hashlib
import json

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select

from app.db.models import Venture, Workspace
from app.db.session import serialize_writes
from app.db.task_models import TaskEvent
from app.secrets.detect import looks_like_secret
from app.state import db
from app.tasks import service, usage
from app.tasks.budgets import effective_limits
from app.tasks.contracts import blocker, stuck

KEY = "_research_progress"
SYSTEM = """Choose one different public web search query to make progress toward the unchanged trusted goal. Diagnostics contain code-owned hashes and observed-source counts only. Do not change the goal, permissions, tools, budget or approval rules. Do not request credentials, account actions, sending or publishing. Return only rationale and newquery in the required schema."""


class ResearchStopped(RuntimeError):
    """The durable task blocker, not model prose, controls whether work resumes."""


class Reflection(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    rationale: str = Field(min_length=1, max_length=500)
    newquery: str = Field(min_length=1, max_length=300)

    @field_validator("rationale", "newquery")
    @classmethod
    def clean(cls, value):
        if not value.strip() or looks_like_secret(value):
            raise ValueError("research_reflection_invalid")
        return value.strip()


def _hash(value, limit=64_000):
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), default=lambda x: type(x).__name__)
    return hashlib.sha256(text[:limit].encode()).hexdigest()


def _query(value):
    return " ".join(str(value or "").split()).casefold()


def _rows(session, workspace_id, venture_id, *, check_binding=True):
    from app.tasks.locks import lock_for_job, lock_step
    run_id, step_id = usage.context()
    task, run, step = lock_step(session, step_id)
    serialize_writes(session, key="task-progress:" + run_id)
    venture, workspace = session.get(Venture, venture_id), session.get(Workspace, workspace_id)
    job = lock_for_job(session, step.job_id)[3] if step and step.job_id else None
    if (not task or not run or not step or not venture or not workspace or not job
            or run.id != run_id or step.run_id != run.id or step.skill != "task.run" or job.kind != "task.run"
            or (task.workspace_id, task.venture_id) != (workspace_id, venture_id)
            or (job.workspace_id, job.venture_id) != (workspace_id, venture_id)
            or venture.workspace_id != workspace_id or workspace.owner_id != task.owner_person_id
            or venture.archived_at or workspace.archived_at):
        raise ResearchStopped("research_progress_scope_invalid")
    binding = _hash({"task": task.id, "run": run.id, "step": step.id, "job": job.id,
        "owner": task.owner_person_id, "workspace": workspace_id, "venture": venture_id,
        "goal": task.goal, "scope": task.scope_json, "budget": task.budget_json,
        "done_test": task.done_test_json, "step_check": step.done_test_json})
    evidence = json.loads(step.evidence_json or "{}")
    if not isinstance(evidence, dict):
        raise ResearchStopped("research_progress_checkpoint_invalid")
    journal = evidence.get(KEY, {})
    prior = session.scalars(select(TaskEvent.data_json).where(TaskEvent.run_id == run.id,
        TaskEvent.step_id == step.id, TaskEvent.kind.in_(("tool_observed", "strategy_reflection_calling")))).all()
    if (not isinstance(journal, dict) or check_binding and (journal and journal.get("binding") != binding
            or any(json.loads(data).get("binding") != binding for data in prior))):
        _stop(session, task, run, step, "outcome_uncertain")
    return task, run, step, evidence, journal, binding


def _stop(session, task, run, step, code):
    if task.state in service.TERMINAL | service.WAITING or run.state in service.TERMINAL | service.WAITING:
        session.commit()
        raise ResearchStopped(code)
    reasons = {
        "strategy_exhausted": "The agreed research approaches did not establish the requested result.",
        "reflection_invalid": "The proposed research approach could not be verified.",
        "reflection_query_required": "Research needs the verified new query before another search.",
    }
    reason = ({"class": "needs_input", "reason": reasons[code],
        "unblock_action": "Review the saved sources and choose a fresh approach."} if code in reasons else blocker(code))
    service.block(session, task, reason, run=run, step=step)
    session.commit()
    raise ResearchStopped(code)


def _guard(session, task, run, step):
    if task.state in service.TERMINAL | service.WAITING or run.state in service.TERMINAL | service.WAITING:
        raise ResearchStopped("research_progress_stopped")
    if not service.guard(session, task, run):
        _stop(session, task, run, step, "budget_exhausted")
    calls = session.scalars(select(TaskEvent).where(TaskEvent.run_id == run.id,
        TaskEvent.kind.in_(("call_uncertain", "call_settled")))).all()
    settled = {json.loads(row.data_json).get("invocation") for row in calls if row.kind == "call_settled"}
    uncertain = any(json.loads(row.data_json).get("invocation") not in settled for row in calls if row.kind == "call_uncertain")
    # A late definitive settlement is reconciled by usage; the strategy code
    # never decides that an uncertain provider invocation is safe to repeat.
    if uncertain:
        _stop(session, task, run, step, "outcome_uncertain")


def _events(session, run, step, kind):
    rows = session.scalars(select(TaskEvent).where(TaskEvent.task_id == run.task_id,
        TaskEvent.run_id == run.id, TaskEvent.step_id == step.id, TaskEvent.kind == kind)).all()
    return sorted((json.loads(row.data_json) for row in rows), key=lambda x: x.get("sequence", 0))


def _emit(session, task, run, step, kind, message, data):
    service.event(session, task, kind, message, run_id=run.id, step_id=step.id, data=data)


def _unsafe(result, error):
    # Only error fields and typed exception metadata have authority here. Page
    # prose saying "login" or "captcha" cannot block or instruct this task.
    codes = []
    if error is not None:
        status = getattr(error, "status_code", None) or getattr(error, "status", None)
        codes = [str(getattr(error, "code", ""))[:80]]
        if isinstance(error, PermissionError):
            return "policy"
        if status in {401, 403}:
            return "auth_required"
        if not codes[0]:
            return "outcome_uncertain"
    elif isinstance(result, dict):
        blocks = [result]
        if isinstance(result.get("result"), dict):
            blocks.append(result["result"])
        for block in list(blocks):
            for key in ("pages", "platforms"):
                blocks.extend(item for item in (block.get(key) or [])[:10] if isinstance(item, dict))
        codes = [str(block.get(key) or "")[:80] for block in blocks for key in ("error", "error_code")]
    lower = " ".join(codes).lower()
    if any(x in lower for x in ("warden_denied", "policy", "captcha", "refused", "boundary", "unknown_tool")):
        return "policy"
    if any(x in lower for x in ("auth", "credential", "login", "api_key", "oauth")):
        return "auth_required"
    if any(x in lower for x in ("uncertain", "reconcil")):
        return "outcome_uncertain"
    if any(x in lower for x in ("budget", "ceiling", "credits")):
        return "budget_exhausted"
    if error is not None or any(x in lower for x in ("unavailable", "timeout", "tool_exception")):
        return "outcome_uncertain"
    if "rate_limited" in lower:
        return "network"
    if any(codes):
        return "outcome_uncertain"
    return None


def before_search(query, *, workspace_id, venture_id):
    if not usage.context():
        return
    proof = _reconciled(workspace_id, venture_id)
    with db()() as session:
        task, run, step, _, journal, _ = _rows(session, workspace_id, venture_id)
        _guard(session, task, run, step)
        if journal.get("state") in {"responded", "switched"} and (not proof or proof != journal.get("billing")):
            _stop(session, task, run, step, "outcome_uncertain")
        if journal.get("state") == "responded":
            raise ResearchStopped("research_reflection_required")
        failed = _events(session, run, step, "strategy_failed")
        if any(row["strategy"] == step.strategy_id for row in failed):
            raise ResearchStopped("research_reflection_required")
        if looks_like_secret(str(query)) or not isinstance(query, str) or not 1 <= len(query.strip()) <= 1000:
            _stop(session, task, run, step, "policy")
        if journal.get("state") in {"calling", "uncertain"}:
            _stop(session, task, run, step, "outcome_uncertain")
        if journal.get("state") == "failed":
            _stop(session, task, run, step, "reflection_invalid")
        if journal.get("state") == "not_called_budget":
            _stop(session, task, run, step, "budget_exhausted")
        if journal.get("state") == "switched":
            if _query(query) == journal["previous_query"] or (not journal.get("applied") and _query(query) != _query(journal["newquery"])):
                _stop(session, task, run, step, "reflection_query_required")


def record_tool(tool, args, result, *, source_count, workspace_id, venture_id, error=None):
    """Observe only at the real ToolBus boundary; no model-provided evidence."""
    if not usage.context():
        return
    if type(source_count) is not int or not 0 <= source_count <= 30:
        raise ValueError("research_progress_metric_invalid")
    with db()() as session:
        task, run, step, evidence, journal, binding = _rows(session, workspace_id, venture_id)
        _guard(session, task, run, step)
        history = _events(session, run, step, "tool_observed")
        actual = {"action": _hash({"tool": tool, "args": args}, 4000), "result": _hash(result),
            "metric": source_count if tool == "explore_web" else None, "strategy": step.strategy_id,
            "sequence": len(history) + 1, "binding": binding}
        if error is not None or isinstance(result, dict) and result.get("ok") is False:
            actual["error"] = _hash({"type": type(error).__name__ if error else "tool_error", "code": (result or {}).get("error") if isinstance(result, dict) else None})
        _emit(session, task, run, step, "tool_observed", "A research tool result was checked.", actual)
        if code := _unsafe(result, error):
            _stop(session, task, run, step, code)
        if tool == "explore_web":
            journal.update(binding=binding, last_query=_query(args.get("query") or args.get("goal")))
            if journal.get("state") == "switched":
                journal["applied"] = True
            evidence[KEY] = journal
            step.evidence_json = json.dumps(evidence, sort_keys=True)
        current = [row for row in [*history, actual] if row["strategy"] == step.strategy_id][-12:]
        if stuck(current):
            if not any(row["strategy"] == step.strategy_id for row in _events(session, run, step, "strategy_failed")):
                _emit(session, task, run, step, "strategy_failed", "This research approach stopped making progress.",
                    {"strategy": step.strategy_id, "reason": "no_observed_progress", "binding": binding})
        session.commit()


def _reconciled(workspace_id, venture_id):
    """Recover funded usage outside Task locks before returning a saved query."""
    from app.credits.errors import CreditsUnavailable
    from app.db.task_models import Task, TaskRun, TaskStep
    from app.tasks import billing
    run_id, step_id = usage.context()
    with db()() as session:
        run, step = session.get(TaskRun, run_id), session.get(TaskStep, step_id)
        task = session.get(Task, run.task_id) if run else None
        if not task or not step or step.run_id != run_id or (task.workspace_id, task.venture_id) != (workspace_id, venture_id):
            raise ResearchStopped("research_progress_scope_invalid")
        evidence = json.loads(step.evidence_json or "{}")
        if not isinstance(evidence, dict) or not isinstance(evidence.get(KEY, {}), dict):
            raise ResearchStopped("research_progress_checkpoint_invalid")
        journal = evidence.get(KEY, {})
        if journal.get("state") not in {"responded", "switched"}:
            return None
        checkpoint, request_id = journal.get("billing"), journal.get("request_id")
    try:
        billing.reconcile(checkpoint, workspace_id=workspace_id, venture_id=venture_id, purpose="task.reflect",
            request_id=request_id, run_id=run_id, step_id=step_id)
    except CreditsUnavailable:
        return False
    return checkpoint


def _checkpoint(workspace_id, venture_id, original, response, receipt):
    from app.tasks import billing
    run_id, step_id = usage.context()
    captured = billing.capture(receipt, workspace_id=workspace_id, venture_id=venture_id, purpose="task.reflect",
        request_id=original["request_id"], run_id=run_id, step_id=step_id)
    try:
        choices = getattr(response, "choices", []) or []
        if len(choices) != 1 or choices[0].finish_reason != "stop":
            raise ValueError("research_reflection_incomplete")
        parsed = Reflection.model_validate_json(choices[0].message.content).model_dump()
        if _query(parsed["newquery"]) == original["previous_query"]:
            raise ValueError("research_reflection_unchanged")
    except (ValueError, TypeError, AttributeError):
        parsed = None
    with db()() as session:
        _, _, step, evidence, current, _ = _rows(session, workspace_id, venture_id, check_binding=False)
        if current != original:
            raise ResearchStopped("research_progress_checkpoint_changed")
        saved = {**original, "state": "responded", "valid": parsed is not None, "billing": captured}
        if parsed is not None:
            saved.update(response=parsed, response_digest=_hash(parsed))
        evidence[KEY] = saved
        step.evidence_json = json.dumps(evidence, sort_keys=True)
        session.commit()  # Normalized funded response survives a failed metering exit.


def _apply_saved(workspace_id, venture_id):
    proof = _reconciled(workspace_id, venture_id)
    with db()() as session:
        task, run, step, evidence, journal, _ = _rows(session, workspace_id, venture_id)
        if not proof or proof != journal.get("billing"):
            _stop(session, task, run, step, "outcome_uncertain")
        if (journal.get("state") not in {"responded", "switched"} or journal.get("valid") is not True
                or journal.get("response_digest") != _hash(journal.get("response"))):
            journal["state"] = "failed"
            evidence[KEY] = journal
            step.evidence_json = json.dumps(evidence, sort_keys=True)
            _stop(session, task, run, step, "reflection_invalid")
        reflected = Reflection.model_validate(journal["response"])
        if journal.get("state") == "switched":
            if journal.get("newquery") != reflected.newquery:
                _stop(session, task, run, step, "reflection_invalid")
            _guard(session, task, run, step)
            return reflected.newquery if not journal.get("applied") else None
        journal.update(state="switched", newquery=reflected.newquery, applied=False)
        evidence[KEY] = journal
        step.evidence_json = json.dumps(evidence, sort_keys=True)
        step.strategy_id = journal["next_strategy"]
        _emit(session, task, run, step, "strategy_switched", "A different research query was saved.",
            {"strategy": step.strategy_id, "binding": journal["binding"], "query_hash": _hash(_query(reflected.newquery)), "rationale_hash": _hash(reflected.rationale)})
        if task.state in service.TERMINAL | service.WAITING or run.state in service.TERMINAL | service.WAITING:
            session.commit()
            raise ResearchStopped("research_progress_stopped")
        _guard(session, task, run, step)
        session.commit()
    return reflected.newquery


async def reflect_if_due(*, workspace_id, venture_id):
    """One metered reflection per failed strategy; ambiguous calls never replay."""
    if not usage.context():
        return None
    with db()() as session:
        _, _, _, _, saved, _ = _rows(session, workspace_id, venture_id, check_binding=False)
        recover = saved.get("state") in {"responded", "switched"}
    if recover:
        query = _apply_saved(workspace_id, venture_id)
        if query is not None:
            return query
    factory = db()
    with factory() as session:
        task, run, step, evidence, journal, binding = _rows(session, workspace_id, venture_id)
        _guard(session, task, run, step)
        if journal.get("state") in {"calling", "uncertain"}:
            _stop(session, task, run, step, "outcome_uncertain")
        if journal.get("state") == "failed":
            _stop(session, task, run, step, "reflection_invalid")
        if journal.get("state") == "not_called_budget":
            _stop(session, task, run, step, "budget_exhausted")
        failed = any(row["strategy"] == step.strategy_id for row in _events(session, run, step, "strategy_failed"))
        if not failed:
            return journal.get("newquery") if journal.get("state") == "switched" and not journal.get("applied") else None
        calls = session.scalars(select(TaskEvent).where(TaskEvent.run_id == run.id, TaskEvent.kind == "strategy_reflection_calling")).all()
        if any(row.step_id == step.id and json.loads(row.data_json)["strategy"] == step.strategy_id for row in calls):
            _stop(session, task, run, step, "outcome_uncertain")
        if len(calls) >= min(3, effective_limits(task, run).max_strategies) - 1:
            _stop(session, task, run, step, "strategy_exhausted")
        if looks_like_secret(task.goal):
            _stop(session, task, run, step, "policy")
        if not service.guard(session, task, run, tokens=512):
            _stop(session, task, run, step, "budget_exhausted")
        from app.credits.metered import llm
        history = [row for row in _events(session, run, step, "tool_observed") if row["strategy"] == step.strategy_id][-12:]
        request_id = "task-reflect:" + _hash({"binding": binding, "strategy": step.strategy_id})
        journal = {"binding": binding, "state": "calling", "previous_query": journal.get("last_query", ""),
            "strategy": step.strategy_id, "next_strategy": "research:" + str(len(calls) + 2), "request_id": request_id}
        evidence[KEY] = journal
        step.evidence_json = json.dumps(evidence, sort_keys=True)
        _emit(session, task, run, step, "strategy_reflection_calling", "A different research approach is being considered.",
            {"strategy": step.strategy_id, "binding": binding, "request_hash": _hash(request_id)})
        payload = {"trusted_goal": task.goal, "diagnostics": [{key: row[key] for key in ("action", "result", "metric", "error") if key in row} for row in history]}
        session.commit()  # Paid-call intent is durable before transport.
    try:
        await llm([{"role": "system", "content": SYSTEM}, {"role": "user", "content": json.dumps(payload, sort_keys=True)}],
            purpose="task.reflect", tier="flash", max_tokens=512, response_schema=Reflection.model_json_schema(),
            workspace_id=workspace_id, venture_id=venture_id, request_id=request_id,
            before_meter_exit=lambda response, receipt: _checkpoint(workspace_id, venture_id, journal, response, receipt))
    except Exception as exc:
        from app.core.jobs import FatalError
        code = str(getattr(exc, "code", "") or (str(exc) if isinstance(exc, FatalError) else ""))
        budget = isinstance(exc, FatalError) and any(word in code for word in ("budget", "ceiling", "credits"))
        with factory() as session:
            task, run, step, evidence, current, _ = _rows(session, workspace_id, venture_id, check_binding=False)
            if current.get("state") == "responded" and current.get("billing"):
                _stop(session, task, run, step, "outcome_uncertain")
            journal["state"] = "not_called_budget" if budget else "uncertain"
            evidence[KEY] = journal
            step.evidence_json = json.dumps(evidence, sort_keys=True)
            _stop(session, task, run, step, "budget_exhausted" if budget else "outcome_uncertain")
    return _apply_saved(workspace_id, venture_id)
