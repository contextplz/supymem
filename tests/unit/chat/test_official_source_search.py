import asyncio
import pytest

from tests.company_os_harness import load_company_os_app


@pytest.mark.parametrize("audience,name,slug", [
    ("commercial greenhouse purchasing directors", "Rina Patel", "rina-purchasing"),
    ("amateur ceramic artists sharing home studios", "Evan Brooks", "evan-ceramics"),
])
def test_public_profile_search_returns_people_not_a_general_summary(monkeypatch, audience, name, slug):
    load_company_os_app()
    from app.chat import customers, execute

    query = customers.empty_search_reply({"icp": {"who": audience}})["actions"][1]["payload"]["query"]
    profile = f"https://www.linkedin.com/in/{slug}"
    hits = [{"title": f"{name}\u2014{audience}", "url": profile, "snippet": audience},
            {"title": "A directory vendor", "url": "https://directory.example/contacts", "snippet": audience},
            {"title": "An organization", "url": "https://www.linkedin.com/company/a-directory"}]
    calls = []

    async def explore(search, **kwargs):
        calls.append((search, kwargs))
        return {"mode": "live" if hits else "no_hits", "hits": hits, "summary": "Purchase a contact database."}

    async def no_summary(*args, **kwargs):
        raise AssertionError("Public people searches must not generate a general summary")

    monkeypatch.setattr("app.research.explore.explore_web", explore)
    monkeypatch.setattr(execute, "_answer_from_search", no_summary)
    for public_only in (True, False):
        reply = asyncio.run(execute._openworker_search(query, public_only=public_only))
        assert name in reply["body_md"] and profile in reply["body_md"]
        assert audience in reply["body_md"]
        assert reply["citations"] == [{"label": f"{name}, {audience}", "href": profile}]
        assert "directory.example" not in reply["body_md"] and "/company/" not in reply["body_md"]
        assert "purchase" not in reply["body_md"].lower() and "credit" not in reply["body_md"].lower()
        assert not reply["actions"] and "\u2014" not in str(reply)
    hits[:] = hits[1:]
    for remaining in (hits, []):
        hits[:] = remaining
        reply = asyncio.run(execute._openworker_search(query, public_only=True))
        assert "no public LinkedIn profiles" in reply["body_md"]
        assert audience in reply["body_md"] and "You can" in reply["body_md"]
        assert reply["citations"] == [] and "purchase" not in reply["body_md"].lower()
    assert all(search == query and kwargs == {"max_results": 5, "extract": 0, "public_only": True}
               for search, kwargs in calls)


def test_two_named_official_sources_are_searched_separately_without_scraper_claims(monkeypatch):
    load_company_os_app()
    from app.chat.execute import _openworker_search

    queries = []

    async def explore(query, **_kwargs):
        queries.append(query)
        if query.startswith("Greenhouse"):
            return {"mode": "live", "hits": [
                {"title": "Scraper", "url": "https://apify.com/greenhouse-jobs"},
                {"title": "Greenhouse Job Board API", "url": "https://docs.greenhouse.io/job-board.html"},
            ]}
        return {"mode": "live", "hits": [
            {"title": "Guide", "url": "https://example.org/lever-guide"},
            {"title": "Lever Postings API", "url": "https://github.com/lever/postings-api"},
        ]}

    monkeypatch.setattr("app.research.explore.explore_web", explore)
    message = ("Research official Greenhouse and Lever job posting API terms for LabHire today. "
               "Give direct source links and say what remains unverified; do not scrape jobs.")
    result = asyncio.run(_openworker_search(message))

    assert queries == [
        "Greenhouse job posting API terms official documentation",
        "Lever job posting API terms official documentation",
    ]
    assert [item["href"] for item in result["citations"]] == [
        "https://docs.greenhouse.io/job-board.html",
        "https://github.com/lever/postings-api",
    ]
    assert "apify.com" not in result["body_md"]
    assert "Still unverified" in result["body_md"]
    assert "No postings were scraped" in result["body_md"]


def test_official_source_search_reports_missing_provider_without_promoting_a_guide(monkeypatch):
    load_company_os_app()
    from app.chat.execute import _openworker_search

    async def explore(query, **_kwargs):
        if query.startswith("Acme"):
            return {"mode": "live", "hits": [
                {"title": "Acme API", "url": "https://docs.acme.com/api"},
            ]}
        return {"mode": "live", "hits": [
            {"title": "Blog", "url": "https://someblog.example/beta-api"},
        ]}

    monkeypatch.setattr("app.research.explore.explore_web", explore)
    result = asyncio.run(_openworker_search("Research official Acme and Beta API terms for my venture."))
    assert len(result["citations"]) == 1
    assert "No provider-controlled source was found" in result["body_md"]
    assert "someblog.example" not in result["body_md"]



def test_failure_summary_cannot_leak_raw_vendor_text(monkeypatch):
    load_company_os_app()
    from app.chat.execute import _openworker_search
    async def unavailable(*_a, **_kw):
        return {"mode": "error", "summary": "Client error 429 https://api.firecrawl.dev/private", "error_code": "search_rate_limited"}
    monkeypatch.setattr("app.research.explore.explore_web", unavailable)
    reply = asyncio.run(_openworker_search("Find independent personal trainers in Bengaluru for interviews"))
    assert "rate-limited" in reply["body_md"] and "Open Google" in reply["body_md"]
    assert "firecrawl" not in reply["body_md"] and reply["citations"] == []
    assert "independent personal trainer" in reply["body_md"]


def test_manual_no_search_action_never_calls_provider(monkeypatch):
    load_company_os_app()
    from app.chat.execute import _openworker_search
    async def forbidden(*_a, **_kw):
        raise AssertionError("no-search request must not search")
    monkeypatch.setattr("app.research.explore.explore_web", forbidden)
    question = ("The trainer lookup just failed. Do it another way: give me a manual Google-to-Gmail discovery "
                "guide and a short interview request. Do not call web-search tools, invent trainer names or "
                "emails, or claim outreach happened. Choose one city and give the exact query, website checks and Gmail compose steps.")
    reply = asyncio.run(_openworker_search(question))
    assert "Compose" in reply["body_md"] and "Bengaluru" in reply["body_md"] and "Review draft" in reply["body_md"]
    assert "before making any product claims" in reply["body_md"] and reply["citations"] == []


def test_contextual_lead_lookup_query_drops_instructions(monkeypatch):
    load_company_os_app()
    from app.chat.execute import _openworker_search
    queries = []
    async def search(query, **_kw):
        queries.append(query)
        return {"mode": "no_hits"}
    monkeypatch.setattr("app.research.explore.explore_web", search)
    asyncio.run(_openworker_search("Find independent personal trainers in Bengaluru for discovery. Do not contact anyone or invent emails."))
    assert queries == ['"independent personal trainer" "Bengaluru" website contact']


@pytest.mark.parametrize("prohibition", ["Do not search.", "Don't search.", "Don’t search.", "No search tools."])
def test_plain_search_prohibition_keeps_entire_manual_request_and_never_searches(monkeypatch, prohibition):
    load_company_os_app()
    from app.chat.intent import split_speech_acts
    from app.chat.execute import _openworker_search
    question = (prohibition + " I have Gmail in my spare account and Instagram is unavailable. "
                "Guide me step by step to find three independent personal trainers in Bengaluru using Google manually, "
                "verify their own professional website and public business email, and draft honest short interview "
                "requests about their last late cancellation. We have no product or customers. No client screenshots "
                "or private chats. Give the exact clicks and wording; do not send anything automatically.")
    async def forbidden(*_a, **_kw):
        raise AssertionError("explicit prohibition must prevent every provider call")
    monkeypatch.setattr("app.research.explore.explore_web", forbidden)
    assert split_speech_acts(question) == [("assistant", question)]
    reply = asyncio.run(_openworker_search(question))
    assert reply["citations"] == []
    assert "I have not searched" in reply["body_md"]
    assert "Compose" in reply["body_md"] and "Review draft" in reply["body_md"]


@pytest.mark.parametrize("question", [
    "Do not contact anyone; search for independent trainers in Bengaluru",
    "Do not contact anyone and search for independent trainers in Bengaluru",
    "Do not send anything. Search for independent trainers in Bengaluru",
])
def test_contact_prohibition_does_not_forbid_authorized_search(monkeypatch, question):
    load_company_os_app()
    from app.chat.search_recovery import search_forbidden
    from app.chat.execute import _openworker_search
    assert not search_forbidden(question)
    queries = []
    async def search(query, **_kw):
        queries.append(query)
        return {"mode": "no_hits"}
    monkeypatch.setattr("app.research.explore.explore_web", search)
    asyncio.run(_openworker_search(question))
    assert len(queries) == 1


def test_search_reply_answers_from_pages_instead_of_dumping_them(monkeypatch):
    load_company_os_app()
    from types import SimpleNamespace
    from app.chat.execute import _openworker_search
    from app.credits import metered

    async def explore(query, **_kwargs):
        return {"mode": "live", "summary": "Searched", "hits": [{"title": "Sample Physio Clinic", "url": "https://sample-physio.test/contact-us/"}],
                "pages": [{"title": "Sample Physio Clinic", "url": "https://sample-physio.test/contact-us/", "excerpt": "RAW PAGE TEXT info@sample-physio.test"}]}
    sent = []

    async def transmit(messages, **kwargs):
        sent.append((messages, kwargs))
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="- [Sample Physio Clinic](https://sample-physio.test/contact-us/): info@sample-physio.test"))])

    monkeypatch.setattr("app.research.explore.explore_web", explore)
    monkeypatch.setattr(metered, "llm", transmit)
    reply = asyncio.run(_openworker_search("Find physiotherapy clinics in Bangalore with contact details"))
    assert reply["body_md"].startswith("- [Sample Physio Clinic]") and "RAW PAGE TEXT" in sent[0][0][1]["content"]
    assert sent[0][1]["purpose"] == "venture.chat.assistant" and sent[0][1]["tier"] == "flash"

    async def broken(*_a, **_kw):
        raise RuntimeError("provider down")
    monkeypatch.setattr(metered, "llm", broken)
    reply = asyncio.run(_openworker_search("Find physiotherapy clinics in Bangalore with contact details"))
    assert "sample-physio.test" in reply["body_md"] and "RAW PAGE TEXT" not in reply["body_md"]
