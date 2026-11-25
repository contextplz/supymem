from __future__ import annotations

import importlib.util
import io
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location("local_build_journey", ROOT / "scripts/local_build_journey.py")
journey = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(journey)


DOMAINS = (
    "A maintenance scheduling service for warehouse supervisors",
    "A neighborhood toy exchange for parents",
)


@pytest.mark.parametrize("private", DOMAINS)
@pytest.mark.parametrize("raised", [True, False])
def test_local_build_failures_leave_scrubbed_file(tmp_path, monkeypatch, private, raised):
    from tests.company_os_harness import load_company_os_app
    load_company_os_app()
    from desktop_runtime.device_worker import DeviceWorker
    import threading

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    worker = DeviceWorker.__new__(DeviceWorker)
    worker.last = {"failure_code": "coding_git_checkpoint_failed"}
    worker.cancelled = threading.Event()
    worker.gateway = SimpleNamespace(claim_rejection=None)
    worker._runtime_diagnostics = SimpleNamespace(snapshot=lambda code: {})
    monkeypatch.setattr("desktop_runtime.device_worker.report_error", lambda *args, **kwargs: None)

    def run():
        if raised:
            raise RuntimeError(private)
        return "failed"

    worker._run = run
    if raised:
        with pytest.raises(RuntimeError, match=private):
            worker._run_with_reporting()
    else:
        assert worker._run_with_reporting() == "failed"
    path = tmp_path / "logs/errors.jsonl"
    assert path.exists()
    entry, = [json.loads(line) for line in path.read_text().splitlines()]
    assert entry["error_type"] == "RuntimeError"
    assert entry["code"] == ("native_execution_failed" if raised else "coding_git_checkpoint_failed")
    assert private not in path.read_text() and str(tmp_path) not in path.read_text()


@pytest.fixture(autouse=True)
def coding_agent_probes():
    from desktop_runtime.agent_status import start_probes, stop_probes

    start_probes()
    yield
    stop_probes()


@pytest.mark.parametrize("idea", DOMAINS)
@pytest.mark.usefixtures("private_coding_install")
@pytest.mark.parametrize("claim_confirmed", [True, False])
def test_darwin_bridge_starts_coding_without_computer_use_setup(tmp_path, monkeypatch, idea, claim_confirmed):
    import time
    from datetime import datetime, timezone
    import httpx
    from fastapi.testclient import TestClient
    from tests.company_os_harness import load_company_os_app

    load_company_os_app()
    from app.build import device_orders
    from app.core import local_owner
    from core import local_security
    from desktop_runtime import coding_loop, device_worker, owned_coding
    from desktop_runtime.bridge import create_bridge_app

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("COMPANY_OS_URL", "http://127.0.0.1:43123")
    monkeypatch.setenv("BRIDGE_AUTH_OWNER", "native")
    monkeypatch.setenv("BRIDGE_NATIVE_TOKEN", "native-test-" * 4)
    monkeypatch.setattr(local_owner, "ensure_owner", lambda: SimpleNamespace(id="per_owner", email=""))
    monkeypatch.setattr(local_owner, "_ensure_proxy_secret", lambda: None)
    monkeypatch.setattr(device_orders, "device_caller", lambda *_: SimpleNamespace(device_id="dev_owned"))
    journey.install_fake_cli(tmp_path / "bin", "codex")
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}:{os.environ.get('PATH', '')}")
    from desktop_runtime.agent_status import pin_provider
    from tests.coding_tool_fixtures import private_test_install
    private_test_install(monkeypatch, tmp_path / "bin")
    pin_provider("codex")
    monkeypatch.setattr("desktop_runtime.device_gateway.build_capability_ready", lambda: True)
    manifest = {"instruction": idea, "applications": ["com.companyos.app"], "origins": [],
        "actions": ["editor.edit", "terminal.execute"], "credit_ceiling_micro": 0, "duration_seconds": 60,
        "coding_loop_verification": {"operation": "workspace_loop_v1", "provider": "codex",
            "profile": "web_app_vercel_v1", "check_recipe": "web_app_vercel_checks_v1",
            "workspace_slug": "owned-app", "phase": "plan", "network": "none",
            "read_scope": "venture_workspace", "write_scope": "venture_workspace"},
        "app_navigation_verification": {"operation": "stale_native_navigation"}}
    saved = {"id": "dwo_owned", "venture_id": "ven_owned", "workspace_id": "wsp_owned",
        "actor_id": "per_owner", "device_id": "dev_owned", "review_digest": "b" * 64,
        "state": "approved", "manifest": manifest, "task_snapshot": {"artifact_profile": "web_app_vercel_v1"}}
    requests = []

    def request(incoming):
        requests.append((incoming.method, incoming.url.path))
        assert incoming.headers["x-local-proxy-secret"] == "a" * 64
        if incoming.method == "GET":
            if incoming.url.path.endswith("/authority"):
                return httpx.Response(200, json={"current": saved["state"] == "claimed"})
            assert incoming.url.path.endswith("/dwo_owned")
        elif incoming.url.path.endswith("/claim"):
            saved.update(state="claimed", execution_id="dex_owned", claimed_at=datetime.now(timezone.utc).isoformat())
            if not claim_confirmed:
                raise httpx.ReadTimeout("unconfirmed")
        elif incoming.url.path.endswith("/stop-receipt"):
            saved.update(state="stopped")
        elif incoming.url.path.endswith("/heartbeat"):
            assert saved["state"] == "claimed"
        else:
            pytest.fail("Coding requested a computer-use effect or unexpected endpoint")
        return httpx.Response(200, json=saved)

    def run_cli(worker, reviewed, gateway, coding, deadline):
        assert worker.cua_socket is None and worker.cua_protocol is None
        coding.loop_mode = True
        result = coding.run(idea, deadline)
        assert result["outcome"] == "pass"
        return {"outcome": "plan_ready", "task_verified": False}

    def refuse_computer_use(*args, **kwargs):
        pytest.fail("Your coding order entered computer use")

    monkeypatch.setattr(coding_loop, "run_coding_loop", run_cli)
    monkeypatch.setattr(device_worker.DeviceWorker, "run_app_navigation", refuse_computer_use)
    monkeypatch.setattr(device_worker.DeviceWorker, "admit", refuse_computer_use)
    monkeypatch.setattr(local_security, "_proxy_secret", lambda: b"a" * 64)
    monkeypatch.setattr(owned_coding, "_mac_has_acl", lambda path: False)
    app = create_bridge_app(transport=httpx.MockTransport(request))
    with TestClient(app) as client:
        session = app.state.bridge_session
        session.account_id, session.device_id, session.access_token = "per_owner", "dev_owned", "a" * 64
        session.connected = True
        session.expires_at = time.time() + 3600
        monkeypatch.setattr(sys, "platform", "darwin")
        response = client.post("/native/device-work-orders/ven_owned/dwo_owned/start", json={}, headers={
            "x-native-token": "native-test-" * 4,
            "x-supymem-cua-socket": "stale-driver.sock", "x-supymem-cua-host-pid": "invalid",
            "x-supymem-cua-protocol": "stale-protocol"})
        assert response.status_code == 200, response.text
        thread = app.state.local_active_worker["thread"]
        thread.join(timeout=10)
        assert not thread.is_alive()
        worker = app.state.local_active_worker["worker"]
        assert worker.last["state"] == "stopped", worker.last
        if claim_confirmed:
            assert worker.last.get("failure_code") is None
        else:
            assert worker.last["failure_code"] == "native_worker_request_unconfirmed"
            assert "Build activity" in worker.last["message"]
            assert "restart your app" in worker.last["message"]
    runs = journey.cli_runs(tmp_path / "bin")
    assert len(runs) == int(claim_confirmed)
    if claim_confirmed:
        assert runs[0]["provider"] == "codex"
    assert ("POST", "/ventures/ven_owned/device-work-orders/dwo_owned/claim") in requests


@pytest.mark.parametrize("root_kind", ["temp", "data", "home", "data_space"])
@pytest.mark.parametrize("fixture_number", [1, 2])
def test_landing_build_check_preview_and_export_through_root_alias(tmp_path, monkeypatch, root_kind, fixture_number):
    import tempfile
    from tests.company_os_harness import load_company_os_app

    load_company_os_app()
    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / ("Application Support" if root_kind == "data_space" else "alias")
    alias.symlink_to(real, target_is_directory=True)
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("HOME", str(alias if root_kind == "home" else tmp_path))
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(alias if root_kind.startswith("data") else tmp_path))
    monkeypatch.setenv("TMPDIR", str(alias if root_kind == "temp" else tmp_path))
    monkeypatch.setattr(tempfile, "tempdir", None)
    temporary_directory = tempfile.TemporaryDirectory

    def temporary(*args, **kwargs):
        if kwargs.get("prefix") == "landing-timing-":
            kwargs["dir"] = alias
        return temporary_directory(*args, **kwargs)

    monkeypatch.setattr(tempfile, "TemporaryDirectory", temporary)
    fixture = json.loads((ROOT / f"tests/unit/pagebuilder/fixtures/local_tool_pages/{fixture_number}.json").read_text())
    result = journey.offline_landing_timing(fixture, "landing_slow")
    assert result["state"] == "ready"
    assert result["files"] > 1


@pytest.mark.parametrize("name,idea", [
    ("Mill Ledger", "A calibration log for independent dental laboratories that keeps a record of daily checks on "
     "milling machines, lets technicians attach readings to each machine, and gives shift leads a single place to review recurring adjustments."),
    ("Trip Tuck", "A printable trip planner for families with toddlers, with room to record stroller-friendly routes, "
     "nap breaks, indoor alternatives for rainy days, and the notes each caregiver wants to share before leaving home."),
], ids=["b2b", "consumer"])
@pytest.mark.parametrize("provider", ["codex", "opencode"])
def test_gate_fake_exports_landing_with_a_long_confirmed_idea(tmp_path, monkeypatch, name, idea, provider):
    from tests.company_os_harness import load_company_os_app

    load_company_os_app()
    from app.pagebuilder import tool
    from app.pagebuilder.local import page_check_message
    from app.pagebuilder.models import PageInput
    from desktop_runtime.landing_loop import public_files
    from local_value_journey import prepare_fake_coding_tool

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("SUPYMEM_TEST_FAKE_CODING_TOOL", "1")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path / "data"))
    fixture = json.loads((ROOT / "tests/unit/pagebuilder/fixtures/local_tool_pages/1.json").read_text())
    inputs = PageInput(venture_id="ven_long_idea", name=name, idea=idea,
        waitlist_url="https://signup.example.test/public/waitlists/ven_long_idea")
    bin_dir = tmp_path / "bin"
    if provider == "codex":
        prepare_fake_coding_tool(bin_dir, [True])
    else:
        journey.install_fake_cli(bin_dir, provider)
    (bin_dir / "page-fixture.json").write_text(json.dumps({"root": str(ROOT), "template": fixture}))
    workspace = tmp_path / "page"
    workspace.mkdir()
    argv = ["exec", "--cd", str(workspace)] if provider == "codex" else ["run", "--format", "json"]
    subprocess.run([str(bin_dir / provider), *argv, tool.build_brief(inputs, None)],
        cwd=workspace, capture_output=True, check=True, timeout=30)
    files = public_files(workspace)
    checked = tool.check_output(files, inputs, None)
    assert checked["passed"], page_check_message(checked["failures"])
    hero = json.loads(files["page-plan.txt"])["sections"][0]
    assert 3 <= len(hero["headline"]) <= 140 < len(idea)
    assert hero["body"] == idea
    assert tool.bundle(files, inputs, None, {"passed": True})["source_zip"]


@pytest.mark.parametrize("idea", DOMAINS)
def test_opencode_requires_a_pin_and_verified_permissions_before_launch(tmp_path, monkeypatch, idea):
    import time
    from fnmatch import fnmatchcase

    from desktop_runtime import agent_status, loop_diagnose
    from desktop_runtime.owned_coding import OwnedCoding, coding_failure_details
    from desktop_runtime.owned_processes import OwnedProcesses
    from tests.coding_tool_fixtures import private_test_install

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("HOME", str(tmp_path))
    global_config = tmp_path / ".config/opencode/opencode.json"
    global_config.parent.mkdir(parents=True)
    original = '{"permission":{"*":"allow"}}'
    global_config.write_text(original)
    binary = journey.install_fake_cli(tmp_path / "bin", "opencode")
    private_test_install(monkeypatch, binary.parent)
    monkeypatch.setenv("PATH", str(binary.parent))
    probe = agent_status._run_probe
    broken = True

    def inspect_policy(binary, argv, tracker, cwd, env, expected):
        code, output = probe(binary, argv, tracker, cwd, env, expected)
        if argv != ["debug", "config"]:
            return code, output
        path = Path(env["OPENCODE_CONFIG"])
        assert path.is_relative_to(cwd / ".tmp") and path != global_config
        assert env["OPENCODE_DISABLE_PROJECT_CONFIG"] == "true"
        assert Path(env["XDG_CONFIG_HOME"]).is_relative_to(cwd / ".tmp")
        config = json.loads(path.read_bytes())
        policy = config["agent"]["supymem-build"]["permission"]
        assert config["enabled_providers"] == ["openai"] and config["plugin"] == [] and config["mcp"] == {}
        assert all(policy[name] == "deny" for name in ("external_directory", "bash", "webfetch", "websearch"))
        assert config["permission"]["external_directory"] == "deny"
        for name in ("read", "edit", "write", "apply_patch"):
            assert [rule for pattern, rule in policy[name].items() if fnmatchcase("app/main.html", pattern)][-1] == "allow"
            assert [rule for pattern, rule in policy[name].items() if fnmatchcase(f"{cwd}/.git/config", pattern)][-1] == "deny"
        if broken:
            policy["external_directory"] = "allow"
            return code, json.dumps(config).encode()
        return code, output

    monkeypatch.setattr(agent_status, "_run_probe", inspect_policy)
    tracker = OwnedProcesses()
    coding = OwnedCoding(venture_id="ven_opencode", workspace_slug="opencode", provider="opencode",
        tracker=tracker, authorize=lambda: True)
    try:
        with pytest.raises(ValueError, match="coding_tool_not_connected"):
            coding.preflight()
        candidate = agent_status._status("opencode")["candidate"]
        assert agent_status.pin_provider("opencode", candidate=candidate) == agent_status.launch_identity(binary)
        with pytest.raises(ValueError, match="coding_sandbox_denied") as failure:
            coding.run(idea, time.monotonic() + 15)
        assert "Check connection in Settings > Keys and connections > Coding tools" in coding_failure_details(
            failure.value, coding.stage, provider="opencode")["message"]
        coding.loop_mode = True
        blocked = coding.run(idea, time.monotonic() + 15)
        diagnosis = loop_diagnose.diagnose({}, blocked, {}, [])
        assert diagnosis["outcome"] == "sandbox_denied" and diagnosis["next_action"] == "stop_error"
        assert "Check connection in Settings > Keys and connections > Coding tools" in diagnosis["summary"]
        assert not journey.cli_runs(binary.parent)
        broken = False
        coding.loop_mode = False
        result = coding.run(idea, time.monotonic() + 15)
        assert result["provider_version"] == "opencode 2.2.5" and result["artifact_verified"]
        runs = journey.cli_runs(binary.parent)
        assert len(runs) == 1 and runs[0]["provider"] == "opencode"
        workspace = Path(runs[0]["workspace"])
        assert (workspace / "index.html").is_file() and not list((workspace / ".tmp").glob("opencode-*"))
        assert global_config.read_text() == original
    finally:
        tracker.stop_all()
        tracker.close()


@pytest.mark.parametrize("provider", ["claude", "codex", "opencode"])
@pytest.mark.parametrize("idea", DOMAINS)
def test_subscription_cli_runs_in_given_folder_and_stops(tmp_path, provider, idea):
    binary = journey.install_fake_cli(tmp_path / "bin", provider)
    version = subprocess.run([str(binary), "--version"], capture_output=True, text=True, check=True)
    assert {"claude": "2.1.284", "codex": "codex-cli 0.163.0", "opencode": "2.2.5"}[provider] in version.stdout
    arguments = {"claude": ["auth", "status"], "codex": ["login", "status"], "opencode": ["auth", "list"]}[provider]
    status = subprocess.run([str(binary), *arguments], capture_output=True, text=True, check=True)
    if provider == "claude":
        assert json.loads(status.stdout) == {"loggedIn": True, "authMethod": "claude.ai"}
    elif provider == "codex":
        assert status.stderr.strip() == "Logged in using ChatGPT"
    else:
        assert "OpenAI oauth" in status.stdout
    workspace = tmp_path / "result"
    workspace.mkdir()
    command = [str(binary), *{"claude": ["-p"], "codex": ["exec", "--cd", str(workspace)],
                              "opencode": ["run", "--format", "json"]}[provider], idea]
    result = subprocess.run(command, cwd=workspace if provider != "codex" else tmp_path,
                            capture_output=True, text=True, check=True)
    assert journey.MARKER in (workspace / "index.html").read_text()
    assert (workspace / "README.md").is_file()
    assert len(result.stdout.splitlines()) >= 3
    (binary.parent / "mode").write_text("hang")
    process = subprocess.Popen(command, cwd=workspace, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        journey.wait_for(lambda: len(journey.cli_runs(binary.parent)) == 2, 5, "Your fake tool did not start.")
        assert process.poll() is None
        process.terminate()
        process.wait(timeout=5)
        assert not journey.process_alive(process.pid)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


@pytest.mark.parametrize("provider", ["claude", "codex"])
@pytest.mark.parametrize("idea", DOMAINS)
def test_subscription_cli_plan_fixture_respects_the_plan_only_contract(tmp_path, monkeypatch, provider, idea):
    from desktop_runtime import loop_checks, loop_prompts

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    binary = journey.install_fake_cli(tmp_path / "bin", provider)
    workspace = tmp_path / "result"
    workspace.mkdir()
    prompt = loop_prompts.plan_prompt(idea, {"idea_summary": idea})
    command = [str(binary), "-p", prompt] if provider == "claude" else [str(binary), "exec", "--cd", str(workspace), prompt]
    subprocess.run(command, cwd=workspace, capture_output=True, text=True, check=True)
    plan = loop_prompts.validate_plan((workspace / "review/spec.md").read_text(),
                                     json.loads((workspace / "review/tasks.json").read_text()))
    assert idea in plan["spec"]
    assert loop_checks.http_journey(plan)
    assert {str(path.relative_to(workspace)) for path in workspace.rglob("*") if path.is_file()} == {
        "review/spec.md", "review/tasks.json"}


@pytest.mark.parametrize("provider", ["claude", "codex"])
@pytest.mark.parametrize("idea", DOMAINS)
def test_subscription_cli_implements_the_reviewed_plan_without_rewriting_it(tmp_path, monkeypatch, provider, idea):
    from desktop_runtime import loop_checks, loop_prompts

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    binary = journey.install_fake_cli(tmp_path / "bin", provider)
    workspace = tmp_path / "result"
    workspace.mkdir()
    def execute(prompt):
        command = [str(binary), "-p", prompt] if provider == "claude" else [str(binary), "exec", "--cd", str(workspace), prompt]
        return subprocess.run(command, cwd=workspace, capture_output=True, text=True, check=True)
    execute(loop_prompts.plan_prompt(idea, {"idea_summary": idea}))
    original = {name: (workspace / name).read_bytes() for name in ("review/spec.md", "review/tasks.json")}
    plan = loop_prompts.validate_plan(original["review/spec.md"].decode(), json.loads(original["review/tasks.json"]))
    execute(loop_prompts.task_prompt(plan["tasks"][0], plan))
    assert all((workspace / name).read_bytes() == body for name, body in original.items())
    required = {*plan["tasks"][0]["files"], *(check["test_file"] for check in plan["tasks"][0]["acceptance"])}
    assert all((workspace / name).is_file() for name in required)
    assert loop_checks.source_manifest(workspace)["file_manifest_sha256"]
    checked = subprocess.run([loop_checks.node_binary(), "--test", "--test-reporter=tap", "tests/project.test.js"],
                             cwd=workspace, capture_output=True, check=True)
    assert loop_checks.parse_tap(checked.stdout, workspace)["tap_summary"] == {"pass": 1, "fail": 0}


def test_failures_are_live_findings_and_secrets_are_redacted(tmp_path, capsys):
    findings = tmp_path / "LIVE_FINDINGS.md"
    report = journey.Report(findings, ["private-model-value"])
    assert not report.step("coding tool", lambda: journey.require(False, "private-model-value: missing setting"),
                           "Open Settings > Keys and connections > Coding tools, then retry.")
    assert report.failures == 1
    assert "LIVE" in findings.read_text() and "FAIL coding tool" in findings.read_text()
    assert "private-model-value" not in findings.read_text() + capsys.readouterr().out


@pytest.mark.parametrize("fixture_number", [1, 2])
@pytest.mark.parametrize("failed", [False, True])
def test_fake_landing_tool_sends_live_activity_and_checked_failures_to_core(tmp_path, monkeypatch, fixture_number, failed):
    from datetime import datetime, timezone
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import sessionmaker
    from tests.company_os_harness import load_company_os_app
    from desktop_runtime import landing_loop
    from desktop_runtime.device_gateway import WorkOrderGateway

    load_company_os_app()
    from app.api import device_orders_api
    from app.build import device_orders
    from app.db.models import Base, VentureDocument
    from app.db.supymem_models import BuildEvent
    from app.pagebuilder import tool
    from app import state

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    fixture = json.loads((ROOT / f"tests/unit/pagebuilder/fixtures/local_tool_pages/{fixture_number}.json").read_text())
    venture_id = fixture["input"]["venture_id"]
    owner = SimpleNamespace(sub="owner", device_id="dev_owned")
    engine = create_engine(f"sqlite:///{tmp_path}/activity.sqlite")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine)
    monkeypatch.setattr(device_orders, "db", lambda: factory)
    monkeypatch.setattr(state, "db", lambda: factory)
    monkeypatch.setattr(device_orders, "_scope", lambda venture, caller: ("workspace", caller.sub, caller.device_id))
    monkeypatch.setattr(device_orders, "authorize", lambda *args, **kwargs: None)
    monkeypatch.setattr(device_orders_api, "verify_caller", lambda *args, **kwargs: owner)
    order = {"id": "dwo_timing", "venture_id": venture_id, "workspace_id": "workspace", "actor_id": owner.sub,
        "device_id": owner.device_id, "state": "claimed", "claimed_at": datetime.now(timezone.utc).isoformat(),
        "execution_id": "dex_owned", "expires_at": "2099-01-01T00:00:00+00:00", "task_snapshot": {},
        "manifest": {"coding_loop_verification": {"phase": "implement", "max_iterations": 2, "max_wall_seconds": 1800}}}
    order["review_digest"] = device_orders._digest(order)
    with factory() as session:
        session.add(VentureDocument(id=order["id"], venture_id=venture_id, kind=device_orders.KIND,
            title=fixture["input"]["idea"], provenance_json=json.dumps(order), created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc)))
        session.commit()
    app = FastAPI()
    app.include_router(device_orders_api.router)
    observed, captured = [], {}
    original_run = landing_loop.run
    if failed:
        monkeypatch.setattr(tool, "check_output", lambda *args: {"passed": False,
            "failures": ["pagebuilder_plan_copy_rejected:" + json.dumps([
                {"field_id": "sections[0].body[0]", "reason": "Your offer contains an unsupported claim."}])]})
    with TestClient(app) as client:
        def request(method, path, payload=None):
            response = client.request(method, path.removeprefix("/api"), json=payload)
            assert response.status_code == 200, response.text
            return response.json()
        gateway = WorkOrderGateway(venture_id=venture_id, order_id=order["id"], device_id=owner.device_id, request=request)
        gateway.execution_id, gateway.claimed = order["execution_id"], True
        def run(worker, manifest, unused_gateway, coding, deadline):
            send_event = gateway.coding_event
            def event(row):
                send_event(row)
                with factory() as session:
                    stored = session.scalars(select(BuildEvent).order_by(BuildEvent.seq)).all()
                    observed.append((json.loads(stored[-1].payload)["text"], len(worker.last.get("iteration_receipts") or [])))
            gateway.coding_event = event
            captured["worker"] = worker
            return original_run(worker, manifest, gateway, coding, deadline)
        monkeypatch.setattr(landing_loop, "run", run)
        if failed:
            with pytest.raises(Exception, match="Your saved page did not pass"):
                journey.offline_landing_timing(fixture, "landing_slow")
        else:
            journey.offline_landing_timing(fixture, "landing_slow")
        assert any(text == "Building your page" and rounds == 0 for text, rounds in observed)
        assert any(text.startswith("Saved ") and rounds == 0 for text, rounds in observed)
        saved = request("GET", gateway.path + "/events")
        assert len(saved["events"]) == len(observed)
        last = captured["worker"].last
        packet = {**last["events"][-1], "seq": last["activity_seq"], "execution_id": gateway.execution_id}
        duplicate = client.post((gateway.path + "/events").removeprefix("/api"), json=packet)
        assert duplicate.status_code == 200
        assert len(request("GET", gateway.path + "/events")["events"]) == len(observed)
        conflict = client.post((gateway.path + "/events").removeprefix("/api"), json={**packet, "text": "Changed update"})
        assert conflict.status_code == 409
        assert "Refresh build" in conflict.json()["detail"]["message"]
        assert "device_coding_" not in conflict.json()["detail"]["message"]
        wrong_execution = client.post((gateway.path + "/events").removeprefix("/api"), json={**packet, "execution_id": "dex_other"})
        assert wrong_execution.status_code == 409
        from desktop_runtime import coding_loop
        frames = ["core.artifact_policy:1", "contextlib:1"]
        coding_loop._event(captured["worker"], "Your file check stopped. Open Build activity, then ask Charlie to retry.",
            None, exception_frames=[*frames, "/Users/private:1"])
        checked_activity = request("GET", gateway.path + "/events")["events"][-1]
        assert checked_activity["exception_frames"] == frames
        owner.device_id = "dev_other"
        assert client.get((gateway.path + "/events").removeprefix("/api")).status_code == 404
        owner.device_id = "dev_owned"
        if failed:
            assert any("unsupported claim" in event["text"] for event in saved["events"])
            assert "unsupported claim" in captured["worker"].last["message"]
            assert "Website" in captured["worker"].last["message"]
            assert "pagebuilder_" not in json.dumps(saved)
            iterations = request("GET", gateway.path + "/iterations")["iterations"]
            assert iterations and all(row["diagnosis"]["failures"] for row in iterations)
            assert len(iterations) == len(last["iteration_receipts"])
            assert all(row["posted"] for row in last["iteration_receipts"])
            view = tool.progress(SimpleNamespace(venture_id=venture_id, workspace_id="workspace"),
                {"_actor_subject": owner.sub, "_runtime": {"state": "failed"}, "tool_build": {"order_id": order["id"]}})
            assert "unsupported claim" in view["error_reason"] and "Website" in view["error_reason"]
        from core import logging as core_logging
        monkeypatch.setattr(core_logging, "_local_secret_values", lambda: ["private-saved-fixture"])
        private = request("POST", gateway.path + "/events", {"seq": last["activity_seq"] + 1,
            "at": packet["at"], "execution_id": gateway.execution_id, "text": "Your tool reported private-saved-fixture"})
        assert "private-saved-fixture" not in json.dumps(private)
        with factory() as session:
            stored = session.scalars(select(BuildEvent).order_by(BuildEvent.seq)).all()
            assert all("private-saved-fixture" not in event.payload for event in stored)
    engine.dispose()


@pytest.mark.parametrize("idea", DOMAINS)
@pytest.mark.parametrize("failure_code", [None, "native_execution_failed", "pagebuilder_capability_grounding_rejected"])
def test_build_waits_for_admission_and_reports_worker_failure(tmp_path, monkeypatch, idea, failure_code):
    fake = tmp_path / "bin"
    fake.mkdir()
    requests = []
    order_reads = 0
    started = False

    class Client:
        def request(self, method, path, payload=None):
            nonlocal order_reads, started
            requests.append(path)
            if path.endswith("/chat"):
                return {"body_md": "Your coding tool is preparing " + idea}
            if path.endswith("/device-work-orders"):
                order_reads += 1
                orders = [{"id": "order"}] if order_reads >= 3 and not (failure_code or "").startswith("pagebuilder_") else []
                started = started or bool(orders)
                return {"orders": orders}
            if path.endswith("/pagebuilder/profile"):
                return {"current": {"state": "failed", "error": failure_code} if (failure_code or "").startswith("pagebuilder_") else None}
            if path.endswith("/start"):
                started = True
                return {"state": "starting"}
            assert path.endswith("/status")
            return {"state": "not_started", "failure_code": failure_code}

    class Checked(Exception):
        pass

    def step(label, action, *_args):
        result = action()
        if label.endswith("order starts"):
            assert result["order"]["id"] == "order"
            raise Checked
        return result

    monkeypatch.setattr(journey.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(journey, "cli_runs", lambda _path: [{"provider": "claude"}] if started and not failure_code else [])
    report = journey.Report(tmp_path / "findings")
    monkeypatch.setattr(report, "step", step)
    check = journey.BuildJourney(Client(), report, tmp_path, fake, 0.01)
    check.venture, check.provider = "venture", "claude"
    if failure_code:
        with pytest.raises(journey.AcceptanceFailure, match=failure_code):
            check.build("landing", idea)
        assert any(path.endswith("/pagebuilder/profile" if failure_code.startswith("pagebuilder_") else "/status") for path in requests)
    else:
        with pytest.raises(Checked):
            check.build("landing", idea)
    assert order_reads >= (2 if (failure_code or "").startswith("pagebuilder_") else 3)
    assert not any(path.endswith("/start") for path in requests)


def test_live_gate_keeps_coding_workspace_in_its_disposable_data(tmp_path, monkeypatch):
    captured = {}
    monkeypatch.setenv("HOME", str(tmp_path))

    def start(_command, **options):
        captured.update(options["env"])
        return SimpleNamespace(stdout=io.StringIO(""), poll=lambda: 0)

    monkeypatch.setattr(journey.subprocess, "Popen", start)
    monkeypatch.setattr(journey, "load_environment", lambda _path: {})
    monkeypatch.setattr(journey, "stop_stack", lambda _stack: None)
    report = journey.Report(tmp_path / "findings")
    monkeypatch.setattr(report, "step", lambda *_args: None)
    arguments = SimpleNamespace(timeout=1, startup_timeout=1)
    journey.run(arguments, {}, report, tmp_path)
    assert Path(captured["HOME"]).is_relative_to(Path(captured["COMPANY_OS_DATA_DIR"]))


@pytest.mark.parametrize("idea", DOMAINS)
@pytest.mark.parametrize("child_provider", ["claude", "codex"])
def test_product_result_follows_only_its_bound_implementation_order(tmp_path, monkeypatch, idea, child_provider):
    fake, data = tmp_path / "bin", tmp_path / "data"
    fake.mkdir()
    data.mkdir()
    (data / "index.html").write_text(journey.MARKER)
    (data / "README.md").write_text(idea)
    requests = []
    started = False
    elapsed = 0.0

    def advance(seconds):
        nonlocal elapsed
        elapsed += seconds

    monkeypatch.setattr(journey, "time", SimpleNamespace(monotonic=lambda: elapsed, sleep=advance))
    parent = {"id": "plan", "manifest": {"coding_loop_verification": {
        "phase": "plan", "provider": "claude", "workspace_slug": "project"}}}
    child = {"id": "implementation", "manifest": {"coding_loop_verification": {
        "phase": "implement", "provider": child_provider, "workspace_slug": "project", "plan_order_id": "plan"}}}

    class Client:
        def request(self, method, path, payload=None):
            nonlocal started
            requests.append((method, path))
            if path.endswith("/chat"):
                started = True
                return {"body_md": "Building " + idea}
            if path.endswith("/device-work-orders"):
                return {"orders": [parent, child] if started else []}
            if path.endswith("/start"):
                return {"state": "starting"}
            if path.endswith("/status"):
                planning = "/plan/" in path
                if elapsed < (0.8 if planning else 1.6):
                    return {"state": "running", "events": [{"text": "Working"}]}
                return {"state": "stopped", "task_verified": True, "evidence_digest": "digest", "events": [{"text": "Working"}],
                    "next_order_id": "implementation" if planning else None,
                    "evidence": {"processes_stopped": True, "outcome": "plan_ready" if planning else "pass"}}
            order = parent if path.endswith("/plan") else child
            return {**order, "state": "stopped", "closed_at": "2026-11-15T00:00:00Z", "native_stop": {
                "task_verified": True, "processes_stopped": True, "evidence_digest": "digest"}}

    monkeypatch.setattr(journey, "cli_runs", lambda _path: [{"provider": "claude", "workspace": str(data), "pid": 123}] if started else [])
    monkeypatch.setattr(journey, "process_alive", lambda _pid: False)
    report = journey.Report(tmp_path / "findings")
    check = journey.BuildJourney(Client(), report, data, fake, 1)
    check.venture, check.provider = "venture", "claude"
    check.build("product", idea)
    assert report.failures == (0 if child_provider == "claude" else 1)
    followed = ("GET", "/native/device-work-orders/venture/implementation/status") in requests
    assert followed is (child_provider == "claude")
    assert ("POST", "/native/device-work-orders/venture/implementation/start") not in requests


@pytest.mark.parametrize("idea", DOMAINS)
@pytest.mark.parametrize("state,verified,stopped,outcome,expected", [
    ("stopped", True, True, "pass", True),
    ("stopped", False, True, "pass", False),
    ("stopped", True, False, "pass", False),
    ("running_coding", True, True, "pass", False),
    ("stopped", True, True, "plan_ready", False),
])
def test_finished_receipt_requires_verified_work_and_process_cleanup(idea, state, verified, stopped, outcome, expected):
    receipt = {"state": state, "task_verified": verified, "evidence": {"processes_stopped": stopped, "outcome": outcome}, "result_summary": idea}
    assert journey.completed_receipt(receipt) is expected


@pytest.mark.parametrize("idea", DOMAINS)
@pytest.mark.parametrize("generation", [None, "existing_status", "internal", "cli", "job", "order"])
def test_unallowed_tool_checks_both_connection_and_absence_of_generation(tmp_path, monkeypatch, idea, generation):
    data = tmp_path / "data"
    data.mkdir()
    with sqlite3.connect(data / "local.db") as database:
        database.execute("CREATE TABLE jobs (id TEXT, status TEXT, venture_id TEXT, kind TEXT)")
        if generation == "existing_status":
            database.execute("INSERT INTO jobs VALUES ('previous', 'pending', 'venture', 'pagebuilder.build')")
    orders = []
    fake = tmp_path / "bin"
    fake.mkdir()

    class Client:
        def request(self, method, path, payload=None):
            if path.endswith("/device-work-orders"):
                return {"orders": list(orders)}
            assert payload["message"] == idea
            if generation == "internal":
                (data / "index.html").write_text("unrequested generation")
            elif generation == "cli":
                (fake / "runs.jsonl").write_text(json.dumps({"pid": 123, "mode": "finish"}) + "\n")
            elif generation == "job":
                with sqlite3.connect(data / "local.db") as database:
                    database.execute("INSERT INTO jobs VALUES ('job', 'pending', 'venture', 'pagebuilder.build')")
            elif generation == "order":
                orders.append({"id": "order"})
            elif generation == "existing_status":
                with sqlite3.connect(data / "local.db") as database:
                    database.execute("UPDATE jobs SET status = 'failed' WHERE id = 'previous'")
            return {"body_md": "Open Settings > Keys and connections > Coding tools and connect your coding tool."}

    monkeypatch.setattr(journey.time, "sleep", lambda _seconds: None)
    check = journey.BuildJourney(Client(), journey.Report(tmp_path / "findings"), data, fake, 1)
    check.venture = "venture"
    if generation in {None, "existing_status"}:
        assert check.denied(idea)
    else:
        with pytest.raises(journey.AcceptanceFailure, match="internal generator or coding process"):
            check.denied(idea)


@pytest.mark.parametrize("idea", DOMAINS)
def test_stop_waits_for_remote_terminal_state_before_negative_snapshots(tmp_path, monkeypatch, idea):
    fake = tmp_path / "bin"
    fake.mkdir()
    state = {"started": False, "cancelled": False, "remote_reads": 0}

    class Client:
        def request(self, method, path, payload=None):
            if path.endswith("/device-work-orders"):
                return {"orders": [{"id": "order"}] if state["started"] else []}
            if path.endswith("/chat"):
                state["started"] = True
                return {"body_md": idea}
            if path.endswith("/start"):
                return {}
            if path.endswith("/cancel"):
                state["cancelled"] = True
                return {}
            if path.endswith("/status"):
                return {"state": "stopped" if state["cancelled"] else "running", "events": [{"text": idea}],
                        "task_verified": False, "evidence": {"processes_stopped": state["cancelled"]}}
            assert path.endswith("/device-work-orders/order")
            state["remote_reads"] += 1
            return {"id": "order", "state": "stopped" if state["remote_reads"] >= 2 else "claimed"}

    monkeypatch.setattr(journey.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(journey, "cli_runs", lambda _path: [{"provider": "codex", "pid": 123}] if state["started"] else [])
    monkeypatch.setattr(journey, "process_alive", lambda _pid: not state["cancelled"])
    report = journey.Report(tmp_path / "findings")
    check = journey.BuildJourney(Client(), report, tmp_path, fake, 1)
    check.venture, check.provider = "venture", "codex"
    check.build("hanging", idea, hanging=True)
    assert report.failures == 0
    assert state["remote_reads"] >= 2


@pytest.mark.integration
@pytest.mark.skipif(os.environ.get("LLM_OFFLINE", "").strip().lower() in {"1", "true", "yes", "on"}
    or os.environ.get("LLM_CASSETTE_MODE", "").strip().lower() == "replay" or not journey.DEFAULT_ENV.is_file(),
    reason="Your live model journey needs online mode and your private live model file.")
def test_local_build_with_your_coding_tool_live():
    arguments = ["--timeout", "25", "--startup-timeout", "120"]
    for index in (1, 2):
        fixture = json.loads((journey.ROOT / "tests/unit/pagebuilder/fixtures/local_tool_pages" / f"{index}.json").read_text())
        arguments.extend(["--idea", fixture["input"]["idea"]])
    assert journey.main(arguments) == 0


@pytest.mark.parametrize("idea", DOMAINS)
@pytest.mark.parametrize("provider", ["codex", "claude", "opencode"])
@pytest.mark.parametrize("hanging,partial_plan", [(False, False), (True, False), (False, True)],
    ids=["preview", "stop", "spec-only-first"])
@pytest.mark.usefixtures("private_coding_install")
def test_product_build_http_journey(tmp_path, monkeypatch, idea, provider, hanging, partial_plan):
    import threading
    import psutil
    import uvicorn
    from tests.unit.product.test_computer_use_jobs import _client

    data = tmp_path / "data"
    data.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    fake = tmp_path / "bin"
    binary = journey.install_fake_cli(fake, provider)
    if partial_plan:
        source = binary.read_text()
        source = source.replace('    review.mkdir(exist_ok=True)\n',
            '    review.mkdir(exist_ok=True)\n'
            '    if len((binary.parent / "runs.jsonl").read_text().splitlines()) > 1:\n'
            '        assert (review / "spec.md").is_file()\n', 1)
        source = source.replace('    (review / "tasks.json").write_text(',
            '    if len((binary.parent / "runs.jsonl").read_text().splitlines()) == 1:\n'
            '        emit({"type": "item.completed", "item": {"type": "agent_message",\n'
            '            "text": "I can\'t read a personal skill; review/spec.md is saved."}})\n'
            '        raise SystemExit(0)\n'
            '    (review / "tasks.json").write_text(', 1)
        binary.write_text(source)
    port = journey.free_ports()[0]
    base_url = f"http://127.0.0.1:{port}"
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", f"{fake}:{os.environ.get('PATH', '')}")
    monkeypatch.setenv("COMPANY_OS_URL", base_url)
    monkeypatch.setenv("SUPYMEM_LOCAL_PROXY_SECRET_FILE", str(data / "owner-session.secret"))
    core = _client(data, monkeypatch)
    from app import state
    from app.coding_agents_consent import ensure_schema
    from app.core.local_secrets import update_settings
    from app.db.supymem_models import IdeaSession, IdeaRevision
    from core.clock import utcnow
    from desktop_runtime import agent_status, loop_checks
    from tests.coding_tool_fixtures import private_test_install

    private_test_install(monkeypatch, fake)
    monkeypatch.setattr(agent_status, "_PROBES_CLOSED", False)
    with state.db()() as session:
        ensure_schema(session.get_bind())
    update_settings("capabilities", {"background_work": False, "research": False}, {})
    update_settings("spend", {"monthly_spend_cap": 0}, {})
    popen = psutil.Popen

    def run_sandbox(argv, **options):
        # shortcut: non-Mac runs use synthetic tools and unsandboxed Node, use platform isolation tests or a Mac run to verify sandbox enforcement.
        if argv[0] == "/usr/bin/sandbox-exec" and sys.platform != "darwin":
            argv = argv[3:]
        return popen(argv, **options)

    monkeypatch.setattr(loop_checks, "preflight", lambda: bool(loop_checks.node_binary()))
    if sys.platform != "darwin":
        monkeypatch.setattr(loop_checks, "_launch", lambda workspace, temporary, node, argv, env, output, **options:
            popen([node, *argv], cwd=workspace, env=env, stdin=subprocess.DEVNULL,
                stdout=output, stderr=subprocess.STDOUT, start_new_session=True))
    monkeypatch.setattr(psutil, "Popen", run_sandbox)
    server = uvicorn.Server(uvicorn.Config(core.app, host="127.0.0.1", port=port,
        lifespan="off", log_level="error", access_log=False))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        journey.wait_for(lambda: server.started, 10, "Your local test API did not start.")
        report = journey.Report(tmp_path / "findings")
        client = journey.OwnerClient(base_url, 30, owner_secret_file=data / "owner-session.secret", redactor=report.redactor)
        original_request = client.request
        starting_reads = 0

        def request(method, path, *arguments, **options):
            nonlocal starting_reads
            result = original_request(method, path, *arguments, **options)
            if (not hanging and path.endswith("/status") and journey.completed_receipt(result)
                    and (result.get("evidence") or {}).get("outcome") == "pass" and starting_reads < 2):
                if starting_reads == 0:
                    assert not result.get("product_preview"), "A finished local build must not start its preview unasked"
                starting_reads += 1
                return {**result, "product_preview": {"status": "unavailable", "ready": False}}
            return result

        monkeypatch.setattr(client, "request", request)
        check = journey.BuildJourney(client, report, data, fake, 30)
        created = client.request("POST", "/ventures", {"raw_idea": idea, "start": False})
        check.venture, check.provider = created["id"], provider
        client.headers["X-Workspace-Id"] = created["workspace_id"]
        now = utcnow()
        with state.db()() as session:
            session.add(IdeaSession(id="idea_product", workspace_id=created["workspace_id"], venture_id=check.venture,
                case="know", state="confirmed", current_revision_id="revision_product",
                confirmed_revision_id="revision_product", created_at=now, updated_at=now))
            session.flush()
            session.add(IdeaRevision(id="revision_product", session_id="idea_product", workspace_id=created["workspace_id"],
                venture_id=check.venture, rev=1, source="founder", summary=idea,
                structured=json.dumps({"idea": idea}), created_at=now))
            session.commit()
        check.allow(provider, True)
        check.build("product", "Build the first version of the product", hanging=hanging, product=True)
        assert report.failures == 0, report.results
        assert starting_reads == (0 if hanging else 2)
        expected = "product Stop ends process" if hanging else "product usable preview"
        assert any(record["step"] == expected and record["state"] == "PASS" for record in report.results)
        orders = [client.request("GET", check.path(f"/device-work-orders/{order['id']}")) for order in check.orders()]
        assert len(orders) == (1 if hanging else 2)
        assert {order["manifest"]["coding_loop_verification"]["phase"] for order in orders} == (
            {"plan"} if hanging else {"plan", "implement"})
        assert all(order["state"] == "stopped" and order["closed_at"] for order in orders)
    finally:
        core.post("/native/emergency-stop")
        server.should_exit = True
        thread.join(timeout=10)
        core.close()
        assert not thread.is_alive()
