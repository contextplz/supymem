"""LinkedIn people: server-side Exa search, with Google results on the founder's Mac as fallback.

Supymem never opens, fetches or automates linkedin.com. Exa returns profile metadata on the server.
Without its key, the founder's Mac reads only Google search results. Both sources feed this store
and playbook messages the founder copies and sends by hand.
"""
from __future__ import annotations

import os

import asyncio
import hashlib
import json
import logging
import re
from datetime import timedelta
from html.parser import HTMLParser
from urllib.parse import parse_qs, quote_plus, unquote, urlsplit

from fastapi import HTTPException
from sqlalchemy import select

from app.authz.workspace import accessible_workspace_ids, assert_venture_access
from app.db.models import Venture, VentureDocument
from app.state import db
from core.clock import utcnow
from core.ids import new_id

log = logging.getLogger(__name__)

KIND = "linkedin_people_v1"
DAILY_TARGET = 10
MAX_SEARCHES_PER_DAY = 3  # automatic top-ups; "Find more" always gets one more
ORDER_TIMEOUT = timedelta(minutes=20)
MAC_FRESH = timedelta(minutes=15)  # the Mac checks in every minute (older releases every 10)
MAX_PAGE_BYTES = 3_000_000
NO_MAC = "Open the Supymem Mac app to find LinkedIn people."
CAPTCHA = ("Google asked for a check. Open Google once in Chrome and search anything, then press Find more. "
           "Supymem never solves these checks.")
_PROFILE = re.compile(r"^/in/([^/?#\s]{2,100})/?(?:[a-z]{2}/?)?$", re.I)


# ---------- Google results page -> LinkedIn profiles ----------

def profile_url(href: str) -> str | None:
    """The exact https://www.linkedin.com/in/<slug> for a result link, unwrapping Google redirects and dropping tracking."""
    if not isinstance(href, str):
        return None
    href = (href or "").strip()
    try:
        parts = urlsplit(href)
        if parts.path == "/url" and (parts.netloc in {"", "www.google.com", "google.com"}):
            query = parse_qs(parts.query)
            href = (query.get("q") or query.get("url") or [""])[0]
            parts = urlsplit(href)
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    if parts.scheme not in {"http", "https"} or not (host == "linkedin.com" or host.endswith(".linkedin.com")):
        return None
    match = _PROFILE.match(parts.path)
    if not match:
        return None
    slug = unquote(match[1])
    if not re.fullmatch(r"[\w\-.%À-ɏ]+", slug):
        return None
    return "https://www.linkedin.com/in/" + quote_plus(slug, safe="-_.%").rstrip("/")


class _Results(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.results: list[dict] = []
        self._skip = 0
        self._anchor: dict | None = None
        self._depth = 0
        self._tail: dict | None = None

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript"}:
            self._skip += 1
        if self._anchor is not None and tag == "a":
            self._depth += 1
        if tag == "a" and self._anchor is None:
            url = profile_url(dict(attrs).get("href") or "")
            if url:
                self._anchor, self._depth, self._tail = {"url": url, "text": [], "h3": []}, 1, None
        if self._anchor is not None and tag == "h3":
            self._anchor["in_h3"] = True

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript"} and self._skip:
            self._skip -= 1
        if self._anchor is not None and tag == "h3":
            self._anchor["in_h3"] = False
        if self._anchor is not None and tag == "a":
            self._depth -= 1
            if self._depth <= 0:
                anchor, self._anchor = self._anchor, None
                title = " ".join(" ".join(anchor["h3"] or anchor["text"]).split())
                self._tail = {"url": anchor["url"], "title": title[:300], "snippet": []}
                self.results.append(self._tail)

    def handle_data(self, data):
        if self._skip:
            return
        text = " ".join(data.split())
        if not text:
            return
        if self._anchor is not None:
            self._anchor["h3" if self._anchor.get("in_h3") else "text"].append(text)
        elif self._tail is not None and len(" ".join(self._tail["snippet"])) < 320:
            if "›" not in text and not text.startswith("http") and text not in {"·", "...", "Translate this page", "Read more"}:
                self._tail["snippet"].append(text)


def extract(html: str) -> list[dict]:
    """[{url, title, snippet}] for linkedin.com/in results only, first occurrence wins."""
    parser = _Results()
    parser.feed((html or "")[:MAX_PAGE_BYTES])
    out: dict[str, dict] = {}
    for item in parser.results:
        key = item["url"].casefold()
        snippet = " ".join(" ".join(item["snippet"]).split())[:320]
        if key not in out and item["title"]:
            out[key] = {"url": item["url"], "title": item["title"], "snippet": snippet}
        elif key in out and not out[key]["snippet"]:
            out[key]["snippet"] = snippet
    return list(out.values())


def captcha(final_url: str, html: str) -> bool:
    return (urlsplit(final_url or "").path.startswith("/sorry")
            or bool(re.search(r"g-recaptcha|id=\"captcha-form\"|unusual traffic from your computer network", html or "", re.I)))


def clean_profile_text(text: str) -> str:
    text = text if isinstance(text, str) else ""
    text = re.sub(r"!?\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"^\s*#{1,6}\s*", "", text, flags=re.M)
    return " ".join(re.sub(r"[*_`]+", "", text).replace("—", "-").split())


def _profile_summary(text: str) -> str:
    text = clean_profile_text(text)
    # ponytail: punctuation-based sentences; use a locale-aware splitter if abbreviations need special handling.
    sentences = re.split(r"(?<=[.!?])\s+", text)
    summary = ""
    for sentence in sentences[:2]:
        candidate = " ".join(filter(None, (summary, sentence)))
        if len(candidate) > 220:
            break
        summary = candidate
    return summary or text[:219].rsplit(" ", 1)[0].rstrip(".,;:") + ("…" if text else "")


def parse_title(title: str) -> dict:
    """'Ravi Kumar - Owner - Iron Gym | LinkedIn' -> name, headline, company (only what the title says)."""
    text = re.sub(r"\s*[|\-–—]\s*LinkedIn\s*$", "", clean_profile_text(title), flags=re.I).strip()
    parts = [part.strip() for part in re.split(r"\s+[-–—|]\s+", text) if part.strip()]
    return {"name": parts[0] if parts else "", "headline": parts[1] if len(parts) > 1 else "",
            "company": parts[2] if len(parts) > 2 else ""}


def clean_person(person: dict) -> dict:
    title = parse_title(person.get("title") or "")
    raw_lines = (person.get("snippet") or "").splitlines()
    lines = [clean_profile_text(line) for line in raw_lines]
    heading = next((clean_profile_text(line) for line in raw_lines if re.match(r"^\s*#\s+", line)), "")
    name = title["name"] or heading or clean_profile_text(person.get("name") or person.get("author") or "")
    slug = urlsplit(person["url"]).path.split("/")[2]
    if name.islower() and re.sub(r"[\W\d]", "", name) == re.sub(r"[\W\d]", "", slug).lower():
        name = heading if heading and not heading.islower() else "LinkedIn member"
    about = next((index for index, line in enumerate(lines) if line.lower() == "about"), len(lines))
    metadata = [line for line in lines[:about] if line and line.casefold() != name.casefold()
                and line.casefold() not in {"headline", "experience", "education", "contact info"}]
    headline = title["headline"] or clean_profile_text(person.get("headline") or person.get("role") or "")
    if not headline or headline.casefold() == name.casefold():
        headline = next((line for line in metadata if not line.lower().startswith("location:")), "")
    location = clean_profile_text(person.get("location") or "")
    if not location:
        location = next((line.split(":", 1)[1].strip() for line in metadata if line.lower().startswith("location:")), "")
    # ponytail: unlabelled location follows the headline; use structured Exa metadata if the profile format changes.
    if not location and headline in metadata:
        index = metadata.index(headline) + 1
        if index < len(metadata) and not re.search(r"[.!?]", metadata[index]):
            location = metadata[index]
    company = title["company"] or clean_profile_text(person.get("company") or "")
    at = re.search(r"\s+at\s+(.+)", headline, re.I)
    company = company or (at[1] if at else "")
    evidence = lines[about + 1:] if about < len(lines) else lines
    snippet = " ".join(line for line in evidence if line and line.casefold() not in {name.casefold(), headline.casefold(), location.casefold(), company.casefold()}
                       and line.casefold() not in {"about", "headline", "experience", "education", "contact info"}
                       and not line.lower().startswith("location:"))[:600] or ". ".join(filter(None, (headline, location)))
    why = person.get("why") or snippet
    if person.get("source") == "exa":
        query = clean_profile_text(person.get("query") or "")
        why = (f"Found for your search: {query}. " if query else "") + snippet
    return {**person, "name": name[:200] or "LinkedIn member", "headline": headline[:200], "role": headline[:200],
            "company": company[:200], "location": location[:200], "snippet": snippet, "why": _profile_summary(why)}


# ---------- queries ----------

def _location(icp: dict) -> str:
    for key in ("location", "geography"):
        value = icp.get(key)
        if isinstance(value, str) and value.strip():
            return " ".join(value.split())[:60]
    found = re.search(r"\b(?:in|near|around)\s+([A-Z][\w.]+(?:[ ,]+[A-Z][\w.]+){0,2})", icp.get("who") or "")
    return found[1].strip(" ,") if found else ""


# Words that name nobody on LinkedIn: "her customers", "my users", "people".
_VAGUE = {"her", "his", "my", "our", "their", "its", "your", "mom's", "mum's", "customers", "customer", "users", "user",
          "people", "clients", "client", "buyers", "buyer", "folks", "everyone", "anyone", "them", "someone", "something"}


def _role(who: str, location: str) -> str:
    text = re.split(r"\b(?:in|near|around|who|that|with|across)\b", who or "", maxsplit=1)[0]
    if location:
        text = text.replace(location, "")
    words = [w for w in re.findall(r"[A-Za-z][\w&'-]*", text)
             if w.lower() not in {"independent", "small", "local", "the", "a", "an", "of", "and", "for", "busy", "early", "stage"} | _VAGUE]
    words = words[-3:]
    if words and words[-1].lower().endswith("s") and not words[-1].lower().endswith("ss"):
        words[-1] = words[-1][:-1]
    return " ".join(words).lower()


def clean_role(icp: dict) -> str:
    """Role words from the saved ICP, or "" when it names nobody a LinkedIn profile could match."""
    return _role(icp.get("who") or icp.get("suggested_who") or "", _location(icp))


def suggest_roles(wid: str, vid: str, icp: dict, idea: str) -> tuple[list[str], str]:
    """One flash call: LinkedIn-style role titles (and a city if stated) for a messy audience. ([], "") on failure."""
    from app.credits import metered
    try:
        response = asyncio.run(metered.llm([
            {"role": "system", "content": "Turn a startup's customer description into job titles people put on LinkedIn. "
                "Return JSON only: {\"roles\":[up to 3 short titles, e.g. \"home baker\", \"bakery owner\"],\"city\":\"<city if stated, else empty>\"}."},
            {"role": "user", "content": json.dumps({"customer": icp.get("who") or icp.get("suggested_who") or "",
                                                    "problem": icp.get("problem") or "", "idea": idea[:400]})}],
            purpose="venture.chat.assistant", workspace_id=wid, venture_id=vid, tier="flash", max_tokens=2000))
        text = str(response.choices[0].message.content or "")
        data = json.loads(text[text.index("{"):text.rindex("}") + 1])
        roles = [" ".join(str(r).split())[:40].lower() for r in data.get("roles") or [] if str(r).strip()][:3]
        return roles, " ".join(str(data.get("city") or "").split())[:60]
    except Exception as error:  # noqa: BLE001 - the idea words still give a query
        log.info("linkedin_roles_fallback", extra={"error_class": type(error).__name__})
        return [], ""


def build_queries(icp: dict, idea: str = "", roles: list[str] | None = None, city: str = "") -> list[str]:
    """3-5 Google queries restricted to linkedin.com/in, from clean role words (ICP or suggested), the idea and the location."""
    from app.gtm.journey import _keywords
    location = _location(icp) or city
    loc = f' "{location}"' if location else ""
    base = "site:linkedin.com/in "
    if roles and not clean_role(icp):
        queries = [f'{base}"{r}"{loc}' for r in roles] + [f'{base}"{roles[0]}"']
        return list(dict.fromkeys(queries))[:5]
    role = clean_role(icp) or _role(idea, "")
    queries = []
    if role:
        queries += [f'{base}"{role}"{loc}', f'{base}"{role}"']
        noun = role.split()[0]
        if not re.search(r"\b(?:owner|founder|manager|director|head|ceo)\b", role):
            queries += [f'{base}"{noun}" owner{loc}', f'{base}"{noun}" founder{loc}']
    topic = _keywords(" ".join(filter(None, [icp.get("problem") or "", idea])), 3)
    if topic and topic != "customer needs":
        queries.append(f"{base}{topic}{loc}")
    return list(dict.fromkeys(q.strip() for q in queries))[:5]


def google_url(query: str, page: int = 0) -> str:
    return f"https://www.google.com/search?q={quote_plus(query)}&hl=en" + (f"&start={page * 10}" if page else "")


# ---------- storage ----------

def _today() -> str:
    # ponytail: UTC day; use the founder's timezone if "a day" must follow their clock.
    return utcnow().date().isoformat()


def _doc(session, vid: str, *, create=False, lock=False):
    stmt = select(VentureDocument).where(VentureDocument.venture_id == vid, VentureDocument.kind == KIND)
    row = session.scalar(stmt.with_for_update() if lock else stmt)
    if row is None and create:
        now = utcnow()
        row = VentureDocument(id=new_id("lip"), venture_id=vid, kind=KIND, title="LinkedIn people",
                              body_md="", provenance_json="{}", created_at=now, updated_at=now)
        session.add(row)
    data = json.loads(row.provenance_json or "{}") if row is not None else {}
    data.setdefault("people", [])
    data.setdefault("seen", [])
    data.setdefault("searches", {})
    data.setdefault("query_runs", {})
    return row, data


def _save(row, data):
    data["seen"] = data["seen"][-3000:]
    row.provenance_json, row.updated_at = json.dumps(data), utcnow()


def new_today(data: dict, today: str | None = None) -> int:
    today = today or _today()
    return sum(1 for person in data["people"] if person.get("found_on") == today and profile_url(person.get("url", "")))


def wants_search(data: dict, now=None) -> bool:
    """Daily top-up: search until 10 new people today (max 3 automatic searches); on demand always."""
    now = now or utcnow()
    today = now.date().isoformat()
    order = data.get("order") or {}
    if order.get("state") == "running" and order.get("started_at", "") > (now - ORDER_TIMEOUT).isoformat():
        return False
    if data.get("requested_at"):
        return True
    if (data.get("needs_human") or {}).get("date") == today:
        return False
    return new_today(data, today) < DAILY_TARGET and data["searches"].get(today, 0) < MAX_SEARCHES_PER_DAY


def _venture_icp(vid: str) -> tuple[dict, str]:
    from app.gtm import journey
    with db()() as session:
        venture = session.get(Venture, vid)
        idea = (venture.raw_idea or venture.name or "") if venture else ""
    try:
        icp = journey._icp(vid)
    except Exception:  # noqa: BLE001 - the idea alone still gives a query
        icp = {}
    return icp, idea


# ---------- Mac side (device caller) ----------

def _device(caller):
    if caller.legacy or not caller.device_id or caller.role != "user":
        raise HTTPException(403, detail={"code": "authenticated_device_required"})


def next_order(caller) -> dict:
    """The Mac asks for work. Marks every venture as Mac-connected and hands out at most one search."""
    _device(caller)
    if os.environ.get("EXA_API_KEY"):
        return {"order": None}
    # Founder rule: research is invisible. Desktop 0.1.101 opens a visible Chrome window for these searches,
    # so the Mac gets no work until a release that searches headless turns this on.
    if os.environ.get("LINKEDIN_MAC_SEARCH", "on").strip().lower() in {"0", "false", "off"}:
        return {"order": None}  # kill switch; the headless marker on the API route keeps old visible builds out
    wids = accessible_workspace_ids(caller.sub)
    if not wids:
        return {"order": None}
    with db()() as session:
        vids = session.scalars(select(Venture.id).where(Venture.workspace_id.in_(wids), Venture.archived_at.is_(None))
                               .order_by(Venture.updated_at.desc()).limit(20)).all()
    picked = None
    now = utcnow()
    for vid in vids:
        with db()() as session:
            row, data = _doc(session, vid, create=True, lock=True)
            data["mac_seen_at"] = now.isoformat()
            if picked is None and wants_search(data, now):
                picked, requested = vid, bool(data.get("requested_at"))
            _save(row, data)
            session.commit()
    if picked is None:
        return {"order": None}
    icp, idea = _venture_icp(picked)
    roles, city = [], ""
    if not clean_role(icp):
        # "her customers who order cakes on whatsapp" matches no profile: ask for role titles once.
        with db()() as session:
            venture = session.get(Venture, picked)
            wid = venture.workspace_id if venture else ""
        roles, city = suggest_roles(wid, picked, icp, idea)
    queries = build_queries(icp, idea, roles, city)
    log.info("linkedin_order_queries", extra={"venture_id": picked, "queries": queries, "suggested_roles": roles})
    with db()() as session:
        row, data = _doc(session, picked, create=True, lock=True)
        if not queries:
            data["requested_at"] = None
            data["order"] = {"state": "failed", "finished_at": now.isoformat(), "message": "Add who your customer is on the Customers page so Supymem knows whom to search for."}
            _save(row, data)
            session.commit()
            return {"order": None}
        order = {"id": new_id("lis"), "state": "running", "started_at": now.isoformat(), "requested": requested,
                 "queries": [{"query": q, "url": google_url(q, data["query_runs"].get(q, 0))} for q in queries]}
        data["order"] = order
        _save(row, data)
        session.commit()
    return {"order": {"id": order["id"], "venture_id": picked, "queries": order["queries"]}}


async def _judge(wid: str, vid: str, icp: dict, idea: str, candidates: list[dict]) -> dict[int, str]:
    """One cheap metered call: which titles/snippets fit the ICP, with a one-line why from that text only."""
    from app.credits import metered
    who = icp.get("who") or icp.get("suggested_who") or ""
    try:
        response = await metered.llm([
            {"role": "system", "content": "You pick LinkedIn profiles that fit a startup's ideal customer. Use only each item's "
                "title and snippet; never guess facts that are not written there. Return JSON only: "
                "{\"keep\":[{\"i\":<index>,\"why\":<one plain sentence under 25 words quoting what in the title or snippet fits>}]}."},
            {"role": "user", "content": json.dumps({"ideal_customer": who, "problem": icp.get("problem") or "", "location": _location(icp),
                "idea": idea[:400], "items": [{"i": i, "title": c["title"], "snippet": c["snippet"][:240]} for i, c in enumerate(candidates)]})}],
            purpose="venture.chat.assistant", workspace_id=wid, venture_id=vid, tier="flash", max_tokens=2000)
        text = str(response.choices[0].message.content or "")
        items = json.loads(text[text.index("{"):text.rindex("}") + 1])["keep"]
        return {int(item["i"]): " ".join(str(item["why"]).split())[:200] for item in items
                if isinstance(item.get("i"), int) and 0 <= item["i"] < len(candidates) and str(item.get("why") or "").strip()}
    except Exception as error:  # noqa: BLE001 - never stuck: fall back to the role words in the title
        log.info("linkedin_judge_fallback", extra={"error_class": type(error).__name__})
    role = _role(who, _location(icp))
    words = [w for w in role.split() if len(w) > 2]
    return {i: f'Their public title mentions "{role}".' for i, c in enumerate(candidates)
            if words and all(w in (c["title"] + " " + c["snippet"]).lower() for w in words)}


def _candidates(order: dict, pages: list[dict], known: set[str], stats: dict | None = None) -> tuple[bool, list[dict]]:
    """New profiles from the pages of this order only; stops at the first CAPTCHA page."""
    queries = {item["url"]: item["query"] for item in order["queries"]}
    candidates = []
    stats = stats if stats is not None else {}
    for page in pages:
        if page.get("url") not in queries:
            continue
        if captcha(page.get("final_url") or "", page.get("html") or ""):
            return True, candidates
        results = extract(page.get("html") or "")
        stats["pages"] = stats.get("pages", 0) + 1
        stats["raw_links"] = stats.get("raw_links", 0) + len(re.findall(r"<a\s", page.get("html") or "", re.I))
        stats["profile_links"] = stats.get("profile_links", 0) + len(results)
        for result in results:
            if result["url"].casefold() not in known:
                known.add(result["url"].casefold())
                candidates.append({**result, "query": queries[page["url"]]})
    return False, candidates


async def record_results(caller, vid: str, order_id: str, pages: list[dict], stopped_for_captcha: bool = False) -> dict:
    """The Mac posts the Google results pages it read. Extract, dedupe, judge, top up."""
    _device(caller)
    _, wid = assert_venture_access(vid, caller.sub)
    with db()() as session:
        row, data = _doc(session, vid, lock=True)
        order = data.get("order") or {}
        if row is None or order.get("id") != order_id or order.get("state") != "running":
            raise HTTPException(409, detail={"code": "linkedin_search_not_running"})
        backlog = data.get("backlog") or []
        known = {p["url"].casefold() for p in data["people"] + backlog} | {u.casefold() for u in data["seen"]}
        requested = order.get("requested")
        need = DAILY_TARGET if requested else max(0, DAILY_TARGET - new_today(data))
    stats: dict = {}
    hit_captcha, candidates = await asyncio.to_thread(_candidates, order, pages, known, stats)  # parsing stays off the event loop
    hit_captcha = hit_captcha or bool(stopped_for_captcha)
    icp, idea = _venture_icp(vid)
    fit = await _judge(wid, vid, icp, idea, candidates[:60]) if candidates and need else {}
    now, today = utcnow(), _today()
    ready = [{"id": "lip_" + hashlib.sha256(candidates[i]["url"].casefold().encode()).hexdigest()[:12], "url": candidates[i]["url"],
              **parse_title(candidates[i]["title"]), "title": candidates[i]["title"], "snippet": candidates[i]["snippet"], "why": why,
              "query": candidates[i]["query"], "sent_at": None, "replied_at": None} for i, why in sorted(fit.items())]
    pool = backlog + ready  # fit people beyond today's need wait for the next top-up
    added = [{**person, "found_on": today, "found_at": now.isoformat()} for person in pool[:need]]
    log.info("linkedin_results", extra={"venture_id": vid, "order_id": order_id, "pages": stats.get("pages", 0),
             "raw_links": stats.get("raw_links", 0), "profile_links": stats.get("profile_links", 0),
             "new_candidates": len(candidates), "kept": len(fit), "added": len(added), "captcha": hit_captcha,
             "queries": [item["query"] for item in order["queries"]]})
    message = None if hit_captcha or added else no_match_message(order, len(candidates))
    with db()() as session:
        row, data = _doc(session, vid, lock=True)
        data["people"] += added
        data["backlog"] = pool[need:][:100]
        data["seen"] += [c["url"] for i, c in enumerate(candidates[:60]) if i not in fit]  # judged unfit only
        data["searches"] = {today: data["searches"].get(today, 0) + 1}
        for item in order["queries"]:
            data["query_runs"][item["query"]] = data["query_runs"].get(item["query"], 0) + 1
        data["requested_at"] = None
        data["order"] = {**order, "state": "needs_human" if hit_captcha else "done", "finished_at": now.isoformat(), "found": len(added),
                         "message": message}
        data["needs_human"] = {"date": today, "message": CAPTCHA} if hit_captcha else None
        _save(row, data)
        session.commit()
    if hit_captcha:
        try:  # the browser is never shown; the founder hears about the check in chat
            from app.chat.store import append_message
            append_message(vid, role="assistant", body_md=CAPTCHA,
                           actions=[{"label": "Open LinkedIn people", "href": f"/v/{vid}/linkedin", "style": "primary"}])
        except Exception as error:  # noqa: BLE001 - the LinkedIn page still shows needs_human
            log.info("linkedin_captcha_chat_failed", extra={"error_class": type(error).__name__})
    return {"found": len(added), "status": "needs_human" if hit_captcha else "done"}


def no_match_message(order: dict, candidates: int) -> str:
    """Plain words for a search that found nobody: what was searched and what to try."""
    first = next((item["query"] for item in order.get("queries") or []), "")
    searched = " ".join(first.replace("site:linkedin.com/in", "").replace('"', "").split()) or "your customer"
    if candidates:
        return f"{candidates} profiles came back for '{searched}', but none fit your customer. Try a different role or city?"
    return f"No profiles matched '{searched}'. Try a different role or city?"


# ---------- founder side ----------

def local_view(venture_id: str, subject: str) -> dict:
    from app.gtm import playbooks
    from app.core.local_secrets import read_settings, get_secret

    assert_venture_access(venture_id, subject)
    icp, idea = _venture_icp(venture_id)
    with db()() as session:
        _, data = _doc(session, venture_id)
    chosen = [{"key": next(iter(playbooks.PLAYBOOKS))}]
    profiles = [clean_person(person) for person in data["people"] if profile_url(person.get("url", ""))]
    settings = read_settings("search")["values"]
    return {"venture_id": venture_id, "local_mode": True, "mac_connected": False,
        "notice": None, "needs_human": None, "searching": False, "queued_at": None,
        "exa_configured": settings.get("provider") == "exa" and bool(get_secret("search", "api_key")),
        "has_audience": bool(str(icp.get("who") or "").strip()), "searches_today": 0,
        "search_limit": 0, "next_search_at": None, "today": {"new": new_today(data), "target": DAILY_TARGET},
        "last_search": data.get("local_search"), "fallback": [],
        "people": [{**person, "messages": _messages(person, chosen, idea)} for person in profiles]}


async def local_find(venture_id: str, subject: str) -> dict:
    from app.db.session import serialize_writes
    from app.core.local_capabilities import enabled, blocked_message
    from app.tenancy import bind
    from core.clock import utcnow
    from core.ids import new_id

    _, workspace_id = assert_venture_access(venture_id, subject)
    if not enabled("research"):
        raise HTTPException(409, detail={"message": blocked_message("research")})
    icp, _ = _venture_icp(venture_id)
    audience = str(icp.get("who") or "").strip()
    if not audience:
        raise HTTPException(422, detail={"message": "Open Idea, describe who your customers are, then select Find more people on LinkedIn."})
    query = f"site:linkedin.com/in {audience}"
    try:
        from app.research.local_providers import local_search
        with bind(workspace_id=workspace_id, venture_id=venture_id, actor=subject):
            results = await local_search(query, limit=DAILY_TARGET)
    except Exception:
        raise HTTPException(502, detail={"message": "Open Settings > Keys and connections > Search, choose Built-in search and save, then select Find more people on LinkedIn."}) from None
    now = utcnow().isoformat()
    with db()() as session:
        serialize_writes(session, key="linkedin-people:" + venture_id)
        row, data = _doc(session, venture_id, create=True, lock=True)
        known = {person.get("url") for person in data["people"]}
        found = 0
        for result in results:
            url = profile_url(str(result.get("url") or ""))
            if not url or url in known:
                continue
            snippet = clean_profile_text(str(result.get("snippet") or result.get("text") or result.get("description") or ""))[:600]
            data["people"].append({"id": new_id("lip"), "url": url, "source_url": url,
                "title": str(result.get("title") or "")[:600], "snippet": snippet,
                "why": f"Found for your search: {audience}. {snippet}"[:220],
                "found_on": now[:10], "found_at": now, "sent_at": None, "replied_at": None})
            known.add(url)
            found += 1
        data["local_search"] = {"state": "done", "found": found, "finished_at": now,
            "message": "Your public profiles are saved. Open a profile and send your draft yourself." if found else
                "No new public profiles matched. Open Idea and make your customer description more specific, then select Find more people."}
        _save(row, data)
        session.commit()
    return local_view(venture_id, subject)


def local_mark(venture_id: str, subject: str, person_id: str, *, sent: bool | None = None, replied: bool | None = None) -> dict:
    from core.clock import utcnow
    from app.db.session import serialize_writes

    assert_venture_access(venture_id, subject)
    with db()() as session:
        serialize_writes(session, key="linkedin-people:" + venture_id)
        row, data = _doc(session, venture_id, lock=True)
        person = next((person for person in data["people"] if person["id"] == person_id), None)
        if row is None or person is None:
            raise HTTPException(404, detail={"message": "Refresh LinkedIn and select a saved profile."})
        for field in ("sent", "replied"):
            value = sent if field == "sent" else replied
            if value is not None:
                person[field + "_at"] = utcnow().isoformat() if value else None
        _save(row, data)
        session.commit()
    return local_view(venture_id, subject)


def request(vid: str, subject: str) -> dict:
    """'Find more': enqueue Exa, or ask the Mac to search on its next check."""
    assert_venture_access(vid, subject)
    _flag(vid, subject)
    return view(vid, subject)


def _flag(vid: str, subject: str | None = None, *, automatic: bool = False) -> bool:
    """Ask for one more search; True when the Mac checked in recently."""
    from app.gtm.exa_people import MAX_CALLS, queue
    with db()() as session:
        session.get(Venture, vid, with_for_update=True)
        row, data = _doc(session, vid, create=True, lock=True)
        today = _today()
        searches = data.get("exa_searches", {}).get(today, 0)
        order = data.get("order") or {}
        if os.environ.get("EXA_API_KEY") and searches >= MAX_CALLS:
            return False
        if automatic and (searches or data.get("searches", {}).get(today, 0) or data.get("requested_at")
                          or order.get("state") in {"queued", "running"}
                          or any(str(order.get(key) or "").startswith(today) for key in ("queued_at", "started_at", "finished_at"))):
            return False
        data["requested_at"] = utcnow().isoformat()
        data["needs_human"] = None
        _save(row, data)
        session.commit()
    queue(vid, subject)
    return bool(data.get("mac_seen_at")) and data["mac_seen_at"] > (utcnow() - MAC_FRESH).isoformat()


def mark(vid: str, subject: str, person_id: str, *, sent: bool | None = None, replied: bool | None = None) -> dict:
    assert_venture_access(vid, subject)
    with db()() as session:
        row, data = _doc(session, vid, lock=True)
        person = next((p for p in data["people"] if p["id"] == person_id), None)
        if row is None or person is None:
            raise HTTPException(404, detail={"code": "not_found"})
        now = utcnow().isoformat()
        if sent is not None:
            person["sent_at"] = now if sent else None
        if replied is not None:
            person["replied_at"] = now if replied else None
        _save(row, data)
        session.commit()
    return view(vid, subject)


def _messages(person: dict, chosen: list[dict], idea: str, *, venture_name: str = "") -> list[dict]:
    from app.gtm import playbooks
    # Personalised from the search title/snippet only; structured "Experience: ... · ..." snippets are not quoted.
    lead = {"person": person.get("name") or "", "company": person.get("company") or "",
            "quote": "" if "·" in (person.get("snippet") or "") else person.get("snippet") or ""}
    return [{key: value for key, value in playbooks.messages(lead, item["key"], idea, venture_name=venture_name).items()
             if key in {"playbook", "title", "connection_note", "linkedin_dm"}}
            for item in chosen if item.get("key") in playbooks.PLAYBOOKS]


def _fallback(vid: str) -> list[dict]:
    """Public-web decision-makers from Find customers, each with a LinkedIn SEARCH link (not a profile)."""
    from app.gtm import journey
    with db()() as session:
        row = journey._row(session, vid)
        data = journey._json(row.provenance_json) if row else {}
    people = []
    for lead in data.get("reachable_leads") or []:
        for person in lead.get("people") or []:
            if person.get("linkedin_search"):
                people.append({"name": person.get("name"), "role": person.get("role"), "company": person.get("company"),
                               "source_url": person.get("source_url"), "linkedin_search": person["linkedin_search"]})
    return people[:20]


def view(vid: str, subject: str) -> dict:
    from app.autopilot import outreach
    from app.gtm import journey, playbooks
    assert_venture_access(vid, subject)
    from app.gtm.exa_people import MAX_CALLS, queue
    exa = bool(os.environ.get("EXA_API_KEY"))
    icp, _ = _venture_icp(vid)
    has_audience = bool(str(icp.get("who") or "").strip())
    if exa and has_audience:
        _flag(vid, subject, automatic=True)
    queue(vid, subject)
    with db()() as session:
        _, data = _doc(session, vid)
        venture = session.get(Venture, vid)
        jrow = journey._row(session, vid)
        jdata = journey._json(jrow.provenance_json) if jrow else {}
        chosen = jdata.get("playbooks")
    idea = outreach.venture_description(vid) if venture else ""
    chosen = chosen or playbooks.fallback(playbooks.evidence(vid))
    now = utcnow()
    mac = bool(data.get("mac_seen_at")) and data["mac_seen_at"] > (now - MAC_FRESH).isoformat()
    order = data.get("order") or {}
    searches = data.get("exa_searches" if exa else "searches", {}).get(now.date().isoformat(), 0)
    limit = MAX_CALLS if exa else MAX_SEARCHES_PER_DAY
    running = ((order.get("source") == "exa" and order.get("state") in {"queued", "running"}) or
               (order.get("state") == "running" and order.get("started_at", "") > (now - ORDER_TIMEOUT).isoformat()))
    people = sorted((clean_person(person) for person in data["people"] if profile_url(person.get("url", ""))),
                    key=lambda person: person.get("found_at") or "", reverse=True)
    return {
        "venture_id": vid, "mac_connected": mac, "notice": None if mac or exa else NO_MAC,
        "exa_configured": exa, "has_audience": has_audience,
        "searches_today": searches, "search_limit": limit,
        "next_search_at": (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0).isoformat() if searches >= limit else None,
        "needs_human": (data.get("needs_human") or {}).get("message"),
        "searching": bool(data.get("requested_at")) or running,
        "queued_at": data.get("requested_at") or (order.get("queued_at") or order.get("started_at") if running else None),
        "last_search": {k: order.get(k) for k in ("state", "started_at", "finished_at", "found", "message")} if order else None,
        "today": {"new": min(new_today(data), DAILY_TARGET), "target": DAILY_TARGET},
        "playbooks": chosen,
        "people": [{**p, "messages": _messages(p, chosen, idea, venture_name=venture.name if venture else "")} for p in people],
        "fallback": [] if mac or exa else _fallback(vid),
    }


def _last_search(vid: str) -> str:
    try:
        with db()() as session:
            _, data = _doc(session, vid)
    except Exception:  # noqa: BLE001
        return ""
    order = data.get("order") or {}
    return str(order.get("message") or "") if order.get("state") == "done" and not order.get("found") else ""


def chat_reply(vid: str) -> dict:
    """Chat 'find LinkedIn people' on the already-authorized chat venture."""
    href = f"/v/{vid}/linkedin"
    try:
        mac = _flag(vid)
    except Exception:  # noqa: BLE001 - the page link is still the answer
        mac = False
    last = _last_search(vid)
    with db()() as session:
        _, data = _doc(session, vid)
    saved = any(profile_url(person.get("url", "")) for person in data.get("people") or []) or bool(_fallback(vid))
    body = ((last + "\n\n") if last else "") + ("Your requested search will look for LinkedIn profiles that fit your customer. "
            "Each profile comes with a connection note and follow-up message to copy and send yourself; Supymem never opens LinkedIn."
            if os.environ.get("EXA_API_KEY") else
            "Your Mac will search Google for LinkedIn profiles that fit your customer in the next few minutes. "
            "Supymem never opens LinkedIn: you get each profile link with a connection note and a follow-up message to send yourself."
            if mac else
            NO_MAC + " It reads Google results in your own browser on your Mac. "
            + ("You can review your saved results on the LinkedIn page."
               if saved else "You have no saved profiles or public-web contacts yet. Open LinkedIn people for your Mac setup next step."))
    return {"kind": "text", "body_md": body, "citations": [],
            "actions": [{"label": "Open LinkedIn people", "href": href, "style": "primary"}]}
