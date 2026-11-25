from __future__ import annotations

import pytest

from coding_agents.detect import DetectedAgent
from coding_agents.select import select, should_fall_through


@pytest.fixture(autouse=True)
def hosted_selection(monkeypatch):
    monkeypatch.setenv("SUPYMEM_MODE", "hosted")


def test_infra_failure_falls_through_to_next_pref():
    detected = [
        DetectedAgent(name="cursor", binary="/bin/cursor-agent", version="1", auth_mode="subscription"),
        DetectedAgent(name="claude", binary="/bin/claude", version="1", auth_mode="subscription"),
    ]
    choice = select(
        prefs=["cursor", "claude", "inhouse"],
        detected=detected,
        mode="local",
        bridge_connected=True,
        failed=[("cursor", "infra")],
    )
    assert choice is not None
    assert choice.adapter_name == "claude"
    assert should_fall_through("infra") is True
    assert should_fall_through("task") is False


def test_task_failure_listed_does_not_auto_select_same_again_when_skipped_as_infra_only():
    # Task failures are not added to failed by select(); caller must stop.
    detected = [
        DetectedAgent(name="cursor", binary="/bin/cursor-agent", version="1", auth_mode="subscription"),
    ]
    choice = select(
        prefs=["cursor", "inhouse"],
        detected=detected,
        mode="local",
        bridge_connected=True,
        failed=[],  # task failure → caller does not pass it as infra
    )
    assert choice is not None
    assert choice.adapter_name == "cursor"


def test_cloud_key_selected_when_bridge_offline():
    choice = select(
        prefs=["cursor", "inhouse"],
        detected=[],
        mode="auto",
        bridge_connected=False,
        cloud_keys={"CURSOR_API_KEY": True},
    )
    assert choice is not None
    assert choice.adapter_name == "cursor_cloud"
    assert choice.mode == "cloud"


def test_claude_routine_skipped_for_connector_kind():
    choice = select(
        kind="connector",
        prefs=[],
        detected=[],
        mode="cloud",
        bridge_connected=False,
        cloud_keys={"ANTHROPIC_API_KEY": True, "CURSOR_API_KEY": False, "CODEX_API_KEY": False},
        inhouse_hosted_available=False,
    )
    # Only claude_routine would match ANTHROPIC — but connector kind skips it.
    assert choice is None or choice.adapter_name != "claude_routine"
