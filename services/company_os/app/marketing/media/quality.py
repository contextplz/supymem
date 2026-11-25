"""Reject drawn placeholders. Founder and product files must be real pixels."""

from __future__ import annotations

from pathlib import Path


def _stats(path: str | Path) -> tuple[int, float, tuple[int, int]] | None:
    src = Path(path)
    if not src.is_file():
        return None
    try:
        from PIL import Image

        with Image.open(src) as img:
            rgb = img.convert("RGB")
            w, h = rgb.size
            sample = rgb.resize((96, 96))
            colors = sample.getcolors(96 * 96) or []
            unique = len(colors)
            pixels = 96 * 96
            dominant = max((count for count, _color in colors), default=0) / pixels
            return unique, dominant, (w, h)
    except Exception:
        return None


def looks_synthetic(path: str | Path, *, kind: str = "product") -> bool:
    """True for solid fills, ellipse cartoons, and other low-entropy drawings."""
    src = Path(path)
    if not src.is_file():
        return True
    if src.suffix.lower() == ".svg":
        return False
    stats = _stats(src)
    if stats is None:
        return True
    unique, dominant, size = stats
    if kind == "logo":
        return unique <= 1
    if min(size) < 64:
        return True
    if unique < 28:
        return True
    if dominant > 0.82 and unique < 80:
        return True
    try:
        from PIL import Image

        with Image.open(src) as img:
            sample = img.convert("RGB").resize((96, 96))
            quantized = sample.quantize(colors=8)
            clustered = quantized.convert("RGB").getcolors(96 * 96) or []
            clustered.sort(reverse=True)
            top3 = sum(count for count, _color in clustered[:3]) / (96 * 96)
            # JPEG-smoothed ellipse faces still collapse to 3 clusters.
            if top3 >= 0.90:
                return True
    except Exception:
        pass
    return False


def reject_reason(kind: str, path: str | Path) -> str | None:
    if kind not in {"founder", "product", "logo"}:
        return None
    src = Path(path)
    suffix = src.suffix.lower()
    if kind == "logo" and suffix == ".svg":
        return None
    if looks_synthetic(src, kind=kind):
        if kind == "logo":
            return "That logo is a solid fill. Upload the real SVG or PNG mark."
        who = "founder photo" if kind == "founder" else "product photo"
        return (
            f"That {who} is a drawing, not a camera or a product screenshot. "
            "Upload a real file. We will not invent a person or a scene."
        )
    if kind == "founder":
        stats = _stats(src)
        if stats and min(stats[2]) < 400:
            return "Founder photos need to be at least 400px on the short side."
    return None
