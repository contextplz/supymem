"""Actual source history cannot overrule a later cleared or pending idea record."""
import json
from datetime import timedelta

import pytest

from tests.unit.memory.test_facts import facts as facts  # noqa: F401


def revision(d, fields, *, rev=2, pending=False):
    from app.db.supymem_models import ClarifierTurn, IdeaRevision, IdeaSession
    when = d.when + timedelta(days=rev)
    rid = "idv_" + str(rev)
    with d.factory() as session:
        idea = session.get(IdeaSession, "ids_current")
        if not idea:
            idea = IdeaSession(id="ids_current", workspace_id="wsp_owned", venture_id="ven_owned",
                case="know", state="confirmed", created_at=d.when, updated_at=when)
            session.add(idea)
            session.flush()
        session.add(IdeaRevision(id=rid, workspace_id="wsp_owned", venture_id="ven_owned",
            session_id=idea.id, rev=rev, source="founder" if pending else "agent",
            structured=json.dumps(fields), summary="A scheduling app", created_at=when))
        session.flush()
        idea.current_revision_id = rid
        idea.updated_at = when
        if pending:
            session.add(ClarifierTurn(id="ict_" + str(rev), workspace_id="wsp_owned", venture_id="ven_owned",
                session_id=idea.id, turn=rev, role="founder", kind="text", revision_id=rid,
                text="I need to change the audience before deciding who to serve.", created_at=when))
        session.commit()
    return when


@pytest.mark.parametrize("fields", [{"customer": ""}, {"customer": "Unknown"},
    {"customer": None}, {"customer": "", "audience": "old fallback"}])
def test_later_clear_or_unknown_keeps_previous_source_in_history_only(facts, fields):
    from app.memory.current import idea_authority, stored_report_is_current
    d = facts
    source = d.message("msg_old", "Our audience is teachers.")
    with d.bind():
        identity = d.write(source)
        revision(d, {"customer": "teachers"}, rev=1)
        when = revision(d, fields, rev=2)
        with d.factory() as session:
            authority = idea_authority(session, venture_id="ven_owned", subject="idea.audience")
            current = d.api.query_facts(session, venture_id="ven_owned", subject="idea.audience")
            assert authority["status"] == "unknown" and authority["value"] is None
            assert [row.id for row in current if stored_report_is_current(authority, row.valid_from)] == []
            assert [row.id for row in d.api.query_facts(session, venture_id="ven_owned", include_history=True)] == [identity]
            assert not stored_report_is_current(authority, when)  # Equal source time cannot overrule the revision.


def test_pending_founder_revision_suppresses_unchanged_previous_structured_audience(facts):
    from app.memory.current import idea_authority, stored_report_is_current
    d = facts
    with d.bind():
        d.write(d.message("msg_old", "Our audience is teachers."))
        revision(d, {"customer": "teachers"}, pending=True)
        with d.factory() as session:
            authority = idea_authority(session, venture_id="ven_owned", subject="idea.audience")
            assert authority["status"] == "pending" and authority["value"] is None
            assert not stored_report_is_current(authority, d.when)


def test_known_current_structured_record_is_cited_as_record_not_founder_quote(facts):
    from app.memory.current import idea_authority, stored_report_is_current
    d = facts
    with d.bind():
        revision(d, {"customer": "Independent architects", "pricing_hypothesis": "ten per month"})
        with d.factory() as session:
            authority = idea_authority(session, venture_id="ven_owned", subject="idea.audience")
            assert authority["value"] == "Independent architects" and authority["status"] == "known"
            assert authority["href"] == "/v/ven_owned/idea"
            assert authority["source_ref"].startswith("idea_revision:")
            assert authority["effect_authority"] is False and "quote" not in authority
            assert stored_report_is_current(authority, d.when + timedelta(days=3))


def test_authority_uses_current_revision_helper_scope_and_leaves_nonidea_memory_unchanged(facts):
    from app.memory.current import idea_authority, stored_report_is_current
    from app.tenancy import bind
    d = facts
    with d.bind(), d.factory() as session:
        assert idea_authority(session, venture_id="ven_owned", subject="idea.audience") is None
        assert stored_report_is_current(None, d.when)
        assert idea_authority(session, venture_id="ven_owned", subject="preference.tone") is None
    with bind(workspace_id="wsp_foreign", venture_id="ven_owned", actor="usr_alice"), d.factory() as session:
        with pytest.raises(PermissionError, match="memory_fact_scope_mismatch"):
            idea_authority(session, venture_id="ven_owned", subject="idea.audience")


def test_current_field_credential_cannot_bypass_memory_withholding(facts):
    from app.memory.current import idea_authority
    d = facts
    credential = "api" + "_key=" + "private-test-value-" * 4
    with d.bind():
        revision(d, {"customer": credential})
        with d.factory() as session:
            authority = idea_authority(session, venture_id="ven_owned", subject="idea.audience")
            assert authority["status"] == "unknown" and authority["value"] is None
            assert credential not in json.dumps(authority)


@pytest.mark.parametrize("customer,pending", [("", False), ("Unknown", False), ("teachers", True)])
def test_natural_recall_and_prompt_do_not_restore_audience_after_current_clear(facts, monkeypatch, customer, pending):
    import app.memory as memory
    import app.memory.task_context as legacy
    import app.state
    from app.chat.memory_commands import fact_reply
    from app.memory.context import context_for
    from app.memory.model import ContextPack

    class EmptyLegacy:
        async def recall(self, *args, **kwargs):
            return []
        async def timeline(self, *args, **kwargs):
            return []
        async def context_pack(self, **kwargs):
            return ContextPack(venture_id="ven_owned")

    d = facts
    monkeypatch.setattr(app.state, "db", lambda: d.factory)
    monkeypatch.setattr(memory, "get_memory", lambda: EmptyLegacy())
    monkeypatch.setattr(legacy, "get_memory", lambda: EmptyLegacy())
    with d.bind():
        d.write(d.message("msg_old_audience", "Our audience is teachers."))
        revision(d, {"customer": customer}, pending=pending)
        reply = fact_reply("ven_owned", "idea.audience")
        assert "teachers" not in reply["body_md"]
        history = fact_reply("ven_owned", "idea.audience", history=True)
        assert "teachers" in history["body_md"] and "Earlier" in history["body_md"]
        with d.factory() as session:
            context = context_for(session, workspace_id="wsp_owned", venture_id="ven_owned",
                                  purpose="venture.chat.assistant", query="Who is my audience?")
        assert context["availability"] == "available"
        assert not any(item["subject"] == "idea.audience" for item in context["facts"])
        assert "Our audience is teachers." not in json.dumps(context)


def test_natural_recall_uses_current_structured_customer_and_actual_idea_citation(facts, monkeypatch):
    import app.memory as memory
    import app.state
    from app.chat.memory_commands import fact_reply

    class EmptyLegacy:
        async def recall(self, *args, **kwargs):
            return []
        async def timeline(self, *args, **kwargs):
            return []

    d = facts
    monkeypatch.setattr(app.state, "db", lambda: d.factory)
    monkeypatch.setattr(memory, "get_memory", lambda: EmptyLegacy())
    with d.bind():
        d.write(d.message("msg_old_audience", "Our audience is teachers."))
        revision(d, {"customer": "Independent architects"})
        reply = fact_reply("ven_owned", "idea.audience")
        assert "Independent architects" in reply["body_md"] and "teachers" not in reply["body_md"]
        assert any(citation.get("href") == "/v/ven_owned/idea" for citation in reply["citations"])
