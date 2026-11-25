"""Bounded public-company contact discovery using Scrapy and the research guard.

Only published, same-domain addresses are contacts. Publication does not establish
mailbox deliverability, a person's identity, consent, or permission to send.
"""
from __future__ import annotations

import ipaddress
import json
import os
import subprocess
import sys
from pathlib import Path
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from itertools import islice
from typing import Any, Iterable
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx
from core.research_web.guard import get_checked

MAX_SITES = 20
MAX_PAGES = 3
MAX_BYTES = 512 * 1024
MAX_SECONDS = 45
CONTACT_PATHS = ("/", "/contact", "/about")
EMAIL_RE = re.compile(r"(?<![A-Za-z0-9._%+-])([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})(?![A-Za-z0-9._%+-])")
_SKIP_LOCALS = {"noreply", "no-reply", "donotreply", "mailer-daemon", "postmaster", "abuse", "example", "email", "sentry", "wix"}


def _host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower().removeprefix("www.")
    except ValueError:
        return ""


def _seed_url(raw: str) -> str:
    """Syntax boundary; the research guard vets and pins DNS on every fetch."""
    raw = str(raw or "").strip()
    if not raw or len(raw) > 2048 or any(c.isspace() for c in raw) or "\\" in raw:
        return ""
    if "://" not in raw:
        raw = "https://" + raw
    try:
        p = urlsplit(raw)
        if p.scheme not in {"http", "https"} or not p.hostname or p.username or p.password or p.port not in {None, 80, 443}:
            return ""
        host = p.hostname.lower().rstrip(".")
        if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
            return ""
        try:
            addr = ipaddress.ip_address(host)
        except ValueError:
            host = host.encode("idna").decode("ascii")
            if "." not in host or not re.fullmatch(r"[a-z0-9.-]+", host):
                return ""
        else:
            if not addr.is_global:
                return ""
            host = f"[{host}]" if addr.version == 6 else host
        return urlunsplit((p.scheme, host + (f":{p.port}" if p.port else ""), "/", "", ""))
    except (ValueError, UnicodeError):
        return ""


class _BoundedClient:
    """Adapter for unchanged guard.get_checked: no proxy, bounded streaming body.

    The guard rewrites DNS names to vetted literal addresses; refusing a name
    here also closes an empty/unusable DNS-answer fallback in its upstream API.
    """
    def __init__(self, client: httpx.Client, deadline: float, *, allow_json: bool = False):
        self.client, self.deadline, self.allow_json = client, deadline, allow_json

    def get(self, url: str, *, headers=None, extensions=None):
        p = urlsplit(url)
        if p.username or p.password or p.port not in {None, 80, 443}:
            raise PermissionError("unsupported_target")
        try:
            addr = ipaddress.ip_address(p.hostname or "")
        except ValueError as exc:
            raise PermissionError("unpinned_target") from exc
        if not addr.is_global:
            raise PermissionError("non_public_target")
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("crawl_deadline")
        with self.client.stream("GET", url, headers=headers, extensions=extensions,
                                timeout=min(5.0, remaining)) as response:
            if response.is_redirect:
                return httpx.Response(response.status_code, headers=response.headers, request=response.request)
            response.raise_for_status()
            if response.headers.get("content-encoding", "identity").lower() not in {"", "identity"}:
                raise ValueError("unsupported_encoding")
            types = ("text/html", "text/plain", "application/xhtml+xml") + (("application/json",) if self.allow_json else ())
            if not any(content_type in response.headers.get("content-type", "").lower() for content_type in types):
                raise ValueError("unsupported_content_type")
            if int(response.headers.get("content-length", "0")) > MAX_BYTES:
                raise ValueError("response_too_large")
            chunks, size = [], 0
            for chunk in response.iter_raw(chunk_size=16 * 1024):
                size += len(chunk)
                if size > MAX_BYTES or time.monotonic() >= self.deadline:
                    raise ValueError("response_limit")
                chunks.append(chunk)
            return httpx.Response(response.status_code, headers=response.headers,
                                  content=b"".join(chunks), request=response.request)


def _fetch(url: str, timeout: float = 10.0, *, deadline: float | None = None, allow_missing: bool = False, allow_json: bool = False) -> str | None:
    end = min(deadline or float("inf"), time.monotonic() + max(0.1, min(timeout, 10)))
    if not _seed_url(url):
        return None
    try:
        with httpx.Client(follow_redirects=False, trust_env=False, timeout=5,
                          headers={"User-Agent": "SupymemResearch/1.0", "Accept": "text/html,text/plain", "Accept-Encoding": "identity"}) as client:
            response = get_checked(_BoundedClient(client, end, allow_json=allow_json), url, max_redirects=3)
            # Company contacts must not come from a redirect onto another company.
            if _host(response.extensions.get("logical_url", url)) != _host(url):
                return None
            return response.text
    except httpx.HTTPStatusError as exc:
        return "" if allow_missing and exc.response.status_code in {404, 410} else None
    except (httpx.HTTPError, PermissionError, ValueError, RuntimeError, OSError):
        return None


def _visible(html: str) -> tuple[str, list[str]]:
    from scrapy.selector import Selector
    sel = Selector(text=html)
    for el in sel.xpath("//script|//style|//noscript|//svg"):
        el.root.getparent().remove(el.root)
    text = " ".join(sel.xpath("//text()").getall())
    mailto = sel.css('a[href^="mailto:"]::attr(href)').getall()
    return text, mailto


def _extract(html: str, *, host: str, org: str, industry: str, base: str) -> list[dict[str, Any]]:
    text, mailto = _visible(html)
    rows, seen = [], set()
    for email in EMAIL_RE.findall(text + " " + " ".join(mailto)):
        email = email.lower().rstrip(".,;)")
        local, _, domain = email.partition("@")
        if local.split("+")[0] in _SKIP_LOCALS or not (domain == host or domain.endswith("." + host)) or email in seen:
            continue
        seen.add(email)
        rows.append({"email": email, "first_name": "", "last_name": "", "title": "Company contact",
                     "organization": org or host, "industry": "", "requested_industry": industry,
                     "employees": "", "linkedin_url": "", "city": "", "country": "",
                     "website": _seed_url(base), "source": "public_web", "source_url": base,
                     "email_confidence": "unverified", "email_tier": "unverified", "email_score": "", "confidence_pct": "",
                     "evidence": {"kind": "published_company_contact", "url": base,
                                  "observed_at": datetime.now(timezone.utc).isoformat()}})
    return rows


def _matches(text: str, seed: dict) -> bool:
    text = " ".join(text.casefold().split())
    terms = seed.get("target_terms") or []
    region = str(seed.get("target_region") or "").casefold()
    return all(re.search(r"\b" + re.escape(t) + r"(?:s|es)?\b", text) for t in terms) and (not region or region in text)


def _crawl_one(seed: dict[str, Any], max_per_site: int, deadline: float) -> list[dict[str, Any]]:
    base = _seed_url(seed.get("website") or seed.get("domain") or "")
    if not base:
        return []
    robots = None
    if seed.get("respect_robots") is True:
        from urllib.robotparser import RobotFileParser
        policy = _fetch(urljoin(base, "/robots.txt"), deadline=deadline, allow_missing=True)
        if policy is None:
            return []  # An unavailable policy does not grant crawling permission.
        robots = RobotFileParser()
        robots.parse(policy.splitlines())
    pages, texts = [], []
    for path in CONTACT_PATHS[:max_per_site]:
        if time.monotonic() >= deadline:
            break
        url = urljoin(base, path)
        if robots is not None and not robots.can_fetch("SupymemResearch", url):
            continue
        html = _fetch(url, deadline=deadline)
        if html:
            visible, _ = _visible(html)
            texts.append(visible)
            pages.append((url, html))
    if not _matches(" ".join(texts), seed):
        return []
    found = []
    for url, html in pages:
        found.extend(_extract(html, host=_host(base), org=str(seed.get("organization") or _host(base)),
                              industry=str(seed.get("industry") or ""), base=url))
    return found[:5]


def crawl_sites(seeds: Iterable[dict[str, Any]], *, max_per_site: int = 3, timeout_sec: float = MAX_SECONDS) -> list[dict[str, Any]]:
    seed_list = [s for s in islice(seeds, MAX_SITES) if isinstance(s, dict) and s]
    if not seed_list:
        return []
    deadline = time.monotonic() + max(0.1, min(float(timeout_sec), MAX_SECONDS))
    pages = max(1, min(int(max_per_site), MAX_PAGES))
    # Input-order aggregation gives deterministic count truncation, independent of network speed.
    with ThreadPoolExecutor(max_workers=min(4, len(seed_list))) as pool:
        futures = [pool.submit(_crawl_one, s, pages, deadline) for s in seed_list]
        rows = [row for f in futures for row in f.result()]
    seen, result = set(), []
    for row in rows:
        if row["email"] not in seen:
            seen.add(row["email"])
            result.append(row)
    return result


def crawl_sites_subprocess(seeds: list[dict[str, Any]], *, max_per_site: int = 3, timeout_sec: float = MAX_SECONDS) -> list[dict[str, Any]]:
    """Kill the owned crawler on deadline, including stuck platform DNS calls.

    Only this packaged Python module runs. The child receives no provider keys,
    account secrets, founder code or arbitrary commands. A timeout is terminal
    for this request; nothing is automatically replayed.
    """
    seconds = max(0.1, min(float(timeout_sec), MAX_SECONDS))
    payload = json.dumps({"seeds": list(islice(seeds, MAX_SITES)), "pages": max(1, min(int(max_per_site), MAX_PAGES)),
                          "timeout_sec": seconds}, ensure_ascii=False)
    if len(payload.encode()) > 128 * 1024:
        raise ValueError("crawl_input_limit")
    env = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT", "LANG", "LC_ALL") if key in os.environ}
    paths = [str(Path(__file__).resolve().parents[2]), *os.environ.get("PYTHONPATH", "").split(os.pathsep)]
    env["PYTHONPATH"] = os.pathsep.join(dict.fromkeys(path for path in paths if path))
    try:
        result = subprocess.run([sys.executable, "-m", "app.gtm.scrapy_leads"], input=payload,
                                text=True, capture_output=True, timeout=seconds, check=False, env=env,
                                cwd=Path(__file__).resolve().parents[2])
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("crawl_deadline") from exc
    if result.returncode or len(result.stdout.encode()) > 1024 * 1024:
        raise RuntimeError("crawler_process_failed")
    rows = json.loads(result.stdout)
    if not isinstance(rows, list) or len(rows) > MAX_SITES * 5 or not all(isinstance(r, dict) for r in rows):
        raise RuntimeError("crawler_invalid_result")
    return rows


def status() -> dict[str, Any]:
    try:
        import scrapy
        return {"ready": True, "version": scrapy.__version__, "mode": "selectors+guarded_http", "network_verified": False}
    except ImportError:
        return {"ready": False, "mode": "selectors+guarded_http", "error": "crawler_dependency_unavailable", "network_verified": False}


if __name__ == "__main__":
    raw = sys.stdin.buffer.read(128 * 1024 + 1)
    if len(raw) > 128 * 1024:
        raise SystemExit(2)
    request = json.loads(raw)
    print(json.dumps(crawl_sites(request["seeds"], max_per_site=request["pages"], timeout_sec=request["timeout_sec"])))
