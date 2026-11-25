from tests.company_os_harness import load_company_os_app

load_company_os_app()

import importlib  # noqa: E402

FACTS = {"paused": False, "gtm_entry": False, "active_jobs": False, "personal": False, "idea_confirmed": False,
         "idea_state": "reviewing", "plan_state": None, "dossier_state": "complete", "workspace_id": "ws"}


def _key(monkeypatch, live, **over):
    # resolve modules at call time: other tests reload the app package
    monkeypatch.setattr(importlib.import_module("app.ventures.state_brief"), "_website_section",
                        lambda *_: {"live": live, "waitlist_enabled": True})
    facts = {**FACTS, **over}
    facts["research_report_ready"] = facts["dossier_state"] in {"complete", "partial"}
    return importlib.import_module("app.ventures.next_step")._resolve("v", "/v/v", facts)["key"]


def test_finished_research_and_live_site_is_not_sent_back_to_idea_review(monkeypatch):
    assert _key(monkeypatch, True) == "plan.generate"


def test_unconfirmed_idea_without_live_site_keeps_idea_gate(monkeypatch):
    assert _key(monkeypatch, False) == "idea.review_validation"
    assert _key(monkeypatch, True, dossier_state=None) == "idea.review_validation"
