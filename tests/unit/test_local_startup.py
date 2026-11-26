import importlib.util
import os
import re
import shutil
import socket
import subprocess
import sys
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]


def startup_script(name, path):
    specification = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


@pytest.mark.parametrize("domain", ["warehouse.example", "hiking.example"])
def test_packaged_api_diagnostics_keep_credentials_out(tmp_path, monkeypatch, domain):
    check = startup_script("startup_e2e_request_diagnostics", "scripts/desktop/e2e_linux.py")
    journey = check.Journey(tmp_path, {"name": domain})
    journey.ports = lambda: {"core": 3300}
    journey.owner_secret_file = tmp_path / "owner-session.secret"
    client = Mock()
    client.response_status = 201
    client.request.side_effect = [{"status": "committed", "api_key": "private-value"},
                                  check.AcceptanceFailure("Your service stopped.", status=503)]
    factory = Mock(return_value=client)
    monkeypatch.setattr(check, "Client", factory)
    assert journey.get("/entry/chat/confirm", payload={"name": domain})["status"] == "committed"
    with pytest.raises(check.AcceptanceFailure):
        journey.get("/ventures")
    import json
    diagnostics = (tmp_path / "api_requests.jsonl").read_text()
    records = [json.loads(line) for line in diagnostics.splitlines()]
    assert [(item["method"], item["path"], item["status"]) for item in records] == [
        ("POST", "/entry/chat/confirm", 201), ("GET", "/ventures", 503)]
    assert "private-value" not in diagnostics and "api_key" not in diagnostics
    assert factory.call_args.kwargs["owner_secret_file"] == journey.owner_secret_file


@pytest.mark.parametrize("domain", ["warehouse.example", "hiking.example"])
def test_packaged_diagnostics_accept_non_question_options(tmp_path, monkeypatch, domain):
    check = startup_script("startup_e2e_turn_diagnostics", "scripts/desktop/e2e_linux.py")
    journey = check.Journey(tmp_path, {"name": domain})
    journey.ports = lambda: {"core": 3300}
    client = Mock()
    turns = [{"kind": "answer", "options": [domain]},
             {"kind": "question", "options": {"field": "customer"}}]
    client.request.return_value = {"turns": turns}
    monkeypatch.setattr(check, "Client", Mock(return_value=client))
    assert journey.get("/session")["turns"] == turns


@pytest.mark.parametrize("domain", ["warehouse.example", "hiking.example"])
@pytest.mark.parametrize("socket_addresses", [False, True])
@pytest.mark.parametrize("received", [False, True])
def test_packaged_health_trace_keeps_only_probe_status_and_timing(tmp_path, domain, socket_addresses, received):
    import json

    check = startup_script("startup_e2e_health_trace", "scripts/desktop/e2e_linux.py")
    network = check.NetworkLog(tmp_path)
    request = 'sendto(9<TCP:[127.0.0.1:1000->127.0.0.1:3300]>, "GET /ready HTTP/1.1\\r\\nHost: 127.0.0.1:3300\\r\\nX-Local-Proxy-Secret: private-value\\r\\n\\r\\n", 1, 0)\n'
    response = 'sendto(8<TCP:[127.0.0.1:3300->127.0.0.1:1000]>, "HTTP/1.1 200 OK\\r\\n\\r\\n", 1, 0)\n'
    if not socket_addresses:
        request = request.replace('TCP:[127.0.0.1:1000->127.0.0.1:3300]', 'TCP:[12345]')
        response = response.replace('TCP:[127.0.0.1:3300->127.0.0.1:1000]', 'TCP:[12345]').replace('sendto', 'recvfrom')
    elif received:
        request = request.replace('127.0.0.1:1000->127.0.0.1:3300', '127.0.0.1:3300->127.0.0.1:1000').replace('sendto', 'recvfrom')
    network.connections = 1
    os.write(network.writer, (request + response).encode())
    network.close()
    diagnostic = (tmp_path / "health_probes.jsonl").read_text()
    records = [json.loads(line) for line in diagnostic.splitlines()]
    assert records[0]["path"] == "/ready" and records[0]["status"] is None
    assert records[1]["status"] == 200 and records[1]["seconds"] >= 0
    assert all(record["port"] == 3300 for record in records)
    assert "private-value" not in diagnostic and domain not in diagnostic


@pytest.mark.parametrize("domain", ["warehouse.example", "hiking.example"])
def test_packaged_failure_reports_the_supervisor_reason_without_private_logs(tmp_path, monkeypatch, domain):
    check = startup_script("startup_e2e_supervisor_failure", "scripts/desktop/e2e_linux.py")
    journey = check.Journey(tmp_path / "output", {"name": domain})
    journey.output.mkdir()
    data = tmp_path / "data"
    (data / "logs").mkdir(parents=True)
    journey.owner_secret_file = data / "owner-session.secret"
    (data / "logs/desktop-local-test.log").write_text("private-value\nLocal core stopped responding\n")
    journey.step = "name"
    journey.state = Mock(side_effect=check.AcceptanceFailure("Your service stopped."))
    journey.app = Mock()
    journey.app.poll.return_value = None
    monkeypatch.setattr(check.time, "monotonic", Mock(side_effect=[0, 0, 100]))
    monkeypatch.setattr(check.time, "sleep", Mock())
    with pytest.raises(RuntimeError, match="Local core stopped responding.*src-tauri/src/local_stack.rs"):
        journey.wait()
    diagnostic = (journey.output / "supervisor_failure.txt").read_text()
    assert diagnostic.strip() == "Local core stopped responding"
    assert "private-value" not in diagnostic


@pytest.mark.parametrize("domain", ["warehouse.example", "hiking.example"])
@pytest.mark.parametrize("configured", [False, True])
def test_packaged_sample_draft_sets_only_a_missing_synthetic_sender_address(tmp_path, monkeypatch, domain, configured):
    from unittest.mock import Mock

    check = startup_script("startup_sample_sender", "scripts/desktop/e2e_linux.py")
    monkeypatch.setattr(check, "ADDED_STEPS", ("draft",))
    journey = check.Journey(tmp_path, {"name": domain})
    journey.venture_id = domain
    journey.state = Mock(return_value={"journey": {
        "sender_postal_address": "Your existing address" if configured else "",
        "drafts": [], "prospects": [{"id": str(index), "name": f"Person {index}",
            "email": f"person{index}@{domain}.example.invalid",
            "source_url": f"https://{domain}.example.invalid/{index}"} for index in range(2)]}})
    journey.get = Mock(return_value={"items": [{"role": "assistant", "actions": []}]})
    journey.wait = Mock()
    journey.run_extended()
    messages = [call.kwargs["payload"]["message"] for call in journey.get.call_args_list
                if call.kwargs.get("payload")]
    if configured:
        assert len(messages) == 1
    else:
        assert len(messages) == 2
        assert messages[0] == "My address is Sample sender address, not for sending"
    assert messages[-1] == check.draft_request(journey.person, "US")
    assert "Do not send anything" in messages[-1]
    journey.wait.assert_called_once()


@pytest.mark.parametrize("domain", ["warehouse.example", "hiking.example"])
@pytest.mark.parametrize("status", ["leased", "failed_dead"])
def test_packaged_research_failure_keeps_only_scoped_safe_receipt_diagnostics(tmp_path, domain, status):
    import json
    import sqlite3

    check = startup_script("startup_research_failure", "scripts/desktop/e2e_linux.py")
    journey = check.Journey(tmp_path, {"name": domain})
    journey.owner_secret_file = tmp_path / "owner-session.secret"
    job = {"id": "job_test", "venture_id": domain, "workspace_id": "workspace", "status": status,
           "error_code": "research_outcome_uncertain", "failure_reason": "private-value"}
    receipt = {"_runtime": {"job_id": job["id"], "operations": {"search": {"state": "uncertain",
        "usage": {"purpose": "research.fetch", "provider": "duckduckgo", "extra": "private-value"},
        "response": "private-value", "failure": {"category": "internal",
            "elapsed_ms": 1400, "extra": "private-value"}}}}}
    with sqlite3.connect(tmp_path / "database.sqlite3") as connection:
        connection.execute("CREATE TABLE research_dossiers (workspace_id TEXT, venture_id TEXT, summary TEXT)")
        connection.execute("INSERT INTO research_dossiers VALUES (?, ?, ?)", ("workspace", domain, json.dumps(receipt)))
        connection.execute("INSERT INTO research_dossiers VALUES (?, ?, ?)", ("another-workspace", domain, "private-value"))
    message = journey.research_failure(job)
    if status == "leased":
        assert message is None and not (tmp_path / "research_failure.json").exists()
    else:
        assert "research_outcome_uncertain" in message and "app/research/metered.py" in message
        diagnostic = (tmp_path / "research_failure.json").read_text()
        assert "private-value" not in diagnostic and "private-value" not in message
        assert json.loads(diagnostic)["failures"] == [{"category": "internal", "elapsed_ms": 1400,
                                                     "purpose": "research.fetch", "provider": "duckduckgo"}]
        journey.search_provider = "builtin"
        assert "unbilled read" in journey.research_failure(job)
        journey.firecrawl_configured = True
        assert "unbilled read" not in journey.research_failure(job)
        journey.firecrawl_configured = False
        receipt["_runtime"]["operations"]["search"]["failure"] = {"category": "http", "http_status": 403}
        with sqlite3.connect(tmp_path / "database.sqlite3") as connection:
            connection.execute("UPDATE research_dossiers SET summary=? WHERE workspace_id=?",
                               (json.dumps(receipt), "workspace"))
        assert "unbilled read" not in journey.research_failure(job)


@pytest.mark.parametrize("domain", ["warehouse.example", "hiking.example"])
def test_packaged_reads_allow_the_same_busy_service_budget_as_writes(tmp_path, monkeypatch, domain):
    check = startup_script("startup_api_read_budget", "scripts/desktop/e2e_linux.py")
    client = Mock(response_status=200)
    client.request.return_value = {}
    factory = Mock(return_value=client)
    monkeypatch.setattr(check, "Client", factory)
    journey = check.Journey(tmp_path, {"name": domain})
    journey.ports = Mock(return_value={"core": 3300})
    journey.get("/ventures/" + domain)
    assert factory.call_args.args == ("http://127.0.0.1:3300", 60)


@pytest.mark.parametrize("domain", ["warehouse.example", "hiking.example"])
@pytest.mark.parametrize("step", ["research", "find customers", "landing page", "answer"])
def test_packaged_live_work_budget_does_not_relax_screen_waits(tmp_path, monkeypatch, domain, step):
    check = startup_script("startup_live_work_budget", "scripts/desktop/e2e_linux.py")
    journey = check.Journey(tmp_path, {"name": domain})
    journey.live, journey.step = True, step
    journey.app = Mock()
    journey.app.poll.return_value = None
    clock = [0]
    monkeypatch.setattr(check.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(check.time, "sleep", Mock())
    def ready():
        clock[0] += 95
        return clock[0] >= 190
    if step == "answer":
        with pytest.raises(RuntimeError, match="did not finish"):
            journey.poll(ready)
    else:
        journey.poll(ready)


@pytest.mark.parametrize("domain", ["warehouse.example", "hiking.example"])
def test_packaged_research_uses_public_jobs_when_chat_hides_the_receipt(tmp_path, monkeypatch, domain):
    check = startup_script("startup_research_public_job", "scripts/desktop/e2e_linux.py")
    journey = check.Journey(tmp_path, {"name": domain, "idea": domain, "answers": {"customer": domain}})
    journey.entry = "env"
    journey.model = {"provider": "openai", "model": "model", "base_url": "http://127.0.0.1"}
    for method in ("wait", "verify_resources", "open_screen", "passed", "connect_coding_tools", "verify_capabilities"):
        setattr(journey, method, Mock())
    paths = []
    def response(path, **kwargs):
        paths.append(path)
        if path == "/local/providers":
            return {"values": journey.model}
        if path == "/local/settings/search":
            return {"values": {"provider": "builtin"}}
        if path == "/entry/chat/preview":
            return {"proposal": {"id": "proposal"}}
        if path == "/ventures":
            return {"items": [{"id": domain, "workspace_id": "workspace"}]}
        if path.endswith("/idea/sessions"):
            return {"items": [{"id": "session"}]}
        if path.endswith("/idea/sessions/session"):
            return {"state": "reviewing", "current_revision": {"id": "revision"},
                    "turns": [{"role": "agent", "kind": "question", "options": {"field": "customer"}}]}
        if path.startswith("/workspaces/workspace/jobs?"):
            return {"items": [{"id": "job_test", "kind": "research.deep", "status": "failed_dead",
                               "workspace_id": "workspace", "venture_id": domain, "error_code": "research_outcome_uncertain"}]}
        if path.endswith("/chat"):
            return {"items": [{"id": "reply", "role": "assistant", "citations": []}]}
        return {}
    journey.get = response
    def poll(predicate):
        if journey.step == "answer":
            assert predicate()
        if journey.step == "research" and not predicate():
            raise RuntimeError("Your research failure was ignored.")
    journey.poll = poll
    with pytest.raises(RuntimeError, match="research_outcome_uncertain"):
        journey.run_live()
    from urllib.parse import parse_qs, urlsplit
    query = parse_qs(urlsplit(next(path for path in paths if path.startswith("/workspaces/workspace/jobs?"))).query)
    assert query["venture_id"] == [domain]
    assert query["kinds"] == ["research.deep"]


@pytest.mark.parametrize("domain", ["warehouse.example", "hiking.example"])
def test_packaged_zip_download_uses_the_validated_owner_session(tmp_path, domain):
    from unittest.mock import MagicMock

    check = startup_script("startup_acceptance_download", "scripts/local_acceptance.py")
    owner = tmp_path / "owner-session.secret"
    owner.write_text("d" * 64)
    owner.chmod(0o600)
    client = check.Client("http://127.0.0.1:3300", 1, owner_secret_file=owner)
    client.opener = MagicMock()
    response = client.opener.open.return_value.__enter__.return_value
    response.status = 201
    response.read.return_value = b"PK\x03\x04" + domain.encode()
    assert client.request("GET", "/export.zip", download=True) == response.read.return_value
    assert client.response_status == 201
    request = client.opener.open.call_args.args[0]
    assert request.get_header("X-local-proxy-secret") == "d" * 64
    response.read.assert_called_once_with(20_000_001)


@pytest.mark.parametrize("domain", ["warehouse.example", "hiking.example"])
def test_packaged_browser_diagnostics_show_owner_setup_without_the_proof(tmp_path, domain):
    import json
    from types import SimpleNamespace

    check = startup_script("startup_e2e_browser_diagnostics", "scripts/desktop/e2e_linux.py")
    desktop = check.Desktop.__new__(check.Desktop)
    desktop.network = SimpleNamespace(output=tmp_path)
    message = {"method": "Network.responseReceived", "params": {"response": {
        "url": "http://127.0.0.1:3100/new", "status": 200,
        "headers": {"Set-Cookie": "supymem_owner=private-value; HttpOnly; SameSite=Strict"}}}}
    desktop.message(None, None, SimpleNamespace(get_data=lambda: json.dumps(message).encode()))
    diagnostic = (tmp_path / "inspector_protocol.log").read_text()
    record = json.loads(diagnostic)
    assert record["path"] == "/new" and record["status"] == 200 and record["owner_cookie"] is True
    assert "private-value" not in diagnostic and domain not in diagnostic


@pytest.mark.parametrize("domain", ["warehouse.example", "hiking.example"])
@pytest.mark.parametrize("sample", [False, True])
@pytest.mark.parametrize("prefixed", [False, True])
def test_packaged_name_check_preserves_the_sample_label(tmp_path, monkeypatch, domain, sample, prefixed):
    check = startup_script("startup_e2e_sample_name", "scripts/desktop/e2e_linux.py")
    name = "Sample: " + domain if prefixed else domain
    shown = "Sample: " + name if sample and not prefixed else name
    venture = {"id": "venture", "name": shown, "raw_idea": domain, "sample": sample}
    state = {"venture": venture, "session": {"turns": [{"kind": "question", "text": "Who is it for?"}]}}
    scenario = {"name": name, "idea": domain}
    assert check.step_ready("name", state, scenario)
    monkeypatch.setenv("E2E_MODE", "sample")
    journey = check.Journey(tmp_path, scenario)
    journey.step = "name"
    journey.get = Mock(side_effect=[{"items": [venture]}, {"items": [{"id": "session"}]},
                                    state["session"], venture])
    assert journey.state()["venture"] == venture
    assert journey.venture_id == "venture"
    venture["name"] = "Different name"
    assert not check.step_ready("name", state, scenario)


@pytest.mark.parametrize("domain", ["warehouse.example", "hiking.example"])
@pytest.mark.parametrize("mode", ["sample", "live"])
def test_packaged_creation_follows_the_current_chat_route(tmp_path, monkeypatch, domain, mode):
    check = startup_script("startup_e2e_chat_route", "scripts/desktop/e2e_linux.py")
    monkeypatch.setenv("E2E_MODE", mode)
    monkeypatch.setenv("E2E_ENTRY", "env")
    journey = check.Journey(tmp_path, {"name": domain, "idea": domain, "answers": {}})
    journey.venture_id = domain
    journey.model = {"provider": "openai", "model": "model", "base_url": "http://127.0.0.1"}
    journey.wait = Mock()
    journey.poll = Mock()
    journey.passed = Mock()
    journey.verify_resources = Mock()
    journey.connect_coding_tools = Mock()
    journey.verify_capabilities = Mock()
    journey.read_screen = Mock(return_value={"clicked": True})
    monkeypatch.setattr(check, "Desktop", Mock())
    journey.get = Mock(side_effect=([{"values": journey.model}, {"proposal": {"id": "proposal"}},
                                    {}, {"items": [{"id": domain}]}] if mode == "live" else
                                   [{"llm_mode": "sample"}, {"proposal": {"id": "proposal"}}, {}]))
    paths = []
    def open_screen(path=None, expected=None):
        if path and path != "/":
            paths.append(path)
            raise StopIteration
    journey.open_screen = open_screen
    with pytest.raises(StopIteration):
        journey.run_api()
    assert paths == ["/new?venture=" + domain]
    assert journey.connect_coding_tools.call_count == int(mode == "live")


@pytest.mark.parametrize("domain", ["warehouse.example", "hiking.example"])
def test_packaged_sample_namespace_preserves_your_desktop_identity(domain):
    import shlex

    source = (ROOT / "scripts/desktop/e2e_linux.sh").read_text()
    options = shlex.split(source.split("600 unshare ", 1)[1].split("\\\n", 1)[0])
    result = subprocess.run(["unshare", *options, "bash", "-c", "ip link set lo up && id -u"],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert int(result.stdout) == os.geteuid(), domain


@pytest.mark.parametrize("domain", ["warehouse.example", "hiking.example"])
@pytest.mark.parametrize("display", ["DISPLAY", "WAYLAND_DISPLAY"])
def test_packaged_launcher_accepts_a_display_without_desktop_services(tmp_path, domain, display):
    packaging = startup_script("startup_linux_packaging", "tests/unit/test_linux_packaging.py")
    launcher, root, environment = packaging.launcher_fixture(tmp_path, domain)
    for name in ("DBUS_SESSION_BUS_ADDRESS", "XDG_SESSION_ID", "XDG_RUNTIME_DIR", "XDG_SESSION_TYPE"):
        environment.pop(name, None)
    environment[display] = ":99" if display == "DISPLAY" else "wayland-0"
    result = subprocess.run([str(launcher), domain], env=environment, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert (root / "args").read_text().splitlines()[-1] == domain


@pytest.mark.parametrize("domain", ["warehouse.example", "hiking.example"])
def test_packaged_owner_session_waits_in_its_data_directory(tmp_path, monkeypatch, domain):
    import secrets
    from types import SimpleNamespace

    acceptance = startup_script("startup_acceptance", "scripts/local_acceptance.py")
    secret = secrets.token_hex(32)
    path = tmp_path / domain / "owner-session.secret"
    path.parent.mkdir()
    client = acceptance.Client("http://127.0.0.1:43300", 0.1, owner_secret_file=path)
    response = Mock()
    response.__enter__ = Mock(return_value=SimpleNamespace(read=lambda *_: b'{"ok":true}'))
    response.__exit__ = Mock(return_value=False)
    client.opener = SimpleNamespace(open=Mock(return_value=response))

    def created(_delay):
        path.write_text(secret)
        path.chmod(0o600)

    monkeypatch.setattr(acceptance.time, "sleep", created)
    assert client.request("GET", "/health") == {"ok": True}
    assert client.opener.open.call_args.args[0].get_header("X-local-proxy-secret") == secret


@pytest.mark.parametrize("domain", ["warehouse.example", "hiking.example"])
@pytest.mark.parametrize("supervisor", [False, True])
def test_packaged_journey_records_owner_failure_before_network_pass(tmp_path, monkeypatch, capsys, domain, supervisor):
    import json
    from io import BytesIO
    from types import SimpleNamespace

    check = startup_script("startup_e2e", "scripts/desktop/e2e_linux.py")
    monkeypatch.setenv("E2E_MODE", "sample")
    monkeypatch.delenv("E2E_OCCUPY_PORT", raising=False)
    monkeypatch.setattr(check.sys, "argv", ["e2e", "/test.AppImage", str(tmp_path)])
    launches = []
    owner_paths = []

    def launch(*_arguments, **keywords):
        launches.append(keywords["env"])
        check.os.write(keywords["pass_fds"][0], b'connect(3, inet_addr("127.0.0.1")) = 0\n')
        return SimpleNamespace(pid=1234, stdout=BytesIO(), poll=lambda: 1)

    def fail(journey):
        from local_acceptance import AcceptanceFailure

        owner_paths.append(journey.owner_secret_file)
        if supervisor:
            logs = journey.owner_secret_file.parent / "logs"
            logs.mkdir(parents=True)
            (logs / "desktop-local-test.log").write_text("Local core stopped responding\n")
        raise AcceptanceFailure("Your private owner session is missing. Check src-tauri/src/local_stack.rs, then restart your app.")

    monkeypatch.setattr(check.subprocess, "Popen", launch)
    monkeypatch.setattr(check.Journey, "run", fail)
    monkeypatch.setattr(check.Journey, "capture", lambda _journey: None)
    assert check.main() == 1
    assert Path(launches[0]["COMPANY_OS_DATA_DIR"]) == owner_paths[0].parent, domain
    results = json.loads((tmp_path / "results.json").read_text())
    assert results[0]["step"] == "splash" and results[0]["passed"] is False
    assert results[-1]["step"] == "no hosted requests" and results[-1]["passed"] is True
    output = capsys.readouterr()
    expected = "Your desktop supervisor reported: Local core stopped responding" if supervisor else "Your private owner session is missing."
    assert "FAIL splash: " + expected in output.err
    assert "SUMMARY PASS=1 FAIL=1 SKIP=0" in output.out


@pytest.mark.parametrize("domain", ["warehouse.example", "hiking.example"])
@pytest.mark.parametrize("live", [False, True])
def test_packaged_setup_follows_the_welcome_buttons(tmp_path, monkeypatch, domain, live):
    from types import SimpleNamespace

    check = startup_script("startup_e2e_welcome", "scripts/desktop/e2e_linux.py")
    monkeypatch.setenv("E2E_MODE", "live" if live else "sample")
    monkeypatch.setattr(check, "Desktop", lambda: SimpleNamespace())
    journey = check.Journey(tmp_path, {"domain": domain})
    journey.entry = "screens"
    journey.model = {"provider": "openai_compatible"}
    journey.wait = Mock()
    journey.poll = Mock()
    journey.verify_resources = Mock()
    journey.passed = Mock()
    journey.open_screen = Mock()
    journey.read_screen = Mock(side_effect=[{"text": "Connect a model"}, {"clicked": True}]) if live else Mock(side_effect=RuntimeError("stop at sample"))
    journey.get = Mock()
    if live:
        journey.desktop = SimpleNamespace(fill=Mock(side_effect=RuntimeError("stop at model")))
    with pytest.raises(RuntimeError, match="stop at"):
        journey.run_live() if live else journey.run_api()
    if live:
        journey.read_screen.assert_any_call(click="Connect a model")
    else:
        assert all(call.kwargs.get("expected") != "Connect your AI service" for call in journey.open_screen.call_args_list)
        journey.read_screen.assert_called_once_with(click="Try without a key (sample mode)")


@pytest.mark.parametrize("domain", ["warehouse.example", "hiking.example"])
@pytest.mark.parametrize("saved", [False, True])
def test_packaged_missing_question_names_the_product_source(tmp_path, monkeypatch, domain, saved):
    from types import SimpleNamespace

    check = startup_script("startup_e2e_question_failure", "scripts/desktop/e2e_linux.py")
    monkeypatch.setenv("E2E_MODE", "sample")
    journey = check.Journey(tmp_path, {"domain": domain})
    journey.step = "name"
    journey.app = SimpleNamespace(poll=lambda: None)
    journey.state = Mock(return_value={"venture": {"id": domain} if saved else {}, "session": {"state": "clarifying", "turns": []}})
    monkeypatch.setattr(check, "step_ready", lambda *_arguments: False)
    monkeypatch.setattr(check.time, "monotonic", Mock(side_effect=[0, 0, 100]))
    monkeypatch.setattr(check.time, "sleep", Mock())
    expected = "first question.*services/company_os/app/idea/runner.py" if saved else "name you chose.*services/company_os/app/entry/service.py"
    with pytest.raises(RuntimeError, match=expected):
        journey.wait()


@pytest.mark.parametrize("domain", ["warehouse.example", "hiking.example"])
@pytest.mark.parametrize("field", ["provider", "model", "base_url"])
def test_packaged_model_mismatch_names_the_setting_without_its_value(tmp_path, monkeypatch, domain, field):
    check = startup_script("startup_e2e_model_failure", "scripts/desktop/e2e_linux.py")
    monkeypatch.setenv("E2E_MODE", "live")
    journey = check.Journey(tmp_path, {"domain": domain})
    journey.entry = "env"
    journey.model = {"provider": "openai", "model": "chosen-model", "base_url": f"https://{domain}/v1"}
    saved = {**journey.model, field: "different-value"}
    journey.wait = Mock()
    journey.poll = Mock()
    journey.verify_resources = Mock()
    journey.passed = Mock()
    journey.get = Mock(return_value={"values": saved})
    with pytest.raises(RuntimeError, match=field + ".*services/company_os/app/core/providers_local.py") as failure:
        journey.run_live()
    assert "different-value" not in str(failure.value)


@pytest.mark.parametrize("domain", ["warehouse.example", "hiking.example"])
@pytest.mark.parametrize("suffix", ["", "/openai", "/openai/v1", "/openai/v1/"])
def test_packaged_live_model_uses_the_saved_azure_endpoint_form(tmp_path, domain, suffix):
    check = startup_script("startup_e2e_endpoint", "scripts/desktop/e2e_linux.py")
    path = tmp_path / "model.env"
    path.write_text("SUPYMEM_LLM_PROVIDER=azure_openai\nAZURE_OPENAI_API_KEY=fake-test-key\n"
                    f"AZURE_OPENAI_ENDPOINT=https://{domain}{suffix}\nSUPYMEM_MODEL_CHAT=chosen-model\n")
    path.chmod(0o600)
    assert check.model_settings(path)["base_url"] == f"https://{domain}/openai/v1"


@pytest.mark.parametrize("domain", ["warehouse.example", "hiking.example"])
@pytest.mark.parametrize("field", ["customer", None])
def test_packaged_live_answer_uses_the_question_not_a_later_note(tmp_path, monkeypatch, domain, field):
    check = startup_script("startup_e2e_live_question", "scripts/desktop/e2e_linux.py")
    monkeypatch.setenv("E2E_MODE", "live")
    journey = check.Journey(tmp_path, {"domain": domain, "idea": domain, "name": domain,
                                      "answers": {"customer": f"People using {domain}"}})
    journey.entry = "env"
    journey.model = {"provider": "openai", "model": "chosen-model", "base_url": f"https://{domain}/v1"}
    for method in ("wait", "verify_resources", "open_screen", "passed", "connect_coding_tools", "verify_capabilities"):
        setattr(journey, method, Mock())
    requests = []

    def response(path, payload=None):
        if path == "/local/providers":
            return {"values": journey.model}
        if path == "/entry/chat/preview":
            return {"proposal": {"id": "proposal"}}
        if path == "/entry/chat/confirm":
            return {}
        if path == "/ventures":
            return {"items": [{"id": "venture", "workspace_id": "workspace"}]}
        if path.endswith("/idea/sessions"):
            return {"items": [{"id": "session"}]}
        if path.endswith("/session"):
            return {"state": "clarifying", "turns": [
                {"role": "agent", "kind": "question", "options": {"field": field}},
                {"role": "agent", "kind": "note", "options": {}}]}
        if path.endswith("/messages"):
            requests.append(payload)
            raise RuntimeError("Your test answer was recorded.")
        raise AssertionError(path)

    journey.get = response
    def poll(predicate):
        if journey.step == "answer":
            assert predicate()
    journey.poll = poll
    expected = "test answer was recorded" if field else "question.*field.*services/company_os/app/idea/service.py"
    with pytest.raises(RuntimeError, match=expected):
        journey.run_live()
    assert requests == ([{"text": journey.scenario["answers"][field]}] if field else [])


@pytest.mark.parametrize("domain", ["warehouse.example", "hiking.example"])
def test_packaged_live_waits_for_the_new_question(tmp_path, monkeypatch, domain):
    check = startup_script("startup_e2e_pending_question", "scripts/desktop/e2e_linux.py")
    monkeypatch.setenv("E2E_MODE", "live")
    journey = check.Journey(tmp_path, {"name": domain, "idea": domain, "answers": {"customer": domain}})
    journey.entry = "env"
    journey.model = {"provider": "openai", "model": "model", "base_url": "http://127.0.0.1"}
    for method in ("wait", "verify_resources", "open_screen", "passed", "connect_coding_tools", "verify_capabilities"):
        setattr(journey, method, Mock())
    reads = []
    def response(path, payload=None):
        if path == "/local/providers":
            return {"values": journey.model}
        if path == "/entry/chat/preview":
            return {"proposal": {"id": "proposal"}}
        if path == "/entry/chat/confirm":
            return {}
        if path == "/ventures":
            return {"items": [{"id": domain}]}
        if path.endswith("/idea/sessions"):
            return {"items": [{"id": "session"}]}
        if path.endswith("/session"):
            reads.append(path)
            turns = [{"role": "agent", "kind": "question", "options": {"field": "offer"}},
                     {"role": "founder", "kind": "answer", "text": domain}]
            if len(reads) > 1:
                turns.append({"role": "agent", "kind": "question", "options": {"field": "customer"}})
            return {"state": "clarifying", "turns": turns}
        if path.endswith("/messages"):
            assert payload == {"text": domain}
            raise RuntimeError("Your new question was answered.")
        raise AssertionError(path)
    journey.get = response
    def poll(predicate):
        if journey.step == "answer":
            assert not predicate()
            assert predicate()
    journey.poll = poll
    with pytest.raises(RuntimeError, match="new question was answered"):
        journey.run_live()
    assert len(reads) == 2


@pytest.mark.parametrize("domain", ["warehouse.example", "hiking.example"])
@pytest.mark.parametrize("mode", ["sample", "live"])
def test_packaged_summary_rejects_an_incomplete_journey(tmp_path, domain, mode):
    import sys

    root = tmp_path / domain
    tools = root / "tools"
    tools.mkdir(parents=True)
    app = root / "app.AppImage"
    app.write_text("#!/bin/sh\nexit 0\n")
    app.chmod(0o755)
    settings = root / "model.env"
    settings.touch(mode=0o600)
    write_startup_tool(tools / "timeout", '''
if [[ "$E2E_MODE" == sample ]]; then
  exec python3 "${@: -4:1}/scripts/desktop/e2e_linux.py" "${@: -3:1}" "${@: -2:1}"
fi
exec python3 "${@: -3}"
''')
    python = tools / "python3"
    python.write_text(f'''#!{sys.executable}
import json
import os
import sys
from pathlib import Path
if sys.argv[1] == "-c":
    sys.exit(0)
if sys.argv[1].endswith("e2e_linux.py"):
    (Path(sys.argv[-1]) / "results.json").write_text(json.dumps([{{"step": "no hosted requests", "passed": True}}]))
    print("PASS no hosted requests", flush=True)
    sys.exit(1)
os.execv(sys.executable, [sys.executable, *sys.argv[1:]])
''')
    python.chmod(0o755)
    environment = {**os.environ, "PATH": f"{tools}:{os.defpath}", "E2E_MODE": mode, "E2E_ENV_FILE": str(settings)}
    result = subprocess.run(["bash", str(ROOT / "scripts/desktop/e2e_linux.sh"), str(app), str(root / "evidence")],
                            env=environment, capture_output=True, text=True, timeout=15)
    assert result.returncode == 1
    if mode == "live":
        assert "FAIL env journey:" in result.stdout
        assert "FAIL screens journey:" in result.stdout
        assert "SUMMARY PASS=2 FAIL=2 SKIP=0" in result.stdout
    else:
        assert "FAIL journey:" in result.stdout
        assert "SUMMARY PASS=1 FAIL=1 SKIP=0" in result.stdout


def write_startup_tool(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/bash\n{body}\n")
    path.chmod(0o755)


@pytest.fixture(params=["warehouse.example", "hiking.example"])
def local_launch_checkout(tmp_path, request):
    root = tmp_path / request.param
    (root / "scripts").mkdir(parents=True)
    (root / "scripts/run_local.sh").write_text((ROOT / "scripts/run_local.sh").read_text())
    for name in ("services/company_os/requirements.runtime.lock", "services/company_os/requirements.build.lock",
                 "packages/desktop_runtime/requirements.local.lock", "services/company_os/pyproject.toml",
                 "packages/core/pyproject.toml", "apps/console/package.json", "apps/console/pnpm-lock.yaml"):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(request.param)
    tools = root / "tools"
    tools.mkdir()
    for name in ("dirname", "cat", "cksum", "mkdir", "cp", "mv", "date", "ln"):
        (tools / name).symlink_to(shutil.which(name))
    write_startup_tool(tools / "python3.12", '''
if [[ "$1" == -m && "$2" == venv ]]; then
  printf 'venv\n' >> "$BOOT_TRACE"
  mkdir -p "$3/bin"
  cp "$0" "$3/bin/python"
elif [[ "$1" == -m ]]; then
  printf 'pip\n' >> "$BOOT_TRACE"
  for argument in "$@"; do
    if [[ "$argument" == packages/harness ]]; then
      [[ -f "$argument/pyproject.toml" ]] || exit 1
      printf 'harness\n' >> "$BOOT_TRACE"
    fi
  done
elif [[ "$1" != -c ]]; then
  printf 'continued\n' >> "$BOOT_TRACE"
  node -v >> "$BOOT_TRACE"
fi''')
    write_startup_tool(tools / "pnpm", '''
if [[ "$1" == --version ]]; then echo 10.11.0; exit; fi
printf 'pnpm-node=%s\n' "$(node -v)" >> "$BOOT_TRACE"
mkdir -p "$2/node_modules"''')
    write_startup_tool(tools / "node", "printf '%s\\n' 'v20.1.0'")
    home = root / "home"
    home.mkdir()
    return root, tools, {"PATH": str(tools), "HOME": str(home),
                         "SUPYMEM_PYTHON": str(tools / "python3.12"), "BOOT_TRACE": str(root / "trace")}


@pytest.mark.parametrize("present", [False, True], ids=["public", "private"])
def test_local_launcher_installs_harness_only_when_present(local_launch_checkout, present):
    root, tools, environment = local_launch_checkout
    write_startup_tool(tools / "node", "echo v22.23.4")
    if present:
        manifest = root / "packages/harness/pyproject.toml"
        manifest.parent.mkdir()
        manifest.write_text('[project]\nname = "companyos-harness"\n')
    result = subprocess.run(["/bin/bash", str(root / "scripts/run_local.sh"), "--web"],
                            env=environment, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    trace = (root / "trace").read_text().splitlines()
    assert ("harness" in trace) is present
    assert "continued" in trace


@pytest.fixture
def free_web_ports():
    ports = {}
    with ExitStack() as sockets:
        for name in ("CORE_PORT", "CONSOLE_PORT", "WORKER_HEALTH_PORT", "BRIDGE_PORT"):
            listener = sockets.enter_context(socket.socket())
            listener.bind(("127.0.0.1", 0))
            ports[name] = str(listener.getsockname()[1])
    # shortcut: assignments are released before the test runs; isolate the network namespace if concurrent listeners race.
    return ports


@pytest.fixture
def web_preflight_checkout(local_launch_checkout):
    root, tools, environment = local_launch_checkout
    for name in ("scripts/desktop_version.py", "src-tauri/tauri.conf.json"):
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, target)
    write_startup_tool(tools / "node", "echo v22.23.4")
    write_startup_tool(root / ".venv/bin/python", f'if [[ "$1" == - ]]; then exec "{sys.executable}" "$@"; fi')
    app = root / "services/company_os/app"
    app.mkdir()
    (app / "__init__.py").write_text('from pkgutil import extend_path\n__path__ = extend_path(__path__, __name__)\n')
    (app / "state.py").write_text('from pathlib import Path\nPath("db-attempted").touch()\ndef db(): raise SystemExit(83)\n')
    environment["PYTHONPATH"] = f"{ROOT / 'services/company_os'}:{ROOT / 'packages'}"
    return root, environment


@pytest.mark.parametrize("name", ["COMPANY_OS_DB_URL", "DATABASE_URL", "DB_URL", "RDS_PROXY_ENDPOINT"])
@pytest.mark.parametrize("source", ["terminal", "dotenv"])
def test_web_preflight_refuses_inherited_database_before_opening_it(web_preflight_checkout, name, source):
    root, environment = web_preflight_checkout
    remote = f"private.{root.name}"
    local_database = root / "data/runtime/local/local.db"
    local_database.parent.mkdir(parents=True)
    local_database.write_text("existing data")
    values = {"COMPANY_OS_DB_URL": f"sqlite:///{local_database}"}
    configured = environment if source == "terminal" else values
    configured[name] = remote if name == "RDS_PROXY_ENDPOINT" else f"postgresql://fake-user:fixture-value@{remote}/db"
    (root / ".env").write_text("".join(f"{key}={value}\n" for key, value in values.items()))
    result = subprocess.run(["/bin/bash", str(root / "scripts/run_local.sh"), "--web"],
                            env=environment, capture_output=True, text=True, timeout=15)
    assert result.returncode == 1
    assert not (root / "db-attempted").exists()
    assert local_database.read_text() == "existing data"
    assert name in result.stderr and ".env" in result.stderr and "./start --web" in result.stderr
    assert remote not in result.stderr and "fixture-value" not in result.stderr


@pytest.mark.parametrize("name", ["CORE_PORT", "WORKER_HEALTH_PORT", "CONSOLE_PORT"])
def test_web_preflight_names_occupied_port_before_opening_database(web_preflight_checkout, name, free_web_ports):
    root, environment = web_preflight_checkout
    port = free_web_ports[name]
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", int(port)))
        listener.listen()
        (root / ".env").write_text("".join(f"{key}={value}\n" for key, value in free_web_ports.items()))
        result = subprocess.run(["/bin/bash", str(root / "scripts/run_local.sh"), "--web"],
                                env=environment, capture_output=True, text=True, timeout=15)
    assert result.returncode == 1
    assert not (root / "db-attempted").exists()
    assert not (root / "data/runtime/local/local.db").exists()
    assert name in result.stderr and port in result.stderr and "./start --web" in result.stderr


@pytest.mark.parametrize("state", ["wrong_version", "missing_python", "broken_python", "backup_exists", "ready", "absent"])
def test_local_launcher_preserves_and_rebuilds_unusable_venv(local_launch_checkout, state):
    root, tools, environment = local_launch_checkout
    write_startup_tool(tools / "node", "printf '%s\\n' 'v22.23.4'")
    previous = root / ".venv"
    if state != "absent":
        (previous / "bin").mkdir(parents=True)
        (previous / "keep").write_text("your existing environment")
        if state == "ready":
            shutil.copy2(tools / "python3.12", previous / "bin/python")
        elif state != "missing_python":
            write_startup_tool(previous / "bin/python", "exit 127" if state == "broken_python" else "exit 1")
    existing_backup = root / ".venv-old-20000101-000000"
    if state == "backup_exists":
        (tools / "date").unlink()
        write_startup_tool(tools / "date", "printf '%s\\n' '20000101-000000'")
        existing_backup.mkdir()
        (existing_backup / "keep").write_text("your earlier backup")
    result = subprocess.run(["/bin/bash", str(root / "scripts/run_local.sh"), "--web"],
                            env=environment, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    trace = (root / "trace").read_text().splitlines()
    assert "continued" in trace
    assert (previous / "bin/python").read_text() == (tools / "python3.12").read_text()
    backups = [path for path in root.glob(".venv-old-*") if path != existing_backup]
    if state in {"ready", "absent"}:
        assert not backups
        assert trace.count("venv") == (state == "absent")
    else:
        assert len(backups) == 1
        assert (backups[0] / "keep").read_text() == "your existing environment"
        assert not (previous / "keep").exists()
        assert trace.count("venv") == 1
        assert sum(backups[0].name in line for line in result.stdout.splitlines()) == 1
        if state != "missing_python":
            assert (backups[0] / "bin/python").is_file()
    if state == "backup_exists":
        assert (existing_backup / "keep").read_text() == "your earlier backup"


@pytest.mark.parametrize("location", ["path_node", "node22", "nodejs", "later_path", "nvm", "fnm", "fnm_legacy",
                                      "fnm_mac", "volta", "volta_image", "asdf", "asdf_shim", "brew", "newer_brew",
                                      "invalid_candidate", "broken_candidate", "no_compatible"])
def test_local_launcher_selects_a_verified_node_candidate(local_launch_checkout, location):
    root, tools, environment = local_launch_checkout
    home = Path(environment["HOME"])
    version = "v24.1.0" if location == "newer_brew" else "v22.23.4"
    paths = {
        "path_node": tools / "node", "node22": tools / "node22", "nodejs": tools / "nodejs",
        "later_path": root / "later/bin/node", "nvm": home / ".nvm/versions/node/v22.23.4/bin/node",
        "fnm": home / ".local/share/fnm/node-versions/v22.23.4/installation/bin/node",
        "fnm_legacy": home / ".fnm/node-versions/v22.23.4/installation/bin/node",
        "fnm_mac": home / "Library/Application Support/fnm/node-versions/v22.23.4/installation/bin/node",
        "volta": home / ".volta/bin/node", "volta_image": home / ".volta/tools/image/node/22.23.4/bin/node",
        "asdf": home / ".asdf/installs/nodejs/22.23.4/bin/node", "asdf_shim": home / ".asdf/shims/node",
        "brew": home / "brew/opt/node@22/bin/node", "newer_brew": home / "brew/opt/node@22/bin/node",
        "invalid_candidate": tools / "nodejs", "broken_candidate": tools / "nodejs",
    }
    if location != "no_compatible":
        candidate = paths[location]
        write_startup_tool(candidate, f"printf '%s\\n' '{version}'")
        if location == "later_path":
            environment["PATH"] += f":{candidate.parent}"
        if location in {"brew", "newer_brew"}:
            write_startup_tool(tools / "brew", f"printf '%s\\n' '{candidate.parent.parent}'")
        if location in {"invalid_candidate", "broken_candidate"}:
            write_startup_tool(tools / "node22", "printf '%s\\n' 'not-a-version'" if location == "invalid_candidate"
                               else "printf '%s\\n' 'v24.1.0'; exit 1")
    result = subprocess.run(["/bin/bash", str(root / "scripts/run_local.sh"), "--web"],
                            env=environment, capture_output=True, text=True)
    if location == "no_compatible":
        assert result.returncode != 0
        assert "Node 22" in result.stderr and "PATH" in result.stderr and "./start" in result.stderr
        assert (root / "trace").read_text().splitlines() == ["venv"]
    else:
        assert result.returncode == 0, result.stderr
        trace = (root / "trace").read_text().splitlines()
        assert f"pnpm-node={version}" in trace
        assert trace[-1] == version
        assert "continued" in trace
        environment["PATH"] = f"{root / '.venv/bin'}:{environment['PATH']}"
        result = subprocess.run(["/bin/bash", str(root / "scripts/run_local.sh"), "--web"],
                                env=environment, capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        trace = (root / "trace").read_text().splitlines()
        assert trace.count("venv") == 1 and trace.count("pip") == 4
        assert trace.count("continued") == 2 and trace[-1] == version


def start_checkout(tmp_path, domain, platform="Linux", node_version="22.23.4"):
    root = tmp_path / domain
    root.mkdir()
    (root / "start").write_text((ROOT / "start").read_text())
    (root / "src-tauri").mkdir()
    (root / "src-tauri/Cargo.lock").write_text((ROOT / "src-tauri/Cargo.lock").read_text())
    tools = root / "tools"
    tools.mkdir()
    for name in ("bash", "sh", "dirname", "awk", "mkdir", "sleep", "cat", "chmod", "mktemp", "rm", "tar", "mv", "ln",
                 "cksum", "date", "ps", "gzip"):
        (tools / name).symlink_to(Path("/usr/bin") / name)
    commands = {
        "uname": f'if [[ "$1" == -m ]]; then echo arm64; else echo {platform}; fi',
        "python3": "exit 0",
        "node": f'''if [[ "$1" == --version || "$1" == -v ]]; then echo v{node_version};
elif [[ "$*" == *'=== 22'* ]]; then [[ "{node_version.split('.')[0]}" == 22 ]];
else [[ "{node_version.split('.')[0]}" =~ ^[0-9]+$ ]] && (( {node_version.split('.')[0] if node_version.split('.')[0].isdigit() else 0} >= 22 )); fi''',
        "pnpm": "echo 10.11.0",
        "cargo": 'if [[ "$1" == --version ]]; then echo cargo 1.89.0; else echo tauri-cli 2.11.5; fi',
        "rustc": "echo rustc 1.89.0", "rustup": "exit 0", "npm": "echo 10.9.2",
        "pkg-config": "exit 0", "cc": "exit 0", "make": "exit 0", "patchelf": "exit 0",
        "xdg-open": "exit 0", "curl": "exit 0", "xcrun": "exit 0", "brew": "exit 0",
        "apt-get": "exit 0", "ldd": "exit 0", "otool": "exit 0", "dpkg": "exit 0", "dpkg-deb": "exit 0",
    }
    for name, body in commands.items():
        tool = tools / name
        tool.write_text(f'#!/bin/bash\nprintf "%s\\n" "{name} $*" >> "$CHECK_TRACE"\n{body}\n')
        tool.chmod(0o755)
    (root / "home").mkdir()
    environment = {"PATH": str(tools), "HOME": str(root / "home"), "CHECK_TRACE": str(root / "trace"), "DISPLAY": ":1"}
    return root, tools, environment


@pytest.mark.parametrize("domain", ["inventory.example", "cycling.example"])
@pytest.mark.parametrize("platform, flags, missing, expected", [
    ("Linux", ("--web",), ("cargo", "rustc", "ldd", "dpkg", "dpkg-deb"), "No app was built"),
    ("Linux", ("--build",), ("dpkg-deb",), "dpkg"),
    ("Linux", (), ("ldd",), "libc-bin"),
    ("Darwin", (), ("otool",), "xcode-select --install"),
    ("Linux", (), ("venv",), "ensurepip"),
    ("Linux", (), ("npm", "pnpm"), "npm"),
    ("Linux", ("--build",), ("ca",), "SSL_CERT_FILE"),
    ("Linux", (), ("old_rust",), "rustup update stable"),
])
def test_start_checks_selected_mode_without_building(tmp_path, domain, platform, flags, missing, expected):
    root, tools, environment = start_checkout(tmp_path, domain, platform)
    for name in missing:
        if name == "venv":
            write_startup_tool(tools / "python3", '[[ "$*" != *ensurepip* ]]')
        elif name == "old_rust":
            write_startup_tool(tools / "rustc", 'echo rustc 1.88.0')
        elif name == "ca":
            environment["SSL_CERT_FILE"] = str(root / "missing-certificates.pem")
        else:
            (tools / name).unlink(missing_ok=True)
    result = subprocess.run(["/bin/bash", str(root / "start"), "--check", *flags], env=environment,
                            capture_output=True, text=True, timeout=15)
    assert (result.returncode == 0) == (flags == ("--web",)), result.stdout + result.stderr
    assert expected in result.stdout + result.stderr
    assert "Install these now?" not in result.stdout


@pytest.mark.parametrize("domain", ["inventory.example", "cycling.example"])
@pytest.mark.parametrize("manager, bundles", [("apt-get", "deb,appimage"), ("dnf", "appimage"), ("pacman", "appimage")])
def test_start_build_selects_distro_bundles(tmp_path, domain, manager, bundles):
    root, tools, environment = start_checkout(tmp_path, domain)
    if manager != "apt-get":
        write_startup_tool(tools / manager, "exit 0")
        (tools / "dpkg").unlink()
        (tools / "dpkg-deb").unlink()
    runtime = root / "home/.local/share/node-v22.23.4-linux-arm64/bin/node"
    write_startup_tool(runtime, "echo v22.23.4")
    write_startup_tool(root / "scripts/run_local.sh", '[[ "$1" == --build && "$SUPYMEM_LINUX_BUNDLES" == "$EXPECTED_BUNDLES" ]]')
    result = subprocess.run(["/bin/bash", str(root / "start"), "--build"],
                            env={**environment, "EXPECTED_BUNDLES": bundles}, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("domain", ["inventory.example", "cycling.example"])
@pytest.mark.parametrize("platform", ["Darwin", "Linux"])
@pytest.mark.parametrize("version", ["21.9.0", "22.0.0", "25.1.0", "30.0.1", "broken"])
def test_start_accepts_node_22_and_newer(tmp_path, domain, platform, version):
    root, _, environment = start_checkout(tmp_path, domain, platform, version)
    result = subprocess.run(["/bin/bash", str(root / "start"), "--check"], env=environment,
                            capture_output=True, text=True)
    assert (result.returncode == 0) == (version in {"22.0.0", "25.1.0", "30.0.1"}), result.stdout + result.stderr


@pytest.mark.parametrize("domain", ["inventory.example", "cycling.example"])
@pytest.mark.parametrize("answer,options,automatic,accepted", [
    ("y\n", [], "0", True), ("\n", [], "0", True), ("n\n", [], "0", False),
    ("", [], "0", False), ("", ["--yes"], "0", True), ("", [], "1", True),
])
def test_start_installs_only_after_consent_and_continues(tmp_path, domain, answer, options, automatic, accepted):
    root, tools, environment = start_checkout(tmp_path, domain)
    cargo = tools / "cargo"
    cargo.write_text('''#!/bin/bash
printf '%s\\n' "cargo $*" >> "$CHECK_TRACE"
if [[ "$1" == --version ]]; then echo cargo 1.89.0;
elif [[ "$1" == install ]]; then touch "$HOME/tauri-installed";
elif [[ -f "$HOME/tauri-installed" ]]; then echo tauri-cli 2.11.5;
else echo tauri-cli 2.0.0; fi
''')
    (tools / "touch").symlink_to("/usr/bin/touch")
    (root / "scripts").mkdir()
    (root / "scripts/run_local.sh").write_text('printf "launch %s\\n" "$1" >> "$CHECK_TRACE"\n')
    environment["SUPYMEM_START_YES"] = automatic
    result = subprocess.run(["/bin/bash", str(root / "start"), *options], env=environment, input=answer,
                            capture_output=True, text=True)
    trace = (root / "trace").read_text()
    assert (result.returncode == 0) == accepted, result.stdout + result.stderr
    assert ("cargo install tauri-cli --version 2.11.5 --locked" in trace) == accepted
    assert ("launch --desktop" in trace) == accepted
    assert result.stdout.count("Install these now? [Y/n]") == (0 if options or automatic == "1" else 1)


@pytest.mark.parametrize("domain", ["inventory.example", "cycling.example"])
@pytest.mark.parametrize("platform,manager", [
    ("Linux", "apt-get"), ("Linux", "dnf"), ("Linux", "pacman"),
    ("Darwin", "brew"), ("Darwin", "bootstrap"),
])
def test_start_installs_missing_tools_for_each_supported_platform(tmp_path, domain, platform, manager):
    root, tools, environment = start_checkout(tmp_path, domain, platform)
    environment["TOOLS"] = str(tools)
    (tools / "cp").symlink_to("/usr/bin/cp")
    (tools / "touch").symlink_to("/usr/bin/touch")
    for name in ("python3", "node", "pnpm", "cargo", "rustc", "rustup", "pkg-config"):
        (tools / name).rename(tools / f"{name}.saved")
    python = tools / "python3.saved"
    python.write_text('''#!/bin/bash
if [[ "$1" == - ]]; then
  printf 'download node\\n' >> "$CHECK_TRACE"
  mkdir -p "$2/bin"
  cp "$TOOLS/node.saved" "$2/bin/node"
fi
''')
    cargo = tools / "cargo.saved"
    cargo.write_text('''#!/bin/bash
printf '%s\\n' "cargo $*" >> "$CHECK_TRACE"
if [[ "$1" == --version ]]; then echo cargo 1.89.0;
elif [[ "$1" == install ]]; then touch "$HOME/tauri-installed";
elif [[ -f "$HOME/tauri-installed" ]]; then echo tauri-cli 2.11.5;
else exit 1; fi
''')
    corepack = tools / "corepack.saved"
    corepack.write_text('''#!/bin/bash
printf '%s\\n' "corepack $*" >> "$CHECK_TRACE"
if [[ "$1" == prepare ]]; then cp "$TOOLS/pnpm.saved" "$HOME/.local/bin/pnpm"; fi
''')
    corepack.chmod(0o755)
    npm = tools / "npm"
    npm.write_text('#!/bin/bash\ncp "$TOOLS/corepack.saved" "$HOME/.local/bin/corepack"\n')
    npm.chmod(0o755)
    uv = tools / "uv.saved"
    uv.write_text('#!/bin/bash\nmkdir -p "$HOME/.local/bin"\ncp "$TOOLS/python3.saved" "$HOME/.local/bin/python3.12"\n')
    uv.chmod(0o755)
    brew = tools / "brew"
    brew.write_text('''#!/bin/bash
printf '%s\\n' "brew $*" >> "$CHECK_TRACE"
if [[ "$1" == --prefix ]]; then echo "$HOME/brew";
else mkdir -p "$HOME/brew/bin"; cp "$TOOLS/python3.saved" "$HOME/brew/bin/python3.12"; fi
''')
    if manager == "bootstrap":
        brew.rename(tools / "brew.saved")
        sudo = tools / "sudo"
        sudo.write_text('#!/bin/bash\nprintf "sudo %s\\n" "$*" >> "$CHECK_TRACE"\n')
        sudo.chmod(0o755)
    (tools / "curl").write_text('''#!/bin/bash
printf '%s\\n' "curl $*" >> "$CHECK_TRACE"
case "$*" in
  *Homebrew*) echo 'cp "$TOOLS/brew.saved" "$TOOLS/brew"' ;;
  *astral*) echo 'cp "$TOOLS/uv.saved" "$TOOLS/uv"' ;;
  *rustup*) echo 'mkdir -p "$HOME/.cargo"; touch "$HOME/.cargo/env"; for name in cargo rustc rustup; do cp "$TOOLS/$name.saved" "$TOOLS/$name"; done' ;;
esac
''')
    if platform == "Linux":
        (tools / "apt-get").unlink()
        package_manager = tools / manager
        package_manager.write_text(f'''#!/bin/bash
printf '%s\\n' "{manager} $*" >> "$CHECK_TRACE"
cp "$TOOLS/pkg-config.saved" "$TOOLS/pkg-config"
''')
        package_manager.chmod(0o755)
        sudo = tools / "sudo"
        sudo.write_text('#!/bin/bash\nprintf "sudo %s\\n" "$*" >> "$CHECK_TRACE"\nexec "$@"\n')
        sudo.chmod(0o755)
    else:
        (tools / "xcrun").rename(tools / "xcrun.saved")
        xcode = tools / "xcode-select"
        xcode.write_text('#!/bin/bash\ncp "$TOOLS/xcrun.saved" "$TOOLS/xcrun"\n')
        xcode.chmod(0o755)
    (root / "scripts").mkdir()
    (root / "scripts/run_local.sh").write_text('printf "launch %s\\n" "$1" >> "$CHECK_TRACE"\n')
    result = subprocess.run(["/bin/bash", str(root / "start")], env=environment, input="y\n",
                            capture_output=True, text=True, timeout=10)
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    trace = (root / "trace").read_text()
    assert output.count("Install these now? [Y/n]") == 1
    assert "download node" in trace and "launch --desktop" in trace
    assert "corepack prepare pnpm@10.11.0 --activate" in trace
    assert "cargo install tauri-cli --version 2.11.5 --locked" in trace
    if platform == "Linux":
        assert f"{manager} " in trace and "webkit2gtk" in output
        assert "sudo " in output if os.geteuid() else "sudo " not in trace
    else:
        assert "brew install python@3.12" in trace
        assert ("sudo -v" in trace) == (manager == "bootstrap")
        assert ("Homebrew/install" in trace) == (manager == "bootstrap")
    before = trace.count("download node")
    again = subprocess.run(["/bin/bash", str(root / "start")], env=environment, input="",
                           capture_output=True, text=True, timeout=10)
    assert again.returncode == 0, again.stdout + again.stderr
    assert "Install these now?" not in again.stdout
    assert (root / "trace").read_text().count("download node") == before


@pytest.mark.parametrize("domain", ["inventory.example", "cycling.example"])
@pytest.mark.parametrize("mode", ["--build", "--web", None])
@pytest.mark.parametrize("cached", [False, True])
def test_start_only_pins_the_installer_node_runtime(tmp_path, domain, mode, cached):
    root, tools, environment = start_checkout(tmp_path, domain, node_version="25.1.0")
    runtime = root / "home/.local/share/node-v22.23.4-linux-arm64/bin/node"
    if cached:
        runtime.parent.mkdir(parents=True)
        runtime.write_text('#!/bin/bash\necho v22.23.4\n')
        runtime.chmod(0o755)
    (tools / "python3").write_text('''#!/bin/bash
if [[ "$1" == - ]]; then
  printf 'download node\\n' >> "$CHECK_TRACE"
  mkdir -p "$2/bin"
  printf '#!/bin/bash\\necho v22.23.4\\n' > "$2/bin/node"
  chmod +x "$2/bin/node"
fi
''')
    (root / "scripts").mkdir()
    (root / "scripts/run_local.sh").write_text('printf "launch %s %s\\n" "$1" "$(node --version)" >> "$CHECK_TRACE"\n')
    result = subprocess.run(["/bin/bash", str(root / "start"), *([mode] if mode else [])], env=environment,
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    trace = (root / "trace").read_text()
    assert f"launch {mode or '--desktop'} v{'22.23.4' if mode == '--build' else '25.1.0'}" in trace
    assert ("download node" in trace) == (mode == "--build" and not cached)


@pytest.mark.parametrize("domain", ["inventory.example", "cycling.example"])
@pytest.mark.parametrize("installer", ["failed", "incomplete"])
def test_start_failed_install_names_next_step_without_retrying_forever(tmp_path, domain, installer):
    root, tools, environment = start_checkout(tmp_path, domain)
    (tools / "cargo").write_text(f'''#!/bin/bash
printf '%s\\n' "cargo $*" >> "$CHECK_TRACE"
if [[ "$1" == --version ]]; then echo cargo 1.89.0; exit; fi
if [[ "$1" == install ]]; then exit {1 if installer == 'failed' else 0}; fi
echo tauri-cli 2.0.0
''')
    result = subprocess.run(["/bin/bash", str(root / "start"), "--yes"], env=environment,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode != 0
    assert "rerun ./start" in result.stderr
    assert (root / "trace").read_text().count("cargo install") == 1


@pytest.mark.parametrize("domain", ["inventory.example", "cycling.example"])
def test_start_auto_yes_environment_does_not_override_terminal_consent(tmp_path, domain):
    root, tools, environment = start_checkout(tmp_path, domain)
    (tools / "pnpm").unlink()
    environment["SUPYMEM_START_YES"] = "1"
    terminal, input_terminal = os.openpty()
    try:
        os.write(terminal, b"n\n")
        result = subprocess.run(["/bin/bash", str(root / "start")], env=environment, stdin=input_terminal,
                                capture_output=True, text=True, timeout=10)
    finally:
        os.close(terminal)
        os.close(input_terminal)
    assert result.returncode != 0
    assert "Install these now? [Y/n]" in result.stdout
    assert "Nothing was installed" in result.stdout


@pytest.mark.parametrize("domain", ["inventory.example", "cycling.example"])
@pytest.mark.parametrize("yes_option", [False, True])
def test_start_check_never_downloads_or_installs_even_with_auto_yes(tmp_path, domain, yes_option):
    root, tools, environment = start_checkout(tmp_path, domain)
    (tools / "pnpm").write_text('''#!/bin/bash
if [[ "$COREPACK_ENABLE_NETWORK" != 0 ]]; then
  printf 'unexpected download\\n' >> "$CHECK_TRACE"
fi
exit 1
''')
    environment["SUPYMEM_START_YES"] = "1"
    result = subprocess.run(["/bin/bash", str(root / "start"), "--check", *(["--yes"] if yes_option else [])],
                            env=environment, input="", capture_output=True, text=True)
    assert result.returncode != 0
    assert "pnpm 10.11.0" in result.stdout
    assert "Install these now?" not in result.stdout
    assert "unexpected download" not in (root / "trace").read_text()
    assert "corepack prepare" not in (root / "trace").read_text()


@pytest.mark.parametrize("domain", ["inventory.example", "cycling.example"])
def test_one_command_start_is_documented_exported_and_keeps_build_caches_private(domain):
    files = (ROOT / "scripts/make_public_tree.sh").read_text().split('files = set("""', 1)[1].split('"""', 1)[0].split()
    assert "start" in files
    assert "/.desktop-python-*/" in (ROOT / ".gitignore").read_text().splitlines()
    for path in ("install.md", "QUICKSTART.md"):
        introduction = " ".join((ROOT / "docs/self-hosting" / path).read_text().split("\n## ", 2)[1].split())
        for command in ("./start", "--check", "--build", "--web", "--yes", ".env", "Keys and connections"):
            assert command in introduction
        assert ".\\start.ps1" in introduction or "wsl -d Ubuntu -- bash -lc" in introduction
        assert domain not in introduction


@pytest.mark.parametrize("domain", ["inventory.example", "cycling.example"])
@pytest.mark.parametrize("state", ["valid", "corrupt", "unsafe"])
def test_start_node_download_is_verified_and_extracted_safely(tmp_path, domain, state, monkeypatch):
    import hashlib
    import io
    import tarfile
    import urllib.request

    destination = tmp_path / domain / "node-v22.23.4-linux-arm64"
    archive = destination.name + ".tar.gz"
    content = io.BytesIO()
    with tarfile.open(fileobj=content, mode="w:gz") as output:
        member = tarfile.TarInfo("../../escape" if state == "unsafe" else destination.name + "/bin/node")
        member.size = 4
        output.addfile(member, io.BytesIO(b"node"))
    payload = content.getvalue()
    digest = hashlib.sha256(payload).hexdigest() if state != "corrupt" else "0" * 64
    monkeypatch.setattr(urllib.request, "urlopen", lambda url, **kwargs: io.BytesIO(
        f"{digest}  {archive}\n".encode() if url.endswith("SHASUMS256.txt") else payload))
    monkeypatch.setattr("sys.argv", ["-", str(destination), "22.23.4", "linux", "arm64"])
    source = (ROOT / "start").read_text().split("<<'NODE_RUNTIME'\n", 1)[1].split("\nNODE_RUNTIME", 1)[0]
    if state == "valid":
        exec(compile(source, str(ROOT / "start"), "exec"), {})
        assert (destination / "bin/node").read_bytes() == b"node"
    else:
        with pytest.raises(SystemExit if state == "corrupt" else tarfile.FilterError):
            exec(compile(source, str(ROOT / "start"), "exec"), {})
        assert not destination.exists()
        assert not (tmp_path / "escape").exists()


@pytest.mark.parametrize("domain", ["inventory.example", "cycling.example"])
@pytest.mark.parametrize("platform", ["Darwin", "Linux", "MINGW64_NT"])
@pytest.mark.parametrize("state", ["ready", "missing", "old_versions"])
def test_start_checks_prerequisites_without_installing(tmp_path, domain, platform, state):
    root = tmp_path / domain
    root.mkdir()
    script = root / "start"
    script.write_text((ROOT / "start").read_text())
    (root / "src-tauri").mkdir()
    (root / "src-tauri/Cargo.lock").write_text((ROOT / "src-tauri/Cargo.lock").read_text())
    tools = root / "tools"
    tools.mkdir()
    trace = root / "trace"
    commands = {"uname": f'if [[ "$1" == -m ]]; then echo x86_64; else echo {platform}; fi',
                "dirname": '/usr/bin/dirname "$@"', "awk": '/usr/bin/awk "$@"', "apt-get": "exit 0"}
    for name in ("bash", "sh", "cat", "cksum", "date", "ln", "mkdir", "mv", "ps", "tar", "gzip"):
        (tools / name).symlink_to(Path("/usr/bin") / name)
    if platform == "MINGW64_NT":
        (root / "start.ps1").write_text((ROOT / "start.ps1").read_text())
        commands["powershell.exe"] = 'printf "%s\\n" "Your prerequisites are ready."' if state == "ready" else 'printf "%s\\n" "Install Python 3.12 and Node 22, then rerun .\\start.ps1 --check."; exit 1'
    if state != "missing":
        commands.update({
            "python3": "exit " + ("1" if state == "old_versions" else "0"),
            "node": "echo " + ("v21.0.0" if state == "old_versions" else "v22.23.4"),
            "pnpm": "echo 10.11.0", "cargo": 'if [[ "$1" == --version ]]; then echo cargo 1.89.0; else echo tauri-cli 2.11.5; fi',
            "rustc": "echo rustc 1.89.0", "rustup": "exit 0", "ldd": "exit 0", "otool": "exit 0",
            "pkg-config": "exit 0", "xcrun": "exit 0", "xcode-select": "exit 0",
            "cc": "exit 0", "make": "exit 0", "patchelf": "exit 0", "xdg-open": "exit 0",
            "curl": "exit 0",
        })
    for name, body in commands.items():
        tool = tools / name
        tool.write_text(f'#!/bin/bash\nprintf "%s\\n" "{name} $*" >> "$CHECK_TRACE"\n{body}\n')
        tool.chmod(0o755)
    result = subprocess.run(["/bin/bash", str(script), "--check"], cwd=root,
                            env={"PATH": str(tools), "CHECK_TRACE": str(trace)}, capture_output=True, text=True)
    output = result.stdout + result.stderr
    assert "install --frozen-lockfile" not in trace.read_text()
    assert not (root / ".venv").exists()
    if platform == "MINGW64_NT":
        assert (result.returncode == 0) == (state == "ready")
        assert f"powershell.exe -NoProfile -ExecutionPolicy Bypass -File {root}/start.ps1 --check" in trace.read_text()
        assert "ready" in output if state == "ready" else "start.ps1 --check" in output
    elif state == "ready":
        assert result.returncode == 0, output
        assert "ready" in output.lower()
    else:
        assert result.returncode != 0
        for name in ("Python 3.12", "Node 22"):
            assert name in output
        assert ("brew install" if platform == "Darwin" else "uv python install 3.12") in output
        assert "./start" in output


@pytest.mark.parametrize("domain", ["inventory.example", "cycling.example"])
def test_public_desktop_commands_select_local_stack(domain):
    import json

    package = json.loads((ROOT / "apps/desktop/package.json").read_text())
    assert package["scripts"]["dev"] == "../../start"
    assert package["scripts"]["build"] == "../../start --build"
    assert package["scripts"]["tauri"] == "../../start"
    makefile = (ROOT / "Makefile").read_text()
    for target, command in (("desktop-dev", "./start"), ("desktop-build", "./start --build")):
        assert f"{target}:\n\t{command}\n" in makefile
    assert "desktop-build-hosted: desktop-runtime-hosted" in makefile
    assert domain not in package["scripts"]["dev"]


@pytest.mark.parametrize("domain", ["inventory.example", "cycling.example"])
def test_legacy_supervisors_start_worker(domain):
    script = (ROOT / "scripts/dev.py").read_text()
    supervisor = (ROOT / "src-tauri/src/supervisor.rs").read_text()
    assert '"app.worker.main"' in script
    assert '("worker", 3301_u16, "/health")' in supervisor
    assert '.env("WORKER_HEALTH_PORT", port.to_string())' in supervisor
    assert domain not in script


@pytest.mark.parametrize("domain", ["inventory.example", "cycling.example"])
def test_local_dependency_failure_keeps_data_and_names_recovery_step(tmp_path, domain):
    root, tools, environment = start_checkout(tmp_path, domain)
    (root / "scripts").mkdir()
    script = root / "scripts/run_local.sh"
    script.write_text((ROOT / "scripts/run_local.sh").read_text())
    for name in ("services/company_os/requirements.runtime.lock", "services/company_os/requirements.build.lock",
                 "packages/desktop_runtime/requirements.local.lock", "services/company_os/pyproject.toml",
                 "packages/core/pyproject.toml"):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(domain)
    python = root / ".venv/bin/python"
    python.parent.mkdir(parents=True)
    python.write_text('#!/bin/bash\nif [[ "$1" == -m ]]; then exit 1; fi\n')
    python.chmod(0o755)
    data = root / "data/runtime/local/local.db"
    data.parent.mkdir(parents=True)
    data.write_text(domain)
    environment["SUPYMEM_PYTHON"] = str(python)
    result = subprocess.run(["/bin/bash", str(script), "--web"], env=environment,
                            capture_output=True, text=True)
    assert result.returncode != 0
    assert "rerun ./start" in result.stderr
    assert not (root / ".venv/.local-dependencies").exists()
    assert data.read_text() == domain


@pytest.mark.parametrize("domain", ["inventory.example", "cycling.example"])
def test_local_dependency_install_is_locked_idempotent_and_refreshes_changed_locks(tmp_path, domain):
    root = tmp_path / domain
    (root / "scripts").mkdir(parents=True)
    script = root / "scripts/run_local.sh"
    script.write_text((ROOT / "scripts/run_local.sh").read_text())
    for name in ("services/company_os/requirements.runtime.lock", "services/company_os/requirements.build.lock",
                 "packages/desktop_runtime/requirements.local.lock", "services/company_os/pyproject.toml",
                 "packages/core/pyproject.toml", "apps/console/package.json", "apps/console/pnpm-lock.yaml",
                 "apps/desktop/package.json", "apps/desktop/pnpm-lock.yaml"):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(domain)
    tools = root / "tools"
    tools.mkdir()
    python = root / ".venv/bin/python"
    python.parent.mkdir(parents=True)
    python.write_text('#!/bin/bash\nif [[ "$1" == -m ]]; then printf "%s\\n" "$*" >> "$INSTALL_TRACE"; fi\n')
    python.chmod(0o755)
    for name, body in {
        "pnpm": 'if [[ "$1" == --version ]]; then echo 10.11.0; exit; fi; printf "%s\\n" "$*" >> "$INSTALL_TRACE"; mkdir -p "$2/node_modules"',
        "node": "if [[ \"$1\" == -v ]]; then echo v22.23.4; fi",
    }.items():
        tool = tools / name
        tool.write_text(f"#!/bin/bash\n{body}\n")
        tool.chmod(0o755)
    trace = root / "installs"
    environment = {"PATH": f"{tools}:/usr/bin:/bin", "SUPYMEM_PYTHON": str(python), "INSTALL_TRACE": str(trace)}
    for _run in range(2):
        result = subprocess.run(["/bin/bash", str(script), "--desktop"], cwd=root, env=environment,
                                capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
    installed = trace.read_text()
    assert installed.count("-m pip install") == 4
    assert installed.count("--require-hashes") == 3
    assert installed.count("--frozen-lockfile --node-linker=hoisted") == 1
    (root / "services/company_os/requirements.runtime.lock").write_text(domain + " changed")
    result = subprocess.run(["/bin/bash", str(script), "--desktop"], cwd=root, env=environment,
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert trace.read_text().count("-m pip install") == 8
    assert trace.read_text().count("--frozen-lockfile --node-linker=hoisted") == 1
    (root / "apps/console/pnpm-lock.yaml").write_text(domain + " changed")
    result = subprocess.run(["/bin/bash", str(script), "--desktop"], cwd=root, env=environment,
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert trace.read_text().count("-m pip install") == 8
    assert trace.read_text().count("--frozen-lockfile --node-linker=hoisted") == 2


@pytest.mark.parametrize("domain", ["manufacturing.example", "camping.example"])
def test_local_setup_forwards_supported_provider_fields_and_never_checks_updates_at_launch(domain):
    config = yaml.safe_load((ROOT / "docker-compose.local.yml").read_text())
    fields = {"SUPYMEM_LLM_PROVIDER", "OPENAI_API_KEY", "OPENAI_BASE_URL", "AZURE_OPENAI_API_KEY",
              "AZURE_OPENAI_ENDPOINT", "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL",
              "OPENAI_COMPATIBLE_API_KEY", "OPENAI_COMPATIBLE_BASE_URL",
              *("SUPYMEM_MODEL_" + role for role in ("CHAT", "PLANNING", "EXTRACTION", "VISION", "REALTIME"))}
    for service in ("core", "worker"):
        assert fields <= config["services"][service]["environment"].keys(), domain
        assert all(config["services"][service]["environment"][field] is None for field in fields)
    assert not fields & config["services"]["console"]["environment"].keys()
    native = (ROOT / "src-tauri/src/lib.rs").read_text()
    launch = native.split("// Check once on every production launch.", 1)[1].split("let crash_app", 1)[0]
    assert "if channel::updater_enabled() && !local_stack::is_local()" in launch
    help_branch = native.split('"help" => {', 1)[1].split('"report" => {', 1)[0]
    assert "local_stack::is_local()" in help_branch and "/guide" in help_branch


@pytest.mark.parametrize("domain", ["manufacturing.example", "camping.example"])
def test_hosted_setup_keeps_provider_environment_and_native_update_help(domain):
    config = yaml.safe_load((ROOT / "docker-compose.companyos.yml").read_text())
    settings = {"SUPYMEM_LLM_PROVIDER", "OPENAI_API_KEY", "OPENAI_BASE_URL", "AZURE_OPENAI_API_KEY",
                "AZURE_OPENAI_ENDPOINT", "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL",
                "OPENAI_COMPATIBLE_API_KEY", "OPENAI_COMPATIBLE_BASE_URL",
                *("SUPYMEM_MODEL_" + role for role in ("CHAT", "PLANNING", "EXTRACTION", "VISION", "REALTIME"))}
    for name in ("company_os", "worker", "console"):
        assert not set(settings) & config["services"][name]["environment"].keys(), domain
    native = (ROOT / "src-tauri/src/lib.rs").read_text()
    launch = native.split("// Check once on every production launch.", 1)[1].split("let crash_app", 1)[0]
    assert "if channel::updater_enabled() && !local_stack::is_local()" in launch
    help_branch = native.split('"help" => {', 1)[1].split('"report" => {', 1)[0]
    assert 'else {\n                let _ = open::that("https://supymem.com");' in help_branch


@pytest.fixture(params=["procurement.example", "gardening.example"])
def configured_install(request, tmp_path):
    root = tmp_path / request.param
    root.mkdir()
    template = (ROOT / ".env.example").read_text()
    (root / ".env").write_text(template + f"\nPROJECT_DOMAIN={request.param}\n")
    return root


def load_launcher(tmp_path):
    source = (ROOT / "scripts/run_local.sh").read_text()
    program = source.split("<<'PYTHON'\n", 1)[1].rsplit("\nPYTHON", 1)[0]
    module_path = tmp_path / "local_launcher.py"
    module_path.write_text(program)
    spec = importlib.util.spec_from_file_location("local_launcher_test", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return vars(module)


@pytest.fixture
def company_os_launcher(tmp_path, monkeypatch, isolate_app_imports):
    monkeypatch.syspath_prepend(str(ROOT / "services/company_os"))
    return load_launcher(tmp_path)


@pytest.mark.parametrize("platform", ["posix", "windows"])
def test_browser_maintenance_uses_the_repo_version(configured_install, monkeypatch, company_os_launcher, platform, free_web_ports):
    from scripts.desktop_version import read_repo_version

    for name, port in free_web_ports.items():
        monkeypatch.setenv(name, port)
    for name in ("COMPANY_OS_DB_URL", "DATABASE_URL", "DB_URL", "RDS_PROXY_ENDPOINT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(configured_install / "data"))
    monkeypatch.setenv("SUPYMEM_APP_VERSION", "0.0.0")
    maintenance = Mock(return_value=Mock(returncode=1))
    monkeypatch.setattr(subprocess, "run", maintenance)
    if platform == "windows":
        launch = startup_script("windows_source_version", "scripts/desktop/run_local_web.py")
        assert launch.run(configured_install, "node.exe") == 1
    else:
        monkeypatch.chdir(configured_install)
        monkeypatch.setattr(sys, "argv", ["start", "--web", "pnpm"])
        assert company_os_launcher["main"]() == 1
    maintenance.assert_called_once()
    assert maintenance.call_args.args[0][-1] == "--maintenance"
    assert maintenance.call_args.kwargs["env"]["SUPYMEM_APP_VERSION"] == read_repo_version()


@pytest.mark.parametrize("platform", ["posix", "windows"])
def test_web_launcher_defaults_openssl_conf_without_overriding_owner(configured_install, company_os_launcher, platform, free_web_ports):
    local_environment = company_os_launcher["local_environment"]
    if platform == "windows":
        local_environment = startup_script("windows_source_tls", "scripts/desktop/run_local_web.py").local_environment
    dotenv = configured_install / ".env"
    default = configured_install / "scripts/desktop/openssl.cnf"
    custom = configured_install / "owner openssl.cnf"
    with patch.dict(os.environ, free_web_ports, clear=True):
        assert all(child["OPENSSL_CONF"] == str(default) for child in local_environment(configured_install))
        dotenv.write_text(f'OPENSSL_CONF="{custom}"\n')
        assert all(child["OPENSSL_CONF"] == str(custom) for child in local_environment(configured_install))
        with patch.dict(os.environ, {"OPENSSL_CONF": ""}):
            assert all(child["OPENSSL_CONF"] == "" for child in local_environment(configured_install))


@pytest.mark.parametrize("domain", ["procurement.example", "gardening.example"])
def test_local_web_starts_a_private_bridge_on_a_free_port(tmp_path, monkeypatch, domain, company_os_launcher, free_web_ports):
    launcher = company_os_launcher
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", [domain, "--web", "pnpm"])
    monkeypatch.setitem(launcher, "supervise", lambda children: 0)
    occupied_port = free_web_ports["BRIDGE_PORT"]
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", int(occupied_port)))
        (tmp_path / ".env").write_text(f"BRIDGE_PORT={occupied_port}\nPROJECT_DOMAIN={domain}\n")
        values = {name: port for name, port in free_web_ports.items() if name != "BRIDGE_PORT"}
        with patch.dict(os.environ, values, clear=True), \
             patch.object(launcher["subprocess"], "run", return_value=Mock(returncode=0)), \
             patch.object(launcher["subprocess"], "Popen", return_value=Mock()) as spawn:
            assert launcher["main"]() == 0
    assert spawn.call_count == 4
    bridge_call = next(call for call in spawn.call_args_list if "bridge" in call.args[0])
    bridge = bridge_call.args[0]
    backend = bridge_call.kwargs["env"]
    console = next(call.kwargs["env"] for call in spawn.call_args_list if "next" in call.args[0])
    assert bridge[bridge.index("--port") + 1] == backend["BRIDGE_PORT"] != occupied_port
    assert backend["BRIDGE_AUTH_OWNER"] == "native"
    assert len(backend["BRIDGE_NATIVE_TOKEN"]) >= 32
    assert backend["BRIDGE_TOKEN"] != backend["BRIDGE_NATIVE_TOKEN"]
    assert "BRIDGE_NATIVE_TOKEN" not in console and "BRIDGE_TOKEN" not in console
    assert backend["COMPANY_OS_URL"] == backend["CORE_URL"]
    assert bridge[1:3] == ["-m", "desktop_runtime"]
    assert bridge[bridge.index("--host") + 1] == "127.0.0.1"


def test_default_compose_starts_local_services_without_changing_hosted_flags():
    default = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    assert default["include"] == ["docker-compose.local.yml"]
    config = yaml.safe_load((ROOT / "docker-compose.local.yml").read_text())
    services = config["services"]
    assert {"core", "worker", "console"} <= services.keys()
    for name in ("core", "worker", "console"):
        assert services[name]["environment"]["SUPYMEM_MODE"] == "local"
        assert "profiles" not in services[name]
        assert "CREDITS_ENFORCEMENT_ENABLED" not in services[name]["environment"]
        assert "COMPANY_OS_REQUIRE_SIGNED_CALLER" not in services[name]["environment"]
    for name in ("core", "worker"):
        assert services[name]["environment"]["LLM_API_KEY"] == "${LLM_API_KEY:-}"
    assert "LLM_API_KEY" not in services["console"]["environment"]
    assert services["core"]["volumes"] == services["worker"]["volumes"]
    assert services["worker"]["environment"]["APP_ROLE"] == "worker"
    assert services["postgres"]["profiles"] == ["postgres"]
    assert services["firecrawl"]["profiles"] == ["firecrawl"]
    assert all(port.startswith("127.0.0.1:") for service in services.values() for port in service.get("ports", []))
    assert "env_file" not in services["console"]
    documented = set(re.findall(r"^(?:# )?([A-Z][A-Z0-9_]*)=", (ROOT / ".env.example").read_text(), re.MULTILINE))
    configurable = set(re.findall(r"\$\{([A-Z][A-Z0-9_]*)", (ROOT / "docker-compose.local.yml").read_text()))
    assert configurable <= documented


def test_browser_example_matches_launcher_defaults(configured_install, company_os_launcher):
    from coding_agents.env import load_dotenv_file

    template = ROOT / ".env.local.example"
    assert not load_dotenv_file(template)
    ports = dict(re.findall(r"^# ((?:CONSOLE|CORE|WORKER_HEALTH|BRIDGE)_PORT)=(\d+)$",
                            template.read_text(), re.MULTILINE))
    assert ports.keys() == {"CONSOLE_PORT", "CORE_PORT", "WORKER_HEALTH_PORT", "BRIDGE_PORT"}
    with patch.dict(os.environ, {}, clear=True), patch.object(socket, "socket") as sockets:
        listener = sockets.return_value.__enter__.return_value
        listener.getsockname.side_effect = lambda: listener.bind.call_args.args[0]
        backend, _ = company_os_launcher["local_environment"](configured_install)
    assert {name: backend[name] for name in ports} == ports
    assert {call.args[0] for call in listener.bind.call_args_list} == {("127.0.0.1", int(port)) for port in ports.values()}


def test_local_console_uses_production_build_with_local_routing(configured_install):
    config = yaml.safe_load((ROOT / "docker-compose.local.yml").read_text())
    recipe = (ROOT / "apps/console/Dockerfile").read_text()
    target = config["services"]["console"]["build"]["target"]
    assert f"FROM runner AS {target}\n" in recipe
    stage = recipe.split(" AS runner\n", 1)[1].split("\nFROM ", 1)[0]
    assert "ENV NODE_ENV=production" in stage
    assert "COPY --from=builder --chown=nextjs:nodejs /app/.next/standalone ./" in stage
    assert 'CMD ["node", "server.js"]' in stage
    assert "command" not in config["services"]["console"]
    builder = recipe.split(" AS builder\n", 1)[1].split("\nFROM ", 1)[0]
    assert "RUN pnpm build" in builder
    arguments = config["services"]["console"]["build"]["args"]
    for name in ("SUPYMEM_MODE", "CORE_URL", "NEXT_PUBLIC_GATEWAY_URL"):
        assert arguments[name] == config["services"]["console"]["environment"][name]
        assert f"ARG {name}" in builder
        assert f"{name}=${name}" in builder


def test_local_runtime_can_create_its_process_lock(configured_install):
    config = yaml.safe_load((ROOT / "docker-compose.local.yml").read_text())
    recipe = (ROOT / "services/company_os/Dockerfile").read_text()
    for name in ("core", "worker"):
        assert config["services"][name]["build"]["target"] == "local"
        parent = Path(config["services"][name]["environment"]["COMPANY_OS_DATA_DIR"]).parent
        stage = recipe.split("FROM runtime AS local\n", 1)[1].split("\nFROM ", 1)[0]
        assert f"RUN chown appuser:appuser {parent}" in stage


@pytest.mark.parametrize("domain", ["dispatch-software.invalid", "garden-club.invalid"])
@pytest.mark.parametrize("local", [True, False])
def test_local_launchers_do_not_trust_forwarded_peer_headers(tmp_path, monkeypatch, domain, local):
    from desktop_runtime import __main__ as runtime
    import uvicorn

    if local:
        monkeypatch.setenv("SUPYMEM_MODE", "local")
    else:
        monkeypatch.delenv("SUPYMEM_MODE", raising=False)
    application = object()
    captured = []
    monkeypatch.setattr(runtime, "_load_asgi_app", lambda role: application)
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: captured.append((args, kwargs)))
    runtime.main(["--role", "core", "--data-dir", str(tmp_path / domain)])
    assert captured[0][0] == (application,)
    assert captured[0][1].get("proxy_headers", True) is not local
    if local:
        launcher = (ROOT / "scripts/run_local.sh").read_text()
        config = yaml.safe_load((ROOT / "docker-compose.local.yml").read_text())
        assert '"--no-proxy-headers"' in launcher
        assert "--no-proxy-headers" in config["services"]["core"]["command"][-1]


@pytest.mark.parametrize("domain", ["dispatch-software.invalid", "garden-club.invalid"])
def test_launcher_preserves_user_configuration_and_keeps_keys_out_of_console(tmp_path, domain, company_os_launcher, free_web_ports):
    launcher = company_os_launcher
    values = {**free_web_ports, "LLM_BASE_URL": f"https://{domain}/v1", "LLM_API_KEY": "fixture-private",
              "SESSION_SECRET": "fixture-session"}
    (tmp_path / ".env").write_text("".join(f"{name}={value}\n" for name, value in values.items()))
    with patch.dict(os.environ, {}, clear=True):
        backend, console = launcher["local_environment"](tmp_path)
    assert backend["SUPYMEM_MODE"] == console["SUPYMEM_MODE"] == "local"
    assert backend["APP_ENV"] == "local"
    assert backend["LLM_BASE_URL"] == f"https://{domain}/v1"
    assert backend["LLM_API_KEY"] == "fixture-private"
    assert "LLM_API_KEY" not in console and "SESSION_SECRET" not in console
    assert "CREDITS_ENFORCEMENT_ENABLED" not in backend
    assert "COMPANY_OS_REQUIRE_SIGNED_CALLER" not in backend
    assert backend["COMPANY_OS_DB_URL"].startswith("sqlite:///")
    assert console["CORE_URL"] == f"http://127.0.0.1:{free_web_ports['CORE_PORT']}"
    assert Path(backend["COMPANY_OS_DATA_DIR"]).is_dir()
    with patch.dict(os.environ, {"LLM_BASE_URL": f"https://override.{domain}/v1"}, clear=True):
        backend, _ = launcher["local_environment"](tmp_path)
    assert backend["LLM_BASE_URL"] == f"https://override.{domain}/v1"


def test_launcher_rejects_invalid_or_colliding_ports_before_starting(tmp_path, company_os_launcher, free_web_ports):
    launcher = company_os_launcher
    for values in ({"CORE_PORT": "bad"}, {"CORE_PORT": "0"}, {"CONSOLE_PORT": "65536"}):
        with patch.dict(os.environ, {**free_web_ports, **values}, clear=True), pytest.raises(ValueError, match="port"):
            launcher["local_environment"](tmp_path)
    for name in ("CONSOLE_PORT", "WORKER_HEALTH_PORT"):
        values = {**free_web_ports, "CORE_PORT": free_web_ports[name]}
        with patch.dict(os.environ, values, clear=True), pytest.raises(ValueError, match=f"{name} and CORE_PORT both use port"):
            launcher["local_environment"](tmp_path)


@pytest.mark.parametrize("domain", ["procurement.example", "gardening.example"])
def test_local_console_uses_its_own_origin_and_compose_keeps_api_calls_on_loopback(tmp_path, domain, company_os_launcher, free_web_ports):
    launcher = company_os_launcher
    values = {**free_web_ports, "PROJECT_DOMAIN": domain}
    (tmp_path / ".env").write_text("".join(f"{name}={value}\n" for name, value in values.items()))
    with patch.dict(os.environ, {}, clear=True):
        backend, console = launcher["local_environment"](tmp_path)
    assert console["NEXT_PUBLIC_GATEWAY_URL"] == backend["CONSOLE_ORIGIN"] == f"http://127.0.0.1:{free_web_ports['CONSOLE_PORT']}"
    config = yaml.safe_load((ROOT / "docker-compose.local.yml").read_text())
    services = config["services"]
    for name in ("worker", "console"):
        assert services[name]["network_mode"] == "service:core"
        assert services[name]["environment"]["CORE_URL"] == "http://127.0.0.1:3300"
    assert services["worker"]["environment"]["WORKER_HEALTH_PORT"] == "3301"
    assert services["worker"]["environment"]["PORT"] == services["worker"]["environment"]["WORKER_HEALTH_PORT"]
    assert "ports" not in services["console"]
    assert "127.0.0.1:${CONSOLE_PORT:-3100}:3100" in services["core"]["ports"]


def test_launcher_terminates_and_waits_for_every_child_on_exit(tmp_path):
    launcher = load_launcher(tmp_path)
    children = [Mock(), Mock(), Mock()]
    children[0].poll.return_value = 7
    children[1].poll.return_value = None
    children[2].poll.return_value = None
    with patch.object(launcher["time"], "sleep"), patch.object(launcher["signal"], "signal"), patch.object(launcher["os"], "killpg") as kill_group:
        assert launcher["supervise"](children) == 7
    assert kill_group.call_count == len(children)
    for child in children:
        child.wait.assert_called_once()


@pytest.mark.parametrize("domain", ["inventory.example", "cycling.example"])
@pytest.mark.parametrize("build", [False, True])
def test_desktop_launcher_prepares_local_command_and_stops_on_exit(tmp_path, monkeypatch, domain, build):
    launcher = load_launcher(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SUPYMEM_LINUX_BUNDLES", raising=False)
    monkeypatch.setattr(launcher["sys"], "argv", [domain, "--build" if build else "--desktop", "pnpm"])
    child = Mock(pid=12345678)
    child.poll.return_value = 0
    with patch.object(launcher["subprocess"], "Popen", return_value=child) as spawn, \
         patch.object(launcher["subprocess"], "run", return_value=Mock(stdout="")), \
         patch.object(launcher["signal"], "signal"), patch.object(launcher["os"], "killpg") as kill_group:
        assert launcher["main"]() == 0
    command = spawn.call_args.args[0]
    assert command[:2] == ["cargo", "tauri"]
    assert command[2:7] == ["build" if build else "dev", "--config", "tauri.local.json", "--features", "local-stack"]
    assert ("--no-dev-server-wait" in command) is not build
    if build:
        assert command[-2:] == ["--bundles", "app,dmg" if launcher["sys"].platform == "darwin" else "appimage"]
    assert spawn.call_args.kwargs["env"]["SUPYMEM_MODE"] == "local"
    assert spawn.call_args.kwargs["start_new_session"] is True
    assert spawn.call_args.kwargs["cwd"] == tmp_path / "src-tauri"
    kill_group.assert_called_once()
    child.wait.assert_called_once()


@pytest.mark.parametrize("domain", ["inventory.example", "cycling.example"])
def test_launcher_stops_owned_descendants_in_separate_sessions_not_other_apps(tmp_path, domain):
    launcher = load_launcher(tmp_path)
    child = Mock(pid=10)
    processes = Mock(stdout="10 1\n11 10\n12 11\n20 1\n")
    with patch.object(launcher["subprocess"], "run", return_value=processes), \
         patch.object(launcher["os"], "getpgid", side_effect=lambda pid: 12 if pid == 12 else 10), \
         patch.object(launcher["os"], "getpgrp", return_value=99), \
         patch.object(launcher["os"], "killpg") as kill_group:
        launcher["stop_children"]([child])
    assert {call.args[0] for call in kill_group.call_args_list} == {10, 12}
    child.wait.assert_called_once()
    assert domain not in processes.stdout


@pytest.mark.parametrize("domain", ["procurement.example", "gardening.example"])
def test_worker_health_bind_changes_only_in_local_mode(tmp_path, monkeypatch, isolate_app_imports, domain):
    monkeypatch.syspath_prepend(str(ROOT / "services/company_os"))
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COMPANY_OS_WORKING_MEMORY_PATH", str(tmp_path / "memory.db"))
    monkeypatch.setenv("WORKER_HEALTH_PORT", "3301")
    monkeypatch.setenv("SUPYMEM_DESKTOP_INSTANCE_ID", domain)
    from app.worker.main import _start_health

    with patch("http.server.ThreadingHTTPServer") as server, patch("threading.Thread"):
        for mode, address in (("local", "127.0.0.1"), (None, "0.0.0.0")):
            if mode:
                monkeypatch.setenv("SUPYMEM_MODE", mode)
            else:
                monkeypatch.delenv("SUPYMEM_MODE", raising=False)
            _start_health(domain)
            assert server.call_args.args[0] == (address, 3301)
            import io
            import json
            from email.message import Message
            from types import SimpleNamespace

            handler = server.call_args.args[1].__new__(server.call_args.args[1])
            handler.path = "/health"
            handler.wfile = io.BytesIO()
            handler.send_response = Mock()
            handler.send_header = Mock()
            handler.end_headers = Mock()
            handler.send_error = Mock()
            handler.server = SimpleNamespace(server_port=3301)
            handler.headers = Message()
            handler.headers["Host"] = "localhost:3301"
            handler.do_GET()
            payload = json.loads(handler.wfile.getvalue())
            assert payload.get("desktop_instance") == (domain if mode else None)
            handler.headers.replace_header("Host", f"{domain}:3301")
            for path in ("/health", "/ready"):
                handler.path = path
                handler.do_GET()
                if mode:
                    assert handler.send_error.call_args.args[0] == 403
                    handler.send_error.reset_mock()
                else:
                    handler.send_error.assert_not_called()


@pytest.mark.parametrize("domain", ["procurement.example", "gardening.example"])
def test_desktop_worker_entry_uses_the_existing_worker_without_importing_api(tmp_path, monkeypatch, isolate_app_imports, domain):
    monkeypatch.syspath_prepend(str(ROOT / "services/company_os"))
    from desktop_runtime import __main__ as launcher

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("PROJECT_DOMAIN", domain)
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COMPANY_OS_WORKING_MEMORY_PATH", str(tmp_path / "memory.db"))
    monkeypatch.setenv("WORKER_HEALTH_PORT", "3301")
    monkeypatch.setattr(launcher, "_ensure_service_on_path", Mock())
    monkeypatch.setattr(launcher, "_load_asgi_app", Mock(side_effect=AssertionError("API must not load")))
    with patch("app.worker.main.main", return_value=0) as worker:
        launcher.main(["--role", "worker", "--port", "3301"])
    worker.assert_called_once_with([])
    assert os.environ["WORKER_HEALTH_PORT"] == "3301"
    monkeypatch.delenv("SUPYMEM_MODE")
    with pytest.raises(SystemExit, match="local mode"):
        launcher.main(["--role", "worker"])


@pytest.mark.parametrize("domain", ["procurement.example", "gardening.example"])
def test_core_health_proves_local_desktop_ownership_only(tmp_path, monkeypatch, isolate_app_imports, domain):
    monkeypatch.syspath_prepend(str(ROOT / "services/company_os"))
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    from app.health import health_response
    from core.health import liveness_payload

    monkeypatch.setenv("SUPYMEM_DESKTOP_INSTANCE_ID", domain)
    for mode in ("local", "hosted"):
        monkeypatch.setenv("SUPYMEM_MODE", mode)
        assert health_response().get("desktop_instance") == (domain if mode == "local" else None)
        for service in ("core", "worker", "gateway", "warden", "builder"):
            assert liveness_payload(service).get("desktop_instance") == (domain if mode == "local" else None)


def test_example_database_uses_persistent_defaults(configured_install, company_os_launcher, free_web_ports):
    from coding_agents.env import load_dotenv_file
    from sqlalchemy.engine import make_url

    environment = load_dotenv_file(configured_install / ".env")
    config = yaml.safe_load((ROOT / "docker-compose.local.yml").read_text())
    for name in ("core", "worker"):
        service = config["services"][name]
        default = re.fullmatch(r"\$\{COMPANY_OS_DB_URL:-(.+)\}", service["environment"]["COMPANY_OS_DB_URL"])[1]
        database = make_url(environment.get("COMPANY_OS_DB_URL") or default).database
        mount = service["volumes"][0].split(":", 1)[1]
        assert Path(database).is_relative_to(mount)
    with patch.dict(os.environ, free_web_ports, clear=True):
        backend, _ = company_os_launcher["local_environment"](configured_install)
    assert Path(make_url(backend["COMPANY_OS_DB_URL"]).database).is_relative_to(backend["COMPANY_OS_DATA_DIR"])


def test_example_configures_real_local_providers(configured_install, monkeypatch, isolate_app_imports):
    from coding_agents.env import load_dotenv_file

    monkeypatch.syspath_prepend(str(ROOT / "services/company_os"))
    from app.core.providers_local import PROVIDER_ENV, load_local_config

    template = (configured_install / ".env").read_text()
    environment = load_dotenv_file(configured_install / ".env")
    assert "SUPYMEM_LLM_PROVIDER" not in environment
    for provider, (key_name, url_name) in PROVIDER_ENV.items():
        settings = {
            "SUPYMEM_LLM_PROVIDER": provider,
            key_name: "fixture-private",
            url_name: f"https://{configured_install.name}/v1",
            "SUPYMEM_MODEL_CHAT": "fixture-model",
        }
        source = template
        for name, value in settings.items():
            assert f"# {name}=" in source
            source = source.replace(f"# {name}=", f"{name}={value}")
        (configured_install / ".env").write_text(source)
        environment = load_dotenv_file(configured_install / ".env")
        config = yaml.safe_load((ROOT / "docker-compose.local.yml").read_text())
        for name in ("core", "worker"):
            assert config["services"][name]["env_file"] == [{"path": ".env", "required": False}]
            assert all(field in config["services"][name]["environment"] and
                       config["services"][name]["environment"][field] is None for field in settings)
        with patch.dict(os.environ, {**environment, "COMPANY_OS_DATA_DIR": str(configured_install)}, clear=True):
            provider_config = load_local_config()
        assert provider_config.provider == ("azure_foundry" if provider == "azure_openai" else provider)
        assert provider_config.api_key.get_secret_value() == settings[key_name]
        assert provider_config._local_role_models["chat"] == settings["SUPYMEM_MODEL_CHAT"]


def test_bundled_firecrawl_uses_an_accepted_private_endpoint(configured_install, monkeypatch, isolate_app_imports):
    monkeypatch.syspath_prepend(str(ROOT / "services/company_os"))
    from app.research.local_providers import _reader_endpoint
    local_secrets = importlib.import_module("app.core.local_secrets")

    config = yaml.safe_load((ROOT / "docker-compose.local.yml").read_text())
    services = config["services"]
    endpoint = re.fullmatch(r"\$\{FIRECRAWL_SELF_HOSTED_URL:-(.+)\}", services["core"]["environment"]["FIRECRAWL_SELF_HOSTED_URL"])[1]
    saved = {"firecrawl_url": endpoint}
    monkeypatch.setattr(local_secrets, "read_settings", lambda section: {"values": saved})
    monkeypatch.setattr(local_secrets, "get_secret", lambda section, name: None)
    assert _reader_endpoint() == (endpoint, "")
    assert services["firecrawl"]["network_mode"] == "service:core"
    assert services["firecrawl"]["environment"]["HOST"] == "127.0.0.1"
    assert "ports" not in services["firecrawl"]
    assert "127.0.0.1:${FIRECRAWL_PORT:-3002}:3002" in services["core"]["ports"]
    for rejected in (f"http://{configured_install.name}:3002", "http://192.168.1.20:3002", "http://user:password@127.0.0.1:3002"):
        saved["firecrawl_url"] = rejected
        assert _reader_endpoint() is None


def test_install_guide_uses_the_loopback_compatible_local_stack(configured_install):
    guide = (ROOT / "docs/self-hosting/install.md").read_text()
    docker = guide.split("## Docker: Mac, Linux and Windows", 1)[1].split("## Minimal Unix launcher", 1)[0]
    assert "docker-compose.companyos.yml" not in docker
    assert "```sh\ndocker compose up --build -d\n```" in docker
    assert "docker compose down" in docker
    assert "A checkout `.env` is optional" in docker
    assert "http://127.0.0.1:3100/setup" in docker
    assert "passphrase" not in docker
    assert "as in the Docker path" not in guide
    config = yaml.safe_load((ROOT / "docker-compose.local.yml").read_text())
    assert config["services"]["console"]["network_mode"] == "service:core"


def test_windows_launcher_starts_the_native_local_stack(configured_install):
    script = (ROOT / "scripts/run_local.ps1").read_text()
    assert "wsl" not in script.lower()
    assert "prepare_local_stack.cjs" in script
    assert "tauri.local.json" in script and "local-stack" in script
    assert "System.Threading.Mutex" in script and "finally" in script
    assert "frozen-lockfile" in script
    assert (ROOT / "start.ps1").is_file()


def test_windows_source_preflights_desktop_tools_but_not_for_web(configured_install):
    entry = (ROOT / "start.ps1").read_text()
    script = (ROOT / "scripts/run_local.ps1").read_text()
    assert '"$PSScriptRoot/scripts/run_local.ps1" @args' in entry
    assert re.search(r"'py'.*?@\('-3\.12'\)", script, re.S)
    assert "@('-3')" not in script
    assert "'--web'" in script
    desktop = script.split("if ($mode -ne 'web') {", 1)[1].split("if ($missing.Count)", 1)[0]
    for probe in ("vswhere.exe", "Microsoft.VisualStudio.Component.VC.Tools.x86.x64", "KitsRoot10",
                  "Windows.h", "kernel32.lib", "F3017226-FE2A-4295-8BDF-00C3A9A7E4C5", "-Name pv"):
        assert probe in desktop
    for installer in ("Python.Python.3.12", "OpenJS.NodeJS.LTS", "Rustlang.Rustup", "Microsoft.WindowsSDK.10.0.26100",
                      "Microsoft.VisualStudio.2022.BuildTools", "Microsoft.EdgeWebView2Runtime"):
        assert f"winget install --id {installer} --exact" in script
    for download in ("Python Releases for Windows", "Node.js Download", "Install Rust",
                     "Visual Studio Downloads", "Windows SDK Downloads", "Download the WebView2 Runtime"):
        assert download in script
    after_check = script.split("if ($mode -eq 'check') {", 1)[1].split("exit 0", 1)[1]
    assert "npm.cmd install --prefix" in after_check and "pnpm@10.11.0" in after_check
    web = after_check.split("if ($mode -eq 'web') {", 1)[1].split("prepare_local_stack.cjs", 1)[0]
    assert "run_local_web.py" in web and "exit $LASTEXITCODE" in web


def test_windows_source_web_launches_loopback_services_and_cleans_up(configured_install, monkeypatch, capsys, isolate_app_imports):
    from contextlib import ExitStack

    import psutil

    monkeypatch.syspath_prepend(str(ROOT / "services/company_os"))
    launch = startup_script("windows_source_web", "scripts/desktop/run_local_web.py")
    with ExitStack() as stack:
        ports = []
        for name in ("CORE_PORT", "CONSOLE_PORT", "WORKER_HEALTH_PORT", "BRIDGE_PORT"):
            listener = stack.enter_context(socket.socket())
            listener.bind(("127.0.0.1", 0))
            ports.append(str(listener.getsockname()[1]))
            monkeypatch.setenv(name, ports[-1])
    data = configured_install / "data folder"
    monkeypatch.setenv("SUPYMEM_DATA_DIR", str(data))
    for name in ("COMPANY_OS_DATA_DIR", "COMPANY_OS_DB_URL", "DATABASE_URL", "DB_URL", "RDS_PROXY_ENDPOINT"):
        monkeypatch.setenv(name, "")
    monkeypatch.setenv("LLM_API_KEY", "private-test-value")
    children = [Mock(poll=Mock(return_value=status)) for status in (None, None, None, 23)]
    popen = Mock(side_effect=children)
    tracker = Mock(stop_all=Mock(return_value=True))
    monkeypatch.setattr(psutil, "Popen", popen)
    monkeypatch.setattr(launch, "OwnedProcesses", lambda: tracker)
    init = Mock(return_value=Mock(returncode=0))
    monkeypatch.setattr(launch.subprocess, "run", init)

    assert launch.run(configured_install, "node.exe") == 23
    calls = popen.call_args_list
    assert [call.args[0][4] for call in calls[:3]] == ["core", "worker", "bridge"]
    assert all("127.0.0.1" in call.args[0] for call in calls)
    assert [call.args[0][-1] for call in calls] == [ports[0], ports[2], ports[3], ports[1]]
    assert calls[-1].kwargs["env"]["COMPANY_OS_DATA_DIR"] == str(data)
    assert "LLM_API_KEY" not in calls[-1].kwargs["env"] and "BRIDGE_TOKEN" not in calls[-1].kwargs["env"]
    assert all(call.kwargs["env"]["SUPYMEM_MODE"] == "local" for call in calls)
    assert calls[0].kwargs["env"]["BRIDGE_TOKEN"] == calls[2].kwargs["env"]["BRIDGE_TOKEN"]
    assert tracker.register.call_count == 4
    tracker.stop_all.assert_called_once()
    output = capsys.readouterr()
    assert f"http://127.0.0.1:{ports[1]}/setup" in output.out and "--web" in output.err
    assert "private-test-value" not in output.out + output.err

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", int(ports[0])))
        assert launch.run(configured_install, "node.exe") == 1
    assert "Set CORE_PORT to a different port" in capsys.readouterr().err
    monkeypatch.setenv("DATABASE_URL", f"postgresql://user:private-test-value@{configured_install.name}/db")
    assert launch.run(configured_install, "node.exe") == 1
    assert popen.call_count == 4
    assert init.call_count == 2
    assert init.call_args_list[0].args[0] == [sys.executable, "-m", "desktop_runtime", "--role", "core", "--maintenance"]
    output = capsys.readouterr().err
    assert "Remove DATABASE_URL" in output and ".\\start.ps1 --web" in output
    assert "private-test-value" not in output

    monkeypatch.delenv("DATABASE_URL")
    init.reset_mock()
    init.return_value.returncode = 1
    assert launch.run(configured_install, "node.exe") == 1
    init.assert_called_once()
    assert "--maintenance" in init.call_args.args[0]
    assert popen.call_count == 4
    assert "Follow the recovery steps above, then rerun .\\start.ps1 --web" in capsys.readouterr().err


@pytest.mark.parametrize("domain,transfer", [("warehouse.example", "restore"), ("hiking.example", "import")])
def test_windows_source_web_applies_pending_recovery_before_starting_services(monkeypatch, tmp_path, domain, transfer):
    import sqlite3
    from contextlib import closing

    from tests.unit.test_cli_local import seed

    launch = startup_script("windows_source_recovery", "scripts/desktop/run_local_web.py")
    data = tmp_path / domain
    database = data / "local.db"
    data.mkdir()
    seed(database, domain)
    backend = {name: os.environ[name] for name in ("PATH", "SYSTEMROOT", "PYTHONPATH") if name in os.environ}
    backend.update(SUPYMEM_MODE="local", APP_ENV="local", COMPANY_OS_DATA_DIR=str(data),
                   COMPANY_OS_DB_URL=f"sqlite:///{database}", COMPANY_OS_WORKING_MEMORY_PATH=str(data / "working_memory.sqlite3"),
                   COMPANY_OS_SECRETS_ENV=str(data / ".env"), CORE_PORT="3300", WORKER_HEALTH_PORT="3301", BRIDGE_PORT="3500")
    stage = ("from app.core import local_data; local_data.queue_import(local_data.export_data())"
             if transfer == "import" else
             "from app.core import local_data; local_data.queue_restore(local_data.create_backup())")
    subprocess.run([sys.executable, "-c", stage], cwd=ROOT, env=backend, check=True)
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("UPDATE notes SET body = 'changed after backup'")

    def start_child(*args, **kwargs):
        with closing(sqlite3.connect(database)) as connection:
            assert connection.execute("SELECT body FROM notes").fetchone() == (domain,)
        return Mock(poll=Mock(return_value=23))

    monkeypatch.setattr(launch, "local_environment", lambda root: (backend, {"CONSOLE_PORT": "3100", "CONSOLE_ORIGIN": "http://127.0.0.1:3100"}))
    popen = Mock(side_effect=start_child)
    monkeypatch.setattr(launch.psutil, "Popen", popen)
    monkeypatch.setattr(launch, "OwnedProcesses", Mock())
    assert launch.run(ROOT, "node.exe") == 23
    assert popen.call_count == 4


@pytest.mark.parametrize("domain", ["warehouse.example", "hiking.example"])
def test_windows_owned_cleanup_never_signals_a_reused_or_unrelated_process(monkeypatch, domain):
    from types import SimpleNamespace
    from desktop_runtime import owned_processes

    tracker = owned_processes.OwnedProcesses()
    child = Mock(pid=987654)
    child.create_time.return_value = tracker.started_at
    child.is_running.return_value = True
    reused = Mock(pid=987655)
    reused.create_time.return_value = tracker.started_at + 1
    tracker.observed = {(child.pid, tracker.started_at): child, (reused.pid, tracker.started_at): reused}
    tracker.workspaces.add(Path("/tmp") / domain)
    unrelated = Mock(pid=987656)
    unrelated.create_time.return_value = tracker.started_at
    unrelated.cwd.return_value = str(Path("/tmp") / domain)
    unrelated.username.return_value = tracker.owner.username()
    monkeypatch.setattr(owned_processes, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(owned_processes.psutil, "process_iter", lambda _fields: [unrelated])
    monkeypatch.setattr(tracker, "quiesce", lambda: True)
    monkeypatch.setattr(tracker, "capture", lambda: None)
    monkeypatch.setattr(owned_processes.psutil, "wait_procs", lambda _processes, timeout: ([], []))
    child.terminate.side_effect = lambda: child.is_running.configure_mock(return_value=False)
    assert not tracker.stop_all(timeout=.01)
    assert tracker.complete is False
    child.terminate.assert_called_once()
    reused.terminate.assert_not_called()
    reused.kill.assert_not_called()
    unrelated.terminate.assert_not_called()
    unrelated.kill.assert_not_called()


@pytest.mark.parametrize("domain", ["warehouse.example", "hiking.example"])
def test_owned_capture_stays_with_the_registered_subtree(domain):
    from desktop_runtime.owned_processes import OwnedProcesses

    tracker = OwnedProcesses()
    root = Mock(pid=987654)
    root.create_time.return_value = tracker.started_at
    child = Mock(pid=987655)
    child.create_time.return_value = tracker.started_at
    child.children.return_value = []
    unrelated = Mock(pid=987656)
    unrelated.create_time.return_value = tracker.started_at
    root.children.return_value = [child]
    tracker.owner = Mock()
    tracker.owner.children.return_value = [root, unrelated]
    tracker.observed[(root.pid, tracker.started_at)] = root
    tracker.capture()
    assert (child.pid, tracker.started_at) in tracker.observed
    assert (unrelated.pid, tracker.started_at) not in tracker.observed, domain


@pytest.mark.parametrize("domain", ["warehouse.example", "hiking.example"])
@pytest.mark.parametrize("cancelled", [True, False])
def test_coding_cancellation_uses_identity_checked_cleanup(monkeypatch, tmp_path, domain, cancelled):
    import asyncio
    from types import SimpleNamespace
    from coding_agents import subprocess_runner
    from coding_agents.protocol import AgentEvent, AgentEventKind, CodingRequest

    tracker = Mock()
    tracker.stop_all.return_value = True
    monkeypatch.setattr(subprocess_runner, "OwnedProcesses", lambda: tracker, raising=False)
    monkeypatch.setattr(subprocess_runner, "hosted_execution", lambda: False)
    monkeypatch.setattr(subprocess_runner, "child_env", lambda: {})
    monkeypatch.setattr(subprocess_runner, "os", SimpleNamespace(name="nt"))
    child = Mock(pid=987654, returncode=None)
    async def wait():
        child.returncode = 0
        return 0

    child.wait = wait
    created = []

    async def spawn(*arguments, **options):
        created.append(options)
        child.stdout = asyncio.StreamReader()
        child.stderr = asyncio.StreamReader()
        child.stdout.feed_data(b'{}\n')
        child.stdout.feed_eof()
        return child

    monkeypatch.setattr(subprocess_runner.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(subprocess_runner, "psutil", SimpleNamespace(Process=lambda _pid: Mock()), raising=False)
    adapter = SimpleNamespace(build_argv=lambda _request: ["coding-tool"],
        parse_event=lambda _raw: AgentEvent(kind=AgentEventKind.MESSAGE))

    async def cancel(_event):
        if cancelled:
            raise asyncio.CancelledError()

    def run():
        return asyncio.run(subprocess_runner.run_live(adapter=adapter,
            req=CodingRequest(prompt=domain, workdir=tmp_path / domain, allow_write=False), on_event=cancel))

    if cancelled:
        with pytest.raises(asyncio.CancelledError):
            run()
        tracker.stop_all.assert_called()
    else:
        assert run().ok
        tracker.stop_all.assert_not_called()
        tracker.close.assert_called_once()
    assert created[0]["start_new_session"] is False
    tracker.register.assert_called_once()


@pytest.mark.parametrize("domain", ["warehouse.example", "hiking.example"])
def test_windows_source_receipt_uses_private_files_and_shared_lock(monkeypatch, tmp_path, isolate_app_imports, domain):
    from contextlib import contextmanager
    from types import SimpleNamespace
    monkeypatch.syspath_prepend(str(ROOT / "services/company_os"))
    from desktop_runtime import source_receipt
    local_process = importlib.import_module("app.core.local_process")
    local_windows = importlib.import_module("app.core.local_windows")

    workspace = tmp_path / "Documents/Supymem/ventures" / domain.replace(".", "-")
    workspace.mkdir(parents=True)
    (workspace / ".supymem-venture").write_text("ven_test")
    (workspace / "app.py").write_text(f"DOMAIN = {domain!r}\n")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(source_receipt, "data_root", lambda **_kwargs: tmp_path / "data")
    private_files, locked = [], []
    real_open = local_process.open_private
    real_lock = local_process.file_lock

    def open_private(path, flags, mode=0o600):
        private_files.append(Path(path).name)
        return real_open(path, flags, mode)

    @contextmanager
    def file_lock(stream, **options):
        locked.append(stream.name)
        with real_lock(stream, **options):
            yield

    monkeypatch.setattr(local_process, "open_private", open_private)
    monkeypatch.setattr(local_process, "file_lock", file_lock)
    monkeypatch.setattr(local_windows, "private_directory", lambda path: path.mkdir(parents=True, exist_ok=True))
    monkeypatch.setattr(local_windows, "atomic_write", lambda path, content: (private_files.append(path.name), path.write_bytes(content)))
    monkeypatch.setattr(source_receipt, "os", SimpleNamespace(name="nt", O_CREAT=os.O_CREAT, O_RDWR=os.O_RDWR,
        fdopen=os.fdopen))
    receipt = source_receipt.capture(venture_id="ven_test", workspace_id="wsp_test", actor_id="owner",
        device_id="dev_test", plan_task_id="tsk_test", workspace_slug=workspace.name,
        order_id="dwo_test", execution_id="dex_test", provider="codex")
    assert locked and ".lock" in private_files and "receipt.json" in private_files
    assert source_receipt.load_archive(receipt)


@pytest.mark.parametrize("domain", ["warehouse.example", "hiking.example"])
def test_payload_verification_imports_api_worker_and_checks_windows_credentials(monkeypatch, tmp_path, domain):
    specification = importlib.util.spec_from_file_location("startup_payload", ROOT / "scripts/desktop/prepare_local_stack.py")
    builder = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(builder)
    monkeypatch.setattr(builder, "ROOT", tmp_path / domain)
    monkeypatch.setattr(builder.sys, "platform", "win32")
    commands = []

    def run(command, **options):
        commands.append((command, options))
        return subprocess.CompletedProcess(command, 0, stdout=f"v{builder.NODE_VERSION}\n", stderr="")

    monkeypatch.setattr(builder.subprocess, "run", run)
    builder.verify_payload("x86_64-pc-windows-msvc")
    python, options = commands[1]
    assert python[1:3] == ["-I", "-c"]
    assert "import app.main, app.worker.main" in python[3]
    assert "_write_credential" in python[3] and "_read_credential" in python[3] and "CredDeleteW" in python[3]
    assert "finally:" in python[3]
    assert "Windows Control Panel" in python[3]
    compile(python[3], "payload-verification", "exec")
    assert options["env"]["COMPANY_OS_DATA_DIR"].startswith(options["cwd"])
    assert "LLM_API_KEY" not in options["env"]


@pytest.mark.parametrize("domain", ["warehouse.example", "hiking.example"])
@pytest.mark.parametrize("state", ["first", "busy", "saved_scope", "empty_target", "occupied_target", "occupied_backups",
                                   "interrupt_first", "interrupt_second", "interrupt_conflict", "interrupt_replaced"])
def test_windows_relocation_holds_shared_locks_and_preserves_identity(monkeypatch, tmp_path, isolate_app_imports, domain, state):
    import hashlib
    import json
    import sys

    monkeypatch.syspath_prepend(str(ROOT / "services/company_os"))
    local_process = importlib.import_module("app.core.local_process")
    local_windows = importlib.import_module("app.core.local_windows")

    code = (ROOT / "src-tauri/src/local_stack.rs").read_text().split('const WINDOWS_RELOCATION: &str = r#"', 1)[1].split('"#;', 1)[0]
    legacy = tmp_path / "old/local"
    target = tmp_path / f"Your app {domain} ü"
    providers = legacy / "vault/providers"
    providers.mkdir(parents=True)
    (providers / "settings.json").write_text("{}")
    (legacy / "database.sqlite3").write_text(domain)
    backups = legacy.with_name("local-backups")
    backups.mkdir()
    (backups / "saved.zip").write_text(domain)
    pending = legacy.with_name(".local-pending-restore")
    pending.mkdir()
    (pending / "request.json").write_text(domain)
    target_backups = target.with_name(f"{target.name}-backups")
    target_pending = target.with_name(f".{target.name}-pending-restore")
    if state in {"empty_target", "occupied_target", "occupied_backups"}:
        target.mkdir()
    conflict = None
    if state == "occupied_target":
        conflict = target / "database.sqlite3"
    if state == "occupied_backups":
        target_backups.mkdir()
        conflict = target_backups / "saved.zip"
    if conflict:
        conflict.write_text("keep this")
    scope = hashlib.sha256(str(providers.resolve()).encode()).hexdigest()
    if state == "saved_scope":
        scope = "a" * 64
        (providers / "keychain.scope").write_text(scope)
    private = []

    def private_directory(path):
        assert not path.is_symlink()
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        private.append(path)

    def atomic_write(path, content):
        path.write_bytes(content)
        path.chmod(0o600)

    monkeypatch.setattr(local_windows, "private_directory", private_directory)
    monkeypatch.setattr(local_windows, "atomic_write", atomic_write)
    monkeypatch.setattr(sys, "argv", ["relocate", str(tmp_path / "payload"), str(legacy), str(target)])
    if conflict:
        with pytest.raises(SystemExit) as refused:
            exec(code, {})
        assert refused.value.code == 3
        assert conflict.read_text() == "keep this"
        assert (legacy / "database.sqlite3").read_text() == domain
        assert (legacy.with_name("local-backups") / "saved.zip").read_text() == domain
        return
    if state == "busy":
        path = legacy.with_name(".local-process.lock")
        with os.fdopen(local_process.open_private(path, os.O_CREAT | os.O_RDWR), "rb") as lock:
            with local_process.file_lock(lock, exclusive=True, blocking=False):
                with pytest.raises(BlockingIOError):
                    exec(code, {})
        assert (legacy / "database.sqlite3").read_text() == domain
        assert not target.exists()
        return
    if state.startswith("interrupt_"):
        blocked = backups if state in {"interrupt_first", "interrupt_conflict"} else pending
        rename = Path.rename

        def interrupted_rename(source, destination):
            if source == blocked:
                raise PermissionError("sharing violation")
            return rename(source, destination)

        with monkeypatch.context() as interruption:
            interruption.setattr(Path, "rename", interrupted_rename)
            with pytest.raises(PermissionError, match="sharing violation"):
                exec(code, {})
        assert not legacy.exists() and (target / "database.sqlite3").read_text() == domain
        assert (pending / "request.json").read_text() == domain and not target_pending.exists()
        if state in {"interrupt_conflict", "interrupt_replaced"}:
            moved_backups = tmp_path / "moved-backups"
            if state == "interrupt_replaced":
                target_backups.rename(moved_backups)
            target_backups.mkdir()
            (target_backups / "saved.zip").write_text("keep this")
            with pytest.raises(SystemExit) as refused:
                exec(code, {})
            assert refused.value.code == 4
            assert not target_pending.exists() and (target_backups / "saved.zip").read_text() == "keep this"
            target_backups.rename(tmp_path / "unrelated-backups")
            if state == "interrupt_replaced":
                moved_backups.rename(target_backups)
    exec(code, {})
    assert not legacy.exists()
    assert (target / "database.sqlite3").read_text() == domain
    assert (target / "vault/providers/keychain.scope").read_text() == scope
    assert json.loads((target / ".data-directory.json").read_text())["source_roots"] == [str(legacy.resolve())]
    assert not backups.exists() and (target_backups / "saved.zip").read_text() == domain
    assert not pending.exists() and (target_pending / "request.json").read_text() == domain
    assert not target.with_name(f".{target.name}-relocation.json").exists()
    assert providers in private and legacy in private
    exec(code, {})
    assert (target / "database.sqlite3").read_text() == domain


@pytest.mark.parametrize("domain", ["warehouse.example", "hiking.test"])
def test_windows_checkout_paths_are_literal(domain):
    launcher = (ROOT / "scripts/run_local.ps1").read_text()
    assert 'Resolve-Path -LiteralPath "$PSScriptRoot/.."' in launcher, domain
    assert 'Push-Location -LiteralPath $root' in launcher
    assert 'Get-FileHash -LiteralPath $inputs' in launcher


@pytest.mark.parametrize("domain", ["warehouse.example", "hiking.test"])
def test_windows_web_stamp_invalidates_editable_installs_after_a_move(domain):
    launcher = (ROOT / "scripts/run_local.ps1").read_text()
    assert re.search(r'(?m)^\s*\$signature = .*\$root\b', launcher), domain
    assert launcher.index('if ($signature -cne') < launcher.index('pip install --no-deps')


@pytest.mark.parametrize("domain", ["warehouse.example", "hiking.test"])
@pytest.mark.parametrize("encoding", ["utf-8-sig", "utf-16"])
def test_windows_source_env_does_not_lose_the_unicode_data_path(tmp_path, monkeypatch, capsys, isolate_app_imports, domain, encoding):
    from contextlib import ExitStack
    import sqlite3
    from sqlalchemy.engine import make_url

    monkeypatch.syspath_prepend(str(ROOT / "services/company_os"))
    launch = startup_script("windows_source_encoding", "scripts/desktop/run_local_web.py")
    for name in ("COMPANY_OS_DATA_DIR", "COMPANY_OS_DB_URL", "DATABASE_URL", "DB_URL", "RDS_PROXY_ENDPOINT", "SUPYMEM_DATA_DIR"):
        monkeypatch.delenv(name, raising=False)
    data = tmp_path / f"{domain} ü [data]"
    data.mkdir()
    env_file = tmp_path / ".env"
    env_file.write_bytes(f'COMPANY_OS_DATA_DIR="{data}"\r\n'.encode(encoding))
    with ExitStack() as stack:
        for name in ("CORE_PORT", "CONSOLE_PORT", "WORKER_HEALTH_PORT", "BRIDGE_PORT"):
            listener = stack.enter_context(socket.socket())
            listener.bind(("127.0.0.1", 0))
            monkeypatch.setenv(name, str(listener.getsockname()[1]))
    if encoding == "utf-16":
        monkeypatch.setattr(launch.subprocess, "run", lambda *args, **kwargs: pytest.fail("initialization must not run"))
        assert launch.run(tmp_path, "node.exe") == 1
        message = capsys.readouterr().err
        assert str(env_file) in message and "UTF-8" in message
    else:
        backend, _ = launch.local_environment(tmp_path)
        assert backend["COMPANY_OS_DATA_DIR"] == str(data)
        with sqlite3.connect(make_url(backend["COMPANY_OS_DB_URL"]).database) as database:
            assert Path(database.execute("PRAGMA database_list").fetchone()[2]).parent == data
    assert not (tmp_path / "data").exists()


def run_startup_rust_test(tmp_path, source, *, windows=False):
    rustc = shutil.which("rustc") or str(Path.home() / ".cargo/bin/rustc")
    if not Path(rustc).is_file():
        pytest.skip("Your Rust compiler is not installed")
    program = tmp_path / "startup.rs"
    program.write_text(source)
    binary = tmp_path / "startup-test"
    command = [rustc, "--edition=2021", "--test", str(program), "-o", str(binary)]
    if windows:
        command += ["--cfg", "windows", "-Aexplicit_builtin_cfgs_in_flags"]
    compiled = subprocess.run(command, capture_output=True, text=True, timeout=60)
    assert compiled.returncode == 0, compiled.stderr
    result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


def test_founder_path_round_trips_with_the_platform_separator(tmp_path):
    source = (ROOT / "src-tauri/src/supervisor.rs").read_text()
    function = source.split("pub fn merge_founder_path", 1)[1].split("\nfn founder_path_extras", 1)[0]
    run_startup_rust_test(tmp_path, "pub fn merge_founder_path" + function + '''
#[test]
fn path_round_trip() {
    for domain in ["warehouse.example", "hiking.example"] {
        let paths = [format!("folder {domain}"), "node".into()];
        let base = std::env::join_paths(&paths).unwrap().to_string_lossy().into_owned();
        let merged = merge_founder_path(&base, &[&paths[0], "python"]);
        assert_eq!(std::env::split_paths(&merged).collect::<Vec<_>>(),
            vec![std::path::PathBuf::from(&paths[0]), "python".into(), "node".into()]);
        let invalid = if cfg!(windows) { "invalid;directory" } else { "invalid:directory" };
        assert_eq!(merge_founder_path(&base, &[invalid]), base);
    }
}
''')


def test_maintenance_command_keeps_its_cwd_outside_replaceable_data(tmp_path):
    source = (ROOT / "src-tauri/src/local_stack.rs").read_text()
    function = source.split("    fn command(&self", 1)[1].split("    fn credentials(&self", 1)[0]
    function = function.replace('env!("SUPYMEM_DESKTOP_VERSION")', '"fixture"')
    roles = "const ROLES" + source.split("const ROLES", 1)[1].split("\n];", 1)[0] + "\n];"
    openssl = "fn set_openssl_conf" + source.split("fn set_openssl_conf", 1)[1].split("\n}", 1)[0] + "\n}"
    run_startup_rust_test(tmp_path, '''
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
const HOST: &str = "127.0.0.1";
''' + roles + '\n' + openssl + '''
struct LocalStack { data_dir: PathBuf, resource_dir: PathBuf, ports: [u16; 4], instance: String,
    bridge_token: String, native_bridge_token: String, readiness_keys: [String; ROLES.len()] }
impl LocalStack {
    fn runtime_target() -> &'static str { "test-runtime" }
    fn url(&self, _: &str) -> String { "http://127.0.0.1:3300".into() }
    fn command(&self''' + function + '''
}
#[test]
fn stable_working_directory() {
    for domain in ["warehouse.example", "hiking.example"] {
        let parent = std::env::temp_dir().join(format!("your app {domain} ü"));
        let stack = LocalStack { data_dir: parent.join("data"), resource_dir: parent.join("resources"),
            ports: [3300, 3301, 3100, 3500], instance: "fixture".into(),
            bridge_token: "bridge-fixture".into(), native_bridge_token: "native-fixture".into(),
            readiness_keys: std::array::from_fn(|_| "readiness-fixture".into()) };
        for role in ["core", "worker", "console", "bridge"] {
            let command = stack.command(role, 3300);
            assert_eq!(command.get_current_dir(), Some(parent.as_path()));
            if role != "console" { assert!(command.get_args().any(|argument| argument == stack.data_dir.as_os_str())); }
        }
    }
}
''')


def test_maintenance_progress_is_allowlisted_without_exposing_appended_data(tmp_path):
    source = (ROOT / "src-tauri/src/local_diagnostics.rs").read_text()
    function = source.split("fn redact_text", 1)[1].split("#[cfg(test)]", 1)[0]
    run_startup_rust_test(tmp_path, "fn redact_text" + function + '''
#[test]
fn private_progress() {
    let message = "Saving your automatic backup before updating your data.";
    assert_eq!(redact_text(message, false), message);
    for domain in ["warehouse.example", "hiking.example"] {
        assert_eq!(redact_text(&format!("{message} {domain}"), false), "[Private diagnostic detail hidden.]");
    }
}
''')


def test_missing_python_modules_keep_type_and_name_without_private_details(tmp_path):
    source = (ROOT / "src-tauri/src/local_diagnostics.rs").read_text()
    function = source.split("fn redact_text", 1)[1].split("#[cfg(test)]", 1)[0]
    run_startup_rust_test(tmp_path, "fn redact_text" + function + '''
#[test]
fn missing_modules() {
    assert_eq!(redact_text("ModuleNotFoundError", false), "ModuleNotFoundError");
    for module in ["warehouse_reporting_provider", "garden_calendar_provider"] {
        let import = format!("    from {module}.client import Reader");
        assert_eq!(redact_text(&import, false), format!("Python import: {module}.client"));
        let line = format!("ModuleNotFoundError: No module named '{module}' https://owner:private@example.invalid /Users/owner/private password=private");
        let safe = redact_text(&line, false);
        assert!(safe.contains("ModuleNotFoundError"));
        assert!(safe.contains(module));
        assert!(!safe.contains("example.invalid"));
        assert!(!safe.contains("/Users/owner"));
        assert!(!safe.contains("password=private"));
        assert_eq!(redact_text(module, false), "[Private diagnostic detail hidden.]");
        assert_eq!(redact_text(&format!("{module} ModuleNotFoundError"), false), "[Private diagnostic detail hidden.]");
    }
}
''')


def test_windows_ide_uses_the_installed_executable_without_a_shell(tmp_path, monkeypatch):
    source = (ROOT / "src-tauri/src/ide.rs").read_text()
    functions = source.split("fn editor_launch_command", 1)[1].split("\npub fn read_venture_landing", 1)[0]
    installed = tmp_path / "Your programs ü/Programs/cursor/Cursor.exe"
    write_startup_tool(installed, '[[ ! -v SUPYMEM_EDITOR_TEST_MARKER ]] && printf "%s\\n" "$@"')
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Your programs ü"))
    monkeypatch.setenv("SUPYMEM_EDITOR_TEST_MARKER", "synthetic")
    run_startup_rust_test(tmp_path, "use std::path::{Path, PathBuf};\nuse std::process::Command;\nfn editor_launch_command" + functions + '''
#[test]
fn open_owned_folder() {
    let installed = PathBuf::from(std::env::var_os("LOCALAPPDATA").unwrap()).join("Programs/cursor/Cursor.exe");
    for domain in ["warehouse.example", "hiking.example"] {
        let folder = std::env::temp_dir().join(format!("folder {domain} ü"));
        let mut command = editor_command("Cursor", &folder).unwrap();
        assert_eq!(command.get_program(), installed.as_os_str());
        let result = command.output().unwrap();
        assert!(result.status.success());
        assert_eq!(String::from_utf8(result.stdout).unwrap(), format!("{}\\n", folder.display()));
    }
}
''', windows=True)


def test_windows_quiet_hours_convert_the_supplied_clock_to_local_time(tmp_path):
    source = (ROOT / "src-tauri/src/suggestions.rs").read_text()
    function = source.split("#[cfg(windows)]\nfn local_clock", 1)[1].split("#[cfg(not(any(unix, windows)))]", 1)[0]
    function = function.replace('#[link(name = "kernel32")]', "")
    run_startup_rust_test(tmp_path, "fn local_clock" + function + '''
#[no_mangle]
unsafe extern "system" fn FileTimeToSystemTime(file: *const u32, system: *mut u16) -> i32 {
    let ticks = u64::from(*file) | (u64::from(*file.add(1)) << 32);
    let now = ticks / 10_000_000 - 11_644_473_600;
    *system.add(4) = ((now / 3600) % 24) as u16;
    *system.add(5) = ((now / 60) % 60) as u16;
    *system.add(6) = (now % 60) as u16;
    1
}
#[no_mangle]
unsafe extern "system" fn SystemTimeToTzSpecificLocalTime(_: *const std::ffi::c_void, utc: *const u16, local: *mut u16) -> i32 {
    std::ptr::copy_nonoverlapping(utc, local, 8);
    *local.add(4) = (*local.add(4) + 16) % 24;
    1
}
#[no_mangle]
unsafe extern "system" fn GetLocalTime(local: *mut u16) {
    *local.add(4) = 9;
    *local.add(5) = 30;
}
#[test]
fn timezone_and_clock_boundary() {
    assert_eq!(local_clock(0), (16, 16 * 3600));
    assert_eq!(local_clock(8 * 3600 + 90), (0, 90));
    assert_eq!(local_clock(i64::MAX), (9, 9 * 3600 + 30 * 60));
}
''')


def test_windows_installer_fixture_uses_its_platform_exit_status():
    source = (ROOT / "src-tauri/src/install.rs").read_text().split("fn pip_failure_does_not_blame_internet_for_python_version()", 1)[1]
    assert re.search(r'#\[cfg\(unix\)\]\s+let status = std::os::unix::process::ExitStatusExt::from_raw', source)
    assert re.search(r'#\[cfg\(windows\)\]\s+let status = std::os::windows::process::ExitStatusExt::from_raw', source)


def test_installed_smoke_rejects_checkout_resources_and_accepts_spaces_and_unicode(tmp_path):
    specification = importlib.util.spec_from_file_location("startup_smoke", ROOT / "scripts/desktop/smoke_local_stack.py")
    smoke = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(smoke)
    for domain in ["warehouse.example", "hiking.example"]:
        installed = tmp_path / f"Your installed app {domain} ü"
        for name in ("console/apps/console/server.js", "local/services/company_os/app/core/local_mode.py"):
            path = installed / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("installed fixture")
        assert smoke.installed_resources(installed / "company-os-desktop.exe", None) == installed.resolve()
        with pytest.raises(ValueError, match="not src-tauri/resources"):
            smoke.installed_resources(installed / "company-os-desktop.exe", ROOT / "src-tauri/resources")


def test_make_up_and_down_select_the_same_local_stack(configured_install):
    result = subprocess.run(["make", "-n", "up", "down"], cwd=ROOT, capture_output=True, text=True, check=True)
    commands = result.stdout.splitlines()
    assert commands == ["docker compose up --build -d", "docker compose down"]


def test_ci_keeps_hosted_boundaries_out_of_local_mode(configured_install):
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    assert "SUPYMEM_MODE" not in workflow["env"]
    for job in workflow["jobs"].values():
        assert "SUPYMEM_MODE" not in job.get("env", {})
    assert "tests/unit/test_local_mode.py" in workflow["env"]["UNIT_PATHS"].split()
    assert "tests/unit/tenancy/test_founder_isolation.py" in workflow["env"]["UNIT_PATHS"].split()


def test_missing_upstream_notices_block_public_release(configured_install):
    checklist = (ROOT / "docs/self-hosting/PUBLIC_RELEASE_CHECKLIST.md").read_text()
    notices = checklist.split("## Must-resolve upstream notices", 1)[1].split("\n## ", 1)[0]
    assert "Do not publish" in notices
    for package, vendor in (("paperclip", "paperclip"), ("munder", "munder-difflin")):
        assert f"services/company_os/app/{package}/" in notices
        assert f"vendor/{vendor}/LICENSE" in notices
    assert "complete upstream" in notices
