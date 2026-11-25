"""Conservative, offline extraction of a website's name, product and audience."""
from __future__ import annotations

import re
from urllib.parse import urlsplit

from app.intake.assets import _Page


class _Website(_Page):
    def __init__(self):
        super().__init__()
        self.title: list[str] = []
        self.site_name = ""
        self.description = ""
        self.brand: list[str] = []
        self.in_title = False
        self.in_header = False
        self.in_brand = False
        self.in_head = False
        self.lines: list[str] = []
        self.buffer: list[str] = []

    def flush(self):
        if self.buffer:
            self.lines.append(" ".join(self.buffer))
            self.buffer = []

    def handle_starttag(self, tag, attrs):
        super().handle_starttag(tag, attrs)
        attributes = dict(attrs)
        if tag == "meta":
            key = str(attributes.get("property") or attributes.get("name") or "").lower()
            if key == "og:site_name":
                self.site_name = attributes.get("content") or ""
            if key in {"description", "og:description"} and not self.description:
                self.description = attributes.get("content") or ""
        if tag == "title":
            self.in_title = True
        if tag == "head":
            self.in_head = True
        if tag == "header":
            self.in_header = True
        if self.in_header and tag == "a" and (attributes.get("href") == "/" or
                re.search(r"brand|logo", str(attributes.get("class", "")), re.I)):
            self.in_brand = True
        if self.in_header and tag == "img" and attributes.get("alt") and not self.brand:
            self.brand.append(attributes["alt"])
        if tag in {"p", "h1", "h2", "h3", "li", "div", "section", "a", "button", "br"}:
            self.flush()

    def handle_endtag(self, tag):
        if tag in {"p", "h1", "h2", "h3", "li", "div", "section", "a", "button"}:
            self.flush()
        super().handle_endtag(tag)
        if tag == "title":
            self.in_title = False
        if tag == "head":
            self.in_head = False
        if tag == "header":
            self.in_header = False
        if tag == "a":
            self.in_brand = False

    def handle_data(self, data):
        super().handle_data(data)
        if self.in_title:
            self.title.append(data.strip())
        if self.in_brand and not self.hidden:
            self.brand.append(data.strip())
        if not self.hidden and not self.in_head and data.strip():
            self.buffer.append(data.strip())


def changing_idea(text: str) -> bool:
    # ponytail: explicit English pivot cues only; add observed phrases before a second model call.
    if not any(cue in text.lower() for cue in ("pivot", "chang", "switch", "replac", "new idea", "different idea", "instead", "rather", "start over")):
        return False
    return bool(re.search(r"\b(?:pivot(?:ing)?|(?:change|changing|switch|switching|replace|replacing)\s+"
                          r"(?:my\s+|our\s+|the\s+|this\s+|business\s+|product\s+)?(?:idea|direction|business|product)|"
                          r"(?:want|plan|build|try)\s+(?:to\s+(?:build|try)\s+)?(?:a\s+)?(?:new|different)\s+idea|"
                          r"(?:instead|rather)\s+(?:I|we)\s+(?:want|plan)|start\s+over)\b", text, re.I)) and not bool(
        re.search(r"\b(?:not|don't|do not|no)\s+(?:want\s+to\s+|planning\s+to\s+)?(?:pivot|chang|switch)", text, re.I))


def extract_website(content: str, url: str, *, max_chars: int = 6000) -> dict:
    page = _Website()
    page.feed(content[:max(30_000, max_chars)])
    page.flush()
    html = bool(re.search(r"<(?:html|head|meta|title|h1|p|header|script|style|div)\b", content, re.I))
    lines = page.lines if html else [line.strip().lstrip("# ") for line in content.splitlines() if line.strip()]
    title = " ".join(page.title) or next((line.removeprefix("Title: ") for line in lines if line.startswith("Title: ")), "")
    brand = " ".join(page.brand)
    titles = re.split(r"\s+[|—–-]\s+|\s*\|\s*", title)
    candidates = [page.site_name, *(sorted(titles, key=lambda value: value != brand) if brand else titles), brand]
    name = next((" ".join(value.split()) for value in candidates if value.strip() and len(value.strip()) <= 48
                 and len(value.split()) <= 4 and not re.search(r"[<>\x00-\x1f]|ignore.*instructions", value, re.I)), "")
    if not name:
        host = (urlsplit(url).hostname or "Your venture").removeprefix("www.")
        name = host.split(".")[0].replace("-", " ").title()[:48] or "Your venture"
    answers = {}
    # shortcut: offline extraction uses labels and excerpts, use quoted model prefills for semantic paraphrases.
    labels = {"customer": "audience|who (?:it is|it's) for|target (?:audience|users)",
              "offer": "what (?:it does|we do)|product|solution|offer",
              "problem": "problem|pain(?: point)?",
              "pricing_hypothesis": "pricing|price|monetization",
              "positioning": "positioning|differentiation|why choose us"}
    for key, label in labels.items():
        match = next((match for line in lines if (match := re.match(rf"(?:{label})\s*:\s*(.+)", line, re.I))), None)
        if match:
            answers[key] = match[1][:2000]
    headline = " ".join(page.headline) or next((line[2:] for line in content.splitlines() if line.startswith("# ")), "")
    offer = page.description or headline
    if offer and "offer" not in answers:
        answers["offer"] = offer[:2000]
    if "customer" not in answers:
        match = re.search(r"\bfor\s+([^.!?]+)", offer, re.I)
        if match and not re.search(r"\b(?:free|now|sale|example)\b", match[1], re.I):
            answers["customer"] = match[1].strip()[:2000]
    return {"name": name, "answers": answers, "content": "\n".join([page.description, *lines]).strip()[:max_chars]}
