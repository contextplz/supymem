"""Deterministic brand kit from the founder's sentence (45 §3.1).

Same idea → same palette, type, and wordmark. Variant index only reshuffles
within the curated sets so "Try another look" is free and still deterministic.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

AA_RATIO = 4.5


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


@lru_cache(maxsize=1)
def _palettes() -> dict[str, Any]:
    path = _repo_root() / "config" / "firstlook" / "palettes.yaml"
    with path.open() as handle:
        return yaml.safe_load(handle) or {}


@lru_cache(maxsize=1)
def _type_pairs() -> dict[str, Any]:
    path = _repo_root() / "config" / "firstlook" / "type_pairs.yaml"
    with path.open() as handle:
        return yaml.safe_load(handle) or {}


@dataclass(frozen=True)
class TypePair:
    name: str
    display: str
    body: str
    display_weight: int
    display_tracking: str
    body_leading: float


@dataclass(frozen=True)
class Colors:
    page: str
    surface: str
    border: str
    muted: str
    ink: str
    accent: str
    accent_ink: str
    family: str
    tone: str


@dataclass(frozen=True)
class BrandKit:
    wordmark: str
    monogram: str
    colors: Colors
    type_pair: TypePair
    seed: str
    variant_index: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "wordmark": self.wordmark,
            "monogram": self.monogram,
            "colors": asdict(self.colors),
            "type_pair": asdict(self.type_pair),
            "seed": self.seed,
            "variant_index": self.variant_index,
        }

    @property
    def hash(self) -> str:
        blob = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode()).hexdigest()


def _seed_int(idea_text: str, variant_index: int = 0) -> int:
    material = f"{idea_text.strip()}\0{variant_index}".encode()
    return int.from_bytes(hashlib.sha256(material).digest()[:8], "big")


def _hsl_to_hex(h: float, s: float, l: float) -> str:  # noqa: E741
    h = h % 360.0
    s = max(0.0, min(1.0, s))
    l = max(0.0, min(1.0, l))  # noqa: E741
    c = (1.0 - abs(2.0 * l - 1.0)) * s
    x = c * (1.0 - abs((h / 60.0) % 2.0 - 1.0))
    m = l - c / 2.0
    if h < 60:
        r, g, b = c, x, 0.0
    elif h < 120:
        r, g, b = x, c, 0.0
    elif h < 180:
        r, g, b = 0.0, c, x
    elif h < 240:
        r, g, b = 0.0, x, c
    elif h < 300:
        r, g, b = x, 0.0, c
    else:
        r, g, b = c, 0.0, x
    return "#{:02x}{:02x}{:02x}".format(
        round((r + m) * 255),
        round((g + m) * 255),
        round((b + m) * 255),
    )


def _hex_to_rgb(hex_color: str) -> tuple[float, float, float]:
    raw = hex_color.lstrip("#")
    return tuple(int(raw[i : i + 2], 16) / 255.0 for i in (0, 2, 4))  # type: ignore[return-value]


def _channel(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def relative_luminance(hex_color: str) -> float:
    r, g, b = _hex_to_rgb(hex_color)
    return 0.2126 * _channel(r) + 0.7152 * _channel(g) + 0.0722 * _channel(b)


def contrast_ratio(a: str, b: str) -> float:
    la, lb = relative_luminance(a), relative_luminance(b)
    lighter, darker = max(la, lb), min(la, lb)
    return (lighter + 0.05) / (darker + 0.05)


def meets_aa(fg: str, bg: str) -> bool:
    return contrast_ratio(fg, bg) >= AA_RATIO


def _wordmark(name: str | None, idea_text: str) -> str:
    if name and name.strip():
        return name.strip()[:40]
    words = [w for w in idea_text.strip().split() if w.isalpha()]
    if not words:
        return "Venture"
    if len(words) == 1:
        return words[0][:1].upper() + words[0][1:24]
    return (words[0][:1].upper() + words[0][1:12] + words[1][:1].upper()).strip()[:28]


def _monogram(wordmark: str) -> str:
    letters = [c for c in wordmark if c.isalpha()]
    if not letters:
        return "V"
    if len(letters) == 1:
        return letters[0].upper()
    return (letters[0] + letters[-1]).upper()


def _accent_pair(hue: float, sat: float, tone: str, cfg: dict[str, Any]) -> tuple[str, str]:
    accent_cfg = cfg.get("accent") or {}
    start = float(accent_cfg.get(f"{tone}_start", 0.4))
    step = float(accent_cfg.get("step", 0.02))
    # Prefer light label on dark accent for light pages, and vice versa.
    label = "#ffffff" if tone == "light" else "#0a0a0a"
    lightness = start
    for _ in range(40):
        accent = _hsl_to_hex(hue, sat + 0.12, lightness)
        if meets_aa(label, accent):
            return accent, label
        lightness = lightness - step if tone == "light" else lightness + step
        lightness = max(0.12, min(0.88, lightness))
    # Last resort: near-black / near-white accent that always clears.
    fallback = "#1a1a1a" if tone == "light" else "#f2f2f2"
    return fallback, "#ffffff" if tone == "light" else "#0a0a0a"


def build_brandkit(
    idea_text: str,
    *,
    venture_name: str | None = None,
    variant_index: int = 0,
) -> BrandKit:
    """Pick palette + type from sha256(idea). Never random RGB."""
    cfg = _palettes()
    families = list(cfg.get("families") or [])
    tones_cfg = cfg.get("tones") or {}
    if not families:
        raise RuntimeError("config/firstlook/palettes.yaml has no families")

    seed = _seed_int(idea_text, variant_index)
    family = families[seed % len(families)]
    tone = "light" if ((seed >> 8) % 2) == 0 else "dark"
    tone_vals = tones_cfg.get(tone) or tones_cfg.get("light") or {}

    hue = float(family["hue"])
    sat = float(family["saturation"])
    colors = Colors(
        page=_hsl_to_hex(hue, sat * 0.25, float(tone_vals["page"])),
        surface=_hsl_to_hex(hue, sat * 0.35, float(tone_vals["surface"])),
        border=_hsl_to_hex(hue, sat * 0.20, float(tone_vals["border"])),
        muted=_hsl_to_hex(hue, sat * 0.15, float(tone_vals["muted"])),
        ink=_hsl_to_hex(hue, sat * 0.10, float(tone_vals["ink"])),
        accent="#000000",
        accent_ink="#ffffff",
        family=str(family["name"]),
        tone=tone,
    )
    # Recompute with AA-checked accent.
    accent, accent_ink = _accent_pair(hue, sat, tone, cfg)
    # Ensure body text clears AA on page.
    ink = colors.ink
    if not meets_aa(ink, colors.page):
        ink = "#0f0f0f" if tone == "light" else "#f5f5f5"
    muted = colors.muted
    if not meets_aa(muted, colors.page):
        muted = "#4a4a4a" if tone == "light" else "#b0b0b0"
    colors = Colors(
        page=colors.page,
        surface=colors.surface,
        border=colors.border,
        muted=muted,
        ink=ink,
        accent=accent,
        accent_ink=accent_ink,
        family=colors.family,
        tone=tone,
    )

    pairs = list((_type_pairs().get("pairs") or []))
    if not pairs:
        raise RuntimeError("config/firstlook/type_pairs.yaml has no pairs")
    raw_pair = pairs[(seed >> 16) % len(pairs)]
    type_pair = TypePair(
        name=str(raw_pair["name"]),
        display=str(raw_pair["display"]),
        body=str(raw_pair["body"]),
        display_weight=int(raw_pair.get("display_weight") or 600),
        display_tracking=str(raw_pair.get("display_tracking") or "0em"),
        body_leading=float(raw_pair.get("body_leading") or 1.6),
    )

    mark = _wordmark(venture_name, idea_text)
    return BrandKit(
        wordmark=mark,
        monogram=_monogram(mark),
        colors=colors,
        type_pair=type_pair,
        seed=hashlib.sha256(f"{idea_text.strip()}\0{variant_index}".encode()).hexdigest()[:16],
        variant_index=variant_index,
    )


def svg_mark(
    brand: BrandKit | dict[str, Any],
    *,
    size: int = 128,
) -> str:
    """Deterministic SVG logo mark (monogram tile) — no external images, no fake brands."""
    data = brand.to_dict() if isinstance(brand, BrandKit) else dict(brand)
    colors = data.get("colors") or {}
    mono = str(data.get("monogram") or "?").upper()[:2]
    accent = colors.get("accent") or "#333"
    ink = colors.get("accent_ink") or "#fff"
    page = colors.get("page") or "#f7f7f5"
    r = max(12, size // 5)
    font_size = int(size * 0.42)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" '
        f'viewBox="0 0 {size} {size}" role="img" aria-label="{mono} mark">'
        f'<rect width="{size}" height="{size}" rx="{r}" fill="{page}"/>'
        f'<rect x="{size * 0.08:.1f}" y="{size * 0.08:.1f}" '
        f'width="{size * 0.84:.1f}" height="{size * 0.84:.1f}" rx="{r}" fill="{accent}"/>'
        f'<text x="50%" y="54%" text-anchor="middle" dominant-baseline="middle" '
        f'fill="{ink}" font-family="system-ui,Segoe UI,sans-serif" '
        f'font-weight="700" font-size="{font_size}">{mono}</text>'
        f"</svg>"
    )


def svg_mark_data_uri(brand: BrandKit | dict[str, Any], *, size: int = 128) -> str:
    import base64

    raw = svg_mark(brand, size=size).encode("utf-8")
    return "data:image/svg+xml;base64," + base64.b64encode(raw).decode("ascii")


__all__ = [
    "BrandKit",
    "Colors",
    "TypePair",
    "AA_RATIO",
    "build_brandkit",
    "contrast_ratio",
    "meets_aa",
    "relative_luminance",
    "svg_mark",
    "svg_mark_data_uri",
]
