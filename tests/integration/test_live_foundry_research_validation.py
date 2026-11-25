"""Opt-in real provider acceptance on synthetic, migrated loopback state only.

Two independent requests test the real validation/research schemas and receipts.
Research retrieves five public GitHub records; no outreach or cloud data writes.
"""

from __future__ import annotations
import asyncio
import json
import os
import pytest
from sqlalchemy import select
from tests.integration.test_postgres_auth_security import pg_database as pg_database
from tests.integration.test_postgres_journey_jobs import journey_db as journey_db

pytestmark = pytest.mark.skipif(
    os.environ.get("SUPYMEM_FOUNDRY_RESEARCH_SMOKE") != "1",
    reason="Live calls require explicit opt-in",
)


def configure(monkeypatch):
    import boto3
    from llm_gateway import sole

    secret = json.loads(
        boto3.client("secretsmanager", region_name="us-east-1").get_secret_value(
            SecretId="selfhost-os/production/llm"
        )["SecretString"]
    )
    assert secret.get("LLM_DEPLOYMENT_PRO") == "DeepSeek-V4-Pro"
    for key in (
        "LLM_API_KEY",
        "LLM_BASE_URL",
        "LLM_API_VERSION",
        "LLM_DEPLOYMENT_PRO",
        "LLM_REASONING_EFFORT",
    ):
        if secret.get(key):
            monkeypatch.setenv(key, str(secret[key]))
    for key, value in {
        "LLM_PROVIDER": "azure_foundry",
        "LLM_FALLBACK_PROVIDER": "",
        "LLM_OFFLINE": "0",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(sole, "_primary_chat", None)
    monkeypatch.setattr(sole, "_fallback_chat", None)


def run(d, kind, jid):
    from app.core.jobs import lease
    from app.db.models import Job, CreditLedgerEntry
    from app.db.supymem_models import ResearchDossier
    from app.credits.accounts import get_account
    from app.worker.handlers import install_default_handlers
    from app.worker.main import _run_one

    install_default_handlers()
    with d["factory"]() as session:
        job = lease(session, "live-research-acceptance", kinds=[kind], n=1)[0]
        session.commit()
    asyncio.run(_run_one(d["factory"], job, {jid}))
    with d["factory"]() as session:
        done = session.get(Job, jid)
        assert done.status == "succeeded", done.last_error
        dossier = session.scalars(
            select(ResearchDossier).where(ResearchDossier.venture_id == d["vid"])
        ).one()
        document = json.loads(dossier.summary)
        receipt = document["_runtime"]["operations"]["review"]
        assert receipt["state"] == "completed"
        spends = session.scalars(
            select(CreditLedgerEntry).where(
                CreditLedgerEntry.account_id == get_account(d["vid"]).id,
                CreditLedgerEntry.kind == "spend",
                CreditLedgerEntry.amount_micro < 0,
            )
        ).all()
        assert len(spends) == 1 and spends[0].amount_micro < 0
        print(
            json.dumps(
                {
                    "workflow": kind,
                    "job_state": done.status,
                    "dossier_state": dossier.state,
                    "calls": dossier.calls_used,
                    "charged_micro": -spends[0].amount_micro,
                    "provider": "azure_foundry",
                    "model": "DeepSeek-V4-Pro",
                    "schema_valid": True,
                }
            )
        )
        return document


def test_real_validation_schema_and_persisted_methods(journey_db, monkeypatch):
    from app.credits.accounts import ensure_account
    from app.db.supymem_models import IdeaSession, ValidationRun
    from app.idea.validation import start_validation
    from app.tenancy import bind

    d = journey_db
    configure(monkeypatch)
    with d["factory"]() as session:
        idea = session.get(IdeaSession, d["sid"])
        idea.state, idea.confirmed_at, idea.confirmed_revision_id = (
            "clarifying",
            None,
            None,
        )
        session.commit()
    ensure_account(d["vid"], workspace_id=d["wid"], owner_person_id=d["actor"])
    with bind(workspace_id=d["wid"], venture_id=d["vid"], actor=d["actor"]):
        jid = start_validation(d["sid"])["job_id"]
    result = run(d, "idea.validate", jid)
    assert len(result["validation"]["findings"]) >= 3
    with d["factory"]() as session:
        assert (
            len(
                session.scalars(
                    select(ValidationRun).where(ValidationRun.venture_id == d["vid"])
                ).all()
            )
            == 3
        )


def test_real_public_repository_review_schema(journey_db, monkeypatch):
    from app.credits.accounts import ensure_account
    from app.db.supymem_models import IdeaRevision, IdeaSession, ResearchFinding
    from core.ids import new_id
    from core.clock import utcnow
    from app.research.service import start
    from app.tenancy import bind

    d = journey_db
    configure(monkeypatch)
    with d["factory"]() as session:
        previous = session.get(IdeaRevision, d["rid"])
        revision = IdeaRevision(
            id=new_id("idv"),
            session_id=d["sid"],
            workspace_id=d["wid"],
            venture_id=d["vid"],
            rev=previous.rev + 1,
            source="founder",
            created_at=utcnow(),
            structured=json.dumps(
                {
                    "offer": "form builder",
                    "problem": "collect structured orders",
                    "desired_outcome": "Test one paying customer",
                }
            ),
        )
        session.add(revision)
        session.flush()
        idea = session.get(IdeaSession, d["sid"])
        idea.current_revision_id = idea.confirmed_revision_id = revision.id
        session.commit()
    ensure_account(d["vid"], workspace_id=d["wid"], owner_person_id=d["actor"])
    with bind(workspace_id=d["wid"], venture_id=d["vid"], actor=d["actor"]):
        jid = start(d["vid"], phase="oss")["job_id"]
    result = run(d, "research.deep", jid)
    assert result["review"] and result["coverage_gaps"]
    with d["factory"]() as session:
        findings = session.scalars(
            select(ResearchFinding).where(ResearchFinding.venture_id == d["vid"])
        ).all()
        assert findings and all(
            f.url.startswith("https://github.com/") for f in findings
        )
