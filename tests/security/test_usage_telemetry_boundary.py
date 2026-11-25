"""Signed usage ingest cannot impersonate identity, tenants or financial events."""
import json
import logging
from uuid import uuid4
from tests.security.test_connector_oauth_boundary import client as client
from tests.security.test_durable_journey_boundary import request, venture


def body(vid):
    return {"event": "ui.screen_view", "event_id": str(uuid4()), "venture_id": vid,
        "props": {"feature": "planning", "platform": "macos", "app_version": "0.1.44", "session_id": str(uuid4())}}


def test_usage_auth_scope_and_replay(client):
    from app.db.models import AnalyticsEvent
    from app.state import db
    alice, _, _ = venture(client)
    bob, _, _ = venture(client, "usr_bob")
    event = body(alice)
    result = request(client, "POST", "/telemetry", json=event)
    assert result.status_code == 200, result.text
    assert request(client, "POST", "/telemetry", json=event).json() == result.json()
    with db()() as session:
        row = session.get(AnalyticsEvent, result.json()["id"])
        assert row.person_id == "usr_alice" and row.venture_id == alice
        assert "job_id" not in json.loads(row.props_json)
        assert "source" not in json.loads(row.props_json)
    assert request(client, "POST", "/telemetry", "usr_bob", json=event).status_code == 404
    changed = {**event, "props": {**event["props"], "feature": "research"}}
    assert request(client, "POST", "/telemetry", json=changed).status_code == 409
    assert request(client, "POST", "/telemetry", json={**event, "context_subject": "usr_bob"}).status_code == 409
    foreign = {**event, "venture_id": bob}
    assert request(client, "POST", "/telemetry", json=foreign).status_code == 404


def test_error_logs_preserve_closed_status_route_origin_and_time(client, caplog):
    caplog.set_level(logging.INFO, logger="app.error_event")
    event = body(None)
    event["event"] = "ui.error"
    event["props"].update({"error_context": "api", "error_code": "network_error",
        "error_text": "API request_failed (status 503)", "error_status": 503,
        "error_server_code": "gateway_unreachable", "error_route": "/auth/session",
        "error_origin": "native_auth_unavailable", "error_transport": "native_bridge",
        "error_severity": "transient"})
    assert request(client, "POST", "/telemetry", json=event).status_code == 200
    receipts = [json.loads(row.message.removeprefix("error_event ")) for row in caplog.records
                if row.name == "app.error_event" and row.message.startswith("error_event ")]
    assert receipts[-1]["error_status"] == 503
    assert receipts[-1]["error_route"] == "/auth/session"
    assert receipts[-1]["error_origin"] == "native_auth_unavailable"
    assert "error_text" not in receipts[-1]
    for server_code in ("gateway_unreachable", "session_verification_failed"):
        valid = {**event, "event_id": str(uuid4()), "props": {**event["props"], "error_server_code": server_code}}
        assert request(client, "POST", "/telemetry", json=valid).status_code == 200
    wrong = {**event, "event_id": str(uuid4()), "props": {**event["props"], "error_server_code": "database_unavailable"}}
    assert request(client, "POST", "/telemetry", json=wrong).status_code == 422
    ui_event = {**event, "event_id": str(uuid4()), "props": {**body(None)["props"],
        "error_context": "ui", "error_code": "ui_error", "error_text": "Runtime failure",
        "error_source": "chat", "error_intent": "send", "error_status": 503,
        "error_occurred_at": "2026-10-12T01:02:03.000Z"}}
    assert request(client, "POST", "/telemetry", json=ui_event).status_code == 200
    ui_receipt = json.loads(next(row.message.removeprefix("error_event ") for row in reversed(caplog.records)
        if row.name == "app.error_event" and row.message.startswith("error_event ")))
    assert ui_receipt["error_occurred_at"] == "2026-10-12T01:02:03.000Z"
    assert ui_receipt["component"] == "chat" and ui_receipt["intent"] == "send"
    for key, value in [("error_route", "/auth/session?email=private@company.com"),
                       ("error_origin", "private@company.com"), ("error_status", 600),
                       ("error_route", "/" + "x" * 81)]:
        private = {**event, "event_id": str(uuid4()), "props": {**event["props"], key: value}}
        assert request(client, "POST", "/telemetry", json=private).status_code == 422


def test_usage_rejects_business_events_and_private_free_text(client):
    vid, _, _ = venture(client)
    event = body(vid)
    for name in ["credits.topup", "billing.started", "site.published", "build.succeeded", "signup"]:
        assert request(client, "POST", "/telemetry", json={**event, "event": name}).status_code == 422
    for prop in ["prompt", "text", "email", "api_key", "nested", "url"]:
        assert request(client, "POST", "/telemetry", json={**event, "props": {**event["props"], prop: "private-value"}}).status_code == 422
    assert request(client, "POST", "/telemetry", json={**event, "person_id": "usr_bob"}).status_code == 422


def test_native_auth_transition_receipt_is_closed_and_actor_bound(client):
    from app.db.models import AnalyticsEvent
    from app.state import db

    event = body(None)
    event.update({"event": "ui.error", "context_subject": "usr_alice"})
    event["props"].update({"feature": "shell", "error_code": "native_error", "error_context": "native",
                           "error_text": "Mac native_failure", "error_auth_reason": "refresh_rejected_or_uncertain",
                           "error_auth_http_class": "5xx"})
    saved = request(client, "POST", "/telemetry", json=event)
    assert saved.status_code == 200, saved.text
    with db()() as session:
        props = json.loads(session.get(AnalyticsEvent, saved.json()["id"]).props_json)
    assert props["error_auth_reason"] == "refresh_rejected_or_uncertain"
    assert props["error_auth_http_class"] == "5xx"
    for changed in (
        {"error_auth_reason": "https://private.example/?token=secret"},
        {"error_auth_http_class": "503 private"},
        {"platform": "web"},
        {"feature": "settings"},
        {"error_native_command": "desktop_auth_reconnect"},
    ):
        invalid = {**event, "event_id": str(uuid4()), "props": {**event["props"], **changed}}
        assert request(client, "POST", "/telemetry", json=invalid).status_code == 422
    no_error = {**event, "event_id": str(uuid4()), "event": "ui.screen_view"}
    assert request(client, "POST", "/telemetry", json=no_error).status_code == 422
    assert request(client, "POST", "/telemetry", "usr_bob", json={**event, "event_id": str(uuid4())}).status_code == 409


def test_named_control_is_closed_and_paired_with_feature(client):
    from app.db.models import AnalyticsEvent
    from app.state import db

    vid, _, _ = venture(client)
    event = body(vid)
    event["event"] = "ui.control_activate"
    event["props"]["feature"] = "chat"
    event["props"]["control_id"] = "chat_send"
    saved = request(client, "POST", "/telemetry", json=event)
    assert saved.status_code == 200, saved.text
    with db()() as session:
        row = session.get(AnalyticsEvent, saved.json()["id"])
        assert row.event_name == "ui.control_activate"
        assert json.loads(row.props_json)["control_id"] == "chat_send"
    for control, feature in [("unknown", "chat"), ("chat_send", "website")]:
        invalid = {**event, "event_id": str(uuid4()), "props": {**event["props"], "control_id": control, "feature": feature}}
        assert request(client, "POST", "/telemetry", json=invalid).status_code == 422
    missing = {**event, "event_id": str(uuid4()), "props": {k: v for k, v in event["props"].items() if k != "control_id"}}
    assert request(client, "POST", "/telemetry", json=missing).status_code == 422
    wrong_event = {**event, "event_id": str(uuid4()), "event": "ui.screen_view"}
    assert request(client, "POST", "/telemetry", json=wrong_event).status_code == 422


def test_chat_navigation_receipt_has_closed_source_and_no_url(client):
    from app.db.models import AnalyticsEvent
    from app.state import db

    vid, _, _ = venture(client)
    event = body(vid)
    event["event"] = "ui.navigation"
    event["props"].update({"feature": "analytics", "source": "chat"})
    saved = request(client, "POST", "/telemetry", json=event)
    assert saved.status_code == 200, saved.text
    with db()() as session:
        row = session.get(AnalyticsEvent, saved.json()["id"])
        assert row.event_name == "ui.navigation"
        assert json.loads(row.props_json)["source"] == "chat"
        assert "url" not in row.props_json
    for bad in (
        {**event, "event_id": str(uuid4()), "props": {**event["props"], "source": "web"}},
        {**event, "event_id": str(uuid4()), "event": "feature.used"},
        {**event, "event_id": str(uuid4()), "props": {k: v for k, v in event["props"].items() if k != "source"}},
        {**event, "event_id": str(uuid4()), "props": {**event["props"], "url": "/v/private/analytics"}},
    ):
        assert request(client, "POST", "/telemetry", json=bad).status_code == 422


def test_content_generation_control_events_accept_only_closed_ids(client):
    vid, _, _ = venture(client)
    for control in ("content_topics_entry", "content_topics_budget", "content_draft_entry", "content_draft_budget", "content_generation_start",
                    "content_topics_confirm", "content_draft_confirm", "content_generation_recover",
                    "content_generation_refresh", "content_agent_cycle", "content_draft_edit", "content_draft_save"):
        event = body(vid)
        event["event"] = "ui.control_activate"
        event["props"].update({"feature": "content", "control_id": control})
        assert request(client, "POST", "/telemetry", json=event).status_code == 200
        wrong_feature = {**event, "event_id": str(uuid4()),
                         "props": {**event["props"], "feature": "gtm"}}
        assert request(client, "POST", "/telemetry", json=wrong_feature).status_code == 422
    private = body(vid)
    private["event"] = "ui.control_activate"
    private["props"].update({"feature": "content", "control_id": "content_generation_start",
                             "prompt": "private founder text"})
    assert request(client, "POST", "/telemetry", json=private).status_code == 422


def test_idea_recovery_controls_distinguish_actions_without_founder_text(client):
    from app.db.models import AnalyticsEvent
    from app.state import db

    vid, _, _ = venture(client)
    controls = {
        "idea_status_check": "idea",
        "idea_validation_retry": "idea",
        "idea_clarification_retry": "idea",
        "idea_validate_revision": "idea",
        "idea_confirm_personal": "idea",
        "idea_continue_research": "idea",
        "idea_review_local_build": "builds",
    }
    for control, feature in controls.items():
        event = body(vid)
        event["event"] = "ui.control_activate"
        event["props"].update({"feature": feature, "control_id": control})
        saved = request(client, "POST", "/telemetry", json=event)
        assert saved.status_code == 200, (control, saved.text)
        with db()() as session:
            props = json.loads(session.get(AnalyticsEvent, saved.json()["id"]).props_json)
            assert props["control_id"] == control
            assert "idea" not in props and "answer" not in props
        wrong = {**event, "event_id": str(uuid4()), "props": {**event["props"], "feature": "chat"}}
        assert request(client, "POST", "/telemetry", json=wrong).status_code == 422


def test_waitlist_control_events_preserve_surface_and_exclude_lead_data(client):
    vid, _, _ = venture(client)
    for surface in ("website", "gtm"):
        for action in ("enable", "close", "test", "refresh"):
            event = body(vid)
            event["event"] = "ui.control_activate"
            event["props"].update({"feature": surface, "control_id": f"waitlist_{action}_{surface}"})
            assert request(client, "POST", "/telemetry", json=event).status_code == 200
            wrong = {**event, "event_id": str(uuid4()), "props": {**event["props"],
                "feature": "gtm" if surface == "website" else "website"}}
            assert request(client, "POST", "/telemetry", json=wrong).status_code == 422
            private = {**event, "event_id": str(uuid4()), "props": {**event["props"],
                "email": "person@example.com"}}
            assert request(client, "POST", "/telemetry", json=private).status_code == 422


def test_mac_start_exposure_is_order_scoped_and_cannot_cross_tenants(client):
    alice, _, _ = venture(client)
    bob, _, _ = venture(client, "usr_bob")
    exposure = body(alice)
    exposure["event"] = "ui.control_exposure"
    exposure["props"].update({"feature": "computer_use", "control_id": "mac_start", "order_id": "dwo_opaque"})
    assert request(client, "POST", "/telemetry", json=exposure).status_code == 200
    assert request(client, "POST", "/telemetry", "usr_bob", json=exposure).status_code == 404
    wrong = {**exposure, "event_id": str(uuid4()), "venture_id": bob}
    assert request(client, "POST", "/telemetry", json=wrong).status_code == 404
    for changed in [
        {**exposure, "event_id": str(uuid4()), "props": {**exposure["props"], "order_id": "private task text"}},
        {**exposure, "event_id": str(uuid4()), "props": {**exposure["props"], "instruction": "private task text"}},
    ]:
        assert request(client, "POST", "/telemetry", json=changed).status_code == 422


def test_usage_local_subject_alias_uses_canonical_person_id(client):
    from app.db.models import AnalyticsEvent
    from app.state import db
    from app.workspaces.resolve import LOCAL_PERSON_ID

    event = body(None)
    first = request(client, "POST", "/telemetry", "founder", json=event)
    assert first.status_code == 200, first.text
    replay = request(client, "POST", "/telemetry", "local", json=event)
    assert replay.status_code == 200, replay.text
    assert replay.json() == first.json()
    with db()() as session:
        row = session.get(AnalyticsEvent, first.json()["id"])
        assert row.person_id == LOCAL_PERSON_ID


def test_ui_error_is_private_bounded_and_event_specific(client):
    from app.db.models import AnalyticsEvent
    from app.state import db

    vid, _, _ = venture(client)
    event = body(vid)
    event["event"] = "ui.error"
    event["props"].update({"error_code": "render_error", "error_context": "render",
        "error_text": "Render failure"})
    saved = request(client, "POST", "/telemetry", json=event)
    assert saved.status_code == 200, saved.text
    assert request(client, "POST", "/telemetry", json=event).json() == saved.json()
    with db()() as session:
        row = session.get(AnalyticsEvent, saved.json()["id"])
        assert row.person_id == "usr_alice"
        assert json.loads(row.props_json)["error_text"] == event["props"]["error_text"]
    for private in ["TypeError: Cannot read properties of undefined", "private customer message",
                    "a@b.test", "https://example.test/private?key=1", "api_key=private",
                    "vck_1234567890123456", "apikey_1234567890123456",
                    "ak_1234567890123456", "AKIA1234567890123456", "x" * 2049,
                    "Render failure: private message", "API private_customer (status 500)",
                    "API request_failed (status 500) private message",
                    "API request_failed (status 999)", "Mac private_command failed"]:
        invalid = {**event, "event_id": str(uuid4()),
            "props": {**event["props"], "error_text": private}}
        assert request(client, "POST", "/telemetry", json=invalid).status_code == 422
    wrong_event = {**event, "event_id": str(uuid4()), "event": "ui.screen_view"}
    assert request(client, "POST", "/telemetry", json=wrong_event).status_code == 422
    assert client.get("/admin/diagnostics/ui-errors").status_code in {401, 403}


def test_unmapped_shell_error_is_captured_without_adding_fake_usage(client):
    from app.db.models import AnalyticsEvent
    from app.state import db

    event = body(None)
    event["event"] = "ui.error"
    event["props"].update({"feature": "shell", "error_code": "render_error",
        "error_context": "render", "error_text": "Render failure"})
    saved = request(client, "POST", "/telemetry", json=event)
    assert saved.status_code == 200, saved.text
    with db()() as session:
        row = session.get(AnalyticsEvent, saved.json()["id"])
        assert row.person_id == "usr_alice"
        assert json.loads(row.props_json)["feature"] == "shell"
    for invalid_event in ("ui.screen_view", "feature.used", "session.active"):
        invalid = {**event, "event_id": str(uuid4()), "event": invalid_event,
            "props": {key: value for key, value in event["props"].items() if not key.startswith("error_")}}
        assert request(client, "POST", "/telemetry", json=invalid).status_code == 422
    private = {**event, "event_id": str(uuid4()),
        "props": {**event["props"], "path": "/v/private-secret"}}
    assert request(client, "POST", "/telemetry", json=private).status_code == 422


def test_ui_error_labels_are_closed_and_match_context(client):
    from app.db.models import VentureDocument
    from app.state import db
    from core.clock import utcnow

    vid, _, _ = venture(client)
    event = body(vid)
    event["event"] = "ui.error"
    for code, context, label in [
        ("network_error", "api", "API request_failed (status 0)"),
        ("request_error", "api", "API request_failed (status 409)"),
        ("server_error", "api", "API request_failed (status 503)"),
        ("native_error", "native", "Mac command_failed"),
        ("render_error", "render", "Render failure"),
        ("runtime_error", "runtime", "Runtime failure"),
        ("runtime_error", "promise", "Promise failure"),
        ("integrity_error", "integrity", "Integrity failure"),
        ("job_error", "job", "Job failure"),
    ]:
        valid = {**event, "event_id": str(uuid4()), "props": {**event["props"],
            "error_code": code, "error_context": context, "error_text": label}}
        assert request(client, "POST", "/telemetry", json=valid).status_code == 200
        wrong_context = {**valid, "event_id": str(uuid4()),
            "props": {**valid["props"], "error_context": "render" if context != "render" else "api"}}
        assert request(client, "POST", "/telemetry", json=wrong_context).status_code == 422
    mismatched_status = {**event, "event_id": str(uuid4()), "props": {**event["props"],
        "error_code": "network_error", "error_context": "api", "error_text": "API request_failed (status 503)"}}
    assert request(client, "POST", "/telemetry", json=mismatched_status).status_code == 422
    with_ref = {**event, "event_id": str(uuid4()), "props": {**event["props"],
        "error_code": "server_error", "error_context": "api", "error_text": "API request_failed (status 503)",
        "error_ref": "req_123456abcdef"}}
    assert request(client, "POST", "/telemetry", json=with_ref).status_code == 200
    core_ref = {**with_ref, "event_id": str(uuid4()), "props": {**with_ref["props"],
        "error_ref": "req_7gkmnpqrstvw"}}
    assert request(client, "POST", "/telemetry", json=core_ref).status_code == 200
    bad_ref = {**with_ref, "event_id": str(uuid4()), "props": {**with_ref["props"], "error_ref": "req_secret"}}
    assert request(client, "POST", "/telemetry", json=bad_ref).status_code == 422
    with_operation = {**with_ref, "event_id": str(uuid4()), "props": {**with_ref["props"],
        "error_operation": "venture_waitlist_read"}}
    assert request(client, "POST", "/telemetry", json=with_operation).status_code == 200
    native_busy = {**with_operation, "event_id": str(uuid4()), "props": {**with_operation["props"],
        "error_origin": "native_auth_busy", "error_operation": "auth_session_read", "error_transport": "native_bridge"}}
    assert request(client, "POST", "/telemetry", json=native_busy).status_code == 200
    stream_busy = {**native_busy, "event_id": str(uuid4()), "props": {**native_busy["props"],
        "error_operation": "venture_activity_stream", "error_transport": "native_stream"}}
    assert request(client, "POST", "/telemetry", json=stream_busy).status_code == 200
    with db()() as session:
        now = utcnow()
        session.add(VentureDocument(id="dwo_exact", venture_id=vid, kind="device_work_order_v1",
            title="Mac review", created_at=now, updated_at=now))
        session.add(VentureDocument(id="dwo_foreign", venture_id="ven_other", kind="device_work_order_v1",
            title="Another Mac review", created_at=now, updated_at=now))
        session.commit()
    order_failure = {**with_ref, "event_id": str(uuid4()), "props": {**with_ref["props"],
        "feature": "computer_use", "error_operation": "mac_order_approve",
        "error_transport": "native_bridge", "error_order_id": "dwo_exact"}}
    assert request(client, "POST", "/telemetry", json=order_failure).status_code == 200
    missing_order = {**order_failure, "event_id": str(uuid4()), "props": {**order_failure["props"], "error_order_id": "dwo_missing"}}
    assert request(client, "POST", "/telemetry", json=missing_order).status_code == 404
    foreign_order = {**order_failure, "event_id": str(uuid4()), "props": {**order_failure["props"], "error_order_id": "dwo_foreign"}}
    assert request(client, "POST", "/telemetry", json=foreign_order).status_code == 404
    for props in [
        {**order_failure["props"], "error_order_id": "dwo_secret?token=private"},
        {**order_failure["props"], "error_operation": "auth_session_read"},
        {**order_failure["props"], "feature": "settings"},
        {**order_failure["props"], "error_transport": "native_secret"},
        {**order_failure["props"], "platform": "web"},
    ]:
        assert request(client, "POST", "/telemetry", json={**order_failure, "event_id": str(uuid4()), "props": props}).status_code == 422
    for props in [
        {**native_busy["props"], "error_origin": "native_secret"},
        {**native_busy["props"], "platform": "web"},
        {**native_busy["props"], "error_text": "API request_failed (status 500)"},
    ]:
        assert request(client, "POST", "/telemetry", json={**native_busy, "event_id": str(uuid4()), "props": props}).status_code == 422
    raw_path = {**with_operation, "event_id": str(uuid4()), "props": {**with_operation["props"],
        "error_operation": "/api/ventures/ven_private/waitlist?token=secret"}}
    assert request(client, "POST", "/telemetry", json=raw_path).status_code == 422
    wrong_context = {**with_operation, "event_id": str(uuid4()), "props": {**with_operation["props"],
        "error_code": "native_error", "error_context": "native", "error_text": "Mac command_failed"}}
    assert request(client, "POST", "/telemetry", json=wrong_context).status_code == 422
    native_command = {**event, "event_id": str(uuid4()), "props": {**event["props"],
        "platform": "macos", "error_code": "native_error", "error_context": "native",
        "error_text": "Mac command_failed", "error_native_command": "install_update"}}
    assert request(client, "POST", "/telemetry", json=native_command).status_code == 200
    for command in ("open_composio_connect_link", "write_hermes_files", "stdio_write", "desktop_events_unsubscribe"):
        labeled = {**native_command, "event_id": str(uuid4()), "props": {
            **native_command["props"], "error_native_command": command}}
        assert request(client, "POST", "/telemetry", json=labeled).status_code == 200
    transient = {**native_command, "event_id": str(uuid4()), "props": {
        **native_command["props"], "error_severity": "transient", "error_repeat_count": 3}}
    assert request(client, "POST", "/telemetry", json=transient).status_code == 200
    for extra in ({"error_severity": "fatal"}, {"error_repeat_count": 0}):
        assert request(client, "POST", "/telemetry", json={**native_command, "event_id": str(uuid4()),
            "props": {**native_command["props"], **extra}}).status_code == 422
    not_error_event = {**event, "event_id": str(uuid4()), "event": "feature.used",
        "props": {**event["props"], "error_severity": "transient"}}
    assert request(client, "POST", "/telemetry", json=not_error_event).status_code == 422
    for props in [
        {**native_command["props"], "error_native_command": "install_update?key=secret"},
        {**native_command["props"], "platform": "web"},
        {**native_command["props"], "error_text": "Mac process_crashed"},
        {**native_command["props"], "error_context": "api", "error_code": "server_error",
         "error_text": "API request_failed (status 503)"},
    ]:
        assert request(client, "POST", "/telemetry", json={**native_command,
            "event_id": str(uuid4()), "props": props}).status_code == 422


def test_job_error_is_idempotent_across_app_sessions(client):
    from app.db.models import AnalyticsEvent
    from app.state import db

    vid, _, _ = venture(client)
    event = body(vid)
    event["event"] = "ui.error"
    event["props"].update({"error_code": "job_error", "error_context": "job",
        "error_text": "Job failure", "job_id": "job_failure_a"})
    first = request(client, "POST", "/telemetry", json=event)
    assert first.status_code == 200, first.text
    reopened = {**event, "event_id": str(uuid4()), "props": {**event["props"],
        "session_id": str(uuid4()), "app_version": "0.1.52"}}
    replay = request(client, "POST", "/telemetry", json=reopened)
    assert replay.status_code == 200, replay.text
    assert replay.json() == first.json()
    with db()() as session:
        assert session.get(AnalyticsEvent, first.json()["id"]) is not None
        assert session.query(AnalyticsEvent).filter(AnalyticsEvent.id == first.json()["id"]).count() == 1
    different = {**reopened, "event_id": str(uuid4()), "props": {**reopened["props"], "job_id": "job_failure_b"}}
    second = request(client, "POST", "/telemetry", json=different)
    assert second.status_code == 200 and second.json()["id"] != first.json()["id"]
    for invalid in [
        {**event, "event_id": str(uuid4()), "props": {**event["props"], "job_id": "private job text"}},
        {**event, "event_id": str(uuid4()), "props": {**event["props"], "error_context": "runtime"}},
        {**event, "event_id": str(uuid4()), "event": "ui.screen_view"},
    ]:
        assert request(client, "POST", "/telemetry", json=invalid).status_code == 422
