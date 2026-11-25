# huginn_watch

Python port of the Huginn pieces Company OS actually needs.

Upstream: [huginn/huginn](https://github.com/huginn/huginn) (MIT). We do **not** run Rails,
Delayed Job, or the 70+ Ruby agents. This package is the extractable “other repository”:
the event kernel plus the agents that watch the web and fire when something changes.

## What we took (working Huginn logic, rewritten in Python)

| Huginn | Here | Why |
|--------|------|-----|
| `Agent#check` / `#receive` / `#create_event` | `Graph.check` / `propagate` | Directed event graph |
| `Agent.receive!` | `Graph.propagate` | Move new events along Links |
| WebsiteAgent `on_change` / `all` / uniqueness look-back | `agents/website.py` | Scrape a URL, emit only when the page changes |
| RssAgent `seen_ids` / `remembered_id_count` | `agents/rss.py` | New feed items only |
| WebhookAgent secret + verbs + payload_path | `agents/webhook.py` | Inbound events |
| TriggerAgent comparison types | `agents/trigger.py` | Filter the stream |
| ChangeDetectorAgent `last_property` | `agents/change.py` | Emit on field change |
| DeDuplicationAgent CRC32 lookback | `agents/dedupe.py` | Drop repeats |
| PeakDetectorAgent population mean+std | `agents/peak.py` | Spike in a count stream |
| DigestAgent queue + retained_events | `agents/digest.py` | Batch for the founder |

Left in `vendor/huginn` (too heavy / wrong shape): Twitter/Weibo/Tumblr, IMAP, Mechanical Turk,
PhantomJS, Liquid templating, Nokogiri CSS/XPath, Rufus + Delayed Job.

## Use without Company OS

```python
from huginn_watch import Graph, Agent
import huginn_watch  # registers agents

g = Graph(fetcher=lambda url, opts: "<title>Hi</title> hello")
g.add_agent(Agent(id="web", type="WebsiteAgent", name="Watch", options={"url": "https://example.com", "mode": "on_change"}, schedule="every_12h"))
g.check("web")
g.propagate()
```
