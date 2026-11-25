"""Closed-source preview and provider journeys without a Mac, browser or socket."""
import hashlib
import json
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from desktop_runtime import agent_status, coding_loop, loop_checks, owned_coding, owned_deploy
from desktop_runtime.device_worker import DeviceWorker
from desktop_runtime.owned_coding import OwnedCoding, _trusted_git, provider_outcome


DOMAINS = [
    {"title": "Track outstanding invoices for bookkeepers", "endpoint": "/api/reconcile",
        "body": {"billed": 30, "paid": 12}, "expected": {"balance_due": 18}},
    {"title": "Pack for a trail walk with friends", "endpoint": "/api/packing",
        "body": {"walkers": 3}, "expected": {"items": 3}},
]


class FakeProcess:
    def __init__(self, *, alive=False, output=b"", port=43127):
        self.alive, self.output, self.port = alive, output, port
        self.returncode, self.pid = 0, 99999999
        self.children_alive = alive

    def poll(self):
        return None if self.alive else self.returncode

    def create_time(self):
        return 1.0

    def communicate(self, **kwargs):
        return self.output, b""

    def wait(self, **kwargs):
        self.alive = False
        return self.returncode

    def kill(self):
        self.alive = self.children_alive = False

    def net_connections(self, **kwargs):
        return [SimpleNamespace(status="LISTEN", laddr=SimpleNamespace(ip="127.0.0.1", port=self.port))] if self.alive else []


class FakeTracker:
    def __init__(self):
        self.processes, self.stops = [], 0

    def register(self, process):
        self.processes.append(process)

    def watch_workspace(self, workspace):
        pass

    def capture(self):
        pass

    def quiesce(self):
        return True

    def stop_all(self, **kwargs):
        self.stops += 1
        for process in self.processes:
            process.kill()
        return True

    def stopped(self):
        return all(not process.alive and not process.children_alive for process in self.processes)


class FakeSocket:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def bind(self, address):
        assert address == ("127.0.0.1", 0)

    def getsockname(self):
        return "127.0.0.1", 43127


class Response:
    def __init__(self, status, body):
        self.status, self.body = status, body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def read(self, limit):
        return self.body[:limit]


class Journey:
    def __init__(self, tmp_path, monkeypatch, provider, domain):
        monkeypatch.setenv("SUPYMEM_MODE", "local")
        self.workspace, self.provider, self.domain = tmp_path, provider, domain
        self.allowed, self.stage, self.fail_first, self.code_calls = True, "plan", True, 0
        self.bad_json, self.nodes, self.http_calls, self.argv = False, [], [], []
        self.plan = {"spec": domain["title"] + " with /api/health and " + domain["endpoint"] +
            "\n```http-journey\n" + json.dumps([{"method": "POST", "path": domain["endpoint"],
                "body": domain["body"], "status": 200, "expected_json": domain["expected"]}]) + "\n```",
            "tasks": [{"id": "app", "title": domain["title"], "files": ["index.html", "api/health.js",
                domain["endpoint"].lstrip("/") + ".js", "dev-server.mjs"],
                "acceptance": [{"test_file": "tests/app.test.js", "test_name": "approved result",
                    "behaviour": "Your input returns the approved result"}]}]}
        from desktop_runtime.hosted import _is_local
        if _is_local():
            self.plan["tasks"][0]["files"].insert(0, "package.json")
        (tmp_path / ".supymem-venture").write_text("ven_fixture\n")
        self.tracker = FakeTracker()
        self.coding = OwnedCoding(venture_id="ven_fixture", workspace_slug="fixture", provider=provider,
            tracker=self.tracker, authorize=lambda: self.allowed)
        monkeypatch.setattr(self.coding, "_workspace", lambda: tmp_path)
        monkeypatch.setattr(self.coding, "preflight", lambda: "/fake/" + provider)
        monkeypatch.setattr(agent_status, "_probe", lambda binary, arguments, **kwargs:
            (0, self.spawn([binary, *arguments]).communicate()[0]))
        monkeypatch.setattr(owned_coding.psutil, "Popen", self.spawn)
        monkeypatch.setattr(OwnedCoding, "_stop_process_group", staticmethod(lambda process: process.kill()))
        monkeypatch.setattr(coding_loop, "_quiescent", lambda workspace, tracker: tracker.stop_all())
        monkeypatch.setattr(loop_checks, "preflight", lambda: True)
        monkeypatch.setattr(loop_checks, "_launch", lambda workspace, temporary, node, argv, env, output, **kwargs:
            loop_checks.psutil.Popen([node, *argv], cwd=workspace, env=env, start_new_session=True))
        monkeypatch.setattr(loop_checks, "node_binary", lambda: "/fake/node")
        monkeypatch.setattr(loop_checks, "_PreviewProcesses", FakeTracker)
        monkeypatch.setattr(loop_checks.socket, "socket", FakeSocket)
        monkeypatch.setattr(loop_checks, "build_opener", lambda *args: SimpleNamespace(open=self.open))
        monkeypatch.setattr(loop_checks, "_run_tests", self.tests)
        thread = loop_checks.threading.Thread
        monkeypatch.setattr(loop_checks.threading, "Thread", lambda **kwargs:
            thread(**kwargs) if "args" in kwargs else SimpleNamespace(start=lambda: None))
        monkeypatch.setattr(loop_checks.OwnedProcesses, "receipt_stopped", staticmethod(lambda inventory:
            inventory == {"inventory_complete": True, "processes": [{"pid": 99999999, "created_at": 1}]}))
        monkeypatch.setattr(owned_deploy.OwnedDeploy, "run", lambda *args: pytest.fail("No deployment is authorized"))

    def spawn(self, argv, **kwargs):
        self.argv.append(argv)
        if "--version" in argv:
            version = b"2.1.284" if self.provider == "claude" else b"codex-cli 0.138.0"
            return FakeProcess(output=version)
        if argv[-1] == "dev-server.mjs":
            assert kwargs["env"]["HOST"] == "127.0.0.1"
            assert kwargs["start_new_session"] is True
            process = FakeProcess(alive=True, port=int(kwargs["env"]["PORT"]))
            self.nodes.append(process)
            return process
        if self.stage == "plan":
            (self.workspace / "review").mkdir(exist_ok=True)
            (self.workspace / "review/spec.md").write_text(self.plan["spec"])
            (self.workspace / "review/tasks.json").write_text(json.dumps(self.plan["tasks"]))
        else:
            self.code_calls += 1
            (self.workspace / "package.json").write_text('{"type":"module"}')
            (self.workspace / "api").mkdir(exist_ok=True)
            (self.workspace / "tests").mkdir(exist_ok=True)
            (self.workspace / "index.html").write_text("<title>" + self.domain["title"] + "</title>")
            (self.workspace / "api/health.js").write_text("export default (req,res)=>res.end('{}');")
            (self.workspace / (self.domain["endpoint"].lstrip("/") + ".js")).write_text(
                "export default (req,res)=>res.end('" + json.dumps(self.domain["expected"]) + "');")
            (self.workspace / "dev-server.mjs").write_text("import http from 'node:http';")
            (self.workspace / "tests/app.test.js").write_text("import test from 'node:test';")
        terminal = {"type": "result", "subtype": "success", "is_error": False} if self.provider == "claude" else {"type": "turn.completed"}
        reader, writer = os.pipe()
        os.write(writer, (json.dumps(terminal) + ("\nnot JSON" if self.bad_json else "")).encode())
        os.close(writer)
        process = FakeProcess()
        process.stdout = os.fdopen(reader, "rb")
        return process

    def tests(self, *args):
        failed = self.fail_first and self.code_calls == 1
        return {"tap_summary": {"pass": 0 if failed else 1, "fail": int(failed)},
            "test_names": [] if failed else ["approved result"],
            "failures": [{"kind": "test", "message": "Your result is incorrect"}] if failed else []}

    def open(self, request, **kwargs):
        url = request if isinstance(request, str) else request.full_url
        assert url.startswith("http://127.0.0.1:43127/")
        self.http_calls.append(url)
        if url.endswith("/api/health"):
            return Response(200, b'{"ok":true}')
        if url.endswith(self.domain["endpoint"]):
            if not isinstance(request, str):
                assert request.method == "POST" and json.loads(request.data) == self.domain["body"]
            return Response(200, json.dumps(self.domain["expected"]).encode())
        return Response(200, b"<title>Your app</title>")

    def run(self, *, approved=None, tries=6):
        worker = SimpleNamespace(last={"state": "running_coding"}, closed=False, cancelled=threading.Event(),
            stop_after_iteration=threading.Event(), loop_saved={"task_snapshot": {"approved_plan": approved or {}}})
        worker.progress = lambda state, **values: worker.last.update(state=state, **values)
        posts = []
        gateway = SimpleNamespace(order_id="order_" + self.stage, iteration_receipt=posts.append, post_plan=posts.append)
        verification = {"phase": self.stage, "max_iterations": tries, "max_wall_seconds": 60,
            "per_call_seconds": 10, "max_turns": 20 if self.provider == "claude" else None,
            "profile": "web_app_vercel_v1", "check_recipe": "web_app_vercel_checks_v1",
            "plan_digest": (approved or {}).get("plan_digest")}
        manifest = {"instruction": self.domain["title"], "coding_loop_verification": verification}
        result = coding_loop.run_coding_loop(worker, manifest, gateway, self.coding, time.monotonic() + 60)
        return result, worker, posts

    def closed(self, result, worker):
        worker.closed = True
        worker.last.update(state="stopped", task_verified=result["task_verified"], evidence={
            "processes_stopped": True, "task_verified": result["task_verified"], "product_build": result,
            "inventory": {"inventory_complete": True, "processes": [{"pid": 99999999, "created_at": 1}]}})


@pytest.mark.parametrize("provider", ["claude", "codex"])
@pytest.mark.parametrize("domain", DOMAINS, ids=["business", "consumer"])
def test_golden_provider_plan_approval_repair_stop_preview_deploy_review(tmp_path, monkeypatch, provider, domain):
    journey = Journey(tmp_path, monkeypatch, provider, domain)
    planned, plan_worker, _posts = journey.run()
    assert planned["outcome"] == "plan_ready" and not planned["task_acceptance_verified"]
    assert not (tmp_path / "index.html").exists() and not journey.nodes
    journey.stage = "implement"
    journey.allowed = False
    with pytest.raises(ValueError, match="coding_cancelled_or_expired"):
        journey.run(approved=planned["plan"])
    assert journey.code_calls == 0
    journey.allowed = True
    result, worker, posts = journey.run(approved=planned["plan"])
    assert [item["outcome"] for item in worker.last["iteration_receipts"]] == ["tests_failed", "task_passed", "pass"]
    assert result["functional_status"] == "verified" and result["availability_status"] == "available"
    assert result["iterations"] == 3 and result["task_verified"]
    assert worker.last["iteration_receipts"][-1]["diagnosis"]["tap_summary"] == {"pass": 1, "fail": 0}
    assert result["http_journey"][0]["body_sha256"] == hashlib.sha256(json.dumps(domain["expected"]).encode()).hexdigest()
    assert len(posts) == 3 and journey.tracker.stopped()
    assert coding_loop.start_product_preview(worker, journey.coding, result)["status"] == "unavailable"
    journey.closed(result, worker)
    ready = coding_loop.start_product_preview(worker, journey.coding, result)
    assert ready["ready"] and ready["url"] == "http://127.0.0.1:43127"
    assert ready["file_manifest_sha256"] == result["file_manifest_sha256"]
    assert worker.product_preview.tracker is not journey.tracker
    stopped = worker.product_preview.stop()
    assert stopped["processes_stopped"] and not stopped["ready"] and "url" not in stopped
    assert all(not process.alive and not process.children_alive for process in journey.nodes)
    ready_again = coding_loop.start_product_preview(worker, journey.coding, result)
    assert ready_again["ready"]
    worker.product_preview.stop()
    reviewed = []
    prepare_review = lambda source: reviewed.append({"target": "preview", "source_commit": source["source_commit"],
        "file_manifest_sha256": source["file_manifest_sha256"], "deployment_authorized": False})
    prepare_review(result)
    assert reviewed == [{"target": "preview", "source_commit": result["source_commit"],
        "file_manifest_sha256": result["file_manifest_sha256"], "deployment_authorized": False}]


@pytest.mark.parametrize("domain", DOMAINS, ids=["business", "consumer"])
def test_preview_allows_runtime_scratch_but_rejects_other_ignored_source(tmp_path, monkeypatch, domain):
    journey = Journey(tmp_path, monkeypatch, "codex", domain)
    planned, _worker, _posts = journey.run()
    journey.stage, journey.fail_first = "implement", False
    result, worker, _posts = journey.run(approved=planned["plan"])
    journey.closed(result, worker)
    assert (tmp_path / ".tmp").is_dir()
    assert coding_loop.start_product_preview(worker, journey.coding, result)["ready"]
    with (tmp_path / ".git/info/exclude").open("a") as exclude:
        exclude.write("\n/unreviewed.html\n")
    (tmp_path / "unreviewed.html").write_text("<title>" + domain["title"] + "</title>")
    receipt = worker.product_preview.status()
    assert receipt["status"] == "expired" and receipt["processes_stopped"]
    assert not receipt["ready"] and "url" not in receipt


@pytest.mark.parametrize("domain", DOMAINS, ids=["business", "consumer"])
def test_local_preview_launches_reviewed_files_inside_workspace_and_kills_the_process(tmp_path, monkeypatch, domain):
    journey = Journey(tmp_path, monkeypatch, "codex", domain)
    planned, _worker, _posts = journey.run()
    journey.stage, journey.fail_first = "implement", False
    result, worker, _posts = journey.run(approved=planned["plan"])
    journey.closed(result, worker)
    launch = loop_checks._launch
    starts = []

    def confined(workspace, temporary, node, argv, env, output, *, readonly=False):
        assert workspace.is_relative_to(tmp_path) and (workspace / "dev-server.mjs").is_file()
        assert not (workspace / "review").exists() and not (workspace / ".git").exists()
        assert readonly and env["HOST"] == "127.0.0.1" and env["PORT"] == "43127"
        assert argv == ["dev-server.mjs"]
        process = launch(workspace, temporary, node, argv, env, output, readonly=readonly)
        starts.append((workspace, process))
        return process

    monkeypatch.setattr(loop_checks, "_launch", confined)
    try:
        receipt = coding_loop.start_product_preview(worker, journey.coding, result)
        assert receipt["ready"] and receipt["url"] == "http://127.0.0.1:43127"
        assert coding_loop.start_product_preview(worker, journey.coding, result) == receipt
        assert len(starts) == 1 and starts[0][1].alive
    finally:
        assert worker.product_preview.stop()["processes_stopped"]
    workspace, process = starts[0]
    assert not process.alive and not process.children_alive and not workspace.exists()


@pytest.mark.parametrize("provider", ["claude", "codex"])
@pytest.mark.parametrize("domain", DOMAINS, ids=["business", "consumer"])
@pytest.mark.parametrize("failure", ["malformed_zero", "source_drift", "cap_exhaustion", "timeout"])
def test_provider_failure_matrix_never_previews_or_authorizes_deployment(tmp_path, monkeypatch, provider, domain, failure):
    journey = Journey(tmp_path, monkeypatch, provider, domain)
    planned, _worker, _posts = journey.run()
    journey.stage = "implement"
    if failure == "source_drift":
        (tmp_path / "review/spec.md").write_text(journey.plan["spec"] + " changed")
        with pytest.raises(ValueError, match="coding_plan_changed"):
            journey.run(approved=planned["plan"])
        assert journey.code_calls == 0
        return
    journey.bad_json = failure == "malformed_zero"
    if failure == "cap_exhaustion":
        journey.fail_first = False
    if failure == "timeout":
        clock = {"now": time.monotonic()}
        monkeypatch.setattr(coding_loop, "time", SimpleNamespace(monotonic=lambda: clock["now"], time=time.time))
        def timeout(instruction, deadline):
            clock["now"] = deadline
            raise ValueError("coding_cancelled_or_expired")
        monkeypatch.setattr(journey.coding, "run", timeout)
    result, worker, _posts = journey.run(approved=planned["plan"], tries=1 if failure == "cap_exhaustion" else 6)
    assert result["outcome"] == {"malformed_zero": "cli_error", "cap_exhaustion": "stopped", "timeout": "timeout"}[failure]
    assert not result["task_verified"] and "product_preview" not in result
    assert all(not process.alive and not process.children_alive for process in journey.nodes) and journey.tracker.stopped()
    journey.closed(result, worker)
    assert not coding_loop.start_product_preview(worker, journey.coding, result)["ready"]


@pytest.mark.parametrize("provider", ["claude", "codex"])
@pytest.mark.parametrize("domain", DOMAINS, ids=["business", "consumer"])
@pytest.mark.parametrize("failure", ["missing", "old", "signed_out"])
def test_cli_failure_matrix_refuses_before_claim(tmp_path, monkeypatch, provider, domain, failure):
    executable = tmp_path / provider
    executable.touch(mode=0o700)
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(agent_status, "provider_binary", lambda name: None if failure == "missing" else str(executable))
    def probe(binary, args, **kwargs):
        if failure == "missing":
            pytest.fail("A missing provider cannot be probed")
        if args == ["--version"]:
            version = b"0.0.1" if failure == "old" else b"2.1.284" if provider == "claude" else b"0.138.0"
            return 0, version if provider == "claude" else b"codex-cli " + version
        return 1, b"not signed in"
    monkeypatch.setattr(agent_status, "_probe", probe)
    status = agent_status._status(provider)
    assert not status["available"]
    monkeypatch.setattr(agent_status, "statuses", lambda: [status])
    gateway = SimpleNamespace(order_id="order_fixture", venture_id="ven_fixture", execution_id="exec_fixture",
        claim_attempted=False, claimed=False)
    gateway.claim = lambda: pytest.fail("Unavailable CLI cannot claim an order")
    worker = DeviceWorker(gateway, lambda value: None)
    monkeypatch.setattr(worker, "_notify_terminal", lambda *args, **kwargs: None)
    manifest = {"instruction": domain["title"], "applications": ["com.companyos.app"], "origins": [],
        "actions": ["editor.edit", "terminal.execute"], "credit_ceiling_micro": 0, "duration_seconds": 60,
        "coding_loop_verification": {"operation": "workspace_loop_v1", "profile": "web_app_vercel_v1",
            "check_recipe": "web_app_vercel_checks_v1", "provider": provider, "workspace_slug": "fixture",
            "phase": "implement", "network": "none", "read_scope": "venture_workspace", "write_scope": "venture_workspace"}}
    worker.run_coding_loop({"state": "approved", "task_snapshot": {"artifact_profile": "web_app_vercel_v1"}}, manifest)
    assert worker.last["state"] == "not_started" and not gateway.claim_attempted
    assert worker.last["failure_code"] == ("coding_provider_not_installed" if failure == "missing" else "coding_sign_in_required")


@pytest.mark.parametrize("domain", DOMAINS, ids=["business", "consumer"])
@pytest.mark.parametrize("body", [b"not json", b"{}", b'{"wrong":true}', b"[]", b"x" * (loop_checks.MAX_OUTPUT + 1)])
def test_independent_journey_rejects_wrong_body_despite_http_200(domain, body):
    plan = {"spec": "```http-journey\n" + json.dumps([{"method": "POST", "path": domain["endpoint"],
        "body": domain["body"], "status": 200, "expected_json": domain["expected"]}]) + "\n```"}
    result = loop_checks.verify_http_journey(lambda step: (200, body), loop_checks.http_journey(plan))
    assert result["functional_status"] == "failed" and result["failures"][0]["kind"] == "http_journey"
    assert body.decode("utf-8", "replace") not in str(result["failures"])


@pytest.mark.parametrize("domain", DOMAINS, ids=["business", "consumer"])
@pytest.mark.parametrize("state", ["source_drift", "expired", "process_exit", "port_collision", "register_failure", "tracker_unavailable"])
def test_preview_failure_matrix_cleans_owned_children(tmp_path, monkeypatch, domain, state):
    journey = Journey(tmp_path, monkeypatch, "codex", domain)
    planned, _worker, _posts = journey.run()
    journey.stage, journey.fail_first = "implement", False
    result, worker, _posts = journey.run(approved=planned["plan"])
    journey.closed(result, worker)
    if state == "port_collision":
        original = journey.spawn
        def collision(argv, **kwargs):
            process = original(argv, **kwargs)
            if argv[-1] == "dev-server.mjs":
                process.kill()
                process.returncode = 1
            return process
        monkeypatch.setattr(owned_coding.psutil, "Popen", collision)
    if state == "register_failure":
        monkeypatch.setattr(FakeTracker, "register", lambda self, process: (_ for _ in ()).throw(ValueError("identity unavailable")))
    if state == "tracker_unavailable":
        monkeypatch.setattr(loop_checks, "_PreviewProcesses", lambda: (_ for _ in ()).throw(PermissionError("PID enumeration unavailable")))
    receipt = coding_loop.start_product_preview(worker, journey.coding, result)
    if state in {"port_collision", "register_failure", "tracker_unavailable"}:
        assert receipt["status"] == "failed" and receipt["processes_stopped"]
    else:
        assert receipt["ready"]
        if state == "source_drift":
            (tmp_path / "index.html").write_text("<title>A changed app</title>")
        elif state == "expired":
            worker.product_preview.receipt["expires_at"] = 1
        else:
            worker.product_preview.process.kill()
        receipt = worker.product_preview.status()
        assert receipt["status"] == ("failed" if state == "process_exit" else "expired")
    assert not receipt["ready"] and "url" not in receipt
    assert all(not process.alive and not process.children_alive for process in journey.nodes)


@pytest.mark.parametrize("provider", ["claude", "codex"])
@pytest.mark.parametrize("domain", DOMAINS, ids=["business", "consumer"])
def test_malformed_json_after_terminal_success_is_not_success(provider, domain):
    terminal = {"type": "result", "subtype": "success", "is_error": False} if provider == "claude" else {"type": "turn.completed"}
    for extra in (b"\n{", b"\n[]", b"\n" + domain["title"].encode()):
        assert provider_outcome(json.dumps(terminal).encode() + extra, provider, 0)["outcome"] == "cli_error"


@pytest.mark.parametrize("domain", DOMAINS, ids=["business", "consumer"])
@pytest.mark.parametrize("mutation", ["external", "redirect", "health_only", "command", "method", "too_many"])
def test_http_contract_has_no_arbitrary_target_or_code(domain, mutation):
    step = {"method": "POST", "path": domain["endpoint"], "status": 200, "expected_json": domain["expected"]}
    if mutation == "external":
        step["path"] = "https://remote.invalid/api/check"
    elif mutation == "redirect":
        step["status"] = 302
    elif mutation == "health_only":
        step["path"] = "/api/health"
    elif mutation == "command":
        step["command"] = "echo not-allowed"
    elif mutation == "method":
        step["method"] = "DELETE"
    plan = {"spec": "```http-journey\n" + json.dumps([step] * (7 if mutation == "too_many" else 1)) + "\n```"}
    with pytest.raises(ValueError, match="coding_http_contract_invalid"):
        loop_checks.http_journey(plan)
    assert loop_checks.http_journey({"spec": domain["title"]}) == []


@pytest.mark.parametrize("domain", DOMAINS, ids=["business", "consumer"])
def test_invalid_http_contract_is_repaired_before_plan_approval(tmp_path, monkeypatch, domain):
    journey = Journey(tmp_path, monkeypatch, "codex", domain)
    journey.plan["spec"] = domain["title"] + " /api/health\n```http-journey\nnot JSON\n```"
    result, worker, posts = journey.run()
    assert result["outcome"] == "plan_invalid" and "plan" not in result
    assert result["iterations"] == 3 and not result["task_verified"]
    assert not journey.nodes and len(posts) == 3


@pytest.mark.parametrize("domain", DOMAINS, ids=["business", "consumer"])
def test_post_only_api_is_available_and_independently_verified(tmp_path, monkeypatch, domain):
    journey = Journey(tmp_path, monkeypatch, "codex", domain)
    original = journey.open
    def open(request, **kwargs):
        if isinstance(request, str) and request.endswith(domain["endpoint"]):
            return Response(405, b"use POST")
        return original(request, **kwargs)
    monkeypatch.setattr(loop_checks, "build_opener", lambda *args: SimpleNamespace(open=open))
    planned, _worker, _posts = journey.run()
    journey.stage, journey.fail_first = "implement", False
    result, _worker, _posts = journey.run(approved=planned["plan"])
    assert result["outcome"] == "pass" and result["availability_status"] == "available"
    assert result["functional_status"] == "verified"


@pytest.mark.parametrize("domain", DOMAINS, ids=["business", "consumer"])
@pytest.mark.parametrize("state", ["available", "protected", "redirect", "timeout"])
@pytest.mark.parametrize("target", ["preview", "production"])
@pytest.mark.parametrize("method", ["GET", "POST"])
def test_deploy_never_runs_approved_side_effect_journeys_on_live_target(tmp_path, monkeypatch, domain, state, target, method):
    import httpx
    from desktop_runtime.owned_terminal import OwnedTerminal

    (tmp_path / "api").mkdir()
    (tmp_path / "review").mkdir()
    (tmp_path / "index.html").write_text("<title>" + domain["title"] + "</title>")
    (tmp_path / "api/health.js").write_text("export default (req,res)=>res.end('{}');")
    (tmp_path / (domain["endpoint"].lstrip("/") + ".js")).write_text("export default (req,res)=>res.end('{}');")
    (tmp_path / "package.json").write_text('{"type":"module"}')
    step = {"method": method, "path": domain["endpoint"], "status": 200, "expected_json": domain["expected"]}
    if method == "POST": step["body"] = domain["body"]
    (tmp_path / "review/spec.md").write_text("```http-journey\n" + json.dumps([step]) + "\n```")
    _trusted_git(tmp_path, "init", "--quiet", "--initial-branch=main")
    _trusted_git(tmp_path, "add", "--all", "--force", "--", ".")
    _trusted_git(tmp_path, "commit", "--quiet", "-m", "approved app")
    verification = {"operation": "workspace_deploy_v1", "provider": "vercel", "target": target,
        "workspace_slug": "fixture", "source_commit": _trusted_git(tmp_path, "rev-parse", "HEAD"),
        "file_manifest_sha256": loop_checks.source_manifest(tmp_path)["file_manifest_sha256"]}
    if target == "production":
        (tmp_path / ".vercel").mkdir()
        link = tmp_path / ".vercel/project.json"
        link.write_text(json.dumps({"projectId": "project_fixture", "orgId": "org_fixture"}))
        verification["project_link_sha256"] = owned_deploy._project_link(link)[1]
    deploy = owned_deploy.OwnedDeploy(venture_id="ven_fixture", verification=verification,
        tracker=FakeTracker(), authorize=lambda: True, cancelled=lambda: False)
    monkeypatch.setattr(deploy, "preflight", lambda **kwargs: "/fake/vercel")
    monkeypatch.setattr(OwnedTerminal, "workspace", lambda self: tmp_path)
    commands, requests = [], []
    def run(argv, *args, **kwargs):
        commands.append(argv)
        return 0, b"https://fixture.vercel.app" if "deploy" in argv else b"account"
    monkeypatch.setattr(deploy, "_run", run)
    def handle(request):
        assert request.url.host == "fixture.vercel.app"
        assert request.method == "GET" and request.url.path in {"/", "/api/health"}
        assert request.content == b""
        requests.append(request)
        if state == "timeout":
            raise httpx.ReadTimeout("unavailable")
        if state == "protected":
            return httpx.Response(401)
        if state == "redirect":
            return httpx.Response(302, headers={"Location": "https://remote.invalid/"})
        return httpx.Response(200, json={"ok": True})
    real_client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: real_client(transport=httpx.MockTransport(handle), **kwargs))
    result = deploy.run(time.monotonic() + 60)
    assert [argv[1] for argv in commands] == ["whoami", "deploy"]
    assert commands[1][-1] == "--target=" + target and deploy.deploy_attempted
    assert result["availability_status"] == ("protected" if state == "protected" else
        "unavailable" if state in {"redirect", "timeout"} else "available")
    assert result["functional_status"] == "not_verified_on_live" and result["http_journey"] == []
    assert all(request.url.host == "fixture.vercel.app" for request in requests)


@pytest.mark.parametrize("domain", DOMAINS, ids=["business", "consumer"])
@pytest.mark.parametrize("failure", [None, "temp_survivor", "group_survivor", "denied", "pid_reused"])
def test_preview_stop_covers_orphaned_group_and_private_temp_children(tmp_path, monkeypatch, domain, failure):
    import os
    import signal
    import psutil

    tracker_class = loop_checks._PreviewProcesses
    journey = Journey(tmp_path, monkeypatch, "codex", domain)
    planned, _worker, _posts = journey.run()
    journey.stage, journey.fail_first = "implement", False
    result, worker, _posts = journey.run(approved=planned["plan"])
    journey.closed(result, worker)
    host, processes, trackers = SimpleNamespace(pid=1), [], []
    state = {"failure": failure, "hidden_group": failure == "group_survivor"}
    process_signals = []

    class Process(FakeProcess):
        def __init__(self, pid, group, cwd, port):
            super().__init__(alive=True, port=port)
            self.pid, self.group, self.directory = pid, group, cwd

        def create_time(self): return float(self.pid)
        def parents(self): return [host]
        def children(self, **kwargs): return []
        def is_running(self): return self.alive
        def suspend(self): pass
        def uids(self): return SimpleNamespace(real=os.getuid())
        def send_signal(self, sig):
            process_signals.append((self.pid, sig))
            if self.pid != 103 or state["failure"] != "temp_survivor":
                super().kill()
        def terminate(self): self.send_signal(signal.SIGTERM)
        def kill(self): self.send_signal(signal.SIGKILL)
        def cwd(self):
            if self.pid == 103 and state["failure"] == "denied": raise psutil.AccessDenied(self.pid)
            return str(self.directory)

    def tracker():
        value = tracker_class.__new__(tracker_class)
        value.owner, value.baseline, value.observed, value.complete = host, set(), {}, True
        value.workspaces, value.started_at = set(), 0
        value._lock, value._watch_stop, value._watch = threading.RLock(), threading.Event(), None
        value._start_watch = lambda: None
        trackers.append(value)
        return value

    def spawn(argv, **kwargs):
        assert kwargs["start_new_session"] is True
        temporary, port = Path(kwargs["env"]["TMPDIR"]).resolve(), int(kwargs["env"]["PORT"])
        trackers[-1].watch_workspace(temporary)
        processes.extend([Process(101, 101, temporary / "app", port),
            Process(102, 101, tmp_path, port), Process(103, 103, temporary / "daemon", port),
            Process(104, 104, tmp_path, port)])
        return processes[0]

    def group(pid):
        if pid == host.pid: return 1
        for process in processes:
            if process.pid == pid and process.alive: return process.group
        raise ProcessLookupError()

    signals = []
    def killpg(group_id, sig):
        members = [process for process in processes if process.alive and process.group == group_id]
        if not members and not (group_id == 101 and state["hidden_group"]): raise ProcessLookupError()
        if sig:
            signals.append((group_id, sig))
            for process in members:
                if process.pid == 103 and state["failure"] == "temp_survivor": continue
                process.kill()

    monkeypatch.setattr(loop_checks, "_PreviewProcesses", tracker)
    monkeypatch.setattr(loop_checks.psutil, "Popen", spawn)
    monkeypatch.setattr(loop_checks.psutil, "process_iter", lambda *args: [process for process in processes if process.alive])
    def process(pid):
        for candidate in processes:
            if candidate.pid == pid and candidate.alive: return candidate
        raise psutil.NoSuchProcess(pid)
    monkeypatch.setattr(loop_checks.psutil, "Process", process)
    monkeypatch.setattr(loop_checks.psutil, "wait_procs", lambda values, **kwargs:
        ([process for process in values if not process.alive], [process for process in values if process.alive]))
    monkeypatch.setattr(loop_checks.os, "getpgid", group)
    monkeypatch.setattr(loop_checks.os, "killpg", killpg)
    monkeypatch.setattr(loop_checks.atexit, "register", lambda *args: None)
    monkeypatch.setattr(loop_checks.atexit, "unregister", lambda *args: None)
    try:
        receipt = coding_loop.start_product_preview(worker, journey.coding, result)
        assert receipt["ready"] is True
        preview = worker.product_preview
        private = next(iter(trackers[0].workspaces))
        assert private == processes[2].directory.parent and private != Path(preview.process.directory)
        processes[0].kill()
        if failure == "pid_reused":
            processes[1].kill()
            replacement = Process(101, 101, tmp_path, 43127)
            replacement.create_time = lambda: 1001.0
            processes.append(replacement)
        process_signals.clear()
        stopped = preview.stop()
        assert stopped["processes_stopped"] is (failure is None)
        assert not stopped["ready"] and "url" not in stopped
        assert processes[3].alive is True
        assert not any(pid == processes[3].pid for pid, _sig in process_signals)
        if failure == "pid_reused":
            assert replacement.alive and not any(group_id == 101 for group_id, _sig in signals)
            assert not any(pid == replacement.pid for pid, _sig in process_signals)
        assert (103, signal.SIGTERM) in process_signals or failure == "denied"
        if failure:
            assert stopped["status"] == "failed" and stopped["stop_available"] is True and private.exists()
        else:
            assert not processes[1].alive and not processes[2].alive
            assert not private.exists()
    finally:
        state.update(failure=None, hidden_group=False)
        if failure == "pid_reused": replacement.alive = False
        trackers[0].complete = True
        assert worker.product_preview.stop()["processes_stopped"] is True
