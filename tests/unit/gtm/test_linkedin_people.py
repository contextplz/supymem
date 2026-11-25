import asyncio
import json
from types import SimpleNamespace

import pytest

from tests.company_os_harness import load_company_os_app
from tests.unit.gtm.test_journey import journey_db  # noqa: F401
from tests.unit.test_local_mode import _owner_client


@pytest.fixture(autouse=True)
def _app():
    load_company_os_app()


def _result(slug, title, snippet, href=None):
    href = href or f"https://www.linkedin.com/in/{slug}"
    return (f'<div class="g"><a href="{href}"><h3>{title}</h3><cite>linkedin.com › in › {slug}</cite></a>'
            f'<div class="VwiC3b"><span>{snippet}</span></div></div>')


@pytest.fixture
def local_journey_db(request, monkeypatch):
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    return request.getfixturevalue("journey_db")


@pytest.mark.parametrize("audience,name,role", [
    ("Warehouse supervisors", "Avery Chen", "Warehouse supervisor"),
    ("Weekend trail runners", "Jordan Rivera", "Trail runner"),
])
def test_local_linkedin_routes_search_only_when_requested(local_journey_db, monkeypatch, audience, name, role):
    from fastapi import FastAPI
    from app.api.linkedin_api import router
    from app.core.local_owner import LocalOwnerMiddleware
    from app.gtm import linkedin_people
    from app.research import local_providers

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setattr(linkedin_people, "_venture_icp", lambda vid: ({"who": audience}, "A place to compare notes"))
    monkeypatch.setattr(linkedin_people, "view", lambda *args: pytest.fail("Local reads must not enqueue hosted search"))
    searches = []
    async def search(provider, query, **kwargs):
        assert provider == "saved-provider"
        searches.append(query)
        return [{"url": "https://www.linkedin.com/in/public-person", "title": f"{name} - {role} | LinkedIn",
            "snippet": f"I am a {role} and share practical advice."},
            {"url": "https://www.linkedin.com/company/not-a-person", "title": "Not a profile"}]
    def provider(**kwargs):
        assert kwargs["limit"] == linkedin_people.DAILY_TARGET
        return "saved-provider"
    monkeypatch.setattr(local_providers, "search_provider", provider)
    monkeypatch.setattr(local_providers, "search", search)
    app = FastAPI()
    app.add_middleware(LocalOwnerMiddleware)
    app.include_router(router)
    path = f"/ventures/{local_journey_db['vid']}/linkedin"
    with _owner_client(app, headers={"X-Subject": local_journey_db["actor"]}) as client:
        response = client.get(path)
        assert response.status_code == 200 and searches == []
        assert response.json()["local_mode"] is True and response.json()["people"] == []
        response = client.post(path + "/find")
        assert response.status_code == 200 and len(searches) == 1
        assert audience in searches[0] and "site:linkedin.com/in" in searches[0]
        person = response.json()["people"][0]
        assert person["name"] == name and person["messages"] and person["source_url"] == person["url"]
        assert len(response.json()["people"]) == 1
        marked = client.patch(path + f"/people/{person['id']}", json={"sent": True, "replied": True})
        assert marked.status_code == 200 and marked.json()["people"][0]["sent_at"]
        assert marked.json()["people"][0]["replied_at"] and len(searches) == 1
        assert client.get(path, headers={"X-Subject": "other_owner"}).status_code == 200
        assert client.patch(path + "/people/missing", json={"sent": True}).status_code == 404


@pytest.mark.parametrize("question", ["warehouse dispatch software", "weekend hiking groups"])
def test_local_search_default_limit_reaches_the_saved_provider(monkeypatch, question):
    from app.research import local_providers

    limits = []
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setattr(local_providers, "search_provider", lambda **kwargs: limits.append(kwargs["limit"]) or "saved-provider")

    async def search(provider, query, **kwargs):
        assert provider == "saved-provider" and query == question
        return [{"title": query, "url": "https://public.example/guide"}]

    monkeypatch.setattr(local_providers, "search", search)
    assert asyncio.run(local_providers.local_search(question))[0]["title"] == question
    assert limits == [6]


GOOGLE = ("<html><head><script>var x='<a href=\"https://www.linkedin.com/in/script-only\">';</script></head><body>"
          + _result("ravi-kumar-123", "Ravi Kumar - Owner - Iron Gym | LinkedIn", "I run Iron Gym in Hyderabad and coach members every morning.")
          + _result("x", "Priya Shah - Gym Founder | LinkedIn", "Founder of FitNation gyms across Hyderabad since 2015.",
                    href="/url?q=https://in.linkedin.com/in/priya-shah%3ForiginalSubdomain%3Din&sa=U&ved=2ah")
          + _result("ravi-kumar-123", "Ravi Kumar again | LinkedIn", "duplicate", href="https://www.linkedin.com/in/ravi-kumar-123?trk=public_profile")
          + '<a href="https://www.linkedin.com/company/iron-gym"><h3>Iron Gym | LinkedIn</h3></a>'
          + '<a href="https://www.linkedin.com/posts/ravi_activity-1"><h3>A post</h3></a>'
          + '<a href="https://www.linkedin.com/pub/dir/Ravi/Kumar"><h3>Directory</h3></a>'
          + '<a href="https://example.com/in/someone"><h3>Not LinkedIn</h3></a>'
          + "</body></html>")


def test_extraction_keeps_only_profile_links_without_tracking():
    from app.gtm.linkedin_people import extract, parse_title
    results = extract(GOOGLE)
    assert [r["url"] for r in results] == ["https://www.linkedin.com/in/ravi-kumar-123", "https://www.linkedin.com/in/priya-shah"]
    assert results[0]["title"] == "Ravi Kumar - Owner - Iron Gym | LinkedIn"
    assert "Iron Gym in Hyderabad" in results[0]["snippet"] and "›" not in results[0]["snippet"]
    assert parse_title(results[0]["title"]) == {"name": "Ravi Kumar", "headline": "Owner", "company": "Iron Gym"}
    assert parse_title("Priya Shah - Gym Founder | LinkedIn") == {"name": "Priya Shah", "headline": "Gym Founder", "company": ""}


@pytest.mark.parametrize("audience", ["Industrial procurement teams", "Family cycling groups"])
def test_conn_regression_empty_linkedin_page_is_not_described_as_found(linkedin, monkeypatch, audience):
    from app.chat.ground import yc_quote_for_reply
    from app.knowledge import yc_video_advice
    from app.chat import customers

    for request in ("Find LinkedIn people", "Find people on LinkedIn"):
        assert customers.command(linkedin["vid"], request) is None

    module = linkedin["mod"]
    monkeypatch.setattr(module, "_flag", lambda *args, **kwargs: False)
    monkeypatch.setattr(module, "_fallback", lambda *args: [])
    monkeypatch.setattr(module, "_last_search", lambda *args: None)
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    reply = module.chat_reply(linkedin["vid"])
    assert "no saved profiles" in reply["body_md"].lower()
    assert "already found" not in reply["body_md"] and reply["actions"]
    monkeypatch.setattr(module, "_fallback", lambda *args: [{"name": audience}])
    present = module.chat_reply(linkedin["vid"])
    assert "no saved profiles" not in present["body_md"].lower()
    assert "saved results" in present["body_md"]
    monkeypatch.setattr(yc_video_advice, "quote", lambda *args, **kwargs: {"text": "Unrelated advice"})
    assert yc_quote_for_reply(f"Find LinkedIn people for {audience}", [], reply=reply["body_md"]) is None


def test_captcha_page_is_detected():
    from app.gtm.linkedin_people import captcha
    assert captcha("https://www.google.com/sorry/index?continue=x", "")
    assert captcha("https://www.google.com/search?q=x", '<form id="captcha-form">')
    assert not captcha("https://www.google.com/search?q=x", GOOGLE)


def test_queries_come_from_icp_and_location():
    from app.gtm.linkedin_people import build_queries, google_url
    queries = build_queries({"who": "Independent gym owners in Hyderabad", "problem": "members cancel memberships"}, "A retention app for gyms")
    assert 3 <= len(queries) <= 5 and all(q.startswith("site:linkedin.com/in ") for q in queries)
    assert queries[0] == 'site:linkedin.com/in "gym owner" "Hyderabad"'
    assert google_url(queries[0], 1).startswith("https://www.google.com/search?q=site%3Alinkedin.com%2Fin+") and google_url(queries[0], 1).endswith("&start=10")


def test_connection_notes_fit_300_characters():
    from app.gtm import playbooks
    from app.gtm.linkedin_people import _messages
    person = {"name": "Maximiliano Fernandez-Castellanos", "company": "A Very Long Company Name Holdings International",
              "snippet": "I run three gyms and I have been coaching members for twenty years in the city."}
    chosen = [{"key": key} for key in playbooks.PLAYBOOKS]
    messages = _messages(person, chosen, "A " + "really " * 90 + "long idea for gyms.")
    assert len(messages) == len(playbooks.PLAYBOOKS)
    for message in messages:
        assert len(message["connection_note"]) <= 300 and message["linkedin_dm"].startswith("Hi Maximiliano,")
        assert set(message) == {"playbook", "title", "connection_note", "linkedin_dm"}


def _page(n, start=0):
    return "<html><body>" + "".join(_result(f"gym-owner-{i}", f"Owner {i} - Gym Owner - Gym {i} | LinkedIn", f"Gym owner in Hyderabad number {i}.")
                                   for i in range(start, start + n)) + "</body></html>"


@pytest.fixture()
def linkedin(journey_db, monkeypatch):
    monkeypatch.setenv("SUPYMEM_MODE", "hosted")
    from app.admin.authn import CallerContext
    from app.credits import metered
    from app.gtm import linkedin_people
    monkeypatch.setenv("LINKEDIN_MAC_SEARCH", "on")
    monkeypatch.setattr(linkedin_people, "_venture_icp", lambda vid: ({"who": "gym owners in Hyderabad"}, "A retention app for gyms"))
    calls = []

    async def judge(messages, **kwargs):
        calls.append(kwargs)
        items = json.loads(messages[1]["content"])["items"]
        content = json.dumps({"keep": [{"i": item["i"], "why": "Their title says Gym Owner."} for item in items]})
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])
    monkeypatch.setattr(metered, "llm", judge)
    caller = CallerContext(sub=journey_db["actor"], email="", role="user", path="", method="", exp=0, nonce="", device_id="dev_mac")
    return {**journey_db, "caller": caller, "calls": calls, "mod": linkedin_people}


def _run(d, pages_html, **extra):
    mod = d["mod"]
    order = mod.next_order(d["caller"])["order"]
    assert order and order["venture_id"] == d["vid"]
    pages = [{"url": item["url"], "final_url": item["url"], "html": html} for item, html in zip(order["queries"], pages_html)]
    return asyncio.run(mod.record_results(d["caller"], d["vid"], order["id"], pages, **extra))


def test_daily_top_up_stops_at_ten_and_find_more_adds_ten(linkedin):
    mod, d = linkedin["mod"], linkedin
    assert _run(d, [_page(15)]) == {"found": 10, "status": "done"}
    view = mod.view(d["vid"], d["actor"])
    assert view["today"] == {"new": 10, "target": 10} and view["mac_connected"] and view["notice"] is None
    assert all(p["url"].startswith("https://www.linkedin.com/in/") and p["why"] and p["messages"] for p in view["people"])
    assert mod.next_order(d["caller"]) == {"order": None}  # target met: no automatic search today
    mod.request(d["vid"], d["actor"])
    assert _run(d, [_page(15, start=15)])["found"] == 10
    assert mod.view(d["vid"], d["actor"])["today"]["new"] == 10
    with d["factory"]() as session:
        assert mod.new_today(mod._doc(session, d["vid"])[1]) == 20
    assert len(d["calls"]) == 2 and all(c["tier"] == "flash" for c in d["calls"])


def test_dedupe_across_runs_and_days(linkedin):
    mod, d = linkedin["mod"], linkedin
    assert _run(d, [_page(4)])["found"] == 4
    mod.request(d["vid"], d["actor"])
    assert _run(d, [_page(6), _page(6)])["found"] == 2  # 4 known, duplicates across pages counted once
    urls = [p["url"] for p in mod.view(d["vid"], d["actor"])["people"]]
    assert len(urls) == len(set(urls)) == 6


def test_captcha_returns_needs_human_and_pauses_until_find_more(linkedin):
    mod, d = linkedin["mod"], linkedin
    order = mod.next_order(d["caller"])["order"]
    sorry = [{"url": order["queries"][0]["url"], "final_url": "https://www.google.com/sorry/index", "html": "<div class=g-recaptcha></div>"}]
    assert asyncio.run(mod.record_results(d["caller"], d["vid"], order["id"], sorry)) == {"found": 0, "status": "needs_human"}
    assert "never solves" in mod.view(d["vid"], d["actor"])["needs_human"]
    assert mod.next_order(d["caller"]) == {"order": None}
    mod.request(d["vid"], d["actor"])
    assert mod.next_order(d["caller"])["order"]


def test_without_a_mac_the_page_explains_and_lists_search_links(linkedin):
    from app.gtm import journey
    from app.state import db
    mod, d = linkedin["mod"], linkedin
    with db()() as session:
        row = journey._row(session, d["vid"], create=True)
        row.provenance_json = json.dumps({"reachable_leads": [{"people": [{"name": "Jane Doe", "role": "Founder", "company": "Acme",
            "source_url": "https://acme.example/team", "linkedin_search": "https://www.linkedin.com/search/results/people/?keywords=Jane%20Doe%20Acme"}]}]})
        session.commit()
    view = mod.view(d["vid"], d["actor"])
    assert view["mac_connected"] is False and view["notice"] == mod.NO_MAC
    assert view["fallback"][0]["linkedin_search"].startswith("https://www.linkedin.com/search/results/people/")


def test_only_an_authenticated_device_gets_orders(linkedin):
    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        linkedin["mod"].next_order(linkedin["caller"].__class__(sub=linkedin["actor"], email="", role="user", path="", method="", exp=0, nonce=""))


def test_chat_phrase_routes_to_linkedin_people():
    from app.chat.intent import classify_intent, linkedin_people_request
    for text in ("find LinkedIn people", "Find me more linkedin profiles today", "can you find people on LinkedIn?"):
        assert linkedin_people_request(text) and classify_intent(text) == "gtm_review"
    assert not linkedin_people_request("post this to LinkedIn")


def test_linkedin_denylists_untouched():
    from app.gtm import lead_pipeline
    from app.gtm.relationships.denylist import linkedin_host_blocked
    from app.research import deep
    assert linkedin_host_blocked("https://www.linkedin.com/in/ravi") and linkedin_host_blocked("in.linkedin.com/in/x")
    assert "linkedin.com" in deep._NO_SCRAPE_HOSTS and "linkedin.com" in lead_pipeline._BLOCKED_HOSTS


def test_mac_never_loads_linkedin_and_stays_on_google(monkeypatch):
    from desktop_runtime import linkedin_search as mac
    assert mac.blocked("https://www.linkedin.com/in/ravi", navigation=False)
    assert mac.blocked("https://static.licdn.com/x.js", navigation=False)
    assert mac.blocked("https://example.com/", navigation=True) and not mac.blocked("https://www.gstatic.com/x.js", navigation=False)
    assert mac.google_search_url("https://www.google.com/search?q=site%3Alinkedin.com%2Fin+x")
    assert not mac.google_search_url("https://www.linkedin.com/search?q=x")

    class Page:
        url = ""
        def goto(self, url, **_): self.url = url if "q=b" not in url else "https://www.google.com/sorry/index"
        def evaluate(self, _): return "<html></html>"
        def close(self): pass

    class Context:
        def __init__(self): self.routes = []
        def route(self, pattern, handler): self.routes.append(pattern)
        def new_page(self): return Page()
        def __enter__(self): return self
        def __exit__(self, *_): pass

    monkeypatch.setattr(mac.random, "uniform", lambda a, b: 0)
    order = {"queries": [{"url": "https://www.google.com/search?q=a"}, {"url": "https://evil.example/search?q=x"},
                         {"url": "https://www.google.com/search?q=b"}, {"url": "https://www.google.com/search?q=c"}]}
    body = mac.run_order(order, "usr_a", open_browser=lambda _: Context())
    assert body["captcha"] and [p["url"] for p in body["pages"]] == ["https://www.google.com/search?q=a", "https://www.google.com/search?q=b"]


def test_fit_people_beyond_the_daily_ten_wait_for_the_next_top_up(linkedin):
    mod, d = linkedin["mod"], linkedin
    assert _run(d, [_page(14)])["found"] == 10
    mod.request(d["vid"], d["actor"])
    assert _run(d, ["<html></html>"])["found"] == 4  # no new results, the 4 extra fit people come from the backlog
    assert len({p["url"] for p in mod.view(d["vid"], d["actor"])["people"]}) == 14


def test_mac_search_kill_switch_and_headless_marker(linkedin, monkeypatch):
    """Founder rule: research is invisible. Old Mac builds (visible window) never get work; a kill switch stops all."""
    from types import SimpleNamespace
    from app.api import linkedin_api

    monkeypatch.setenv("LINKEDIN_MAC_SEARCH", "off")
    assert linkedin["mod"].next_order(linkedin["caller"]) == {"order": None}
    monkeypatch.setattr(linkedin_api, "verify_caller", lambda request, required=True: linkedin["caller"])
    monkeypatch.setattr(linkedin_api.people, "next_order", lambda caller: {"order": {"id": "x"}})
    assert linkedin_api.next_search(SimpleNamespace(query_params={})) == {"order": None}
    assert linkedin_api.next_search(SimpleNamespace(query_params={"headless": "1"})) == {"order": {"id": "x"}}


def test_mac_checks_in_right_after_launch_and_every_minute():
    import threading
    from desktop_runtime import linkedin_search as mac
    assert mac.POLL_SECONDS <= 60
    called = threading.Event()

    class Response:
        is_success = True
        def json(self): return {"order": None}

    def request(method, path, body):
        called.set()
        return Response()
    mac.start(request, lambda: "usr_a", interval=3600)
    assert called.wait(2), "the first check-in must not wait for the poll interval"


def test_first_check_in_marks_the_mac_seen_and_find_more_shows_when_it_was_queued(linkedin):
    mod, d = linkedin["mod"], linkedin
    assert mod.view(d["vid"], d["actor"])["notice"] == mod.NO_MAC
    _run(d, [_page(15)])  # the Mac's first check-in after launch
    queued = mod.request(d["vid"], d["actor"])
    assert queued["mac_connected"] and queued["notice"] is None and queued["searching"] and queued["queued_at"]


def test_messy_audience_becomes_role_queries_and_an_empty_search_says_why(linkedin, monkeypatch, caplog):
    import logging
    from app.credits import metered
    mod, d = linkedin["mod"], linkedin
    messy = {"who": "her customers who order cakes on whatsapp, its messy"}
    assert mod.clean_role(messy) == ""
    assert not any("her customer" in q for q in mod.build_queries(messy, "cake orders"))
    monkeypatch.setattr(mod, "_venture_icp", lambda vid: (messy, "something for my mom's bakery orders on whatsapp idk"))

    async def roles(messages, **kwargs):
        content = json.dumps({"roles": ["Home Baker", "Bakery Owner", "Cake Shop Owner"], "city": "Bengaluru"})
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])
    monkeypatch.setattr(metered, "llm", roles)
    caplog.set_level(logging.INFO, logger=mod.log.name)
    order = mod.next_order(d["caller"])["order"]
    assert [q["query"] for q in order["queries"]][:3] == ['site:linkedin.com/in "home baker" "Bengaluru"',
        'site:linkedin.com/in "bakery owner" "Bengaluru"', 'site:linkedin.com/in "cake shop owner" "Bengaluru"']
    pages = [{"url": q["url"], "final_url": q["url"], "html": "<html></html>"} for q in order["queries"]]
    assert asyncio.run(mod.record_results(d["caller"], d["vid"], order["id"], pages))["found"] == 0
    assert mod.view(d["vid"], d["actor"])["last_search"]["message"] == "No profiles matched 'home baker Bengaluru'. Try a different role or city?"
    assert mod.chat_reply(d["vid"])["body_md"].startswith("No profiles matched 'home baker Bengaluru'.")
    assert {"linkedin_order_queries", "linkedin_results"} <= {r.getMessage() for r in caplog.records}


def test_mac_search_browser_is_headless_on_the_persistent_profile(monkeypatch, tmp_path):
    from desktop_runtime import cu_ai_browser, linkedin_search as mac
    from desktop_runtime.cu_browser import ChromeSession
    seen = {}

    class Factory:
        def __init__(self, data_dir, **kwargs): seen.update(kwargs, data_dir=data_dir)
    monkeypatch.setattr(cu_ai_browser, "ChromeFactory", Factory)
    monkeypatch.setenv("SUPYMEM_DATA_DIR", str(tmp_path))
    mac._browser("usr_a")
    assert seen["headless"] is True and seen["account_id"] == "usr_a" and seen["data_dir"] == tmp_path / "browser"
    args = ChromeSession(tmp_path / "profile", headless=True, port=9222).args()
    assert "--headless=new" in args and "--start-minimized" not in args

    def busy(order, account_id):
        raise RuntimeError("browser_profile_in_use")
    monkeypatch.setattr(mac, "run_order", busy)

    class Response:
        is_success = True
        def json(self): return {"order": {"id": "o", "venture_id": "v", "queries": []}}
    posted = []
    assert mac.poll_once(lambda method, path, body: posted.append(method) or Response(), "usr_a") == "busy"
    assert posted == ["GET"]


def test_captcha_is_reported_in_chat_without_showing_a_browser(linkedin):
    from app.chat.store import list_messages
    mod, d = linkedin["mod"], linkedin
    order = mod.next_order(d["caller"])["order"]
    sorry = [{"url": order["queries"][0]["url"], "final_url": "https://www.google.com/sorry/index", "html": ""}]
    asyncio.run(mod.record_results(d["caller"], d["vid"], order["id"], sorry))
    assert any(m["content"].startswith("Google asked for a check.") for m in list_messages(d["vid"], limit=5))
