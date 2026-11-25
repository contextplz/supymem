"""Founder progress uses saved evidence and shares automation's primary step."""
import json

import pytest

from tests.company_os_harness import load_company_os_app


@pytest.fixture()
def stages(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/stage.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    load_company_os_app()
    from app.ventures import stage
    return stage


@pytest.fixture()
def journey_scope(stages):
    from app.tenancy import bind
    with bind(workspace_id="wsp_fixture", venture_id="ven_stage", actor="usr_fixture"):
        yield


@pytest.mark.parametrize("signals,expected", [
    ({}, "exploring"), ({"extracted": {"idea": "Booking software"}}, "idea"),
    ({"extracted": {"repo_url": "https://github.com/example/product"}}, "building"),
    ({"extracted": {"existing_site_url": "https://product.example.com"}, "entry_goal": "gtm"}, "page_live"),
    ({"contacts": 50, "live_url": "https://product.example.com"}, "outreach"),
    ({"signups": 1, "contacts": 50}, "traction"),
    ({"extracted": {"has_customers": True}}, "traction"),
])
def test_evidence_rules(stages, signals, expected):
    assert stages.derive_stage(signals) == expected


@pytest.mark.parametrize("body,kind,venture,expected", [
    ('{"extracted":{"has_customers":true}}', "intake_decision", "ven_stage", True),
    ('{"extracted":{"has_customers":false}}', "intake_decision", "ven_stage", False),
    ('{"extracted":{"has_customers":"true"}}', "intake_decision", "ven_stage", False),
    ('{"extracted":{"has_customers":true}}', "other", "ven_stage", False),
    ('{"extracted":{"has_customers":true}}', "intake_decision", "ven_other", False),
    ('{"extracted":[]}', "intake_decision", "ven_stage", False),
    ('bad json', "intake_decision", "ven_stage", False),
])
def test_existing_customer_signal_uses_only_saved_scoped_intake(stages, body, kind, venture, expected):
    from types import SimpleNamespace
    session = SimpleNamespace(get=lambda *_: SimpleNamespace(body_md=body, kind=kind, venture_id=venture))
    assert stages.has_existing_customers(session, "ven_stage") is expected


@pytest.mark.parametrize("idea,expected", [
    ("BrewBox: espresso bean subscription, we have 140 paying subscribers in the US and Canada", True),
    ("We have 2,000 newsletter subscribers", False),
    ("We have no paying subscribers yet", False),
    ("I plan to get paying subscribers", False),
])
def test_founder_words_count_when_intake_missed_existing_customers(stages, idea, expected):
    from types import SimpleNamespace
    session = SimpleNamespace(get=lambda *_: SimpleNamespace(body_md='{"extracted":{}}', kind="intake_decision",
                                                             venture_id="ven_stage", raw_idea=idea))
    assert stages.has_existing_customers(session, "ven_stage") is expected


@pytest.mark.parametrize("old,expected", list({"unknown": "exploring", "idea": "idea", "code": "building",
    "landing": "page_live", "live": "page_live", "customers": "traction"}.items()))
def test_legacy_vocabulary(stages, old, expected):
    assert stages.derive_stage({"extracted": {"stage": old}}) == expected
    assert stages.OLD_STAGES[old] == expected


@pytest.mark.parametrize("url,expected", [("https://recalldesk.supymem.com", "page_live"),
    ("https://127.0.0.1/preview", "idea"), ("file:///private/preview.html", "idea")])
def test_publication_receipt_drives_stage_and_next_step_without_waitlist_or_research(stages, monkeypatch, url, expected):
    from app.authz.workspace import ensure_subject_person
    from app.db.models import Venture
    from app.db.supymem_models import Deployment, FounderApp
    from app.state import db
    from app.tenancy import bind
    from app.ventures import next_step
    from app.workspaces.service import create_workspace
    from core.clock import utcnow

    actor = ensure_subject_person("publication_fixture")
    workspace = create_workspace(kind="company", name="Publication fixture", owner_id=actor)
    now, vid = utcnow(), "ven_publication_stage"
    with db()() as session:
        session.add(Venture(id=vid, workspace_id=workspace.id, name="RecallDesk", raw_idea="Rebook patients",
                            stage="draft", created_at=now, updated_at=now))
        session.flush()
        session.add(FounderApp(id="app_publication_stage", workspace_id=workspace.id, venture_id=vid,
                              slug="publication-fixture", name="Landing", kind="static", created_at=now, updated_at=now))
        session.flush()
        session.add(Deployment(id="dep_publication_stage", app_id="app_publication_stage", workspace_id=workspace.id,
                               venture_id=vid, build_artifact_id="artifact_fixture", target="supymem_static",
                               state="live", version=1, url=url, live_at=now))
        session.commit()
    record = stages.get_stage(vid)
    assert record["stage"] == expected
    assert any(item["source"] == "publication" for item in record["evidence"]) == (expected == "page_live")
    if expected == "page_live":
        monkeypatch.setattr(next_step, "_coding_tool", lambda _: False)
        with bind(workspace_id=workspace.id, venture_id=vid, actor=actor):
            facts = next_step._facts(vid)
            assert facts["published_site_url"] == url
            step = next_step.next_step(vid)
        assert step["founder_stage"] == record
        from app.core.local_mode import is_local

        if is_local():
            assert step["key"] == "venture.context" and step["cta_label"] == "Review my answers here"
            assert step["href"] == f"/v/{vid}/context"
            return
        assert step["key"] == "gtm.outreach" and step["site_url"] == url
        assert step["cta_label"] == "Find customers"
        assert step["href"] == f"/gtm?venture={vid}"


@pytest.mark.parametrize("answer", ["No prototype yet", "Nothing built", "Planning to build a prototype", "Not talked to anyone", "0 contacts", "Haven't built it", "Skip"])
def test_absent_or_future_assets_do_not_claim_progress(stages, answer):
    assert not stages._present_answer(answer)


def test_confirmation_persists_until_new_scoped_evidence(stages, monkeypatch):
    from app.db.models import GtmContact, Venture, VentureDocument
    from app.authz.workspace import ensure_subject_person
    from app.workspaces.service import create_workspace
    from app.state import db
    from core.clock import utcnow

    actor = ensure_subject_person("founder")
    workspace = create_workspace(kind="company", name="Stage test", owner_id=actor)
    now = utcnow()
    monkeypatch.setattr("app.ventures.state_brief._website_section", lambda *_: {"live": False})
    with db()() as session:
        session.add(Venture(id="ven_stage", workspace_id=workspace.id, name="Booking", raw_idea="A booking tool",
                            stage="draft", autonomy_level="L1", created_at=now, updated_at=now))
        session.commit()
    first = stages.reassess("ven_stage")
    assert first["stage"] == "idea"
    confirmed = stages.confirm_stage("ven_stage", "building")
    assert confirmed["confirmed_by_founder"] is True
    assert stages.confirm_stage("ven_stage", "building") == confirmed
    assert stages.get_stage("ven_stage")["stage"] == "building"
    with db()() as session:
        session.add(GtmContact(id="gtc_other", workspace_id=workspace.id, email="other@example.com",
            source="venture:ven_other:founder_import", created_at=now, stage_changed_at=now))
        session.commit()
    assert stages.get_stage("ven_stage")["stage"] == "building"
    with db()() as session:
        session.add(GtmContact(id="gtc_stage", workspace_id=workspace.id, email="stage@example.com",
            source="venture:ven_stage:founder_import", created_at=now, stage_changed_at=now))
        session.commit()
    latest = stages.reassess("ven_stage")
    assert latest["stage"] == "outreach" and latest["confirmed_by_founder"] is False
    assert latest["history"][-1]["previous"] == "building"
    assert all(item["timestamp"] for item in latest["evidence"])
    with db()() as session:
        saved = session.get(VentureDocument, "founder_stage:ven_stage")
        assert json.loads(saved.body_md)["stage"] == "outreach"
    assert stages.get_stage("ven_stage") == latest


@pytest.mark.parametrize("stage,over,expected", [
    ("page_live", {"external_site": True}, "site.audit"),
    ("page_live", {"external_site": True, "site_audited": True}, "research.start"),
    ("page_live", {"external_site": True, "site_audited": True, "dossier_state": "complete"}, "gtm.outreach"),
    ("outreach", {}, "research.start"),
    ("outreach", {"dossier_state": "complete"}, "gtm.outreach"),
    ("traction", {"dossier_state": "complete"}, "experiment.plan"),
    ("building", {}, "research.start"),
])
def test_public_resolvers_agree(stages, journey_scope, monkeypatch, stage, over, expected):
    from app.ventures import next_step
    facts = {"workspace_id": "wsp_fixture", "founder_stage": stage, "idea_confirmed": True, "intake_path": "startup", **over}
    facts["research_report_ready"] = facts.get("dossier_state") in {"complete", "partial"}
    monkeypatch.setattr(next_step, "_facts", lambda _: facts)
    automated, founder = next_step.next_step("ven_stage"), next_step.founder_next_step("ven_stage")
    assert automated == founder
    assert automated["key"] == expected
    assert automated["key"] != "site.build"
    if expected in {"site.audit", "experiment.plan"}:
        from app.autopilot.lifecycle import _step_move
        move = _step_move(automated, "ven_stage")
        assert move.human and move.human_href == automated["href"] and not move.wait


def test_pause_and_active_work_are_not_bypassed(stages, journey_scope, monkeypatch):
    from app.ventures import next_step
    facts = {"workspace_id": "wsp_fixture", "founder_stage": "page_live", "external_site": True, "paused": True}
    monkeypatch.setattr(next_step, "_facts", lambda _: facts)
    assert next_step.next_step("ven_stage")["key"] == "venture.paused"
    facts.update(paused=False, active_jobs=True)
    assert next_step.next_step("ven_stage")["key"] == "work.running"
    facts.update(founder_stage="idea", gtm_entry=True, idea_confirmed=True, dossier_state="complete")
    assert next_step.next_step("ven_stage")["key"] == "work.running"


def test_existing_asset_without_reviewed_idea_reaches_clarification(stages, journey_scope, monkeypatch):
    from app.ventures import next_step
    facts = {"founder_stage": "outreach", "intake_path": "gtm", "gtm_entry": True, "idea_confirmed": False,
             "idea_state": None, "personal": False, "active_jobs": False, "workspace_id": "wsp_fixture"}
    monkeypatch.setattr(next_step, "_facts", lambda _: facts)
    step = next_step.next_step("ven_stage")
    assert step["key"] == "idea.clarify" and step["href"] == "/v/ven_stage/idea"


def test_initial_question_keeps_specific_progress_before_other_active_work(stages, journey_scope, monkeypatch):
    from app.ventures import next_step
    facts = {"workspace_id": "wsp_fixture", "founder_stage": "idea", "first_question_pending": True, "active_jobs": True,
             "idea_confirmed": False, "personal": True, "gtm_entry": True}
    monkeypatch.setattr(next_step, "_facts", lambda _: facts)
    step = next_step.next_step("ven_stage")
    assert step["key"] == "work.running" and step["title"] == "Preparing your first question"


def test_unscoped_guidance_fails_closed_before_reading_facts(stages, monkeypatch):
    from app.ventures import next_step
    monkeypatch.setattr(next_step, "_facts", lambda _: pytest.fail("Unscoped guidance must not read private facts"))
    result = next_step.next_step("ven_stage")
    assert result["key"] == "venture.review" and result["chat_action"] is None


def test_approved_current_work_and_human_review_precede_optional_stage_actions(stages, journey_scope, monkeypatch):
    from app.ventures import next_step
    from tests.unit.product.test_next_step import FACTS
    facts = {**FACTS, "workspace_id": "wsp_fixture", "idea_confirmed": True, "idea_state": "confirmed",
             "dossier_state": "complete", "research_report_ready": True,
             "plan_state": "approved", "plan_id": "plan_fixture", "plan_current": True,
             "founder_stage": "outreach", "external_site": True, "site_audited": True, "gtm_entry": True}
    monkeypatch.setattr(next_step, "_facts", lambda _: facts)
    monkeypatch.setattr("app.planning.store.list_current_tasks", lambda _: [{"id": "stale_work", "title": "Old plan work",
        "source": "approved_plan:old_plan", "runnable": True, "tags": ["capability:build.run", "plan_task:stale_task"]},
        {"id": "work_fixture", "title": "Build the approved product",
        "source": "approved_plan:plan_fixture", "runnable": True, "tags": ["capability:build.run", "plan_task:task_fixture"]}])
    step = next_step.next_step("ven_stage")
    assert step["key"] == "work.next_task" and step["chat_action"] == "build.prepare"
    assert step["payload"] == {"plan_task_id": "task_fixture"}
    assert step == next_step.founder_next_step("ven_stage")
    facts.update(external_site=False, accepted_landing=True, accepted_current_landing=True, accepted_build=True)
    assert next_step.next_step("ven_stage")["key"] == "work.next_task"
    facts["build_review_pending"] = True
    review = next_step.next_step("ven_stage")
    assert review["key"] == "build.review" and review["chat_action"] is None
    from app.autopilot.lifecycle import _step_move
    assert _step_move(review, "ven_stage").human


def test_old_approved_plan_cannot_resume_after_new_idea_research(stages, journey_scope, monkeypatch):
    from app.ventures import next_step
    from tests.unit.product.test_next_step import FACTS
    facts = {**FACTS, "workspace_id": "wsp_fixture", "idea_confirmed": True, "idea_state": "confirmed",
             "dossier_state": "complete", "research_report_ready": True,
             "plan_state": "approved", "plan_id": "old_plan", "plan_current": False,
             "founder_stage": "idea", "page_build": "ready", "coding_tool": True}
    monkeypatch.setattr(next_step, "_facts", lambda _: facts)
    monkeypatch.setattr("app.pagebuilder.api.enabled", lambda: True)
    monkeypatch.setattr("app.ventures.state_brief._website_section", lambda *_: {"live": False, "waitlist_enabled": False})
    monkeypatch.setattr("app.planning.store.list_current_tasks", lambda _: [{"id": "old_work", "title": "Old build",
        "source": "approved_plan:old_plan", "runnable": True, "tags": ["capability:build.run", "plan_task:old_task"]}])
    result = next_step.next_step("ven_stage")
    assert result["key"] == "gtm.outreach" and result["chat_action"] != "build.prepare"
    assert result == next_step.founder_next_step("ven_stage")


@pytest.mark.parametrize("stage", ["outreach", "traction"])
def test_accepted_private_landing_is_not_hidden_by_growth_progress(stages, journey_scope, monkeypatch, stage):
    from app.ventures import next_step
    from tests.unit.product.test_next_step import FACTS
    facts = {**FACTS, "workspace_id": "wsp_fixture", "idea_confirmed": True, "dossier_state": "complete",
             "research_report_ready": True,
             "plan_state": "approved", "founder_stage": stage, "accepted_landing": True, "accepted_build": True,
             "accepted_current_landing": True}
    monkeypatch.setattr(next_step, "_facts", lambda _: facts)
    monkeypatch.setattr("app.ventures.state_brief._website_section", lambda *_: {"live": False, "waitlist_enabled": True})
    result = next_step.next_step("ven_stage")
    assert result["key"] == "site.publish" and result["chat_action"] == "site.publish"
    assert result == next_step.founder_next_step("ven_stage")


@pytest.mark.parametrize("over,expected", [
    ({"personal": True, "intake_path": "personal_build"}, "build.first"),
    ({"idea_confirmed": False, "idea_state": "reviewing"}, "idea.review_validation"),
    ({"accepted_current_landing": False}, "gtm.outreach"),
    ({"accepted_current_landing": None}, "gtm.outreach"),
    ({"dossier_state": None}, "research.start"),
    ({"active_jobs": True}, "work.running"),
    ({"page_skipped": True}, "gtm.outreach"),
    ({"paused": True}, "venture.paused"),
])
def test_private_landing_priority_preserves_current_business_gates(stages, journey_scope, monkeypatch, over, expected):
    from app.ventures import next_step
    from tests.unit.product.test_next_step import FACTS
    facts = {**FACTS, "workspace_id": "wsp_fixture", "idea_confirmed": True, "dossier_state": "complete",
             "founder_stage": "outreach", "accepted_landing": True, "accepted_build": True,
             "accepted_current_landing": True, **over}
    facts["research_report_ready"] = facts["dossier_state"] in {"complete", "partial"}
    monkeypatch.setattr(next_step, "_facts", lambda _: facts)
    monkeypatch.setattr("app.ventures.state_brief._website_section", lambda *_: {"live": False, "waitlist_enabled": True})
    result = next_step.next_step("ven_stage")
    assert result["key"] == expected and result["chat_action"] not in {"site.publish", "waitlist.enable"}
    assert result == next_step.founder_next_step("ven_stage")


def test_research_asset_capture_uses_callers_transaction_and_rolls_back(stages):
    from app.authz.workspace import ensure_subject_person
    from app.db.models import Venture, VentureDocument
    from app.intake.assets import get_site
    from app.state import db
    from app.workspaces.service import create_workspace
    from core.clock import utcnow

    actor = ensure_subject_person("stage_transaction_founder")
    workspace = create_workspace(kind="company", name="Transactional stage", owner_id=actor)
    now = utcnow()
    with db()() as session:
        session.add(Venture(id="ven_transaction", workspace_id=workspace.id, name="Booking", raw_idea="Booking software",
                            stage="draft", autonomy_level="L1", created_at=now, updated_at=now))
        session.commit()
    cold = stages.get_stage("ven_transaction")
    assert stages.get_stage("ven_transaction") == cold
    with db()() as session:
        assert session.get(VentureDocument, "founder_stage:ven_transaction") is None
    with db()() as session:
        session.get(Venture, "ven_transaction").name = "Uncommitted change"
        captured = get_site("ven_transaction", session=session)
        assert captured["site"] is None and captured["founder_stage"]["stage"] == "idea"
        session.flush()
        assert session.get(VentureDocument, "founder_stage:ven_transaction") is None
        session.rollback()
    with db()() as session:
        assert session.get(Venture, "ven_transaction").name == "Booking"
        assert session.get(VentureDocument, "founder_stage:ven_transaction") is None


@pytest.mark.parametrize("text,customers,language", [
    ("Freelance graphic and brand designers in the UK who invoice 3 to 15 clients a month and lose hours chasing late payments.", False, "en"),
    ("Agencies who manage 20 clients", False, "en"),
    ("We serve 40 clients", True, "en"),
    ("I have 5 clients who love it", True, "en"),
    ("Nous avons 20 clients", True, "fr"),
])
def test_audience_clients_are_not_founder_customers_or_french(stages, text, customers, language):
    from app.intake import smart
    assert smart._has_customers(text) is customers
    assert smart._language(text) == language


def test_pre_launch_idea_with_audience_clients_stays_a_new_idea(stages):
    from app.intake import smart
    result = smart.classify_fallback(
        "Tern Invoice Desk: automatic, polite payment reminders for freelance designers in the UK who chase late client invoices by hand.\n\n"
        "Freelance graphic and brand designers in the UK who invoice 3 to 15 clients a month and lose hours chasing late payments.")
    assert (result.path, result.extracted.language, result.extracted.stage) == ("startup", "en", "idea")


@pytest.mark.parametrize("text,name", [
    ("Tern Invoice Desk: automatic reminders", "Tern Invoice Desk"), ("Bean Box Club — beans monthly", "Bean Box Club"),
    ("Note: this is my idea", None), ("Hi: hello", None), ("An app that helps dog walkers", None), ("I want: an app", None),
])
def test_stated_venture_name_prefills(stages, text, name):
    from app.entry.chat_start import _stated_name
    assert _stated_name(text) == name


@pytest.mark.parametrize("extracted,raw_idea,expected", [
    ({"has_customers": True, "stage": "customers", "language": "en",
      "idea": "Tern Invoice Desk: payment reminders for freelance designers in the UK.\n\nFreelance designers in the UK who invoice 3 to 15 clients a month."}, None, False),
    ({"has_customers": True, "language": "en", "idea": "BrewBox: we have 140 paying subscribers"}, None, True),
    ({"has_customers": True, "language": "fr", "idea": "Nous vendons du café en ligne"}, None, True),
    ({"has_customers": True}, None, True),
])
def test_saved_customer_flag_is_rechecked_against_founder_words(stages, extracted, raw_idea, expected):
    from types import SimpleNamespace
    body = json.dumps({"extracted": extracted})
    session = SimpleNamespace(get=lambda *_: SimpleNamespace(body_md=body, kind="intake_decision", venture_id="ven_stage", raw_idea=raw_idea))
    assert stages.has_existing_customers(session, "ven_stage") is expected
