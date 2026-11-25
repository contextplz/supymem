"""Task first row locks, refreshing old identities once per transaction."""
from sqlalchemy import select
from sqlalchemy.orm.util import identity_key

from app.db.models import Job
from app.db.task_models import Task, TaskRun, TaskStep

_CACHE = "task_row_locks"


class TaskLockRequired(RuntimeError):
    """A caller must acquire its task locks before changing cached rows."""
    code = "task_lock_before_mutation_required"


def _held(session):
    transaction = session.get_nested_transaction() or session.get_transaction()
    if transaction is None:
        session.begin()
        transaction = session.get_transaction()
    previous, keys = session.info.get(_CACHE, (None, set()))
    if previous is not transaction:
        keys = set()
        session.info[_CACHE] = (transaction, keys)
    return keys


def _row(session, model, identifier, *, skip_locked=False):
    """First lock refreshes; re-entry keeps this transaction's own changes."""
    key = (model, identifier)
    cached = session.identity_map.get(identity_key(model, identifier))
    held = _held(session)
    if key in held and cached is not None:
        return cached
    if cached is not None and session.is_modified(cached, include_collections=False):
        raise TaskLockRequired(TaskLockRequired.code)
    row = session.scalar(select(model).where(model.id == identifier).with_for_update(skip_locked=skip_locked)
        .execution_options(populate_existing=True))
    if row is not None:
        held.add(key)
    return row


def lock_task(session, task_id, *, skip_locked=False):
    """Acquire the parent before any child/job lock or task state mutation."""
    from app.db.session import serialize_writes
    with session.no_autoflush:
        connection = session.connection()  # Establish the transaction identity before caching any lock.
        if session.get_bind().dialect.name == "sqlite":
            if not connection.connection.driver_connection.in_transaction:
                serialize_writes(session, key="task-control:" + task_id)
        return _row(session, Task, task_id, skip_locked=skip_locked)


def lock_run(session, run_id, *, skip_locked=False):
    with session.no_autoflush:
        task_id = session.scalar(select(TaskRun.task_id).where(TaskRun.id == run_id))
        if task_id is None:
            return None, None
        task = lock_task(session, task_id, skip_locked=skip_locked)
        run = _row(session, TaskRun, run_id, skip_locked=skip_locked) if task else None
        if run is not None and run.task_id != task_id:
            raise TaskLockRequired("task_parent_changed")
        return task, run


def lock_step(session, step_id, *, skip_locked=False):
    with session.no_autoflush:
        run_id = session.scalar(select(TaskStep.run_id).where(TaskStep.id == step_id))
        if run_id is None:
            return None, None, None
        task, run = lock_run(session, run_id, skip_locked=skip_locked)
        step = _row(session, TaskStep, step_id, skip_locked=skip_locked) if run else None
        if step is not None and step.run_id != run_id:
            raise TaskLockRequired("task_parent_changed")
        return task, run, step


def lock_for_job(session, job_id, *, skip_locked=False):
    """Discover without locks, then acquire Task → Run → Step → linked Job.

    Control jobs have a parent run but no step. Unlinked jobs keep their
    existing Job-only locking; this helper must precede their leased-row read.
    """
    with session.no_autoflush:
        from app.tasks.skills import get_skill
        from app.tasks import goals
        kind = session.scalar(select(Job.kind).where(Job.id == job_id))
        if kind not in {"tasks.execute", "tasks.verify"} and get_skill(kind) is None and not goals.is_action(kind):
            return None, None, None, None
        step_id = session.scalar(select(TaskStep.id).where(TaskStep.job_id == job_id))
        if step_id is not None:
            task, run, step = lock_step(session, step_id, skip_locked=skip_locked)
            if step is not None and step.job_id != job_id:
                raise TaskLockRequired("task_job_parent_changed")
        else:
            run_ids = session.scalars(select(TaskRun.id).where(TaskRun.control_job_id == job_id).limit(2)).all()
            if len(run_ids) > 1:
                raise TaskLockRequired("task_job_parent_ambiguous")
            run_id = run_ids[0] if run_ids else None
            if run_id is None:
                return None, None, None, None
            task, run = lock_run(session, run_id, skip_locked=skip_locked)
            step = None
            if run is not None and run.control_job_id != job_id:
                return None, None, None, _row(session, Job, job_id, skip_locked=skip_locked)
        job = _row(session, Job, job_id, skip_locked=skip_locked) if task and run else None
        return task, run, step, job
