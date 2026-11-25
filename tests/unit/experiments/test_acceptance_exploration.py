"""Test-first service contract; signatures and payloads are in REQUIREMENTS_LEDGER.md."""

from datetime import timedelta

import pytest

from tests.unit.chat.test_proactive import NOON, env


DOMAINS = [
    ("Shipment billing for import brokers", [
        "Freight accountants", "Customs teams", "Warehouse finance teams",
        "Trucking controllers", "Port agents", "Export coordinators",
    ]),
    ("Meal planning for home cooks", [
        "Busy couples", "Student flatmates", "Home gardeners",
        "Amateur athletes", "Large households", "Shift workers",
    ]),
]


@pytest.fixture(params=DOMAINS, ids=["business", "consumer"])
def venture(env, request):
    client, _, _ = env
    idea, audiences = request.param
    made = client.post("/ventures", json={"raw_idea": idea, "start": False})
    assert made.status_code == 201, made.text
    options = [{
        "statement": f"You could help {audience.lower()} with {idea.lower()}.",
        "audience": audience,
        "evidence_level": "hypothesis",
        "spec": {
            "metric": "positive_reply_rate", "min_sample": 20,
            "success_threshold": {"metric": "positive_reply_rate", "op": ">=", "value": .1},
            "failure_threshold": {"metric": "positive_reply_rate", "op": "==", "value": 0},
        },
    } for audience in audiences]
    return made.json()["id"], options


def _action(option_id, *, action="search", start=0, end=10,
            evidence_level="desk_research", outcome=None):
    return {
        "option_id": option_id, "action": action, "tool": "recorded_runner",
        "source_url": "https://source.example/recorded-action",
        "evidence_summary": "A source was recorded by the runner.",
        "evidence_level": evidence_level,
        "started_at": (NOON + timedelta(seconds=start)).isoformat(),
        "ended_at": (NOON + timedelta(seconds=end)).isoformat(),
        "outcome": outcome or {},
    }


def test_three_options_per_round_in_one_message(venture):
    from app.experiments import exploration

    venture_id, options = venture
    for kind in ("segment", "market_gap", "value_proposition", "product_approach", "demand_test"):
        result = exploration.create_round(venture_id, kind=kind, options=options[:3])
        assert result["round"] >= 1
        assert len(result["options"]) == 3
        assert len({option["id"] for option in result["options"]}) == 3
        assert [option["statement"] for option in result["options"]] == [option["statement"] for option in options[:3]]
        assert result["kind"] == "exploration_options"
        assert all(result["body_md"].count(option["statement"]) == 1 for option in options[:3])
        assert all("\n" not in option["statement"] for option in result["options"])
        assert all(option["evidence_level"] == "hypothesis" for option in result["options"])
        assert all(option["founder_status"] == "Idea" for option in result["options"])
        for count in (0, 2, 4):
            with pytest.raises(ValueError):
                exploration.create_round(venture_id, kind=kind, options=options[:count])


def test_unsupported_round_offers_another_three_without_selecting_a_winner(venture):
    from app.experiments import exploration

    venture_id, options = venture
    first = exploration.create_round(venture_id, kind="segment", options=options[:3])
    resolved = exploration.resolve_round(venture_id, first["id"], next_options=options[3:])
    assert resolved["selected_option_id"] is None
    assert resolved["state"] == "insufficient_evidence"
    second = resolved["next_round"]
    assert second["round"] == first["round"] + 1
    assert len(second["options"]) == 3
    assert not {option["id"] for option in first["options"]} & {option["id"] for option in second["options"]}
    assert [option["statement"] for option in second["options"]] == [option["statement"] for option in options[3:]]
    saved = exploration.get_round(venture_id, first["id"])
    assert [(option["id"], option["statement"]) for option in saved["options"]] == [
        (option["id"], option["statement"]) for option in first["options"]
    ], "Another round must not overwrite the first one's candidates"


def test_parallel_and_sequential_labels_follow_recorded_timestamps(venture):
    from app.experiments import activity, exploration

    venture_id, options = venture
    for expected, windows in (
        ("parallel", [(0, 12), (2, 14), (4, 16)]),
        ("sequential", [(0, 10), (10, 20), (20, 30)]),
    ):
        round_data = exploration.create_round(venture_id, kind="segment", options=options[:3])
        recorded = []
        for option, (start, end) in zip(round_data["options"], windows):
            item = _action(option["id"], start=start, end=end)
            recorded.append(item)
            activity.record(venture_id, item)
        view = exploration.get_round(venture_id, round_data["id"])
        assert view["execution_mode"] == expected
        for option, item in zip(view["options"], recorded):
            assert option["started_at"] == item["started_at"]
            assert option["ended_at"] == item["ended_at"]
            assert option["activity"][0]["started_at"] == item["started_at"]
            assert option["activity"][0]["ended_at"] == item["ended_at"]


def test_activity_attribution_uses_only_recorded_actions(venture):
    from app.experiments import activity, exploration

    venture_id, options = venture
    options[0]["statement"] += " I searched 90 sites, sent 40 emails and booked 8 meetings."
    round_data = exploration.create_round(venture_id, kind="segment", options=options[:3])
    initial = exploration.get_round(venture_id, round_data["id"])
    assert all(option["activity"] == [] for option in initial["options"])
    assert all(option["counts"].get("sent", 0) == option["counts"].get("meetings", 0) == 0
               for option in initial["options"])
    item = _action(round_data["options"][1]["id"])
    receipt = activity.record(venture_id, item)
    rows = activity.list_for_option(venture_id, item["option_id"])
    assert len(rows) == 1 and rows[0]["id"] == receipt["id"]
    assert all(rows[0][key] == value for key, value in item.items())
    view = exploration.get_round(venture_id, round_data["id"])
    assert view["options"][0]["activity"] == view["options"][2]["activity"] == []
    assert [entry["id"] for entry in view["options"][1]["activity"]] == [receipt["id"]]
    assert all(option["counts"].get("sent", 0) == option["counts"].get("meetings", 0) == 0
               for option in view["options"])
    with pytest.raises((KeyError, PermissionError, ValueError)):
        activity.record(venture_id, {**item, "option_id": "unowned_option"})
    with pytest.raises(ValueError):
        activity.record(venture_id, {**item, "started_at": None, "ended_at": None})
    assert len(activity.list_for_option(venture_id, item["option_id"])) == 1
    secret = "Bearer " + "fake123" * 4
    activity.record(venture_id, _action(item["option_id"], outcome={"debug": {"authorization": secret}}))
    assert secret not in str(exploration.get_round(venture_id, round_data["id"]))
    assert activity.list_for_option(venture_id, item["option_id"])[-1]["outcome"]["debug"]["authorization"] == "[REDACTED]"
    from app.db.models import Venture
    from app.state import db

    with db()() as session:
        saved = session.get(Venture, venture_id)
        saved.archived_at = NOON
        context = {"venture_id": venture_id, "workspace_id": saved.workspace_id, "option_id": item["option_id"]}
        session.commit()
    receipt_id = activity.record(context, action="search", tool="recorded_runner", evidence_level="desk_research",
        started_at=NOON, ended_at=NOON + timedelta(seconds=20), evidence_summary="A running action finished after you archived this venture.")
    assert activity.list_for_option(venture_id, item["option_id"])[-1]["id"] == receipt_id


def test_founder_status_words_are_plain_and_do_not_call_research_market_success(venture):
    from app.experiments import exploration

    venture_id, options = venture
    allowed = {"Idea", "Testing", "Worked", "Didn't work", "Too early to tell"}
    cases = [
        ("proposed", None, "hypothesis", "Idea"),
        ("running", None, "desk_research", "Testing"),
        ("concluded", "supported", "market_response", "Worked"),
        ("concluded", "refuted", "market_response", "Didn't work"),
        ("concluded", "inconclusive", "market_response", "Too early to tell"),
        ("concluded", "supported", "desk_research", "Too early to tell"),
        ("failed", None, "hypothesis", "Too early to tell"),
        ("blocked_by_permission", None, "hypothesis", "Too early to tell"),
    ]
    for state, outcome, level, expected in cases:
        status = exploration.founder_status(state=state, outcome=outcome, evidence_level=level)
        assert status == expected and status in allowed
    round_data = exploration.create_round(venture_id, kind="segment", options=options[:3])
    for word in ("hypothesis", "assumption", "evidence level"):
        assert word not in round_data["body_md"].lower()
    assert "\u2014" not in round_data["body_md"]


def test_too_small_to_conclude_always_has_an_executable_next_step(venture, monkeypatch):
    import asyncio

    from app.chat.actions import REGISTRY
    from app.chat.execute import execute_action
    from app.experiments import activity, exploration

    venture_id, options = venture
    round_data = exploration.create_round(venture_id, kind="value_proposition", options=options[:3])
    for option in round_data["options"]:
        activity.record(venture_id, _action(option["id"], action="reply_received",
                        evidence_level="market_response", outcome={"events": 1, "positive_reply_rate": 1.0}))
    resolved = exploration.resolve_round(venture_id, round_data["id"], next_options=options[3:])
    assert resolved["selected_option_id"] is None
    assert "too small to conclude" in resolved["body_md"].lower()
    assert resolved["actions"], "Insufficient results must not strand you at a summary"
    assert all(action["action"] in REGISTRY and action["label"].strip() for action in resolved["actions"])
    assert all("href" not in action for action in resolved["actions"])
    monkeypatch.setattr(exploration, "get_policy", lambda _key: {"exploration_v1": True})
    action = resolved["actions"][0]
    following = asyncio.run(execute_action(venture_id, action["action"], payload=action["payload"]))
    assert following["id"] == resolved["next_round"]["id"]
    assert [option["statement"] for option in following["options"]] == [option["statement"] for option in options[3:]]


def test_drafted_approved_queued_sent_replied_and_meeting_are_distinct(venture):
    from app.experiments import activity, exploration

    venture_id, options = venture
    round_data = exploration.create_round(venture_id, kind="value_proposition", options=options[:3])
    option_id = round_data["options"][0]["id"]
    steps = [
        ("drafted", "drafted_outreach", "drafted"),
        ("approved", "approved_outreach", "approved"),
        ("queued", "approved_outreach", "queued"),
        ("sent", "sent_outreach", "sent"),
        ("reply_received", "market_response", "replies"),
        ("meeting_booked", "market_response", "meetings"),
    ]
    for index, (action, level, count_key) in enumerate(steps):
        activity.record(venture_id, _action(option_id, action=action, start=index * 10, end=index * 10 + 5,
                        evidence_level=level, outcome={"message_id": "message_1", "count": 1}))
        view = exploration.get_round(venture_id, round_data["id"])
        option = view["options"][0]
        assert option["counts"][count_key] == 1
        assert option["evidence_level"] == level
        for _, _, future_key in steps[index + 1:]:
            assert option["counts"].get(future_key, 0) == 0
        assert len(option["activity"]) == index + 1
        assert all(other["activity"] == [] for other in view["options"][1:])


def test_comparison_uses_only_receipts_and_requires_comparable_meaningful_results(venture, env, monkeypatch):
    from app.experiments import activity, exploration

    venture_id, options = venture
    monkeypatch.setattr(exploration, "get_policy", lambda _key: {"exploration_v1": True})
    for sample, replies, different_method, classified, expected_winner in (
        (1, (1, 0, 0), False, True, False), (20, (10, 10, 10), False, True, False),
        (20, (20, 0, 0), True, True, False), (20, (20, 4, 4), False, False, False),
        (20, (20, 0, 0), False, True, True),
    ):
        round_data = exploration.create_round(venture_id, kind="value_proposition", options=options[:3])
        receipt_ids = set()
        for index, option in enumerate(round_data["options"]):
            sent = _action(option["id"], action="sent", evidence_level="sent_outreach",
                           outcome={"count": sample, "message_id": f"batch_{index}"})
            receipt_ids.add(activity.record(venture_id, sent)["id"])
            receipt_ids.add(activity.record(venture_id, sent)["id"])
            reply = _action(option["id"], action="reply_received", evidence_level="market_response",
                outcome={"count": replies[index], "positive_replies": replies[index],
                         "pain": 3 - index, "need": 2, "buying_intent": replies[index]})
            if not classified and index:
                reply["outcome"].pop("positive_replies")
            receipt_ids.add(activity.record(venture_id, reply)["id"])
            if different_method and index == 2:
                receipt_ids.add(activity.record(venture_id, _action(option["id"]))["id"])
            activity.record(venture_id, _action(option["id"], action="sent", evidence_level="sent_outreach",
                outcome={"count": 90, "status": "failed"}))
        resolved = exploration.resolve_round(venture_id, round_data["id"])
        comparison = resolved["comparison"]
        assert (resolved["selected_option_id"] == round_data["options"][0]["id"]) is expected_winner
        assert comparison["selected_option_id"] == resolved["selected_option_id"]
        assert all(row["sample_size"] == sample and row["counts"]["sent"] == sample for row in comparison["options"])
        assert [row["pain"] for row in comparison["options"]] == [3, 2, 1]
        assert all(row["need"] == 2 and row["evidence_level"] == "market_response" for row in comparison["options"])
        assert {item["id"] for item in comparison["citations"]} <= receipt_ids
        assert comparison["why_not_the_others"] and comparison["uncertainty"]
        assert "you recorded 90 sends" not in comparison["explanation"].lower()
        assert all(word not in resolved["body_md"].lower() for word in ("hypothesis", "assumption", "evidence level"))
        response = env[0].get(f"/ventures/{venture_id}/experiments/rounds")
        assert response.status_code == 200, response.text
        public = next(item for item in response.json()["rounds"] if item["id"] == round_data["id"])
        assert "evidence_level" not in response.text and "hypothesis" not in response.text
        assert public["comparison"]["selected_option_id"] == comparison["selected_option_id"]
        assert public["comparison"]["options"][0]["counts"]["sent"] == sample
        if expected_winner:
            assert public["status"] == "Worked"
            assert [option["status"] for option in public["options"]] == ["Worked", "Didn't work", "Didn't work"]
            activity.record(venture_id, _action(round_data["options"][1]["id"], action="reply_received",
                evidence_level="market_response", outcome={"count": sample, "positive_replies": sample}))
            assert exploration.get_round(venture_id, round_data["id"])["selected_option_id"] is None
            updated = exploration.resolve_round(venture_id, round_data["id"])
            assert updated["selected_option_id"] is None and updated["state"] == "insufficient_evidence"
            assert updated["ended_at"] == resolved["ended_at"]

    for metric, sample in (("booking_rate", 20), (None, 20), ("positive_reply_rate", 5)):
        candidates = [{**option, "spec": ({**option["spec"], "metric": metric,
            "min_sample": sample,
            "success_threshold": {"metric": metric, "op": ">=", "value": .1},
            "failure_threshold": {"metric": metric, "op": "==", "value": 0}} if metric else {})} for option in options[:3]]
        round_data = exploration.create_round(venture_id, kind="demand_test", options=candidates)
        for index, option in enumerate(round_data["options"]):
            activity.record(venture_id, _action(option["id"], action="sent", evidence_level="sent_outreach", outcome={"count": sample}))
            activity.record(venture_id, _action(option["id"], action="meeting_booked" if metric == "booking_rate" else "reply_received",
                evidence_level="market_response", outcome={"count": 0 if index else sample,
                    **({} if metric == "booking_rate" else {"positive_replies": 0 if index else sample})}))
        resolved = exploration.resolve_round(venture_id, round_data["id"])
        assert resolved["selected_option_id"] == round_data["options"][0]["id"]
        assert resolved["options"][0]["counts"]["meetings" if metric == "booking_rate" else "positive_replies"] == sample
        if metric == "booking_rate":
            assert resolved["comparison"]["options"][0]["buying_intent"] == "20 meetings booked"
    for blocker in ("failed", "blocked_by_permission"):
        round_data = exploration.create_round(venture_id, kind="segment", options=options[:3])
        for option in round_data["options"]:
            activity.record(venture_id, _action(option["id"], outcome={"status": blocker}))
        assert exploration.get_round(venture_id, round_data["id"])["state"] == blocker
        resolved = exploration.resolve_round(venture_id, round_data["id"])
        assert resolved["state"] == blocker and all(option["state"] == blocker for option in resolved["options"])
        assert "too small to conclude" in resolved["body_md"].lower() and resolved["actions"]


def test_confirmed_send_override_reaches_mailbox_limits_and_multiday_tests(venture, env, monkeypatch):
    import json

    from app.approvals import external
    from app.autopilot import guard, service
    from app.db.models import GtmContact, GtmEmailAccount, GtmOutreachSettings, Venture
    from app.experiments import outreach_tests
    from app.gtm.relationships.email_compliance import complete_body
    from app.gtm.supymem import composio_gmail
    from app.state import db
    from app.core.local_mode import is_local

    if is_local():
        from app.core.local_secrets import update_settings

        update_settings("smtp", {"host": "smtp.founder.example", "user": "you@founder.example",
            "sender_name": "Your founder", "postal_address": "1 Fixture Road, CA, US"}, {"password": "offline-mail-password"})
        update_settings("capabilities", {"send_email": True, "background_work": False}, {})

    venture_id, options = venture
    client = env[0]
    assert client.put(f"/ventures/{venture_id}/autopilot", json={"allow_send": True}).json()["send_max_per_day"] == 15
    unconfirmed = client.put(f"/ventures/{venture_id}/autopilot", json={"allow_send": True, "send_max_per_day": 40})
    assert unconfirmed.status_code == 200 and unconfirmed.json()["send_max_per_day"] == 40
    assert service.public_settings(venture_id)["send_max_per_day"] == 40
    assert guard.get_settings(venture_id)["send_max_per_day"] == 15
    confirmed = client.put(f"/ventures/{venture_id}/autopilot", json={
        "allow_send": True, "send_max_per_day": 40, "confirmed_send_max": True})
    assert confirmed.status_code == 200 and confirmed.json()["send_max_per_day"] == 40
    unchanged = client.put(f"/ventures/{venture_id}/autopilot", json={"allow_send": True, "send_max_per_day": 40})
    assert unchanged.status_code == 200 and unchanged.json()["send_max_per_day"] == 40
    with db()() as session:
        workspace_id = session.get(Venture, venture_id).workspace_id
        mail_user = composio_gmail.user_id_for(workspace_id) if is_local() else "test_founder"
        account = GtmEmailAccount(id="test_mailbox", workspace_id=workspace_id, email="you@founder.example",
            provider="smtp" if is_local() else "gmail", status="active",
            composio_user_id=mail_user, connected_account_id="test_connected", daily_send_limit=25,
            created_at=NOON, updated_at=NOON)
        session.add(account)
        contact = GtmContact(id="test_recipient", workspace_id=workspace_id, email="reader@recipient.example",
            recipient_geo="US", lawful_basis="consent", lawful_basis_at=NOON, lawful_basis_note="You requested email updates.",
            enrichment_json=json.dumps({"founder_import": True}), stage_changed_at=NOON, created_at=NOON)
        session.add(contact)
        session.add(GtmOutreachSettings(workspace_id=workspace_id, send_enabled=True,
            sender_postal_address="1 Fixture Road, CA, US", created_at=NOON, updated_at=NOON))
        session.flush()
        assert guard.mailbox_send_max(session, account) == 40
        body = complete_body(session, workspace_id, contact, "Would you like the details?", account.email)
        session.commit()
    sent = []
    monkeypatch.setattr(composio_gmail, "mailbox_slots_used_today", lambda *_args, **_kwargs: 15)
    monkeypatch.setattr(composio_gmail, "_send_gmail_email", lambda **fields: sent.append(fields) or {"provider_message_id": "recorded_send"})
    result = composio_gmail.send_gmail_email(composio_user_id=mail_user, connected_account_id="test_connected",
        to="reader@recipient.example", subject="Your requested details", body=body)
    assert result["provider_message_id"] == "recorded_send" and len(sent) == 1
    assert "I have your address from my own contacts." in sent[0]["body"]
    assert "no thanks" in sent[0]["body"] and "1 Fixture Road, CA, US" in sent[0]["body"]
    settings = guard.get_settings(venture_id)
    if is_local():
        assert not guard.check(venture_id, "outreach.send", settings={**settings, "god_mode": True}).ok
    assert guard.check(venture_id, "outreach.send", settings={**settings, "sends_today": 15}, human_approved=True).ok
    assert guard.check(venture_id, "outreach.send", settings={**settings, "sends_today": 40}, human_approved=True).reason == "send_limit_reached"
    monkeypatch.setattr(outreach_tests.metered, "price_micro", lambda _key: 0)
    monkeypatch.setattr(external, "outbound_quote", lambda *_args: {"credit_estimate_micro": 1})
    snapshot = {"journey": {"playbooks": [{"key": "problem_interview"}]}, "segments": []}
    base = {"venture_id": venture_id, "workspace_id": workspace_id,
            "icp": {"who": options[0]["audience"], "problem": options[0]["statement"]}}
    service.update_settings(venture_id, allow_send=True, send_max_per_day=1)
    specs = outreach_tests.specs(snapshot, base, None)
    assert specs and all(spec["max_duration_days"] >= spec["min_sample"] for spec in specs)
    assert all(spec["send_plan"]["daily_limit"] == 1 for spec in specs)
    with db()() as session:
        assert guard.mailbox_send_max(session, session.get(GtmEmailAccount, "test_mailbox")) == 15
        next_contact = GtmContact(id="test_next_recipient", workspace_id=workspace_id, email="another@recipient.example",
            recipient_geo="US", lawful_basis="consent", lawful_basis_at=NOON, lawful_basis_note="You requested email updates.",
            enrichment_json=json.dumps({"founder_import": True}), stage_changed_at=NOON, created_at=NOON)
        session.add(next_contact)
        session.flush()
        next_body = complete_body(session, workspace_id, next_contact, "Would you like the details?",
            session.get(GtmEmailAccount, "test_mailbox").email)
        session.commit()
    with pytest.raises(composio_gmail.DefiniteFailure, match="daily cap"):
        composio_gmail.send_gmail_email(composio_user_id=mail_user, connected_account_id="test_connected",
            to="another@recipient.example", subject="Your requested details", body=next_body)
    assert len(sent) == 1
    stopped = client.put(f"/ventures/{venture_id}/autopilot", json={"allow_send": False, "send_max_per_day": 40})
    assert stopped.status_code == 200 and not stopped.json()["allow_send"] and stopped.json()["send_max_per_day"] == 0


def test_legacy_send_caps_require_an_authorized_receipt_for_the_exact_value(venture, env):
    import json
    from sqlalchemy import select

    from app.autopilot import guard, service
    from app.chat.guard import _bulk_reply
    from app.db.models import EventLog, GtmEmailAccount, Venture
    from app.db.supymem_models import AutopilotSetting
    from app.state import db

    venture_id, _ = venture
    client = env[0]
    with db()() as session:
        workspace_id = session.get(Venture, venture_id).workspace_id
        session.add(AutopilotSetting(venture_id=venture_id, allow_send=True, send_max_per_day=40, updated_at=NOON))
        session.add(GtmEmailAccount(id="legacy_mailbox", workspace_id=workspace_id, email="you@sender.example",
            composio_user_id="receipt_owner", connected_account_id="receipt_connection", daily_send_limit=100,
            created_at=NOON, updated_at=NOON))
        session.commit()
    assert guard.get_settings(venture_id)["send_max_per_day"] == 15
    assert service.public_settings(venture_id)["send_max_per_day"] == 40
    assert "up to 15 a day" in _bulk_reply(venture_id, "send a thousand emails")["body_md"]
    with db()() as session:
        assert guard.mailbox_send_max(session, session.get(GtmEmailAccount, "legacy_mailbox")) == 15
    unconfirmed = client.put(f"/ventures/{venture_id}/autopilot", json={"allow_send": True, "send_max_per_day": 40})
    assert unconfirmed.status_code == 200 and unconfirmed.json()["send_max_per_day"] == 40
    assert guard.get_settings(venture_id)["send_max_per_day"] == 15
    with pytest.raises(PermissionError, match="change_autonomy_required"):
        service.update_settings(venture_id, allow_send=True, send_max_per_day=40, confirmed_send_max=True)
    confirmed = client.put(f"/ventures/{venture_id}/autopilot", json={
        "allow_send": True, "send_max_per_day": 40, "confirmed_send_max": True})
    assert confirmed.status_code == 200 and confirmed.json()["send_max_per_day"] == 40
    assert "up to 40 a day" in _bulk_reply(venture_id, "send a thousand emails")["body_md"]
    with db()() as session:
        receipt = session.scalar(select(EventLog).where(EventLog.venture_id == venture_id,
            EventLog.topic == "autopilot.send_cap.confirmed"))
        assert receipt.actor_id == service._owner(venture_id) and receipt.actor_kind == "person"
        assert receipt.ts is not None and json.loads(receipt.payload_json) == {"send_max_per_day": 40}
        assert guard.mailbox_send_max(session, session.get(GtmEmailAccount, "legacy_mailbox")) == 40
        session.get(AutopilotSetting, venture_id).send_max_per_day = 41
        session.commit()
    assert guard.get_settings(venture_id)["send_max_per_day"] == 15
    with db()() as session:
        assert guard.mailbox_send_max(session, session.get(GtmEmailAccount, "legacy_mailbox")) == 15


def test_owner_and_admin_can_confirm_a_bounded_send_cap(venture, env):
    from sqlalchemy import select

    from app.authz.workspace import ensure_subject_person
    from app.autopilot import guard, service
    from app.db.models import EventLog, Venture, WorkspaceMember
    from app.state import db

    venture_id, _ = venture
    client = env[0]
    admin = ensure_subject_person("send_cap_admin")
    with db()() as session:
        workspace_id = session.get(Venture, venture_id).workspace_id
        session.add(WorkspaceMember(id="send_cap_admin_membership", workspace_id=workspace_id,
            person_id=admin, role="admin", created_at=NOON))
        session.commit()
    assert client.put(f"/ventures/{venture_id}/autopilot", json={"allow_send": True}).status_code == 200
    for maximum in (101, 1000):
        for headers in ({}, {"X-Subject": admin}):
            invalid = client.put(f"/ventures/{venture_id}/autopilot", headers=headers, json={
                "allow_send": True, "send_max_per_day": maximum, "confirmed_send_max": True})
            assert invalid.status_code == 422 and guard.get_settings(venture_id)["send_max_per_day"] == 15
    with db()() as session:
        assert session.scalar(select(EventLog.id).where(EventLog.venture_id == venture_id,
            EventLog.topic == "autopilot.send_cap.confirmed")) is None
    allowed = client.put(f"/ventures/{venture_id}/autopilot", json={
        "allow_send": True, "send_max_per_day": 100, "confirmed_send_max": True})
    assert allowed.status_code == 200 and allowed.json()["send_max_per_day"] == 100
    unconfirmed = client.put(f"/ventures/{venture_id}/autopilot", headers={"X-Subject": admin}, json={
        "allow_send": True, "send_max_per_day": 40})
    assert unconfirmed.status_code == 200 and unconfirmed.json()["send_max_per_day"] == 40
    assert guard.get_settings(venture_id)["send_max_per_day"] == 15
    for maximum in (40, 100):
        allowed = client.put(f"/ventures/{venture_id}/autopilot", headers={"X-Subject": admin}, json={
            "allow_send": True, "send_max_per_day": maximum, "confirmed_send_max": True})
        assert allowed.status_code == 200 and guard.get_settings(venture_id)["send_max_per_day"] == maximum
    assert service.update_settings(venture_id, allow_send=True, send_max_per_day=40,
        confirmed_send_max=True, actor_subject=admin)["send_max_per_day"] == 40
    stopped = client.put(f"/ventures/{venture_id}/autopilot", headers={"X-Subject": admin},
        json={"allow_send": False, "send_max_per_day": 1000})
    assert stopped.status_code == 200 and not stopped.json()["allow_send"]


def test_send_cap_confirmation_is_explicit_and_supplied_cards_require_unchanged_payload(venture, env):
    import json
    from sqlalchemy import select

    from app.autopilot import guard, service
    from app.chat import jarvis
    from app.chat.store import append_message
    from app.db.models import EventLog
    from app.state import db

    venture_id, _ = venture
    client = env[0]
    endpoint = f"/ventures/{venture_id}/chat/confirm"
    for maximum in (None, True, 0, -1, 101, 1000, "40"):
        invalid = client.post(endpoint, json={"action": "autopilot.send_cap", "payload": {"send_max": maximum}})
        assert invalid.status_code == 200 and "not valid" in invalid.json()["body_md"]
        assert not guard.get_settings(venture_id)["allow_send"]
    reply = jarvis.send_cap(venture_id, "allow sending up to 40 a day")
    saved = append_message(venture_id, role="assistant", body_md=reply["body_md"],
        kind=reply["kind"], actions=reply["actions"])
    unrelated = append_message(venture_id, role="assistant", body_md="Your test is ready.")
    user_copy = append_message(venture_id, role="user", body_md=reply["body_md"], actions=reply["actions"])
    for message_id, maximum in (("missing_card", 40), (unrelated["id"], 40), (user_copy["id"], 40),
                                (saved["id"], 1000), (saved["id"], 41)):
        denied = client.post(endpoint, json={"action": "autopilot.send_cap", "message_id": message_id,
            "payload": {"send_max": maximum}})
        assert denied.status_code == 409 and not guard.get_settings(venture_id)["allow_send"]
    with db()() as session:
        assert session.scalar(select(EventLog.id).where(EventLog.venture_id == venture_id,
            EventLog.topic == "autopilot.send_cap.confirmed")) is None
    accepted = client.post(endpoint, json={"action": "autopilot.send_cap", "message_id": saved["id"],
        "payload": {"send_max": 40}})
    assert accepted.status_code == 200 and guard.get_settings(venture_id)["send_max_per_day"] == 40
    assert service.public_settings(venture_id)["allow_send"]
    for maximum in (41, 12, 100):
        accepted = client.post(endpoint, json={"action": "autopilot.send_cap", "payload": {"send_max": maximum}})
        assert accepted.status_code == 200 and guard.get_settings(venture_id)["send_max_per_day"] == maximum
        with db()() as session:
            receipt = session.scalar(select(EventLog).where(EventLog.venture_id == venture_id,
                EventLog.topic == "autopilot.send_cap.confirmed",
                EventLog.payload_json == json.dumps({"send_max_per_day": maximum})))
            assert receipt.actor_id == service._owner(venture_id) and receipt.actor_kind == "person"
            assert receipt.ts is not None


@pytest.mark.hosted_only
@pytest.mark.parametrize("role", ["owner", "admin", "maintainer", "contributor", "viewer", "unknown", None])
def test_send_cap_authorization_precedes_card_and_confirmation_validation(venture, env, role):
    from sqlalchemy import select

    from app.authz.workspace import ensure_subject_person
    from app.autopilot import service
    from app.db.models import EventLog, Venture, WorkspaceMember
    from app.state import db

    venture_id, _ = venture
    client = env[0]
    actor = ensure_subject_person("send_cap_actor")
    if role:
        with db()() as session:
            workspace_id = session.get(Venture, venture_id).workspace_id
            session.add(WorkspaceMember(id="send_cap_membership", workspace_id=workspace_id,
                person_id=actor, role=role, created_at=NOON))
            session.commit()
    before = service.public_settings(venture_id)
    permitted = role in {"owner", "admin"}
    forbidden = 403 if role else 404
    endpoint = f"/ventures/{venture_id}/chat/confirm"
    response = client.post(endpoint, headers={"X-Subject": actor}, json={
        "action": "autopilot.send_cap", "message_id": "missing_card", "payload": {"send_max": 1000}})
    assert response.status_code == (409 if permitted else forbidden)
    response = client.put(f"/ventures/{venture_id}/autopilot", headers={"X-Subject": actor}, json={
        "allow_send": True, "send_max_per_day": 1000, "confirmed_send_max": True})
    assert response.status_code == (422 if permitted else forbidden)
    if not permitted:
        for confirmed in (False, True):
            with pytest.raises(PermissionError, match="change_autonomy_required"):
                service.update_settings(venture_id, allow_send=True, send_max_per_day=40,
                    confirmed_send_max=confirmed, actor_subject=actor)
    assert service.public_settings(venture_id) == before
    with db()() as session:
        assert session.scalar(select(EventLog.id).where(EventLog.venture_id == venture_id,
            EventLog.topic == "autopilot.send_cap.confirmed")) is None
