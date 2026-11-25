"""Explicit test-only tariffs and runtime acceptance; never a live quote."""
from datetime import datetime, timezone, timedelta
from tests.fargate_fixtures import profile_data, task_definition


def runtime():
    from app.builder.lifecycle import INCLUDED
    from app.builder.budgets import model_pricing
    from app.builder.model_proxy import digest
    from app.infra.fargate_qa import QaProfile
    builder = profile_data()
    qa = QaProfile.from_dict({**builder, "container_name": "static-qa",
        "task_definition_arn": builder["task_definition_arn"].replace("managed-fixture", "qa-fixture")}).snapshot()
    now = datetime.now(timezone.utc)
    def rate(profile):
        return {"policy_id": "isolated_test_only", "micro_per_second": "1", "minimum_seconds": 60, "currency": "USD", "unit": "task_second",
            "profile_digest": digest(profile), "region": profile["region"], "cpu": 2048, "memory_mb": 4096, "architecture": "X86_64",
            "source": "https://fixture.invalid/not-a-live-rate", "observed_at": (now-timedelta(days=1)).isoformat(), "expires_at": (now+timedelta(days=1)).isoformat()}
    return {"lifecycle_version": 2, "ready": True, "reasons": [], "ceiling_micro": 10000, "model_ceiling_micro": 8000,
        "compute_ceiling_micro": 2000, "builder_compute_ceiling_micro": 1000, "qa_compute_ceiling_micro": 1000,
        "artifact_ceiling_micro": 0, "included_overhead": INCLUDED.copy(), "model": "DeepSeek-V4-Pro",
        "model_config": {"model": "DeepSeek-V4-Pro", "provider": "azure_foundry", "source": "platform_default", "pricing": model_pricing("DeepSeek-V4-Pro")},
        "runtime_policy": {"profile": "fargate_openhands_v1", "timeout_seconds": 1800, "max_steps": 40,
                           "tool_names": ["terminal", "file_editor", "finish", "think"]},
        "fargate_profile": builder, "builder_profile": builder, "qa_profile": qa,
        "task_pricing": {"builder": rate(builder), "qa": rate(qa)},
        "deadlines": {"queue_seconds": 60, "builder_seconds": 900, "builder_stop_seconds": 100,
                      "qa_seconds": 900, "qa_stop_seconds": 100, "margin_seconds": 60}}


def qa_definition(profile):
    value = task_definition(profile)
    value["volumes"] = [{"name": "state"}, {"name": "tmp"}]
    value["runtimePlatform"]["cpuArchitecture"] = profile.architecture
    c = value["containerDefinitions"][0]
    c.update(name="static-qa", entryPoint=["python", "-m", "app.static_qa_api"],
             mountPoints=[
                 {"sourceVolume": "state", "containerPath": "/state", "readOnly": False},
                 {"sourceVolume": "tmp", "containerPath": "/tmp", "readOnly": False},
             ])
    return value
