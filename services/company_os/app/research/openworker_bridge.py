"""Thin bridge to Andrew Ng's OpenWorker (vendor/openworker) — no upstream edits.

Usable leaf modules (tested without full OpenWorker install):
- ``coworker/web/providers.py`` — DuckDuckGo / Tavily / Brave search
- ``coworker/web/guard.py`` + ``fetch.py`` — SSRF-safe page fetch (aisuite stubbed only
  for ToolMetadata; OpenWorker source untouched)

Full desktop/connectors remain a sidecar:
  make openworker-server
"""

from __future__ import annotations

import importlib.util
import os
import sys
import types
from pathlib import Path
from types import ModuleType
from typing import Any, Callable

_ROOT = Path(__file__).resolve().parents[4]
_OPENWORKER = _ROOT / "vendor" / "openworker"
_WEB = _OPENWORKER / "coworker" / "web"
_PROVIDERS: ModuleType | None = None
_FETCH_TOOL: Callable[..., Any] | None = None
_FETCH_LOAD_ERROR: str | None = None


def openworker_available() -> bool:
    return (_WEB / "providers.py").is_file()


def _ensure_aisuite_stub() -> None:
    """Only needed so fetch.py's ToolMetadata import succeeds — we never call aisuite."""
    if "aisuite" in sys.modules:
        return
    ai = types.ModuleType("aisuite")

    class ToolMetadata:  # noqa: N801 — match aisuite API surface
        def __init__(self, **kwargs: Any) -> None:
            self.__dict__.update(kwargs)

    ai.ToolMetadata = ToolMetadata  # type: ignore[attr-defined]
    sys.modules["aisuite"] = ai


def _load_file(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _providers_mod() -> ModuleType:
    global _PROVIDERS
    if _PROVIDERS is not None:
        return _PROVIDERS
    path = _WEB / "providers.py"
    if not path.is_file():
        raise ImportError("vendor/openworker not present, run scripts/sync_vendors.sh")
    _PROVIDERS = _load_file("company_os_openworker_web_providers", path)
    return _PROVIDERS


def _fetch_tool() -> Callable[..., Any]:
    """Load OpenWorker make_web_fetch_tool() with package wiring + aisuite stub."""
    global _FETCH_TOOL, _FETCH_LOAD_ERROR
    if _FETCH_TOOL is not None:
        return _FETCH_TOOL
    if _FETCH_LOAD_ERROR:
        raise ImportError(_FETCH_LOAD_ERROR)
    try:
        _ensure_aisuite_stub()
        guard = _load_file("coworker.web.guard", _WEB / "guard.py")
        # Relative import in fetch.py: `from .guard import get_checked`
        pkg = types.ModuleType("coworker")
        web = types.ModuleType("coworker.web")
        web.guard = guard  # type: ignore[attr-defined]
        sys.modules.setdefault("coworker", pkg)
        sys.modules["coworker.web"] = web
        sys.modules["coworker.web.guard"] = guard
        fetch = _load_file("coworker.web.fetch", _WEB / "fetch.py")
        _FETCH_TOOL = fetch.make_web_fetch_tool()
        return _FETCH_TOOL
    except Exception as exc:  # noqa: BLE001
        _FETCH_LOAD_ERROR = str(exc)[:200]
        raise ImportError(_FETCH_LOAD_ERROR) from exc


def search_web(
    query: str,
    *,
    max_results: int = 6,
    provider: str | None = None,
) -> list[dict[str, str]]:
    """Run OpenWorker's web search. Default: keyless DuckDuckGo (`ddgs`)."""
    from app.core.local_mode import is_local
    if is_local():
        import asyncio

        from app.research.local_providers import local_search
        return asyncio.run(local_search(query, limit=max_results))
    mod = _providers_mod()
    name = (provider or os.environ.get("OPENWORKER_SEARCH_PROVIDER") or "duckduckgo").lower()
    api_key: str | None = None
    if name == "tavily":
        api_key = os.environ.get("TAVILY_API_KEY")
    elif name == "brave":
        api_key = os.environ.get("BRAVE_API_KEY") or os.environ.get("BRAVE_SEARCH_API_KEY")
    try:
        eng = mod.build_provider(name, api_key)
    except ValueError:
        eng = mod.DuckDuckGoProvider()
        name = "duckduckgo"
    rows = eng.search(query, max_results=max_results) or []
    out: list[dict[str, str]] = []
    for r in rows:
        d = r.to_dict() if hasattr(r, "to_dict") else {}
        url = str(d.get("url") or "")
        if not url:
            continue
        out.append(
            {
                "url": url,
                "title": str(d.get("title") or url).strip(),
                "snippet": str(d.get("snippet") or "")[:600],
                "provider": f"openworker:{name}",
            }
        )
    return out


def fetch_url(url: str, *, max_chars: int = 20000) -> dict[str, Any]:
    """SSRF-safe page fetch via OpenWorker guard + html→text (tested)."""
    from app.core.local_mode import is_local
    if is_local():
        import asyncio
        from app.research.local_providers import local_read
        page = asyncio.run(local_read(url))
        return {"text": page[:max_chars], "url": page.metadata.get("sourceURL", url),
            "title": page.metadata.get("title", url), "fetched_at": page.metadata["fetched_at"]}
    tool = _fetch_tool()
    result = tool(url, max_chars=max_chars)
    if not isinstance(result, dict):
        return {"error": "unexpected_fetch_result"}
    return result


def check_url_safe(url: str) -> str | None:
    """Return block reason or None if OpenWorker guard allows the URL."""
    try:
        guard = _load_file("coworker.web.guard", _WEB / "guard.py")
        return guard.check_url(url)
    except Exception as exc:  # noqa: BLE001
        return f"guard_unavailable:{exc}"[:120]


class OpenWorkerSearchProvider:
    """Async ResearchProvider-compatible wrapper around OpenWorker search."""

    name = "openworker"

    async def search(self, query: str) -> list[dict[str, str]]:
        import asyncio

        return await asyncio.to_thread(search_web, query, max_results=6)


def openworker_meta() -> dict[str, Any]:
    fetch_ok = False
    fetch_err = _FETCH_LOAD_ERROR
    try:
        _fetch_tool()
        fetch_ok = True
    except Exception as exc:  # noqa: BLE001
        fetch_err = str(exc)[:120]
    server = _probe_openworker_server()
    return {
        "vendored": openworker_available(),
        "path": str(_OPENWORKER) if openworker_available() else None,
        "upstream": "https://github.com/andrewyng/openworker",
        "search_provider": os.environ.get("OPENWORKER_SEARCH_PROVIDER", "duckduckgo"),
        "search_ok": openworker_available(),
        "fetch_ok": fetch_ok,
        "fetch_error": fetch_err,
        "guard_ok": (_WEB / "guard.py").is_file(),
        "full_install": "pip install -e vendor/openworker  # optional: full agent server",
        "server": server,
        "capabilities_bridged": ["web_search", "web_fetch", "url_guard"],
        "capabilities_via_sidecar": [
            "files",
            "shell",
            "git",
            "browser",
            "connectors",
            "mcp",
            "skills",
            "automations",
        ],
    }


def _probe_openworker_server() -> dict[str, Any]:
    """Detect a running openworker-server without importing coworker."""
    import urllib.request

    port = os.environ.get("OPENWORKER_PORT", "8765")
    url = f"http://127.0.0.1:{port}/health"
    try:
        with urllib.request.urlopen(url, timeout=0.6) as resp:
            return {"ok": resp.status < 400, "url": f"http://127.0.0.1:{port}", "status": resp.status}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "url": f"http://127.0.0.1:{port}", "error": str(exc)[:120]}
