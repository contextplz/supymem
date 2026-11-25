"""Per-founder coding-agent consent (flag coding_agents_v1) across unrelated founders."""
from __future__ import annotations

import json

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from tests.company_os_harness import load_company_os_app
from tests.coding_tool_fixtures import private_test_install

FOUNDERS = ("usr_bakery_orders", "usr_trail_journal")


@pytest.fixture
def coding(tmp_path, monkeypatch):
    monkeypatch.delenv("SUPYMEM_MODE", raising=False)
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/coding.db")
    monkeypatch.setenv("LLM_OFFLINE", "1")
    monkeypatch.delenv("COMPANY_OS_REQUIRE_SIGNED_CALLER", raising=False)
    load_company_os_app()
    from app import state
    from app.api.coding_api import router as legacy_router
    from app.api.ui_config_api import router
    from app.coding_agents_consent import CodingAgentConsent
    from app.credits import policy
    state.reset_db_factory()
    policy.invalidate()
    app = FastAPI()
    app.include_router(router)
    app.include_router(legacy_router)
    with TestClient(app) as client:
        yield client, policy, state, CodingAgentConsent
    policy.invalidate()
    state.reset_db_factory()


def _flags(policy, value):
    policy.set_policy("feature_flags", value, "test")
    policy.invalidate()


def test_consent_and_revoke_per_founder_without_cross_founder_leak(coding):
    client, policy, state, model = coding
    _flags(policy, {"coding_agents_v1": True})
    none = {"claude": False, "codex": False, "cursor": False, "opencode": False}
    for subject in FOUNDERS:
        config = client.get("/ui-config", headers={"X-Subject": subject}).json()
        assert config == {"founder_ui": "chat", "coding_agents_v1": True, "coding_agents": {"consent": none}}

    def allow(subject, provider, value):
        return client.post("/ui-config/coding-agents/consent",
                           headers={"X-Subject": subject}, json={"provider": provider, "allow": value})

    response = allow(FOUNDERS[0], "claude", True)
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert response.json() == {"consent": {**none, "claude": True}}
    assert allow(FOUNDERS[0], "claude", True).json() == {"consent": {**none, "claude": True}}  # idempotent
    assert allow(FOUNDERS[1], "codex", True).json() == {"consent": {**none, "codex": True}}

    def read(subject):
        return client.get("/ui-config", headers={"X-Subject": subject}).json()["coding_agents"]["consent"]

    assert read(FOUNDERS[0]) == {**none, "claude": True}
    assert read(FOUNDERS[1]) == {**none, "codex": True}

    assert allow(FOUNDERS[0], "claude", False).json() == {"consent": none}
    assert read(FOUNDERS[0]) == none and read(FOUNDERS[1]) == {**none, "codex": True}

    with state.db()() as session:
        rows = [(row.founder_id, row.provider) for row in session.query(model).all()]
        assert len(rows) == 1 and rows[0][1] == "codex"
        # Only provider and time are stored: no credential-shaped columns exist.
        assert {column.name for column in model.__table__.columns} == {"id", "founder_id", "provider", "consented_at"}

    assert allow(FOUNDERS[0], "hermes", True).status_code == 400
    for subject in (None, "", "anonymous", "system"):
        headers = {"X-Subject": subject} if subject is not None else {}
        assert client.post("/ui-config/coding-agents/consent", headers=headers,
                           json={"provider": "claude", "allow": True}).status_code == 401


def test_flag_off_is_unchanged_and_refuses_consent_writes(coding, monkeypatch):
    client, policy, state, model = coding
    _flags(policy, {"coding_agents_v1": True})
    client.post("/ui-config/coding-agents/consent", headers={"X-Subject": FOUNDERS[0]}, json={"provider": "cursor", "allow": True})
    monkeypatch.setattr("app.coding_agents_consent.consents", lambda subject: pytest.fail("Flag-off config must not read consent"))
    for flags in ({}, {"coding_agents_v1": False}, {"coding_agents_v1": "true"}, {"coding_agents_v1": 1}):
        for baseline in ({}, {"founder_ui": "classic", "goals_v1": True}):
            _flags(policy, {**baseline, **flags})
            for subject in FOUNDERS:
                response = client.get("/ui-config", headers={"X-Subject": subject})
                expected = {"founder_ui": baseline.get("founder_ui", "chat")}
                if baseline.get("goals_v1"):
                    expected["goals_v1"] = True
                assert response.status_code == 200
                assert response.content == json.dumps(expected, separators=(",", ":")).encode()
                assert response.headers["cache-control"] == "no-store"
                assert client.post("/ui-config/coding-agents/consent", headers={"X-Subject": subject},
                                   json={"provider": "claude", "allow": True}).status_code == 404
            assert client.post("/ui-config/coding-agents/consent", json={"provider": "claude", "allow": True}).status_code == 404
    with state.db()() as session:
        assert [row.provider for row in session.query(model).all()] == ["cursor"]


@pytest.mark.parametrize("subject", FOUNDERS, ids=["business", "consumer"])
def test_local_owner_can_connect_and_revoke_without_a_hosted_flag(coding, monkeypatch, tmp_path, subject):
    client, policy, state, model = coding
    from app import coding_agents_consent
    from desktop_runtime import agent_status

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    private_test_install(monkeypatch, tmp_path)
    def hosted_policy(*_args):
        raise AssertionError("Local connection must not require a hosted policy")
    monkeypatch.setattr(coding_agents_consent, "get_policy", hosted_policy)
    binaries = {}
    monkeypatch.setattr(agent_status, "provider_binary", binaries.get)
    status = agent_status._status
    monkeypatch.setattr(agent_status, "_status", lambda *args, **kwargs: {"available": True}
                        if kwargs.get("expected") else status(*args, **kwargs))
    for provider in ("claude", "codex", "cursor", "opencode"):
        reply = client.post("/ui-config/coding-agents/consent", headers={"X-Subject": subject},
                                json={"provider": provider, "allow": True})
        assert reply.status_code == 409 and "Coding tools" in reply.json()["detail"]
        assert not coding_agents_consent.consents(subject)[provider]
        binary = tmp_path / provider
        binary.touch(mode=0o700)
        binaries[provider] = str(binary)
        candidate = agent_status._status(provider)["candidate"]
        for allow in (True, False):
            response = client.post("/ui-config/coding-agents/consent", headers={"X-Subject": subject},
                                   json={"provider": provider, "allow": allow, "candidate": candidate})
            assert response.status_code == 200
            assert response.headers["cache-control"] == "no-store"
            assert response.json()["consent"][provider] is allow
            assert coding_agents_consent.consents(subject)[provider] is allow
            assert agent_status.provider_pin(provider) == (agent_status.executable_identity(binary) if allow else None)
            assert (tmp_path / "coding-tools" / f"{provider}.json").exists() is allow
            other = next(founder for founder in FOUNDERS if founder != subject)
            assert coding_agents_consent.consents(other)[provider] is False
    with state.db()() as session:
        assert session.query(model).count() == 0


@pytest.mark.parametrize("subject", FOUNDERS, ids=["business", "consumer"])
def test_check_connection_allows_the_new_binary_without_restoring_revoked_consent(coding, monkeypatch, tmp_path, subject):
    from types import SimpleNamespace

    from app.chat.product_build_request import local_product_provider
    from desktop_runtime import agent_status
    from desktop_runtime.owned_coding import OwnedCoding, coding_failure_details

    client, _, _, _ = coding
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    private_test_install(monkeypatch, tmp_path)
    monkeypatch.setattr("app.workspaces.resolve.local_person", lambda: SimpleNamespace(id=subject))
    monkeypatch.setattr("app.core.local_owner.current_owner", lambda: SimpleNamespace(id=subject))
    binary = tmp_path / "codex"
    binary.touch(mode=0o700)
    probes = []
    login = b"Logged in using ChatGPT"

    def probe(_binary, arguments, *, expected):
        probes.append(expected)
        return 0, b"codex-cli 0.138.0" if arguments == ["--version"] else login

    monkeypatch.setattr(agent_status, "provider_binary", lambda provider, path_env=None: str(binary) if provider == "codex" else None)
    monkeypatch.setattr(agent_status, "_probe", probe)
    choices = "/ui-config/coding-agents/"
    tool, headers = {"provider": "codex"}, {"X-Subject": subject}
    approval = {**tool, "candidate": agent_status._status("codex")["candidate"]}
    assert client.post(choices + "consent", headers=headers, json={**approval, "allow": True}).status_code == 200
    previous = agent_status.provider_pin("codex")
    binary.write_text("new version")

    def build():
        return OwnedCoding(venture_id="ven_example", workspace_slug="example", provider="codex",
                           tracker=None, authorize=lambda: True).preflight()

    verified = len(probes)
    with pytest.raises(ValueError, match="^coding_provider_changed$") as changed:
        build()
    message = "You need to approve Codex again. Open Settings > Keys and connections > Coding tools, review the location and select Check connection."
    assert coding_failure_details(changed.value, "preflight", provider="codex")["message"] == message
    with pytest.raises(HTTPException) as blocked:
        local_product_provider("Build my landing page with Codex", subject)
    assert blocked.value.detail == {"code": "coding_provider_changed", "message": message}
    assert len(probes) == verified and agent_status.provider_pin("codex") == previous

    assert client.post(choices + "check", headers=headers, json=tool).status_code == 409
    assert len(probes) == verified
    approval = {**tool, "candidate": agent_status._status("codex")["candidate"]}
    login = b"Not logged in"
    assert client.post(choices + "check", headers=headers, json=approval).status_code == 409
    assert agent_status.provider_pin("codex") == previous
    login = b"Logged in using ChatGPT"
    assert client.post(choices + "check", headers=headers, json=approval).status_code == 200
    assert agent_status.provider_pin("codex") == agent_status.executable_identity(binary) != previous
    assert build() == str(binary)

    assert client.post(choices + "consent", headers=headers, json={**tool, "allow": False}).status_code == 200
    assert client.post(choices + "check", headers=headers, json=tool).status_code == 200
    with pytest.raises(ValueError, match="^coding_tool_not_connected$") as revoked:
        build()
    assert "allow Charlie" in coding_failure_details(revoked.value, "preflight", provider="codex")["message"]
    assert not (tmp_path / "coding-tools/codex.json").exists()
    assert client.post("/coding/consent", json={"adapter": "codex"}).status_code == 409
    assert client.post("/coding/consent", json={"adapter": "codex", "candidate": approval["candidate"]}).json()["consent"]["codex"] is True
    assert agent_status.provider_pin("codex") == agent_status.executable_identity(binary)


@pytest.mark.parametrize("subject", FOUNDERS, ids=["business", "consumer"])
def test_legacy_consent_requires_fresh_allow(coding, monkeypatch, tmp_path, subject):
    from app.coding_agents_consent import raise_if_tool_changed
    from desktop_runtime import agent_status

    client, policy, _, _ = coding
    _flags(policy, {"coding_agents_v1": True})
    headers = {"X-Subject": subject}
    routes = "/ui-config/coding-agents/"
    provider = {"provider": "codex"}
    assert client.post(routes + "consent", headers=headers, json={**provider, "allow": True}).status_code == 200

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    private_test_install(monkeypatch, tmp_path)
    binary = tmp_path / "codex"
    binary.touch(mode=0o700)
    monkeypatch.setattr(agent_status, "provider_binary", lambda name: str(binary) if name == "codex" else None)
    status = agent_status._status("codex")
    approval = {**provider, "candidate": status["candidate"]}
    probes = []

    def probe(binary, arguments, *, expected):
        probes.append(arguments)
        return 0, b"codex-cli 0.138.0" if arguments == ["--version"] else b"Logged in using ChatGPT"

    monkeypatch.setattr(agent_status, "_probe", probe)
    checked = client.post(routes + "check", headers=headers, json=approval)
    assert (checked.status_code, probes) == (409, [])
    detail = checked.json()["detail"]
    assert detail["code"] == "coding_tool_not_connected" and "Turn on Allow" in detail["message"]
    with pytest.raises(HTTPException) as blocked:
        raise_if_tool_changed({"codex": True}, [status])
    assert blocked.value.detail == detail
    assert agent_status.provider_pin("codex") is None
    allowed = client.post(routes + "consent", headers=headers, json={**approval, "allow": True})
    assert allowed.status_code == 200 and allowed.json()["consent"]["codex"] is True
    assert agent_status.provider_pin("codex") == agent_status.executable_identity(binary)


@pytest.mark.parametrize("subject", FOUNDERS, ids=["business", "consumer"])
@pytest.mark.parametrize("scenario", ["brew", "cursor", "npm", "claude-native", "outside", "world-writable",
    "launcher-file", "other-launcher", "same-target", "verification", "legacy"])
def test_coding_tool_update_requires_the_same_trusted_install(coding, monkeypatch, tmp_path, subject, scenario):
    from pathlib import Path
    from types import SimpleNamespace

    from app.chat.product_build_request import local_product_provider
    from desktop_runtime import agent_status, loop_diagnose
    from desktop_runtime.owned_coding import OwnedCoding

    client, _, _, _ = coding
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    data = tmp_path / "data"
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(data))
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{data}/coding.db")
    prefix = tmp_path / "prefix"
    provider, label, command = "codex", "Codex", "codex"
    install = prefix / "Caskroom/codex"
    if scenario == "cursor":
        provider, label, command = "cursor", "Cursor", "cursor-agent"
        install = prefix / "share/cursor-agent/versions"
    elif scenario in {"npm", "claude-native"}:
        provider, label, command = "claude", "Claude Code", "claude"
        install = prefix / ("lib/node_modules/@anthropic-ai/claude-code" if scenario == "npm" else "share/claude/versions")
    launcher = prefix / "bin" / command
    launcher.parent.mkdir(parents=True)
    monkeypatch.setenv("PATH", str(launcher.parent))
    lstat = Path.lstat

    def installed_on_private_volume(path):
        if path in tmp_path.parents:
            return SimpleNamespace(st_mode=0o40755, st_uid=0)
        return lstat(path)

    monkeypatch.setattr(Path, "lstat", installed_on_private_volume)
    old = install / "1.0.0" / command
    new = (prefix / "outside" if scenario == "outside" else install / "2.0.0") / command
    for target in (old, new):
        target.parent.mkdir(parents=True)
        target.touch(mode=0o700)
        for parent in target.parents:
            if parent.is_relative_to(prefix):
                parent.chmod(0o755)
    launcher.parent.chmod(0o755)
    launcher.symlink_to(Path("..") / old.relative_to(prefix))
    probes = []
    earlier = {"codex": b"codex-cli 0.163.0", "cursor": b"cursor-agent 2026.11.17", "claude": b"2.1.299"}[provider]
    responses = {
        ("--version",): {"codex": b"codex-cli 0.164.0\n", "cursor": b"cursor-agent 2026.11.18\n",
                        "claude": b"2.1.300 (Claude Code)\n"}[provider],
        ("login", "status"): b"Logged in using ChatGPT",
        ("auth", "status"): b'{"loggedIn":true,"authMethod":"claude.ai"}',
        ("status",): b'{"isAuthenticated":true,"hasAccessToken":true,"hasRefreshToken":true}',
        ("--help",): b"  --sandbox <mode> Explicitly enable or disable sandbox mode.\n  --workspace <path> Workspace\n",
    }

    def probe(binary, arguments, *, expected, tracker=None):
        assert agent_status.executable_identity(binary) == expected
        probes.append((expected[0], arguments))
        if arguments == ["--version"] and expected[0] == str(old):
            return 0, earlier
        return 0, responses[tuple(arguments)]

    monkeypatch.setattr(agent_status, "_probe", probe)
    headers = {"X-Subject": subject}
    assert client.post("/ui-config/coding-agents/consent", headers=headers,
        json={"provider": provider, "allow": True, "candidate": agent_status._status(provider)["candidate"]}).status_code == 200
    previous = agent_status.provider_pin(provider)
    if scenario == "legacy":
        (data / "coding-tools" / f"{provider}.json").write_text(json.dumps(previous))
    probes.clear()
    if scenario == "same-target":
        old.write_text("replaced")
    else:
        if scenario == "npm":
            install.rename(install.with_name("retired-package"))
            new.parent.mkdir(parents=True)
            for parent in (install, new.parent):
                parent.chmod(0o755)
            new.touch(mode=0o700)
        launcher.unlink()
        if scenario == "launcher-file":
            launcher.touch(mode=0o700)
        else:
            launcher.symlink_to(Path("..") / new.relative_to(prefix))
        if scenario not in {"other-launcher", "npm"}:
            old.unlink()
    if scenario == "world-writable":
        new.parent.chmod(0o777)
    elif scenario == "other-launcher":
        other = tmp_path / "elsewhere"
        other.mkdir()
        (other / command).symlink_to(new)
        monkeypatch.setenv("PATH", str(other))
    elif scenario == "verification":
        responses["login", "status"] = b"Not logged in"

    build = OwnedCoding(venture_id=subject, workspace_slug=subject, provider=provider, tracker=None, authorize=lambda: True)
    request = f"Build my landing page with {label}"
    if scenario not in {"brew", "cursor", "npm", "claude-native"}:
        with pytest.raises(ValueError, match="^coding_provider_changed$"):
            build.preflight()
        with pytest.raises(HTTPException) as blocked:
            local_product_provider(request, subject)
        assert blocked.value.detail["code"] == "coding_provider_changed"
        assert "Check connection" in blocked.value.detail["message"]
        assert agent_status.provider_pin(provider) == previous
        assert bool(probes) == (scenario == "verification")
        return

    assert local_product_provider(request, subject) == provider
    assert agent_status.provider_pin(provider) == previous
    probes.clear()
    assert build.preflight() == str(new)
    assert {binary for binary, _ in probes} == {str(new)}
    login_checks = {"codex": [("login", "status")], "claude": [("auth", "status")],
                    "cursor": [("status",), ("--help",)]}[provider]
    assert [tuple(arguments) for _, arguments in probes] == [("--version",), *login_checks]
    assert agent_status.provider_pin(provider) == agent_status.executable_identity(new) != previous
    activity = []
    build.line_callback = lambda line: activity.append(loop_diagnose.stream_text(line, provider, tmp_path))
    assert build.preflight() == build.preflight() == str(new)
    version = {"codex": "0.164.0", "cursor": "2026.11.18", "claude": "2.1.300"}[provider]
    assert activity == [f"{label} was updated to {version}; verified and continuing"]
