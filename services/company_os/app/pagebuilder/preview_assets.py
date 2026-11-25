"""Make verified build assets usable inside an inert private preview."""
from __future__ import annotations

import base64
from html import escape
from html.parser import HTMLParser
import mimetypes
import re
from urllib.parse import quote, unquote, urlsplit

from app.firstlook.asset_versions import AssetURLs

_HTML_SPACE = "\t\n\f\r "
PREVIEW_DOCUMENT_MAX_BYTES = 2_500_000
PREVIEW_POLICY = "default-src 'none'; img-src data:; style-src 'unsafe-inline'; font-src data:; script-src 'none'; connect-src 'none'; form-action 'none'; base-uri 'none'"


class PreviewTooLarge(ValueError):
    pass


def signup_preview_notice(source, notice):
    parser = HTMLParser()
    start_tags = []
    parser.handle_starttag = lambda tag, attributes: start_tags.append(parser.get_starttag_text()) if dict(attributes).get("id") == "waitlist" else None
    parser.feed(source)
    if not start_tags:
        raise ValueError("pagebuilder_waitlist_preview_not_found")
    return source.replace(start_tags[0], start_tags[0] + '<p>Preview only. This page does not collect signups. '
                          + escape(notice) + '</p>', 1)


def srcdoc_document(content):
    from app.pagebuilder.qa import PageParser

    parser = PageParser()
    parser.feed(content)
    for tag, attributes, _ in parser.elements:
        if tag != "a" or not (attributes.get("href") or "").startswith("#"):
            continue
        values = [(key, value) for key, value in attributes.items()
                  if key not in {"_label", "_starttag", "href", "target"}]
        values.extend((("href", "about:srcdoc" + attributes["href"]), ("target", "_self")))
        start_tag = "<a" + "".join(" " + key + ("" if value is None else '="' + escape(value, quote=True) + '"')
                                 for key, value in values) + ">"
        content = content.replace(attributes["_starttag"], start_tag, 1)
    if len(content.encode("utf-8")) > PREVIEW_DOCUMENT_MAX_BYTES:
        raise PreviewTooLarge("index.html")
    return content


class PreviewAssets(AssetURLs):
    def __init__(self, files, read_file, page_link):
        self.files, self.read_file, self.page_link = files, read_file, page_link
        self.embedded = {}
        self.expanded_bytes = 0

    def url(self, value):
        value = value.strip()
        if value.startswith("#") or re.match(r"data:(?:image|font)/[-\w.+]+[;,]", value, re.I):
            return value
        try:
            url = urlsplit(value)
        except ValueError:
            return None
        if url.scheme or url.netloc or value.startswith("//"):
            return None
        path = unquote(url.path).removeprefix("./").removeprefix("/")
        if path not in self.files:
            return None
        mime = mimetypes.guess_type(path)[0]
        if not mime or not mime.startswith(("image/", "font/")):
            return None
        if path not in self.embedded:
            self.embedded[path] = f"data:{mime};base64," + base64.b64encode(self.read_file(path)).decode()
        result = self.embedded[path] + ("#" + quote(url.fragment, safe="%") if url.fragment else "")
        self.expanded_bytes += len(result)
        if self.expanded_bytes > PREVIEW_DOCUMENT_MAX_BYTES:
            raise PreviewTooLarge(path)
        return result

    def css(self, value):
        try:
            return super().css(value)
        except PreviewTooLarge:
            raise
        except ValueError:
            return ""

    def document(self, source, policy):
        self.expanded_bytes = 0
        parser = _PreviewMarkup(source, self)
        parser.feed(source)
        parser.close()
        meta = f'<meta http-equiv="Content-Security-Policy" content="{escape(policy, quote=True)}">'
        parser.edits.append((parser.policy_at, parser.policy_at, meta))
        result, cursor = [], 0
        for start, end, replacement in sorted(parser.edits):
            result.extend((source[cursor:start], replacement))
            cursor = end
        result.append(source[cursor:])
        document = "".join(result)
        if len(document.encode("utf-8")) > PREVIEW_DOCUMENT_MAX_BYTES:
            raise PreviewTooLarge(max(self.embedded, key=lambda path: len(self.embedded[path]), default="index.html"))
        return document


class _PreviewMarkup(HTMLParser):
    def __init__(self, source, assets):
        super().__init__(convert_charrefs=False)
        self.source, self.assets = source, assets
        self.lines = [0, *(match.end() for match in re.finditer("\n", source))]
        self.policy_at = re.match(r"[\t\n\f\r ]*(?:<!doctype[\t\n\f\r ]+html[\t\n\f\r ]*>)?", source, re.I).end()
        self.edits = []
        self.in_style = False

    def source_offset(self):
        line, column = self.getpos()
        return self.lines[line - 1] + column

    def tag(self, tag, attrs, ending):
        start = self.source_offset()
        end = start + len(self.get_starttag_text())
        fields = dict(reversed(attrs))
        if (tag in {"html", "head"} and start >= self.policy_at
                and not self.source[self.policy_at:start].strip(_HTML_SPACE)):
            self.policy_at = end
        if tag == "meta":
            directive = (fields.get("http-equiv") or "").strip().casefold()
            name = (fields.get("property") or fields.get("name") or "").casefold()
            if directive in {"content-security-policy", "refresh"} or name.startswith(("og:image", "twitter:image")):
                self.edits.append((start, end, ""))
                return
        rendered = []
        for key, value in attrs:
            if key == "href" and tag == "a" and value:
                value = self.assets.page_link(value) or self.assets.url(value) or value
            elif key in {"src", "href", "poster", "data", "xlink:href", "background"}:
                if not (value := self.assets.url(value or "")):
                    continue
            elif key in {"srcset", "imagesrcset"}:
                if not (value := self.assets.srcset(value or "")):
                    continue
            elif key == "style" and value:
                value = self.assets.css(value)
            rendered.append((key, value))
        if rendered != attrs:
            replacement = "<" + tag + "".join(" " + key if value is None else f' {key}="{escape(value, quote=True)}"'
                                              for key, value in rendered) + ending
            self.edits.append((start, end, replacement))

    def handle_starttag(self, tag, attrs):
        self.tag(tag, attrs, ">")
        if tag == "style":
            self.in_style = True

    def handle_startendtag(self, tag, attrs):
        self.tag(tag, attrs, "/>")

    def handle_endtag(self, tag):
        if tag == "style":
            self.in_style = False

    def handle_data(self, value):
        if self.in_style:
            css = re.sub(r"</style", r"\\3c /style", self.assets.css(value), flags=re.I)
            start = self.source_offset()
            self.edits.append((start, start + len(value), css))
