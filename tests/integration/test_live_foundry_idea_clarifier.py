"""Opt-in: ONE real configured Foundry clarification, synthetic/local state only.

Requires SUPYMEM_FOUNDRY_SMOKE=1 and TEST_POSTGRES_URL on loopback. The operator
already authorizes this metered call. Never retrieves database/provider user data.
"""
from __future__ import annotations

import asyncio
import json
import os

import pytest
from sqlalchemy import select

from tests.integration.test_postgres_auth_security import pg_database as pg_database
from tests.integration.test_postgres_journey_jobs import journey_db as journey_db


@pytest.mark.skipif(os.environ.get("SUPYMEM_FOUNDRY_SMOKE") != "1", reason="One live provider call requires explicit opt-in")
def test_single_real_foundry_clarification(journey_db, monkeypatch):
    import boto3
    from app.credits.accounts import ensure_account
    from app.core.jobs import lease
    from app.db.models import Job
    from app.db.supymem_models import IdeaSession, IdeaClarificationRun, IdeaRevision
    from app.idea.service import post_message
    from app.tenancy import bind
    from app.worker.handlers import install_default_handlers
    from app.worker.main import _run_one
    from llm_gateway import sole
    d = journey_db
    # Values remain in memory; do not log or persist the Secrets Manager payload.
    secret = json.loads(boto3.client("secretsmanager", region_name="us-east-1").get_secret_value(
        SecretId="selfhost-os/production/llm")["SecretString"])
    for key in ("LLM_API_KEY", "LLM_BASE_URL", "LLM_API_VERSION", "LLM_DEPLOYMENT_PRO", "LLM_DEPLOYMENT_FLASH", "LLM_REASONING_EFFORT"):
        if secret.get(key):
            monkeypatch.setenv(key, str(secret[key]))
    if secret.get("LLM_DEPLOYMENT_PRO") != "DeepSeek-V4-Pro":
        pytest.fail("Production model metadata is not the required DeepSeek V4 Pro")
    monkeypatch.setenv("LLM_PROVIDER", "azure_foundry")
    monkeypatch.setenv("LLM_FALLBACK_PROVIDER", "")
    monkeypatch.setenv("LLM_OFFLINE", "0")
    monkeypatch.setattr(sole, "_primary_chat", None)
    monkeypatch.setattr(sole, "_fallback_chat", None)
    with d["factory"]() as session:
        row = session.get(IdeaSession, d["sid"])
        row.state, row.confirmed_at, row.confirmed_revision_id = "clarifying", None, None
        session.commit()
    ensure_account(d["vid"], workspace_id=d["wid"], owner_person_id=d["actor"])
    with bind(workspace_id=d["wid"], venture_id=d["vid"], actor=d["actor"]):
        jid = post_message(d["sid"], "I want an order intake board for neighbourhood bakeries. Owners receive custom cake orders through a form, review the date and price, and confirm the order. I can invite two bakery owners. I want to test a monthly subscription after a free trial and get one paying bakery. Please propose this narrow idea without inventing market research.")["job_id"]
    install_default_handlers()
    with d["factory"]() as session:
        job = lease(session, "single-live-clarifier-smoke", kinds=["idea.clarify_turn"], n=1)[0]
        session.commit()
    asyncio.run(_run_one(d["factory"], job, {jid}))
    with d["factory"]() as session:
        done = session.get(Job, jid)
        assert done.status == "succeeded", done.last_error
        run = session.scalars(select(IdeaClarificationRun).where(IdeaClarificationRun.job_id == jid)).one()
        usage = json.loads(run.usage)
        idea = session.get(IdeaSession, d["sid"])
        rev = session.get(IdeaRevision, idea.current_revision_id)
        assert rev.source == "agent" and run.state == "completed"
        assert usage["source"] == "platform" and usage["settled"] and usage["credits_micro"] > 0
        print(json.dumps({"provider": "azure_foundry", "model": usage["model"], "receipt": run.state,
                          "schema_valid": True, "idea_state": idea.state,
                          "prompt_tokens": usage["prompt_tokens"], "completion_tokens": usage["completion_tokens"],
                          "charged_micro": usage["credits_micro"]}))
