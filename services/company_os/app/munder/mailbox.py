"""FIPA-lite hive mailbox: hop-cap, speech acts, single-writer delivery.

Ported from Munder Difflin ``src/main/hive.ts`` (normalize / routeMessage /
deliver / drainForStop). Company OS has one orchestrator per venture (``god``),
not a floor of PTY agents — the same routing rules apply to org characters.

MIT: Chaitanya Giri. Do not run vendor/munder-difflin in-process.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timezone
from typing import Any

from app.state import MEM
from app.memory.working import update_working_value

HOP_CAP = 12
MESSAGE_ACTS = ("request", "inform", "propose", "query", "agree", "refuse", "done")
REPLY_ACTS = frozenset({"request", "query", "propose"})
GOD_ID = "god"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _stamp() -> str:
    return _now_iso().replace(":", "-").replace(".", "-")


def _hive(venture_id: str) -> dict[str, Any]:
    key = f"munder_hive:{venture_id}"
    data = MEM.get(key)
    if not isinstance(data, dict):
        data = {
            "god_id": GOD_ID,
            "agents": {
                GOD_ID: {
                    "id": GOD_ID,
                    "name": "Orchestrator",
                    "isGod": True,
                    "status": "idle",
                    "archived": False,
                    "isAssistant": False,
                }
            },
            "inboxes": {},
            "outboxes": {},
            "cursors": {},
            "log": [],
        }
    return data


def _update(venture_id: str, change):
    result = None
    def apply(value):
        nonlocal result
        hive = value if isinstance(value, dict) else {
            "god_id": GOD_ID, "agents": {GOD_ID: {"id": GOD_ID, "isGod": True, "archived": False}},
            "inboxes": {}, "outboxes": {}, "cursors": {}, "log": [],
        }
        result = change(hive)
        return hive
    update_working_value(MEM, f"munder_hive:{venture_id}", apply)
    return result


def register_agent(venture_id: str, agent: dict[str, Any]) -> None:
    return _update(venture_id, lambda hive: _register_agent(hive, agent))


def _register_agent(hive, agent):
    aid = str(agent.get("id") or "").strip()
    if not aid:
        return
    agents = dict(hive.get("agents") or {})
    prev = dict(agents.get(aid) or {})
    prev.update(
        {
            "id": aid,
            "name": agent.get("name") or prev.get("name") or aid,
            "isGod": bool(agent.get("isGod", prev.get("isGod"))),
            "isAssistant": bool(agent.get("isAssistant", prev.get("isAssistant"))),
            "archived": bool(agent.get("archived", prev.get("archived"))),
            "status": agent.get("status") or prev.get("status") or "idle",
            "can_inbox": bool(agent.get("can_inbox", prev.get("can_inbox", True))),
        }
    )
    agents[aid] = prev
    hive["agents"] = agents



def normalize(partial: dict[str, Any], from_id: str) -> dict[str, Any]:
    """Copy of HiveManager.normalize."""
    act = partial.get("act") or "inform"
    if act not in MESSAGE_ACTS:
        act = "inform"
    hops = partial.get("hops")
    hops_n = hops if isinstance(hops, int) else 0
    requires = partial.get("requires_reply")
    if requires is None:
        requires = act in REPLY_ACTS
    return {
        "id": partial.get("id") or f"{_stamp()}-{secrets.token_hex(3)}",
        "conversation": partial.get("conversation") or f"conv-{secrets.token_hex(3)}",
        "in_reply_to": partial.get("in_reply_to"),
        "from": partial.get("from") or from_id,
        "to": partial.get("to") or GOD_ID,
        "act": act,
        "subject": partial.get("subject") or "",
        "body": partial.get("body") or "",
        "hops": hops_n,
        "requires_reply": bool(requires),
        "needs_human": bool(partial.get("needs_human")),
        "created_at": partial.get("created_at") or _now_iso(),
    }


def _inbox(hive: dict[str, Any], agent_id: str) -> list[dict[str, Any]]:
    boxes = dict(hive.get("inboxes") or {})
    items = list(boxes.get(agent_id) or [])
    boxes[agent_id] = items
    hive["inboxes"] = boxes
    return items


def _deliver(hive: dict[str, Any], msg: dict[str, Any], to_id: str) -> None:
    agents = hive.get("agents") or {}
    if to_id not in agents:
        return
    box = _inbox(hive, to_id)
    if any(item.get("id") == msg["id"] for item in box):
        return
    box.append(dict(msg))
    hive["inboxes"][to_id] = box


def _append_log(hive: dict[str, Any], event: dict[str, Any]) -> None:
    log = list(hive.get("log") or [])
    log.append({**event, "ts": _now_iso()})
    hive["log"] = log[-500:]


def route_message(venture_id: str, msg: dict[str, Any]) -> list[str]:
    """Copy of ``routeMessage``: hop-cap drop, human→god, no self, skip assistants."""
    return _update(venture_id, lambda hive: _route_message(hive, msg))


def _route_message(hive, msg):
    if int(msg.get("hops") or 0) > HOP_CAP:
        _append_log(
            hive,
            {
                "kind": "drop",
                "reason": "hop-cap",
                "from": msg.get("from"),
                "to": msg.get("to"),
                "id": msg.get("id"),
            },
        )
        return []

    agents = dict(hive.get("agents") or {})
    god_id = str(hive.get("god_id") or GOD_ID)
    if god_id not in agents:
        agents[god_id] = {"id": god_id, "isGod": True, "archived": False}
        hive["agents"] = agents

    def resolve_to(to: str) -> str:
        return god_id if to in {"human", "god"} else to

    dest = str(msg.get("to") or god_id)
    if dest == "broadcast":
        targets = [
            aid
            for aid, meta in agents.items()
            if aid != msg.get("from")
            and not meta.get("isAssistant")
            and not meta.get("archived")
            and meta.get("can_inbox", True)
        ]
    else:
        targets = [t for t in [resolve_to(dest)] if t != msg.get("from")]

    delivered: list[str] = []
    for target in targets:
        meta = agents.get(target) or {}
        if meta.get("isAssistant"):
            bounced = {
                **msg,
                "to": god_id,
                "subject": (
                    f'[bounced — "{target}" is the send-only prep assistant; '
                    f"route work to a real agent] {msg.get('subject') or ''}"
                ),
            }
            _deliver(hive, bounced, god_id)
            delivered.append(god_id)
            continue
        if target not in agents:
            bounced = {
                **msg,
                "to": god_id,
                "subject": (
                    f'[undeliverable — "{target}" is unknown; relay this] '
                    f"{msg.get('subject') or ''}"
                ),
            }
            _deliver(hive, bounced, god_id)
            delivered.append(god_id)
            continue
        _deliver(hive, msg, target)
        delivered.append(target)

    _append_log(
        hive,
        {
            "kind": "message",
            "from": msg.get("from"),
            "to": msg.get("to"),
            "act": msg.get("act"),
            "subject": msg.get("subject"),
            "id": msg.get("id"),
        },
    )
    return delivered


def send(venture_id: str, partial: dict[str, Any], from_id: str = "system") -> dict[str, Any]:
    msg = normalize(partial, from_id)
    route_message(venture_id, msg)
    return msg


def inbox(venture_id: str, agent_id: str) -> list[dict[str, Any]]:
    hive = _hive(venture_id)
    return list((hive.get("inboxes") or {}).get(agent_id) or [])


def inbox_backlog(venture_id: str, agent_id: str) -> int:
    return len(inbox(venture_id, agent_id))


def drain_for_stop(venture_id: str, agent_id: str) -> dict[str, Any]:
    """Copy of ``drainForStop``: cursor so a message is surfaced exactly once."""
    return _update(venture_id, lambda hive: _drain_for_stop(hive, agent_id))


def _drain_for_stop(hive, agent_id):
    cursors = dict(hive.get("cursors") or {})
    last = (cursors.get(agent_id) or {}).get("lastProcessed")
    fresh = [
        m
        for m in list((hive.get("inboxes") or {}).get(agent_id) or [])
        if not last or str(m.get("id") or "") > str(last)
    ]
    fresh.sort(key=lambda m: str(m.get("id") or ""))
    if not fresh:
        return {"block": False}

    cursors[agent_id] = {"lastProcessed": fresh[-1]["id"]}
    hive["cursors"] = cursors
    _append_log(hive, {"kind": "drain", "agentId": agent_id, "count": len(fresh)})

    lines = [
        f"- [from {m.get('from')}, {m.get('act')}] {m.get('subject')}: {m.get('body')}"
        for m in fresh
    ]
    reason = (
        f"You have {len(fresh)} new hive message(s) in your inbox. "
        "Address them before finishing:\n" + "\n".join(lines)
    )
    return {"block": True, "reason": reason, "messages": fresh}


def event_log(venture_id: str) -> list[dict[str, Any]]:
    return list(_hive(venture_id).get("log") or [])
