"""One source-backed preflight across unrelated, isolated ventures."""
import json

import pytest

from tests.unit.gtm.test_journey import journey_db


@pytest.fixture(params=[("Factory audit coordinators", "Factory audit coordinators struggle to schedule compliance visits."),
                        ("Urban dog owners", "Urban dog owners struggle to find safe walking routes.")], ids=["b2b-audits", "consumer-pets"])
def known_case(journey_db, monkeypatch, request):
    from app.config import settings
    from app.core.local_mode import is_local
    from app.core.local_secrets import update_settings
    from app.connections import context_docs
    from app.credits.policy import set_policy
    from app.db.models import Venture
    from app.ventures import founder_facts, startup_review

    data = {**journey_db, "audience": request.param[0], "problem": request.param[1]}
    if is_local():
        update_settings("composio", {"auth_configs": {"notion": "offline-document-config"}},
                        {"api_key": "offline-connected-documents"})
        update_settings("capabilities", {"connected_apps": True}, {})
    else:
        set_policy("feature_flags", {"known_check_v1": True, "experiments_autopilot_v1": True}, "test")
    monkeypatch.setattr(settings, "company_os_memory_facts_enabled", False)
    monkeypatch.setattr(context_docs, "research_context", lambda *_args: {})
    with data["factory"]() as session:
        venture = session.get(Venture, data["vid"])
        venture.name, venture.raw_idea = "Your " + data["audience"], "A product to explore."
        founder_facts.save(session, venture, {"audience": founder_facts._fact(data["audience"]),
            "problem": founder_facts._fact(data["problem"])})
        session.commit()
    return data


@pytest.mark.parametrize("state", ["known", "confirm", "conflict", "missing", "cleared", "review", "saved_conflict"])
def test_known_sources_and_editable_policy_are_scoped(known_case, monkeypatch, state):
    from app.connections import context_docs
    from app.db.models import Venture
    from app.tenancy import bind
    from app.ventures import founder_facts, known, startup_review

    data = known_case
    if state in {"confirm", "missing", "cleared"}:
        with data["factory"]() as session:
            startup_review._document(session, data["vid"], startup_review.INTERNAL_KIND,
                {"founder_facts": {"audience": founder_facts._fact(data["audience"], "website", .8)}
                 if state != "missing" else {}, **({"cleared_fields": ["audience"]} if state == "cleared" else {})})
            session.commit()
    if state in {"conflict", "review"}:
        monkeypatch.setattr(context_docs, "research_context", lambda *_args: {"connected_context": {"documents": [
            {"title": "Your selected strategy", "excerpt": "Audience: Museum visitors", "source_url": "https://docs.example/selected"}]}})
    if state == "review":
        with data["factory"]() as session:
            founder_facts.save(session, session.get(Venture, data["vid"]),
                {"audience": founder_facts._fact(data["audience"], "review", 1)})
            session.commit()
    result = known.known(data["vid"], "who", saved="Museum visitors" if state == "saved_conflict" else None)
    assert result["status"] == {"cleared": "missing", "review": "known", "saved_conflict": "conflict"}.get(state, state)
    if state == "saved_conflict":
        assert known.known(data["vid"], "who", saved=data["audience"])["status"] == "known"
    if result["status"] == "conflict":
        assert set(result["value"]) == {data["audience"], "Museum visitors"}
        assert len(result["source"]) == 2
    elif result["status"] != "missing":
        assert result["value"] == data["audience"]
    if result["status"] != "missing":
        card = known.question(result, "audience")
        assert data["audience"] in card["text"] and "\u2014" not in card["text"]
        assert "edit" in card["text"].lower()
    with bind(workspace_id="unrelated_workspace", venture_id="unrelated_venture", actor="unrelated_founder"):
        with pytest.raises(PermissionError, match="known_context_scope_mismatch"):
            known.known(data["vid"], "audience")


@pytest.mark.parametrize("memory_on,docs_on", [(False, False), (True, False), (False, True)])
def test_known_optional_sources_follow_their_flags(known_case, monkeypatch, memory_on, docs_on):
    from app.config import settings
    from app.core.local_mode import is_local
    from app.core.local_secrets import update_settings
    from app.connections import context_docs
    from app.credits.policy import set_policy
    from app.memory import context
    from app.ventures import known, startup_review

    data = known_case
    with data["factory"]() as session:
        startup_review._document(session, data["vid"], startup_review.INTERNAL_KIND, {"founder_facts": {}})
        session.commit()
    calls = []
    monkeypatch.setattr(settings, "company_os_memory_facts_enabled", memory_on)
    set_policy("feature_flags", {"known_check_v1": True, "experiments_autopilot_v1": docs_on}, "test")
    if is_local():
        update_settings("capabilities", {"connected_apps": docs_on}, {})

    def memory(session, **kwargs):
        from app.tenancy import current
        assert (current().workspace_id, current().venture_id) == (data["wid"], data["vid"])
        assert (kwargs["workspace_id"], kwargs["venture_id"], kwargs["purpose"]) == (data["wid"], data["vid"], "known.check")
        calls.append("memory")
        return {"facts": [{"id": "fact_owned", "subject": "idea.target_audience", "statement": data["audience"],
                           "source": {"type": "chat"}}]}

    def documents(session, venture_id, workspace_id):
        assert (venture_id, workspace_id) == (data["vid"], data["wid"])
        calls.append("docs")
        return {"connected_context": {"documents": [{"title": "Your selected plan", "excerpt": "Audience: " + data["audience"]}]}}

    monkeypatch.setattr(context, "context_for", memory)
    monkeypatch.setattr(context_docs, "research_context", documents)
    result = known.known(data["vid"], "audience")
    assert result["status"] == ("known" if memory_on else "confirm" if docs_on else "missing")
    assert calls == (["memory"] if memory_on else ["docs"] if docs_on else [])
    if memory_on:
        from app.tenancy import bind
        with bind(workspace_id=data["wid"], venture_id="", actor=data["actor"]):
            assert known.known(data["vid"], "audience")["status"] == "known"
    set_policy("feature_flags", {"known_check_v1": False, "experiments_autopilot_v1": True}, "test")
    calls.clear()
    result = known.known(data["vid"], "audience")
    if is_local():
        assert result["status"] == ("known" if memory_on else "confirm" if docs_on else "missing")
        assert calls == (["memory"] if memory_on else ["docs"] if docs_on else [])
    else:
        assert result["status"] == "missing" and calls == []


def test_local_saved_answers_need_no_policy_row(known_case, monkeypatch):
    from app.core.local_secrets import update_settings
    from app.connections import context_docs
    from app.db.models import CreditPolicy
    from app.ventures import known

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    with known_case["factory"]() as session:
        flags = session.get(CreditPolicy, "feature_flags")
        if flags:
            session.delete(flags)
        session.commit()
        assert session.get(CreditPolicy, "feature_flags") is None
    update_settings("capabilities", {"connected_apps": False}, {})
    monkeypatch.setattr(context_docs, "research_context", lambda *_: pytest.fail("Your connected apps are off."))
    result = known.known(known_case["vid"], "audience")
    assert known.enabled()
    assert result["status"] == "known" and result["value"] == known_case["audience"]
    assert result["source"] == "Your founder message"


def test_known_recovers_literal_problem_from_revision_summary(known_case):
    from app.db.models import Venture, VentureDocument
    from app.db.supymem_models import IdeaRevision, IdeaSession
    from app.ventures import founder_facts, known, startup_review
    from core.clock import utcnow

    data, now = known_case, utcnow()
    request = "Find me " + data["audience"] + " I can email"
    problem = data["audience"] + " lose time arranging appointments."
    with data["factory"]() as session:
        venture = session.get(Venture, data["vid"])
        venture.raw_idea = "Unknown"
        startup_review._document(session, venture.id, startup_review.INTERNAL_KIND,
            {"founder_facts": {"problem": founder_facts._fact(request, "review")}})
        session.add(IdeaSession(id="literal_problem", venture_id=venture.id, workspace_id=data["wid"],
            case="know", state="reviewing", current_revision_id="literal_problem_revision",
            created_at=now, updated_at=now))
        session.flush()
        structured = json.dumps({"customer": data["audience"], "problem": request,
            "summary": "A product for " + data["audience"] + ". " + problem})
        session.add(IdeaRevision(id="literal_problem_revision", session_id="literal_problem",
            venture_id=venture.id, workspace_id=data["wid"], rev=1, source="founder",
            structured=structured, created_at=now))
        session.commit()
    result = known.known(data["vid"], "problem")
    assert result["status"] == "known" and result["value"] == problem
    with data["factory"]() as session:
        assert session.get(IdeaRevision, "literal_problem_revision").structured == structured
        assert json.loads(session.get(VentureDocument, f"{startup_review.INTERNAL_KIND}:{data['vid']}").body_md)[
            "founder_facts"]["problem"]["value"] == request


def test_inferred_problem_stays_editable_and_respects_corrections_and_clears(known_case):
    from app.db.models import Venture, VentureDocument
    from app.ventures import founder_facts, known, startup_review

    data = known_case
    with data["factory"]() as session:
        venture = session.get(Venture, data["vid"])
        venture.raw_idea = "A scheduling app for " + data["audience"]
        startup_review._document(session, venture.id, startup_review.INTERNAL_KIND, {"founder_facts": {}})
        session.commit()
    result = known.known(data["vid"], "problem")
    assert result["status"] == "confirm"
    assert result["value"] == data["audience"] + " need help with scheduling."
    assert result["source"] == "Your idea description"
    with data["factory"]() as session:
        venture = session.get(Venture, data["vid"])
        assert founder_facts.read(session, venture)["problem"]["value"] is None
        assert json.loads(session.get(VentureDocument, f"{startup_review.INTERNAL_KIND}:{data['vid']}").body_md)[
            "founder_facts"] == {}
        founder_facts.save(session, venture, {"problem": founder_facts._fact(data["problem"], "review")})
        session.commit()
    assert known.known(data["vid"], "problem")["value"] == data["problem"]
    with data["factory"]() as session:
        founder_facts.save(session, session.get(Venture, data["vid"]), {"problem": None})
        session.commit()
    assert known.known(data["vid"], "problem")["status"] == "missing"
