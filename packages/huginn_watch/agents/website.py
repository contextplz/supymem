"""WebsiteAgent — scrape a URL and emit events (Huginn on_change / all / merge)."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from huginn_watch.agents import BaseHandler
from huginn_watch.engine import Agent, Event, Graph, register, same_json
from huginn_watch.fetch import fetch_url
from huginn_watch.jsonpath import values_at

UNIQUENESS_LOOK_BACK = 200
UNIQUENESS_FACTOR = 3


@register
class WebsiteAgent(BaseHandler):
    type_name = "WebsiteAgent"

    def check(self, agent: Agent, graph: Graph) -> None:
        url = agent.options.get("url")
        self._check_urls(agent, graph, url, {})

    def receive(self, agent: Agent, graph: Graph, events: list[Event]) -> None:
        mode = str(agent.options.get("mode") or "on_change")
        for event in events:
            existing = dict(event.payload) if mode == "merge" else {}
            data_tpl = agent.options.get("data_from_event")
            if data_tpl:
                data = event.payload.get(str(data_tpl).strip("{} ").strip()) if "{{" in str(data_tpl) else event.payload
                if isinstance(data, (dict, str)):
                    body = data if isinstance(data, str) else json.dumps(data)
                    self._handle_data(agent, graph, body, event.payload.get("url") or "", existing)
                continue
            url = event.payload.get("url") or agent.options.get("url")
            self._check_urls(agent, graph, url, existing)

    def _check_urls(self, agent: Agent, graph: Graph, in_url: Any, existing: dict[str, Any]) -> None:
        urls = in_url if isinstance(in_url, list) else [in_url]
        for url in urls:
            if not url:
                continue
            self._check_url(agent, graph, str(url), existing)

    def _check_url(self, agent: Agent, graph: Graph, url: str, existing: dict[str, Any]) -> None:
        if not re.match(r"\Ahttps?://", url, re.I):
            return
        fetcher = graph.fetcher or fetch_url
        body = fetcher(url, agent.options)
        self._handle_data(agent, graph, body, url, existing)

    def _handle_data(self, agent: Agent, graph: Graph, body: str, url: str, existing: dict[str, Any]) -> None:
        kind = str(agent.options.get("type") or _guess_type(url, body)).lower()
        extract = agent.options.get("extract")
        if kind == "json":
            try:
                doc = json.loads(body)
            except json.JSONDecodeError:
                return
            if not extract:
                payloads = [doc if isinstance(doc, dict) else {"value": doc}]
            else:
                payloads = _extract_json(doc, extract)
        elif kind == "text" and extract:
            payloads = _extract_text(body, extract)
        else:
            payloads = [_html_or_text_payload(body, url)]

        look_back = _look_back(agent, len(payloads))
        old = graph.events_for(agent.id, limit=look_back) if str(agent.options.get("mode") or "on_change") == "on_change" else []
        for result in payloads:
            merged = {**existing, **result}
            if _store_payload(agent, old, merged):
                graph.create_event(agent, merged)
                old.insert(0, Event(id=0, agent_id=agent.id, payload=merged, created_at=0))


def _guess_type(url: str, body: str) -> str:
    if re.search(r"\.(rss|xml)$", url, re.I) or body.lstrip().startswith("<?xml") or "<rss" in body[:200]:
        return "xml"
    if re.search(r"\.json$", url, re.I) or body.lstrip()[:1] in "{[":
        return "json"
    if re.search(r"\.(txt|text)$", url, re.I):
        return "text"
    return "html"


def _html_or_text_payload(body: str, url: str) -> dict[str, Any]:
    title = ""
    m = re.search(r"<title[^>]*>(.*?)</title>", body, re.I | re.S)
    if m:
        title = re.sub(r"\s+", " ", m.group(1)).strip()
    text = re.sub(r"<script[\s\S]*?</script>", " ", body, flags=re.I)
    text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    digest = hashlib.sha256(text.encode()).hexdigest()[:24]
    return {
        "url": url,
        "title": title,
        "content": text[:8000],
        "content_hash": digest,
    }


def _extract_text(doc: str, extract: dict[str, Any]) -> list[dict[str, Any]]:
    columns: dict[str, list[str]] = {}
    for name, spec in extract.items():
        if not isinstance(spec, dict):
            continue
        pattern = re.compile(str(spec.get("regexp") or ""))
        index = spec.get("index", 0)
        if isinstance(index, str) and index.isdigit():
            index = int(index)
        values: list[str] = []
        for match in pattern.finditer(doc):
            if isinstance(index, str):
                values.append(match.group(index) or "")
            else:
                values.append(match.group(int(index)) or "")
        columns[name] = values
    return _zip_columns(columns)


def _extract_json(doc: Any, extract: dict[str, Any]) -> list[dict[str, Any]]:
    columns: dict[str, list[Any]] = {}
    for name, spec in extract.items():
        if not isinstance(spec, dict):
            continue
        columns[name] = values_at(doc, spec.get("path"))
    return _zip_columns(columns)


def _zip_columns(columns: dict[str, list[Any]]) -> list[dict[str, Any]]:
    if not columns:
        return []
    size = max(len(v) for v in columns.values()) or 0
    rows: list[dict[str, Any]] = []
    for i in range(size):
        row = {}
        for name, values in columns.items():
            if i < len(values):
                row[name] = values[i]
            elif values:
                row[name] = values[0]
        rows.append(row)
    return rows


def _look_back(agent: Agent, num_events: int) -> int:
    if agent.options.get("uniqueness_look_back"):
        return int(agent.options["uniqueness_look_back"])
    return max(UNIQUENESS_LOOK_BACK, UNIQUENESS_FACTOR * num_events)


def _store_payload(agent: Agent, old_events: list[Event], result: dict[str, Any]) -> bool:
    mode = str(agent.options.get("mode") or "on_change")
    if mode == "on_change":
        for event in old_events:
            if same_json(event.payload, result):
                return False
        return True
    if mode in {"all", "merge", ""}:
        return True
    raise ValueError(f"Illegal options[mode]: {mode}")
