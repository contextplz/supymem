"""Strict role-free Fargate adapter; all callers must persist intent before I/O.

No AWS client is constructed at import time. Tests inject SDK-shaped clients;
the configured factory remains unreachable from a paid quote until the reviewed
runtime/pricing/QA gate is completed. No local/Docker/fake provider fallback.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import os
import re
from urllib.parse import urlsplit


LOCAL_BUILD_NEXT_STEP = "Open Settings > Keys and connections > Coding tools, connect your coding tool, then retry your build in chat."


class BuildProviderError(RuntimeError):
    pass


class LaunchRejected(BuildProviderError):
    """A definite zero-task rejection, not an ambiguous transport outcome."""


_DEFINITE_RUN_TASK_REJECTIONS = {
    "AccessDenied",
    "AccessDeniedException",
    "ClientException",
    "ClusterNotFoundException",
    "InvalidParameterException",
    "UnsupportedFeatureException",
}


def _run_task_rejection_code(exc):
    """Return a service rejection that proves ECS created no task.

    Transport errors remain ambiguous because ECS may have accepted the
    idempotent request before the response was lost. An explicit ECS 4xx
    response is different: the service rejected the request and CloudTrail
    records no task creation, so the controller can release the reservation
    instead of entering an endless discovery-only loop.
    """
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return None
    error = response.get("Error")
    if not isinstance(error, dict):
        return None
    code = error.get("Code")
    return code if code in _DEFINITE_RUN_TASK_REJECTIONS else None


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def is_empty_host_volume(value, *, name):
    """Recognize the DescribeTaskDefinition form of Fargate scratch storage."""
    return (
        isinstance(value, dict)
        and value.get("name") == name
        and set(value).issubset({"name", "host"})
        and value.get("host", {}) == {}
    )


@dataclass(frozen=True)
class FargateProfile:
    region: str
    cluster_arn: str
    task_definition_arn: str
    execution_role_arn: str
    image_uri: str
    subnet_ids: tuple[str, ...]
    security_group_ids: tuple[str, ...]
    vpc_id: str
    core_origin: str
    container_name: str = "managed-builder"

    def __post_init__(self):
        cluster = re.fullmatch(
            r"arn:aws:ecs:([a-z0-9-]+):(\d{12}):cluster/([A-Za-z0-9_-]+)",
            self.cluster_arn,
        )
        if not cluster or cluster[1] != self.region:
            raise BuildProviderError("build_profile_cluster_invalid")
        account = cluster[2]
        if not re.fullmatch(
            rf"arn:aws:ecs:{re.escape(self.region)}:{account}:task-definition/[A-Za-z0-9_-]+:[1-9][0-9]*",
            self.task_definition_arn,
        ):
            raise BuildProviderError("build_profile_task_definition_invalid")
        if not re.fullmatch(
            rf"arn:aws:iam::{account}:role/[A-Za-z0-9_+=,.@/-]+",
            self.execution_role_arn,
        ):
            raise BuildProviderError("build_profile_execution_role_invalid")
        if not re.fullmatch(
            rf"{account}\.dkr\.ecr\.{re.escape(self.region)}\.amazonaws\.com/[a-z0-9_./-]+@sha256:[a-f0-9]{{64}}",
            self.image_uri,
        ):
            raise BuildProviderError("build_profile_image_not_immutable")
        for values, prefix in (
            (self.subnet_ids, "subnet"),
            (self.security_group_ids, "sg"),
        ):
            if (
                not 1 <= len(values) <= 5
                or len(set(values)) != len(values)
                or any(
                    not re.fullmatch(prefix + r"-[a-f0-9]{8,17}", value)
                    for value in values
                )
            ):
                raise BuildProviderError("build_profile_network_invalid")
        if (
            not re.fullmatch(r"vpc-[a-f0-9]{8,17}", self.vpc_id)
            or self.container_name != "managed-builder"
        ):
            raise BuildProviderError("build_profile_network_invalid")
        url = urlsplit(self.core_origin)
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username
            or url.password
            or url.path not in {"", "/"}
            or url.query
            or url.fragment
            or url.port not in {None, 443}
        ):
            raise BuildProviderError("build_profile_core_tls_required")

    @classmethod
    def from_dict(cls, value):
        try:
            data = dict(value)
            data["subnet_ids"], data["security_group_ids"] = (
                tuple(data["subnet_ids"]),
                tuple(data["security_group_ids"]),
            )
            return cls(**data)
        except (TypeError, ValueError, KeyError) as exc:
            raise BuildProviderError("build_profile_invalid") from exc

    def snapshot(self):
        return json.loads(canonical(asdict(self)))

    @property
    def digest(self):
        return hashlib.sha256(canonical(self.snapshot()).encode()).hexdigest()


def run_request(
    profile,
    *,
    run_id,
    generation,
    workspace_id,
    venture_id,
    app_id,
    conversation_id,
    task_token,
    intent_token,
    model_token,
):
    from app.core.local_mode import is_local

    if is_local():
        raise BuildProviderError(LOCAL_BUILD_NEXT_STEP)
    if (
        min(len(task_token), len(intent_token), len(model_token)) < 32
        or len({task_token, intent_token, model_token}) != 3
    ):
        raise BuildProviderError("build_task_credentials_invalid")
    token = hashlib.sha256(f"{run_id}:{generation}:fargate1".encode()).hexdigest()
    env = {
        "MANAGED_BUILD_ID": run_id,
        "MANAGED_GENERATION": str(generation),
        "MANAGED_CONVERSATION_ID": conversation_id,
        "MANAGED_TASK_CONTROL_TOKEN": task_token,
        "MANAGED_BRIDGE_TOKEN": intent_token,
        "MANAGED_MODEL_TOKEN": model_token,
        "MANAGED_INTENT_URL": profile.core_origin.rstrip("/")
        + f"/internal/builds/{run_id}/intent",
        "MANAGED_MODEL_URL": profile.core_origin.rstrip("/")
        + f"/internal/builds/{run_id}/v1/chat/completions",
    }
    tags = {
        "supymem:runtime": "b5",
        "supymem:purpose": "managed_builder",
        "supymem:run": run_id,
        "supymem:generation": str(generation),
        "supymem:workspace": workspace_id,
        "supymem:venture": venture_id,
        "supymem:app": app_id,
        "supymem:profile": profile.digest,
    }
    return {
        "cluster": profile.cluster_arn,
        "taskDefinition": profile.task_definition_arn,
        "launchType": "FARGATE",
        "platformVersion": "1.4.0",
        "count": 1,
        "clientToken": token,
        "startedBy": "supymem-" + token[:32],
        "enableExecuteCommand": False,
        "networkConfiguration": {
            "awsvpcConfiguration": {
                "subnets": list(profile.subnet_ids),
                "securityGroups": list(profile.security_group_ids),
                "assignPublicIp": "DISABLED",
            }
        },
        "overrides": {
            "containerOverrides": [
                {
                    "name": profile.container_name,
                    "environment": [
                        {"name": key, "value": env[key]} for key in sorted(env)
                    ],
                }
            ]
        },
        "tags": [{"key": key, "value": tags[key]} for key in sorted(tags)],
    }


class FargateBuildProvider:
    def __init__(self, profile, *, ecs, ec2):
        self.profile, self.ecs, self.ec2 = profile, ecs, ec2

    def verify_definition(self):
        p = self.profile
        data = self.ecs.describe_task_definition(taskDefinition=p.task_definition_arn)[
            "taskDefinition"
        ]
        containers = data.get("containerDefinitions", [])
        if (
            data.get("taskDefinitionArn") != p.task_definition_arn
            or data.get("taskRoleArn")
            or data.get("executionRoleArn") != p.execution_role_arn
            or data.get("networkMode") != "awsvpc"
            or "FARGATE" not in data.get("requiresCompatibilities", [])
            or (data.get("cpu"), data.get("memory")) != ("2048", "4096")
            or data.get("runtimePlatform", {}).get("operatingSystemFamily") != "LINUX"
            or data.get("runtimePlatform", {}).get("cpuArchitecture") != "X86_64"
            or data.get("ephemeralStorage", {}).get("sizeInGiB", 20) != 20
            or len(containers) != 1
        ):
            raise BuildProviderError("build_task_definition_not_isolated")
        c = containers[0]
        linux = c.get("linuxParameters", {})
        expected_mounts = {
            ("workspace", "/workspace", False),
            ("state", "/state", False),
            ("tmp", "/tmp", False),
        }
        mounts = {
            (m.get("sourceVolume"), m.get("containerPath"), m.get("readOnly", False))
            for m in c.get("mountPoints", [])
        }
        volumes = data.get("volumes", [])
        if (
            c.get("name") != p.container_name
            or c.get("image") != p.image_uri
            or c.get("user") != "10001:10001"
            or c.get("privileged", False)
            or c.get("readonlyRootFilesystem") is not True
            or linux.get("capabilities", {}).get("drop") != ["ALL"]
            or linux.get("capabilities", {}).get("add")
            or linux.get("initProcessEnabled") is not True
            or c.get("secrets")
            or c.get("environmentFiles")
            or c.get("entryPoint") != ["python", "-m", "app.managed_api"]
            or c.get("command")
            or c.get("workingDirectory") != "/opt/supymem"
            or mounts != expected_mounts
            or len(volumes) != 3
            or not all(
                any(is_empty_host_volume(volume, name=name) for volume in volumes)
                for name in {"workspace", "state", "tmp"}
            )
        ):
            raise BuildProviderError("build_task_definition_not_isolated")
        allowed_environment = {
            "HOME": "/workspace",
            "TMPDIR": "/workspace/.tmp",
            "TMUX_TMPDIR": "/workspace/.tmux",
            "PYTHONPATH": "/opt/supymem",
            "PYTHONUNBUFFERED": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PORT": "8000",
        }
        if any(
            allowed_environment.get(row.get("name")) != row.get("value")
            for row in c.get("environment", [])
        ):
            raise BuildProviderError("build_task_definition_ambient_credentials")

    def verify_no_task_logs(self):
        """V2 included-overhead profile never forwards untrusted stdout/stderr."""
        data = self.ecs.describe_task_definition(taskDefinition=self.profile.task_definition_arn)["taskDefinition"]
        if data.get("taskDefinitionArn") != self.profile.task_definition_arn or any(c.get("logConfiguration") for c in data.get("containerDefinitions", [])):
            raise LaunchRejected("build_task_logging_policy_unverified")

    def launch(self, request):
        from app.core.local_mode import is_local

        if is_local() and self.profile.container_name == "managed-builder":
            raise LaunchRejected(LOCAL_BUILD_NEXT_STEP)
        # Every failure before ``run_task`` is a definitive zero-launch
        # outcome.  Preserve that distinction so the controller can release
        # the hold instead of treating a failed read-only preflight as an
        # ambiguous cloud mutation.
        try:
            self.verify_definition()  # Read-only inspection before mutation.
        except BuildProviderError as exc:
            raise LaunchRejected(str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 -- SDK errors are zero-launch too
            raise LaunchRejected("build_task_definition_preflight_rejected") from exc
        try:
            result = self.ecs.run_task(**request)
        except Exception as exc:  # noqa: BLE001 -- distinguish service rejection from transport loss
            code = _run_task_rejection_code(exc)
            if code:
                raise LaunchRejected("build_launch_rejected:" + code) from exc
            raise
        tasks = result.get("tasks", [])
        if not tasks and result.get("failures"):
            raise LaunchRejected("build_launch_rejected")
        if len(tasks) != 1 or not tasks[0].get("taskArn"):
            raise BuildProviderError("build_launch_response_uncertain")
        return tasks[0]["taskArn"]

    def observe(self, task_arn, request, *, identity_only=False):
        result = self.ecs.describe_tasks(
            cluster=self.profile.cluster_arn, tasks=[task_arn], include=["TAGS"]
        )
        rows = result.get("tasks", [])
        if len(rows) != 1:
            raise BuildProviderError("build_task_observation_unavailable")
        row = rows[0]
        tags = {entry["key"]: entry["value"] for entry in row.get("tags", [])}
        expected = {entry["key"]: entry["value"] for entry in request["tags"]}
        if (
            row.get("taskArn") != task_arn
            or row.get("clusterArn") != self.profile.cluster_arn
            or row.get("taskDefinitionArn") != self.profile.task_definition_arn
            or row.get("launchType") != "FARGATE"
            or row.get("startedBy") != request["startedBy"]
            or any(tags.get(key) != value for key, value in expected.items())
        ):
            raise BuildProviderError("build_task_identity_unverified")
        state = row.get("lastStatus")
        result = {
            "task_arn": task_arn,
            "state": state,
            "endpoint": None,
            "pull_started_at": _timestamp(row.get("pullStartedAt")),
            "stopped_at": _timestamp(row.get("stoppedAt")),
        }
        if state == "STOPPED":
            result["stop_code"] = row.get("stopCode")
            return result  # A removed ENI is normal after an attested task stops.
        if state != "RUNNING" or identity_only:
            return result
        containers = row.get("containers", [])
        if (
            len(containers) != 1
            or containers[0].get("name") != self.profile.container_name
            or containers[0].get("imageDigest") != self.profile.image_uri.split("@")[1]
        ):
            raise BuildProviderError("build_task_image_unverified")
        attachments = [
            a
            for a in row.get("attachments", [])
            if a.get("type") == "ElasticNetworkInterface"
        ]
        if len(attachments) != 1 or attachments[0].get("status") != "ATTACHED":
            raise BuildProviderError("build_task_network_unverified")
        details = {d["name"]: d["value"] for d in attachments[0].get("details", [])}
        eni_id = details.get("networkInterfaceId")
        if not eni_id:
            raise BuildProviderError("build_task_network_unverified")
        interfaces = self.ec2.describe_network_interfaces(
            NetworkInterfaceIds=[eni_id]
        ).get("NetworkInterfaces", [])
        if len(interfaces) != 1:
            raise BuildProviderError("build_task_network_unverified")
        eni = interfaces[0]
        if (
            eni.get("NetworkInterfaceId") != eni_id
            or eni.get("VpcId") != self.profile.vpc_id
            or eni.get("SubnetId") not in self.profile.subnet_ids
            or eni.get("Status") != "in-use"
            or {g["GroupId"] for g in eni.get("Groups", [])}
            != set(self.profile.security_group_ids)
            or eni.get("Association", {}).get("PublicIp")
            or eni.get("PrivateIpAddress") != details.get("privateIPv4Address")
        ):
            raise BuildProviderError("build_task_network_unverified")
        from app.builder.intent_bridge import _endpoint

        result["endpoint"] = _endpoint(f"http://{eni['PrivateIpAddress']}:8000")
        from datetime import datetime, timezone
        result["task_binding"] = {"task_arn": task_arn, "cluster_arn": self.profile.cluster_arn,
            "task_definition_arn": self.profile.task_definition_arn, "image_digest": containers[0]["imageDigest"],
            "eni_id": eni_id, "private_ip": eni["PrivateIpAddress"], "subnet_id": eni["SubnetId"],
            "security_group_ids": sorted(g["GroupId"] for g in eni["Groups"]),
            "observed_at": datetime.now(timezone.utc).isoformat()}
        return result

    def stop(self, task_arn, request):
        # A task whose image/network later violates policy must still be stopped
        # once its immutable run/cluster/task/tag identity has been established.
        observed = self.observe(task_arn, request, identity_only=True)
        if observed["state"] != "STOPPED":
            self.ecs.stop_task(
                cluster=self.profile.cluster_arn,
                task=task_arn,
                reason="Supymem reviewed build cleanup",
            )
        return observed

    def discover(self, request):
        """Read-only recovery after a lost response; absence never means free.

        ECS startedBy is scoped to a cluster. Inspect returned tasks (recently stopped
        tasks may appear), then independently verify the immutable run tags. A short
        bounded result cannot prove an older task never existed.
        """
        candidates = set()
        token = None
        for _ in range(5):
            # AWS permits startedBy as the sole filter (apart from the cluster
            # routing parameter); combining desiredStatus is invalid.
            args = {"cluster": self.profile.cluster_arn, "startedBy": request["startedBy"], "maxResults": 100}
            if token:
                args["nextToken"] = token
            page = self.ecs.list_tasks(**args)
            candidates.update(page.get("taskArns", []))
            token = page.get("nextToken")
            if not token:
                break
        if token:
            raise BuildProviderError("build_task_discovery_limit")
        verified = []
        for arn in candidates:
            self.observe(arn, request, identity_only=True)
            verified.append(arn)
        if len(verified) > 1:
            raise BuildProviderError("build_task_identity_ambiguous")
        return verified[0] if verified else None


def _timestamp(value):
    if value is None:
        return None
    from core.clock import as_utc

    return as_utc(value).isoformat()


def configured_provider(expected_profile):
    try:
        primary = FargateProfile.from_dict(json.loads(os.environ["BUILD_FARGATE_PROFILE"]))
        raw_compat = json.loads(os.environ.get("BUILD_FARGATE_COMPAT_PROFILES", "[]"))
        if not isinstance(raw_compat, list) or len(raw_compat) > 3:
            raise ValueError("build_runtime_compat_profiles_invalid")
        profiles = [primary, *(FargateProfile.from_dict(row) for row in raw_compat)]
        configured = next((row for row in profiles if row.snapshot() == expected_profile), None)
    except (KeyError, TypeError, ValueError, BuildProviderError) as exc:
        raise BuildProviderError("build_runtime_not_configured") from exc
    if configured is None:
        raise BuildProviderError("build_runtime_profile_changed")
    import boto3
    from botocore.config import Config

    options = Config(
        region_name=configured.region,
        connect_timeout=3,
        read_timeout=20,
        retries={"total_max_attempts": 1},
        ignore_configured_endpoint_urls=True,
        proxies={},
    )
    return FargateBuildProvider(
        configured,
        ecs=boto3.client("ecs", config=options),
        ec2=boto3.client("ec2", config=options),
    )
