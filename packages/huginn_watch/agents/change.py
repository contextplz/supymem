"""ChangeDetectorAgent — re-emit when a property differs from memory."""

from __future__ import annotations

from huginn_watch.agents import BaseHandler
from huginn_watch.engine import Agent, Event, Graph, register
from huginn_watch.jsonpath import value_at


@register
class ChangeDetectorAgent(BaseHandler):
    type_name = "ChangeDetectorAgent"

    def receive(self, agent: Agent, graph: Graph, events: list[Event]) -> None:
        path = str(agent.options.get("property") or "output").strip("{} ").strip()
        for event in events:
            prop = value_at(event.payload, path)
            if prop is None and path in event.payload:
                prop = event.payload.get(path)
            last = agent.memory.get("last_property")
            if prop != last:
                graph.create_event(agent, dict(event.payload))
                agent.memory["last_property"] = prop
