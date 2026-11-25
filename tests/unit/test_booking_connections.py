"""Booking account boundaries and normalization across unrelated venture domains."""
import asyncio
from contextlib import contextmanager, nullcontext
from datetime import timedelta
import json
from types import SimpleNamespace

import pytest

from tests.unit.tasks.test_goals_v1 import contract, create, goal_env
from tests.unit.tasks.test_task_lifecycle import lifecycle


@pytest.fixture(autouse=True)
def local_booking_setup(goal_env, monkeypatch):
    from app.core.local_mode import is_local
    from app.core.local_secrets import update_settings
    from app.core import providers_local
    from app.content import composio_social
    from app.gtm.supymem import composio_gmail

    if not is_local():
        return

    def unavailable_keychain(*args):
        raise providers_local.LocalProviderError("Test keychain is unavailable")

    monkeypatch.setattr(providers_local, "_keychain", unavailable_keychain)
    update_settings("composio", {}, {"api_key": "test-only-booking-key"})
    update_settings("capabilities", {"connected_apps": True}, {})

    def request(method, path, *, json_body, timeout):
        assert method == "POST" and path.startswith("/tools/execute/")
        assert timeout == 60
        return composio_gmail.execute_tool(path.rsplit("/", 1)[-1],
            user_id=json_body["user_id"], connected_account_id=json_body["connected_account_id"],
            arguments=json_body["arguments"], toolkit_version=json_body["version"])

    monkeypatch.setattr(composio_social, "_request", request)


def connection(session, now, *, platform="google_calendar", user_id="usr_lifecycle", **changes):
    from app.db.models import ContentOAuthState
    values = {"id": "booking_account", "workspace_id": "ws_lifecycle", "platform": platform,
        "actor_subject": "usr_lifecycle", "auth_method": "composio", "provider_user_id": user_id,
        "auth_config_id": "booking_auth", "connected_account_id": "booking_connected",
        "outcome": "connected", "consumed_at": now, "created_at": now,
        "expires_at": now, "redirect_uri": "https://example.org/callback"}
    row = ContentOAuthState(**{**values, **changes})
    session.add(row)
    session.commit()
    return row


def calendar_event(now, email, **changes):
    return {"summary": "Your scheduled conversation", "status": "confirmed",
        "start": {"dateTime": now.isoformat()}, "end": {"dateTime": (now + timedelta(minutes=30)).isoformat()},
        "attendees": [{"email": email, "responseStatus": "accepted"}], **changes}


def metering(monkeypatch):
    from app.credits import metered
    charges = []
    @contextmanager
    def charge(**kwargs):
        charges.append(kwargs)
        yield SimpleNamespace(record_micro=lambda cost: charges.append(cost))
    monkeypatch.setattr(metered, "charge_sync", charge)
    return charges


def test_customer_organizers_count_without_counting_your_own_events(goal_env, monkeypatch):
    from app.connections import bookings
    from app.content.oauth import callbacks
    from app.db.models import GtmContact, Person
    from app.gtm.supymem import composio_gmail

    env = goal_env
    env["flags"]["experiments_autopilot_v1"] = True
    now = env["clock"][0]
    metering(monkeypatch)
    with env["factory"]() as session:
        session.get(Person, "usr_lifecycle").email = "founder@example.org"
        for identifier, email in (("customer", "customer@example.org"), ("yourself", "founder@example.org")):
            session.add(GtmContact(id=identifier, workspace_id="ws_lifecycle", email=email,
                source="venture:ven_lifecycle:public", created_at=now, stage_changed_at=now))
        connection(session, now)
    events = [calendar_event(now + timedelta(hours=1), "customer@example.org", summary=env["domain"],
        attendees=[{"email": "customer@example.org", "organizer": True, "responseStatus": "accepted"}]),
        calendar_event(now + timedelta(hours=2), "founder@example.org", attendees=[
            {"email": "founder@example.org", "organizer": True, "responseStatus": "accepted"}]),
        calendar_event(now + timedelta(hours=3), "customer@example.org", attendees=[
            {"email": "customer@example.org", "self": True, "responseStatus": "accepted"}])]
    def execute(slug, **kwargs):
        assert kwargs["user_id"] == "usr_lifecycle" and kwargs["connected_account_id"] == "booking_connected"
        assert slug in bookings.READ_TOOLS["google_calendar"]
        return {"successful": True, "data": {"items": [{"id": "primary"}]
            if slug == "GOOGLECALENDAR_LIST_CALENDARS" else events}}
    monkeypatch.setattr(composio_gmail, "execute_tool", execute)
    monkeypatch.setattr(callbacks, "_verified_connection", lambda account: {"status": "ACTIVE"})
    with env["factory"]() as session:
        signal = bookings.read_bookings(session, "ven_lifecycle", now, now + timedelta(days=1),
            feature_flag="experiments_autopilot_v1")
    assert signal["available"] and len(signal["bookings"]) == 1
    assert signal["bookings"][0]["contact_id"] == "customer"
    assert signal["bookings"][0]["title"] == env["domain"]


@pytest.mark.parametrize("platform", ["cal", "calendly", "google_calendar"])
@pytest.mark.parametrize("identity", ["usr_lifecycle", "content:ws_lifecycle"])
def test_booking_reader_is_owned_read_only_paginated_scoped_and_private(goal_env, monkeypatch, platform, identity):
    from app.connections import bookings
    from app.content.oauth import callbacks
    from app.db.models import GtmContact, GtmTouch, Person
    from app.gtm.supymem import composio_gmail

    env, calls, verified = goal_env, [], []
    env["flags"]["experiments_autopilot_v1"] = True
    now = env["clock"][0]
    charges = metering(monkeypatch)
    with env["factory"]() as session:
        session.get(Person, "usr_lifecycle").email = "founder@example.org"
        for identifier, email, source, workspace in (
            ("lead_one", "one@example.org", "venture:ven_lifecycle:public", "ws_lifecycle"),
            ("lead_two", "two@example.org", "founder_import", "ws_lifecycle"),
            ("other_venture", "other@example.org", "venture:ven_lifecycle_other:public", "ws_lifecycle"),
            ("other_workspace", "foreign@example.org", "venture:ven_lifecycle:public", "ws_other"),
            ("founder_contact", "founder@example.org", "venture:ven_lifecycle:public", "ws_lifecycle")):
            session.add(GtmContact(id=identifier, workspace_id=workspace, email=email, source=source,
                created_at=now, stage_changed_at=now))
        session.add(GtmTouch(id="imported_invitation", workspace_id="ws_lifecycle", venture_id="ven_lifecycle",
            contact_id="lead_two", direction="out", simulated=0, sent_at=now, created_at=now))
        connection(session, now, platform=platform, user_id=identity)
    def verify(account):
        assert account["actor_subject"] == "usr_lifecycle" and account["provider_user_id"] == identity
        verified.append(account["connected_account_id"])
        return {"status": "ACTIVE"}
    monkeypatch.setattr(callbacks, "_verified_connection", verify)
    valid = calendar_event(now, " ONE@example.org ", summary=env["domain"], description="Private unrelated notes")
    rows = [valid, valid, calendar_event(now, "other@example.org"), calendar_event(now, "foreign@example.org"),
        calendar_event(now, "founder@example.org"), calendar_event(now, "one@example.org", status="cancelled"),
        calendar_event(now, "one@example.org", attendees=[{"email": "one@example.org", "responseStatus": "declined"}]),
        calendar_event(now, "one@example.org", attendees=[{"email": "one@example.org", "organizer": True}]),
        calendar_event(now, "one@example.org", start={"date": now.date().isoformat()}),
        calendar_event(now, "one@example.org", start={"dateTime": now.replace(tzinfo=None).isoformat()}),
        calendar_event(now, "one@example.org", start={"dateTime": (now - timedelta(days=10)).isoformat()}),
        {"summary": "If I booked a call for " + env["domain"], "attendees": [{"email": "one@example.org"}]},
        calendar_event(now + timedelta(hours=1), "two@example.org", status="tentative")]
    later = calendar_event(now + timedelta(days=1), "two@example.org")
    def execute(slug, **kwargs):
        assert verified and kwargs["user_id"] == identity and kwargs["connected_account_id"] == "booking_connected"
        assert slug in bookings.READ_TOOLS[platform]
        calls.append((slug, kwargs["arguments"]))
        params = kwargs["arguments"]
        if slug == "GOOGLECALENDAR_LIST_CALENDARS":
            data = {"items": [{"id": "primary"}, {"id": "interviews"}]}
        elif slug == "CALENDLY_GET_CURRENT_USER":
            data = {"resource": {"uri": "https://api.calendly.com/users/owner"}}
        elif slug == "CALENDLY_LIST_EVENT_INVITEES":
            row = later if params["event"].endswith("later") else rows[int(params["event"].rsplit("/", 1)[1])]
            data = {"collection": [{"email": attendee["email"], "status": "active"}
                for attendee in row["attendees"] if not attendee.get("organizer") and attendee.get("responseStatus") != "declined"]}
        elif slug == "CALENDLY_LIST_EVENTS":
            selected = [later] if params.get("page_token") else rows
            data = {"collection": [{"name": row.get("summary"), "status": "active" if row.get("status") in {None, "confirmed"} else row["status"],
                "start_time": (row.get("start") or {}).get("dateTime"), "end_time": (row.get("end") or {}).get("dateTime"),
                "uri": "https://api.calendly.com/scheduled_events/" + ("later" if row is later else str(index))}
                for index, row in enumerate(selected)],
                "pagination": {} if params.get("page_token") else {"next_page_token": "next"}}
        elif slug == "CAL_GET_BOOKINGS":
            selected = [later] if params["skip"] else rows
            data = {"bookings": [{"title": row.get("summary"), "status": "ACCEPTED" if row.get("status") in {None, "confirmed"} else row["status"].upper(),
                "startTime": (row.get("start") or {}).get("dateTime"), "endTime": (row.get("end") or {}).get("dateTime"),
                "attendees": [attendee for attendee in row["attendees"] if not attendee.get("organizer") and attendee.get("responseStatus") != "declined"]}
                for row in selected], "hasMore": not params["skip"]}
        else:
            data = {"items": [later]} if params.get("pageToken") else {"items": rows, "nextPageToken": "next"}
        return {"successful": True, "data": {"data": data} if platform == "cal" else data}
    monkeypatch.setattr(composio_gmail, "execute_tool", execute)
    with env["factory"]() as session:
        signal = bookings.read_bookings(session, "ven_lifecycle", now - timedelta(days=1), now + timedelta(days=3),
            feature_flag="experiments_autopilot_v1")
        assert signal["available"] and signal["connected"]
        assert {row["contact_id"] for row in signal["bookings"]} == {"lead_one", "lead_two"}
        assert len(signal["bookings"]) == 2
        assert all(set(row) == {"contact_id", "title", "attendees", "starts_at", "ends_at"} for row in signal["bookings"])
        assert "Private unrelated notes" not in json.dumps(signal)
        assert all(charge == 0 or charge["workspace_id"] == "ws_lifecycle" and charge["provider"] == "composio" for charge in charges)
        assert len(charges) == 2 * len(calls)
        if platform == "google_calendar":
            assert {params["calendar_id"] for slug, params in calls if slug == "GOOGLECALENDAR_FIND_EVENT"} == {"primary", "interviews"}
        previous = len(calls)
        from app.core.local_mode import is_local
        from app.core.local_secrets import update_settings
        for value in (False, 1, "true"):
            env["flags"]["experiments_autopilot_v1"] = value
            if is_local():
                update_settings("capabilities", {"connected_apps": value}, {})
                blocked = bookings.read_bookings(object(), "ven_lifecycle", now, now,
                    feature_flag="experiments_autopilot_v1")
                assert not blocked["connected"] and not blocked["available"] and not blocked["bookings"]
                assert "Keys and connections" in blocked["next_step"]
            else:
                assert bookings.read_bookings(object(), "ven_lifecycle", now, now,
                    feature_flag="experiments_autopilot_v1") is None
        assert len(calls) == previous
        env["flags"]["experiments_autopilot_v1"] = True
        if is_local():
            update_settings("capabilities", {"connected_apps": True}, {})
        with pytest.raises(ValueError, match="booking_owner_missing"):
            bookings.read_bookings(session, "ven_lifecycle", now, now + timedelta(days=1),
                feature_flag="experiments_autopilot_v1", person_id="usr_other")
        for attr, value in (("outcome", "revoked"), ("outcome", "needs_reconnect"), ("actor_subject", "usr_other"),
                ("provider_user_id", "default"), ("consumed_at", None), ("auth_method", "native")):
            row = session.get(bookings.ContentOAuthState, "booking_account")
            old = getattr(row, attr)
            setattr(row, attr, value)
            session.flush()
            signal = bookings.read_bookings(session, "ven_lifecycle", now, now + timedelta(days=1),
                feature_flag="experiments_autopilot_v1")
            assert not signal["connected"] and signal["connection_action"]["label"].startswith("Connect ")
            assert set(signal["connection_action"]) == {"label", "action", "payload"} and len(calls) == previous
            assert signal["connection_action"]["action"] == "connections.connect"
            setattr(row, attr, old)
            session.flush()
        def inactive(account):
            raise callbacks.OAuthError("inactive", "Your account is not active.")
        monkeypatch.setattr(callbacks, "_verified_connection", inactive)
        signal = bookings.read_bookings(session, "ven_lifecycle", now, now + timedelta(days=1), feature_flag="goals_v1")
        assert not signal["available"] and not signal["bookings"] and len(calls) == previous
        assert signal["connection_action"]["label"] == "Connect " + bookings.PROVIDERS[platform]


def test_booking_goal_uses_connected_events_not_replies_and_offers_one_connection(goal_env, monkeypatch):
    from app.connections import bookings
    from app.chat import conversations
    from app.content.oauth import callbacks
    from app.db.models import ChatMessage, GtmContact, GtmTouch
    from app.db.task_models import Task, TaskStep
    from app.gtm.supymem import composio_gmail

    env, now, events = goal_env, goal_env["clock"][0], []
    task_id, _, steps = create(env, contract(env, steps=[{"title": "Check calls", "skill": "tasks.customer_calls",
        "subgoal": "Check booked conversations for " + env["domain"], "inputs": {"target_count": 2}}]))
    metering(monkeypatch)
    monkeypatch.setattr(callbacks, "_verified_connection", lambda account: {"status": "ACTIVE"})
    monkeypatch.setattr(conversations, "job_receipt_conversation", lambda *args, **kwargs: nullcontext(True))
    monkeypatch.setattr(composio_gmail, "execute_tool", lambda slug, **kwargs: {"successful": True,
        "data": {"items": [{"id": "primary"}] if slug == "GOOGLECALENDAR_LIST_CALENDARS" else events}})
    with env["factory"]() as session:
        account = connection(session, now)
        for index in range(2):
            session.add(GtmContact(id=f"lead_{index}", workspace_id="ws_lifecycle", email=f"lead{index}@example.org",
                source="venture:ven_lifecycle:public", created_at=now, stage_changed_at=now))
        session.add(GtmTouch(id="sent", workspace_id="ws_lifecycle", venture_id="ven_lifecycle", contact_id="lead_0",
            direction="out", simulated=0, sent_at=now - timedelta(hours=1), created_at=now - timedelta(hours=1)))
        session.add(GtmTouch(id="reply", workspace_id="ws_lifecycle", venture_id="ven_lifecycle", contact_id="lead_0",
            direction="in", simulated=0, body_text="I booked a call.", created_at=now))
        session.commit()
        task, step = session.get(Task, task_id), session.get(TaskStep, steps[0])
        receipt = env["goals"].customer_calls_receipt(session, task, step)
        assert not receipt["verified"] and receipt["outputs"]["booking_count"] == 0
        events.append(calendar_event(now + timedelta(days=1), "lead0@example.org",
            attendees=[{"email": "lead0@example.org"}, {"email": "lead1@example.org"}]))
        receipt = env["goals"].customer_calls_receipt(session, task, step)
        assert not receipt["verified"] and receipt["outputs"]["booking_count"] == 1
        events.clear()
        events.extend(calendar_event(now + timedelta(days=1, hours=index), f"lead{index}@example.org") for index in range(2))
        receipt = env["goals"].customer_calls_receipt(session, task, step)
        assert receipt["verified"] and receipt["outputs"]["booking_count"] == 2
        for event in events:
            event["status"] = "cancelled"
        assert env["goals"].customer_calls_receipt(session, task, step)["outputs"]["booking_count"] == 0
        def unavailable(*args, **kwargs):
            raise RuntimeError("No calendar response")
        monkeypatch.setattr(composio_gmail, "execute_tool", unavailable)
        receipt = env["goals"].customer_calls_receipt(session, task, step)
        assert not receipt["verified"] and receipt["pending"] and receipt["outputs"]["booking_count"] == 0
        assert not receipt["evidence"][0]["bookings_checked"]
        account.outcome = "revoked"
        session.commit()
        receipt = env["goals"].customer_calls_receipt(session, task, step)
        assert receipt["pending"] and receipt["connection_action"]["label"] == "Connect Google Calendar"
        env["goals"].finish(session, task, session.get(env["goals"].TaskRun, step.run_id), step, receipt)
        session.commit()
        messages = session.query(ChatMessage).filter(ChatMessage.kind == "text").all()
        assert len(messages) == 1 and json.loads(messages[0].actions_json) == [receipt["connection_action"]]
        assert "Nothing is changed" in messages[0].body_md


def test_booking_refresh_runs_outside_task_locks_with_owned_budget_and_job(goal_env, monkeypatch, request):
    from sqlalchemy import event
    from sqlalchemy.orm import Session
    from app.connections import bookings
    from app.content.oauth import callbacks
    from app.db.models import GtmContact, Job
    from app.db.task_models import Task, TaskRun, TaskStep
    from app.gtm.supymem import composio_gmail
    from app.tasks import locks, usage
    from app.tenancy import current_or_none

    env, now, held, calls = goal_env, goal_env["clock"][0], set(), []
    task_id, run_id, steps = create(env, contract(env, steps=[{"title": "Check calls", "skill": "tasks.customer_calls",
        "subgoal": "Check booked conversations for " + env["domain"], "inputs": {"target_count": 2}}]))
    original = locks.lock_step
    def lock(session, step_id):
        result = original(session, step_id)
        held.add(id(session))
        return result
    def release(session, transaction):
        held.discard(id(session))
    event.listen(Session, "after_transaction_end", release)
    request.addfinalizer(lambda: event.remove(Session, "after_transaction_end", release))
    monkeypatch.setattr(locks, "lock_step", lock)
    metering(monkeypatch)
    def verify(account):
        assert not held
        assert current_or_none().actor == "usr_lifecycle" and usage.context() == (run_id, steps[0])
        calls.append("verified")
        return {"status": "ACTIVE"}
    monkeypatch.setattr(callbacks, "_verified_connection", verify)
    events = [calendar_event(now + timedelta(days=1), "lead0@example.org")]
    def execute(slug, **kwargs):
        assert not held and usage.context() == (run_id, steps[0])
        return {"successful": True, "data": {"items": [{"id": "primary"}] if slug == "GOOGLECALENDAR_LIST_CALENDARS" else events}}
    monkeypatch.setattr(composio_gmail, "execute_tool", execute)
    with env["factory"]() as session:
        connection(session, now)
        for index in range(2):
            session.add(GtmContact(id=f"lead_{index}", workspace_id="ws_lifecycle", email=f"lead{index}@example.org",
                source="venture:ven_lifecycle:public", created_at=now, stage_changed_at=now))
        session.commit()
    asyncio.run(env["goals"].dispatch(task_id, run_id, steps[0], {"target_count": 2}))
    assert calls == ["verified"]
    with env["factory"]() as session:
        run = session.get(TaskRun, run_id)
        session.get(Job, run.control_job_id).status = "succeeded"
        session.flush()
        env["goals"].reconcile(session)
        session.commit()
        assert calls == ["verified"]
        check = session.get(Job, run.control_job_id)
        payload = {**json.loads(check.payload_json), "_job_id": check.id,
            "workspace_id": check.workspace_id, "venture_id": check.venture_id}
        assert payload["_booking_step_id"] == steps[0] and payload["_actor_subject"] == "usr_lifecycle"
    with pytest.raises(PermissionError, match="task_scope_mismatch"):
        asyncio.run(env["execution"].execute({**payload, "workspace_id": "ws_other"}))
    assert calls == ["verified"]
    env["flags"].clear()
    from app.core.local_mode import is_local
    from app.core.local_secrets import update_settings
    if is_local():
        update_settings("capabilities", {"routines": False}, {})
    asyncio.run(env["execution"].execute(payload))
    assert calls == ["verified"]
    env["flags"]["goals_v1"] = True
    if is_local():
        update_settings("capabilities", {"routines": True}, {})
    events.append(calendar_event(now + timedelta(days=1, hours=1), "lead1@example.org"))
    asyncio.run(env["execution"].execute(payload))
    with env["factory"]() as session:
        task, step = session.get(Task, task_id), session.get(TaskStep, steps[0])
        assert task.state == step.state == "completed"
        assert session.get(TaskRun, run_id).tool_calls == 4
        assert env["goals"].customer_calls_receipt(session, task, step)["outputs"]["booking_count"] == 2
    assert calls == ["verified", "verified"]
