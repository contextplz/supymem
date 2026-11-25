"""Read at most ten key pages on a founder-confirmed domain, under one deadline."""

import re
import time
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

USER_AGENT = "SupymemResearch"
MAX_PAGES = 10
KEY_PATH = re.compile(r"(?:^|/)(?:about(?:-us)?|pricing|products?|features|team|customers|case-studies|blog)(?:/|$)", re.I)


def read_policy(url: str, deadline: float):
    from app.gtm.scrapy_leads import _fetch

    policy = _fetch(urljoin(url, "/robots.txt"), deadline=deadline, allow_missing=True)
    if policy is None:
        return None
    robots = RobotFileParser()
    robots.parse(policy.splitlines())
    return robots


def crawl_site(url: str, *, deadline: float | None = None, robots=None, home_html: str | None = None) -> list[dict]:
    from app.intake.assets import _allowed_page, public_site_url
    from app.intake.website import extract_website

    url = public_site_url(url)
    if not url:
        return []
    parsed = urlsplit(url)
    url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path or "/", parsed.query, ""))
    deadline = deadline or time.monotonic() + 20
    robots = robots if robots is not None else read_policy(url, deadline)
    if robots is None:
        return []
    origin = urlsplit(url)

    class Links(HTMLParser):
        def __init__(self):
            super().__init__()
            self.urls = []

        def handle_starttag(self, tag, attrs):
            if tag != "a":
                return
            href = dict(attrs).get("href") or ""
            try:
                parsed = urlsplit(urljoin(url, href))
                if parsed.scheme not in {"http", "https"} or parsed.hostname != origin.hostname or parsed.username or parsed.password or parsed.port != origin.port:
                    return
                if KEY_PATH.search(parsed.path):
                    self.urls.append(urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", "")))
            except ValueError:
                return

    queue = [url]
    home = urljoin(url, "/")
    if home != url:
        queue.append(home)
    pages, visited = [], set()
    while queue and len(visited) < MAX_PAGES and time.monotonic() < deadline:
        target = queue.pop(0)
        if target in visited:
            continue
        visited.add(target)
        if not robots.can_fetch(USER_AGENT, target):
            continue
        delay = robots.crawl_delay(USER_AGENT) or 0
        if pages and delay:
            if time.monotonic() + delay >= deadline:
                break
            time.sleep(delay)
        html = home_html if target == url and home_html is not None else _allowed_page(target, robots, deadline)
        if not html:
            continue
        if re.search(r"(?:sign in|log in|login)\s+to\s+(?:continue|access|view)", html, re.I) and len(html) < 1500:
            continue
        website = extract_website(html, target, max_chars=200_000)
        content = website["content"]
        if content:
            pages.append({"url": target, "text": content, "venture_name": website["name"],
                          "website_answers": website["answers"]})
        if target in {url, home}:
            links = Links()
            links.feed(html)
            categories = ("about", "pricing", "product", "features", "team", "customers", "case-studies", "blog")
            for category in categories:
                linked = next((link for link in links.urls if urlsplit(link).path.strip("/").split("/")[0] in
                               {category, category + "s", "about-us" if category == "about" else category}), None)
                queue.append(linked or urljoin(home, category))
    return pages


def store_pages(venture_id: str, pages: list[dict]) -> None:
    from hashlib import sha256
    from app.intake.evidence import save_evidence

    for page in pages:
        digest = sha256(f"{venture_id}:{page['url']}:{page['text']}".encode()).hexdigest()
        save_evidence(venture_id, "website", page["url"], page["text"], evidence_id=f"site:{digest}")
