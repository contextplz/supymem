"""PeakDetectorAgent — mean + std spike detector (Huginn stats_for)."""

from __future__ import annotations

import math
from typing import Any

from huginn_watch.agents import BaseHandler
from huginn_watch.engine import Agent, Event, Graph, register
from huginn_watch.jsonpath import value_at


@register
class PeakDetectorAgent(BaseHandler):
    type_name = "PeakDetectorAgent"

    def receive(self, agent: Agent, graph: Graph, events: list[Event]) -> None:
        incoming = sorted(events, key=lambda e: e.created_at)
        for event in incoming:
            group = _group_for(agent, event)
            _remember(agent, group, event)
            _check_for_peak(agent, graph, group, event)


def _group_for(agent: Agent, event: Event) -> str:
    path = str(agent.options.get("group_by_path") or "").strip()
    if path:
        found = value_at(event.payload, path)
        if found is not None:
            return str(found)
    return "no_group"


def _remember(agent: Agent, group: str, event: Event) -> None:
    data = agent.memory.setdefault("data", {})
    series = data.setdefault(group, [])
    path = str(agent.options.get("value_path") or "count")
    value = value_at(event.payload, path)
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = 0.0
    series.append([number, float(event.created_at)])
    newest = series[-1][1]
    window = _window_duration(agent)
    data[group] = [row for row in series if row[1] > newest - window]


def _check_for_peak(agent: Agent, graph: Graph, group: str, event: Event) -> None:
    peaks = agent.memory.setdefault("peaks", {})
    peaks.setdefault(group, [])
    series = (agent.memory.get("data") or {}).get(group) or []
    min_events = int(agent.options.get("min_events") or 4)
    if len(series) <= min_events:
        return
    newest_value, newest_time = float(series[-1][0]), float(series[-1][1])
    if peaks[group] and peaks[group][-1] >= newest_time - _peak_spacing(agent):
        return
    average, std = _stats_for(series, skip_last=1)
    if newest_value > average + _std_multiple(agent) * std:
        peaks[group].append(newest_time)
        window = _window_duration(agent)
        peaks[group] = [p for p in peaks[group] if p > newest_time - window]
        message = str(agent.options.get("message") or "A peak was found")
        graph.create_event(
            agent,
            {
                "message": message,
                "peak": newest_value,
                "peak_time": newest_time,
                "grouped_by": group,
            },
        )


def _stats_for(series: list[list[Any]], *, skip_last: int = 0) -> tuple[float, float]:
    """Huginn PeakDetectorAgent#stats_for — population mean and std."""
    data = [float(row[0]) for row in series]
    if skip_last:
        data = data[: len(data) - skip_last]
    length = float(len(data)) or 1.0
    mean = sum(data) / length
    mean_variance = sum((value - mean) ** 2 for value in data) / length
    return mean, math.sqrt(mean_variance)


def _window_duration(agent: Agent) -> float:
    if agent.options.get("window_duration"):
        return float(agent.options["window_duration"])
    days = float(agent.options.get("window_duration_in_days") or 14)
    return days * 86400


def _peak_spacing(agent: Agent) -> float:
    if agent.options.get("peak_spacing"):
        return float(agent.options["peak_spacing"])
    days = float(agent.options.get("min_peak_spacing_in_days") or 2)
    return days * 86400


def _std_multiple(agent: Agent) -> float:
    return float(agent.options.get("std_multiple") or 3)
