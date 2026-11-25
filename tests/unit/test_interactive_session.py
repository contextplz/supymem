"""Interactive Claude/Codex argv — subscription TTY, no platform keys, no bypass by default."""

from __future__ import annotations

import pytest

from coding_agents.env import COMPANY_OS_OWNED_AUTH_VARS, child_env
from coding_agents.session import (
    build_control_argv,
    build_interactive_argv,
    ide_for_provider,
    scrub_var_names,
)


def test_claude_interactive_has_no_print_flag_and_no_bypass_by_default():
    argv = build_interactive_argv("claude")
    assert argv[0] == "claude"
    assert "--ide" in argv
    assert "-p" not in argv
    assert "--print" not in argv
    assert "bypassPermissions" not in argv
    assert "--bare" not in argv


def test_claude_auto_approve_is_opt_in():
    argv = build_interactive_argv("claude", auto_approve=True, model="sonnet")
    assert argv[0] == "claude"
    assert "--ide" in argv
    assert argv[argv.index("--model") + 1] == "sonnet"
    assert argv[argv.index("--permission-mode") + 1] == "bypassPermissions"


def test_codex_interactive_is_not_exec_and_uses_workspace_write():
    argv = build_interactive_argv("codex")
    assert argv[0] == "codex"
    assert "exec" not in argv
    assert "--sandbox" in argv
    assert "workspace-write" in argv
    assert "--dangerously-bypass-approvals-and-sandbox" not in argv


def test_codex_auto_approve_opt_in():
    argv = build_interactive_argv("codex", auto_approve=True)
    assert "--dangerously-bypass-approvals-and-sandbox" in argv


def test_cursor_interactive_is_not_print():
    argv = build_interactive_argv("cursor")
    assert argv[0] == "cursor-agent"
    assert "-p" not in argv
    assert "--print" not in argv
    assert "--force" not in argv
    assert "--force" in build_interactive_argv("cursor", auto_approve=True)


def test_cursor_acp_is_stdio_control_not_tui():
    argv = build_control_argv("cursor", mode="acp")
    assert argv == ["cursor-agent", "acp"]
    assert "-p" not in argv


def test_cursor_acp_pins_workspace_for_open_ide():
    argv = build_control_argv("cursor", mode="acp", workspace="/tmp/songback")
    assert argv[:4] == ["cursor-agent", "--workspace", "/tmp/songback", "acp"]


def test_claude_control_is_print_stream_json():
    argv = build_control_argv(
        "claude", mode="control", prompt="Build the landing page", resume="abc"
    )
    assert argv[0] == "claude"
    assert "-p" in argv
    assert "stream-json" in argv
    assert "--ide" in argv
    assert "--resume" in argv
    assert argv[-1] == "Build the landing page"


def test_claude_control_names_the_ide_session():
    argv = build_control_argv("claude", mode="print", prompt="hi", session_name="Supymem Songback")
    assert argv[argv.index("--name") + 1] == "Supymem Songback"


def test_hermes_acp_is_stdio_control():
    argv = build_control_argv("hermes", mode="acp")
    assert argv == ["hermes", "acp"]


def test_opencode_acp_is_stdio_control():
    argv = build_control_argv("opencode", mode="acp")
    assert argv[:2] == ["opencode", "acp"]


def test_codex_control_is_exec_json():
    argv = build_control_argv("codex", mode="control", prompt="fix tests")
    assert argv[0] == "codex"
    assert "exec" in argv
    assert "--json" in argv
    assert "--skip-git-repo-check" in argv
    assert argv[-1] == "fix tests"


def test_codex_control_resume_uses_exec_resume():
    argv = build_control_argv("codex", mode="print", prompt="continue", resume="thr_1")
    assert argv[:3] == ["codex", "exec", "--skip-git-repo-check"]
    i = argv.index("resume")
    assert argv[i : i + 2] == ["resume", "thr_1"]
    assert argv.index("--json") > i


def test_ide_for_provider_opens_a_real_app():
    assert ide_for_provider("hermes")["bin"] == "hermes"
    assert ide_for_provider("hermes")["kind"] == "acp"
    assert ide_for_provider("cursor")["app"] == "Cursor"
    assert ide_for_provider("claude")["app"] == "Cursor"
    assert ide_for_provider("opencode")["kind"] == "web"


def test_unknown_provider_raises():
    with pytest.raises(ValueError):
        build_interactive_argv("munder")


def test_scrub_list_covers_platform_keys():
    names = set(scrub_var_names())
    for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "LLM_API_KEY", "DEEPSEEK_API_KEY"):
        assert key in names
    env = child_env(
        {
            "ANTHROPIC_API_KEY": "platform",
            "PATH": "/usr/bin",
            "_COS_OWNED_AUTH": "ANTHROPIC_API_KEY",
        }
    )
    assert "ANTHROPIC_API_KEY" not in env
    assert env["PATH"] == "/usr/bin"
    assert "ANTHROPIC_API_KEY" in COMPANY_OS_OWNED_AUTH_VARS
