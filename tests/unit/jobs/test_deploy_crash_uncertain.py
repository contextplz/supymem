"""A lost publish ACK cannot authorize another public mapping switch."""
import json
from datetime import timedelta

from sqlalchemy import select

from tests.unit.credits.conftest import account as account, credits_env as credits_env


def test_stale_deploy_after_switch_is_uncertain_but_pushing_retries(account):
    from app.core.jobs import (
        _deploy_switch_may_have_happened,
        enqueue,
        lease,
        reclaim_stale,
    )
    from app.db.models import AnalyticsEvent, ChatMessage, Job
    from app.db.supymem_models import Deployment, FounderApp
    from app.state import db
    from core.clock import utcnow

    venture_id = account.attribution_venture_id
    workspace_id = "ws_wallet:" + venture_id
    now = utcnow()
    jobs = {}
    with db()() as session:
        session.add(FounderApp(id="app_crash", workspace_id=workspace_id, venture_id=venture_id,
            slug="crash", name="Crash recovery", kind="landing", created_at=now, updated_at=now))
        session.flush()
        for version, state in enumerate(("health_checking", "pushing"), 1):
            deployment_id = "dep_" + state
            session.add(Deployment(id=deployment_id, app_id="app_crash", workspace_id=workspace_id,
                venture_id=venture_id, target="supymem_static", version=version, state=state))
            jobs[state] = enqueue(session, "deploy.run", {"schema_version": 1, "deployment_id": deployment_id},
                workspace_id=workspace_id, venture_id=venture_id)
        session.commit()
        assert len(lease(session, "lost-worker", kinds=["deploy.run"], n=2)) == 2
        for state, job_id in jobs.items():
            row = session.get(Job, job_id)
            row.locked_at = now - timedelta(minutes=2)
            assert _deploy_switch_may_have_happened(session, row) == (state == "health_checking")
        session.commit()

    with db()() as session:
        assert reclaim_stale(session, now=now) == 2
        assert reclaim_stale(session, now=now) == 0
        session.commit()
        failed = session.get(Job, jobs["health_checking"])
        assert (failed.status, failed.last_error) == ("failed_dead", "job_outcome_uncertain")
        assert failed.finished_at is not None and failed.locked_by is None and failed.locked_at is None
        deployment = session.get(Deployment, "dep_health_checking")
        assert (deployment.state, deployment.error) == ("failed", "hosting_public_state_uncertain")
        retry = session.get(Job, jobs["pushing"])
        assert retry.status == "retry_wait" and retry.last_error is None
        assert session.get(Deployment, "dep_pushing").state == "pushing"
        messages = list(session.scalars(select(ChatMessage).where(ChatMessage.venture_id == venture_id)))
        assert len(messages) == 1 and messages[0].kind == "site_publish_failed"
        failures = list(session.scalars(select(AnalyticsEvent).where(AnalyticsEvent.event_name == "job.failed")))
        assert len(failures) == 1
        assert json.loads(failures[0].props_json)["error_code"] == "job_outcome_uncertain"
        assert [job.id for job in lease(session, "replacement", kinds=["deploy.run"], n=2)] == [retry.id]
