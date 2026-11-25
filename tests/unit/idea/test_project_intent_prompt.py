"""The paid clarifier receives the founder's saved project intent."""

import pytest

from tests.company_os_harness import load_company_os_app
from tests.unit.idea.test_core_onboarding import core as core


def test_personal_project_prompt_does_not_force_business_goals(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/intent.db")
    load_company_os_app()
    from app.idea.agent import messages_for

    personal = messages_for({"project_intent": "personal", "original_idea": "a small bot for my own reminders"})
    startup = messages_for({"project_intent": "startup", "original_idea": "a reminder product for teams"})
    assert "not a startup" in personal[0]["content"]
    assert "small working result" in personal[0]["content"]
    assert "do not claim the\nconnector works" in personal[0]["content"]
    assert "first paid user" not in personal[0]["content"]
    assert "first paid user" in startup[0]["content"]
    assert personal[1]["content"].find('"project_intent": "personal"') >= 0


@pytest.mark.parametrize("idea,audience", [
    ("You reconcile hospital access permissions", "Hospital IT teams"),
    ("You track medication schedules", "Families managing prescriptions"),
])
def test_readiness_instruction_matches_the_root_schema(tmp_path, monkeypatch, idea, audience):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/readiness.db")
    load_company_os_app()
    from app.idea.agent import messages_for

    message = messages_for({"project_intent": "startup", "original_idea": idea,
                            "understanding": {"customer": audience}})[0]["content"]
    assert "top-level confidence" in message
    assert "not inside question or understanding" in message


@pytest.mark.parametrize("idea,buyer,object_phrase,kind", [
    ("An inspection robot for bridge cables, sold to engineering firms to prevent failures.", "engineering firms", "bridge cables", "organisation"),
    ("An imaging headset for dental enamel, sold to parents to monitor children's oral health.", "parents", "dental enamel", "person"),
    ("A repair service for shared buildings, sold to housing cooperatives to reduce maintenance costs.", "housing cooperatives", "shared buildings", "organisation"),
    ("A training aid for speech sounds, sold to recent graduates to practise their presentation skills.", "recent graduates", "speech sounds", "person"),
])
def test_model_audience_schema_requires_a_grounded_person_or_organisation(core, idea, buyer, object_phrase, kind):
    from app.idea.agent import messages_for, response_schema
    from app.idea.audience import local_audience, stated_buyer, validated_audience

    snapshot = {"original_idea": idea}
    assert stated_buyer(idea) == buyer
    assert local_audience(snapshot, object_phrase) == buyer
    stale = {**snapshot, "answered_fields": {"customer": "other people"}}
    assert local_audience(stale, "other people") == buyer
    corrected = {**stale, "conversation": [{"role": "founder", "field": "customer", "text": "other people"}]}
    assert local_audience(corrected, "other people") == "other people"
    assert not local_audience({**snapshot, "answered_fields": {"customer": "not sure"}}, buyer)
    assert validated_audience({"text": buyer, "kind": kind, "quote": idea}, [idea]) == buyer
    assert not validated_audience({"text": object_phrase, "kind": "person", "quote": idea}, [idea])
    assert not validated_audience({"text": buyer, "kind": "unknown", "quote": idea}, [idea])
    assert not validated_audience({"text": buyer, "kind": "person", "quote": "An invented source"}, [idea])
    assert not validated_audience({"text": "other people", "kind": "person", "quote": idea}, [idea])
    assert not validated_audience({"text": "parents", "kind": "person", "quote": "for grandparents"}, ["for grandparents"])
    schema = response_schema()["$defs"]["Audience"]
    assert response_schema()["$defs"]["Understanding"]["properties"]["customer_extraction"] == {"$ref": "#/$defs/Audience"}
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"text", "kind", "quote"}
    prompt = messages_for(snapshot)[0]["content"]
    assert "WHO buys or uses" in prompt and "biggest remaining decision" in prompt
    assert "when the idea already says so" in prompt


@pytest.mark.parametrize("buyer,kind", [("Repair shops", "organisation"), ("New parents", "person")])
def test_model_customer_extraction_never_keeps_secret_values(core, buyer, kind):
    from app.idea.audience import Audience

    secret = "sk-" + "x" * 24
    extraction = Audience(text=buyer, kind=kind, quote=f"For {buyer}. {secret}")
    assert extraction.model_dump() == {"text": "", "kind": "unknown", "quote": ""}
    assert secret not in extraction.model_dump_json()


@pytest.mark.parametrize("idea,buyer,kind", [
    ("A repair service for building faults, sold to housing cooperatives that manage shared apartments to reduce delays.",
     "housing cooperatives that manage shared apartments", "organisation"),
    ("A tutoring app for lesson plans, sold to parents who teach their children at home to reduce preparation time.",
     "parents who teach their children at home", "person"),
])
def test_model_audience_keeps_named_buyer_qualifications(core, idea, buyer, kind):
    from app.idea.audience import local_audience, validated_audience

    extraction = {"text": buyer, "kind": kind, "quote": idea}
    assert validated_audience(extraction, [idea]) == buyer
    assert local_audience({"original_idea": idea}, extracted={"customer_extraction": extraction}) == buyer


@pytest.mark.parametrize("idea,object_phrase", [
    ("A controller for high-power lasers that aligns beams in a factory.", "high-power lasers"),
    ("An automatic watering kit for balcony flowers that adjusts water from soil readings.", "balcony flowers"),
])
def test_unknown_model_audience_does_not_promote_product_objects(core, monkeypatch, idea, object_phrase):
    from app.idea.agent import Clarification, Question, Understanding
    from app.idea.runner import _core_clarification, _known_fields

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    understanding = Understanding(customer=object_phrase, offer=idea,
        customer_extraction={"text": "", "kind": "unknown", "quote": ""})
    snapshot = {"original_idea": idea, "project_intent": "startup"}
    assert "customer" not in _known_fields(snapshot, extracted=understanding.model_dump())
    result = _core_clarification(snapshot, Clarification(case="know", action="question", message="Let's check who would use it.",
        understanding=understanding, question=Question(text="Who would you ask to try it first?", field="first_user_path", why="Find a first user.", options=[])))
    assert result.understanding.customer == ""
    assert result.understanding.customer_extraction.kind == "unknown"
    assert result.question.field == "customer"
