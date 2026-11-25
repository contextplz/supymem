"""Surface modules must not reach into the ORM (46 T10.7, invariant I4).

Enforced by import graph rather than review. Routes parse arguments and call a
service; the service owns the session. Phase 0-9 routers predate this rule and
are grandfathered by name — the list may shrink, never grow.
Hosted ORM paths are outside this local branch contract in either test mode.
"""

from __future__ import annotations

import ast
import socket
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
API_DIR = ROOT / "services" / "company_os" / "app" / "api"

# Routers written before the service layer existed. Adding a name here is a
# regression; removing one is progress.
GRANDFATHERED = {
    "activity.py",
    "agents.py",
    "build.py",
    "charter.py",
    "chat_api.py",
    "clarify.py",
    "concierge.py",
    "dashboard.py",
    "events.py",
    "evolution.py",
    "feedback.py",
    "governance.py",
    "gtm.py",
    "hosting.py",
    "intake.py",
    "onboarding.py",
    "org.py",
    "schemas.py",
    "tasks_api.py",
    "ventures.py",
    "worldmodel.py",
}

FORBIDDEN_MODULES = {"app.db.models", "app.db.session", "app.state"}
HOSTED_ROUTERS = {"beta_api.py", "credits_api.py", "internal_ops.py", "payments_api.py"}


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


def _phase10_routers() -> list[Path]:
    return sorted(
        p
        for p in API_DIR.glob("*.py")
        if p.name not in GRANDFATHERED | HOSTED_ROUTERS and p.name != "__init__.py"
    )


def test_there_is_something_to_check() -> None:
    assert _phase10_routers(), "no Phase 10 routers found — did the glob break?"


@pytest.mark.parametrize("path", _phase10_routers(), ids=lambda p: p.name)
def test_router_does_not_import_the_orm(path: Path) -> None:
    offenders = sorted(_imported_modules(path) & FORBIDDEN_MODULES)
    assert not offenders, (
        f"{path.name} imports {offenders}; move the query into a service module"
    )


@pytest.mark.parametrize("path", _phase10_routers(), ids=lambda p: p.name)
def test_router_does_not_open_a_session(path: Path) -> None:
    source = path.read_text()
    assert "session.commit()" not in source, (
        f"{path.name} commits a transaction; that belongs in a service module"
    )


def test_grandfather_list_is_accurate() -> None:
    """Every grandfathered name must still exist, so the list cannot rot."""
    missing = {name for name in GRANDFATHERED if not (API_DIR / name).exists()}
    assert not missing, f"stale grandfather entries: {sorted(missing)}"


def test_local_flow_files_are_registered_once_in_ci() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    paths = workflow["env"]["UNIT_PATHS"].split()
    flows = sorted((ROOT / "tests/unit/local_flows").glob("test_*_flow.py"))
    assert len(flows) == 12
    for path in flows:
        assert paths.count(path.relative_to(ROOT).as_posix()) == 1
    assert paths.count("tests/unit/db/test_local_upgrade.py") == 1


def test_windows_release_smokes_the_installed_nsis_payload() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/desktop-release.yml").read_text())
    steps = workflow["jobs"]["build"]["steps"]
    smoke = next(step for step in steps if step.get("if") == "runner.os == 'Windows'"
                 and "smoke_local_stack.py" in step.get("run", ""))
    script = smoke["run"]
    assert smoke["shell"] == "pwsh"
    assert "bundle/nsis" in script and "Start-Process" in script
    assert '"/D=$install"' in script and "-Wait -PassThru" in script
    assert "ExitCode" in script and "Join-Path $env:RUNNER_TEMP" in script
    assert "café" in script
    assert "smoke_local_stack.py $executable $install" in script
    assert "src-tauri/resources" not in script


@pytest.mark.parametrize("domain", ["freight-audits", "garden-journals"], ids=["b2b", "consumer"])
def test_shared_local_owner_headers_preserve_authentication(monkeypatch, tmp_path, isolate_app_imports, domain):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from core.local_security import LocalNetworkMiddleware
    from tests import company_os_harness as harness

    monkeypatch.syspath_prepend(str(harness.COMPANY_OS))
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path / domain))
    monkeypatch.setenv("SUPYMEM_LOCAL_PROXY_SECRET_FILE", str(tmp_path / domain / "owner-session.secret"))
    application = FastAPI()

    @application.get("/fixture")
    def fixture_response():
        return {"domain": domain}

    headers = harness.local_owner_headers()
    assert set(headers) == {"X-Local-Proxy-Secret", "Origin"}
    with TestClient(LocalNetworkMiddleware(application), base_url="http://127.0.0.1",
                    client=("127.0.0.1", 50000)) as client:
        assert client.get("/fixture", headers=headers).json() == {"domain": domain}
        assert client.get("/fixture").status_code == 403
        assert client.get("/fixture", headers={"X-Local-Proxy-Secret": "invalid"}).status_code == 403
        assert client.get("/fixture", headers={"Origin": headers["Origin"]}).status_code == 403
        assert client.get("/fixture", headers={**headers, "Origin": "https://untrusted.invalid"}).status_code == 403
    monkeypatch.delenv("SUPYMEM_MODE")
    assert harness.local_owner_headers() == {}


@pytest.mark.parametrize("message", ["Help me review my freight-audit idea", "Help me review my gardening idea"], ids=["b2b", "consumer"])
def test_chat_requests_keep_conversation_types_and_input_bounds(monkeypatch, tmp_path, isolate_app_imports, message):
    from pydantic import ValidationError
    from tests.company_os_harness import load_company_os_app

    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    load_company_os_app()
    from app.api.chat_api import ChatBody, ConfirmBody, UpdateConversationBody

    assert ChatBody(message="x" * 4000, conversation_id="c" * 128).message == "x" * 4000
    assert ConfirmBody(action="a" * 64, conversation_id="c" * 128).conversation_id == "c" * 128
    assert UpdateConversationBody(pinned=False, archived=True).archived is True
    for invalid in ({"conversation_id": 1}, {"conversation_id": {}}, {"conversation_id": "c" * 129},
                    {"message": "x" * 4001}, {"body": "x" * 4001}, {"idempotency_key": "k" * 129}):
        with pytest.raises(ValidationError):
            ChatBody(**{"message": message, **invalid})
    for invalid in ({"conversation_id": 1}, {"conversation_id": "c" * 129}, {"action": "a" * 65}):
        with pytest.raises(ValidationError):
            ConfirmBody(**{"action": "site.preview", **invalid})
    for invalid in ({"pinned": "true"}, {"pinned": 1}, {"archived": "false"}, {"archived": None}):
        with pytest.raises(ValidationError):
            UpdateConversationBody(**invalid)


@pytest.mark.parametrize("message", ["Review my warehouse tool", "Review my knitting idea"], ids=["b2b", "consumer"])
def test_chat_reply_flag_requires_an_actual_boolean(monkeypatch, tmp_path, isolate_app_imports, message):
    from pydantic import ValidationError
    from tests.company_os_harness import load_company_os_app

    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    load_company_os_app()
    from app.api.chat_api import ChatBody

    assert ChatBody(message=message, reply_to_question=False).reply_to_question is False
    assert ChatBody(message=message, reply_to_question=True).reply_to_question is True
    for invalid in ("true", "false", 0, 1, None):
        with pytest.raises(ValidationError):
            ChatBody(message=message, reply_to_question=invalid)


@pytest.mark.parametrize("domain", ["equipment-service", "weekend-recipes"], ids=["b2b", "consumer"])
def test_local_flow_isolation_uses_private_state_and_rejects_network(monkeypatch, tmp_path, isolate_app_imports, domain):
    from tests import conftest as isolation

    root = tmp_path / domain
    root.mkdir()
    monkeypatch.setenv("SUPYMEM_LOCAL_KEY_PIPE", "1")
    request = SimpleNamespace(module=SimpleNamespace(
        __file__=str(ROOT / "tests/unit/local_flows/test_sample_flow.py")))
    fixture = isolation._isolate_local_flows.__wrapped__(request, root, monkeypatch)
    next(fixture)
    from app.core.local_secrets import read_settings
    from app.core.providers_local import configured_providers
    from app.state import db
    import primp

    assert not read_settings("smtp")["values"]
    assert not read_settings("search")["values"]
    assert not any(provider["configured"] for provider in configured_providers()["providers"].values())
    assert Path(db().kw["bind"].url.database).is_relative_to(root)
    with socket.socket() as connection:
        with pytest.raises(AssertionError, match="offline flow"):
            connection.connect(("127.0.0.1", 443))
    with pytest.raises(AssertionError, match="offline flow"):
        primp.Client().request("GET", "https://example.invalid")
    with pytest.raises(AssertionError, match="offline flow"):
        next(fixture)
