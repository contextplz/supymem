"""Deterministic CLI fixtures exercise approvals, checkpoint custody and guards."""
import json
import threading
import tomllib
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from desktop_runtime import coding_loop, device_journal, loop_diagnose, loop_prompts
from desktop_runtime.owned_coding import OwnedCoding, _trusted_git, provider_outcome, workspace_files


TASKS = [{"id": "app", "title": "Build a tip splitter", "files": ["index.html", "api/health.js", "api/split.js", "dev-server.mjs"],
    "acceptance": [{"test_file": "tests/split.test.js", "test_name": "splits evenly", "behaviour": "equal shares"}]}]
SPEC = "A tip splitter with /api/health and /api/split. Recent splits stay on this device only."


@pytest.mark.parametrize("domain", ["supplier-invoices", "trail-packing"])
def test_restoring_git_snapshot_keeps_repository_valid(tmp_path, domain):
    _trusted_git(tmp_path, "init", "--quiet", "--initial-branch=main")
    (tmp_path / "index.html").write_text(domain)
    before = workspace_files(tmp_path, include_git=True)
    coding_loop._restore(tmp_path, before, git=True)
    assert (tmp_path / ".git/objects").is_dir()
    assert (tmp_path / ".git/refs").is_dir()
    assert "index.html" in _trusted_git(tmp_path, "status", "--porcelain=v1")


@pytest.mark.parametrize("domain", ["supplier-invoices", "trail-packing"])
def test_existing_workspace_recovers_missing_empty_git_directories(monkeypatch, tmp_path, domain):
    import subprocess
    from desktop_runtime.owned_terminal import OwnedTerminal

    _trusted_git(tmp_path, "init", "--quiet", "--initial-branch=main")
    (tmp_path / "index.html").write_text(domain)
    coding_loop._remove(tmp_path / ".git/objects")
    coding_loop._remove(tmp_path / ".git/refs")
    monkeypatch.setattr(OwnedTerminal, "workspace", lambda self: tmp_path)
    coding = OwnedCoding(venture_id="ven_fixture", workspace_slug="fixture", provider="codex",
        tracker=None, authorize=lambda: True)
    assert coding._workspace() == tmp_path
    assert "index.html" in _trusted_git(tmp_path, "status", "--porcelain=v1")
    checked = subprocess.run(["/usr/bin/git", "-C", str(tmp_path), "status", "--porcelain=v1"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5, check=False)
    assert checked.returncode == 0 and b"index.html" in checked.stdout


@pytest.mark.parametrize("domain", ["supplier-invoices", "trail-packing"])
def test_watch_commands_are_visible_without_credentials(domain, tmp_path):
    for command in (f"node tests/{domain}.test.js", "curl --api-key synthetic-private-fixture"):
        events = [("claude", {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Bash", "input": {"command": command}}]}}),
            ("codex", {"type": "item.started", "item": {"type": "command_execution", "command": command}}),
            ("cursor", {"type": "tool_call", "tool_call": {"shellToolCall": {"args": {"command": command}}}}),
            ("opencode", {"type": "tool_use", "part": {"type": "tool", "state": {"input": {"command": command}}}})]
        for provider, event in events:
            text = loop_diagnose.stream_text(json.dumps(event).encode(), provider, tmp_path)
            assert isinstance(text, str)
            assert "synthetic-private-fixture" not in text
            if command.startswith("node"):
                assert command in text


@pytest.mark.parametrize("domain", ["supplier-invoices", "trail-packing"])
def test_watch_file_changes_compare_the_saved_result_not_a_failed_attempt(domain):
    before = {"unchanged.js": b"same", f"{domain}/old.js": b"old"}
    after = {"unchanged.js": b"same", f"{domain}/new.js": b"new"}
    assert coding_loop._changed_files(before, after) == [
        {"path": f"{domain}/new.js", "deleted": False}, {"path": f"{domain}/old.js", "deleted": True}]
    assert coding_loop._changed_files(before, before) == []


@pytest.mark.parametrize("domain", ["supplier-invoices", "trail-packing"])
def test_live_activity_keeps_first_last_and_count_with_a_bounded_ordered_retry_queue(tmp_path, monkeypatch, domain):
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    scope = ("local", "account", "device")
    device_journal.create(scope, "venture", "build")
    worker = SimpleNamespace(last={"state": "running_coding"})
    delivered = []
    def progress(state, **values):
        worker.last.update(state=state, **values)
        device_journal.save(scope, "venture", "build", worker.last)
    worker.progress = progress
    def offline(row):
        raise OSError("offline")
    worker.gateway = SimpleNamespace(coding_event=offline)
    for index in range(100):
        coding_loop._event(worker, f"{domain}: Saved file {index}", tmp_path)
    saved = device_journal.read(scope, "venture", "build")
    events = saved["events"]
    assert len(events) == 12 and saved["activity_count"] == 100
    assert events[0]["text"].endswith("Saved file 0") and events[-1]["text"].endswith("Saved file 99")
    assert len(saved["pending_activity"]) == 12
    recovered = SimpleNamespace(last=saved)
    def recovered_progress(state, **values):
        recovered.last.update(state=state, **values)
    recovered.progress = recovered_progress
    coding_loop.post_events(recovered, SimpleNamespace(coding_event=delivered.append))
    assert [row["seq"] for row in delivered] == list(range(1, 13))
    assert not recovered.last["pending_activity"]
    assert delivered[0]["text"].endswith("Saved file 0")
    assert delivered[-1]["text"].endswith("Saved file 99")
    recovered.gateway = SimpleNamespace(coding_event=delivered.append)
    coding_loop._event(recovered, f"{domain}: Back online", tmp_path)
    assert delivered[-1]["seq"] == 13 and recovered.last["activity_count"] == 101


class Tracker:
    def stop_all(self, **kwargs):
        return True
    def watch_workspace(self, workspace):
        pass


def setup(tmp_path, monkeypatch, changes, *, phase="plan", approved=None, checks=None, max_iterations=6, post_fail=False):
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    (tmp_path / ".supymem-venture").write_text("ven_product\n")
    worker = SimpleNamespace(last={"state": "running_coding"}, cancelled=threading.Event(),
        stop_after_iteration=threading.Event(), loop_saved={"task_snapshot": {"approved_plan": approved or {}}})
    worker.progress = lambda state, **values: worker.last.update(state=state, **values)
    calls, posted = [], []
    def run(instruction, deadline):
        calls.append(instruction)
        return changes[min(len(calls) - 1, len(changes) - 1)](tmp_path) or {"outcome": "pass"}
    coding = SimpleNamespace(_workspace=lambda: tmp_path, provider="claude", venture_id="ven_product", run=run,
        tracker=Tracker(), authorize=lambda: True, pagebuilder_reference=None)
    def post(value):
        if post_fail and not posted:
            posted.append("network failure")
            raise OSError()
        posted.append(value)
    gateway = SimpleNamespace(order_id="order_fixture", iteration_receipt=post, post_plan=post)
    verification = {"max_iterations": max_iterations, "max_wall_seconds": 60, "max_turns": 20,
        "per_call_seconds": 10, "profile": "web_app_vercel_v1", "check_recipe": "web_app_vercel_checks_v1",
        "phase": phase, "plan_digest": (approved or {}).get("plan_digest")}
    manifest = {"instruction": "Build a tip splitter", "coding_loop_verification": verification}
    monkeypatch.setattr(coding_loop, "_quiescent", lambda *args: True)
    if checks is not None:
        values = iter(checks)
        monkeypatch.setattr(coding_loop.loop_checks, "run", lambda *args, **kwargs: next(values))
    return worker, gateway, coding, manifest, calls, posted


def plan_files(workspace):
    (workspace / "review").mkdir(exist_ok=True)
    (workspace / "review/spec.md").write_text(SPEC)
    tasks = [dict(task) for task in TASKS]
    if loop_prompts.is_local() and "package.json" not in tasks[0]["files"]:
        tasks[0]["files"] = ["package.json", *tasks[0]["files"]]
    (workspace / "review/tasks.json").write_text(json.dumps(tasks))


def run(worker, gateway, coding, manifest):
    return coding_loop.run_coding_loop(worker, manifest, gateway, coding, time.monotonic() + 60)


@pytest.mark.parametrize("domain", ["supplier_invoices", "trail_packing"])
@pytest.mark.parametrize("changed", [True, False])
def test_utility_run_checks_and_records_only_real_workspace_changes(tmp_path, monkeypatch, domain, changed):
    def edit(workspace):
        if changed:
            (workspace / f"{domain}.py").write_text("def list_items(items):\n    return sorted(items)\n")

    worker, gateway, coding, manifest, calls, posted = setup(tmp_path, monkeypatch, [edit], max_iterations=1)
    gateway.execution_id = "dex_utility"
    coding._git_state = OwnedCoding._git_state
    coding._verify_changes = OwnedCoding._verify_changes
    manifest["instruction"] = f"Add a script for {domain}"
    manifest["coding_loop_verification"].update(profile="workspace_utility_v1", check_recipe="workspace_utility_checks_v1",
                                                phase="implement")
    if changed:
        result = run(worker, gateway, coding, manifest)
        assert result["task_verified"] and not result["task_acceptance_verified"]
        assert [item["path"] for item in result["changed_files"]] == [f"{domain}.py"]
        assert posted[0]["file_manifest_sha256"] == result["file_manifest_sha256"]
    else:
        with pytest.raises(ValueError, match="coding_change_set_invalid"):
            run(worker, gateway, coding, manifest)
        assert posted == []
    assert calls == [manifest["instruction"]]


def plan_then_implement(tmp_path, monkeypatch, changes, checks, **kwargs):
    worker, gateway, coding, manifest, *_ = setup(tmp_path, monkeypatch, [plan_files])
    plan = run(worker, gateway, coding, manifest)["plan"]
    return setup(tmp_path, monkeypatch, changes, phase="implement", approved=plan, checks=checks, **kwargs)


def app_files(workspace):
    (workspace / "package.json").write_text('{"type":"module"}')
    (workspace / "index.html").write_text("<title>Tip splitter</title>")
    (workspace / "api").mkdir(exist_ok=True)
    (workspace / "api/health.js").write_text("export default (req,res) => res.end('ok');")
    (workspace / "api/split.js").write_text("export default (req,res) => res.end('split');")
    (workspace / "dev-server.mjs").write_text("// local server")
    (workspace / "tests").mkdir(exist_ok=True)
    (workspace / "tests/split.test.js").write_text("// tests")


def failed(message="The total is wrong", count=1):
    return {"passed": False, "failures": [{"kind": "test", "test": str(index), "message": message} for index in range(count)]}


def test_plan_stops_for_separate_approval_and_queues_receipts(tmp_path, monkeypatch):
    worker, gateway, coding, manifest, calls, posted = setup(tmp_path, monkeypatch, [plan_files], post_fail=True)
    result = run(worker, gateway, coding, manifest)
    assert result["outcome"] == "plan_ready" and result["task_acceptance_verified"] is False
    assert len(calls) == 1 and not (tmp_path / "package.json").exists()
    tasks = [{**TASKS[0], "files": ["package.json", *TASKS[0]["files"]]}] if loop_prompts.is_local() else TASKS
    assert result["plan"]["plan_digest"] == loop_prompts.validate_plan(SPEC, tasks)["plan_digest"]
    assert worker.last["iteration_receipts"][0]["caps_used"]["iterations"] == 1
    assert worker.last["plan_posted"] is True
    assert worker.last["iteration_receipts"][0]["posted"] is False
    coding_loop.post_pending(worker, gateway)
    assert worker.last["iteration_receipts"][0]["posted"] is True


def test_invalid_plan_gets_two_repairs(tmp_path, monkeypatch):
    def invalid(workspace):
        plan_files(workspace)
        (workspace / "review/tasks.json").write_text("[]")
    worker, gateway, coding, manifest, calls, *_ = setup(tmp_path, monkeypatch, [invalid, invalid])
    result = run(worker, gateway, coding, manifest)
    assert result["outcome"] == "plan_invalid" and len(calls) == 3 and "plan" not in result


@pytest.mark.parametrize("domain", ["Supplier payment reconciliation", "Community bicycle repairs"])
def test_missing_plan_files_get_precise_repairs_and_keep_the_spec(tmp_path, monkeypatch, domain):
    def spec_only(workspace):
        (workspace / "review").mkdir(exist_ok=True)
        (workspace / "review/spec.md").write_text(domain + "\n" + SPEC)
        return {"outcome": "cli_error", "exit_code": 0}
    def invalid_tasks(workspace):
        assert (workspace / "review/spec.md").read_text() == domain + "\n" + SPEC
        (workspace / "review/tasks.json").write_text("[]")
    def complete(workspace):
        spec = (workspace / "review/spec.md").read_text()
        plan_files(workspace)
        (workspace / "review/spec.md").write_text(spec)
    worker, gateway, coding, manifest, calls, *_ = setup(tmp_path, monkeypatch,
        [spec_only, invalid_tasks, complete])
    manifest["instruction"] = domain
    result = run(worker, gateway, coding, manifest)
    assert result["outcome"] == "plan_ready" and result["iterations"] == 3
    assert "Write review/tasks.json" in calls[1]
    assert "review/tasks.json" in calls[2] and "acceptance" in calls[2]
    assert result["plan"]["spec"] == domain + "\n" + SPEC
    assert all(len(receipt["summary"]) <= 280 for receipt in worker.last["iteration_receipts"])


@pytest.mark.parametrize("domain", ["Supplier payment reconciliation", "Community bicycle repairs"])
@pytest.mark.parametrize("outcome", ["cli_error", "timeout"])
def test_failed_plan_keeps_safe_deliverables_in_its_checkpoint(tmp_path, monkeypatch, domain, outcome):
    def partial(workspace):
        (workspace / "review").mkdir(exist_ok=True)
        (workspace / "review/spec.md").write_text(domain)
        return {"outcome": outcome, "exit_code": 0}
    worker, gateway, coding, manifest, *_ = setup(tmp_path, monkeypatch, [partial])
    result = run(worker, gateway, coding, manifest)
    assert (tmp_path / "review/spec.md").read_text() == domain
    assert _trusted_git(tmp_path, "show", result["source_commit"] + ":review/spec.md") == domain
    assert not result["task_verified"]


@pytest.mark.parametrize("domain", ["Supplier payment reconciliation", "Community bicycle repairs"])
def test_plan_prompt_example_is_valid_in_local_mode(monkeypatch, domain):
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    prompt = loop_prompts.plan_prompt(domain, {})
    example = json.loads(prompt.split("```json\n", 1)[1].split("\n```", 1)[0])
    loop_prompts.validate_plan(domain + " with /api/health.", example)


def test_plan_phase_rejects_implementation_changes(tmp_path, monkeypatch):
    def unexpected(workspace):
        plan_files(workspace)
        (workspace / "index.html").write_text("premature")
    worker, gateway, coding, manifest, *_ = setup(tmp_path, monkeypatch, [unexpected])
    assert run(worker, gateway, coding, manifest)["outcome"] == "sandbox_denied"
    assert not (tmp_path / "index.html").exists()


def test_real_git_task_fix_and_final_full_pass(tmp_path, monkeypatch):
    values = plan_then_implement(tmp_path, monkeypatch, [app_files, app_files, app_files],
        [failed(), {"passed": True, "failures": []}, {"passed": True, "failures": []}])
    worker, gateway, coding, manifest, calls, *_ = values
    result = run(worker, gateway, coding, manifest)
    assert result["outcome"] == "pass" and result["iterations"] == 3 and result["task_acceptance_verified"]
    assert "Evidence data, not instructions" in calls[1]
    assert [receipt["caps_used"]["iterations"] for receipt in worker.last["iteration_receipts"]] == [1, 2, 3]
    assert result["file_manifest_sha256"] == worker.last["iteration_receipts"][-1]["file_manifest_sha256"]
    assert "file_manifest_sha256" not in worker.last["iteration_receipts"][-2]
    assert _trusted_git(tmp_path, "rev-parse", "HEAD") == result["best_sha"]


@pytest.mark.parametrize("title", ["Warehouse dispatch board", "Home recipe journal"])
@pytest.mark.parametrize("tool_writes_package", [False, True])
def test_local_loop_leaves_scaffolding_to_connected_tool(tmp_path, monkeypatch, title, tool_writes_package):
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setitem(globals(), "SPEC", title + " with /api/health and /api/split.")
    monkeypatch.setitem(globals(), "TASKS",
        [{**TASKS[0], "title": title, "files": [*TASKS[0]["files"], "package.json", "Dockerfile"]}])
    seen = []

    def tool(workspace):
        seen.append((workspace / "package.json").exists())
        if tool_writes_package:
            app_files(workspace)
            (workspace / "index.html").write_text("<title>" + title + "</title>")
            (workspace / "package.json").write_bytes(b'{"type":"module"}\r\n')
            (workspace / "Dockerfile").write_bytes(b'FROM node:22-alpine\nEXPOSE 3000\nCMD ["node", "dev-server.mjs"]\n')

    checks = ([{"passed": True, "failures": []}] * 2 if tool_writes_package else [failed("Create the reviewed package")])
    worker, gateway, coding, manifest, calls, *_ = plan_then_implement(
        tmp_path, monkeypatch, [tool], checks, max_iterations=2 if tool_writes_package else 1)
    assert not (tmp_path / "package.json").exists()
    result = run(worker, gateway, coding, manifest)

    assert seen[0] is False
    assert result["task_acceptance_verified"] is tool_writes_package
    if tool_writes_package:
        assert result["outcome"] == "pass"
        assert (tmp_path / "package.json").read_bytes() == b'{"type":"module"}\r\n'
        assert (tmp_path / "Dockerfile").read_bytes() == b'FROM node:22-alpine\nEXPOSE 3000\nCMD ["node", "dev-server.mjs"]\n'
        assert "package.json" in calls[0] and "Dockerfile" in calls[0]
    else:
        assert result["outcome"] != "pass"
        assert not (tmp_path / "package.json").exists()


@pytest.mark.parametrize("title", ["Warehouse dispatch board", "Home recipe journal"])
def test_local_plan_checks_and_briefs_tool_authored_package(monkeypatch, title):
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    prompt = loop_prompts.plan_prompt("Build " + title, {})
    assert "Supymem creates the fixed package.json" not in prompt
    assert "Dockerfile" in prompt
    with pytest.raises(ValueError, match="coding_plan_invalid"):
        loop_prompts.validate_plan(title + " with /api/health.", TASKS)
    tasks = [{**TASKS[0], "title": title, "files": [*TASKS[0]["files"], "package.json"]}]
    assert loop_prompts.validate_plan(title + " with /api/health.", tasks)["tasks"] == tasks
    with pytest.raises(ValueError, match="coding_plan_invalid"):
        loop_prompts.validate_plan(title + " with /api/health.",
            [TASKS[0], {**tasks[0], "id": "package", "files": ["package.json"]}])
    for unsafe in ("tests/package.json", "Package.JSON", ".git/config", "tests/.npmrc"):
        assert not loop_prompts.safe_path(unsafe)


@pytest.mark.parametrize("title", ["Freight invoice reconciliation", "Garden watering journal"])
@pytest.mark.parametrize("disconnected", [False, True])
def test_authority_loss_during_final_cleanup_cannot_publish_success(tmp_path, monkeypatch, title, disconnected):
    worker, gateway, coding, manifest, *_ = plan_then_implement(tmp_path, monkeypatch,
        [app_files, app_files], [{"passed": True, "failures": []}, {"passed": True, "failures": []}])
    manifest["instruction"] = title
    current = [True]
    def authorize():
        if not current[0] and disconnected:
            raise PermissionError("owner disconnected")
        return current[0]
    coding.authorize = authorize
    def cleanup(*args):
        current[0] = False
        return True
    monkeypatch.setattr(coding_loop, "_quiescent", cleanup)
    result = run(worker, gateway, coding, manifest)
    assert result["outcome"] == "stopped"
    assert result["task_verified"] is False


@pytest.mark.parametrize("title", ["Freight invoice reconciliation", "Garden watering journal"])
def test_quota_pause_keeps_the_last_checked_source_and_reset_time(tmp_path, monkeypatch, title):
    worker, gateway, coding, manifest, *_ = plan_then_implement(tmp_path, monkeypatch,
        [app_files, lambda workspace: {"outcome": "quota_paused", "reset_at": 123456}],
        [{"passed": True, "failures": []}])
    manifest["instruction"] = title
    result = run(worker, gateway, coding, manifest)
    assert result["outcome"] == "quota_paused" and result["task_verified"] is False
    assert result["source_commit"] == worker.last["iteration_receipts"][0]["commit_sha"]
    assert _trusted_git(tmp_path, "rev-parse", "HEAD") == result["source_commit"]
    assert (tmp_path / "index.html").is_file() and (tmp_path / "review/tasks.json").is_file()
    assert worker.last["iteration_receipts"][-1]["diagnosis"]["reset_at"] == 123456


def test_partial_task_pass_at_cap_cannot_be_accepted(tmp_path, monkeypatch):
    worker, gateway, coding, manifest, *_ = plan_then_implement(tmp_path, monkeypatch, [app_files],
        [{"passed": True, "failures": []}], max_iterations=1)
    result = run(worker, gateway, coding, manifest)
    assert result["outcome"] == "stopped" and not result["task_verified"]
    assert "file_manifest_sha256" not in result


@pytest.mark.parametrize("outcome", ["quota_paused", "auth_required", "cli_error"])
def test_unchecked_partial_source_is_never_best(tmp_path, monkeypatch, outcome):
    def partial(workspace):
        (workspace / "index.html").write_text("unchecked")
        return {"outcome": outcome}
    worker, gateway, coding, manifest, *_ = plan_then_implement(tmp_path, monkeypatch, [partial], [])
    result = run(worker, gateway, coding, manifest)
    assert result["outcome"] == outcome and not result["task_verified"]
    assert not (tmp_path / "index.html").exists()


@pytest.mark.parametrize("path", [".claude/settings.json", ".mcp.json", ".git/config", "package.json",
    "tests/.claude/settings.json", "tests/.CoDeX/config.toml", "tests/agents.md", "tests/cLaUdE.Md",
    "tests/package.json", "tests/fixtures/Package.JSON", "tests/.npmrc"])
def test_configuration_guard_restores_before_checks(tmp_path, monkeypatch, path):
    (tmp_path / "package.json").write_text('{"type":"module"}')
    def plant(workspace):
        target = workspace / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text('{"scripts":{"test":"echo planted"}}')
    worker, gateway, coding, manifest, *_ = plan_then_implement(tmp_path, monkeypatch, [plant], [])
    before = workspace_files(tmp_path, include_git=True)
    assert run(worker, gateway, coding, manifest)["outcome"] == "sandbox_denied"
    assert not (tmp_path / ".claude").exists()
    if path.startswith("tests/"):
        assert not (tmp_path / path).exists()
    assert json.loads((tmp_path / "package.json").read_bytes()) == {"type": "module"}
    assert (tmp_path / ".git/config").read_bytes() == before[".git/config"]


@pytest.mark.parametrize("drift", ["spec", "tasks_whitespace", "ignored_file"])
def test_approved_plan_bytes_and_commit_are_bound(tmp_path, monkeypatch, drift):
    worker, gateway, coding, manifest, *_ = plan_then_implement(tmp_path, monkeypatch, [app_files], [])
    if drift == "spec":
        (tmp_path / "review/spec.md").write_text(SPEC + " drift")
    elif drift == "tasks_whitespace":
        path = tmp_path / "review/tasks.json"
        path.write_text(json.dumps(json.loads(path.read_text()), indent=2))
    else:
        with (tmp_path / ".git/info/exclude").open("a") as handle:
            handle.write("\nreview/extra.md\n")
        (tmp_path / "review/extra.md").write_text("not reviewed")
    with pytest.raises(ValueError, match="coding_plan_changed"):
        run(worker, gateway, coding, manifest)


@pytest.mark.parametrize("domain,dirty", [("maple-ledger.test", False), ("winter-picnic.test", True)])
def test_product_retry_keeps_previous_branch_and_uses_the_approved_plan(tmp_path, monkeypatch, domain, dirty):
    import hashlib

    def write_product(workspace):
        assert not (workspace / "owner-notes.txt").exists()
        app_files(workspace)
        (workspace / "index.html").write_text(f"<title>{domain}</title>")

    passed = {"passed": True, "failures": []}
    worker, gateway, coding, manifest, calls, _ = plan_then_implement(tmp_path, monkeypatch, [write_product], [passed, passed])
    worker.loop_saved["retry_of"] = "dwo_previous"
    branch = "supymem/loop-" + hashlib.sha256(b"dwo_previous").hexdigest()[:8]
    _trusted_git(tmp_path, "checkout", "-b", branch)
    note = tmp_path / "owner-notes.txt"
    note.write_text(domain)
    previous = coding_loop._checkpoint(tmp_path, "Previous attempt")
    if dirty:
        note.write_text(domain + " unfinished")
        with pytest.raises(ValueError, match="coding_plan_changed"):
            run(worker, gateway, coding, manifest)
        assert not calls and note.read_text() == domain + " unfinished"
    else:
        result = run(worker, gateway, coding, manifest)
        assert result["task_verified"]
        assert "owner-notes.txt" not in [item["path"] for item in result["changed_files"]]
        assert calls and (tmp_path / "index.html").read_text() == f"<title>{domain}</title>"
    assert _trusted_git(tmp_path, "rev-parse", branch) == previous


def test_ignored_and_untracked_source_are_restored(tmp_path):
    _trusted_git(tmp_path, "init", "--quiet", "--initial-branch=main")
    (tmp_path / ".gitignore").write_text("ignored.js\n")
    (tmp_path / "ignored.js").write_text("baseline")
    (tmp_path / "untracked.js").write_text("baseline")
    before = workspace_files(tmp_path)
    coding_loop._checkpoint(tmp_path, "baseline")
    (tmp_path / "ignored.js").write_text("wrong")
    (tmp_path / "untracked.js").write_text("wrong")
    (tmp_path / "new.js").write_text("new")
    coding_loop._restore(tmp_path, before)
    assert workspace_files(tmp_path) == before
    assert "ignored.js" in _trusted_git(tmp_path, "ls-files")


@pytest.mark.parametrize("fault", ["gitfile", "worktree", "include", "commondir", "alternates", "env"])
def test_git_boundary_rejects_before_mutation(tmp_path, monkeypatch, fault):
    workspace, outside = tmp_path / "venture", tmp_path / "outside"
    workspace.mkdir(); outside.mkdir()
    _trusted_git(workspace, "init", "--quiet")
    _trusted_git(outside, "init", "--quiet")
    if fault == "gitfile":
        import shutil
        shutil.rmtree(workspace / ".git")
        (workspace / ".git").write_text(f"gitdir: {outside / '.git'}")
    elif fault in {"worktree", "include"}:
        with (workspace / ".git/config").open("a") as handle:
            handle.write(f"\n[core]\nworktree = {outside}\n" if fault == "worktree" else f"\n[include]\npath = {outside / 'config'}\n")
    elif fault == "env":
        monkeypatch.setenv("GIT_DIR", str(outside / ".git"))
    else:
        target = workspace / (".git/commondir" if fault == "commondir" else ".git/objects/info/alternates")
        target.write_text(str(outside / ".git"))
    before = workspace_files(outside, include_git=True)
    with pytest.raises(ValueError, match="coding_git_scope_invalid"):
        _trusted_git(workspace, "checkout", "-B", "supymem/test")
    assert workspace_files(outside, include_git=True) == before


def test_codex_failed_terminal_never_passes_and_turn_cap_is_honest(tmp_path, monkeypatch):
    assert provider_outcome(b'{"type":"turn.completed"}\n{"type":"turn.failed","error":{"message":"stopped"}}', "codex")["outcome"] == "cli_error"
    assert provider_outcome(b'{"type":"turn.completed"}', "codex", 1)["outcome"] == "cli_error"
    assert provider_outcome(b'{"type":"item.completed"}', "codex")["outcome"] == "cli_error"
    assert provider_outcome(b'{"type":"turn.completed"}', "codex")["outcome"] == "pass"
    assert provider_outcome(b'{"type":"turn.completed","token":"fakeprivatevalue123456789"}', "codex")["outcome"] == "secret_detected"
    assert provider_outcome(b'{"type":"error","error":{"type":"usage_limit_reached"},"reset_at":123}', "codex") == {"outcome": "quota_paused", "reset_at": 123}
    owned = OwnedCoding(venture_id="ven", workspace_slug="tip", provider="codex", tracker=Tracker(), authorize=lambda: True)
    owned.loop_mode, owned.max_turns = True, 40
    with pytest.raises(ValueError, match="turn_cap_unsupported"):
        owned._argv("codex", "Build", tmp_path)
    owned.max_turns = None
    argv = owned._argv("codex", "Build", tmp_path)
    assert "--sandbox" not in argv and "--strict-config" in argv
    config = {}
    for index, argument in enumerate(argv[:-1]):
        if argument == "-c":
            config.update(tomllib.loads(argv[index + 1]))
    assert config["default_permissions"] == "supymem_product"
    policy = config["permissions"]["supymem_product"]
    assert policy["extends"] == ":workspace"
    assert policy["filesystem"][":root"] == "deny"
    assert policy["filesystem"][":minimal"] == "read"
    assert policy["filesystem"][":tmpdir"] == policy["filesystem"][":slash_tmp"] == "deny"
    assert policy["filesystem"][":workspace_roots"]["."] == "write"
    assert policy["filesystem"][":workspace_roots"][".git"] == "read"
    assert policy["filesystem"][":workspace_roots"]["**/.env*"] == "deny"
    assert policy["network"]["enabled"] is False
    assert config["hooks"] == config["mcp_servers"] == {}
    assert config["project_doc_max_bytes"] == 0 and config["web_search"] == "disabled"
    assert config["projects"][str(tmp_path)]["trust_level"] == "untrusted"
    owned.loop_mode = False
    if loop_prompts.is_local():
        local = owned._argv("codex", "Build", tmp_path)
        assert "--sandbox" not in local and "--strict-config" in local
    monkeypatch.setenv("SUPYMEM_MODE", "hosted")
    legacy = owned._argv("codex", "Build", tmp_path)
    assert legacy[legacy.index("--sandbox") + 1] == "workspace-write"
    assert "--ignore-user-config" in argv and "--ignore-rules" in argv
    assert not any("network_access=true" in value for value in argv)
    owned.provider, owned.max_turns = "claude", 40
    argv = owned._argv("claude", "Build", tmp_path)
    settings = json.loads(argv[argv.index("--settings") + 1])
    assert settings["permissions"]["blockReadsOutsideWorkingDirectories"] is True
    assert settings["sandbox"]["filesystem"]["allowRead"] == [str(tmp_path)]
    assert settings["sandbox"]["network"]["allowedDomains"] == []
    assert argv[argv.index("--max-turns") + 1] == "40"
    assert argv[argv.index("--tools") + 1] == "Bash"


def test_diagnosis_stop_quota_repetition_and_scrubbing():
    failure = failed("wrong total")
    delta = {"changed_file_count": 1}
    initial = loop_diagnose.diagnose(failure, {"outcome": "pass"}, delta, [])
    history = [{"signature": initial["signature"], "changed": True, "failure_count": 1, "checked": True}] * 2
    assert loop_diagnose.diagnose(failure, {"outcome": "pass"}, delta, history)["next_action"] == "stop_no_progress"
    assert loop_diagnose.diagnose({"passed": True}, {"outcome": "cli_error"}, delta, [])["outcome"] == "cli_error"
    assert "dummyprivatevalu" not in loop_diagnose.scrub("token=dummyprivatevalue12345")
    assert loop_diagnose.stream_text(b'{"type":"item.completed","item":{"type":"agent_message","text":"Your build is running"}}', "codex", Path("/tmp")) == "Your build is running"


@pytest.mark.parametrize("domain", ["supplier-scheduling", "home-cooking"])
@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_live_history_shows_commands_without_private_values(domain, provider):
    private = "sk-" + "z" * 25
    command = f"node --test {domain}.test.js --token={private}"
    event = {"type": "item.started", "item": {"type": "command_execution", "command": command}} if provider == "codex" else {
        "type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {"command": command}}]}}
    text = loop_diagnose.stream_text(json.dumps(event).encode(), provider, Path("/tmp"))
    assert text.startswith(f"Running: node --test {domain}.test.js")
    assert private not in text


@pytest.mark.usefixtures("private_coding_install")
def test_real_fake_cli_zero_exit_failed_terminal_is_not_success(tmp_path, monkeypatch):
    """No authenticated CLI: this executable writes one ordinary source file."""
    from desktop_runtime.owned_processes import OwnedProcesses
    binary = tmp_path / "fake-codex"
    binary.write_text("#!/bin/sh\nif [ \"$1\" = \"--version\" ]; then\nsleep .05\necho 'codex-cli 0.160.0'\nelse\nprintf '<title>Fixture</title>' > index.html\necho '{\"type\":\"turn.failed\",\"error\":{\"message\":\"ordinary task failed\"}}'\nsleep .05\nfi\n")
    binary.chmod(0o755)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".supymem-venture").write_text("ven_fixture\n")
    tracker = OwnedProcesses()
    coding = OwnedCoding(venture_id="ven_fixture", workspace_slug="tip", provider="codex", tracker=tracker, authorize=lambda: True)
    coding.loop_mode = True
    monkeypatch.setattr(coding, "preflight", lambda: str(binary))
    monkeypatch.setattr(coding, "_workspace", lambda: workspace)
    try:
        result = coding.run("Write a local title", time.monotonic() + 10)
        assert result["exit_code"] == 0 and result["outcome"] == "cli_error"
    finally:
        tracker.stop_all()
    assert tracker.stopped()


def test_final_quiescence_failure_cannot_publish_pass_receipt(tmp_path, monkeypatch):
    worker, gateway, coding, manifest, *_ = plan_then_implement(tmp_path, monkeypatch, [app_files, app_files],
        [{"passed": True, "failures": []}, {"passed": True, "failures": []}])
    monkeypatch.setattr(coding_loop, "_quiescent", lambda *args: False)
    result = run(worker, gateway, coding, manifest)
    assert result["outcome"] == "stopped_unclean" and not result["task_verified"]
    assert worker.last["iteration_receipts"][-1]["outcome"] == "stopped_unclean"
    assert "file_manifest_sha256" not in worker.last["iteration_receipts"][-1]


@pytest.mark.parametrize("phase", ["plan", "implement"])
def test_immediate_stop_during_quiescence_cannot_publish_success(tmp_path, monkeypatch, phase):
    if phase == "plan":
        values = setup(tmp_path, monkeypatch, [plan_files])
    else:
        values = plan_then_implement(tmp_path, monkeypatch, [app_files, app_files],
            [{"passed": True, "failures": []}, {"passed": True, "failures": []}])
    worker, gateway, coding, manifest, calls, posted = values
    def stop_during_quiescence(*args):
        worker.cancelled.set()
        return True
    monkeypatch.setattr(coding_loop, "_quiescent", stop_during_quiescence)
    result = run(worker, gateway, coding, manifest)
    assert result["outcome"] == "stopped" and not result["task_verified"]
    assert not result["task_acceptance_verified"] and "plan" not in result
    receipt = worker.last["iteration_receipts"][-1]
    assert receipt["outcome"] == "stopped" and "file_manifest_sha256" not in receipt
    assert receipt["posted"] and posted[-1]["outcome"] == "stopped"
    assert len(calls) == (1 if phase == "plan" else 2)
    assert _trusted_git(tmp_path, "rev-parse", "HEAD") == result["best_sha"]


def test_soft_stop_is_between_tries_and_never_accepts_partial_app(tmp_path, monkeypatch):
    worker, gateway, coding, manifest, calls, *_ = plan_then_implement(tmp_path, monkeypatch, [app_files],
        [{"passed": True, "failures": []}])
    worker.stop_after_iteration.set()
    result = run(worker, gateway, coding, manifest)
    assert result["outcome"] == "stopped" and not result["task_verified"] and len(calls) == 1


def test_same_failure_three_times_stops_at_best_checkpoint(tmp_path, monkeypatch):
    def change(workspace):
        app_files(workspace)
        (workspace / "index.html").write_text('<title>Try</title>' + str(time.monotonic()))
    worker, gateway, coding, manifest, calls, *_ = plan_then_implement(tmp_path, monkeypatch,
        [change], [failed(), failed(), failed()])
    result = run(worker, gateway, coding, manifest)
    assert len(calls) == 3 and result["outcome"] == "tests_failed" and not result["task_verified"]
    assert worker.last["iteration_receipts"][-1]["diagnosis"]["next_action"] == "stop_no_progress"
    assert _trusted_git(tmp_path, "rev-parse", "HEAD") == worker.last["iteration_receipts"][0]["commit_sha"]


def test_regression_rolls_back_checked_best_before_next_fix(tmp_path, monkeypatch):
    def best(workspace):
        app_files(workspace)
        (workspace / "index.html").write_text('<title>Best</title>')
    def worse(workspace):
        (workspace / "index.html").write_text('<title>Worse</title>')
    def fixed(workspace):
        assert (workspace / "index.html").read_text() == '<title>Best</title>'
        (workspace / "index.html").write_text('<title>Fixed</title>')
    worker, gateway, coding, manifest, *_ = plan_then_implement(tmp_path, monkeypatch,
        [best, worse, fixed, lambda _: None], [failed(count=1), failed(count=2), {"passed":True,"failures":[]}, {"passed":True,"failures":[]}])
    result = run(worker, gateway, coding, manifest)
    assert result["outcome"] == "pass"
    assert worker.last["iteration_receipts"][1]["diagnosis"]["next_action"] == "rollback_and_fix"


@pytest.mark.parametrize("path", [".codex/config.toml", "tests/AGENTS.md", "tests/.claude/settings.json"])
def test_preexisting_provider_rules_are_refused_before_checkpoint(tmp_path, monkeypatch, path):
    worker, gateway, coding, manifest, calls, *_ = setup(tmp_path, monkeypatch, [plan_files])
    target = tmp_path / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("unreviewed provider configuration")
    with pytest.raises(ValueError, match="coding_sandbox_denied"):
        run(worker, gateway, coding, manifest)
    assert not calls
    assert target.read_text() == "unreviewed provider configuration"



def test_trusted_git_accepts_same_filesystem_case_alias(tmp_path):
    workspace = tmp_path / "ProductWorkspace"
    workspace.mkdir()
    _trusted_git(workspace, "init", "--quiet", "--initial-branch=main")
    alternate = tmp_path / "productworkspace"
    if not alternate.is_dir():
        pytest.skip("This filesystem has case-sensitive directory names")
    assert alternate.samefile(workspace)
    assert _trusted_git(alternate, "status", "--porcelain=v1") == ""
    _trusted_git(alternate, "checkout", "-B", "supymem/case-alias")
    assert _trusted_git(workspace, "symbolic-ref", "--short", "HEAD") == "supymem/case-alias"
