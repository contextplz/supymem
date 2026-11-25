"""Huginn agent types. Importing this module registers every handler."""

from __future__ import annotations

from huginn_watch.agents import change as _change  # noqa: F401
from huginn_watch.agents import dedupe as _dedupe  # noqa: F401
from huginn_watch.agents import digest as _digest  # noqa: F401
from huginn_watch.agents import manual as _manual  # noqa: F401
from huginn_watch.agents import peak as _peak  # noqa: F401
from huginn_watch.agents import rss as _rss  # noqa: F401
from huginn_watch.agents import trigger as _trigger  # noqa: F401
from huginn_watch.agents import webhook as _webhook  # noqa: F401
from huginn_watch.agents import website as _website  # noqa: F401
from huginn_watch.engine import Agent, Event, Graph, handler_for, register

__all__ = ["Agent", "Event", "Graph", "handler_for", "register"]
