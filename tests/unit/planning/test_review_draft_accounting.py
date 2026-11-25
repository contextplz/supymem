"""Review-only writing preserves terminal provider failures rather than templates."""
from __future__ import annotations

import asyncio

import pytest

from tests.unit.chat.test_assistant_safe_retries import (
    transport_boundary as transport_boundary,  # noqa: PLC0414
)
from tests.unit.chat.test_round3_matrix import venture as venture  # noqa: F401


@pytest.mark.parametrize("idea", [
    "A tool for warehouse teams to schedule loading bays.",
    "An app for hikers to plan weekend routes.",
])
def test_review_draft_unknown_usage_keeps_saved_pause_without_retry(venture, transport_boundary, idea):
    from app.core.local_mode import is_local
    from app.db.models import Venture
    from app.planning.execute import execute_planned_task
    from app.planning.store import enqueue_task, get_task
    from app.state import db

    from tests.unit.chat.test_assistant_safe_retries import response

    _, venture_id, _ = venture
    if is_local():
        from app.core.providers_local import save_settings

        save_settings(provider="openai", api_key="synthetic-review-key", models={"chat": "gpt-6.1-sol"})
    with db()() as session:
        session.get(Venture, venture_id).raw_idea = idea
        session.commit()
    transport_boundary.install([response(usage={})])
    task = enqueue_task(venture_id, {"title": "Draft an outreach email for review", "detail": "Do not send anything.", "runnable": True})
    result = asyncio.run(execute_planned_task(venture_id, task["id"]))
    assert result["status"] == "needs_you" and "paused" in result["pause_message"]
    assert "check your saved request" in result["pause_message"]
    assert len(transport_boundary.calls) == 1
    assert not transport_boundary.scopes[0].definitive_failure
    saved = get_task(venture_id, task["id"])
    assert saved["status"] == "open" and not saved["runnable"]
    assert saved["last_error"] == "needs_you:review_draft_provider_outcome_unknown"


@pytest.mark.parametrize("code,status", [
    ("provider_outcome_unknown", None), ("provider_down", 503), ("bad_api_key", 401),
])
@pytest.mark.parametrize("phase", ["draft", "pitch"])
def test_terminal_provider_failure_never_selects_a_template(venture, monkeypatch, code, status, phase):
    from app.autopilot import outreach
    from app.chat import assistant
    from app.credits import metered
    from app.planning import execute
    from llm_gateway.errors import ProviderCallError

    _, vid, _ = venture
    failure = ProviderCallError(code, "Synthetic private endpoint detail must not become a draft", status=status)
    async def complete(*args, **kwargs):
        raise failure
    def no_template(*args, **kwargs):
        pytest.fail("An unconfirmed paid failure is not a free successful draft")
    monkeypatch.setattr(assistant, "_complete", complete)
    if phase == "pitch":
        monkeypatch.setenv("LLM_OFFLINE", "0")
        monkeypatch.setattr(outreach, "saved_pitch", lambda _vid: "")
        monkeypatch.setattr(metered, "llm", complete)
    monkeypatch.setattr(execute, "_template_drafts", no_template)
    with pytest.raises(ProviderCallError) as caught:
        asyncio.run(execute._review_drafts(vid, "Draft outreach emails for review", "Do not send anything."))
    assert caught.value is failure


def test_provider_pause_is_not_withdrawn_as_an_old_posting_connection(venture):
    from app.chat.offers import _withdraw_stale_draft_gates
    from app.db.models import PlannedTask
    from app.planning.store import enqueue_task, get_task
    from app.state import db

    _, vid, _ = venture
    task = enqueue_task(vid, {"title": "Draft outreach emails for me to review", "detail": "Do not send anything.",
        "runnable": False})
    with db()() as session:
        row = session.get(PlannedTask, task["id"])
        row.last_error = "needs_you:review_draft_provider_outcome_unknown"
        session.commit()
    _withdraw_stale_draft_gates(vid)
    current = get_task(vid, task["id"])
    assert current["status"] == "open" and current["runnable"] is False
    assert current["last_error"] == "needs_you:review_draft_provider_outcome_unknown"
