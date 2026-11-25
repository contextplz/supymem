"""Search rank alone must not promote unrelated snippets into a report."""
from __future__ import annotations

import pytest

from tests.company_os_harness import load_company_os_app

load_company_os_app()

from app.research import deep  # noqa: E402
from app.research.deep import normalize  # noqa: E402
from app.core.jobs import FatalError  # noqa: E402
from app.research.source_quality import relevant_search_lead, research_topic  # noqa: E402
from app.signals import research  # noqa: E402


TOPIC = "Collect actionable pilot feedback and prioritize requests for product teams"
RESULTS = [
    {"url": "https://tasks.example/pricing", "title": "Task management pricing",
     "snippet": "Plans for projects and teams. Gather feedback in one place."},
    {"url": "https://agat.example/privacy-policy", "title": "AGAT privacy policy",
     "snippet": "We collect feedback from users and prioritize requests for product teams."},
    {"url": "https://datawiza.example/terms", "title": "Datawiza terms of service",
     "snippet": "Pilot feedback and requests are governed by these terms."},
    {"url": "https://feedbacktool.example/product", "title": "Pilot feedback platform",
     "snippet": "Collect pilot feedback and prioritize feature requests."},
    {"url": "https://forum.example/t/42", "title": "Discussion",
     "snippet": "We need a way to collect pilot feedback and prioritize requests."},
]


def test_unrelated_pricing_and_legal_boilerplate_are_excluded():
    assert not any(relevant_search_lead(TOPIC, row) for row in RESULTS[:3])
    assert all(relevant_search_lead(TOPIC, row) for row in RESULTS[3:])
    assert [row["url"] for row in normalize(RESULTS, "fixture", topic=TOPIC)] == [
        RESULTS[3]["url"], RESULTS[4]["url"]
    ]


def test_broad_agent_results_do_not_inherit_relevance_from_search_query():
    topic = "Security engineers reviewing AI agent access requests"
    generic = [
        {"url": "https://agents.example/cost", "title": "AI Agents Cost",
         "snippet": "Compare the cost of AI agents and hosting plans."},
        {"url": "https://agents.example/overview", "title": "What are AI agents?",
         "snippet": "Learn how agents automate common business tasks."},
    ]
    alternative = {"url": "https://governance.example/product",
                   "title": "Access reviews for autonomous agents",
                   "snippet": "Review permissions before an agent can use a connected app."}
    assert not any(relevant_search_lead(topic, row) for row in generic)
    assert relevant_search_lead(topic, alternative)
    assert [row["url"] for row in normalize([*generic, alternative], "fixture", topic=topic)] == [alternative["url"]]


def test_matching_category_words_do_not_replace_the_problem_facet():
    topic = "Security engineers review AI agent access requests and permissions"
    unrelated = [
        {"url": "https://www.reddit.com/r/LocalLLaMA/comments/1q7hywi/how_do_you_manage_quality_when_ai_agents_write/",
         "title": "How do you manage quality when AI agents write code faster than humans can review it?",
         "snippet": "We need better ways to review code written by AI agents."},
        {"url": "https://www.appsecengineer.com/blog/ai-agents-in-appsec-are-coming-for-your-manual-reviews",
         "title": "AI Agents in AppSec Are Coming for Your Manual Reviews",
         "snippet": "AI agents automate security tasks and review code."},
        {"url": "https://www.linkedin.com/posts/jonathan-capriola-bb46a445_a2spa-aisecurity-zerotrust-activity-7352397412415545345-EO0H",
         "title": "AI Agents are getting smarter.",
         "snippet": "AI agents improve risk reviews for insurance claims and audit-ready transcripts."},
    ]
    relevant = {"url": "https://governance.example/agent-access",
                "title": "Agent access governance",
                "snippet": "Security engineers review permissions before agents use connected systems."}
    assert not any(relevant_search_lead(topic, row) for row in unrelated)
    assert relevant_search_lead(topic, relevant)
    assert [row["url"] for row in normalize([*unrelated, relevant], "fixture", topic=topic)] == [relevant["url"]]


@pytest.mark.parametrize("topic", [
    "Job application assistant for recent graduates",
    "Recent graduates need an assistant to organize job applications",
    "For recent graduates, an assistant that tracks job applications",
])
def test_problem_match_does_not_depend_on_topic_word_order(topic):
    competitor = {"url": "https://careers.example/job-application-tracker",
                  "title": "Job application tracker",
                  "snippet": "Organize applications and follow-ups while searching for work."}
    assert relevant_search_lead(topic, competitor)


def test_reordered_problem_still_excludes_adjacent_agent_discussions():
    topic = "AI agent access requests need reviews and permissions from security engineers"
    unrelated = {"url": "https://www.appsecengineer.com/blog/ai-agents-in-appsec-are-coming-for-your-manual-reviews",
                 "title": "AI Agents in AppSec Are Coming for Your Manual Reviews",
                 "snippet": "AI agents automate security tasks and review code."}
    relevant = {"url": "https://governance.example/agent-access",
                "title": "Agent access governance",
                "snippet": "Security engineers review permissions before agents use connected systems."}
    assert not relevant_search_lead(topic, unrelated)
    assert relevant_search_lead(topic, relevant)


def test_problem_anchors_relevance_despite_broad_customer_language():
    snapshot = {
        "problem": "Security engineers review AI agent access requests and permissions",
        "customer": "Teams buying ChatGPT Business subscriptions and comparing pricing plans",
        "offer": "Software for security teams",
    }
    topic = research_topic(snapshot)
    assert topic == snapshot["problem"]
    off_topic = [
        {"url": "https://mintmcp.com/blog/ai-agent-cost", "title": "AI Agent Cost",
         "snippet": "Compare AI agent pricing, hosting and security costs."},
        {"url": "https://community.openai.com/t/chatgpt-do-15", "title": "ChatGPT Do £15 and subscription experience",
         "snippet": "I need a cheaper ChatGPT Business subscription for my team."},
        {"url": "https://help.openai.com/en/articles/chatgpt-business", "title": "ChatGPT Business",
         "snippet": "Manage workspace access, billing, and security settings."},
        {"url": "https://openai.com/release-notes", "title": "OpenAI release notes",
         "snippet": "Updates to agents, access, and security features across products."},
        {"url": "https://snyk.io/plans", "title": "Snyk pricing",
         "snippet": "Security plans for developer teams."},
        {"url": "https://example.com/blog/top-agent-tools", "title": "Top 10 AI agent security tools",
         "snippet": "A general list of popular agent tools and pricing."},
    ]
    relevant = [
        {"url": "https://governance.example/agent-access", "title": "Agent access governance",
         "snippet": "Review permissions before an autonomous agent connects to data."},
        {"url": "https://forum.example/t/agent-access", "title": "Discussion",
         "snippet": "We need a way to review agent access requests before granting permissions."},
    ]
    assert not any(relevant_search_lead(topic, row) for row in off_topic)
    assert all(relevant_search_lead(topic, row) for row in relevant)
    assert [row["url"] for row in normalize([*off_topic, *relevant], "fixture", topic=topic)] == [
        row["url"] for row in relevant
    ]


def test_exact_qa_idea_does_not_promote_repeated_category_words_alone():
    topic = (
        "Fictional benchmark venture: AI agent security for enterprise teams. Help security "
        "engineers inspect and approve AI agent access to sensitive systems, with audit "
        "trails and policy controls. This is a test venture for clarification and research "
        "UI; do not build, deploy, or contact users."
    )
    cost = {"url": "https://mintmcp.com/blog/ai-agent-cost", "title": "AI Agent Cost",
            "snippet": "Compare AI agent pricing, hosting and security costs."}
    listicle = {"url": "https://example.com/blog/top-agent-tools", "title": "Top 10 AI agent security tools",
               "snippet": "A general list of popular agent tools and pricing."}
    competitor = {"url": "https://governance.example/agent-access", "title": "Agent access governance",
                  "snippet": "Review permissions before an autonomous agent connects to data."}
    assert not relevant_search_lead(topic, cost)
    assert not relevant_search_lead(topic, listicle)
    assert relevant_search_lead(topic, competitor)
    assert not relevant_search_lead(topic, {
        "url": "https://www.radware.com/cyberpedia/best-commercial-tools-for-securing-multi-agent-ai-systems/",
        "title": "Best Commercial Tools for Securing Multi-Agent AI Systems: Top 8 in 2026",
        "snippet": "Compare security tools, agent access, policies, and audit trails for enterprise teams.",
    })


def test_followup_provider_path_rechecks_topic_after_domain_scope(monkeypatch):
    """A vendor-domain pricing hit must not bypass the real search normalizer."""
    topic = "Security engineers inspect and approve AI agent access with audit trails"

    class Provider:
        name = "fixture"

        async def search(self, query):
            assert query.startswith("site:checkmarx.com")
            return [
                {"url": "https://checkmarx.com/learn/snyk/snyk-pricing-free-vs-paid-plans-tco-and-what-users-have-to-say/", "title": "Snyk Pricing: Free vs. Paid Plans, TCO, and What Users Have to Say",
                 "snippet": "Compare security plans for developer teams."},
                {"url": "https://checkmarx.com/agent-access", "title": "AI agent access review",
                 "snippet": "Security engineers inspect and approve agent permissions with audit trails."},
                {"url": "https://unrelated.example/agent-access", "title": "Agent access review",
                 "snippet": "Security engineers approve agent access."},
            ]

    monkeypatch.setattr(deep, "perform", lambda *args, callback, **kwargs: callback())
    result = deep._search("dossier", {}, Provider(), "followup", "site:checkmarx.com pricing", topic,
                          allowed_host="checkmarx.com")
    assert [item["url"] for item in result["items"]] == ["https://checkmarx.com/agent-access"]
    assert result["source_counts"]["provider_rows"] == 3
    assert result["source_counts"]["scoped_out"] == 1
    assert result["source_counts"]["off_topic"] == 1


def test_deep_normalizer_cannot_silently_drop_topic():
    with pytest.raises(FatalError, match="research_topic_missing"):
        normalize([{"url": "https://checkmarx.com/learn/snyk/snyk-pricing-free-vs-paid-plans-tco-and-what-users-have-to-say/", "title": "Snyk pricing"}],
                  "fixture", topic="")


def test_search_receipt_counts_provider_rows_and_relevance_rejections(monkeypatch):
    from app.signals.research import SearchResults

    class Provider:
        name = "firecrawl"

        async def search(self, query):
            return SearchResults([
                {"url": "https://clinic.example/handover", "title": "Clinic handover checklist",
                 "snippet": "Create shift handover checklists for clinic managers."},
                {"url": "https://unrelated.example/weather", "title": "Weather forecast", "snippet": "Sunny today."},
                {"url": "http://localhost/private", "title": "Private clinic note", "snippet": "Sensitive"},
            ], provider_rows=5, rejected_shape=1, rejected_missing_url=1)

    monkeypatch.setattr(deep, "perform", lambda *args, callback, **kwargs: callback())
    result = deep._search("dossier", {}, Provider(), "direct", "clinic handover", "Clinic shift handover checklist")
    assert len(result["items"]) == 1
    assert result["source_counts"] == {"provider_rows": 5, "parsed_rows": 3, "scoped_out": 0,
                                        "missing_url": 1, "invalid_shape": 1, "unsafe_url": 1,
                                        "off_topic": 1, "over_limit": 0, "admitted": 1, "review_leads": 0}
    assert "Private clinic note" not in str(result["source_counts"])


def test_empty_search_receipt_distinguishes_no_provider_hits_from_screened_hits(monkeypatch):
    from app.signals.research import SearchResults

    class Provider:
        name = "firecrawl"
        rows = SearchResults([], provider_rows=0, rejected_shape=0, rejected_missing_url=0)

        async def search(self, query):
            return self.rows

    provider = Provider()
    monkeypatch.setattr(deep, "perform", lambda *args, callback, **kwargs: callback())
    empty = deep._search("dossier", {}, provider, "direct", "query", "Clinic shift handover checklist")
    assert empty["source_counts"]["provider_rows"] == 0
    assert empty["source_counts"]["admitted"] == 0

    provider.rows = SearchResults([
        {"url": "https://weather.example/forecast", "title": "Weather forecast", "snippet": "Sunny."}
    ], provider_rows=1, rejected_shape=0, rejected_missing_url=0)
    screened = deep._search("dossier", {}, provider, "direct", "query", "Clinic shift handover checklist")
    assert screened["source_counts"]["provider_rows"] == 1
    assert screened["source_counts"]["off_topic"] == 1
    assert screened["source_counts"]["admitted"] == 0


def test_firecrawl_web_rows_keep_scoped_unverified_leads_separate_from_evidence(monkeypatch):
    """A valid v2 web list can be useful to inspect even when no row qualifies as evidence."""
    from app.signals.research import _sources_from_response

    topic = "AI assistant turns clinic shift notes into a handover checklist for independent clinics"
    body = {"success": True, "data": {"web": [
        {"url": "https://handoff.example/shift-report", "title": "Nursing shift report",
         "description": "Clinic managers use a checklist during handover."},
        {"url": "https://care.example/clinic-software", "title": "Clinic management software",
         "description": "Organize shift notes for small practices."},
        {"url": "https://weather.example/today", "title": "Weather forecast", "description": "Sunny today."},
        {"url": "https://care.example/privacy-policy", "title": "Privacy policy",
         "description": "Clinic shift notes and handover checklist."},
        {"url": "http://localhost/private", "title": "Clinic notes", "description": "Private."},
        {"url": "https://care.example/clinic-software", "title": "Clinic management software",
         "description": "Duplicate source."},
    ]}}

    class Provider:
        name = "firecrawl"

        async def search(self, query):
            return _sources_from_response("firecrawl", body)

    monkeypatch.setattr(deep, "perform", lambda *args, callback, **kwargs: callback())
    result = deep._search("dossier", {}, Provider(), "direct", "clinic handover software", topic)
    assert result["items"] == []
    assert len(result["review_leads"]) == 2
    assert {lead["url"] for lead in result["review_leads"]} == {
        "https://handoff.example/shift-report", "https://care.example/clinic-software"
    }
    assert all("snippet" not in lead for lead in result["review_leads"])
    assert result["source_counts"]["provider_rows"] == 6
    assert result["source_counts"]["off_topic"] == 5
    assert result["source_counts"]["review_leads"] == 2


def test_review_leads_are_bounded_and_never_enter_saved_evidence(monkeypatch):
    import json
    from types import SimpleNamespace

    stored = {}

    class Session:
        def scalars(self, query):
            return self

        def all(self):
            return [finding for finding in stored.values() if finding.category == "review_lead"]

        def get(self, model, identifier, **kwargs):
            return stored.get(identifier)

        def add(self, finding):
            stored[finding.id] = finding

    session = Session()
    monkeypatch.setattr(deep.ex, "mutate", lambda did, payload, callback: callback(
        SimpleNamespace(workspace_id="workspace", venture_id="venture"), {}, session))
    result = {"observed_at": "2026-09-29T00:00:00Z", "review_leads": [
        {"url": f"https://clinic{i}.example/notes", "title": f"Clinic notes {i}", "provider": "firecrawl"}
        for i in range(12)
    ]}
    deep._store_review_leads("dossier", {}, result, "direct")
    assert len(stored) == deep.REVIEW_LEADS_PER_DOSSIER
    assert all(json.loads(finding.evidence)["evidence_kind"] == "search_lead_unverified" for finding in stored.values())
    assert all(finding.category == "review_lead" for finding in stored.values())
    assert all("Unverified search result" in finding.summary for finding in stored.values())
    deep._store_review_leads("dossier", {}, result, "direct")
    assert len(stored) == deep.REVIEW_LEADS_PER_DOSSIER


def test_unverified_review_lead_cannot_become_competition_or_pricing_signal():
    from app.research.service import dossier_evidence_dimensions

    lead = {"id": "lead", "url": "https://care.example/clinic-software",
            "summary": "Plans start at $9 per month.", "evidence_kind": "search_lead_unverified"}
    review = {"assessments": [{"category": "competitor_direct", "source_ids": ["lead"]}]}
    dimensions = dossier_evidence_dimensions({"review_lead": [lead]}, review)
    assert dimensions["competition"]["status"] == "unknown"
    assert dimensions["pricing"]["status"] == "unknown"


def test_review_rerun_does_not_reintroduce_old_off_topic_source(monkeypatch):
    """The inherited row path is gated before it can reach model synthesis."""
    import json
    from types import SimpleNamespace

    from app.research import service

    topic = "Security engineers inspect and approve AI agent access with audit trails"
    prior = SimpleNamespace(id="prior", workspace_id="workspace", venture_id="venture", revision_id="revision", summary='{"_runtime":{}}')
    row = SimpleNamespace(workspace_id="workspace", venture_id="venture", revision_id="revision")
    old = [
        SimpleNamespace(url="https://checkmarx.com/learn/snyk/snyk-pricing-free-vs-paid-plans-tco-and-what-users-have-to-say/", title="Snyk pricing guide",
                        summary="Compare security plans for developer teams.", category="source", source="fixture",
                        evidence=json.dumps({"observed_at": "2026-09-28T00:00:00Z", "evidence_kind": "search_excerpt"}),
                        license=None, stars=None, reuse_recommendation=None),
        SimpleNamespace(url="https://checkmarx.com/agent-access", title="AI agent access review",
                        summary="Security engineers inspect and approve agent permissions with audit trails.",
                        category="source", source="fixture",
                        evidence=json.dumps({"observed_at": "2026-09-28T00:00:00Z", "evidence_kind": "search_excerpt"}),
                        license=None, stars=None, reuse_recommendation=None),
        SimpleNamespace(url="https://www.appsecengineer.com/blog/ai-agents-in-appsec-are-coming-for-your-manual-reviews",
                        title="AI Agents in AppSec Are Coming for Your Manual Reviews",
                        summary="AI agents automate security tasks and review code.", category="source", source="fixture",
                        evidence=json.dumps({"observed_at": "2026-09-28T00:00:00Z", "evidence_kind": "search_excerpt"}),
                        license=None, stars=None, reuse_recommendation=None),
    ]

    class Session:
        added = []

        def get(self, model, identifier, **kwargs):
            return prior if identifier == "prior" else None

        def scalars(self, query):
            return self

        def all(self):
            return old

        def add(self, item):
            self.added.append(item)

    session = Session()
    doc = {"_runtime": {"snapshot": {"problem": topic}}}
    monkeypatch.setattr(service, "review_inputs", lambda did: (None, None, "digest"))
    monkeypatch.setattr(deep.ex, "mutate", lambda did, payload, update: update(row, doc, session))
    deep.inherit_feedback("new", {"previous_dossier_id": "prior", "phase": "review", "source_digest": "digest"})
    assert [finding.url for finding in session.added] == ["https://checkmarx.com/agent-access"]


def test_bakery_plural_and_repository_description_retain_real_alternatives():
    topic = "Small bakeries lose custom cake orders in email"
    website = {"url": "https://example.com/bakery", "title": "Bakery order software",
               "snippet": "Bakery order management. Plans cost $12 monthly."}
    repository = {"url": "https://github.com/example/bakery", "title": "example/bakery",
                  "snippet": "An order management library.", "repository": {"full_name": "example/bakery"}}
    assert relevant_search_lead(topic, website)
    assert relevant_search_lead(topic, repository)
    assert not relevant_search_lead(topic, {key: value for key, value in repository.items() if key != "repository"})


@pytest.mark.asyncio
async def test_fast_scan_only_promotes_relevant_leads(monkeypatch):
    class Provider:
        name = "duckduckgo"  # Explicitly free offline fixture.

        async def search(self, query):
            return RESULTS

    monkeypatch.setattr(research, "_provider_from_env", lambda: Provider())
    scan = await research.research_recon(TOPIC)
    assert scan["mode"] == "live"
    assert {row["url"] for row in scan["sources"]} == {RESULTS[3]["url"], RESULTS[4]["url"]}
    assert all("tasks.example" not in str(row) for row in scan["claims"])
    assert all("agat.example" not in str(row) for row in scan["competitors"])


@pytest.mark.asyncio
async def test_empty_relevance_set_is_coverage_gap_not_market_verdict(monkeypatch):
    class Provider:
        name = "duckduckgo"  # Explicitly free offline fixture.

        async def search(self, query):
            return RESULTS[:3]

    monkeypatch.setattr(research, "_provider_from_env", lambda: Provider())
    scan = await research.research_recon(TOPIC)
    assert scan["mode"] == "blocked"
    assert scan["sources"] == []
    assert "no relevant usable sources" in scan["summary"]


@pytest.mark.asyncio
async def test_matching_query_does_not_make_generic_agent_page_relevant(monkeypatch):
    topic = "Security engineers reviewing AI agent access requests"
    seen_queries = []

    class Provider:
        name = "duckduckgo"  # Explicitly free offline fixture.

        async def search(self, query):
            seen_queries.append(query)
            return [{"url": "https://agents.example/cost", "title": "AI Agents Cost",
                     "snippet": "Compare the cost of AI agents and hosting plans."}]

    monkeypatch.setattr(research, "_provider_from_env", lambda: Provider())
    scan = await research.research_recon(topic)
    assert any("security engineers" in query.lower() for query in seen_queries)
    assert scan["mode"] == "blocked"
    assert scan["sources"] == []


def test_gym_churn_competitor_qualifies_despite_different_wording():
    from app.research.source_quality import relevant_search_lead

    topic = "software that flags gym members likely to cancel (from check-in data) for independent gym owners"
    competitor = {
        "title": "Gym Member Retention Software | Churn Prediction for Fitness Clubs",
        "url": "https://www.example-gym-retention.com/features/member-churn-prediction",
        "snippet": "Spot at-risk members from check-in data and reach out before they cancel their membership.",
    }
    assert relevant_search_lead(topic, competitor)
    unrelated = {"title": "Best pizza dough recipes", "url": "https://example.org/pizza",
                 "snippet": "Members of our cooking club share dough tips."}
    assert not relevant_search_lead(topic, unrelated)
