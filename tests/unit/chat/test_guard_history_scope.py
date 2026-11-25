"""Text-only classification has no conversation-read authority."""
import pytest

from tests.company_os_harness import load_company_os_app


@pytest.mark.parametrize("text", ["yes", "no", "cancel", "do it", "ok", "", "   "])
def test_text_only_guard_never_reads_history(monkeypatch, text):
    load_company_os_app()
    from app.chat import guard, store

    def forbidden(*_args, **_kwargs):
        raise AssertionError("an empty text-only scope must not read a conversation")
    monkeypatch.setattr(store, "list_messages", forbidden)
    reply = guard.guard_reply("", text)
    assert reply["kind"] == "text"
    assert "Nothing is waiting" in reply["body_md"] if text.strip() else "didn't get a message" in reply["body_md"]


def test_nonempty_scope_history_refusal_is_not_masked(monkeypatch):
    load_company_os_app()
    from app.chat import guard, store

    def retired(*_args, **_kwargs):
        raise ValueError("chat_conversation_not_found")
    monkeypatch.setattr(store, "list_messages", retired)
    with pytest.raises(ValueError, match="chat_conversation_not_found"):
        guard.guard_reply("ven_retired", "yes")


def test_model_original_text_guard_has_no_history_authority(monkeypatch):
    load_company_os_app()
    from app.chat import llm_router, store

    def forbidden(*_args, **_kwargs):
        raise AssertionError("an original-text classifier must not read a conversation")
    monkeypatch.setattr(store, "list_messages", forbidden)
    decision = llm_router._validate({"acts": [{"intent": "approve", "text": "do it"}], "clarify": ""}, "do it")
    # The text-only safety classifier must also keep its existing refusal to
    # turn this bare phrase into model-granted approval authority.
    assert decision.acts == [("assistant", "do it")]


def test_router_eval_setup_failure_restores_replay_environment(monkeypatch):
    """A failed eval setup cannot turn later isolated handlers into replay routes."""
    import os
    from tests.unit.chat import test_chat_router_eval as evaluation

    monkeypatch.setenv("CHAT_ROUTER_MODE", "off")
    monkeypatch.setenv("CHAT_ROUTER_DECISIONS", "original-decisions")
    monkeypatch.setenv("CHAT_ROUTER_RECORD", "original-record")

    def failed(*_args, **_kwargs):
        raise RuntimeError("synthetic_eval_setup_failure")
    monkeypatch.setattr(evaluation.runner, "run_all", failed)
    with pytest.raises(RuntimeError, match="synthetic_eval_setup_failure"):
        evaluation._replay_cases([
            ("question", "How should a warehouse track stock?", ["ANSWER"]),
            ("question", "How can a family plan meals?", ["ANSWER"]),
        ])
    assert os.environ["CHAT_ROUTER_MODE"] == "off"
    assert os.environ["CHAT_ROUTER_DECISIONS"] == "original-decisions"
    assert os.environ["CHAT_ROUTER_RECORD"] == "original-record"
