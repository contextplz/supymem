"""Migrated server-origin QA admission; only model/render/cloud transports are fake."""
import asyncio
import hashlib
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from tests.integration.test_postgres_auth_security import pg_database as pg_database  # noqa: F401
from tests.integration.test_postgres_auth_security import concurrent
from tests.integration.test_postgres_journey_jobs import journey_db as journey_db  # noqa: F401
from tests.integration.test_postgres_build_intents import ready_plan as ready_plan  # noqa: F401
from tests.integration.test_postgres_subscription_static import planning as planning  # noqa: F401
from tests.integration.test_postgres_planning import planning as planning_standard  # noqa: F401
from tests.integration.test_postgres_pagebuilder import pagebuilder_db as setup  # noqa: F401
from tests.lifecycle_fixtures import runtime, qa_definition
from tests.qa_storage_fixtures import MemoryS3, report_fixture
from tests.unit.pagebuilder.test_api import install_pipeline_stub
from tests.unit.pagebuilder_admission.test_no_plan_inputs import no_plan_candidate as no_plan_candidate  # noqa: F401
from tests.integration.test_postgres_static_publication import _hosting_environment, _verified_waitlist


@pytest.fixture(autouse=True)
def hosting_before_plan(monkeypatch):
    _hosting_environment(monkeypatch)


@pytest.fixture()
def candidate(ready_plan, monkeypatch):
    from app.builder.artifact_store import S3ArtifactStore, ArtifactStoreConfig
    from app.core.jobs import lease, complete_job
    from app.db.models import Job
    from app.pagebuilder import api, jobs
    d = ready_plan
    for key, value in {"SUPYMEM_PAGEBUILDER_ENABLED": "1", "SUPYMEM_PAGEBUILDER_ADMISSION_ENABLED": "1",
        "LLM_OFFLINE": "0", "LLM_API_KEY": "offline-model-fixture", "LLM_BASE_URL": "https://provider.example.invalid",
        "LLM_PROVIDER": "azure_foundry", "LLM_DEPLOYMENT_PRO": "DeepSeek-V4-Pro",
        "PUBLIC_GATEWAY_URL": "https://gateway.supymem.com", "COMPANY_OS_VAULT_SECRET": "server-source-test-vault-more-than-thirty-two"}.items():
        monkeypatch.setenv(key, value)
    fixture = runtime()
    monkeypatch.setenv("BUILD_QA_FARGATE_PROFILE", json.dumps(fixture["qa_profile"]))
    monkeypatch.setenv("BUILD_FARGATE_TASK_PRICING", json.dumps(fixture["task_pricing"]))
    store = S3ArtifactStore(ArtifactStoreConfig("test-artifacts", "managed-builds",
        "arn:aws:kms:us-east-1:123456789012:key/11111111-1111-1111-1111-111111111111", "us-east-1", "123456789012"), MemoryS3())
    monkeypatch.setattr(S3ArtifactStore, "configured", lambda: store)
    install_pipeline_stub(monkeypatch)
    started = api.start(d["vid"], d["actor"])
    with d["factory"]() as session:
        job = lease(session, "server-candidate-worker", kinds=[api.KIND], n=1)[0]
        session.commit()
    payload = job.payload | {"_job_id": job.id, "_worker_id": job.worker_id, "_job_attempt": job.attempts}
    jobs.run(payload)
    with d["factory"]() as session:
        complete_job(session, job.id, worker_id=job.worker_id, attempt=job.attempts)
        session.commit()
        assert session.get(Job, job.id).status == "succeeded"
    async def no_more_model(*args, **kwargs):
        pytest.fail("Source QA/review/publication must never regenerate or call a model")
    monkeypatch.setattr("llm_gateway.sole.transmit", no_more_model)
    return {**d, "job_id": started["job_id"], "store": store, "fixture": fixture,
        "caller": SimpleNamespace(sub=d["actor"], role="user", legacy=False),
        "selection": {"job_id": started["job_id"], "variant": 0, "plan_id": d["plan_id"], "plan_task_id": d["plan_task_id"]}}


def admit(d):
    from app.pagebuilder_admission import admission
    quote = admission.estimate(d["vid"], d["caller"], d["selection"])
    accepted = admission.admit(d["vid"], d["caller"], d["selection"], quote_id=quote["quote_id"],
        quote_digest=quote["quote_digest"], approve_qa_ceiling_micro=quote["qa_ceiling_micro"])
    return accepted, quote


def test_genuine_server_source_creates_only_one_qa_intent_without_native_receipt(candidate):
    from app.db.models import CreditReservation
    from app.db.supymem_models import BuildRun, BuildModelCall, CodingSourceReceipt
    from app.pagebuilder_admission import admission
    d = candidate
    result, quote = admit(d)
    assert result["mode"] == "pagebuilder_source_qa" and result["verified_execution"] is False
    assert admission.estimate(d["vid"], d["caller"], d["selection"])["existing_run"]["id"] == result["id"]
    with d["factory"]() as session:
        runs = list(session.scalars(select(BuildRun).where(BuildRun.app_id == d["app_id"])))
        assert len(runs) == 1 and runs[0].trigger == "pagebuilder_source_qa"
        spec = json.loads(runs[0].spec)
        assert spec["source_origin"] == "pagebuilder_candidate_v1" and spec["execution_verified"] is False
        assert "device_id" not in spec["source_import"]["identity"]["scope"]
        assert not session.scalar(select(CodingSourceReceipt.order_id))
        assert not session.scalar(select(BuildModelCall.id).where(BuildModelCall.build_run_id == result["id"]))
        children = list(session.scalars(select(CreditReservation).where(CreditReservation.parent_id == runs[0].reservation_id)))
        assert len(children) == 1 and children[0].purpose == "build.compute.qa"
        assert quote["model_ceiling_micro"] == quote["builder_compute_ceiling_micro"] == 0


@pytest.fixture()
def cloud(candidate, monkeypatch):
    result, quote = admit(candidate)
    return qa_cloud(candidate, monkeypatch, result["id"], quote)


def qa_cloud(d, monkeypatch, build_run_id, quote=None):
    from app.builder import qa_lifecycle as q
    from app.infra.fargate_qa import FargateQaProvider, QaProfile
    from tests.fargate_fixtures import FakeAWS
    profile = QaProfile.from_dict(d["fixture"]["qa_profile"])
    aws = FakeAWS(profile)
    aws.definition = qa_definition(profile)
    real_launch = aws.run_task
    def launch(**body):
        try:
            return real_launch(**body)
        finally:
            for task in aws.tasks.values():
                task["containers"][0]["name"] = "static-qa"
    aws.run_task = launch
    monkeypatch.setattr(q, "configured_provider", lambda _: FargateQaProvider(profile, ecs=aws, ec2=aws))
    state = {"phase": "running"}
    def progress(binding, request, reader, *, permit_execution):
        permit_execution()
        assert reader()
        state.update(input=request, id=binding["qa_id"])
        return {"phase": state["phase"]}
    monkeypatch.setattr(q.qa_transport, "progress", progress)
    def result(binding, _reader):
        blob, evidence = report_fixture(state, binding["build_id"])
        if state.get("failed_report"):
            from core.artifact_snapshot import canonical
            report = json.loads(blob)
            next(case for case in report["cases"] if case["id"] == "desktop-0:render")["status"] = "failed"
            report["outcome"] = "failed"
            blob = canonical(report)
        return blob, evidence
    monkeypatch.setattr(q.qa_transport, "result", result)
    return {**d, "build_run_id": build_run_id, "qa": aws, "qa_state": state, "qa_quote": quote}


def step(d):
    from tests.integration.test_postgres_fargate_lifecycle import claim, saved
    from app.builder import orchestration
    _job, payload = claim(d, "build.watch")
    orchestration.watch(payload)
    return saved(d)


def test_actual_independent_qa_then_explicit_founder_acceptance(cloud):
    from app.builder.execution import read, review
    from app.hosting.service import _accepted_source, HostingError
    from app.db.supymem_models import FounderApp
    d = cloud
    first = step(d)
    assert len(d["qa"].launches) == 1, first
    d["qa_state"]["phase"] = "completed"
    step(d)
    final = step(d)
    assert final["closed"] and final["state"] == "succeeded" and final["spent"] > 0 and final["reserved"] == 0, final
    evidence = read(d["build_run_id"], actor=d["actor"])
    assert evidence["verified_execution"] is False and evidence["verified_source_import"] is True
    assert evidence["review"]["status"] == "pending"
    with d["factory"]() as session:
        with pytest.raises(HostingError, match="hosting_build_not_accepted"):
            _accepted_source(session, session.get(FounderApp, d["app_id"]), d["build_run_id"])
    body = dict(actor=d["actor"], source_artifact_id=evidence["artifact_id"], report_digest=evidence["review"]["report_digest"], decision="accept")
    assert review(d["build_run_id"], **body) == review(d["build_run_id"], **body)
    with d["factory"]() as session:
        selected = _accepted_source(session, session.get(FounderApp, d["app_id"]), d["build_run_id"])
        assert selected[0].id == d["build_run_id"]


@pytest.mark.parametrize("change", ["revision", "contract", "source", "unknown_hold", "flag", "archive", "actor"])
def test_changed_or_unsettled_source_cannot_reserve_qa(candidate, monkeypatch, change):
    from app.db.models import Job, Venture
    from app.db.supymem_models import IdeaSession, ExecutionPlan, BuildRun
    from app.pagebuilder_admission import admission
    from app.credits._txn import naive_utc
    d = candidate
    quote = admission.estimate(d["vid"], d["caller"], d["selection"])
    caller = d["caller"]
    if change == "flag":
        monkeypatch.setenv("SUPYMEM_PAGEBUILDER_ADMISSION_ENABLED", "0")
    elif change == "actor":
        caller = SimpleNamespace(sub="usr_foreign", role="user", legacy=False)
    else:
        with d["factory"]() as session:
            if change == "revision":
                session.get(IdeaSession, d["sid"]).current_revision_id = "changed"
            if change == "contract":
                plan = session.get(ExecutionPlan, d["plan_id"])
                doc = json.loads(plan.structured)
                doc["proposal"]["milestones"][0]["tasks"][0]["landing_expectations"]["contract"]["product_name"] = "New offer"
                plan.structured = json.dumps(doc)
            if change in {"source", "unknown_hold"}:
                job = session.get(Job, d["job_id"])
                doc = json.loads(job.payload_json)
                if change == "source":
                    doc["result"]["bundles"][0]["spec_markdown"] += " changed"
                else:
                    next(iter(doc["_runtime"]["operations"].values()))["usage"]["settled"] = False
                job.payload_json = json.dumps(doc)
            if change == "archive":
                session.get(Venture, d["vid"]).archived_at = naive_utc()
            session.commit()
    with pytest.raises(Exception):
        admission.admit(d["vid"], caller, d["selection"], quote_id=quote["quote_id"],
            quote_digest=quote["quote_digest"], approve_qa_ceiling_micro=quote["qa_ceiling_micro"])
    with d["factory"]() as session:
        assert not session.scalar(select(BuildRun.id).where(BuildRun.app_id == d["app_id"]))


def test_uncertain_immutable_storage_retries_only_same_source_without_model_or_qa_relaunch(cloud):
    from app.pagebuilder_admission import admission
    from app.db.models import Job
    d = cloud
    with d["factory"]() as session:
        before = json.loads(session.get(Job, d["job_id"]).payload_json)["_runtime"]["operations"]
    d["store"].client.lose_head_after_put = True
    first = step(d)
    assert not d["qa"].launches and first["spec"]["source_import"]["state"] != "stored"
    assert admission.estimate(d["vid"], d["caller"], d["selection"])["existing_run"]["id"] == d["build_run_id"]
    recovered = step(d)
    assert len(d["qa"].launches) == 1 and recovered["spec"]["source_import_verified"] is True
    with d["factory"]() as session:
        assert json.loads(session.get(Job, d["job_id"]).payload_json)["_runtime"]["operations"] == before


def test_signed_boundary_requires_exact_ceiling_and_rejects_native_or_voice_shortcuts(candidate, monkeypatch):
    from fastapi.testclient import TestClient
    from core.assertion import sign_assertion
    d = candidate
    secret = "server-source-route-assertion-more-than-thirty-two"
    monkeypatch.setenv("COS_INTERNAL_ASSERTION_SECRET", secret)
    monkeypatch.setenv("ADMIN_BOOTSTRAP_EMAILS", "admin@fixture.invalid")
    from app.main import create_app
    app = create_app()
    assert any(path.endswith("/pagebuilder-admission/quote") for path in app.openapi()["paths"])
    client = TestClient(app)
    base = f"/ventures/{d['vid']}/pagebuilder-admission"
    def post(path, body, extra=None):
        return client.post(path, json=body, headers={"X-COS-Assertion": sign_assertion(secret,
            sub=d["actor"], email="", role="user", path=path, method="POST"), **(extra or {})})
    assert client.post(base + "/quote", json=d["selection"]).status_code == 401
    quote = post(base + "/quote", d["selection"])
    assert quote.status_code == 200, quote.text
    q = quote.json()
    body = d["selection"] | {"quote_id": q["quote_id"], "quote_digest": q["quote_digest"], "approve_qa_ceiling_micro": q["qa_ceiling_micro"], "confirmed": True}
    assert post(base + "/admit", body | {"confirmed": 1}).status_code == 422
    assert post(base + "/admit", body | {"execution_verified": True}).status_code == 422
    assert post(base + "/admit", body, {"X-SupyMem-Source": "voice"}).status_code == 403
    assert post(base + "/admit", body | {"approve_qa_ceiling_micro": 1}).status_code == 409
    result = post(base + "/admit", body)
    assert result.status_code == 200, result.text
    assert result.json()["verified_execution"] is False


def test_existing_publisher_runs_only_after_explicit_review_and_completes_public_contract(cloud):
    from app.builder.execution import read, review
    from app.db.supymem_models import PlanTask
    from app.hosting.service import queue_accepted_build, publish_accepted_build, HostingError
    from tests.integration.test_postgres_static_task_completion import Provider
    d = cloud
    with pytest.raises(HostingError):
        queue_accepted_build(d["app_id"], d["build_run_id"], actor=d["actor"])
    step(d)
    d["qa_state"]["phase"] = "completed"
    step(d)
    assert step(d)["state"] == "succeeded"
    evidence = read(d["build_run_id"], actor=d["actor"])
    with pytest.raises(HostingError, match="hosting_build_not_accepted"):
        queue_accepted_build(d["app_id"], d["build_run_id"], actor=d["actor"])
    review(d["build_run_id"], actor=d["actor"], source_artifact_id=evidence["artifact_id"],
        report_digest=evidence["review"]["report_digest"], decision="accept")
    with d["factory"]() as session:
        assert session.get(PlanTask, d["plan_task_id"]).state == "waiting_for_founder"
    _verified_waitlist(d)  # Explicit storage fixture; live public conversion acceptance is separate.
    queued = queue_accepted_build(d["app_id"], d["build_run_id"], actor=d["actor"])
    provider = Provider()
    live = publish_accepted_build(queued["id"], d["app_id"], d["build_run_id"], actor=d["actor"], provider=provider)
    assert live["state"] == "live"
    assert publish_accepted_build(queued["id"], d["app_id"], d["build_run_id"], actor=d["actor"], provider=provider) == live
    assert provider.effects == ["upload", "switch", "verified_public_readback"]
    with d["factory"]() as session:
        assert session.get(PlanTask, d["plan_task_id"]).state == "done"
    assert read(d["build_run_id"], actor=d["actor"])["task_completion"]["state"] == "completed"


def test_concurrent_confirmations_create_one_server_qa_pool(candidate):
    from app.pagebuilder_admission import admission
    from app.db.models import CreditReservation
    from app.db.supymem_models import BuildRun
    d = candidate
    quote = admission.estimate(d["vid"], d["caller"], d["selection"])
    rows = concurrent(lambda: admission.admit(d["vid"], d["caller"], d["selection"], quote_id=quote["quote_id"],
        quote_digest=quote["quote_digest"], approve_qa_ceiling_micro=quote["qa_ceiling_micro"]))
    assert len({r["id"] for r in rows}) == 1
    with d["factory"]() as session:
        runs = list(session.scalars(select(BuildRun).where(BuildRun.app_id == d["app_id"])))
        assert len(runs) == 1
        children = list(session.scalars(select(CreditReservation).where(CreditReservation.parent_id == runs[0].reservation_id)))
        assert len(children) == 1


def test_scope_change_during_storage_releases_unlaunched_qa_without_acceptance(cloud):
    from app.db.supymem_models import ExecutionPlan
    d = cloud
    def change():
        with d["factory"]() as session:
            plan = session.get(ExecutionPlan, d["plan_id"])
            doc = json.loads(plan.structured)
            doc["proposal"]["milestones"][0]["tasks"][0]["landing_expectations"]["contract"]["product_name"] = "Changed scope"
            plan.structured = json.dumps(doc)
            session.commit()
    d["store"].client.after_put = change
    stopped = step(d)
    assert not d["qa"].launches and stopped["closed"] and stopped["reserved"] == 0
    assert stopped["spec"]["source_import_verified"] is False


def test_unknown_qa_launch_is_discovery_only_and_keeps_one_admission(cloud):
    from app.pagebuilder_admission import admission
    from app.db.supymem_models import QaRun
    d = cloud
    d["qa"].lose_launch_response = True
    first = step(d)
    assert first["spec"]["lifecycle"]["qa"]["cleanup_state"] == "pending"
    attempts = len(d["qa"].launches)
    with d["factory"]() as session:
        qa = session.scalar(select(QaRun).where(QaRun.build_run_id == d["build_run_id"]))
        receipt = json.loads(qa.receipt_json)["lifecycle"]
        assert receipt["phase"] == "uncertain" and receipt["discovery_only"] is True
    assert admission.estimate(d["vid"], d["caller"], d["selection"])["existing_run"]["id"] == d["build_run_id"]
    step(d)
    assert len(d["qa"].launches) == attempts


def test_false_origin_or_execution_flag_never_satisfies_publisher_proof(cloud):
    from app.builder.execution import read, review
    from app.db.supymem_models import BuildRun, FounderApp
    from app.hosting.service import _accepted_source, HostingError
    d = cloud
    step(d)
    d["qa_state"]["phase"] = "completed"
    step(d)
    step(d)
    evidence = read(d["build_run_id"], actor=d["actor"])
    review(d["build_run_id"], actor=d["actor"], source_artifact_id=evidence["artifact_id"],
        report_digest=evidence["review"]["report_digest"], decision="accept")
    with d["factory"]() as session:
        row = session.get(BuildRun, d["build_run_id"])
        original = json.loads(row.spec)
        for field, value in [("source_origin", "native"), ("execution_verified", True)]:
            row.spec = json.dumps(original | {field: value})
            session.flush()
            with pytest.raises(HostingError, match="hosting_build_not_accepted"):
                _accepted_source(session, session.get(FounderApp, d["app_id"]), row.id)


@pytest.fixture()
def no_plan_cloud(no_plan_candidate, monkeypatch):
    from app.pagebuilder_admission import publication

    d = no_plan_candidate
    requested = publication.request(d["vid"], d["caller"], d["selection"])
    assert requested["state"] == "checking" and requested["live_url"] is None
    return qa_cloud(d, monkeypatch, requested["run_id"])


def worker_step(d, kind):
    """The exact migrated venture job, through real leasing, scope and completion."""
    from app.core.jobs import lease
    from app.credits._txn import naive_utc
    from app.db.models import Job
    from app.worker.handlers import install_default_handlers
    from app.worker.main import _run_one

    with d["factory"]() as session:
        row = session.scalar(select(Job).where(Job.workspace_id == d["wid"], Job.venture_id == d["vid"],
            Job.kind == kind, Job.status == "scheduled").order_by(Job.created_at.desc(), Job.id.desc()).limit(1))
        assert row is not None, "This venture must have one durable continuation"
        job_id = row.id
        row.run_after = row.scheduled_for = naive_utc()
        session.commit()
    with d["factory"]() as session:
        jobs = lease(session, "no-plan-acceptance-worker", kinds=[kind], n=100)
        job = next(row for row in jobs if row.id == job_id)
        session.commit()
    install_default_handlers()
    asyncio.run(_run_one(d["factory"], job, {job.id}))
    with d["factory"]() as session:
        row = session.get(Job, job.id)
        session.expunge(row)
        return row


def complete_no_plan_qa(d):
    from tests.integration.test_postgres_fargate_lifecycle import saved

    for phase in ("running", "completed", "completed"):
        d["qa_state"]["phase"] = phase
        job = worker_step(d, "build.watch")
        assert job.status == "succeeded", job.last_error
    return saved(d)


def public_probe_transport(d, monkeypatch, *, persist=True, after_submit=None):
    """Fake only the HTTPS hop; consent, test token and durable readback are real."""
    import httpx
    from app.conversion import waitlist

    client = httpx.Client
    requests = []

    def public_submit(request):
        assert request.method == "POST" and str(request.url) == waitlist.public_url(d["vid"])
        payload = json.loads(request.content)
        details = waitlist.public_details(d["vid"])
        assert payload["consent"] == "yes" and payload["consent_version"] == details["consent_version"]
        assert payload["consent_digest"] == hashlib.sha256(details["consent"].encode()).hexdigest()
        assert payload.get("test_token") and payload["email"].endswith("@qa.invalid")
        requests.append(True)
        result = waitlist.capture(d["vid"], payload) if persist else {"received": True, "email_verified": False}
        if after_submit:
            after_submit()
        return httpx.Response(201, json=result)

    def owned_client(**kwargs):
        assert kwargs["follow_redirects"] is False and kwargs["trust_env"] is False and kwargs["timeout"] == 8
        return client(transport=httpx.MockTransport(public_submit), **kwargs)

    monkeypatch.setattr(waitlist.httpx, "Client", owned_client)
    return requests


def assert_no_plan_rows(d):
    from app.db.supymem_models import ExecutionPlan, PlanTask

    with d["factory"]() as session:
        for model in (ExecutionPlan, PlanTask):
            assert session.scalar(select(func.count()).select_from(model).where(
                model.workspace_id == d["wid"], model.venture_id == d["vid"])) == 0


def test_pg_no_plan_one_publish_click_probes_real_consent_and_reaches_live_without_duplicate_costs(no_plan_cloud, monkeypatch):
    from app.api.pagebuilder_api import profile
    from app.builder.execution import read
    from app.conversion import waitlist
    from app.db.models import CreditReservation
    from app.db.supymem_models import AppEvent, BuildRun, Deployment, VentureWaitlistEntry
    from app.hosting.service import AwsStaticPublisher
    from app.pagebuilder_admission import publication
    from tests.integration.test_postgres_static_task_completion import Provider

    d = no_plan_cloud
    assert_no_plan_rows(d)
    current = profile(d["vid"], d["caller"])
    assert current["plan_id"] is None and current["task_id"] is None
    assert current["current"]["server_publish_ready"] and not current["current"]["native_ready"]
    assert not waitlist.status(d["vid"], d["wid"])["submission_verified"]
    requests = public_probe_transport(d, monkeypatch)
    repeats = concurrent(lambda: publication.request(d["vid"], d["caller"], d["selection"]), count=3)
    assert {item["run_id"] for item in repeats} == {d["build_run_id"]}
    closed = complete_no_plan_qa(d)
    assert closed["state"] == "succeeded" and closed["closed"] and closed["reserved"] == 0
    assert closed["spec"]["execution_verified"] is False and closed["spec"]["inputs"]["plan_id"] is None
    assert closed["spec"]["lifecycle"]["review"]["status"] == "pending"
    job = worker_step(d, publication.KIND)
    assert job.status == "succeeded", job.last_error
    assert requests == [True]
    evidence = read(d["build_run_id"], actor=d["actor"])
    assert evidence["review"]["status"] == "accepted" and evidence["verified_execution"] is False
    assert evidence["task_completion"] is None

    class ReadbackProvider(Provider):
        def put_files(self, **kwargs):
            self.files = {item["path"]: item for item in kwargs["files"]}
            for item in self.files.values():
                assert hashlib.sha256(item["body"]).hexdigest() == item["sha256"]
            super().put_files(**kwargs)

        def verify_public(self, hostname, index_sha):
            assert hashlib.sha256(self.files["index.html"]["body"]).hexdigest() == index_sha
            assert hostname == closed["spec"]["source_import"]["identity"]["hostname"]
            super().verify_public(hostname, index_sha)

    provider = ReadbackProvider()
    monkeypatch.setattr(AwsStaticPublisher, "configured", lambda: provider)
    deployed = worker_step(d, "deploy.run")
    assert deployed.status == "succeeded", deployed.last_error
    live = publication.status(d["vid"], d["caller"], d["build_run_id"])
    assert live["state"] == "live" and live["live_url"].startswith("https://")
    assert publication.request(d["vid"], d["caller"], d["selection"]) == live
    assert publication.current(d["vid"], d["caller"], d["selection"]) == live
    assert provider.effects == ["upload", "switch", "verified_public_readback"]
    status = waitlist.status(d["vid"], d["wid"], include_leads=True)
    assert status["submission_verified"] and status["total"] == 0 and status["leads"] == []
    assert_no_plan_rows(d)
    with d["factory"]() as session:
        runs = session.scalars(select(BuildRun).where(BuildRun.venture_id == d["vid"])).all()
        assert len(runs) == 1 and runs[0].plan_task_id is None
        holds = session.scalars(select(CreditReservation).where(CreditReservation.parent_id == runs[0].reservation_id)).all()
        assert len(holds) == 1 and holds[0].purpose == "build.compute.qa" and holds[0].status == "settled"
        assert session.scalar(select(func.count()).select_from(Deployment).where(Deployment.venture_id == d["vid"])) == 1
        entries = session.scalars(select(VentureWaitlistEntry).where(VentureWaitlistEntry.venture_id == d["vid"])).all()
        assert len(entries) == 1 and entries[0].is_test and entries[0].consent_version == waitlist.VERSION
        assert session.scalar(select(func.count()).select_from(AppEvent).where(AppEvent.app_id == d["app_id"], AppEvent.name == "signup")) == 0


@pytest.mark.parametrize("fault", ["qa_failed", "source_changed", "unverified_consent", "disabled_consent"])
def test_pg_no_plan_refusals_never_queue_publication_effects(no_plan_cloud, monkeypatch, fault):
    from app.conversion import waitlist
    from app.db.models import Job
    from app.db.supymem_models import BuildRun, Deployment
    from app.hosting.service import AwsStaticPublisher
    from app.pagebuilder_admission import publication
    from tests.integration.test_postgres_fargate_lifecycle import saved
    from tests.integration.test_postgres_static_task_completion import Provider

    d = no_plan_cloud
    requests = public_probe_transport(d, monkeypatch, persist=fault != "unverified_consent")
    provider = Provider()
    monkeypatch.setattr(AwsStaticPublisher, "configured", lambda: provider)
    if fault == "source_changed":
        with d["factory"]() as session:
            source = session.get(Job, d["job_id"])
            doc = json.loads(source.payload_json)
            doc["result"]["bundles"][0]["spec_markdown"] += " changed source binding"
            source.payload_json = json.dumps(doc)
            session.commit()
        assert worker_step(d, "build.watch").status == "succeeded"
        stopped = saved(d)
        assert stopped["state"] == "failed" and stopped["closed"] and stopped["reserved"] == 0
        assert not d["qa"].launches
    else:
        d["qa_state"]["failed_report"] = fault == "qa_failed"
        closed = complete_no_plan_qa(d)
        if fault == "qa_failed":
            assert closed["state"] == "failed" and closed["closed"] and closed["reserved"] == 0
        else:
            if fault == "disabled_consent":
                waitlist.configure(d["vid"], d["wid"], enabled=False, actor=d["actor"])
            job = worker_step(d, publication.KIND)
            assert job.status == "failed_dead" and job.last_error == "pagebuilder_publication_refused"
    assert not provider.effects
    assert requests == ([True] if fault == "unverified_consent" else [])
    with d["factory"]() as session:
        assert not session.scalar(select(Deployment.id).where(Deployment.venture_id == d["vid"]))
        assert not session.scalar(select(Job.id).where(Job.venture_id == d["vid"], Job.kind == "deploy.run"))
        run = session.get(BuildRun, d["build_run_id"])
        spec = json.loads(run.spec)
        assert spec["execution_verified"] is False and spec["lifecycle"]["review"].get("decision") != "accept"
    assert_no_plan_rows(d)


@pytest.mark.parametrize("change", ["cancelled", "replaced"])
def test_pg_no_plan_publication_lease_change_during_probe_cannot_accept_or_deploy(no_plan_cloud, monkeypatch, change):
    from app.db.models import Job
    from app.db.supymem_models import BuildRun, Deployment
    from app.pagebuilder_admission import publication

    d = no_plan_cloud
    closed = complete_no_plan_qa(d)
    job_id = closed["spec"]["publication_request"]["job_id"]

    def revoke_after_public_submission():
        with d["factory"]() as session:
            job = session.get(Job, job_id)
            assert job.status == "leased"
            if change == "cancelled":
                job.status = "cancelled"
            else:
                job.attempts += 1
                job.locked_by = "replacement-publication-worker"
            session.commit()

    requests = public_probe_transport(d, monkeypatch, after_submit=revoke_after_public_submission)
    worker_step(d, publication.KIND)
    assert requests == [True]
    with d["factory"]() as session:
        run = session.get(BuildRun, d["build_run_id"])
        assert json.loads(run.spec)["lifecycle"]["review"]["status"] == "pending"
        assert not session.scalar(select(Deployment.id).where(Deployment.venture_id == d["vid"]))
        assert not session.scalar(select(Job.id).where(Job.venture_id == d["vid"], Job.kind == "deploy.run"))
        current = session.get(Job, job_id)
        assert current.status == ("cancelled" if change == "cancelled" else "leased")
        if change == "replaced":
            assert current.locked_by == "replacement-publication-worker"


def test_pg_no_plan_qa_closure_before_publish_approval_is_captured_queues_once(no_plan_candidate, monkeypatch):
    from app.db.models import Job
    from app.db.supymem_models import BuildRun
    from app.pagebuilder_admission import admission, publication

    d, admitted = no_plan_candidate, admission.admit
    completed = {}

    def close_before_click_is_saved(*args, **kwargs):
        accepted = admitted(*args, **kwargs)
        cloud = qa_cloud(d, monkeypatch, accepted["id"])
        closed = complete_no_plan_qa(cloud)
        assert closed["state"] == "succeeded" and closed["closed"]
        assert "publication_request" not in closed["spec"]
        completed.update(cloud)
        return accepted

    monkeypatch.setattr(admission, "admit", close_before_click_is_saved)
    first = publication.request(d["vid"], d["caller"], d["selection"])
    assert first["state"] == "publishing" and first["run_id"] == completed["build_run_id"]
    assert publication.request(d["vid"], d["caller"], d["selection"]) == first
    assert_no_plan_rows(d)
    with d["factory"]() as session:
        run = session.get(BuildRun, first["run_id"])
        spec = json.loads(run.spec)
        assert spec["publication_request"]["job_id"]
        assert spec["execution_verified"] is False and spec["lifecycle"]["review"]["status"] == "pending"
        assert session.scalar(select(func.count()).select_from(Job).where(
            Job.venture_id == d["vid"], Job.kind == publication.KIND)) == 1


def test_pg_no_plan_explicit_probe_retry_recovers_same_source_and_closed_costs_once(no_plan_cloud, monkeypatch):
    import httpx
    from datetime import timedelta
    from app.conversion import waitlist
    from app.credits.accounts import get_account
    from app.db.models import CreditReservation, Job
    from app.db.supymem_models import BuildRun, Deployment, VentureWaitlist, VentureWaitlistEntry
    from app.hosting.service import AwsStaticPublisher
    from app.pagebuilder_admission import publication
    from core.clock import utcnow
    from tests.integration.test_postgres_static_task_completion import Provider

    d = no_plan_cloud
    closed = complete_no_plan_qa(d)
    spent = get_account(d["vid"]).spent_micro
    client, response = httpx.Client, {"status": 503, "requests": 0}

    def owned_submit(request):
        assert request.method == "POST" and str(request.url) == waitlist.public_url(d["vid"])
        response["requests"] += 1
        payload = json.loads(request.content)
        details = waitlist.public_details(d["vid"])
        assert payload.get("test_token") and payload["consent_digest"] == hashlib.sha256(details["consent"].encode()).hexdigest()
        result = waitlist.capture(d["vid"], payload) if response["status"] == 201 else {}
        return httpx.Response(response["status"], json=result)

    monkeypatch.setattr(waitlist.httpx, "Client", lambda **kwargs: client(transport=httpx.MockTransport(owned_submit), **kwargs))
    failed = worker_step(d, publication.KIND)
    assert failed.status == "failed_dead" and failed.last_error == "pagebuilder_publication_refused"
    observed = publication.status(d["vid"], d["caller"], d["build_run_id"])
    assert observed["retryable"] and observed["job_attempt"] == failed.attempts
    with d["factory"]() as session:
        job = session.get(Job, failed.id)
        original_job = (job.payload_json, job.dedupe_key)
        source_operations = json.loads(session.get(Job, d["job_id"]).payload_json)["_runtime"]["operations"]
        # The failed probe's bounded cooldown has elapsed before the founder's next click.
        session.get(VentureWaitlist, d["vid"]).probe_expires_at = utcnow() - timedelta(seconds=1)
        session.commit()
    response["status"] = 201
    retries = concurrent(lambda: publication.retry(d["vid"], d["caller"], d["build_run_id"],
        expected_attempt=observed["job_attempt"]), count=3)
    assert all(item["state"] == "publishing" and item["job_attempt"] == observed["job_attempt"] for item in retries)
    accepted = worker_step(d, publication.KIND)
    assert accepted.id == failed.id and accepted.status == "succeeded" and accepted.attempts == failed.attempts + 1
    provider = Provider()
    monkeypatch.setattr(AwsStaticPublisher, "configured", lambda: provider)
    assert worker_step(d, "deploy.run").status == "succeeded"
    live = publication.status(d["vid"], d["caller"], d["build_run_id"])
    assert live["state"] == "live" and live["live_url"].startswith("https://") and not live["retryable"]
    assert publication.retry(d["vid"], d["caller"], d["build_run_id"], expected_attempt=observed["job_attempt"]) == live
    assert provider.effects == ["upload", "switch", "verified_public_readback"] and response["requests"] == 2
    assert get_account(d["vid"]).spent_micro == spent and get_account(d["vid"]).reserved_micro == 0
    assert_no_plan_rows(d)
    with d["factory"]() as session:
        job = session.get(Job, failed.id)
        assert (job.payload_json, job.dedupe_key) == original_job
        assert json.loads(session.get(Job, d["job_id"]).payload_json)["_runtime"]["operations"] == source_operations
        runs = session.scalars(select(BuildRun).where(BuildRun.venture_id == d["vid"])).all()
        assert len(runs) == 1 and runs[0].artifact_id == closed["spec"]["artifact_capture"]["artifact_id"]
        assert runs[0].credits_used == closed["spent"] and json.loads(runs[0].spec)["execution_verified"] is False
        holds = session.scalars(select(CreditReservation).where(CreditReservation.parent_id == runs[0].reservation_id)).all()
        assert len(holds) == 1 and holds[0].purpose == "build.compute.qa" and holds[0].status == "settled"
        assert session.scalar(select(func.count()).select_from(Deployment).where(Deployment.venture_id == d["vid"])) == 1
        entries = session.scalars(select(VentureWaitlistEntry).where(VentureWaitlistEntry.venture_id == d["vid"])).all()
        assert len(entries) == 1 and entries[0].is_test
