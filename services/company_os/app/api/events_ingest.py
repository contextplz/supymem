"""Public product-analytics ingest. Unknown keys return 204 (do not leak)."""

from __future__ import annotations

import json
import re
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from app.analytics.feature_catalog import INGEST_FEATURES, control_features
from app.product_analytics.service import ingest_public
from core.bounded_body import BodyRejected, events_body
from core.runtime_diagnostics import closed_runtime_diagnostic, compatible_runtime_diagnostic

router = APIRouter(tags=["events-ingest"])


class TrackBody(BaseModel):
    key: str
    event: str
    ts: str | None = None
    anon_id: str | None = None
    user_hash: str | None = None
    props: dict | None = None


@router.post("/e")
async def track(request: Request):
    """Anonymous hosted-product beacon. Auth is the ``pk_app_`` key itself."""
    try:
        raw = await events_body(request)
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            return Response(status_code=204)
        body = TrackBody.model_validate(payload)
    except (BodyRejected, UnicodeDecodeError, json.JSONDecodeError, ValidationError, TypeError, ValueError):
        # Oversize, bad JSON, or schema failures never disclose whether a key exists.
        return Response(status_code=204)
    stored = ingest_public(body.model_dump())
    if not stored:
        return Response(status_code=204)
    return {"ok": True}


_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_SECRET = re.compile(r"\b(?:Bearer|Basic)\s+[\w.~+/=-]{8,}|\b(?:sk|pk|rk|ek|ghp|gho|ghs|xox[a-z]|AKIA)[-_][\w-]{8,}"
                     r"|\beyJ[\w-]{8,}\.[\w-]{4,}\.[\w-]*|\b[A-Fa-f0-9]{32,}\b|\b[A-Za-z0-9+/_-]{40,}={0,2}", re.I)


def scrub_error_text(value: str) -> str:
    """Second line of defense behind the client scrub: emails and credential-shaped strings never get stored."""
    return " ".join(_SECRET.sub("[token]", _EMAIL.sub("[email]", value)).split())[:300]


class UsageProps(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    # Labels come from the feature catalog so one edit there adds a feature.
    feature: Literal[INGEST_FEATURES]  # type: ignore[valid-type]
    platform: Literal["macos", "web"]
    app_version: str | None = Field(default=None, pattern=r"^\d{1,4}\.\d{1,4}\.\d{1,4}$")
    source: Literal["chat"] | None = None
    session_id: UUID = Field(strict=False)
    duration_ms: int | None = Field(default=None, ge=0, le=86400000)
    error_code: Literal["network_error", "server_error", "request_error", "native_error", "desktop_cua_observation_unpinned", "desktop_cua_transport_unavailable", "cu_browser_decision_unavailable", "render_error", "runtime_error", "integrity_error", "job_error", "ui_error", "voice_error"] | None = None
    error_context: Literal["api", "native", "render", "runtime", "promise", "integrity", "job", "ui", "voice"] | None = None
    # Closed label for system contexts; for ui/voice contexts the (scrubbed) message the user actually saw.
    error_text: str | None = Field(default=None, max_length=300)
    error_ref: str | None = Field(default=None, pattern=r"^req_[0-9abcdefghjkmnpqrstvwxyz]{12}$")
    error_operation: Literal[
        "auth_session_read", "venture_list_read",
        "venture_read", "venture_dashboard_read", "venture_onboarding_read", "venture_charter_read",
        "venture_waitlist_read", "venture_waitlist_test", "workspace_site_read",
        "venture_plans_read", "venture_plan_read", "venture_resources_read",
        "app_analytics_overview_read", "app_analytics_funnel_read",
        "research_dossiers_read", "workspace_llm_settings_read", "workspace_llm_catalog_read",
        "venture_delete_phrase_read", "mac_order_read", "mac_source_receipt_read",
        "mac_source_preflight_read", "mac_order_approve", "mac_order_cancel",
        "venture_activity_stream", "venture_events_stream",
    ] | None = None
    error_origin: Literal["native_auth_busy", "native_auth_unavailable", "credential_store_failed"] | None = None
    error_auth_reason: Literal["invalid_gateway_origin", "invalid_saved_credentials", "authorization_expired",
                               "authorization_rejected", "refresh_outcome_unknown", "refresh_rejected_or_uncertain",
                               "invalid_refresh_response", "device_revoked_or_expired", "unclassified"] | None = None
    error_auth_http_class: Literal["4xx", "5xx", "other", "unknown"] | None = None
    error_transport: Literal["native_bridge", "native_stream", "browser_fetch"] | None = None
    error_native_command: Literal[
        "check_for_update", "install_update", "defer_update", "request_update_check",
        "desktop_auth_status", "desktop_auth_start", "desktop_auth_complete_code", "desktop_auth_sign_out",
        "desktop_permission_status", "desktop_permission_targets", "desktop_permission_request", "open_macos_privacy_pane",
        "set_autostart", "open_setup_window", "reveal_in_file_manager", "mac_notice_test",
        "connect_local_tool", "install_local_builder", "uninstall_local_builder", "install_docker", "start_docker",
        "spawn_coding_control", "kill_coding_control", "device_work_order_action", "inspect_device_work_order",
        "desktop_http_request", "desktop_api_request", "desktop_events_subscribe", "listen_event",
        "desktop_auth_reconnect", "desktop_app_inventory", "desktop_launch_installed_app",
        "desktop_events_unsubscribe", "gateway_url", "backend_status", "open_composio_connect_link",
        "store_passphrase", "mac_notice_diagnostic", "data_dir_path", "venture_workspace_path",
        "set_active_venture", "start_cursor_ide_session", "read_venture_landing", "list_local_clis",
        "list_local_tools", "macos_control_status", "show_permission_coach", "docker_status",
        "write_hermes_config", "write_hermes_files", "stdio_write", "autostart_enabled", "app_version",
    ] | None = None
    error_order_id: str | None = Field(default=None, pattern=r"^dwo_[A-Za-z0-9_-]{1,64}$")
    job_id: str | None = Field(default=None, pattern=r"^job_[A-Za-z0-9_-]{1,64}$")
    control_id: str | None = Field(default=None, min_length=1, max_length=40, pattern=r"^[a-z][a-z0-9_]*$")
    order_id: str | None = Field(default=None, pattern=r"^dwo_[A-Za-z0-9_-]{1,64}$")
    # Closed route template (ids as {id}) and HTTP status of the failed call, plus the service's own
    # machine error code (a slug like insufficient_credits, never a message).
    error_route: str | None = Field(default=None, pattern=r"^/[a-z0-9_{}./-]{1,80}$")
    error_status: int | None = Field(default=None, ge=0, le=599)
    error_server_code: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,39}$")
    # "transient": recorded but excluded from user-facing error counts (retried / expected states).
    error_severity: Literal["error", "transient"] | None = None
    error_repeat_count: int | None = Field(default=None, ge=1, le=100000)
    outcome: Literal["ok", "failed"] | None = None
    # User-visible errors (context ui/voice): where it came from, what the user was doing, the page, when, and
    # an upstream request/response/event id.
    error_source: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_.-]{0,63}$")
    error_intent: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_.-]{0,63}$")
    error_page: str | None = Field(default=None, pattern=r"^/[A-Za-z0-9_{}./-]{0,120}$")
    error_occurred_at: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}T[0-9:.]{8,12}Z$")
    error_request_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.:-]{1,80}$")
    runtime_diagnostic: dict | None = None
    reason: Literal["unknown", "panic", "unclean_exit", "supervisor_start_failed", "console_navigation_failed",
                    "console_unreachable", "update_failed", "sidecar_spawn_failed", "sidecar_port_conflict",
                    "sidecar_binary_missing", "sidecar_permission_denied", "sidecar_startup_exited",
                    "sidecar_health_timeout", "sidecar_health_unavailable", "sidecar_protocol_mismatch", "sidecar_exited"] | None = None

    @field_validator("runtime_diagnostic", mode="before")
    @classmethod
    def require_closed_runtime_diagnostic(cls, value):
        if value is None:
            return None
        parsed = closed_runtime_diagnostic(value)
        if parsed is None:
            raise ValueError("runtime diagnostic requires closed bounded metadata")
        return parsed

    @field_validator("error_text")
    @classmethod
    def require_closed_error_text(cls, value: str | None, info) -> str | None:
        if value is not None and info.data.get("error_context") in {"ui", "voice"}:
            return scrub_error_text(value)
        if value is not None and len(value) > 40:
            raise ValueError("error text must use a fixed diagnostic label")
        if value is not None and value not in {
            "Mac command_failed", "Mac process_crashed", "Mac panic_observed", "Mac uncaught_exception", "Mac native_failure",
            "Render failure", "Runtime failure", "Promise failure", "Integrity failure", "Job failure",
        } and not re.fullmatch(r"API request_failed \(status (?:0|[4-5][0-9]{2})\)", value):
            raise ValueError("error text must use a fixed diagnostic label")
        return value


class ObservabilityProps(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    feature: Literal[INGEST_FEATURES]  # type: ignore[valid-type]
    route: str = Field(pattern=r"^/(?:[a-z][a-z0-9_-]*|\{id\})(?:/(?:[a-z][a-z0-9_-]*|\{id\}))*$|^/$", max_length=80)
    code: Literal["ui_error_shown", "reconnect_shown", "empty_no_action", "action_failed"] | None = None
    step: Literal["idea", "clarify", "validate", "review", "find_customers", "review_leads", "sent", "call_booked"] | None = None

    @field_validator("route")
    @classmethod
    def closed_route(cls, value):
        from app.analytics.feature_catalog import console_route

        if console_route(value) != value:
            raise ValueError("route must be a known console template")
        return value


class TelemetryBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event: Literal["ui.screen_view", "ui.navigation", "feature.used", "ui.control_activate", "ui.control_exposure", "ui.control_outcome", "session.active", "ui.error", "desktop.process_heartbeat", "ui.dead_end", "journey.step_entered", "journey.step_completed"]
    event_id: UUID
    context_subject: str | None = Field(default=None, max_length=128)
    props: UsageProps | ObservabilityProps
    workspace_id: str | None = Field(default=None, max_length=128)
    venture_id: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def validate_control(self):
        if self.event in {"ui.dead_end", "journey.step_entered", "journey.step_completed"}:
            if any(value is not None and not re.fullmatch(r"[A-Za-z0-9_:-]{1,128}", value)
                   for value in (self.workspace_id, self.venture_id)):
                raise ValueError("scope must use opaque IDs")
            if not isinstance(self.props, ObservabilityProps):
                raise ValueError("observability requires content-free props")
            if self.event == "ui.dead_end":
                if self.props.code is None or self.props.step is not None:
                    raise ValueError("dead end requires code only")
            elif self.props.step is None or self.props.code is not None or not self.venture_id:
                raise ValueError("journey requires step and venture only")
            return self
        if not isinstance(self.props, UsageProps):
            raise ValueError("usage requires usage props")
        if (self.event == "ui.navigation") != (self.props.source == "chat"):
            raise ValueError("chat source requires navigation event")
        if self.props.error_order_id is not None and self.venture_id is None:
            raise ValueError("order correlation requires a venture")
        if self.props.feature == "shell" and self.event != "ui.error":
            raise ValueError("shell label is only for UI errors")
        if self.event == "ui.error":
            if not self.props.error_code or not self.props.error_context or not self.props.error_text:
                raise ValueError("UI error requires diagnostic code, context and text")
            if self.props.runtime_diagnostic is not None and (
                self.props.platform != "macos" or self.props.error_context != "native"
                or compatible_runtime_diagnostic(self.props.runtime_diagnostic, self.props.error_source,
                    self.props.error_intent, self.props.error_text) is None
            ):
                raise ValueError("runtime diagnostic requires a compatible native failure")
            expected = {
                "api": {"network_error", "server_error", "request_error"},
                "native": {"native_error", "desktop_cua_observation_unpinned", "desktop_cua_transport_unavailable", "cu_browser_decision_unavailable"},
                "render": {"render_error"},
                "runtime": {"runtime_error"},
                "promise": {"runtime_error"},
                "integrity": {"integrity_error"},
                "job": {"job_error"},
                "ui": {"ui_error"},
                "voice": {"voice_error"},
            }
            if self.props.error_code not in expected[self.props.error_context]:
                raise ValueError("diagnostic code and context do not match")
            if self.props.error_code in {"desktop_cua_observation_unpinned", "desktop_cua_transport_unavailable", "cu_browser_decision_unavailable"} and (
                self.props.feature != "computer_use" or self.props.platform != "macos"
                or self.props.error_text != "Mac native_failure"
                or self.props.error_source != "device_worker" or self.props.error_intent != "run_mac_task"
            ):
                raise ValueError("CUA failure requires a reviewed Mac run diagnostic")
            user_visible = self.props.error_context in {"ui", "voice"}
            if user_visible and (self.props.error_source is None or self.props.error_intent is None):
                raise ValueError("user-visible errors require source and intent")
            if not user_visible and any(getattr(self.props, n) is not None for n in (
                    "error_page", "error_occurred_at", "error_request_id")):
                raise ValueError("page, time and request id require ui or voice context")
            if self.props.error_ref is not None and self.props.error_context != "api":
                raise ValueError("request reference requires API context")
            if self.props.error_operation is not None and self.props.error_context != "api":
                raise ValueError("operation label requires API context")
            if self.props.error_native_command is not None and (
                self.props.error_context != "native" or self.props.platform != "macos"
                or self.props.error_text != "Mac command_failed"
            ):
                raise ValueError("native command label requires a Mac command failure")
            if self.props.error_transport is not None and (
                self.props.error_context != "api" or
                (self.props.error_transport.startswith("native_") and self.props.platform != "macos")
            ):
                raise ValueError("transport label requires compatible API context")
            if self.props.error_origin is not None and (
                self.props.error_context != "api" or self.props.platform != "macos"
                or self.props.error_text != "API request_failed (status 503)"
                or self.props.error_transport == "browser_fetch"
            ):
                raise ValueError("native origin requires a Mac API 503")
            if (self.props.error_auth_reason is not None or self.props.error_auth_http_class is not None):
                if (self.props.error_auth_reason is None or self.props.error_auth_http_class is None
                    or self.props.error_context != "native" or self.props.platform != "macos"
                    or self.props.feature != "shell" or self.props.error_text != "Mac native_failure"
                    or self.props.error_native_command is not None):
                    raise ValueError("auth transition requires closed native shell receipt")
            if self.props.error_order_id is not None and (
                self.props.error_context != "api" or self.props.feature != "computer_use"
                or self.props.error_operation not in {"mac_order_read", "mac_source_receipt_read",
                                                      "mac_source_preflight_read", "mac_order_approve", "mac_order_cancel"}
            ):
                raise ValueError("order correlation requires an exact Mac-order API operation")
            text = self.props.error_text
            if user_visible:
                pass
            elif self.props.error_context == "api":
                if not text.startswith("API request_failed (status "):
                    raise ValueError("diagnostic label and context do not match")
                status = int(text.removeprefix("API request_failed (status ").removesuffix(")"))
                network = status == 0 or (status == 503 and self.props.error_server_code in {
                    "gateway_unreachable", "session_verification_failed",
                })
                expected_code = "network_error" if network else "server_error" if status >= 500 else "request_error"
                if self.props.error_code != expected_code:
                    raise ValueError("API status and diagnostic code do not match")
            else:
                labels = {
                    "native": {"Mac command_failed", "Mac process_crashed", "Mac panic_observed", "Mac uncaught_exception", "Mac native_failure"},
                    "render": {"Render failure"}, "runtime": {"Runtime failure"},
                    "promise": {"Promise failure"}, "integrity": {"Integrity failure"}, "job": {"Job failure"},
                }
                if text not in labels[self.props.error_context]:
                    raise ValueError("diagnostic label and context do not match")
            if (self.props.error_route is not None or self.props.error_server_code is not None
                    or self.props.error_status is not None) and self.props.error_context not in {"api", "ui", "voice"}:
                raise ValueError("route, status and server code require API, ui or voice context")
            if self.props.error_route is not None and user_visible:
                raise ValueError("route requires API context")
            if self.props.outcome is not None:
                raise ValueError("outcome requires a control outcome event")
        elif self.event == "ui.control_outcome":
            if self.props.reason is not None:
                raise ValueError("reason requires UI error event")
            if self.props.outcome is None or self.props.control_id is None:
                raise ValueError("control outcome requires outcome and control ID")
            if (self.props.error_status is not None or self.props.error_server_code is not None) and self.props.outcome != "failed":
                raise ValueError("status and server code require a failed outcome")
            if any(getattr(self.props, name) is not None for name in (
                    "error_route", "error_code", "error_context", "error_text", "error_ref", "error_operation",
                    "error_origin", "error_transport", "error_order_id", "error_native_command",
                    "error_auth_reason", "error_auth_http_class", "error_severity", "error_repeat_count",
                    "error_source", "error_intent", "error_page", "error_occurred_at", "error_request_id")):
                raise ValueError("control outcome carries only outcome, status and server code")
            if self.props.runtime_diagnostic is not None:
                raise ValueError("runtime diagnostic requires UI error event")
        elif (self.props.error_code is not None or self.props.error_context is not None
              or self.props.error_text is not None or self.props.error_ref is not None
              or self.props.error_operation is not None or self.props.error_origin is not None
              or self.props.error_transport is not None or self.props.error_order_id is not None
              or self.props.error_native_command is not None or self.props.error_auth_reason is not None
              or self.props.error_auth_http_class is not None or self.props.error_route is not None
              or self.props.error_status is not None or self.props.error_server_code is not None
              or self.props.error_severity is not None or self.props.error_repeat_count is not None
              or self.props.error_source is not None or self.props.error_intent is not None
              or self.props.error_page is not None or self.props.error_occurred_at is not None
              or self.props.error_request_id is not None
              or self.props.runtime_diagnostic is not None or self.props.reason is not None
              or self.props.outcome is not None):
            raise ValueError("diagnostic fields require UI error event")
        if self.props.job_id is not None and (self.event != "ui.error" or self.props.error_context != "job"):
            raise ValueError("job ID requires job error event")
        control = self.props.control_id
        if self.event in {"ui.control_activate", "ui.control_exposure", "ui.control_outcome"}:
            if not control or self.props.feature not in control_features(control):
                raise ValueError("unknown or mismatched control ID")
        elif control is not None:
            raise ValueError("control ID requires a control event")
        if self.props.order_id is not None and (control != "mac_start" or self.event not in {"ui.control_activate", "ui.control_exposure"}):
            raise ValueError("order ID requires a Mac Start control event")
        if self.event == "ui.control_exposure" and control == "mac_start" and self.props.order_id is None:
            raise ValueError("Mac Start exposure requires an order ID")
        return self


@router.post("/telemetry")
def telemetry(
    body: TelemetryBody,
    request: Request,
    x_subject: str | None = Header(default=None, alias="X-Subject"),
):
    if body.event == "desktop.process_heartbeat":
        from app.admin.authn import verify_caller

        caller = verify_caller(request, required=True)
        if caller.legacy or caller.role != "user" or not caller.device_id:
            raise HTTPException(403, detail={"error": {"code": "authenticated_device_required"}})
        from app.analytics.usage import record_process_heartbeat

        record_process_heartbeat(caller.sub, caller.device_id, body.props.app_version)
    from app.analytics.usage import capture
    if not x_subject:
        raise HTTPException(401, detail={"error": {"code": "unauthenticated"}})
    return capture(body.model_dump(mode="json"), x_subject)
