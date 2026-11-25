"""Synthetic checks of actual emitted environments; no provider or vendor calls."""
from __future__ import annotations

import importlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.company_os_harness import load_company_os_app

_PLATFORM_ENV = (
    "OPENAI_API_KEY", "PAGEBUILDER_OPENAI_API_KEY", "OPENAI_DEVICE_VISION_API_KEY",
    "LLM_API_KEY", "GEMINI_API_KEY", "CRM_WEBHOOK_URL", "COMPOSIO_API_KEY", "RESEND_API_KEY",
    "COS_INTERNAL_ASSERTION_SECRET", "SESSION_SECRET", "COMPANY_OS_VAULT_SECRET",
    "DATABASE_URL", "COMPANY_OS_DB_URL", "STRIPE_SECRET_KEY", "GITHUB_TOKEN",
    "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
    "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI", "AWS_CONTAINER_CREDENTIALS_FULL_URI",
    "NODE_OPTIONS", "PYTHONPATH", "LD_PRELOAD", "HTTPS_PROXY", "NEW_VENDOR_SECRET",
    "TWITTER_AUTH_TOKEN", "TWITTER_CT0", "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE",
)


@pytest.fixture
def service_env(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("COMPANY_OS_ENV", "local")
    monkeypatch.setenv("COMPANY_OS_REQUIRE_SIGNED_CALLER", "0")
    monkeypatch.delenv("ECS_CONTAINER_METADATA_URI", raising=False)
    monkeypatch.delenv("ECS_CONTAINER_METADATA_URI_V4", raising=False)
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COMPANY_OS_WORKING_MEMORY_PATH", str(tmp_path / "memory.db"))
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/synthetic.db")
    monkeypatch.setenv("LLM_OFFLINE", "1")
    load_company_os_app()
    for name in _PLATFORM_ENV:
        monkeypatch.setenv(name, "synthetic-parent-value")
    for name in ("USER", "LOGNAME"):
        monkeypatch.setenv(name, "synthetic-founder")


def _capture(monkeypatch, module):
    captured = []
    def run(argv, **kwargs):
        captured.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout='{"result":"synthetic"}', stderr="")
    monkeypatch.setattr(module.subprocess, "run", run)
    return captured


def _assert_safe(env):
    assert isinstance(env, dict), "subprocess inherited its parent environment"
    assert env["USER"] == env["LOGNAME"] == "synthetic-founder"
    assert "HOME" in env and "PATH" in env
    assert not (set(_PLATFORM_ENV) - {"GIT_DIR", "GIT_WORK_TREE"}) & env.keys()
    assert "synthetic-parent-value" not in env.values()


@pytest.mark.asyncio
@pytest.mark.parametrize("callsite", [
    "opengtm_status", "opengtm_run", "openleads_find", "agent_reach",
    "ops_claude", "firstlook_git", "repo_local", "build_timeout",
])
async def test_process_boundaries_emit_only_safe_environment(service_env, monkeypatch, tmp_path, callsite):
    names = {
        "opengtm_status": "app.gtm.opengtm_bridge", "opengtm_run": "app.gtm.opengtm_bridge",
        "openleads_find": "app.gtm.openleads_bridge", "agent_reach": "app.research.agent_reach_bridge",
        "ops_claude": "app.ops.base_agent", "firstlook_git": "app.firstlook.serve",
        "repo_local": "app.provisioning.adapters.repo_local", "build_timeout": "app.build.dispatcher",
    }
    module = importlib.import_module(names[callsite])
    if callsite == "build_timeout":
        launcher = AsyncMock(return_value=SimpleNamespace(returncode=0, wait=AsyncMock()))
        monkeypatch.setattr(module.asyncio, "create_subprocess_exec", launcher)
        assert await module.dispatch_with_timeout_kill(["synthetic"], cwd=tmp_path, timeout_s=1) == {
            "killed": False, "returncode": 0}
        _assert_safe(launcher.call_args.kwargs["env"])
        return
    captured = _capture(monkeypatch, module)
    if callsite.startswith("opengtm"):
        binary = tmp_path / "opengtm"
        binary.write_text("synthetic executable placeholder", encoding="utf-8")
        binary.chmod(0o700)
        monkeypatch.setattr(module, "OPENGTM_BIN", binary)
        monkeypatch.setattr(module, "OPENGTM_DIR", tmp_path)
        monkeypatch.setattr(module, "OPENGTM_HOME", tmp_path / "adapter-home")
        if callsite == "opengtm_status":
            assert module.status()["ready"]
        else:
            monkeypatch.setattr(module, "_require_bin", lambda: None)
            module._run(["qualify"])
    elif callsite == "openleads_find":
        # Production's license/consent admission remains closed. This synthetic
        # admission tests the latent process boundary without enabling it.
        monkeypatch.setattr(module, "status", lambda: {"ready": True})
        monkeypatch.setattr(module, "OPENLEADS_HOME", tmp_path / "adapter-home")
        monkeypatch.setattr(module, "OPENLEADS_DIR", tmp_path)
        module.find_leads(query="synthetic", deep=False)
    elif callsite == "agent_reach":
        monkeypatch.setattr(module.shutil, "which", lambda name: "/synthetic/" + name)
        module._run(["gh", "repo", "view", "synthetic/project"], env={"OPENAI_API_KEY": "synthetic-injection"})
    elif callsite == "ops_claude":
        class Agent(module.BaseOpsAgent):
            def run(self, task, context):
                return {}
        assert Agent()._call_claude_cli("synthetic") == "synthetic"
    elif callsite == "firstlook_git":
        module._run_git(["init"], repo=tmp_path, env=module._isolated_git_env(tmp_path))
    else:
        monkeypatch.setattr(module, "__file__", str(tmp_path / "services/company_os/app/provisioning/adapters/repo_local.py"))
        await module.RepoLocal().provision(SimpleNamespace(venture=SimpleNamespace(id="ven_synthetic"), company="Synthetic"))
    assert len(captured) == 1
    env = captured[0][1]["env"]
    _assert_safe(env)
    if callsite == "firstlook_git":
        assert env["GIT_DIR"] == str(tmp_path / ".git")
        assert env["GIT_WORK_TREE"] == str(tmp_path)
        assert env["GIT_CEILING_DIRECTORIES"] == str(tmp_path.parent.resolve())
        assert env["GIT_AUTHOR_NAME"] == env["GIT_COMMITTER_NAME"] == "Company OS"
    if callsite.startswith("opengtm"):
        assert env["OPENGTM_HOME"] == str(tmp_path / "adapter-home")
    if callsite == "openleads_find":
        assert env["OPENLEADS_HOME"] == str(tmp_path / "adapter-home")


@pytest.mark.parametrize("hosted,command", [(False, "twitter"), (True, "twitter"), (False, "gh")])
def test_only_local_twitter_receives_explicit_adapter_tokens(service_env, monkeypatch, hosted, command):
    from app.research import agent_reach_bridge as reach
    monkeypatch.setenv("APP_ENV", "production" if hosted else "local")
    monkeypatch.setattr(reach.shutil, "which", lambda name: "/synthetic/" + name)
    captured = _capture(monkeypatch, reach)
    explicit = {"TWITTER_AUTH_TOKEN": "synthetic-adapter-token", "TWITTER_CT0": "synthetic-adapter-csrf",
                "OPENAI_API_KEY": "synthetic-injected-platform", "DATABASE_URL": "synthetic-injected-dsn"}
    reach._run([command, "synthetic"], env=explicit)
    env = captured[0][1]["env"]
    assert "OPENAI_API_KEY" not in env and "DATABASE_URL" not in env
    assert "synthetic-parent-value" not in env.values()
    if not hosted and command == "twitter":
        assert env["TWITTER_AUTH_TOKEN"] == explicit["TWITTER_AUTH_TOKEN"]
        assert env["TWITTER_CT0"] == explicit["TWITTER_CT0"]
    else:
        assert "TWITTER_AUTH_TOKEN" not in env and "TWITTER_CT0" not in env


def test_hosted_ops_cannot_start_subscription_cli(service_env, monkeypatch):
    from app.ops import base_agent
    monkeypatch.setenv("APP_ENV", "production")
    def forbidden(*args, **kwargs):
        pytest.fail("hosted Ops crossed the subscription CLI boundary")
    monkeypatch.setattr(base_agent.subprocess, "run", forbidden)
    class Agent(base_agent.BaseOpsAgent):
        def run(self, task, context):
            return {}
    with pytest.raises(RuntimeError, match="cloud_forbids_byo"):
        Agent()._call_claude_cli("synthetic")


def test_opengtm_parent_credentials_do_not_make_capabilities_ready(service_env, monkeypatch, tmp_path):
    from app.gtm import opengtm_bridge as bridge
    binary = tmp_path / "opengtm"
    binary.write_text("synthetic executable placeholder", encoding="utf-8")
    binary.chmod(0o700)
    monkeypatch.setattr(bridge, "OPENGTM_BIN", binary)
    monkeypatch.setattr(bridge, "OPENGTM_DIR", tmp_path)
    captured = _capture(monkeypatch, bridge)
    status = bridge.status()
    assert status["offline_ok"]
    assert not status["gemini"]["ready"] and not status["crm_webhook"]["ready"]
    assert status["gemini"]["code"] == status["crm_webhook"]["code"] == "founder_scoped_account_required"
    _assert_safe(captured[0][1]["env"])


@pytest.mark.parametrize("args,require_gemini", [(["research"], True), (["sync"], False)])
def test_opengtm_parent_credentials_cannot_admit_paid_or_crm_command(service_env, monkeypatch, args, require_gemini):
    from app.gtm import opengtm_bridge as bridge
    # service_env populates both GEMINI_API_KEY and CRM_WEBHOOK_URL. They are
    # server settings, not evidence of a founder-scoped account connection.
    def forbidden(*args, **kwargs):
        pytest.fail("unscoped credentials admitted an OpenGTM subprocess")
    monkeypatch.setattr(bridge, "_require_bin", forbidden)
    monkeypatch.setattr(bridge.subprocess, "run", forbidden)
    with pytest.raises(RuntimeError, match="founder-scoped account connection"):
        bridge._run(args, require_gemini=require_gemini)
