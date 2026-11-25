"""ManualEventAgent — Huginn source that only emits when something injects an event."""

from __future__ import annotations

from huginn_watch.agents import BaseHandler
from huginn_watch.engine import register


@register
class ManualEventAgent(BaseHandler):
    type_name = "ManualEventAgent"
