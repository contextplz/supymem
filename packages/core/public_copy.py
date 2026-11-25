"""Deterministic checks for unresolved instructions in public site copy."""
from __future__ import annotations

from html.parser import HTMLParser
import re


UNREVIEWED_COPY = {
    "editor_marker": re.compile(r"\b(?:TODO|TBD|FIXME)\b|\blorem\s+ipsum\b|\[\s*(?:insert|replace|your)\b[^\]]{0,80}\]", re.I),
    "placeholder_copy": re.compile(r"\b(?:placeholder\s+(?:text|copy|content|image|link)|replace\s+this\s+(?:text|copy|content|image|link))\b", re.I),
    "publication_instruction": re.compile(
        r"\b(?:must|needs?\s+to|should|please)\s+(?:be\s+)?(?:configured|verified|added|provided|replaced|set\s+up)\b.{0,100}?\b(?:before\s+(?:publishing|publication|launch(?:ing)?|going\s+live)|prior\s+to\s+(?:publication|launch))\b"
        r"|\b(?:configure|verify|replace|add|provide)\b.{0,100}?\bbefore\s+(?:publishing|publication|launch(?:ing)?|going\s+live)\b"
        r"|\b(?:draft|preview)\s*[-:—]?\s*(?:do\s+not|don't)\s+publish\b", re.I),
}


def public_copy_observation(rendered: str) -> tuple[list[str], str]:
    """Return fixed categories without copying arbitrary founder text to a receipt."""
    normalized = " ".join(rendered.split())
    findings = [kind for kind, pattern in UNREVIEWED_COPY.items() if pattern.search(normalized)]
    return findings, "Unreviewed public copy: " + ", ".join(findings) if findings else "No unresolved public-copy markers found"


class _PublicText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.excluded = 0
        self.stack: list[tuple[str, bool]] = []
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}:
            return
        attributes = dict(attrs)
        excluded = bool(self.excluded or tag in {"head", "script", "style", "template", "noscript"}
                        or "hidden" in attributes or attributes.get("aria-hidden") == "true")
        self.stack.append((tag, excluded))
        self.excluded += excluded

    def handle_startendtag(self, tag, attrs):
        pass

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                self.excluded -= sum(excluded for _, excluded in self.stack[index:])
                del self.stack[index:]
                break

    def handle_data(self, data):
        if not self.excluded:
            self.parts.append(data)


def public_html_text(html: str) -> str:
    """Extract likely public text without executing HTML or including scripts."""
    parser = _PublicText()
    parser.feed(html)
    return " ".join(parser.parts)
