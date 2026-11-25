"""DigestAgent — queue incoming events, emit one batched event on check."""

from __future__ import annotations

from huginn_watch.agents import BaseHandler
from huginn_watch.engine import Agent, Event, Graph, register


@register
class DigestAgent(BaseHandler):
    type_name = "DigestAgent"

    def receive(self, agent: Agent, graph: Graph, events: list[Event]) -> None:
        queue = list(agent.memory.get("queue") or [])
        for event in events:
            queue.append(event.id)
        retained = int(agent.options.get("retained_events") or 0)
        if retained > 0 and len(queue) > retained:
            queue = queue[-retained:]
        agent.memory["queue"] = queue

    def check(self, agent: Agent, graph: Graph) -> None:
        queue = list(agent.memory.get("queue") or [])
        if not queue:
            return
        wanted = set(queue)
        payloads = [e.payload for e in graph.events if e.id in wanted]
        messages = [str(p.get("message") or p.get("title") or p.get("url") or "") for p in payloads]
        message = str(agent.options.get("message") or "")
        if "{{" in message or not message:
            message = ", ".join(m for m in messages if m)
        graph.create_event(agent, {"events": payloads, "message": message})
        if int(agent.options.get("retained_events") or 0) == 0:
            agent.memory["queue"] = []
