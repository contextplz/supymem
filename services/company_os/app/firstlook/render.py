"""Jinja2 render → self-contained HTML string (45 §3 step 6)."""

from __future__ import annotations

from datetime import date
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.firstlook.brandkit import BrandKit
from app.firstlook.templates import pick_template, templates_dir

STAGE_LABELS = {
    "first_look": "First look, built from your one sentence. Nothing here is final.",
    "refined": "Draft site, reflects your charter.",
}

PRIVACY_LINE = "Stored on this machine only. Never sent to a model."
LOCAL_NOTE = "This link only works on your machine."


def _env() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(templates_dir())),
        autoescape=select_autoescape(["html", "xml", "j2"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )


def render_html(
    *,
    brand: BrandKit | dict[str, Any],
    copy: dict[str, Any],
    archetype: str,
    idea_text: str,
    version: int,
    stage: str = "first_look",
    interest_action: str = "",
    local_only: bool = True,
    lang: str = "en",
    generated_on: str | None = None,
    logo_url: str | None = None,
) -> tuple[str, str]:
    """Return (html, template_name)."""
    from app.firstlook.brandkit import svg_mark_data_uri

    template_name = pick_template(archetype)
    brand_dict = brand.to_dict() if isinstance(brand, BrandKit) else dict(brand)
    # Nested objects for template attribute access.
    class _NS(dict):
        def __getattr__(self, item: str) -> Any:
            try:
                val = self[item]
            except KeyError as exc:
                raise AttributeError(item) from exc
            if isinstance(val, dict) and not isinstance(val, _NS):
                return _NS(val)
            return val

    brand_ns = _NS(brand_dict)
    if isinstance(brand_ns.get("colors"), dict):
        brand_ns["colors"] = _NS(brand_ns["colors"])
    if isinstance(brand_ns.get("type_pair"), dict):
        brand_ns["type_pair"] = _NS(brand_ns["type_pair"])

    copy_ns = _NS(dict(copy))
    if isinstance(copy_ns.get("cta"), dict):
        copy_ns["cta"] = _NS(copy_ns["cta"])

    custom_logo = logo_url or brand_dict.get("logo_url")
    mark_src = custom_logo or svg_mark_data_uri(brand_dict, size=96)

    action = interest_action or "#"
    html = _env().get_template(f"{template_name}.html.j2").render(
        lang=lang,
        brand=brand_ns,
        copy=copy_ns,
        interest_action=action,
        version=version,
        privacy_line=PRIVACY_LINE,
        stage_label=STAGE_LABELS.get(stage, STAGE_LABELS["first_look"]),
        idea_text=idea_text,
        local_only=local_only,
        local_note=LOCAL_NOTE,
        generated_on=generated_on or date.today().isoformat(),
        logo_src=mark_src,
        logo_is_custom=bool(custom_logo),
    )
    return html, template_name


__all__ = ["render_html", "STAGE_LABELS", "PRIVACY_LINE", "LOCAL_NOTE"]
