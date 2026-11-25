import hashlib
import json
import socket
import sys
import tempfile
import threading
import time
from types import SimpleNamespace

import psutil
import pytest

from desktop_runtime import coding_loop, loop_checks, loop_prompts
from desktop_runtime.owned_processes import OwnedProcesses


@pytest.mark.skipif(sys.platform != "linux", reason="Linux isolation required")
@pytest.mark.parametrize("backend", ["bubblewrap", "restricted"])
@pytest.mark.parametrize("domain", [
    {"title": "Warehouse dispatch board", "endpoint": "dispatch", "value": "queued"},
    {"title": "Home recipe journal", "endpoint": "recipes", "value": "saved"},
])
def test_linux_runs_reviewed_tests_and_http_checks(tmp_path, monkeypatch, backend, domain):
    if backend == "restricted":
        original = loop_checks.shutil.which
        monkeypatch.setattr(loop_checks.shutil, "which", lambda name: None if name == "bwrap" else original(name))
    node = tmp_path.parent / (tmp_path.name + "-node")
    loop_checks.shutil.copy2(loop_checks.node_binary(), node)
    monkeypatch.setattr(loop_checks, "node_binary", lambda: str(node))
    fixture(tmp_path)
    (tmp_path / "index.html").write_text(f"<title>{domain['title']}</title><h1>{domain['title']}</h1>")
    endpoint = domain["endpoint"]
    expected = {"result": domain["value"]}
    (tmp_path / f"api/{endpoint}.js").write_text(
        "export default (req,res)=>{res.setHeader('Content-Type','application/json');res.end(" +
        json.dumps(json.dumps(expected)) + ");};")
    plan = {"spec": domain["title"] + "\n```http-journey\n" + json.dumps([
        {"method": "GET", "path": f"/api/{endpoint}", "status": 200, "expected_json": expected},
    ]) + "\n```"}
    tracker = OwnedProcesses()
    try:
        result = loop_checks.run("web_app_vercel_checks_v1", tmp_path, tracker=tracker, plan=plan,
            tasks=[{"acceptance": [{"test_file": "tests/split.test.js", "test_name": "splits evenly"}]}],
            deadline=time.monotonic() + 20)
        assert result["passed"] is True, result
        assert result["tap_summary"] == {"pass": 1, "fail": 0}
        assert result["functional_status"] == "verified"
        assert result["http_probes"] == [{"path": "/", "status": 200}, *[
            {"path": path, "status": 200} for path in sorted([f"/api/{endpoint}", "/api/health"])]]
    finally:
        tracker.stop_all()


@pytest.mark.parametrize("title", ["Warehouse dispatch board", "Home recipe journal"])
def test_windows_missing_isolation_does_not_suggest_macos_tools(monkeypatch, title):
    monkeypatch.setattr(loop_checks, "node_binary", lambda: "node.exe")
    monkeypatch.setattr(loop_checks.sys, "platform", "win32")
    assert not loop_checks.preflight(), title
    next_step = loop_checks.checks_next_step()
    assert "Windows" in next_step and "Build activity" in next_step
    assert "sandbox-exec" not in next_step


@pytest.mark.parametrize("title", ["Warehouse dispatch board", "Home recipe journal"])
def test_missing_linux_isolation_names_install_and_retry(tmp_path, monkeypatch, title):
    fixture(tmp_path)
    (tmp_path / "index.html").write_text(f"<title>{title}</title>")
    original = loop_checks.shutil.which
    monkeypatch.setattr(loop_checks.sys, "platform", "linux")
    monkeypatch.setattr(loop_checks.shutil, "which", lambda name: None if name in {"bwrap", "unshare"} else original(name))
    result = loop_checks.run("web_app_vercel_checks_v1", tmp_path)
    failure = next(item for item in result["failures"] if item["kind"] == "checks_unavailable")
    assert "bubblewrap" in failure["message"] and "Build activity" in failure["message"]
    assert "Node" not in failure["message"] and "Mac" not in failure["message"]


@pytest.mark.skipif(sys.platform != "linux", reason="Linux isolation required")
@pytest.mark.parametrize("backend", ["bubblewrap", "restricted"])
@pytest.mark.parametrize("title", ["Warehouse dispatch board", "Home recipe journal"])
def test_linux_checks_deny_host_access_and_limit_resources(tmp_path, monkeypatch, backend, title):
    if backend == "restricted":
        original = loop_checks.shutil.which
        monkeypatch.setattr(loop_checks.shutil, "which", lambda name: None if name == "bwrap" else original(name))
    outside = tmp_path.parent / (tmp_path.name + "-private")
    outside.write_text("private fixture")
    with socket.socket() as host, tempfile.TemporaryDirectory(prefix="checks-unix-") as private, \
            socket.socket(socket.AF_UNIX) as host_unix:
        host.bind(("127.0.0.1", 0))
        host.listen(1)
        port = host.getsockname()[1]
        unix_path = private + "/host.sock"
        host_unix.bind(unix_path)
        host_unix.listen(1)
        ipc_check = """import ctypes,socket
for kind in (socket.SOCK_DGRAM,socket.SOCK_SEQPACKET):
    try:
        socket.socketpair(socket.AF_UNIX,kind)
    except PermissionError:
        pass
    else:
        raise AssertionError('Only private stream IPC is allowed')
left,right=socket.socketpair()
assert ctypes.CDLL(None).connect(left.fileno(),ctypes.c_char_p(bytes(16)),16)==-1
try:
    left.connect(HOST_SOCKET)
except OSError:
    pass
else:
    raise AssertionError('Stream IPC reconnected to your host')
""".replace("HOST_SOCKET", repr(unix_path))
        source = """import test from 'node:test';import assert from 'node:assert/strict';
import fs from 'node:fs';import net from 'node:net';import {execFileSync} from 'node:child_process';
test('checks stay isolated',async()=>{
const outside=OUTSIDE;
assert.throws(()=>fs.readFileSync(outside));
assert.throws(()=>fs.writeFileSync(outside,'changed'));
assert.throws(()=>fs.readFileSync('/etc/passwd'));
assert.throws(()=>fs.readFileSync('.git/config'));
assert.equal(process.env.SUPYMEM_TEST_PRIVATE,undefined);
assert.ok(fs.readFileSync('/proc/net/route','utf8').trim().split('\\n').length===1);
assert.match(fs.readFileSync('/proc/self/limits','utf8'),/Max cpu time\\s+120\\s+120/);
assert.match(fs.readFileSync('/proc/self/limits','utf8'),/Max data size\\s+536870912\\s+536870912/);
assert.match(fs.readFileSync('/proc/self/limits','utf8'),/Max file size\\s+4194304\\s+4194304/);
assert.match(fs.readFileSync('/proc/self/limits','utf8'),/Max open files\\s+128\\s+128/);
fs.writeFileSync('check-result.txt','workspace');
fs.writeFileSync(process.env.TMPDIR+'/check-result.txt','temporary');
execFileSync('/usr/bin/python3',['-I','-c',IPC]);
await new Promise((resolve,reject)=>{const connection=net.connect(HOST_SOCKET);
connection.on('connect',()=>{connection.destroy();reject(new Error('Host socket escaped'));});
connection.on('error',resolve);connection.setTimeout(500,()=>{connection.destroy();resolve();});});
await new Promise((resolve,reject)=>{const connection=net.connect(PORT,'127.0.0.1');
connection.on('connect',()=>{connection.destroy();reject(new Error('Host connection escaped'));});
connection.on('error',resolve);connection.setTimeout(500,()=>{connection.destroy();resolve();});});
});""".replace("OUTSIDE", json.dumps(str(outside))).replace("PORT", str(port)).replace(
            "HOST_SOCKET", json.dumps(unix_path)).replace("IPC", json.dumps(ipc_check))
        fixture(tmp_path, source)
        (tmp_path / "index.html").write_text(f"<title>{title}</title>")
        (tmp_path / ".git").mkdir()
        (tmp_path / ".git/config").write_text("private fixture")
        monkeypatch.setenv("SUPYMEM_TEST_PRIVATE", "private fixture")
        tracker = OwnedProcesses()
        try:
            result = loop_checks.run("web_app_static_checks_v1", tmp_path, tracker=tracker,
                deadline=time.monotonic() + 10)
            assert result["passed"] is True, json.dumps(result)
            assert result["tap_summary"] == {"pass": 1, "fail": 0}
            assert outside.read_text() == "private fixture"
            assert (tmp_path / ".git/config").read_text() == "private fixture"
            assert (tmp_path / "check-result.txt").read_text() == "workspace"
            host.settimeout(.1)
            with pytest.raises(socket.timeout):
                host.accept()
            host_unix.settimeout(.1)
            with pytest.raises(socket.timeout):
                host_unix.accept()
        finally:
            tracker.stop_all()


@pytest.mark.skipif(sys.platform != "linux", reason="Linux isolation required")
@pytest.mark.parametrize("backend", ["bubblewrap", "restricted"])
@pytest.mark.parametrize("title", ["Warehouse dispatch board", "Home recipe journal"])
def test_linux_preview_cannot_write_its_snapshot(tmp_path, monkeypatch, backend, title):
    if backend == "restricted":
        original = loop_checks.shutil.which
        monkeypatch.setattr(loop_checks.shutil, "which", lambda name: None if name == "bwrap" else original(name))
    (tmp_path / "index.html").write_text(title)
    tracker = OwnedProcesses()
    with tempfile.TemporaryDirectory() as temporary, tempfile.TemporaryFile() as output:
        process = loop_checks._launch(tmp_path, loop_checks.Path(temporary), loop_checks.node_binary(),
            ["-e", "const fs=require('node:fs'),assert=require('node:assert/strict');" +
                "assert.throws(()=>fs.writeFileSync('index.html','changed'));" +
                "fs.writeFileSync(process.env.TMPDIR+'/result.txt','checked');"], {}, output, readonly=True)
        tracker.register(process)
        try:
            assert process.wait(timeout=5) == 0
            assert (tmp_path / "index.html").read_text() == title
        finally:
            loop_checks._stop(process, tracker)


@pytest.mark.skipif(sys.platform != "linux", reason="Linux isolation required")
@pytest.mark.parametrize("backend", ["bubblewrap", "restricted"])
@pytest.mark.parametrize("title", ["Warehouse dispatch board", "Home recipe journal"])
def test_linux_checks_stop_at_deadline(tmp_path, monkeypatch, backend, title):
    if backend == "restricted":
        original = loop_checks.shutil.which
        monkeypatch.setattr(loop_checks.shutil, "which", lambda name: None if name == "bwrap" else original(name))
    fixture(tmp_path, "import test from 'node:test';test(" + json.dumps(title) +
        ",()=>new Promise(()=>{}));setInterval(()=>{},1000);")
    tracker = OwnedProcesses()
    try:
        result = loop_checks.run("web_app_static_checks_v1", tmp_path, tracker=tracker,
            deadline=time.monotonic() + .4)
        assert result["passed"] is False
        assert any(item["kind"] == "timeout" for item in result["failures"])
        assert tracker.stopped()
    finally:
        tracker.stop_all()


@pytest.mark.skipif(sys.platform != "linux", reason="Linux isolation required")
@pytest.mark.parametrize("backend", ["bubblewrap", "restricted"])
@pytest.mark.parametrize("title", ["Warehouse dispatch board", "Home recipe journal"])
def test_linux_launch_failure_stops_its_owned_process(tmp_path, monkeypatch, backend, title):
    if backend == "restricted":
        original = loop_checks.shutil.which
        monkeypatch.setattr(loop_checks.shutil, "which", lambda name: None if name == "bwrap" else original(name))
    fixture(tmp_path)
    (tmp_path / "index.html").write_text(f"<title>{title}</title>")
    processes, popen = [], psutil.Popen
    def spawn(*args, **kwargs):
        process = popen(*args, **kwargs)
        processes.append(process)
        return process
    monkeypatch.setattr(loop_checks.psutil, "Popen", spawn)
    with socket.socket() as occupied, tempfile.TemporaryDirectory() as temporary, tempfile.TemporaryFile() as output:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen(1)
        with pytest.raises(OSError):
            loop_checks._launch(tmp_path, loop_checks.Path(temporary), loop_checks.node_binary(), ["dev-server.mjs"],
                {"PORT": str(occupied.getsockname()[1]), "HOST": "127.0.0.1"}, output)
        assert len(processes) == 1 and processes[0].poll() is not None


def fixture(workspace, test_source=None):
    (workspace / ".supymem-venture").write_text("ven_tests\n")
    (workspace / "package.json").write_text('{"type":"module"}')
    (workspace / "index.html").write_text("<title>Tip splitter</title><h1>Split the bill</h1>")
    (workspace / "api").mkdir()
    (workspace / "api/health.js").write_text("export default (req,res) => {res.setHeader('Content-Type','application/json');res.end(JSON.stringify({ok:true}));};")
    (workspace / "api/split.js").write_text("export const split=(total,people)=>total/people; export default (req,res)=>{res.end(JSON.stringify({share:split(20,2)}));};")
    (workspace / "dev-server.mjs").write_text("""import http from 'node:http';
import {readFile} from 'node:fs/promises';
http.createServer(async(req,res)=>{try{
if(req.url.startsWith('/api/')){const mod=await import('.'+req.url+'.js');return mod.default(req,res);}
res.end(await readFile('index.html'));
}catch{res.statusCode=404;res.end('missing');}}).listen(Number(process.env.PORT),'127.0.0.1');""")
    (workspace / "tests").mkdir()
    (workspace / "tests/split.test.js").write_text(test_source or "import test from 'node:test';import assert from 'node:assert/strict';import {split} from '../api/split.js';test('splits evenly',()=>assert.equal(split(20,2),10));")


@pytest.mark.parametrize("title", ["Warehouse dispatch board", "Home recipe journal"])
def test_local_manifest_checks_tool_authored_container_source(tmp_path, monkeypatch, title):
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    fixture(tmp_path)
    source = ("FROM node:22-alpine\nLABEL description=\"" + title + "\"\nEXPOSE 3000\n").encode()
    (tmp_path / "Dockerfile").write_bytes(source)
    row = next(row for row in loop_checks.source_manifest(tmp_path)["files"] if row["path"] == "Dockerfile")
    assert row["sha256"] == hashlib.sha256(source).hexdigest()
    assert row["bytes"] == len(source)
    assert (tmp_path / "Dockerfile").read_bytes() == source
    monkeypatch.delenv("SUPYMEM_MODE")
    with pytest.raises(ValueError, match="coding_file_policy_invalid"):
        loop_checks.source_manifest(tmp_path)


def test_source_manifest_excludes_runtime_review_test_and_credentials_metadata(tmp_path):
    fixture(tmp_path)
    for name in ("review/spec.md", ".vercel/project.json", "docs/README.md"):
        path = tmp_path / name
        path.parent.mkdir(exist_ok=True)
        path.write_text("private review")
    result = loop_checks.source_manifest(tmp_path)
    assert [item["path"] for item in result["files"]] == ["api/health.js", "api/split.js", "index.html", "package.json"]
    assert len(result["file_manifest_sha256"]) == 64


@pytest.mark.parametrize("content", ['{"type":"module","dependencies":{"x":"1"}}', '{"type":"module","scripts":{"test":"echo wrong"}}'])
def test_dependencies_and_scripts_refused(tmp_path, content):
    fixture(tmp_path)
    (tmp_path / "package.json").write_text(content)
    with pytest.raises(ValueError, match="package_policy_invalid"):
        loop_checks.source_manifest(tmp_path)


def test_symlinks_secret_values_and_bad_task_shapes_are_rejected(tmp_path):
    fixture(tmp_path)
    (tmp_path / "link.js").symlink_to(tmp_path / "api/split.js")
    with pytest.raises(ValueError, match="path_invalid"):
        loop_checks.source_manifest(tmp_path)
    (tmp_path / "link.js").unlink()
    (tmp_path / "dummy.js").write_text('{"token":"fakeprivatevalue1234567890"}')
    with pytest.raises(ValueError, match="secret_detected"):
        loop_checks.source_manifest(tmp_path)
    for path in ("../outside.js", "review/spec.md", ".claude/settings.json", "tests/package.json", "node_modules/x.js"):
        assert not loop_prompts.safe_path(path)
    assert loop_prompts.safe_path("package.json") is loop_prompts.is_local()
    with pytest.raises(ValueError, match="plan_invalid"):
        loop_prompts.validate_plan("spec", [{"id":"one","title":"title","files":[{}],"acceptance":[]}])


def test_tap_failure_scrubbing_and_explicit_pass_shape(tmp_path, monkeypatch):
    result = loop_checks.parse_tap(f"not ok 1 - sum\n location: '{tmp_path}/tests/a.js:4:1'\n error: token=fakeprivatevalue12345\n# pass 0\n# fail 1\n".encode(), tmp_path)
    assert result["tap_summary"] == {"pass": 0, "fail": 1}
    assert result["failures"][0]["file"] == "tests/a.js"
    assert "fakeprivate" not in result["failures"][0]["message"]
    (tmp_path / "index.html").write_text("<title>Demo</title>")
    assert loop_checks.run("static_html_checks_v1", tmp_path)["passed"] is True
    monkeypatch.setattr(loop_checks, "preflight", lambda: False)
    result = loop_checks.run("web_app_vercel_checks_v1", tmp_path)
    assert result["passed"] is False and any(item["kind"] == "checks_unavailable" for item in result["failures"])


@pytest.mark.parametrize("domain", ["inventory", "meal-planner"])
def test_mac_checks_grant_only_node_and_the_private_server_socket(tmp_path, domain):
    workspace, scratch = tmp_path / domain, tmp_path / "scratch"
    node = tmp_path / "founder" / "bin" / "node"
    node.parent.mkdir(parents=True)
    node.touch()
    alias = tmp_path / "node"
    alias.symlink_to(node)
    listener = scratch / "server.sock"
    offline = loop_checks.sandbox_profile(workspace, scratch, str(alias))
    online = loop_checks.sandbox_profile(workspace, scratch, str(alias), listener=listener)
    assert "(deny network*)" in offline and "(allow network-" not in offline
    assert [line for line in online.splitlines() if line.startswith("(allow network-")] == [
        f'(allow network-bind network-inbound (local unix-socket (path-literal "{listener.resolve()}")))']
    assert f'(literal "{node.resolve()}")' in offline
    assert f'"{node.parent.parent.resolve()}"' not in offline


@pytest.mark.skipif(sys.platform != "linux", reason="Linux relay required")
@pytest.mark.parametrize("domain", ["dispatch.example", "recipes.example"])
def test_linux_relay_validates_browser_authority_before_forwarding(monkeypatch, domain):
    from http.client import HTTPConnection
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from desktop_runtime.linux_checks import LoopbackRelay

    received = []

    class Upstream(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append(self.rfile.read(int(self.headers["Content-Length"])))
            self.send_response(200)
            self.end_headers()

    with ThreadingHTTPServer(("127.0.0.1", 0), Upstream) as upstream:
        threading.Thread(target=upstream.serve_forever, daemon=True).start()
        channel, peer = socket.socketpair()
        with peer:
            monkeypatch.setattr(LoopbackRelay, "connection", lambda self: socket.create_connection(upstream.server_address))
            relay = LoopbackRelay(channel, 0)
            try:
                port = relay.http.server_port

                def post(hosts, origins=()):
                    client = HTTPConnection("127.0.0.1", port, timeout=2)
                    try:
                        client.putrequest("POST", "/api/save", skip_host=True)
                        for host in hosts:
                            client.putheader("Host", host)
                        for origin in origins:
                            client.putheader("Origin", origin)
                        client.putheader("Content-Length", "4")
                        client.endheaders(b"save")
                        with client.getresponse() as response:
                            response.read()
                            return response.status
                    finally:
                        client.close()

                authority = f"localhost:{port}"
                assert post([authority], [f"http://{authority}"]) == 200
                assert post([f"127.0.0.1:{port}"]) == 200
                assert post([f"{domain}:{port}"]) == 403
                assert post([]) == 403
                assert post(["localhost:1"]) == 403
                assert post([authority, authority]) == 403
                assert post([authority], [f"http://{domain}"]) == 403
                assert post([authority], [f"http://{authority}"] * 2) == 403
                assert received == [b"save", b"save"]
            finally:
                relay.close()
                upstream.shutdown()


@pytest.mark.parametrize("domain", ["Coordinate warehouse shipments", "Plan meals at home"])
@pytest.mark.parametrize("denied", [False, True])
def test_preview_inventory_captures_only_its_own_children(domain, denied):
    tracker = loop_checks._PreviewProcesses.__new__(loop_checks._PreviewProcesses)
    tracker._lock, tracker.complete = threading.RLock(), True
    child = SimpleNamespace(pid=112, create_time=lambda: 2)
    def children(**kwargs):
        if denied:
            raise psutil.AccessDenied(111)
        return [child]
    root = SimpleNamespace(pid=111, create_time=lambda: 1, children=children, purpose=domain)
    tracker.observed = {(111, 1): root}
    tracker.owner = SimpleNamespace(children=lambda **kwargs: pytest.fail("A preview must not capture coding or unrelated processes"))
    tracker.capture()
    assert tracker.complete is not denied
    assert set(tracker.observed) == ({(111, 1)} if denied else {(111, 1), (112, 2)})


@pytest.mark.parametrize("domain", ["Coordinate warehouse shipments", "Plan meals at home"])
@pytest.mark.parametrize("invalid", ["processes_stopped", "task_verified", "product_build", "inventory"])
def test_preview_requires_a_clean_bound_stop_before_starting(tmp_path, monkeypatch, domain, invalid):
    monkeypatch.setattr(loop_checks, "_PreviewProcesses", SimpleNamespace)
    inventory = {"inventory_complete": True, "processes": [{"pid": 111, "created_at": 1}]}
    monkeypatch.setattr(OwnedProcesses, "receipt_stopped", staticmethod(lambda value: value == inventory))
    monkeypatch.setattr(loop_checks, "preflight", lambda: pytest.fail("Invalid Stop cannot start a preview or inspect a Mac"))
    build = {"outcome": "pass", "task_verified": True, "artifact_verified": True,
        "source_commit": "a" * 40, "file_manifest_sha256": "b" * 64, "purpose": domain}
    closed = {"processes_stopped": True, "task_verified": True, "product_build": build, "inventory": inventory}
    closed[invalid] = False if invalid in {"processes_stopped", "task_verified"} else {}
    preview = loop_checks.OwnedProductPreview(workspace=tmp_path, build=build, closed=closed)
    assert preview.start()["status"] == "unavailable" and preview.process is None


@pytest.mark.parametrize("domain", ["Warehouse shipment notes", "Family cooking notes"])
def test_preview_start_failure_writes_scrubbed_origin(tmp_path, monkeypatch, domain):
    from tests.company_os_harness import load_company_os_app
    load_company_os_app()
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(loop_checks, "preflight", lambda: True)
    monkeypatch.setattr(OwnedProcesses, "receipt_stopped", staticmethod(lambda receipt: True))
    build = {"outcome": "pass", "task_verified": True, "artifact_verified": True,
             "source_commit": "a" * 40, "file_manifest_sha256": "b" * 64}
    closed = {"processes_stopped": True, "task_verified": True, "product_build": build}
    preview = loop_checks.OwnedProductPreview(workspace=tmp_path, build=build, closed=closed)

    def unavailable():
        raise ValueError(domain)

    monkeypatch.setattr(preview, "_source_current", unavailable)
    assert preview.start()["status"] == "failed"
    path = tmp_path / "logs/errors.jsonl"
    assert path.exists()
    entry, = [json.loads(line) for line in path.read_text().splitlines()]
    assert entry["error_type"] == "ValueError" and entry["frames"][-1].startswith(__name__ + ":")
    assert domain not in path.read_text()


@pytest.mark.skipif(sys.platform != "linux" or not loop_checks.preflight(), reason="Linux check isolation and Node required")
@pytest.mark.parametrize("backend", ["bubblewrap", "restricted"])
@pytest.mark.parametrize("domain", ["Warehouse dispatch board", "Family recipe journal"])
def test_linux_preview_outlives_its_build_thread(tmp_path, monkeypatch, domain, backend):
    from tests.company_os_harness import load_company_os_app
    load_company_os_app()
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path / "data"))
    if backend == "restricted":
        original = loop_checks.shutil.which
        monkeypatch.setattr(loop_checks.shutil, "which", lambda name: None if name == "bwrap" else original(name))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    fixture(workspace)
    (workspace / "index.html").write_text(f"<title>{domain}</title><h1>{domain}</h1>")
    build = {"outcome": "pass", "task_verified": True, "artifact_verified": True,
             "source_commit": "a" * 40, "file_manifest_sha256": loop_checks.source_manifest(workspace)["file_manifest_sha256"]}
    closed = {"processes_stopped": True, "task_verified": True, "product_build": build}
    monkeypatch.setattr(OwnedProcesses, "receipt_stopped", staticmethod(lambda receipt: True))
    monkeypatch.setattr(loop_checks, "_trusted_git", lambda root, command, spec, **options: (root / spec.split(":", 1)[1]).read_bytes())
    unrelated_pid = psutil.Process().ppid()
    original_cwd = psutil.Process.cwd

    def cwd(process):
        if process.pid == unrelated_pid:
            raise psutil.AccessDenied(process.pid)
        return original_cwd(process)

    monkeypatch.setattr(psutil.Process, "cwd", cwd)
    preview = loop_checks.OwnedProductPreview(workspace=workspace, build=build, closed=closed)
    monkeypatch.setattr(preview, "_source_current", lambda: True)
    results = []
    worker = threading.Thread(target=lambda: results.append(preview.start()))
    try:
        worker.start()
        worker.join(timeout=15)
        assert not worker.is_alive()
        assert results[0]["ready"] is True
        time.sleep(1.5)
        assert preview.status()["ready"] is True
        assert preview.process.poll() is None
    finally:
        assert preview.stop()["processes_stopped"] is True


@pytest.mark.skipif(not loop_checks.preflight(), reason="Real check isolation and Node needed")
@pytest.mark.parametrize("test_source,expected", [(None, True),
    ("import test from 'node:test'; test('splits evenly',()=>{throw new Error('Wrong total')});", False)])
def test_real_node_tests_and_real_localhost_probes(tmp_path, test_source, expected):
    fixture(tmp_path, test_source)
    tracker = OwnedProcesses()
    tasks = [{"acceptance": [{"test_file": "tests/split.test.js", "test_name": "splits evenly"}]}]
    try:
        result = loop_checks.run("web_app_vercel_checks_v1", tmp_path, tracker=tracker,
            plan={"spec": "GET /api/health and /api/split"}, tasks=tasks, deadline=time.monotonic() + 20)
        assert result["passed"] is expected, result
        if expected:
            assert result["tap_summary"]["pass"] == 1
            assert result["http_probes"] == [{"path": "/", "status": 200}, {"path": "/api/health", "status": 200}, {"path": "/api/split", "status": 200}]
        else:
            assert any("Wrong total" in item["message"] for item in result["failures"])
    finally:
        tracker.stop_all()
    assert tracker.stopped()


@pytest.mark.skipif(not loop_checks.preflight(), reason="Real check isolation and Node needed")
def test_real_check_cannot_pass_missing_reviewed_test(tmp_path):
    fixture(tmp_path)
    tracker = OwnedProcesses()
    try:
        result = loop_checks.run("web_app_vercel_checks_v1", tmp_path, tracker=tracker, plan={"spec":"/api/health"},
            tasks=[{"acceptance":[{"test_file":"tests/split.test.js","test_name":"missing behavior"}]}], deadline=time.monotonic() + 15)
        assert result["passed"] is False and any("reviewed acceptance test" in item["message"] for item in result["failures"])
    finally:
        tracker.stop_all()


def test_ordinary_detached_local_dev_server_stops_without_later_source_change(tmp_path):
    """A normal test starts a server child; its test process finishes first."""
    script = tmp_path / "test-runner.py"
    script.write_text("""import subprocess, sys, time
p=subprocess.Popen([sys.executable,'-m','http.server','0','--bind','127.0.0.1'],start_new_session=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
open('server.pid','w').write(str(p.pid))
time.sleep(.6)
""")
    tracker = OwnedProcesses()
    tracker.watch_workspace(tmp_path)
    parent = psutil.Popen([sys.executable, str(script)], cwd=tmp_path, start_new_session=True)
    tracker.register(parent)
    parent.wait(timeout=3)
    child_pid = int((tmp_path / "server.pid").read_text())
    assert psutil.pid_exists(child_pid)
    before = {path.name: path.read_bytes() for path in tmp_path.iterdir() if path.is_file()}
    try:
        assert coding_loop._quiescent(tmp_path, tracker, seconds=.3)
        assert not psutil.pid_exists(child_pid) or psutil.Process(child_pid).status() == psutil.STATUS_ZOMBIE
        assert {path.name: path.read_bytes() for path in tmp_path.iterdir() if path.is_file()} == before
    finally:
        tracker.stop_all()
        try:
            psutil.Process(child_pid).kill()
        except psutil.NoSuchProcess:
            pass
