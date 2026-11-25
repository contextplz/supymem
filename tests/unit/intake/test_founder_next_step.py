"""Founder assets and automation share research-first journey decisions."""
import pytest
from tests.company_os_harness import load_company_os_app


@pytest.fixture()
def steps(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/intake.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    load_company_os_app()
    from app.ventures import next_step
    from app.tenancy import bind
    with bind(workspace_id="wsp_test", venture_id="ven_test", actor="usr_fixture"):
        yield next_step


@pytest.mark.parametrize("facts,expected", [
    ({"external_site": True}, "site.audit"),
    ({"external_site": True, "product_attempt": True, "accepted_product": False}, "site.audit"),
    ({"has_customers": True}, "research.start"),
    ({"intake_path": "gtm"}, "research.start"),
    ({"intake_path": "landing_gtm", "active_jobs": True}, "work.running"),
])
def test_existing_asset_never_offers_build(steps, facts, expected):
    result = steps._founder_resolve("ven_test", "/v/ven_test", facts)
    assert result["key"] == expected
    assert result["key"] != "site.build"
    assert result["href"].startswith("/v/ven_test/")


def test_pause_wins_over_existing_site(steps):
    result = steps._founder_resolve("ven_test", "/v/ven_test", {"paused": True, "external_site": True})
    assert result["key"] == "venture.paused"


@pytest.mark.parametrize("state,key", [(None, "research.start"), ("pending", "research.running"),
    ("running", "research.running"), ("partial", "research.review"), ("complete", "research.review")])
def test_research_only_stops_at_verdict(steps, state, key):
    result = steps._founder_resolve("ven_test", "/v/ven_test", {
        "intake_path": "research", "idea_confirmed": True, "dossier_state": state,
        "research_report_ready": state in {"partial", "complete"}})
    assert result["key"] == key


def test_personal_tool_does_not_enter_gtm(steps, monkeypatch):
    sentinel = {"key": "build.first"}
    monkeypatch.setattr(steps, "_resolve", lambda *args: sentinel)
    assert steps._founder_resolve("ven_test", "/v/ven_test", {
        "personal": True, "external_site": True, "has_customers": True}) is sentinel


def test_default_and_founder_share_stage_order(steps, monkeypatch):
    monkeypatch.setattr(steps, "_facts", lambda _id: {"workspace_id": "wsp_test", "external_site": True})
    monkeypatch.setattr(steps, "_resolve", lambda *args: {"key": "legacy"})
    assert steps.next_step("ven_test")["key"] == "site.audit"
    assert steps.next_step("ven_test") == steps.founder_next_step("ven_test")


@pytest.mark.parametrize("path", [None, "startup"])
@pytest.mark.parametrize("audience", ["Freight scheduling for distributors", "Weekend activity planners for families"])
def test_landing_page_uses_connected_tool_before_research(steps, monkeypatch, path, audience):
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    facts = {"workspace_id": "wsp_test", "gtm_entry": False, "active_jobs": False, "personal": False,
             "idea_confirmed": True, "idea_state": "confirmed", "plan_state": None, "dossier_state": None,
             "intake_path": path, "landing": False, "accepted_build": False, "raw_idea": audience}
    monkeypatch.setattr(steps, "_facts", lambda _id: facts)
    monkeypatch.setattr("app.pagebuilder.api.enabled", lambda: facts.get("coding_tool", False))
    monkeypatch.setattr("app.ventures.state_brief._website_section", lambda *_: {"live": False, "waitlist_enabled": False})
    monkeypatch.setattr("app.chat.execute.research_needs_start", lambda _id: True)
    monkeypatch.setattr("app.planning.store.list_current_tasks", lambda _id: [])
    for resolver in (steps.founder_next_step, steps.next_step):
        result = resolver("ven_test")
        assert result["key"] == "site.connect_tool"
        assert "Settings > Keys and connections > Coding tools" in result["detail"]
    facts.update(plan_state="approved", coding_tool=True, accepted_landing=False,
                 products=0, accepted_build=False)
    assert steps.founder_next_step("ven_test")["key"] == "site.build"
    assert steps.next_step("ven_test")["key"] == "site.build"
    facts["accepted_build"] = True
    assert steps.founder_next_step("ven_test")["key"] == "site.build"
    facts["accepted_landing"] = True
    assert steps.founder_next_step("ven_test")["key"] == "site.publish"
    assert steps.next_step("ven_test")["key"] == "site.publish"


def test_founder_fallback_preserves_one_argument_contract(steps, monkeypatch):
    def unreadable(_id):
        raise RuntimeError("Temporary fact read failure")
    monkeypatch.setattr(steps, "_facts", unreadable)
    fallback_calls = []
    def fallback(venture_id):
        fallback_calls.append(venture_id)
        return {"key": "venture.review"}
    monkeypatch.setattr(steps, "next_step", fallback)
    assert steps.founder_next_step("ven_test")["key"] == "venture.review"
    assert fallback_calls == ["ven_test"]


def test_existing_customers_find_next_channel_before_website_audit(steps):
    result = steps._founder_resolve("ven_test", "/v/ven_test", {
        "has_customers": True, "founder_stage": "building", "external_site": True,
        "idea_confirmed": True, "dossier_state": "complete", "research_report_ready": True})
    assert result["key"] == "gtm.outreach"
    assert result["cta_label"] == "Find customers"
    assert "paying customers" in result["detail"]


@pytest.mark.parametrize("active_jobs", [False, True])
@pytest.mark.parametrize("work", ["page", "research"])
def test_active_work_precedes_choices_even_without_generic_job_receipt(steps, monkeypatch, active_jobs, work):
    monkeypatch.setattr("app.pagebuilder.api.enabled", lambda: False)
    facts = {"workspace_id": "wsp_test", "idea_confirmed": True, "dossier_state": "complete",
             "active_jobs": active_jobs, "external_site": True, "page_skipped": True,
             "accepted_landing": True, "build_review_pending": True}
    facts.update({"page_build": "running"} if work == "page" else {"active_research": True})
    result = steps._founder_resolve("ven_test", "/v/ven_test", facts)
    assert result["key"] == ("site.building" if work == "page" else "research.running")
    assert result["href"].endswith("/site" if work == "page" else "/research")
    assert "alternatives" not in result


def test_next_step_carries_saved_customer_and_website_signals(steps, monkeypatch):
    monkeypatch.setattr(steps, "_facts", lambda _: {"workspace_id": "wsp_test", "has_customers": True,
        "external_site": True, "idea_confirmed": True, "dossier_state": "complete", "research_report_ready": True})
    result = steps.next_step("ven_test")
    assert result["has_customers"] and result["has_website"]
    assert result["cta_label"] == "Find customers"


def test_published_evidence_and_founder_stage_share_the_dashboard_receipt(steps, monkeypatch):
    record = {"stage": "outreach", "confirmed_by_founder": False,
              "evidence": [{"source": "publication", "link": "https://recalldesk.supymem.com"}]}
    monkeypatch.setattr(steps, "_facts", lambda _: {"workspace_id": "wsp_test", "idea_confirmed": True,
        "founder_stage": record["stage"], "founder_stage_record": record,
        "published_site_url": record["evidence"][0]["link"], "dossier_state": None})
    result = steps.next_step("ven_test")
    assert result == steps.founder_next_step("ven_test")
    assert result["key"] == "gtm.outreach"
    assert result["founder_stage"] == record
    assert result["href"] == "/gtm?venture=ven_test"


@pytest.mark.parametrize("over,key", [({"paused": True}, "venture.paused"),
    ({"active_jobs": True}, "work.running"), ({"active_research": True}, "research.running")])
def test_live_page_receipt_survives_higher_priority_work(steps, monkeypatch, over, key):
    monkeypatch.setattr(steps, "_facts", lambda _: {"workspace_id": "wsp_test", "founder_stage": "page_live",
        "published_site_url": "https://recalldesk.supymem.com", **over})
    result = steps.next_step("ven_test")
    assert result["key"] == key
    assert result["site_url"] == "https://recalldesk.supymem.com"
