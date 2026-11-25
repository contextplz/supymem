import asyncio
import re
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select

from tests.unit.gtm.test_linkedin_people import _app as _app, journey_db as journey_db, linkedin as linkedin


@pytest.mark.parametrize("audience", ["Industrial purchasing coordinators", "Families growing balcony herbs"])
def test_local_profile_search_uses_keyless_provider(_app, monkeypatch, audience):
    from app.gtm import exa_people
    from app.research import local_providers

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    calls = []

    async def search(query, *, limit):
        assert limit == 25
        calls.append(query)
        return [{"url": "https://www.linkedin.com/in/morgan-hale", "title": "Morgan Hale", "snippet": audience}]

    monkeypatch.setattr(local_providers, "local_search", search, raising=False)
    body = asyncio.run(exa_people._http(audience))
    assert calls == ["site:linkedin.com/in/ " + audience]
    assert body["results"][0]["text"] == audience


@pytest.fixture()
def exa(linkedin, monkeypatch):
    from app.credits.accounts import ensure_account
    from app.db.models import CreditAccount
    from app.gtm import exa_people
    from app.tenancy import bind

    if linkedin["account"] is None:
        account = ensure_account(linkedin["vid"], workspace_id=linkedin["wid"], owner_person_id=linkedin["actor"])
        with linkedin["factory"]() as session:
            session.get(CreditAccount, account.id).granted_micro = 10_000_000
            session.commit()
        linkedin["account"] = account.id
    monkeypatch.setenv("EXA_API_KEY", "offline-exa-key")
    calls = []
    response = {"results": [], "costDollars": {"total": 0.012}}

    async def post(client, url, *, headers, json):
        assert url == "https://api.exa.ai/search"
        assert headers == {"x-api-key": "offline-exa-key"}
        assert json["category"] == "linkedin profile" and json["numResults"] == 25
        assert json["contents"] == {"text": {"maxCharacters": 600}}
        calls.append(json)
        return httpx.Response(200, json=response, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    with bind(workspace_id=linkedin["wid"], venture_id="", actor=linkedin["actor"]):
        yield {**linkedin, "exa": exa_people, "http_calls": calls, "response": response}


def _run(data):
    from app.core.jobs import complete_job, lease
    from app.worker import handlers
    from app.worker.catalog import enabled_kinds
    assert data["exa"].KIND in enabled_kinds()
    handlers.install_default_handlers()
    with data["factory"]() as session:
        jobs = lease(session, "test-exa-worker", kinds=[data["exa"].KIND], n=1)
        session.commit()
    assert len(jobs) == 1
    job = jobs[0]
    payload = {**job.payload, "_job_id": job.id, "_worker_id": job.worker_id, "_job_attempt": job.attempts}
    asyncio.run(handlers.dispatch(job.kind, payload))
    asyncio.run(handlers.dispatch(job.kind, payload))
    with data["factory"]() as session:
        complete_job(session, job.id, worker_id=job.worker_id, attempt=job.attempts)
        session.commit()


def _jobs(data):
    from app.db.models import Job
    with data["factory"]() as session:
        return session.scalars(select(Job).where(Job.kind == "gtm.linkedin.search")).all()


@pytest.mark.parametrize("audience", ["Industrial purchasing coordinators", "Families growing balcony herbs"])
def test_local_profile_reads_do_not_start_another_search(exa, monkeypatch, audience):
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setattr(exa["exa"], "_available", lambda: True)
    with exa["factory"]() as session:
        row, data = exa["mod"]._doc(session, exa["vid"], create=True, lock=True)
        data.update(exa_active=True, requested_at=None, order={"query": audience})
        exa["mod"]._save(row, data)
        session.commit()
    exa["exa"].queue(exa["vid"], exa["actor"])
    assert not _jobs(exa)


@pytest.fixture()
def unrelated_ventures(exa, monkeypatch):
    from app.core.local_mode import is_local
    from app.credits.accounts import ensure_account
    from app.db.models import CreditAccount, Venture
    now = exa["mod"].utcnow()
    domains = [
        {"vid": exa["vid"], "name": "Stock Ledger", "idea": "Inventory alerts for dental practices", "audience": "dental practice managers in Bristol",
         "person": "Avery Chen", "role": "Dental Practice Manager", "location": "Bristol", "company": "Harbor Dental",
         "about": "She manages supplies and purchasing for a dental practice.", "count": 22},
        {"vid": "ven_trail_club", "name": "Trail Circle", "idea": "A consumer app for weekend trail runners", "audience": "trail runners in Denver",
         "person": "Jordan Rivera", "role": "Trail Runner", "location": "Denver", "company": "",
         "about": "They organize weekend trail runs and share routes with fellow runners.", "count": 3},
    ]
    with exa["factory"]() as session:
        venture = session.get(Venture, exa["vid"])
        venture.name, venture.raw_idea = domains[0]["name"], domains[0]["idea"]
        session.add(Venture(id=domains[1]["vid"], workspace_id=exa["wid"], name=domains[1]["name"],
                            raw_idea=domains[1]["idea"], stage="draft", created_at=now, updated_at=now))
        session.commit()
    if not is_local():
        account = ensure_account(domains[1]["vid"], workspace_id=exa["wid"], owner_person_id=exa["actor"])
        with exa["factory"]() as session:
            session.get(CreditAccount, account.id).granted_micro = 10_000_000
            session.commit()
    audiences = {domain["vid"]: ({"who": domain["audience"]}, domain["idea"]) for domain in domains}
    monkeypatch.setattr(exa["mod"], "_venture_icp", lambda vid: audiences[vid])
    return domains


def _profile(domain, index=0):
    slug = domain["person"].lower().replace(" ", "-")
    return {"url": f"https://www.linkedin.com/in/{slug}-{index}", "author": domain["person"].lower(),
            "title": f"# {domain['person']} | LinkedIn", "location": domain["location"], "company": domain["company"],
            "text": f"# {domain['person']}\n## {domain['person']}\n**{domain['role']}**\nLocation: {domain['location']}\n"
                    f"## About\n{domain['about']}\nThey enjoy **helping** [their community](https://example.test/community). "
                    + "Additional background that should not appear in the card. " * 8}


@pytest.mark.hosted_only
def test_request_queues_exa_filters_normalizes_dedupes_and_meters(exa):
    from app.db.models import CreditAccount
    data, mod = exa, exa["mod"]
    with data["factory"]() as session:
        row, stored = mod._doc(session, data["vid"], create=True)
        stored["people"] = [{"id": "existing", "url": "https://in.linkedin.com/in/known/?trk=old", "found_on": "2026-01-01"}]
        mod._save(row, stored)
        session.commit()
    data["response"]["results"] = [
        {"url": "https://linkedin.com/in/Ada-Lovelace/?trk=public", "title": "Ada Lovelace - Owner - Iron Gym | LinkedIn", "text": "Owner in Hyderabad."},
        {"url": "https://in.linkedin.com/in/ada-lovelace", "title": "Duplicate"},
        {"url": "https://www.linkedin.com/in/KNOWN", "title": "Already stored"},
        {"url": "https://uk.linkedin.com/in/grace-hopper/details/experience#about", "author": "Grace Hopper", "text": "Gym Founder\nHyderabad", "company": "Fit Gym"},
        *[{"url": url, "title": "Not a profile"} for url in [
            "https://www.linkedin.com/company/iron-gym", "https://www.linkedin.com/posts/ada", "https://www.linkedin.com/pub/ada",
            "https://example.com/in/ada", "https://evil.linkedin.com/in/ada", "https://linkedin.com.evil/in/ada",
            "https://[broken/in/ada", "javascript:alert(1)", "ftp://linkedin.com/in/ada", "https://www.google.com/url?q=https://linkedin.com/in/unwrapped"]],
        None, {"url": 123},
    ]
    queued = mod.request(data["vid"], data["actor"])
    assert queued["searching"] and queued["queued_at"] and queued["notice"] is None
    assert data["http_calls"] == [] and len(_jobs(data)) == 1
    assert mod.next_order(data["caller"]) == {"order": None}
    mod.view(data["vid"], data["actor"])
    assert len(_jobs(data)) == 1
    _run(data)
    with data["factory"]() as session:
        _, stored = mod._doc(session, data["vid"])
        added = stored["people"][1:]
        assert [person["url"] for person in added] == ["https://www.linkedin.com/in/Ada-Lovelace", "https://www.linkedin.com/in/grace-hopper"]
        assert added[0]["name"] == "Ada Lovelace" and added[0]["headline"] == "Owner" and added[0]["company"] == "Iron Gym"
        assert added[1]["name"] == "Grace Hopper" and added[1]["role"] == "Gym Founder" and added[1]["company"] == "Fit Gym"
        assert all(person["found_at"] and person["found_on"] == mod._today() and person["snippet"] for person in added)
        assert stored["order"]["costDollars"] == {"total": 0.012}
        assert session.get(CreditAccount, data["account"]).spent_micro == 12_000
    assert "gym owner" in data["http_calls"][0]["query"] and "Hyderabad" in data["http_calls"][0]["query"]
    assert all(person["messages"] for person in mod.view(data["vid"], data["actor"])["people"])


def test_polls_top_up_with_varied_queries_and_cap_all_requests_per_day(exa, monkeypatch):
    data, mod = exa, exa["mod"]
    assert mod.view(data["vid"], data["actor"])["searching"]
    assert len(_jobs(data)) == 1
    mod.chat_reply(data["vid"])
    for attempt in range(3):
        _run(data)
        mod.view(data["vid"], data["actor"])
    for repeat in range(4):
        assert not mod.request(data["vid"], data["actor"])["searching"]
    assert len(data["http_calls"]) == len(_jobs(data)) == 3
    assert len({call["query"] for call in data["http_calls"]}) == 3
    tomorrow = mod.utcnow() + timedelta(days=1)
    monkeypatch.setattr(mod, "utcnow", lambda: tomorrow)
    monkeypatch.setattr(data["exa"], "utcnow", lambda: tomorrow)
    mod.view(data["vid"], data["actor"])
    _run(data)
    assert len(data["http_calls"]) == 4


def test_target_counts_only_today_for_each_venture_and_caps_progress(exa, unrelated_ventures):
    data, mod = exa, exa["mod"]
    yesterday = (mod.utcnow() - timedelta(days=1)).date().isoformat()
    for domain in unrelated_ventures:
        with data["factory"]() as session:
            row, stored = mod._doc(session, domain["vid"], create=True)
            stored["people"] = [{"id": "yesterday", "url": "https://www.linkedin.com/in/older-person", "found_on": yesterday}]
            mod._save(row, stored)
            session.commit()
        data["response"]["results"] = [_profile(domain, index) for index in range(domain["count"])]
        mod.request(domain["vid"], data["actor"])
        _run(data)
    for domain in unrelated_ventures:
        result = mod.view(domain["vid"], data["actor"])
        assert result["today"] == {"new": min(domain["count"], mod.DAILY_TARGET), "target": mod.DAILY_TARGET}
        with data["factory"]() as session:
            assert mod.new_today(mod._doc(session, domain["vid"])[1]) == domain["count"]
        assert len(result["people"]) == domain["count"] + 1
        assert result["searching"] == (domain["count"] < mod.DAILY_TARGET)
    _run(data)
    data["response"]["results"] = [_profile(unrelated_ventures[0], index) for index in range(22)]
    mod.request(data["vid"], data["actor"])
    _run(data)
    assert mod.view(data["vid"], data["actor"])["today"]["new"] == mod.DAILY_TARGET
    assert len(data["http_calls"]) == 4


def test_profile_cards_use_title_names_clean_metadata_and_bounded_fit_summaries(exa, unrelated_ventures):
    data, mod = exa, exa["mod"]
    for domain in unrelated_ventures:
        data["response"]["results"] = [_profile(domain)]
        mod.request(domain["vid"], data["actor"])
        _run(data)
        with data["factory"]() as session:
            row, stored = mod._doc(session, domain["vid"])
            person = stored["people"][0]
            assert person["name"] == domain["person"]
            assert person["headline"] == person["role"] == domain["role"]
            assert person["location"] == domain["location"]
            assert domain["about"] in person["why"]
            assert "your search" in person["why"] and 0 < len(person["why"]) <= 220
            assert person["why"].endswith(".") and len(re.split(r"(?<=[.!?])\s+", person["why"])) <= 2
            assert not re.search(r"[#*_`\[\]]|https?://|\s{2,}|—", person["why"] + person["snippet"])
            assert "Additional background" not in person["why"]
            assert mod.clean_person(person) == person
            person.update(name=domain["person"].lower(), headline=f"# {domain['person']}",
                          snippet=_profile(domain)["text"], why="Exa profile result for your customer search.")
            mod._save(row, stored)
            session.commit()
        result = mod.view(domain["vid"], data["actor"])["people"][0]
        assert result["name"] == domain["person"] and result["headline"] == domain["role"]
        assert result["why"] == mod.clean_person(result)["why"] and domain["about"] in result["why"]
        assert result["messages"] and result["sent_at"] is None and result["replied_at"] is None
        _run(data)
    for snippet in ("# Someone Proper\n**A role**\n## About\n" + "Long background " * 30,
                    "## About\n**" + "x" * 250 + "**"):
        result = mod.clean_person({"url": "https://www.linkedin.com/in/someone-proper", "title": "Someone Proper",
                                   "snippet": snippet, "source": "exa"})
        assert 0 < len(result["why"]) <= 220 and result["why"].endswith("…")
    for author in ("someone-proper", "someone proper"):
        result = mod.clean_person({"url": "https://www.linkedin.com/in/someone-proper-42", "name": author,
                                   "headline": "Community Organizer", "snippet": "They coordinate local events.", "source": "exa"})
        assert result["name"] == "LinkedIn member"


def test_company_and_post_results_are_never_saved_listed_or_counted(exa, unrelated_ventures):
    data, mod = exa, exa["mod"]
    for domain in unrelated_ventures:
        invalid = ["https://www.linkedin.com/company/a-business", "https://www.linkedin.com/posts/an-update",
                   "https://www.linkedin.com/feed/update/a-post", "https://www.linkedin.com/in/a-person/posts/a-post",
                   "https://[broken/in/a-person", "https://www.linkedin.com/in/a-person/details/experience/posts/a-post"]
        with data["factory"]() as session:
            row, stored = mod._doc(session, domain["vid"], create=True)
            stored["people"] = [{"id": f"invalid-{index}", "url": url, "found_on": mod._today()} for index, url in enumerate(invalid)]
            mod._save(row, stored)
            session.commit()
        data["response"]["results"] = [_profile(domain), *[{"url": url, "title": domain["person"]} for url in invalid]]
        mod.request(domain["vid"], data["actor"])
        _run(data)
        with data["factory"]() as session:
            assert mod._doc(session, domain["vid"])[1]["order"]["found"] == 1
        result = mod.view(domain["vid"], data["actor"])
        assert result["today"]["new"] == len(result["people"]) == 1
        assert result["people"][0]["name"] == domain["person"]
        with data["factory"]() as session:
            assert len(mod._doc(session, domain["vid"])[1]["people"]) == len(invalid) + 1
        _run(data)


def test_without_key_request_and_chat_keep_mac_path(linkedin, monkeypatch):
    from app.gtm import exa_people
    data, mod = linkedin, linkedin["mod"]
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    monkeypatch.setattr(exa_people, "_http", lambda query: pytest.fail("Exa must not run without its key"))
    queued = mod.request(data["vid"], data["actor"])
    assert queued["notice"] == mod.NO_MAC and queued["searching"]
    assert _jobs(data) == []
    assert "Mac" in mod.chat_reply(data["vid"])["body_md"]
    assert mod.next_order(data["caller"])["order"]["venture_id"] == data["vid"]
