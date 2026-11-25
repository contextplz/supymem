from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

from tests.company_os_harness import load_company_os_app

load_company_os_app(reset=False)


def _resolved():
    return SimpleNamespace(
        provider="azure_foundry",
        model="DeepSeek-V4-Pro",
        source="platform_default",
    )


def test_runtime_quote_stays_unavailable_without_attested_deployment(monkeypatch):
    from app.builder import budgets

    monkeypatch.setattr(budgets, "resolve_llm_config", lambda _wid: _resolved())
    for name in (
        "BUILD_FARGATE_PROFILE",
        "BUILD_QA_FARGATE_PROFILE",
        "BUILD_FARGATE_TASK_PRICING",
    ):
        monkeypatch.delenv(name, raising=False)
    quote = budgets.runtime_quote("workspace")
    assert quote["ready"] is False
    assert quote["ceiling_micro"] is None
    assert quote["reasons"] == ["build_runtime_configuration_unverified"]


def test_runtime_quote_accepts_exact_profiles_and_unexpired_pricing(monkeypatch):
    from app.builder import budgets, lifecycle
    from app.infra.fargate_qa import QaProfile
    from tests.fargate_fixtures import profile_data

    monkeypatch.setattr(budgets, "resolve_llm_config", lambda _wid: _resolved())
    builder = profile_data()
    qa = QaProfile.from_dict(
        {
            **builder,
            "container_name": "static-qa",
            "task_definition_arn": builder["task_definition_arn"].replace(
                "managed-fixture", "qa-fixture"
            ),
        }
    ).snapshot()
    now = datetime.now(timezone.utc)

    def rate(policy):
        return {
            "policy_id": policy,
            "micro_per_second": "27.4277777778",
            "minimum_seconds": 60,
            "currency": "USD",
            "unit": "task_second",
            "region": "us-east-1",
            "cpu": 2048,
            "memory_mb": 4096,
            "architecture": "X86_64",
            "source": "https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonECS/current/index.json",
            "observed_at": (now - timedelta(minutes=1)).isoformat(),
            "expires_at": (now + timedelta(days=7)).isoformat(),
        }

    monkeypatch.setenv("BUILD_FARGATE_PROFILE", json.dumps(builder))
    monkeypatch.setenv("BUILD_QA_FARGATE_PROFILE", json.dumps(qa))
    monkeypatch.setenv(
        "BUILD_FARGATE_TASK_PRICING",
        json.dumps({"builder": rate("aws-fargate-use1-x86-2vcpu-4gb"), "qa": rate("aws-fargate-use1-x86-2vcpu-4gb")}),
    )
    quote = budgets.runtime_quote("workspace")
    assert quote["ready"] is True
    assert quote["runtime_policy"]["verified_execution"] is True
    assert quote["runtime_policy"]["tool_names"] == ["terminal", "file_editor", "finish", "think"]
    assert quote["artifact_ceiling_micro"] == 0
    assert quote["ceiling_micro"] == (
        quote["model_ceiling_micro"]
        + quote["builder_compute_ceiling_micro"]
        + quote["qa_compute_ceiling_micro"]
    )
    assert lifecycle.validate_runtime(quote) is quote
    product_quote = budgets.runtime_quote("workspace", product=True)
    assert quote["runtime_policy"]["max_steps"] == 40
    assert product_quote["runtime_policy"]["max_steps"] == 56
    assert product_quote["deadlines"]["builder_seconds"] == 2400
    assert product_quote["runtime_policy"]["timeout_seconds"] == 2400
    assert product_quote["model_ceiling_micro"] * 40 == quote["model_ceiling_micro"] * 56
    assert product_quote["builder_compute_ceiling_micro"] == quote["builder_compute_ceiling_micro"]
    assert lifecycle.validate_runtime(product_quote) is product_quote


def test_runtime_quote_rejects_expired_pricing(monkeypatch):
    from app.builder import budgets
    from app.infra.fargate_qa import QaProfile
    from tests.fargate_fixtures import profile_data

    monkeypatch.setattr(budgets, "resolve_llm_config", lambda _wid: _resolved())
    builder = profile_data()
    qa = QaProfile.from_dict(
        {
            **builder,
            "container_name": "static-qa",
            "task_definition_arn": builder["task_definition_arn"].replace(
                "managed-fixture", "qa-fixture"
            ),
        }
    ).snapshot()
    now = datetime.now(timezone.utc)
    stale = {
        "policy_id": "expired",
        "micro_per_second": "27.4277777778",
        "minimum_seconds": 60,
        "currency": "USD",
        "unit": "task_second",
        "region": "us-east-1",
        "cpu": 2048,
        "memory_mb": 4096,
        "architecture": "X86_64",
        "source": "https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonECS/current/index.json",
        "observed_at": (now - timedelta(days=2)).isoformat(),
        "expires_at": (now - timedelta(days=1)).isoformat(),
    }
    monkeypatch.setenv("BUILD_FARGATE_PROFILE", json.dumps(builder))
    monkeypatch.setenv("BUILD_QA_FARGATE_PROFILE", json.dumps(qa))
    monkeypatch.setenv("BUILD_FARGATE_TASK_PRICING", json.dumps({"builder": stale, "qa": stale}))
    quote = budgets.runtime_quote("workspace")
    assert quote["ready"] is False
    assert quote["ceiling_micro"] is None


def test_qa_provider_accepts_only_an_explicit_bounded_compatibility_profile(monkeypatch):
    import boto3
    from app.infra import fargate_qa
    from app.infra.fargate_qa import QaProfile
    from tests.fargate_fixtures import profile_data

    base = profile_data()
    previous = QaProfile.from_dict({
        **base,
        "container_name": "static-qa",
        "task_definition_arn": base["task_definition_arn"].replace("managed-fixture", "qa-fixture"),
    }).snapshot()
    current = {**previous, "task_definition_arn": previous["task_definition_arn"].rsplit(":", 1)[0] + ":2",
               "image_uri": previous["image_uri"].split("@", 1)[0] + "@sha256:" + "b" * 64}
    clients = []
    monkeypatch.setattr(boto3, "client", lambda name, config: clients.append(name) or object())
    monkeypatch.setenv("BUILD_QA_FARGATE_PROFILE", json.dumps(current))
    monkeypatch.setenv("BUILD_QA_FARGATE_COMPAT_PROFILES", json.dumps([previous]))
    assert fargate_qa.configured_provider(previous).profile.snapshot() == previous
    assert clients == ["ecs", "ec2"]
    monkeypatch.setenv("BUILD_QA_FARGATE_COMPAT_PROFILES", json.dumps([previous] * 4))
    with __import__("pytest").raises(fargate_qa.BuildProviderError, match="not_configured"):
        fargate_qa.configured_provider(previous)


def test_stale_invalid_qa_compatibility_record_does_not_disable_current_profile(monkeypatch):
    import boto3
    import pytest
    from app.infra import fargate_qa
    from app.infra.fargate_qa import QaProfile
    from tests.fargate_fixtures import profile_data

    base = profile_data()
    current = QaProfile.from_dict({
        **base,
        "container_name": "static-qa",
        "task_definition_arn": base["task_definition_arn"].replace("managed-fixture", "qa-fixture"),
    }).snapshot()
    stale = {**current, "browser_version": "151.0.7922.34"}
    monkeypatch.setattr(boto3, "client", lambda name, config: object())
    monkeypatch.setenv("BUILD_QA_FARGATE_PROFILE", json.dumps(current))
    monkeypatch.setenv("BUILD_QA_FARGATE_COMPAT_PROFILES", json.dumps([stale]))

    assert fargate_qa.configured_provider(current).profile.snapshot() == current
    with pytest.raises(fargate_qa.BuildProviderError, match="not_configured"):
        fargate_qa.configured_provider(stale)
