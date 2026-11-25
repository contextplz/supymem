"""Bounded public-page reads. feature_flags.reader_v2 is off by default."""

from __future__ import annotations

import asyncio
import codecs
import ipaddress
import json
import os
import re
import subprocess
import sys
import threading
import time
import zlib
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import httpx

from core.research_web.guard import MAX_REDIRECTS, get_checked

MAX_BYTES = 8 * 1024 * 1024
MAX_LINKS = 200
CHUNK_CHARS = 4000
TIMEOUT_S = 40.0
_ACTIVE: dict[str, int] = {}
_LOCK = threading.Condition()
_SLOTS = threading.BoundedSemaphore(32)
_HIDDEN_STYLE = re.compile(r"(?:display\s*:\s*none|visibility\s*:\s*hidden)", re.I)


class ReaderError(ValueError):
    def __init__(self, message, *, response: httpx.Response | None = None):
        super().__init__(message)
        self.response = response


def enabled() -> bool:
    from app.core.local_mode import is_local
    if is_local():
        try:
            from app.core.local_capabilities import enabled as capability_enabled
        except ImportError:
            return True
        return capability_enabled("research")
    from app.credits.policy import get_policy
    flags = get_policy("feature_flags")
    return isinstance(flags, dict) and flags.get("reader_v2") is True


def max_chars() -> int:
    return min(200_000, max(1, int(os.getenv("READER_MAX_CHARS", "40000"))))


def _target(url: str) -> None:
    from app.chat.link_context import _OWN

    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower().rstrip(".")
        if (parts.scheme not in {"http", "https"} or not host or parts.username or parts.password
                or parts.port == 0 or len(url) > 2000 or any(ord(char) < 32 for char in url)
                or any(host == own or host.endswith("." + own) for own in _OWN)):
            raise ValueError
    except ValueError as exc:
        raise ReaderError("url_not_allowed") from exc


@contextmanager
def _slot(founder: str, *, deadline: float | None = None):
    limit = min(8, max(1, int(os.getenv("READER_FOUNDER_CONCURRENCY", "2"))))
    remaining = max(0, deadline - time.monotonic()) if deadline is not None else 0
    if not _SLOTS.acquire(timeout=remaining):
        raise ReaderError("reader_busy")
    acquired = False
    try:
        with _LOCK:
            remaining = max(0, deadline - time.monotonic()) if deadline is not None else 0
            if not _LOCK.wait_for(lambda: _ACTIVE.get(founder, 0) < limit, timeout=remaining):
                raise ReaderError("reader_busy")
            _ACTIVE[founder] = _ACTIVE.get(founder, 0) + 1
            acquired = True
        yield
    finally:
        if acquired:
            with _LOCK:
                _ACTIVE[founder] -= 1
                if not _ACTIVE[founder]:
                    del _ACTIVE[founder]
                _LOCK.notify_all()
        _SLOTS.release()


class _Body:
    def __init__(self, encoding: str):
        self.data = bytearray()
        self.received = 0
        self.encoding = encoding.strip().lower()
        self.decoder = None
        self.decode_errors = (zlib.error,)
        if self.encoding == "gzip":
            self.decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
        elif self.encoding == "br":
            import brotli

            self.decoder = brotli.Decompressor()
            self.decode_errors += (brotli.error,)
        elif self.encoding not in {"", "identity"}:
            raise ReaderError("unsupported_encoding")

    def add(self, raw: bytes) -> None:
        self.received += len(raw)
        if self.received > MAX_BYTES:
            raise ReaderError("page_too_large")
        try:
            self.data.extend(self._decode(raw))
        except self.decode_errors as exc:
            raise ReaderError("invalid_encoding") from exc
        if len(self.data) > MAX_BYTES:
            raise ReaderError("page_too_large")

    def _decode(self, raw: bytes) -> bytes:
        remaining = MAX_BYTES - len(self.data) + 1
        if self.encoding == "gzip":
            decoded = self.decoder.decompress(raw, remaining)
            while self.decoder.eof and self.decoder.unused_data:
                rest = self.decoder.unused_data
                self.data.extend(decoded)
                remaining = MAX_BYTES - len(self.data) + 1
                if remaining <= 0:
                    raise ReaderError("page_too_large")
                self.decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
                decoded = self.decoder.decompress(rest, remaining)
            return decoded
        if self.encoding == "br":
            return self.decoder.process(raw, output_buffer_limit=remaining)
        return raw

    def finish(self) -> bytes:
        if (self.encoding == "gzip" and not self.decoder.eof
                or self.encoding == "br" and not self.decoder.is_finished()):
            raise ReaderError("invalid_encoding")
        return bytes(self.data)


class _BoundedClient:
    def __init__(self, client: httpx.Client, deadline: float, *, automated: bool = False):
        self.client, self.deadline, self.automated = client, deadline, automated
        self.checking_robots = False

    def get(self, url: str, *, headers=None, extensions=None):
        _target(url)
        logical_host = (headers or {}).get("Host", urlsplit(url).netloc)
        parts = urlsplit(url)
        logical_url = urlunsplit((parts.scheme, logical_host, parts.path, parts.query, parts.fragment))
        _target(logical_url)
        if not ipaddress.ip_address(urlsplit(url).hostname).is_global:
            raise ReaderError("url_not_allowed")
        if self.automated and not self.checking_robots:
            self.checking_robots = True
            try:
                _robots(self, logical_url)
            finally:
                self.checking_robots = False
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("reader_deadline")
        with self.client.stream("GET", url, headers=headers, extensions=extensions,
                                timeout=min(10.0, remaining)) as response:
            if response.is_redirect:
                return httpx.Response(response.status_code, headers=response.headers, request=response.request)
            body = None
            try:
                body = _Body(response.headers.get("content-encoding", ""))
                for raw in response.iter_raw(chunk_size=16_384):
                    if time.monotonic() >= self.deadline:
                        raise TimeoutError("reader_deadline")
                    body.add(raw)
                return _response(response, body.finish())
            except (ReaderError, TimeoutError, httpx.HTTPError) as exc:
                exc.response = response
                exc.byte_length = body.received if body else 0
                raise


def _response(response: httpx.Response, content: bytes) -> httpx.Response:
    headers = dict(response.headers)
    headers.pop("content-encoding", None)
    headers.pop("content-length", None)
    return httpx.Response(response.status_code, headers=headers, content=content,
                          request=response.request, extensions=dict(response.extensions))


class _ScrapeClient:
    def __init__(self, client: httpx.AsyncClient, deadline: float):
        self.client, self.deadline = client, deadline

    @asynccontextmanager
    async def stream(self, *args, **kwargs):
        kwargs["timeout"] = min(kwargs.get("timeout", 25.0), max(0.01, self.deadline - time.monotonic()))
        async with self.client.stream(*args, **kwargs) as response:
            response.raise_for_status()
            body = _Body(response.headers.get("content-encoding", ""))
            async for raw in response.aiter_raw(chunk_size=16_384):
                if time.monotonic() >= self.deadline:
                    raise TimeoutError("reader_deadline")
                body.add(raw)
            yield _response(response, body.finish())


class _HTML(HTMLParser):
    def __init__(self, url: str):
        super().__init__(convert_charrefs=True)
        self.url = url
        self.stack = []
        self.title, self.text, self.main, self.links = [], [], [], []
        self.scripts = False

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        hidden = (tag in {"script", "style", "nav", "header", "footer", "noscript", "svg", "template"}
                  or "hidden" in attributes or attributes.get("aria-hidden") == "true"
                  or bool(_HIDDEN_STYLE.search(attributes.get("style") or "")))
        self.scripts |= tag == "script"
        if tag in {"p", "div", "br", "li", "section", "h1", "h2", "h3", "tr"}:
            self.handle_data("\n")
        if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}:
            if len(self.stack) >= 256:
                raise ReaderError("page_too_complex")
            self.stack.append((tag, hidden, tag in {"main", "article"} or attributes.get("role") == "main"))
        if tag == "a" and not any(entry[1] for entry in self.stack) and len(self.links) < MAX_LINKS:
            try:
                target = urljoin(self.url, attributes.get("href") or "")
                if urlsplit(target).scheme in {"http", "https"} and len(target) <= 2000 and target not in self.links:
                    self.links.append(target)
            except ValueError:
                pass

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break
        if tag in {"p", "div", "li", "section", "h1", "h2", "h3", "tr", "main", "article"}:
            self.handle_data("\n")

    def handle_data(self, data):
        if any(entry[0] == "title" for entry in self.stack):
            self.title.append(data)
        if any(entry[1] or entry[0] == "head" for entry in self.stack):
            return
        self.text.append(data)
        if any(entry[2] for entry in self.stack):
            self.main.append(data)


def _clean(text: str) -> str:
    return "\n".join(" ".join(line.split()) for line in text.splitlines() if line.strip())


@dataclass
class Page:
    text: str
    title: str
    links: list[str]
    final_url: str
    truncated: bool = False

    def model_data(self) -> str:
        return json.dumps({"title": self.title, "links": self.links, "final_url": self.final_url,
                           "chunks": [self.text[index:index + CHUNK_CHARS]
                                      for index in range(0, len(self.text), CHUNK_CHARS)],
                           "truncated": self.truncated}, ensure_ascii=False)


def _pdf(content: bytes, limit: int, deadline: float) -> tuple[str, str]:
    from app.intake import documents

    # Reuse the existing disposable parser's CPU and memory limits, not an in-process PDF decoder.
    try:
        result = subprocess.run([sys.executable, documents.__file__, "page.pdf"], input=content,
            capture_output=True, env={}, timeout=min(15.0, max(0.01, deadline - time.monotonic())))
        data = json.loads(result.stdout)
    except (subprocess.TimeoutExpired, ValueError, OSError) as exc:
        raise ReaderError("pdf_requires_rendering") from exc
    if result.returncode or data.get("error") or not isinstance(data.get("text"), str):
        raise ReaderError("pdf_requires_rendering")
    text = data["text"]
    title = next((line for line in text.splitlines() if line.strip() and not re.fullmatch(r"Page \d+", line)), "")
    return text[:limit + 1], title[:300]


def _robots(client, url: str) -> None:
    parts = urlsplit(url)
    robots_url = f"{parts.scheme}://{parts.netloc}/robots.txt"
    _target(robots_url)
    response = get_checked(client, robots_url)
    if response.status_code == 404:
        return
    if response.status_code in {401, 403}:
        raise ReaderError("robots_disallowed")
    response.raise_for_status()
    policy = RobotFileParser()
    policy.parse(response.text.splitlines())
    if not policy.can_fetch("*", url):
        raise ReaderError("robots_disallowed")


def _scrape(url: str, deadline: float, ctx, scope) -> Page:
    from app.signals.research import _self_hosted_firecrawl

    if not _self_hosted_firecrawl():
        raise ReaderError("firecrawl_not_configured")
    if time.monotonic() >= deadline:
        raise TimeoutError("reader_deadline")
    _target(url)
    # shortcut: rendering is refused until the service can pin redirects and subrequests to guarded public IPs.
    raise ReaderError("renderer_egress_not_pinned")


def read_page(url: str, timeout_s: float = TIMEOUT_S, *, limit: int | None = None,
              ctx=None, automated: bool = False) -> Page:
    from app.tenancy import current_or_none
    from app.core.local_mode import is_local

    if is_local():
        from app.research.local_providers import fetch_page
        try:
            text = asyncio.run(fetch_page(url, timeout_s=min(TIMEOUT_S, max(0.01, timeout_s))))
        except ValueError as exc:
            raise ReaderError(str(exc)) from None
        metadata = text.metadata
        limit = min(max_chars(), max(1, limit)) if limit is not None else max_chars()
        return Page(text[:limit], metadata["title"], metadata.get("links", []), metadata["sourceURL"], len(text) > limit)

    scope = current_or_none()
    founder = scope.actor if scope and scope.actor not in {"local", "anonymous", "system"} else (
        scope.workspace_id if scope else ctx.workspace_id if ctx is not None else "local")
    limit = min(max_chars(), max(1, limit)) if limit is not None else max_chars()
    deadline = time.monotonic() + min(TIMEOUT_S, max(0.01, timeout_s))
    _target(url)
    # Robots are honoured for automated/bulk reads; a single page requested by you is allowed.
    # The slot stays in this worker until completion, even if an async caller stops waiting.
    with _slot(founder):
        with httpx.Client(follow_redirects=False, trust_env=False,
                          headers={"User-Agent": "CompanyOS-Reader/2.0", "Accept-Encoding": "gzip, br"}) as client:
            bounded = _BoundedClient(client, deadline, automated=automated)
            try:
                response = get_checked(bounded, url, max_redirects=MAX_REDIRECTS)
            except PermissionError as exc:
                raise ReaderError("url_not_allowed") from exc
            except httpx.TimeoutException as exc:
                raise TimeoutError("reader_hop_timeout") from exc
            response.raise_for_status()
            final_url = response.extensions.get("logical_url", url)
        content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if content_type == "application/pdf" or response.content.startswith(b"%PDF-"):
            try:
                text, title = _pdf(response.content, limit, deadline)
            except (ImportError, ValueError, OSError):
                text, title = "", ""
            page = Page(text, title, [], final_url)
            thin = not text.strip()
        else:
            charset = re.search(r"charset\s*=\s*[\"']?([\w-]+)", response.headers.get("content-type", ""), re.I)
            encoding = charset[1] if charset else "utf-8"
            try:
                codecs.lookup(encoding)
            except LookupError:
                encoding = "utf-8"
            text = response.content.decode(encoding, errors="replace")
            html = content_type in {"text/html", "application/xhtml+xml"} or (
                not content_type and re.search(r"<(?:html|head|body|p|div|main|article|h1)\b", text, re.I))
            if html:
                parsed = _HTML(final_url)
                parsed.feed(text)
                text = _clean("".join(parsed.main or parsed.text))
                page = Page(text, _clean("".join(parsed.title))[:300], parsed.links, final_url)
                thin = len(text) < 200 or bool(parsed.scripts and re.search(
                    r"(?:enable|requires?)\s+javascript|loading\.{3}", text, re.I))
            elif (content_type.startswith("text/") or content_type in {"", "application/json", "application/ld+json"}
                  or content_type.endswith("+json")):
                links = []
                title = ""
                if content_type in {"text/markdown", "text/x-markdown"}:
                    heading = re.search(r"^#\s+(.+)$", text, re.M)
                    title = heading[1][:300] if heading else ""
                    for target in re.findall(r"(?<!!)\[[^\]\n]*\]\(([^)\s]+)\)", text):
                        try:
                            link = urljoin(final_url, target)
                            if urlsplit(link).scheme in {"http", "https"} and len(link) <= 2000 and link not in links:
                                links.append(link)
                        except ValueError:
                            continue
                        if len(links) >= MAX_LINKS:
                            break
                page = Page(text.strip(), title, links, final_url)
                thin = False
            else:
                raise ReaderError("unsupported_content_type")
        if time.monotonic() >= deadline:
            raise TimeoutError("reader_deadline")
        if thin and automated:
            # shortcut: bulk rendering is refused until renderer redirects can check robots before fetching.
            raise ReaderError("automated_rendering_not_allowed")
        if thin:
            page = _scrape(final_url, deadline, ctx, scope)
        if not page.text:
            raise ReaderError("page_empty")
        page.truncated = len(page.text) > limit
        page.text = page.text[:limit]
        page.title = page.title or urlsplit(page.final_url).hostname or ""
        return page
