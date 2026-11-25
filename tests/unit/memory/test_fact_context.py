"""Offline founder-source correction, history, abstention and prompt budget checks."""
from __future__ import annotations

import json

import pytest

from tests.unit.memory.test_facts import facts as facts  # noqa: F401


def test_thirty_scripted_corrections_keep_latest_quotes_and_history(facts, monkeypatch):
    from app.memory.extraction import capture_chat_message, quoted_candidates
    from app.memory.context import context_for
    from app.chat.memory_commands import fact_reply
    from app.memory.model import ContextPack
    import app.memory.task_context as legacy

    class Empty:
        async def context_pack(self, **kwargs):
            return ContextPack(venture_id="ven_owned")
    monkeypatch.setattr(legacy, "get_memory", lambda: Empty())
    d = facts
    import app.state
    monkeypatch.setattr(app.state, "db", lambda: d.factory)
    # These are offline scripts over actual persisted source rows. They measure
    # quoted recall, not provider quality or real microphone acceptance.
    for index in range(30):
        with d.bind_private():
            assert quoted_candidates("hello") == []
            for change, audience in enumerate(("teachers", "dentists", "clinics")):
                source = d.message(f"eval_{index}_{change}", f"Our audience is {audience}.", day=(index * 3 + change) / 100)
                assert len(capture_chat_message(source["ref"].split(":", 1)[1])) == 1
            with d.factory() as session:
                projection = context_for(session, workspace_id="wsp_owned", venture_id="ven_owned",
                                         purpose="venture.chat.assistant", query="who is my audience?")
            assert projection["facts"][0]["statement"] == "Our audience is clinics."
            assert projection["effect_authority"] is False
            reply = fact_reply("ven_owned", "idea.audience")
            assert "clinics" in reply["body_md"] and "teachers" not in reply["body_md"]
            history = fact_reply("ven_owned", "idea.audience", history=True)
            assert all(audience in history["body_md"] for audience in ("teachers", "dentists", "clinics"))
    with d.bind():
        assert "don't have a source-backed statement" in fact_reply("ven_owned", "idea.pricing")["body_md"]


def test_extraction_schema_rejects_invented_quote_and_non_declarations(facts):
    from app.memory.extraction import FactExtraction, quoted_candidates
    from pydantic import ValidationError
    assert quoted_candidates("Should our audience be dentists?") == []
    assert quoted_candidates("Charlie suggested that our audience is dentists.") == []
    assert quoted_candidates("We might target dentists.") == []
    with pytest.raises(ValidationError):
        FactExtraction.model_validate({"facts": [{"subject": "system.approval", "quote": "approved"}]})
    with facts.bind(), facts.factory() as session:
        source = facts.message("real", "Our audience is teachers.")
        with pytest.raises(ValueError, match="quote_unproven"):
            facts.api.remember_fact(session, venture_id="ven_owned", subject="idea.audience", predicate="statement",
                value="Our audience is dentists.", source={"type": "chat", "ref": source["ref"],
                                                           "quote": "Our audience is dentists."})


def test_self_correction_in_same_turn_uses_last_source_quote(facts, monkeypatch):
    from app.memory.extraction import capture_chat_message
    import app.state
    monkeypatch.setattr(app.state, "db", lambda: facts.factory)
    with facts.bind_private():
        source = facts.message("self_correction", "Our audience is teachers. Actually, our audience is dentists.")
        assert len(capture_chat_message(source["ref"].split(":", 1)[1])) == 1
        assert facts.read()[0]["value"] == "Actually, our audience is dentists."


def test_context_does_not_clip_sources_and_marks_unavailable(facts, monkeypatch):
    from app.memory.context import context_for
    from app.memory.model import ContextPack
    import app.memory.task_context as legacy
    class Empty:
        async def context_pack(self, **kwargs):
            return ContextPack(venture_id="ven_owned")
    monkeypatch.setattr(legacy, "get_memory", lambda: Empty())
    with facts.bind():
        source = facts.message("large", "Our audience is " + "international teachers " * 400)
        facts.write(source)
        with facts.factory() as session:
            bounded = context_for(session, workspace_id="wsp_owned", venture_id="ven_owned", purpose="plan.compile",
                                  query="audience", budget_bytes=1024)
            assert bounded["facts"] == [] and bounded["omitted_count"] == 1
            assert len(json.dumps(bounded, separators=(",", ":")).encode()) <= 1024
        from sqlalchemy import text
        session.execute(text("DROP TABLE venture_facts"))
        session.commit()
        with facts.factory() as session:
            failed = context_for(session, workspace_id="wsp_owned", venture_id="ven_owned", purpose="plan.compile", query="audience")
            assert failed["availability"] == "unavailable" and failed["facts"] == []
