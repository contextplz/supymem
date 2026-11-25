"""Offline directory recall through real budgets, homepage gates and judging."""
import asyncio
import json
import re
from pathlib import Path
from urllib.parse import urljoin

import pytest

from tests.unit.gtm.test_b2b_fit_leads import FIXTURE, install_model
from tests.unit.gtm.test_journey import journey_db as journey_db, _work
from tests.unit.gtm.test_journey_signals import _leased_payload

DIRECTORY = json.loads((Path(__file__).parent / "fixtures/directory_physio.json").read_text())
DIRECTORY["html"] = (DIRECTORY["html"].replace("https://profiles.test/", "https://www.linkedin.com/")
    .replace("https://photos.test/", "https://www.instagram.com/").replace("https://directory.invalid/", "https://yell.com/")
    .replace("http://loopback.invalid/", "http://127.0.0.1/"))
HOMES = ["https://willowphysio.example/", "https://birchphysio.example/", "https://cedarphysio.example/"]


@pytest.mark.parametrize("audience,profession", [
    ("cold storage operators", "cold storage operator"),
    ("independent music schools", "independent music school"),
])
@pytest.mark.parametrize("role_search", [False, True], ids=["business_search", "role_fallback"])
def test_local_articles_yield_public_company_and_leader_routes(journey_db, monkeypatch, audience, profession, role_search):
    from app.core.local_mode import is_local
    from app.gtm import journey, reachable
    from app.gtm.discovery import LocalDiscoveryProvider, local_provider

    if not is_local():
        pytest.skip("Local customer discovery uses the laptop's public page reader")
    context = FIXTURE["context"] | {"audience": audience, "problem": "Scheduling repair work"}
    wid, vid = journey_db["wid"], journey_db["vid"]
    journey.update_icp(wid, vid, audience, problem=context["problem"])
    payload = _leased_payload(journey_db)
    article = "https://tradepress.example/news/industry-roundup"
    homes = {f"https://{name.lower()}.example/": name for name in ("Alder", "Birch", "Cedar", "Dogwood", "Elm")}
    profile = "https://www.linkedin.com/in/avery-stone"
    pages = {article: "<article>" + "\n".join(f'<p><a href="{url}">{name}</a> is a {profession}.</p>'
        for url, name in homes.items()) + "</article>"}
    for url, name in homes.items():
        pages[url] = (f'<h1>{name}</h1><p>{name} is a {profession} with regional operations.</p>'
            '<a href="/contact">Contact us</a><a href="/leadership">Leadership</a>')
        pages[url + "contact"] = f"<h1>Contact {name}</h1><p>Our office welcomes business enquiries.</p>"
        pages[url + "leadership"] = f"<h1>{name} leadership</h1><p>Our leadership oversees regional operations.</p>"
    pages["https://alder.example/contact"] += '<p><a href="mailto:info@alder.example">info@alder.example</a></p>'
    pages["https://alder.example/leadership"] += f'<p>Avery Stone, Director of Operations</p><a href="{profile}">LinkedIn</a>'
    pages["https://elm.example/"] = pages["https://elm.example/"].replace('<a href="/contact">Contact us</a>', "")
    pages["https://elm.example/leadership"] += '<p>Dana Price, Owner</p><a href="https://www.linkedin.com/in/dana-price">LinkedIn</a>'

    async def scrape(url, **kwargs):
        return pages[url]

    async def model(payload, stage, context):
        if stage == "marketrole":
            row = context["candidates"][0]
            quote = next(line for line in row["text"].splitlines() if profession in line)
            return FIXTURE["sites"][0]["role"] | {"homepage_quote": quote}
        if stage == "passages":
            return {"items": []}
        if stage == "reachjudge":
            return {"items": [{"id": row["id"], "kind": "business", "name": row["title"], "affiliation": "",
                "url": row["evidence_pages"][0]["url"], "quote": row["evidence_pages"][0]["text"], "fit": 95,
                "why": "Their public site fits your audience."} for row in context["candidates"]]}
        if stage == "reformulate":
            return {"queries": []}
        assert stage == "reachdraft"
        return {"items": []}

    monkeypatch.setattr(journey, "_discovery_model", model)
    monkeypatch.setattr("app.research.local_providers.local_read", scrape)
    monkeypatch.setattr("app.gtm.email_learning.cached", lambda key: None)
    monkeypatch.setattr("app.gtm.email_learning.cache_save", lambda key, value: None)
    article_hit = {"url": article, "title": f"An emerging market for {audience}", "kind": "business"}
    if role_search:
        async def search(self, query, **kwargs):
            return [article_hit] if query == audience else []
        monkeypatch.setattr(LocalDiscoveryProvider, "search", search)
        journal = journey._journal(payload)
        journal.mutate(payload["journey_id"], payload, lambda row, doc, session: doc.update(
            search_target={"target_type": "person", "audience": f"Facilities supervisors at {audience}",
                "organisation_audience": audience, "requested_role": "Facilities supervisor"},
            discovery_plan=[{"query": audience, "intent": "business"}, {"query": "facilities supervisors", "intent": "person"}]))
        asyncio.run(journey._discover(audience, problem=context["problem"], payload=payload))
        leads = journey.status(wid, vid)["reachable_leads"]
        assert all(lead.get("company_contact_fallback") and not lead.get("person") for lead in leads)
    else:
        hits = reachable.candidate_hits([article_hit], context, payload)
        leads = asyncio.run(reachable.discover(local_provider(), hits, [], context, payload))
    assert {lead["name"] for lead in leads} == (set(homes.values()) - {"Elm"} if role_search else set(homes.values()))
    if not role_search:
        elm = next(lead for lead in leads if lead["name"] == "Elm")
        assert elm["person"] == "Dana Price" and [route["kind"] for route in elm["routes"]] == ["linkedin"]
    alder = next(lead for lead in leads if lead["name"] == "Alder")
    assert any(route.get("email") == "info@alder.example" and route["source_url"] == "https://alder.example/contact"
        for route in alder["routes"])
    assert any(route.get("url") == profile and route["source_url"] == "https://alder.example/leadership" for route in alder["routes"]) is not role_search
    birch = next(lead for lead in leads if lead["name"] == "Birch")
    assert any(route.get("url") == "https://birch.example/contact" and route["company_contact"] for route in birch["routes"])
    runtime = journey._journal(payload).document(payload["journey_id"])["_runtime"]
    sources = runtime["source_yield"]
    assert sources[article]["page_type"] == "article"
    assert set(sources[article]["companies"]) == set(homes.values())
    assert sources["https://alder.example/leadership"]["names"] == ["Avery Stone"]
    assert {route["kind"] for route in sources["https://alder.example/contact"]["routes"]} >= {"email", "website"}
    assert not journey.status(wid, vid).get("source_yield")


@pytest.mark.parametrize("audience,role,mode,path", [
    ("commercial greenhouse operators", "", "B2B", "members"),
    ("amateur ceramic painters", "Art instructor", "B2C", "speakers"),
])
def test_local_public_rosters_use_existing_directory_reader(monkeypatch, audience, role, mode, path):
    from tests.company_os_harness import load_company_os_app
    load_company_os_app()
    from app.gtm import reachable

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    hit = {"url": f"https://association.example/{path}", "title": f"{role or audience} {path}",
        "kind": "person", **({"expert_role": role} if role else {})}
    context = {"mode": mode, "audience": audience}
    assert reachable.directory_listing(hit, context)
    assert not reachable.directory_listing(hit | {"url": hit["url"] + "/jordan", "title": "Jordan Ellis"}, context)


@pytest.mark.parametrize("fallback", [False, pytest.param(True, marks=pytest.mark.hosted)], ids=["paid_search", "public_search"])
@pytest.mark.parametrize("search_count", [0, 30], ids=["directory_only", "full_search_queue"])
def test_directory_homepages_use_normal_reachable_path(journey_db, monkeypatch, fallback, search_count):
    from app.core.local_mode import is_local
    from app.gtm import enrich, journey, reachable
    context = FIXTURE["context"]
    journey.update_icp(journey_db["wid"], journey_db["vid"], context["audience"], problem=context["problem"])
    payload = _leased_payload(journey_db)
    calls = install_model(monkeypatch)
    site = FIXTURE["sites"][0]
    names = {home: name for home, name in enrich.directory_organisations(DIRECTORY["html"], DIRECTORY["url"], context["audience"])}
    if is_local():
        original_model = journey._discovery_model

        async def model(payload, stage, context):
            if stage == "marketrole":
                calls.append((stage, context))
                text = context["candidates"][0]["text"]
                name = next(name for name in names.values() if name in text)
                return site["role"] | {"homepage_quote": site["role"]["homepage_quote"].replace(site["name"], name)}
            if stage == "reachdraft":
                return {"items": []}
            return await original_model(payload, stage, context)

        monkeypatch.setattr(journey, "_discovery_model", model)
    monkeypatch.setattr(enrich, "enabled", lambda: True)
    fetched = []
    def fetch(url, **kwargs):
        fetched.append(url)
        if url.endswith("/robots.txt"):
            return "User-agent: *\nAllow: /"
        if url == DIRECTORY["url"]:
            return DIRECTORY["html"]
        host = enrich._host(url)
        return site["html"].replace("willowphysio.example", host).replace(site["name"], names.get(f"https://{host}/", site["name"]) if is_local() else site["name"])
    monkeypatch.setattr("app.gtm.scrapy_leads._fetch", fetch)
    monkeypatch.setattr(enrich, "guessed_routes", lambda *args, **kwargs: [])
    class Provider:
        async def scrape(self, url, **kwargs):
            assert is_local() or url != DIRECTORY["url"]
            return fetch(url)
    provider = reachable.PublicPages() if fallback else Provider()
    seeds = reachable.candidate_hits([DIRECTORY], context, payload)
    assert len(seeds) == 1
    search_hits = reachable.candidate_hits([{"url": f"https://www.linkedin.com/in/physio{index}", "kind": "person",
        "title": f"Physiotherapist {index}", "snippet": "UK physiotherapy clinic"} for index in range(search_count)], context, payload)
    leads = asyncio.run(reachable.discover(provider, search_hits + seeds, [], context, payload, fallback=fallback))
    assert set(HOMES) <= {lead["source_url"] for lead in leads}  # judged public profiles are kept too
    assert DIRECTORY["url"] not in {lead["source_url"] for lead in leads}
    assert all(lead["judging_status"] == "judged" and lead["routes"] for lead in leads)
    assert [stage for stage, _ in calls].count("marketrole") == 3
    assert any(stage == "reachjudge" for stage, _ in calls)
    assert payload["_trace_counts"]["seed:candidate:directory_mined"] == 3
    runtime = journey._journal(payload).document(payload["journey_id"])["_runtime"]
    candidates = runtime["public_reachable_candidates_quality_v1" if fallback else "reachable_candidates_quality_v1"]
    assert len(candidates) == min(search_count + len(HOMES), journey.MAX_CANDIDATES)
    assert all("already_seen_or_candidate_budget:" + enrich._host(home) not in payload.get("_trace_drops", {}) for home in HOMES)
    assert runtime["calls_used"]["directory_pages"] == 1
    assert runtime["calls_used"]["public_page"] <= journey.CALL_CEILINGS["public_page"]
    assert all(used <= runtime["ceilings"].get(bucket, runtime["ceilings"]["contact_pages"])
        for bucket, used in runtime["calls_used"].items())
    assert DIRECTORY["url"] in fetched
    if not is_local():
        assert urljoin(DIRECTORY["url"], "/robots.txt") in fetched
    assert set(HOMES) <= {lead["source_url"] for lead in journey.status(journey_db["wid"], journey_db["vid"])["reachable_leads"]}


@pytest.mark.parametrize("directory_budget,public_budget,blocked,expected", [
    (3, 45, False, 3), (0, 45, False, 0), (3, 0, False, 0), (3, 45, True, 0)])
def test_directory_reads_obey_ceilings_and_robots(journey_db, monkeypatch, directory_budget, public_budget, blocked, expected):
    from app.gtm import journey, reachable
    context = FIXTURE["context"]
    journey.update_icp(journey_db["wid"], journey_db["vid"], context["audience"], problem=context["problem"])
    payload = _leased_payload(journey_db)
    install_model(monkeypatch)
    journal = journey._journal(payload)
    journal.mutate(payload["journey_id"], payload, lambda row, doc, session:
        doc["_runtime"]["ceilings"].update(directory_pages=directory_budget, public_page=public_budget))
    fetched = []
    def fetch(url, **kwargs):
        fetched.append(url)
        if url.endswith("/robots.txt"):
            return "User-agent: *\n" + ("Disallow: /" if blocked else "Allow: /")
        return "<main>Directory of independent physiotherapy clinics.</main>"
    monkeypatch.setattr("app.gtm.scrapy_leads._fetch", fetch)
    hits = [DIRECTORY | {"url": f"{DIRECTORY['url']}/{index}"} for index in range(5)]
    assert asyncio.run(reachable.discover(reachable.PublicPages(), hits, [], context, payload, fallback=True)) == []
    assert len([url for url in fetched if not url.endswith("/robots.txt")]) == expected
    used = journal.document(payload["journey_id"])["_runtime"]["calls_used"]
    assert used.get("directory_pages", 0) <= directory_budget
    assert used.get("public_page", 0) <= public_budget


def test_directory_links_are_bounded_homepages():
    from app.gtm import enrich
    assert enrich.directory_links(DIRECTORY["html"], DIRECTORY["url"]) == HOMES
    links = " ".join(f"[Clinic {index}](https://clinic{index}.co.uk/contact?secret=x#email)" for index in range(20))
    assert enrich.directory_links(links, DIRECTORY["url"]) == [f"https://clinic{index}.co.uk/" for index in range(10)]


def test_directory_links_filter_boilerplate_and_prefer_audience():
    from app.gtm import enrich
    raw = """
    <link rel="stylesheet" href="https://fonts.googleapis.com/css?family=Roboto">
    <link rel="stylesheet" href="https://assets.example/theme.css">
    <script src="https://assets.example/app.js">[Script](https://script.example/)</script>
    <style>[Stylesheet](https://style.example/)</style>
    <img src="https://image.example/photo.png" srcset="https://image2.example/photo.png 2x">
    <iframe src="https://frame.example/"></iframe>
    <a href="https://windows.microsoft.com/">Upgrade your browser</a>
    <a href="https://bupa.com/privacy">Privacy policy</a>
    <a href="https://fonts.gstatic.com/">Font resources</a>
    <a href="https://mozilla.org/">Firefox</a>
    <a href="https://empty.example/"><img src="logo.png"></a>
    <a href="https://birchphysio.example/">Birch Health</a>
    Physiotherapy clinic: <a href="https://willowphysio.example/contact">Willow</a>
    <a href="https://cedarphysio.example/"><strong>Cedar Physiotherapy</strong></a>
    """
    assert enrich.directory_links(raw, DIRECTORY["url"], FIXTURE["context"]["audience"]) == [HOMES[0], HOMES[2], HOMES[1]]
    markdown = "\n".join(["[Birch Health](https://birchphysio.example/)",
        "![Physiotherapy](https://image.example/)", "[Cookies](https://policy.example/)",
        "[Cedar Physiotherapy](https://cedarphysio.example/)"])
    assert enrich.directory_links(markdown, DIRECTORY["url"], FIXTURE["context"]["audience"]) == [HOMES[2], HOMES[1]]
    links = "\n".join(f"[Shop {index}](https://shop{index}.co.uk/)" for index in range(10))
    assert enrich.directory_links(links + "\n" + markdown, DIRECTORY["url"], FIXTURE["context"]["audience"])[0] == HOMES[2]


def test_consumer_directory_never_mined(journey_db, monkeypatch):
    from app.gtm import journey, reachable
    context = FIXTURE["context"] | {"mode": "B2C"}
    journey.update_icp(journey_db["wid"], journey_db["vid"], context["audience"], problem=context["problem"])
    payload = _leased_payload(journey_db)
    monkeypatch.setattr("app.gtm.scrapy_leads._fetch", lambda *args, **kwargs: pytest.fail("consumer directory fetched"))
    directory = DIRECTORY | {"title": "Physiotherapy directory"}
    assert reachable.candidate_hits([directory], context) == []
    assert asyncio.run(reachable.discover(reachable.PublicPages(), [DIRECTORY], [], context, payload, fallback=True)) == []
    assert "directory_pages" not in journey._journal(payload).document(payload["journey_id"])["_runtime"]["calls_used"]


def test_seed_rejection_diagnostics_include_business_domains():
    from app.gtm import reachable
    payload = {}
    hit = {"url": "https://complete-physio.example/", "title": "Complete Physiotherapy", "kind": "business"}
    assert reachable.candidate_hits([hit], FIXTURE["context"], payload) == []
    assert payload["_trace_drops"] == {"directory_or_article:complete-physio.example": 1}


def test_trace_drops_are_sanitized_and_log_top_25(journey_db, monkeypatch, caplog):
    from app.gtm import journey, reachable
    view = journey.update_icp(journey_db["wid"], journey_db["vid"], FIXTURE["context"]["audience"], problem=FIXTURE["context"]["problem"])
    async def discover(*args, payload, **kwargs):
        for index in range(30):
            reachable.trace(payload, f"https://www.clinic{index}.example/private?email=secret@example.com", "dropped",
                "low_fit: secret@example.com https://private.example")
        for _ in range(3):
            reachable.trace(payload, "https://www.clinic29.example/path", "rejected", "anti_icp")
        reachable.trace(payload, "https://clinic0.example/", "candidate", "directory_mined")
        reachable.trace(payload, "https://clinic0.example/", "draft_rejected", "wrong_recipient")
        return []
    monkeypatch.setattr(journey, "_discover", discover)
    caplog.set_level("INFO", logger=journey.__name__)
    _work(journey_db, view)
    record = next(record for record in caplog.records if record.message == "gtm_journey_discovery")
    assert len(record.trace_drops) == 25
    assert record.trace_drops["anti_icp:clinic29.example"] == 3
    assert all(re.fullmatch(r"[a-z_]+:[a-z0-9.-]+", key) for key in record.trace_drops)
    assert "private" not in str(record.trace_drops) and "secret" not in str(record.trace_drops)
    assert "directory_mined:clinic0.example" not in record.trace_drops

