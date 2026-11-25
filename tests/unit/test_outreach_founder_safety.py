"""Founder-safety regressions for shared outreach commands and saved history."""
from __future__ import annotations

import asyncio
import json

import pytest
from sqlalchemy import select

from tests.unit.chat.test_customers_in_chat import (
    customer_chat as customer_chat,
    journey_db as journey_db,
)
from tests.unit.gtm.test_journey import _mail


def _customer_lead(data):
    return {"company": data["audience"].split(" in ")[0], "domain": "research.example",
            "role": data["audience"].split(" in ")[0], "country": data["audience"].split(" in ")[-1],
            "source_url": "https://research.example/contact", "why_fits": data["problem"]}


@pytest.fixture
def outreach_case(customer_chat, monkeypatch):
    from app.autopilot import outreach
    from app.core.local_mode import is_local
    from app.core.local_secrets import update_settings

    _, data = customer_chat
    _mail(data)
    if is_local():
        update_settings("capabilities", {"send_email": True}, {})
    monkeypatch.setattr(outreach, "site_url", lambda _: None)

    def forbidden_send(*_args, **_kwargs):
        pytest.fail("Review and preference changes must not send email")

    monkeypatch.setattr(outreach, "_send_one", forbidden_send)
    return data


def _drafts(data, count, *, start=0):
    from app.autopilot import outreach
    from app.db.models import GtmContact
    from core.clock import utcnow

    saved = []
    for index in range(start, start + count):
        evidence = _customer_lead(data)
        email = f"reader{index}@{evidence['domain']}"
        contact_id = f"review_contact_{index}"
        now = utcnow()
        with data["factory"]() as session:
            session.add(GtmContact(id=contact_id, workspace_id=data["wid"], email=email,
                first_name=f"Reader {index}", role=evidence["role"], recipient_geo=evidence["country"],
                source=f"venture:{data['vid']}:founder", email_confidence="safe",
                lawful_basis="consent", lawful_basis_at=now,
                lawful_basis_note="This person explicitly agreed to receive your research invitation.",
                stage_changed_at=now, created_at=now,
                enrichment_json=json.dumps(evidence | {"business": evidence["company"],
                    "person_role": evidence["role"], "email_evidence": "published",
                    "found_at": evidence["source_url"]})))
            session.commit()
        draft_id = outreach._save_draft(data["vid"], {"state": "draft", "review_state": "draft",
            "channel": "email", "contact_id": contact_id, "to": email, "day": 0,
            "batch_id": "founder_review", "requires_journey_review": True,
            "suggestion_draft": data["business_model"] == "consumer",
            "subject": f"Question {index} about {data['problem']}",
            "body": f"You mentioned {data['problem']}. Would you share how you handle it?"})
        saved.append(draft_id)
    return saved


def _command(data, text):
    from app.chat import customers

    name, match = customers.command(data["vid"], text)
    return asyncio.run(customers.run_command(data["vid"], name, match, text, data["actor"]))


@pytest.mark.parametrize("existing_drafts", [False, True])
def test_not_now_revokes_existing_automation(outreach_case, existing_drafts):
    from app.autopilot import guard
    from app.gtm import journey

    data = outreach_case
    if existing_drafts:
        _drafts(data, 2)
    journey.update_settings(data["wid"], data["vid"], {"allow_sending": True,
        "auto_approve_first_emails": True, "auto_search": {"enabled": True}})
    assert guard.get_settings(data["vid"])["allow_send"]
    reply = _command(data, "Not now, I will review emails myself")
    policy = guard.get_settings(data["vid"])
    view = journey.status(data["wid"], data["vid"], authorize=False)
    assert not policy["allow_send"]
    assert policy["send_max_per_day"] == 0
    assert not view["auto_approve_first_emails"] and not view["auto_search"]["enabled"]
    assert "approval" in reply["body_md"].lower()
    assert "automate my emails" in reply["body_md"].lower()


def test_approve_all_is_bound_to_displayed_draft_revisions(outreach_case):
    from app.autopilot import outreach
    from app.chat import customers
    from app.gtm import journey

    data = outreach_case
    initial_ids = set(_drafts(data, 8))
    review = asyncio.run(customers.execute(data["vid"], "gtm.status", {"show_saved": True}, data["actor"]))
    initial = outreach.drafts(data["vid"], "draft")
    shown_ids = {draft["doc_id"] for draft in initial if draft["to"] in review["body_md"]
        and draft["subject"] in review["body_md"] and draft["body"] in review["body_md"]}
    button = next(action for action in review["actions"] if action.get("action") == "gtm.approve")
    assert shown_ids and shown_ids <= initial_ids
    _drafts(data, 1, start=8)
    changed = next(draft for draft in initial if draft["doc_id"] in shown_ids)
    outreach._save_draft(data["vid"], changed | {"subject": "A different question"}, changed["doc_id"])
    asyncio.run(customers.execute(data["vid"], button["action"], button["payload"], data["actor"]))
    approved = {draft["doc_id"] for draft in outreach.drafts(data["vid"], "draft")
        if draft.get("review_state") == "approved"}
    assert approved == shown_ids - {changed["doc_id"]}
    fresh = journey.status(data["wid"], data["vid"], authorize=False)
    assert all(draft["review_state"] == "draft" for draft in fresh["drafts"]
        if draft["doc_id"] not in approved)
    _command(data, "approve all")
    assert {draft["doc_id"] for draft in outreach.drafts(data["vid"], "draft")
        if draft.get("review_state") == "approved"} == approved


def test_opt_out_history_retains_identity_and_suppression(outreach_case, monkeypatch):
    from app.autopilot import outreach
    from app.db.models import GtmContact, GtmSuppression, GtmTouch
    from app.gtm import journey
    from app.gtm.supymem import composio_gmail, workers
    from core.clock import utcnow

    data = outreach_case
    _drafts(data, 1)
    draft = outreach.drafts(data["vid"], "draft")[0]
    with data["factory"]() as session:
        contact = session.get(GtmContact, draft["contact_id"])
        contact.times_emailed = 1
        session.add(GtmTouch(id="prior_outbound", workspace_id=data["wid"], venture_id=data["vid"],
            contact_id=contact.id, direction="out", channel="email", simulated=0,
            send_mode="founder_gmail", provider_thread_id="review_thread",
            message_id="<sent@research.example>", sent_at=utcnow(), created_at=utcnow()))
        session.commit()
    monkeypatch.setattr(composio_gmail, "get_gmail_profile", lambda *_args, **_kwargs: {"raw": {"historyId": "1"}})
    monkeypatch.setattr(composio_gmail, "fetch_thread", lambda **_kwargs: {"messages": [{
        "id": "opt_out_reply", "threadId": "review_thread", "snippet": "Please remove me and stop emailing.",
        "payload": {"headers": [{"name": "From", "value": draft["to"]},
            {"name": "Auto-Submitted", "value": "no"}]}}]})
    result = workers.tick_replies(data["wid"])
    assert len(result["replies"]) == 1, result
    view = journey.status(data["wid"], data["vid"], authorize=False)
    with data["factory"]() as session:
        contact = session.get(GtmContact, draft["contact_id"])
        assert contact.email == draft["to"] and contact.do_not_contact and contact.unsubscribed_at
        assert session.scalar(select(GtmSuppression.id).where(
            GtmSuppression.workspace_id == data["wid"], GtmSuppression.email == draft["to"]))
        assert outreach._suppressed(session, data["wid"], contact)
    prospects = [person for person in view["prospects"] if person["id"] == draft["contact_id"]]
    assert all(not person["email"] and not person["eligible"] for person in prospects)
    assert outreach.schedule_followups(data["vid"]) == 0
    reply = next(reply for reply in view["results"]["replies"] if reply["contact_id"] == draft["contact_id"])
    assert reply["from"] == draft["to"]


@pytest.mark.parametrize("existing_drafts", [False, True])
def test_followup_preference_saves_without_customer_search(outreach_case, existing_drafts):
    from app.db.models import Job
    from app.gtm import journey

    data = outreach_case
    if existing_drafts:
        _drafts(data, 1)
    with data["factory"]() as session:
        assert journey._row(session, data["vid"]) is None
    reply = _command(data, "no follow-ups")
    assert journey.followup_days(data["vid"]) == ()
    assert "each person gets only the first email" in reply["body_md"].lower()
    with data["factory"]() as session:
        assert journey._json(journey._row(session, data["vid"]).provenance_json)["followup_days"] == []
        assert not session.scalar(select(Job.id).where(Job.venture_id == data["vid"]))
    if existing_drafts:
        from app.core.local_mode import is_local

        if is_local():
            from app.core.local_secrets import update_settings

            blocked = _command(data, "send first emails automatically")
            assert "Settings > Capabilities" in blocked["body_md"] and "Background work" in blocked["body_md"]
            assert journey.followup_days(data["vid"]) == ()
            with data["factory"]() as session:
                assert not session.scalar(select(Job.id).where(Job.venture_id == data["vid"]))
            update_settings("capabilities", {"background_work": True}, {})
        # Founder decision: no confirmation card for what you asked; your follow-up preference still holds.
        automatic = _command(data, "send first emails automatically")
        assert "First emails now go out automatically" in automatic["body_md"] and "Sending is not allowed yet" in automatic["body_md"]
        assert "day 2, 4 and 7" not in automatic["body_md"] and journey.followup_days(data["vid"]) == ()


def test_search_pause_does_not_claim_sending_is_on(outreach_case):
    from app.autopilot import guard, service
    from app.gtm import journey

    data = outreach_case
    _drafts(data, 1)
    journey.update_settings(data["wid"], data["vid"], {"allow_sending": False,
        "auto_approve_first_emails": False, "auto_search": {"enabled": True}})
    service.update_settings(data["vid"], allow_send=False, send_max_per_day=0)
    before = guard.get_settings(data["vid"])
    assert not before["allow_send"] and before["send_max_per_day"] == 0
    reply = _command(data, "stop finding customers")
    after = guard.get_settings(data["vid"])
    assert after["allow_send"] == before["allow_send"] and after["send_max_per_day"] == 0
    assert not journey.status(data["wid"], data["vid"], authorize=False)["auto_search"]["enabled"]
    assert "resume finding customers" in reply["body_md"].lower()
    assert "schedule stays on" not in reply["body_md"].lower()
    assert "\u2014" not in reply["body_md"]
