"""Deterministic branded card renderer (Pillow).

No LLM. No image model. Spec → PNG. Fail closed if Pillow or fonts missing.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# services/company_os/app/content/visuals/render.py → repo root is parents[5]
REPO_DATA = Path(__file__).resolve().parents[5] / "data" / "content_visuals"


def _hex_to_rgb(value: str) -> tuple[int, int, int]:
    raw = (value or "").strip().lstrip("#")
    if len(raw) == 3:
        raw = "".join(ch * 2 for ch in raw)
    if len(raw) != 6:
        return (11, 31, 23)
    try:
        return int(raw[0:2], 16), int(raw[2:4], 16), int(raw[4:6], 16)
    except ValueError:
        return (11, 31, 23)


def _font(size: int, *, bold: bool = False):
    from PIL import ImageFont

    candidates = [
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf" if bold else "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/System/Library/Fonts/Supplemental/Helvetica.ttc",
        "/Library/Fonts/Arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size=size)
        except Exception:
            continue
    return ImageFont.load_default()


def _wrap(draw, text: str, font, max_width: int) -> list[str]:
    words = (text or "").split()
    if not words:
        return []
    lines: list[str] = []
    current = words[0]
    for word in words[1:]:
        trial = f"{current} {word}"
        if draw.textlength(trial, font=font) <= max_width:
            current = trial
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


def render_png(spec: dict[str, Any], dest: Path) -> dict[str, Any]:
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return {"ok": False, "error": "pillow_not_installed"}

    width = int(spec.get("width") or 1080)
    height = int(spec.get("height") or 1080)
    bg = _hex_to_rgb(str(spec.get("bg") or "#0B1F17"))
    fg = _hex_to_rgb(str(spec.get("fg") or "#E8F0EC"))
    accent = _hex_to_rgb(str(spec.get("accent") or "#1B4D3E"))
    template = str(spec.get("template") or "insight_card")

    img = Image.new("RGB", (width, height), bg)
    draw = ImageDraw.Draw(img)

    # Accent bar.
    draw.rectangle([0, 0, 18, height], fill=accent)
    # Soft top band.
    draw.rectangle([0, 0, width, 120], fill=tuple(max(0, c - 8) for c in bg))

    kicker_font = _font(28, bold=True)
    headline_font = _font(64 if template != "quote_card" else 56, bold=True)
    sub_font = _font(32)
    footer_font = _font(24)
    point_font = _font(30)

    margin = 72
    y = 56
    kicker = str(spec.get("kicker") or "").upper()
    if kicker:
        draw.text((margin, y), kicker, fill=accent, font=kicker_font)
        y += 70

    headline = str(spec.get("headline") or "")
    for line in _wrap(draw, headline, headline_font, width - margin * 2)[:5]:
        draw.text((margin, y), line, fill=fg, font=headline_font)
        y += int(headline_font.size * 1.2) + 8

    y += 24
    sub = str(spec.get("subheadline") or "")
    if sub:
        muted = (
            min(255, (fg[0] + bg[0]) // 2 + 20),
            min(255, (fg[1] + bg[1]) // 2 + 20),
            min(255, (fg[2] + bg[2]) // 2 + 20),
        )
        for line in _wrap(draw, sub, sub_font, width - margin * 2)[:4]:
            draw.text((margin, y), line, fill=muted, font=sub_font)
            y += int(sub_font.size * 1.35)

    points = spec.get("points") if isinstance(spec.get("points"), list) else []
    if template in {"before_after", "stat_card"} and points:
        y += 36
        for point in points[:4]:
            draw.ellipse([margin, y + 10, margin + 14, y + 24], fill=accent)
            for line in _wrap(draw, str(point), point_font, width - margin * 2 - 40)[:2]:
                draw.text((margin + 36, y), line, fill=fg, font=point_font)
                y += int(point_font.size * 1.35)
            y += 12

    if template == "quote_card":
        draw.text((margin, 160), "“", fill=accent, font=_font(120, bold=True))

    footer = str(spec.get("footer") or "")
    if footer:
        draw.text(
            (margin, height - 72),
            footer,
            fill=tuple(min(255, c + 30) for c in accent),
            font=footer_font,
        )

    dest.parent.mkdir(parents=True, exist_ok=True)
    img.save(dest, format="PNG", optimize=True)
    return {"ok": True, "path": str(dest), "bytes": dest.stat().st_size}


def render_to_workspace(
    *, workspace_id: str, post_id: str, spec: dict[str, Any]
) -> dict[str, Any]:
    from app.content.visuals.spec import validate_spec

    errors = validate_spec(spec)
    if errors:
        return {"ok": False, "error": ",".join(errors)}

    dest = REPO_DATA / workspace_id / f"{post_id}.png"
    try:
        return render_png(spec, dest)
    except Exception as exc:
        log.exception("visual render failed")
        return {"ok": False, "error": str(exc)[:200]}
