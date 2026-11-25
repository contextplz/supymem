from copy import deepcopy
from contextlib import nullcontext
import json
import time
from types import SimpleNamespace

import httpx
import pytest

from tests.fargate_fixtures import FakeAWS
from tests.lifecycle_fixtures import runtime, qa_definition
from tests.integration.test_postgres_auth_security import service_modules


@pytest.mark.parametrize("domain", ["Warehouse procurement", "Weekend cooking"])
def test_fenced_controller_handoffs_release_capacity_without_releasing_live_work(tmp_path, monkeypatch, domain):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    monkeypatch.setenv("SUPYMEM_MODE", "hosted")
    with service_modules("company_os"):
        from app.builder import orchestration
        from app.core.jobs import FatalError, complete_job, enqueue, lease
        from app.credits._txn import naive_utc
        from app.db.models import Base, Job

        monkeypatch.setenv("JOB_WORKSPACE_INFLIGHT_CAP", "2")
        engine = create_engine(f"sqlite:///{tmp_path / 'handoff.db'}")
        Base.metadata.create_all(engine)
        factory = sessionmaker(bind=engine)
        with factory() as session:
            live = enqueue(session, "future.bulk", {"domain": domain}, workspace_id="workspace")
            initial = enqueue(session, "build.run", {"domain": domain}, workspace_id="workspace")
            session.commit()
            lease(session, "live-worker", kinds=["future.bulk"])
            current, = lease(session, "controller", kinds=["build.run"])
            assert current.id == initial
            session.commit()
            spec = {"actor": "founder", "domain": domain,
                    "controller_fence": {"id": current.worker_id, "attempt": current.attempts}}
            ctx = SimpleNamespace(run=SimpleNamespace(id="build", workspace_id="workspace", venture_id=None,
                                                      spec=json.dumps(spec), closed_at=None))
            for _ in range(5):
                payload = {"_job_id": current.id, "_worker_id": current.worker_id, "_job_attempt": current.attempts}
                orchestration._commit(session, ctx, payload)
                assert session.get(Job, current.id).status == "leased"
                orchestration._followup(session, ctx, spec, delay=0)
                with pytest.raises(FatalError, match="build_controller_job_fence_lost"):
                    orchestration._commit(session, ctx, {**payload, "_job_attempt": current.attempts + 1})
                session.rollback()
                assert session.get(Job, current.id).status == "leased"
                assert session.get(Job, spec["watch_job_id"]) is None
                orchestration._followup(session, ctx, spec, delay=0)
                orchestration._commit(session, ctx, payload)
                retired = session.get(Job, current.id)
                assert retired.status == "succeeded" and retired.finished_at is not None
                assert retired.locked_by is None and retired.locked_at is None
                complete_job(session, current.id, worker_id=current.worker_id, attempt=current.attempts)
                current, = lease(session, "controller", kinds=["build.watch"])
                assert current.id == spec["watch_job_id"]
                assert session.get(Job, live).status == "leased"
                assert lease(session, "full", n=100) == []
                session.commit()
            ctx.run.closed_at = naive_utc()
            orchestration._commit(session, ctx, {"_job_id": current.id, "_worker_id": current.worker_id,
                                                "_job_attempt": current.attempts})
            assert session.get(Job, current.id).status == "succeeded"
            assert session.get(Job, live).status == "leased"
        engine.dispose()


@pytest.mark.parametrize("complete,code", [(True, "qa_checks_failed"), (False, "qa_runtime_failed")])
def test_stored_failed_qa_distinguishes_incomplete_runtime_from_completed_checks(monkeypatch, complete, code):
    with service_modules("company_os"):
        from app.builder import qa_lifecycle
        ctx, spec, launch = SimpleNamespace(), {}, {}
        row = SimpleNamespace(id="qa", input_digest="input", report_digest="report")
        receipt = {"phase": "stored", "outcome": "failed", "report_complete": complete,
            "input": {}, "source": {}, "scope": {}, "task_binding_digest": "binding"}
        observed = []
        monkeypatch.setattr(qa_lifecycle, "db", lambda: lambda: nullcontext(None))
        monkeypatch.setattr(qa_lifecycle, "_begin", lambda *_args: (ctx, spec))
        monkeypatch.setattr(qa_lifecycle, "_row", lambda *_args: (row, receipt, launch))
        monkeypatch.setattr(qa_lifecycle.qa_transport, "progress", lambda *_args, **_kwargs: {"phase": "running"})
        monkeypatch.setattr(qa_lifecycle, "_error", lambda _session, _ctx, saved, error: observed.append(error) or saved)
        for name in ("_save", "_write", "_followup", "_commit"):
            monkeypatch.setattr(qa_lifecycle, name, lambda *_args: None)
        qa_lifecycle._command({}, {})
        assert observed == [code] and spec["phase"] == "qa_stopping"
        assert launch["normal_stop_intent"]["report_digest"] == "report"


@pytest.fixture()
def modules():
    with service_modules("company_os"):
        from app.builder import lifecycle, qa_transport, expectations
        from app.infra import fargate_qa

        yield lifecycle, qa_transport, expectations, fargate_qa


def test_artifact_transport_preserves_only_allowlisted_content_rejection():
    with service_modules("company_os"):
        from app.builder import artifacts
        from app.builder.model_proxy import BuildModelError

        class Body(httpx.SyncByteStream):
            def __iter__(self):
                yield b'{"detail":"artifact_unapproved_contact_detail"}'

        def response(request):
            return httpx.Response(422, headers={"content-type": "application/json"}, stream=Body())

        with httpx.Client(transport=httpx.MockTransport(response)) as client:
            with pytest.raises(BuildModelError, match="artifact_unapproved_contact_detail"):
                artifacts._read(client, "https://builder.test/manifest", {}, limit=4096,
                                content_type="application/json", deadline=time.monotonic() + 5,
                                allowed_errors={"artifact_unapproved_contact_detail"})


def test_artifact_transport_does_not_trust_unknown_remote_error():
    with service_modules("company_os"):
        from app.builder import artifacts
        from app.builder.model_proxy import BuildModelError

        class Body(httpx.SyncByteStream):
            def __iter__(self):
                yield b'{"detail":"attacker_selected_code"}'

        def response(request):
            return httpx.Response(422, headers={"content-type": "application/json"}, stream=Body())

        with httpx.Client(transport=httpx.MockTransport(response)) as client:
            with pytest.raises(BuildModelError, match="artifact_task_read_failed"):
                artifacts._read(client, "https://builder.test/manifest", {}, limit=4096,
                                content_type="application/json", deadline=time.monotonic() + 5,
                                allowed_errors={"artifact_unapproved_contact_detail"})


def test_artifact_transport_accepts_only_content_free_visual_diagnostics():
    with service_modules("company_os"):
        from app.builder import artifacts
        from app.builder.model_proxy import BuildModelError

        diagnostics = {
            "page_readable": True, "candidate_count": 1,
            "qualified_distinct_count": 0,
            "candidates": [{"element_index": 1, "tag": "img", "semantic": True,
                            "name_ok": True, "distinct_name": True, "width_ok": True,
                            "height_ok": True, "responsive": False}],
        }
        body = json.dumps({"detail": {"code": "artifact_product_visual_contract_invalid",
                                      "diagnostics": diagnostics}}).encode()
        class Body(httpx.SyncByteStream):
            def __iter__(self):
                yield body
        with httpx.Client(transport=httpx.MockTransport(
            lambda request: httpx.Response(422, headers={"content-type": "application/json"}, stream=Body())
        )) as client:
            with pytest.raises(BuildModelError, match="artifact_product_visual_contract_invalid") as caught:
                artifacts._read(client, "https://builder.test/manifest", {}, limit=4096,
                                content_type="application/json", deadline=time.monotonic() + 5,
                                allowed_errors={"artifact_product_visual_contract_invalid"})
        assert caught.value.diagnostics == diagnostics

        diagnostics["candidates"][0]["alt"] = "private generated page content"
        body = json.dumps({"detail": {"code": "artifact_product_visual_contract_invalid",
                                      "diagnostics": diagnostics}}).encode()
        with httpx.Client(transport=httpx.MockTransport(
            lambda request: httpx.Response(422, headers={"content-type": "application/json"}, stream=Body())
        )) as client:
            with pytest.raises(BuildModelError, match="artifact_task_read_failed"):
                artifacts._read(client, "https://builder.test/manifest", {}, limit=4096,
                                content_type="application/json", deadline=time.monotonic() + 5,
                                allowed_errors={"artifact_product_visual_contract_invalid"})


def test_runtime_policy_includes_overhead_without_claiming_aws_zero(modules):
    life, _, _, _ = modules
    policy = runtime()
    assert life.validate_runtime(policy)
    assert policy["included_overhead"]["aws_actual_cost"] is None
    for mutate in [
        lambda p: p.update(artifact_ceiling_micro=1),
        lambda p: p["task_pricing"]["qa"].update(profile_digest="0" * 64),
        lambda p: p["task_pricing"]["builder"].update(micro_per_second="NaN"),
        lambda p: p["task_pricing"]["builder"].update(
            expires_at="2020-01-01T00:00:00+00:00"
        ),
        lambda p: p["runtime_policy"].update(tool_names=[]),
    ]:
        bad = deepcopy(policy)
        mutate(bad)
        with pytest.raises(ValueError):
            life.validate_runtime(bad)


@pytest.mark.parametrize(
    "fault",
    [
        "role",
        "mutable_image",
        "root",
        "writable_root",
        "logs",
        "secret",
        "network_tool",
        "mount",
        "sandbox_off",
        "architecture",
    ],
)
def test_qa_task_policy_rejects_unsafe_definition_before_launch(modules, fault):
    _, _, _, m = modules
    profile = m.QaProfile.from_dict(runtime()["qa_profile"])
    aws = FakeAWS(profile)
    aws.definition = qa_definition(profile)
    c = aws.definition["containerDefinitions"][0]
    if fault == "role":
        aws.definition["taskRoleArn"] = "master"
    if fault == "mutable_image":
        c["image"] = "qa:latest"
    if fault == "root":
        c["user"] = "0"
    if fault == "writable_root":
        c["readonlyRootFilesystem"] = False
    if fault == "logs":
        c["logConfiguration"] = {"logDriver": "awslogs"}
    if fault == "secret":
        c["secrets"] = [{"name": "AZURE_KEY", "valueFrom": "master"}]
    if fault == "network_tool":
        c["environment"] = [{"name": "HTTPS_PROXY", "value": "http://external"}]
    if fault == "mount":
        aws.definition["volumes"][0]["host"] = {"sourcePath": "/"}
    if fault == "sandbox_off":
        c["command"] = ["--no-sandbox"]
    if fault == "architecture":
        aws.definition["runtimePlatform"]["cpuArchitecture"] = "ARM64"
    provider = m.FargateQaProvider(profile, ecs=aws, ec2=aws)
    with pytest.raises(m.BuildProviderError):
        provider.launch({})
    assert not aws.launches


def test_qa_request_has_only_control_token_and_distinct_purpose_identity(modules):
    _, _, _, m = modules
    profile = m.QaProfile.from_dict(runtime()["qa_profile"])
    value = m.run_request(
        profile,
        run_id="run",
        qa_id="qa",
        generation=1,
        workspace_id="wid",
        venture_id="vid",
        app_id="app",
        task_token="x" * 40,
    )
    names = {
        row["name"]
        for row in value["overrides"]["containerOverrides"][0]["environment"]
    }
    assert names == {"QA_BUILD_ID", "QA_RUN_ID", "QA_GENERATION", "QA_CONTROL_TOKEN"}
    assert (
        value["networkConfiguration"]["awsvpcConfiguration"]["assignPublicIp"]
        == "DISABLED"
    )
    assert {row["key"]: row["value"] for row in value["tags"]}[
        "supymem:purpose"
    ] == "static_qa"


def test_qa_accepts_aws_normalized_empty_host_volume(modules):
    _, _, _, m = modules
    profile = m.QaProfile.from_dict(runtime()["qa_profile"])
    aws = FakeAWS(profile)
    aws.definition = qa_definition(profile)
    aws.definition["volumes"][0]["host"] = {}
    aws.definition["containerDefinitions"][0]["linuxParameters"]["capabilities"][
        "add"
    ] = []
    m.FargateQaProvider(profile, ecs=aws, ec2=aws).verify_definition()


@pytest.mark.parametrize(
    "mutate",
    [
        lambda definition: definition["volumes"].pop(),
        lambda definition: definition["volumes"].append({"name": "unexpected"}),
        lambda definition: definition["containerDefinitions"][0]["mountPoints"][1].update(containerPath="/var/tmp"),
        lambda definition: definition["volumes"][1].update(host={"sourcePath": "/tmp"}),
    ],
)
def test_qa_rejects_missing_extra_or_host_backed_temp_storage(modules, mutate):
    _, _, _, m = modules
    profile = m.QaProfile.from_dict(runtime()["qa_profile"])
    aws = FakeAWS(profile)
    aws.definition = qa_definition(profile)
    mutate(aws.definition)
    with pytest.raises(m.BuildProviderError, match="qa_task_definition_not_isolated"):
        m.FargateQaProvider(profile, ecs=aws, ec2=aws).verify_definition()


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("qa_task_definition_not_isolated", "qa_task_definition_not_isolated"),
        ("build_launch_rejected:AccessDeniedException", "qa_launch_rejected"),
        ("credentials=must-not-leak", "qa_launch_rejected"),
    ],
)
def test_qa_launch_rejection_code_only_preserves_fixed_qa_codes(modules, message, expected):
    from app.builder.qa_lifecycle import _launch_rejection_code

    assert _launch_rejection_code(Exception(message)) == expected


BINDING = {
    "endpoint": "http://10.0.1.2:8000",
    "token": "fixture-token",
    "build_id": "build",
    "qa_id": "qa",
    "generation": 1,
}


def test_qa_transport_enforces_absolute_raw_chunk_deadline(modules, monkeypatch):
    _, m, _, _ = modules

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"{"
            yield b"}"
            raise AssertionError("must stop before more bytes")

    def handle(request):
        assert request.headers["authorization"] == "Bearer fixture-token"
        assert request.headers["x-supymem-qa-id"] == "qa"
        return httpx.Response(
            200, stream=Stream(), headers={"content-type": "application/json"}
        )

    monkeypatch.setattr(
        m, "_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handle))
    )
    ticks = iter([0, 1, 15, 31])
    monkeypatch.setattr(m, "_clock", lambda: next(ticks))
    with pytest.raises(m.BuildModelError, match="limit"):
        m.state(BINDING)


@pytest.mark.parametrize("boundary", ["headers", "drip", "eof", "idle"])
def test_sdk_event_reader_checks_every_raw_boundary_and_socket_idle_timeout(
    modules, monkeypatch, boundary
):
    from types import SimpleNamespace
    from app.builder import intent_bridge as m

    data = json.dumps({"build_run_id": "run", "generation": 1, "events": []}).encode()

    class Stream(httpx.SyncByteStream):
        closed = False

        def __iter__(self):
            if boundary == "idle":
                raise httpx.ReadTimeout("fixture socket idle timeout")
            yield data[:1]
            yield data[1:]
            if boundary == "drip":
                raise AssertionError("late raw chunk must stop the reader before EOF")

        def close(self):
            self.closed = True

    stream = Stream()

    def handle(request):
        assert request.headers["accept-encoding"] == "identity"
        assert request.headers["x-supymem-build-id"] == "run"
        return httpx.Response(200, stream=stream, headers={"content-type": "application/json", "content-length": str(len(data))})

    ticks = iter({"headers": [0, 31], "drip": [0, 1, 15, 31], "eof": [0, 1, 15, 29, 31], "idle": [0, 1]}[boundary])
    # Replace only this module's clock reference; never patch the process clock.
    monkeypatch.setattr(m, "time", SimpleNamespace(monotonic=lambda: next(ticks)))
    monkeypatch.setattr(m, "_client", lambda: httpx.Client(transport=httpx.MockTransport(handle)))
    with pytest.raises(m.BuildModelError, match="read_failed|snapshot_limit"):
        m.read_events("run", 1, "http://10.0.1.2:8000", "test-token")
    assert stream.closed


@pytest.mark.parametrize(
    "fault", ["redirect", "compressed", "long", "short", "foreign"]
)
def test_qa_transfer_rejects_redirect_encoding_length_and_scope(
    modules, monkeypatch, fault
):
    _, m, _, _ = modules
    value = {
        "build_run_id": "build",
        "qa_run_id": "qa",
        "generation": 1,
        "phase": "running",
    }
    headers = {"content-type": "application/json"}
    status = 200
    if fault == "redirect":
        status = 307
    if fault == "compressed":
        headers["content-encoding"] = "gzip"
    if fault == "long":
        headers["content-length"] = "999999999"
    if fault == "short":
        headers["content-length"] = "1"
    if fault == "foreign":
        value["qa_run_id"] = "foreign"
    monkeypatch.setattr(
        m,
        "_client",
        lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    status,
                    stream=httpx.ByteStream(json.dumps(value).encode()),
                    headers=headers,
                )
            )
        ),
    )
    with pytest.raises(m.BuildModelError):
        m.state(BINDING)


def test_expectations_cover_each_product_criterion_with_typed_server_proposal(modules):
    _, _, e, _ = modules
    task = {
        "acceptance": ["Visitors can read the product offer"],
        "landing_expectations": {
            "contract": {
                "profile": "static_landing_v1",
                "product_name": "Acorn",
                "required_text": ["Reading lists"],
                "actions": [],
            },
            "criteria": [
                {
                    "criterion": 0,
                    "summary": "The offer is visible on desktop and mobile",
                    "text_indices": [0],
                    "action_ids": [],
                }
            ],
        },
    }
    visible = e.founder_view(task)
    assert "criterion" not in visible["criteria"][0] and "contract" not in visible
    task["acceptance"].append("A payment is collected")
    with pytest.raises(ValueError):
        e.validate(task)


def test_absolute_deadline_covers_a_peer_that_never_finishes_upload_response(modules, monkeypatch):
    import asyncio
    _, m, _, _ = modules
    real_timeout = asyncio.timeout
    entered = []
    async def blocked(request):
        entered.append(request.method)
        await asyncio.Future()
    def short_test_timeout(seconds):
        assert seconds == 30
        return real_timeout(0)
    monkeypatch.setattr(m.asyncio, "timeout", short_test_timeout)
    monkeypatch.setattr(m, "_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(blocked)))
    with pytest.raises(m.BuildModelError, match="qa_task_deadline"):
        m.request(BINDING, "PUT", "/v1/qa/input", body=b"fixture-source")
    assert entered == ["PUT"]


@pytest.mark.parametrize("domain", ["Warehouse procurement", "Weekend cooking"])
@pytest.mark.parametrize("entrypoint", ["run", "watch"])
@pytest.mark.parametrize("connected", [True, False])
def test_local_managed_entrypoints_use_connected_tool_without_fallback(monkeypatch, domain, entrypoint, connected):
    from fastapi import HTTPException

    with service_modules("company_os"):
        from app.builder import execution, orchestration

        monkeypatch.setenv("SUPYMEM_MODE", "local")
        payload = {"build_run_id": "build", "workspace_id": "workspace", "venture_id": "venture",
                   "_job_id": "job", "_worker_id": "worker", "_job_attempt": 2}
        spec = {"actor": "owner", "inputs": {"task": {"title": domain},
                "source": {"kind": "artifact", "id": "prior-source", "sha256": "a" * 64}},
                "instruction": "Fix the approved workflow without changing anything else."}
        selected = SimpleNamespace(trigger="founder_approved", spec=json.dumps(spec),
                                   reservation_id=None, sandbox_ref=None)
        monkeypatch.setattr(orchestration, "db", lambda: lambda: nullcontext(
            SimpleNamespace(get=lambda *_args: selected)))
        calls = []

        def local(saved):
            calls.append(deepcopy(saved))
            if not connected:
                raise HTTPException(409, detail={"message":
                    "Open Settings > Keys and connections > Coding tools, connect your tool, then retry your build."})
            return {"id": "order", "state": "approved"}

        def internal(*_args, **_kwargs):
            pytest.fail("An internal implementation or repair must not run locally")

        monkeypatch.setattr(execution, entrypoint + "_local", local)
        for name in ("_prepare", "_begin", "_launch_request", "configured_provider", "_command"):
            monkeypatch.setattr(orchestration, name, internal)
        if connected:
            assert getattr(orchestration, entrypoint)(payload) == {"id": "order", "state": "approved"}
        else:
            with pytest.raises(HTTPException) as failure:
                getattr(orchestration, entrypoint)(payload)
            assert "Settings > Keys and connections > Coding tools" in failure.value.detail["message"]
        assert calls == [payload]
        assert json.loads(selected.spec) == spec


@pytest.mark.parametrize("domain", ["Warehouse procurement", "Weekend cooking"])
@pytest.mark.parametrize("entrypoint", ["request", "launch"])
def test_local_platform_builder_cannot_admit_implementation(monkeypatch, domain, entrypoint):
    from tests.fargate_fixtures import profile_data

    with service_modules("company_os"):
        from app.infra import fargate_build

        monkeypatch.delenv("SUPYMEM_MODE", raising=False)
        profile = fargate_build.FargateProfile.from_dict(profile_data())
        aws = FakeAWS(profile)
        provider = fargate_build.FargateBuildProvider(profile, ecs=aws, ec2=aws)
        args = dict(run_id="build", generation=1, workspace_id="workspace", venture_id="venture",
                    app_id="app", conversation_id=domain, task_token="t" * 40,
                    intent_token="i" * 40, model_token="m" * 40)
        request = fargate_build.run_request(profile, **args)
        monkeypatch.setenv("SUPYMEM_MODE", "local")
        with pytest.raises(fargate_build.BuildProviderError, match="Settings > Keys and connections > Coding tools"):
            if entrypoint == "request":
                fargate_build.run_request(profile, **args)
            else:
                provider.launch(request)
        assert not aws.launches


@pytest.mark.parametrize("domain", ["Warehouse procurement", "Weekend cooking"])
@pytest.mark.parametrize("trigger", ["coding_source_import", "pagebuilder_source_qa", "subscription_product_diagnostic", "founder_approved"])
def test_local_managed_watch_preserves_hosted_artifacts_without_running_hosted_checks(monkeypatch, domain, trigger):
    import os

    import boto3

    with service_modules("company_os"):
        from app.build import subscription_product_diagnostic_controller, subscription_static_controller
        from app.builder import execution, orchestration
        from app.core.jobs import FatalError, LOCAL_MANAGED_BUILD_CODE

        monkeypatch.setenv("SUPYMEM_MODE", "local")
        for name in list(os.environ):
            if name.startswith("AWS_"):
                monkeypatch.delenv(name)
        payload = {"build_run_id": "build", "domain": domain}
        spec = {"lifecycle_version": 3, "source": {"id": "prior-source", "sha256": "a" * 64}}
        selected = SimpleNamespace(trigger=trigger, runtime_version=1, closed_at=None,
                                   reservation_id="saved-hold", sandbox_ref="saved-task", spec=json.dumps(spec))
        session = lambda: nullcontext(SimpleNamespace(get=lambda *_args: selected))
        monkeypatch.setattr(execution, "db", lambda: session)
        monkeypatch.setattr(orchestration, "db", lambda: session)

        def no_cloud(*_args, **_kwargs):
            pytest.fail("Legacy work must not run a hosted check")

        monkeypatch.setattr(boto3, "client", no_cloud)
        monkeypatch.setattr(orchestration, "_begin", no_cloud)
        for module in (subscription_product_diagnostic_controller, subscription_static_controller):
            monkeypatch.setattr(module, "watch", no_cloud)
        with pytest.raises(FatalError, match=LOCAL_MANAGED_BUILD_CODE):
            orchestration.watch(payload)
        assert json.loads(selected.spec) == spec
        assert selected.reservation_id == "saved-hold" and selected.sandbox_ref == "saved-task"


@pytest.mark.parametrize("domain", ["Warehouse procurement", "Weekend cooking"])
def test_hosted_independent_qa_can_still_launch(modules, monkeypatch, domain):
    _, _, _, qa = modules
    monkeypatch.setenv("SUPYMEM_MODE", "hosted")
    profile = qa.QaProfile.from_dict(runtime()["qa_profile"])
    aws = FakeAWS(profile)
    aws.definition = qa_definition(profile)
    provider = qa.FargateQaProvider(profile, ecs=aws, ec2=aws)
    request = qa.run_request(profile, run_id="build", qa_id=domain, generation=1,
                             workspace_id="workspace", venture_id="venture", app_id="app", task_token="t" * 40)
    assert provider.launch(request)
    assert len(aws.launches) == 1
