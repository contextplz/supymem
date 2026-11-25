"""DeDuplicationAgent — remits an event only if the property is new."""

from __future__ import annotations

import json
import zlib

from huginn_watch.agents import BaseHandler
from huginn_watch.engine import Agent, Event, Graph, register
from huginn_watch.jsonpath import value_at


@register
class DeDuplicationAgent(BaseHandler):
    type_name = "DeDuplicationAgent"

    def receive(self, agent: Agent, graph: Graph, events: list[Event]) -> None:
        agent.memory.setdefault("properties", [])
        path = str(agent.options.get("property") or "")
        lookback = int(agent.options.get("lookback") or 100)
        for event in events:
            raw = (
                json.dumps(event.payload, sort_keys=True, default=str)
                if not path
                else str(value_at(event.payload, path.strip("{} ").strip()) or "")
            )
            key = _get_hash(raw)
            if key in agent.memory["properties"]:
                continue
            graph.create_event(agent, dict(event.payload))
            props = list(agent.memory["properties"])
            if lookback != 0 and len(props) == lookback:
                props = props[1:]
            props.append(key)
            agent.memory["properties"] = props


def _get_hash(property_value: str) -> str:
    """Huginn DeDuplicationAgent#get_hash — CRC32 once the string is long."""
    if len(property_value) > 10:
        return str(zlib.crc32(property_value.encode()) & 0xFFFFFFFF)
    return property_value
