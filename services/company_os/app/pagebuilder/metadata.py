"""Pin a browser-rendered social image without inventing a public hosting URL."""
from __future__ import annotations

from html import escape
from html.parser import HTMLParser
import re
import struct

from app.core.local_mode import is_local

SOCIAL_PREVIEW_PATH = "assets/social-preview.png"


def social_preview_requirements(product_name: str) -> dict[str, str]:
    alt = product_name + " concept landing page with original venture artwork"
    return {"og:image": SOCIAL_PREVIEW_PATH, "og:image:type": "image/png",
        "og:image:width": "1200", "og:image:height": "630", "og:image:alt": alt,
        "twitter:image": SOCIAL_PREVIEW_PATH, "twitter:image:alt": alt}


class _ImageMetadata(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tags = []
        self.values = []
        self.in_head = False

    def handle_starttag(self, tag, attrs):
        if tag == "head":
            self.in_head = True
        if tag == "meta":
            attributes = dict(attrs)
            name = (attributes.get("property") or attributes.get("name") or "").casefold()
            if name.startswith(("og:image", "twitter:image")) or name == "og:url":
                self.tags.append(self.get_starttag_text())
                self.values.append((name, attributes.get("content") if self.in_head else None))

    def handle_endtag(self, tag):
        if tag == "head":
            self.in_head = False


def add_social_preview(html: str, png: bytes, product_name: str) -> tuple[str, dict]:
    """Validate local tool metadata; hosted rendering still pins its own image."""
    if (not isinstance(png, bytes) or not 33 <= len(png) <= 1000000
            or png[:8] != b"\x89PNG\r\n\x1a\n" or png[12:16] != b"IHDR"
            or struct.unpack(">II", png[16:24]) != (1200, 630)):
        if is_local():
            raise ValueError("Your page needs a valid 1200 by 630 PNG social preview. Ask Charlie to rebuild it "
                "with your tool connected in Settings > Keys and connections > Coding tools.")
        raise ValueError("pagebuilder_social_preview_invalid")
    parser = _ImageMetadata()
    parser.feed(html)
    if is_local():
        required = social_preview_requirements(product_name)
        if (len(parser.values) != len(required) or dict(parser.values) != required
                or len(re.findall(r"</head\s*>", html, re.I)) != 1):
            raise ValueError("Your page needs the required social preview metadata before it can pass its checks. "
                "Ask Charlie to update it with your tool connected in Settings > Keys and connections > Coding tools.")
        return html, {"path": SOCIAL_PREVIEW_PATH, "width": 1200, "height": 630, "mime_type": "image/png",
            "provenance": "original_page_browser_screenshot", "canonical_url_binding": "required_before_final_source_qa"}
    for tag in parser.tags:
        html = html.replace(tag, "", 1)
    alt = escape(product_name + " concept landing page with original venture artwork", quote=True)
    tags = (f'<meta property="og:image" content="{SOCIAL_PREVIEW_PATH}">\n'
            '<meta property="og:image:type" content="image/png">\n'
            '<meta property="og:image:width" content="1200">\n'
            '<meta property="og:image:height" content="630">\n'
            f'<meta property="og:image:alt" content="{alt}">\n'
            f'<meta name="twitter:image" content="{SOCIAL_PREVIEW_PATH}">\n'
            f'<meta name="twitter:image:alt" content="{alt}">\n')
    html, count = re.subn(r"</head\s*>", lambda match: tags + match.group(0), html, count=1, flags=re.I)
    if count != 1:
        raise ValueError("pagebuilder_social_preview_head_missing")
    return html, {"path": SOCIAL_PREVIEW_PATH, "width": 1200, "height": 630, "mime_type": "image/png",
        "provenance": "original_page_browser_screenshot", "canonical_url_binding": "required_before_final_source_qa"}
