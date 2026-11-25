import pytest

from tests.integration.test_chat_first_idea_bridge import _chat_started_venture, _ideas, client as client  # noqa: PLC0414


@pytest.mark.parametrize("idea,answer", [
    ("A tool that triages alerts for risk teams", "Heads of fraud operations at mid-size banks"),
    ("Monitoring software for pipeline methane leaks", "Integrity engineers at gas pipeline operators"),
])
def test_typed_answer_counts_while_the_question_is_on_screen(client, monkeypatch, idea, answer):
    from app.chat import idea_bridge
    from app.chat.store import append_message

    venture_id = _chat_started_venture(client, idea)
    _, turns, _ = _ideas(venture_id)
    question_id = turns[-1].id
    monkeypatch.setattr(idea_bridge, "_chat_entry_deferred", lambda *args: False)
    monkeypatch.setattr(idea_bridge, "chat_entry_goal", lambda *args: False)
    append_message(venture_id, role="assistant", body_md="Here is something else you asked about.", kind="text")
    assert idea_bridge.pending_question(venture_id, answer) is None  # Charlie has moved on, so this is ordinary chat
    append_message(venture_id, role="assistant", body_md="Who is it for?", kind="idea_clarification_ready",
                   actions=[{"label": "Skip the questions", "action": "chat.reply", "payload": {"text": "Skip", "question_id": question_id}}])
    pending = idea_bridge.pending_question(venture_id, answer)
    assert pending and pending[1] == question_id
    assert idea_bridge.pending_question(venture_id, "find me customers") is None  # commands stay commands
    assert idea_bridge.pending_question(venture_id, "Who are my competitors in this market?") is None  # a question isn't an answer
