"""Automatic capture has private-turn admission; long-term reads retain actor scope."""
import pytest

from tests.unit.memory.test_facts import facts as facts  # noqa: F401


def add_conversation(d, identity, *, visibility="private", creator="usr_alice"):
    with d.factory() as session:
        session.add(d.Conversation(id=identity, workspace_id="wsp_owned", venture_id="ven_owned",
            creator_person_id=creator, visibility=visibility, title="Owned chat", creation_key=identity,
            created_at=d.when, updated_at=d.when))
        session.commit()


def test_capture_requires_actual_private_binding_and_is_idempotent(facts):
    from app.memory.extraction import capture_chat_message
    d = facts
    source = d.message("msg_private", "Our audience is teachers.")
    with d.bind():
        assert capture_chat_message("msg_private") == []
        assert d.read() == []
    with d.bind_private():
        captured = capture_chat_message("msg_private")
        assert len(captured) == 1
        assert capture_chat_message("msg_private") == captured
        assert d.read()[0]["source"] == source


def test_disabled_capture_and_fact_writes_cannot_touch_storage(facts, monkeypatch):
    from app.config import settings
    from app.memory.extraction import capture_chat_message
    d = facts
    source = d.message("msg_disabled", "Our audience is teachers.")
    monkeypatch.setattr(settings, "company_os_memory_facts_enabled", False)
    with d.bind_private():
        assert capture_chat_message("msg_disabled") == []
        with pytest.raises(ValueError, match="memory_facts_disabled"):
            d.write(source)
        assert d.read() == []


def test_automatic_capture_skips_legacy_shared_but_deliberate_source_api_remains(facts):
    from app.memory.extraction import capture_chat_message
    d = facts
    add_conversation(d, "cnv_legacy", visibility="legacy_shared", creator=None)
    source = d.message("msg_legacy", "Our audience is teachers.", conversation_id="cnv_legacy")
    with d.bind_private(conversation_id="cnv_legacy"):
        assert capture_chat_message("msg_legacy") == []
        assert d.read() == []
    with d.bind():
        assert d.write(source)


def test_private_binding_does_not_ingest_another_owned_thread_but_recall_can_read_it(facts):
    from app.memory.extraction import capture_chat_message
    d = facts
    add_conversation(d, "cnv_other")
    d.message("msg_other", "Our audience is teachers.", conversation_id="cnv_other")
    with d.bind_private():
        assert capture_chat_message("msg_other") == []
    with d.bind_private(conversation_id="cnv_other"):
        captured = capture_chat_message("msg_other")
        assert len(captured) == 1
    with d.bind_private():
        assert d.read()[0]["id"] == captured[0]  # Same actor's long-term memory crosses owned threads.


@pytest.mark.parametrize("field,value", [("creator_person_id", "usr_revoked"), ("visibility", "legacy_shared"),
    ("workspace_id", "wsp_foreign")])
def test_live_conversation_revocation_or_scope_drift_refuses_capture(facts, field, value):
    from app.memory.extraction import capture_chat_message
    d = facts
    d.message("msg_source", "Our audience is teachers.")
    with d.bind_private():
        with d.factory() as session:
            setattr(session.get(d.Conversation, "cnv_owned"), field, value)
            session.commit()
        assert capture_chat_message("msg_source") == []
        assert d.read() == []


@pytest.mark.parametrize("changed", [{"workspace_id": "wsp_foreign"}, {"venture_id": "ven_foreign"},
                                     {"actor": "usr_foreign"}])
def test_tenancy_drift_cannot_reuse_an_earlier_bound_conversation(facts, changed):
    from app.memory.extraction import capture_chat_message
    from app.tenancy import bind
    d = facts
    d.message("msg_source", "Our audience is teachers.")
    with d.bind_private():
        scope = dict(workspace_id="wsp_owned", venture_id="ven_owned", actor="usr_alice")
        with bind(**(scope | changed)):
            assert capture_chat_message("msg_source") == []
        assert d.read() == []


@pytest.mark.parametrize("changes", [{"author_person_id": "usr_foreign"}, {"workspace_id": "wsp_foreign"},
    {"venture_id": "ven_foreign"}, {"role": "assistant"}, {"role": "system"}])
def test_unowned_or_assistant_source_cannot_be_automatically_ingested(facts, changes):
    from app.memory.extraction import capture_chat_message
    d = facts
    d.message("msg_source", "Our audience is teachers.", **changes)
    with d.bind_private():
        assert capture_chat_message("msg_source") == []
        assert d.read() == []


@pytest.mark.parametrize("body,kind", [("Our audience is teachers.", "memory_command"),
    ("Our audience is teachers.", "voice_summary"),
    ("Remember: a preference. Our audience is teachers.", "text"),
    ("Correct memory mem_owned: Our audience is teachers.", "text")])
def test_explicit_memory_commands_are_not_mirrored_as_automatic_ingest(facts, body, kind):
    from app.memory.extraction import capture_chat_message
    d = facts
    d.message("msg_command", body, kind=kind)
    with d.bind_private():
        assert capture_chat_message("msg_command") == []
        assert d.read() == []


def test_sixth_declaration_keeps_final_same_source_correction_in_real_private_capture(facts):
    import json
    from app.memory.extraction import capture_chat_message, quoted_candidates
    from app.memory.model_extract import validate_quotes
    d = facts
    statements = [f"Our audience is {audience}." for audience in
                  ("teachers", "nurses", "clinics", "bookshops", "architects")]
    final = "Actually, our audience is dentists."
    body = " ".join([*statements, final])
    source = d.message("msg_six_declarations", body)
    assert [candidate.quote for candidate in quoted_candidates(body)] == [final]
    validated = validate_quotes(json.dumps({"facts": [{"subject": "idea.audience", "quote": final}]}), body)
    assert validated["facts"][0]["quote"] == final
    with pytest.raises(ValueError, match="quote_unproven"):
        validate_quotes(json.dumps({"facts": [{"subject": "idea.audience", "quote": "Our audience is plumbers."}]}), body)
    with d.bind_private():
        identities = capture_chat_message("msg_six_declarations")
        assert len(identities) == 1
        assert capture_chat_message("msg_six_declarations") == identities
        with d.factory() as session:
            current = d.api.query_facts(session, venture_id="ven_owned", subject="idea.audience")
            assert [fact.value for fact in current] == [final]
            assert len(d.api.query_facts(session, venture_id="ven_owned", include_history=True)) == 1
            proof, _, _ = d.api._proof(session, venture_id="ven_owned", source=source | {"quote": final})
            assert proof == current[0].source_digest


def test_five_subject_limit_is_applied_after_last_occurrence_ordering(facts):
    from app.memory.extraction import quoted_candidates
    body = ("Our audience is teachers. Our pricing is ten dollars. Our problem is scheduling. "
            "Our product is a booking calendar. I prefer short answers. We decided to interview users. "
            "Actually, our audience is dentists.")
    candidates = quoted_candidates(body)
    assert len(candidates) == 5
    assert [candidate.subject for candidate in candidates] == ["idea.problem", "idea.solution",
        "preference.answer_length", "decision.strategy", "idea.audience"]
    assert candidates[-1].quote == "Actually, our audience is dentists."
    assert len({candidate.subject for candidate in candidates}) == 5
    assert all(candidate.quote in body for candidate in candidates)


def test_automatic_scan_refuses_source_over_utf8_byte_bound(facts):
    from app.memory.extraction import capture_chat_message, quoted_candidates
    d = facts
    body = "Our audience is teachers. " + "已知来源" * 1400
    assert len(body.encode()) > 16000
    assert quoted_candidates(body) == []
    d.message("msg_large", body)
    with d.bind_private():
        assert capture_chat_message("msg_large") == []
        assert d.read() == []
