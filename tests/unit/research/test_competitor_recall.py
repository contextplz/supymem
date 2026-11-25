"""Competitor recall: category queries and the category-product relevance rule."""
from tests.company_os_harness import load_company_os_app

load_company_os_app()

from app.research import deep  # noqa: E402
from app.research.deep import normalize  # noqa: E402
from app.research.source_quality import relevant_search_lead, research_topic  # noqa: E402

ROASTER = {"problem": "Forecasting green bean inventory", "customer": "Indie coffee roasters"}
RESULTS = [
    {"url": "https://www.cropster.com/", "title": "Cropster | Coffee roasting software",
     "snippet": "Roast profiling and green coffee inventory management for roasters."},
    {"url": "https://roastlog.com/", "title": "RoastLog - software for coffee roasters",
     "snippet": "Track green bean stock, roasts and orders in one app."},
    {"url": "https://github.com/artisan-roaster-scope/artisan", "title": "artisan: coffee roaster scope",
     "snippet": "Open-source roast logging software for coffee roasters, with green bean inventory."},
    {"url": "https://www.example-accounting.com/small-business-software", "title": "Small business accounting software",
     "snippet": "Invoices and payroll for coffee shops."},
    {"url": "https://www.example.com/best-coffee-roaster-software", "title": "Best coffee roaster software of 2026",
     "snippet": "Roundup of tools for roasters with green bean inventory."},
]


def test_competitors_named_for_the_customer_category_are_admitted():
    topic = research_topic(ROASTER)
    kept = [row["url"] for row in normalize(RESULTS, "fixture", topic=topic)]
    assert kept == [row["url"] for row in RESULTS[:3]]  # generic accounting page and roundup stay out
    # A plain string topic (no customer) keeps the old strict gate, so the gain comes from the category rule.
    assert not relevant_search_lead(str(topic), RESULTS[0])


def test_research_queries_include_competitor_discovery():
    queries = dict(deep.queries_for(ROASTER, "all"))
    assert queries["category"] == "Forecasting green bean inventory software for Indie coffee roasters"
    assert queries["job_tool"] == "Forecasting green bean inventory tool for Indie coffee roasters"
    assert len(queries) == 10
    assert "category" not in dict(deep.queries_for({"problem": "p"}, "all"))


def test_full_research_covers_pricing_and_buying_intent_without_extra_queries():
    snapshots = (
        {"problem": "Scheduling laboratory equipment maintenance", "customer": "Laboratory operations managers"},
        {"problem": "Planning allergy-safe family meals", "customer": "Parents managing food allergies"},
    )
    for snapshot in snapshots:
        planned = deep.queries_for(snapshot, "all")
        assert len(planned) == 10
        assert [key for key, _ in planned] == [
            "direct", "alternatives", "funded", "accelerator", "emerging", "community",
            "services", "category", "job_tool", "customer_voice",
        ]
        queries = dict(planned)
        assert "pricing" in queries["direct"] and "paid plans" in queries["direct"]
        assert "buying intent" in queries["customer_voice"] and "willingness to pay" in queries["customer_voice"]
        for key in ("direct", "customer_voice"):
            assert snapshot["problem"] in queries[key]
            assert snapshot["customer"] in queries[key]
        prior_art_supplement = [query for key, query in planned if key in {"services", "customer_voice"}]
        assert len(prior_art_supplement) == 2
        assert any("pricing" in query for query in prior_art_supplement)
        assert any("buying intent" in query for query in prior_art_supplement)

# Saved idea wording supplied by the installed Current understanding view.
LONG_LAUNCH = {
    'problem': "A first-time solo SaaS founder, about to announce their launch, has no quick and reliable way to verify that their public landing page's primary call-to-action—the 'Get Started' button—actually leads to a working signup form. Clicking it and finding a 404, a forgotten staging URL, or an error page moments before going public is an embarrassing, preventable failure.",
    'customer': 'First-time solo SaaS founders who have just finished their landing page and signup flow and intend to announce the launch publicly within seven days. They have no QA teammate, are anxious about looking unprofessional, and are focused on the core Get Started path working for a real visitor.',
}


def test_saved_long_problem_retains_actual_job_in_discovery_queries():
    queries = dict(deep.queries_for(LONG_LAUNCH, 'all'))
    for key in ('direct', 'category', 'job_tool'):
        assert 'verify' in queries[key].lower()
        assert 'signup' in queries[key].lower()
        assert 'landing' in queries[key].lower()
        assert len(queries[key]) <= 420
    assert len(queries) == 10  # one extra customer-conversation search


def test_long_customer_first_problem_retains_other_domain_job():
    snapshot = {
        'problem': 'An independent neighborhood coffee roaster who is getting ready to grow the business and has very little spare time needs to forecast green bean inventory and track wholesale purchase orders before stock runs out.',
        'customer': 'Independent neighborhood coffee roasters who are getting ready to grow the business',
    }
    queries = dict(deep.queries_for(snapshot, 'all'))
    for key in ('direct', 'category', 'job_tool'):
        assert 'inventory' in queries[key].lower()
        assert 'orders' in queries[key].lower()
    assert len(queries) == 10


def test_compact_queries_are_bounded_and_dont_add_provider_work():
    snapshot = {'problem': ' '.join(f'specific{i}' for i in range(2000)), 'customer': 'Independent operators'}
    for phase in ('all', 'competitors', 'services', 'pricing', 'demand'):
        queries = deep.queries_for(snapshot, phase)
        assert len(queries) <= 10
        assert all(len(query) <= 420 for _, query in queries)


def test_corrected_queries_do_not_promote_a_search_result_to_verified_evidence(monkeypatch):
    lead = {'url': 'https://synthetic.example/signup-verification',
            'title': 'Landing page signup form verification',
            'snippet': 'Verify the Get Started button opens a working signup form without a staging URL or 404 error.'}
    rows = normalize([lead], 'direct', topic=research_topic(LONG_LAUNCH))
    assert len(rows) == 1
    import json
    from types import SimpleNamespace
    saved = []
    session = SimpleNamespace(get=lambda *a, **kw: None, add=saved.append)
    owner = SimpleNamespace(workspace_id='fixture-workspace', venture_id='fixture-venture')
    monkeypatch.setattr(deep.ex, 'mutate', lambda did, payload, callback: callback(owner, None, session))
    deep._store_sources('fixture-dossier', {}, {'items': rows, 'observed_at': '2026-10-03T00:00:00Z'}, 'fixture query', 'direct')
    assert len(saved) == 1
    evidence = json.loads(saved[0].evidence)
    assert evidence['page_verified'] is False
    assert evidence['evidence_kind'] == 'search_excerpt'


def test_customer_persona_alone_cannot_qualify_problem_evidence():
    generic = {'url': 'https://synthetic.example/solo-founders',
               'title': 'First-time solo founder software',
               'snippet': 'Reliable tools for solo SaaS founders launching their startup.'}
    focused = {'url': 'https://synthetic.example/signup-verification',
               'title': 'Landing page signup form verification',
               'snippet': 'Verify the Get Started button opens a working signup form without a staging URL or 404 error.'}
    topic = research_topic(LONG_LAUNCH)
    assert not relevant_search_lead(topic, generic)
    assert relevant_search_lead(topic, focused)
    assert deep.review_leads([generic], "fixture", topic=topic)[0]["url"] == generic["url"]


def test_long_non_ascii_problem_is_not_erased_by_compaction():
    snapshot = {'problem': 'Δοκιμή ' * 20 + 'απόθεμα παραγγελίες', 'customer': 'Αρτοποιεία'}
    query = dict(deep.queries_for(snapshot, 'all'))['direct']
    assert 'απόθεμα' in query
    assert 'παραγγελίες' in query


def test_long_compaction_preserves_tail_even_when_head_words_fill_budget():
    problem = ' '.join(f'introductorycontext{i}' for i in range(25)) + ' track warehouse inventory prevent stockouts'
    query = dict(deep.queries_for({'problem': problem}, 'all'))['direct']
    assert 'warehouse' in query
    assert 'inventory' in query
    assert 'stockouts' in query


def test_search_preparation_preserves_negative_problem_terms():
    problem = 'A small founder about to grow the business has a very long description of requirements and must not send personal data to external systems without consent'
    query = dict(deep.queries_for({'problem': problem}, 'all'))['direct']
    assert 'not' in query.split()
    assert 'without' in query.split()


def test_each_web_run_includes_one_customer_conversation_query():
    for phase in ("all", "competitors", "services", "pricing", "demand"):
        voice = [(key, query) for key, query in deep.queries_for(ROASTER, phase) if key == "customer_voice"]
        assert len(voice) == 1
        assert "site:reddit.com OR site:news.ycombinator.com" in voice[0][1]
    assert deep.CALL_BUDGET == 19 + deep.MAX_PAGE_SCRAPES
