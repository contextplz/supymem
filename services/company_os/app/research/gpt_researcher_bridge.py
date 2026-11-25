"""Optional GPT Researcher deep report (vendor/gpt-researcher)."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[4]
_GPR = _ROOT / "vendor" / "gpt-researcher"


def _ensure_path() -> bool:
    if not _GPR.is_dir():
        return False
    p = str(_GPR)
    if p not in sys.path:
        sys.path.insert(0, p)
    return True


async def run_gpt_researcher(
    query: str,
    *,
    report_type: str = "research_report",
    timeout_s: float = 180.0,
) -> dict[str, Any]:
    """Run GPT Researcher when installed; returns empty dict on failure."""
    if os.environ.get("DEEP_RESEARCH", "0").lower() in {"0", "false", "no"}:
        return {"ok": False, "reason": "deep_research_disabled"}
    from app.core.local_mode import is_local
    if is_local():
        from app.research.explore import explore_web, render_explore
        result = await asyncio.wait_for(explore_web(query, public_only=True), timeout=timeout_s)
        if result.get("mode") != "live":
            return {"ok": False, "reason": result.get("summary") or "You have no usable sources yet. Configure your search provider, then retry."}
        return {"ok": True, "engine": "configured_search", "report_markdown": render_explore(result),
                "source_urls": [hit["url"] for hit in result["hits"]]}
    if not _ensure_path():
        return {"ok": False, "reason": "gpt_researcher_not_vendored"}
    try:
        from gpt_researcher import GPTResearcher  # type: ignore
    except ImportError as exc:
        return {"ok": False, "reason": f"gpt_researcher_import:{exc}"}

    async def _run() -> dict[str, Any]:
        researcher = GPTResearcher(
            query=query,
            report_type=report_type,
            report_format="markdown",
            verbose=False,
        )
        await researcher.conduct_research()
        report = await researcher.write_report()
        sources = list(getattr(researcher, "visited_urls", None) or [])
        return {
            "ok": True,
            "engine": "gpt_researcher",
            "report_markdown": report or "",
            "source_urls": sources[:40],
            "research_costs": getattr(researcher, "research_costs", None),
        }

    try:
        return await asyncio.wait_for(_run(), timeout=timeout_s)
    except TimeoutError:
        return {"ok": False, "reason": "gpt_researcher_timeout"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": str(exc)[:200]}
