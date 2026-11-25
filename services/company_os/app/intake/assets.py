"""Founder assets use the existing guarded crawler and ordinary venture documents."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from html.parser import HTMLParser
from urllib.parse import parse_qsl, unquote, urljoin, urlsplit
from urllib.robotparser import RobotFileParser

from core.clock import utcnow

SITE_SECONDS = 8.0


def public_site_url(url: str) -> str | None:
    from app.intake.smart import _canonical_url, _repo
    from app.secrets.detect import looks_like_secret
    from core.redact import redact_string

    canonical = _canonical_url(str(url).strip())
    if not canonical or _repo(canonical) or looks_like_secret(unquote(url)) or redact_string(unquote(url)) != unquote(url):
        return None
    if any(re.search(r"(?:token|secret|password|credential|api[_-]?key|signature|^sig$)", key, re.I)
           for key, _ in parse_qsl(urlsplit(canonical).query, keep_blank_values=True)):
        return None
    return canonical


def _allowed_page(url: str, robots: RobotFileParser, deadline: float) -> str | None:
    import httpx
    from core.research_web.guard import get_checked
    from app.gtm.scrapy_leads import _BoundedClient, _host

    class RobotsClient(_BoundedClient):
        def get(self, target, **kwargs):
            # get_checked supplies a pinned address but preserves the path.
            # Check each redirect, so an allowed path cannot lead to a disallowed one.
            from urllib.parse import urlsplit
            if urlsplit("//" + (kwargs.get("headers") or {}).get("Host", "")).hostname != urlsplit(url).hostname:
                raise PermissionError("different_domain")
            if not robots.can_fetch("SupymemResearch", target):
                raise PermissionError("robots_disallowed")
            return super().get(target, **kwargs)

    try:
        with httpx.Client(follow_redirects=False, trust_env=False, timeout=5,
                headers={"User-Agent": "SupymemResearch/1.0", "Accept": "text/html,text/plain", "Accept-Encoding": "identity"}) as client:
            response = get_checked(RobotsClient(client, deadline), url, max_redirects=3)
            return response.text if _host(response.extensions.get("logical_url", url)) == _host(url) else None
    except (httpx.HTTPError, PermissionError, ValueError, RuntimeError, OSError):
        return None


class _Page(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tag = ""
        self.hidden = 0
        self.headline: list[str] = []
        self.in_headline = False
        self.visible: list[str] = []
        self.contact = False
        self.analytics = False

    def handle_starttag(self, tag, attrs):
        self.tag = tag
        attributes = dict(attrs)
        if tag == "h1":
            self.in_headline = True
        if tag in {"script", "style", "noscript"}:
            self.hidden += 1
        if tag == "form" or tag == "a" and str(attributes.get("href", "")).startswith("mailto:"):
            self.contact = True
        if tag == "script" and re.search(r"googletagmanager|google-analytics|plausible|posthog|segment|matomo|fathom|umami", str(attributes.get("src", "")), re.I):
            self.analytics = True

    def handle_endtag(self, tag):
        if tag == "h1":
            self.in_headline = False
        if tag in {"script", "style", "noscript"}:
            self.hidden = max(0, self.hidden - 1)
        self.tag = ""

    def handle_data(self, data):
        if self.hidden:
            if re.search(r"gtag\(|ga\(|posthog\.|analytics\.track|plausible\(", data):
                self.analytics = True
            return
        if data.strip():
            self.visible.append(data.strip())
            if self.in_headline:
                self.headline.append(data.strip())


def audit_html(html: str, *, fetch_ms: int) -> dict:
    page = _Page()
    page.feed(html)
    headline = " ".join(page.headline)[:240]
    cta = bool(re.search(r"\b(?:join|sign\s*up|get started|book|contact|start|try|download|subscribe|buy)\b", " ".join(page.visible), re.I))
    recommendations = []
    if not headline:
        recommendations.append("Add one clear headline explaining what your product does and who it helps.")
    if not cta:
        recommendations.append("Give visitors one clear next action, such as joining a waitlist or booking a call.")
    if not page.contact:
        recommendations.append("Add a working signup or contact form so interested visitors can reach you.")
    if not page.analytics:
        recommendations.append("Check that analytics records visits and completed signups.")
    recommendations.append("Check mobile layout and page speed in a browser before making changes.")
    return {"headline": headline or None, "cta": "present" if cta else "missing",
            "contact": "present" if page.contact else "missing", "analytics": "present" if page.analytics else "unverified",
            "fetch_ms": fetch_ms, "mobile": "unverified", "lighthouse": "unverified",
            "recommendations": recommendations}


def _fetch_site(url: str) -> dict:
    from app.gtm.scrapy_leads import _fetch

    deadline = time.monotonic() + SITE_SECONDS - 1
    policy = _fetch(urljoin(url, "/robots.txt"), deadline=deadline, allow_missing=True)
    if policy is None:
        return {"status": "unverified", "audit": None, "note": "The site's crawl policy could not be checked. Try again later."}
    robots = RobotFileParser()
    robots.parse(policy.splitlines())
    if not robots.can_fetch("SupymemResearch", url):
        return {"status": "unverified", "audit": None, "note": "This site asks crawlers not to read this page."}
    started = time.monotonic()
    html = _allowed_page(url, robots, deadline)
    if not html:
        return {"status": "unverified", "audit": None, "note": "The page could not be read. Your URL is saved so you can try again."}
    # A readable login gate is not evidence of a live product page.
    if re.search(r"(?:sign in|log in|login)\s+to\s+(?:continue|access|view)", html, re.I) and len(html) < 1500:
        return {"status": "unverified", "audit": None, "note": "The page needs a login. Share a public page to audit it."}
    from app.intake.crawl import crawl_site
    pages = crawl_site(url, deadline=deadline, robots=robots, home_html=html)
    return {"status": "verified", "audit": audit_html(html, fetch_ms=round((time.monotonic() - started) * 1000)),
            "evidence_pages": pages,
            "note": "The public page was read. Form delivery, mobile layout and browser performance still need checking."}


def _read_site(url: str) -> dict:
    """Kill stalled DNS/fetches under one deadline, without passing process secrets."""
    env = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT", "LANG", "LC_ALL") if key in os.environ}
    paths = [str(Path(__file__).resolve().parents[2]), str(Path(__file__).resolve().parents[4] / "packages"),
             *(str(Path(path).resolve()) for path in os.environ.get("PYTHONPATH", "").split(os.pathsep) if path)]
    env["PYTHONPATH"] = os.pathsep.join(dict.fromkeys(path for path in paths if path))
    try:
        result = subprocess.run([sys.executable, "-m", "app.intake.assets"], input=json.dumps({"url": url}),
            capture_output=True, text=True, timeout=SITE_SECONDS, check=False, env=env,
            cwd=Path(__file__).resolve().parents[2])
        if result.returncode == 0 and len(result.stdout.encode()) <= 8_000_000:
            data = json.loads(result.stdout)
            if isinstance(data, dict) and data.get("status") in {"verified", "unverified"}:
                return data
    except (subprocess.TimeoutExpired, ValueError, OSError):
        pass
    return {"status": "unverified", "audit": None, "note": "The page check timed out. Your URL is saved so you can try again."}


def import_site(venture_id: str, url: str) -> dict:
    from app.db.models import VentureDocument
    from app.state import db
    from app.ventures.stage import reassess

    canonical = public_site_url(url)
    if not canonical:
        raise ValueError("Share a public website URL starting with https:// or http://.")
    fetched = _read_site(canonical)
    from app.intake.crawl import store_pages
    store_pages(venture_id, fetched.pop("evidence_pages", []))
    site = {"url": canonical, **fetched, "checked_at": utcnow().isoformat()}
    with db()() as session:
        row = session.get(VentureDocument, f"external_site:{venture_id}")
        if row is None:
            row = VentureDocument(id=f"external_site:{venture_id}", venture_id=venture_id, kind="external_site",
                title="Your existing website", created_at=utcnow())
            session.add(row)
        if row.venture_id != venture_id:
            raise ValueError("This website could not be saved to your venture.")
        row.path, row.body_md, row.updated_at = canonical, json.dumps(site), utcnow()
        row.provenance_json = json.dumps({"source": "founder_import", "verified": site["status"] == "verified",
            "checked_at": site["checked_at"], "verification_scope": "public_page_fetch"})
        session.commit()
    return {"site": site, "founder_stage": reassess(venture_id)}


def get_site(venture_id: str, *, session=None) -> dict:
    from app.db.models import VentureDocument
    from app.state import db
    from app.ventures.stage import get_stage

    if session is None:
        with db()() as owned:
            result = get_site(venture_id, session=owned)
            owned.commit()
            return result
    row = session.get(VentureDocument, f"external_site:{venture_id}")
    site = None
    if row and row.venture_id == venture_id and row.path:
        try:
            site = json.loads(row.body_md or "{}")
        except ValueError:
            site = {}
        if not isinstance(site, dict) or site.get("url") != row.path or site.get("status") not in {"verified", "unverified"} or (
                site.get("status") == "verified" and not isinstance(site.get("audit"), dict)):
            site = {"url": row.path, "status": "unverified", "audit": None,
                    "note": "Your URL is saved. Import it to check the public page."}
    return {"site": site, "founder_stage": get_stage(venture_id, session=session)}


def intake_questions(decision: dict, original: str, venture_id: str | None = None) -> dict:
    from app.intake.smart import _founder_data, _owned_urls, _repo

    extracted = decision.get("extracted", {})
    known = {"website": extracted.get("existing_site_url") or "", "contacts": "", "built": extracted.get("repo_url") or "", "tried": ""}
    owned_urls = _owned_urls(original)
    known["website"] = known["website"] or next((url for url in owned_urls if not _repo(url)), "")
    known["built"] = known["built"] or next((repo for url in owned_urls if (repo := _repo(url))), "")
    for key in ("contacts", "tried"):
        match = re.search(r"^\s*" + key + r"\s*:\s*(.+)$", _founder_data(original), re.I | re.M)
        if match:
            known[key] = match[1].strip()[:2000]
    if venture_id:
        from app.db.models import VentureDocument
        from app.state import db
        with db()() as session:
            for kind in ("founder_assets", "intake_decision", "external_site"):
                row = session.get(VentureDocument, f"{kind}:{venture_id}")
                if not row or row.venture_id != venture_id or row.kind != kind:
                    continue
                try:
                    stored = json.loads(row.body_md or "{}")
                except ValueError:
                    stored = {}
                if kind == "founder_assets" and isinstance(stored, dict):
                    for key in known:
                        if not known[key] and isinstance(stored.get(key), str):
                            known[key] = stored[key][:2048 if key == "website" else 2000]
                if kind == "intake_decision" and isinstance(stored, dict):
                    known["built"] = known["built"] or str((stored.get("extracted") or {}).get("repo_url") or "")
                if kind == "external_site":
                    known["website"] = known["website"] or row.path or ""
    from app.ventures.startup_review import _known
    known = {key: value if _known(value) else "" for key, value in known.items()}
    questions = [
        ("website", "Do you have a website or landing page?", "Paste your public URL"),
        ("contacts", "Have you talked to potential customers or built a list?", "Share a count; import your list after setup"),
        ("built", "Is anything built yet?", "Share a repo URL or describe your prototype"),
        ("tried", "What have you tried, and what happened?", "Share one result"),
    ]
    return {"asset_prefill": known, "asset_questions": [{"id": key, "question": question,
            "recommended": recommended, "skip": True, "value": known[key]} for key, question, recommended in questions
            if not known[key].strip()]}


def chat_asset_reply(venture_id: str, text: str) -> dict | None:
    """Offer the reviewed site check or contact import as in-chat actions; a chat sentence alone imports nothing."""
    from app.chat.assets_in_chat import ask_for_contacts, pasted_contacts
    from app.intake.smart import _owned_urls, _repo
    from app.secrets.detect import looks_like_secret
    from core.redact import redact_string

    if looks_like_secret(text) or redact_string(text) != text:
        return None

    site = next((url for url in _owned_urls(text) if not _repo(url)), None)
    if site and public_site_url(site) and re.search(r"\b(?:already|existing|my|our)\b[^\n]{0,70}\b(?:site|website|page)\b", text, re.I):
        return {"kind": "text", "body_md": "Let's check your existing website and continue from what you already have.",
                "actions": [{"label": "Review existing website", "action": "website.review",
                             "payload": {"url": public_site_url(site)}, "style": "primary"}], "citations": []}
    if (re.search(r"\b(?:here (?:are|is)|import|upload|paste|already have)\b[^\n]{0,70}\b(?:contacts|contact list|csv|prospect list)\b", text, re.I)
            or pasted_contacts(text)):
        return ask_for_contacts(text)
    return None


if __name__ == "__main__":
    raw = sys.stdin.buffer.read(4097)
    if len(raw) > 4096:
        raise SystemExit(2)
    print(json.dumps(_fetch_site(json.loads(raw)["url"])))
