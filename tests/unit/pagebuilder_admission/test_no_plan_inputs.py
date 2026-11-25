"""Genuine settled server candidates bind a no-plan contract without creating plan rows."""
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import event, func, select, text
from sqlalchemy.exc import OperationalError

from tests.unit.pagebuilder import test_api

setup = test_api.setup


def test_sqlite_writer_lock_is_reused_until_commit_or_rollback(setup, tmp_path):
    from app.db.session import make_engine, make_session_factory, serialize_writes

    engine = make_engine("sqlite:///" + str(tmp_path / "writer-lock.db"))
    statements = []
    event.listen(engine, "before_cursor_execute", lambda _conn, _cursor, statement, *_rest:
                 statements.append(statement) if statement == "BEGIN IMMEDIATE" else None)
    try:
        with make_session_factory(engine)() as session:
            serialize_writes(session, key="venture")
            serialize_writes(session, key="credit-account")
            assert len(statements) == 1
            with engine.connect() as competing:
                competing.exec_driver_sql("PRAGMA busy_timeout=0")
                with pytest.raises(OperationalError, match="database is locked"):
                    competing.exec_driver_sql("BEGIN IMMEDIATE")
            session.commit()
            serialize_writes(session, key="credit-account")
            assert len(statements) == 3  # The refused competing BEGIN was also observed.
            session.rollback()
            serialize_writes(session, key="venture")
            assert len(statements) == 4
            session.commit()
            session.execute(text("CREATE TABLE unmarked (id INTEGER)"))
            session.execute(text("INSERT INTO unmarked VALUES (1)"))
            with pytest.raises(OperationalError, match="cannot start a transaction within a transaction"):
                serialize_writes(session, key="late-lock")
    finally:
        engine.dispose()


@pytest.fixture()
def no_plan_candidate(setup, monkeypatch):
    from app.builder.artifact_store import ArtifactStoreConfig, S3ArtifactStore
    from app.core.jobs import complete_job
    from app.db.models import Job
    from app.pagebuilder import jobs, pipeline
    from app.pagebuilder.qa import qa_contract
    from tests.lifecycle_fixtures import runtime
    from tests.qa_storage_fixtures import MemoryS3

    monkeypatch.setenv("SUPYMEM_PAGEBUILDER_ADMISSION_ENABLED", "1")
    monkeypatch.setenv("COMPANY_OS_VAULT_SECRET", "no-plan-source-test-vault-more-than-thirty-two")
    fixture = runtime()
    monkeypatch.setenv("BUILD_QA_FARGATE_PROFILE", json.dumps(fixture["qa_profile"]))
    monkeypatch.setenv("BUILD_FARGATE_TASK_PRICING", json.dumps(fixture["task_pricing"]))
    store = S3ArtifactStore(ArtifactStoreConfig("test-artifacts", "managed-builds",
        "arn:aws:kms:us-east-1:123456789012:key/11111111-1111-1111-1111-111111111111",
        "us-east-1", "123456789012"), MemoryS3())
    monkeypatch.setattr(S3ArtifactStore, "configured", lambda: store)
    test_api.install_pipeline_stub(monkeypatch)
    generate = pipeline.generate_page

    def generated(inputs, call, **kwargs):
        page = generate(inputs, call, **kwargs)
        page.manifest["qa_contract"] = qa_contract(inputs, page.plan).model_dump(mode="json")
        return page

    monkeypatch.setattr(pipeline, "generate_page", generated)
    started, payload = test_api.claimed(setup)
    jobs.run(payload)
    with setup["factory"]() as session:
        complete_job(session, started["job_id"], worker_id=payload["_worker_id"], attempt=payload["_job_attempt"])
        session.commit()
        doc = json.loads(session.get(Job, started["job_id"]).payload_json)

    async def no_more_model(*args, **kwargs):
        pytest.fail("Admission must reuse the settled source without another model call")

    monkeypatch.setattr("llm_gateway.sole.transmit", no_more_model)
    return {**setup, "job_id": started["job_id"], "app_id": doc["_runtime"]["analytics_app_id"],
        "caller": SimpleNamespace(sub=setup["actor"], role="user", legacy=False),
        "selection": {"job_id": started["job_id"], "variant": 0, "plan_id": None, "plan_task_id": None},
        "store": store, "fixture": fixture}


def test_no_plan_contract_is_rederived_from_the_confirmed_input_and_reviewed_plan(no_plan_candidate):
    from app.db.models import Job
    from app.db.supymem_models import ExecutionPlan, PlanTask
    from app.pagebuilder.models import PageInput, PagePlan
    from app.pagebuilder.qa import qa_contract
    from app.pagebuilder_admission import inputs

    d = no_plan_candidate
    with d["factory"]() as session:
        envelope, app = inputs.current(session, d["vid"], d["caller"], d["selection"])
        doc = json.loads(session.get(Job, d["job_id"]).payload_json)
        bundle = doc["result"]["bundles"][0]
        expected = qa_contract(PageInput.model_validate(doc["_runtime"]["input"]), PagePlan.model_validate(bundle["plan"]))
        values = envelope["inputs"]
        assert values["task"]["landing_expectations"]["contract"] == expected.model_dump(mode="json")
        assert expected.required_text and len(expected.visuals) >= 2
        assert any(action.kind == "external_link" and action.target == values["pagebuilder_input"]["waitlist_url"]
                   for action in expected.actions)
        assert values["analytics_beacon"]["app_id"] == app.id == d["app_id"]
        assert values["product_context"]["revision_id"] == envelope["identity"]["scope"]["revision_id"] == d["rid"]
        assert not session.scalar(select(ExecutionPlan.id)) and not session.scalar(select(PlanTask.id))


def test_no_plan_admission_reserves_only_qa_once_and_keeps_execution_unverified(no_plan_candidate):
    from app.db.models import CreditReservation
    from app.db.supymem_models import BuildModelCall, BuildRun, ExecutionPlan, PlanTask
    from app.pagebuilder_admission import admission, inputs

    d = no_plan_candidate
    quote = admission.estimate(d["vid"], d["caller"], d["selection"])
    approval = dict(quote_id=quote["quote_id"], quote_digest=quote["quote_digest"],
                    approve_qa_ceiling_micro=quote["qa_ceiling_micro"])
    first = admission.admit(d["vid"], d["caller"], d["selection"], **approval)
    assert admission.admit(d["vid"], d["caller"], d["selection"], **approval)["id"] == first["id"]
    with d["factory"]() as session:
        run = session.get(BuildRun, first["id"])
        spec = json.loads(run.spec)
        assert inputs.no_plan(run, spec) and run.plan_task_id is None
        assert spec["execution_verified"] is False and spec["lifecycle_version"] == 3
        assert spec["inputs"]["plan_id"] is None and spec["inputs"]["plan_task_id"] is None
        assert session.scalar(select(func.count()).select_from(BuildRun)) == 1
        assert not session.scalar(select(ExecutionPlan.id)) and not session.scalar(select(PlanTask.id))
        assert not session.scalar(select(BuildModelCall.id))
        holds = session.scalars(select(CreditReservation).where(CreditReservation.parent_id == run.reservation_id)).all()
        assert len(holds) == 1 and holds[0].purpose == "build.compute.qa"
        assert not inputs.no_plan(SimpleNamespace(**{key: getattr(run, key) for key in
            ("workspace_id", "venture_id", "app_id", "plan_task_id")}, trigger="coding_source_import"), spec)
        changed = json.loads(run.spec)
        changed["source_import"]["identity"]["scope"]["workspace_id"] = "foreign-workspace"
        assert not inputs.no_plan(run, changed)


@pytest.mark.parametrize("change", ["revision", "research", "consent", "contract", "beacon", "source", "unsettled", "storage"])
def test_changed_or_unsettled_no_plan_candidate_cannot_reserve_qa(no_plan_candidate, monkeypatch, change):
    from app.builder.model_proxy import BuildModelError
    from app.db.models import CreditReservation, Job, VentureDocument
    from app.db.supymem_models import BuildRun, IdeaSession
    from app.pagebuilder import api, native_reference
    from app.pagebuilder_admission import admission

    d = no_plan_candidate
    quote = admission.estimate(d["vid"], d["caller"], d["selection"])
    if change == "research":
        snapshot = api.snapshot

        def changed_research(*args):
            rid, values = snapshot(*args)
            return rid, {**values, "research": {**values["research"], "new_evidence": "Changed research"}}

        monkeypatch.setattr(api, "snapshot", changed_research)
    elif change == "consent":
        monkeypatch.setattr("app.conversion.waitlist.VERSION", "changed-consent-version")
    else:
        with d["factory"]() as session:
            if change == "revision":
                session.get(IdeaSession, d["sid"]).current_revision_id = "changed-revision"
            elif change == "storage":
                saved = session.get(VentureDocument, native_reference._reference_id(d["job_id"], 0))
                stored = json.loads(saved.provenance_json)
                stored["receipt"]["version_id"] = "null"
                saved.provenance_json = json.dumps(stored)
            else:
                job = session.get(Job, d["job_id"])
                doc = json.loads(job.payload_json)
                bundle = doc["result"]["bundles"][0]
                if change == "contract":
                    bundle["manifest"]["qa_contract"]["product_name"] = "Changed product"
                elif change == "beacon":
                    bundle["manifest"]["analytics_beacon"]["endpoint"] = "https://foreign.example/analytics"
                elif change == "source":
                    bundle["spec_markdown"] += " Changed source specification"
                else:
                    next(iter(doc["_runtime"]["operations"].values()))["usage"]["settled"] = False
                job.payload_json = json.dumps(doc)
            session.commit()
    with pytest.raises((BuildModelError, ValueError, KeyError, HTTPException)):
        admission.admit(d["vid"], d["caller"], d["selection"], quote_id=quote["quote_id"],
            quote_digest=quote["quote_digest"], approve_qa_ceiling_micro=quote["qa_ceiling_micro"])
    with d["factory"]() as session:
        assert not session.scalar(select(BuildRun.id))
        assert not session.scalar(select(CreditReservation.id).where(CreditReservation.purpose == "build.compute.qa"))


@pytest.mark.parametrize("plan,task", [("plan_unknown", None), (None, "task_unknown"), ("plan_unknown", "task_unknown")])
def test_absent_or_partial_plan_ids_never_waive_a_selected_plan(no_plan_candidate, plan, task):
    from app.builder.model_proxy import BuildModelError
    from app.pagebuilder_admission import inputs

    d = no_plan_candidate
    with d["factory"]() as session, pytest.raises(BuildModelError, match="pagebuilder_admission_inputs_changed"):
        inputs.current(session, d["vid"], d["caller"], {**d["selection"], "plan_id": plan, "plan_task_id": task})
