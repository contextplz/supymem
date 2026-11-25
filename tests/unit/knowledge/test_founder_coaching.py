"""Offline coaching uses current journey state and verbatim cited evidence."""
import hashlib
import json
import shutil
from pathlib import Path

import pytest

from tests.company_os_harness import load_company_os_app


@pytest.fixture(scope="module")
def knowledge():
    load_company_os_app()
    from app.knowledge import pmf, yc_video_advice
    return pmf, yc_video_advice


@pytest.mark.parametrize("step,expected", [
    ({"key": "idea.clarify", "title": "Answer the next question about your idea"}, "Problem discovery"),
    ({"key": "research.review", "detail": "Review research and shape the MVP"}, "Segment validation"),
    ({"key": "plan.generate"}, "MVP and delivery"),
    ({"key": "site.publish", "detail": "Invite customers after publication"}, "MVP and delivery"),
    ({"key": "gtm.outreach"}, "Founder-led sales & outreach"),
    ({"key": "learning.replies"}, "Activation & retention"),
    ({"key": "experiment.plan", "title": "Choose your next growth experiment"}, "Distribution & growth"),
    ({"key": "work.next_task", "title": "Test pricing"}, "Pricing and economics"),
])
def test_stage_projection(knowledge, step, expected):
    pmf, _ = knowledge
    assert pmf.journey_stage(step) == expected
    assert pmf.journey_stage({"key": "work.running"}, {"stage": "Research"}) == "Segment validation"


def test_recommend_boosts_stage_without_rejecting_other_relevant_cases(knowledge, monkeypatch):
    pmf, _ = knowledge
    rows = [{"id": name, "kind": "historical_claim", "sheet": "Case Library", "row": 6,
             "fields": [{"label": key, "value": value} for key, value in {
                 "Company": name, "Stage": stage, "Model": "B2B SaaS",
                 "Problem": "customer feedback workflow", "Documented company pattern": "Observed customer workflow",
                 "Transfer theory": "Observe before investing", "Reusable experiment": "Interview a recent user",
             }.items()]} for name, stage in [("Alpha", "MVP and delivery"), ("Beta", "Problem discovery")]]
    monkeypatch.setattr(pmf, "corpus", lambda: {"records": rows})
    pmf.index.cache_clear()
    try:
        assert pmf.recommend("B2B SaaS customer feedback workflow", stage="Problem discovery")[0]["company"] == "Beta"
        assert pmf.recommend("B2B SaaS customer feedback workflow", stage="MVP and delivery")[0]["company"] == "Alpha"
        assert len(pmf.recommend("B2B SaaS customer feedback workflow", stage="Problem discovery")) == 2
    finally:
        pmf.index.cache_clear()


def test_real_cards_have_why_do_avoid_metric_sources_and_refresh(knowledge):
    pmf, _ = knowledge
    query = pmf.reference_context("B2B SaaS developer tools customer feedback manual beta workflow", {"customer": "API platform teams"})
    earlier = pmf.coaching(query, next_step={"key": "idea.clarify", "title": "Clarify the painful job"})
    later = pmf.coaching(query, next_step={"key": "site.publish", "title": "Review the private page"})
    assert not earlier["abstained"] and later["stage"] != earlier["stage"]
    for result in (earlier, later):
        for card in result["items"]:
            assert all(card[key] for key in ("company", "situation_at_this_stage", "what_they_did", "do", "sources"))
            assert not card["why_it_worked"] or not card["why_it_worked"].startswith("Workbook transfer theory")
            assert "API platform teams" in card["do"]
            assert all(source["title"] and source["url"].startswith("https://") for source in card["sources"])
    assert "Clarify the painful job" in earlier["items"][0]["do"]
    assert "Review the private page" in later["items"][0]["do"]
    airbnb = pmf.search("poor photos listings conversion", kind="historical_claim", limit=1)[0]
    card = pmf.coaching_card(airbnb, "marketplace hosts", {"title": "Test listing quality"}, "Segment validation")
    assert card["why_it_worked"] == pmf.fields(airbnb)["Why it worked"]


def test_coaching_uses_specific_fields_omits_shared_rows_and_drops_step_prefix(knowledge, monkeypatch):
    pmf, _ = knowledge
    def record(name, **values):
        return {"id": name, "sheet": "Case Library", "fields": [{"label": key, "value": value} for key, value in {
            "Company": name, "Founder's question": "What should we deliver manually?",
            "Documented company pattern": "Delivered orders by hand", "Transfer theory": "Generic delivery theory",
            "When not to copy": "Never hide manual work", "Primary metric": "Successful outcomes", **values}.items()]}
    records = [record("Alpha", **{"Why it worked": "Learned delivery constraints", "Limits and guardrails": "Check delivery costs",
                                  "Primary signal": "On-time orders"}), record("Beta"), record("Gamma")]
    monkeypatch.setattr(pmf, "corpus", lambda: {"records": records})
    cards = [pmf.coaching_card(pmf.card(row), "{}", {"title": "Your landing page is ready"}, "MVP and delivery") for row in records]
    assert cards[0]["situation_at_this_stage"] == "What should we deliver manually?"
    assert cards[0]["why_it_worked"] == "Learned delivery constraints"
    assert cards[0]["avoid"] == "Avoid: Check delivery costs" and cards[0]["metric"] == "On-time orders"
    for card in cards[1:]:
        assert card["why_it_worked"] is None and card["avoid"] is None and card["metric"] is None


def test_abstains_on_missing_and_unrelated_context(knowledge):
    pmf, yc = knowledge
    for query in ("", "unrelated quantum neutrino crystals"):
        assert pmf.coaching(query)["items"] == []
        assert pmf.coaching(query)["abstained"]
        assert yc.quote(query) is None


def test_existing_customers_only_receive_growth_stage_coaching(knowledge, monkeypatch):
    pmf, _ = knowledge
    rows = [{"id": name, "company": name, "title": name, "documented_action": "Test a channel",
             "fields": [{"label": "Stage", "value": stage}]} for name, stage in (
                 ("Ring", "MVP and delivery"), ("Growth", "Distribution & growth"), ("Traction", "Early traction"))]
    monkeypatch.setattr(pmf, "recommend", lambda *args, **kwargs: rows)
    for key in ("site.audit", "site.build", "research.review", "gtm.outreach"):
        step = {"key": key, "has_customers": True}
        assert pmf.journey_stage(step) == "Distribution & growth"
        result = pmf.coaching("customer acquisition", next_step=step)
        assert [card["company"] for card in result["items"]] == ["Growth", "Traction"]
        assert "acquisition channel" in result["items"][0]["do"]
        assert "smallest usable" not in result["items"][0]["do"]
    monkeypatch.setattr(pmf, "recommend", lambda *args, **kwargs: rows[:1])
    assert pmf.coaching("customer acquisition", next_step={"has_customers": True})["abstained"]


@pytest.mark.parametrize("mode", ["local", "hosted"])
@pytest.mark.parametrize("query", [
    "willingness to pay pricing paid pilot enterprise sales",
    "consumer pricing willingness to pay",
])
def test_yc_mode_switch_and_real_excerpt(knowledge, monkeypatch, mode, query):
    _, yc = knowledge
    monkeypatch.setenv("SUPYMEM_MODE", mode)
    monkeypatch.delenv("YC_VIDEO_ADVICE_ENABLED", raising=False)
    monkeypatch.delenv("YC_VIDEO_ADVICE_RIGHTS_APPROVED", raising=False)
    assert yc.enabled()
    quote = yc.quote(query)
    assert quote and quote["url"].startswith("https://www.youtube.com/watch?v=")
    card = next(row for row in yc.corpus()["cards"] if row["id"] == quote["id"])
    assert any(quote["text"] in excerpt for excerpt in card["evidence"]["excerpts"])
    assert len(quote["text"].split()) <= 25
    assert quote["title"] == card["source_video"]["title"]
    monkeypatch.setenv("YC_VIDEO_ADVICE_ENABLED", "0")
    if mode == "local":
        assert yc.enabled() and yc.quote(query) == quote
        assert yc.planning_context({"offer": query})["items"]
    else:
        assert not yc.enabled()
        assert yc.quote(query) is None and yc.planning_context({"offer": query})["items"] == []


def test_quote_rejects_unsourced_paraphrase_and_long_or_unrelated_excerpts(knowledge, monkeypatch):
    _, yc = knowledge
    row = {"id": "real", "source_video": {"title": "A video", "url": "https://www.youtube.com/watch?v=real"},
           "action_discussed": "Pricing conversations reveal willingness to pay.",
           "evidence": {"excerpts": []}}
    monkeypatch.setattr(yc, "search", lambda *args, **kwargs: [row])
    assert yc.quote("pricing conversations willingness pay") is None
    row["evidence"]["excerpts"] = ["Pricing conversations reveal willingness to pay."]
    assert yc.quote("pricing conversations willingness pay")["text"] == row["evidence"]["excerpts"][0]
    row["source_video"]["url"] = "javascript:alert(1)"
    assert yc.quote("pricing conversations willingness pay") is None
    row["source_video"]["url"] = "https://www.youtube.com/watch?v=real"
    row["evidence"]["excerpts"] = ["pricing " * 30 + "conversations.", "Entirely unrelated material is here today."]
    assert yc.quote("pricing conversations willingness pay") is None


def test_compact_runtime_manifest_and_planning_context(knowledge):
    _, yc = knowledge
    root = Path(__file__).resolve().parents[3]
    manifest = json.loads((root / "data/knowledge/yc-video-advice-1dfdaf90/manifest.json").read_text())
    assert manifest["runtime_sha256"] == hashlib.sha256(yc.CORPUS.read_bytes()).hexdigest()
    assert len(yc.corpus()["cards"]) == 883 and "passages" not in yc.corpus()
    context = yc.planning_context({"offer": "willingness to pay pricing paid pilot enterprise sales"})
    assert context["enabled"] and context["items"]
    assert all(item["source_video"]["url"] for item in context["items"])


def test_u12_coaching_quote_uses_venture_and_current_topic(knowledge, monkeypatch):
    pmf, yc = knowledge
    seen = []
    def quote(query, **kwargs):
        seen.append((query, kwargs))
        return None
    monkeypatch.setattr(yc, "quote", quote)
    query = "App for first-time parents in the US tracking baby sleep"
    result = pmf.coaching(query, next_step={"key": "idea.clarify", "title": "Interview parents about night wakings"})
    assert seen == [(query, {"context": query, "topic": "Interview parents about night wakings"})]
    assert not any(item.get("yc_says") for item in result["items"])


@pytest.mark.parametrize("mode", ["local", "hosted"])
def test_idea_references_mode_switch_personal_and_incidental_requirements(knowledge, monkeypatch, mode):
    pmf, _ = knowledge
    monkeypatch.setenv("SUPYMEM_MODE", mode)
    idea = {"summary": "B2B SaaS developer tools customer feedback manual beta workflow", "customer": "API platform teams"}
    monkeypatch.setenv("YC_VIDEO_ADVICE_ENABLED", "0")
    card = pmf.idea_references(idea)[0]["founder_references"]
    assert len(card["startups"]) >= 2
    if mode == "local":
        assert card["yc_advice"]
    else:
        assert card["yc_advice"] == []
    assert pmf.idea_references({**idea, "project_intent": "personal"}) == []
    assert pmf.idea_references({"summary": "Unrelated quantum neutrino crystals", "customer": "Neutrino physicists",
                                "requirements": [idea["summary"]]}) == []
    cards = pmf.idea_references({
        "summary": "AI job application assistant for job seekers: match skills, build resumes and track applications",
        "customer": "Software engineers looking for a job",
        "requirements": ["Secure API keys and developer tools for integrations"],
    })
    assert not any("developer tooling" in case["relevance_reason"] or "security" in case["relevance_reason"]
                   for card in cards for case in card["founder_references"]["startups"])


@pytest.mark.parametrize("idea,relevant", [
    ({"summary": "B2B inspection dashboard for turbine overhaul crews to manage routine maintenance across facilities",
      "customer": "Power station mechanics", "offer": "An easy product to track their work before they buy"}, False),
    ({"summary": "Consumer audio app for amateur birdwatchers identifying migrating birds in local woodlands",
      "customer": "Weekend nature enthusiasts", "offer": "An easy product to track their observations before they buy"}, False),
    ({"summary": "B2B SaaS developer tools customer feedback manual beta workflow", "customer": "API platform teams"}, True),
])
def test_references_require_customer_industry_or_decision_overlap(knowledge, monkeypatch, idea, relevant):
    pmf, yc = knowledge
    monkeypatch.setenv("YC_VIDEO_ADVICE_ENABLED", "1")
    query = pmf._context_text(pmf.reference_context("", idea))
    cases = pmf.recommend(query, kind="historical_claim", exact_kind=True)
    advice = yc.recommend(query, stage="idea")
    references = pmf.idea_references(idea)
    if not relevant:
        assert cases == []
        assert advice == []
        assert references == []
        return
    assert len(cases) >= 2 and advice and references
    for row in cases + advice:
        assert "Your context overlaps with this source:" in row["relevance_reason"]
        evidence = row["relevance_reason"].split('"')[1]
        if row.get("source") == yc.SOURCE_ID:
            assert evidence in " ".join(str(row.get(key) or "") for key in ("situation", "action_discussed"))
        else:
            assert any(evidence in str(value) for value in pmf.fields(row).values())


def test_idea_references_abstain_on_failure_and_never_pad_a_single_case(knowledge, monkeypatch):
    pmf, yc = knowledge
    idea = {"summary": "B2B SaaS developer tools customer feedback manual beta workflow", "customer": "API platform teams"}
    row = pmf.recommend(pmf.reference_context("", idea), limit=1, exact_kind=True)[0]
    monkeypatch.setattr(pmf, "recommend", lambda *args, **kwargs: [row])
    monkeypatch.setattr(yc, "recommend", lambda *args, **kwargs: [])
    assert pmf.idea_references(idea) == []

    def unavailable(*args, **kwargs):
        raise OSError("missing local corpus")

    monkeypatch.setattr(pmf, "recommend", unavailable)
    monkeypatch.setattr(yc, "recommend", unavailable)
    assert pmf.idea_references(idea) == []


@pytest.mark.parametrize("query", [
    "enterprise sales procurement pricing paid pilot",
    "consumer marketplace photos listings conversion",
])
@pytest.mark.parametrize("source", [0, 1], ids=["workbook", "talks"])
def test_local_data_directory_preserves_retrieval_and_hosted_loading(knowledge, monkeypatch, tmp_path, query, source):
    module = knowledge[source]
    original = module.CORPUS
    raw = original.read_bytes()
    module.corpus.cache_clear()
    module.index.cache_clear()
    expected = module.corpus()
    expected_search = module.search(query)
    directory = tmp_path / "knowledge"
    directory.mkdir()
    target = directory / original.name
    target.write_bytes(raw)
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setattr(module, "CORPUS", tmp_path / original.name)
    try:
        module.corpus.cache_clear()
        module.index.cache_clear()
        assert module.corpus() == expected
        assert module.search(query) == expected_search
        target.write_bytes(raw[:-1])
        module.corpus.cache_clear()
        with pytest.raises(ValueError, match="Restore .* and restart your app"):
            module.corpus()
        monkeypatch.setenv("SUPYMEM_MODE", "hosted")
        monkeypatch.setattr(module, "CORPUS", original)
        module.corpus.cache_clear()
        assert module.corpus() == expected
    finally:
        module.corpus.cache_clear()
        module.index.cache_clear()


@pytest.mark.parametrize("query", [
    "enterprise sales procurement pricing paid pilot",
    "consumer marketplace photos listings conversion",
])
@pytest.mark.parametrize("source,key", [(0, "records"), (1, "cards")], ids=["workbook", "talks"])
def test_local_missing_reference_data_gives_a_restore_step(knowledge, monkeypatch, tmp_path, query, source, key):
    module = knowledge[source]
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(module, "CORPUS", tmp_path / module.CORPUS.name)
    module.corpus.cache_clear()
    module.index.cache_clear()
    try:
        assert module.corpus()[key] == []
        assert module.corpus()["dataset_id"] is None
        assert module.search(query) == []
        context = module.planning_context({"offer": query})
        assert context["items"] == []
        assert context["message"] == module.corpus()["message"]
        assert "you" in context["message"].lower()
        assert "restart" in context["message"]
        assert str(tmp_path / "knowledge") in context["message"]
        assert module.CORPUS.name in context["message"]
    finally:
        module.corpus.cache_clear()
        module.index.cache_clear()


@pytest.mark.parametrize("query,source", [
    ("enterprise sales procurement pricing paid pilot", 0),
    ("consumer marketplace photos listings conversion", 1),
])
def test_local_integrity_lists_pinned_files_and_detects_damage(knowledge, monkeypatch, tmp_path, query, source):
    from app.knowledge import local_data

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    root = Path(__file__).resolve().parents[3]
    shutil.copytree(root / "data/knowledge", tmp_path / "knowledge")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    report = local_data.verify_data()
    assert report["ok"] and len(report["files"]) == 13
    assert all(item["status"] == "verified" for item in report["files"])
    module = knowledge[source]
    raw = module.CORPUS.read_bytes()
    entry = next(item for item in report["files"] if item["path"].endswith(module.CORPUS.name))
    assert entry["size_bytes"] == len(raw)
    assert entry["sha256"] == hashlib.sha256(raw).hexdigest()
    assert isinstance(module.search(query), list)
    directory = tmp_path / "owner/knowledge"
    directory.mkdir(parents=True)
    target = directory / module.CORPUS.name
    target.write_bytes(raw)
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(directory.parent))
    report = local_data.verify_data()
    missing = [item for item in report["files"] if item["status"] == "missing"]
    assert report["ok"] and missing
    assert all(item["optional"] and "You can" in item["message"] for item in missing)
    target.write_bytes(raw[:-1])
    report = local_data.verify_data()
    assert not report["ok"]
    entry = next(item for item in report["files"] if item["path"].endswith(module.CORPUS.name))
    assert entry["status"] == "mismatch" and "Restore" in entry["message"]
    monkeypatch.setenv("SUPYMEM_MODE", "hosted")
    assert local_data.verify_data() == {"enabled": False, "ok": True, "files": []}


@pytest.mark.parametrize("relative", [
    "startup-pmf-2026-09-19/README.md",
    "yc-video-advice-1dfdaf90/README.md",
])
def test_integrity_restore_step_names_the_original_subdirectory(knowledge, monkeypatch, tmp_path, relative):
    from app.knowledge import local_data

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    name = f"data/knowledge/{relative}"
    monkeypatch.setattr(local_data, "BUNDLED_FILES", {name: local_data.BUNDLED_FILES[name]})
    target = tmp_path / "knowledge" / relative
    target.parent.mkdir(parents=True)
    target.write_bytes(b"damaged source inventory")
    report = local_data.verify_data()
    assert not report["ok"] and report["files"][0]["status"] == "mismatch"
    assert str(target) in report["files"][0]["message"]


@pytest.mark.parametrize("question,step", [
    ("How should I price enterprise SaaS procurement with a paid pilot and test willingness to pay?",
     {"key": "pricing.test", "title": "Pricing and willingness to pay"}),
    ("How can I improve consumer marketplace photos and listings conversion before scaling growth?",
     {"key": "learning.replies", "title": "Improve photos and listings conversion"}),
    ("How should I validate developer tools for API platform teams with a manual beta and customer feedback workflow?",
     {"key": "idea.clarify", "title": "Validate the manual beta workflow"}),
])
@pytest.mark.parametrize("location", ["source", "payload", "user_data"])
def test_local_founder_questions_return_cited_coaching(knowledge, monkeypatch, tmp_path, question, step, location):
    pmf, yc = knowledge
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("YC_VIDEO_ADVICE_ENABLED", "0")
    if location != "source":
        directory = (tmp_path / "payload/services/company_os/app/knowledge" if location == "payload"
                     else tmp_path / "data/knowledge")
        directory.mkdir(parents=True)
        for module in knowledge:
            shutil.copy2(module.CORPUS, directory / module.CORPUS.name)
            monkeypatch.setattr(module, "CORPUS", (directory if location == "payload" else tmp_path) / module.CORPUS.name)
    for module in knowledge:
        module.corpus.cache_clear()
        module.index.cache_clear()
    try:
        assert yc.enabled()
        result = pmf.coaching(question, next_step=step)
        assert "message" not in result
        assert all(card["what_they_did"] and card["do"] and card["sources"] for card in result["items"])
        assert all(source["url"].startswith(("https://", "http://"))
                   for card in result["items"] for source in card["sources"])
        advice = yc.recommend(question)
        assert result["items"] and not result["abstained"]
        assert all(card["source_video"]["url"] and card["suggested_next_action"] for card in advice)
        quote = yc.quote(question)
        if quote:
            assert len(quote["text"].split()) <= 25
            original = next(card for card in yc.corpus()["cards"] if card["id"] == quote["id"])
            assert any(quote["text"] in excerpt for excerpt in original["evidence"]["excerpts"])
        for module in knowledge:
            assert module.corpus()["corpus_sha256"] == hashlib.sha256(module.CORPUS.read_bytes() if location != "user_data"
                                                                      else (directory / module.CORPUS.name).read_bytes()).hexdigest()
    finally:
        for module in knowledge:
            module.corpus.cache_clear()
            module.index.cache_clear()


@pytest.mark.parametrize("question", ["enterprise SaaS pricing paid pilot", "consumer marketplace photos listings conversion"])
@pytest.mark.parametrize("failure", ["missing", "damaged"])
@pytest.mark.parametrize("endpoint", ["search", "coaching", "record", "review"])
def test_local_reference_api_explains_how_to_restore_packs(knowledge, monkeypatch, tmp_path, question, failure, endpoint):
    from fastapi import HTTPException, Response
    from app.api import knowledge_api
    from app.ventures import next_step

    pmf, yc = knowledge
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(knowledge_api, "assert_venture_access", lambda *args: ("owner", "workspace"))
    monkeypatch.setattr(knowledge_api, "venture_query", lambda *args: question)
    monkeypatch.setattr(next_step, "founder_next_step", lambda *args: {"key": "idea.clarify"})
    for module in knowledge:
        monkeypatch.setattr(module, "CORPUS", tmp_path / module.CORPUS.name)
        module.corpus.cache_clear()
        module.index.cache_clear()
    if failure == "damaged":
        pmf.CORPUS.write_bytes(b"damaged coaching file")
    try:
        with pytest.raises(HTTPException) as raised:
            if endpoint == "search":
                knowledge_api.references("venture", q=question, kind=None, stage=None, limit=3)
            elif endpoint == "coaching":
                knowledge_api.coaching_references("venture", Response())
            elif endpoint == "record":
                knowledge_api.reference("venture", "advice")
            else:
                knowledge_api.review_references("venture", knowledge_api.ReviewBody(query=question))
        assert raised.value.status_code == 503
        message = raised.value.detail
        assert isinstance(message, str) and "you" in message.lower() and "restart" in message
        assert pmf.CORPUS.name in message and str(tmp_path / "knowledge") in message
        assert "\u2014" not in message
    finally:
        for module in knowledge:
            module.corpus.cache_clear()
            module.index.cache_clear()


@pytest.mark.parametrize("question", [
    "enterprise SaaS pricing willingness to pay paid pilot",
    "consumer marketplace photos listings conversion",
])
def test_quote_does_not_attach_an_incidental_excerpt_from_unrelated_advice(knowledge, monkeypatch, question):
    _, yc = knowledge
    monkeypatch.setattr(yc, "search", lambda *args, **kwargs: [{
        "id": "unrelated", "situation": "Keep your cooking ingredients chilled.",
        "action_discussed": "Food storage and refrigerator temperatures",
        "source_video": {"title": "Safe food storage", "url": "https://www.youtube.com/watch?v=food"},
        "evidence": {"excerpts": [f"You should consider {question} carefully before choosing your next step."]},
    }])
    assert yc.quote(question) is None


@pytest.mark.parametrize("question", [
    "enterprise sales SaaS pricing willingness to pay paid pilot",
    "consumer marketplace photos listings conversion",
])
def test_local_relevance_review_uses_cited_ranking_without_a_provider(knowledge, monkeypatch, question):
    from app.api import knowledge_api
    from app.knowledge import jev

    pmf, _ = knowledge
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("JEV_REFERENCE_REVIEW_ENABLED", "0")
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.setattr(jev, "provider_call", lambda *args: pytest.fail("Your local review must not call a provider"))
    monkeypatch.setattr(jev, "db", lambda: pytest.fail("Your read-only local review must not create a spend receipt"))
    monkeypatch.setattr(knowledge_api, "assert_venture_access", lambda *args: ("owner", "workspace"))
    monkeypatch.setattr(knowledge_api, "subject_person_id", lambda *args: "owner")
    candidates = pmf.recommend(question, limit=6)
    assert candidates and jev.enabled()
    result = knowledge_api.review_references("venture", knowledge_api.ReviewBody(query=question))
    assert result["advisory"] == {"assessment": "selected", "selected_id": candidates[0]["id"]}
    assert result["items"][0]["source_urls"]
    empty = knowledge_api.review_references("venture", knowledge_api.ReviewBody(query=" "))
    assert empty["advisory"] == {"assessment": "no_close_match", "selected_id": None}
    invalid = jev.review("", candidates, workspace_id="workspace", venture_id="venture", actor="owner")
    assert invalid["reason"] == "input_bounds" and invalid["selected_id"] is None
    unrelated = jev.review(question, [{**candidates[0], "id": "not-in-your-workbook"}],
                           workspace_id="workspace", venture_id="venture", actor="owner")
    assert unrelated == {"assessment": "no_close_match", "selected_id": None}


@pytest.mark.parametrize("question", ["enterprise SaaS pricing paid pilot", "consumer marketplace listings conversion"])
def test_local_missing_coaching_packs_gives_one_message(knowledge, monkeypatch, tmp_path, question):
    from app.knowledge import local_data

    pmf, yc = knowledge
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    for module in knowledge:
        monkeypatch.setattr(module, "CORPUS", tmp_path / module.CORPUS.name)
        module.corpus.cache_clear()
        module.index.cache_clear()
    try:
        result = pmf.coaching(question)
        assert result["abstained"] and not result["items"]
        assert result["message"].count("restart") == 1
        assert all(module.CORPUS.name in result["message"] for module in knowledge)
        assert str(tmp_path / "knowledge") in result["message"]
        monkeypatch.setattr(local_data, "_runtime_path", lambda packaged: tmp_path / packaged.name)
        report = local_data.verify_data()
        required = [entry for entry in report["files"] if not entry["optional"]]
        assert not report["ok"] and len(required) == 2
        assert all(entry["status"] == "missing" and str(tmp_path / "knowledge") in entry["message"] for entry in required)
    finally:
        for module in knowledge:
            module.corpus.cache_clear()
            module.index.cache_clear()
