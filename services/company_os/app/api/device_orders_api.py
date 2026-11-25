"""Signed device-bound review lifecycle; these routes do not launch native tools."""
import hashlib
import json
import logging
import os
from dataclasses import asdict
from typing import Literal
from urllib.parse import quote

from coding_agents.jev_decision import Observation
from core.artifact_policy import DEFAULT_LIMITS
from core.clock import utcnow
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.admin.authn import verify_caller as _verify_caller
from app.core.local_mode import is_local
from app.build import (
    coding_source_intake,
    coding_source_preflight,
    device_effects,
    device_invocations,
    device_jev,
    device_browser_navigation,
    device_browser_ai,
    device_vision,
    device_ai_step,
    device_orders,
    device_desktop_pause,
    device_stop,
)
from app.credits.errors import CreditError, InsufficientCredits

router = APIRouter(tags=["device-work-orders"])
log = logging.getLogger(__name__)


def verify_caller(request, *, required=True):
    return device_orders.device_caller(_verify_caller(request, required=required))


class ReviewBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    review_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    read_only_parser_supported: Literal[True] | None = None
    browser_qualification_parser_supported: Literal[True] | None = None
    product_build_parser_supported: Literal[True] | None = None
    unstarted_only: Literal[True] | None = None

    @field_validator("unstarted_only", mode="before")
    @classmethod
    def exact_unstarted_condition(cls, value):
        if value is not True:
            raise ValueError("unstarted_only_requires_true")
        return value

    @field_validator("read_only_parser_supported", "browser_qualification_parser_supported", "product_build_parser_supported", mode="before")
    @classmethod
    def exact_parser_acknowledgement(cls, value):
        if value is not None and value is not True:
            raise ValueError("read_only_parser_acknowledgement_required")
        return value


CONFLICTS = {
    "device_order_not_executing", "device_order_requires_reconciliation", "device_order_lease_closed",
    "device_coding_loop_unavailable", "device_coding_loop_scope_invalid", "device_product_build_unavailable",
    "device_coding_request_contains_secret", "device_coding_request_too_long",
    "device_coding_plan_unavailable", "device_coding_plan_invalid", "device_coding_receipt_invalid",
    "device_coding_iteration_execution_invalid", "device_coding_iteration_conflict", "device_coding_iteration_order_invalid",
    "device_deploy_source_unavailable", "device_deploy_source_changed", "device_deploy_receipt_invalid",
    "device_deploy_project_unavailable",
    "device_coding_subscription_required", "device_product_scope_unsupported",
    "device_task_not_ready", "device_task_has_active_order", "device_order_review_changed",
    "device_order_expired", "device_order_task_changed", "device_order_transition_invalid",
    "device_budget_already_reserved", "device_budget_not_reserved", "device_budget_binding_invalid",
    "device_budget_unavailable", "device_budget_requires_reconciliation",
    "device_public_review_task_unavailable", "device_public_review_deployment_ambiguous",
    "device_public_review_task_ambiguous", "device_chat_intent_unsupported",
    "device_read_only_browser_task_unavailable",
    "device_desktop_multisurface_unavailable", "device_desktop_verification_required", "device_desktop_modal_policy_unavailable", "device_chat_email_required", "device_desktop_paused", "device_desktop_pause_invalid",
    "device_desktop_execution_unavailable",
    "device_general_browser_execution_unavailable", "device_typed_navigation_unavailable",
    "device_browser_ai_configuration_unavailable", "device_browser_ai_request_invalid",
    "device_browser_ai_request_bounds", "device_browser_ai_already_attempted",
    "device_browser_ai_provider_or_usage_unconfirmed", "device_browser_ai_usage_bounds",
    "device_browser_ai_target_invalid", "device_browser_ai_type_unreviewed",
    "device_browser_ai_action_unsupported", "device_browser_ai_terminal_effect_refused",
    "device_browser_ai_plan_invalid", "device_browser_ai_verdict_invalid",
    "device_browser_ai_reply_invalid",
    "device_decision_already_attempted", "device_effect_requires_reconciliation",
    "device_terminal_task_unavailable", "device_terminal_task_ambiguous",
    "device_coding_task_unavailable", "device_coding_provider_unsupported", "device_coding_offline_profile_unavailable",
    "device_static_analytics_app_unavailable",
    "device_coding_focus_too_long", "device_coding_qa_feedback_unavailable", "device_coding_qa_feedback_bounds",
    "device_personal_build_unavailable", "device_personal_build_app_ambiguous",
    "subscription_static_app_ambiguous", "subscription_static_publication_baseline_ambiguous",
    "subscription_static_publication_baseline_changed", "subscription_static_publication_baseline_unverified",
    "device_inspection_provider_unsupported",
    "device_startup_reconciliation_invalid", "device_startup_reconciliation_unavailable",
    "device_startup_reconciliation_conflict", "device_startup_reconciliation_budget_inconsistent",
    "device_desktop_click_ack_unavailable", "device_desktop_click_ack_conflict",
    "device_unknown_usage_reconciliation_invalid", "device_unknown_usage_reconciliation_unavailable",
    "device_unknown_usage_reconciliation_too_early", "device_unknown_usage_reconciliation_conflict",
    # AI-primary Mac loop: surfaced verbatim so the sidecar can stop truthfully (credit/call cap, scope, provider).
    "device_ai_step_scope_invalid", "device_ai_step_request_invalid", "device_ai_step_already_attempted",
    "device_ai_step_provider_failed", "device_ai_step_provider_or_usage_unconfirmed",
    "device_invocation_budget_exhausted", "device_invocation_limit",
    "device_vision_not_enabled",
    "device_pause_kind_invalid", "device_pause_superseded",
    "pagebuilder_candidate_invalid", "pagebuilder_candidate_changed", "pagebuilder_reference_unavailable",
    "pagebuilder_runner_upgrade_required",
    "browser_qualification_unavailable", "browser_qualification_binding_invalid",
    "browser_qualification_usd_limit", "browser_qualification_pricing_changed",
    "browser_qualification_task_seed_changed", "browser_qualification_grant_changed",
    "browser_qualification_cleanup_unconfirmed", "browser_qualification_order_not_executing",
    "browser_qualification_cancel_requires_reconciliation", "browser_qualification_effect_unavailable",
    "browser_qualification_effect_binding_changed", "browser_qualification_request_protected",
}


_LOCAL_BUILD_MESSAGES = {
    "device_coding_request_contains_secret": "Remove credentials from your build request. Save your keys in Settings > Keys and connections, then resend the request without secrets.",
    "device_coding_request_too_long": "In your venture chat, shorten your request to the code changes you want made, then send it again. Nothing was started.",
    "device_coding_focus_too_long": "Shorten your request or the saved task on Plan, then retry. Your complete coding instructions must fit within 12000 characters. No coding task was prepared or started.",
    "device_coding_plan_invalid": "Open Builds in your venture, review the failed plan check, then ask Charlie to repair the plan with your connected coding tool.",
    "device_coding_loop_scope_invalid": "Open Builds in your venture, review the failed scope check, then revise your build request in your venture chat. Nothing was narrowed or started.",
    "device_coding_task_invalid": "Open Builds in your venture, review the failed task check, then ask Charlie to repair its requirements and acceptance checks using your connected coding tool.",
    "device_coding_receipt_invalid": "Your coding tool's result could not be verified. Open Builds in your venture and check your saved files and any completed deployment before asking Charlie to repair the result. Do not restart or publish again until you have checked what completed.",
}
CONFLICTS.update(_LOCAL_BUILD_MESSAGES)


def _conflict_detail(code: str) -> dict:
    if code not in CONFLICTS:
        code = "device_order_invalid"
    detail: dict = {"code": code}
    if is_local() and code in _LOCAL_BUILD_MESSAGES:
        return {**detail, "message": _LOCAL_BUILD_MESSAGES[code]}
    if code == "device_order_requires_reconciliation":
        detail["message"] = "This Mac task has an uncertain action or process. Review its saved result before resuming."
    elif code in {"device_order_lease_closed", "device_order_not_executing"}:
        detail["message"] = "This Mac task is no longer executing. Review its saved result or prepare a fresh request."
    elif code == "device_typed_navigation_unavailable":
        detail["message"] = "The exact next reviewed link could not be qualified. No provider call or new browser action was authorized by this request."
    elif code == "device_general_browser_execution_unavailable":
        detail["message"] = device_orders.GENERAL_BROWSER_EXECUTION_MESSAGE
    elif code == "device_browser_ai_configuration_unavailable":
        detail["message"] = "Browser task settings changed since this task was prepared. Cancel the saved task and rerun it for a fresh review, or ask Charlie for manual steps. Started work: stop it and review its saved result first."
    elif code == "device_browser_ai_request_invalid":
        detail["message"] = "Which website should I use? Send its HTTPS URL with the task, without passwords or payment details."
    elif code == "device_browser_ai_provider_or_usage_unconfirmed":
        detail["message"] = ("The admitted browser model call has an unknown outcome. Its reservation remains held; this call will not be repeated. "
            "Stop this task and review its saved result before preparing a new task; contact support to reconcile the held credits.")
    elif code == "device_coding_loop_unavailable":
        if is_local():
            from app.core.local_capabilities import blocked_message
            detail["message"] = blocked_message("build_with_my_tool")
        else:
            detail["message"] = "Product builds on your Mac are not enabled for your account yet."
    elif code == "device_coding_subscription_required":
        detail["message"] = "In Settings > Keys and connections > Coding tools, sign in to the tool you requested with your own subscription, then check the connection again. API-key login cannot start this build."
    elif is_local() and code in {"device_coding_receipt_invalid", "device_coding_iteration_execution_invalid",
            "device_coding_iteration_conflict", "device_coding_iteration_order_invalid"}:
        detail["message"] = ("Your build activity could not be saved for this run. Open Build activity in your venture and choose Refresh build. "
            "If your tool has stopped, choose Retry to start a new build.")
    elif code == "device_product_build_unavailable":
        detail["message"] = "Confirm your product idea before asking a coding agent to build it."
    elif code == "device_coding_plan_unavailable":
        detail["message"] = ("Open Builds in your venture and check whether your requested plan finished. "
            "If you stopped it or changed your idea, ask in your venture chat for a new build."
            if is_local() else "Finish reviewing the saved product plan before starting its build.")
    elif code in {"device_deploy_source_unavailable", "device_deploy_source_changed"}:
        detail["message"] = "Finish a product build with passing checks before reviewing its deployment."
    elif code == "device_deploy_project_unavailable":
        detail["message"] = "Deploy a preview first so production uses the same reviewed Vercel project."
    elif code == "device_chat_intent_unsupported":
        detail["message"] = device_orders.UNSUPPORTED_MAC_TASK_MESSAGE
        detail["supported"] = list(device_orders.SUPPORTED_MAC_TASKS)
    elif code == "device_read_only_browser_task_unavailable":
        detail["message"] = (
            "Free-form read-only background browser tasks are not available through the current task profile. "
            "Its policy cannot enforce your full no-change constraint. This request did not prepare or start "
            "a new task or reserve credits. The reviewed live-site/waitlist check remains available when "
            "the current approved plan and published site provide its exact scope."
        )
    elif code == "device_desktop_multisurface_unavailable":
        detail["message"] = "Multiple-window tasks are not enabled on this runtime yet. Use one reviewed window, or wait for the reviewed multi-window update. No task was started."
    elif code == "device_desktop_verification_required":
        detail["message"] = ("This multi-window goal needs one supported final check in the first selected window: "
            "an exact visible result supplied by you, or supported exact document contents. "
            "Ambiguous or unsupported checks cannot start a task.")
    elif code == "device_desktop_paused":
        detail["message"] = device_desktop_pause.MESSAGE
    elif code == device_orders.DESKTOP_EXECUTION_UNAVAILABLE:
        detail["message"] = device_orders.DESKTOP_EXECUTION_MESSAGE
    elif code == "device_task_has_active_order":
        detail["message"] = "Open Build activity for your current task and choose Stop or Cancel, then retry your request."
    elif code == "device_coding_task_unavailable":
        detail["message"] = (
            "The current approved plan step is not a coding task. "
            "Complete or review that step before asking a coding agent to edit the venture workspace."
        )
    elif code == "device_coding_provider_unsupported":
        detail["message"] = ("In Settings > Keys and connections > Coding tools, connect and verify your coding tool. "
            "Then ask in your venture chat to build with that tool. Nothing was started."
            if is_local() else
            "Reviewed background coding runs through Codex or Claude Code 2.1.284 and newer. "
            "Cursor is available for a reviewed read-only workspace inspection; "
            "Ask Supymem to use Codex or Claude for an approved static landing task, then review the exact coding card."
        )
    elif code == "device_coding_focus_too_long":
        detail["message"] = (
            "Shorten your request or the saved task on Plan, then retry. "
            "Your complete coding instructions must fit within 12000 characters. "
            "No coding task was prepared or started."
        ) if is_local() else (
            "Shorten this coding request to 1200 characters or fewer. Name the approved task and a concise repair focus; "
            "Supymem includes its saved approved contract and verified QA findings when available. "
            "No coding task was prepared or started."
        )
    elif code == "device_coding_qa_feedback_unavailable":
        detail["message"] = (
            "Supymem could not verify the saved QA evidence for this coding review. "
            "Open this venture's Builds and review the current task's QA results, then refresh before preparing a new review. "
            "No coding task was started."
        )
    elif code == "device_coding_qa_feedback_bounds":
        detail["message"] = (
            "The approved scope and QA evidence do not fit this coding review. "
            "Shorten the requested focus or review and revise the approved plan; the exact contract cannot be silently shortened. "
            "No coding task was prepared or started."
        )
    elif code == "device_coding_offline_profile_unavailable":
        detail["message"] = (
            "This task has no supported offline build profile. Reviewed native coding currently supports "
            "an approved static landing contract using plain HTML, CSS and JavaScript with bundled assets. "
            "Framework dependencies, personal/native product builds and backend or database connections "
            "are not provisioned through this route. Choose an approved static landing task, or use the "
            "managed product build when available. Historical coding cards need a fresh supported review. "
            "This request did not authorize a coding launch or reserve credits."
        )
    elif code == "device_personal_build_unavailable":
        detail["message"] = "Confirm the current personal project idea before reviewing a local build."
    elif code == "device_personal_build_app_ambiguous":
        detail["message"] = "This project has more than one product. Open Builds to choose which one to work on."
    elif code == "device_inspection_provider_unsupported":
        detail["message"] = (
            "A read-only workspace inspection is available for Cursor. "
            "Asking another provider to inspect does not grant an edit session."
        )
    if is_local() and "message" not in detail:
        detail["message"] = ("Your build request could not be prepared. Open Builds in your venture and review the saved result, "
            "then retry from your venture chat. If it stops again, open Account settings > Report a problem and include code " + code + ".")
    return detail


def call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except PermissionError as error:
        detail = {"code": "device_order_forbidden"}
        if is_local():
            detail["message"] = "Open your desktop app and retry this build from your own venture."
        raise HTTPException(403, detail=detail) from error
    except KeyError as error:
        detail = {"code": "device_order_not_found"}
        if is_local():
            detail["message"] = "Open Builds in your venture and choose its saved build, then try again."
        raise HTTPException(404, detail=detail) from error
    except InsufficientCredits as error:
        raise HTTPException(402, detail={"code": "insufficient_credits"}) from error
    except CreditError as error:
        if error.code == "local_spend_limit":
            raise HTTPException(402, detail={"code": error.code, "message": str(error)}) from error
        raise HTTPException(409, detail={"code": "device_budget_unavailable"}) from error
    except device_orders.ActiveDeviceOrder as error:
        order = error.order
        detail = _conflict_detail("device_task_has_active_order")
        detail["message"] = error.message
        order_id = str(order.get("id") or "")
        venture_id = str(order.get("venture_id") or "")
        if order_id and venture_id:
            detail["order_id"] = order_id
            detail["review_path"] = (
                f"/v/{quote(venture_id, safe='')}/device-work-orders/{quote(order_id, safe='')}"
            )
        raise HTTPException(409, detail=detail) from error
    except ValueError as error:
        if is_local():
            from app.build.product_scope import UnsupportedProductScope
            from app.llm.founder_provider import FounderLLMConfigError

            if isinstance(error, UnsupportedProductScope):
                raise HTTPException(409, detail={"code": "device_product_scope_unsupported",
                    "message": " ".join(error.reasons) + " In your venture chat, revise and confirm your idea without these requirements, then ask for a local web build. To keep your full requirements, use your coding tool directly."}) from error
            if isinstance(error, FounderLLMConfigError):
                raise HTTPException(409, detail={"code": "device_vision_not_enabled", "message": str(error)}) from error
        if type(error) is ValueError and str(error) == "device_decisions_not_enabled":
            raise HTTPException(503, detail={"code": "device_decisions_not_enabled"}) from error
        code = str(error) if type(error) is ValueError and str(error) in CONFLICTS else "device_order_invalid"
        log.warning("device_order_request_failed", extra={"error_code": code})
        raise HTTPException(409, detail=_conflict_detail(code)) from error


def invoke(fn, *args, **kwargs):
    body = call(fn, *args, **kwargs)
    return {**body, "review_path": f"/v/{quote(body['venture_id'], safe='')}/device-work-orders/{quote(body['id'], safe='')}"}


class DecisionBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    invocation_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")
    observation: Observation


class VisionBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    invocation_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")
    snapshot_id: str = Field(pattern=r"^ps1_[A-Za-z0-9]+$")
    screenshot_png_base64: str = Field(min_length=1, max_length=2_100_000)
    observation: Observation | None = None


@router.post("/ventures/{venture_id}/device-work-orders/{order_id}/browser-decision")
def browser_decision(venture_id: str, order_id: str, body: DecisionBody, request: Request):
    caller = verify_caller(request, required=True)
    result = call(device_jev.decide_browser, venture_id, caller, order_id,
                  invocation_id=body.invocation_id, observation=body.observation)
    candidate = {key: value for key, value in asdict(result).items()
                 if key != "value_index" or value is not None}
    return {"authority": "proposal_only", "candidate": candidate}


@router.post("/ventures/{venture_id}/device-work-orders/{order_id}/browser-navigation-proposal")
def browser_navigation_proposal(venture_id: str, order_id: str,
        body: device_browser_navigation.NavigationBody, request: Request):
    return call(device_browser_navigation.propose, venture_id, execution_caller(request),
                order_id, request=body)


@router.post("/ventures/{venture_id}/device-work-orders/{order_id}/browser-ai-call")
def browser_ai_call(venture_id: str, order_id: str,
        body: device_browser_ai.BrowserAIBody, request: Request):
    return call(device_browser_ai.run, venture_id, execution_caller(request), order_id, request=body)


@router.post("/ventures/{venture_id}/device-work-orders/{order_id}/desktop-vision")
def desktop_vision(venture_id: str, order_id: str, body: VisionBody, request: Request):
    caller = verify_caller(request, required=True)
    return call(device_vision.review_desktop, venture_id, caller, order_id,
                invocation_id=body.invocation_id, snapshot_id=body.snapshot_id,
                screenshot_png_base64=body.screenshot_png_base64,
                observation=body.observation)


class AIStepBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    invocation_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")
    kind: Literal["step", "plan", "verify"]
    context: dict
    image_png_base64: str | None = Field(default=None, max_length=2_100_000)


@router.post("/ventures/{venture_id}/device-work-orders/{order_id}/ai-step")
def ai_step(venture_id: str, order_id: str, body: AIStepBody, request: Request):
    """AI-primary Mac loop: metered model proposal (step), plan, or final verification. Proposal only."""
    caller = verify_caller(request, required=True)
    return call(device_ai_step.run, venture_id, caller, order_id, invocation_id=body.invocation_id,
                kind=body.kind, context=body.context, image_base64=body.image_png_base64)


@router.post("/ventures/{venture_id}/device-work-orders")
def prepare(venture_id: str, body: device_orders.Manifest, request: Request):
    caller = verify_caller(request, required=True)
    return invoke(device_orders.prepare, venture_id, caller, body)


@router.post("/ventures/{venture_id}/plan-tasks/{task_id}/public-site-review")
def prepare_public_site_review(venture_id: str, task_id: str, request: Request):
    caller = verify_caller(request, required=True)
    return invoke(device_orders.prepare_public_site_review, venture_id, caller, task_id)


class ChatDeviceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    instruction: str = Field(min_length=1, max_length=12000)
    modal_policy: device_orders.ReviewedModalPolicy | None = None
    reviewed_surfaces: list[device_orders.ReviewedDesktopSurface] | None = Field(default=None, min_length=1, max_length=8)
    target_application: str | None = Field(default=None, pattern=r"^[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+$", max_length=200)
    target_pid: int | None = Field(default=None, gt=0)
    target_process_start: str | None = Field(default=None, pattern=r"^[0-9]{1,24}$")
    target_window: int | None = Field(default=None, gt=0)
    target_window_title: str | None = Field(default=None, max_length=200)
    source: str | None = Field(default=None, max_length=16)
    browser_execution_profile_digest: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    pagebuilder_candidate: device_orders.CandidateSelection | None = None


@router.get("/ventures/{venture_id}/device-browser-ai-profile")
def browser_ai_profile(venture_id: str, request: Request):
    return call(device_browser_ai.available_profile, venture_id, verify_caller(request, required=True))


@router.post("/ventures/{venture_id}/device-work-orders/from-chat")
def prepare_from_chat(venture_id: str, body: ChatDeviceRequest, request: Request):
    from app.api.voice_api import reject_voice_confirm

    reject_voice_confirm(body.source)
    return _prepare_from_chat(venture_id, verify_caller(request, required=True), body)


@router.post("/ventures/{venture_id}/device-work-orders/{order_id}/renew")
def renew(venture_id: str, order_id: str, request: Request):
    """Recreate a lapsed review or retry a stopped local build from its saved request."""
    from app.chat.store import idempotent_message

    caller = verify_caller(request, required=True)
    old = call(device_orders.retrieve, venture_id, caller, order_id)
    loop = old["manifest"].get("coding_loop_verification") or {}
    profile = loop.get("profile")
    utility = profile == "workspace_utility_v1"
    expired = (old["state"] in {"pending", "approved"} and not old.get("claimed_at")
               and old["expires_at"] <= utcnow().isoformat())
    expired_requested_product = (profile == "web_app_vercel_v1" and expired and old["state"] == "approved"
                                 and not old.get("execution_id") and old.get("requested_build"))
    unstarted_failure = (old["state"] == "failed" and not old.get("claimed_at") and not old.get("execution_id")
        and old.get("failure_code") in device_orders.CODING_START_FAILURES)
    if is_local() and loop and (old["state"] in {"stopped", "cancelled"}
            or utility and old["state"] == "failed" or unstarted_failure or expired_requested_product):
        if (old.get("stop_reconciliation_required") or old.get("inference_reconciliation_required")
                or (old.get("claimed_at") or old.get("execution_id")) and (old.get("native_stop") or {}).get("processes_stopped") is not True):
            raise HTTPException(409, detail=_conflict_detail("device_order_requires_reconciliation"))
        if profile == "static_landing_v1":
            from app.pagebuilder import api as pages

            job_id = old.get("task_snapshot", {}).get("pagebuilder_job_id")
            if not job_id:
                raise HTTPException(409, detail={"code": "pagebuilder_not_found", "message":
                    "This build is not linked to a saved page. Open Website and ask Charlie to build a new landing page."})
            result = pages._start(venture_id, caller, new_version_of=job_id, retry_job_id=job_id)
            order = result.get("order_id")
            base = f"/v/{quote(venture_id, safe='')}"
            return {**result, "review_path": f"{base}/device-work-orders/{quote(order, safe='')}" if order else f"{base}/site"}
        if utility:
            from app.ventures.lifecycle import is_paused

            if is_paused(venture_id):
                raise HTTPException(409, detail={"code": "venture_paused", "message":
                    "Resume your venture in its chat, then return to this build and choose Retry build."})
            prepared = call(device_orders.prepare, venture_id, caller, device_orders.Manifest.model_validate(old["manifest"]),
                            recover_active=True, requested_build=old.get("requested_build"))
            return {**prepared, "review_path": f"/v/{quote(venture_id, safe='')}/device-work-orders/{quote(prepared['id'], safe='')}"}
        if (profile != "web_app_vercel_v1" or not old.get("requested_build")
                or old["review_digest"] != device_orders._digest(old)):
            raise HTTPException(409, detail=_conflict_detail("device_order_review_changed"))
        retry_id = "dwo_" + hashlib.sha256(f"build-retry:{order_id}".encode()).hexdigest()[:32]
        prepared = call(device_orders.prepare, venture_id, caller, device_orders.Manifest.model_validate(old["manifest"]),
            requested_build=old["requested_build"], order_id=retry_id, retry_of=order_id)
        if prepared["state"] == "pending":
            return invoke(device_orders.transition, venture_id, caller, prepared["id"], prepared["review_digest"], "approve")
        return {**prepared, "review_path": f"/v/{quote(venture_id, safe='')}/device-work-orders/{quote(prepared['id'], safe='')}"}
    lapsed = expired or (old["state"] == "cancelled" and not old.get("claimed_at"))
    if not lapsed:
        raise HTTPException(409, detail=_conflict_detail("device_order_invalid"))
    loop = old["manifest"].get("coding_loop_verification") or {}
    deploy = old["manifest"].get("deploy_verification")
    if loop.get("phase") == "implement" or deploy:
        # These reviews are derived from immutable device receipts, not a
        # second chat message. Renewal grants only a fresh pending review.
        if caller.device_id != old["device_id"] or old["review_digest"] != device_orders._digest(old):
            raise HTTPException(409, detail=_conflict_detail("device_order_review_changed"))
        if deploy:
            return invoke(device_orders.prepare_product_deploy, venture_id, caller,
                deploy["source_order_id"], deploy["target"])
        caps = device_orders.CodingLoopCaps.model_validate({key: loop[key] for key in
            ("max_iterations", "max_wall_seconds", "per_call_seconds", "max_turns") if key in loop})
        return invoke(device_orders.prepare_product_run, venture_id, caller, loop["plan_order_id"], caps)
    conversation_id = (old.get("browser_chat_scope") or {}).get("conversation_id")
    saved = idempotent_message(venture_id, "user", f"mac-review:{order_id}:user", conversation_id=conversation_id)
    if not saved:
        raise HTTPException(409, detail=_conflict_detail("device_order_invalid"))
    old_profile = (old["manifest"].get("general_browser_verification") or {}).get("execution_profile")
    if conversation_id and old_profile:
        from app.chat.conversations import bind_conversation
        from app.chat.store import append_message
        with bind_conversation(venture_id, conversation_id):
            prepared = invoke(device_orders.prepare_browser_task, venture_id, caller, saved["body_md"],
                              conversation_id=conversation_id)
            append_message(venture_id, role="user", body_md=saved["body_md"], conversation_id=conversation_id,
                           idempotency_key=f"mac-review:{prepared['id']}:user")
            return prepared
    return _prepare_from_chat(venture_id, caller, ChatDeviceRequest(instruction=saved["body_md"],
        browser_execution_profile_digest=device_browser_ai.profile_digest(old_profile) if old_profile is not None else None,
        pagebuilder_candidate={key: old["manifest"]["coding_verification"]["pagebuilder_candidate"][key]
            for key in ("job_id", "variant")} if (old["manifest"].get("coding_verification") or {}).get("pagebuilder_candidate") else None))


def _prepare_from_chat(venture_id: str, caller, body: ChatDeviceRequest):
    from app.secrets.detect import looks_like_secret

    if looks_like_secret(body.instruction):
        raise HTTPException(409, detail={"code": "device_chat_secret_use_form",
            "message": "Do not paste credentials into a Mac task. Use the secure connection form."})
    if body.pagebuilder_candidate is not None:
        if any(value is not None for value in (body.browser_execution_profile_digest, body.target_application,
                body.target_pid, body.target_process_start, body.target_window, body.target_window_title,
                body.reviewed_surfaces, body.modal_policy)):
            raise HTTPException(409, detail=_conflict_detail("pagebuilder_candidate_invalid"))
    if device_orders.readonly_browser_task_request(body.instruction) and body.browser_execution_profile_digest is None:
        # Even explicit target/surface fields cannot turn this text into an
        # unrestricted native or general-browser task.
        raise HTTPException(409, detail=_conflict_detail("device_read_only_browser_task_unavailable"))
    if device_orders.coding_loop_request(body.instruction) is not None:
        if any(value is not None for value in (body.pagebuilder_candidate, body.browser_execution_profile_digest,
                body.target_application, body.target_pid, body.target_process_start, body.target_window,
                body.target_window_title, body.reviewed_surfaces, body.modal_policy)):
            raise HTTPException(409, detail=_conflict_detail("device_coding_loop_scope_invalid"))
        prepared = invoke(device_orders.prepare_product_build_from_chat, venture_id, caller, body.instruction)
    elif body.browser_execution_profile_digest is not None:
        if any(value is not None for value in (body.target_application, body.target_pid,
                body.target_process_start, body.target_window, body.target_window_title,
                body.reviewed_surfaces, body.modal_policy)):
            raise HTTPException(409, detail=_conflict_detail(device_browser_ai.INVALID))
        offered = call(device_browser_ai.available_profile, venture_id, caller)
        if not offered.get("available") or offered.get("execution_profile_digest") != body.browser_execution_profile_digest:
            raise HTTPException(409, detail=_conflict_detail(device_browser_ai.UNAVAILABLE))
        prepared = invoke(device_orders.prepare_general_browser_from_chat, venture_id, caller, body.instruction,
            device_orders.BrowserAIProfile.model_validate(offered["execution_profile"]))
    elif body.reviewed_surfaces is not None:
        if any(value is not None for value in (body.target_application, body.target_pid,
                body.target_process_start, body.target_window, body.target_window_title)):
            raise HTTPException(422, detail="Choose either one legacy target or explicit reviewed surfaces.")
        prepared = invoke(device_orders.prepare_multisurface_from_chat,
                          venture_id, caller, body.instruction, body.reviewed_surfaces, body.modal_policy)
    else:
        if body.modal_policy is not None:
            raise HTTPException(422, detail="Modal policy requires explicit reviewed surfaces.")
        prepared = invoke(device_orders.prepare_from_chat,
                          venture_id, caller, body.instruction, body.target_application,
                          body.target_pid, body.target_process_start, body.target_window,
                          body.target_window_title,
                          **({"pagebuilder_candidate": body.pagebuilder_candidate.model_dump()}
                             if body.pagebuilder_candidate is not None else {}))
    # from-chat bypasses the ordinary chat endpoint. Save the same reviewed
    # request/card once per durable order so reopening the app keeps the
    # conversation, and operator chat analytics includes computer-use asks.
    from app.analytics.events import emit_once
    from app.authz.workspace import subject_person_id
    from app.chat.analytics_topics import classify_topics
    from app.chat.store import append_message

    order_id = prepared["id"]
    title = prepared["manifest"]["title"]
    workspace_id = prepared["workspace_id"]
    review_path = prepared["review_path"]
    append_message(venture_id, role="user", body_md=body.instruction,
                   idempotency_key=f"mac-review:{order_id}:user")
    requested = is_local() and bool(prepared.get("requested_build"))
    append_message(venture_id, role="assistant", kind="text" if requested else "confirmation",
        body_md=(f"Your requested build is ready to start with your coding tool: **{title}**. You can watch it and stop at any time."
                 if requested else f"I prepared a Mac task for review: **{title}**. Nothing runs on your Mac until you open the review and approve it."),
        actions=[{"label": "Watch your build" if requested else "Review Mac task", "href": review_path, "style": "primary"}],
        idempotency_key=f"mac-review:{order_id}:prepared")
    person_id = subject_person_id(caller.sub)
    received = emit_once("request.received", event_key=f"mac_review:{order_id}:received",
        person_id=person_id, workspace_id=workspace_id, venture_id=venture_id,
        props={"source": "chat", "intent_categories": ["computer_use"],
               "topics": classify_topics(body.instruction), "work_requested": True,
               "invocation": "mac_review_prepared", "order_id": order_id})
    if received:
        emit_once("request.outcome", event_key=f"mac_review:{order_id}:prepared",
            person_id=person_id, workspace_id=workspace_id, venture_id=venture_id,
            props={"source": "chat", "request_event_id": received,
                   "response_kind": "text" if requested else "confirmation", "offered_actions": 1})
        # Count the durable computer-use preparation as use of that feature,
        # separate from the chat request and any later Mac execution outcome.
        # The order key makes a retried request idempotent.
        emit_once("feature.used", event_key=f"mac_review:{order_id}:feature",
            person_id=person_id, workspace_id=workspace_id, venture_id=venture_id,
            props={"source": "chat", "feature": "computer_use",
                   "invocation": "mac_review_prepared", "order_id": order_id})
    return prepared


class DesktopPauseBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    paused: bool


@router.get("/ventures/{venture_id}/device-desktop-pause")
def desktop_pause_status(venture_id: str, request: Request):
    caller = verify_caller(request, required=True)
    if is_local():
        from app.authz.workspace import assert_venture_access

        assert_venture_access(venture_id, caller.sub)
        return {"paused": False, "execution_available": False,
                "unavailability_code": device_orders.DESKTOP_EXECUTION_UNAVAILABLE,
                "execution_message": "Computer control is not included in your local app. Ask in your venture chat for steps you can follow yourself."}
    status = call(device_desktop_pause.status, venture_id, caller)
    _, actor, device = call(device_orders._scope, venture_id, caller)
    from app.build.device_desktop_pilot import available as pilot_available
    with device_orders.db()() as session:
        enabled = pilot_available(session, actor, device)
    return {**status, "execution_available": enabled and not status["paused"],
            "unavailability_code": None if enabled else device_orders.DESKTOP_EXECUTION_UNAVAILABLE,
            "execution_message": None if enabled else device_orders.DESKTOP_EXECUTION_MESSAGE}


@router.put("/ventures/{venture_id}/device-desktop-pause")
def desktop_pause_set(venture_id: str, body: DesktopPauseBody, request: Request):
    return call(device_desktop_pause.set_pause, venture_id, verify_caller(request, required=True), body.paused)


@router.get("/ventures/{venture_id}/device-work-orders")
def list_orders(venture_id: str, request: Request):
    caller = verify_caller(request, required=True)
    return {"orders": [{**o, "review_path": f"/v/{quote(venture_id, safe='')}/device-work-orders/{quote(o['id'], safe='')}"}
                       for o in call(device_orders.list_orders, venture_id, caller)]}


@router.get("/ventures/{venture_id}/device-work-orders/{order_id}")
def retrieve(venture_id: str, order_id: str, request: Request):
    caller = verify_caller(request, required=True)
    return invoke(device_orders.retrieve, venture_id, caller, order_id)


def transition(venture_id, order_id, body, request, action):
    caller = verify_caller(request, required=True)
    if body.unstarted_only is True and action != "cancel":
        raise HTTPException(409, detail=_conflict_detail("device_order_transition_invalid"))
    if action == "approve":
        saved = call(device_orders.retrieve, venture_id, caller, order_id)
        if (saved["manifest"].get("coding_loop_verification") or saved["manifest"].get("deploy_verification")) and body.product_build_parser_supported is not True:
            raise HTTPException(409, detail={"code": "device_product_build_parser_unavailable",
                "message": "Update the Mac app before approving this product build. No task was started."})
        if saved.get("browser_qualification_scope") is not None and body.browser_qualification_parser_supported is not True:
            raise HTTPException(409, detail={"code": "browser_qualification_parser_unavailable",
                "message": "Update the Mac app before approving this qualification task. No new credits were reserved."})
        if device_orders.general_browser_read_only(saved["manifest"]) and body.read_only_parser_supported is not True:
            raise HTTPException(409, detail={"code": "device_read_only_browser_parser_unavailable",
                "message": "Update the Mac app before approving this read-only browser task. No new credits were reserved."})
    return invoke(device_orders.transition, venture_id, caller, order_id, body.review_digest, action,
                  **({"unstarted_only": True} if body.unstarted_only is True else {}))


@router.post("/ventures/{venture_id}/device-work-orders/{order_id}/approve")
def approve(venture_id: str, order_id: str, body: ReviewBody, request: Request):
    return transition(venture_id, order_id, body, request, "approve")


@router.post("/ventures/{venture_id}/device-work-orders/{order_id}/claim")
def claim(venture_id: str, order_id: str, body: ReviewBody, request: Request):
    return transition(venture_id, order_id, body, request, "claim")


@router.post("/ventures/{venture_id}/device-work-orders/{order_id}/cancel")
def cancel(venture_id: str, order_id: str, body: ReviewBody, request: Request):
    return transition(venture_id, order_id, body, request, "cancel")


class StartFailureBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    review_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    failure_code: Literal["coding_sign_in_required", "coding_provider_not_installed", "coding_loop_scope_unsupported",
                          "coding_provider_changed", "coding_tool_not_connected",
                          "coding_checks_unavailable", "coding_capability_disabled", "native_execution_failed"]


@router.post("/ventures/{venture_id}/device-work-orders/{order_id}/fail-before-start")
def fail_before_start(venture_id: str, order_id: str, body: StartFailureBody, request: Request):
    caller = verify_caller(request, required=True)
    return invoke(device_orders.fail_before_start, venture_id, caller, order_id, body.review_digest, body.failure_code)


class EmergencyStopBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    source: str = Field(default="app", max_length=16)


@router.post("/ventures/{venture_id}/device-work-orders/{order_id}/emergency-stop")
def emergency_stop(venture_id: str, order_id: str, body: EmergencyStopBody, request: Request):
    """Idempotent founder Stop; the Mac has already stopped locally, this records it."""
    caller = verify_caller(request, required=True)
    return invoke(device_orders.emergency_stop, venture_id, caller, order_id, body.source)


@router.post("/ventures/{venture_id}/device-work-orders/{order_id}/acknowledge")
def acknowledge(venture_id: str, order_id: str, body: ReviewBody, request: Request):
    return transition(venture_id, order_id, body, request, "acknowledge")


class EffectBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    invocation_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")
    effect_digest: str = Field(pattern=r"^[a-f0-9]{64}$")


class QualificationEffectScope(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    grant_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    rule_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")
    request_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")
    request_digest: str = Field(pattern=r"^[a-f0-9]{64}$")


class EffectAdmission(EffectBody):
    observation_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    qualification: QualificationEffectScope | None = None


class QualificationEffectBinding(EffectBody):
    qualification: QualificationEffectScope


class EffectReceipt(EffectBody):
    outcome: str = Field(pattern=r"^(returned|uncertain|not_dispatched)$")
    pre_dispatch_code: str | None = Field(default=None, max_length=100)


@router.get("/ventures/{venture_id}/device-work-orders/{order_id}/capabilities")
def execution_capabilities(venture_id: str, order_id: str, request: Request):
    caller = verify_caller(request, required=True)
    order = call(device_orders.retrieve, venture_id, caller, order_id)
    actions = set(order.get("manifest", {}).get("actions", []))
    browser = bool(actions & {"browser.read", "browser.interact"})
    profile = (order.get("manifest", {}).get("general_browser_verification") or {}).get("execution_profile")
    available = os.environ.get("DEVICE_BROWSER_EXECUTION_ENABLED") == "1"
    typed_navigation = available and device_browser_navigation.supported(order.get("manifest", {}))
    native = device_orders.native_desktop_manifest(order.get("manifest", {}))
    native_available = False
    if native:
        from app.build.device_desktop_pilot import available as pilot_available
        with device_orders.db()() as session:
            native_available = caller.device_id == order["device_id"] and pilot_available(
                session, order["actor_id"], order["device_id"], order["manifest"])
        available = available and native_available
    if browser:
        if profile is not None:
            try:
                device_browser_ai.assert_configuration(profile)
            except ValueError:
                return {"available": False, "code": device_browser_ai.UNAVAILABLE,
                    "general_browser_execution_v1": False, "browser_cuai_text_v1": False,
                    "execution_profile_digest": device_browser_ai.profile_digest(profile)}
        else:
            available = (available and (typed_navigation or os.environ.get("JEV_DEVICE_DECISIONS_ENABLED") == "1"
                and bool(os.environ.get("TYPESAFE_API_KEY"))))
    if (order.get("manifest", {}).get("general_browser_verification") is not None
            and not device_orders.general_browser_execution_enabled(order.get("actor_id"))):
        return {"available": False, "code": device_orders.GENERAL_BROWSER_EXECUTION_UNAVAILABLE,
                "message": device_orders.GENERAL_BROWSER_EXECUTION_MESSAGE,
                "general_browser_execution_v1": False}
    if native and not native_available:
        return {"available": False, "code": device_orders.DESKTOP_EXECUTION_UNAVAILABLE,
                "message": device_orders.DESKTOP_EXECUTION_MESSAGE}
    result = {"available": available}
    if (order.get("manifest", {}).get("coding_loop_verification") or order.get("manifest", {}).get("deploy_verification")
            or is_local() and order.get("manifest", {}).get("coding_verification")):
        if is_local() and (order["manifest"].get("coding_loop_verification") or order["manifest"].get("coding_verification")):
            _, actor, device = call(device_orders._scope, venture_id, caller)
            try:
                device_orders.assert_coding_loop_available(order["manifest"], actor)
                available = device == order["device_id"] and device_orders.coding_loop_enabled(actor)
                if not available:
                    result.update(_conflict_detail("device_coding_loop_unavailable"))
            except ValueError as error:
                available = False
                result.update(_conflict_detail(str(error)))
            except HTTPException as error:
                available = False
                result.update(error.detail)
        else:
            available = available and caller.device_id == order["device_id"] and device_orders.coding_loop_enabled(order["actor_id"])
        result.update(available=available, product_build_v1=available,
            coding_loop_v1=bool(available and order["manifest"].get("coding_loop_verification")),
            coding_subscription_loop_v1=bool(available and order["manifest"].get("coding_loop_verification")),
            owned_deploy_v1=bool(available and order["manifest"].get("deploy_verification")))
        if not available:
            result.setdefault("code", "device_coding_loop_unavailable")
    if (order.get("manifest", {}).get("coding_verification") or {}).get("pagebuilder_candidate"):
        from app.pagebuilder.native_reference import runner_supported, RUNNER_UNAVAILABLE
        with device_orders.db()() as session:
            supported = caller.device_id == order["device_id"] and runner_supported(
                session, order["actor_id"], order["device_id"])
        result["pagebuilder_reference_v1"] = bool(available and supported)
        if not supported:
            result.update(available=False, code=RUNNER_UNAVAILABLE)
    if device_browser_navigation.supported(order.get("manifest", {})):
        result["typed_navigation_proposal_v1"] = typed_navigation
    if order.get("manifest", {}).get("general_browser_verification") is not None:
        result["general_browser_execution_v1"] = bool(available
            and device_orders.general_browser_execution_enabled(order.get("actor_id")))
    if profile is not None:
        result[profile["version"]] = bool(available and device_orders.general_browser_execution_enabled(order.get("actor_id")))
        result["execution_profile_digest"] = device_browser_ai.profile_digest(profile)
    if order.get("browser_qualification_scope") is not None:
        from app.build import browser_qualification as qualification
        result["browser_qualification_v1"] = bool(available and qualification.enabled(order["actor_id"]))
    return result


class ProductRunBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    caps: device_orders.CodingLoopCaps | None = None


class ProductDeployBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    target: Literal["preview", "production"]


@router.post("/ventures/{venture_id}/device-work-orders/{order_id}/prepare-run")
def prepare_product_run(venture_id: str, order_id: str, body: ProductRunBody, request: Request):
    return invoke(device_orders.prepare_product_run, venture_id, verify_caller(request, required=True), order_id, body.caps)


@router.post("/ventures/{venture_id}/device-work-orders/{order_id}/prepare-deploy")
def prepare_product_deploy(venture_id: str, order_id: str, body: ProductDeployBody, request: Request):
    return invoke(device_orders.prepare_product_deploy, venture_id, verify_caller(request, required=True), order_id, body.target)


async def _product_receipt(request, venture_id, order_id, model, record, limit):
    caller = verify_caller(request, required=True)
    payload = bytearray()
    async for chunk in request.stream():
        if len(payload) + len(chunk) > limit:
            raise HTTPException(413, detail={"code": "device_coding_receipt_invalid"})
        payload.extend(chunk)
    try:
        receipt = model.model_validate(json.loads(payload))
    except (ValidationError, ValueError, UnicodeError, RecursionError):
        code = ("device_coding_plan_invalid" if is_local() and model is device_orders.ProductPlanReceipt
                else "device_coding_receipt_invalid")
        log.warning("device_order_receipt_rejected", extra={"error_code": code})
        detail = _conflict_detail(code) if is_local() else {"code": code}
        raise HTTPException(422, detail=detail) from None
    from starlette.concurrency import run_in_threadpool
    return await run_in_threadpool(call, record, venture_id, caller, order_id, receipt)


@router.post("/ventures/{venture_id}/device-work-orders/{order_id}/plan")
async def post_product_plan(venture_id: str, order_id: str, request: Request):
    return await _product_receipt(request, venture_id, order_id, device_orders.ProductPlanReceipt,
        device_orders.record_product_plan, 32768)


@router.get("/ventures/{venture_id}/device-work-orders/{order_id}/plan")
def get_product_plan(venture_id: str, order_id: str, request: Request):
    return call(device_orders.product_plan, venture_id, verify_caller(request, required=True), order_id)


@router.post("/ventures/{venture_id}/device-work-orders/{order_id}/iterations")
async def post_product_iteration(venture_id: str, order_id: str, request: Request):
    return await _product_receipt(request, venture_id, order_id, device_orders.CodingLoopIteration,
        device_orders.record_coding_iteration, 4096)


@router.get("/ventures/{venture_id}/device-work-orders/{order_id}/iterations")
def get_product_iterations(venture_id: str, order_id: str, request: Request):
    return call(device_orders.coding_iterations, venture_id, verify_caller(request, required=True), order_id)


@router.post("/ventures/{venture_id}/device-work-orders/{order_id}/events")
async def post_coding_event(venture_id: str, order_id: str, request: Request):
    return await _product_receipt(request, venture_id, order_id, device_orders.CodingActivityEvent,
        device_orders.record_coding_event, 2048)


@router.get("/ventures/{venture_id}/device-work-orders/{order_id}/events")
def get_coding_events(venture_id: str, order_id: str, request: Request):
    return call(device_orders.coding_events, venture_id, verify_caller(request, required=True), order_id)


@router.post("/ventures/{venture_id}/device-work-orders/{order_id}/deploy-receipt")
async def post_product_deploy_receipt(venture_id: str, order_id: str, request: Request):
    return await _product_receipt(request, venture_id, order_id, device_orders.ProductDeployReceipt,
        device_orders.record_product_deploy, 2048)


@router.get("/ventures/{venture_id}/device-work-orders/{order_id}/deploy-receipt")
def get_product_deploy_receipt(venture_id: str, order_id: str, request: Request):
    return call(device_orders.product_deploy_receipt, venture_id, verify_caller(request, required=True), order_id)


def execution_caller(request):
    caller = verify_caller(request, required=True)
    if is_local():
        return device_orders.device_caller(caller)
    if not is_local() and os.environ.get("DEVICE_BROWSER_EXECUTION_ENABLED") != "1":
        raise HTTPException(503, detail={"code": "device_execution_not_enabled"})
    return caller


@router.post("/ventures/{venture_id}/device-work-orders/{order_id}/heartbeat")
def heartbeat(venture_id: str, order_id: str, body: device_orders.DeviceHeartbeat, request: Request):
    return call(device_orders.heartbeat, venture_id, verify_caller(request, required=True), order_id, body)


@router.get("/ventures/{venture_id}/device-work-orders/{order_id}/authority")
def current_authority(venture_id: str, order_id: str, request: Request):
    caller = execution_caller(request)
    call(device_invocations.check_current, venture_id, caller, order_id)
    return {"current": True}


@router.get("/ventures/{venture_id}/device-work-orders/{order_id}/pagebuilder-reference")
def pagebuilder_reference(venture_id: str, order_id: str, request: Request):
    from app.pagebuilder.native_reference import download
    blob = call(download, venture_id, execution_caller(request), order_id)
    return Response(blob, media_type="application/zip", headers={"Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff", "Content-Disposition": 'attachment; filename="pagebuilder-reference.zip"'})


@router.post("/ventures/{venture_id}/device-work-orders/{order_id}/effects/admit")
def admit_effect(venture_id: str, order_id: str, body: EffectAdmission, request: Request):
    admitted = call(device_effects.admit, venture_id, execution_caller(request), order_id, **body.model_dump())
    return {"admitted_now": admitted}


@router.post("/ventures/{venture_id}/device-work-orders/{order_id}/effects/bind-qualification")
def bind_qualification_effect(venture_id: str, order_id: str, body: QualificationEffectBinding, request: Request):
    bound = call(device_effects.bind_qualification, venture_id, execution_caller(request), order_id, **body.model_dump())
    return {"bound_now": bound}


@router.post("/ventures/{venture_id}/device-work-orders/{order_id}/effects/receipt")
def effect_receipt(venture_id: str, order_id: str, body: EffectReceipt, request: Request):
    # Receipts must remain writable when execution is disabled mid-run; the
    # receipt service accepts only a previously bound one-use effect identity.
    caller = verify_caller(request, required=True)
    call(device_effects.receipt, venture_id, caller, order_id, **body.model_dump())
    return {"recorded": True}


class StopReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    execution_id: str = Field(pattern=r"^dex_[A-Za-z0-9]+$", max_length=128)
    evidence_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    processes_stopped: bool
    task_verified: bool | None = None
    reason_code: str | None = Field(default=None, pattern=r"^cu_[a-z_]{1,40}$")
    outcome_code: Literal["ok", "blocked", "timeout", "auth_lost", "model_unavailable", "needs_founder"] | None = None


class StartupReconciliation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    execution_id: str = Field(pattern=r"^dex_[A-Za-z0-9]+$", max_length=128)
    evidence_digest: str = Field(pattern=r"^[a-f0-9]{64}$")


class UncertainClickAcknowledgement(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    acknowledged: Literal[True]


@router.post("/ventures/{venture_id}/device-work-orders/{order_id}/stop-receipt")
def stop_receipt(venture_id: str, order_id: str, body: StopReceipt, request: Request):
    caller = verify_caller(request, required=True)
    result = call(device_stop.record, venture_id, caller, order_id, **body.model_dump(exclude_none=True))
    return {"state": result["state"], "settled_micro": result.get("settled_micro")}


@router.post("/ventures/{venture_id}/device-work-orders/{order_id}/uncertain-click-ack")
def uncertain_click_ack(venture_id: str, order_id: str,
                        body: UncertainClickAcknowledgement, request: Request):
    caller = verify_caller(request, required=True)
    result = call(device_stop.acknowledge_uncertain_desktop_click,
                  venture_id, caller, order_id)
    return {"acknowledged": True,
            "execution_id": result["uncertain_desktop_click_ack"]["execution_id"]}


@router.post("/ventures/{venture_id}/device-work-orders/{order_id}/startup-reconciliation")
def startup_reconciliation(venture_id: str, order_id: str, body: StartupReconciliation, request: Request):
    caller = verify_caller(request, required=True)
    result = call(device_stop.reconcile_startup_failure, venture_id, caller, order_id, **body.model_dump())
    return {"state": result["state"], "settled_micro": result.get("settled_micro"),
            "reconciliation": "pre_inference_startup_failure"}


@router.post("/ventures/{venture_id}/device-work-orders/{order_id}/unknown-usage-reconciliation")
def unknown_usage_reconciliation(venture_id: str, order_id: str, body: StartupReconciliation, request: Request):
    caller = verify_caller(request, required=True)
    result = call(device_stop.settle_cancelled_unknown_usage, venture_id, caller, order_id,
                  **body.model_dump())
    return {"state": result["state"], "settled_micro": result.get("settled_micro"),
            "reconciliation": "admitted_quote_upper_bound",
            "actual_usage_unconfirmed": result["unknown_usage_reconciliation"]["actual_usage_unconfirmed"],
            "effect_outcome_unconfirmed": result["unknown_usage_reconciliation"]["effect_outcome_unconfirmed"]}


class CodingSourceIntent(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    execution_id: str = Field(pattern=r"^dex_[A-Za-z0-9]+$", max_length=128)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    manifest_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    size_bytes: int = Field(gt=0, le=DEFAULT_LIMITS.max_archive_bytes)


def source_call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except coding_source_intake.SourceIntakeError as error:
        raise HTTPException(error.status, detail={"code": error.code}) from error
    except PermissionError as error:
        raise HTTPException(403, detail={"code": "coding_source_forbidden"}) from error
    except KeyError as error:
        raise HTTPException(404, detail={"code": "coding_source_not_found"}) from error


@router.post("/ventures/{venture_id}/device-work-orders/{order_id}/source-intent")
def prepare_coding_source(venture_id: str, order_id: str, body: CodingSourceIntent, request: Request):
    caller = verify_caller(request, required=True)
    return source_call(coding_source_intake.prepare, venture_id, order_id, caller, **body.model_dump())


@router.put("/ventures/{venture_id}/device-work-orders/{order_id}/source-archive")
async def upload_coding_source(venture_id: str, order_id: str, request: Request):
    caller = verify_caller(request, required=True)
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/zip":
        raise HTTPException(415, detail={"code": "coding_source_content_type_invalid"})
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > DEFAULT_LIMITS.max_archive_bytes:
            raise HTTPException(413, detail={"code": "coding_source_archive_bounds"})
        chunks.append(chunk)
    return source_call(coding_source_intake.upload, venture_id, order_id, caller, b"".join(chunks))


@router.get("/ventures/{venture_id}/device-work-orders/{order_id}/source-archive")
def download_coding_source(venture_id: str, order_id: str, request: Request):
    caller = verify_caller(request, required=True)
    blob = source_call(coding_source_intake.download, venture_id, order_id, caller)
    return Response(blob, media_type="application/zip", headers={"Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff", "Content-Disposition": 'attachment; filename="your-project.zip"'})


@router.get("/ventures/{venture_id}/device-work-orders/{order_id}/source-receipt")
def coding_source_receipt(venture_id: str, order_id: str, request: Request):
    caller = verify_caller(request, required=True)
    return source_call(coding_source_intake.read, venture_id, order_id, caller)


@router.post("/ventures/{venture_id}/device-work-orders/{order_id}/source-preflight")
def run_coding_source_preflight(venture_id: str, order_id: str, request: Request):
    caller = verify_caller(request, required=True)
    return source_call(coding_source_preflight.run, venture_id, order_id, caller)


@router.get("/ventures/{venture_id}/device-work-orders/{order_id}/source-preflight")
def coding_source_preflight_report(venture_id: str, order_id: str, request: Request):
    caller = verify_caller(request, required=True)
    return source_call(coding_source_preflight.read, venture_id, order_id, caller)


class StaticQaAdmission(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    quote_id: str = Field(pattern=r"^ssq_[a-f0-9]{48}$")
    quote_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    approve_qa_ceiling_micro: int = Field(gt=0, le=1_000_000_000)


def static_qa_call(fn, *args, **kwargs):
    from app.builder.execution import BuildError
    from app.builder.model_proxy import BuildModelError
    try:
        return source_call(fn, *args, **kwargs)
    except InsufficientCredits as error:
        raise HTTPException(402, detail={"code": "insufficient_credits"}) from error
    except CreditError as error:
        if error.code == "local_spend_limit":
            raise HTTPException(402, detail={"code": error.code, "message": str(error)}) from error
        raise HTTPException(409, detail={"code": "subscription_static_qa_budget_unavailable"}) from error
    except (BuildError, BuildModelError) as error:
        raise HTTPException(409, detail={"code": str(error)}) from error
    except ValueError as error:
        code = str(error)
        if code not in {"subscription_static_app_ambiguous",
                "subscription_static_publication_baseline_ambiguous",
                "subscription_static_publication_baseline_changed",
                "subscription_static_publication_baseline_unverified"}:
            code = "subscription_static_scope_unavailable"
        raise HTTPException(409, detail={"code": code}) from error


@router.post("/ventures/{venture_id}/device-work-orders/{order_id}/static-qa-quote")
def quote_subscription_static_qa(venture_id: str, order_id: str, request: Request):
    from app.build.subscription_static_admission import estimate
    caller = verify_caller(request, required=True)
    return static_qa_call(estimate, venture_id, order_id, caller)


@router.post("/ventures/{venture_id}/device-work-orders/{order_id}/static-qa")
def admit_subscription_static_qa(venture_id: str, order_id: str, body: StaticQaAdmission, request: Request):
    from app.build.subscription_static_admission import admit
    caller = verify_caller(request, required=True)
    return static_qa_call(admit, venture_id, order_id, caller, **body.model_dump())


class HumanPauseBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    pause_token: str = Field(pattern=r"^[a-f0-9]{32}$")
    pause_kind: str = Field(min_length=1, max_length=40)


class HumanResumeAck(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    pause_token: str = Field(pattern=r"^[a-f0-9]{32}$")
    request_id: str = Field(pattern=r"^nyr_[A-Za-z0-9]+$")
    execution_id: str = Field(pattern=r"^dex_[A-Za-z0-9]+$")


class HumanPauseClear(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    pause_token: str = Field(pattern=r"^[a-f0-9]{32}$")


@router.post("/ventures/{venture_id}/device-work-orders/{order_id}/human-pause")
def register_human_pause(venture_id: str, order_id: str, body: HumanPauseBody, request: Request):
    from app.build import device_resume
    return call(device_resume.register, venture_id, execution_caller(request), order_id, **body.model_dump())


@router.get("/ventures/{venture_id}/device-work-orders/{order_id}/human-resume")
def pending_human_resume(venture_id: str, order_id: str, request: Request):
    from app.build import device_resume
    return call(device_resume.pending, venture_id, execution_caller(request), order_id)


@router.post("/ventures/{venture_id}/device-work-orders/{order_id}/human-resume/ack")
def acknowledge_human_resume(venture_id: str, order_id: str, body: HumanResumeAck, request: Request):
    from app.build import device_resume
    return call(device_resume.acknowledge, venture_id, execution_caller(request), order_id, **body.model_dump())


@router.post("/ventures/{venture_id}/device-work-orders/{order_id}/human-pause/clear")
def clear_human_pause(venture_id: str, order_id: str, body: HumanPauseClear, request: Request):
    from app.build import device_resume
    return call(device_resume.clear, venture_id, execution_caller(request), order_id, **body.model_dump())
