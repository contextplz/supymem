import pytest

from tests.fargate_fixtures import FakeAWS, profile_data
from tests.integration.test_postgres_auth_security import service_modules


@pytest.fixture()
def adapter(monkeypatch):
    monkeypatch.setenv("SUPYMEM_MODE", "hosted")
    with service_modules("company_os"):
        from app.infra import fargate_build as m

        profile = m.FargateProfile.from_dict(profile_data())
        aws = FakeAWS(profile)
        provider = m.FargateBuildProvider(profile, ecs=aws, ec2=aws)
        request = m.run_request(
            profile,
            run_id="bld_fixture",
            generation=1,
            workspace_id="workspace_a",
            venture_id="venture_a",
            app_id="app_a",
            conversation_id="00000000-0000-0000-0000-000000000001",
            task_token="task-" + "x" * 32,
            intent_token="intent-" + "x" * 32,
            model_token="model-" + "x" * 32,
        )
        yield m, provider, aws, request


def test_role_free_exact_single_task_request_and_observed_eni(adapter):
    m, provider, aws, request = adapter
    assert len(request["clientToken"]) == 64 and request["count"] == 1
    assert (
        request["networkConfiguration"]["awsvpcConfiguration"]["assignPublicIp"]
        == "DISABLED"
    )
    assert request["enableExecuteCommand"] is False
    assert set(request["overrides"]) == {"containerOverrides"}
    names = {
        row["name"]
        for row in request["overrides"]["containerOverrides"][0]["environment"]
    }
    assert (
        len(names) == 8
        and not {"AWS_ACCESS_KEY_ID", "BUILD_CONTROLLER_TOKEN", "COMPANY_OS_DB_URL"}
        & names
    )
    arn = provider.launch(request)
    assert provider.observe(arn, request)["endpoint"] == "http://10.91.0.11:8000"
    assert provider.launch(request) == arn and len(aws.tasks) == 1


def test_aws_normalized_empty_host_volumes_are_still_scratch_only(adapter):
    _m, provider, aws, _request = adapter
    for volume in aws.definition["volumes"]:
        volume["host"] = {}
    provider.verify_definition()


@pytest.mark.parametrize(
    "fault",
    [
        "role",
        "root",
        "write_root",
        "sudo_cap",
        "secret",
        "host_mount",
        "mutable",
        "ambient",
        "legacy_entry",
    ],
)
def test_task_definition_policy_fails_before_aws_mutation(adapter, fault):
    m, provider, aws, request = adapter
    container = aws.definition["containerDefinitions"][0]
    if fault == "role":
        aws.definition["taskRoleArn"] = "arn:aws:iam::000000000000:role/master"
    if fault == "root":
        container["user"] = "0"
    if fault == "write_root":
        container["readonlyRootFilesystem"] = False
    if fault == "sudo_cap":
        container["linuxParameters"]["capabilities"]["add"] = ["SYS_ADMIN"]
    if fault == "secret":
        container["secrets"] = [{"name": "KEY", "valueFrom": "secret"}]
    if fault == "host_mount":
        aws.definition["volumes"][0]["host"] = {"sourcePath": "/"}
    if fault == "mutable":
        container["image"] = "builder:latest"
    if fault == "ambient":
        container["environment"] = [{"name": "AZURE_API_KEY", "value": "fixture"}]
    if fault == "legacy_entry":
        container["entryPoint"] = ["python", "-m", "app.openhands_runtime"]
    with pytest.raises(m.BuildProviderError):
        provider.launch(request)
    assert not aws.launches


def test_foreign_task_denial_and_policy_violation_still_allows_owned_stop(adapter):
    m, provider, aws, request = adapter
    arn = provider.launch(request)
    aws.eni["Association"] = {"PublicIp": "203.0.113.1"}
    with pytest.raises(m.BuildProviderError, match="network_unverified"):
        provider.observe(arn, request)
    provider.stop(arn, request)
    assert aws.stops == [arn]
    aws.tasks[arn]["tags"][0]["value"] = "foreign"
    with pytest.raises(m.BuildProviderError, match="identity_unverified"):
        provider.stop(arn, request)
    assert aws.stops == [arn]


def test_ambiguous_launch_can_replay_only_identical_aws_token_body(adapter):
    m, provider, aws, request = adapter
    aws.lose_launch_response = True
    with pytest.raises(TimeoutError):
        provider.launch(request)
    arn = provider.launch(request)
    assert len(aws.tasks) == 1 and provider.observe(arn, request)["state"] == "RUNNING"


def test_definition_read_failure_is_definite_zero_launch(adapter):
    m, provider, aws, request = adapter

    def denied(**_kwargs):
        raise PermissionError("fixture describe denied")

    aws.describe_task_definition = denied
    with pytest.raises(m.LaunchRejected, match="preflight_rejected"):
        provider.launch(request)
    assert not aws.launches


def test_explicit_ecs_access_denial_is_definite_zero_launch(adapter):
    from botocore.exceptions import ClientError

    m, provider, aws, request = adapter

    def denied(**_kwargs):
        aws.launches.append(_kwargs)
        raise ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "denied"}},
            "RunTask",
        )

    aws.run_task = denied
    with pytest.raises(m.LaunchRejected, match="AccessDeniedException"):
        provider.launch(request)
    assert len(aws.launches) == 1


def test_run_task_transport_failure_remains_ambiguous(adapter):
    m, provider, aws, request = adapter

    def response_lost(**_kwargs):
        aws.launches.append(_kwargs)
        raise TimeoutError("response lost")

    aws.run_task = response_lost
    with pytest.raises(TimeoutError, match="response lost"):
        provider.launch(request)
    assert len(aws.launches) == 1


def test_configured_sdk_disables_ambient_endpoints_and_proxies(adapter, monkeypatch):
    import json
    import boto3
    m, provider, aws, request = adapter
    monkeypatch.setenv("BUILD_FARGATE_PROFILE", json.dumps(provider.profile.snapshot()))
    monkeypatch.setenv("AWS_ENDPOINT_URL", "https://untrusted.fixture.invalid")
    calls = []
    def client(name, config):
        calls.append((name, config))
        return aws
    monkeypatch.setattr(boto3, "client", client)
    configured = m.configured_provider(provider.profile.snapshot())
    assert configured.profile == provider.profile and len(calls) == 2
    for _, config in calls:
        assert config.ignore_configured_endpoint_urls is True and config.proxies == {}
        assert config.retries["total_max_attempts"] == 1


def test_configured_sdk_accepts_an_explicit_bounded_compatibility_profile(adapter, monkeypatch):
    import json
    import boto3
    m, provider, aws, request = adapter
    previous = provider.profile.snapshot()
    current = {**previous, "task_definition_arn": previous["task_definition_arn"].rsplit(":", 1)[0] + ":2",
               "image_uri": previous["image_uri"].split("@", 1)[0] + "@sha256:" + "b" * 64}
    monkeypatch.setenv("BUILD_FARGATE_PROFILE", json.dumps(current))
    monkeypatch.setenv("BUILD_FARGATE_COMPAT_PROFILES", json.dumps([previous]))
    monkeypatch.setattr(boto3, "client", lambda name, config: aws)
    configured = m.configured_provider(previous)
    assert configured.profile.snapshot() == previous


def test_configured_sdk_rejects_unlisted_or_unbounded_compatibility_profiles(adapter, monkeypatch):
    import json
    m, provider, aws, request = adapter
    current = provider.profile.snapshot()
    unexpected = {**current, "task_definition_arn": current["task_definition_arn"].rsplit(":", 1)[0] + ":9"}
    monkeypatch.setenv("BUILD_FARGATE_PROFILE", json.dumps(current))
    with pytest.raises(m.BuildProviderError, match="profile_changed"):
        m.configured_provider(unexpected)
    monkeypatch.setenv("BUILD_FARGATE_COMPAT_PROFILES", json.dumps([current] * 4))
    with pytest.raises(m.BuildProviderError, match="not_configured"):
        m.configured_provider(current)


def test_mutable_pythonpath_cannot_shadow_controller_code(adapter):
    m, provider, aws, request = adapter
    aws.definition["containerDefinitions"][0]["environment"] = [{"name": "PYTHONPATH", "value": "/workspace"}]
    with pytest.raises(m.BuildProviderError, match="ambient"):
        provider.launch(request)
    assert not aws.launches
