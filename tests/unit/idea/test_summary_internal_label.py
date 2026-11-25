"""The wire label joining founder answers never reaches what founders read; prompts keep it."""
import json
from types import SimpleNamespace

import pytest

from tests.integration.test_postgres_auth_security import service_modules

LABEL = "Clarification:"
# (vague opening, the answer that describes the product), then (clear opening, a side remark).
VAGUE = [
    ("Software for freight brokers", "It reads carrier invoices and flags overcharges before payment"),
    ("An app for new parents", "It tracks feeding times and reminds both parents when a bottle is due"),
]
CLEAR = [
    ("A tool that reconciles customs filings for import brokers", "Mostly the monthly audit"),
    ("An app that plans weekend hikes for families", "Mostly on Sunday mornings"),
]
JOINS = ["{}\nClarification: {}", "{} Clarification: {}"]  # new writes, and old rows a model flattened


@pytest.fixture(scope="module")
def app_modules(tmp_path_factory):
    with pytest.MonkeyPatch.context() as setup, service_modules("company_os"):
        setup.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path_factory.mktemp('summary-label')}/label.db")
        setup.setenv("LLM_OFFLINE", "1")
        from app.idea import agent, runner, service
        yield SimpleNamespace(agent=agent, runner=runner, service=service)


@pytest.mark.parametrize("join", JOINS)
@pytest.mark.parametrize("opening,answer", VAGUE)
def test_summary_shows_the_answer_that_describes_the_product(app_modules, join, opening, answer):
    text = app_modules.runner._understanding_summary(app_modules.agent.Understanding(offer=join.format(opening, answer)))
    assert text.strip() == f"- **What it does:** {answer}"


@pytest.mark.parametrize("join", JOINS)
@pytest.mark.parametrize("opening,remark", CLEAR)
def test_summary_keeps_a_clear_opening_without_pasting_the_remark(app_modules, join, opening, remark):
    text = app_modules.runner._understanding_summary(app_modules.agent.Understanding(offer=join.format(opening, remark)))
    assert text.strip() == f"- **What it does:** {opening}"


@pytest.mark.parametrize("opening,answer", VAGUE + CLEAR)
def test_unlabelled_text_is_untouched(app_modules, opening, answer):
    text = f"{opening}\n{answer}"
    assert app_modules.agent.founder_description(text) == text


@pytest.mark.parametrize("opening,answer", VAGUE)
def test_assumed_and_skipped_answers_are_not_the_description(app_modules, opening, answer):
    describe = app_modules.agent.founder_description
    assert describe(f"{answer}\nClarification: Assumed: {opening}") == answer
    assert describe(f"{answer}\nClarification: Skip this question") == answer
    assert describe(f"{answer}\nAudience: {opening}\nClarification: Not sure") == answer


@pytest.mark.parametrize("opening,remark", CLEAR)
def test_known_offer_from_a_joined_idea_has_no_label(app_modules, opening, remark):
    known = app_modules.runner._known_fields({"original_idea": f"{opening}\nClarification: {remark}",
        "understanding": {}, "question_fields_asked": ["offer"]})
    assert known["offer"] == opening


@pytest.mark.parametrize("opening,answer", VAGUE)
def test_core_clarification_output_is_clean(app_modules, opening, answer):
    agent, runner = app_modules.agent, app_modules.runner
    joined = f"{opening}\nClarification: {answer}"
    result = runner._core_clarification({"original_idea": joined, "project_intent": "startup",
        "question_fields_asked": ["offer", "customer"]},
        agent.Clarification(case="know", action="proposal", message="Review your idea.", understanding=agent.Understanding()))
    assert result.understanding.offer == answer


@pytest.mark.parametrize("opening,answer", VAGUE)
def test_old_row_reads_clean_and_is_never_rewritten(app_modules, opening, answer):
    joined = f"{opening}\nClarification: {answer}"
    stored = json.dumps({"offer": joined, "summary": joined, "_answered_fields": {"offer": joined}})
    rev = SimpleNamespace(id="r", rev=1, source="founder", summary=joined, structured=stored, diff_from_prev="{}", created_at=None)
    shown = app_modules.service.serialize_revision(rev)
    assert shown["summary"] == shown["structured"]["offer"] == shown["structured"]["summary"] == answer
    assert rev.structured == stored and rev.summary == joined


@pytest.mark.parametrize("opening,answer", VAGUE)
def test_new_revisions_store_clean_fields(app_modules, monkeypatch, opening, answer):
    from app.ventures import idea_page
    monkeypatch.setattr(idea_page, "schedule", lambda *_: None)
    added = []
    session = SimpleNamespace(add=added.append, flush=lambda: None, get=lambda *_: None)
    row = SimpleNamespace(id="s", workspace_id="w", venture_id="v", current_revision_id=None, updated_at=None)
    joined = f"{opening}\nClarification: {answer}"
    rev = app_modules.service._append_revision(session, row, {"offer": joined, "summary": joined,
        "_answered_fields": {"offer": joined}}, source="agent", summary=joined)
    assert LABEL not in json.loads(rev.structured)["offer"] and LABEL not in rev.summary
    assert json.loads(rev.structured)["_answered_fields"]["offer"] == joined
    assert json.loads(rev.structured)["offer"] == answer


@pytest.mark.parametrize("opening,answer", VAGUE)
def test_display_idea_is_clean_while_the_prompt_keeps_both_answers(app_modules, monkeypatch, opening, answer):
    from app.ventures import current_idea as module, startup_review
    joined = f"{opening}\nClarification: {answer}"
    revision = SimpleNamespace(summary=joined)
    monkeypatch.setattr(module, "current_revision", lambda *_: revision)
    monkeypatch.setattr(module, "revision_fields", lambda _: {"offer": joined})
    monkeypatch.setattr(module, "latest_founder_statement", lambda *_: "")
    monkeypatch.setattr(startup_review, "effective_fields", lambda _s, _v, fields: fields)
    venture = SimpleNamespace(raw_idea=joined)
    assert module.current_idea(venture, session=object(), for_display=True) == f"Offer: {answer}"
    prompt = module.current_idea(venture, session=object())
    assert opening in prompt and answer in prompt
    monkeypatch.setattr(module, "revision_fields", lambda _: {})
    revision.summary = ""
    assert module.current_idea(venture, session=object(), for_display=True) == answer
