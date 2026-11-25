"""Huginn watch kernel — event graph, on_change, RSS ids, peaks, triggers."""

from __future__ import annotations

import huginn_watch  # registers agents
from huginn_watch import Agent, Graph


def test_website_on_change_emits_once():
    pages = {"https://example.com/x": "<title>One</title> hello world"}

    def fetch(url, opts):
        return pages[url]

    g = Graph(fetcher=fetch)
    g.add_agent(
        Agent(
            id="web",
            type="WebsiteAgent",
            name="Page",
            options={"url": "https://example.com/x", "mode": "on_change"},
            schedule="every_1h",
        )
    )
    first = g.check("web")
    assert len(first) == 1
    assert first[0].payload["title"] == "One"
    second = g.check("web")
    assert second == []
    pages["https://example.com/x"] = "<title>Two</title> goodbye"
    third = g.check("web")
    assert len(third) == 1
    assert third[0].payload["title"] == "Two"


def test_rss_remembers_ids():
    feed = """<?xml version="1.0"?>
    <rss version="2.0"><channel>
      <title>News</title>
      <item><title>A</title><link>https://ex.com/a</link><guid>a</guid></item>
      <item><title>B</title><link>https://ex.com/b</link><guid>b</guid></item>
    </channel></rss>"""
    g = Graph(fetcher=lambda url, opts: feed)
    g.add_agent(
        Agent(
            id="rss",
            type="RssAgent",
            name="Feed",
            options={"url": "https://ex.com/rss", "remembered_id_count": 50},
            schedule="every_1d",
        )
    )
    first = g.check("rss")
    assert len(first) == 2
    again = g.check("rss")
    assert again == []


def test_trigger_and_digest_along_links():
    g = Graph()
    src = g.add_agent(Agent(id="src", type="ManualEventAgent", name="Src", schedule="never"))
    trig = g.add_agent(
        Agent(
            id="trig",
            type="TriggerAgent",
            name="Match",
            options={
                "rules": [{"type": "regex", "path": "text", "value": "spike"}],
                "keep_event": True,
                "message": "matched",
            },
            schedule="never",
        )
    )
    digest = g.add_agent(Agent(id="digest", type="DigestAgent", name="Digest", schedule="every_1h"))
    g.link("src", "trig")
    g.link("trig", "digest")
    g.create_event(src, {"text": "nothing"})
    g.create_event(src, {"text": "a spike in signups"})
    g.propagate()
    assert [e.payload.get("message") for e in g.events_for("trig")] == ["matched"]
    g.check("digest")
    out = g.events_for("digest")
    assert len(out) == 1
    assert out[0].payload["events"]


def test_dedupe_and_change_detector():
    g = Graph()
    src = g.add_agent(Agent(id="src", type="ManualEventAgent", name="Src"))
    change = g.add_agent(
        Agent(id="chg", type="ChangeDetectorAgent", name="Chg", options={"property": "price"})
    )
    dedupe = g.add_agent(
        Agent(id="dup", type="DeDuplicationAgent", name="Dup", options={"property": "sku", "lookback": 10})
    )
    g.link("src", "chg")
    g.link("chg", "dup")
    g.create_event(src, {"price": 10, "sku": "a"})
    g.propagate()
    g.create_event(src, {"price": 10, "sku": "a"})
    g.propagate()
    g.create_event(src, {"price": 12, "sku": "a"})
    g.propagate()
    assert len(g.events_for("chg")) == 2
    assert len(g.events_for("dup")) == 1


def test_peak_detector_fires_on_std_spike():
    clock = {"t": 1_000_000.0}

    def now():
        clock["t"] += 86_400
        return clock["t"]

    g = Graph(now=now)
    src = g.add_agent(Agent(id="src", type="ManualEventAgent", name="Src"))
    peak = g.add_agent(
        Agent(
            id="peak",
            type="PeakDetectorAgent",
            name="Peak",
            options={
                "value_path": "count",
                "group_by_path": "filter",
                "min_events": 4,
                "std_multiple": 2,
                "window_duration": 1_209_600,
                "peak_spacing": 86_400,
                "message": "spiked",
            },
        )
    )
    g.link("src", "peak")
    for n in (1, 1, 1, 1, 1, 20):
        g.create_event(src, {"count": n, "filter": "waitlist"})
        g.propagate()
    peaks = g.events_for("peak")
    assert peaks
    assert peaks[0].payload["peak"] == 20
    assert peaks[0].payload["message"] == "spiked"


def test_webhook_secret():
    g = Graph()
    g.add_agent(
        Agent(
            id="hook",
            type="WebhookAgent",
            name="Hook",
            options={"secret": "s3cret", "verbs": "post"},
        )
    )
    msg, code = g.webhook("hook", secret="nope", method="post", params={"ok": 1})
    assert code == 401
    msg, code = g.webhook("hook", secret="s3cret", method="post", params={"ok": 1})
    assert code == 201
    assert g.events_for("hook")[0].payload["ok"] == 1
