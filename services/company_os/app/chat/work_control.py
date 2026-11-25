"""Stop only this founder's chat work; already admitted calls settle normally."""
import hashlib
import json

from sqlalchemy import or_, select

from app.chat.conversations import actor_id, bind_conversation, current_conversation
from app.core.jobs import ACTIVE_JOB_STATUSES, request_cancel
from app.db.models import Job, PlannedTask, Venture
from app.db.supymem_models import FounderRequest
from app.db.task_models import TaskRun
from app.state import db
from core.clock import utcnow


def _stop_id(conversation: dict, nonce: str) -> str:
    return "frq_" + hashlib.sha256(json.dumps(["chat_stop_v1", conversation, nonce], sort_keys=True).encode()).hexdigest()


def stop_binding() -> dict | None:
    scope = current_conversation()
    return {"conversation_id": scope.id, "workspace_id": scope.workspace_id,
            "venture_id": scope.venture_id, "actor_id": actor_id()} if scope else None


def check_stopped(session, nonce: str | None) -> None:
    binding = stop_binding()
    if binding and nonce:
        from app.db.session import serialize_writes
        key = _stop_id(binding, nonce)
        if session.get_bind().dialect.name != "sqlite" or not session.connection().connection.driver_connection.in_transaction:
            serialize_writes(session, key=key)
        if session.get(FounderRequest, key):
            raise RuntimeError("chat_work_stopped")


def work(venture_id: str, conversation_id: str | None, *, stop: bool = False, nonce: str | None = None,
         job_ids: list[str] | None = None) -> dict:
    from app.db.session import serialize_writes
    from app.tasks.service import cancel
    from app.planning.store import mark_task_cancelled
    from app.gtm.journey import _json, _row
    from app.gtm.progress import compute_progress

    with bind_conversation(venture_id, conversation_id):
        scope = current_conversation()
        binding = stop_binding()
        from app.authz.roles import role_allows
        from app.workspaces.service import role_of
        if stop and not role_allows(role_of(scope.workspace_id, actor_id()), "request_work"):
            raise PermissionError("request_work_required")
        if stop and nonce:
            with db()() as session:
                key = _stop_id(binding, nonce)
                serialize_writes(session, key=key)
                if session.get(FounderRequest, key) is None:
                    session.add(FounderRequest(id=key, workspace_id=scope.workspace_id, venture_id=venture_id,
                        source="chat_stop_v1", text="{}", intent="chat.stop", classification="founder_approved",
                        outcome="applied", created_at=utcnow(), resolved_at=utcnow()))
                session.commit()
        with db()() as session:
            if stop:
                serialize_writes(session, key="chat-stop:" + scope.id)
            venture = session.get(Venture, venture_id)
            journey = _row(session, venture_id)
            search = _json(journey.provenance_json) if journey else {}
            jobs = session.scalars(select(Job).where(Job.venture_id == venture_id,
                Job.workspace_id == venture.workspace_id,
                or_(Job.status.in_(ACTIVE_JOB_STATUSES), Job.id == search.get("job_id")))).all()
            items = []
            progress = None
            canceled_tasks = set()
            for job in jobs:
                payload = json.loads(job.payload_json)
                destination = payload.get("_chat_conversation")
                matches = (isinstance(destination, dict) and all(destination.get(key) == value for key, value in binding.items()))
                if scope.visibility == "legacy_shared" and destination is None:
                    matches = payload.get("_actor_subject") == actor_id()
                if not matches:
                    continue
                if job.kind == "gtm.journey.prepare" and job.id == search.get("job_id") and payload.get("prepare_id") == search.get("run_id"):
                    progress = {**compute_progress(search, job), "job_id": job.id}
                if job.cancel_requested or job.status not in ACTIVE_JOB_STATUSES:
                    continue
                if stop and job_ids is not None and job.id not in job_ids and (not nonce or payload.get("_chat_request_nonce") != nonce):
                    continue
                items.append({"id": job.id})
                if not stop:
                    continue
                run = session.get(TaskRun, payload.get("task_run_id")) if payload.get("task_run_id") else None
                if run and run.task_id not in canceled_tasks:
                    from app.db.task_models import Task
                    task = session.get(Task, run.task_id)
                    if task and task.venture_id == venture_id and task.owner_person_id == actor_id():
                        cancel(session, task)
                        canceled_tasks.add(task.id)
                if job.kind == "task.run" and payload.get("task_id"):
                    task = session.get(PlannedTask, payload["task_id"])
                    if task and task.venture_id == venture_id:
                        task.status, task.runnable = "dismissed", 0
                        task.finished_at, task.updated_at = utcnow(), utcnow()
                        mark_task_cancelled(task.id)
                request_cancel(session, job.id)
            if stop:
                session.commit()
            return {"items": items, "research_progress": progress}
