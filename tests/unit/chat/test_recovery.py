"""Never stuck: every failure message is logged and offers another way; the founder's own result continues the venture."""
import asyncio
import json
import logging

import pytest

from tests.company_os_harness import load_company_os_app
from tests.security.test_connector_oauth_boundary import client as client  # noqa: F401
from tests.security.test_durable_journey_boundary import confirm_idea, request
from tests.security.test_durable_journey_boundary import venture as signed_venture
from tests.unit.chat.test_round3_matrix import venture as venture

load_company_os_app()


def _messages(vid):
    from app.db.models import ChatMessage
    from app.state import db
    from sqlalchemy import select

    with db()() as s:
        return [(m.kind, json.loads(m.actions_json or "[]")) for m in s.scalars(
            select(ChatMessage).where(ChatMessage.venture_id == vid).order_by(ChatMessage.created_at))]


def _events(vid, name="recovery.failure_shown"):
    from app.db.models import AnalyticsEvent
    from app.state import db
    from sqlalchemy import select

    with db()() as s:
        return [json.loads(e.props_json) for e in s.scalars(select(AnalyticsEvent).where(
            AnalyticsEvent.venture_id == vid, AnalyticsEvent.event_name == name))]


def _alt(actions):
    return next((a for a in actions if a.get("action") == "recovery.alternative"), None)


def _signed_next_step(client, venture_id):
    response = request(client, "GET", f"/ventures/{venture_id}/dashboard")
    assert response.status_code == 200, response.text
    return response.json()["next_step"]


@pytest.mark.parametrize("venture", ["Invoice reconciliation for wholesale distributors", "Weekend hiking routes for families"], indirect=True)
def test_failed_job_notice_posts_once_without_an_analytics_flush(venture, monkeypatch):
    from app.chat import milestone_hooks, recovery
    from app.db.models import ChatMessage
    from app.state import db
    from sqlalchemy import select

    _, venture_id, _ = venture
    monkeypatch.setattr(recovery, "record", lambda *args, **kwargs: None)
    emails = []
    monkeypatch.setattr("app.email.triggers.on_milestone", lambda *args, **kwargs: emails.append(args))
    with db()() as session:
        for _attempt in range(2):
            milestone_hooks.failed(session, venture_id, "idea.clarify_turn", "job_questions", "clarification_invalid_output")
        pending = [row for row in session.new if isinstance(row, ChatMessage) and row.kind == "idea_failed"]
        assert len(pending) == 1
        session.commit()
    with db()() as session:
        milestone_hooks.failed(session, venture_id, "idea.clarify_turn", "job_questions", "clarification_invalid_output")
        session.commit()
        notices = session.scalars(select(ChatMessage).where(ChatMessage.venture_id == venture_id,
                                                          ChatMessage.kind == "idea_failed")).all()
        assert len(notices) == 1
        assert sum(action.get("action") == "job.retry" for action in json.loads(notices[0].actions_json)) == 1
    assert len(emails) == 1


@pytest.mark.parametrize("venture", ["Freight invoice reconciliation for distributors", "Weekend trails for families"], indirect=True)
@pytest.mark.parametrize("state", ["complete", "partial", "thin", "failed"])
def test_local_research_outcome_reaches_chat_once(venture, state, monkeypatch, tmp_path):
    from app.chat import milestone_hooks, milestones
    from app.db.models import ChatMessage
    from app.research import service
    from app.research.execution import identifier
    from app.state import db
    from app.worker import handlers
    from core.clock import utcnow
    from sqlalchemy import select

    _, vid, wid = venture
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setattr(handlers, "require_capability", lambda *_: None)
    monkeypatch.setattr("app.research.deep.run_deep", lambda *_: None)
    monkeypatch.setattr("app.chat.next_moves.post", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("app.ventures.next_step._facts", lambda *_: pytest.fail("Research should not depend on the build journey"))
    monkeypatch.setattr("app.core.local_secrets.read_settings", lambda *_: {"values": {"provider": "builtin"}})
    monkeypatch.setattr("app.research.local_providers.setup_reply", lambda: None)
    job_id = "research-fixture"
    source = "https://alternative.example/overview"
    code = "research_credentials_unavailable" if state == "failed" else ""
    report = {"id": identifier(job_id), "venture_id": vid, "state": "partial" if state == "thin" else state, "summary": {
        "error_code": code, "verdict": {"source_count": 3, "why": []},
        "review_kind": "retrieved_sources_only" if state == "thin" else "model_assessment",
        "founder_report": {"findings": [{"finding": f"Finding {index}", "url": source} for index in range(3)],
                           "competitor_grid": {"rows": [{"name": "Saved alternative", "url": source}]}},
    }}
    monkeypatch.setattr(service, "get_dossier", lambda dossier_id: report if dossier_id == report["id"] else None)
    monkeypatch.setattr(service, "list_dossiers", lambda *_: [report])
    now = utcnow()
    with db()() as session:
        session.add_all(ChatMessage(id=f"notice-{index}", venture_id=vid, workspace_id=wid,
                                    role="assistant", kind="text", body_md="Earlier notice", created_at=now)
                        for index in range(milestones.DAILY_MESSAGE_LIMIT))
        session.commit()

    for _attempt in range(2):
        if code:
            with db()() as session:
                milestone_hooks.failed(session, vid, "research.deep", job_id, code)
                session.commit()
        else:
            handlers._research_deep({"_job_id": job_id, "venture_id": vid, "workspace_id": wid})

    with db()() as session:
        notice = session.scalars(select(ChatMessage).where(ChatMessage.venture_id == vid,
            ChatMessage.kind == ("research_failed" if code else "research_ready"))).one()
        assert "Saved alternative" in notice.body_md
        assert notice.body_md.count("\n- ") == 3
        assert any(item.get("href") == source for item in json.loads(notice.citations_json))
        assert any(action["label"] == "Open full report" and action.get("kind")
                   and action.get("href") == f"/v/{vid}/research?dossier={report['id']}"
                   for action in json.loads(notice.actions_json))
        assert "partial" not in notice.body_md.lower()
        if code:
            assert "Keys and connections > Your AI provider" in notice.body_md
    if state in {"partial", "thin"}:
        from app.chat.execute import _saved_research
        monkeypatch.setattr("app.chat.execute.research_needs_start", lambda *_: False)
        markdown = tmp_path / "ventures" / vid / "research" / "validation_dossier.md"
        markdown.parent.mkdir(parents=True)
        markdown.write_text(f"# Saved report\n\nResearch status: partial\n\n{report['id']}", encoding="utf-8")
        monkeypatch.setattr("app.config.data_root", lambda: tmp_path)
        saved_copy = _saved_research(vid)["body_md"]
        assert ("Keys and connections > Search" if state == "thin" else "You have some findings") in saved_copy
        assert "partial" not in saved_copy.lower()


@pytest.mark.hosted_only
def test_any_failure_message_is_logged_and_offers_another_way(venture, caplog):
    _, vid, _ = venture
    from app.chat.milestones import post_milestone

    caplog.set_level(logging.WARNING, logger="app.error_event")
    retry = {"label": "Retry thing", "action": "job.retry", "payload": {"kind": "x"}}
    assert post_milestone(vid, key="k1", kind="widget_failed", body_md="Widget did not finish.", actions=[retry])
    kind, actions = _messages(vid)[-1]
    assert kind == "widget_failed" and actions[0] == retry
    assert _alt(actions)["label"] == "Do it another way"
    assert _events(vid)[-1]["kind"] == "widget_failed"
    assert any("error_event" in r.getMessage() and "recovery" in r.getMessage() for r in caplog.records)
    # Posting the same failure again is a no-op: one message, one receipt.
    assert not post_milestone(vid, key="k1", kind="widget_failed", body_md="Widget did not finish.")
    assert len(_events(vid)) == 1


@pytest.mark.hosted_only
def test_every_wired_failure_kind_goes_through_the_helper(venture):
    _, vid, _ = venture
    from app.chat import milestone_hooks as hooks
    from app.chat.mac_stop import post_mac_stopped
    from app.state import db

    with db()() as s:
        hooks.failed(s, vid, "plan.compile", "job_p", "plan_invalid_output")
        hooks.failed(s, vid, "research.deep", "job_r", "")
        hooks.content_failed(s, vid, "content.draft", "job_c", "content_job_failed")
        s.commit()
    hooks.deployment_failed(vid, "dep_1", "")
    assert post_mac_stopped(vid, "ord_1", "cu_permission_missing", "Open TextEdit")
    sent = [(k, a) for k, a in _messages(vid) if k.endswith("_failed") or k == "mac_task_stopped"]
    assert {k for k, _ in sent} == {"plan_failed", "research_failed", "content_failed", "site_publish_failed",
                                    "mac_task_stopped"}
    for kind, actions in sent:
        assert _alt(actions), kind
    steps = {e["step"] for e in _events(vid)}
    assert steps == {"plan.compile", "research.deep", "content.draft", "publish", "mac.task"}
    plan_actions = dict(sent)["plan_failed"]
    assert any(a.get("label") == "Retry planning" for a in plan_actions)  # the existing Retry is kept
    assert _alt(plan_actions)["payload"] == {"step": "plan.compile", "kind": "plan_failed", "job_id": "job_p",
                                             "error_code": "plan_invalid_output"}


@pytest.mark.hosted_only
def test_failed_job_posts_for_content_and_validation_via_fail_job(venture):
    _, vid, wid = venture
    from app.core.jobs import FatalError, enqueue, fail_job, lease
    from app.state import db

    with db()() as s:
        enqueue(s, "content.draft", {"workspace_id": wid, "venture_id": vid}, workspace_id=wid, venture_id=vid)
        s.commit()
        job = lease(s, "w1", kinds=["content.draft"], n=1)[0]
        fail_job(s, job.id, FatalError("content_job_failed"))
        s.commit()
    assert [k for k, _ in _messages(vid) if k == "content_failed"]
    assert _events(vid)[-1]["step"] == "content.draft"


def test_dead_end_reply_gets_another_way_button(venture):
    from app.chat import claims

    reply = {"body_md": "Sorry, I can't click the button for you.", "actions": []}
    assert _alt(claims.with_alternative(reply)["actions"])
    fine = {"body_md": "Done, it is saved.", "actions": []}
    assert claims.with_alternative(fine) == fine


def _alternative(vid, step, llm, monkeypatch, *, template_ready=True, **extra):
    from app.chat import recovery

    async def fake(messages, workspace_id):
        if isinstance(llm, Exception):
            raise llm
        return llm

    monkeypatch.setattr(recovery, "_llm", fake)
    monkeypatch.setattr(recovery, "_template_ready", lambda *_: template_ready)
    return asyncio.run(recovery.alternative(vid, {"step": step, "error_code": "plan_invalid_output", **extra}, None, None))


@pytest.mark.hosted_only
def test_alternative_offers_automatic_one_tap_card_when_the_ai_chooses_it(venture, monkeypatch):
    _, vid, _ = venture
    reply = _alternative(vid, "plan.compile", json.dumps({"choice": "plan_template", "intro": "I can build a starter plan."}),
                         monkeypatch)
    assert reply["kind"] == "confirmation" and reply["body_md"] == "I can build a starter plan."
    card = reply["actions"][0]["confirmation"]
    assert card["action"] == "recovery.plan_template"
    assert reply["actions"][1]["payload"]["mode"] == "diy"  # the founder can still choose to do it themselves
    assert _events(vid, "recovery.alternative_offered")[-1]["mode"] == "auto"


def test_alternative_falls_back_to_a_do_it_yourself_guide(venture, monkeypatch):
    _, vid, _ = venture
    guide = {"choice": "diy", "intro": "Do this in any doc.", "steps": ["Write the goal.", "List 3 milestones."],
             "bring_back": "Your plan as text."}
    reply = _alternative(vid, "plan.compile", json.dumps(guide), monkeypatch)
    assert "1. Write the goal." in reply["body_md"] and "**Bring back:** Your plan as text." in reply["body_md"]
    submit = reply["actions"][0]
    assert submit["label"] == "Submit my result" and submit["action"] == "recovery.submit"
    assert submit["payload"]["step"] == "plan.compile"
    # The model cannot invent an automatic option the step does not offer, and an outage still yields a guide.
    research = _alternative(vid, "research.deep", json.dumps({"choice": "plan_template", "steps": ["x"]}), monkeypatch)
    assert research["actions"][0]["action"] == "recovery.submit"
    down = _alternative(vid, "research.deep", RuntimeError("provider down"), monkeypatch)
    assert "alternatives" in down["body_md"] and down["actions"][0]["action"] == "recovery.submit"
    forced = _alternative(vid, "plan.compile", json.dumps({"choice": "plan_template"}), monkeypatch, mode="diy")
    assert forced["actions"][0]["action"] == "recovery.submit"


def test_submitted_url_must_be_http_and_is_never_marked_verified(venture):
    _, vid, wid = venture
    from app.chat import recovery
    from app.db.models import VentureDocument
    from app.state import db

    for bad in ("javascript:alert(1)", "ftp://x.test/a", "https://user:pw@x.test/", "not a url"):
        r = recovery.submit(vid, {"step": "publish", "url": bad, "text": "live"}, wid)
        assert "Nothing was saved" in r["body_md"] or "did not save" in r["body_md"], bad
    assert "Nothing was saved" in recovery.submit(vid, {"step": "build", "text": "no link"}, wid)["body_md"]
    ok = recovery.submit(vid, {"step": "build", "url": "https://mysite.carrd.co/", "text": "x" * 9000}, wid)
    assert "founder-provided and not verified" in ok["body_md"]
    with db()() as s:
        doc = s.get(VentureDocument, f"external_site:{vid}")
        assert doc.path == "https://mysite.carrd.co/" and len(doc.body_md) == 6000
        assert json.loads(doc.provenance_json) == {"source": "founder_provided", "step": "build", "verified": False}


@pytest.fixture()
def confirmed(client, monkeypatch):  # noqa: F811
    from tests.idea_fixtures import install_provider

    install_provider(monkeypatch)
    vid, wid, sid = signed_venture(client)
    confirm_idea(client, vid, sid)
    assert request(client, "PUT", f"/ventures/{vid}/resources", json={"assets": [], "complete": True}).status_code == 200
    from app.db.models import Job
    from app.state import db

    with db()() as s:  # leftover queued housekeeping would make next_step say "work is running"
        for job in s.query(Job).filter(Job.venture_id == vid, Job.status.in_(["scheduled", "pending", "retry_wait"])):
            job.status = "succeeded"
        s.commit()
    return vid, wid


def test_founder_research_then_plan_advance_the_next_step(confirmed, client):
    vid, wid = confirmed
    from app.chat import recovery
    from app.db.models import AnalyticsEvent
    from app.db.supymem_models import ExecutionPlan, ResearchFinding
    from app.planning.compiler import get_plan
    from app.research.service import get_dossier
    from app.state import db

    assert _signed_next_step(client, vid)["key"] != "plan.generate"
    text = ("https://alt.example.com/bakery - Order board for bakeries, closest competitor\n"
            "javascript:alert(1) bad link is dropped\nWhat the price pages say: about 20 a month")
    r = recovery.submit(vid, {"step": "research.deep", "text": text}, wid)
    assert "founder-provided and not verified" in r["body_md"]
    assert _signed_next_step(client, vid)["key"] == "research.start"
    with db()() as s:
        findings = s.query(ResearchFinding).filter_by(venture_id=vid, source="founder_provided").all()
        assert len(findings) == 3
        assert all(json.loads(f.evidence)["page_verified"] is False for f in findings)
        assert [f.url for f in findings if f.url] == ["https://alt.example.com/bakery"]
        did = findings[0].dossier_id
    dossier = get_dossier(did)
    assert dossier["state"] == "partial"
    from app.db.supymem_models import ResearchDossier
    with db()() as s:  # the provenance note is kept with its kind for admins, never shown as a founder gap
        saved = json.loads(s.get(ResearchDossier, did).summary)
    assert any("did not fetch or verify" in gap for gap in saved["coverage_gaps"])
    assert "did not fetch or verify" not in json.dumps(dossier)

    r = recovery.submit(vid, {"step": "plan.compile", "text": "Goal: first paid bakery. Week 1: call five bakeries."}, wid)
    assert "draft for you to review and approve" in r["body_md"]
    with db()() as s:
        row = s.query(ExecutionPlan).filter_by(venture_id=vid).one()
        assert row.state == "founder_review"
        assert json.loads(row.structured)["provenance"] == "founder_provided"
        plan_id = row.id
    plan = get_plan(plan_id)
    assert plan["reviewable"] and len(plan["milestones"]) == 9 and plan["method"] == "founder_provided_plan"
    assert "call five bakeries" in plan["summary"]
    assert _signed_next_step(client, vid)["key"] == "research.start"
    from app.authz.workspace import subject_person_id
    from app.planning.compiler import approve_plan

    assert approve_plan(plan_id, person_id=subject_person_id("usr_alice"), review_token=plan["review_token"])["ok"]
    with db()() as s:
        assert s.query(AnalyticsEvent).filter_by(venture_id=vid, event_name="recovery.result_submitted").count() == 2


def test_template_plan_action_needs_no_ai_and_lands_in_review(confirmed, monkeypatch, client):
    vid, wid = confirmed
    from app.chat import recovery
    from app.chat.execute import execute_action
    from app.autopilot import receipts
    from app.autopilot.authority import founder_scope

    recovery.submit(vid, {"step": "research.deep", "text": "https://alt.example.com/x - a competitor"}, wid)
    with founder_scope(vid, "usr_alice"):
        with receipts.collect(vid) as evidence:
            reply = asyncio.run(execute_action(vid, "recovery.plan_template", actor_subject="usr_alice"))
    assert evidence.outcome() == "completed"
    assert evidence.persisted[0][0].__name__ == "ExecutionPlan"
    assert "starter plan is saved" in reply["body_md"]
    assert _signed_next_step(client, vid)["key"] == "research.start"


def test_template_plan_without_research_explains_instead_of_failing(confirmed):
    vid, _ = confirmed
    from app.chat.execute import execute_action

    reply = asyncio.run(execute_action(vid, "recovery.plan_template", actor_subject="usr_alice"))
    assert "did not create it" in reply["body_md"]
    assert _alt(reply["actions"])


@pytest.mark.hosted_only
def test_chat_action_crash_is_logged_and_gets_a_recovery_message(venture, monkeypatch):
    client, vid, _ = venture
    from app.chat import execute

    async def boom(*_a, **_k):
        raise RuntimeError("secret internals")

    monkeypatch.setattr(execute, "_execute_action", boom)
    import app.api.chat_api as api

    monkeypatch.setattr(api, "execute_action", execute.execute_action)
    r = client.post(f"/ventures/{vid}/chat/confirm", json={"action": "status", "payload": {}})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kind"] == "action_failed" and "secret internals" not in body["body_md"]
    labels = [a["label"] for a in body["actions"]]
    assert labels[0] == "Retry" and "Do it another way" in labels
    assert _events(vid)[-1]["step"] == "chat.action"
