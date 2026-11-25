"""Munder Difflin hive algorithms, adapted for Company OS.

Upstream: https://github.com/chaitanyagiri/munder-difflin (MIT, Chaitanya Giri).
Copyright (c) 2026 Chaitanya Giri. The MIT notice from
``vendor/munder-difflin/LICENSE`` applies to the algorithms copied here.

Munder Difflin is an Electron + Pixi.js "office of clones" that wraps terminal
agent CLIs (Claude Code, Codex, …). We do **not** run that desktop app, PTY
manager, pixel-art floor, or MemPalace CLI in-process.

We take the hive *process* — mailbox routing, hop-cap, kanban ledger, GOD
adjudication, steer→constrain→stop breaker, operator control, memory regions,
secret redaction — and execute it against Company OS ventures, planned tasks,
and the founder ask queue.

Same pattern as ``app.paperclip`` / ``app.gbrain``.
"""

from app.munder.adjudicate import classify_work
from app.munder.breaker import (
    block_for_venture,
    forget as forget_breaker,
    level_for,
    record_compact_end,
    record_compact_start,
    record_error,
    record_tool_use,
    tick_venture,
)
from app.munder.condense import condense_memory, parse_memory, verify_condense
from app.munder.control import ControlRegistry, control_for, should_halt, take_steer, tool_decision
from app.munder.ledger import add_task, delete_task, deps_satisfied, patch_task, snapshot as ledger_snapshot
from app.munder.mailbox import HOP_CAP, drain_for_stop, inbox_backlog, send
from app.munder.redact import redact_secrets, redact_tree

__all__ = [
    "HOP_CAP",
    "ControlRegistry",
    "add_task",
    "block_for_venture",
    "classify_work",
    "condense_memory",
    "control_for",
    "delete_task",
    "deps_satisfied",
    "drain_for_stop",
    "forget_breaker",
    "hive_snapshot",
    "inbox_backlog",
    "ledger_snapshot",
    "level_for",
    "parse_memory",
    "patch_task",
    "record_compact_end",
    "record_compact_start",
    "record_error",
    "record_tool_use",
    "redact_secrets",
    "redact_tree",
    "send",
    "should_halt",
    "take_steer",
    "tick_venture",
    "tool_decision",
    "verify_condense",
]


def hive_snapshot(venture_id: str) -> dict:
    """Dashboard-facing hive health for one venture."""
    from app.munder.control import snapshot as control_snapshot

    log = ledger_snapshot(venture_id)
    blocked = [c for c in log["tasks"] if c.get("status") == "blocked"]
    return {
        "breaker": level_for(venture_id),
        "inbox_backlog": inbox_backlog(venture_id, "god"),
        "blocked_cards": len(blocked),
        "open_human_qa": sum(
            1
            for c in blocked
            for qa in (c.get("humanQA") or [])
            if qa.get("q") and not qa.get("a") and not qa.get("dismissedAt")
        ),
        "control": control_snapshot(venture_id),
        "cards": len(log["tasks"]),
    }
