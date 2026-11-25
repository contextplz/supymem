"""WebhookAgent — POST with a secret creates events (Huginn receive_web_request)."""

from __future__ import annotations

from typing import Any

from huginn_watch.agents import BaseHandler
from huginn_watch.engine import Agent, Graph, register
from huginn_watch.jsonpath import value_at


@register
class WebhookAgent(BaseHandler):
    type_name = "WebhookAgent"

    def receive_web_request(
        self,
        agent: Agent,
        graph: Graph,
        *,
        secret: str,
        method: str,
        params: dict[str, Any],
        headers: dict[str, str] | None = None,
    ) -> tuple[str, int]:
        expected = str(agent.options.get("secret") or "")
        if not expected or secret != expected:
            return ("Not Authorized", 401)
        verbs = [
            v.strip().lower()
            for v in str(agent.options.get("verbs") or "post").split(",")
            if v.strip()
        ]
        if method not in verbs:
            return (f"Please use {'/'.join(v.upper() for v in verbs)} requests only", 401)
        path = agent.options.get("payload_path") or "."
        payload = value_at(params, path) if path not in {".", "$"} else params
        rows = payload if isinstance(payload, list) else [payload]
        header_key = str(agent.options.get("event_headers_key") or "headers")
        wanted = [
            h.strip()
            for h in str(agent.options.get("event_headers") or "").split(",")
            if h.strip()
        ]
        extra = {}
        if wanted and headers:
            extra[header_key] = {k: headers.get(k) or headers.get(k.lower()) for k in wanted}
        for row in rows:
            body = dict(row) if isinstance(row, dict) else {"value": row}
            body.update(extra)
            graph.create_event(agent, body)
        code = int(agent.options.get("code") or 201)
        message = str(agent.options.get("response") or "Event Created")
        return (message, code)
