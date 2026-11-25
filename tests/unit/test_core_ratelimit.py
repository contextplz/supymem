import asyncio

import pytest
from fastapi import HTTPException

from tests.integration.test_postgres_auth_security import service_modules


def test_workspace_cap_and_worker_bypass_check(monkeypatch):
    with service_modules("company_os"):
        from app.core import ratelimit
        from app.core.jobs import RetryableError
        from app.tenancy import bind
        from app.worker import handlers
        from core.ratelimit import RateLimiter

        monkeypatch.setattr(ratelimit, "limiter", RateLimiter(redis_url=""))
        monkeypatch.setenv("RATE_LIMIT_AI_60", "1")
        ratelimit.check_workspace("wsp_a")
        ratelimit.check_workspace("wsp_b")
        with pytest.raises(HTTPException):
            ratelimit.check_workspace("wsp_a")
        ratelimit.limiter.reset()
        calls = []
        monkeypatch.setattr(handlers, "HANDLERS", {"content.draft": lambda payload: calls.append(payload)})
        with bind(workspace_id="wsp_a", venture_id="ven_a"):
            asyncio.run(handlers.dispatch("content.draft", {}))
            with pytest.raises(RetryableError, match="rate_limited"):
                asyncio.run(handlers.dispatch("content.draft", {}))
        assert calls == [{}]


def test_throttled_job_keeps_attempt_budget_and_retry_time(tmp_path):
    from sqlalchemy.orm import Session, sessionmaker

    with service_modules("company_os"):
        from app.core.jobs import enqueue, fail_job, lease
        from app.core.ratelimit import RateLimitedJob
        from app.db.models import Base, Job
        from app.db.session import make_engine
        from core.clock import utcnow

        engine = make_engine(f"sqlite:///{tmp_path / 'limit.sqlite'}")
        Base.metadata.create_all(engine)
        factory = sessionmaker(bind=engine, class_=Session)
        try:
            with factory() as session:
                job_id = enqueue(session, "credits.sweep", {}, max_attempts=1)
                session.commit()
                job = lease(session, "worker", kinds=["credits.sweep"], n=1)[0]
                before = utcnow()
                assert fail_job(session, job_id, RateLimitedJob(900), worker_id="worker", attempt=job.attempts) == "retry_wait"
                session.commit()
                row = session.get(Job, job_id)
                assert row.attempts == 1 and row.max_attempts == 2
                assert (row.run_after.replace(tzinfo=before.tzinfo) - before).total_seconds() >= 899
        finally:
            engine.dispose()


def test_build_polling_does_not_spend_ai_admission(monkeypatch):
    with service_modules("company_os"):
        from app.core import ratelimit

        monkeypatch.setattr(ratelimit, "check_workspace", lambda _workspace: (_ for _ in ()).throw(AssertionError("spent")))
        for kind in ("build.watch", "import.cleanup"):
            ratelimit.check_job(kind)
