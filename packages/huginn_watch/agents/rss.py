"""RssAgent — emit new feed items, remember IDs (Huginn remembered_id_count)."""

from __future__ import annotations

from huginn_watch.agents import BaseHandler
from huginn_watch.engine import Agent, Graph, register
from huginn_watch.fetch import fetch_url
from huginn_watch.rssparse import parse_feed


@register
class RssAgent(BaseHandler):
    type_name = "RssAgent"

    def check(self, agent: Agent, graph: Graph) -> None:
        urls = agent.options.get("url")
        url_list = urls if isinstance(urls, list) else [urls]
        fetcher = graph.fetcher or fetch_url
        max_events = int(agent.options.get("max_events_per_run") or 0)
        created = 0
        for url in url_list:
            if not url:
                continue
            body = fetcher(str(url), agent.options)
            for payload in parse_feed(body, feed_url=str(url)):
                if not _check_and_track(agent, str(payload.get("id") or "")):
                    continue
                graph.create_event(agent, payload)
                created += 1
                if max_events and created >= max_events:
                    return


def _check_and_track(agent: Agent, entry_id: str) -> bool:
    """Huginn RssAgent#check_and_track — newest IDs at the front, cap the list."""
    seen = list(agent.memory.get("seen_ids") or [])
    if entry_id in seen:
        return False
    seen.insert(0, entry_id)
    cap = int(agent.options.get("remembered_id_count") or 500)
    if len(seen) > cap:
        seen = seen[:cap]
    agent.memory["seen_ids"] = seen
    return True
