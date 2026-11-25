"""Automatic sending after Allow sending: follow-ups on the founder's chosen cadence (default 2/4/7), first emails only once approved
(or with auto-approve on), every gate kept, playbook experiments, and no em dashes in anything we compose."""
from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from tests.unit.gtm.test_journey import _mail, journey_db  # noqa: F401


def _prospect(d, monkeypatch, i):
    from app.gtm import journey
    monkeypatch.setattr(journey.outreach, "site_url", lambda vid: None)
    journey.add_prospect(d["wid"], d["vid"], {"name": f"Ada{i}", "company": f"Bakery {i}", "email": f"ada@bakery{i}.example",
        "source_url": f"https://bakery{i}.example/contact", "country": "CA", "lawful_basis": "consent",
        "lawful_basis_note": "The contact explicitly agreed to hear about the bakery order board."})
    journey._draft_ready(d["wid"], d["vid"])
    return journey.status(d["wid"], d["vid"], authorize=False)


def _setup(d, monkeypatch, n=1, cap=None, *, background_work=True):
    from app.autopilot import outreach
    from app.autopilot.service import update_settings
    from app.core.local_mode import is_local
    from app.gtm import journey
    if is_local():
        from app.core.local_secrets import update_settings as save_settings

        save_settings("capabilities", {"background_work": background_work}, {})
    for i in range(n):
        _prospect(d, monkeypatch, i)
    _mail(d)
    with d["factory"]() as session:  # a prepared journey exists for every venture with leads
        journey._row(session, d["vid"], create=True)
        session.commit()
    update_settings(d["vid"], allow_send=True, send_max_per_day=cap)
    sent = []
    monkeypatch.setattr(outreach, "_send_one", lambda box, to, subject, body, thread: sent.append(
        {"to": to, "subject": subject, "body": body, "thread": thread}) or {"provider_message_id": f"m{len(sent)}", "thread_id": f"thread-{to}"})
    return sent


def _assert_send_spend(data, count):
    from app.core.local_mode import is_local
    from app.credits import local_meter
    from tests.unit.gtm.test_journey import _assert_credit_usage

    with data["factory"]() as session:
        _assert_credit_usage(data, session, spent_micro=0 if is_local() else 10_000 * count)
    if is_local():
        with local_meter._database() as connection:
            calls = connection.execute("SELECT status, cost_usd_micro FROM calls WHERE venture_id = ? AND purpose = ?",
                (data["vid"], "outbound:gtm_email_send")).fetchall()
            assert [(row["status"], row["cost_usd_micro"]) for row in calls] == [("confirmed", 0)] * count


def _local_campaign(data, monkeypatch, audience, email):
    from app.gtm import journey
    from tests.unit.intake.test_contact_import import _commit, _preview
    from tests.unit.gtm.test_journey import _review

    journey.update_icp(data["wid"], data["vid"], audience, problem="Coordinating shared plans",
        recruitment_opt_in=True, start=False)
    imported = _commit(data, _preview(data, monkeypatch, f"name,email,country\nAvery,{email},CA"),
        lawful_basis_note="You have explicit permission for this research email.")
    sent = _setup(data, monkeypatch, n=0, background_work=False)
    journey.draft_for_review(data["vid"], contact_id=imported["contact_ids"][0])
    _, request = _review(data)
    return sent, request


@pytest.mark.parametrize("audience,email", [("Industrial quality inspectors", "avery@industry.example"),
    ("Families sharing recipes", "avery@recipes.example")])
def test_local_requested_send_needs_no_hosted_wallet_or_rate(journey_db, monkeypatch, audience, email):
    from app.core.local_mode import is_local
    from app.credits.policy import set_policy
    from app.gtm import journey

    if not is_local():
        pytest.skip("Local SMTP accounting contract")
    data = journey_db
    sent, request = _local_campaign(data, monkeypatch, audience, email)
    set_policy("outbound_provider_rates", {}, "test")
    asyncio.run(journey.send(data["wid"], data["vid"], request))
    assert len(sent) == 1
    assert _auto(data) is None and len(sent) == 1
    _assert_send_spend(data, 1)
    from app.approvals.external import OutboundExecutionError, execute_metered_external_action
    from app.db.models import Approval

    draft = journey.outreach.drafts(data["vid"])[0]
    with data["factory"]() as session:
        target = json.loads(session.get(Approval, draft["delivery_approval_id"]).detail_json)["target"]
    arguments = {"workspace_id": data["wid"], "approval_id": draft["delivery_approval_id"],
        "action": "gtm_email_send", "provider": "composio_gmail", "target": target,
        "invoke": lambda: pytest.fail("An invalid or consumed approval must not send.")}
    for overrides, error in (({}, "external_approval_already_consumed"),
        ({"workspace_id": "another-workspace"}, "external_approval_workspace_mismatch"),
        ({"target": target | {"body": "Changed after review"}}, "external_approval_content_mismatch"),
        ({"target": target | {"credit_quote": target["credit_quote"] | {"credit_estimate_micro": 1}}}, "outbound_rate_version_changed")):
        with pytest.raises(OutboundExecutionError) as caught:
            execute_metered_external_action(**(arguments | overrides))
        assert caught.value.code == error


@pytest.mark.parametrize("audience,email", [("Industrial quality inspectors", "avery@industry.example"),
    ("Families sharing recipes", "avery@recipes.example")])
@pytest.mark.parametrize("outcome", ["sent", "not_sent"])
def test_local_unknown_delivery_never_retries_and_owner_can_resolve(journey_db, monkeypatch, audience, email, outcome):
    from app.autopilot import outreach
    from app.chat.execute import execute_action
    from app.core.local_mode import is_local
    from app.gtm import journey

    if not is_local():
        pytest.skip("Local SMTP recovery contract")
    data = journey_db
    _, request = _local_campaign(data, monkeypatch, audience, email)
    calls = []

    def timeout(*args):
        calls.append("attempt")
        raise TimeoutError("Synthetic acknowledgement lost")

    monkeypatch.setattr(outreach, "_send_one", timeout)
    asyncio.run(journey.send(data["wid"], data["vid"], request))
    draft = outreach.drafts(data["vid"])[0]
    assert draft["state"] == "delivery_unconfirmed" and calls == ["attempt"]
    assert outreach.send_slots_used_today(data["vid"]) == 1
    assert all("confirmation" not in action and "credit" not in str(action).lower()
        for action in outreach.delivery_resolution_actions(draft))
    assert _auto(data) is None and calls == ["attempt"]
    with pytest.raises(PermissionError, match="outreach_approver_required"):
        outreach.resolve_delivery(data["vid"], {"draft_id": draft["doc_id"], "outcome": outcome}, actor_subject="foreign")
    result = asyncio.run(execute_action(data["vid"], "outreach.delivery_resolve",
        payload={"draft_id": draft["doc_id"], "outcome": outcome}, actor_subject=data["actor"]))
    assert "credit" not in result["body_md"].lower()
    assert outreach.drafts(data["vid"])[0]["state"] == ("sent" if outcome == "sent" else "draft")
    assert calls == ["attempt"] and outreach.send_slots_used_today(data["vid"]) == int(outcome == "sent")
    assert _auto(data) is None
    from app.credits import local_meter
    with local_meter._database() as connection:
        receipt = connection.execute("SELECT status, cost_usd_micro FROM calls WHERE venture_id = ? AND purpose = ?",
            (data["vid"], "outbound:gtm_email_send")).fetchone()
        assert receipt["status"] == "unknown" and receipt["cost_usd_micro"] is None


def _reply(d, contact):
    from app.gtm.supymem import pipeline
    pipeline.apply_reply(contact.id, body="Yes, interested", classification="human_positive", venture_id=d["vid"],
                         provider_thread_id=f"thread-{contact.email}", provider_message_id=f"reply-{contact.id}")


def _auto(d):
    from app.gtm import journey
    return asyncio.run(journey.auto_send(d["wid"], d["vid"], d["actor"]))


@pytest.fixture()
def followup_clock(journey_db, monkeypatch):
    from app.approvals import external, service as approvals
    from app.autopilot import guard, outreach, service as autopilot
    from app.gtm import journey
    from app.gtm.relationships import cadence, unsubscribe
    from app.gtm.supymem import pipeline
    from core import clock

    simulated = SimpleNamespace(now=clock.utcnow())
    for module in (clock, guard, outreach, autopilot, journey, cadence, pipeline, unsubscribe, external, approvals):
        monkeypatch.setattr(module, "utcnow", lambda: simulated.now)
    return simulated


def _choose_cadence(d, days):
    from app.chat import customers
    from app.gtm import journey

    label = next(label for label, choice in customers.CADENCES if choice == tuple(days))
    name, match = customers.command(d["vid"], label)
    asyncio.run(customers.run_command(d["vid"], name, match, label, d["actor"]))
    assert journey.followup_days(d["vid"]) == tuple(days)


def test_first_emails_wait_for_approval_unless_auto_approve_is_on(journey_db, monkeypatch):
    from app.gtm import journey
    d = journey_db
    sent = _setup(d, monkeypatch, n=2)
    assert _auto(d) is None and not sent  # Allow sending alone never sends an unreviewed first email
    state = journey.status(d["wid"], d["vid"])
    first = state["drafts"][0]
    journey.patch_draft(d["wid"], d["vid"], first["doc_id"], {"decision": "approved", "expected_revision": first["revision"]})
    _auto(d)
    assert [s["to"] for s in sent] == [first["to"]]  # the one the founder approved, with no extra click
    assert journey.update_settings(d["wid"], d["vid"], {"auto_approve_first_emails": True})["auto_approve_first_emails"] is True
    _auto(d)
    assert len(sent) == 2 and len({s["to"] for s in sent}) == 2


def test_followups_send_automatically_on_the_cadence_and_stop_on_reply(journey_db, monkeypatch, followup_clock):
    from app.autopilot import outreach
    from app.db.models import GtmTouch
    from app.gtm import journey
    d = journey_db
    sent = _setup(d, monkeypatch, n=2)
    journey.update_settings(d["wid"], d["vid"], {"auto_approve_first_emails": True})
    _choose_cadence(d, [2, 4, 7])
    started = followup_clock.now
    monkeypatch.setattr(journey, "_discovery_eligible", lambda *a: False)

    async def no_poll(*args, **kwargs):
        return 0
    monkeypatch.setattr(outreach, "poll_replies", no_poll)
    asyncio.run(journey.maintain_all())  # the 5-minute worker tick is the only trigger
    assert len(sent) == 2 and all(s["thread"] is None for s in sent)
    asyncio.run(journey.maintain_all())
    assert len(sent) == 2  # nothing is due yet
    quiet, replier = outreach.contacts(d["vid"])
    for after in (1, 2, 3, 4, 7, 10):
        followup_clock.now = started + timedelta(days=after)
        if after == 3:
            _reply(d, replier)
        before = len(sent)
        asyncio.run(journey.maintain_all())
        assert len(sent) - before == {2: 2, 4: 1, 7: 1}.get(after, 0)
    followups = sent[2:]
    assert [s["to"] for s in followups].count(replier.email) == 1
    mine = [s for s in followups if s["to"] == quiet.email]
    assert len(mine) == 3 and all(s["thread"] == f"thread-{quiet.email}" and s["subject"].startswith("Re: ") for s in mine)
    assert "close the loop" in mine[-1]["body"] and not any("bump" in s["body"].lower() for s in mine)
    assert len({s["body"].split("\n\n")[1] for s in mine}) == 3
    assert sorted(x["day"] for x in outreach.drafts(d["vid"], "sent") if x["contact_id"] == quiet.id) == [1, 3, 5, 8]
    with d["factory"]() as session:
        touches = list(session.scalars(select(GtmTouch).where(GtmTouch.venture_id == d["vid"], GtmTouch.direction == "out")))
        assert len(touches) == len(sent)  # every automatic send is logged like a manual one
    _assert_send_spend(d, len(sent))


def test_daily_cap_and_suppression_still_hold_for_automatic_sends(journey_db, monkeypatch, followup_clock):
    from app.autopilot import outreach
    from app.db.models import GtmSuppression
    from app.gtm import journey
    from core.clock import utcnow
    d = journey_db
    sent = _setup(d, monkeypatch, n=3, cap=1)
    journey.update_settings(d["wid"], d["vid"], {"auto_approve_first_emails": True})
    blocked = outreach.contacts(d["vid"])[0]
    with d["factory"]() as session:
        session.add(GtmSuppression(id="sup_auto", workspace_id=d["wid"], email=blocked.email, source="opt_out", created_at=utcnow()))
        session.commit()
    _auto(d)
    _auto(d)
    assert len(sent) == 1 and blocked.email not in [s["to"] for s in sent]  # cap of 1 a day, suppressed never
    followup_clock.now += timedelta(days=1)
    _auto(d)
    assert len(sent) == 2 and blocked.email not in [s["to"] for s in sent]
    from app.autopilot.service import update_settings
    update_settings(d["vid"], allow_send=False)
    followup_clock.now += timedelta(days=1)
    assert _auto(d) is None and len(sent) == 2  # turning Allow sending off stops everything


def test_each_prospect_gets_a_rotated_playbook_and_experiments_report_it(journey_db, monkeypatch):
    from app.autopilot import outreach
    from app.gtm import journey
    d = journey_db
    sent = _setup(d, monkeypatch, n=2)
    keys = [draft["playbook"] for draft in outreach.drafts(d["vid"], "draft")]
    assert len(set(keys)) == 2  # balanced across the venture's playbooks, not all on the first
    journey.update_settings(d["wid"], d["vid"], {"auto_approve_first_emails": True})
    _auto(d)
    assert len(sent) == 2
    first = outreach.contacts(d["vid"])[0]
    _reply(d, first)
    experiments = {row["playbook"]: row for row in journey.status(d["wid"], d["vid"])["experiments"]}
    assert set(keys) <= set(experiments)
    winner = next(x["playbook"] for x in outreach.drafts(d["vid"], "sent") if x["contact_id"] == first.id)
    assert experiments[winner]["sent"] == 1
    assert (experiments[winner]["replies"], experiments[winner]["positive"], experiments[winner]["reply_rate"]) == (1, 1, 1.0)
    other = next(k for k in keys if k != winner)
    assert (experiments[other]["sent"], experiments[other]["replies"], experiments[other]["reply_rate"]) == (1, 0, 0.0)


@pytest.mark.parametrize("product,idea,audience,problem", [
    ("Order board", "Keep bakery orders in one place", "Independent bakery owners", "bakery orders phone"),
    ("Plant calendar", "Track watering for balcony plants", "Balcony gardeners", "remembering watering days"),
])
def test_composed_email_is_transparent_and_has_no_dashes(journey_db, monkeypatch, product, idea, audience, problem):
    from app.autopilot import outreach
    from app.credits import metered
    from app.db.models import Venture
    from app.gtm import playbooks
    from app.gtm.relationships.email_compliance import no_dashes
    d = journey_db
    with d["factory"]() as session:
        venture = session.get(Venture, d["vid"])
        venture.name, venture.raw_idea = product, idea
        session.commit()
    monkeypatch.setattr(outreach, "_brief", lambda vid: {"idea": {
        "audience": audience, "problem": problem, "confirmed": True}})
    monkeypatch.setenv("LLM_OFFLINE", "0")
    monkeypatch.setenv("LLM_CASSETTE_MODE", "off")
    calls = []

    async def model(messages, **kwargs):
        calls.append(kwargs["purpose"])
        intro = messages[0]["content"].split("word for word: \"", 1)[1].split("\".", 1)[0]
        body = (f"Hi Ada0,\n\n{intro} Your contact page mentions {problem} — a recurring challenge. "
                f"I'm trying to learn how {audience.lower()} handle it today, and I'm not selling anything. "
                "Would you share how you handle it in a short reply?")
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps({"subject": f"{problem} — a question", "body": body})))])
    monkeypatch.setattr(metered, "llm", model)
    _setup(d, monkeypatch, n=1)
    draft = outreach.drafts(d["vid"], "draft")[0]
    assert calls == ["outreach.draft_batch"] and draft["source"] == "model"  # one metered call per email
    assert "—" not in draft["body"] + draft["subject"] and "–" not in draft["body"]
    assert draft["body"].startswith("Hi Ada0,") and f"building {product}" in draft["body"]
    assert "no thanks" in draft["body"] and "Source:" not in draft["body"]
    assert "Your email address is listed publicly on bakery0.example." in draft["body"]
    from app.gtm.relationships.unsubscribe import REPLY_OPT_OUT, unsubscribe_url
    opt_out = unsubscribe_url(d["wid"], draft["contact_id"], draft["to"])
    assert ("Stop these emails: " + opt_out if opt_out else REPLY_OPT_OUT) in draft["body"]
    assert draft["source_url"] == "https://bakery0.example/contact"
    message = playbooks.messages({"company": "Acme — Dental"}, "design_partner", "An AI receptionist — fills gaps.", intro="I'm Sam, building X.")
    assert "—" not in json.dumps(message, ensure_ascii=False)
    assert no_dashes("Last note — Ann. Days 3–5.") == "Last note, Ann. Days 3-5."


def _cadence_days(d, monkeypatch, cadence, clock, days):
    """Days of the drafts sent to one quiet prospect over two weeks on the given cadence."""
    from app.autopilot import outreach
    from app.gtm import journey
    sent = _setup(d, monkeypatch)
    journey.update_settings(d["wid"], d["vid"], {"auto_approve_first_emails": True})
    if cadence is not None:
        _choose_cadence(d, cadence)
    started = clock.now
    _auto(d)
    first = outreach.drafts(d["vid"], "sent")[0]
    assert len(sent) == 1 and first["sent_at"] == started.isoformat()
    for after in range(1, 15):
        clock.now = started + timedelta(days=after, seconds=-1)
        assert outreach.schedule_followups(d["vid"]) == 0
        assert _auto(d) is None
        clock.now = started + timedelta(days=after)
        due = after + 1 in days
        assert outreach.schedule_followups(d["vid"]) == int(due)
        assert outreach.schedule_followups(d["vid"]) == 0
        queued = outreach.drafts(d["vid"], "draft")
        assert len(queued) == int(due)
        if due:
            assert queued[0]["day"] == after + 1
            assert queued[0]["body"].startswith("Hi Ada0,\n\n")
            assert {1: "One thing I left out yesterday:", 2: "Another way to look at it:",
                    3: "Another way to look at it:", 4: "If someone else on your team owns this,",
                    7: "I'll close the loop here"}[after] in queued[0]["body"]
            assert queued[0]["review_state"] == "draft" and queued[0]["requires_journey_review"]
        before = len(sent)
        _auto(d)
        assert len(sent) - before == int(due)
        assert not outreach.drafts(d["vid"], "draft")
        if due:
            delivered = next(draft for draft in outreach.drafts(d["vid"], "sent") if draft["day"] == after + 1)
            assert delivered["sent_at"] == clock.now.isoformat()
            assert sent[-1]["body"] == queued[0]["body"]
            assert sent[-1]["thread"] == f"thread-{first['to']}"
    assert next(draft for draft in outreach.drafts(d["vid"], "sent") if draft["day"] == 1)["sent_at"] == first["sent_at"]
    return sorted(x["day"] for x in outreach.drafts(d["vid"], "sent")), sent


@pytest.mark.parametrize("cadence,days", [([1, 2, 4, 7], [1, 2, 3, 5, 8]), ([2, 4, 7], [1, 3, 5, 8]), ([3, 7], [1, 4, 8]), ([], [1]), (None, [1, 3, 5, 8])])
def test_each_cadence_choice_sets_the_follow_up_days(journey_db, monkeypatch, followup_clock, cadence, days):
    sent_days, sent = _cadence_days(journey_db, monkeypatch, cadence, followup_clock, days)
    assert sent_days == days and len(sent) == len(days)
    assert not any("\u2014" in s["body"] or "\u2013" in s["body"] for s in sent)


@pytest.mark.parametrize("event", ["reply", "bounce", "unsubscribe"])
@pytest.mark.parametrize("queued", [False, True])
def test_followups_stop_before_drafting_and_before_sending(journey_db, monkeypatch, followup_clock, event, queued):
    from app.autopilot import outreach
    from app.db.models import GtmTouch
    from app.gtm import journey
    from app.gtm.relationships import unsubscribe
    from app.gtm.supymem import pipeline

    d = journey_db
    sent = _setup(d, monkeypatch)
    journey.update_settings(d["wid"], d["vid"], {"auto_approve_first_emails": True})
    _auto(d)
    assert len(sent) == 1
    contact = outreach.contacts(d["vid"])[0]
    followup_clock.now += timedelta(days=2)
    if queued:
        assert outreach.schedule_followups(d["vid"]) == 1
    if event == "reply":
        _reply(d, contact)
    elif event == "bounce":
        assert pipeline.apply_bounce(contact.id, reason="5.1.1", hard_bounce=True)
    else:
        token = unsubscribe.issue_unsubscribe_token(d["wid"], contact.id, contact.email)
        assert unsubscribe.honor_unsubscribe_token(token)["ok"]
    for advance in (0, 2, 3, 7):
        followup_clock.now += timedelta(days=advance)
        assert outreach.schedule_followups(d["vid"]) == 0
        assert _auto(d) is None and len(sent) == 1
    with d["factory"]() as session:
        touches = list(session.scalars(select(GtmTouch).where(GtmTouch.venture_id == d["vid"], GtmTouch.direction == "out")))
        assert len(touches) == 1
    _assert_send_spend(d, 1)


def test_none_also_holds_an_already_queued_followup(journey_db, monkeypatch, followup_clock):
    from app.autopilot import outreach
    from app.gtm import journey

    d = journey_db
    sent = _setup(d, monkeypatch)
    journey.update_settings(d["wid"], d["vid"], {"auto_approve_first_emails": True})
    _auto(d)
    assert len(sent) == 1
    followup_clock.now += timedelta(days=2)
    assert outreach.schedule_followups(d["vid"]) == 1
    _choose_cadence(d, [])
    assert _auto(d) is None
    followup_clock.now += timedelta(days=12)
    assert outreach.schedule_followups(d["vid"]) == 0
    assert _auto(d) is None and len(sent) == 1


def test_default_daily_cap_is_shared_by_first_emails_and_followups(journey_db, monkeypatch, followup_clock):
    from app.autopilot import guard, outreach
    from app.gtm import journey

    d = journey_db
    sent = _setup(d, monkeypatch, n=16)
    journey.update_settings(d["wid"], d["vid"], {"auto_approve_first_emails": True})
    started = followup_clock.now
    assert guard.get_settings(d["vid"])["send_max_per_day"] == 15
    _auto(d)
    assert len(sent) == 15 and outreach.send_slots_used_today(d["vid"]) == 15
    assert _auto(d) is None and len(outreach.drafts(d["vid"], "draft")) == 1
    followup_clock.now = started + timedelta(days=1)
    _auto(d)
    assert len(sent) == 16 and outreach.send_slots_used_today(d["vid"]) == 1
    followup_clock.now = started + timedelta(days=2)
    assert outreach.schedule_followups(d["vid"]) == 15
    _prospect(d, monkeypatch, 16)
    _auto(d)
    assert len(sent) == 31 and all(mail["thread"] for mail in sent[16:])
    assert outreach.send_slots_used_today(d["vid"]) == guard.get_settings(d["vid"])["sends_today"] == 15
    assert _auto(d) is None and len(sent) == 31
    assert outreach.schedule_followups(d["vid"]) == 0
    assert [draft["day"] for draft in outreach.drafts(d["vid"], "draft")] == [1]
    followup_clock.now = started + timedelta(days=3)
    assert outreach.schedule_followups(d["vid"]) == 1
    _auto(d)
    assert len(sent) == 33
    assert sum(mail["thread"] is not None for mail in sent[-2:]) == 1
    assert outreach.send_slots_used_today(d["vid"]) == 2
    assert not outreach.drafts(d["vid"], "draft")


def test_invalid_cadence_is_refused_and_the_default_cap_is_15(journey_db):
    from app.autopilot import guard
    from app.autopilot.service import update_settings
    from app.gtm import journey
    d = journey_db
    assert guard.DEFAULT_SEND_MAX == 15
    assert update_settings(d["vid"], allow_send=True)["send_max_per_day"] == 15
    assert update_settings(d["vid"], allow_send=True, send_max_per_day=40, confirmed_send_max=True, actor_subject=d["actor"])["send_max_per_day"] == 40  # an explicit cap is kept
    assert update_settings(d["vid"], allow_send=True)["send_max_per_day"] == 40
    with pytest.raises(ValueError):
        journey.update_settings(d["wid"], d["vid"], {"followup_days": [1, 5]})
