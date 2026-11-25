"""Only an advisory, bounded diagnosis can leave the paid parser."""
import json
import pytest
from tests.company_os_harness import load_company_os_app

@pytest.fixture()
def schema():
    load_company_os_app()
    from app.gtm.god import generation_schema
    return generation_schema

@pytest.mark.parametrize("extra", [{"users_count": True}, {"users_count": -1}, {"users_count": 1000000001},
    {"kind":"strategy"}, {"venture_id":"foreign"}, {"idea":" "}])
def test_request_rejects_coercion_and_unapproved_scope(schema, extra):
    with pytest.raises(ValueError):
        schema.Request.model_validate({"idea":"Bakery order board", **extra})

@pytest.mark.parametrize("change", [{"published": True}, {"evidence_gaps": []},
    {"next_actions":[{"kind":"send_email", "instruction":"Send", "reason":"Launch"}]}])
def test_output_rejects_execution_and_missing_uncertainty(schema, change):
    value = {"bottleneck":"Demand unverified", "rationale":"Founder assertions only",
        "assumptions":["Owners need this"], "evidence_gaps":["No interviews"],
        "next_actions":[{"kind":"research", "instruction":"Review demand", "reason":"Validate hypothesis"}]}
    with pytest.raises(ValueError):
        schema.parse(json.dumps({**value, **change}))


def test_output_is_not_extracted_from_fences_or_surrounding_text(schema):
    with pytest.raises(ValueError):
        schema.parse('```json\n{"done":true}\n```')


def test_overlong_diagnosis_text_is_clipped(schema):
    value = {"bottleneck": "Demand unverified " * 100, "rationale": "Founder assertions only",
        "assumptions": ["Owners need this"] * 9, "evidence_gaps": ["No interviews"],
        "next_actions": [{"kind": "research", "instruction": "Review demand", "reason": "Validate hypothesis"}]}
    parsed = schema.parse(json.dumps(value))
    assert len(parsed["bottleneck"]) <= 1200 and len(parsed["assumptions"]) == 8


@pytest.mark.parametrize("idea", ["Safety inspection reports for manufacturers", "Meal planning for hikers"])
def test_local_diagnosis_next_actions_are_real_capabilities(schema, monkeypatch, idea):
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    value = {"bottleneck": "You need demand evidence", "rationale": idea,
        "assumptions": ["You can reach your audience"], "evidence_gaps": ["No interviews yet"],
        "next_actions": [{"kind": kind, "instruction": "You can test demand for " + idea,
            "reason": "Check your assumptions"} for kind in ("find_customers", "research", "draft_outreach", "build_page")]}
    result = schema.parse(json.dumps(value))
    assert [action["capability"] for action in result["next_actions"]] == ["find_customers", "research", "find_customers", "landing_pages"]
    messages = schema.messages({"brief": {"idea": idea}})
    assert "Keys and connections" not in messages[0]["content"]
    assert "find_customers" in messages[1]["content"] and "draft_outreach" in messages[1]["content"]
    value["next_actions"] = [{"kind": "human_interview", "instruction": "Interview", "reason": "Demand"}]
    with pytest.raises(ValueError):
        schema.parse(json.dumps(value))


@pytest.mark.parametrize("idea", ["Maintenance planning for freight fleets", "Learning piano at home"])
def test_local_diagnosis_needs_no_credit_account_or_hosted_prices(schema, monkeypatch, idea):
    from types import SimpleNamespace
    from app.gtm.god import generation
    from app.credits import accounts, estimate
    from app.llm import founder_provider

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setattr(founder_provider, "resolve_llm_config", lambda *_: SimpleNamespace(
        provider="openai", model="owner-model", source="founder_override", key_is_set=True,
        base_url=None, api_key_ref=None))
    monkeypatch.setattr(founder_provider, "resolved_provider_identity", lambda *_: "saved-model-identity")
    monkeypatch.setattr(estimate, "_lookup_model_row", lambda *_: pytest.fail("Local diagnosis needs no hosted prices"))
    monkeypatch.setattr(accounts, "get_account", lambda *_: pytest.fail("Local diagnosis needs no credit account"))
    result = generation.resources("local-workspace", "local-venture")
    assert result["payer"] == "founder" and result["call_ceiling_micro"] == 0
    assert result["charge_identity"] == generation.digest(["saved-model-identity", None])


@pytest.mark.parametrize("idea", ["Purchase order tracking for lab managers", "Local walks for dog owners"])
def test_local_diagnosis_gives_the_capability_setup_step(schema, monkeypatch, idea):
    import sys
    from types import SimpleNamespace

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    next_step = "Open Keys and connections and switch on Find customers, then retry."
    monkeypatch.setitem(sys.modules, "app.core.local_capabilities", SimpleNamespace(capability=lambda name: {
        "name": name, "title": "Find customers", "state": "off", "missing": [], "next_step": next_step}))
    result = schema.parse(json.dumps({"bottleneck": "You need evidence", "rationale": idea,
        "assumptions": ["You can reach customers"], "evidence_gaps": ["No interviews yet"],
        "next_actions": [{"kind": "find_customers", "instruction": "Find people now", "reason": "Test demand"}]}))
    assert result["next_actions"][0]["instruction"] == next_step
    assert result["next_actions"][0]["capability"] == "find_customers"
