import asyncio
import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from tests.unit.gtm.test_journey import journey_db
from tests.unit.test_outreach_experiment_splits import contact, draft, outreach_case, proposal, save_experiment


def test_send_windows_preserve_approvals_dst_fallback_normal_sends_and_slot_time_gates(outreach_case, monkeypatch):
    from app.approvals import external, service as approvals
    from app.autopilot import guard, outreach, service as autopilot
    from app.core import jobs
    from app.db.models import Approval, GtmContact, GtmTouch
    from app.db.supymem_models import Experiment
    from app.experiments import autonomous, outreach_tests
    from app.gtm import journey
    from app.gtm.relationships import cadence
    from core import clock
    from core.clock import as_utc
    from core.ids import new_id

    case = outreach_case
    current = SimpleNamespace(now=as_utc(datetime(2026, 10, 24, 5)))
    for module in (clock, guard, outreach, autopilot, journey, cadence, external, approvals, jobs, outreach_tests):
        monkeypatch.setattr(module, "utcnow", lambda: current.now)
    monkeypatch.setattr("app.chat.reminders.founder_zone", lambda vid: ZoneInfo("Asia/Tokyo"))
    spec = proposal(case, "E21")
    people = [contact(case, index + 700) for index in range(4)]
    zone, city = ("Europe/London", "London") if case["consumer"] else ("America/New_York", "New York")
    with case["factory"]() as session:
        for person in people:
            row = session.get(GtmContact, person)
            row.contact_provenance = "founder_import"
            row.enrichment_json = json.dumps({**json.loads(row.enrichment_json), "city": city,
                "founder_import": {"lawful_basis_attested": True, "email_check": "mx"}})
        session.commit()
    assignments = {people[0]: "arm_0", people[1]: "arm_1", people[3]: "arm_0"}
    save_experiment(case, spec, assignments)
    with case["factory"]() as session:
        row = session.get(Experiment, spec["id"])
        row.context = json.dumps({**json.loads(row.context), "run_id": "timing-run"})
        session.commit()
    saved = [draft(case, spec, person, assignments[person]) for person in assignments]
    assert outreach_tests.send_block(case["vid"], saved[0]) == "experiment_review_required"
    slot = outreach_tests.send_slot(case["vid"], saved[0])
    assert slot["timezone"] == zone and datetime.fromisoformat(slot["due_at"]).astimezone(ZoneInfo(zone)).hour == 7
    assert outreach_tests.recipient_zone(SimpleNamespace(enrichment_json='{}', timezone="Asia/Tokyo", recipient_geo=None), ZoneInfo("UTC"))[0].key == "Asia/Tokyo"
    unknown = SimpleNamespace(enrichment_json='{"timezone": "Unknown/Zone", "country": "ZZ"}', timezone=None, recipient_geo=None)
    assert outreach_tests.recipient_zone(unknown, ZoneInfo("Asia/Tokyo")) == (ZoneInfo("Asia/Tokyo"), "your timezone fallback")
    confirmed_country = SimpleNamespace(enrichment_json='{"country": "ZZ"}', timezone=None, recipient_geo="GB")
    assert outreach_tests.recipient_zone(confirmed_country, ZoneInfo("Asia/Tokyo"))[0].key == "Europe/London"
    with case["factory"]() as session:
        person = session.get(GtmContact, people[0])
        original, original_geo = person.enrichment_json, person.recipient_geo
        person.enrichment_json, person.recipient_geo = unknown.enrichment_json, None
        session.commit()
    fallback = outreach_tests.send_slot(case["vid"], saved[0])
    assert fallback["timezone"] == "Asia/Tokyo" and datetime.fromisoformat(fallback["due_at"]).astimezone(ZoneInfo("Asia/Tokyo")).hour == 7
    with case["factory"]() as session:
        person = session.get(GtmContact, people[0])
        person.enrichment_json, person.recipient_geo = original, original_geo
        session.commit()
    ambiguous = SimpleNamespace(enrichment_json='{"country": "US"}', timezone=None, recipient_geo="US")
    assert outreach_tests.recipient_zone(ambiguous, ZoneInfo("Asia/Tokyo"))[0].key == "Asia/Tokyo"
    transition = datetime(2026, 10, 24 if case["consumer"] else 31, 6, tzinfo=ZoneInfo(zone))
    before = outreach_tests.send_slot(case["vid"], saved[0], now=transition)
    after = outreach_tests.send_slot(case["vid"], saved[0], now=transition, next_day=True)
    assert datetime.fromisoformat(after["due_at"]) - datetime.fromisoformat(before["due_at"]) == timedelta(hours=25)
    spring = datetime(2026, 3, 28 if case["consumer"] else 7, 6, tzinfo=ZoneInfo(zone))
    before = outreach_tests.send_slot(case["vid"], saved[0], now=spring)
    after = outreach_tests.send_slot(case["vid"], saved[0], now=spring, next_day=True)
    assert datetime.fromisoformat(after["due_at"]) - datetime.fromisoformat(before["due_at"]) == timedelta(hours=23)
    normal_id = outreach._save_draft(case["vid"], {"contact_id": people[2], "to": "hello@lead702.example", "channel": "email",
        "day": 1, "state": "draft", "subject": "A research question", "body": "Could you describe your recent experience?"})
    saved.append(next(item for item in outreach.drafts(case["vid"]) if item["doc_id"] == normal_id))
    assert outreach_tests.send_slot(case["vid"], saved[-1]) is None
    autopilot.update_settings(case["vid"], allow_send=True, send_max_per_day=15)
    sent = []
    monkeypatch.setattr(outreach, "_send_one", lambda box, to, subject, body, thread: sent.append(to) or {"provider_message_id": new_id("msg")})
    box, approval_ids = outreach._mailbox(case["wid"]), {}
    for item in saved:
        item.update(batch_id="timing-batch", requires_batch_approval=True, requires_journey_review=True, review_state="approved")
        outreach._save_draft(case["vid"], item, item["doc_id"])
        proposed = external.propose_external_approval(workspace_id=case["wid"], subject_ref=item["doc_id"], action="gtm_email_send",
            target=outreach.journey_send_target(case["vid"], item, box), summary="Approve your research email", review={})
        approvals.decide(proposed["id"], decision="approved", decided_by=case["actor"], surface="console")
        approval_ids[item["doc_id"]] = proposed["id"]
    payload = {"batch_id": "timing-batch", "approved_by_founder": True, "journey_approved_ids": list(approval_ids), "journey_approvals": approval_ids}
    reply = asyncio.run(outreach.send_batch(case["vid"], payload, actor_subject=case["actor"]))
    assert len(sent) == 1 and "3 approved experiment" in reply["body_md"], reply["body_md"]
    queued = outreach.drafts(case["vid"], "draft")
    assert len(queued) == 3 and all(item["scheduled_send"]["approval_id"] == approval_ids[item["doc_id"]] for item in queued)
    cap_counter = outreach.send_slots_used_today

    def dispatch_at(when):
        current.now = as_utc(when)
        with case["factory"]() as session:
            leased = jobs.lease(session, "timing-worker", kinds=[autonomous.KIND], now=current.now, n=10)
            session.commit()
        assert leased
        for job in leased:
            data = {**job.payload, "_job_id": job.id, "_worker_id": job.worker_id, "_job_attempt": job.attempts}
            asyncio.run(autonomous.run_job(data))
            asyncio.run(autonomous.run_job(data))
            with case["factory"]() as session:
                jobs.complete_job(session, job.id, worker_id=job.worker_id, attempt=job.attempts)
                session.commit()

    send_batch = outreach.send_batch
    async def busy(*args, **kwargs):
        return outreach._reply("Your send is already in progress. Your approval stays saved.", "wait")
    monkeypatch.setattr(outreach, "send_batch", busy)
    dispatch_at(datetime.fromisoformat(slot["due_at"]))
    retry = next(item for item in outreach.drafts(case["vid"], "draft") if item["doc_id"] == saved[0]["doc_id"])
    retry_at = datetime.fromisoformat(retry["scheduled_send"]["due_at"])
    assert retry_at - datetime.fromisoformat(slot["due_at"]) == timedelta(minutes=5)
    monkeypatch.setattr(outreach, "send_batch", send_batch)
    monkeypatch.setattr(outreach, "send_slots_used_today", lambda vid: 15)
    dispatch_at(retry_at)
    assert len(sent) == 1
    postponed = next(item for item in outreach.drafts(case["vid"], "draft") if item["doc_id"] == saved[0]["doc_id"])
    due = datetime.fromisoformat(postponed["scheduled_send"]["due_at"])
    assert due > datetime.fromisoformat(slot["due_at"])
    monkeypatch.setattr(outreach, "send_slots_used_today", cap_counter)
    with case["factory"]() as session:
        session.get(GtmContact, people[3]).do_not_contact = 1
        session.get(Approval, approval_ids[saved[1]["doc_id"]]).status = "rejected"
        session.commit()
    dispatch_at(due)
    assert len(sent) == 2 and len(outreach.drafts(case["vid"], "sent")) == 2
    assert any(item["contact_id"] == people[3] for item in outreach.drafts(case["vid"], "skipped"))
    rejected = next(item for item in outreach.drafts(case["vid"], "draft") if item["contact_id"] == people[1])
    dispatch_at(datetime.fromisoformat(rejected["scheduled_send"]["due_at"]))
    rejected = next(item for item in outreach.drafts(case["vid"], "draft") if item["contact_id"] == people[1])
    assert len(sent) == 2 and rejected["error"] == "external_approval_not_approved"
    with case["factory"]() as session:
        outbound = list(session.scalars(select(GtmTouch).where(GtmTouch.venture_id == case["vid"], GtmTouch.direction == "out")))
        assert len(outbound) == 2 and all(outreach.OPT_OUT in touch.body_text for touch in outbound)
    counts_people = [contact(case, index + 800) for index in range(20)]
    for sample, responders, expected in ((20, {0, 1, 10}, "supported"), (20, {0, 10, 11}, "refuted"),
            (19, {0, 1, 10}, "inconclusive"), (20, {0, 10}, "inconclusive")):
        current.now += timedelta(seconds=1)
        observed = {**spec, "id": new_id("exp")}
        with case["factory"]() as session:
            for index, person in enumerate(counts_people[:sample]):
                session.add(GtmTouch(id=new_id("gtmtch"), workspace_id=case["wid"], venture_id=case["vid"], contact_id=person,
                    direction="out", channel="email", simulated=0, sent_at=current.now, created_at=current.now,
                    honesty_json=json.dumps({"experiment_id": observed["id"], "variant": f"arm_{index // 10}", "day": 1})))
                if index in responders:
                    session.add(GtmTouch(id=new_id("gtmtch"), workspace_id=case["wid"], venture_id=case["vid"], contact_id=person,
                        direction="in", channel="email", simulated=0, classification="human_neutral", body_text="Here is my experience.", created_at=current.now))
            session.commit()
        current.now += timedelta(days=observed.get("response_window_days", observed["max_duration_days"]))
        result = outreach_tests.observe(observed)
        assert result["outcome"] == expected and f"Outcome: {expected}" in result["insight"]
        assert result["measured"]["winner"] == ("arm_0" if expected == "supported" else None)
    card = outreach_tests.confirmation(spec)
    assert card["actions"][0]["label"] == "Run it for me" and len(card["actions"]) == 1


def test_booking_link_drafts_deterministic_confirmation_counts_and_corrections(outreach_case, monkeypatch):
    from app.autopilot import outreach
    from app.chat import experiments
    from app.db.models import GtmContact, GtmTouch
    from app.db.supymem_models import Experiment
    from app.experiments import autonomous, outreach_tests
    from app.gtm import journey
    from core.clock import utcnow
    from core.ids import new_id

    case = outreach_case
    link = "https://appointments.example/research?type=15" if case["consumer"] else "https://meetings.example/interview#slots"
    for valid in (link, "https://calendar.example:443/choose", "https://[::1]/choose"):
        assert outreach_tests.validate_booking_link(valid) == valid
    international = outreach_tests.validate_booking_link("https://予約.example/面談\u2014時間")
    assert "\u2014" not in international and "%E2%80%94" in international
    assert outreach_tests.validate_booking_link(international) == international
    for invalid in (None, "http://calendar.example", "javascript:alert(1)", "https:///choose", "https://user:pass@calendar.example",
            "https://bad..example", "https://calendar.example:abc", "https://calendar.example\n/x", "https://-bad.example", "https://calendar.example\\@evil.example"):
        with pytest.raises(ValueError, match="https URL"):
            outreach_tests.validate_booking_link(invalid)
    assert outreach_tests.confirmation(proposal(case, "E26"))["kind"] == "question"
    configured = asyncio.run(experiments.dispatch(case["vid"], "experiment_start", f"start an interview booking test with {link}"))
    from app.core.local_mode import is_local
    assert configured["kind"] == ("experiment_result" if is_local() else "confirmation")
    assert configured["actions"][0]["label"] == ("Run your interview booking test" if is_local() else "Run it for me")
    if is_local():
        assert configured["actions"][0]["action"] == "experiment.prepare"
        assert not any(action.get("confirmation") for action in configured["actions"])
    assert outreach_tests.booking_link(case["vid"]) == link
    assert outreach_tests.booking_link("ven_journey") is None
    spec = proposal(case, "E26")
    assert spec["min_sample"] == 30 and spec["max_duration_days"] == 10
    people = [contact(case, index + 900) for index in range(30)]
    with case["factory"]() as session:
        for person in people:
            row = session.get(GtmContact, person)
            row.contact_provenance = "founder_import"
            row.enrichment_json = json.dumps({**json.loads(row.enrichment_json),
                "founder_import": {"lawful_basis_attested": True, "email_check": "mx"}})
        existing = session.get(Experiment, spec["id"])
        session.delete(existing)
        session.commit()
    save_experiment(case, spec, dict.fromkeys(people, "arm_0"))
    assert journey._draft_ready_admitted(case["wid"], case["vid"], review_only=True, experiment={**spec, "assignments": dict.fromkeys(people, "arm_0")}) > 0
    initial = outreach.drafts(case["vid"], "draft")
    assert initial and all(link in item["body"] and "15-minute" in item["body"] for item in initial)
    from app.gtm.relationships.drafts import body_has_calendar
    assert all(body_has_calendar(item["body"]) for item in initial)
    assert all(("Your email address is listed publicly on" in item["body"] or "I have your address from my own contacts." in item["body"])
        and outreach.OPT_OUT in item["body"] for item in initial)
    followup_id = outreach._save_draft(case["vid"], {**initial[0], "day": 3, "body": "Would a short interview work for you?"})
    followup = next(item for item in outreach.drafts(case["vid"], "draft") if item["doc_id"] == followup_id)
    assert link in followup["body"] and outreach.OPT_OUT in followup["body"]
    for text in ("I've booked a call.", "We have scheduled the interview.", "Booking confirmed.", "Booked!", "I've booked it.", "I reserved a slot.", "I booked an interview.", "I've booked an appointment."):
        assert outreach_tests.booked_reply(text)
    for text in ("I haven't booked a call.", "Can I book a time?", "I will book a call.", "I booked a hotel.",
            "We cancelled the booking.", "Automatic reply: I've booked a call.", "No thanks.\n> I've booked a call.", "Please say booked."):
        assert not outreach_tests.booked_reply(text)
    headers = {"Content-Type": "text/calendar; method=REPLY"}
    assert outreach_tests.booked_reply("PARTSTAT=ACCEPTED", headers=headers)
    assert outreach_tests.booked_reply("PARTSTAT=ACCEPTED\nURL:https://calendar.example/event?id=1", headers=headers)
    assert outreach_tests.booked_reply("", headers=headers, subject="Accepted: Research interview")
    assert not outreach_tests.booked_reply("PARTSTAT=DECLINED", headers=headers)
    assert not outreach_tests.booked_reply("", headers=headers, subject="Invitation: Research interview")
    now = utcnow() - timedelta(days=spec.get("response_window_days", spec["max_duration_days"]))
    with case["factory"]() as session:
        for index, person in enumerate(people):
            session.add(GtmTouch(id=new_id("gtmtch"), workspace_id=case["wid"], venture_id=case["vid"], contact_id=person,
                direction="out", channel="email", simulated=0, sent_at=now, created_at=now,
                honesty_json=json.dumps({"experiment_id": spec["id"], "variant": "arm_0", "day": 1})))
            if index < 4:
                session.add(GtmTouch(id=new_id("gtmtch"), workspace_id=case["wid"], venture_id=case["vid"], contact_id=person,
                    direction="in", channel="email", simulated=0, message_id=f"booking-{index}", classification="automated" if index in {2, 3} else "human_neutral",
                    body_text="I've booked a call." if index != 2 else "", created_at=now))
        session.commit()
    outreach_tests.capture_booking_headers(case["vid"], "booking-2", {"content-type": "text/calendar; method=REPLY", "subject": "Accepted: Interview", "from": "private@example"})
    result = outreach_tests.observe(spec)
    assert result["measured"]["booked"] == 3 and result["outcome"] == "supported"
    assert len(result["booking_replies"]) == 4 and sum(reply["counted"] for reply in result["booking_replies"]) == 3
    readout = asyncio.run(experiments.execute(case["vid"], "experiment.booking.readout", {"experiment_id": spec["id"]}, case["actor"]))
    assert ("3 confirmed bookings from 30" if is_local() else "3 / 30") in readout["body_md"]
    assert "Your reviewed replies" in readout["body_md"]
    for person in people[:3]:
        response = asyncio.run(experiments.execute(case["vid"], "experiment.booking.mark", {"experiment_id": spec["id"], "contact_id": person, "booked": False}, case["actor"]))
    assert outreach_tests.observe(spec)["outcome"] == "refuted"
    assert ("0 confirmed bookings from 30" if is_local() else "Outcome: refuted") in response["body_md"]
    if not is_local():
        assert "0 / 30" in response["body_md"]
    response = outreach_tests.mark_booking(case["vid"], {"experiment_id": spec["id"], "contact_id": people[4], "booked": True})
    assert outreach_tests.observe(spec)["outcome"] == "inconclusive"
    assert ("1 confirmed bookings from 30" if is_local() else "Outcome: inconclusive") in response["body_md"]
    if not is_local():
        assert "1 / 30" in response["body_md"]
    with pytest.raises(ValueError, match="contacted"):
        outreach_tests.mark_booking(case["vid"], {"experiment_id": spec["id"], "contact_id": "unrelated-contact", "booked": True})
    smaller = {**spec, "id": new_id("exp")}
    with case["factory"]() as session:
        for person in people[:29]:
            session.add(GtmTouch(id=new_id("gtmtch"), workspace_id=case["wid"], venture_id=case["vid"], contact_id=person,
                direction="out", channel="email", simulated=0, sent_at=now + timedelta(seconds=1), created_at=now + timedelta(seconds=1),
                honesty_json=json.dumps({"experiment_id": smaller["id"], "variant": "arm_0", "day": 1})))
        session.commit()
    result = outreach_tests.observe(smaller)
    assert result["measured"]["events"] == 29 and result["outcome"] == "inconclusive"


def test_interview_bookings_use_connected_events_and_keep_flag_off_behavior(outreach_case, monkeypatch):
    from app.connections import bookings
    from app.content.oauth import callbacks
    from app.credits import policy
    from app.db.models import ContentOAuthState, GtmContact, GtmTouch
    from app.db.supymem_models import Experiment
    from app.experiments import autonomous, outreach_tests
    from app.gtm.supymem import composio_gmail
    from core.clock import utcnow
    from core.ids import new_id
    from tests.unit.test_booking_connections import calendar_event, metering

    case, now, calls, events, flags = outreach_case, utcnow(), [], [], {}
    original = policy.get_policy
    monkeypatch.setattr(policy, "get_policy", lambda key, default=None: flags if key == "feature_flags" else original(key, default))
    monkeypatch.setattr(callbacks, "_verified_connection", lambda account: {"status": "ACTIVE"})
    metering(monkeypatch)
    def execute(slug, **kwargs):
        assert slug in {"GOOGLECALENDAR_LIST_CALENDARS", "GOOGLECALENDAR_FIND_EVENT"} and kwargs["user_id"] == case["actor"]
        assert kwargs["connected_account_id"] == "interview_calendar"
        calls.append(kwargs)
        return {"successful": True, "data": {"items": [{"id": "primary"}] if slug == "GOOGLECALENDAR_LIST_CALENDARS" else events}}
    monkeypatch.setattr(composio_gmail, "execute_tool", execute)
    from app.core.local_mode import is_local
    if is_local():
        from app.content import composio_social
        def local_request(method, path, *, json_body, **kwargs):
            assert method == "POST"
            return execute(path.rsplit("/", 1)[-1], user_id=json_body["user_id"],
                connected_account_id=json_body["connected_account_id"], arguments=json_body["arguments"])
        monkeypatch.setattr(composio_social, "_toolkit_version", lambda *_args: "latest")
        monkeypatch.setattr(composio_social, "_request", local_request)
    spec = proposal(case, "E26")
    now -= timedelta(days=spec.get("response_window_days", spec["max_duration_days"]))
    people = [contact(case, index + 1200) for index in range(3)]
    save_experiment(case, spec, dict.fromkeys(people[:2], "arm_0"))
    with case["factory"]() as session:
        for index, person in enumerate(people[:2]):
            session.add(GtmTouch(id=new_id("gtmtch"), workspace_id=case["wid"], venture_id=case["vid"], contact_id=person,
                direction="out", channel="email", simulated=0, sent_at=now, created_at=now,
                honesty_json=json.dumps({"experiment_id": spec["id"], "variant": "arm_0", "day": 1})))
            session.add(GtmTouch(id=new_id("gtmtch"), workspace_id=case["wid"], venture_id=case["vid"], contact_id=person,
                direction="in", channel="email", simulated=0, classification="human_neutral",
                body_text="I booked a call.", created_at=now + timedelta(seconds=1)))
        row = session.get(Experiment, spec["id"])
        row.context = json.dumps({**json.loads(row.context), "booking_marks": {people[1]: True}})
        account = ContentOAuthState(id="interview_account", workspace_id=case["wid"], platform="google_calendar",
            actor_subject=case["actor"], auth_method="composio", provider_user_id=case["actor"],
            auth_config_id="interview_auth", connected_account_id="interview_calendar", outcome="connected",
            consumed_at=now, created_at=now, expires_at=now, redirect_uri="https://example.org/callback")
        session.add(account)
        events.extend(calendar_event(now + timedelta(days=20), session.get(GtmContact, person).email) for person in (people[0], people[2]))
        session.commit()
    assert outreach_tests.observe(spec)["measured"]["booked"] == 2 and not calls
    flags["experiments_autopilot_v1"] = True
    from app.core.local_mode import is_local
    from app.core.local_secrets import update_settings
    if is_local():
        update_settings("capabilities", {"connected_apps": True}, {})
        update_settings("composio", {"auth_configs": {"googlecalendar": "interview_auth"}}, {"api_key": "test-calendar-key"})
    result = outreach_tests.observe(spec)
    assert result["measured"]["booked"] == 1 and result["booking_signal"]["available"]
    assert {person["contact_id"]: person["counted"] for person in result["booking_contacts"]} == {people[0]: True, people[1]: False}
    assert {reply["contact_id"]: reply["counted"] for reply in result["booking_replies"]} == {people[0]: True, people[1]: False}
    assert all(reply["source"] == "your booking account" for reply in result["booking_replies"])
    readout = outreach_tests.booking_readout(case["vid"], {"experiment_id": spec["id"]})
    assert not any(action.get("action") == "experiment.booking.mark" and action["payload"]["booked"] for action in readout["actions"])
    events[0]["status"] = "cancelled"
    assert outreach_tests.observe(spec)["measured"]["booked"] == 0
    def unavailable(*args, **kwargs):
        raise RuntimeError("No calendar response")
    monkeypatch.setattr(composio_gmail, "execute_tool", unavailable)
    if is_local():
        monkeypatch.setattr(composio_social, "_request", unavailable)
    result = outreach_tests.observe(spec)
    assert result["outcome"] == "inconclusive" and not result["booking_signal"]["available"]
    assert "could not be checked" in result["insight"]
    with case["factory"]() as session:
        session.get(ContentOAuthState, "interview_account").outcome = "revoked"
        session.commit()
    readout = outreach_tests.booking_readout(case["vid"], {"experiment_id": spec["id"]})
    assert sum(action["label"] == "Connect Google Calendar" for action in readout["actions"]) == 1
    result = outreach_tests.observe(spec)
    posted = autonomous.result_reply({**spec, "result": result})
    assert sum(action["label"] == "Connect Google Calendar" for action in posted["actions"]) == 1
    if is_local():
        update_settings("capabilities", {"connected_apps": False}, {})
        call_count = len(calls)
        disabled = bookings.read_bookings(object(), case["vid"], now, now, feature_flag="goals_v1")
        assert not disabled["available"] and not disabled["connected"] and disabled["bookings"] == []
        assert "Connected apps" in disabled["next_step"] and "Connections" in disabled["next_step"]
        assert len(calls) == call_count
    else:
        assert not bookings.read_bookings(object(), case["vid"], now, now, feature_flag="goals_v1")
    hypothetical = {**spec, "id": new_id("exp")}
    save_experiment(case, hypothetical, dict.fromkeys(people[:2], "arm_0"))
    with case["factory"]() as session:
        for person in people[:2]:
            session.add(GtmTouch(id=new_id("gtmtch"), workspace_id=case["wid"], venture_id=case["vid"], contact_id=person,
                direction="out", channel="email", simulated=0, sent_at=now + timedelta(seconds=2), created_at=now + timedelta(seconds=2),
                honesty_json=json.dumps({"experiment_id": hypothetical["id"], "variant": "arm_0", "day": 1})))
            body = ("If I booked a call, a reminder would be useful." if person == people[1]
                else "I booked a call.\n\n> If I booked a call, a reminder would be useful.")
            session.add(GtmTouch(id=new_id("gtmtch"), workspace_id=case["wid"], venture_id=case["vid"], contact_id=person,
                direction="in", channel="email", simulated=0, classification="human_neutral", body_text=body,
                created_at=now + timedelta(seconds=3)))
        session.commit()
    assert outreach_tests.observe(hypothetical)["measured"]["booked"] == 1
