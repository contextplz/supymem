import asyncio
import json

import pytest

from tests.unit.product.test_device_orders import orders
from tests.unit.product.test_device_browser_ai import configure
from tests.unit.product.test_device_order_api import api


def test_browser_suggestion_is_read_only_preparation_never_autopilot(api, monkeypatch):
    _, (svc, caller, _) = api
    from app.suggestions import proposals, service
    from app.autopilot import guard
    from app.db.supymem_models import Suggestion
    from core.clock import utcnow
    from app.tenancy import bind

    configure(monkeypatch)
    monkeypatch.setenv("DEVICE_DESKTOP_EXECUTION_ENABLED", "0")
    monkeypatch.setattr(svc, "_current_open_task", lambda *args, **kwargs: "task_one")
    monkeypatch.setattr(service, "_expire_stale", lambda *args: None)
    item = proposals._shape({"action_kind": "browser.task.prepare", "title": "Competitor check",
        "payload": {"url": "https://example.test/pricing", "name": "Example", "instruction": "send messages"}})
    assert item and item["requires_mac"]
    assert "browser.task.prepare" in proposals.EXECUTABLE_KINDS
    assert "browser.task.prepare" not in guard.AUTO_ACTIONS
    assert item["payload"] == {"url": "https://example.test/pricing", "name": "Example"}
    assert proposals._shape({"action_kind": "browser.task.prepare", "title": "bad", "payload": {"url": "http://localhost", "name": "bad"}}) is None
    with svc.db()() as session:
        session.add(Suggestion(id="sug_browser", venture_id="v_one", person_id="founder", title=item["title"],
            why="Saved competitor", plan_text=item["plan_text"], est_credits=0, action_kind=item["action_kind"],
            payload_json=json.dumps(item["payload"]), requires_mac=True, status="new", dedupe_key="browser", created_at=utcnow()))
        session.commit()
    with bind(workspace_id="w_one", venture_id="v_one", actor="founder"):
        with pytest.raises(PermissionError):
            asyncio.run(service.accept("sug_browser", "founder"))
        result = asyncio.run(service.accept("sug_browser", "founder", caller=caller))
    assert result["result_state"] == "done"
    assert result["result_href"].startswith("/v/v_one/device-work-orders/dwo_")
    saved = svc.retrieve("v_one", caller, result["result_href"].split("/")[-1])
    assert saved["state"] == "pending" and not saved.get("claimed_at")
    assert saved["manifest"]["actions"] == ["browser.read"]
    assert saved["manifest"]["general_browser_verification"]["allow_type"] is False


def test_browser_suggestion_uses_a_named_competitor_from_current_saved_research(api, monkeypatch):
    _, (svc, _, _) = api
    from app.suggestions import proposals
    from app.db.supymem_models import ResearchDossier
    from core.clock import utcnow

    configure(monkeypatch)
    monkeypatch.setattr("app.suggestions.service._owner", lambda vid: "founder")
    with svc.db()() as session:
        session.add(ResearchDossier(id="research_browser", workspace_id="w_one", venture_id="v_one", revision_id="rev_current",
            kind="deep", state="complete", summary="{}", created_at=utcnow()))
        session.commit()
    monkeypatch.setattr("app.research.service.get_dossier", lambda did: {"summary": {"founder_report": {"dimensions": {
        "competition": {"support": [{"source_id": "source_competitor", "name": "Named competitor", "url": "https://example.test/pricing"}]}}}}})
    item = proposals._browser_suggestion("v_one", "w_one", "rev_current")
    assert item["payload"] == {"name": "Named competitor", "url": "https://example.test/pricing"}
    assert item["action_kind"] == "browser.task.prepare"
    assert proposals._browser_suggestion("v_one", "w_other", "rev_current") is None
    assert proposals._browser_suggestion("v_one", "w_one", "rev_old") is None
