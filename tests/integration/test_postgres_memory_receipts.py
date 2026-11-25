"""Actual migrated source/QA receipts back memory; provider prose cannot replace them.

Cloud QA/storage and the recorded deployment are existing owned fixtures. These
checks prove database receipt custody, not a live cloud or public HTTP request.
"""
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select

from tests.integration.test_postgres_auth_security import pg_database as pg_database  # noqa: F401
from tests.integration.test_postgres_journey_jobs import journey_db as journey_db  # noqa: F401
from tests.integration.test_postgres_planning import planning as planning_standard  # noqa: F401
from tests.integration.test_postgres_subscription_static import (
    accepted_native_publication as accepted_native_publication,
    planning as planning,
    ready_plan as ready_plan,
    source_cloud as source_cloud,
    source_ready as source_ready,
    source_step,
)


def receipt_write(d, reference, statement, key, *, kind=None):
    from app.memory.model import MemoryKind, MemoryWrite
    return MemoryWrite(venture_id=d["vid"], kind=kind or MemoryKind.OUTCOME,
                       subject="reviewed static source", statement=statement, evidence=[reference],
                       confidence=1, confidence_source="gate", author="system", dedupe_key=key)


def bind_owned(d):
    from app.tenancy import bind
    return bind(workspace_id=d["wid"], venture_id=d["vid"], actor=d["actor"])


def test_accepted_source_and_recorded_publication_are_distinct_owned_receipts(accepted_native_publication):
    from app.db.models import InstitutionalMemory
    from app.db.supymem_models import BuildModelCall
    from app.memory.adapters.postgres import PostgresMemoryAdapter
    from app.memory.evidence import evidence_current
    from app.memory.model import MemoryRecord, RecallQuery

    d = accepted_native_publication
    accepted = f"Static source {d['build_run_id']} was accepted with source-linked QA; publication is separate."
    with d["factory"]() as session:
        from app.db.supymem_models import Deployment
        url = session.get(Deployment, d["deployment_id"]).url
    published = f"Reviewed static source {d['build_run_id']} has a recorded live publication at {url}."
    reader, writer = PostgresMemoryAdapter(), PostgresMemoryAdapter()
    with bind_owned(d):
        first = asyncio.run(writer.remember(receipt_write(d, "accepted_source:" + d["build_run_id"], accepted, "accepted-only")))
        second = asyncio.run(writer.remember(receipt_write(d, "publication:" + d["deployment_id"], published, "recorded-publication")))
        hits = asyncio.run(reader.recall(RecallQuery(venture_id=d["vid"], text="", limit=10)))
        assert {row.mem_id for row in hits} == {first, second}
        with d["factory"]() as session:
            records = [MemoryRecord.model_validate_json(session.get(InstitutionalMemory, identity).record_json)
                       for identity in [first, second]]
            assert all(evidence_current(session, value) for value in records)
            assert [value.metadata["source_evidence"][0]["kind"] for value in records] == [
                "accepted_static_source", "recorded_static_publication"]
            assert all(value.metadata["workspace_id"] == d["wid"] for value in records)
            assert not session.scalar(select(BuildModelCall.id).where(BuildModelCall.build_run_id == d["build_run_id"]))


def test_source_acceptance_cannot_be_promoted_to_publication_or_preference(accepted_native_publication):
    from app.memory.adapters.postgres import PostgresMemoryAdapter
    from app.memory.model import MemoryKind
    d = accepted_native_publication
    adapter = PostgresMemoryAdapter()
    statement = f"Static source {d['build_run_id']} was accepted with source-linked QA; publication is separate."
    with bind_owned(d):
        with pytest.raises(ValueError, match="memory_statement_source_mismatch"):
            asyncio.run(adapter.remember(receipt_write(d, "accepted_source:" + d["build_run_id"],
                        "The app is publicly available and its launch is complete.", "false-publication")))
        with pytest.raises(ValueError, match="memory_receipt_kind_invalid"):
            asyncio.run(adapter.remember(receipt_write(d, "accepted_source:" + d["build_run_id"],
                        statement, "false-preference", kind=MemoryKind.FOUNDER_PREFERENCE)))


def test_successful_qa_without_founder_acceptance_cannot_back_outcome_memory(source_cloud):
    from app.memory.adapters.postgres import PostgresMemoryAdapter
    d = source_cloud
    source_step(d)
    d["qa_state"]["phase"] = "completed"
    source_step(d)
    assert source_step(d)["state"] == "succeeded"
    # The actual QA result exists but no founder source review is accepted.
    with bind_owned(d), pytest.raises(ValueError, match="memory_accepted_source_unverified"):
        asyncio.run(PostgresMemoryAdapter().remember(receipt_write(d, "accepted_source:" + d["build_run_id"],
                    f"Static source {d['build_run_id']} was accepted with source-linked QA; publication is separate.",
                    "unaccepted-qa")))


@pytest.mark.parametrize("tamper", ["qa_report", "artifact_digest", "deployment_state", "foreign_app"])
def test_receipt_tamper_is_revalidated_by_separate_reader(accepted_native_publication, tamper):
    from app.db.supymem_models import BuildArtifact, Deployment, FounderApp, QaRun
    from app.memory.adapters.postgres import PostgresMemoryAdapter
    from app.memory.model import RecallQuery
    d = accepted_native_publication
    with d["factory"]() as session:
        url = session.get(Deployment, d["deployment_id"]).url
    statement = f"Reviewed static source {d['build_run_id']} has a recorded live publication at {url}."
    writer, reader = PostgresMemoryAdapter(), PostgresMemoryAdapter()
    with bind_owned(d):
        identity = asyncio.run(writer.remember(receipt_write(d, "publication:" + d["deployment_id"], statement, "publication-tamper")))
        assert [row.mem_id for row in asyncio.run(reader.recall(RecallQuery(venture_id=d["vid"], text="")))] == [identity]
        with d["factory"]() as session:
            if tamper == "qa_report":
                qa = session.scalar(select(QaRun).where(QaRun.build_run_id == d["build_run_id"]))
                qa.report_digest = "f" * 64
            elif tamper == "artifact_digest":
                session.get(BuildArtifact, d["published_artifact_id"]).sha256 = "e" * 64
            elif tamper == "deployment_state":
                session.get(Deployment, d["deployment_id"]).state = "failed"
            else:
                # Same existing workspace/venture-independent source lookup is
                # refused even if the matching accepted artifact remains intact.
                from app.db.models import Venture
                from core.clock import utcnow
                from core.ids import new_id
                foreign = new_id("ven")
                session.add(Venture(id=foreign, workspace_id=d["wid"], name="Other receipt owner",
                                    raw_idea="Other owned fixture", stage="draft", autonomy_level="L1",
                                    created_at=utcnow(), updated_at=utcnow()))
                session.flush()
                session.get(FounderApp, d["app_id"]).venture_id = foreign
            session.commit()
        assert asyncio.run(reader.recall(RecallQuery(venture_id=d["vid"], text=""))) == []


def test_replanning_keeps_historical_receipt_context_without_execution_authority(accepted_native_publication):
    import json
    from app.db.supymem_models import ExecutionPlan, FounderApp
    from app.hosting.service import HostingError, _accepted_source
    from app.memory.adapters.postgres import PostgresMemoryAdapter
    from app.memory.model import RecallQuery
    from app.memory.task_context import for_task
    d = accepted_native_publication
    statement = f"Static source {d['build_run_id']} was accepted with source-linked QA; publication is separate."
    writer, reader = PostgresMemoryAdapter(), PostgresMemoryAdapter()
    with bind_owned(d):
        identity = asyncio.run(writer.remember(receipt_write(d, "accepted_source:" + d["build_run_id"],
                                                            statement, "historical-accepted")))
        assert [row.mem_id for row in asyncio.run(reader.recall(RecallQuery(venture_id=d["vid"], text="")))] == [identity]
        with d["factory"]() as session:
            plan = session.get(ExecutionPlan, d["plan_id"])
            document = json.loads(plan.structured)
            document["proposal"]["milestones"][0]["tasks"][0]["landing_expectations"]["contract"]["product_name"] = "New proposed offer"
            plan.structured = json.dumps(document)
            session.commit()
            # Changed current approval still prevents new hosting effects. It
            # must not erase an immutable, accurately worded prior acceptance.
            with pytest.raises(HostingError, match="hosting_build_not_accepted"):
                _accepted_source(session, session.get(FounderApp, d["app_id"]), d["build_run_id"])
            context = for_task(session, workspace_id=d["wid"], venture_id=d["vid"], title="Review new launch")
            assert context["availability"] == "available"
            assert context["effect_authority"] is False and context["current_state_authority"] is False
            assert context["statements"] == []  # Outcomes are not founder approval/preferences.
        hits = asyncio.run(reader.recall(RecallQuery(venture_id=d["vid"], text="")))
        assert len(hits) == 1 and hits[0].mem_id == identity and hits[0].statement == statement
