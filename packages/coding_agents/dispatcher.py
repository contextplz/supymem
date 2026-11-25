"""Dispatch Work Orders to local / cloud / in-house adapters (docs 91 + 95)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from coding_agents import load_adapter
from coding_agents.consent import require_consent
from coding_agents.detect import detect
from coding_agents.env import child_env, hosted_execution, is_local
from coding_agents.gstack_charter import wrap_prompt
from coding_agents.protocol import CodingRequest, CodingResult
from coding_agents.select import CONNECT_TOOL_MESSAGE, FailureClass, Selection, select


def _cloud_keys_present() -> dict[str, bool]:
    if is_local():
        return {}
    return {
        "CURSOR_API_KEY": bool(os.environ.get("CURSOR_API_KEY", "").strip()),
        "CODEX_API_KEY": bool(os.environ.get("CODEX_API_KEY", "").strip()),
        "OPENAI_API_KEY": bool(os.environ.get("OPENAI_API_KEY", "").strip()),
        "ANTHROPIC_API_KEY": bool(os.environ.get("ANTHROPIC_API_KEY", "").strip()),
        "CLAUDE_ROUTINE_TOKEN": bool(os.environ.get("CLAUDE_ROUTINE_TOKEN", "").strip()),
    }


def create_worktree(repo: Path, branch: str) -> Path:
    """Create a git worktree for an isolated Work Order. Falls back to a plain dir."""
    repo = repo.resolve()
    repo.mkdir(parents=True, exist_ok=True)
    worktrees = repo.parent / "worktrees"
    worktrees.mkdir(parents=True, exist_ok=True)
    safe = branch.replace("/", "_")
    dest = worktrees / safe
    if dest.exists():
        return dest

    git_dir = repo / ".git"
    if not git_dir.exists():
        subprocess.run(["git", "init"], cwd=repo, env=child_env(), check=False, capture_output=True)
        subprocess.run(["git", "add", "-A"], cwd=repo, env=child_env(), check=False, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "init", "--allow-empty"],
            cwd=repo,
            env=child_env(),
            check=False,
            capture_output=True,
        )

    r = subprocess.run(
        ["git", "worktree", "add", "-b", branch, str(dest), "HEAD"],
        cwd=repo,
        env=child_env(),
        capture_output=True,
        text=True,
        check=False,
    )
    if r.returncode != 0:
        dest.mkdir(parents=True, exist_ok=True)
    return dest


async def dispatch_request(
    req: CodingRequest,
    *,
    prefs: list[str] | None = None,
    kind: str = "code",
    mode: str = "auto",
    bridge_connected: bool = True,
    allow_server_side_byo: bool = False,
    transcript_dir: Path | None = None,
    failed: list[tuple[str, FailureClass]] | None = None,
) -> CodingResult:
    """Select an adapter and run. Never runs BYO CLIs on hosted infra (doc 08 §2 / 95)."""
    if not is_local() and os.environ.get("LLM_OFFLINE", "").strip().lower() in {"1", "true", "yes", "on"}:
        return CodingResult(ok=False, error="llm_offline", argv=[])
    failed = list(failed or [])
    local = is_local()
    hosted = hosted_execution()
    req = req.model_copy(update={"prompt": wrap_prompt(req.prompt, kind=kind)})

    while True:
        choice = select(
            kind=kind,
            prefs=prefs,
            detected=[] if hosted else detect(),
            mode=mode,  # type: ignore[arg-type]
            bridge_connected=bridge_connected,
            cloud_keys=_cloud_keys_present(),
            inhouse_hosted_available=True,
            failed=failed,
        )
        if choice is None:
            return CodingResult(ok=False, error=CONNECT_TOOL_MESSAGE if local else "no_adapter_available", argv=[])
        if local and choice.adapter_name not in {"codex", "claude", "cursor", "opencode"}:
            return CodingResult(ok=False, error=CONNECT_TOOL_MESSAGE)

        # Hard rule: never spawn BYO CLIs on Company-OS cloud servers.
        if hosted and choice.adapter_name in {
            "cursor",
            "claude",
            "codex",
            "opencode",
        }:
            # Keep the old argument for caller compatibility; it cannot grant
            # permission to run a subscription CLI on a platform server.
            failed.append((choice.adapter_name, "infra"))
            continue

        try:
            if not local:
                require_consent(choice.adapter_name)
        except PermissionError:
            # Unconsented BYO CLIs must not stall the company; skip to the next adapter.
            failed.append((choice.adapter_name, "infra"))
            continue

        try:
            result = await _run_selection(choice, req, transcript_dir=transcript_dir)
        except FileNotFoundError:
            if local:
                return CodingResult(ok=False, error="Open Settings > Keys and connections > Coding tools, install your selected tool and check it again, then ask in your venture chat to build again.")
            failed.append((choice.adapter_name, "infra"))
            continue
        except Exception as exc:  # noqa: BLE001
            if local:
                return CodingResult(ok=False, error="Your coding tool stopped; review any changes in its result folder in Builds, then reconnect it in Settings > Keys and connections > Coding tools before asking in your venture chat to try again.")
            # Treat unexpected startup errors as infra fallthrough.
            failed.append((choice.adapter_name, "infra"))
            if "task" in str(exc).lower():
                return CodingResult(ok=False, error=str(exc), argv=[])
            continue

        if result.ok:
            return result
        if local:
            from desktop_runtime.owned_coding import coding_connection_message

            sandbox_message = coding_connection_message("coding_sandbox_denied", choice.adapter_name)
            if sandbox_message and result.error == sandbox_message:
                return result
            if result.error == "auth_failure":
                message = "Your coding tool could not sign in; open Settings > Keys and connections > Coding tools, verify your subscription login, then ask in your venture chat to try again."
            elif result.error == "binary_missing":
                message = "Your coding tool was not found; open Settings > Keys and connections > Coding tools, install and connect it, then ask in your venture chat to try again."
            elif result.error == "cancelled":
                message = "Your coding task was stopped; review any changes in its result folder in Builds, then ask in your venture chat to continue only when you are ready."
            else:
                message = "Your coding tool did not finish the requested changes; review its output and any changed files in Builds, then ask in your venture chat to try again."
            return result.model_copy(update={"error": message})

        # Infra-ish errors fall through; task errors stop.
        infra_errors = {
            "live_not_enabled",
            "binary_missing",
            "auth_failure",
            "timed_out",
            "no_adapter_available",
            "infra",
            "llm_offline",
            "llm_key_missing",
        }
        if result.error in infra_errors or (result.error or "").startswith("exit_"):
            # exit codes from missing binary / auth often surface as nonzero exit —
            # only fall through when nothing was written (no task progress).
            if not result.changed_files:
                failed.append((choice.adapter_name, "infra"))
                continue
        return result


async def _run_selection(
    choice: Selection,
    req: CodingRequest,
    *,
    transcript_dir: Path | None,
) -> CodingResult:
    name = choice.adapter_name
    if is_local() and name not in {"codex", "claude", "cursor", "opencode"}:
        return CodingResult(ok=False, error=CONNECT_TOOL_MESSAGE)
    adapter = load_adapter(name if name != "inhouse" else "inhouse")

    # Cloud adapters
    if name.endswith("_cloud") or name == "claude_routine":
        return await adapter.run(req)

    # Local live path for BYO — enable when CODING_AGENT_LIVE=1 or recorded is unset
    # and binary exists. Adapters still support recorded fixtures for CI.
    live = is_local() or os.environ.get("CODING_AGENT_LIVE", "1").lower() in {"1", "true", "yes"}
    if not live and name in {"cursor", "claude", "codex", "opencode"} and getattr(adapter, "recorded", None) is None:
        # run() is not necessarily offline (OpenCode invokes its live CLI).
        # Only an explicitly supplied recorded adapter may cross this seam.
        return CodingResult(ok=False, error="live_not_enabled", argv=[])
    if live and name in {"cursor", "claude", "codex", "opencode"} and hasattr(adapter, "run_live"):
        transcript = None
        if transcript_dir:
            transcript = transcript_dir / f"{name}.ndjson"
        return await adapter.run_live(req, transcript_path=transcript)

    if live and name in {"cursor", "claude", "codex", "opencode"}:
        from coding_agents.subprocess_runner import run_live

        transcript = None
        if transcript_dir:
            transcript = transcript_dir / f"{name}.ndjson"
        return await run_live(adapter=adapter, req=req, transcript_path=transcript)

    return await adapter.run(req)


async def dispatch(
    *,
    prompt: str,
    repo: Path,
    branch: str,
    prefs: list[str] | None = None,
    kind: str = "code",
    allow_write: bool = True,
    timeout_s: int = 1800,
    bridge_connected: bool = True,
    mode: str = "auto",
) -> CodingResult:
    if not is_local() and os.environ.get("LLM_OFFLINE", "").strip().lower() in {"1", "true", "yes", "on"}:
        return CodingResult(ok=False, error="llm_offline", argv=[])
    worktree = repo if is_local() else create_worktree(repo, branch)
    req = CodingRequest(
        prompt=wrap_prompt(prompt, kind=kind),
        workdir=worktree,
        allow_write=allow_write,
        timeout_s=timeout_s,
    )
    logs = repo.parent / "logs"
    return await dispatch_request(
        req,
        prefs=prefs,
        kind=kind,
        mode=mode,
        bridge_connected=bridge_connected,
        transcript_dir=logs,
    )
