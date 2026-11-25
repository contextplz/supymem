"""Facts-only internal catalogue index. Descriptions are never public evidence."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
from html.parser import HTMLParser
import json
import logging
import re
from threading import Event, Lock
import time
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser
import xml.etree.ElementTree as ET

import httpx
from sqlalchemy import select, text

from app.db.supymem_models import LootDropEntry
from app.memory.fuse import bm25_rank
from app.state import db
from core.clock import utcnow

ROOT = "https://www.loot-drop.io"
USER_AGENT = "SupymemResearchBot/1.0 (+https://supymem.com)"
log = logging.getLogger(__name__)
_LOCK = Lock()
_MAX_BYTES = 1_000_000


def entry_url(raw: str) -> str | None:
    try:
        parsed = urlsplit(raw)
        if (parsed.scheme == "https" and parsed.hostname in {"www.loot-drop.io", "loot-drop.io"}
                and not parsed.username and not parsed.password and parsed.port in {None, 443}
                and re.fullmatch(r"/startup/\d+-[a-z0-9-]+/?", parsed.path)):
            return ROOT + parsed.path.rstrip("/")
    except (TypeError, ValueError):
        pass
    return None


@contextmanager
def request_slot():
    with _LOCK:
        with db()() as session:
            if session.get_bind().dialect.name == "postgresql":
                session.execute(text("SET LOCAL statement_timeout = '30000'"))
                session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended('loot-drop-http', 0))"))
                time.sleep(2)
                yield
            else:
                import fcntl
                from app.config import data_root
                directory = data_root() / "research"
                directory.mkdir(parents=True, exist_ok=True)
                with (directory / "loot-drop-http.lock").open("a") as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX)
                    time.sleep(2)
                    yield


class Reader:
    def __init__(self, stopped: Event | None = None):
        from app.core.local_mode import is_local
        from app.research.local_providers import READER_UNAVAILABLE, scrape_provider

        self.local_reader = scrape_provider() if is_local() else None
        if is_local() and self.local_reader is None:
            raise ValueError(READER_UNAVAILABLE)
        self.stopped = stopped
        # Brotli decoding is unreliable in this httpx build; plain gzip is enough for small pages.
        self.client = None if self.local_reader else httpx.Client(headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, deflate"}, timeout=20, follow_redirects=False)
        self.robots = RobotFileParser()

    def __enter__(self):
        try:
            response = self.get(ROOT + "/robots.txt")
            self.robots.parse(response.text.splitlines())
            return self
        except BaseException:
            if self.client:
                self.client.close()
            raise

    def __exit__(self, *args):
        if self.client:
            self.client.close()

    def get(self, url: str, *, etag: str | None = None, modified: str | None = None):
        if url not in {ROOT + "/robots.txt", ROOT + "/sitemap.xml"} and entry_url(url) != url:
            raise ValueError("loot_drop_path_disallowed")
        if url != ROOT + "/robots.txt" and not self.robots.can_fetch(USER_AGENT, url):
            raise ValueError("loot_drop_robots_disallowed")
        if self.local_reader:
            from app.credits import metered
            from app.tenancy import current_or_none

            async def fetch():
                scope = current_or_none()
                async with httpx.AsyncClient(follow_redirects=False, trust_env=False) as client:
                    response = await metered.call(lambda: self.local_reader._firecrawl_post(client, "scrape",
                        {"url": url, "formats": ["rawHtml"], "onlyMainContent": False}),
                        purpose="research.fetch", provider=self.local_reader.name, payer="founder",
                        estimate_micro=0 if self.local_reader.name == "duckduckgo" else metered.price_micro("firecrawl_scrape"),
                        workspace_id=scope.workspace_id if scope else None, venture_id=scope.venture_id if scope else None)
                data = response.json().get("data")
                metadata = data.get("metadata") if isinstance(data, dict) else None
                text = data.get("rawHtml") if isinstance(data, dict) else None
                if (not isinstance(metadata, dict) or not isinstance(text, str) or not text.strip() or len(text.encode()) > _MAX_BYTES
                        or metadata.get("sourceURL") != url or metadata.get("url", url) != url
                        or metadata.get("statusCode", 200) != 200):
                    raise ValueError("You could not read that source. Check your self-hosted reader in Settings, then retry.")
                return httpx.Response(200, text=text, request=httpx.Request("GET", url))

            with request_slot():
                if self.stopped and self.stopped.is_set():
                    raise ValueError("loot_drop_stopped")
                return asyncio.run(fetch())
        headers = {}
        if etag:
            headers["If-None-Match"] = etag
        if modified:
            headers["If-Modified-Since"] = modified
        with request_slot():
            if self.stopped and self.stopped.is_set():
                raise ValueError("loot_drop_stopped")
            with self.client.stream("GET", url, headers=headers) as response:
                if response.status_code != 304:
                    response.raise_for_status()
                if response.status_code in {301, 302, 303, 307, 308}:
                    raise ValueError("loot_drop_redirect_disallowed")
                chunks, size = [], 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > _MAX_BYTES:
                        raise ValueError("loot_drop_response_too_large")
                    chunks.append(chunk)
                # iter_bytes() already decoded the body; drop encoding headers so it isn't decoded twice.
                headers = {k: v for k, v in response.headers.items() if k.lower() not in {"content-encoding", "content-length"}}
                return httpx.Response(response.status_code, headers=headers,
                                      content=b"".join(chunks), request=response.request)


class Page(HTMLParser):
    def __init__(self, html: str):
        super().__init__(convert_charrefs=True)
        self.scripts, self.words = [], []
        self.script, self.hidden = None, 0
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "nav", "footer"}:
            self.hidden += 1
        if tag == "script" and dict(attrs).get("type", "").lower() == "application/ld+json":
            self.script = []

    def handle_endtag(self, tag):
        if tag == "script" and self.script is not None:
            self.scripts.append("".join(self.script))
            self.script = None
        if tag in {"script", "style", "nav", "footer"}:
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data):
        if self.script is not None:
            self.script.append(data)
        elif not self.hidden and data.strip():
            self.words.append(data.strip())


def _articles(value):
    if isinstance(value, list):
        for child in value:
            yield from _articles(child)
    elif isinstance(value, dict):
        if isinstance(value.get("headline"), str) and isinstance(value.get("description"), str):
            yield value
        if "@graph" in value:
            yield from _articles(value["@graph"])


def parse_page(url: str, html: str) -> dict | None:
    page = Page(html)
    articles = []
    for script in page.scripts:
        try:
            articles.extend(_articles(json.loads(script)))
        except (ValueError, RecursionError):
            continue
    if not articles:
        return None
    article = articles[0]
    name = re.split(r"\s+[-–—]\s+Why\b", article["headline"], flags=re.I)[0].strip()
    description = " ".join(article["description"].split())
    if not name or len(name) > 80 or not description or len(description) > 1200 or entry_url(url) is None:
        return None
    body = " ".join(page.words)
    full = " ".join([name, description, body])
    years = re.search(r"\b((?:19|20)\d{2})\s*[-–—]\s*((?:19|20)\d{2})\b", full)
    founded = re.search(r"\bFounded\s*:?\s+((?:19|20)\d{2})\b", full, re.I)
    ended = re.search(r"\b(?:Ended|Closed|Shut down)\s*:?\s+((?:19|20)\d{2})\b", full, re.I)
    cash = re.search(r"(?:cash\s+burned|burn(?:ed|ing)(?:\s+through)?|lost)\s*:?\s*"
                     r"([$€£₹]\s*[\d,.]+\s*(?:billion|million|[MBK])?\b)", full, re.I)
    category = article.get("articleSection")
    if not isinstance(category, str):
        sector = re.search(r"\b(?:Sector|Category)\s*:?\s+([\w &/-]{1,100}?)(?=\s+(?:Founded|Ended|Cash|Why)\b|$)", body, re.I)
        category = sector.group(1) if sector else None
    return {"url": entry_url(url), "name": name, "description": description,
            "founded": founded.group(1) if founded else years.group(1) if years else None,
            "ended": ended.group(1) if ended else years.group(2) if years else None,
            "cash_burned": cash.group(1).strip()[:60] if cash else None,
            "category": category[:120] if category else None,
            "text": full[:10000]}


def sitemap_urls(xml: str) -> list[str]:
    document = ET.fromstring(xml)
    return list(dict.fromkeys(url for node in document.iter() if node.tag.rsplit("}", 1)[-1] == "loc"
                             if (url := entry_url((node.text or "").strip()))))[:2000]


def entries() -> list[dict]:
    # ponytail: bounded 2,000-entry BM25 scan, paginate/rank in SQL if the catalogue grows beyond that.
    with db()() as session:
        rows = session.scalars(select(LootDropEntry).order_by(LootDropEntry.url).limit(2000)).all()
        return [{key: getattr(row, key) for key in ("url", "name", "description", "founded", "ended", "cash_burned", "category")}
                for row in rows]


def rank(snapshot: dict, selected: dict | None, rows: list[dict]) -> list[dict]:
    from app.research.deep import _QUERY_FILLER
    from app.research.prior_art.plan import fallback
    selected = selected or fallback(snapshot)
    query = " ".join([str(snapshot.get("problem") or ""), str(snapshot.get("customer") or ""),
                      " ".join(selected["category_terms"])])
    query = " ".join(word for word in re.findall(r"[^\W_]+", query.lower()) if word not in _QUERY_FILLER)
    corpus = [(index, " ".join(str(row.get(key) or "") for key in ("name", "description", "category")))
              for index, row in enumerate(rows) if entry_url(str(row.get("url") or ""))]
    return [rows[index] for index in bm25_rank(query, corpus, limit=5)]


def refresh(*, limit: int = 2000) -> dict:
    if type(limit) is not int or not 1 <= limit <= 2000:
        raise ValueError("loot_drop_seed_limit_invalid")
    stats = {"updated": 0, "unchanged": 0, "skipped": 0}
    with Reader() as reader:
        urls = sitemap_urls(reader.get(ROOT + "/sitemap.xml").text)[:limit]
        for url in urls:
            try:
                with db()() as session:
                    saved = session.get(LootDropEntry, url)
                    etag, modified = (saved.etag, saved.last_modified) if saved else (None, None)
                response = reader.get(url, etag=etag, modified=modified)
                if response.status_code == 304:
                    with db()() as session:
                        saved = session.get(LootDropEntry, url, with_for_update=True)
                        if saved:
                            saved.checked_at = utcnow()
                            session.commit()
                    stats["unchanged"] += 1
                    continue
                parsed = parse_page(url, response.text)
                if not parsed:
                    stats["skipped"] += 1
                    continue
                with db()() as session:
                    saved = session.get(LootDropEntry, url, with_for_update=True)
                    if saved is None:
                        saved = LootDropEntry(url=url)
                        session.add(saved)
                    for key in ("name", "description", "founded", "ended", "cash_burned", "category"):
                        setattr(saved, key, parsed[key])
                    saved.etag = response.headers.get("etag")
                    saved.last_modified = response.headers.get("last-modified")
                    saved.checked_at = utcnow()
                    session.commit()
                stats["updated"] += 1
            except Exception as exc:
                stats["skipped"] += 1
                log.info("loot_drop_index_skipped", extra={"reason": type(exc).__name__})
                raise
    return stats


def run_refresh(payload: dict) -> None:
    refresh()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=2000)
    args = parser.parse_args()
    print(json.dumps(refresh(limit=args.limit)))


if __name__ == "__main__":
    main()
