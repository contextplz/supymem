"""One saved-settings resolver for local search and public page reads."""

from __future__ import annotations

import asyncio
import os
import re
import time
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from urllib.parse import SplitResult, parse_qs, unquote, unquote_plus, urlsplit

from app.core.local_mode import is_local
from app.signals.research import (
    HttpResearchProvider,
    _sources_from_response,
)
from core.clock import utcnow

SEARCH_UNAVAILABLE = ("Your search could not finish. Retry your search. If you use built-in search and it keeps failing, "
    "open Settings > Keys and connections > Your search and page reader, choose Tavily or Exa, and save your Search API key.")
SEARCH_LIMITED = "Your built-in search was limited. Try again in 30 minutes or add a search key for Tavily or Exa in Settings > Keys and connections > Your search and page reader."
SEARCH_BUDGET_EXHAUSTED = "Narrow your audience and retry, or select Tavily or Exa and save its API key in Keys and connections > Search."
READER_UNAVAILABLE = "Your page could not be read. Retry with another public URL."
CUSTOMER_SEARCH_ENGINE_BUDGET = 20
_ENGINE_COOLDOWN_SECONDS = 30 * 60
_ENGINE_ERROR_BACKOFF_SECONDS = 60
_PERSISTED_COOLDOWN_CAUSES = ("refused", "challenge")
_BUILTIN_SEARCH_SECONDS = 10  # Leave time for the caller's 12-second connection test.
_SEARCH_MODIFIERS = {"alternative", "comparison", "compare", "competitor", "official", "platform",
    "profile", "public", "vendor", "website"}
_REFERENCE_HOSTS = {"wikipedia.org": "wikipedia", "britannica.com": "britannica", "investopedia.com": "investopedia",
    "grokipedia.com": "grokipedia"}
_JOB_HOSTS = {"indeed.com", "glassdoor.com", "ziprecruiter.com", "monster.com"}
_JOB_TERMS = {"job", "jobs", "career", "hiring", "vacancy", "recruitment"}
_TRACKING_KEYS = {"gclid", "dclid", "fbclid", "msclkid", "yclid", "gbraid", "wbraid"}
_builtin_next_at = 0.0
_builtin_cooldowns: dict[str, _EngineCooldown] = {}
search_diagnostics: ContextVar[dict | None] = ContextVar("search_diagnostics", default=None)


class SearchEngineLimited(ValueError):
    pass


class SearchBudgetExhausted(SearchEngineLimited):
    pass


class BuiltinSearchUnavailable(ValueError):
    pass


@dataclass
class _EngineCooldown:
    until: float
    cause: str
    error_delay: int = 0


@dataclass
class _BuiltinSearchRun:
    run_id: str
    calls_used: int = 0
    attempts: dict[str, dict[str, tuple[list[tuple[tuple[int, int], dict]], str]]] = field(default_factory=dict)


builtin_search_run: ContextVar[_BuiltinSearchRun | None] = ContextVar("builtin_search_run", default=None)


@contextmanager
def collect_search_diagnostics(*, customer_run_id: str | None = None):
    queries = {}
    token = search_diagnostics.set(queries)
    run_token = builtin_search_run.set(_BuiltinSearchRun(customer_run_id)) if customer_run_id is not None else None
    try:
        yield queries
    finally:
        if run_token is not None:
            builtin_search_run.reset(run_token)
        search_diagnostics.reset(token)


def _record_query(query, provider, count, *, reason="", **details):
    if (queries := search_diagnostics.get()) is not None:
        from app.gtm.email_learning import opaque

        queries[opaque(f"{provider}:{query}", "search-diagnostics")] = {
            "provider": provider, "results": count, "reason": reason, **details}


def search_was_limited(queries: dict) -> bool:
    return any(result["reason"] == "search_engine_limited" for result in queries.values())


@contextmanager
def _builtin_gate(deadline: float | None = None):
    from app.config import data_root
    from app.core.local_process import check_private, file_lock, open_private

    root = data_root()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = open_private(root / "builtin-search.lock", os.O_CREAT | os.O_RDWR)
    with os.fdopen(descriptor, "r+b") as gate, ExitStack() as locks:
        check_private(gate)
        while True:
            remaining = deadline - time.monotonic() if deadline is not None else None
            if remaining is not None and remaining <= 0:
                raise BuiltinSearchUnavailable(SEARCH_UNAVAILABLE)
            try:
                locks.enter_context(file_lock(gate, blocking=deadline is None))
                break
            except BlockingIOError:
                time.sleep(min(remaining, 0.1))
        yield


def _on_domain(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def _search_target(url: str | None) -> SplitResult | None:
    from ddgs.engines.bing import unwrap_bing_url
    from ddgs.engines.yahoo import extract_url

    # shortcut: decode known search wrappers without fetching them; add formats when an engine changes its links.
    for _ in range(3):
        if not isinstance(url, str) or "\\" in url or any(char.isspace() or ord(char) < 32 for char in url):
            return None
        if url.startswith("//"):
            url = "https:" + url
        try:
            parts = urlsplit(url)
            host = (parts.hostname or "").rstrip(".")
            if not host or parts.scheme not in {"https", "http"} or parts.username is not None or parts.port == 0:
                return None
            path = unquote(parts.path).casefold().rstrip("/")
            if (_on_domain(host, "bing.com") and path == "/aclick"
                    or _on_domain(host, "duckduckgo.com") and path == "/y.js"
                    or host.startswith(("google.", "www.google.")) and path in {"/aclk", "/pagead/aclk"}
                    or _on_domain(host, "doubleclick.net")):
                return None
            if _on_domain(host, "bing.com") and path == "/ck/a":
                url = unwrap_bing_url(url)
            elif _on_domain(host, "duckduckgo.com") and path == "/l":
                url = next(iter(parse_qs(parts.query).get("uddg", ())), None)
            elif host.startswith(("google.", "www.google.")) and path == "/url":
                params = parse_qs(parts.query)
                url = next(iter(params.get("q") or params.get("url", ())), None)
            elif host.startswith("r.search.yahoo."):
                url = extract_url(parts.path) if "/RU=" in parts.path else None
            else:
                def tracked(parameter):
                    name = unquote_plus(parameter.partition("=")[0]).casefold()
                    return name.startswith("utm_") or name in _TRACKING_KEYS

                return parts._replace(query="&".join(part for part in parts.query.split("&") if not tracked(part)))
        except ValueError:
            return None
    return None


def _rank_builtin_rows(query: str, rows: list[dict]) -> list[tuple[tuple[int, int], dict]]:
    from app.research.source_quality import _tokens

    sites = re.findall(r"(?<!\S)site:([^/\s]+)\S*", query, flags=re.I)
    words = _tokens(re.sub(r"(?<!\S)(?:site:\S+|-\S+)", "", query, flags=re.I))
    terms = words - _SEARCH_MODIFIERS
    required = min(len(terms), 2 if len(terms) < 5 else 3)
    reference_lookup = bool(words & {"definition", "meaning", "encyclopedia"})
    ranked = []
    for row in rows:
        target = _search_target(row.get("href") or row.get("url"))
        if target is None:
            continue
        host = target.hostname.rstrip(".")
        if not any(_on_domain(host, site.lower()) for site in sites):
            if not reference_lookup and any(_on_domain(host, domain) and label not in words
                    for domain, label in _REFERENCE_HOSTS.items()):
                continue
            if not words & _JOB_TERMS and (any(_on_domain(host, domain) for domain in _JOB_HOSTS)
                    or re.search(r"/(?:jobs?|careers?|vacancies)(?:/|$)", unquote(target.path), re.I)):
                continue
        strong = terms & _tokens(f"{row.get('title') or ''} {host} {unquote(target.path)}")
        matches = strong | (terms & _tokens(row.get("body") or row.get("description") or row.get("snippet") or ""))
        if len(matches) < required or (not strong and len(matches) < min(3, len(terms))):
            continue
        ranked.append(((len(matches), len(strong)), {**row, "href": target.geturl()}))
    return ranked


def _builtin_search_sync(query: str, limit: int, deadline: float) -> list[dict]:
    import httpx
    from ddgs.engines import ENGINES
    from ddgs.engines.bing import Bing
    from ddgs.exceptions import DDGSException, RatelimitException
    from sqlalchemy.exc import SQLAlchemyError
    from app.gtm.email_learning import cache_key, cache_save, cached
    from app.signals import research

    global _builtin_next_at
    ENGINES["text"].setdefault("bing", Bing)
    key = cache_key("builtin", "search-v8", [query, limit])

    def load(cache_id):
        try:
            return cached(cache_id)
        except (SQLAlchemyError, ValueError):
            research.log.warning("local_search_cache_unavailable operation=read")

    def save(cache_id, value):
        try:
            cache_save(cache_id, value)
        except (SQLAlchemyError, ValueError):
            research.log.warning("local_search_cache_unavailable operation=write")

    with _builtin_gate(deadline):
        entry = load(key)
        if (isinstance(entry, dict) and isinstance(entry.get("expires_at"), (int, float))
                and entry["expires_at"] > utcnow().timestamp() and isinstance(entry.get("rows"), list)
                and entry.get("reason") in ("", "search_engine_limited") and isinstance(entry.get("engines"), dict)):
            research.log.info("local_search_engine_result: engine=cache outcome=hit results=%s", len(entry["rows"]))
            _record_query(query, "builtin", len(entry["rows"]), reason=entry["reason"], engines=entry["engines"], cached=True)
            return entry["rows"]
        cooldown_key = cache_key("builtin", "engine-cooldown-v2", "text")
        recorded = load(cooldown_key)
        now = utcnow().timestamp()
        for backend, health in (recorded.items() if isinstance(recorded, dict) else ()):
            if (isinstance(health, list) and len(health) == 2 and isinstance(health[0], (int, float))
                    and health[0] > now and health[1] in _PERSISTED_COOLDOWN_CAUSES):
                current = _builtin_cooldowns.get(backend)
                if current is None or current.error_delay or health[0] > current.until:
                    _builtin_cooldowns[backend] = _EngineCooldown(*health)
        run = builtin_search_run.get()
        if run is not None:
            budget_key = cache_key("builtin", "customer-engine-calls", run.run_id)
            spent = load(budget_key)
            if type(spent) is int:
                run.calls_used = max(run.calls_used, spent)
        attempts = run.attempts.setdefault(query, {}) if run is not None else {}
        rows = {}
        budget_exhausted = health_changed = deadline_reached = False
        engines = {}
        for backend in dict.fromkeys(("bing", "yahoo", "duckduckgo", "brave", "mojeek", *ENGINES["text"])):
            if len(rows) >= limit or (run is not None and rows and backend not in {"bing", "yahoo"}):
                break
            if backend not in ENGINES["text"]:
                continue
            if backend in attempts:
                found, outcome = attempts[backend]
            elif (health := _builtin_cooldowns.get(backend)) and health.until > (
                    time.monotonic() if health.error_delay else utcnow().timestamp()):
                research.log.info("local_search_engine_result: engine=%s outcome=cooldown results=0 cause=%s",
                    backend, health.cause)
                engines[backend] = {"outcome": "cooldown", "results": 0, "cause": health.cause}
                continue
            elif run is not None and run.calls_used >= CUSTOMER_SEARCH_ENGINE_BUDGET:
                budget_exhausted = True
                break
            else:
                delay = max(1, _builtin_next_at - time.monotonic())
                if deadline - time.monotonic() <= delay:
                    deadline_reached = True
                    break
                time.sleep(delay)
                if run is not None:
                    run.calls_used += 1
                    # Reserve before dispatch so an interrupted worker cannot spend the same slot again.
                    save(budget_key, run.calls_used)
                timeout = min(3, deadline - time.monotonic())
                if timeout <= 0:
                    deadline_reached = True
                    break
                try:
                    raw = research._ddgs_text(query, limit=limit, backend=backend, timeout=timeout) or []
                    found = _rank_builtin_rows(query, raw)
                    outcome = "success" if found else "irrelevant" if raw else "empty"
                except SearchEngineLimited:
                    found, outcome = [], "challenge"
                except (DDGSException, TimeoutError, ConnectionError, RuntimeError, httpx.HTTPError, ValueError) as exc:
                    # shortcut: ddgs exposes empty searches through exception text, replace when it supplies a typed empty-result error.
                    found = []
                    if isinstance(exc, DDGSException) and str(exc).rstrip(".") == "No results found":
                        outcome = "empty"
                    else:
                        refused = isinstance(exc, RatelimitException) or (
                            isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in {202, 403, 429, 503})
                        outcome = "refused" if refused else "error"
                        research.log.warning("local_search_engine_result: engine=%s outcome=%s results=0 kind=%s",
                            backend, outcome, type(exc).__name__)
                _builtin_next_at = time.monotonic() + (2 if outcome == "challenge" else 1)
                if outcome in _PERSISTED_COOLDOWN_CAUSES:
                    _builtin_cooldowns[backend] = _EngineCooldown(utcnow().timestamp() + _ENGINE_COOLDOWN_SECONDS, outcome)
                    health_changed = True
                elif outcome == "error":
                    delay = min(_ENGINE_COOLDOWN_SECONDS,
                        max(_ENGINE_ERROR_BACKOFF_SECONDS, 2 * health.error_delay if health else 0))
                    _builtin_cooldowns[backend] = _EngineCooldown(time.monotonic() + delay, "backoff", delay)
                else:
                    _builtin_cooldowns.pop(backend, None)
                if outcome != "error":
                    attempts[backend] = (found, outcome)
                if outcome not in {"refused", "error"}:
                    research.log.info("local_search_engine_result: engine=%s outcome=%s results=%s",
                        backend, outcome, len(found))
            engines[backend] = {"outcome": outcome, "results": len(found)}
            for score, row in found:
                url = row["href"]
                if url not in rows or score > rows[url][0]:
                    rows[url] = (score, row)
        if health_changed:
            save(cooldown_key, {backend: (health.until, health.cause) for backend, health in _builtin_cooldowns.items()
                if health.cause in _PERSISTED_COOLDOWN_CAUSES})
        outcomes = {result.get("cause", result["outcome"]) for result in engines.values()
                    if not rows or result["outcome"] != "cooldown"}
        limited = bool(outcomes & {"challenge", "refused"})
        no_matches = "irrelevant" in outcomes or ("empty" in outcomes and not deadline_reached)
        reason = ("search_budget_exhausted" if budget_exhausted else "search_engine_limited" if limited
            else "" if rows else "no_matches" if no_matches else "search_unavailable")
        rows = [row for _, row in sorted(rows.values(), key=lambda result: result[0], reverse=True)[:limit]]
        _record_query(query, "builtin", len(rows), reason=reason, engines=engines, cached=False)
        if rows:
            if not budget_exhausted:
                save(key, {"expires_at": utcnow().timestamp() + (120 if limited else 900),
                    "rows": rows, "reason": reason, "engines": engines})
            return rows
        if budget_exhausted:
            raise SearchBudgetExhausted(SEARCH_BUDGET_EXHAUSTED) from None
        if limited:
            raise SearchEngineLimited(SEARCH_LIMITED) from None
        if no_matches:
            return []
        raise BuiltinSearchUnavailable(SEARCH_UNAVAILABLE) from None


async def _builtin_search(query: str, limit: int) -> list[dict]:
    from app.signals import research

    if not is_local():
        return await asyncio.to_thread(research._ddgs_text, query, limit=limit)
    return await asyncio.to_thread(_builtin_search_sync, query, limit, time.monotonic() + _BUILTIN_SEARCH_SECONDS)


def _values() -> dict:
    from app.core.local_secrets import read_settings
    return read_settings("search")["values"]


def customer_search_notice(found: int, requested: int = 3) -> str:
    if not is_local() or _values().get("provider", "builtin") != "builtin" or found >= min(3, requested):
        return ""
    return ("Your built-in search found few public contacts. For more results, choose Tavily or Exa and save its API key "
            "in Keys and connections > Search.")


def _research_enabled() -> bool:
    try:
        from app.core.local_capabilities import enabled
    except ImportError:
        return True
    return enabled("research")


def _blocked_message() -> str:
    if not _research_enabled():
        from app.core.local_capabilities import blocked_message
        return blocked_message("research")
    name = _values().get("provider", "builtin")
    if name in {"tavily", "exa"}:
        return f"Save your {name.title()} search API key on the Keys and connections screen, then retry."
    if name == "openai":
        return "Select OpenAI and save your OpenAI API key and chat model on the Keys and connections screen, then retry."
    if name == "firecrawl":
        return "Save your Firecrawl URL and, if required, Firecrawl key on the Keys and connections screen, then retry."
    return SEARCH_UNAVAILABLE


def setup_reply(*, review_only: bool = False) -> dict | None:
    from app.core.local_mode import is_test_mode

    if not is_local() or is_test_mode():
        return None
    if review_only:
        try:
            from app.core.local_capabilities import capability
        except ImportError:
            return None  # The saved review still checks its AI connection.
        research = capability("research")
        if research["state"] != "off":
            return None
        message, label, href = research["next_step"], "Capabilities", "/settings/local/capabilities"
    elif search_ready():
        return None
    else:
        message, label, href = _blocked_message(), "Keys and connections", "/settings/local"
    return {"kind": "text", "body_md": message, "actions": [
        {"label": label, "href": href, "style": "primary"}], "citations": []}


def _reader_endpoint() -> tuple[str, str] | None:
    from app.core.providers_local import LocalProviderError, validate_endpoint
    from app.core.local_secrets import get_secret

    base = str(_values().get("firecrawl_url") or "").strip()
    if not base:
        return None
    try:
        return validate_endpoint(base).rstrip("/"), get_secret("search", "firecrawl_key") or ""
    except LocalProviderError:
        return None


def _openai_config() -> tuple[str, str, str] | None:
    from app.core.providers_local import LocalProviderError, load_local_config, validate_endpoint

    try:
        config = load_local_config()
        if config.provider != "openai":
            return None
        model = getattr(config, "_local_role_models", {}).get("chat", config.deployment_flash)
        if not config.api_key.get_secret_value() or not model:
            return None
        return (validate_endpoint(config.base_url or "https://api.openai.com/v1").rstrip("/"),
            config.api_key.get_secret_value(), model)
    except LocalProviderError:
        return None


class LocalHttpResearchProvider(HttpResearchProvider):
    async def scrape(self, url: str, *, main_only: bool = True) -> str:
        return await HttpResearchProvider("firecrawl", "").scrape(url, main_only=main_only)

    async def _search(self, query: str, **kwargs):
        import httpx
        from app.signals import research

        if not _research_enabled():
            raise ValueError(_blocked_message())
        limit = kwargs.get("limit") or self._limit
        if self.name == "duckduckgo":
            rows = await _builtin_search(query, limit)
            results = _sources_from_response("duckduckgo", {"results": [
                {**row, "url": row.get("href") or row.get("url"), "snippet": row.get("body") or row.get("description")}
                for row in rows]})
        elif self.name == "openai":
            from app.signals.local_reader import _search
            configured = _openai_config()
            if configured is None:
                raise ValueError(_blocked_message())
            try:
                async with httpx.AsyncClient(timeout=30.0, trust_env=False) as client:
                    response = await _search(client, {"query": query, "limit": limit,
                        "excludeDomains": list(kwargs.get("exclude_domains") or []),
                        **({"tbs": kwargs["tbs"]} if kwargs.get("tbs") else {})}, ("openai", *configured))
            except (httpx.HTTPError, ValueError, TimeoutError) as exc:
                _record_query(query, self.name, 0, reason="search_unavailable")
                exc.args = ("Your OpenAI search failed. " + _blocked_message(),)
                raise
            results = _sources_from_response("firecrawl", response.json())
        else:
            try:
                results = await super()._search(query, **kwargs)
            except (httpx.HTTPError, ValueError, TimeoutError) as exc:
                _record_query(query, self.name, 0, reason="search_unavailable")
                exc.args = (f"Your {self.name.title()} search failed. Check your {self.name.title()} "
                    "key and settings on the Keys and connections screen, then retry.",)
                raise
        excluded = kwargs.get("exclude_domains") or ()
        results[:] = [row for row in results if not any(
            (urlsplit(row["url"]).hostname or "").lower() == domain.lower()
            or (urlsplit(row["url"]).hostname or "").lower().endswith("." + domain.lower()) for domain in excluded)][:limit]
        if self.name != "duckduckgo":
            _record_query(query, self.name, len(results))
        for result in results:
            result["provider"] = self.name
            result["fetched_at"] = utcnow().isoformat()
            if kwargs.get("include_content") and not result.get("content"):
                try:
                    result["content"] = await self.scrape(result["url"])
                except (ValueError, TimeoutError, httpx.HTTPError) as exc:
                    research.log.warning("local_search_page_unavailable kind=%s", type(exc).__name__)
        return results


def search_provider(*, exclude: set[str] | None = None, limit: int = 6, **kwargs):
    if not is_local():
        from app.signals.research import _provider_from_env
        return _provider_from_env(exclude=exclude)
    if not _research_enabled():
        return None
    return _configured_provider(exclude=exclude, limit=limit, **kwargs)


def _configured_provider(*, exclude: set[str] | None = None, limit: int = 6, **kwargs):
    from app.core.local_secrets import get_secret
    name = _values().get("provider") or "builtin"
    if name in (exclude or set()) or name == "builtin" and "duckduckgo" in (exclude or set()):
        return None
    if name == "builtin":
        return LocalHttpResearchProvider("duckduckgo", "", limit=limit, **kwargs)
    if name in {"tavily", "exa"} and (key := get_secret("search", "api_key")):
        return LocalHttpResearchProvider(name, key, limit=limit, **kwargs)
    if name == "openai" and (configured := _openai_config()):
        return LocalHttpResearchProvider(name, configured[1], limit=limit, **kwargs)
    if name == "firecrawl" and _reader_endpoint():
        return LocalHttpResearchProvider(name, "", limit=limit, **kwargs)
    return None


def search_ready() -> bool:
    return search_provider() is not None and scrape_provider() is not None


def scrape_provider():
    if is_local() and _research_enabled():
        return LocalHttpResearchProvider("firecrawl" if _reader_endpoint() else "duckduckgo", "")
    return None


async def search(provider, query: str, *, purpose: str = "research.search", **kwargs):
    from app.credits import metered
    from app.tenancy import current_or_none

    scope = current_or_none()
    if is_local() and scope is not None:
        kwargs.setdefault("workspace_id", scope.workspace_id)
        kwargs.setdefault("venture_id", scope.venture_id)

    if not is_local():
        return await metered.search(provider, query, purpose=purpose, **kwargs)
    if provider is None:
        raise ValueError(_blocked_message())
    options = {name: kwargs.pop(name) for name in ("tbs", "include_content", "limit", "exclude_domains") if name in kwargs}
    batches = max(1, ((options.get("limit") or getattr(provider, "_limit", 6)) + 9) // 10) if provider.name == "firecrawl" else 1
    # shortcut: OpenAI search uses the existing call estimate, upgrade to token and tool receipts for exact spending history.
    return await metered.call(lambda: provider.search(query, **options), provider=provider.name, payer="founder",
        estimate_micro=0 if provider.name == "duckduckgo" else metered.price_micro(
            "research_call" if provider.name == "openai" else provider.name + "_search", batches),
        purpose=purpose, **kwargs)


async def local_search(query: str, *, limit: int = 6):
    if not isinstance(query, str) or not query.strip() or type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("Enter a search query and choose between 1 and 100 results, then retry.")
    return await search(search_provider(limit=limit), query.strip())


async def local_read(url: str, *, main_only: bool = True):
    return await fetch_page(url, main_only=main_only)


async def fetch_page(url: str, *, timeout_s: float = 25.0, main_only: bool = True) -> str:
    from core.ssrf import assert_safe_remote_url

    from app.credits import metered
    from app.tenancy import current_or_none

    provider = scrape_provider()
    if provider is None:
        raise ValueError(_blocked_message())
    try:
        from app.tools.reader import _target
        _target(url)
        async with asyncio.timeout(timeout_s):
            await asyncio.to_thread(assert_safe_remote_url, url)
    except (ValueError, TimeoutError) as exc:
        raise ValueError("Your page URL could not be checked. Choose a public HTTP or HTTPS URL without credentials, then retry.") from exc
    scope = current_or_none()
    try:
        return await metered.call(lambda: asyncio.wait_for(provider.scrape(url, main_only=main_only), timeout=timeout_s),
            purpose="research.fetch", provider=provider.name, payer="founder",
            estimate_micro=metered.price_micro("firecrawl_scrape") if provider.name == "firecrawl" else 0,
            workspace_id=scope.workspace_id if scope else None, venture_id=scope.venture_id if scope else None)
    except TimeoutError as exc:
        raise ValueError(READER_UNAVAILABLE) from exc


async def research_recon(idea: str, *, budget_s: float = 35.0) -> dict:
    from app.research.source_quality import relevant_search_lead
    from app.signals.research import _blocked, _structure_evidence

    try:
        with collect_search_diagnostics() as queries:
            rows = await asyncio.wait_for(local_search(idea, limit=6), timeout=budget_s)
    except SearchEngineLimited:
        return _blocked([idea], "search_engine_limited", SEARCH_LIMITED)
    except (ValueError, TimeoutError) as exc:
        return _blocked([idea], "search_unavailable", str(exc) or SEARCH_UNAVAILABLE)
    sources = [source for source in rows if relevant_search_lead(idea, source)]
    limited = search_was_limited(queries)
    if not sources:
        if limited:
            return _blocked([idea], "search_engine_limited", SEARCH_LIMITED)
        return _blocked([idea], "search_unavailable", "You have no relevant sources yet. Try a more specific search or add an original source.")
    structured = _structure_evidence(idea, sources)
    if limited:
        structured["summary"] += " " + SEARCH_LIMITED
    return {"queries": [idea], "sources": sources, "mode": "live", "reason": None,
            "provider": sources[0]["provider"], "errors": ["search_engine_limited"] if limited else [],
            "depth": "snippet_scan", **structured}
