"""Thin bridge to Panniantong/Agent-Reach — no upstream edits.

Agent Reach is an installer + doctor + a handful of in-process readers
(Web/Jina, YouTube transcribe, V2EX, Xueqiu) plus documented CLI recipes
(gh, yt-dlp, bili, twitter, rdt, opencli, mcporter). We call those as-is.

Do not import ``agent_reach.cli`` (installer). Read-only only — SKILL.md
forbids posting / commenting / liking.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from coding_agents.env import child_env, hosted_execution

_ROOT = Path(__file__).resolve().parents[4]
_AGENT_REACH = _ROOT / "vendor" / "agent-reach"

_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.I)
_GH_REPO_RE = re.compile(r"github\.com/([^/]+)/([^/#?]+)", re.I)
_BV_RE = re.compile(r"(BV[\w]+)", re.I)
_V2EX_TOPIC_RE = re.compile(r"v2ex\.com/t/(\d+)", re.I)

# SKILL.md routing table — query text → channel name.
_PLATFORM_QUERY = (
    ("youtube", re.compile(r"\b(youtube|youtu\.be|transcript)\b", re.I)),
    ("github", re.compile(r"\bgithub\b", re.I)),
    ("twitter", re.compile(r"\b(twitter|\btweets?\b|\bx\.com\b)\b", re.I)),
    ("reddit", re.compile(r"\breddit\b", re.I)),
    ("bilibili", re.compile(r"\b(bilibili|b[- ]?site|b站)\b", re.I)),
    ("v2ex", re.compile(r"\bv2ex\b", re.I)),
    ("xiaohongshu", re.compile(r"\b(xiaohongshu|rednote|小红书|xhs)\b", re.I)),
    ("xueqiu", re.compile(r"\b(xueqiu|雪球|stock quote)\b", re.I)),
    ("linkedin", re.compile(r"\blinkedin\b", re.I)),
    ("facebook", re.compile(r"\bfacebook\b", re.I)),
    ("instagram", re.compile(r"\binstagram\b", re.I)),
    ("rss", re.compile(r"\brss(\s+feed)?\b", re.I)),
)

_READ_TIMEOUT = 45
_SEARCH_TIMEOUT = 40
_MAX_CHARS = 40_000


def vendored() -> bool:
    return (_AGENT_REACH / "agent_reach" / "__init__.py").is_file()


def skill_dir() -> Path:
    return _AGENT_REACH / "agent_reach" / "skill"


def _ensure_vendor() -> None:
    if not vendored():
        raise ImportError("vendor/agent-reach not present, run scripts/sync_vendors.sh")
    path = str(_AGENT_REACH)
    if path not in sys.path:
        sys.path.insert(0, path)


def _run(
    argv: list[str],
    *,
    timeout: int = _READ_TIMEOUT,
    env: dict[str, str] | None = None,
    cwd: str | None = None,
) -> tuple[int, str]:
    """Run an upstream CLI exactly as SKILL.md documents. Never shell=True."""
    if not argv or not shutil.which(argv[0]):
        raise FileNotFoundError(argv[0] if argv else "missing command")
    child = child_env()
    if argv[0] == "twitter" and env and not hosted_execution():
        # Upstream's explicit local configuration supplies only these two
        # names. Hosted credentials need a separate founder-scoped adapter.
        child.update({name: value for name, value in env.items()
                      if name in {"TWITTER_AUTH_TOKEN", "TWITTER_CT0"}})
    proc = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=child,
        cwd=cwd,
        check=False,
    )
    out = (proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr else "")
    return proc.returncode, out[:_MAX_CHARS]


def doctor() -> dict[str, dict]:
    """``agent-reach doctor`` — their check_all, unmodified."""
    _ensure_vendor()
    from agent_reach.config import Config
    from agent_reach.doctor import check_all

    return check_all(Config(read_only=True))


def channel_for_url(url: str) -> Any | None:
    """First non-web channel whose can_handle() accepts the URL."""
    _ensure_vendor()
    from agent_reach.channels import get_all_channels

    for ch in get_all_channels():
        if getattr(ch, "name", "") == "web":
            continue
        try:
            if ch.can_handle(url):
                return ch
        except Exception:
            continue
    return None


def detect_platforms(query: str) -> list[str]:
    names: list[str] = []
    for name, pattern in _PLATFORM_QUERY:
        if pattern.search(query or "") and name not in names:
            names.append(name)
    return names


def urls_in(text: str) -> list[str]:
    return [m.group(0).rstrip(").,;]") for m in _URL_RE.finditer(text or "")]


def meta() -> dict[str, Any]:
    """Fast health snapshot — no live doctor probes (those hit the network)."""
    channels: list[dict[str, Any]] = []
    if vendored():
        try:
            _ensure_vendor()
            from agent_reach import __version__ as version
            from agent_reach.channels import get_all_channels

            for ch in get_all_channels():
                channels.append(
                    {
                        "name": ch.name,
                        "description": ch.description,
                        "backends": list(ch.backends),
                        "tier": ch.tier,
                    }
                )
        except Exception as exc:  # noqa: BLE001
            return {"vendored": True, "error": str(exc)[:160], "binaries": _binaries()}
        return {
            "vendored": True,
            "version": version,
            "skill": str(skill_dir() / "SKILL.md"),
            "channels": channels,
            "binaries": _binaries(),
        }
    return {"vendored": False, "binaries": _binaries()}


def _binaries() -> dict[str, bool]:
    names = (
        "yt-dlp",
        "gh",
        "bili",
        "twitter",
        "rdt",
        "opencli",
        "mcporter",
        "xhs",
        "ffmpeg",
    )
    return {name: bool(shutil.which(name)) for name in names}


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------


def read_url(url: str, *, specialist_only: bool = False) -> str:
    """Read a URL through the matching Agent-Reach channel / SKILL command.

    ``specialist_only`` skips the generic Jina WebChannel so ``web_fetch`` can
    keep its OpenWorker → Jina cascade for ordinary pages.
    """
    from app.core.local_mode import is_local
    if is_local():
        import asyncio
        from app.research.local_providers import local_read
        return asyncio.run(local_read(url))
    _ensure_vendor()
    from agent_reach.utils.url import normalize_public_http_url

    url = normalize_public_http_url(url)
    ch = channel_for_url(url)
    name = getattr(ch, "name", "") if ch is not None else ""
    if name == "youtube":
        return _read_youtube(url)
    if name == "github":
        return _read_github(url)
    if name == "rss":
        return _read_rss(url)
    if name == "v2ex":
        return _read_v2ex(url)
    if name == "twitter":
        return _read_cli(["twitter", "tweet", url], extra_env=_twitter_env())
    if name == "reddit":
        return _read_reddit(url)
    if name == "bilibili":
        return _read_bilibili(url)
    if name == "xueqiu":
        return _read_xueqiu_url(url)
    if name in {"xiaohongshu", "facebook", "instagram", "linkedin", "xiaoyuzhou"}:
        text = _read_opencli_or_cli(name, url)
        if text:
            return text
    if specialist_only:
        return ""
    from agent_reach.channels.web import WebChannel

    return WebChannel().read(url)


def _twitter_env() -> dict[str, str]:
    try:
        from agent_reach.channels.twitter import twitter_cli_child_env
        from agent_reach.config import Config

        return twitter_cli_child_env(Config(read_only=True))
    except Exception:
        return {}


def _read_cli(argv: list[str], *, extra_env: dict[str, str] | None = None) -> str:
    code, out = _run(argv, env=extra_env)
    if code != 0 and not out.strip():
        raise RuntimeError(f"{argv[0]} exited {code}")
    return out.strip()


def _read_youtube(url: str) -> str:
    """SKILL video.md: yt-dlp subs → dump-json → transcribe fallback."""
    if shutil.which("yt-dlp"):
        with tempfile.TemporaryDirectory(prefix="agent-reach-yt-") as tmp:
            outtmpl = str(Path(tmp) / "%(id)s")
            try:
                _run(
                    [
                        "yt-dlp",
                        "--write-sub",
                        "--write-auto-sub",
                        "--sub-lang",
                        "zh-Hans,zh,en",
                        "--skip-download",
                        "-o",
                        outtmpl,
                        url,
                    ],
                    timeout=60,
                )
            except (FileNotFoundError, subprocess.TimeoutExpired):
                pass
            vtts = sorted(Path(tmp).glob("*.vtt"))
            if vtts:
                body = vtts[0].read_text(encoding="utf-8", errors="replace")
                if body.strip():
                    return body[:_MAX_CHARS]
        try:
            code, raw = _run(["yt-dlp", "--dump-json", url], timeout=40)
            if code == 0 and raw.strip():
                info = json.loads(raw.splitlines()[0])
                title = info.get("title") or ""
                desc = info.get("description") or ""
                return f"# {title}\n\n{desc}"[:_MAX_CHARS]
        except Exception:
            pass
    try:
        from agent_reach.channels.youtube import YouTubeChannel

        return YouTubeChannel().transcribe(url)[:_MAX_CHARS]
    except Exception as exc:
        raise RuntimeError(f"youtube read failed: {exc}") from exc


def _read_github(url: str) -> str:
    match = _GH_REPO_RE.search(url)
    if not match or not shutil.which("gh"):
        from agent_reach.channels.web import WebChannel

        return WebChannel().read(url)
    slug = f"{match.group(1)}/{match.group(2).removesuffix('.git')}"
    code, out = _run(["gh", "repo", "view", slug])
    if code != 0 or not out.strip():
        from agent_reach.channels.web import WebChannel

        return WebChannel().read(url)
    return out.strip()


def _read_rss(url: str) -> str:
    """SKILL web.md feedparser snippet, as a function."""
    try:
        import feedparser
    except ImportError as exc:
        raise RuntimeError("feedparser not installed (Agent-Reach RSS backend)") from exc
    parsed = feedparser.parse(url)
    lines = [f"# {parsed.feed.get('title') or url}", ""]
    for entry in (parsed.entries or [])[:12]:
        title = entry.get("title") or "(untitled)"
        link = entry.get("link") or ""
        summary = re.sub(r"<[^>]+>", "", entry.get("summary") or "")[:280]
        lines.append(f"- [{title}]({link})")
        if summary:
            lines.append(f"  {summary}")
    return "\n".join(lines).strip()


def _read_v2ex(url: str) -> str:
    from agent_reach.channels.v2ex import V2EXChannel

    ch = V2EXChannel()
    match = _V2EX_TOPIC_RE.search(url)
    if match:
        topic = ch.get_topic(int(match.group(1)))
        replies = "\n".join(
            f"- {r.get('author')}: {r.get('content')}" for r in (topic.get("replies") or [])[:20]
        )
        return (
            f"# {topic.get('title')}\n\n{topic.get('content') or ''}\n\n{replies}"
        )[:_MAX_CHARS]
    return ch.get_hot_topics(limit=15).__repr__()[:_MAX_CHARS]


def _read_reddit(url: str) -> str:
    if shutil.which("rdt"):
        return _read_cli(["rdt", "search", url, "--limit", "5"])
    if shutil.which("opencli"):
        return _read_cli(["opencli", "reddit", "search", url, "-f", "yaml"])
    from agent_reach.channels.web import WebChannel

    return WebChannel().read(url)


def _read_bilibili(url: str) -> str:
    bv = None
    found = _BV_RE.search(url)
    if found:
        bv = found.group(1)
    if bv and shutil.which("bili"):
        return _read_cli(["bili", "video", bv])
    if shutil.which("opencli") and bv:
        return _read_cli(["opencli", "bilibili", "video", bv, "-f", "yaml"])
    from agent_reach.channels.web import WebChannel

    return WebChannel().read(url)


def _read_xueqiu_url(url: str) -> str:
    from agent_reach.channels.xueqiu import XueqiuChannel

    symbol = Path(urlparse(url).path).name.upper() or url
    try:
        quote = XueqiuChannel().get_stock_quote(symbol)
        return json.dumps(quote, ensure_ascii=False, indent=2)[:_MAX_CHARS]
    except Exception:
        if shutil.which("opencli"):
            return _read_cli(["opencli", "xueqiu", "stock", symbol, "-f", "yaml"])
        raise


def _read_opencli_or_cli(platform: str, url: str) -> str:
    if shutil.which("opencli"):
        cmd = {
            "xiaohongshu": ["opencli", "xiaohongshu", "note", url, "-f", "yaml"],
            "facebook": ["opencli", "facebook", "search", url, "-f", "yaml"],
            "instagram": ["opencli", "instagram", "user", url, "-f", "yaml"],
            "linkedin": ["opencli", "linkedin", "profile", url, "-f", "yaml"],
            "xiaoyuzhou": ["opencli", "xiaoyuzhou", "episode", url, "-f", "yaml"],
        }.get(platform)
        if cmd:
            try:
                return _read_cli(cmd)
            except Exception:
                return ""
    if platform == "xiaohongshu" and shutil.which("xhs"):
        try:
            return _read_cli(["xhs", "read", url])
        except Exception:
            return ""
    return ""


# ---------------------------------------------------------------------------
# Search — SKILL.md zero-config commands, plus in-process V2EX / Xueqiu
# ---------------------------------------------------------------------------


def search_platform(platform: str, query: str, *, limit: int = 5) -> dict[str, Any]:
    q = (query or "").strip()
    platform = (platform or "").strip().lower()
    if not q or not platform:
        return {"platform": platform, "hits": [], "error": "missing query"}
    from app.core.local_mode import is_local
    if is_local():
        import asyncio

        from app.research.local_providers import local_search
        try:
            hits = asyncio.run(local_search(q, limit=limit))
        except (ValueError, TimeoutError, OSError) as exc:
            from app.research.local_providers import SEARCH_UNAVAILABLE
            return {"platform": platform, "hits": [], "error": str(exc) if isinstance(exc, ValueError) else SEARCH_UNAVAILABLE}
        return {"platform": platform, "hits": hits, "error": None}
    try:
        if platform == "github":
            return _search_github(q, limit=limit)
        if platform == "youtube":
            return _search_youtube(q, limit=limit)
        if platform == "v2ex":
            return _search_v2ex(q, limit=limit)
        if platform == "xueqiu":
            return _search_xueqiu(q, limit=limit)
        if platform == "bilibili":
            return _search_bilibili(q, limit=limit)
        if platform == "twitter":
            return _search_twitter(q, limit=limit)
        if platform == "reddit":
            return _search_reddit(q, limit=limit)
        if platform == "xiaohongshu":
            return _search_xiaohongshu(q, limit=limit)
        if platform == "exa_search" or platform == "exa":
            return _search_exa(q, limit=limit)
        return {
            "platform": platform,
            "hits": [],
            "error": f"no search backend wired for {platform}",
        }
    except FileNotFoundError as exc:
        return {"platform": platform, "hits": [], "error": f"{exc} not on PATH"}
    except subprocess.TimeoutExpired:
        return {"platform": platform, "hits": [], "error": "timeout"}
    except Exception as exc:  # noqa: BLE001
        return {"platform": platform, "hits": [], "error": str(exc)[:200]}


def _hits(platform: str, rows: list[dict[str, str]], *, backend: str) -> dict[str, Any]:
    return {"platform": platform, "backend": backend, "hits": rows, "error": None}


def _search_github(query: str, *, limit: int) -> dict[str, Any]:
    code, out = _run(
        [
            "gh",
            "search",
            "repos",
            query,
            "--sort",
            "stars",
            "--limit",
            str(limit),
            "--json",
            "url,fullName,description,stargazersCount",
        ],
        timeout=_SEARCH_TIMEOUT,
    )
    if code != 0:
        return {"platform": "github", "hits": [], "error": out[:200] or f"gh exit {code}"}
    rows = []
    try:
        payload = json.loads(out.strip() or "[]")
    except json.JSONDecodeError:
        return {"platform": "github", "hits": [], "error": "gh returned non-json"}
    for item in payload[:limit]:
        rows.append(
            {
                "title": item.get("fullName") or item.get("url") or "",
                "url": item.get("url") or "",
                "snippet": (item.get("description") or "")[:240],
            }
        )
    return _hits("github", rows, backend="gh CLI")


def _search_youtube(query: str, *, limit: int) -> dict[str, Any]:
    code, out = _run(
        ["yt-dlp", "--dump-json", f"ytsearch{limit}:{query}"],
        timeout=60,
    )
    rows = []
    for line in out.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            info = json.loads(line)
        except json.JSONDecodeError:
            continue
        url = info.get("webpage_url") or info.get("original_url") or ""
        if not url and info.get("id"):
            url = f"https://www.youtube.com/watch?v={info['id']}"
        rows.append(
            {
                "title": info.get("title") or url,
                "url": url,
                "snippet": (info.get("description") or "")[:240],
            }
        )
        if len(rows) >= limit:
            break
    if not rows:
        return {
            "platform": "youtube",
            "hits": [],
            "error": out[:200] if code else "no yt-dlp results",
        }
    return _hits("youtube", rows, backend="yt-dlp")


def _search_v2ex(query: str, *, limit: int) -> dict[str, Any]:
    from agent_reach.channels.v2ex import V2EXChannel

    ch = V2EXChannel()
    found = ch.search(query, limit=limit)
    if found and isinstance(found[0], dict) and found[0].get("error"):
        # Their search() documents that the public API has no search endpoint.
        hot = ch.get_hot_topics(limit=limit)
        rows = [
            {
                "title": item.get("title") or "",
                "url": item.get("url") or "",
                "snippet": (item.get("content") or "")[:240],
            }
            for item in hot[:limit]
        ]
        return {
            "platform": "v2ex",
            "backend": "V2EX public API",
            "hits": rows,
            "error": None,
            "note": found[0]["error"],
        }
    rows = [
        {
            "title": item.get("title") or "",
            "url": item.get("url") or "",
            "snippet": (item.get("snippet") or item.get("content") or "")[:240],
        }
        for item in found[:limit]
        if isinstance(item, dict)
    ]
    return _hits("v2ex", rows, backend="V2EX public API")


def _search_xueqiu(query: str, *, limit: int) -> dict[str, Any]:
    from agent_reach.channels.xueqiu import XueqiuChannel

    stocks = XueqiuChannel().search_stock(query, limit=limit)
    rows = []
    for item in stocks[:limit]:
        if not isinstance(item, dict):
            continue
        symbol = item.get("symbol") or item.get("code") or ""
        rows.append(
            {
                "title": item.get("name") or symbol,
                "url": f"https://xueqiu.com/S/{symbol}" if symbol else "",
                "snippet": json.dumps(item, ensure_ascii=False)[:240],
            }
        )
    return _hits("xueqiu", rows, backend="XueqiuChannel")


def _search_bilibili(query: str, *, limit: int) -> dict[str, Any]:
    if shutil.which("bili"):
        code, out = _run(
            ["bili", "search", query, "--type", "video", "-n", str(limit)],
            timeout=_SEARCH_TIMEOUT,
        )
        return _hits(
            "bilibili",
            _lines_to_hits(out, host_hint="bilibili.com"),
            backend="bili-cli",
        ) if (code == 0 and out.strip()) else {
            "platform": "bilibili",
            "hits": [],
            "error": out[:200] or f"bili exit {code}",
        }
    if shutil.which("opencli"):
        code, out = _run(
            ["opencli", "bilibili", "search", query, "-f", "yaml"],
            timeout=_SEARCH_TIMEOUT,
        )
        return _hits(
            "bilibili",
            _lines_to_hits(out, host_hint="bilibili.com"),
            backend="OpenCLI",
        )
    return {"platform": "bilibili", "hits": [], "error": "bili-cli / opencli not on PATH"}


def _search_twitter(query: str, *, limit: int) -> dict[str, Any]:
    env = _twitter_env()
    if shutil.which("twitter"):
        code, out = _run(
            ["twitter", "search", query, "-n", str(limit)],
            env=env,
            timeout=_SEARCH_TIMEOUT,
        )
        if code == 0 and out.strip():
            return _hits("twitter", _lines_to_hits(out, host_hint="x.com"), backend="twitter-cli")
    if shutil.which("opencli"):
        code, out = _run(
            ["opencli", "twitter", "search", query, "-f", "yaml"],
            timeout=_SEARCH_TIMEOUT,
        )
        if code == 0 and out.strip():
            return _hits("twitter", _lines_to_hits(out, host_hint="x.com"), backend="OpenCLI")
    return {"platform": "twitter", "hits": [], "error": "twitter-cli / opencli not configured"}


def _search_reddit(query: str, *, limit: int) -> dict[str, Any]:
    if shutil.which("opencli"):
        code, out = _run(
            ["opencli", "reddit", "search", query, "-f", "yaml"],
            timeout=_SEARCH_TIMEOUT,
        )
        if code == 0 and out.strip():
            return _hits("reddit", _lines_to_hits(out, host_hint="reddit.com"), backend="OpenCLI")
    if shutil.which("rdt"):
        code, out = _run(
            ["rdt", "search", query, "--limit", str(limit)],
            timeout=_SEARCH_TIMEOUT,
        )
        if code == 0 and out.strip():
            return _hits("reddit", _lines_to_hits(out, host_hint="reddit.com"), backend="rdt-cli")
    return {"platform": "reddit", "hits": [], "error": "OpenCLI / rdt-cli not on PATH"}


def _search_xiaohongshu(query: str, *, limit: int) -> dict[str, Any]:
    if shutil.which("opencli"):
        code, out = _run(
            ["opencli", "xiaohongshu", "search", query, "-f", "yaml"],
            timeout=_SEARCH_TIMEOUT,
        )
        if code == 0 and out.strip():
            return _hits(
                "xiaohongshu",
                _lines_to_hits(out, host_hint="xiaohongshu.com"),
                backend="OpenCLI",
            )
    if shutil.which("xhs"):
        code, out = _run(["xhs", "search", query], timeout=_SEARCH_TIMEOUT)
        if code == 0 and out.strip():
            return _hits(
                "xiaohongshu",
                _lines_to_hits(out, host_hint="xiaohongshu.com"),
                backend="xhs-cli",
            )
    return {"platform": "xiaohongshu", "hits": [], "error": "OpenCLI / xhs-cli not on PATH"}


def _search_exa(query: str, *, limit: int) -> dict[str, Any]:
    if not shutil.which("mcporter"):
        return {"platform": "exa", "hits": [], "error": "mcporter not on PATH"}
    code, out = _run(
        [
            "mcporter",
            "call",
            "exa.web_search_exa",
            f"query={query}",
            f"numResults={limit}",
        ],
        timeout=_SEARCH_TIMEOUT,
    )
    if code != 0 or not out.strip():
        return {"platform": "exa", "hits": [], "error": out[:200] or f"mcporter exit {code}"}
    return _hits("exa", _lines_to_hits(out), backend="mcporter/Exa")


def _lines_to_hits(text: str, *, host_hint: str = "") -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for match in _URL_RE.finditer(text or ""):
        url = match.group(0).rstrip(").,;'\"")
        if url in seen:
            continue
        if host_hint and host_hint not in url:
            continue
        seen.add(url)
        rows.append({"title": url, "url": url, "snippet": ""})
        if len(rows) >= 8:
            break
    if not rows and (text or "").strip():
        snippet = " ".join(text.split())[:400]
        rows.append({"title": snippet[:80], "url": "", "snippet": snippet})
    return rows
