"""Huginn watches for a venture — persist the graph, sweep, notify the founder."""

from __future__ import annotations

import hashlib
import secrets
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import func, select

_ROOT = Path(__file__).resolve().parents[4]
_PACKAGES = _ROOT / "packages"
if str(_PACKAGES) not in sys.path:
    sys.path.insert(0, str(_PACKAGES))

from huginn_watch import Agent, Graph
from huginn_watch.fetch import fetch_url

from app.db.models import InterestSignal, Venture
from app.state import db


def _mem():
    from app.state import MEM

    return MEM

_MEM = "huginn_watch:"
_GRAPHS: dict[str, dict[str, Any]] = {}


def _key(venture_id: str) -> str:
    return f"{_MEM}{venture_id}"


def load_graph(venture_id: str, *, fetcher=None) -> Graph:
    data = _GRAPHS.get(_key(venture_id))
    try:
        stored = _mem().get(_key(venture_id))
        if stored:
            data = stored
    except Exception:
        pass
    return Graph.from_dict(data, fetcher=fetcher or _company_fetch)


def save_graph(venture_id: str, graph: Graph) -> None:
    payload = graph.to_dict()
    _GRAPHS[_key(venture_id)] = payload
    try:
        _mem()[_key(venture_id)] = payload
    except Exception:
        pass


def _company_fetch(url: str, options: dict[str, Any] | None = None) -> str:
    try:
        from app.research.web_fetch import fetch_page_markdown

        text = fetch_page_markdown(url)
        if text:
            return text
    except Exception:
        pass
    return fetch_url(url, options or {})


def ensure_scenario(venture_id: str) -> Graph:
    """Create the default watch graph once: websites → dedupe → digest, plus webhook + peaks."""
    graph = load_graph(venture_id)
    if graph.agents:
        return graph
    urls = _watch_urls(venture_id)
    digest = graph.add_agent(
        Agent(
            id="digest",
            type="DigestAgent",
            name="Founder digest",
            options={"retained_events": 0, "message": ""},
            schedule="every_1h",
        )
    )
    dedupe = graph.add_agent(
        Agent(
            id="dedupe",
            type="DeDuplicationAgent",
            name="Drop repeats",
            options={"property": "content_hash", "lookback": 200},
            schedule="never",
        )
    )
    graph.link(dedupe.id, digest.id)
    for i, url in enumerate(urls[:5]):
        aid = f"web-{i+1}"
        graph.add_agent(
            Agent(
                id=aid,
                type="WebsiteAgent",
                name=f"Watch {urlparse(url).netloc or url}",
                options={"url": url, "mode": "on_change", "type": "html"},
                schedule="every_12h",
            )
        )
        graph.link(aid, dedupe.id)
    feeds = [u for u in urls if "rss" in u.lower() or u.lower().endswith(".atom") or "/feed" in u.lower()]
    for i, url in enumerate(feeds[:2]):
        aid = f"rss-{i+1}"
        graph.add_agent(
            Agent(
                id=aid,
                type="RssAgent",
                name=f"Feed {urlparse(url).netloc}",
                options={"url": url, "remembered_id_count": 200, "max_events_per_run": 8},
                schedule="every_12h",
            )
        )
        graph.link(aid, dedupe.id)
    secret = secrets.token_hex(16)
    graph.add_agent(
        Agent(
            id="hook",
            type="WebhookAgent",
            name="Inbound webhook",
            options={"secret": secret, "verbs": "post", "payload_path": ".", "code": 201},
            schedule="never",
        )
    )
    graph.link("hook", digest.id)
    waitlist = graph.add_agent(
        Agent(
            id="waitlist",
            type="ManualEventAgent",
            name="Waitlist counts",
            schedule="never",
        )
    )
    peak = graph.add_agent(
        Agent(
            id="peaks",
            type="PeakDetectorAgent",
            name="Waitlist spikes",
            options={
                "value_path": "count",
                "group_by_path": "filter",
                "min_events": 4,
                "std_multiple": 3,
                "window_duration": 1_209_600,
                "peak_spacing": 172_800,
                "message": "Waitlist signups spiked",
            },
            schedule="never",
        )
    )
    graph.link(waitlist.id, peak.id)
    graph.link(peak.id, digest.id)
    save_graph(venture_id, graph)
    return graph


def _watch_urls(venture_id: str) -> list[str]:
    urls: list[str] = []
    brief = _mem().get(f"brief:{venture_id}") or {}
    research = brief.get("research") if isinstance(brief.get("research"), dict) else {}
    for row in list(research.get("competitors") or []) + list(research.get("sources") or []):
        if not isinstance(row, dict):
            continue
        url = str(row.get("url") or row.get("href") or "")
        if url.startswith("http"):
            urls.append(url)
    seen: set[str] = set()
    out: list[str] = []
    for url in urls:
        host = urlparse(url).netloc.lower()
        if not host or host in seen:
            continue
        seen.add(host)
        out.append(url)
    return out


def sweep_venture(venture_id: str, *, force: bool = False) -> dict[str, Any]:
    graph = ensure_scenario(venture_id)
    before = {e.id for e in graph.events}
    emitted = graph.check_due(force=force)
    _inject_waitlist(venture_id, graph)
    graph.propagate()
    digest = graph.check("digest") if "digest" in graph.agents else []
    save_graph(venture_id, graph)
    new_events = [e for e in graph.events if e.id not in before]
    notable = [
        e
        for e in new_events
        if graph.agents.get(e.agent_id) and graph.agents[e.agent_id].type in {"DigestAgent", "PeakDetectorAgent", "WebsiteAgent"}
    ]
    notified = 0
    if notable:
        notified = _notify(venture_id, notable, graph)
    return {
        "venture_id": venture_id,
        "checked": len(emitted),
        "new_events": len(new_events),
        "digest": len(digest),
        "notified": notified,
        "agents": len(graph.agents),
    }


def sweep_all() -> list[dict[str, Any]]:
    factory = db()
    with factory() as session:
        ids = list(
            session.scalars(select(Venture.id).where(Venture.archived_at.is_(None))).all()
        )
    results = []
    for vid in ids:
        try:
            results.append(sweep_venture(vid))
        except Exception as exc:  # noqa: BLE001
            results.append({"venture_id": vid, "error": str(exc)[:180]})
    return results


def _inject_waitlist(venture_id: str, graph: Graph) -> None:
    agent = graph.agents.get("waitlist")
    if not agent:
        return
    count = 0
    factory = db()
    with factory() as session:
        ven = session.get(Venture, venture_id)
        wid = ven.workspace_id if ven else None
        if wid:
            since = datetime.now(timezone.utc) - timedelta(days=1)
            count = int(
                session.scalar(
                    select(func.count())
                    .select_from(InterestSignal)
                    .where(InterestSignal.workspace_id == wid, InterestSignal.created_at >= since)
                )
                or 0
            )
    graph.create_event(agent, {"count": count, "filter": "waitlist"})


def _notify(venture_id: str, events: list, graph: Graph) -> int:
    try:
        from app.chat.store import append_message
        from app.org.characters import stamp_speaker
    except Exception:
        return 0
    lines = ["**Watch**, something changed out there."]
    for event in events[:8]:
        agent = graph.agents.get(event.agent_id)
        name = agent.name if agent else event.agent_id
        payload = event.payload or {}
        bit = payload.get("message") or payload.get("title") or payload.get("url") or ""
        lines.append(f"- {name}: {bit}"[:220])
    body = stamp_speaker("\n".join(lines), {"name": "Maya", "title": "Researcher", "role": "research"})
    key = hashlib.sha256(f"{venture_id}:{events[-1].id}".encode()).hexdigest()[:16]
    append_message(
        venture_id,
        role="assistant",
        kind="watch",
        body_md=body,
        idempotency_key=f"watch:{venture_id}:{key}",
    )
    try:
        from app.desktop_notices import enqueue

        enqueue(
            title="Company OS, needs a look",
            body="Watch picked up a change. Open the app to read the digest.",
            venture_id=venture_id,
            kind="watch",
        )
    except Exception:  # noqa: BLE001 — desktop queue must never fail the sweep
        pass
    return 1


def graph_view(venture_id: str) -> dict[str, Any]:
    graph = ensure_scenario(venture_id)
    return {
        "agents": [
            {
                "id": a.id,
                "type": a.type,
                "name": a.name,
                "schedule": a.schedule,
                "url": (a.options or {}).get("url"),
                "events": len(graph.events_for(a.id)),
            }
            for a in graph.agents.values()
        ],
        "links": [
            {"source": ln.source_id, "receiver": ln.receiver_id} for ln in graph.links
        ],
        "recent": [
            {
                "id": e.id,
                "agent_id": e.agent_id,
                "title": (e.payload or {}).get("title") or (e.payload or {}).get("message"),
                "url": (e.payload or {}).get("url"),
            }
            for e in list(graph.events)[-20:]
        ],
        "webhook": {
            "agent_id": "hook",
            "secret_set": bool((graph.agents.get("hook").options or {}).get("secret"))
            if graph.agents.get("hook")
            else False,
        },
    }


def webhook_secret(venture_id: str) -> str:
    graph = ensure_scenario(venture_id)
    hook = graph.agents.get("hook")
    if not hook:
        return ""
    return str(hook.options.get("secret") or "")


def receive_webhook(venture_id: str, secret: str, payload: dict[str, Any]) -> tuple[str, int]:
    graph = ensure_scenario(venture_id)
    msg, code = graph.webhook("hook", secret=secret, method="post", params=payload)
    if code < 300:
        graph.propagate()
        save_graph(venture_id, graph)
    return msg, code

from fastapi import HTTPException, Request

def watch_get_watch(venture_id: str):
    factory = db()
    with factory() as session:
        if not session.get(Venture, venture_id):
            raise HTTPException(404, "not_found")
    ensure_scenario(venture_id)
    return graph_view(venture_id)

def watch_run_watch(venture_id: str, payload = None):
    factory = db()
    with factory() as session:
        if not session.get(Venture, venture_id):
            raise HTTPException(404, "not_found")
    return sweep_venture(venture_id, force=True)

async def watch_inbound_watch_webhook(venture_id: str, secret: str, request: Request):
    factory = db()
    with factory() as session:
        if not session.get(Venture, venture_id):
            raise HTTPException(404, "not_found")
    expected = webhook_secret(venture_id)
    if not expected:
        raise HTTPException(404, "not_found")
    try:
        body = await request.json()
    except Exception:
        body = dict(request.query_params)
    if not isinstance(body, dict):
        body = {"value": body}
    msg, code = receive_webhook(venture_id, secret, body)
    if code >= 400:
        raise HTTPException(code, msg)
    return {"ok": True, "message": msg}
