"""Current citation custody in the actual legacy outbound draft consumer."""
import json

import pytest

from tests.unit.memory.test_facts import facts as facts  # noqa: F401


@pytest.mark.parametrize("ids", [["fact_foreign"], ["fact_own", "fact_own"], "fact_own", [1], None])
def test_unknown_or_malformed_draft_citations_are_refused(facts, ids):
    from app.memory.grounding import validate_fact_citations
    with pytest.raises(ValueError):
        validate_fact_citations({"facts": [{"id": "fact_own"}], "statements": []}, ids)


def test_unknown_idea_record_cannot_be_claimed_as_known_source(facts):
    from app.memory.grounding import validate_fact_citations
    with pytest.raises(ValueError):
        validate_fact_citations({"facts": [], "statements": [], "idea_records": [
            {"status": "unknown", "source_ref": "idea_revision:idv_unknown"}]}, ["idea_revision:idv_unknown"])


def test_scoped_draft_uses_owned_source_quotes_without_charging_for_model_prose(facts, monkeypatch):
    from app.gtm.supymem import draft
    import app.state
    import app.memory.task_context as legacy
    from app.memory.model import ContextPack
    class Empty:
        async def context_pack(self, **kwargs):
            return ContextPack(venture_id="ven_owned")
    monkeypatch.setattr(legacy, "get_memory", lambda: Empty())
    monkeypatch.setattr(app.state, "db", lambda: facts.factory)
    def model(prompt, **kwargs):
        raise AssertionError("Unverifiable model draft must not be charged or used")
    monkeypatch.setattr(draft, "_canonical_complete", model)
    with facts.bind():
        facts.write(facts.message("draft_source", "Our audience is bookshops."))
        result = draft.draft_step(first_name="Pat", email=None, company="Example", title=None,
            hook="the new store", linkedin_url=None, research_notes="Public source: a store opened.",
            day=7, workspace_id="wsp_owned")
    assert '"Our audience is bookshops."' in result["body"]
    assert result["source"] == "source_quotes" and result["llm_status"] == "source_only_draft"
    assert len(result["venture_fact_ids"]) == 1 and result["venture_fact_ids"][0] not in result["body"]


@pytest.mark.parametrize("ids", [[], None, ["unrelated_valid_id"]])
def test_empty_or_false_model_citations_cannot_emit_unsupported_metrics(facts, monkeypatch, ids):
    from app.gtm.supymem import draft
    import app.state
    monkeypatch.setattr(app.state, "db", lambda: facts.factory)
    calls = []
    def unsupported(*args, **kwargs):
        calls.append(True)
        return json.dumps({"subject": "Results", "body": "We have 100 paying customers and doubled revenue.",
                           "hook": "the new store", "venture_fact_ids": ids}), {"status": "llm_completed"}
    monkeypatch.setattr(draft, "_canonical_complete", unsupported)
    with facts.bind():
        facts.write(facts.message("metric_source", "Our audience is bookshops."))
        result = draft.draft_step(first_name="Pat", email=None, company="Example", title=None, hook="the new store",
            linkedin_url=None, research_notes="Public source: a store opened.", day=7, workspace_id="wsp_owned")
    assert calls == []
    assert "100 paying" not in result["body"] and "doubled revenue" not in result["body"]
    assert result["source"] == "source_quotes"


def test_scoped_empty_context_uses_safe_recipient_template_without_cold_case_story(facts, monkeypatch):
    from app.gtm.supymem import draft
    import app.state
    monkeypatch.setattr(app.state, "db", lambda: facts.factory)
    monkeypatch.setattr(draft, "_canonical_complete", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no paid call")))
    with facts.bind():
        result = draft.draft_step(first_name="Pat", email=None, company="Example", title=None, hook="the new store",
            linkedin_url=None, research_notes=None, day=4, sequence_kind="cold", workspace_id="wsp_owned")
    assert result["source"] == "template" and result["venture_fact_ids"] == []
    assert "founder in a similar seat" not in result["body"] and "started getting replies" not in result["body"]


@pytest.mark.parametrize("messages", [None, {"first_dm": "Hi Pat, congratulations on the new store."}])
def test_default_off_scoped_draft_retains_main_behavior(facts, monkeypatch, messages):
    from app.config import settings
    from app.gtm.supymem import draft
    import app.state
    monkeypatch.setattr(settings, "company_os_memory_facts_enabled", False)
    def unexpected_memory_read():
        raise AssertionError("Default-off draft must not read memory")
    monkeypatch.setattr(app.state, "db", unexpected_memory_read)
    calls = []
    def model(*args, **kwargs):
        calls.append(True)
        return None, {"status": "llm_unavailable"}
    monkeypatch.setattr(draft, "_canonical_complete", model)
    with facts.bind():
        result = draft.draft_step(first_name="Pat", email=None, company="Example", title=None, hook="the new store",
            linkedin_url=None, research_notes=None, day=1, workspace_id="wsp_owned", opengtm_messages=messages)
    assert "venture_fact_ids" not in result
    assert result["source"] == ("opengtm" if messages else "template")
    assert calls == ([] if messages else [True])
    if messages:
        assert result["body"] == messages["first_dm"] + "\n"
