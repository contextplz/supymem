"""Sealed static QA task profile, reusing the attested Fargate identity adapter.

No launch is enabled by this module. The immutable reviewed quote must carry this
profile. The task receives only a QA control token, no model/cloud credentials.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
import re

from .fargate_build import (
    BuildProviderError,
    FargateProfile,
    FargateBuildProvider,
    is_empty_host_volume,
)


@dataclass(frozen=True)
class QaProfile(FargateProfile):
    container_name: str = "static-qa"
    architecture: str = "X86_64"
    browser_version: str = "153.0"

    def __post_init__(self):
        # Reuse the exact account/region/immutable-image/private-network policy.
        FargateProfile(**{key: getattr(self, key) for key in FargateProfile.__dataclass_fields__ if key != "container_name"})
        if self.container_name != "static-qa" or self.architecture not in {"X86_64", "ARM64"} or self.browser_version != "153.0":
            raise BuildProviderError("qa_profile_invalid")
        if len(self.subnet_ids) > 3 or len(self.security_group_ids) > 3:
            raise BuildProviderError("qa_profile_invalid")

    def receipt_policy(self):
        return {"image_digest": self.image_uri.split("@")[1], "browser_version": self.browser_version,
                **{key: list(getattr(self, key)) if key.endswith("_ids") else getattr(self, key)
                   for key in ("cluster_arn", "task_definition_arn", "subnet_ids", "security_group_ids")}}


def run_request(profile, *, run_id, qa_id, generation, workspace_id, venture_id, app_id, task_token):
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", task_token):
        raise BuildProviderError("qa_task_token_invalid")
    token = hashlib.sha256(f"{run_id}:{generation}:{qa_id}:static-qa1".encode()).hexdigest()
    env = {"QA_BUILD_ID": run_id, "QA_RUN_ID": qa_id, "QA_GENERATION": str(generation), "QA_CONTROL_TOKEN": task_token}
    tags = {"supymem:runtime": "b5", "supymem:run": run_id, "supymem:qa": qa_id, "supymem:purpose": "static_qa",
            "supymem:generation": str(generation), "supymem:workspace": workspace_id,
            "supymem:venture": venture_id, "supymem:app": app_id, "supymem:profile": profile.digest}
    return {"cluster": profile.cluster_arn, "taskDefinition": profile.task_definition_arn, "launchType": "FARGATE",
            "platformVersion": "1.4.0", "count": 1, "clientToken": token, "startedBy": "supymem-" + token[:32],
            "enableExecuteCommand": False, "networkConfiguration": {"awsvpcConfiguration": {
                "subnets": list(profile.subnet_ids), "securityGroups": list(profile.security_group_ids), "assignPublicIp": "DISABLED"}},
            "overrides": {"containerOverrides": [{"name": profile.container_name,
                "environment": [{"name": key, "value": env[key]} for key in sorted(env)]}]},
            "tags": [{"key": key, "value": tags[key]} for key in sorted(tags)]}


class FargateQaProvider(FargateBuildProvider):
    def verify_definition(self):
        p = self.profile
        d = self.ecs.describe_task_definition(taskDefinition=p.task_definition_arn)["taskDefinition"]
        containers = d.get("containerDefinitions", [])
        if (d.get("taskDefinitionArn") != p.task_definition_arn or d.get("taskRoleArn")
                or d.get("executionRoleArn") != p.execution_role_arn or d.get("networkMode") != "awsvpc"
                or "FARGATE" not in d.get("requiresCompatibilities", []) or (d.get("cpu"), d.get("memory")) != ("2048", "4096")
                or d.get("runtimePlatform") != {"operatingSystemFamily": "LINUX", "cpuArchitecture": p.architecture}
                or d.get("ephemeralStorage", {}).get("sizeInGiB", 20) != 20
                or len(containers) != 1):
            raise BuildProviderError("qa_task_definition_not_isolated")
        c = containers[0]
        linux = c.get("linuxParameters", {})
        capabilities = linux.get("capabilities", {})
        expected_mounts = {
            ("state", "/state", False),
            ("tmp", "/tmp", False),
        }
        mounts = {
            (row.get("sourceVolume"), row.get("containerPath"), row.get("readOnly", False))
            for row in c.get("mountPoints", [])
        }
        volumes = d.get("volumes", [])
        if (c.get("name") != p.container_name or c.get("image") != p.image_uri or c.get("user") != "10001:10001"
                or c.get("privileged", False) or c.get("readonlyRootFilesystem") is not True
                or set(linux) != {"capabilities", "initProcessEnabled"}
                or linux.get("initProcessEnabled") is not True
                or set(capabilities).difference({"add", "drop"})
                or capabilities.get("drop") != ["ALL"]
                or capabilities.get("add", [])
                or c.get("entryPoint") != ["python", "-m", "app.static_qa_api"] or c.get("command")
                or c.get("workingDirectory") != "/opt/supymem" or c.get("secrets") or c.get("environmentFiles")
                or c.get("logConfiguration") or c.get("dockerSecurityOptions") or c.get("systemControls")
                or mounts != expected_mounts
                or len(volumes) != 2
                or not all(any(is_empty_host_volume(volume, name=name) for volume in volumes)
                           for name in {"state", "tmp"})):
            raise BuildProviderError("qa_task_definition_not_isolated")
        allowed = {"HOME": "/state/home", "TMPDIR": "/state/tmp", "PYTHONPATH": "/opt/supymem", "PYTHONUNBUFFERED": "1",
                   "PYTHONDONTWRITEBYTECODE": "1", "PLAYWRIGHT_BROWSERS_PATH": "/ms-playwright"}
        if any(allowed.get(row.get("name")) != row.get("value") for row in c.get("environment", [])):
            raise BuildProviderError("qa_task_definition_ambient_credentials")


def configured_provider(expected_profile):
    try:
        primary = json.loads(os.environ["BUILD_QA_FARGATE_PROFILE"])
        raw_compat = json.loads(os.environ.get("BUILD_QA_FARGATE_COMPAT_PROFILES", "[]"))
        if (not isinstance(primary, dict) or not isinstance(raw_compat, list)
                or len(raw_compat) > 3 or not all(isinstance(row, dict) for row in raw_compat)):
            raise ValueError("qa_runtime_compat_profiles_invalid")
        # Select the exact immutable profile quoted for this run before applying
        # today's validation contract.  A retained compatibility record can
        # become invalid after the runtime contract is tightened; that stale,
        # unrelated record must not disable the current primary runtime.  The
        # selected record is still fully validated below and therefore fails
        # closed when an old run actually asks to use it.
        selected = next((row for row in [primary, *raw_compat] if row == expected_profile), None)
        profile = QaProfile.from_dict(selected) if selected is not None else None
    except (KeyError, TypeError, ValueError, BuildProviderError) as exc:
        raise BuildProviderError("qa_runtime_not_configured") from exc
    if profile is None:
        raise BuildProviderError("qa_runtime_profile_changed")
    import boto3
    from botocore.config import Config
    options = Config(region_name=profile.region, connect_timeout=3, read_timeout=20,
                     retries={"total_max_attempts": 1}, ignore_configured_endpoint_urls=True, proxies={})
    return FargateQaProvider(profile, ecs=boto3.client("ecs", config=options), ec2=boto3.client("ec2", config=options))
