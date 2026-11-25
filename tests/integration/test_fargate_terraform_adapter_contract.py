"""Evaluate the enabled build-task Terraform module and enforce Core's launch profile.

The provider is configured with inert fixture credentials and refresh is disabled;
this test never contacts AWS or creates a resource.  It prevents a static HCL
change from drifting away from ``FargateBuildProvider.verify_definition``.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.fargate_fixtures import profile_data
from tests.integration.test_postgres_auth_security import service_modules

ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / "infra/terraform/modules/ecs-build-run"
IMAGE = "000000000000.dkr.ecr.us-east-1.amazonaws.com/selfhost-os/agent@sha256:" + "a" * 64


def _resource(module: dict, resource_type: str) -> dict:
    for row in module.get("resources", []):
        if row.get("type") == resource_type:
            return row
    for child in module.get("child_modules", []):
        found = _resource(child, resource_type)
        if found:
            return found
    return {}


@pytest.mark.skipif(shutil.which("terraform") is None, reason="terraform is required for the IaC contract")
def test_enabled_terraform_task_definition_satisfies_runtime_adapter(tmp_path: Path):
    (tmp_path / "main.tf").write_text(
        f'''terraform {{
  required_providers {{ aws = {{ source = "hashicorp/aws", version = "~> 6.0" }} }}
}}
provider "aws" {{
  region = "us-east-1"
  access_key = "testing"
  secret_key = "testing"
  skip_credentials_validation = true
  skip_requesting_account_id = true
  skip_metadata_api_check = true
}}
module "build" {{
  source = "{MODULE}"
  enabled = true
  name = "fixture-build"
  env = "fixture"
  image = "{IMAGE}"
  execution_role_arn = "arn:aws:iam::000000000000:role/isolated-build-execution"
  cpu = 2048
  memory = 4096
}}
'''
    )
    env = {**os.environ, "AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing", "AWS_EC2_METADATA_DISABLED": "true"}
    run = lambda *args: subprocess.run(args, cwd=tmp_path, env=env, check=True, capture_output=True, text=True)
    run("terraform", "init", "-backend=false", "-input=false")
    plan_path = tmp_path / "plan.bin"
    run("terraform", "plan", "-refresh=false", "-input=false", f"-out={plan_path}")
    plan = json.loads(run("terraform", "show", "-json", str(plan_path)).stdout)
    task = _resource(plan["planned_values"]["root_module"], "aws_ecs_task_definition")["values"]
    container = json.loads(task["container_definitions"])[0]
    assert task.get("ephemeral_storage") == []  # provider's validated default is 20 GiB
    assert container["workingDirectory"] == "/opt/supymem"

    profile_payload = profile_data()
    profile_payload["image_uri"] = IMAGE
    with service_modules("company_os"):
        from app.infra import fargate_build as module

        profile = module.FargateProfile.from_dict(profile_payload)
        evaluated = {
            "taskDefinitionArn": profile.task_definition_arn,
            "executionRoleArn": task["execution_role_arn"],
            "networkMode": task["network_mode"],
            "requiresCompatibilities": task["requires_compatibilities"],
            "cpu": task["cpu"],
            "memory": task["memory"],
            "runtimePlatform": {
                "operatingSystemFamily": task["runtime_platform"][0]["operating_system_family"],
                "cpuArchitecture": task["runtime_platform"][0]["cpu_architecture"],
            },
            "volumes": [{"name": volume["name"]} for volume in task["volume"]],
            "containerDefinitions": [container],
        }

        class ECS:
            def describe_task_definition(self, **_kwargs):
                return {"taskDefinition": evaluated}

        module.FargateBuildProvider(profile, ecs=ECS(), ec2=None).verify_definition()
