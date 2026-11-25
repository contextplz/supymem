"""Visual spec — tiny JSON the renderer paints. Almost no LLM tokens.

Tier 0: derive headline from the post body (0 extra tokens).
Tier 1: use a visual object already on the draft (same writer call).
Never invent unconstrained HTML. Template allowlist only.
"""

from __future__ import annotations

import re
from typing import Any

TEMPLATES = frozenset(
    {
        "insight_card",
        "stat_card",
        "quote_card",
        "before_after",
        "screenshot_frame",
    }
)

_MAX_HEADLINE = 90
_MAX_SUB = 140


def _first_line(body: str) -> str:
    for line in (body or "").splitlines():
        cleaned = line.strip().strip('"').strip("'")
        if cleaned:
            return cleaned[:_MAX_HEADLINE]
    return "One idea. Said clearly."


def _pick_template(body: str, preferred: str | None = None) -> str:
    if preferred and preferred in TEMPLATES:
        return preferred
    text = (body or "").lower()
    if re.search(r"\b\d+\b", text) and any(
        w in text for w in ("→", "->", "before", "after", "from", "to")
    ):
        return "before_after"
    if re.search(r"\b\d{2,}\b", text) or "%" in text:
        return "stat_card"
    if text.startswith('"') or "i don't" in text or "unpopular" in text:
        return "quote_card"
    return "insight_card"


def build_spec(
    *,
    body: str,
    template: str | None = None,
    brand: dict[str, Any] | None = None,
    existing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a renderable visual spec. Prefer existing writer output when valid."""
    brand = brand or {}
    existing = existing or {}

    chosen = str(existing.get("template") or template or "")
    if chosen not in TEMPLATES:
        chosen = _pick_template(body, None)

    headline = str(existing.get("headline") or "").strip()
    if not headline:
        headline = _first_line(body)
    headline = headline[:_MAX_HEADLINE]

    sub = str(existing.get("subheadline") or existing.get("sub") or "").strip()
    if not sub:
        lines = [ln.strip() for ln in (body or "").splitlines() if ln.strip()]
        sub = lines[1][:_MAX_SUB] if len(lines) > 1 else ""
    sub = sub[:_MAX_SUB]

    points = existing.get("points") if isinstance(existing.get("points"), list) else []
    points = [str(p).strip()[:60] for p in points if str(p).strip()][:4]

    colors = brand.get("colors") if isinstance(brand.get("colors"), list) else []
    bg = str(colors[0] if colors else brand.get("bg") or "#0B1F17")
    fg = str(colors[1] if len(colors) > 1 else brand.get("fg") or "#E8F0EC")
    accent = str(brand.get("accent") or (colors[2] if len(colors) > 2 else "#1B4D3E"))
    wordmark = str(brand.get("wordmark") or existing.get("kicker") or "Company OS")[:40]
    footer = str(existing.get("footer") or brand.get("footer") or wordmark)[:60]

    return {
        "template": chosen,
        "headline": headline,
        "subheadline": sub,
        "points": points,
        "kicker": wordmark.upper(),
        "footer": footer,
        "bg": bg,
        "fg": fg,
        "accent": accent,
        "width": 1080,
        "height": 1080,
    }


def validate_spec(spec: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if spec.get("template") not in TEMPLATES:
        errors.append(f"unknown_template:{spec.get('template')}")
    if not str(spec.get("headline") or "").strip():
        errors.append("missing_headline")
    if len(str(spec.get("headline") or "")) > _MAX_HEADLINE:
        errors.append("headline_too_long")
    return errors
