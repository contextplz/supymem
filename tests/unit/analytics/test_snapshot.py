"""Analytics definitions: views, actions, agent receipts and bounded time."""
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from tests.company_os_harness import load_company_os_app

load_company_os_app()
from app.analytics.snapshot import calculate

NOW = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)


def event(name, person, at, *, venture="ven_a", feature="builds", eid="usage_a"):
    return SimpleNamespace(event_name=name, person_id=person, occurred_at=at, venture_id=venture,
                           id=eid, props_json=json.dumps({"feature": feature, "session_id": "one",
                                                            "app_version": "0.1.48"}))


def order(venture, created, *, state="stopped", stopped=True, action="app.navigate", actor="founder"):
    return SimpleNamespace(venture_id=venture, created_at=created, provenance_json=json.dumps({
        "actor_id": actor, "state": state,
        "manifest": {"actions": [action], "credit_ceiling_micro": 1000},
        "approved_at": created.isoformat(), "claimed_at": created.isoformat(),
        "closed_at": (created + timedelta(seconds=30)).isoformat(),
        "native_stop": {"processes_stopped": stopped}, "settled_micro": 20,
    }))


def test_views_do_not_become_actions_or_hosted_users():
    rows = [event("ui.screen_view", "viewer", NOW, feature="analytics"),
            event("venture.created", "founder", NOW, eid="ane_1")]
    result = calculate(rows, [], [], now=NOW, internal_ids={"founder"},
                       venture_names={"ven_a": "LabHire"}, venture_id="ven_a")
    assert result["activity"]["dau"] == 1
    assert result["activity"]["observed_dau"] == 2
    assert result["activity"]["observed_mau"] == 2
    assert result["activity"]["screen_only_today"] == 1
    assert result["activity"]["internal_mau"] == 1
    assert result["activity"]["external_customer_mau"] is None
    assert result["features"][0]["success_rate"] is None
    assert result["coverage"]["hosted_product"] == "separate_app_events"


def test_observed_app_reach_deduplicates_views_and_actions_across_day_windows():
    rows = [
        event("ui.screen_view", "reader", NOW, eid="usage_reader_one"),
        event("ui.screen_view", "reader", NOW - timedelta(minutes=1), eid="usage_reader_two"),
        event("ui.screen_view", "actor", NOW, eid="usage_actor_view"),
        event("venture.created", "actor", NOW, eid="ane_actor_action"),
        event("ui.screen_view", "week_reader", NOW - timedelta(days=6), eid="usage_week"),
        event("ui.screen_view", "month_reader", NOW - timedelta(days=20), eid="usage_month"),
        event("ui.screen_view", "outside", NOW - timedelta(days=31), eid="usage_outside"),
    ]
    result = calculate(rows, [], [], now=NOW, internal_ids=set(), venture_names={})
    assert {key: result["activity"][key] for key in ("observed_dau", "observed_wau", "observed_mau")} == {
        "observed_dau": 2, "observed_wau": 3, "observed_mau": 4}
    assert result["activity"]["dau"] == result["activity"]["mau"] == 1
    assert result["activity"]["screen_only_today"] == 1


def test_native_heartbeats_and_server_receipts_do_not_hide_stale_ui_telemetry():
    ui_at = NOW - timedelta(minutes=20)
    server_at = NOW - timedelta(minutes=2)
    rows = [
        event("ui.screen_view", "founder", ui_at, eid="usage_ui"),
        event("request.received", "founder", server_at, eid="ane_server"),
        event("desktop.process_heartbeat", "founder", NOW, eid="usage_native"),
    ]
    result = calculate(rows, [], [], now=NOW, internal_ids=set(), venture_names={})
    assert result["newest_event_at"] == NOW.isoformat()
    assert result["newest_ui_event_at"] == ui_at.isoformat()
    assert result["newest_native_heartbeat_at"] == NOW.isoformat()
    assert result["newest_server_event_at"] == server_at.isoformat()


def test_chat_navigation_receipt_does_not_claim_a_visible_screen_or_feature_use():
    row = event("ui.navigation", "founder", NOW, feature="analytics", eid="usage_nav")
    row.props_json = json.dumps({"feature": "analytics", "source": "chat",
                                 "platform": "macos", "session_id": "one"})
    result = calculate([row], [], [], now=NOW, internal_ids=set(), venture_names={})
    assert result["newest_ui_event_at"] == NOW.isoformat()
    assert result["activity"]["observed_dau"] == 0
    assert result["features"] == []


def test_chat_entry_routes_are_attributed_without_claiming_feature_use():
    preview = event("chat.message", "founder", NOW, venture=None, eid="ane_entry_preview")
    preview.props_json = json.dumps({"source": "entry_chat", "route": "gtm"})
    confirmed = event("entry.chat_confirmed", "founder", NOW, eid="ane_entry_confirm")
    confirmed.props_json = json.dumps({"source": "entry_chat", "route": "gtm", "feature": "gtm"})
    forged = event("entry.chat_confirmed", "founder", NOW, eid="ane_entry_forged")
    forged.props_json = json.dumps({"source": "entry_chat", "route": "gtm", "feature": "builds"})
    result = calculate([preview, confirmed, forged], [], [], now=NOW,
                       internal_ids=set(), venture_names={})
    assert result["chat"]["entry_previews_by_route"] == {"gtm": 1}
    assert result["chat"]["entry_confirmations_by_route"] == {"gtm": 1}
    assert result["chat"]["entry_routed_features"] == {"gtm": 1}
    assert result["chat"]["entry_previewing_people"] == 1
    assert result["chat"]["entry_confirming_people"] == 1
    assert result["chat"]["feature_uses_from_returned_tools"] == {}


def test_saved_chat_provider_error_counts_as_reply_failure_not_ui_error():
    row = event("chat.reply_error", "founder", NOW, eid="ane_provider_error")
    row.props_json = json.dumps({"source": "chat", "failure_kind": "provider_call"})
    result = calculate([row], [], [], now=NOW, internal_ids=set(), venture_names={})
    assert result["chat"]["saved_error_replies_by_kind"] == {"provider_call": 1}
    assert result["errors_by_code"] == {}


def test_control_use_counts_interaction_without_inflating_activation_or_success():
    rows = [event("ui.screen_view", "viewer", NOW, feature="website", eid="usage_view"),
            event("feature.used", "viewer", NOW, feature="website", eid="usage_click")]
    result = calculate(rows, [], [], now=NOW, internal_ids=set(), venture_names={})
    assert result["features"] == [{"feature": "website", "views": 1, "viewing_people": 1,
                                   "interactions": 1, "interacting_people": 1,
                                   "named_activations": 0, "named_actors": 0,
                                   "chat_prepared_actions": 0, "chat_preparing_people": 0,
                                   "chat_initiated_uses": 0, "chat_initiating_people": 0,
                                   "attempts": 0, "successes": 0, "failures": 0, "success_rate": None}]
    assert result["activity"]["dau"] == 0
    assert result["activity"]["screen_only_today"] == 1


def test_named_controls_count_attempts_and_distinct_actors_not_success():
    rows = [event("ui.control_activate", "a", NOW, feature="chat", eid="usage_one"),
            event("ui.control_activate", "a", NOW, feature="chat", eid="usage_two"),
            event("ui.control_activate", "b", NOW, feature="chat", eid="usage_three"),
            event("ui.control_activate", "b", NOW, feature="website", eid="usage_mismatch")]
    for row in rows:
        props = json.loads(row.props_json)
        props["control_id"] = "chat_send"
        row.props_json = json.dumps(props)
    result = calculate(rows, [], [], now=NOW, internal_ids=set(), venture_names={})
    chat = next(control for control in result["controls"] if control["control_id"] == "chat_send")
    assert chat == {"control_id": "chat_send", "feature": "chat", "exposures": 0, "viewers": 0,
                    "activations": 3, "actors": 2}
    assert all(control["activations"] == 0 for control in result["controls"] if control["control_id"] != "chat_send")
    assert result["activity"]["dau"] == 0
    chat_feature = next(feature for feature in result["features"] if feature["feature"] == "chat")
    assert chat_feature["named_activations"] == 3
    assert chat_feature["named_actors"] == 2
    assert chat_feature["interactions"] == chat_feature["chat_prepared_actions"] == 0


def test_named_control_exposure_is_separate_from_activation_and_server_outcome():
    rows = [event("ui.control_exposure", "a", NOW, feature="content", eid="usage_exposure_a"),
            event("ui.control_exposure", "b", NOW, feature="content", eid="usage_exposure_b"),
            event("ui.control_activate", "a", NOW, feature="content", eid="usage_activate_a"),
            event("ui.control_exposure", "b", NOW, feature="gtm", eid="usage_mismatch")]
    for row in rows:
        props = json.loads(row.props_json)
        props["control_id"] = "content_platform_select"
        row.props_json = json.dumps(props)
    result = calculate(rows, [], [], now=NOW, internal_ids=set(), venture_names={})
    control = next(item for item in result["controls"] if item["control_id"] == "content_platform_select")
    assert control == {"control_id": "content_platform_select", "feature": "content",
                       "exposures": 2, "viewers": 2, "activations": 1, "actors": 1}
    assert next(item for item in result["features"] if item["feature"] == "content")["successes"] == 0


def test_non_mac_control_exposure_accepts_only_closed_id_without_order_or_content():
    from pydantic import ValidationError
    from app.api.events_ingest import TelemetryBody

    body = {"event": "ui.control_exposure", "event_id": "63f99cf4-55d9-4c87-9976-92f9b0596ea4",
            "props": {"feature": "content", "platform": "macos",
                      "session_id": "8793811c-d724-46ca-b3e4-6b5d06b8be80",
                      "control_id": "content_platform_select"}}
    assert TelemetryBody.model_validate(body).props.control_id == "content_platform_select"
    for props in [{**body["props"], "control_id": "private draft text"},
                  {**body["props"], "feature": "gtm"},
                  {**body["props"], "order_id": "dwo_opaque"},
                  {**body["props"], "label": "private draft text"}]:
        try:
            TelemetryBody.model_validate({**body, "props": props})
            assert False, "invalid exposure accepted"
        except ValidationError:
            pass


def test_chat_prepared_mac_review_is_separate_from_chat_submit_click_and_claim():
    prepared = event("request.received", "founder", NOW, feature="chat", eid="ane_prepared")
    prepared.props_json = json.dumps({"source": "chat", "intent_categories": ["computer_use"],
                                      "work_requested": True, "invocation": "mac_review_prepared"})
    mere_ask = event("request.received", "founder", NOW, feature="chat", eid="ane_ask")
    mere_ask.props_json = json.dumps({"source": "chat", "intent_categories": ["computer_use"],
                                      "work_requested": True})
    click = event("ui.control_activate", "founder", NOW, feature="chat", eid="usage_submit")
    click.props_json = json.dumps({"feature": "chat", "control_id": "chat_send"})
    result = calculate([prepared, mere_ask, click], [], [], now=NOW,
                       internal_ids={"founder"}, venture_names={})
    by_feature = {row["feature"]: row for row in result["features"]}
    assert by_feature["chat"]["named_activations"] == 1
    assert by_feature["chat"]["chat_prepared_actions"] == 0
    assert by_feature["computer_use"]["chat_prepared_actions"] == 1
    assert by_feature["computer_use"]["chat_preparing_people"] == 1
    assert by_feature["computer_use"]["attempts"] == 0
    assert by_feature["computer_use"]["successes"] is None
    assert result["chat"]["classified_user_requests"] == 2


def test_chat_tool_dispatch_and_feature_use_are_separate_from_clicks_and_receipt_success():
    invocation = event("chat.tool_invoked", "founder", NOW, feature="research", eid="ane_tool")
    invocation.props_json = json.dumps({"source": "chat", "tool": "research", "feature": "research", "outcome": "returned"})
    feature_use = event("feature.used", "founder", NOW, feature="research", eid="ane_feature")
    feature_use.props_json = json.dumps({"source": "chat", "tool": "research", "feature": "research"})
    result = calculate([invocation, feature_use], [], [], now=NOW,
                       internal_ids={"founder"}, venture_names={})
    assert result["chat"]["tool_invocations"] == {"research": 1}
    assert result["chat"]["tool_outcomes"] == {"research": {"returned": 1}}
    assert result["chat"]["tool_actors"] == {"research": 1}
    assert result["chat"]["feature_uses_from_returned_tools"] == {"research": 1}
    assert result["chat"]["feature_uses_from_prepared_mac_reviews"] == {}
    assert result["activity"]["dau"] == 0
    assert result["features"][0]["feature"] == "research"
    assert result["features"][0]["chat_initiated_uses"] == 1
    assert result["features"][0]["chat_initiating_people"] == 1
    assert result["features"][0]["interactions"] == 0


def test_saved_gtm_chat_read_counts_feature_but_navigation_offer_does_not():
    read = event("feature.used", "founder", NOW, feature="gtm", eid="ane_gtm_read")
    read.props_json = json.dumps({"source": "chat", "tool": "gtm_review", "feature": "gtm"})
    offer = event("feature.used", "founder", NOW, feature="builds", eid="ane_build_offer")
    offer.props_json = json.dumps({"source": "chat", "tool": "start_product_build", "feature": "builds"})
    result = calculate([read, offer], [], [], now=NOW,
                       internal_ids=set(), venture_names={})
    assert result["chat"]["feature_uses_from_returned_tools"] == {"gtm": 1}
    by_feature = {row["feature"]: row for row in result["features"]}
    assert by_feature["gtm"]["chat_initiated_uses"] == 1
    assert "builds" not in by_feature


def test_prepared_mac_review_counts_chat_and_computer_use_without_claiming_execution():
    use = event("feature.used", "founder", NOW, feature="computer_use", eid="ane_mac_feature")
    use.props_json = json.dumps({"source": "chat", "feature": "computer_use",
                                 "invocation": "mac_review_prepared", "order_id": "dwo_one"})
    invalid = event("feature.used", "founder", NOW, feature="computer_use", eid="ane_bad")
    invalid.props_json = json.dumps({"source": "chat", "feature": "computer_use",
                                     "invocation": "mac_review_prepared", "order_id": "free text"})
    missing_actor = event("feature.used", None, NOW, feature="computer_use", eid="ane_no_actor")
    missing_actor.props_json = use.props_json
    blank_actor = event("feature.used", "   ", NOW, feature="computer_use", eid="ane_blank_actor")
    blank_actor.props_json = use.props_json
    result = calculate([use, invalid, missing_actor, blank_actor], [], [], now=NOW,
                       internal_ids=set(), venture_names={})
    feature = result["features"][0]
    assert feature["feature"] == "computer_use"
    assert feature["chat_initiated_uses"] == feature["chat_initiating_people"] == 1
    assert feature["interactions"] == feature["attempts"] == 0
    assert feature["successes"] is None
    assert result["chat"]["feature_uses_from_prepared_mac_reviews"] == {"computer_use": 1}


def test_mac_start_funnel_joins_exact_actor_venture_and_order_to_verified_receipt():
    created = NOW - timedelta(minutes=2)
    verified = order("ven_a", created, actor="founder")
    verified.id = "dwo_verified"
    body = json.loads(verified.provenance_json)
    body["native_stop"].update({"task_verified": True, "verification_kind": "open_venture_screen_v1"})
    verified.provenance_json = json.dumps(body)
    unverified = order("ven_a", created, actor="founder")
    unverified.id = "dwo_unverified"
    another_actor = order("ven_a", created, actor="other")
    another_actor.id = "dwo_other"
    rows = []
    for order_id, event_name, person, venture in [
        ("dwo_verified", "ui.control_exposure", "founder", "ven_a"),
        ("dwo_verified", "ui.control_activate", "founder", "ven_a"),
        ("dwo_unverified", "ui.control_exposure", "founder", "ven_a"),
        ("dwo_unverified", "ui.control_activate", "founder", "ven_a"),
        ("dwo_other", "ui.control_activate", "founder", "ven_a"),
        ("dwo_verified", "ui.control_activate", "other", "ven_b"),
    ]:
        row = event(event_name, person, NOW, venture=venture, feature="computer_use", eid=f"usage_{len(rows)}")
        row.props_json = json.dumps({"feature": "computer_use", "control_id": "mac_start", "order_id": order_id})
        rows.append(row)
    result = calculate(rows, [], [verified, unverified, another_actor], now=NOW,
                       internal_ids={"founder"}, venture_names={})
    funnel = result["mac_start_funnel"]
    assert funnel["eligible_orders"] == 2
    assert funnel["attempted_orders"] == 2
    assert funnel["accepted_starts"] == 2
    assert funnel["verified_goals"] == 1
    assert funnel["closed_unverified"] == 1
    assert funnel["claims_without_named_attempt"] == 1
    assert funnel["eligible_people"] == funnel["attempting_people"] == 1


def test_chat_mac_funnel_joins_saved_order_and_excludes_visual_only_results():
    created = NOW - timedelta(minutes=2)
    saved = []
    for order_id, kind in (("dwo_verified", "open_venture_screen_v1"),
                           ("dwo_visual", "visual_review_v1"), ("dwo_pending", None),
                           ("dwo_direct", "open_venture_screen_v1")):
        row = order("ven_a", created, actor="founder")
        row.id = order_id
        body = json.loads(row.provenance_json)
        body["workspace_id"] = "wsp_a"
        if kind:
            body["native_stop"].update({"task_verified": True, "verification_kind": kind})
        if order_id == "dwo_pending":
            body["closed_at"] = None
            body["state"] = "claimed"
        row.provenance_json = json.dumps(body)
        saved.append(row)
    rows = []
    for order_id in ("dwo_verified", "dwo_visual", "dwo_pending"):
        row = event("request.received", "founder", created, venture="ven_a", eid=f"ane_{order_id}")
        row.workspace_id = "wsp_a"
        row.props_json = json.dumps({"source": "chat", "intent_categories": ["computer_use"],
                                     "work_requested": True, "invocation": "mac_review_prepared",
                                     "order_id": order_id})
        rows.append(row)
    # A forged cross-workspace receipt and a duplicate request cannot add orders.
    wrong = event("request.received", "founder", created, venture="ven_a", eid="ane_wrong")
    wrong.workspace_id = "wsp_other"
    wrong.props_json = rows[0].props_json
    rows.extend((wrong, rows[0]))
    funnel = calculate(rows, [], saved, now=NOW, internal_ids=set(), venture_names={})["chat_mac_funnel"]
    assert {key: funnel[key] for key in ("prepared", "approved", "claimed", "verified",
                                        "closed_unverified", "still_open")} == {
        "prepared": 3, "approved": 3, "claimed": 3, "verified": 1,
        "closed_unverified": 1, "still_open": 1,
    }
    assert funnel["people_by_stage"] == {"prepared": 1, "approved": 1, "claimed": 1, "verified": 1}


def test_mac_start_exposure_schema_accepts_only_closed_order_scoped_dimensions():
    from pydantic import ValidationError
    from app.api.events_ingest import TelemetryBody

    body = {"event": "ui.control_exposure", "event_id": "63f99cf4-55d9-4c87-9976-92f9b0596ea4",
            "props": {"feature": "computer_use", "platform": "macos",
                      "session_id": "8793811c-d724-46ca-b3e4-6b5d06b8be80",
                      "control_id": "mac_start", "order_id": "dwo_opaque"}}
    assert TelemetryBody.model_validate(body).props.order_id == "dwo_opaque"
    for props in [
        {**body["props"], "order_id": "https://example.test/private"},
        {**body["props"], "control_id": "chat_send"},
        {**body["props"], "instruction": "private task text"},
    ]:
        try:
            TelemetryBody.model_validate({**body, "props": props})
            assert False, "invalid exposure accepted"
        except ValidationError:
            pass


def test_mac_start_funnel_is_unavailable_without_instrumented_exposure_or_attempt():
    saved = order("ven_a", NOW - timedelta(minutes=1))
    saved.id = "dwo_older_client"
    result = calculate([], [], [saved], now=NOW, internal_ids=set(), venture_names={})
    assert result["mac_start_funnel"]["eligible_orders"] is None
    assert result["mac_start_funnel"]["verified_goals"] is None


def test_feature_people_count_distinct_authenticated_people_not_repeat_clicks():
    rows = [event("ui.screen_view", "a", NOW, feature="computer_use", eid="usage_view_a"),
            event("feature.used", "a", NOW, feature="computer_use", eid="usage_click_a1"),
            event("feature.used", "a", NOW, feature="computer_use", eid="usage_click_a2"),
            event("feature.used", "b", NOW, feature="computer_use", eid="usage_click_b")]
    result = calculate(rows, [], [], now=NOW, internal_ids=set(), venture_names={})
    assert result["features"][0]["views"] == 1
    assert result["features"][0]["viewing_people"] == 1
    assert result["features"][0]["interactions"] == 3
    assert result["features"][0]["interacting_people"] == 2
    assert result["activity"]["dau"] == 0


def test_ist_day_and_gap_limit():
    # 23:30 UTC is already the next day in India. A 3-minute gap contributes 0.
    midnight = datetime(2026, 9, 21, 23, 30, tzinfo=timezone.utc)
    rows = [event("venture.created", "founder", midnight, eid="ane_2"),
            event("session.active", "founder", midnight, eid="usage_1"),
            event("session.active", "founder", midnight + timedelta(minutes=1), eid="usage_2"),
            event("session.active", "founder", midnight + timedelta(minutes=4), eid="usage_3")]
    result = calculate(rows, [], [], now=NOW, internal_ids=set(), venture_names={})
    assert result["activity"]["dau"] == 1
    assert result["attention"]["observed_seconds"] == 60


def test_native_process_time_is_separate_and_sleep_gaps_add_zero():
    from app.api.events_ingest import TelemetryBody

    admitted = TelemetryBody.model_validate({
        "event": "desktop.process_heartbeat", "event_id": "63f99cf4-55d9-4c87-9976-92f9b0596ea4",
        "context_subject": "founder", "props": {"feature": "settings", "platform": "macos",
            "app_version": "0.1.48", "session_id": "8793811c-d724-46ca-b3e4-6b5d06b8be80"},
    })
    assert admitted.event == "desktop.process_heartbeat"
    rows = [
        event("desktop.process_heartbeat", "founder", NOW, eid="usage_runtime_1"),
        event("desktop.process_heartbeat", "founder", NOW + timedelta(minutes=1), eid="usage_runtime_2"),
        event("desktop.process_heartbeat", "founder", NOW + timedelta(minutes=6), eid="usage_runtime_3"),
        event("session.active", "founder", NOW, eid="usage_attention_1"),
        event("session.active", "founder", NOW + timedelta(seconds=30), eid="usage_attention_2"),
    ]
    result = calculate(rows, [], [], now=NOW + timedelta(minutes=6), internal_ids=set(), venture_names={})
    assert result["attention"]["observed_seconds"] == 30
    assert result["attention"]["process_uptime_seconds"] == 60
    assert result["attention"]["mean_process_uptime_seconds_per_person"] == 60
    assert result["coverage"]["process_uptime"] == "authenticated_native_heartbeat_observed"


def test_process_uptime_is_unavailable_without_an_adjacent_native_interval():
    result = calculate([], [], [], now=NOW, internal_ids=set(), venture_names={})
    assert result["attention"]["process_uptime_seconds"] is None
    assert result["attention"]["mean_process_uptime_seconds_per_person"] is None
    assert result["attention"]["observed_seconds"] is None
    assert result["coverage"]["process_uptime"] == "no_measurable_native_intervals"
    assert result["coverage"]["attention"] == "no_measurable_attention_intervals"
    assert result["retention"]["d1_rate"] is None
    assert result["retention"]["d7_rate"] is None
    assert result["activation"]["median_seconds_signup_to_first_venture"] is None
    assert result["coverage"]["retention"] == "not_measured_yet"
    assert "unavailable" in result["summary"]


def test_d1_retention_and_activation_use_real_action_days():
    day0 = datetime(2026, 9, 14, 10, 0, tzinfo=timezone.utc)
    day1 = datetime(2026, 9, 15, 11, 0, tzinfo=timezone.utc)
    day7 = datetime(2026, 9, 21, 9, 0, tzinfo=timezone.utc)
    rows = [
        event("signup", "founder", day0, eid="ane_signup"),
        event("venture.created", "founder", day0 + timedelta(minutes=5), eid="ane_venture"),
        event("idea.confirmed", "founder", day1, eid="ane_d1"),
        event("plan.approved", "founder", day7, eid="ane_d7"),
        event("signup", "churned", day0, eid="ane_signup2"),
        event("venture.created", "churned", day0 + timedelta(hours=1), eid="ane_venture2"),
    ]
    result = calculate(rows, [], [], now=NOW, internal_ids=set(), venture_names={},
                       external_ids={"founder", "churned"})
    assert result["retention"]["d1_eligible"] == 2
    assert result["retention"]["d1_retained"] == 1
    assert result["retention"]["d1_rate"] == 0.5
    assert result["activity"]["external_customer_mau"] == 2
    assert result["retention"]["d7_eligible"] == 2
    assert result["retention"]["d7_retained"] == 1
    assert result["retention"]["d7_rate"] == 0.5
    assert result["activation"]["signups_observed"] == 2
    assert result["activation"]["median_seconds_signup_to_first_venture"] == 1950
    assert result["coverage"]["retention"] == "action_active_d1_d7"
    assert result["coverage"]["activation"] == "signup_linked"


def test_unclassified_dogfood_is_not_an_external_retention_cohort():
    day0 = NOW - timedelta(days=2)
    rows = [event("signup", "unclassified", day0, eid="ane_signup"),
            event("venture.created", "unclassified", day0 + timedelta(minutes=5), eid="ane_created"),
            event("idea.confirmed", "unclassified", day0 + timedelta(days=1), eid="ane_return")]
    result = calculate(rows, [], [], now=NOW, internal_ids=set(), venture_names={})
    assert result["activity"]["unclassified_mau"] == 1
    assert result["activity"]["external_customer_mau"] is None
    assert result["retention"]["d1_rate"] is None
    assert result["activation"]["signups_observed"] is None


def test_internal_operators_excluded_from_product_retention_and_activation():
    day0 = datetime(2026, 9, 14, 10, 0, tzinfo=timezone.utc)
    day1 = datetime(2026, 9, 15, 11, 0, tzinfo=timezone.utc)
    rows = [
        event("signup", "founder", day0, eid="ane_signup_int"),
        event("venture.created", "founder", day0 + timedelta(minutes=5), eid="ane_venture_int"),
        event("idea.confirmed", "founder", day1, eid="ane_d1_int"),
        event("signup", "customer", day0, eid="ane_signup_ext"),
        event("venture.created", "customer", day0 + timedelta(minutes=10), eid="ane_venture_ext"),
        event("idea.confirmed", "customer", day1, eid="ane_d1_ext"),
    ]
    result = calculate(rows, [], [], now=NOW, internal_ids={"founder"}, external_ids={"customer"}, venture_names={})
    assert result["retention"]["d1_eligible"] == 1
    assert result["retention"]["d1_retained"] == 1
    assert result["retention"]["d1_rate"] == 1.0
    assert result["retention"]["internal_d1_eligible"] == 1
    assert result["retention"]["internal_d1_retained"] == 1
    assert result["retention"]["internal_d1_rate"] == 1.0
    assert result["activity"]["external_customer_mau"] == 1
    assert result["activation"]["signups_observed"] == 1
    assert result["activation"]["internal_signups_observed"] == 1
    assert result["activation"]["median_seconds_signup_to_first_venture"] == 600
    assert result["activation"]["internal_median_seconds_signup_to_first_venture"] == 300
    assert result["coverage"]["retention"] == "action_active_d1_d7"
    assert result["coverage"]["activation"] == "signup_linked"

    only_internal = calculate(rows[:3], [], [], now=NOW, internal_ids={"founder"}, external_ids={"customer"}, venture_names={})
    assert only_internal["retention"]["d1_rate"] is None
    assert only_internal["retention"]["d1_eligible"] is None
    assert only_internal["retention"]["internal_d1_eligible"] == 1
    assert only_internal["activation"]["signups_observed"] is None
    assert only_internal["activation"]["internal_signups_observed"] == 1
    assert only_internal["coverage"]["retention"] == "not_measured_yet"
    assert only_internal["coverage"]["activation"] == "not_measured_yet"


def test_claimed_vs_verified_agent_and_idle_job():
    created = NOW - timedelta(minutes=5)
    verified = order("ven_a", created)
    refused = order("ven_a", created, state="cancelled", stopped=False, action="browser.read")
    idle = SimpleNamespace(kind="research.deep", status="queued", created_at=created, started_at=None,
                           venture_id="ven_a")
    result = calculate([], [idle], [verified, refused], now=NOW, internal_ids=set(),
                       venture_names={"ven_a": "LabHire"}, venture_id="ven_a")
    assert result["agents"]["started_by_type"].get("researcher", 0) == 0
    assert result["agents"]["started_by_type"]["computer_use_navigation"] == 1
    assert result["agents"]["attributed_starting_people"] == 1
    assert result["agents"]["mean_per_attributed_person"] == 2
    assert result["computer_use"]["orders_by_state"] == {"verified_stop": 1, "cancelled": 1}
    assert result["computer_use"]["goal_verified_orders"] == 0
    assert result["computer_use"]["closed_without_goal_verification"] == 2
    assert result["computer_use"]["goal_verified_by_type"] == {}
    assert result["computer_use"]["closed_without_goal_verification_by_type"] == {
        "computer_use_navigation": 1, "computer_use_browser": 1,
    }
    computer_feature = next(row for row in result["features"] if row["feature"] == "computer_use")
    assert computer_feature["attempts"] == 2
    assert computer_feature["successes"] is None
    assert computer_feature["failures"] is None
    assert computer_feature["success_rate"] is None
    assert result["computer_use"]["median_claim_to_verified_stop_seconds"] == 30
    assert result["venture_ranking"] == []
    assert result["activity"]["dau"] == 0


def test_goal_verification_requires_an_explicit_native_task_receipt():
    saved = order("ven_a", NOW - timedelta(minutes=1))
    body = json.loads(saved.provenance_json)
    body["native_stop"].update({"task_verified": True, "verification_kind": "open_venture_screen_v1"})
    saved.provenance_json = json.dumps(body)
    result = calculate([], [], [saved], now=NOW, internal_ids=set(), venture_names={})
    assert result["computer_use"]["goal_verified_orders"] == 1
    assert result["computer_use"]["closed_without_goal_verification"] == 0
    assert result["computer_use"]["goal_verified_by_type"] == {"computer_use_navigation": 1}
    assert result["venture_ranking"][0]["successful_actions"] == 1
    assert result["activity"]["dau"] == 1
    assert result["features"][0]["success_rate"] is None


def test_finished_job_states_are_saved_outcomes():
    created = NOW - timedelta(minutes=2)
    completed = SimpleNamespace(kind="research.deep", status="succeeded", created_at=created,
                                started_at=created, venture_id="ven_a", payload_json=json.dumps({"_actor_subject": "founder"}))
    failed = SimpleNamespace(kind="build.run", status="failed_dead", created_at=created,
                             started_at=created, venture_id="ven_a", payload_json=json.dumps({"_actor_subject": "founder"}))
    result = calculate([], [completed, failed], [], now=NOW, internal_ids=set(),
                       venture_names={"ven_a": "LabHire"}, venture_id="ven_a")
    assert result["agents"]["started_by_type"] == {"researcher": 1, "builder": 1}
    assert result["agents"]["attributed_starting_people"] == 1
    assert result["agents"]["attributed_starts"] == 2
    assert result["agents"]["median_per_attributed_person"] == 2
    assert {row["feature"]: (row["successes"], row["failures"]) for row in result["features"]} == {
        "builds": (0, 1), "research": (1, 0),
    }


def test_build_watch_polling_does_not_inflate_builds_or_agents():
    created = NOW - timedelta(minutes=2)
    jobs = [SimpleNamespace(kind="build.run", status="succeeded", created_at=created,
                            started_at=created, venture_id="ven_a", payload_json="{}")]
    jobs.extend(SimpleNamespace(kind="build.watch", status="succeeded", created_at=created,
                                started_at=created, venture_id="ven_a", payload_json="{}")
                for _ in range(20))
    result = calculate([], jobs, [], now=NOW, internal_ids=set(),
                       venture_names={"ven_a": "LabHire"})
    assert result["agents"]["started_by_type"] == {"builder": 1}
    builds = next(row for row in result["features"] if row["feature"] == "builds")
    assert builds["attempts"] == 1
    assert builds["successes"] == 1
    assert result["venture_ranking"][0]["successful_actions"] == 1


def test_corrupt_order_provenance_cannot_invent_a_verified_stop():
    malformed = SimpleNamespace(venture_id="ven_a", created_at=NOW,
                                provenance_json=json.dumps({"state": "stopped", "manifest": "bad",
                                                            "native_stop": "bad", "closed_at": "not-a-date",
                                                            "claimed_at": "not-a-date"}))
    result = calculate([], [], [malformed], now=NOW, internal_ids=set(), venture_names={})
    assert result["computer_use"]["orders_by_state"] == {"stopped_unverified": 1}
    assert result["activity"]["dau"] == 0


def test_running_jobs_are_spinning_and_unattributed_jobs_are_not_imputed():
    created = NOW - timedelta(seconds=10)
    running = SimpleNamespace(kind="gtm.god.run", status="leased", created_at=created,
                              started_at=created, venture_id="ven_a",
                              payload_json=json.dumps({"_actor_subject": "founder"}))
    anonymous = SimpleNamespace(kind="research.deep", status="running", created_at=created,
                                started_at=created, venture_id="ven_a", payload_json="{}")
    result = calculate([], [running, anonymous], [], now=NOW, internal_ids=set(), venture_names={})
    assert result["agents"]["in_progress_by_type"] == {"gtm_diagnosis": 1, "researcher": 1}
    assert result["agents"]["attributed_starting_people"] == 1
    assert result["agents"]["attributed_starts"] == 1
    assert result["agents"]["mean_per_active_person"] is None
    assert result["coverage"]["agent_per_person"] == "partial_saved_actor_only"


def test_saved_request_and_chat_mix_excludes_private_text_and_unknown_dimensions():
    requests = [
        SimpleNamespace(created_at=NOW, intent_category="planning", source="chat", outcome="applied",
                        text="private business idea and an API key"),
        SimpleNamespace(created_at=NOW, intent_category="private idea text!", source="chat", outcome="failed",
                        text="private user message"),
        SimpleNamespace(created_at=NOW - timedelta(days=31), intent_category="import", source="import", outcome="done",
                        text="old private content"),
    ]
    result = calculate([], [], [], now=NOW, internal_ids=set(), venture_names={}, requests=requests,
                       chat_messages=[("user", NOW), ("assistant", NOW), ("system", NOW)])
    assert result["founder_requests"]["saved_by_category"] == {"planning": 1, "unclassified": 1}
    assert result["founder_requests"]["saved_by_outcome"] == {"applied": 1, "failed": 1}
    assert result["chat"]["saved_messages_by_role"] == {"user": 1, "assistant": 1}
    assert "private" not in json.dumps(result)


def test_authenticated_chat_intents_count_requests_and_people_without_text():
    rows = [SimpleNamespace(id="ane_chata", event_name="request.received", person_id="founder",
                            occurred_at=NOW, venture_id="ven_a", props_json=json.dumps({
                                "source": "chat", "intent_categories": ["assistant", "change"],
                                "topics": ["product_build", "status_blocker"],
                                "work_requested": True, "private_text": "must not be surfaced"})),
            SimpleNamespace(id="ane_chatb", event_name="request.received", person_id="founder",
                            occurred_at=NOW, venture_id="ven_a", props_json=json.dumps({
                                "source": "chat", "intent_categories": ["assistant"],
                                "topics": ["product_build"], "work_requested": False})),
            SimpleNamespace(id="ane_outcome", event_name="request.outcome", person_id="founder",
                            occurred_at=NOW, venture_id="ven_a", props_json=json.dumps({
                                "source": "chat", "request_event_id": "ane_chata",
                                "response_kind": "confirmation", "offered_actions": 2})),
            SimpleNamespace(id="ane_wrongscope", event_name="request.outcome", person_id="founder",
                            workspace_id="wsp_other", occurred_at=NOW, venture_id="ven_a", props_json=json.dumps({
                                "source": "chat", "request_event_id": "ane_chatb",
                                "response_kind": "text", "offered_actions": 0}))]
    result = calculate(rows, [], [], now=NOW, internal_ids=set(), venture_names={})
    assert result["chat"]["classified_user_requests"] == 2
    assert result["chat"]["work_requested"] == 1
    assert result["chat"]["paired_saved_responses"] == 1
    assert result["chat"]["without_paired_response_receipt"] == 1
    assert result["chat"]["median_saved_response_seconds"] == 0
    assert result["chat"]["saved_responses_over_30_seconds"] == 0
    assert result["chat"]["response_kinds"] == {"confirmation": 1}
    assert result["chat"]["offered_action_cards"] == 2
    assert result["chat"]["intent_categories"] == {"assistant": 2, "change": 1}
    assert result["chat"]["distinct_requesting_people_by_intent"] == {"assistant": 1, "change": 1}
    assert result["chat"]["topics"] == {"product_build": 2, "status_blocker": 1}
    assert result["chat"]["distinct_requesting_people_by_topic"] == {"product_build": 1, "status_blocker": 1}
    assert "must not be surfaced" not in json.dumps(result)


def test_chat_response_latency_uses_only_same_scope_saved_pairs():
    def event(event_id, name, at, *, workspace="wsp_a", ref=None):
        props = ({"source": "chat", "intent_categories": ["assistant"], "topics": ["status_blocker"],
                  "work_requested": False} if ref is None else
                 {"source": "chat", "request_event_id": ref, "response_kind": "text", "offered_actions": 0})
        return SimpleNamespace(id=event_id, event_name=name, person_id="founder", workspace_id=workspace,
                               occurred_at=at, venture_id="ven_a", props_json=json.dumps(props))

    rows = [event("ane_one", "request.received", NOW - timedelta(seconds=60)),
            event("ane_two", "request.received", NOW - timedelta(seconds=50)),
            event("ane_late_duplicate", "request.outcome", NOW, ref="ane_one"),
            event("ane_outone", "request.outcome", NOW - timedelta(seconds=50), ref="ane_one"),
            event("ane_outtwo", "request.outcome", NOW, ref="ane_two"),
            event("ane_wrong", "request.outcome", NOW - timedelta(seconds=49), workspace="wsp_other", ref="ane_two"),
            event("ane_backwards", "request.outcome", NOW - timedelta(seconds=55), ref="ane_two")]
    chat = calculate(rows, [], [], now=NOW, internal_ids=set(), venture_names={})["chat"]
    assert chat["paired_saved_responses"] == 2
    assert chat["median_saved_response_seconds"] == 30
    assert chat["saved_responses_over_30_seconds"] == 1


def test_computer_use_funnel_counts_durable_milestones_without_inventing_goal_success():
    created = NOW - timedelta(minutes=5)
    approved = order("ven_a", created, state="cancelled", stopped=False)
    pending = SimpleNamespace(venture_id="ven_a", created_at=created, provenance_json=json.dumps({
        "state": "pending", "manifest": {"actions": ["app.navigate"]},
    }))
    result = calculate([], [], [approved, pending], now=NOW, internal_ids=set(), venture_names={})
    assert result["computer_use"]["approval_funnel"] == {
        "prepared": 2, "approved": 1, "claimed": 1, "closed": 1,
    }
    assert result["computer_use"]["approval_funnel_by_type"] == {
        "computer_use_navigation": {"prepared": 2, "approved": 1, "claimed": 1, "closed": 1},
    }
    assert result["computer_use"]["median_prepared_to_approval_seconds"] == 0
    assert result["computer_use"]["orders_by_state"] == {"cancelled": 1, "pending": 1}


def test_a_message_to_charlie_makes_the_founder_active_that_day():
    chats = [("user", NOW - timedelta(minutes=5), "founder_cafe"),          # today
             ("assistant", NOW - timedelta(minutes=4), None),                # Charlie's reply never counts
             ("user", NOW - timedelta(days=3), "founder_saas"),              # this week, not today
             ("user", NOW - timedelta(days=20), "founder_clinic"),           # this month only
             ("user", NOW - timedelta(minutes=2), "")]                       # no author: ignored
    result = calculate([], [], [], now=NOW, internal_ids=set(), venture_names={}, chat_messages=chats)
    activity = result["activity"]
    assert (activity["dau"], activity["wau"], activity["mau"]) == (1, 2, 3)
    # Older (role, created_at) rows without an author still load.
    assert calculate([], [], [], now=NOW, internal_ids=set(), venture_names={},
                     chat_messages=[("user", NOW)])["activity"]["dau"] == 0
