"""Interactive Claude / Codex / Cursor sessions (desktop PTY, founder's subscription).

Headless Work Orders stay in ``dispatcher.py`` (``claude -p``, ``codex exec``).
This module is the argv source of truth for a real TTY the website cannot host.

Defaults are conservative: the founder is at the keyboard, so we do **not** pass
Munder-style ``bypassPermissions`` / ``--dangerously-bypass-approvals-and-sandbox``
unless they explicitly opt in. Company OS never injects platform API keys.
"""

from __future__ import annotations

from coding_agents.env import COMPANY_OS_OWNED_AUTH_VARS

INTERACTIVE_PROVIDERS = ("hermes", "claude", "codex", "cursor", "opencode")


def build_interactive_argv(
    provider: str,
    *,
    auto_approve: bool = False,
    model: str | None = None,
) -> list[str]:
    """Argv for an interactive CLI session (no ``-p`` / ``exec``)."""
    return build_control_argv(
        provider, mode="interactive", auto_approve=auto_approve, model=model
    )


def build_control_argv(
    provider: str,
    *,
    mode: str = "acp",
    auto_approve: bool = False,
    model: str | None = None,
    prompt: str | None = None,
    resume: str | None = None,
    workspace: str | None = None,
    session_name: str | None = None,
) -> list[str]:
    """Argv to *control* a coding CLI from the desktop (ACP or print/stream-json).

    Cursor and OpenCode speak ACP natively (``cursor-agent acp``, ``opencode acp``).
    Claude Code and Codex do not; we drive them with print/JSON
    (``claude -p --output-format stream-json``, ``codex exec --json``) so the
    desktop can send a prompt and watch tool calls the same way.

    The Mac app opens the matching IDE on ``workspace`` first. Claude then
    gets ``--ide`` so that print session appears in the IDE's Claude panel.
    Cursor ACP is given ``--workspace`` so edits land in the open window.
    """
    name = (provider or "").strip().lower()
    kind = (mode or "acp").strip().lower()
    label = (session_name or "").strip() or None
    if name == "cursor":
        if kind in {"acp", "control"}:
            argv = ["cursor-agent"]
            if workspace:
                argv += ["--workspace", workspace]
            if model:
                argv += ["--model", model]
            argv.append("acp")
            return argv
        argv = ["cursor-agent"]
        if workspace:
            argv += ["--workspace", workspace]
        if model:
            argv += ["--model", model]
        if auto_approve:
            argv.append("--force")
        if kind == "print" and prompt:
            argv += ["-p", "--output-format", "stream-json", prompt]
        return argv
    if name == "claude":
        if kind in {"acp", "control", "print"}:
            argv = ["claude", "-p", "--output-format", "stream-json", "--verbose", "--ide"]
            if model:
                argv += ["--model", model]
            if auto_approve:
                argv += ["--permission-mode", "bypassPermissions"]
            if label:
                argv += ["--name", label]
            if resume:
                argv += ["--resume", resume]
            if prompt:
                argv.append(prompt)
            return argv
        argv = ["claude", "--ide"]
        if model:
            argv += ["--model", model]
        if auto_approve:
            argv += ["--permission-mode", "bypassPermissions"]
        if label:
            argv += ["--name", label]
        return argv
    if name == "codex":
        if kind in {"acp", "control", "print"}:
            argv = ["codex", "exec", "--skip-git-repo-check"]
            if model:
                argv += ["--model", model]
            if auto_approve:
                argv.append("--dangerously-bypass-approvals-and-sandbox")
            else:
                argv += ["--sandbox", "workspace-write"]
            if resume:
                argv += ["resume", resume]
            argv.append("--json")
            if prompt:
                argv.append(prompt)
            return argv
        argv = ["codex"]
        if model:
            argv += ["--model", model]
        if auto_approve:
            argv.append("--dangerously-bypass-approvals-and-sandbox")
        else:
            argv += ["--sandbox", "workspace-write"]
        return argv
    if name == "opencode":
        if kind in {"acp", "control"}:
            argv = ["opencode", "acp"]
            if workspace:
                argv += ["--cwd", workspace]
            if model:
                argv += ["--model", model]
            return argv
        argv = ["opencode"]
        if kind == "print":
            argv.append("run")
            if model:
                argv += ["--model", model]
            if prompt:
                argv.append(prompt)
            return argv
        if model:
            argv += ["--model", model]
        return argv
    if name == "hermes":
        argv = ["hermes", "acp"]
        return argv
    raise ValueError(f"unsupported interactive provider: {provider!r}")


def scrub_var_names() -> list[str]:
    return sorted(COMPANY_OS_OWNED_AUTH_VARS)


def ide_for_provider(provider: str) -> dict[str, str]:
    """Visible IDE the Mac app should open so the CLI session has somewhere to run."""
    name = (provider or "").strip().lower()
    if name == "cursor":
        return {"app": "Cursor", "bin": "cursor", "kind": "gui"}
    if name == "claude":
        return {"app": "Cursor", "fallback_app": "Visual Studio Code", "bin": "code", "kind": "gui"}
    if name == "codex":
        return {"app": "Cursor", "fallback_app": "Visual Studio Code", "bin": "code", "kind": "gui"}
    if name == "opencode":
        return {"app": "OpenCode", "bin": "opencode", "kind": "web"}
    if name == "hermes":
        return {"app": "Supymem Computer Use", "bin": "hermes", "kind": "acp"}
    raise ValueError(f"unsupported interactive provider: {provider!r}")
