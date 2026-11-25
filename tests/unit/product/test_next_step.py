"""The single venture next-step resolver keeps the Cockpit's stage ordering."""
import pytest

from tests.company_os_harness import load_company_os_app

FACTS = dict(workspace_id="w1", gtm_entry=False, active_jobs=False, idea_state="open", idea_confirmed=False,
             personal=False, plan_state=None, dossier_state=None, products=0, accepted_build=False, accepted_landing=False,
             product_attempt=False, accepted_product=False)


def test_u77_booked_call_overrides_running_gtm_next_step(ns):
    from app.api.dashboard import _handoff_next_step
    step = ns._founder_resolve("v1", "/v/v1", {**FACTS, "active_jobs": True, "first_call_booked": True})
    assert step["key"] == "gtm.results"
    assert step["title"] == "Prepare your booked call and record what you learn"
    assert step["href"] == "/gtm?venture=v1"
    assert _handoff_next_step({"state": "completed", "action": {"label": "Review reachable leads", "href": "/gtm?venture=v1"}}, step) == step


@pytest.fixture
def ns(monkeypatch):
    load_company_os_app()
    from app.chat import execute
    from app.planning import store
    from app.ventures import next_step, state_brief

    monkeypatch.setattr(execute, "research_needs_start", lambda _v: True)
    monkeypatch.setattr(store, "list_current_tasks", lambda _v: [])
    monkeypatch.setattr(state_brief, "_website_section", lambda *_a: {"live": True, "waitlist_enabled": False})
    return next_step


def key(ns, **over):
    over.setdefault("research_report_ready", over.get("dossier_state") in {"complete", "partial"})
    return ns._resolve("v1", "/v/v1", {**FACTS, **over}, page_first=True)


def test_interrupted_research_has_a_fresh_run_action_and_respects_skip(ns, monkeypatch):
    facts = {**FACTS, "idea_confirmed": True, "idea_state": "confirmed", "research_interrupted": True,
             "dossier_state": "failed"}
    step = ns._founder_resolve("v1", "/v/v1", facts)
    assert step["key"] == "research.retry" and step["cta_label"] == "Run research again"
    assert step["href"] == "/v/v1/research" and "skip research for now" in step["detail"]
    assert "reconciliation" not in str(step)
    monkeypatch.setattr(ns, "_customers_step", lambda _vid: {"key": "gtm.begin"})
    skipped = ns._founder_resolve("v1", "/v/v1", {**facts,
        "journey_overrides": {"skipped": {"research": {}}, "target": "find_customers"}})
    assert skipped["key"] == "gtm.begin"


def test_stage_transitions(ns, monkeypatch):
    assert key(ns)["key"] == "idea.clarify"
    assert key(ns, idea_state="validating")["key"] == "idea.validating"
    review = key(ns, idea_state="reviewing")
    assert (review["key"], review["cta_label"], review["href"]) == ("idea.review_validation", "Review idea", "/v/v1?chat=1")
    confirmed = dict(idea_state="confirmed", idea_confirmed=True)
    start = key(ns, **confirmed)
    assert (start["key"], start["chat_action"]) == ("research.start", "research.start")
    assert key(ns, **confirmed, dossier_state="running")["key"] == "research.running"
    assert key(ns, **confirmed, dossier_state="complete")["key"] == "plan.generate"
    assert key(ns, **confirmed, dossier_state="complete")["chat_action"] == "plan.generate"
    assert key(ns, **confirmed, plan_state="founder_review")["key"] == "plan.review"
    retry = key(ns, **confirmed, plan_state="draft", plan_has_proposal=False)
    assert (retry["key"], retry["chat_action"]) == ("plan.retry", "plan.generate")
    assert key(ns, **confirmed, plan_state="draft", plan_has_proposal=False, active_jobs=True)["key"] == "work.running"
    assert key(ns, **confirmed, plan_state="founder_review")["chat_action"] == "plan.approve"
    approved = dict(**confirmed, plan_state="approved")
    assert key(ns, **approved)["key"] == "build.first"
    assert key(ns, **approved, products=1, accepted_build=True, product_attempt=True)["key"] == "build.finish_product"
    done = dict(**approved, products=1, accepted_build=True, accepted_landing=True, landing=True)
    monkeypatch.setattr("app.ventures.state_brief._website_section", lambda *_a: {"live": False, "waitlist_enabled": False})
    assert (key(ns, **done)["key"], key(ns, **done)["chat_action"]) == ("site.publish", "site.publish")
    monkeypatch.setattr("app.ventures.state_brief._website_section", lambda *_a: {"live": False, "waitlist_enabled": True})
    assert key(ns, **done)["chat_action"] == "site.publish"
    monkeypatch.setattr("app.ventures.state_brief._website_section", lambda *_a: {"live": True, "waitlist_enabled": True})
    assert key(ns, **done)["key"] == "gtm.outreach"


def test_gtm_entry_drafts_outreach_never_builds(ns):
    step = key(ns, gtm_entry=True, idea_confirmed=True, plan_state="approved")
    assert (step["key"], step["chat_action"]) == ("gtm.outreach", "outreach.draft")


def test_landing_review_routes_to_website_but_product_review_stays_in_builds(ns):
    facts = dict(idea_state="confirmed", idea_confirmed=True, plan_state="approved",
                 build_review_pending=True, dossier_state="complete")
    href = "/v/v1/site?app=landing&run=run#build-review"
    assert key(ns, **facts, landing_review_href=href)["href"] == href
    assert key(ns, **facts)["href"] == "/v/v1/builds"


def test_founder_next_step_never_sends_landing_failure_to_product_builds(ns):
    facts = {**FACTS, "idea_confirmed": True, "plan_state": "approved", "dossier_state": "complete",
        "landing_build": {"state": "failed", "href": "/v/v1/site?app=a&run=r#build-review",
                          "page": {"available": True, "qa_note": "QA notes remain."}}}
    assert ns._founder_resolve("v1", "/v/v1", facts)["href"] == facts["landing_build"]["href"]
    assert ns._founder_resolve("v1", "/v/v1", facts)["cta_label"] == "Review landing page"
    facts["landing_build"]["page"]["available"] = False
    assert ns._founder_resolve("v1", "/v/v1", facts)["href"] == "/v/v1/site?build=1"


@pytest.mark.parametrize("waitlist_enabled", [False, True])
@pytest.mark.parametrize("build_state", ["succeeded", "failed"])
def test_live_landing_opens_customers_instead_of_private_review(ns, waitlist_enabled, build_state):
    facts = {**FACTS, "idea_confirmed": True, "founder_stage": "outreach",
             "waitlist_enabled": waitlist_enabled,
             "landing_build": {"state": build_state, "href": "/v/v1/site#build-review",
                               "page": {"available": True, "live_url": "https://recalldesk.supymem.com",
                                        "qa_note": "Old preview notes."}}}
    step = ns._founder_resolve("v1", "/v/v1", facts)
    assert step["key"] == "gtm.outreach"
    assert step["title"] == "Your landing page is live, find your first customers"
    assert step["cta_label"] == "Find customers"
    assert step["href"] == "/gtm?venture=v1"
    assert step["site_url"] == facts["landing_build"]["page"]["live_url"]
    assert step["chat_action"] == "outreach.draft"
    assert "Old preview notes" not in step["detail"]


def test_page_choice_offers_equal_alternatives_and_skip_routes_to_customers(ns, monkeypatch):
    monkeypatch.setattr("app.ventures.state_brief._website_section", lambda *_a: {"live": False, "waitlist_enabled": False})
    monkeypatch.setattr("app.pagebuilder.api.enabled", lambda: True)
    confirmed = dict(idea_confirmed=True, idea_state="confirmed", dossier_state="complete")
    step = key(ns, **confirmed)
    assert step["key"] == "site.build"
    assert [(a["choice"], a["href"]) for a in step["alternatives"]] == [
        ("build", "/v/v1/site?build=1"), ("skip", "/gtm?venture=v1")]
    assert [a.get("recommended", False) for a in step["alternatives"]] == [True, False]
    # A saved plan cannot send a founder who skipped the page back into building.
    for plan_state in (None, "founder_review", "approved"):
        assert key(ns, **confirmed, page_skipped=True, plan_state=plan_state)["key"] == "gtm.outreach"


def test_page_step_is_a_real_build_or_connect_tool(ns, monkeypatch):
    monkeypatch.setenv("SUPYMEM_MODE", "hosted")
    monkeypatch.setattr("app.ventures.state_brief._website_section", lambda *_a: {"live": False, "waitlist_enabled": False})
    def key(ns, **over):  # founder-facing callers pass page_first=True
        return ns._resolve("v1", "/v/v1", {**FACTS, **over}, page_first=True)
    c = dict(idea_state="confirmed", idea_confirmed=True)
    assert key(ns, **c)["key"] == "research.start"  # the build implements the approved plan's page contract
    approved = dict(**c, dossier_state="complete", research_report_ready=True, plan_state="approved")
    connect = key(ns, **approved)
    assert (connect["key"], connect["title"]) == ("site.connect_tool", "Connect Claude Code or Codex")
    build = key(ns, **approved, coding_tool=True)
    assert (build["key"], build["title"], build["href"]) == ("site.build", "Build your landing page", "/v/v1/site")
    assert "Claude Code or Codex" in build["detail"] and build["chat_action"] is None
    publish = key(ns, **approved, coding_tool=True, accepted_build=True, accepted_landing=True)
    assert (publish["key"], publish["chat_action"]) == ("site.publish", "site.publish")
    assert key(ns, **approved, coding_tool=True, build_review_pending=True)["key"] == "build.review"
    assert key(ns, **approved, coding_tool=True, personal=True)["key"] != "site.build"
    assert ns._resolve("v1", "/v/v1", {**FACTS, **approved, "coding_tool": True}, page_first=False)["key"] == "build.first"
    monkeypatch.setattr("app.ventures.state_brief._website_section", lambda *_a: {"live": True, "waitlist_enabled": True})
    assert key(ns, **approved, coding_tool=True)["key"] == "build.first"
    assert key(ns, gtm_entry=True, **approved)["key"] == "gtm.outreach"  # gtm entry never requires a build


def test_finished_validation_is_review_not_validating(ns):
    done = key(ns, idea_state="validating", validation_done=True)
    assert done["key"] == "idea.review_validation"
    assert key(ns, idea_state="validating", question_pending=True)["key"] == "idea.clarify"
    assert key(ns, idea_state="validating")["key"] == "idea.validating"
    assert key(ns, idea_state="validating", active_jobs=True)["key"] == "work.running"


def test_precedence_gtm_entry_then_active_work(ns):
    assert key(ns, gtm_entry=True, active_jobs=True)["chat_action"] == "outreach.draft"
    assert key(ns, active_jobs=True, idea_confirmed=True)["key"] == "work.running"


def test_open_plan_task_offers_work_start(ns, monkeypatch):
    from app.planning import store

    monkeypatch.setattr(store, "list_current_tasks", lambda _v: [{"id": "t1", "title": "Build landing", "source": "approved_plan:p1"}])
    step = key(ns, idea_confirmed=True, plan_state="approved")
    assert (step["key"], step["chat_action"], step["payload"]) == ("work.next_task", "work.start", {"task_id": "t1"})


def test_unknown_venture_gives_fallback_not_exception(ns, monkeypatch):
    monkeypatch.setattr(ns, "_facts", lambda _v: (_ for _ in ()).throw(KeyError("nope")))
    step = ns.next_step("missing")
    assert step["key"] == "venture.review" and step["chat_action"] is None


def test_guided_answer_returns_confirmation_card_for_chat_action(ns):
    from app.chat.journey import guided_answer

    step = key(ns, idea_confirmed=True)
    reply = guided_answer("v1", {"idea": {"name": "Co"}, "next_step": step},
                          {"state": "idle", "activity": "", "action": None})
    assert reply["kind"] == "confirmation"
    assert reply["actions"][0]["confirmation"]["action"] == "research.start"


def test_founder_research_keeps_page_and_customer_choices_without_a_plan(ns, monkeypatch):
    monkeypatch.setattr("app.pagebuilder.api.enabled", lambda: True)
    monkeypatch.setattr("app.ventures.state_brief._website_section", lambda *_: {"live": False, "waitlist_enabled": False})
    done = dict(idea_state="confirmed", idea_confirmed=True, dossier_state="complete", research_report_ready=True)
    founder = lambda **over: ns._founder_resolve("v1", "/v/v1", {**FACTS, **done, **over})
    assert founder()["key"] == "site.build"
    assert founder(founder_stage="traction")["key"] == "experiment.plan"
