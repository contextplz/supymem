"""Bounded free discovery. Callers own durable metering with ``estimate=0``.

Only fixed public API endpoints are fetched. Returned text is untrusted; dates,
counts and names are API observations, never inferred company facts. Failures
produce partial results with gaps, without retries or source text in logs.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

from connectors.http import request_json

from app.config import data_root
from app.knowledge.pmf import tokens
from app.memory.fuse import bm25_rank
from app.research import github_search as gh

HN_SEARCH = "https://hn.algolia.com/api/v1/search"
HN_ITEM = "https://hacker-news.firebaseio.com/v0/item"
YC_URL = "https://yc-oss.github.io/api/companies/all.json"
HN_CALL_CAP = 8
GITHUB_CALL_CAP = 4
YC_CALL_CAP = 1
HTTP_TIMEOUT_S = 10.0
MAX_RESULTS = 10
YC_CACHE_BYTES = 10_000_000
YC_MAX_AGE_S = 7 * 86400
_HN_USER = re.compile(r"[A-Za-z0-9_-]{1,80}")
_TAGS = {"story", "comment", "show_hn", "launch_hn", "ask_hn", "(show_hn,launch_hn)", "(story,comment,ask_hn)"}


class _PublicAPILog(logging.Filter):
    """httpx INFO URLs must not log query terms from a founder's idea."""
    prior_art_redaction = True
    def filter(self, record):
        message = record.getMessage()
        if any(host in message for host in ("hn.algolia.com/", "hacker-news.firebaseio.com/", "yc-oss.github.io/", "api.github.com/search/")):
            record.msg, record.args = "prior_art_public_api_request", ()
        return True


_http_log = logging.getLogger("httpx")
if not any(getattr(item, "prior_art_redaction", False) for item in _http_log.filters):
    _http_log.addFilter(_PublicAPILog())


def _result(provider: str) -> dict[str, Any]:
    return {"items": [], "provider": provider, "calls": 0, "incomplete": False, "gaps": []}


def _gap(result: dict, message: str, *, kind: str = "system_adapter") -> None:
    result["incomplete"] = True
    if message not in result["gaps"]:
        result["gaps"].append(message)
        result.setdefault("gap_items", []).append({"kind": kind, "text": message})


def _cap(value: int, ceiling: int) -> int:
    if type(value) is not int or value < 0:
        raise ValueError("prior-art call cap must be a nonnegative integer")
    return min(value, ceiling)


def _limit(value: int) -> int:
    if type(value) is not int or value < 1:
        raise ValueError("prior-art result limit must be a positive integer")
    return min(value, MAX_RESULTS)


def _query(value: str, ceiling: int = 240) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > ceiling or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError("prior-art query must be bounded plain text")
    return value.strip()


def _text(value: Any, limit: int = 2000) -> str:
    if not isinstance(value, str):
        return ""
    value = html.unescape(re.sub(r"<[^>]*>", " ", value[:8000]))
    return " ".join("".join(c for c in value if c.isprintable() or c.isspace()).split())[:limit]


def _url(value: Any) -> str | None:
    # Lazy import preserves deep's existing display URL guard without a cycle.
    from app.research.deep import safe_source_url

    return safe_source_url(value) if isinstance(value, str) else None


def _integer(value: Any) -> int | None:
    return value if type(value) is int and 0 <= value <= 2**63 - 1 else None


def _id(value: Any) -> int | None:
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,19}", value):
        value = int(value)
    return value if _integer(value) is not None and value > 0 else None


def _epoch(value: Any) -> str | None:
    if _integer(value) is None:
        return None
    try:
        return datetime.fromtimestamp(value, timezone.utc).isoformat()
    except (ValueError, OverflowError, OSError):
        return None


async def _request(result: dict, cap: int, url: str, **kwargs):
    if result["calls"] >= cap:
        _gap(result, "This source reached its request limit.", kind="system_request_limit")
        return None
    result["calls"] += 1  # Count attempted child HTTP calls, including timeouts.
    try:
        reply = await asyncio.wait_for(request_json("GET", url, timeout=HTTP_TIMEOUT_S, **kwargs), HTTP_TIMEOUT_S + 1)
        if reply.status != 200:
            _gap(result, "This source was temporarily unavailable.")
            return None
        return reply.body
    except Exception:
        _gap(result, "This source was temporarily unavailable.")
        return None


def _hn_author(value: Any) -> str | None:
    return f"https://news.ycombinator.com/user?id={quote(value)}" if isinstance(value, str) and _HN_USER.fullmatch(value) else None


def _company_url(value: Any) -> str | None:
    url = _url(value)
    return url if url and urlsplit(url).hostname != "news.ycombinator.com" else None


async def search_hn(query: str, *, tags: str = "(show_hn,launch_hn)", limit: int = 5, created_after: int | None = None, min_points: int | None = None, min_comments: int | None = None, max_calls: int = HN_CALL_CAP, by_date: bool = False) -> dict[str, Any]:
    """Search one Algolia page; launch names remain the source's title text."""
    query, limit, cap = _query(query), _limit(limit), _cap(max_calls, HN_CALL_CAP)
    if not isinstance(tags, str) or tags not in _TAGS:
        raise ValueError("unsupported Hacker News tags")
    params: dict[str, Any] = {"query": query, "tags": tags, "hitsPerPage": limit, "page": 0}
    filters = []
    for value, field in ((created_after, "created_at_i"), (min_points, "points"), (min_comments, "num_comments")):
        if value is not None:
            if _integer(value) is None:
                raise ValueError("Hacker News numeric filters require nonnegative integers")
            filters.append(f"{field}>{value}")
    if filters:
        params["numericFilters"] = ",".join(filters)
    result = _result("hn_algolia")
    body = await _request(result, cap, HN_SEARCH + ("_by_date" if by_date else ""), params=params)
    if not isinstance(body, dict) or not isinstance(body.get("hits"), list):
        if body is not None:
            _gap(result, "Hacker News returned an unreadable response.")
        return result
    seen = set()
    for row in body["hits"][:limit]:
        if not isinstance(row, dict) or (identifier := _id(row.get("objectID"))) is None or identifier in seen:
            continue
        title = _text(row.get("title") or (row.get("story_title") if by_date else None), 300)
        if by_date and not title:
            title = _text(row.get("comment_text"), 300)
        if not title or row.get("deleted") is True or row.get("dead") is True:
            continue
        seen.add(identifier)
        company_url = _company_url(row.get("url"))
        row_tags = row.get("_tags") if isinstance(row.get("_tags"), list) else []
        launch_type = next((tag for tag in ("launch_hn", "show_hn", "ask_hn") if tag in row_tags), "story")
        result["items"].append({
            "url": f"https://news.ycombinator.com/item?id={identifier}", "title": title,
            "snippet": _text(row.get("comment_text") if "comment" in row_tags else row.get("story_text")) or title, "provider": "hn_algolia", "source_kind": "hn_item",
            **({"content": (row.get("comment_text") if "comment" in row_tags else row.get("story_text"))[:8000]}
               if by_date and isinstance(row.get("comment_text") if "comment" in row_tags else row.get("story_text"), str) else {}),
            "prior_art": {"company_name": re.sub(r"^(?:Show|Launch|Ask) HN:\s*", "", title) if company_url else None,
                          "company_url": company_url, "hn_id": identifier, "launch_type": launch_type,
                          "date": _epoch(row.get("created_at_i")), "date_source": "created_at_i", "date_raw": _integer(row.get("created_at_i")),
                          "points": _integer(row.get("points")), "comments": _integer(row.get("num_comments")),
                          "author": row.get("author") if _hn_author(row.get("author")) else None,
                          "author_url": _hn_author(row.get("author")), "status": "unknown"},
        })
    return result


async def hn_comments(story_id: int, *, parent_company_url: str | None = None, parent_company_name: str | None = None, limit: int = 5, max_calls: int = HN_CALL_CAP) -> dict[str, Any]:
    """Fetch a ranked comment tree with a strict total cap, including the story."""
    identifier, limit, cap = _id(story_id), _limit(limit), _cap(max_calls, HN_CALL_CAP)
    if identifier is None:
        raise ValueError("Hacker News story id must be a positive integer")
    result = _result("hn_firebase")
    story = await _request(result, cap, f"{HN_ITEM}/{identifier}.json")
    if not isinstance(story, dict) or _id(story.get("id")) != identifier or story.get("type") != "story" or story.get("deleted") is True or story.get("dead") is True:
        if story is not None:
            _gap(result, "This Hacker News discussion was unavailable.")
        return result
    company_url = _company_url(story.get("url"))
    selected_url = _company_url(parent_company_url)
    matched_company = bool(company_url and selected_url and company_url.rstrip("/") == selected_url.rstrip("/"))
    name = (_text(parent_company_name, 300) or None) if matched_company else None
    if parent_company_url is not None and not matched_company:
        _gap(result, "This discussion could not be matched to the selected company.")
    children = story.get("kids") if isinstance(story.get("kids"), list) else []
    # ponytail: Firebase's ranked child order, not public comment scores; use a scored API if needed.
    pending = [(_id(child), identifier) for child in children[:min(limit, max(0, cap - result["calls"]))] if _id(child) is not None]
    seen = {identifier}
    while pending and len(result["items"]) < limit and result["calls"] < cap:
        child, parent = pending.pop(0)
        if child in seen:
            continue
        seen.add(child)
        row = await _request(result, cap, f"{HN_ITEM}/{child}.json")
        if not isinstance(row, dict) or _id(row.get("id")) != child or _id(row.get("parent")) != parent or row.get("type") != "comment" or row.get("deleted") is True or row.get("dead") is True:
            continue
        snippet = _text(row.get("text"))
        if snippet:
            result["items"].append({
                "url": f"https://news.ycombinator.com/item?id={child}", "title": f"Comment on {_text(story.get('title'), 250)}",
                "snippet": snippet, "provider": "hn_firebase", "source_kind": "hn_comment",
                "prior_art": {"company_name": name, "company_url": company_url, "hn_id": child, "story_id": identifier,
                              "date": _epoch(row.get("time")), "date_source": "time", "date_raw": _integer(row.get("time")),
                              "author": row.get("by") if _hn_author(row.get("by")) else None,
                              "author_url": _hn_author(row.get("by")), "status": "unknown"},
            })
        children = row.get("kids") if isinstance(row.get("kids"), list) else []
        slots = max(0, min(limit - len(result["items"]), cap - result["calls"]) - len(pending))
        pending.extend((_id(nested), child) for nested in children[:slots] if _id(nested) is not None)
    return result


def _yc_cache(path: Path, now: float) -> tuple[list[dict], float] | None:
    try:
        if path.stat().st_size > YC_CACHE_BYTES:
            return None
        document = json.loads(path.read_text())
        fetched = document["fetched_at"]
        rows = document["rows"]
        if type(fetched) not in (int, float) or not 0 <= now - fetched <= YC_MAX_AGE_S or not isinstance(rows, list) or len(rows) > 10000:
            return None
        return rows, fetched
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _save_yc(path: Path, rows: list, fetched: float) -> None:
    # ponytail: replica-local daily cache; shared object storage if fleet refresh load warrants it.
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        try:
            json.dump({"fetched_at": fetched, "rows": rows}, handle)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


async def search_yc(terms: list[str] | str, *, limit: int = 8, max_calls: int = YC_CALL_CAP) -> dict[str, Any]:
    """Daily cached BM25 matching, disabled until mirror rights are approved."""
    limit, cap = _limit(limit), _cap(max_calls, YC_CALL_CAP)
    query = _query(" ".join(terms) if isinstance(terms, list) and all(isinstance(term, str) for term in terms) else terms, 1000)
    result = _result("yc_oss")
    if os.environ.get("RESEARCH_YC_OSS_RIGHTS_APPROVED") != "1":
        return result
    path, now = data_root() / "research" / "yc-oss.json", datetime.now(timezone.utc).timestamp()
    cached = await asyncio.to_thread(_yc_cache, path, now)
    stale = cached is not None and now - cached[1] >= 86400
    if cached is None or stale:
        body = await _request(result, cap, YC_URL)
        if isinstance(body, list) and len(body) <= 10000 and all(isinstance(row, dict) for row in body) and len(json.dumps(body)) <= YC_CACHE_BYTES:
            cached, stale = (body, now), False
            try:
                await asyncio.to_thread(_save_yc, path, body, now)
            except OSError:
                _gap(result, "YC directory results could not be cached.")
        else:
            if body is not None:
                _gap(result, "YC directory returned an unreadable response.")
            if cached:
                _gap(result, "YC directory matching used an older cached snapshot.")
    if cached is None:
        return result
    rows, fetched = cached
    corpus = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        tags = row.get("tags") if isinstance(row.get("tags"), list) else []
        description = " ".join(_text(row.get(field)) for field in ("name", "one_liner", "long_description", "industry")) + " " + " ".join(_text(tag) for tag in tags[:20])
        corpus.append((index, " ".join(tokens(description))))
    ranked = await asyncio.to_thread(bm25_rank, " ".join(tokens(query)), corpus, limit=limit)
    for index in ranked:
        row = rows[index]
        name, company_url = _text(row.get("name"), 300), _url(row.get("website"))
        url = _url(row.get("url"))
        if not name or not url or not url.startswith("https://www.ycombinator.com/companies/"):
            continue
        raw_status = row.get("status")
        yc_status = raw_status if isinstance(raw_status, str) and raw_status in {"Active", "Inactive", "Acquired", "Public"} else None
        result["items"].append({
            "url": url, "title": name, "snippet": _text(row.get("one_liner")) or _text(row.get("long_description")), "provider": "yc_oss", "source_kind": "yc_company",
            "prior_art": {"company_name": name, "company_url": company_url, "yc_status": yc_status,
                          "status": {"Active": "live", "Inactive": "inactive", "Acquired": "acquired", "Public": "live"}.get(yc_status, "unknown") if not stale else "unknown",
                          "batch": _text(row.get("batch"), 80) or None, "team_size": _integer(row.get("team_size")),
                          "date": _epoch(row.get("launched_at")), "date_source": "launched_at", "date_raw": _integer(row.get("launched_at")),
                          "source_observed_at": datetime.fromtimestamp(fetched, timezone.utc).isoformat(), "cache_stale": stale},
        })
    return result


async def search_github(query: str, workspace_id: str, *, limit: int = 5, issue_repositories: int = 3, max_calls: int = GITHUB_CALL_CAP) -> dict[str, Any]:
    """Public repositories and most-reacted public issues using existing credentials."""
    query, limit, cap = _query(query), _limit(limit), _cap(max_calls, GITHUB_CALL_CAP)
    if gh._NONPUBLIC_QUERY.search(query) or not isinstance(workspace_id, str) or not workspace_id.strip() or len(workspace_id) > 128 or any(ord(c) < 33 or ord(c) == 127 for c in workspace_id):
        raise ValueError("GitHub prior-art search requires a public query and workspace")
    issue_repositories = _cap(issue_repositories, 3)
    result = _result("github")
    try:
        token, source = gh._credentials(workspace_id)
    except Exception:
        _gap(result, "The selected GitHub connection was unavailable.")
        return result
    result["credential_source"] = source
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": gh.API_VERSION}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body = await _request(result, cap, f"{gh.API}/search/repositories", headers=headers, params={"q": f"{query} is:public", "per_page": limit, "page": 1, "sort": "stars", "order": "desc"})
    if not isinstance(body, dict) or not isinstance(body.get("items"), list):
        if body is not None:
            _gap(result, "GitHub returned an unreadable response.")
        return result
    repositories, seen = [], set()
    for row in body["items"][:limit]:
        item = gh._item(row)
        if item is None or item["url"] in seen:
            continue
        seen.add(item["url"])
        meta = item["repository"]
        item["source_kind"] = "repository_metadata"
        item["prior_art"] = {"company_name": item["title"], "company_url": item["url"], "homepage": _url(row.get("homepage")),
                             "status": "unknown", "stars": meta["stars"], "archived": meta["archived"], "pushed_at": meta["pushed_at"],
                             "date": meta["pushed_at"], "date_source": "pushed_at", "date_raw": meta["pushed_at"],
                             "author": meta["full_name"].split("/")[0], "author_url": f"https://github.com/{meta['full_name'].split('/')[0]}"}
        result["items"].append(item)
        repositories.append(item)
    if body.get("incomplete_results") is True:
        _gap(result, "GitHub returned only part of the repository search.", kind="system_request_limit")
    for repository in repositories[:issue_repositories]:
        if result["calls"] >= cap:
            _gap(result, "GitHub issue research reached its request limit.", kind="system_request_limit")
            break
        full_name = repository["repository"]["full_name"]
        body = await _request(result, cap, f"{gh.API}/search/issues", headers=headers, params={"q": f"repo:{full_name} is:issue is:public", "per_page": 3, "page": 1, "sort": "reactions", "order": "desc"})
        if not isinstance(body, dict) or not isinstance(body.get("items"), list):
            if body is not None:
                _gap(result, "GitHub issue details were unreadable.")
            continue
        if body.get("incomplete_results") is True:
            _gap(result, "GitHub returned only part of the issue search.", kind="system_request_limit")
        for row in body["items"][:3]:
            if not isinstance(row, dict) or row.get("pull_request") is not None:
                continue
            number = _id(row.get("number"))
            expected = f"{repository['url']}/issues/{number}"
            if number is None or row.get("html_url") != expected or row.get("repository_url") != f"{gh.API}/repos/{full_name}":
                continue
            user = row.get("user") if isinstance(row.get("user"), dict) else {}
            login = user.get("login")
            author = f"https://github.com/{login}" if isinstance(login, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", login) else None
            reactions = row.get("reactions") if isinstance(row.get("reactions"), dict) else {}
            result["items"].append({
                "url": expected, "title": _text(row.get("title"), 300), "snippet": _text(row.get("body")), "provider": "github", "source_kind": "github_issue",
                "prior_art": {"company_name": repository["title"], "company_url": repository["url"], "reactions": _integer(reactions.get("total_count")),
                              "date": gh._timestamp(row.get("created_at")), "date_source": "created_at", "date_raw": gh._timestamp(row.get("created_at")),
                              "updated_at": gh._timestamp(row.get("updated_at")), "author": login if author else None, "author_url": author, "status": "unknown"},
            })
    return result
