"""Live subprocess supervision for BYO coding agents (doc 08 §6 / doc 91 §4)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable, Awaitable

import psutil

from coding_agents.env import child_env, hosted_execution, is_local
from coding_agents.protocol import AgentEvent, AgentEventKind, CodingRequest, CodingResult
from desktop_runtime.owned_processes import OwnedProcesses
from desktop_runtime.owned_coding import workspace_files


async def _run_owned(*, adapter, req, transcript_path, on_event, kill_switch):
    from desktop_runtime.agent_status import _status
    from desktop_runtime.owned_coding import OwnedCoding, _PROVIDER_BINARIES

    provider = adapter.name
    if provider not in _PROVIDER_BINARIES:
        message = "Open Settings > Keys and connections > Coding tools and connect your coding tool, then ask to build again."
        return CodingResult(ok=False, error=message)
    root = Path.home() / "Documents/Supymem/ventures"
    workspace = req.workdir.absolute()
    marker = workspace / ".supymem-venture"
    if (workspace.parent != root or workspace.resolve() != workspace or marker.is_symlink()
            or not marker.is_file() or marker.stat().st_size > 161):
        return CodingResult(ok=False, error="Open your venture and ask to build again so your app creates an isolated result folder under Documents > Supymem > ventures.")
    venture_id = marker.read_text().strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", venture_id):
        return CodingResult(ok=False, error="Your result folder's ownership could not be verified. Open your venture and start a new build folder.")
    if not req.allow_write or req.allow_network or req.resume_session_id or req.model:
        return CodingResult(ok=False, error="Start a new build from your venture using your connected tool's default model. Your build stays offline, inside its result folder, and does not reuse an earlier tool session.")
    status = await asyncio.to_thread(_status, provider)
    if status.get("subscription_login") is not True or status.get("available") is not True:
        login = {"codex": "codex login", "claude": "claude auth login",
                 "cursor": "cursor-agent login", "opencode": "opencode auth login"}[provider]
        setup = "Install your coding tool, then sign in" if status.get("installed") is False else "Sign in"
        return CodingResult(ok=False, error=f"{setup} with your own subscription using {login} in Terminal, then open Settings > Keys and connections > Coding tools and check your connection again. API-key login is not used for this build.")

    def permitted():
        try:
            from app.core.local_capabilities import enabled
            return enabled("build_with_my_tool")
        except (ImportError, KeyError):
            try:
                from app.core.local_secrets import read_settings
                on = read_settings("capabilities")["values"].get("build_with_my_tool", True) is True
            except ImportError:
                on = True
            return on and status["available"] is True

    if not permitted():
        try:
            from app.core.local_capabilities import blocked_message
            message = blocked_message("build_with_my_tool")
        except (ImportError, KeyError):
            message = "Open Settings > Your capabilities and switch on Build with my tool, then ask to build again."
        return CodingResult(ok=False, error=message)
    tracker, stopped = OwnedProcesses(), threading.Event()
    coding = OwnedCoding(venture_id=venture_id, workspace_slug=workspace.name, provider=provider,
        tracker=tracker, authorize=permitted, cancelled=stopped.is_set)
    coding.loop_mode = True
    before = OwnedCoding._git_state(workspace) if (workspace / ".git").exists() else {
        name: hashlib.sha256(data).hexdigest() for name, data in workspace_files(workspace).items()}
    events, queue = [], asyncio.Queue()
    loop = asyncio.get_running_loop()

    def receive(line):
        try:
            raw = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            return
        event = adapter.parse_event(raw) if isinstance(raw, dict) else None
        if event is not None:
            loop.call_soon_threadsafe(queue.put_nowait, event)

    coding.line_callback = receive

    async def stream():
        while (event := await queue.get()) is not None:
            events.append(event)
            if len(events) > 500:
                del events[0]
            if transcript_path:
                transcript_path.parent.mkdir(parents=True, exist_ok=True)
                with transcript_path.open("a", encoding="utf-8") as handle:
                    handle.write(event.model_dump_json() + "\n")
            if on_event:
                await on_event(event)

    async def watch():
        while not stopped.is_set():
            if kill_switch and kill_switch():
                stopped.set()
                return
            await asyncio.sleep(.05)

    task = asyncio.create_task(asyncio.to_thread(coding.run, req.prompt, time.monotonic() + req.timeout_s))
    stream_task, watch_task = asyncio.create_task(stream()), asyncio.create_task(watch())
    try:
        done, _pending = await asyncio.wait([task, stream_task], return_when=asyncio.FIRST_COMPLETED)
        if stream_task in done:
            await stream_task
        receipt = await asyncio.shield(task)
        queue.put_nowait(None)
        await stream_task
        delta = OwnedCoding._verify_changes(workspace, before, allow_empty=True)
        changed = [item["path"] for item in delta["changed_files"] if transcript_path is None
            or (workspace / item["path"]).resolve() != transcript_path.resolve()]
        ok = receipt["outcome"] == "pass" and bool(changed)
        return CodingResult(ok=ok, changed_files=changed,
            events=events, write_flag_used=True, error=None if ok else receipt.get("failure_message") or
            "Your coding tool stopped. Review your build's activity and result folder, then check your tool's sign-in and usage in Settings > Keys and connections > Coding tools before trying again.")
    except (OSError, ValueError):
        return CodingResult(ok=False, events=events, error="Your coding tool stopped. Review your build's activity and result folder, then check your connection in Settings > Keys and connections > Coding tools before trying again.")
    finally:
        stopped.set()
        await asyncio.gather(asyncio.shield(task), return_exceptions=True)
        queue.put_nowait(None)
        await asyncio.gather(stream_task, watch_task, return_exceptions=True)
        await asyncio.to_thread(tracker.stop_all)


async def run_live(
    *,
    adapter: Any,
    req: CodingRequest,
    transcript_path: Path | None = None,
    on_event: Callable[[AgentEvent], Awaitable[None]] | None = None,
    kill_switch: Callable[[], bool] | None = None,
) -> CodingResult:
    if hosted_execution():
        return CodingResult(ok=False, error="cloud_forbids_byo", argv=[])
    if is_local() and getattr(adapter, "name", None) in {"codex", "claude", "cursor", "opencode"}:
        try:
            return await _run_owned(adapter=adapter, req=req, transcript_path=transcript_path,
                on_event=on_event, kill_switch=kill_switch)
        except (OSError, ValueError):
            return CodingResult(ok=False, error="Open your build's result folder to review any changes, then check your tool in Settings > Keys and connections > Coding tools before trying again.")
    if getattr(adapter, "name", None) in {"cursor", "opencode"}:
        from desktop_runtime.agent_status import _status

        status = await asyncio.to_thread(_status, adapter.name)
        if not status["available"]:
            command = "cursor-agent login" if adapter.name == "cursor" else "opencode auth login"
            version = "Cursor Agent 2026.04.17 or newer" if adapter.name == "cursor" else "OpenCode 1.18.23 or newer"
            remove_keys = " First run opencode auth logout in Terminal to remove any API-key connections." if adapter.name == "opencode" else ""
            return CodingResult(ok=False, error=f"Install or update to {version}.{remove_keys} In Terminal, run {command} and sign in with your own account using its subscription login. Then open Settings > Keys and connections > Coding tools and check your connection again.",
                argv=adapter.build_argv(req))
    argv = adapter.build_argv(req)
    write_flag_used = getattr(adapter, "write_flag", "") in argv
    if req.allow_write and not write_flag_used:
        return CodingResult(
            ok=False,
            error="missing_write_flag",
            argv=argv,
            write_flag_used=False,
        )

    env = child_env()
    # Ensure workdir exists
    req.workdir.mkdir(parents=True, exist_ok=True)
    if transcript_path:
        transcript_path.parent.mkdir(parents=True, exist_ok=True)
    def snapshot():
        return {name: data for name, data in workspace_files(req.workdir).items()
                if transcript_path is None or (req.workdir / name).resolve() != transcript_path.resolve()}
    before = snapshot()

    tracker = OwnedProcesses()
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=str(req.workdir),
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=os.name != "nt",
        limit=8 * 1024 * 1024,
    )
    assert proc.stdout is not None
    try:
        tracker.register(psutil.Process(proc.pid))
    except psutil.NoSuchProcess:
        tracker.capture()

    events: list[AgentEvent] = []
    changed: list[str] = []
    session_id: str | None = None
    timed_out = False
    killed = False
    output_bytes = 0
    terminal_failed = False

    async def _kill_tree() -> None:
        await asyncio.to_thread(tracker.stop_all)

    async def _watch_timeout() -> None:
        nonlocal timed_out
        await asyncio.sleep(req.timeout_s)
        timed_out = True
        await _kill_tree()

    async def _watch_kill_switch() -> None:
        nonlocal killed
        if kill_switch is None:
            return
        while proc.returncode is None:
            if kill_switch():
                killed = True
                await _kill_tree()
                return
            await asyncio.sleep(.05)

    timeout_task = asyncio.create_task(_watch_timeout())
    ks_task = asyncio.create_task(_watch_kill_switch())

    try:
        while True:
            line_b = await proc.stdout.readline()
            if not line_b:
                break
            output_bytes += len(line_b)
            if output_bytes > 4 * 1024 * 1024:
                raise ValueError("coding_output_bounds")
            line = line_b.decode("utf-8", errors="replace").rstrip("\n")
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue
            else:
                ev = adapter.parse_event(raw) if isinstance(raw, dict) else None
            if ev is None:
                continue
            terminal_failed = terminal_failed or (ev.kind == AgentEventKind.FAILED
                and (raw.get("type") or raw.get("kind")) in {"result", "error", "failed"})
            if transcript_path:
                with transcript_path.open("a", encoding="utf-8") as fh:
                    fh.write(ev.model_dump_json() + "\n")
            events.append(ev)
            if len(events) > 500:
                del events[0]
            if ev.session_id:
                session_id = ev.session_id
            if ev.kind == AgentEventKind.FILE_CHANGED and ev.path:
                changed.append(ev.path)
            if on_event:
                await on_event(ev)
        await proc.wait()
    finally:
        timeout_task.cancel()
        ks_task.cancel()
        if proc.returncode is None:
            await _kill_tree()
        else:
            tracker.close()
        await asyncio.gather(timeout_task, ks_task, return_exceptions=True)

    after = snapshot()
    missing = object()
    changed = sorted(name for name in before.keys() | after.keys()
                     if before.get(name, missing) != after.get(name, missing))

    if timed_out:
        return CodingResult(
            ok=False,
            error="timed_out",
            session_id=session_id,
            changed_files=changed,
            events=events,
            write_flag_used=write_flag_used,
            argv=argv,
        )
    if killed:
        return CodingResult(
            ok=False,
            error="kill_switch",
            session_id=session_id,
            changed_files=changed,
            events=events,
            write_flag_used=write_flag_used,
            argv=argv,
        )

    ok = proc.returncode == 0 and not terminal_failed
    if req.allow_write and not changed and not terminal_failed:
        return CodingResult(
            ok=False,
            error="coding_agent_no_op",
            session_id=session_id,
            changed_files=changed,
            events=events,
            write_flag_used=write_flag_used,
            argv=argv,
        )
    return CodingResult(
        ok=ok,
        session_id=session_id,
        changed_files=changed,
        events=events,
        error=("Your coding tool reported a problem. Open Settings > Keys and connections > Coding tools to check your connection, then open your build activity and try again."
               if terminal_failed else None if ok else f"exit_{proc.returncode}"),
        write_flag_used=write_flag_used,
        argv=argv,
    )
