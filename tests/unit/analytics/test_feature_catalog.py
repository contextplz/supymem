"""The feature catalog is the one place a feature label is defined."""

import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from tests.company_os_harness import load_company_os_app

ROOT = Path(__file__).resolve().parents[3]
NEW_LABELS = {"experiments", "waitlist", "publish", "outreach", "gtm_diagnosis", "voice", "greeting", "tray",
              "updater", "permissions", "local_tools", "account", "venture_admin", "simulate", "billing",
              "llm_settings", "guards"}


def test_every_inventory_feature_maps_to_a_slug():
    load_company_os_app()
    from app.analytics.feature_catalog import FEATURES, INVENTORY_SLUG

    text = (ROOT / "docs/supymem/feature-audit-2026-09-30/INVENTORY.md").read_text()
    ids = re.findall(r"^\| ([A-I]\d+) \|", text, re.MULTILINE)
    assert len(ids) == len(set(ids)) == 146
    assert set(ids) == set(INVENTORY_SLUG), "new or removed inventory id: update INVENTORY_SLUG"
    assert set(INVENTORY_SLUG.values()) <= set(FEATURES)


def test_labels_groups_and_ingest_list_are_consistent():
    load_company_os_app()
    from app.analytics.feature_catalog import FEATURES, INGEST_FEATURES, LABEL_ALIAS, LEGACY_LABELS

    assert set(LEGACY_LABELS) <= set(FEATURES) and NEW_LABELS <= set(FEATURES)
    assert {group for group, _ in FEATURES.values()} <= set(LEGACY_LABELS)
    assert set(LABEL_ALIAS.values()) <= set(FEATURES)
    assert set(INGEST_FEATURES) == set(FEATURES) | {"voice_chat", "mac_tasks"}
    assert all(re.fullmatch(r"[a-z][a-z0-9_]*", label) for label in INGEST_FEATURES)


def _props(feature, **extra):
    return {"feature": feature, "platform": "macos", "session_id": "0f9ff3a5-2a9d-4a1b-9d20-7c0f7a7e7c11", **extra}


def test_ingest_accepts_new_labels_and_keeps_old_ones():
    load_company_os_app()
    from app.analytics.feature_catalog import INGEST_FEATURES
    from app.api.events_ingest import UsageProps

    for label in INGEST_FEATURES:
        assert UsageProps(**_props(label)).feature == label
    with pytest.raises(ValidationError):
        UsageProps(**_props("not_a_feature"))


def test_control_events_accept_legacy_group_or_fine_slug_only():
    load_company_os_app()
    from app.analytics.control_catalog import CONTROL_FEATURE
    from app.analytics.feature_catalog import CONTROL_SLUG, FEATURES, NEW_CONTROL_SLUG, control_features
    from app.api.events_ingest import TelemetryBody

    def body(control, feature):
        return {"event": "ui.control_activate", "event_id": "8b7d3f3e-1d1b-4c6e-a3a8-0a9d3d6f3c10",
                "props": _props(feature, control_id=control)}

    assert set(CONTROL_FEATURE) <= set(CONTROL_SLUG)
    assert set(CONTROL_SLUG.values()) <= set(FEATURES)
    assert all(len(control) <= 40 and re.fullmatch(r"[a-z][a-z0-9_]*", control) for control in NEW_CONTROL_SLUG)
    assert not set(NEW_CONTROL_SLUG) & set(CONTROL_FEATURE)
    # An old client still sends the coarse label; a new one sends the fine slug.
    TelemetryBody.model_validate(body("waitlist_enable_website", "website"))
    TelemetryBody.model_validate(body("waitlist_enable_website", "waitlist"))
    TelemetryBody.model_validate(body("tray_new_venture", "tray"))
    for control, feature in (("waitlist_enable_website", "gtm"), ("tray_new_venture", "shell"), ("nope", "tray")):
        with pytest.raises(ValidationError):
            TelemetryBody.model_validate(body(control, feature))
    assert control_features("nope") == frozenset()


def test_standing_approval_controls_accept_closed_labels_across_venture_domains():
    load_company_os_app()
    from app.analytics.feature_catalog import control_features
    from app.api.events_ingest import TelemetryBody

    for venture_id in ("venture_procurement", "venture_music"):
        for control, feature in (("approval_always", "notifications"), ("approval_deny", "notifications"),
                                 ("approval_revoke", "settings")):
            assert control_features(control) == frozenset({feature})
            body = {"event": "ui.control_activate", "event_id": "8b7d3f3e-1d1b-4c6e-a3a8-0a9d3d6f3c10",
                    "venture_id": venture_id, "props": _props(feature, control_id=control)}
            TelemetryBody.model_validate(body)
            with pytest.raises(ValidationError):
                TelemetryBody.model_validate({**body, "props": {**body["props"], "feature": "credits"}})


def test_role_preset_controls_accept_only_closed_settings_labels_across_domains():
    load_company_os_app()
    from app.analytics.feature_catalog import control_features
    from app.api.events_ingest import TelemetryBody

    for venture_id in ("venture_fleet_maintenance", "venture_home_gardening"):
        for control in ("charlie_role_preset", "charlie_role_preset_save"):
            assert control_features(control) == frozenset({"settings"})
            body = {"event": "ui.control_activate", "event_id": "8b7d3f3e-1d1b-4c6e-a3a8-0a9d3d6f3c10",
                    "venture_id": venture_id, "props": _props("settings", control_id=control)}
            TelemetryBody.model_validate(body)
            with pytest.raises(ValidationError):
                TelemetryBody.model_validate({**body, "props": {**body["props"], "feature": "credits"}})


def test_every_registered_chat_action_and_legacy_map_is_in_the_catalog():
    load_company_os_app()
    from app.analytics.chat_usage import ACTION_FEATURE, READ_INTENT_FEATURE, REVIEWED_CHAT_FEATURE
    from app.analytics.feature_catalog import ACTION_SLUG, FEATURES
    from app.chat.actions import REGISTRY

    assert set(REGISTRY) <= set(ACTION_SLUG)
    assert set(ACTION_FEATURE) | set(READ_INTENT_FEATURE) | set(REVIEWED_CHAT_FEATURE) <= set(ACTION_SLUG)
    assert set(ACTION_SLUG.values()) <= set(FEATURES)
    assert ACTION_SLUG["experiment.record"] == "experiments" and ACTION_SLUG["site.publish"] == "publish"
    assert ACTION_SLUG["waitlist.enable"] == "waitlist" and ACTION_SLUG["outreach.draft"] == "outreach"


def test_job_kinds_and_routes_resolve_to_catalog_slugs():
    load_company_os_app()
    from app.analytics.feature_catalog import FEATURES, JOB_FEATURES, job_feature, route_feature
    from app.analytics.snapshot import _job_feature
    from app.worker.catalog import enabled_kinds

    assert job_feature("gtm.god.run") == "gtm_diagnosis" and job_feature("deploy.run") == "publish"
    assert job_feature("analytics.rollup") == "other"
    for kind in enabled_kinds():
        assert job_feature(kind) in set(FEATURES) | {"other"}
    # The operator snapshot keeps its legacy coarse labels.
    assert _job_feature("deploy.run") == "website" and set(JOB_FEATURES.values()) <= set(FEATURES)
    cases = {
        "/ventures/{venture_id}/waitlist": "waitlist", "/ventures/{venture_id}/experiments/start": "experiments",
        "/ventures/{venture_id}/chat/confirm": "chat", "/ventures/{venture_id}/chat/gtm/quote": "gtm_diagnosis",
        "/ventures/{venture_id}/device-work-orders/{order_id}": "computer_use", "/apps/{app_id}/deployments": "publish",
        "/ventures/{venture_id}/credits/topup": "billing", "/w/{slug}/site": "website", "/telemetry": "shell",
        "/workspaces/{workspace_id}/llm-settings": "llm_settings", "/ventures": "ventures", None: "shell",
    }
    for template, slug in cases.items():
        assert route_feature(template) == slug, template


def test_every_app_route_resolves_to_a_known_slug(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/cos.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    main = load_company_os_app()
    from app.analytics.feature_catalog import FEATURES, route_feature

    paths = set(main.create_app().openapi()["paths"])
    assert len(paths) > 100
    assert {route_feature(path) for path in paths} <= set(FEATURES) | {"other"}


def test_reference_rows_cover_every_slug_and_inventory_id():
    load_company_os_app()
    from app.analytics.feature_catalog import FEATURES, INVENTORY_SLUG, reference_rows

    catalog, mapping = reference_rows()
    assert {row["slug"] for row in catalog} == set(FEATURES)
    listed = {i for row in catalog for i in row["inventory_ids"].split(",") if i}
    assert listed == set(INVENTORY_SLUG)
    assert {row["kind"] for row in mapping} == {"control", "tool", "label"}


def test_control_outcome_and_api_error_route_fields():
    load_company_os_app()
    from app.api.events_ingest import TelemetryBody

    eid = "8b7d3f3e-1d1b-4c6e-a3a8-0a9d3d6f3c10"
    ok = {"event": "ui.control_outcome", "event_id": eid,
          "props": _props("settings", control_id="settings_manager_rename", outcome="failed", error_status=402,
                          error_server_code="insufficient_credits")}
    TelemetryBody.model_validate(ok)
    api_error = {"event": "ui.error", "event_id": eid, "props": _props(
        "builds", error_code="request_error", error_context="api", error_text="API request_failed (status 409)",
        error_route="/ventures/{id}/builds", error_status=409, error_server_code="build_busy")}
    TelemetryBody.model_validate(api_error)
    bad_bodies = [
        {**ok, "props": _props("settings", control_id="settings_manager_rename")},  # outcome required
        {**ok, "props": _props("settings", control_id="settings_manager_rename", outcome="ok", error_status=500)},
        {**ok, "props": _props("settings", control_id="nope", outcome="ok")},
        {"event": "feature.used", "event_id": eid, "props": _props("builds", error_route="/ventures/{id}/builds")},
        {**api_error, "props": {**api_error["props"], "error_route": "/Has Spaces?q=1"}},
        {**api_error, "props": {**api_error["props"], "error_server_code": "Bad Message text"}},
        {**api_error, "props": _props("shell", error_code="runtime_error", error_context="runtime",
                                      error_text="Runtime failure", error_route="/x")},
    ]
    for bad in bad_bodies:
        with pytest.raises(ValidationError):
            TelemetryBody.model_validate(bad)


def test_user_visible_error_fields_are_accepted_scrubbed_and_closed():
    load_company_os_app()
    from app.api.events_ingest import TelemetryBody

    eid = "8b7d3f3e-1d1b-4c6e-a3a8-0a9d3d6f3c10"
    seen = {"event": "ui.error", "event_id": eid, "props": _props(
        "voice_chat", error_code="voice_error", error_context="voice", app_version="0.1.54",
        error_text="Conversation already has an active response in progress: resp_abc. mail me a@b.co sk-live_abcdef123456",
        error_source="voice.realtime", error_intent="voice_turn", error_page="/v/{id}/chat",
        error_occurred_at="2026-10-01T12:34:56.789Z", error_request_id="resp_AbC123",
        error_server_code="conversation_already_has_active_response")}
    body = TelemetryBody.model_validate(seen)
    assert "a@b.co" not in body.props.error_text and "sk-live" not in body.props.error_text
    assert "active response in progress" in body.props.error_text  # the message the user saw survives
    ui = {**seen, "props": {**seen["props"], "error_code": "ui_error", "error_context": "ui"}}
    TelemetryBody.model_validate(ui)
    bad = [
        {**seen, "props": {k: v for k, v in seen["props"].items() if k != "error_intent"}},  # intent required
        {**seen, "props": {**seen["props"], "error_code": "ui_error"}},  # code must match context
        {**seen, "props": {**seen["props"], "error_source": "Has Spaces"}},
        {**seen, "props": _props("shell", error_code="runtime_error", error_context="runtime", error_text="Runtime failure",
                                 error_page="/x")},  # page needs ui/voice context
        {**seen, "props": _props("shell", error_code="runtime_error", error_context="runtime", error_text="a" * 41)},
        {"event": "feature.used", "event_id": eid, "props": _props("builds", error_source="x.y")},
    ]
    for item in bad:
        with pytest.raises(ValidationError):
            TelemetryBody.model_validate(item)
