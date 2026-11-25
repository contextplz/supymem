"""Dedicated durable worker: python -m app.pagebuilder.worker [--once]."""
from __future__ import annotations

import argparse
import asyncio
from datetime import timedelta
import json
import logging
import re
import signal
import time
import uuid

from app.core.jobs import FatalError, RetryableError, complete_job, fail_job, heartbeat, lease, release_lease
from app.core.local_mode import is_local
from app.pagebuilder import api, jobs, runtime
from app.state import db
from app.tenancy import bind


def _post_failure(job):
    from app.chat.conversations import job_receipt_conversation
    from app.chat.milestones import post_milestone

    try:
        with bind(workspace_id=job.workspace_id, venture_id=job.venture_id, actor=job.payload["_actor_subject"]):
            view = api.status(job.venture_id, job.payload["_actor_subject"], job.id)
            if view["state"] != "failed":
                return
            with job_receipt_conversation(job.payload, venture_id=job.venture_id, workspace_id=job.workspace_id) as qualified:
                if qualified:
                    post_milestone(job.venture_id, key=f"page_failed:{job.id}", kind="text", requested=True,
                                   body_md="Your landing page could not be completed. " + view["error_reason"])
    except Exception as exc:  # noqa: BLE001 - a notice must not change the saved job outcome
        from app.chat.recovery import log_founder_exception
        log_founder_exception(exc, job.venture_id, "site.page_failed")


async def run_one(factory, job):
    payload = {**job.payload, "_job_id": job.id, "_worker_id": job.worker_id, "_job_attempt": job.attempts}
    ownership = {"worker_id": job.worker_id, "attempt": job.attempts}
    stop = asyncio.Event()
    async def keep_lease():
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=15)
            except TimeoutError:
                with factory() as session:
                    heartbeat(session, job.id, **ownership)
                    session.commit()
                from app.worker.health import write as write_health
                write_health("running")
    pulse = asyncio.create_task(keep_lease())
    try:
        with bind(workspace_id=job.workspace_id, venture_id=job.venture_id, actor=job.payload["_actor_subject"]):
            await asyncio.to_thread(jobs.run, payload)
        if is_local():
            saved = runtime.read(job.id)
            if saved.status == "succeeded":
                return
            if json.loads(saved.payload_json)["_runtime"]["state"] != "ready":
                from core.clock import utcnow
                with factory() as session:
                    status = release_lease(session, job.id, now=utcnow() + timedelta(seconds=15), interrupted=False, **ownership)
                    session.commit()
                if status == "failed_dead":
                    await asyncio.to_thread(_post_failure, job)
                return
        with factory() as session:
            complete_job(session, job.id, **ownership)
            session.commit()
        from app.chat.site_actions import page_built
        with bind(workspace_id=job.workspace_id, venture_id=job.venture_id, actor=job.payload["_actor_subject"]):
            await asyncio.to_thread(page_built, job.id)
    except Exception as exc:
        code = str(exc).partition(":")[0] if isinstance(exc, ValueError) else "pagebuilder_generation_failed"
        if not re.fullmatch(r"pagebuilder_[a-z_]{1,80}", code):
            code = "pagebuilder_generation_failed"
        safe = exc if isinstance(exc, (FatalError, RetryableError)) else FatalError(code)
        logging.getLogger(__name__).warning("pagebuilder_worker_failed", extra={
            "job_id": job.id, "attempt": job.attempts, "error_code": str(safe).partition(":")[0]})
        if str(safe) != "pagebuilder_operation_in_progress":
            with factory() as session:
                status = fail_job(session, job.id, safe, **ownership)
                session.commit()
            if status == "failed_dead":
                await asyncio.to_thread(_post_failure, job)
    finally:
        stop.set()
        await pulse


async def run_worker(*, once=False):
    api.require_enabled()
    from app.llm.founder_provider import register_workspace_llm_resolver
    register_workspace_llm_resolver()
    factory = db()
    worker_id = "pbw_" + uuid.uuid4().hex[:12]
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            pass
    from app.pagebuilder import health
    from app.worker.health import write as write_health
    checked = 0.0
    while not stop.is_set():
        if time.monotonic() - checked >= 600 or checked == 0:
            if not is_local():
                await asyncio.to_thread(health.probe)
            checked = time.monotonic()
        with factory() as session:
            claimed = lease(session, worker_id, kinds=[api.KIND], n=1)
            session.commit()
        write_health("running")
        for job in claimed:
            await run_one(factory, job)
        if once:
            return
        if not claimed:
            try:
                await asyncio.wait_for(stop.wait(), timeout=2)
            except TimeoutError:
                pass
    write_health("stopped")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    asyncio.run(run_worker(once=parser.parse_args().once))
