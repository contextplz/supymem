"""Huginn-shaped event graph — agents emit JSON events along directed links.

Ported from huginn/huginn (MIT). We do not run Rails. The check / receive /
create_event / receive! loop is the same kernel, in-process.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from huginn_watch.jsonpath import same_json, value_at

Fetcher = Callable[[str, dict[str, Any]], str]


class AgentHandler(Protocol):
    type_name: str

    def check(self, agent: "Agent", graph: "Graph") -> None: ...
    def receive(self, agent: "Agent", graph: "Graph", events: list["Event"]) -> None: ...
    def receive_web_request(
        self,
        agent: "Agent",
        graph: "Graph",
        *,
        secret: str,
        method: str,
        params: dict[str, Any],
        headers: dict[str, str] | None = None,
    ) -> tuple[str, int]: ...


@dataclass
class Event:
    id: int
    agent_id: str
    payload: dict[str, Any]
    created_at: float


@dataclass
class Link:
    source_id: str
    receiver_id: str
    event_id_at_creation: int = 0


@dataclass
class Agent:
    id: str
    type: str
    name: str
    options: dict[str, Any] = field(default_factory=dict)
    memory: dict[str, Any] = field(default_factory=dict)
    schedule: str = "never"
    disabled: bool = False
    last_checked_event_id: int | None = None
    keep_events_for: int = 0
    last_check_at: float | None = None


_HANDLERS: dict[str, AgentHandler] = {}


def register(handler: AgentHandler | type[AgentHandler]) -> AgentHandler | type[AgentHandler]:
    inst = handler() if isinstance(handler, type) else handler
    _HANDLERS[inst.type_name] = inst
    return handler


def handler_for(type_name: str) -> AgentHandler:
    key = type_name.split(".")[-1]
    if key not in _HANDLERS:
        raise KeyError(f"unknown Huginn agent type: {type_name}")
    return _HANDLERS[key]


_SCHEDULE_S = {
    "every_1m": 60,
    "every_2m": 120,
    "every_5m": 300,
    "every_10m": 600,
    "every_30m": 1800,
    "every_1h": 3600,
    "every_2h": 7200,
    "every_5h": 18000,
    "every_12h": 43200,
    "every_1d": 86400,
    "every_2d": 172800,
    "every_7d": 604800,
}


def _due(agent: Agent, now: float) -> bool:
    if agent.schedule == "never" or agent.disabled:
        return False
    interval = _SCHEDULE_S.get(agent.schedule, 3600)
    if agent.last_check_at is None:
        return True
    return now - float(agent.last_check_at) >= interval


class Graph:
    """In-memory Huginn scenario: agents, links, events."""

    def __init__(self, *, now: Callable[[], float] | None = None, fetcher: Fetcher | None = None) -> None:
        self.agents: dict[str, Agent] = {}
        self.links: list[Link] = []
        self.events: list[Event] = []
        self._next_event_id = 1
        self._now = now or _default_now
        self.fetcher = fetcher

    def add_agent(self, agent: Agent) -> Agent:
        self.agents[agent.id] = agent
        return agent

    def link(self, source_id: str, receiver_id: str) -> Link:
        row = Link(source_id=source_id, receiver_id=receiver_id, event_id_at_creation=self._next_event_id - 1)
        self.links.append(row)
        return row

    def create_event(self, agent: Agent, payload: dict[str, Any]) -> Event:
        event = Event(
            id=self._next_event_id,
            agent_id=agent.id,
            payload=dict(payload),
            created_at=self._now(),
        )
        self._next_event_id += 1
        self.events.append(event)
        return event

    def events_for(self, agent_id: str, *, limit: int | None = None) -> list[Event]:
        rows = [e for e in self.events if e.agent_id == agent_id]
        rows.sort(key=lambda e: e.id, reverse=True)
        if limit is not None:
            return rows[:limit]
        return rows

    def check(self, agent_id: str) -> list[Event]:
        agent = self.agents[agent_id]
        if agent.disabled:
            return []
        before = self._next_event_id
        handler_for(agent.type).check(agent, self)
        agent.last_check_at = self._now()
        return [e for e in self.events if e.id >= before]

    def check_due(self, *, schedule: str | None = None, force: bool = False) -> list[Event]:
        emitted: list[Event] = []
        now = self._now()
        for agent in list(self.agents.values()):
            if schedule and agent.schedule != schedule:
                continue
            if not force and not _due(agent, now):
                continue
            emitted.extend(self.check(agent.id))
        return emitted

    def propagate(self, *, waves: int = 8) -> dict[str, int]:
        """Huginn ``Agent.receive!``. In-process we drain a few waves so a
        Website → Dedupe → Digest chain finishes in one sweep (Rails used Delayed Job).
        """
        total_agents = 0
        total_events = 0
        for _ in range(max(1, waves)):
            before = self._next_event_id
            stats = self._propagate_once()
            total_agents += stats["agent_count"]
            total_events += stats["event_count"]
            if self._next_event_id == before:
                break
        return {"agent_count": total_agents, "event_count": total_events}

    def _propagate_once(self) -> dict[str, int]:
        receivers: dict[str, list[int]] = {}
        for link in self.links:
            receiver = self.agents.get(link.receiver_id)
            source = self.agents.get(link.source_id)
            if not receiver or not source or receiver.disabled:
                continue
            last = receiver.last_checked_event_id or 0
            for event in self.events:
                if event.agent_id != source.id:
                    continue
                if event.id <= link.event_id_at_creation:
                    continue
                if event.id <= last:
                    continue
                receivers.setdefault(receiver.id, []).append(event.id)
        agent_count = 0
        event_ids: set[int] = set()
        for rid, ids in receivers.items():
            uniq = sorted(set(ids))
            agent = self.agents[rid]
            agent.last_checked_event_id = uniq[-1]
            incoming = [e for e in self.events if e.id in set(uniq)]
            incoming.sort(key=lambda e: e.id)
            handler_for(agent.type).receive(agent, self, incoming)
            agent_count += 1
            event_ids.update(uniq)
        return {"agent_count": agent_count, "event_count": len(event_ids)}

    def webhook(
        self,
        agent_id: str,
        *,
        secret: str,
        method: str = "post",
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[str, int]:
        agent = self.agents[agent_id]
        return handler_for(agent.type).receive_web_request(
            agent,
            self,
            secret=secret,
            method=method.lower(),
            params=params or {},
            headers=headers,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "next_event_id": self._next_event_id,
            "agents": [
                {
                    "id": a.id,
                    "type": a.type,
                    "name": a.name,
                    "options": a.options,
                    "memory": a.memory,
                    "schedule": a.schedule,
                    "disabled": a.disabled,
                    "last_checked_event_id": a.last_checked_event_id,
                    "keep_events_for": a.keep_events_for,
                    "last_check_at": a.last_check_at,
                }
                for a in self.agents.values()
            ],
            "links": [
                {
                    "source_id": ln.source_id,
                    "receiver_id": ln.receiver_id,
                    "event_id_at_creation": ln.event_id_at_creation,
                }
                for ln in self.links
            ],
            "events": [
                {
                    "id": e.id,
                    "agent_id": e.agent_id,
                    "payload": e.payload,
                    "created_at": e.created_at,
                }
                for e in self.events[-2000:]
            ],
        }

    @classmethod
    def from_dict(
        cls,
        data: dict[str, Any] | None,
        *,
        now: Callable[[], float] | None = None,
        fetcher: Fetcher | None = None,
    ) -> "Graph":
        graph = cls(now=now, fetcher=fetcher)
        if not data:
            return graph
        graph._next_event_id = int(data.get("next_event_id") or 1)
        for row in data.get("agents") or []:
            graph.add_agent(
                Agent(
                    id=str(row["id"]),
                    type=str(row["type"]),
                    name=str(row.get("name") or row["type"]),
                    options=dict(row.get("options") or {}),
                    memory=dict(row.get("memory") or {}),
                    schedule=str(row.get("schedule") or "never"),
                    disabled=bool(row.get("disabled")),
                    last_checked_event_id=row.get("last_checked_event_id"),
                    keep_events_for=int(row.get("keep_events_for") or 0),
                    last_check_at=row.get("last_check_at"),
                )
            )
        for row in data.get("links") or []:
            graph.links.append(
                Link(
                    source_id=str(row["source_id"]),
                    receiver_id=str(row["receiver_id"]),
                    event_id_at_creation=int(row.get("event_id_at_creation") or 0),
                )
            )
        for row in data.get("events") or []:
            graph.events.append(
                Event(
                    id=int(row["id"]),
                    agent_id=str(row["agent_id"]),
                    payload=dict(row.get("payload") or {}),
                    created_at=float(row.get("created_at") or 0),
                )
            )
        return graph


def _default_now() -> float:
    import time

    return time.time()


# Re-export helpers agents use
__all__ = [
    "Agent",
    "AgentHandler",
    "Event",
    "Fetcher",
    "Graph",
    "Link",
    "handler_for",
    "register",
    "same_json",
    "value_at",
]
