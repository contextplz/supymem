"""Detect coding agents on PATH — never read credential files."""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from coding_agents.env import hosted_execution, is_local

@dataclass
class DetectedAgent:
    name: str
    binary: str
    version: str
    auth_mode: str  # "subscription" | "api_key" | "unknown"


# Forbidden credential paths — detection must NEVER touch these.
FORBIDDEN_PATH_SUFFIXES = (
    ".claude",
    ".cursor",
    ".codex",
    ".opencode",
)


def _version(binary: str, expected=None) -> str:
    from desktop_runtime.agent_status import _probe

    try:
        code, raw = (_probe(binary, ["--version"]) if expected is None
                     else _probe(binary, ["--version"], expected=expected))
        output = raw.decode("utf-8", "replace").strip()
        if code != 0 or not output or len(output) > 4096:
            return "unknown"
        line = output.splitlines()[0]
        match = re.search(r"\b\d+\.\d+\.\d+\b", line)
        if not match:
            return "unknown"
        return ("codex-cli " if line.startswith("codex-cli ") else "") + match.group()
    except Exception:  # noqa: BLE001
        return "unknown"


# shortcut: versions refresh when the executable changes, use a TTL if launcher diagnostics become transient.
@lru_cache(maxsize=32)
def _cached_version(expected: tuple) -> str:
    return _version(expected[0], expected)


def detect(path_env: str | None = None) -> list[DetectedAgent]:
    """Scan PATH for known CLIs. Never opens ~/.claude etc."""
    if hosted_execution():
        return []
    from desktop_runtime.agent_status import provider_binary, statuses
    from desktop_runtime.owned_coding import _safe_location, executable_identity, provider_which

    if is_local():
        found = [DetectedAgent(status["name"], location, "unknown",
                              "subscription" if status["subscription_login"] else "unknown")
                 for status in statuses(path_env=path_env)
                 if status["installed"] and not status.get("unsafe_location")
                 and (location := status.get("candidate_path") or provider_binary(status["name"], path_env))]
        if (hermes := provider_which("hermes", path_env, launcher=True)) and _safe_location(hermes):
            found.append(DetectedAgent("hermes", hermes, "unknown", "unknown"))
        return found

    found: list[DetectedAgent] = []
    mapping = {
        "hermes": "hermes",
        "opencode": "opencode",
        "cursor-agent": "cursor",
        "claude": "claude",
        "codex": "codex",
    }
    for binary, name in mapping.items():
        try:
            loc = provider_which(binary, path_env) if name == "hermes" else provider_binary(name, path_env)
            if not loc:
                continue
            identity = executable_identity(loc)
        except (OSError, ValueError, RuntimeError):
            continue
        found.append(
            DetectedAgent(
                name=name,
                binary=loc,
                version=_cached_version(identity),
                auth_mode="unknown",
            )
        )
    return found


def assert_no_credential_reads(opened_paths: list[str]) -> None:
    home = str(Path.home())
    for p in opened_paths:
        for suf in FORBIDDEN_PATH_SUFFIXES:
            if f"{home}/{suf}" in p or p.startswith(str(Path.home() / suf)):
                raise AssertionError(f"credential path touched: {p}")
