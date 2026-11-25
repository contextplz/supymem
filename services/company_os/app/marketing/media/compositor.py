"""Photographic compositor — real pixels + grain. Never redraws a logo."""

from __future__ import annotations

import random
from pathlib import Path
from typing import Any

from core.ids import new_id

from app.marketing.media.store import latest_of_kind, media_dir


def _hex_to_rgb(value: str, fallback: tuple[int, int, int] = (18, 22, 28)) -> tuple[int, int, int]:
    raw = (value or "").strip().lstrip("#")
    if len(raw) == 3:
        raw = "".join(ch * 2 for ch in raw)
    if len(raw) != 6:
        return fallback
    try:
        return int(raw[0:2], 16), int(raw[2:4], 16), int(raw[4:6], 16)
    except ValueError:
        return fallback


def _font(size: int, *, bold: bool = False):
    from PIL import ImageFont

    candidates = [
        "/System/Library/Fonts/Supplemental/Georgia Bold.ttf" if bold else "/System/Library/Fonts/Supplemental/Georgia.ttf",
        "/System/Library/Fonts/Supplemental/Times New Roman.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf" if bold else "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size=size)
        except Exception:
            continue
    return ImageFont.load_default()


def _open_rgb(path: str | Path):
    from PIL import Image

    img = Image.open(path)
    if img.mode in {"RGBA", "LA"}:
        bg = Image.new("RGB", img.size, (16, 18, 20))
        bg.paste(img, mask=img.split()[-1])
        return bg
    return img.convert("RGB")


def _cover(img, width: int, height: int):
    from PIL import Image

    src_w, src_h = img.size
    scale = max(width / src_w, height / src_h)
    new_w = max(1, int(src_w * scale))
    new_h = max(1, int(src_h * scale))
    resized = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
    left = (new_w - width) // 2
    top = (new_h - height) // 2
    return resized.crop((left, top, left + width, top + height))


def _grain(img, amount: float = 0.035, seed: int = 7):
    from PIL import Image

    rng = random.Random(seed)
    noise = Image.new("RGB", img.size)
    px = noise.load()
    w, h = img.size
    for y in range(h):
        for x in range(w):
            v = rng.randint(-18, 18)
            px[x, y] = (v, v, v)
    return Image.blend(img, Image.eval(noise, lambda p: p + 128), amount)


def _fast_grain(img, amount: float = 0.04, seed: int = 7):
    """Coarse grain (8px cells) so a 1080 canvas stays interactive."""
    from PIL import Image, ImageEnhance, ImageFilter

    rng = random.Random(seed)
    cell = 8
    w, h = img.size
    cw, ch = max(1, w // cell), max(1, h // cell)
    small = Image.new("L", (cw, ch))
    sp = small.load()
    for y in range(ch):
        for x in range(cw):
            sp[x, y] = rng.randint(96, 160)
    noise = small.resize((w, h), Image.Resampling.BILINEAR).convert("RGB")
    blended = Image.blend(img, noise, amount)
    return ImageEnhance.Contrast(blended.filter(ImageFilter.UnsharpMask(radius=0.6, percent=40, threshold=2))).enhance(1.04)


def _vignette(img, strength: float = 0.28):
    from PIL import Image, ImageDraw

    w, h = img.size
    overlay = Image.new("RGB", (w, h), (0, 0, 0))
    mask = Image.new("L", (w, h), 0)
    draw = ImageDraw.Draw(mask)
    draw.ellipse((-int(w * 0.12), -int(h * 0.16), int(w * 1.12), int(h * 1.16)), fill=int(255 * (1 - strength)))
    from PIL import ImageFilter

    mask = mask.filter(ImageFilter.GaussianBlur(radius=min(w, h) // 8))
    return Image.composite(img, overlay, mask)


def _overlay_logo(base, logo_path: str, *, margin: int = 48, max_width: int | None = None):
    from PIL import Image

    logo = Image.open(logo_path)
    if logo.mode != "RGBA":
        logo = logo.convert("RGBA")
    max_w = max_width or max(120, base.size[0] // 6)
    scale = min(1.0, max_w / max(1, logo.size[0]))
    new_size = (max(1, int(logo.size[0] * scale)), max(1, int(logo.size[1] * scale)))
    logo = logo.resize(new_size, Image.Resampling.LANCZOS)
    pos = (base.size[0] - logo.size[0] - margin, margin)
    layered = base.convert("RGBA")
    layered.alpha_composite(logo, dest=pos)
    return layered.convert("RGB")


def _empty_scene(width: int, height: int, accent: tuple[int, int, int], seed: int):
    """Window-light documentary empty frame — no generated faces."""
    from PIL import Image, ImageDraw, ImageFilter

    rng = random.Random(seed)
    img = Image.new("RGB", (width, height), (22, 24, 28))
    draw = ImageDraw.Draw(img)
    warm = (accent[0], accent[1], accent[2])
    cool = (28, 34, 48)
    for y in range(height):
        t = y / max(1, height - 1)
        r = int(cool[0] * (1 - t) + warm[0] * t * 0.35 + 18)
        g = int(cool[1] * (1 - t) + warm[1] * t * 0.35 + 16)
        b = int(cool[2] * (1 - t) + warm[2] * t * 0.2 + 14)
        draw.line([(0, y), (width, y)], fill=(r, g, b))
    window = Image.new("RGB", (width, height), (0, 0, 0))
    wdraw = ImageDraw.Draw(window)
    wx0, wy0 = int(width * 0.58), int(height * -0.05)
    wx1, wy1 = int(width * 1.12), int(height * 0.55)
    wdraw.rectangle((wx0, wy0, wx1, wy1), fill=(210, 198, 176))
    window = window.filter(ImageFilter.GaussianBlur(radius=90))
    img = Image.blend(img, window, 0.38)
    desk_y = int(height * 0.72)
    draw = ImageDraw.Draw(img)
    draw.rectangle((0, desk_y, width, height), fill=(32, 28, 24))
    # slight table edge catch-light
    draw.line([(0, desk_y), (width, desk_y)], fill=(90, 78, 64), width=3)
    _ = rng
    return img


def compose_still(
    *,
    workspace_id: str,
    headline: str,
    caption: str | None = None,
    width: int = 1080,
    height: int = 1350,
    accent: str = "#6B5A4A",
    want_person: bool = False,
    seed: int | None = None,
) -> dict[str, Any]:
    try:
        from PIL import Image, ImageDraw, ImageEnhance
    except ImportError:
        return {"ok": False, "error": "pillow_not_installed"}

    seed = int(seed or 17)
    founder = latest_of_kind(workspace_id, "founder")
    product = latest_of_kind(workspace_id, "product")
    logo = latest_of_kind(workspace_id, "logo")

    if want_person and not founder:
        return {
            "ok": False,
            "status": "blocked",
            "error": "founder_photos_required",
            "block_reason": (
                "Upload your own founder photo in Media Studio, then request your image again. "
                "We will not generate a fake person."
            ),
        }

    subject_path = None
    source_kind = "empty_scene"
    if want_person and founder:
        subject_path = founder["path"]
        source_kind = "founder_photo"
    elif product:
        subject_path = product["path"]
        source_kind = "product_photo"
    elif founder and want_person:
        subject_path = founder["path"]
        source_kind = "founder_photo"

    if not subject_path or not Path(subject_path).is_file():
        return {
            "ok": False,
            "status": "blocked",
            "error": "brand_kit_required",
            "block_reason": (
                "Upload a real product screenshot (or a founder photo if a person "
                "should be in frame). We will not invent a scene or a face."
            ),
        }

    base = _cover(_open_rgb(subject_path), width, height)
    base = ImageEnhance.Color(base).enhance(0.92)
    base = ImageEnhance.Brightness(base).enhance(1.02)

    base = _vignette(base, 0.22)
    base = _fast_grain(base, 0.045, seed)

    if logo and Path(logo["path"]).is_file():
        try:
            base = _overlay_logo(base, logo["path"])
        except Exception:
            pass

    draw = ImageDraw.Draw(base)
    bar_h = 210 if headline else 0
    if headline:
        overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
        od = ImageDraw.Draw(overlay)
        od.rectangle((0, height - bar_h, width, height), fill=(12, 12, 12, 150))
        base = Image.alpha_composite(base.convert("RGBA"), overlay).convert("RGB")
        draw = ImageDraw.Draw(base)
        font = _font(42, bold=True)
        words = headline.strip().split()
        lines: list[str] = []
        current = ""
        for word in words:
            trial = f"{current} {word}".strip()
            if draw.textlength(trial, font=font) <= width - 96:
                current = trial
            else:
                if current:
                    lines.append(current)
                current = word
        if current:
            lines.append(current)
        y = height - bar_h + 28
        for line in lines[:3]:
            draw.text((48, y), line, font=font, fill=(236, 230, 220))
            y += 50
        if caption:
            small = _font(22)
            draw.text((48, height - 48), caption[:72], font=small, fill=(190, 184, 174))

    dest_dir = media_dir(workspace_id, "stills")
    dest = dest_dir / f"still_{new_id('stl')}_{width}x{height}.jpg"
    # Mild JPEG recompress kills the too-clean 16-bit look.
    base.save(dest, format="JPEG", quality=82, optimize=True, subsampling=2)
    return {
        "ok": True,
        "path": str(dest),
        "width": width,
        "height": height,
        "mime": "image/jpeg",
        "source": "compositor",
        "source_kind": source_kind,
        "used_logo": bool(logo),
        "used_founder": source_kind == "founder_photo",
        "used_product": source_kind == "product_photo",
        "review_status": "pending",
        "license": {
            "engine": "pillow_compositor",
            "commercial": True,
            "real_pixels": True,
        },
    }


def jpeg_size(path: str) -> tuple[int, int] | None:
    try:
        from PIL import Image

        with Image.open(path) as img:
            return img.size
    except Exception:
        return None
