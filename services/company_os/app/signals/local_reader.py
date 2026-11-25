"""Local-only page reads and explicitly configured search transports."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import httpx
from app.tools import reader

log = logging.getLogger(__name__)
_USER_AGENT = "LocalPageReader/1.0"
_HTML_START = re.compile(rb"(?:<\?xml[^>]*>\s*)?(?:<!--.*?-->\s*)*<(?:!doctype\s+html|html|head|body|main|article|section|header|footer|nav|div|p|h[1-6]|ul|table)(?=[\s>])", re.I | re.S)


class SourcesUnreadable(reader.ReaderError):
    pass


class UnbilledReadError(ValueError):
    pass


EXPECTED_PAGE_READ_ERRORS = (UnbilledReadError, reader.ReaderError, httpx.HTTPError, TimeoutError)


class _Markdown(reader._HTML):
    def __init__(self, url: str):
        super().__init__(url)
        self.link = None

    def handle_starttag(self, tag, attrs):
        super().handle_starttag(tag, attrs)
        if tag != "a" or self.link or any(entry[1] for entry in self.stack):
            return
        try:
            target = urljoin(self.url, dict(attrs).get("href") or "")
        except ValueError:
            return
        if target in self.links:
            self.link = target.replace("(", "%28").replace(")", "%29")
            self.handle_data("[")

    def handle_endtag(self, tag):
        if tag == "a" and self.link:
            self.handle_data(f"]({self.link})")
            self.link = None
        super().handle_endtag(tag)
        if tag in {"td", "th"}:
            self.handle_data(" ")


class _RobotsClient(reader._BoundedClient):
    def get(self, url: str, *, headers=None, extensions=None):
        parts = urlsplit(url)
        logical_url = urlunsplit((parts.scheme, (headers or {}).get("Host", parts.netloc),
                                 parts.path, parts.query, parts.fragment))
        reader._target(logical_url)
        if not self.checking_robots:
            from core.research_web.guard import get_checked
            self.checking_robots = True
            try:
                origin = urlsplit(logical_url)
                response = get_checked(self, f"{origin.scheme}://{origin.netloc}/robots.txt")
                if response.status_code in {401, 403}:
                    raise reader.ReaderError("robots_disallowed", response=response)
                if response.status_code != 404:
                    response.raise_for_status()
                    policy = RobotFileParser()
                    policy.parse(response.text.splitlines())
                    if not policy.can_fetch(_USER_AGENT, logical_url):
                        raise reader.ReaderError("robots_disallowed", response=response)
            except (httpx.HTTPError, TimeoutError, ValueError, PermissionError, RuntimeError) as exc:
                if isinstance(exc, reader.ReaderError) and str(exc) == "robots_disallowed":
                    raise
                raise reader.ReaderError("robots_unavailable") from exc
            finally:
                self.checking_robots = False
        return super().get(url, headers=headers, extensions=extensions)


def _firecrawl() -> tuple[str, str] | None:
    from app.research.local_providers import _reader_endpoint, _values

    endpoint = _reader_endpoint()
    if _values().get("firecrawl_url") and endpoint is None:
        raise ValueError("Save an HTTPS Firecrawl URL, or an HTTP loopback URL, without credentials on the Keys and connections screen, then retry.")
    return endpoint


def _search_provider() -> tuple[str, str, str, str] | None:
    from app.core.local_secrets import get_secret
    from app.research.local_providers import _values

    if _values().get("provider") == "tavily" and (key := get_secret("search", "api_key")):
        return "tavily", "https://api.tavily.com", key, ""
    return None


def require_search_configuration() -> None:
    from app.research.local_providers import _blocked_message, search_ready
    if not search_ready():
        raise ValueError(_blocked_message())


async def _post(client, url: str, *, headers: dict, body: dict) -> httpx.Response:
    try:
        response = await client.post(url, headers=headers, json=body, timeout=25.0)
    except httpx.HTTPError as exc:
        exc.args = ("Your provider could not be reached. Check your provider URL and key on the Keys and connections screen, then retry.",)
        raise
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        exc.status_code = response.status_code
        exc.args = (f"Your provider returned HTTP {response.status_code}. Check your provider key and URL on the Keys and connections screen, then retry.",)
        raise
    return response


def _page_response(url: str, text: str, title: str = "", links: list[str] | None = None, *, metadata=None) -> httpx.Response:
    return httpx.Response(200, json={"success": True, "data": {
        "markdown": text, "rawHtml": text, "metadata": {"sourceURL": url, "url": url, "title": title, "links": links or [], **(metadata or {})}}},
        request=httpx.Request("GET", url))


def _basic_read(url: str, main_only: bool) -> httpx.Response:
    from core.research_web.guard import get_checked

    deadline = time.monotonic() + 25
    parts = urlsplit(url)
    targets = [url]
    if parts.scheme == "http":
        authority = parts.netloc.rsplit(":", 1)[0] if parts.port == 80 else parts.netloc
        targets.insert(0, parts._replace(scheme="https", netloc=authority).geturl())
    # shortcut: the pool keys by IP, not SNI; use per-host pools if TLS setup becomes a bottleneck.
    with reader._slot("local", deadline=deadline), httpx.Client(follow_redirects=False, trust_env=False,
            limits=httpx.Limits(max_keepalive_connections=0),
            headers={"User-Agent": _USER_AGENT, "Accept-Encoding": "gzip, br"}) as client:
        for target in targets:
            try:
                response = get_checked(_RobotsClient(client, deadline), target,
                    https_only=urlsplit(target).scheme == "https")
                break
            except PermissionError as exc:
                raise reader.ReaderError("url_not_allowed") from exc
            except (httpx.ConnectError, httpx.ConnectTimeout, reader.ReaderError) as exc:
                failure = exc.__cause__ if isinstance(exc, reader.ReaderError) else exc
                if target == url or not isinstance(failure, (httpx.ConnectError, httpx.ConnectTimeout)):
                    raise
        response.raise_for_status()
    final_url = response.extensions.get("logical_url", url)
    content_type = response.headers.get("content-type", "").split(";", 1)[0].lower().strip()
    body = response.content
    if not body.strip():
        raise reader.ReaderError("empty_page", response=response)
    is_html = content_type in {"text/html", "application/xhtml+xml"} or (
        content_type in {"", "text/plain", "application/octet-stream"}
        and _HTML_START.match(body[:1024].removeprefix(b"\xef\xbb\xbf").lstrip()))
    title = ""
    links = []
    if content_type == "application/pdf" or body.startswith(b"%PDF-"):
        try:
            content, title = reader._pdf(body, reader.max_chars(), deadline)
        except (ImportError, ValueError, OSError) as exc:
            raise reader.ReaderError("text_extraction_failed", response=response) from exc
    elif is_html:
        content = response.text
        if not response.charset_encoding and (charset := re.search(
                rb"<meta\b[^>]*\bcharset\s*=\s*[\"']?([\w-]+)", body[:1024], re.I)):
            try:
                content = body.decode(charset[1].decode("ascii"), errors="replace")
            except LookupError:
                pass
        parsed = _Markdown(final_url)
        try:
            parsed.feed(content)
            parsed.close()
        except reader.ReaderError as exc:
            exc.response = response
            raise
        title = reader._clean("".join(parsed.title))[:300]
        links = parsed.links
        extracted = reader._clean("".join(parsed.main)) or reader._clean("".join(parsed.text))
        # shortcut: built-in reads do not execute scripts; use a guarded renderer if script-only pages need support.
        if parsed.scripts and (not extracted or re.fullmatch(r"(?:loading|please enable javascript)[.!\s…]*", extracted, re.I)):
            raise reader.ReaderError("javascript_required", response=response)
        if not extracted:
            raise reader.ReaderError("text_extraction_failed", response=response)
        if main_only:
            content = extracted
        else:
            from lxml import etree, html
            try:
                document = html.fromstring(content.encode("utf-8"), parser=html.HTMLParser(encoding="utf-8"))
            except (UnicodeError, etree.ParserError) as exc:
                raise reader.ReaderError("text_extraction_failed", response=response) from exc
            for node in document.xpath("//script|//style|//svg|//template|//noscript|//*[@hidden or @aria-hidden='true']"):
                if node.tag == "script" and node.get("type", "").lower() == "application/ld+json":
                    continue
                node.drop_tree()
            for node in document.xpath("//*[@style]"):
                if reader._HIDDEN_STYLE.search(node.get("style")):
                    node.drop_tree()
            content = html.tostring(document, encoding="unicode")
    elif not content_type.startswith("text/") and content_type not in {"", "application/json", "application/ld+json"} and not content_type.endswith("+json"):
        raise reader.ReaderError("unsupported_content_type", response=response)
    else:
        content = response.text
    if not content.strip():
        raise reader.ReaderError("text_extraction_failed", response=response)
    return _page_response(final_url, content[:reader.max_chars()], title, links, metadata={
        "statusCode": response.status_code, "contentType": content_type, "byteLength": len(body)})


async def _search(client, body: dict, config: tuple[str, str, str, str]) -> httpx.Response:
    from app.signals.research import _WEB_SEARCH_RULES

    provider, base, key, model = config
    if provider == "tavily":
        target = body["url"]
        response = await _post(client, base + "/extract", headers={"Authorization": f"Bearer {key}"}, body={"urls": [target]})
        result = response.json()
        rows = result.get("results") if isinstance(result, dict) else None
        if not isinstance(rows, list):
            raise ValueError("Your search provider returned invalid results. Check your provider configuration and try again.")
        text = next((row.get("raw_content") for row in rows if isinstance(row, dict) and row.get("url") == target), None)
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Your search provider could not read that page. Try another public URL.")
        # This response matches the requested URL, but doesn't identify the final page after redirects.
        return _page_response(target, text, metadata={"url": None})
    response = await _post(client, base + "/responses", headers={
        "Authorization": f"Bearer {key}"}, body={"model": model, "tools": [{"type": "web_search"}],
        "max_tool_calls": 1, "max_output_tokens": 2048, "include": ["web_search_call.action.sources"],
        "instructions": _WEB_SEARCH_RULES.replace("LIMIT", str(body.get("limit", 6))),
        "input": body["query"] + (" " + body["tbs"] if body.get("tbs") else "")})
    rows = []
    result = response.json()
    if not isinstance(result, dict) or not isinstance(result.get("output"), list) or result.get("status") == "failed":
        raise ValueError("Your search provider returned invalid results. Check your provider configuration and try again.")
    for output in result["output"]:
        if not isinstance(output, dict):
            continue
        action = output.get("action")
        if output.get("type") == "web_search_call" and isinstance(action, dict):
            rows.extend(source for source in action.get("sources") or [] if isinstance(source, dict))
        for content in output.get("content") or []:
            if not isinstance(content, dict) or content.get("type") != "output_text":
                continue
            try:
                parsed = json.loads(content.get("text"))
            except (ValueError, TypeError):
                raise ValueError("Your search provider returned invalid results. Try the search again.") from None
            if not isinstance(parsed, list):
                raise ValueError("Your search provider returned invalid results. Try the search again.")
            rows.extend(parsed)
    excluded = body.get("excludeDomains", [])
    filtered = []
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("url"), str):
            continue
        try:
            host = (urlsplit(row["url"]).hostname or "").lower().rstrip(".")
        except ValueError:
            continue
        if row["url"] not in seen and not any(host == domain.lower() or host.endswith("." + domain.lower()) for domain in excluded):
            seen.add(row["url"])
            filtered.append(row)
    return httpx.Response(200, json={"success": True, "data": {"web": filtered[:body.get("limit", 6)]}},
        request=response.request)


async def request(client, path: str, body: dict) -> httpx.Response:
    from app.research.local_providers import _blocked_message, _research_enabled
    if not _research_enabled():
        raise ValueError(_blocked_message())
    if path == "search":
        from app.research.local_providers import search_provider
        provider = search_provider(limit=body.get("limit", 6))
        if provider is None:
            raise ValueError(_blocked_message())
        if provider.name != "firecrawl":
            rows = await provider.search(body["query"], limit=body.get("limit"), tbs=body.get("tbs"),
                exclude_domains=body.get("excludeDomains", []), include_content="scrapeOptions" in body)
            return httpx.Response(200, json={"success": True, "data": {"web": [
                {**row, **({"markdown": row["content"]} if row.get("content") else {})} for row in rows]}},
                request=httpx.Request("POST", "https://local.invalid/search"))
        endpoint = _firecrawl()
        if not endpoint:
            raise ValueError(_blocked_message())
    else:
        from core.ssrf import assert_safe_remote_url
        try:
            reader._target(body["url"])
            # shortcut: OS DNS in a worker cannot be cancelled, use a cancellable resolver if background DNS must stop.
            async with asyncio.timeout(25):
                await asyncio.to_thread(assert_safe_remote_url, body["url"])
        except asyncio.CancelledError:
            from app.signals.research import FirecrawlLimiterUnavailable
            raise FirecrawlLimiterUnavailable("Your page URL check was cancelled before a request started. Retry your page read.") from None
        except TimeoutError as exc:
            raise UnbilledReadError("Your page URL could not be checked in time. Retry, or choose another public URL.") from exc
        except ValueError as exc:
            raise UnbilledReadError("Your page URL is not allowed. Choose a public HTTP or HTTPS URL without credentials.") from exc
        endpoint = _firecrawl()
    if endpoint:
        from app.signals.research import _firecrawl_slot
        base, token = endpoint
        host = (urlsplit(base).hostname or "").lower()
        self_hosted = not (host == "firecrawl.dev" or host.endswith(".firecrawl.dev"))
        try:
            async with _firecrawl_slot(), asyncio.timeout(25):
                response = await _post(client, base + "/v2/" + path,
                    headers={"Authorization": f"Bearer {token}"} if token else {}, body=body)
            try:
                result = response.json()
            except ValueError as exc:
                raise ValueError("Your Firecrawl returned invalid results. Check your Firecrawl URL and key on the Keys and connections screen, then retry.") from exc
            if not isinstance(result, dict) or result.get("success") is not True:
                raise ValueError("Your Firecrawl request failed. Check your Firecrawl URL and key on the Keys and connections screen, then retry.")
        except (httpx.HTTPError, TimeoutError, ValueError) as exc:
            if path == "search":
                raise
            if isinstance(exc, httpx.HTTPStatusError):
                exc.response.extensions["firecrawl_self_hosted"] = self_hosted
            log.warning("firecrawl_self_hosted_error path=%s kind=%s status=%s", path, type(exc).__name__,
                getattr(getattr(exc, "response", None), "status_code", None),
                extra={"source_url": urlunsplit(urlsplit(body["url"])._replace(query="", fragment=""))})
            if isinstance(exc, TimeoutError):
                exc.args = ("Your page read timed out. Check your Firecrawl URL on the Keys and connections screen, then retry.",)
            raise
        response.extensions["firecrawl_self_hosted"] = self_hosted
        if path == "search":
            return response
        from app.research.deep import safe_source_url

        data = result.get("data")
        field = "rawHtml" if body.get("formats") == ["rawHtml"] else "markdown"
        text = data.get(field, data.get("markdown")) if isinstance(data, dict) else None
        metadata = data.get("metadata") if isinstance(data, dict) else None
        final_url = metadata.get("url") if isinstance(metadata, dict) else None
        if isinstance(text, str) and text.strip() and isinstance(final_url, str) and safe_source_url(final_url):
            return response
    try:
        async with asyncio.timeout(25):
            return await asyncio.to_thread(_basic_read, body["url"], body.get("formats") != ["rawHtml"])
    except (httpx.HTTPError, TimeoutError, ValueError, PermissionError, RuntimeError) as exc:
        error_type = ValueError if endpoint else UnbilledReadError
        if isinstance(exc, reader.ReaderError) and str(exc) == "robots_unavailable":
            raise error_type("Your page permissions could not be checked. Retry later or choose another public URL.") from exc
        if isinstance(exc, reader.ReaderError) and str(exc) in {"robots_disallowed", "url_not_allowed"}:
            raise error_type("Your page does not allow this read. Choose another public URL.") from exc
        log.warning("local_page_read_failed reader=http kind=%s", type(exc).__name__)
        if config := _search_provider():
            from app.credits import metered
            from app.credits.gate import _call_allowance
            from app.tenancy import current_or_none
            scope = current_or_none()
            allowance = _call_allowance.get()
            scope_ids = (scope.workspace_id, scope.venture_id) if scope else allowance[:2] if allowance else (None, None)
            try:
                async with asyncio.timeout(25):
                    return await metered.call(lambda: _search(client, {"url": body["url"]}, config),
                        purpose="research.fetch", provider=config[0], payer="founder",
                        estimate_micro=metered.price_micro("tavily_search"), workspace_id=scope_ids[0], venture_id=scope_ids[1])
            except (json.JSONDecodeError, TimeoutError) as final:
                raise ValueError("Your page could not be read. Retry, choose another public URL, or check your search provider key on the Keys and connections screen.") from final
        from app.research.local_providers import READER_UNAVAILABLE
        if not endpoint:
            raise UnbilledReadError(READER_UNAVAILABLE) from exc
        exc.args = (READER_UNAVAILABLE,)
        raise
