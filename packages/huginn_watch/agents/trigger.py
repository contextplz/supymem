"""TriggerAgent — emit when incoming payload matches Huginn comparison rules."""

from __future__ import annotations

import re
from typing import Any

from huginn_watch.agents import BaseHandler
from huginn_watch.engine import Agent, Event, Graph, register
from huginn_watch.jsonpath import value_at

VALID_COMPARISON_TYPES = {
    "regex",
    "!regex",
    "field<value",
    "field<=value",
    "field==value",
    "field!=value",
    "field>=value",
    "field>value",
    "not in",
}


@register
class TriggerAgent(BaseHandler):
    type_name = "TriggerAgent"

    def receive(self, agent: Agent, graph: Graph, events: list[Event]) -> None:
        for event in events:
            if not _matches(agent, event.payload):
                continue
            keep = _bool(agent.options.get("keep_event"))
            message = str(agent.options.get("message") or "")
            if keep:
                payload = dict(event.payload)
                if message:
                    payload["message"] = message
            else:
                payload = {"message": message or "matched"}
            graph.create_event(agent, payload)


def _matches(agent: Agent, payload: dict[str, Any]) -> bool:
    rules = list(agent.options.get("rules") or [])
    if not rules:
        return False
    results = [_rule_ok(rule, payload) for rule in rules]
    must = agent.options.get("must_match")
    if must is None or must == "":
        return all(results)
    return sum(1 for r in results if r) >= int(must)


def _rule_ok(rule: Any, payload: dict[str, Any]) -> bool:
    if isinstance(rule, str):
        return rule.strip().lower() in {"true", "1", "yes"}
    if not isinstance(rule, dict):
        return False
    path = str(rule.get("path") or "")
    value_at_path = value_at(payload, path)
    rule_values = rule.get("value")
    if not isinstance(rule_values, list):
        rule_values = [rule_values]
    kind = str(rule.get("type") or "")
    if kind == "not in":
        return str(value_at_path) not in [str(v) for v in rule_values]
    if kind == "field==value":
        return str(value_at_path) in [str(v) for v in rule_values]
    for rule_value in rule_values:
        if _compare(kind, value_at_path, rule_value):
            return True
    return False


def _compare(kind: str, left: Any, right: Any) -> bool:
    if kind == "regex":
        return re.search(str(right), str(left), re.I) is not None
    if kind == "!regex":
        return re.search(str(right), str(left), re.I) is None
    if kind == "field>value":
        return _num(left) > _num(right)
    if kind == "field>=value":
        return _num(left) >= _num(right)
    if kind == "field<value":
        return _num(left) < _num(right)
    if kind == "field<=value":
        return _num(left) <= _num(right)
    if kind == "field!=value":
        return str(left) != str(right)
    return False


def _num(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"true", "1", "yes"}
