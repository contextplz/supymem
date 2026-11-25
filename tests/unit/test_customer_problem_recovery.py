import asyncio
import json
from types import SimpleNamespace

import pytest

from tests.unit.gtm.test_journey import journey_db


@pytest.fixture(params=[
    (False, "contractors in Toronto", "Contractors lose income chasing late invoice payments.",
     "An invoice tool for contractors in Toronto. Contractors lose income chasing late invoice payments."),
    (True, "families in Kyoto", "Families struggle to plan meals around food allergies.",
     "A meal planning app for families in Kyoto. Families struggle to plan meals around food allergies."),
], ids=["b2b-canada", "consumer-japan"])
def customer_problem(journey_db, monkeypatch, request):
    from app.core.local_mode import is_local
    from app.db.models import Venture
    from app.gtm import journey

    if is_local():
        from app.core.providers_local import save_settings
        save_settings(provider="openai_compatible", base_url="http://127.0.0.1:1234/v1",
            models={"chat": "local-test-model"})

    consumer, audience, pain, idea = request.param
    data = {**journey_db, "consumer": consumer, "audience": audience, "pain": pain, "idea": idea,
            "request": f"Find me {audience} I can email"}
    with data["factory"]() as session:
        venture = session.get(Venture, data["vid"])
        venture.name, venture.raw_idea = audience + " project", idea
        session.commit()
    monkeypatch.setattr(journey, "_icp_seed", lambda vid: {"idea": {"audience": audience, "confirmed": True}})
    return data


def test_customer_search_never_saves_or_truncates_a_problem(customer_problem):
    from app.db.models import Venture
    from app.entry import chat_start
    from app.gtm import journey
    from app.ventures import founder_facts, startup_review

    data = customer_problem
    assert chat_start._classify(data["request"], data["vid"])[0] == "gtm"
    assert startup_review.problem_prefill(data["pain"], stated=True) == data["pain"]
    source = f"Show me {data['audience']}"
    fragment = source[1:]
    extraction = dict.fromkeys(founder_facts.FIELDS)
    extraction["problem"] = {"value": fragment, "quote": fragment, "source": "clarification", "confidence": .95}
    assert "problem" not in founder_facts.validated_extraction(extraction, source, "clarification")
    for quote in (data["pain"], data["pain"][1:]):
        extraction["problem"] = {"value": data["pain"][1:], "quote": quote, "source": "clarification", "confidence": .95}
        assert "problem" not in founder_facts.validated_extraction(extraction, data["pain"], "clarification")
    extraction["problem"] = {"value": data["pain"], "quote": data["pain"], "source": "clarification", "confidence": .95}
    assert founder_facts.validated_extraction(extraction, data["pain"], "clarification")["problem"]["value"] == data["pain"]
    for command in (data["request"], data["request"][1:], f"Show me {data['audience']}", f"Find 8 {data['audience']}"):
        assert startup_review.problem_prefill(command, stated=True) == ""
        with pytest.raises(ValueError, match="gtm_invalid_input"):
            journey.update_icp(data["wid"], data["vid"], data["audience"], problem=command, start=False)
        with data["factory"]() as session:
            venture = session.get(Venture, data["vid"])
            founder_facts.save(session, venture, {"problem": {
                "value": command, "quote": command, "source": "clarification", "confidence": .95}})
            session.commit()
            assert founder_facts.read(session, venture)["problem"]["value"] != command
    with data["factory"]() as session:
        venture = session.get(Venture, data["vid"])
        founder_facts.save(session, venture, {"problem": {
            "value": data["audience"], "quote": data["request"], "source": "clarification", "confidence": .95}})
        session.commit()
        assert founder_facts.read(session, venture)["problem"]["value"] != data["audience"]
    saved = journey.update_icp(data["wid"], data["vid"], data["audience"], problem=data["pain"], start=False)
    assert saved["icp"]["problem"] == data["pain"]


@pytest.mark.parametrize("source", ["raw_idea", "current_revision"])
def test_saved_search_problem_recovers_idea_without_updating_revisions(customer_problem, source, monkeypatch):
    from app.core.local_mode import is_local
    from app.db.models import Venture
    from app.db.supymem_models import IdeaRevision, IdeaSession
    from app.experiments import autonomous
    from app.gtm import journey
    from app.ventures import founder_facts, journey_overrides, startup_review
    from core.clock import utcnow

    data = customer_problem
    broken = data["request"][1:]
    fields = {"customer": data["audience"], "problem": broken,
              "summary": data["idea"] if source == "current_revision" else "Unknown"}
    original = json.dumps(fields)
    with data["factory"]() as session:
        venture = session.get(Venture, data["vid"])
        if source == "current_revision":
            venture.raw_idea = "Unknown"
        startup_review._document(session, venture.id, "internal_new_user_flow", {"enabled": True})
        startup_review._document(session, venture.id, startup_review.INTERNAL_KIND,
            {"founder_facts": {**founder_facts.empty(), "problem": {
                "value": broken, "quote": data["request"], "source": "review", "confidence": .95}}})
        session.add(IdeaSession(id="problem_session", venture_id=venture.id, workspace_id=data["wid"],
            case="know", state="reviewing", current_revision_id="problem_revision", created_at=utcnow(), updated_at=utcnow()))
        session.flush()
        session.add(IdeaRevision(id="problem_revision", session_id="problem_session", venture_id=venture.id,
            workspace_id=data["wid"], rev=1, source="founder", structured=original,
            summary=fields["summary"], created_at=utcnow()))
        row = journey._row(session, venture.id, create=True)
        row.provenance_json = json.dumps({"icp": {"who": data["audience"], "problem": broken,
            "problem_basis": "From the problem you entered.", "confirmed": True, "chat_confirmed": True,
            "needs_input": True}})
        session.commit()
        assert startup_review.effective_fields(session, venture, fields)["problem"] == data["pain"]
    view = journey.status(data["wid"], data["vid"], authorize=False)
    assert view["icp"]["problem"] == data["pain"] and not view["icp"]["needs_input"]
    snapshot = autonomous._snapshot(data["vid"])
    assert snapshot["idea_problem"] == data["pain"]
    provider = SimpleNamespace(name="firecrawl", search_micro=0, scrape_micro=0)
    monkeypatch.setattr("app.credits.metered.price_micro", lambda name: 1)
    expected = {kind for kind, recipe in autonomous.RECIPES.items()
        if not recipe.get("connector") and (is_local() or not recipe.get("approach"))} - {"segments", "reviews"}
    if is_local():
        expected.discard("adjacent_swap")
        if data["consumer"]:
            expected.discard("postings")
    for state in (snapshot, {**snapshot, "journey": {**view, "icp": {
            **view["icp"], "problem": broken, "needs_input": True}}}):
        specs = autonomous.select_runnable(state, provider)
        scans = [spec for spec in specs if spec.get("runner") == "scan"]
        assert {spec["scan"] for spec in scans} == expected
        assert all(spec["icp"]["problem"] == data["pain"] for spec in specs)
        assert all(broken not in spec["hypothesis"] for spec in specs)
        assert all(broken not in query for spec in scans for query in spec["queries"])
    with data["factory"]() as session:
        journey_overrides._save_context(session, data["vid"], data["wid"], data["actor"], skipped={"clarification": {}})
        session.commit()
        assert session.get(IdeaRevision, "problem_revision").structured == original
        current = session.get(IdeaSession, "problem_session").current_revision_id
        assert current != "problem_revision"
        assert json.loads(session.get(IdeaRevision, current).structured)["problem"] == data["pain"]


def test_missing_customer_problem_can_be_supplied_in_experiment_chat(customer_problem, monkeypatch):
    from app.core.local_mode import is_local
    from app.chat import customers, experiments
    from app.chat.store import append_message
    from app.db.models import Venture
    from app.db.supymem_models import IdeaRevision, IdeaSession
    from app.experiments import autonomous
    from app.gtm import journey
    from core.clock import utcnow

    data = customer_problem
    empty_summary = "No idea summary supplied; use placeholders, not invented product claims."
    with data["factory"]() as session:
        session.get(Venture, data["vid"]).raw_idea = data["request"]
        session.add(IdeaSession(id="empty_problem_session", venture_id=data["vid"], workspace_id=data["wid"],
            case="know", state="confirmed", current_revision_id="empty_problem_revision", confirmed_revision_id="empty_problem_revision",
            created_at=utcnow(), updated_at=utcnow()))
        session.flush()
        session.add(IdeaRevision(id="empty_problem_revision", session_id="empty_problem_session", venture_id=data["vid"],
            workspace_id=data["wid"], rev=1, source="founder_override", created_at=utcnow(),
            summary=empty_summary,
            structured=json.dumps({"customer": data["audience"], "problem": data["request"], "summary": empty_summary, "_missing_idea_summary": True})))
        session.commit()
    monkeypatch.setattr(autonomous, "_provider", lambda: SimpleNamespace(name="firecrawl", search_micro=0, scrape_micro=0))
    monkeypatch.setattr("app.credits.metered.price_micro", lambda name: 1)
    reply = experiments.list_reply(data["vid"])
    assert "What problem do they have that you solve?" in reply["body_md"]
    assert "Reply here with one sentence" in reply["body_md"]
    assert "No autonomous experiments are runnable" not in reply["body_md"]
    append_message(data["vid"], role="assistant", body_md=reply["body_md"], kind=reply["kind"])
    command = customers.command(data["vid"], data["pain"])
    assert command and command[0] == "audience"
    asyncio.run(customers.run_command(data["vid"], *command, data["pain"], data["actor"]))
    assert journey.status(data["wid"], data["vid"], authorize=False)["icp"]["problem"] == data["pain"]
    expected = {kind for kind, recipe in autonomous.RECIPES.items()
        if not recipe.get("connector") and (is_local() or not recipe.get("approach"))} - {"segments", "reviews"}
    if is_local():
        expected.discard("adjacent_swap")
        if data["consumer"]:
            expected.discard("postings")
    assert {spec["scan"] for spec in autonomous.available(data["vid"]) if spec.get("runner") == "scan"} == expected
    monkeypatch.setattr(autonomous, "available", lambda vid: [])
    if is_local():
        from app.ventures import founder_facts
        with data["factory"]() as session:
            founder_facts.save(session, session.get(Venture, data["vid"]), {"audience": None})
            session.commit()
    snapshot = experiments._experiment_snapshot(data["vid"])
    monkeypatch.setattr(experiments, "_experiment_snapshot", lambda vid: {**snapshot, "journey": {"icp": {}}, "idea_problem": ""})
    missing_audience = experiments.list_reply(data["vid"])
    assert "First, who are you trying to reach?" in missing_audience["body_md"]
    assert "What problem do they have that you solve?" not in missing_audience["body_md"]


@pytest.mark.parametrize("pitch,expected", [
    ("Rotafy: an app for community choirs in Ghana that organizes rehearsal rotas and absences",
     "Community choirs in Ghana need a way to organize rehearsal rotas and absences."),
    ("Kilnly: a tool for pottery studios which tracks kiln firings and glaze batches",
     "Pottery studios need a way to track kiln firings and glaze batches."),
])
def test_pitch_with_article_topic_derives_the_job_not_the_article(pitch, expected):
    from app.ventures.startup_review import problem_prefill

    assert problem_prefill(pitch) == expected


@pytest.mark.parametrize("who,description,idea,task_word", [
    ("busy commuters in Chicago", "A mobile app that sends gentle alerts to busy commuters before trains leave",
     "an app that reminds busy commuters in Chicago to leave for their train on time", "train"),
    ("small bakeries in Lagos", "A tool for small bakeries to log flour deliveries",
     "Doughly: an app for small bakeries in Lagos that tracks flour deliveries and spoilage", "flour"),
])
def test_search_terms_never_use_a_product_description_as_the_problem(who, description, idea, task_word):
    from app.gtm import journey

    queries = journey._search_queries(who, description, [], context={"idea": idea, "problem": description})
    text = " ".join(queries).lower()
    assert "mobile app" not in text and "a tool for" not in text and " sends " not in text
    assert task_word in text
    assert all(task_word in query.lower() for query in queries if "site:" in query or '"' in query)


@pytest.mark.parametrize("broken", ["Independent yoga studios in Denver need help with an.", "Choirs in Ghana need help with the", "Bakeries need a way to."])
def test_saved_problem_with_dangling_article_is_not_usable(broken):
    from app.ventures.founder_facts import usable_problem

    assert usable_problem(broken) == ""
    assert usable_problem("Bakeries need help with spoiled flour.") == "Bakeries need help with spoiled flour."
