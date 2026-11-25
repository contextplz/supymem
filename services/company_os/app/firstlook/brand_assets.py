"""Venture brand assets — generated SVG mark + optional founder logo upload."""

from __future__ import annotations

import os
import io
import re
import tempfile
import warnings
import xml.etree.ElementTree as ET
from pathlib import Path

_ALLOWED = {".png", ".jpg", ".jpeg", ".webp", ".svg", ".gif"}


def _validate_image(data: bytes, suffix: str) -> None:
    if suffix == ".svg":
        lowered = data.lower()
        if any(value in lowered for value in (b"<!doctype", b"<!entity", b"<?xml-stylesheet")):
            raise ValueError("SVG logos cannot include document types, entities or external stylesheets")
        try:
            root = ET.fromstring(data)
        except ET.ParseError:
            raise ValueError("The logo is not a valid SVG image") from None
        allowed = {"svg", "g", "path", "rect", "circle", "ellipse", "line", "polyline", "polygon", "text",
                   "tspan", "defs", "linearGradient", "radialGradient", "stop", "clipPath", "mask", "title", "desc", "use", "symbol"}
        if root.tag.split("}")[-1] != "svg":
            raise ValueError("The logo is not an SVG image")
        for node in root.iter():
            if node.tag.split("}")[-1] not in allowed or (node.tag.startswith("{") and not node.tag.startswith("{http://www.w3.org/2000/svg}")):
                raise ValueError("SVG logos must contain only static vector elements")
            for name, value in node.attrib.items():
                name = name.split("}")[-1].lower()
                normalized = value.strip().lower()
                if name.startswith("on") or name == "base" or (name == "href" and not re.fullmatch(r"#[a-zA-Z_][\w:.-]*", value)):
                    raise ValueError("SVG logos cannot include scripts or external links")
                if any(token in normalized for token in ("@import", "javascript:", "expression(", "data:")):
                    raise ValueError("SVG logos cannot include active content")
                safe_urls = re.sub(r"url\(\s*['\"]?#[\w:.-]+['\"]?\s*\)", "", normalized)
                if "url" in safe_urls or "\\" in normalized:
                    raise ValueError("SVG logos cannot load external resources")
        return
    from PIL import Image
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                formats = {".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG", ".webp": "WEBP", ".gif": "GIF"}
                if image.format != formats[suffix] or image.width * image.height > 16_000_000:
                    raise ValueError("Logo format or dimensions are unsupported")
                image.verify()
    except Exception:
        raise ValueError("Choose a valid image with no more than 16 million pixels") from None


def _data_root() -> Path:
    override = os.environ.get("FIRSTLOOK_DATA_DIR") or os.environ.get("COMPANY_OS_DATA_DIR")
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[4] / "data"


def brand_dir(workspace_id: str, venture_id: str | None = None) -> Path:
    d = _data_root() / "workspaces" / workspace_id / "brand"
    if venture_id:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", venture_id):
            raise ValueError("Choose your idea before uploading your logo.")
        d = d / "ventures" / venture_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def custom_logo_path(workspace_id: str, venture_id: str | None = None) -> Path | None:
    root = brand_dir(workspace_id, venture_id)
    for p in sorted(root.glob("logo.*")):
        if p.suffix.lower() in _ALLOWED and p.is_file():
            return p
    if venture_id:
        import base64

        from app.db.models import Venture
        from app.firstlook import versions
        from app.state import db

        legacy = custom_logo_path(workspace_id)
        if legacy is None:
            return None
        saved = versions.get_latest(workspace_id, venture_id=venture_id)
        logo = (saved.copy.get("_render_snapshot", {}).get("logo", {}) if saved else {})
        if logo.get("data"):
            return legacy if base64.b64encode(legacy.read_bytes()).decode("ascii") == logo["data"] else None
        with db()() as session:
            ventures = session.query(Venture.id).filter(Venture.workspace_id == workspace_id).all()
        return legacy if ventures == [(venture_id,)] else None
    scoped = [path for path in root.glob("ventures/*/logo.*") if path.suffix.lower() in _ALLOWED and path.is_file()]
    if len(scoped) == 1:
        return scoped[0]
    return None


def custom_logo_url(workspace_id: str, venture_id: str | None = None) -> str | None:
    if custom_logo_path(workspace_id, venture_id) is None:
        if not venture_id:
            return None
        from app.firstlook import versions
        saved = versions.get_latest(workspace_id, venture_id=venture_id)
        if not saved or not saved.copy.get("_render_snapshot", {}).get("logo"):
            return None
    return f"/w/{workspace_id}/brand/logo" + (f"?venture_id={venture_id}" if venture_id else "")


def save_logo(workspace_id: str, data: bytes, filename: str, venture_id: str | None = None) -> Path:
    suffix = Path(filename).suffix.lower() or ".png"
    if suffix not in _ALLOWED:
        raise ValueError(f"unsupported logo type: {suffix}")
    if len(data) > 2_000_000:
        raise ValueError("logo too large (max 2MB)")
    _validate_image(data, suffix)
    root = brand_dir(workspace_id, venture_id)
    path = root / f"logo{suffix}"
    # Validate first and atomically replace the file before removing an old format.
    with tempfile.NamedTemporaryFile(dir=root, prefix=".logo-", delete=False) as handle:
        pending = Path(handle.name)
        handle.write(data)
    try:
        os.replace(pending, path)
        for old in root.glob("logo.*"):
            if old != path:
                old.unlink(missing_ok=True)
    finally:
        pending.unlink(missing_ok=True)
    return path
