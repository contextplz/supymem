from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

ROOT = Path(os.environ.get("TRIAGE_ROOT", Path(__file__).resolve().parents[2]))


@pytest.fixture(params=["freight", "gardening"], ids=["b2b", "consumer"])
def plain_tree(tmp_path, request):
    root = tmp_path / (request.param + " worktree")
    (root / "scripts").mkdir(parents=True)
    shutil.copyfile(ROOT / "scripts/local_ci.sh", root / "scripts/local_ci.sh")
    admin = root / "services/company_os/app/admin/router.py"
    admin.parent.mkdir(parents=True)
    admin.touch()
    tests = root / "tests/unit" / request.param
    tests.mkdir(parents=True)
    for name in ("alpha", "beta"):
        (tests / f"test_{name}.py").touch()
    (root / "apps/console").mkdir(parents=True)
    commands = root / "bin"
    commands.mkdir()
    python = commands / "python"
    python.write_text(f"#!{sys.executable}\n" + '''
import json
import os
from pathlib import Path
import sys

arguments = sys.argv[1:]
kind = "unit" if arguments[:2] == ["-m", "pytest"] else "ruff"
mode = os.environ.get("SUPYMEM_MODE", "unset")
call = {"kind": kind, "mode": mode, "arguments": arguments}
if kind == "unit":
    call["flags"] = [os.environ.get(name) for name in (
        "GUARDRAILS_MODE", "LLM_OFFLINE", "LLM_CASSETTE_MODE")]
    call["database_overrides"] = any(name in os.environ for name in (
        "DATABASE_URL", "DB_URL"))
    call["data_path"] = os.environ["COMPANY_OS_DATA_DIR"]
    call["database_path"] = os.environ["COMPANY_OS_DB_URL"]
    call["memory_override"] = "COMPANY_OS_WORKING_MEMORY_PATH" in os.environ
with (Path(os.environ["STUB_ROOT"]) / "calls.jsonl").open("a") as output:
    output.write(json.dumps(call) + "\\n")
failure = os.environ.get("STUB_FAIL", "")
if kind == "unit" and failure == f"empty_{mode}":
    sys.exit(5)
sys.exit(1 if failure == kind or failure == f"{kind}_{mode}" else 0)
''')
    node = commands / "node"
    node.write_text(f"#!{sys.executable}\n" + '''
import json
import os
from pathlib import Path
import sys

kind = "console" if sys.argv[1] == "scripts/test.cjs" else "tsc"
with (Path(os.environ["STUB_ROOT"]) / "calls.jsonl").open("a") as output:
    output.write(json.dumps({"kind": kind}) + "\\n")
sys.exit(1 if os.environ.get("STUB_FAIL") == kind else 0)
''')
    cores = commands / "nproc"
    cores.write_text("#!/bin/sh\nprintf '8\\n'\n")
    for executable in (python, node, cores):
        executable.chmod(0o755)
    return root, {**os.environ, "PY": str(python), "STUB_ROOT": str(root),
        "SUPYMEM_MODE": "inherited", "PATH": f"{commands}:{os.defpath}", "TMPDIR": str(tmp_path),
        "COMPANY_OS_DB_URL": "must-not-inherit", "DATABASE_URL": "must-not-inherit", "DB_URL": "must-not-inherit",
        "COMPANY_OS_WORKING_MEMORY_PATH": "must-not-inherit"}


def run_ci(tree, *arguments):
    root, environment = tree
    result = subprocess.run(["bash", str(root / "scripts/local_ci.sh"), *arguments],
        env=environment, capture_output=True, text=True, timeout=30, check=False)
    calls_path = root / "calls.jsonl"
    calls = [json.loads(line) for line in calls_path.read_text().splitlines()] if calls_path.exists() else []
    return result, calls


def test_local_ci_runs_one_process_per_file_in_both_modes_without_git(plain_tree):
    result, calls = run_ci(plain_tree)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "2 parallel workers" in result.stdout
    units = [call for call in calls if call["kind"] == "unit"]
    assert sorted(call["mode"] for call in units) == ["local", "local", "unset", "unset"]
    assert len({call["data_path"] for call in units}) == 4
    for call in units:
        assert call["flags"] == ["deterministic_only", "1", "replay"]
        assert ("not hosted" in call["arguments"]) is (call["mode"] == "local")
        assert not call["database_overrides"]
        assert call["data_path"].startswith(str(Path(result.stdout.split("Logs: ")[-1].strip())))
        assert call["database_path"] == "sqlite:///" + str(Path(call["data_path"]) / "app.db")
        assert not call["memory_override"]
        targets = [argument for argument in call["arguments"] if argument.endswith(".py")]
        assert len(targets) == 1 and Path(targets[0]).is_absolute()
    assert [call["kind"] for call in calls if call["kind"] != "unit"] == ["ruff", "console", "tsc"]
    assert not (plain_tree[0] / ".git").exists()


def test_public_ci_runs_only_local_checks(plain_tree):
    (plain_tree[0] / "services/company_os/app/admin/router.py").unlink()
    result, calls = run_ci(plain_tree)
    assert result.returncode == 0, result.stdout + result.stderr
    assert [call["mode"] for call in calls if call["kind"] == "unit"] == ["local", "local"]
    assert not {"services/ai_proxy", "services/egress_proxy", "services/sandbox", "packages/harness"}.intersection(calls[0]["arguments"])


@pytest.mark.parametrize("failure", ["unit_local", "unit_unset", "ruff", "console", "tsc"])
def test_local_ci_reports_failure_without_skipping_remaining_checks(plain_tree, failure):
    plain_tree[1]["STUB_FAIL"] = failure
    result, calls = run_ci(plain_tree, "2")
    assert result.returncode == 1
    assert "FAILED" in result.stdout
    assert len(calls) == 7
    assert "local: 2 files" in result.stdout and "unset: 2 files" in result.stdout
    assert {call["kind"] for call in calls} == {"ruff", "unit", "console", "tsc"}


@pytest.mark.parametrize(("mode", "status"), [("local", 0), ("unset", 1)])
def test_local_ci_accepts_deselection_only_in_local_mode(plain_tree, mode, status):
    plain_tree[1]["STUB_FAIL"] = f"empty_{mode}"
    result, calls = run_ci(plain_tree, "2")
    assert result.returncode == status, result.stdout + result.stderr
    assert len(calls) == 7


@pytest.mark.parametrize("arguments", [("0",), ("-1",), ("invalid",), ("2", "3")])
def test_local_ci_rejects_invalid_worker_counts(plain_tree, arguments):
    result, calls = run_ci(plain_tree, *arguments)
    assert result.returncode == 2
    assert "positive worker count" in result.stderr
    assert calls == []


@pytest.mark.parametrize("mode,skipped", [("local", True), (" LOCAL ", True), ("hosted", False), (None, False)])
def test_hosted_only_skips_only_explicit_hosted_contracts(monkeypatch, mode, skipped):
    spec = importlib.util.spec_from_file_location("triage_conftest", ROOT / "tests/conftest.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if mode is None:
        monkeypatch.delenv("SUPYMEM_MODE", raising=False)
    else:
        monkeypatch.setenv("SUPYMEM_MODE", mode)
    hosted_marks, local_marks = [], []
    hosted = SimpleNamespace(path=ROOT / "tests/unit/example.py", originalname="test_hosted",
        get_closest_marker=lambda name: name == "hosted_only", add_marker=hosted_marks.append)
    local = SimpleNamespace(path=ROOT / "tests/unit/example.py", originalname="test_local",
        get_closest_marker=lambda name: None, add_marker=local_marks.append)
    module.pytest_collection_modifyitems([hosted, local])
    assert [mark.name for mark in hosted_marks] == (["hosted", "skip"] if skipped else ["hosted"])
    assert not local_marks


def test_public_ci_local_units_and_desktop_check_are_blocking_without_private_secrets():
    text = (ROOT / ".github/workflows/public-ci.yml").read_text()
    workflow = yaml.safe_load(text)
    python = workflow["jobs"]["python"]
    desktop = workflow["jobs"]["desktop"]
    assert python["env"]["SUPYMEM_MODE"] == "local"
    unit = next(step for step in python["steps"] if ' -m "not hosted" ' in step.get("run", ""))
    assert unit["env"]["COMPANY_OS_DB_URL"].startswith("sqlite:///")
    splits = int(unit["run"].split("--splits ", 1)[1].split()[0])
    assert python["strategy"]["matrix"]["shard"] == list(range(1, splits + 1))
    check = next(step for step in desktop["steps"] if "cargo check" in step.get("run", ""))
    assert check["working-directory"] == "src-tauri"
    assert not python.get("continue-on-error", False)
    assert not desktop.get("continue-on-error", False)
    assert "if" not in desktop
    assert "secrets." not in text and workflow["permissions"] == {"contents": "read"}


@pytest.mark.parametrize("domain", ["dispatch", "recipes"], ids=["b2b", "consumer"])
def test_pristine_restore_clears_prior_rate_usage_without_disabling_limits(domain):
    from core.ratelimit import RateLimiter
    from fastapi import HTTPException
    spec = importlib.util.spec_from_file_location("triage_harness", ROOT / "tests/company_os_harness.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    limiter = RateLimiter(redis_url="", clock=lambda: 1)
    restorers = []
    module._capture(limiter, set(), restorers)
    limiter.check(domain, [(1, 60)])
    with pytest.raises(HTTPException) as exhausted:
        limiter.check(domain, [(1, 60)])
    assert exhausted.value.status_code == 429
    for restore in restorers:
        restore()
    limiter.check(domain, [(1, 60)])
    with pytest.raises(HTTPException):
        limiter.check(domain, [(1, 60)])


@pytest.mark.parametrize("domain", ["warehouse", "garden"], ids=["b2b", "consumer"])
def test_local_entry_client_supplies_browser_origin_without_bypassing_boundary(monkeypatch, tmp_path, domain):
    from core.local_security import _proxy_secret, check_request, console_origin
    from fastapi import HTTPException
    from httpx import Headers
    from starlette.requests import HTTPConnection

    spec = importlib.util.spec_from_file_location("triage_conftest", ROOT / "tests/conftest.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path / domain))
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/client.db")
    from tests.company_os_harness import load_company_os_app

    load_company_os_app()
    from app.core.local_owner import _ensure_proxy_secret

    _ensure_proxy_secret()
    client = SimpleNamespace(headers=Headers(), base_url=console_origin() + "/")
    fixture = SimpleNamespace(__module__="tests.integration.test_entry_chat_api")
    initialized = []
    request = SimpleNamespace(module=SimpleNamespace(client=fixture), fixturenames=["_isolated_local_setup"],
        getfixturevalue=lambda name: initialized.append(name) or client)
    module._local_entry_client_origin.__wrapped__(request, monkeypatch)
    assert initialized == ["_isolated_local_setup", "client"]
    from urllib.parse import urlsplit

    headers = [(b"host", urlsplit(client.base_url).netloc.encode()), (b"content-type", b"multipart/form-data"),
        (b"x-local-proxy-secret", _proxy_secret())]
    scope = {"type": "http", "scheme": "http", "method": "POST", "path": f"/{domain}",
        "client": ("127.0.0.1", 50000), "headers": headers}
    with pytest.raises(HTTPException) as missing:
        check_request(HTTPConnection(scope))
    assert missing.value.status_code == 403
    check_request(HTTPConnection({**scope, "headers": headers + [(b"origin", client.headers["Origin"].encode())]}))


@pytest.mark.parametrize("domain", ["inventory", "birdwatching"], ids=["b2b", "consumer"])
def test_sqlite_templates_do_not_cross_local_and_hosted_migration_modes(tmp_path, monkeypatch, domain):
    import sqlite3

    from tests.company_os_harness import ROOT as product_root

    spec = importlib.util.spec_from_file_location("triage_harness", ROOT / "tests/company_os_harness.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.ROOT = product_root
    module.COMPANY_OS = product_root / "services/company_os"
    monkeypatch.setenv("COS_TEST_APP_REUSE", "1")
    monkeypatch.setenv("COMPANY_OS_DEPLOYMENT_CONFIG", str(product_root / "config/deployment.example.json"))
    monkeypatch.setenv("CHANNELS_DRY_RUN", "true")
    for index, mode in enumerate(("hosted", "local", "hosted")):
        data = tmp_path / domain / str(index)
        database = data / "app.db"
        monkeypatch.setenv("SUPYMEM_MODE", mode)
        monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(data))
        monkeypatch.setenv("COMPANY_OS_WORKING_MEMORY_PATH", str(data / "memory.db"))
        monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{database}")
        module.load_company_os_app()
        from app import state

        state.db()
        with sqlite3.connect(database) as connection:
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            assert "ventures" in tables
            assert ("alembic_version" in tables) is (mode == "local")


@pytest.mark.parametrize("domain", ["wholesale orders", "garden planning"], ids=["b2b", "consumer"])
def test_wire_fixture_installs_real_boundary_and_still_refuses_secrets(tmp_path, monkeypatch, domain):
    from llm_gateway.boundary import BoundaryViolation, resolve_filter

    spec = importlib.util.spec_from_file_location("triage_conftest", ROOT / "tests/conftest.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    request = SimpleNamespace(module=SimpleNamespace(__file__=str(ROOT / "tests/unit/llm/test_responses_wire_usage.py")))
    module._wire_usage_boundary.__wrapped__(request, tmp_path, monkeypatch)
    boundary = resolve_filter()
    decision = boundary([{"role": "user", "content": domain}], purpose="build.model",
        workspace_id=None, provider="openai:model")
    assert decision.messages == [{"role": "user", "content": domain}]
    with pytest.raises(BoundaryViolation):
        boundary([{"role": "user", "content": domain + " sk_live_" + "b" * 24}],
            purpose="build.model", workspace_id=None, provider="openai:model")


@pytest.mark.parametrize("domain", ["shipment planning", "pet training"], ids=["b2b", "consumer"])
def test_local_paid_search_fixture_selects_mock_transport_without_skipping_pricing(tmp_path, monkeypatch, domain):
    import asyncio

    from tests.company_os_harness import load_company_os_app

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COMPANY_OS_WORKING_MEMORY_PATH", str(tmp_path / "memory.db"))
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/search.db")
    load_company_os_app()
    from app.credits.errors import CreditsUnavailable
    from app.research import local_providers

    async def forbidden(query):
        raise AssertionError("Unpriced transport must not run")

    provider = SimpleNamespace(name="unknown-provider", search=forbidden)
    paths = SimpleNamespace(explore=SimpleNamespace(_provider_from_env=lambda: provider))
    initialized = []
    request = SimpleNamespace(module=SimpleNamespace(__file__=str(ROOT / "tests/unit/research/test_paid_search_paths.py")),
        getfixturevalue=lambda name: initialized.append(name) or paths)
    spec = importlib.util.spec_from_file_location("triage_conftest", ROOT / "tests/conftest.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module._local_paid_search_provider.__wrapped__(request, monkeypatch)
    assert initialized == ["paths"]
    assert local_providers.search_provider() is provider
    with pytest.raises(CreditsUnavailable):
        asyncio.run(local_providers.search(provider, domain))
