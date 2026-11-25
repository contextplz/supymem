"""Founder-approved plan, task/check/fix and local checkpoint loop."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import time
from pathlib import Path

from core.artifact_policy import directory_fd, read_file, replace_file
from desktop_runtime import loop_checks, loop_diagnose, loop_prompts
from desktop_runtime.hosted import _is_local as is_local
from desktop_runtime.owned_coding import OwnedCoding, _SECRET, _trusted_git, redact_tool_event, workspace_files
from desktop_runtime.owned_terminal import read_workspace_file, restore_workspace_file, workspace_file_scope

GUARDED = {".git", ".claude", ".codex", ".cursor", ".vercel", ".vscode", ".mcp.json",
    ".gitattributes", ".gitignore", ".supymem-venture", "AGENTS.md", "CLAUDE.md", "CLAUDE.local.md", "artifacts"}
_git = _trusted_git


def _scan(workspace: Path) -> dict[str, bytes]:
    files = workspace_files(workspace)
    if any(_SECRET.search(data) for data in files.values()):
        raise ValueError("coding_secret_detected")
    denied = {".claude", ".codex", ".cursor", ".mcp.json", ".vscode", ".gitattributes",
        "agents.md", "claude.md", "claude.local.md"}
    if any(any(part.casefold() in denied for part in Path(name).parts)
            or len(Path(name).parts) > 1 and Path(name).name.casefold() == "package.json" for name in files):
        raise ValueError("coding_sandbox_denied")
    return files


def _changed_files(before: dict[str, bytes], after: dict[str, bytes]) -> list[dict]:
    return [{"path": name, "deleted": name not in after} for name in sorted(before.keys() | after.keys())
            if before.get(name) != after.get(name)]


def _remove(path: Path):
    with directory_fd(path.parent) as parent:
        if os.name == "nt":
            if path.is_symlink() or path.is_file():
                path.unlink()
            elif path.exists():
                shutil.rmtree(path)
            return
        try:
            mode = os.stat(path.name, dir_fd=parent, follow_symlinks=False).st_mode
        except FileNotFoundError:
            return
        if stat.S_ISDIR(mode):
            shutil.rmtree(path.name, dir_fd=parent)
        else:
            os.unlink(path.name, dir_fd=parent)


def _make_restore_directory(path: Path, *, exist_ok=False):
    with directory_fd(path.parent) as parent:
        if os.name == "nt":
            path.mkdir(mode=0o700, exist_ok=exist_ok)
            return
        try:
            os.mkdir(path.name, 0o700, dir_fd=parent)
        except FileExistsError:
            if not exist_ok or not stat.S_ISDIR(os.stat(path.name, dir_fd=parent, follow_symlinks=False).st_mode):
                raise


def _restore(workspace: Path, files: dict[str, bytes], *, git=False, preserve_source=False):
    """Restore trusted metadata, or the complete owned tree when replacing source."""
    guarded = {name.casefold() for name in GUARDED}
    protected = lambda part: part.startswith(".") or part.casefold() in guarded
    with directory_fd(workspace):
        pass
    for name in files:
        workspace_file_scope(workspace, name)
    if preserve_source:
        for name in files:
            if not any(protected(part) for part in Path(name).parts):
                continue
            parent = workspace
            for part in Path(name).parts[:-1]:
                if protected(part) and part.casefold() != "artifacts":
                    break
                parent = parent / part
                if parent.is_symlink() or parent.exists() and not parent.is_dir():
                    raise ValueError("coding_sandbox_denied")
        for base, directories, names in os.walk(workspace, followlinks=False):
            if Path(base) == workspace:
                directories[:] = [name for name in directories if name != ".tmp"]
                names = [name for name in names if name != ".tmp"]
            for name in [*directories, *names]:
                if protected(name) and name.casefold() != "artifacts" and (name != ".git" or git):
                    _remove(Path(base) / name)
                    if name in directories:
                        directories.remove(name)
    else:
        for path in workspace.iterdir():
            if path.name != ".tmp" and (path.name != ".git" or git):
                _remove(path)
    if git and any(name.startswith(".git/") for name in files):
        _make_restore_directory(workspace / ".git")
    for name, data in files.items():
        if preserve_source and not any(protected(part) for part in Path(name).parts):
            continue
        restore_workspace_file(workspace, name, data)
    if git and ".git/HEAD" in files:
        for name in ("objects", "refs"):
            _make_restore_directory(workspace / ".git" / name, exist_ok=True)


def _guard(workspace: Path, before: dict[str, bytes], *, preserve_source=False) -> bool:
    try:
        after = workspace_files(workspace, include_git=True)
    except (ValueError, OSError):
        _restore(workspace, before, git=True, preserve_source=preserve_source)
        return True
    guarded = {name.casefold() for name in GUARDED}
    protected = lambda name: any(part.casefold() in guarded or part.startswith(".") for part in Path(name).parts)
    changed = any(before.get(name) != after.get(name) for name in before.keys() | after.keys() if protected(name))
    changed = changed or any(len(Path(name).parts) > 1 and Path(name).name.casefold() == "package.json" for name in after)
    try:
        package = json.loads(after.get("package.json", b'{"type":"module"}'))
        changed = changed or package != {"type": "module"}
    except (ValueError, UnicodeDecodeError):
        changed = True
    if changed:
        # Restoring metadata happens before any trusted git command can read a
        # provider-written include, hook or gitfile.
        _restore(workspace, before, git=True, preserve_source=preserve_source)
    return changed


_ACTIVITY_WINDOW = 6


def _event(worker, text: str, workspace: Path | None, *, exception_frames=None):
    from desktop_runtime.cu_public_errors import safe_exception_details

    value = loop_diagnose.scrub(redact_tool_event(text), workspace, 280)
    details = safe_exception_details({"exception_frames": exception_frames})
    events = list(worker.last.get("events") or [])
    if value and (not events or events[-1]["text"] != value or events[-1].get("exception_frames") != details.get("exception_frames")):
        event = {"at": time.time(), "text": value, **details}
        events.append(event)
        limit = 2 * _ACTIVITY_WINDOW
        if len(events) > limit:
            events = events[:_ACTIVITY_WINDOW] + events[-_ACTIVITY_WINDOW:]
        previous_seq = worker.last.get("activity_seq", 0)
        count = worker.last.get("activity_count", previous_seq) + 1
        queued = list(worker.last.get("pending_activity") or [])
        seq = previous_seq + 1
        if len(queued) >= limit:
            # The head may have reached the server; only replace an event that was never sent.
            seq = queued[limit - 1]["seq"]
            queued = queued[:limit - 1]
        worker.progress(worker.last.get("state", "running_coding"), events=events, activity_seq=seq,
            activity_count=count, pending_activity=[*queued, {**event, "seq": seq}])
        post_events(worker, getattr(worker, "gateway", None))


def post_events(worker, gateway):
    send = getattr(gateway, "coding_event", None)
    if not callable(send):
        return
    queued = list(worker.last.get("pending_activity") or [])
    while queued:
        try:
            send(queued[0])
        except Exception:
            return
        queued.pop(0)
        worker.progress(worker.last.get("state", "running_coding"), pending_activity=list(queued))


def post_pending(worker, gateway):
    post_events(worker, gateway)
    queued = list(worker.last.get("iteration_receipts") or [])
    for item in queued:
        if item.get("posted"):
            continue
        try:
            gateway.iteration_receipt({key: value for key, value in item.items() if key != "posted"})
        except Exception:
            break
        item["posted"] = True
        worker.progress(worker.last["state"], iteration_receipts=queued)
    plan = worker.last.get("plan")
    if plan and not worker.last.get("plan_posted"):
        try:
            gateway.post_plan({key: value for key, value in plan.items() if key != "plan_digest"})
        except Exception:
            return
        worker.progress(worker.last["state"], plan_posted=True)


def _read_plan(workspace):
    blobs = []
    for name, instruction in (("review/spec.md", "the pages, API endpoints, data, exclusions and the required HTTP journey."),
            ("review/tasks.json", "a JSON task array: id, title, files, acceptance (test_file, test_name, behaviour).")):
        try:
            blobs.append(read_workspace_file(workspace, name, 16000))
        except (ValueError, OSError):
            raise ValueError("Write " + name + " with " + instruction) from None
    try:
        plan = loop_prompts.validate_plan(blobs[0].decode(), json.loads(blobs[1]))
        loop_checks.http_journey(plan)
        return plan
    except (ValueError, OSError, UnicodeDecodeError):
        raise ValueError("Repair review/tasks.json using the task and acceptance schema in this brief, "
            "and review/spec.md with /api/health and the required HTTP journey.") from None


def _checkpoint(workspace, text):
    _scan(workspace)
    _git(workspace, "add", "--all", "--force", "--", ".", ":(exclude).tmp", ":(exclude).reference")
    _git(workspace, "commit", "--quiet", "--allow-empty", "-m", text)
    return _git(workspace, "rev-parse", "HEAD")


def _quiescent(workspace, tracker, seconds=3):
    if not tracker.stop_all(timeout=.5):
        return False
    try:
        before = workspace_files(workspace, include_git=True)
    except (ValueError, OSError):
        return False
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        time.sleep(min(.1, max(0, until - time.monotonic())))
    try:
        return before == workspace_files(workspace, include_git=True)
    except (ValueError, OSError):
        return False


def _caps(verification, provider):
    for key, maximum in (("max_iterations", 10), ("max_wall_seconds", 5400), ("per_call_seconds", 1200)):
        if type(verification.get(key)) is not int or not 1 <= verification[key] <= maximum:
            raise ValueError("coding_loop_caps_invalid")
    if provider in {"codex", "cursor", "opencode"} and verification.get("max_turns") is not None:
        raise ValueError("coding_codex_turn_cap_unsupported")
    if provider == "claude" and (type(verification.get("max_turns")) is not int or not 1 <= verification["max_turns"] <= 60):
        raise ValueError("coding_max_turns_invalid")


def start_product_preview(worker, coding: OwnedCoding, build: dict) -> dict:
    if getattr(worker, "closed", False) is not True or worker.last.get("state") != "stopped":
        return {"status": "unavailable", "ready": False}
    existing = getattr(worker, "product_preview", None)
    if existing is not None and existing.build != build:
        existing.stop(status="expired")
        if existing.status().get("processes_stopped") is not True:
            return existing.status()
        existing = None
    if existing is None or existing.status().get("processes_stopped") is True:
        worker.product_preview = loop_checks.OwnedProductPreview(workspace=coding._workspace(), build=build,
            closed=worker.last.get("evidence") or {})
    receipt = worker.product_preview.start()
    try:
        worker.progress("stopped", product_preview=receipt)
    except Exception:
        worker.product_preview.stop(status="failed")
        raise
    return receipt


def _run_utility(worker, manifest, gateway, coding, deadline):
    descriptor = manifest["coding_loop_verification"]
    started = time.monotonic()
    deadline = min(deadline, started + descriptor["per_call_seconds"])
    if worker.cancelled.is_set() or not coding.authorize():
        raise ValueError("coding_cancelled_or_expired")
    workspace = coding._workspace()
    if not (workspace / ".git").exists() and not (workspace / ".git").is_symlink():
        _git(workspace, "init", "--quiet", "--initial-branch=main")
    OwnedCoding._env(workspace)
    _checkpoint(workspace, "Save your workspace before the requested change")
    before = coding._git_state(workspace)
    protected = workspace_files(workspace, include_git=True)
    coding.tracker.watch_workspace(workspace)
    coding.loop_mode, coding.max_turns = True, descriptor.get("max_turns")
    coding.line_callback = lambda line: _event(worker, text, workspace) if (
        text := loop_diagnose.stream_text(line, coding.provider, workspace)) else None
    worker.progress("running_coding", loop_phase="code")
    try:
        result = coding.run(manifest["instruction"], deadline)
    finally:
        if not _quiescent(workspace, coding.tracker):
            raise ValueError("coding_sandbox_denied")
        if _guard(workspace, protected):
            raise ValueError("coding_sandbox_denied")
        try:
            changes = coding._verify_changes(workspace, before, allow_empty=True)
            files = _scan(workspace)
        except (OSError, ValueError):
            _restore(workspace, protected, git=True)
            raise
    if worker.cancelled.is_set() or not coding.authorize():
        raise ValueError("coding_cancelled_or_expired")
    if result.get("outcome") != "pass":
        return {**result, "task_verified": False}
    worker.progress("running_coding", loop_phase="check")
    if not changes["changed_files"]:
        raise ValueError("coding_change_set_invalid")
    commit = _checkpoint(workspace, "Save your checked changes")
    hashes = {name: hashlib.sha256(body).hexdigest() for name, body in files.items()}
    digest = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()
    summary = "Your files passed the workspace checks. Open the result folder to review the changes and your tool's test notes."
    receipt = {"execution_id": gateway.execution_id, "index": 0, "phase": "code", "outcome": "pass",
        "summary": summary, "commit_sha": commit, "file_manifest_sha256": digest,
        "caps_used": {"iterations": 1, "wall_seconds": min(descriptor["max_wall_seconds"], int(time.monotonic() - started))},
        "evidence_sha256": digest}
    worker.progress("running_coding", iteration_receipts=[receipt], changed_files=changes["changed_files"], message=summary)
    post_pending(worker, gateway)
    return {**changes, "outcome": "pass", "task_verified": True, "source_commit": commit, "file_manifest_sha256": digest}


def run_coding_loop(worker, manifest: dict, gateway, coding: OwnedCoding, deadline: float) -> dict:
    worker.gateway = gateway
    verification = manifest["coding_loop_verification"]
    _caps(verification, coding.provider)
    if verification.get("profile") == "static_landing_v1":
        from desktop_runtime.landing_loop import run
        return run(worker, manifest, gateway, coding, deadline)
    if verification.get("profile") == "workspace_utility_v1":
        return _run_utility(worker, manifest, gateway, coding, deadline)
    phase = verification.get("phase", "plan")
    if phase not in {"plan", "implement"}:
        raise ValueError("coding_loop_phase_invalid")
    if worker.cancelled.is_set() or time.monotonic() >= deadline or not coding.authorize():
        raise ValueError("coding_cancelled_or_expired")
    workspace = coding._workspace()
    if not (workspace / ".git").exists() and not (workspace / ".git").is_symlink():
        _git(workspace, "init", "--quiet", "--initial-branch=main")
    else:
        _git(workspace, "status", "--porcelain=v1")
    OwnedCoding._env(workspace)
    if hasattr(coding.tracker, "watch_workspace"):
        coding.tracker.watch_workspace(workspace)
    initial_files = _scan(workspace)
    saved = getattr(worker, "loop_saved", {}) or {}
    snapshot = saved.get("task_snapshot") or {}
    approved = snapshot.get("approved_plan") or {}
    if phase == "implement":
        if is_local() and saved.get("retry_of") and approved and _git(workspace, "rev-parse", "HEAD") != approved.get("commit_sha"):
            previous_branch = "supymem/loop-" + hashlib.sha256(saved["retry_of"].encode()).hexdigest()[:8]
            if (_git(workspace, "rev-parse", "--abbrev-ref", "HEAD") != previous_branch
                    or any(line != "!! .tmp/" for line in _git(workspace, "status", "--porcelain=v1",
                        "--untracked-files=all", "--ignored=matching").splitlines())):
                raise ValueError("coding_plan_changed")
            _git(workspace, "checkout", "--detach", approved["commit_sha"])
            initial_files = _scan(workspace)
        plan = _read_plan(workspace)
        if (not approved or plan["plan_digest"] != verification.get("plan_digest")
                or plan["plan_digest"] != approved.get("plan_digest")
                or _git(workspace, "rev-parse", "HEAD") != approved.get("commit_sha")
                or any(line != "!! .tmp/" for line in _git(workspace, "status", "--porcelain=v1",
                    "--untracked-files=all", "--ignored=matching").splitlines())
                or loop_checks.source_manifest(workspace)["file_manifest_sha256"] != approved.get("file_manifest_sha256")):
            raise ValueError("coding_plan_changed")
    else:
        if verification.get("plan_digest") is not None:
            raise ValueError("coding_loop_phase_invalid")
        plan = None
    if phase == "implement":
        package = workspace / "package.json"
        if not package.exists() and not package.is_symlink():
            if not is_local():
                replace_file(workspace, package.name, b'{"type":"module"}\n')
        elif json.loads(read_workspace_file(workspace, package.name, 16000)) != {"type": "module"}:
            raise ValueError("coding_package_policy_invalid")
    order_suffix = hashlib.sha256(str(gateway.order_id).encode()).hexdigest()[:8]
    _git(workspace, "checkout", "-B", "supymem/loop-" + order_suffix)
    best_sha = _checkpoint(workspace, "Supymem loop baseline")
    best_files = _scan(workspace)
    coding.loop_mode, coding.max_turns = True, verification.get("max_turns")
    coding.line_callback = lambda line: _event(worker, value, workspace) if (
        value := loop_diagnose.stream_text(line, coding.provider, workspace)) else None
    started = time.monotonic()
    deadline = min(deadline, started + verification["max_wall_seconds"])
    history, diagnosis, best_count = [], None, None
    task_index, calls, outcome, artifact_verified, plan_packet = 0, 0, "stopped", True, None
    completed, final_quiescence_done = False, False
    soft_stop = getattr(worker, "stop_after_iteration", getattr(worker, "soft_stop_requested", None))
    tries = min(3, verification["max_iterations"]) if phase == "plan" else verification["max_iterations"]
    for index in range(tries):
        if worker.cancelled.is_set() or time.monotonic() >= deadline or (index and soft_stop and soft_stop.is_set()):
            outcome = "timeout" if time.monotonic() >= deadline else "stopped"
            break
        if not coding.authorize():
            outcome = "stopped"
            break
        final = phase == "implement" and task_index >= len(plan["tasks"])
        task = None if phase == "plan" else ({"id": "final", "title": "Check the complete reviewed app",
            "files": sorted({name for item in plan["tasks"] for name in item["files"]}),
            "acceptance": [check for item in plan["tasks"] for check in item["acceptance"]]}
            if final else plan["tasks"][task_index])
        worker.progress("running_coding", loop_index=index, loop_phase="plan" if phase == "plan" else "code")
        _event(worker, "Planning your app" if phase == "plan" else f"Coding your app (try {index + 1} of {tries})", workspace)
        before = _scan(workspace)
        metadata_before = workspace_files(workspace, include_git=True)
        instruction = loop_prompts.plan_prompt(manifest["instruction"], {"product_scope": snapshot.get("product_scope") or {}, "confirmed_idea": snapshot.get("confirmed_idea") or snapshot.get("idea_snapshot") or {}, "idea_summary": snapshot.get("idea_summary") or "", "execution_plan": snapshot.get("execution_plan") or {}}, repair=diagnosis["failures"][0]["message"] if diagnosis and diagnosis.get("failures") else False) if phase == "plan" else loop_prompts.task_prompt(task, plan, diagnosis, final=final)
        if phase == "plan":
            instruction += ("\nIn spec.md include exactly one fenced http-journey JSON array for an independent HTTP check. "
                "Use 1 to 6 steps with method (GET or POST), path (a local /api/ endpoint, optional query), "
                "status (2xx), expected_json (the exact response object or array), and optional JSON body for POST. "
                "Include a meaningful product API result, not only health. These checks stay within the authorized build request.")
        cli, checks, changed_names = {}, {"passed": False, "failures": []}, set()
        checked, commit_sha, current_manifest = False, None, None
        call_deadline = min(deadline, time.monotonic() + verification["per_call_seconds"])
        try:
            calls += 1
            cli = coding.run(instruction, call_deadline)
        except Exception as error:
            code = str(error)
            cli = {"outcome": "auth_required" if code == "coding_auth_required" else
                "secret_detected" if code == "coding_secret_detected" else
                "stopped" if worker.cancelled.is_set() else "timeout" if time.monotonic() >= call_deadline else "cli_error"}
        if not coding.tracker.stop_all(timeout=.5):
            cli["outcome"] = "stopped_unclean"
        try:
            if _guard(workspace, metadata_before):
                cli["outcome"] = "sandbox_denied"
            after = _scan(workspace)
            changed_names = {name for name in before.keys() | after.keys() if before.get(name) != after.get(name)}
            allowed = {"review/spec.md", "review/tasks.json"} if phase == "plan" else set(task["files"])
            if any(name not in allowed and not (phase == "implement" and name.startswith("tests/")) for name in changed_names):
                _restore(workspace, before)
                cli["outcome"] = "sandbox_denied"
            if phase == "implement" and _read_plan(workspace)["plan_digest"] != plan["plan_digest"]:
                cli["outcome"] = "sandbox_denied"
            if getattr(coding, "pagebuilder_reference", None) is not None:
                from desktop_runtime.pagebuilder_reference import verify_staged
                verify_staged(workspace, *coding.pagebuilder_reference)
            if phase == "plan" and cli.get("outcome") in {"pass", "cli_error"}:
                if is_local() and allowed.isdisjoint(after):
                    cli.update(outcome="cli_error", failure_message=cli.get("failure_message") or
                        "Your coding tool stopped without saving a plan. Open Build activity and choose Retry to run it again.")
                else:
                    try:
                        plan = _read_plan(workspace)
                        checks = {"passed": True, "failures": []}
                    except ValueError as error:
                        checks = {"passed": False, "failures": [{"kind": "plan", "message": str(error)}]}
            elif phase == "implement" and cli.get("outcome") == "pass":
                _event(worker, f"Running your tests (try {index + 1} of {tries})", workspace)
                checked_tasks = plan["tasks"] if final else plan["tasks"][:task_index + 1]
                check_plan = plan if final else {**plan, "spec": " ".join("/" + name[:-3] for item in checked_tasks for name in item["files"] if name.startswith("api/") and name.endswith(".js"))}
                checks = loop_checks.run(verification["check_recipe"], workspace, tracker=coding.tracker,
                    authorize=coding.authorize, cancelled=worker.cancelled.is_set, deadline=deadline,
                    plan=check_plan, tasks=checked_tasks, partial=not final)
                checked = True
            # Tests are untrusted programs too. Stop them and recheck source,
            # approval bytes, protected metadata and task boundaries before commit.
            if _guard(workspace, metadata_before):
                cli["outcome"] = "sandbox_denied"
            after = _scan(workspace)
            changed_names = {name for name in before.keys() | after.keys() if before.get(name) != after.get(name)}
            if any(name not in allowed and not (phase == "implement" and name.startswith("tests/")) for name in changed_names):
                cli["outcome"] = "sandbox_denied"
            delta = OwnedCoding._verify_changes(workspace, {name: hashlib.sha256(data).hexdigest() for name, data in before.items()}, allow_empty=True)
            diagnosis = loop_diagnose.diagnose(checks, cli, delta, history)
            if phase == "plan" and diagnosis["outcome"] == "pass":
                diagnosis.update(outcome="plan_ready", next_action="stop_done", summary="Your build plan is saved. Open your venture build result to view it.")
            elif phase == "plan" and cli.get("outcome") in {"pass", "cli_error"} and checks["failures"]:
                diagnosis.update(outcome="plan_invalid", next_action="fix" if index + 1 < tries else "stop_error",
                    failures=checks["failures"], summary=(
                        "Your plan is incomplete. Your tool is repairing review/spec.md and review/tasks.json now."
                        if index + 1 < tries else
                        "Your draft is saved. Open Build activity, then ask Charlie to finish review/spec.md and review/tasks.json."))
            elif phase == "implement" and diagnosis["outcome"] == "pass" and not final:
                diagnosis.update(outcome="task_passed", next_action="next_task", summary="This task passed its checks. Continuing with the next task.")
            if cli.get("outcome") != "pass" and (phase != "plan" or cli.get("outcome") in {
                    "sandbox_denied", "secret_detected", "stopped_unclean"}):
                _restore(workspace, best_files)
            current_manifest = loop_checks.source_manifest(workspace)["file_manifest_sha256"]
            commit_sha = _checkpoint(workspace, f"Supymem try {index + 1}: {diagnosis['outcome']}")
            artifact_verified = True
            if phase == "plan":
                best_sha, best_files = commit_sha, _scan(workspace)
        except Exception as error:
            code = str(error)
            diagnosis = loop_diagnose.diagnose(checks, {"outcome": "secret_detected" if code == "coding_secret_detected" else "sandbox_denied"}, {}, history)
            _restore(workspace, best_files)
            artifact_verified = False
        if diagnosis["outcome"] in {"pass", "plan_ready"}:
            final_quiescence_done = True
            if not _quiescent(workspace, coding.tracker):
                coding.tracker.stop_all(timeout=.5)
                _restore(workspace, best_files)
                _git(workspace, "reset", "--hard", best_sha)
                diagnosis = loop_diagnose.diagnose(checks, {"outcome": "stopped_unclean"}, {}, history)
                artifact_verified, current_manifest = False, None
        # Stop can arrive while final process quiescence is being verified.
        # Observe it before publishing success or promoting this checkpoint.
        current = False
        if diagnosis["outcome"] in {"pass", "plan_ready", "task_passed"}:
            try:
                current = not worker.cancelled.is_set() and coding.authorize()
            except Exception:
                current = False
        if diagnosis["outcome"] in {"pass", "plan_ready", "task_passed"} and not current:
            cli["outcome"] = "stopped"
            diagnosis = loop_diagnose.diagnose(checks, cli, {}, history)
            current_manifest = None
        outcome = diagnosis["outcome"]
        completed = final and outcome == "pass"
        evidence = {key: diagnosis[key] for key in ("signature", "next_action", "failures")}
        if completed and "tap_summary" in checks:
            evidence["tap_summary"] = checks["tap_summary"]
        # Forced Stop cleanup can outlive the execution budget. Preserve that
        # actual elapsed time without spending beyond the reviewed CLI budget.
        evidence["elapsed_wall_seconds"] = int(time.monotonic() - started)
        if "reset_at" in diagnosis:
            evidence["reset_at"] = diagnosis["reset_at"]
        receipt = {"index": index, "phase": "plan" if phase == "plan" else "code", "outcome": outcome,
            "summary": diagnosis["summary"], "diagnosis": evidence, "commit_sha": commit_sha,
            "caps_used": {"iterations": index + 1, "wall_seconds": min(verification["max_wall_seconds"], int(time.monotonic() - started))},
            "evidence_sha256": hashlib.sha256(json.dumps(evidence, sort_keys=True).encode()).hexdigest(), "posted": False}
        if current_manifest and (outcome == "plan_ready" or outcome == "pass" and final):
            receipt["file_manifest_sha256"] = current_manifest
        worker.progress(worker.last["state"], changed_files=_changed_files(initial_files, _scan(workspace)),
            iteration_receipts=[*(worker.last.get("iteration_receipts") or []), receipt])
        _event(worker, diagnosis["summary"], workspace)
        if commit_sha:
            _event(worker, "Saved a checkpoint", workspace)
        if phase == "plan" and outcome == "plan_ready":
            best_sha, best_files = commit_sha, _scan(workspace)
            plan_packet = {**plan, "commit_sha": commit_sha, "file_manifest_sha256": current_manifest}
            worker.progress(worker.last["state"], plan_posted=False, plan=plan_packet)
        elif checked and cli.get("outcome") == "pass" and artifact_verified and commit_sha:
            count = len(checks.get("failures", []))
            if best_count is None or count < best_count:
                best_count, best_sha, best_files = count, commit_sha, _scan(workspace)
            history.append({"signature": diagnosis["signature"], "changed": bool(changed_names),
                "failure_count": count, "checked": True, "commit_sha": commit_sha})
        post_pending(worker, gateway)
        if diagnosis["next_action"] == "next_task":
            task_index += 1
            history, diagnosis, best_count = [], None, None
            best_sha, best_files = commit_sha, _scan(workspace)
        elif diagnosis["next_action"] == "rollback_and_fix":
            _restore(workspace, best_files)
            _git(workspace, "reset", "--hard", best_sha)
        if diagnosis and (diagnosis["next_action"].startswith("stop_") or diagnosis["next_action"] == "pause_quota"):
            break
    if phase == "implement" and outcome in {"pass", "task_passed"} and not completed:
        outcome = "stopped"
        _event(worker, "We reached the approved try limit. Your last checked checkpoint is saved; the full app is not finished.", workspace)
    if outcome not in {"pass", "plan_ready"}:
        _restore(workspace, best_files)
        _git(workspace, "reset", "--hard", best_sha)
    if not final_quiescence_done and not _quiescent(workspace, coding.tracker):
        coding.tracker.stop_all(timeout=.5)
        _restore(workspace, best_files)
        _git(workspace, "reset", "--hard", best_sha)
        outcome, artifact_verified = "stopped_unclean", False
        _event(worker, "We stopped because a background process kept changing your project. Your last safe checkpoint was restored.", workspace)
    changed_files = _changed_files(initial_files, _scan(workspace))
    worker.progress(worker.last["state"], changed_files=changed_files)
    result = {"operation": "workspace_loop_v1", "provider": coding.provider, "iterations": calls,
        "changed_files": changed_files,
        "best_sha": best_sha, "source_commit": best_sha, "final_outcome": outcome, "outcome": outcome,
        "artifact_verified": artifact_verified, "task_verified": outcome in {"pass", "plan_ready"} and artifact_verified,
        "task_acceptance_verified": outcome == "pass" and artifact_verified,
        "workspace_marker_verified": read_file(workspace, ".supymem-venture").decode().strip() == coding.venture_id}
    if plan_packet:
        result["plan"] = plan_packet
    if outcome == "pass":
        result["file_manifest_sha256"] = loop_checks.source_manifest(workspace)["file_manifest_sha256"]
        result.update({key: checks.get(key, "unavailable" if key.endswith("status") else [])
            for key in ("availability_status", "functional_status", "http_journey")})
        result["product_preview"] = {"status": "unavailable", "ready": False}
    return result
