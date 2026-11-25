import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from tests.unit.chat.test_next_moves import _saved_state, env, env_fixture


@pytest.mark.parametrize("idea", ["Purchase orders for retailers", "Swimming lessons for families"])
def test_ready_page_milestone_has_preview_and_export(env, monkeypatch, idea):
    from app.chat import milestones, site_actions
    from app.core import local_mode
    from app import state

    row = SimpleNamespace(venture_id="venture", workspace_id="workspace", payload_json=json.dumps({
        "idea_summary": idea, "_runtime": {"state": "ready"}, "result": {"bundles": []}}))
    session = MagicMock()
    session.get.return_value = row
    session.__enter__.return_value = session
    monkeypatch.setattr(state, "db", lambda: lambda: session)
    monkeypatch.setattr(local_mode, "is_local", lambda: True)
    posted = []
    monkeypatch.setattr(milestones, "post_milestone", lambda *args, **kwargs: posted.append(kwargs))
    site_actions.page_built("saved-page")
    assert len(posted) == 1
    actions = {action["label"]: action for action in posted[0]["actions"]}
    assert actions["Open preview"]["href"] == "/api/ventures/venture/pagebuilder/jobs/saved-page/preview"
    assert actions["Export"]["href"] == "/api/ventures/venture/pagebuilder/jobs/saved-page/source"
    assert posted[0]["requested"] is True


@pytest.mark.parametrize("idea", ["Purchase orders for retailers", "Swimming lessons for families"])
@pytest.mark.parametrize("research_state", ["missing", "running", "ready"])
def test_ready_page_offers_sharing_signups_and_customers_before_research(env, monkeypatch, idea, research_state):
    from app.chat import actions, build_moves, next_moves

    _, venture_id, _ = env
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    saved = {**_saved_state(idea), "idea": {"confirmed": True, "idea_summary": idea},
             "idea_state": "confirmed", "questions_pending": False, "page": False, "page_build": "ready",
             "research": {"present": research_state == "ready"}, "research_running": research_state == "running",
             "priority_step": {"key": "research.start", "title": "Research your market", "detail": "Check demand",
                               "cta_label": "Start research", "href": f"/v/{venture_id}/research"}}
    monkeypatch.setattr(next_moves, "_snapshot", lambda _: saved)
    monkeypatch.setattr(next_moves, "_blocked_actions", lambda _: set())
    monkeypatch.setattr(next_moves, "_fresh_recommendation", lambda _: None)
    monkeypatch.setattr(next_moves, "experiments_autopilot_enabled", lambda: False)
    monkeypatch.setattr(build_moves, "recommendation", lambda *_: None)
    monkeypatch.setattr(actions, "disabled_reason", lambda _: None)
    card = next_moves.compose(venture_id, requested=True)
    assert [action["label"] for action in card["actions"]] == [
        "Set up publishing and signups", "Find customers",
        "Pressure-test with research" if research_state == "missing" else "Review your research"]
    assert card["actions"][0]["href"] == "/settings/local#publishing-title"
    assert "private page is ready" in card["body_md"]
    assert "choose Set up publishing and signups" in card["body_md"]
