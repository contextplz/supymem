"""Pin declared local resources using HTML and CSS parsers, not text substitutions."""
from __future__ import annotations

from html import escape
from html.parser import HTMLParser
from urllib.parse import parse_qsl, quote, unquote, urlencode, urljoin, urlsplit, urlunsplit

import tinycss2
from tinycss2.serializer import serialize_string_value


class AssetURLs:
    def url(self, value: str) -> str | None:
        raise NotImplementedError

    def css(self, value: str) -> str:
        if len(value.encode()) > 2_000_000:
            raise ValueError("firstlook_css_too_large")
        tokens = tinycss2.parse_component_value_list(value)
        def walk(items, depth=0):
            if depth > 64:
                raise ValueError("firstlook_css_too_deep")
            importing = False
            for token in items:
                if token.type == "url":
                    changed = self.url(token.value) or ""
                    if changed != token.value:
                        token.value = changed
                        token.representation = 'url("' + serialize_string_value(changed) + '")'
                elif token.type == "function" and token.lower_name == "url":
                    meaningful = [t for t in token.arguments if t.type not in {"whitespace", "comment"}]
                    if len(meaningful) == 1 and meaningful[0].type == "string":
                        part = meaningful[0]
                        changed = self.url(part.value) or ""
                        if changed != part.value:
                            part.value = changed
                            part.representation = '"' + serialize_string_value(changed) + '"'
                elif importing and token.type == "string":
                    changed = self.url(token.value) or ""
                    if changed != token.value:
                        token.value = changed
                        token.representation = '"' + serialize_string_value(changed) + '"'
                if token.type not in {"whitespace", "comment"}:
                    importing = token.type == "at-keyword" and token.lower_value == "import"
                if hasattr(token, "content"):
                    walk(token.content, depth + 1)
                elif hasattr(token, "arguments") and not (token.type == "function" and token.lower_name == "url"):
                    walk(token.arguments, depth + 1)
        walk(tokens)
        try:
            return tinycss2.serialize(tokens)
        except TypeError as exc:
            raise ValueError("firstlook_css_invalid") from exc

    def srcset(self, value: str) -> str:
        # HTML's candidate tokenizer keeps commas inside data URLs. A candidate
        # URL ends at whitespace; descriptor lists end at an unparenthesized comma.
        candidates, i = [], 0
        while i < len(value):
            while i < len(value) and (value[i].isspace() or value[i] == ","):
                i += 1
            start = i
            while i < len(value) and not value[i].isspace():
                i += 1
            url = value[start:i]
            if not url:
                break
            if url.endswith(","):
                if changed := self.url(url.rstrip(",")):
                    candidates.append(changed)
                continue
            start, depth = i, 0
            while i < len(value):
                if value[i] == "," and depth == 0:
                    break
                if value[i] == "(": depth += 1
                if value[i] == ")": depth = max(0, depth - 1)
                i += 1
            descriptor = value[start:i].strip()
            if changed := self.url(url):
                candidates.append(changed + (" " + descriptor if descriptor else ""))
            i += 1
        return ", ".join(candidates)


class AssetVersions(AssetURLs):
    def __init__(self, workspace_id: str, version: int, files: dict[str, str]):
        self.base = f"/w/{workspace_id}/site/"
        self.logo = f"/w/{workspace_id}/brand/logo"
        self.version = version
        self.files = files

    def url(self, value: str) -> str:
        try:
            parsed = urlsplit(value)
        except ValueError:
            return value
        # External/data/blob/fragment references are not this version's manifest.
        if parsed.scheme or parsed.netloc or not parsed.path:
            return value
        absolute = urlsplit(urljoin("https://manifest.invalid" + self.base, value))
        path = unquote(absolute.path)
        name = path.removeprefix(self.base)
        # Missing local assets must also retain the selected version: leaving
        # them unversioned could borrow a file introduced by a later release.
        if path != self.logo and (not path.startswith(self.base) or not name or "/" in name):
            return value
        target = self.logo if path == self.logo else self.base + quote(name, safe="")
        query = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True) if k != "v"]
        query.append(("v", str(self.version)))
        return urlunsplit(("", "", target, urlencode(query), parsed.fragment))

    def html(self, value: str) -> str:
        if len(value.encode()) > 8_000_000:
            raise ValueError("firstlook_html_too_large")
        parser = _HTML(self)
        parser.feed(value)
        parser.close()
        return "".join(parser.output)


class _HTML(HTMLParser):
    def __init__(self, versions: AssetVersions):
        super().__init__(convert_charrefs=False)
        self.versions = versions
        self.output: list[str] = []
        self.in_style = False

    def tag(self, tag, attrs, closing):
        rendered = []
        for key, value in attrs:
            if value is not None:
                if key in {"src", "href", "poster", "data", "xlink:href"}:
                    value = self.versions.url(value)
                elif key in {"srcset", "imagesrcset"}:
                    value = self.versions.srcset(value)
                elif key == "style":
                    value = self.versions.css(value)
            rendered.append(key if value is None else f'{key}="{escape(value, quote=True)}"')
        self.output.append("<" + tag + (" " + " ".join(rendered) if rendered else "") + closing)

    def handle_starttag(self, tag, attrs):
        self.tag(tag, attrs, ">")
        if tag == "style": self.in_style = True

    def handle_startendtag(self, tag, attrs): self.tag(tag, attrs, "/>")
    def handle_endtag(self, tag):
        self.output.append(f"</{tag}>")
        if tag == "style": self.in_style = False
    def handle_data(self, data): self.output.append(self.versions.css(data) if self.in_style else data)
    def handle_entityref(self, name): self.output.append("&" + name + ";")
    def handle_charref(self, name): self.output.append("&#" + name + ";")
    def handle_comment(self, data): self.output.append("<!--" + data + "-->")
    def handle_decl(self, decl): self.output.append("<!" + decl + ">")
    def handle_pi(self, data): self.output.append("<?" + data + ">")
    def unknown_decl(self, data): self.output.append("<![" + data + "]>")
