"""RSS/Atom parse — Huginn RssAgent event shape, stdlib XML instead of Feedjira."""

from __future__ import annotations

import hashlib
import re
import xml.etree.ElementTree as ET
from typing import Any

_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "dc": "http://purl.org/dc/elements/1.1/",
    "content": "http://purl.org/rss/1.0/modules/content/",
}


def parse_feed(body: str, *, feed_url: str = "") -> list[dict[str, Any]]:
    root = ET.fromstring(body.encode("utf-8") if isinstance(body, str) else body)
    tag = _local(root.tag)
    if tag == "feed" or root.tag.endswith("}feed"):
        return _atom_entries(root, feed_url)
    channel = root.find("channel")
    if channel is None:
        channel = root.find(".//channel")
    if channel is None:
        return []
    return _rss_items(channel, feed_url)


def _rss_items(channel: ET.Element, feed_url: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in channel.findall("item"):
        title = _text(item, "title")
        link = _text(item, "link")
        guid = _text(item, "guid") or link or title
        desc = _text(item, "description")
        content = _text(item, "{http://purl.org/rss/1.0/modules/content/}encoded") or desc
        out.append(
            {
                "id": _id(guid or title),
                "url": link,
                "urls": [link] if link else [],
                "title": title,
                "description": desc,
                "content": content,
                "date_published": _text(item, "pubDate"),
                "last_updated": _text(item, "pubDate"),
                "authors": [_text(item, "{http://purl.org/dc/elements/1.1/}creator") or _text(item, "author")],
                "feed_url": feed_url,
            }
        )
    return out


def _atom_entries(feed: ET.Element, feed_url: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for entry in feed.findall("atom:entry", _NS) or feed.findall("{http://www.w3.org/2005/Atom}entry"):
        title = _text(entry, "atom:title") or _text(entry, "{http://www.w3.org/2005/Atom}title")
        link_el = entry.find("atom:link", _NS) or entry.find("{http://www.w3.org/2005/Atom}link")
        link = ""
        if link_el is not None:
            link = link_el.attrib.get("href") or ""
        ident = _text(entry, "atom:id") or _text(entry, "{http://www.w3.org/2005/Atom}id") or link or title
        summary = _text(entry, "atom:summary") or _text(entry, "{http://www.w3.org/2005/Atom}summary")
        content = _text(entry, "atom:content") or _text(entry, "{http://www.w3.org/2005/Atom}content") or summary
        updated = _text(entry, "atom:updated") or _text(entry, "{http://www.w3.org/2005/Atom}updated")
        published = _text(entry, "atom:published") or _text(entry, "{http://www.w3.org/2005/Atom}published") or updated
        out.append(
            {
                "id": _id(ident),
                "url": link,
                "urls": [link] if link else [],
                "title": title,
                "description": summary,
                "content": content,
                "date_published": published,
                "last_updated": updated,
                "authors": [],
                "feed_url": feed_url,
            }
        )
    return out


def _text(el: ET.Element, path: str) -> str:
    found = el.find(path)
    if found is None and ":" not in path and not path.startswith("{"):
        found = el.find(path)
    if found is None:
        return ""
    return (found.text or "").strip()


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _id(raw: str) -> str:
    text = (raw or "").strip()
    if text:
        return hashlib.md5(text.encode()).hexdigest()
    return hashlib.md5(b"empty").hexdigest()


def strip_tags(html: str) -> str:
    return re.sub(r"<[^>]+>", " ", html or "")
