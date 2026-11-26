"""Execute chat actions through the same service paths the UI uses (doc 83)."""

from __future__ import annotations

from app.ventures.current_idea import current_idea

from app.chat.conversations import pending_key

import asyncio
import json
import logging
import re
from typing import Any
from urllib.parse import urlsplit

from app.chat.actions import REGISTRY, get_action
from app.chat.blockers import with_explain
from app.chat.recovery import founder_failure, log_founder_exception
from app.core.local_mode import is_local
from app.db.models import Venture
from app.state import MEM, db
from app.tenancy import current_or_none


PRIVATE_WORK_ACTIONS = frozenset({
    "research.start", "research.update_and_rerun", "plan.generate", "plan.approve",
    "build.start", "build.app.create", "pagebuilder.build", "work.start", "outreach.draft",
    "gtm.find", "experiment.run_review", "experiment.run", "experiment.record",
    "experiment.page.configure", "experiment.channel.configure", "idea.confirm",
    "job.retry", "gtm.send", "outreach.send",
    "idea.pressure_test",
})
_IDEA_CONFIRMATION_BLOCKERS = frozenset({
    "idea_advance_scope_changed", "not_ready_to_confirm", "no_revision", "idea_review_conflict",
    "validation_pending", "clarification_in_progress", "validation_required",
})


def _local_settings_ready(name: str) -> bool:
    from app.core.local_secrets import get_secret, read_settings

    if name == "build_with_my_tool":
        from app.chat.product_build_request import local_product_provider

        scope = current_or_none()
        return bool(scope and local_product_provider("", scope.actor))

    if name in {"research", "find_customers"}:
        search = read_settings("search")["values"]
        provider = search.get("provider", "builtin")
        if provider == "builtin":
            return True
        if provider in {"tavily", "exa"}:
            return bool(get_secret("search", "api_key"))
        if provider == "firecrawl":
            return bool(search.get("firecrawl_url"))
        if provider != "openai":
            return False
    if name in {"research", "find_customers", "landing_pages", "content", "background_work", "run_for_me_experiments", "routines"}:
        from app.core.providers_local import LocalProviderError, load_local_config

        try:
            return load_local_config() is not None
        except LocalProviderError:
            return False
    if name in {"send_email", "read_replies"}:
        smtp = read_settings("smtp")["values"]
        required = ("host", "user", "postal_address") if name == "send_email" else ("imap_host", "user")
        return all(smtp.get(field) for field in required) and bool(get_secret("smtp", "password"))
    if name in {"connected_apps", "publish_pages"}:
        section = "composio" if name == "connected_apps" else "publishing"
        values = read_settings(section)["values"]
        if section == "composio":
            return bool(get_secret(section, "api_key"))
        if values.get("provider") == "s3":
            return bool(values.get("bucket") and get_secret(section, "access_key_id")
                        and get_secret(section, "secret_access_key"))
        return bool(values.get("project") and get_secret(section, "token"))
    return True


def local_capability_reply(name: str) -> dict[str, Any] | None:
    if not is_local():
        return None
    from app.core.local_mode import is_sample_mode

    if name == "send_email" and is_sample_mode():
        from app.chat.customers import _SEND_ERRORS

        return {"kind": "text", "body_md": _SEND_ERRORS["sample_mode"], "action_started": False,
                "actions": [{"label": "Keys and connections", "href": "/settings/local", "style": "primary"}], "citations": []}

    settings_label, settings_href = "Open Settings", "/settings/local"
    coding = name in {"build_with_my_tool", "landing_pages"}
    try:
        from app.core.local_capabilities import blocked_message, capability, enabled
        if name == "find_customers" and capability("research")["state"] == "off":
            return local_capability_reply("research")
        snapshot = capability(name)
        state = snapshot["state"]
        if state == "off":
            settings_href = "/settings/local/capabilities"
        elif state == "needs_setup" and coding:
            if name == "landing_pages" and snapshot["next_step"].startswith("In Settings > Capabilities,"):
                settings_href = "/settings/local/capabilities"
            else:
                settings_label, settings_href = "Connect your coding tool", "/settings/local#coding-tools"
        ready = state == "ready" if coding else enabled(name)
    except ImportError:
        # shortcut: settings-only readiness until the shared capability module is installed.
        def blocked_message(capability_name):
            setup = {"research": "choose your Search provider and save its required key",
                "find_customers": "choose your Search provider and save its required key",
                "send_email": "save your SMTP host, port, mailbox address, app password and postal address",
                "read_replies": "save your IMAP host, port, mailbox address and app password",
                "publish_pages": "save your Publishing provider, project and token or S3 bucket and access keys",
                "connected_apps": "save your Composio API key and app connections"}.get(
                    capability_name, "save your Model provider, chat model and key")
            if capability_name == "build_with_my_tool":
                return ("Open Settings > Keys and connections > Coding tools, connect Codex or Claude Code, "
                        "sign in with your own subscription, allow Charlie to use it, then retry your build.")
            return f"Open Settings > Keys and connections, {setup}, then retry your request."

        coding = False
        ready = _local_settings_ready(name)
    if ready and name == "find_customers":
        if blocked := local_capability_reply("research"):
            return blocked
    if ready and name in {"research", "find_customers"}:
        try:
            from app.research.local_providers import search_ready
        except ImportError:
            def search_ready():
                return _local_settings_ready("research")
        ready = search_ready()
    if ready:
        return None
    return {"kind": "text", "body_md": snapshot["next_step"] if coding else blocked_message(name), "action_started": False,
            "actions": [{"label": settings_label, "href": settings_href, "style": "primary", "kind": "recovery_link"}], "citations": []}


def _action_capability(action_name: str, payload: dict) -> str | None:
    if action_name in {"research.start", "research.prepare", "research.update_and_rerun", "openworker_search", "agent_reach",
                       "experiment.exploration.next", "experiment.exploration.prepare", "experiment.exploration.openworld", "experiment.exploration.next_round"}:
        return "research"
    if action_name in {"gtm.find", "outreach.targets"}:
        return "find_customers"
    if action_name in {"pagebuilder.build", "site.rewrite", "refine_site", "experiment.page.prepare", "experiment.page.configure"}:
        return "landing_pages"
    if action_name == "site.publish":
        return "publish_pages"
    if action_name in {"gtm.send", "outreach.send", "gtm.resume_sending", "autopilot.allow_send",
                       "send_named", "send_draft", "send", "allow", "auto_on"}:
        return "send_email"
    if action_name in {"outreach.draft", "gtm.draft_rewrite"}:
        return "content"
    if action_name in {"social_connect", "social_post"}:
        return "connected_apps"
    if action_name in {"routines.create", "routines.approve"}:
        return "routines"
    if action_name in {"autopilot.god_on", "gtm.automate", "gtm.auto_first"}:
        return "background_work"
    if action_name == "gtm.auto_search" and isinstance(payload.get("auto_search"), dict) and payload["auto_search"].get("enabled"):
        return "background_work"
    if action_name in {"experiment.run", "experiment.prepare"}:
        return "research"
    if action_name in {"experiment.beliefs.prepare", "experiment.beliefs.approve"}:
        return "run_for_me_experiments"
    return None


async def start_requested_work(venture_id, reply, actor_subject=None, *, actions=PRIVATE_WORK_ACTIONS):
    from app.chat.confirmations import start_requested_work as start

    if reply.get("kind") == "idea_recovery":
        return reply
    return await start(venture_id, reply, actor_subject, actions=actions)


async def execute_action(
    venture_id: str,
    action_name: str,
    *,
    payload: dict[str, Any] | None = None,
    actor_subject: str | None = None,
    request_nonce: str | None = None,
    founder_initiated: bool = False,
    confirmation_message_id: str | None = None,
    standing_grant_id: str | None = None,
) -> dict[str, Any]:
    """Run a registered action and record only closed invocation metadata."""
    from app.analytics.chat_usage import record_chat_action
    from app.chat.intent import _EXP_RUN_RE
    from app.chat.store import CLIENT_REQUEST_ID
    token = CLIENT_REQUEST_ID.set(request_nonce) if request_nonce else None

    try:
        if payload and "_goal_step" in payload:
            result = await _execute_confirmed_action(venture_id, action_name, payload, actor_subject,
                **({"standing_grant_id": standing_grant_id} if standing_grant_id else {}))
        else:
            result = await _execute_action(venture_id, action_name, payload=payload,
                                       actor_subject=actor_subject, **({"founder_initiated": True} if founder_initiated else {}),
                                       **({"confirmation_message_id": confirmation_message_id} if confirmation_message_id else {}),
                                       **({"standing_grant_id": standing_grant_id} if standing_grant_id else {}))
        from app.core.local_mode import is_local

        context = (payload or {}).get("search_context")
        if not is_local() and action_name in {"openworker_search", "contacts.preview"} and isinstance(context, str) and context.strip():
            from core.copy import no_dashes
            result = {**result, "body_md": no_dashes(context[:2000]) + "\n\n" + result["body_md"]}

        preparing = {
            "plan.generate": {"plan.generate"}, "research.prepare": {"research.start"},
            "plan.resources_none": {"plan.generate"},
            "plan.approve": {"plan.approve"},
            "experiment.recommendation.prepare": {"experiment.run", "experiment.run_review", "experiment.page.configure", "experiment.channel.configure"},
            "experiment.prepare": {"experiment.run"}, "experiment.start": {"experiment.run", "experiment.run_review"},
            "experiment.page.prepare": {"experiment.run", "experiment.page.configure"},
            "experiment.channel.prepare": {"experiment.channel.configure"},
            "outreach.draft": {"outreach.draft"}, "pagebuilder.build": {"pagebuilder.build"},
            "build.prepare": {"build.start", "build.app.create", "pagebuilder.build"},
            "gtm.find": {"gtm.find"},
            "job.retry": {"job.retry", "research.start", "plan.generate"},
            "ceo_review": {"ceo_review"}, "idea.pressure_test.prepare": {"idea.pressure_test"},
        }
        if (action_name in preparing and (action_name != "job.retry" or is_local())
                and result.get("kind") not in {"action_failed", "error"} and "run" not in (payload or {})
                and (action_name != "gtm.find" or (payload or {}).get("prepare") is True)
                and not ({"ceiling_micro", "proposal_id"} & (payload or {}).keys())):
            result = await start_requested_work(venture_id, result, actor_subject,
                                                actions=preparing[action_name])
        if (action_name == "experiment.recommendation.prepare" and (payload or {}).get("run") is not False) or (
                action_name == "experiment.run_review" and (payload or {}).get("run") is True) or (
                action_name == "experiment.start" and _EXP_RUN_RE.match(str((payload or {}).get("text") or ""))):
            result = await start_requested_work(venture_id, result, actor_subject,
                                                actions={"gtm.find", "outreach.draft"})
        from app.chat.milestone_hooks import started_reply
        if action_name != "job.retry":  # a retry keeps the job id, and its reply must still post
            result = started_reply(result)
    except Exception as exc:
        log_founder_exception(exc, venture_id, action_name if isinstance(action_name, str) and action_name in REGISTRY else "chat.execute_action")
        await asyncio.to_thread(record_chat_action, venture_id, action_name, "raised")
        raise
    finally:
        if token is not None:
            CLIENT_REQUEST_ID.reset(token)
    await asyncio.to_thread(record_chat_action, venture_id, action_name, "returned")
    return result


def _approve_standing_card(venture_id, action_name, payload, actor_subject, grant_id, *, message_id, session=None):
    from app.approvals import standing
    from app.approvals.service import ApprovalError
    from app.tenancy import current

    tenant = current()
    candidate = standing.card_action(action_name, payload)
    if session is not None and "_goal_step" in payload:
        from app.db.task_models import Task
        from app.tasks.routines import reviewed_card_action
        task = session.get(Task, payload["_goal_step"]["task_id"])
        if task and json.loads(task.scope_json).get("routine_id"):
            candidate = reviewed_card_action(session, venture_id, actor_subject, action_name, payload)
            if candidate is None:
                raise ApprovalError("standing_approval_unavailable", "Your routine's approval or schedule changed. Review this action again.")
    decision = standing.auto_approve(workspace_id=tenant.workspace_id, venture_id=venture_id,
        founder_id=actor_subject, action=candidate,
        kind="workflow_write", subject_ref=message_id, summary="Your saved chat action",
        requested_by="standing_policy", grant_id=grant_id, session=session)
    if decision is None:
        raise ApprovalError("standing_approval_unavailable", "Your standing approval changed. Please approve this action again.")


async def _execute_confirmed_action(venture_id, action_name, payload, actor_subject, *, standing_grant_id=None):
    """Accept only the saved exact review, under its original task-step custody."""
    from app.authz.workspace import subject_person_id
    from app.chat import confirmations
    from app.db.models import Job
    from app.tasks import goals, service, usage
    from app.tasks.locks import lock_step
    from app.tenancy import bind

    if not isinstance(payload.get("_goal_step"), dict):
        raise ValueError("Goal planning is not enabled.")
    binding = payload["_goal_step"]
    if set(binding) != {"task_id", "run_id", "step_id"} or any(not isinstance(value, str) for value in binding.values()):
        raise ValueError("Your task confirmation changed. Review the saved step.")
    if blocked := local_capability_reply("background_work"):
        return blocked
    if not is_local() and not goals.enabled():
        raise ValueError("Goal planning is not enabled.")
    with db()() as session:
        task, run, step = lock_step(session, binding["step_id"])
        if (not task or (task.id, run.id, step.id) != (binding["task_id"], binding["run_id"], binding["step_id"])
                or task.venture_id != venture_id or subject_person_id(actor_subject) != task.owner_person_id
                or not service.active_scope(session, task) or not json.loads(task.scope_json).get("_goals_v1")
                or (task.state, run.state, step.state) != ("approval_required",) * 3
                or step.skill != action_name or not goals.is_action(action_name)):
            raise PermissionError("task_confirmation_scope_mismatch")
        stamp = json.loads(step.inputs_json)
        approval = stamp.get("_goal_approval", {})
        if approval.get("action") != action_name or approval.get("payload") != payload:
            raise ValueError("Your task confirmation changed. Review the saved step.")
        message = confirmations.card(venture_id, approval["message_id"], action_name, payload)
        cards = [item["confirmation"] for item in message["actions"] if isinstance(item.get("confirmation"), dict)
                 and item["confirmation"].get("action") == action_name
                 and item["confirmation"].get("resulting_state") == payload]
        if len(cards) != 1 or cards[0].get("state") != "starting":
            raise ValueError("Accept the exact confirmation in your private chat first.")
        resolved = goals.resolve_inputs(session, task, run, step)
        if any(stamp.get(key) != value for key, value in resolved.items()):
            raise ValueError("Your earlier result changed. Review this step again.")
        clean = {key: value for key, value in payload.items() if key != "_goal_step"}
        if action_name == "experiment.run":
            goals.scan_spec(venture_id, clean.get("experiment_id"))
            if clean != resolved:
                raise ValueError("Your experiment review changed. Review this step again.")
        if standing_grant_id:
            _approve_standing_card(venture_id, action_name, payload, actor_subject, standing_grant_id,
                                   message_id=approval["message_id"], session=session)
        task.state = run.state = "working"
        step.state = "admitting"
        if not service.guard(session, task, run, cost_micro=clean.get("ceiling_micro", 0)):
            service.block(session, task, {"class": "needs_input", "reason": "Your goal reached its deadline or agreed work limit.",
                "unblock_action": "Review your saved progress and choose a new goal."}, run=run, step=step)
            session.commit()
            raise ValueError("Your goal reached its deadline or agreed work limit.")
        goals.record_attempt(session, task, run, step, action_name, clean)
        task.blocker_json = None
        run_id, step_id, wid, actor = run.id, step.id, task.workspace_id, task.owner_person_id
        session.commit()
    try:
        with bind(workspace_id=wid, venture_id=venture_id, actor=actor), service.step_context(run_id, step_id), usage.run_context(run_id, step_id=step_id):
            reply = await _execute_action(venture_id, action_name, payload=clean, actor_subject=actor_subject)
            with db()() as session:
                task, run, step = lock_step(session, step_id)
                job_id = reply.get("job_id") or reply.get("run_id")
                job = session.get(Job, job_id) if job_id else None
                if job:
                    service.attach_job(session, job)
                if step.job_id:
                    if task.state not in service.TERMINAL | service.WAITING:
                        step.state = "working"
                    session.commit()
                    service.after_job(session, step.job_id)
                elif action_name == "outreach.draft" and reply.get("kind") not in {"needs_you", "confirmation"} and reply.get("action_started") is not False:
                    from app.autopilot import outreach
                    draft = next((item for item in outreach.drafts(venture_id, "draft", "manual")
                                  if item.get("contact_id") == resolved["contact_id"]), None)
                    if not draft:
                        raise ValueError("Your invitation draft has no saved receipt.")
                    saved = {"draft_id": draft["doc_id"]}
                    service.event(session, task, "goal_receipt", "Your draft is saved for review. Nothing was sent.",
                        run_id=run.id, step_id=step.id, data=saved)
                    goals.finish(session, task, run, step, goals.verify_sync(session, task, step, saved))
                else:
                    service.block(session, task, {"class": "needs_input", "reason": "Your action did not produce a saved result for this step.",
                        "unblock_action": "Review your saved progress before continuing."}, run=run, step=step)
                session.commit()
        return reply
    except Exception as exc:
        log_founder_exception(exc, venture_id, action_name if isinstance(action_name, str) and action_name in REGISTRY else "chat.confirmed_action")
        with db()() as session:
            task, run, step = lock_step(session, step_id)
            service.block(session, task, {"class": "needs_input", "reason": "Your approved step was interrupted before its result was verified.",
                "unblock_action": "Review its saved progress. Do not repeat the action without checking its result."}, run=run, step=step)
            session.commit()
        raise


def _active_product_build_reply(venture_id: str, workspace_id: str, text: str, actor_subject: str | None) -> dict | None:
    from app.admin.authn import CallerContext
    from app.authz.workspace import subject_person_id
    from app.build import device_orders
    from app.builder.access import authorize
    from app.chat.product_build_request import is_product_build_request
    from app.core.local_owner import current_owner
    from app.secrets.detect import looks_like_secret
    from core.clock import utcnow

    if not is_product_build_request(text) or looks_like_secret(text):
        return None
    actor = current_owner().id
    if subject_person_id(actor_subject) != actor:
        raise PermissionError("local_owner_required")
    caller = CallerContext(sub=actor, email="", role="user", path="", method="GET", exp=0, nonce="")
    with db()() as session:
        for order in device_orders._active_orders(session, venture_id, f"product_build_{venture_id}", utcnow()):
            if ((order.get("workspace_id"), order.get("actor_id")) != (workspace_id, actor)
                    or order.get("state") not in {"approved", "claimed", "waiting_for_device"}
                    or order.get("device_id") != device_orders.device_caller(caller).device_id):
                continue
            authorize(session, workspace_id=workspace_id, venture_id=venture_id, actor=actor)
            return {"kind": "navigation", "body_md": "Your build is already planned. Choose Watch your tool to check its progress or stop it.",
                    "action_started": False, "citations": [], "actions": [{"label": "Watch your tool",
                        "href": f"/v/{venture_id}/device-work-orders/{order['id']}", "style": "primary"}]}
    return None


def _start_tool_build(venture_id: str, payload: dict, actor_subject: str | None) -> dict:
    from fastapi import HTTPException
    from app.admin.authn import CallerContext
    from app.authz.workspace import subject_person_id
    from app.build import device_orders
    from app.chat.product_build_request import is_product_build_request, local_product_provider
    from app.chat.store import CLIENT_REQUEST_ID
    from app.core.local_owner import current_owner
    from app.secrets.detect import looks_like_secret
    from app.chat.build_moves import is_repair_request

    def blocked(message):
        return {"kind": "text", "body_md": message, "action_started": False, "citations": [],
                "actions": [{"label": "Open Coding tools", "href": "/settings/local#coding-tools", "style": "primary"}]}

    def open_existing_build(review_path: str) -> dict:
        return {"kind": "navigation", "body_md": "You already have a build on this computer. Open it to watch progress. "
                "To start a new build, choose Stop build or Check Stop there, then ask me again.",
                "action_started": False, "citations": [], "actions": [{"label": "Open existing build",
                    "href": review_path, "style": "primary"}]}

    text = payload.get("text")
    repair = payload.get("repair") is True and is_repair_request(text)
    if not is_local() or not (is_product_build_request(text) or repair):
        return blocked("In your desktop app's venture chat, explicitly ask me to build your app or make the code change you describe. Nothing was started.")
    if looks_like_secret(text):
        return blocked("Remove credentials from your build request. Save keys in Settings > Keys and connections, then resend your request without secrets.")
    owner = current_owner()
    if subject_person_id(actor_subject) != owner.id:
        raise PermissionError("local_owner_required")
    try:
        provider = local_product_provider(text, actor_subject)
        if provider is None:
            return blocked("Open Settings > Keys and connections > Coding tools, connect your selected coding tool, sign in with your own subscription, allow Charlie to use it, then retry your build.")
        caller = CallerContext(sub=owner.id, email="", role="user", path="", method="POST", exp=0,
                               nonce=CLIENT_REQUEST_ID.get() or "")
        from app.builder.ctl import request_local_build
        order = request_local_build(venture_id, text, caller=caller, provider=provider,
                                    repair=repair, plan_task_id=payload.get("plan_task_id"))
        if order["state"] == "cancelled" or order.get("stop_reconciliation_required"):
            return open_existing_build(order["review_path"])
        if order["state"] == "pending":
            order = device_orders.transition(venture_id, caller, order["id"], order["review_digest"], "approve")
    except device_orders.ActiveDeviceOrder as exc:
        return open_existing_build(f"/v/{exc.order['venture_id']}/device-work-orders/{exc.order['id']}")
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        if (exc.status_code == 409 and detail.get("code") == "device_task_has_active_order"
                and isinstance(review_path := detail.get("review_path"), str) and review_path):
            return open_existing_build(review_path)
        log_founder_exception(exc, venture_id, "build.tool.start")
        message = detail.get("message")
        if detail.get("code") == "build_tool_choice_required":
            return {"kind": "needs_you", "body_md": message, "action_started": False, "actions": [], "citations": []}
        return blocked(message or "Open Settings > Keys and connections > Coding tools, check your selected tool's connection and permission, then retry your build.")
    except (ValueError, OSError, PermissionError) as exc:
        log_founder_exception(exc, venture_id, "build.tool.start")
        if str(exc) == "device_product_scope_unsupported":
            return blocked(" ".join(getattr(exc, "reasons", [])) + " In your venture chat, explicitly request a local web build without these requirements, or keep your full requirements and use your coding tool directly. Nothing was narrowed or started.")
        steps = {
            "device_product_build_unavailable": "Open your venture home and confirm your idea, then retry your build.",
            "device_coding_loop_scope_invalid": "Open your saved product plan, revise the unsupported scope, then request the build again. Nothing was narrowed or deployed.",
            "device_coding_consent_required": "Open Settings > Keys and connections > Coding tools, choose Allow Charlie for your selected tool, then retry your build.",
            "device_coding_subscription_required": "Open Settings > Keys and connections > Coding tools, sign in to your selected tool with your own subscription, check its connection, then retry your build. API-key login cannot start this build.",
            "device_coding_request_contains_secret": "Remove credentials from your build request. Save keys in Settings > Keys and connections, then resend without secrets.",
            "device_coding_request_too_long": "In your venture chat, shorten your request to the code changes you want made, then send it again. Nothing was started.",
        }
        return blocked(steps.get(str(exc), "Open Settings > Keys and connections > Coding tools to reconnect your tool, then ask me to retry your build. No new tool was selected."))
    return {"kind": "build_tool_started", "body_md": "Your build uses your connected coding tool on your own login. Choose Watch your build below to watch progress, stop it, or open your result folder. Nothing is deployed or published.",
            "action_started": True, "citations": [],
            "actions": [{"label": "Watch your build", "href": f"/v/{venture_id}/device-work-orders/{order['id']}",
                "payload": {"order_id": order["id"]}, "style": "primary"}]}


async def _execute_action(
    venture_id: str,
    action_name: str,
    *,
    payload: dict[str, Any] | None = None,
    actor_subject: str | None = None,
    founder_initiated: bool = False,
    confirmation_message_id: str | None = None,
    standing_grant_id: str | None = None,
) -> dict[str, Any]:
    """Run a registered chat action. Returns founder-facing body_md + optional links."""
    payload = payload or {}
    try:
        action = get_action(action_name)
    except ValueError:
        from app.analytics.error_events import log_error_event

        await asyncio.to_thread(log_error_event, "chat", feature="chat", code="unhandled_chat_action", venture_id=venture_id,
                        status=400, level=logging.ERROR)
        raise
    from app.chat.actions import beta_blocked

    local_page_action = is_local() and action_name in {"site.rewrite", "pagebuilder.build"}
    blocked = None if local_page_action or is_local() and action_name in {"connections.status", "build.tool.start"} else (
        await asyncio.to_thread(beta_blocked, action_name, venture_id) if is_local() or action_name == "autopilot.god_on"
        else beta_blocked(action_name, venture_id))
    if blocked is not None:
        if is_local() and (capability_name := _action_capability(action_name, payload)):
            setup = await asyncio.to_thread(local_capability_reply, capability_name)
            if setup and setup["body_md"] == blocked["body_md"]:
                return setup
        return blocked

    if action_name.startswith("routines."):
        if action_name in {"routines.create", "routines.approve"} and (blocked := local_capability_reply("routines")):
            return blocked
        if action_name == "routines.approve":
            if not confirmation_message_id:
                raise ValueError("chat_confirmation_required")
            from app.chat.confirmations import card
            await asyncio.to_thread(card, venture_id, confirmation_message_id, action_name, payload)
        from app.tasks.routines import action as routine_action
        return await routine_action(venture_id, action_name, payload)
    from app.ventures import journey_overrides
    if action_name in journey_overrides.ACTIONS:
        if "proposal_id" not in payload:
            return await asyncio.to_thread(journey_overrides.prepare, venture_id, action_name, payload, actor_subject)
        if set(payload) != {"proposal_id"}:
            raise ValueError("journey_invalid_confirmation")
        result = await asyncio.to_thread(journey_overrides.apply, venture_id, action_name, payload["proposal_id"], actor_subject)
        if action_name == "journey.jump_to" and (await asyncio.to_thread(journey_overrides.read, venture_id))["target"] == "find_customers":
            # The founder chose to go straight to customers: continue into the search card, not a dead end.
            card = await find_customers_reply(venture_id)
            return {**card, "action_started": True, "body_md": result["body_md"] + "\n\n" + card["body_md"],
                    "actions": [*(card.get("actions") or []), *result["actions"]]}
        return result
    from app.authz.roles import role_allows
    from app.authz.workspace import assert_venture_access, subject_person_id
    from app.tenancy import current_or_none
    from app.workspaces.service import role_of

    scope = current_or_none()
    if actor_subject is None and scope and scope.actor != "system":
        actor_subject = scope.actor
    _, workspace_id = await asyncio.to_thread(assert_venture_access, venture_id, actor_subject)
    role = await asyncio.to_thread(role_of, workspace_id, subject_person_id(actor_subject))
    if not role_allows(role, action.required_permission):
        raise PermissionError(f"{action.required_permission}_required")
    if not local_page_action and (capability_name := _action_capability(action_name, payload)):
        if blocked := await asyncio.to_thread(local_capability_reply, capability_name):
            return blocked

    if standing_grant_id:
        _approve_standing_card(venture_id, action_name, payload, actor_subject, standing_grant_id,
                               message_id=confirmation_message_id)

    if action_name == "build.tool.start":
        from app.chat.guard import paused_reply
        from app.ventures.lifecycle import is_paused

        if await asyncio.to_thread(is_paused, venture_id):
            return paused_reply(venture_id)
        if is_local():
            from app.chat.product_build_request import named_coding_tools

            if existing := await asyncio.to_thread(_active_product_build_reply, venture_id, workspace_id, payload.get("text", ""), actor_subject):
                return existing
            text = payload.get("text")
            if not isinstance(text, str) or not named_coding_tools(text):
                if setup := await asyncio.to_thread(local_capability_reply, "build_with_my_tool"):
                    return setup
        return await asyncio.to_thread(_start_tool_build, venture_id, payload, actor_subject)

    if action_name == "approvals.revoke":
        from app.approvals.service import decide

        approval_id = payload.get("approval_id")
        if not isinstance(approval_id, str) or not approval_id:
            raise ValueError("standing_approval_required")
        await asyncio.to_thread(decide, approval_id, decision="withdrawn", decided_by=actor_subject)
        return {"kind": "text", "body_md": "Your standing approval is revoked. You will be asked before this work runs again.",
                "actions": [], "citations": []}

    if action_name in {"inbox.briefing.consent", "inbox.briefing.settings", "inbox.item.feedback"}:
        from app.inbox.briefing import action as inbox_action
        return await asyncio.to_thread(inbox_action, workspace_id, venture_id, action_name,
                                       payload, subject_person_id(actor_subject))

    if action_name == "venture.rename":
        from fastapi import HTTPException
        from pydantic import ValidationError
        from app.api.ventures import RenameVentureBody, rename_venture

        with db()() as session:
            old_name = session.get(Venture, venture_id).name
        try:
            body = RenameVentureBody(name=payload.get("name"))
            renamed = await asyncio.to_thread(rename_venture, venture_id, body, actor_subject)
        except (ValidationError, HTTPException) as exc:
            if isinstance(exc, HTTPException) and exc.status_code != 422:
                raise
            return {"kind": "text", "body_md": "Use 2 to 80 characters, not an untitled name or an internal ID, and do not end the name with an ellipsis. Your venture name was not changed.",
                    "actions": [], "citations": []}
        return {"kind": "venture_renamed", "body_md": f"Renamed {old_name} to {renamed['name']}.",
                "actions": [], "citations": []}

    if action_name in {"host.choose", "host.plan", "host.verify"}:
        from app.chat import hosting

        if action_name == "host.choose":
            return await asyncio.to_thread(hosting.choose, venture_id, payload)
        if action_name == "host.verify":
            return await asyncio.to_thread(hosting.verify, venture_id, payload.get("url"))
        return await asyncio.to_thread(hosting.plan, venture_id)

    if action_name in {"service.guide", "service.select", "product.record", "product.status"}:
        from app.chat import service_guides

        if action_name == "service.select":
            return await asyncio.to_thread(service_guides.select_service, venture_id, payload)
        if action_name == "product.record":
            return await asyncio.to_thread(service_guides.record_product, venture_id, payload)
        if action_name == "product.status":
            return await asyncio.to_thread(service_guides.status, venture_id)
        return service_guides.guide(venture_id, str(payload.get("provider") or ""),
                                    str(payload.get("category") or ""))

    if action_name == "site.unpublish":
        from app.chat.unpublish import execute
        return await asyncio.to_thread(execute, venture_id, payload, actor_subject)

    if is_local() and action_name in {"memory.list", "memory.remember", "memory.correct", "memory.forget"}:
        from app.chat.memory_commands import HELP, dispatch

        source_message_id = payload.get("source_message_id")
        if not isinstance(source_message_id, str) or not source_message_id:
            return {"kind": "text", "body_md": "No memory was changed. " + HELP, "actions": [], "citations": []}
        return await dispatch(venture_id, source_message_id, action_name=action_name)

    if action_name == "status":
        if failed := await asyncio.to_thread(_idea_review_failure, venture_id):
            return failed
        if "reasoning" in payload:
            from app.tasks import reasoning

            if is_local() or await asyncio.to_thread(reasoning.enabled):
                progress = payload["reasoning"]
                return await asyncio.to_thread(reasoning.progress,
                    progress.get("run_id") if isinstance(progress, dict) else None, venture_id)
        from app.chat.context import assemble
        from app.chat.handlers import status

        return await asyncio.to_thread(lambda: status.handle(assemble(venture_id), "status"))

    if action_name == "why":
        from app.chat.ground import answer_from_memory

        return await answer_from_memory(venture_id, str(payload.get("query") or "why"))

    if action_name == "money":
        from app.chat.context import assemble
        from app.chat.refuse import refuse_revenue

        return await asyncio.to_thread(lambda: refuse_revenue(assemble(venture_id)))

    if action_name == "leads":
        from app.chat.intent import _LEADS_RE
        query = str(payload.get("query") or "")
        return await asyncio.to_thread(_leads, venture_id) if _LEADS_RE.search(query) else await find_customers_reply(venture_id, query)

    if action_name == "research":
        job_id = payload.get("job_id")
        return await _research(venture_id, job_id if is_local() and isinstance(job_id, str) and 0 < len(job_id) <= 128 else None)

    if action_name in {"idea.pressure_test.prepare", "idea.pressure_test", "idea.pressure_test.accept"}:
        from app.validation import board
        if action_name == "idea.pressure_test.accept":
            return await asyncio.to_thread(board.accept, venture_id, payload)
        try:
            if action_name == "idea.pressure_test.prepare":
                return await asyncio.to_thread(board.prepare, venture_id)
            return await board.run(venture_id, payload)
        except Exception as exc:
            from app.credits.errors import InsufficientCredits
            from llm_gateway.hosted import HostedCreditsRequired
            from app.chat.recovery import button
            reply = founder_failure(exc, venture_id, "your independent idea review", {"action": "idea.pressure_test.prepare", "payload": {}})
            if isinstance(exc, (InsufficientCredits, HostedCreditsRequired)):
                return reply
            reply["actions"].append(button("idea.board"))
            return with_explain(reply, "idea_review_failed")
    if action_name == "research.competitors":
        from app.chat.milestone_hooks import research_report_link
        from app.research.categorize import competitor_grid_markdown
        from app.research.service import get_dossier, list_dossiers
        if research_needs_start(venture_id):
            from app.chat.intent import _ANSWER_ONLY
            from app.chat.link_context import read_prohibited
            from app.chat.rephrase import INPUT

            original = str((INPUT.get() or {}).get("original") or "")
            if is_local() and not _ANSWER_ONLY.search(original) and not read_prohibited(original):
                return await execute_action(venture_id, "research.start", actor_subject=actor_subject)
            from app.chat.build_path import research_card
            return await asyncio.to_thread(research_card, venture_id)
        rows = await asyncio.to_thread(list_dossiers, venture_id)
        for row in rows:
            if row["state"] not in {"complete", "partial"}:
                continue
            try:
                report = await asyncio.to_thread(get_dossier, row["id"])
            except KeyError:
                continue
            if report["state"] not in {"complete", "partial"}:
                continue
            grid = (report["summary"].get("founder_report") or {}).get("competitor_grid") or {}
            if grid.get("rows"):
                return {"kind": "text", "body_md": competitor_grid_markdown(grid),
                        "actions": [research_report_link(venture_id, report["id"])], "citations": []}
        return {"kind": "text", "body_md": "Your current idea has no saved competitor comparison from verified pages. Choose Run research again to look for current competitors.",
                "actions": [{"label": "Run research again", "action": "research.start"}], "citations": []}

    if action_name == "research.start":
        return await asyncio.to_thread(_research_start, venture_id, actor_subject, expected_revision_id=payload.get("expected_revision_id"))
    if action_name == "research.prepare":
        from app.chat.build_path import research_card
        return await asyncio.to_thread(research_card, venture_id)
    if action_name == "startup.review":
        from app.chat.build_path import startup_review_reply
        return await asyncio.to_thread(startup_review_reply, venture_id)
    if action_name == "research.update_and_rerun":
        from app.chat.research_revision import confirm
        return await asyncio.to_thread(confirm, venture_id, actor_subject, payload)

    if action_name == "idea.page_refresh":
        from app.ventures.idea_page import enqueue_failure_reply, request_refresh

        started = await asyncio.to_thread(request_refresh, venture_id, actor=actor_subject, requested=True)
        if not started:
            return {**enqueue_failure_reply(), "action_started": False}
        return {"kind": "idea_page_refresh_pending",
                "body_md": "Your Idea page will update from your saved answers. Open Idea in a few minutes to see the result.",
                "actions": [{"label": "Open Idea", "href": f"/v/{venture_id}/idea"}],
                "citations": [], "action_started": True}

    if action_name == "idea.review":
        return await asyncio.to_thread(_review_idea, venture_id)

    if action_name == "idea.confirm":
        reply = await asyncio.to_thread(_confirm_idea, venture_id, payload, actor_subject)
        request = reply.pop("experiment_request", None)
        if request:
            return await execute_action(venture_id, request["action"], payload=request["payload"], actor_subject=actor_subject)
        return reply

    if action_name == "pagebuilder.build":
        return await asyncio.to_thread(_build_landing_page, venture_id, payload, actor_subject)

    if action_name == "build.prepare":
        from app.chat.router import _product_build_reply

        return await asyncio.to_thread(lambda: _product_build_reply(
            venture_id, _brief_or_empty(venture_id, force=True), actor_subject,
            hint=str(payload.get("hint") or ""), task_id=str(payload.get("plan_task_id") or "") or None))

    if action_name == "outreach.draft" and payload.get("run") is True and (payload.get("batch") or payload.get("experiment_id")) and not any(
            key in payload for key in ("contact_id", "email_revision", "review_only")):
        from app.experiments import recruitment
        experiment = await asyncio.to_thread(recruitment.selected, venture_id, payload.get("experiment_id"))
        if experiment:
            return await recruitment.draft(venture_id, experiment, str(payload.get("recruitment_request") or "")[:1500])
        if payload.get("experiment_id"):
            return {"kind": "text", "body_md": "You cannot draft invitations for that test here. Review your current experiments and choose a test to prepare. Nothing was sent.",
                "actions": [{"label": "Review your experiments", "action": "experiment.list", "payload": {}}], "citations": []}

    if action_name == "outreach.draft" and "contact_id" in payload:
        contact_id = payload["contact_id"]
        if not isinstance(contact_id, str) or not contact_id or len(contact_id) > 100:
            raise ValueError("gtm_invalid_input")
        payload = {**payload, "review_only": True}
    if action_name == "outreach.draft" and payload.get("run") is True and payload.get("review_only") is True:
        from app.gtm.journey import draft_for_review

        return await asyncio.to_thread(draft_for_review, venture_id, **({"contact_id": payload["contact_id"]} if "contact_id" in payload else {}))
    if action_name == "outreach.draft" and payload.get("review_only") is True:
        from app.chat.offers import draft_prepare
        return await asyncio.to_thread(draft_prepare, venture_id, review_only=True, **({"contact_id": payload["contact_id"]} if "contact_id" in payload else {}))

    if action_name == "outreach.delivery_resolve":
        from app.autopilot.outreach import resolve_delivery

        return await asyncio.to_thread(resolve_delivery, venture_id, payload, actor_subject=actor_subject)

    if action_name in {"outreach.targets", "outreach.send"} or (action_name == "outreach.draft" and payload.get("batch")):
        from app.autopilot import outreach

        if action_name == "outreach.draft" and payload.get("run") is True:
            request = str(payload.get("recruitment_request") or "")[:4000]
            batch = await outreach.draft_batch(venture_id, request=request, review_only=True)
            if batch is not None:
                return batch
            from app.chat import offers

            return await offers.draft_run(venture_id, request=request)
        return await outreach.run(venture_id, action_name, payload, actor_subject=actor_subject)

    if action_name == "outreach.drafts":
        return await asyncio.to_thread(_saved_outreach_drafts, venture_id)

    if action_name == "outreach.draft":
        from app.chat import offers

        if "email_revision" in payload:
            from app.chat.email_revision import run, prepare, refusal
            revision = payload["email_revision"]
            if payload.get("run") is True:
                return await run(venture_id, revision)
            if isinstance(revision, dict) and isinstance(revision.get("request"), str):
                return await asyncio.to_thread(prepare, venture_id, revision["request"])
            return refusal()
        request = str(payload.get("recruitment_request") or "")[:4000]
        # shortcut: direct English saved-recipient requests only, extend the shared intent parser for other phrasings.
        if is_local() and payload.get("run") is True and re.search(
                r"\b(?:emails|(?:email\s+)?invitations)\s+(?:to|for)\s+(?:my|our|the)\s+(?:saved|existing)\s+(?:customer\s+)?(?:contacts?|customers?|prospects?|people|interviews?)\b",
                request, re.I):
            from app.gtm.journey import draft_for_review

            return await asyncio.to_thread(draft_for_review, venture_id, founder_request=request)
        return await offers.draft_run(venture_id, request=request) if payload.get("run") else await asyncio.to_thread(offers.draft_prepare, venture_id, request=request)

    if action_name in {"gtm.find", "gtm.status", "gtm.eligibility", "gtm.approve", "gtm.draft_edit", "gtm.draft_rewrite", "gtm.send", "gtm.auto_first", "gtm.accept_terms", "gtm.automate", "gtm.auto_search",
                       "gtm.library_claim", "gtm.library_more", "gtm.library_notes"}:
        from app.chat import customers
        if payload.get("prepare") is True and action_name == "gtm.find":
            note = payload.get("note")
            if note is not None and (not isinstance(note, str) or len(note) > 2000):
                raise ValueError("gtm_invalid_input")
            if is_local() and not note:
                return await customers.find_reply(venture_id, explicit=True)
            return await find_customers_reply(venture_id, note or "", start_requested=False)
        return await customers.execute(venture_id, action_name, payload, subject_person_id(actor_subject))

    if action_name == "gtm.resume_sending":
        from app.gtm import journey
        from app.gtm.relationships.service import OutreachError
        try:
            result = await asyncio.to_thread(journey.resume_sending, workspace_id, venture_id,
                reviewed_bounced_addresses=payload.get("reviewed_bounced_addresses"), actor=actor_subject)
        except OutreachError as error:
            from app.chat.customers import terms_reply
            reply = terms_reply(getattr(error, "code", "") or str(error), "Nothing was sent. ")
            return reply or {"kind": "needs_you", "body_md": "Resuming was blocked by a safety check. Nothing was sent.",
                "actions": [{"label": "Show customers and drafts", "action": "gtm.status"}], "citations": []}
        return {"kind": "text", "body_md": result["receipt"]["body_md"], "actions": [], "citations": []}

    if action_name == "needs_you.resolve":
        from app.needs_you import service as ny

        from app.build.device_resume import ResumeUnavailable
        try:
            done = await asyncio.to_thread(ny.resolve, str(payload.get("request_id") or ""), venture_id=venture_id, actor=actor_subject)
        except ResumeUnavailable:
            return {"kind": "text", "citations": [], "actions": [], "body_md":
                "This request has not resumed execution. Continue in the active Mac task, or review its recovery status before retrying."}
        message = "Thanks, your update is saved. I will recheck the task before continuing." if done else "That one is already handled."
        if done and done.get("resume_state") == "queued":
            message = "Your Continue request is queued for the same active Mac task. Supymem must recheck the blocker before continuing."
        return {"kind": "text", "citations": [], "actions": [], "body_md": message}

    if action_name.startswith("recovery."):
        return await asyncio.to_thread(_recovery_action, venture_id, action_name, payload, actor_subject)

    if action_name in {"idea.proposal.approve", "idea.proposal.edit"}:
        from app.chat import idea_proposal

        handler = idea_proposal.approve if action_name == "idea.proposal.approve" else idea_proposal.edit
        return await asyncio.to_thread(handler, venture_id, payload)

    if action_name == "job.retry":
        from app.chat import failed_jobs

        if is_local() and isinstance(payload.get("job_id"), str):
            from sqlalchemy import select
            from app.db.models import Job

            workspace_id = _workspace_id(venture_id)
            with db()() as session:
                failed_page = session.scalar(select(Job.id).where(
                    Job.id == payload["job_id"], Job.kind == "pagebuilder.build",
                    Job.venture_id == venture_id, Job.workspace_id == workspace_id,
                    Job.status.in_(failed_jobs.FAILED)))
            if failed_page:
                return await execute_action(venture_id, "pagebuilder.build", payload={"retry_job_id": failed_page},
                    actor_subject=actor_subject)

        if is_local() and payload.get("job_id") and (failed := _idea_review_failure(venture_id)):
            if failed["actions"][0]["payload"]["job_id"] == payload["job_id"]:
                from app.core.jobs import ACTIVE_JOB_STATUSES, publish_wake
                from app.db.models import Job

                with db()() as session:
                    job = session.get(Job, payload["job_id"])
                    waiting = job is not None and job.status in ACTIVE_JOB_STATUSES
                if waiting:
                    publish_wake()
                    return failed
        return await asyncio.to_thread(lambda: failed_jobs.run(
            venture_id, payload, _workspace_id(venture_id), allow_uncertain=founder_initiated))

    if action_name == "jobs.status":
        from app.chat import failed_jobs

        return await asyncio.to_thread(lambda: failed_jobs.status(
            venture_id, _workspace_id(venture_id), str(payload.get("state") or "running")))

    if action_name == "build.start" and payload.get("create_app") and not payload.get("app_id"):
        return await asyncio.to_thread(_build_with_new_app, venture_id, payload, actor_subject)

    if action_name == "build.start":
        return await asyncio.to_thread(_start_build, venture_id, payload, actor_subject)

    if action_name.startswith("plan."):
        from app.chat import plan_chain

        if action_name == "plan.resources_none":
            return await asyncio.to_thread(plan_chain.resources_none, venture_id)
        if action_name == "plan.quote":
            return await asyncio.to_thread(plan_chain.quote_reply, venture_id)
        if action_name == "plan.generate":
            return await asyncio.to_thread(plan_chain.generate_run, venture_id, payload) if "ceiling_micro" in payload \
                else await asyncio.to_thread(plan_chain.generate_prepare, venture_id, bool(payload.get("regenerate"))
                                             or plan_chain.asks_regenerate(str(payload.get("query") or "")))
        if action_name == "plan.approve":
            return await asyncio.to_thread(plan_chain.approve_run, venture_id, actor_subject, payload)

    if action_name == "work.start":
        return await asyncio.to_thread(_start_work, venture_id, payload, actor_subject)

    if action_name == "build.app.create":
        return await asyncio.to_thread(_create_build_app, venture_id, payload, actor_subject)

    if action_name == "agents":
        return await asyncio.to_thread(_agents, venture_id)

    if action_name in {"whats_next", "next_moves"}:
        return await asyncio.to_thread(_whats_next, venture_id)

    if action_name in {"contacts.preview", "contacts.import", "website.review"}:
        from app.chat import assets_in_chat

        if action_name == "contacts.preview":
            return await assets_in_chat.preview(venture_id, payload, actor_subject)
        if action_name == "contacts.import":
            return await asyncio.to_thread(assets_in_chat.commit, venture_id, payload, actor_subject)
        return await assets_in_chat.review_site(venture_id, payload)

    if action_name.startswith(("site.", "waitlist.")):
        from app.chat.site_actions import execute

        return await execute(venture_id, action_name, payload, actor_subject)

    if action_name == "refine_site":
        return await _refine_site(venture_id)

    if action_name == "openworker_search":
        return await _openworker_search(str(payload.get("query") or ""), venture_id=venture_id,
                                        public_only=bool(payload.get("search_context")))

    if action_name == "agent_reach":
        return await _openworker_search(str(payload.get("query") or ""), venture_id=venture_id)

    if action_name == "pause":
        if payload.get("stop_everything") is True:
            return await _pause(venture_id)
        return await asyncio.to_thread(_pause_work, venture_id)

    if action_name == "resume":
        return await _resume(venture_id)

    if action_name == "autopilot.allow_send":
        from app.needs_you.readiness import allow_send

        return await asyncio.to_thread(allow_send, venture_id)

    if action_name == "autopilot.send_off":
        from app.chat.send_control import stop_sending

        return await asyncio.to_thread(stop_sending, venture_id)

    if action_name in {"autopilot.set_cap", "autopilot.send_cap", "build.accept", "credits.request"}:
        from app.chat import jarvis

        if action_name == "autopilot.set_cap":
            return await asyncio.to_thread(jarvis.execute_daily_cap, venture_id, payload)
        if action_name == "autopilot.send_cap":
            return await asyncio.to_thread(jarvis.execute_send_cap, venture_id, payload, actor_subject, confirmation_message_id)
        if action_name == "build.accept" and not payload.get("report_digest"):  # a plain Accept button shows the review card first
            return await asyncio.to_thread(jarvis.build_accept, venture_id, None)
        if action_name == "build.accept":
            return await asyncio.to_thread(jarvis.execute_build_accept, venture_id, payload, actor_subject)
        return await asyncio.to_thread(jarvis.execute_credits_request, venture_id, actor_subject)

    if action_name == "god_mode.request":
        if is_local():
            return await _execute_action(venture_id, "autopilot.god_on", actor_subject=actor_subject, founder_initiated=True)
        from app.autopilot import god_requests

        return await asyncio.to_thread(god_requests.request_reply, venture_id, actor_subject)

    if action_name in {"autopilot.god_on", "autopilot.god_off"}:
        return await asyncio.to_thread(_god_mode, venture_id, action_name == "autopilot.god_on")

    if action_name == "venture.start_new":
        from app.chat import new_venture

        return await new_venture.execute(venture_id, payload, actor_subject)

    if action_name == "cancel":
        return await asyncio.to_thread(_cancel, venture_id, payload)

    if action_name == "approve":
        return await _approve(venture_id, payload, actor_subject)

    if action_name == "build.cancel":
        from app.chat.stop_work import run_build_cancel

        return await asyncio.to_thread(run_build_cancel, venture_id, payload, actor_subject)

    if action_name == "manager.rename":
        return await asyncio.to_thread(_rename_manager, venture_id, payload)

    if action_name == "slate_override":
        return await asyncio.to_thread(_slate_override, venture_id, payload)

    if action_name == "office_hours":
        return await asyncio.to_thread(_office_hours, venture_id)

    if action_name == "ceo_review":
        return await _ceo_review(venture_id, payload)

    if action_name == "brain_brief":
        return await _brain_brief(venture_id, str(payload.get("query") or ""))

    if action_name == "social_post":
        from app.chat.actions import SOCIAL_POST_COMING_SOON, social_post_enabled

        if not is_local() and not social_post_enabled():
            return dict(SOCIAL_POST_COMING_SOON)
        return await _social_post(venture_id, payload, actor_subject)

    if action_name == "social_guide":
        return await asyncio.to_thread(_social_guide, venture_id, payload)

    if action_name == "social_status":
        return await _social_status(venture_id)

    if action_name == "social_connect":
        return await asyncio.to_thread(_social_connect, venture_id, payload)

    if action_name.startswith("experiment.exploration."):
        from app.experiments import exploration

        return await exploration.act(venture_id, action_name, payload)

    if action_name == "gtm.diagnosis":
        return await asyncio.to_thread(_gtm_diagnosis, venture_id)

    if action_name == "credits.balance":
        return await asyncio.to_thread(_credits_balance, venture_id, question=str(payload.get("query") or ""))

    if action_name == "connections.status":
        return await asyncio.to_thread(_connections_status, venture_id)

    if action_name == "connections.connect":
        from app.chat import connect

        card = await asyncio.to_thread(connect.handle, venture_id, str(payload.get("query") or ""))
        if card.get("watch"):
            task = asyncio.get_running_loop().create_task(connect.poll(venture_id, card["watch"]))
            _BG.add(task)
            task.add_done_callback(_BG.discard)
        return card

    if action_name == "connections.disconnect":
        from app.chat import connect

        return await asyncio.to_thread(connect.disconnect, venture_id, payload, confirmation_message_id)

    if action_name == "content.drafts":
        return await asyncio.to_thread(_content_drafts, venture_id)

    if action_name in {"experiment.persevere", "experiment.pivot", "experiment.spinoff", "experiment.kill"}:
        from app.chat import experiments

        return await experiments.decision_run(venture_id, action_name, payload, actor_subject)

    if action_name == "experiment.run_review":
        from app.chat import experiments

        return await experiments.execute(venture_id, action_name, payload, actor_subject)

    if action_name == "experiment.export_sheet":
        from app.connections import sheets_export

        return await asyncio.to_thread(sheets_export.export, venture_id, payload, actor_subject)

    if action_name.startswith("experiment."):
        from app.chat import experiments

        return await experiments.execute(venture_id, action_name, payload, actor_subject)

    if action_name == "blocker.explain":
        from app.chat.blockers import explain

        return explain(venture_id, payload)

    if action_name == "chat.model_retry":
        from app.chat.assistant import retry_reply

        return await retry_reply(venture_id, payload)

    from app.analytics.error_events import log_error_event
    from app.chat.actions import disabled_reason

    await asyncio.to_thread(log_error_event, "chat", feature="chat", code="unhandled_chat_action", venture_id=venture_id,
                    status=400, level=logging.ERROR)
    return {
        "kind": "needs_you",
        "action_started": False,
        "body_md": disabled_reason(action.name) or "You cannot use this action in chat yet. Ask me for your next step.",
        "actions": [{"label": "Show your saved status", "action": "status", "style": "primary"}],
        "citations": [],
    }


def _idea_review_failure(venture_id: str) -> dict[str, Any] | None:
    if not is_local():
        return None
    from sqlalchemy import select
    from app.core.jobs import CLAIMABLE, LEASED
    from app.db.models import Job
    from app.db.supymem_models import IdeaSession
    from app.idea.service import loads
    from core.clock import as_utc, utcnow

    with db()() as session:
        venture = session.get(Venture, venture_id)
        if not venture:
            return None
        idea = session.scalar(select(IdeaSession).where(IdeaSession.venture_id == venture_id,
            IdeaSession.workspace_id == venture.workspace_id, IdeaSession.state != "abandoned")
            .order_by(IdeaSession.created_at.desc()).limit(1))
        if not idea or idea.state == "confirmed":
            return None
        latest = next((job for job in session.scalars(select(Job).where(Job.venture_id == venture_id,
            Job.workspace_id == venture.workspace_id, Job.kind.in_(("idea.clarify_turn", "idea.validate")))
            .order_by(Job.created_at.desc(), Job.id.desc()))
            if loads(job.payload_json).get("session_id") == idea.id
            and loads(job.payload_json).get("revision_id") in {None, idea.current_revision_id}), None)
        if not latest:
            return None
        if latest.kind == "idea.clarify_turn" and latest.status in {"failed", "failed_dead", "poison"}:
            return None
        now = utcnow()
        waiting = latest.status in CLAIMABLE and (now - as_utc(latest.created_at)).total_seconds() >= 120
        expired = latest.status in LEASED and (
            now - as_utc(latest.started_at or latest.locked_at or latest.created_at)
        ).total_seconds() >= min(max(1, latest.timeout_s or 900), 900)
        if latest.status not in {"failed", "failed_dead", "poison"} and not waiting and not expired:
            return None
        message = ("Your idea review has not started within two minutes." if waiting else
                   "Your idea review did not finish in time." if expired else "Your idea review stopped before it finished.")
        if waiting or expired:
            message += " Quit and reopen the app, then choose Retry to check your saved review."
        else:
            message += " Choose Retry to review your saved answers again."
        return {"kind": "idea_recovery", "body_md": message + " Your answers are saved.", "citations": [],
                "actions": [{"label": "Retry", "action": "job.retry", "payload": {"job_id": latest.id}, "style": "primary"},
                            {"label": "Check review", "action": "idea.review", "style": "secondary"}]}


def _review_idea(venture_id: str) -> dict[str, Any]:
    if failed := _idea_review_failure(venture_id):
        return failed
    from sqlalchemy import select
    from app.db.models import Job, Venture
    from app.core.jobs import ACTIVE_JOB_STATUSES
    from app.db.supymem_models import IdeaSession
    from app.idea.service import SAVED_IDEA_OPENING, get_session, loads
    from app.idea.agent import Understanding
    from app.idea.runner import _understanding_summary
    from app.chat.idea_proposal import proposal_actions, question_open, review
    from app.state import db
    from core.copy import founder_copy

    with db()() as session:
        venture = session.get(Venture, venture_id)
        row = session.scalar(select(IdeaSession).where(IdeaSession.venture_id == venture_id,
            IdeaSession.workspace_id == venture.workspace_id, IdeaSession.state != "abandoned")
            .order_by(IdeaSession.created_at.desc()).limit(1)) if venture else None
        saved = get_session(row.id) if row else None
        queued = next((job for job in session.scalars(select(Job).where(Job.venture_id == venture_id,
            Job.workspace_id == row.workspace_id, Job.kind.in_(["idea.clarify_turn", "idea.validate"]),
            Job.status.in_(ACTIVE_JOB_STATUSES)))
            if loads(job.payload_json).get("session_id") == row.id
            and (loads(job.payload_json).get("revision_id") == row.current_revision_id
                 or row.state == "validating" and job.kind == "idea.clarify_turn"
                 and saved["turns"] and saved["turns"][-1]["kind"] == "proposal")), None) if row else None
    if not saved:
        return review(venture_id)
    revision = saved.get("current_revision") or {}
    structured = revision.get("structured") or {}
    turn = (saved.get("turns") or [None])[-1]
    if queued and (not turn or turn["role"] == "founder" or turn["kind"] in {"opening", "proposal"}):
        from app.gstack.office_hours import SKIP
        return {"kind": "idea_clarification_pending", "questions_pending": queued.kind == "idea.clarify_turn" or bool(turn and turn["kind"] == "proposal"),
            "body_md": SAVED_IDEA_OPENING + " If no question or summary appears within two minutes, choose Check review. You can also skip the remaining questions and review now.",
            "actions": [{"label": "Check review", "action": "idea.review", "style": "primary"},
                        {"label": SKIP, "action": "chat.reply", "text": SKIP,
                         "payload": {"text": SKIP}, "style": "secondary"}], "citations": [{"job_started": {"job_id": queued.id}}]}
    if question_open(turn, saved["state"]):
        if saved["state"] == "validating" and (turn.get("options") or {}).get("phase") == "gstack":
            from app.gstack.office_hours import question_reply

            body, actions = question_reply(turn["id"])
            return {"kind": "idea_clarification_ready", "body_md": founder_copy(body),
                    "actions": actions or [], "citations": [{"question": True}]}
        return review(venture_id)
    if any(run["state"] == "complete" and run["revision_id"] == revision.get("id")
            for run in saved.get("validation_runs") or []):
        reply = review(venture_id)
        if saved["state"] != "confirmed":
            reply["actions"] = [{**action, "style": "primary"} for action in reply["actions"] if action.get("action") == "idea.confirm"]
            reply["body_md"] += "\n\nConfirm your idea before planning a market experiment."
        return reply
    if saved["state"] == "confirmed":
        understanding = Understanding.model_validate({key: value for key, value in structured.items() if key in Understanding.model_fields})
        return {"kind": "text", "body_md": founder_copy("Your idea is saved." + _understanding_summary(understanding)
            + "\n\nAsk me what to do next."), "actions": [{"label": "Choose my next step", "action": "whats_next", "style": "primary"}], "citations": []}
    if turn and turn["role"] == "agent" and turn["kind"] == "proposal" and saved["state"] == "reviewing":
        understanding = Understanding.model_validate({key: value for key, value in structured.items() if key in Understanding.model_fields})
        actions = proposal_actions(saved["id"], revision["id"])
        if structured.get("_questions_skipped"):
            from dataclasses import replace
            from app.chat.confirm import ConfirmationCard
            card = ConfirmationCard.for_action(get_action("idea.confirm"),
                interpretation="Confirm your idea using the answers you shared, with the remaining questions unanswered.",
                consequences=["Confirms this saved idea. Skipped questions remain unanswered.",
                              "No new paid review, research, experiment or build starts."],
                resulting_state={"session_id": saved["id"], "revision_id": revision["id"]})
            actions[0] = replace(card, confirm_label="Confirm your idea").as_action()
        return {"kind": "confirmation", "body_md": founder_copy(turn["text"] + _understanding_summary(understanding)),
                "actions": actions, "citations": []}
    # No question, proposal or running work is left (its job failed), so showing the summary again goes nowhere.
    from app.gstack.office_hours import SKIP
    actions = [{"label": SKIP, "action": "chat.reply", "text": SKIP, "payload": {"text": SKIP}, "style": "secondary"}]
    if saved.get("clarification_retry_available"):
        actions.insert(0, {"label": "Ask me again", "action": "job.retry", "payload": {"kind": "idea.clarify_turn"}, "style": "primary"})
    return {"kind": "idea_recovery", "citations": [], "actions": actions, "body_md": founder_copy(
        "I could not prepare your next question. Your answers are saved. "
        + ("Ask me to try again, or skip" if len(actions) > 1 else "Skip") + " the questions and review your idea.")}


def _typed_confirm(venture_id: str, actor_subject: str | None) -> dict[str, Any]:
    from app.chat.idea_proposal import typed_confirm

    return _idea_review_failure(venture_id) or typed_confirm(venture_id, actor_subject)


def _skip_and_confirm(venture_id: str, payload: dict[str, Any], actor_subject: str | None) -> dict[str, Any]:
    from sqlalchemy import select
    from app.db.supymem_models import ClarifierTurn, IdeaSession
    from app.gstack.office_hours import SKIP
    from app.idea.service import post_message
    from app.state import db

    with db()() as session:
        row = session.get(IdeaSession, payload["session_id"])
        latest = row and session.scalar(select(ClarifierTurn).where(ClarifierTurn.session_id == row.id)
                                        .order_by(ClarifierTurn.turn.desc()).limit(1))
        # A review still running keeps its own check; only an open question or an unreviewed summary can be skipped.
        allowed = (row is not None and row.venture_id == venture_id and row.current_revision_id == payload["revision_id"]
                   and (row.state == "reviewing" or (latest is not None and latest.role == "agent" and latest.kind == "question")))
    if not allowed:
        return {**_review_idea(venture_id), "citations": []}  # show what is current instead: the idea moved on, or its review is running
    post_message(payload["session_id"], SKIP, actor_subject=actor_subject, reply_in_chat=True)
    with db()() as session:
        revision_id = session.get(IdeaSession, payload["session_id"]).current_revision_id
    return _confirm_idea(venture_id, {"session_id": payload["session_id"], "revision_id": revision_id}, actor_subject)


def _confirm_idea(venture_id: str, payload: dict[str, Any], actor_subject: str | None) -> dict[str, Any]:
    from app.idea.service import confirm

    if payload.get("typed") is True:
        return _typed_confirm(venture_id, actor_subject)
    if not isinstance(payload.get("session_id"), str) or not isinstance(payload.get("revision_id"), str):
        return _validation_first(venture_id)
    try:
        confirmed = confirm(payload["session_id"], revision_id=payload["revision_id"], person_id=actor_subject,
                venture_id=venture_id, start_research=False, accept_validation=True)
    except RuntimeError as exc:
        if str(exc) in _IDEA_CONFIRMATION_BLOCKERS:
            if payload.get("skip_open_questions") is True:
                if str(exc) in {"validation_pending", "clarification_in_progress"}:
                    from app.chat.idea_proposal import confirmation_pending

                    return confirmation_pending()
                return _skip_and_confirm(venture_id, payload, actor_subject)
            log_founder_exception(exc, venture_id, "idea.confirm")
            if payload.get("build_landing_page") is True:
                return _pagebuilder_idea_card(venture_id, actor_subject)
            return _validation_first(venture_id)
        return founder_failure(exc, venture_id, "your idea confirmation",
            {"action": "idea.review", "payload": {}},
            safety="Review the current idea before confirming it again.")
    if payload.get("publish_landing_page") is True:
        from app.chat.site_actions import publish_page

        return publish_page(venture_id, actor_subject)
    if payload.get("build_landing_page") is True:
        if is_local():
            return _build_landing_page(venture_id, {}, actor_subject)
        from app.pagebuilder import api

        quote = api.estimate(venture_id, actor_subject)
        # One card: it showed this ceiling, so the same confirmation starts the build. Older cards without
        # a ceiling, or a changed price, get the quote card instead and nothing starts.
        if quote.get("ready") and type(payload.get("ceiling_micro")) is int and payload["ceiling_micro"] == quote["ceiling_micro"]:
            return _pagebuilder_start(venture_id, actor_subject, quote["quote_fingerprint"])
        return _pagebuilder_quote_card(venture_id, quote)
    new_path = confirmed.get("next_href") == f"/v/{venture_id}/context"
    questions_skipped = ((confirmed["session"].get("current_revision") or {}).get("structured") or {}).get("_questions_skipped")
    if new_path and not (is_local() and questions_skipped):
        from app.chat.build_path import startup_review_reply
        from app.ventures.startup_review import get as startup_context
        if not startup_context(venture_id)["complete"]:
            handoff = startup_review_reply(venture_id)
            return {**handoff, "body_md": "Your idea is saved. " + handoff["body_md"]}
    request = payload.get("experiment_request")
    if isinstance(request, dict):
        from app.chat.experiments import GATED_ACTIONS

        if request.get("action") in GATED_ACTIONS and isinstance(request.get("payload"), dict):
            return {"kind": "text", "body_md": "Your idea is saved. I am preparing the test you requested.",
                "actions": [], "citations": [], "experiment_request": request}
    if new_path or questions_skipped:
        follow = {"label": "Choose my next step", "action": "whats_next", "style": "primary"}
    else:
        follow = {"label": "Build my landing page now", "action": "pagebuilder.build", "payload": {"prepare": True}, "style": "primary"}
    return {"kind": "navigation" if new_path else "text", "body_md": "Your idea is saved. " + ("Ask me what to do next." if new_path or questions_skipped else "Ask me to build your landing page."),
            "actions": [follow], "citations": []}


def _build_landing_page(venture_id: str, payload: dict[str, Any], actor_subject: str | None) -> dict[str, Any]:
    from fastapi import HTTPException

    provider = None
    instruction = None
    if is_local():
        if "retry_job_id" in payload:
            retry_job_id = payload["retry_job_id"]
            if not isinstance(retry_job_id, str) or not retry_job_id:
                raise HTTPException(422, detail={"code": "pagebuilder_new_version_unavailable", "message":
                    "Open Build activity and choose Retry on your stopped landing page."})
            return _pagebuilder_start(venture_id, actor_subject, None, retry_job_id=retry_job_id)
        from app.chat.product_build_request import local_build_intent, local_product_provider

        query = payload.get("query") or ""
        instruction = query or None
        if local_build_intent(query) == "pagebuilder.build":
            try:
                provider = local_product_provider(query, actor_subject)
            except HTTPException as exc:
                return {"kind": "needs_you", "body_md": exc.detail["message"], "action_started": False,
                    "actions": [] if exc.detail["code"] == "build_tool_choice_required" else
                        [{"label": "Connect your coding tool", "href": "/settings/local#coding-tools", "style": "primary"}], "citations": []}
        from app.chat.site_actions import _local_page_setup
        if setup := _local_page_setup(actor_subject):
            return setup
    elif blocked := local_capability_reply("landing_pages"):
        return blocked
    from app.pagebuilder import api

    try:
        quote = api.estimate(venture_id, actor_subject)
    except HTTPException as exc:
        if payload.get("prepare") is True and exc.status_code == 409 and exc.detail == {"code": "pagebuilder_confirmed_revision_required"}:
            return _pagebuilder_idea_card(venture_id, actor_subject)
        if not is_local() and payload.get("prepare") is True and exc.status_code == 404 and exc.detail == {"code": "pagebuilder_disabled"}:
            from app.chat.experiments import page_unavailable_reply
            return page_unavailable_reply()
        raise
    if is_local():
        if ("quote_fingerprint" in payload or "ceiling_micro" in payload) and (
                payload.get("quote_fingerprint") != quote.get("quote_fingerprint")
                or type(payload.get("ceiling_micro")) is not int
                or payload["ceiling_micro"] != quote.get("ceiling_micro")):
            raise HTTPException(409, detail={"code": "pagebuilder_quote_changed"})
        if not quote.get("ready"):
            return _pagebuilder_quote_card(venture_id, quote)
        # A direct request uses the saved idea at admission, not an unreviewed estimate from before admission.
        return _pagebuilder_start(venture_id, actor_subject, payload.get("quote_fingerprint"), provider=provider, instruction=instruction)
    if payload.get("prepare") is True and not is_local():
        return _pagebuilder_quote_card(venture_id, quote)
    # Anything else is an execution: it must carry the exact quote the founder approved, else nothing starts.
    if (payload.get("quote_fingerprint") != quote["quote_fingerprint"]
            or type(payload.get("ceiling_micro")) is not int or payload["ceiling_micro"] != quote["ceiling_micro"]):
        raise HTTPException(409, detail={"code": "pagebuilder_quote_changed"})
    return _pagebuilder_start(venture_id, actor_subject, payload["quote_fingerprint"])


def _saved_outreach_drafts(venture_id: str) -> dict[str, Any]:
    from app.autopilot import outreach
    from app.chat import customers, offers

    view = customers._view(venture_id)
    view["drafts"] = outreach.drafts(venture_id, "draft", "manual")
    if view["drafts"]:
        if not view.get("allow_sending") and any(draft.get("review_state") == "approved" for draft in view["drafts"]):
            view["sends_left_today"] = customers._remaining_today(venture_id, view)
        return customers.render(venture_id, view, show_saved=True)
    notes = outreach.review_notes(venture_id)
    if notes:
        reply = {"kind": "text", "citations": [],
                 "body_md": "Your saved drafts are here for your review. Nothing was sent. "
                            "Choose a recipient, add their email source, and save your sending address in Find customers before sending. "
                            "Tell me what you want to change.\n\n"
                            + "\n\n".join(offers.format_drafts(note["text"]) for note in notes),
                 "actions": [{"label": "Open work", "href": f"/v/{venture_id}/tasks?task={notes[0]['task_id']}", "style": "secondary"}]}
        return reply
    return customers.render(venture_id, view, show_saved=True)


def _recovery_action(venture_id: str, action_name: str, payload: dict[str, Any], actor_subject: str | None) -> dict[str, Any]:
    from app.chat import recovery

    ws = _workspace_id(venture_id)
    if action_name == "recovery.alternative":
        return recovery.queue_alternative(venture_id, payload, ws, actor_subject)
    if action_name == "recovery.submit":
        return recovery.submit(venture_id, payload, ws, actor_subject)
    try:
        recovery.create_plan(venture_id, ws or "", provenance="template")
    except ValueError:
        return recovery._reply("The starter plan needs your idea confirmed and research saved first, so I did not "
                               "create it. Tap Do it another way again and choose to do it yourself.",
                               [recovery.button("plan.compile")])
    from app.chat.handlers.general import _next_step_actions

    return recovery._reply("Your starter plan is saved as a draft. Review it and approve its execution when "
                           "you are ready.", _next_step_actions(venture_id))


def _start_build(venture_id: str, payload: dict[str, Any], actor_subject: str | None) -> dict[str, Any]:
    from app.builder import execution as managed_builds
    from app.infra.apps import get_app

    if not actor_subject:
        raise PermissionError("build_chat_authentication_required")
    app_id = str(payload.get("app_id") or "")
    quote_id = str(payload.get("quote_id") or "")
    input_digest = str(payload.get("input_digest") or "")
    ceiling = payload.get("ceiling_micro")
    if not app_id or not quote_id or not input_digest or type(ceiling) is not int:
        raise ValueError("build_quote_confirmation_required")
    app = get_app(app_id)
    if app["venture_id"] != venture_id:
        raise KeyError("app")
    try:
        result = managed_builds.start(app_id, actor=actor_subject, quote_id=quote_id,
            input_digest=input_digest, approve_budget_micro=ceiling,
            idempotency_key="chat-build:" + quote_id)
    except managed_builds.BuildError as exc:
        if exc.code not in {"build_quote_expired", "build_quote_inputs_changed"}:
            raise
        from app.chat.router import _product_build_reply  # the quote lapsed: show a fresh card, not an error

        fresh = _product_build_reply(venture_id, _brief_or_empty(venture_id, force=True), actor_subject)
        return {**fresh, "body_md": "That quote expired, so I did not start the build. " + fresh["body_md"]}
    run_id = result.get("build_run_id") or result.get("id")
    return {"kind": "text", "body_md": (
        "Your build was queued. Review progress and QA evidence before any publication."),
        "actions": [{"label": "Review build progress", "href": f"/v/{venture_id}/builds?app={app_id}&run={run_id}",
                     "style": "primary"}], "citations": [{"job_started": {"job_id": result.get("job_id")}}]}


def _start_work(venture_id: str, payload: dict[str, Any], actor_subject: str | None) -> dict[str, Any]:
    from fastapi import HTTPException
    from app.db.models import PlannedTask
    from app.planning.execute import TaskRunError, start_planned_task

    task_id = str(payload.get("task_id") or "")
    if not task_id:
        from app.chat.plan_chain import next_task_reply
        return next_task_reply(venture_id, actor_subject)
    with db()() as session:
        row = session.get(PlannedTask, task_id)
        if row is None or row.venture_id != venture_id or row.status != "open" \
                or not row.source.startswith("approved_plan:"):
            raise KeyError("task")
        title = row.title
        managed = "capability:build.run" in (row.tags_json or "")
        plan_task = next((t[len("plan_task:"):] for t in json.loads(row.tags_json or "[]")
                          if t.startswith("plan_task:")), "")
    if managed and not is_local():  # reviewed build path: quote the exact build from chat, or explain the review step
        return _managed_work_reply(venture_id, title, plan_task, actor_subject)
    try:
        started = start_planned_task(venture_id, task_id)
    except TaskRunError as exc:
        if is_local():
            return {"kind": "needs_you", "body_md": exc.message, "action_started": False,
                    "actions": [{"label": "Open Work", "href": f"/v/{venture_id}/tasks"}], "citations": []}
        raise KeyError(exc.code) from exc
    except HTTPException as exc:
        if not is_local():
            raise
        message = exc.detail.get("message") if isinstance(exc.detail, dict) else None
        return {"kind": "needs_you", "action_started": False,
                "body_md": message or "Open Settings > Keys and connections > Coding tools, check your tool connection, then retry your request.",
                "actions": ([{"label": "Buy more", "href": f"/v/{venture_id}/settings#settings-credits"}]
                    if isinstance(exc.detail, dict) and exc.detail.get("code") == "insufficient_credits" else
                    [{"label": "Connect your coding tool", "href": "/settings/local"}]), "citations": []}
    if is_local() and started.get("status") == "cancelled":
        return {"kind": "needs_you", "body_md": "Your saved work changed, so I stopped the coding request; open Work to review the current task before running it.",
                "action_started": False, "actions": [{"label": "Open Work", "href": f"/v/{venture_id}/tasks"}], "citations": []}
    if is_local() and started.get("device_work_order_id"):
        return {"kind": "text", "body_md": "Your request is saved for your coding tool. Choose Watch your build below to watch or stop it.",
                "action_started": True, "actions": [{"label": "Watch your build", "href": started["review_path"]}], "citations": []}
    return {"kind": "text", "body_md": f"Started **{title}** from your plan.",
            "actions": [{"label": "Open work", "href": f"/v/{venture_id}/tasks", "style": "primary"}],
            "citations": [{"job_started": {"job_id": started.get("job_id")}}]}


def _create_build_app(venture_id: str, payload: dict[str, Any], actor_subject: str | None) -> dict[str, Any]:
    from app.infra.apps import create_draft

    if not actor_subject:
        raise PermissionError("build_chat_authentication_required")
    name = str(payload.get("name") or "").strip()
    slug = str(payload.get("slug") or "").strip()
    if not 1 <= len(name) <= 120 or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{1,61}[a-z0-9])", slug):
        raise ValueError("build_app_review_required")
    with db()() as session:
        venture = session.get(Venture, venture_id)
        if venture is None:
            raise KeyError("venture")
        workspace_id = venture.workspace_id
    app = create_draft(workspace_id=workspace_id, venture_id=venture_id,
        name=name, slug=slug, actor=actor_subject, idempotency_key="chat-build-app:" + slug)
    return {"kind": "text", "body_md": (
        f"The draft app {app['name']} is saved. You can start its exact build step here. No build or publication started."),
        "actions": [{"label": "Review the build", "action": "build.prepare", "style": "primary"}], "citations": []}


def _pagebuilder_start(venture_id: str, actor_subject: str | None, quote_fingerprint: str | None, *,
                       provider=None, instruction=None, retry_job_id=None) -> dict[str, Any]:
    from app.autopilot.receipts import queued_job
    from app.pagebuilder import api

    options = {}
    if retry_job_id is not None:
        options["retry_job_id"] = retry_job_id
    elif is_local() and instruction:
        current = api.profile(venture_id, actor_subject).get("current")
        options = {"provider": provider, "instruction": instruction}
        if current and current["state"] in {"ready", "failed"}:
            options["new_version_of"] = current["job_id"]
    result = api._start(venture_id, actor_subject, expected_quote=quote_fingerprint, require_quote=True, retry_failed=True, **options)
    if result["state"] == "ready":
        return {"kind": "site_preview", "run_id": result["job_id"],
                "body_md": "Your saved landing page is already ready. Review its private preview below. Nothing new was started or published.",
                "actions": [{"label": "Review your landing page", "action": "site.preview",
                             "payload": {"job_id": result["job_id"]}}]}
    queued_job(result["job_id"])
    if is_local():
        return {"kind": "text", "run_id": result["job_id"],
            "body_md": "Your private page request is saved for your connected coding tool. Choose Show status here below to watch it, stop it, or open its result folder. Nothing is published.",
            "citations": [{"job_started": {"job_id": result["job_id"]}}],
            "actions": [{"label": "Show status here", "action": "status", "task_id": "landing_page_build",
                         "payload": {"job_id": result["job_id"], "venture_id": venture_id}}]}
    body = "Building your private landing page preview. Nothing is published."
    return {"kind": "text", "run_id": result["job_id"], "body_md": body,
            "actions": [{"label": "View Website", "href": f"/v/{venture_id}/site"}]}


def _pagebuilder_idea_card(venture_id: str, actor_subject: str | None, *, publish: bool = False) -> dict[str, Any]:
    from sqlalchemy import select
    from app.chat.confirm import ConfirmationCard
    from app.db.supymem_models import IdeaSession, ValidationRun
    from app.idea.validation import METHODS
    from app.pagebuilder import api

    with db()() as session:
        idea = session.scalar(select(IdeaSession).where(IdeaSession.venture_id == venture_id,
            IdeaSession.state != "abandoned").order_by(IdeaSession.updated_at.desc()).limit(1))
        latest = {}
        if idea:
            runs = session.scalars(select(ValidationRun).where(ValidationRun.session_id == idea.id,
                ValidationRun.revision_id == idea.current_revision_id, ValidationRun.workspace_id == idea.workspace_id,
                ValidationRun.venture_id == idea.venture_id).order_by(ValidationRun.created_at.desc(), ValidationRun.id))
            for run in runs:
                latest.setdefault(run.method, run.state)
        if not idea or idea.state not in {"reviewing", "validating"} or not idea.current_revision_id or any(latest.get(method) != "complete" for method in METHODS):
            return {"kind": "needs_you", "body_md": "Your idea review is not finished yet, so I cannot build your landing page. Finish the review here in chat first. Nothing was started or charged.",
                    "actions": [{"label": "Continue idea review", "action": "idea.review", "style": "primary"}], "citations": []}
        session_id, revision_id = idea.id, idea.current_revision_id
    price = api.price(venture_id, actor_subject)
    if not price["ready"]:
        return _pagebuilder_quote_card(venture_id, price)
    if is_local():
        return _confirm_idea(venture_id, {"session_id": session_id, "revision_id": revision_id,
            "build_landing_page": True, **({"publish_landing_page": True} if publish else {})}, actor_subject)
    card = ConfirmationCard.for_action(get_action("idea.confirm"),
        interpretation="Confirm your validated idea and build a private landing page preview from it.",
        consequences=["Accepts the completed validation findings and confirms your idea. No research starts.",
                      "You see a private preview first. Nothing is published."],
        resulting_state={"session_id": session_id, "revision_id": revision_id, "build_landing_page": True,
                         "ceiling_micro": price["ceiling_micro"], **({"publish_landing_page": True} if publish else {})})
    return {"kind": "confirmation", "body_md": "Confirm to accept your idea and build your landing page. Nothing has started yet.",
            "actions": [card.as_action()], "citations": []}


def _pagebuilder_quote_card(venture_id: str, quote: dict[str, Any]) -> dict[str, Any]:
    if not quote.get("ready"):
        if is_local():
            from app.pagebuilder.local import failure_reason
            return {"kind": "needs_you", "citations": [], "body_md": failure_reason(quote.get("reason")),
                "actions": [{"label": "Retry", "action": "pagebuilder.build", "payload": {"prepare": True}}]}
        if quote.get("reason") == "pagebuilder_insufficient_credits":
            from app.chat.blockers import explain

            return explain(venture_id, {"code": "insufficient_credits"})
        from app.chat.blockers import error_copy
        return with_explain({"kind": "needs_you", "citations": [], "actions": [],
                             "body_md": error_copy(quote.get("reason")) or "I could not start your landing page yet. Your idea is saved. Open Settings to review your page setup before trying again."},
                            str(quote.get("reason") or "pagebuilder_provider_or_credits_unavailable"))
    from app.chat.confirm import ConfirmationCard

    card = ConfirmationCard.for_action(get_action("pagebuilder.build"),
        interpretation="Build a private landing page preview from your idea.",
        consequences=["You see a private preview first. Nothing is published."],
        resulting_state={"quote_fingerprint": quote["quote_fingerprint"], "ceiling_micro": quote["ceiling_micro"]})
    return {"kind": "confirmation", "citations": [], "actions": [card.as_action()],
            "body_md": "Confirm to build your landing page. Nothing has started yet."}


def _workspace_id(venture_id: str) -> str | None:
    with db()() as session:
        venture = session.get(Venture, venture_id)
        return venture.workspace_id if venture else None


def _build_with_new_app(venture_id: str, payload: dict[str, Any], actor_subject: str | None) -> dict[str, Any]:
    """Save the draft app if missing, take a fresh quote, and start the requested build.

    The server ceiling must match the fresh quote before execution. The draft app is idempotent by slug.
    """
    from app.builder import execution as managed_builds
    from app.chat.blockers import humanize_error
    from app.infra.apps import create_draft

    if not actor_subject:
        raise PermissionError("build_chat_authentication_required")
    create = payload.get("create_app") if isinstance(payload.get("create_app"), dict) else {}
    name, slug = str(create.get("name") or "").strip(), str(create.get("slug") or "").strip()
    plan_id, plan_task = str(payload.get("plan_id") or ""), str(payload.get("plan_task_id") or "")
    ceiling = payload.get("ceiling_micro")
    if (not 1 <= len(name) <= 120 or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{1,61}[a-z0-9])", slug)
            or not plan_id or not plan_task or type(ceiling) is not int):
        raise ValueError("build_app_review_required")
    workspace_id = _workspace_id(venture_id)
    if workspace_id is None:
        raise KeyError("venture")
    builds = {"label": "Open builds", "href": f"/v/{venture_id}/builds", "style": "primary"}
    try:
        app = create_draft(workspace_id=workspace_id, venture_id=venture_id, name=name, slug=slug,
                           actor=actor_subject, idempotency_key="chat-build-app:" + slug)
        quote = managed_builds.estimate(app["id"], actor=actor_subject, plan_id=plan_id, plan_task_id=plan_task)
    except (managed_builds.BuildError, KeyError, ValueError) as exc:
        return {"kind": "text", "citations": [], "actions": [builds], "body_md": (
            f"I could not start the build: {humanize_error(str(exc))} Nothing was reserved or started.")}
    if not quote.get("ready"):
        reasons = quote.get("reasons") or []
        reply = {"kind": "needs_you", "citations": [], "actions": [builds], "body_md": (
            "Your app is saved, but the build cannot start yet: "
            + (", ".join(humanize_error(str(r)) for r in reasons) or "the build is not available right now")
            + ". Nothing was reserved or started.")}
        return with_explain(reply, "insufficient_credits") if "insufficient_credits" in reasons else reply
    if quote.get("ceiling_micro") != ceiling:
        from app.chat.router import _product_build_reply

        fresh = _product_build_reply(venture_id, _brief_or_empty(venture_id, force=True), actor_subject,
                                     task_id=plan_task)
        return {**fresh, "body_md": "The build price changed since you saw it, so I did not start it. " + fresh["body_md"]}
    run = managed_builds.start(app["id"], actor=actor_subject, quote_id=quote["quote_id"],
        input_digest=quote["input_digest"], approve_budget_micro=ceiling, idempotency_key="chat-build:" + quote["quote_id"])
    return {"kind": "text", "citations": [{"job_started": {"job_id": run.get("job_id")}}], "actions": [{"label": "Watch the build", "style": "primary",
            "href": f"/v/{venture_id}/builds?app={app['id']}&run={run['build_run_id']}"}],
            "body_md": f"Build started for {name}. It needs independent QA and your review before anything is published."}


def _managed_work_reply(venture_id: str, title: str, plan_task: str, actor_subject: str | None) -> dict[str, Any]:
    """Reuse the managed build's guarded quote and execution path."""
    from app.chat.router import _product_build_reply

    href = f"/v/{venture_id}/builds" + (f"?task={plan_task}" if plan_task else "")
    try:
        reply = _product_build_reply(venture_id, _brief_or_empty(venture_id, force=True), actor_subject, task_id=plan_task or None)
    except Exception as exc:  # noqa: BLE001
        reply = founder_failure(exc, venture_id, "your build estimate",
            {"action": "build.prepare", "payload": {}})
    if reply.get("kind") != "confirmation":
        reply["actions"] = [{"label": "View build", "style": "primary", "href": href}] + [
            a for a in reply.get("actions") or [] if a.get("href") != href]
    reply["body_md"] = (f"**{title}** is a managed build, so it starts only after you approve its exact budget. "
                        + str(reply.get("body_md") or "Open it to review the estimate."))
    return reply


def _brief_or_empty(venture_id: str, *, force: bool = False) -> dict[str, Any]:
    from app.ventures.state_brief import get_venture_state_brief

    try:
        return get_venture_state_brief(venture_id, force=force)
    except Exception as exc:  # noqa: BLE001
        log_founder_exception(exc, venture_id, "chat.venture_brief")
        return {}


def _gtm_diagnosis(venture_id: str) -> dict[str, Any]:
    return _gtm_diagnosis_from_brief(venture_id, _brief_or_empty(venture_id))


def _gtm_diagnosis_from_brief(venture_id: str, brief: dict[str, Any]) -> dict[str, Any]:
    """Render the same saved diagnosis for a card or a mixed chat answer."""
    gtm = brief.get("gtm") or {}
    if not gtm.get("present"):
        return {
            "kind": "text",
            "body_md": "No GTM diagnosis is saved for this venture yet.",
            "actions": [{"label": "Open GTM", "href": f"/gtm?venture={venture_id}", "style": "primary"}],
            "citations": [],
        }
    bottleneck = str(gtm.get("bottleneck") or "The saved diagnosis does not name a bottleneck.")
    gaps = [str(gap) for gap in (gtm.get("evidence_gaps") or []) if gap]
    evidence = (" Missing evidence: " + " ".join(gaps[:2])) if gaps else " No supporting research is recorded here."
    return {
        "kind": "text",
        "body_md": (
            f"The saved GTM diagnosis for this venture says: {bottleneck} "
            "It is provisional advice based on founder-reported input, not independent research or outreach."
            f"{evidence}"
        ),
        "actions": [{"label": "Open GTM", "href": f"/gtm?venture={venture_id}", "style": "primary"}],
        "citations": [],
    }


def _credits_balance(venture_id: str, *, question: str = "") -> dict[str, Any]:
    if is_local():
        from app.chat.jarvis import spend_today

        return spend_today(venture_id)
    return {"kind": "navigation", "body_md": "You can review your account in Settings.",
            "actions": [{"label": "Open Settings", "href": f"/v/{venture_id}/settings", "style": "primary"}],
            "citations": []}


_BG: set = set()  # keeps connect-poll tasks alive until they finish
_DRAFT_STATES = {"draft": "saved as a draft", "review": "waiting for your review", "in_review": "waiting for your review",
                 "pending_review": "waiting for your review", "approved": "approved", "scheduled": "scheduled",
                 "published": "posted", "rejected": "rejected"}


def _connections_status(venture_id: str) -> dict[str, Any]:
    from app.chat.connect import status

    return status(venture_id)


def _content_drafts(venture_id: str) -> dict[str, Any]:
    brief = _brief_or_empty(venture_id)
    drafts = brief.get("content_drafts") or []
    if not drafts:
        return {
            "kind": "text",
            "body_md": "No content drafts are on file for this venture.",
            "actions": [{"label": "Open content", "href": f"/content?venture={venture_id}", "style": "primary"}],
            "citations": [],
        }
    from app.chat.posts import LABELS

    lines = [
        "- Draft written by you, not posted" if str(d.get("platform") or "") == "founder" else
        f"- Draft for {LABELS.get(str(d.get('platform') or '').lower(), str(d.get('platform') or '').title() or 'a platform')}, "
        f"{_DRAFT_STATES.get(str(d.get('state') or '').lower(), 'saved as a draft')}"
        for d in drafts[:8]
    ]
    return {
        "kind": "text",
        "body_md": "Content drafts on file:\n" + "\n".join(lines),
        "actions": [],
        "citations": [],
    }


async def find_customers_reply(venture_id: str, query: str = "", *, start_requested: bool = True) -> dict[str, Any]:
    """Find customers stays in chat: search card, progress, leads, drafts and send review (app.chat.customers)."""
    from app.chat.customers import find_reply
    from app.chat.intent import linkedin_people_request, requested_customer_search

    if linkedin_people_request(query):
        from app.gtm.linkedin_people import chat_reply
        return await asyncio.to_thread(chat_reply, venture_id)

    reply = await find_reply(venture_id, query)
    from app.chat.customers import _SEARCH_REQUEST
    from app.chat.guard import _outreach_requests
    from app.chat.search_recovery import search_forbidden

    if start_requested and (requested_customer_search(query) or _SEARCH_REQUEST.match(query) and not search_forbidden(query) and not _outreach_requests(query)):
        from app.chat.confirmations import start_requested_work

        return await start_requested_work(venture_id, reply, None, actions={"gtm.find"})
    return reply


def _leads(venture_id: str) -> dict[str, Any]:
    from app.conversion import waitlist

    wid = _workspace_id(venture_id)
    open_site = [{"label": "Open Website", "href": f"/v/{venture_id}/site", "style": "primary"}]
    if not wid:
        return {"kind": "text", "body_md": "No workspace yet, so there is no waitlist to read.",
                "actions": open_site, "citations": []}
    try:
        state = waitlist.status(venture_id, wid, include_leads=True)
    except waitlist.WaitlistError:
        return {"kind": "text", "body_md": "I could not read the waitlist just now. Open Website to see it.",
                "actions": open_site, "citations": []}
    total = int(state.get("total") or 0)
    lines = [f"**{total} waitlist signup{'' if total == 1 else 's'}**"
             + ("" if state.get("enabled") else " (the waitlist is currently closed)")]
    lines += [f"- {lead.get('email') or '(no email)'}" for lead in (state.get("leads") or [])[:8]]
    lines.append("These people consented to hear from you. Sign-ups show interest, not proof of demand or users.")
    return {"kind": "text", "body_md": "\n".join(lines), "actions": open_site, "citations": []}


def research_needs_start(venture_id: str) -> bool:
    """True when chat should offer durable research.start instead of a dossier read."""
    from app.config import data_root
    dossier = MEM.get(f"research_dossier:{venture_id}") or {}
    if dossier:
        return False
    md_path = data_root() / "ventures" / venture_id / "research" / "validation_dossier.md"
    if md_path.is_file():
        return False
    try:
        from app.research.service import list_dossiers

        if list_dossiers(venture_id):
            return False
    except Exception as exc:  # noqa: BLE001
        log_founder_exception(exc, venture_id, "research.list_dossiers")
    return True


def _validation_first(venture_id: str) -> dict[str, Any]:
    return with_explain({
        "kind": "needs_you",
        "action_started": False,
        "body_md": "Review and confirm your idea before research. Nothing was started.",
        "actions": [{"label": "Review my idea here", "action": "idea.review", "style": "primary"}],
        "citations": [],
    }, "validation_incomplete")


def _confirm_idea_for_research(venture_id: str, actor_subject: str | None, *,
                              expected_advance_binding: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Do what the Idea page's "Continue to research" does: accept the complete
    validation runs, then confirm the current revision. Returns a needs_you
    reply when the idea cannot be confirmed yet, else None."""
    from sqlalchemy import select

    from app.db.supymem_models import IdeaRevision, IdeaSession, ValidationRun
    from app.idea.service import _loads, _project_intent, confirm
    from app.idea.validation import METHODS, decide

    if expected_advance_binding is not None:
        try:
            confirm(expected_advance_binding["idea_session_id"],
                    revision_id=expected_advance_binding["idea_revision_id"], person_id=actor_subject,
                    expected_advance_binding=expected_advance_binding)
        except RuntimeError as exc:
            if str(exc) in _IDEA_CONFIRMATION_BLOCKERS:
                log_founder_exception(exc, venture_id, "idea.confirm")
                return _validation_first(venture_id)
            return founder_failure(exc, venture_id, "your idea confirmation",
                {"action": "idea.review", "payload": {}},
                safety="Review the current idea before confirming it again.")
        return None

    with db()() as session:
        idea = session.scalar(select(IdeaSession).where(
            IdeaSession.venture_id == venture_id, IdeaSession.state != "abandoned",
        ).order_by(IdeaSession.created_at.desc()).limit(1))
        if idea is None:
            return _validation_first(venture_id)
        if idea.state == "confirmed":
            return None
        sid, revision_id, case = idea.id, idea.current_revision_id, idea.case
        rev = session.get(IdeaRevision, revision_id) if revision_id else None
        personal = (_project_intent(session, idea.workspace_id, idea.venture_id) == "personal"
                    and _loads(rev.structured if rev else None).get("project_intent") == "personal")
        runs = session.scalars(select(ValidationRun).where(
            ValidationRun.session_id == sid, ValidationRun.revision_id == revision_id,
        ).order_by(ValidationRun.created_at.desc(), ValidationRun.id)).all()
        latest: dict[str, Any] = {}
        for run in runs:
            latest.setdefault(run.method, run)
        pending = [r.id for r in latest.values() if r.state == "complete" and r.founder_decision != "accept"]
        complete = all(m in latest and latest[m].state == "complete" for m in METHODS)
        from app.ventures.journey_overrides import read as journey_choices
        skipped_validation = "validation" in journey_choices(venture_id, session=session)["skipped"]
    if personal:
        return None  # personal projects keep the existing "confirm on Idea first" reply
    if case == "explore" and not skipped_validation:
        if not complete:
            return _validation_first(venture_id)
        for run_id in pending:
            decide(sid, run_id, "accept")
    try:
        confirm(sid, revision_id=revision_id, person_id=actor_subject)
    except RuntimeError as exc:
        if str(exc) in _IDEA_CONFIRMATION_BLOCKERS:
            log_founder_exception(exc, venture_id, "idea.confirm")
            return _validation_first(venture_id)
        return founder_failure(exc, venture_id, "your idea confirmation",
            {"action": "idea.review", "payload": {}},
            safety="Review the current idea before confirming it again.")
    return None


def _research_start(venture_id: str, actor_subject: str | None = None, *, expected_revision_id: str | None = None) -> dict[str, Any]:
    """Queue durable deep research via the same path as the Research UI."""
    from app.autopilot import receipts
    from app.chat.guard import paused_reply
    from app.research.errors import ResearchPreflightBlocked
    from app.ventures.lifecycle import is_paused
    from app.ventures.state_brief import invalidate_venture_state_brief

    if is_paused(venture_id):
        return paused_reply(venture_id)
    started: bool | None = False
    try:
        from app.research.service import start
        from app.chat.build_path import research_prerequisites

        from app.ventures.build_prerequisites import answers_pending

        with db()() as session:
            if answers_pending(session, session.get(Venture, venture_id)):
                raise ResearchPreflightBlocked("startup_review_required")

        from app.research.local_providers import setup_reply
        setup = local_capability_reply("research") if is_local() else setup_reply()
        if setup:
            return {**setup, "action_started": False}

        from app.ventures.journey_overrides import read as journey_choices
        # Starting research accepts finished validation and confirms the idea, like the Idea page does.
        blocked = None if journey_choices(venture_id)["skipped"] else _confirm_idea_for_research(venture_id, actor_subject)
        if blocked:
            if blocked.get("kind") == "text":
                return {**blocked, "action_started": False}
            return {**(research_prerequisites(venture_id) or blocked), "action_started": False}

        started = None
        out = start(venture_id, **({"expected_revision_id": expected_revision_id} if expected_revision_id else {}))
        if not out.get("job_id"):
            return {**out, "action_started": False}
        started = True
        receipts.queued_job(out["job_id"])
        invalidate_venture_state_brief(venture_id)
        return {
            "kind": "text",
            "action_started": True,
            "run_id": out["job_id"],
            "body_md": (
                "I scheduled your research. You can follow its progress here. Nothing is built or published."
            ),
            "actions": [
                {
                    "label": "Open report" if is_local() else "Open research",
                    "href": f"/v/{venture_id}/research",
                    "style": "primary",
                }
            ] + ([{"label": "Show findings here", "action": "research", "style": "secondary",
                   "payload": {"job_id": out["job_id"]}}] if is_local() else []),
            "citations": [],
        }
    except RuntimeError as exc:
        if not isinstance(exc, ResearchPreflightBlocked):
            receipts.failed()
        code = str(exc) if isinstance(exc, ResearchPreflightBlocked) else ""
        if code == "startup_review_required":
            log_founder_exception(exc, venture_id, "research.start")
            return {**(research_prerequisites(venture_id) or {"kind": "needs_you", "body_md": "Review your saved answers and priorities before starting research. No research started.",
                    "actions": [{"label": "Review my answers here", "action": "startup.review", "style": "primary"}], "citations": []}), "action_started": False}
        if code == "research_confirmation_required":
            log_founder_exception(exc, venture_id, "research.start")
            return {**(research_prerequisites(venture_id) or _validation_first(venture_id)), "action_started": False}
        if code == "research_insufficient_credits":
            log_founder_exception(exc, venture_id, "research.start")
            return with_explain({
                "kind": "needs_you",
                "action_started": False,
                "body_md": "You have reached your usage limit. Open Settings to get more, then retry.",
                "actions": [
                    {
                        "label": "Open Settings",
                        "href": f"/v/{venture_id}/settings",
                        "style": "primary",
                    }
                ],
                "citations": [],
            }, "insufficient_credits")
        reply = founder_failure(exc, venture_id, "your research", {"action": "status", "payload": {}},
            safety="I could not confirm whether research started. Check its progress before starting it again.")
        return {**reply, "action_started": False if isinstance(exc, ResearchPreflightBlocked) else started}
    except Exception as exc:  # noqa: BLE001, never 500 the chat dock
        receipts.failed()
        reply = founder_failure(exc, venture_id, "your research", {"action": "status", "payload": {}},
            safety="I could not confirm whether research started. Check its progress before starting it again.")
        return {**reply, "action_started": started}


async def _research(venture_id: str, job_id: str | None = None) -> dict[str, Any]:
    return await asyncio.to_thread(_saved_research, venture_id, job_id)


def _saved_research(venture_id: str, job_id: str | None = None) -> dict[str, Any]:
    """Read saved research. Never call live research_recon from chat (that 500'd the dock)."""
    from app.config import data_root
    from app.org.characters import owner_for_work, stamp_speaker
    from app.org.summary import resolve_org
    from app.chat.steer import founder_competitors

    if not job_id and research_needs_start(venture_id):
        from app.chat.build_path import research_card
        return research_card(venture_id)

    org = resolve_org(venture_id)
    speaker = owner_for_work("research dossier", ["research"], org.get("agents") or [])
    named = founder_competitors(venture_id)
    if named:
        from app.chat.build_path import research_prerequisites
        blocked = research_prerequisites(venture_id)
        if blocked:
            return blocked
        body = stamp_speaker(
            "Founder-corrected competitors:\n"
            + "\n".join(f"- **{name}**" for name in named)
            + "\n\nI will not reuse the old competitor names for this. You can start fresh research here.",
            speaker,
        )
        return {
            "kind": "text",
            "body_md": body,
            "actions": [
                {
                    "label": "Prepare fresh research",
                    "action": "research.prepare",
                    "style": "primary",
                }
            ],
            "citations": [],
        }

    md_path = data_root() / "ventures" / venture_id / "research" / "validation_dossier.md"
    dossier = MEM.get(f"research_dossier:{venture_id}") or {}
    dossier = dossier if isinstance(dossier, dict) else {}
    dossiers: list[dict] = []
    try:
        from app.research.service import list_dossiers

        dossiers = list_dossiers(venture_id)
    except Exception as exc:  # noqa: BLE001
        log_founder_exception(exc, venture_id, "research.saved_dossiers")
        dossiers = []

    from app.chat.milestone_hooks import _local_research_result, research_report_link, research_status

    if is_local() and (dossiers or job_id):
        selected = next((row for row in dossiers if row.get("job_id") == job_id), None) if job_id else dossiers[0]
        if selected and selected["state"] in {"complete", "partial"}:
            return {"kind": "text", **_local_research_result(venture_id, selected["id"])}
        if not selected and job_id:
            from app.core.job_status import read_job

            job = read_job(job_id)
            if job and job["venture_id"] == venture_id and job["kind"] == "research.deep":
                body = research_status({"state": job["status"]}, code=job["error_code"] or "")
            else:
                body = "I could not open that report. Open Research to check your saved reports."
        else:
            body = research_status(selected)
        return {"kind": "text", "body_md": body,
                "actions": [research_report_link(venture_id, selected["id"] if selected else None)], "citations": []}

    if md_path.is_file() or dossier or dossiers:
        excerpt = ""
        if md_path.is_file():
            try:
                excerpt = md_path.read_text(encoding="utf-8")[:500].strip()
            except OSError as exc:
                log_founder_exception(exc, venture_id, "research.saved_excerpt")
                excerpt = ""
        lines = ["Your saved research:"]
        if dossiers:
            lines.append(research_status(dossiers[0]))
        if excerpt:
            lines.append("")
            lines.append(excerpt)
            if len(excerpt) >= 480:
                lines.append("…")
        body = stamp_speaker("\n".join(lines), speaker)
        return {
            "kind": "text",
            "body_md": body,
            "actions": [
                {
                    "label": "What should I do next?",
                    "action": "whats_next",
                    "style": "primary",
                }
            ],
            "citations": [],
        }

    from app.chat.build_path import research_card
    return research_card(venture_id)


def _agents(venture_id: str) -> dict[str, Any]:
    from app.org.characters import enrich_org
    from app.org.summary import resolve_org

    org = enrich_org(resolve_org(venture_id))
    agents = org.get("agents") or []
    if not agents:
        return {
            "kind": "text",
            "body_md": "No teammates on this venture yet, they appear after the charter locks.",
            "actions": [{"label": "Open team", "href": f"/v/{venture_id}/org"}],
            "citations": [],
        }
    live_by_id: dict[str, dict] = {}
    try:
        from app.org.heartbeat import list_live_status

        live_by_id = {
            str(row.get("agent_id") or ""): row for row in list_live_status(venture_id)
        }
    except Exception as exc:  # noqa: BLE001
        log_founder_exception(exc, venture_id, "agents.live_status")
        live_by_id = {}
    lines = ["**Who's on the team**"]
    for a in agents[:12]:
        name = a.get("name") or "Teammate"
        title = a.get("title") or a.get("role") or "teammate"
        live = live_by_id.get(str(a.get("id") or "")) or {}
        state = str(live.get("state") or a.get("status") or "ready")
        doing = live.get("current_task_desc")
        suffix = f", {doing}" if doing else ""
        lines.append(f"- **{name}** · {title} · {state}{suffix}")
    lines.append("\nPing someone with `@Maya` or `@Research` in chat.")
    return {
        "kind": "text",
        "body_md": "\n".join(lines),
        "actions": [{"label": "Open team", "href": f"/v/{venture_id}/org", "style": "primary"}],
        "citations": [],
    }


def _whats_next(venture_id: str) -> dict[str, Any]:
    from app.chat.next_moves import compose
    return compose(venture_id, requested=True)


def _refine_site_context(venture_id: str) -> dict[str, Any] | tuple[str, str, dict, str]:
    factory = db()
    with factory() as session:
        ven = session.get(Venture, venture_id)
        if not ven or not ven.workspace_id:
            return {
                "kind": "text",
                "body_md": "You do not have a saved website here yet. Ask me to build your private landing page first.",
                "actions": [],
                "citations": [],
            }
        wid = ven.workspace_id
        idea = current_idea(ven, session=session) or ""
        name = ven.name
    charter = MEM.get(f"charter:{venture_id}") or {}
    if not charter:
        return {
            "kind": "text",
            "body_md": "You need to save your website requirements before changing this page. Review your requirements, then ask me to update your website.",
            "actions": [{"label": "Review requirements", "href": f"/v/{venture_id}/charter"}],
            "citations": [],
        }
    from app.firstlook.service import FirstLookError, assert_existing_page

    try:
        assert_existing_page(wid, venture_id)
    except FirstLookError as exc:
        return {"kind": "text", "body_md": exc.message, "citations": [],
                "actions": [{"label": "Review your saved page", "action": "site.preview", "style": "primary"}]}
    return wid, idea, charter, name


async def _refine_site(venture_id: str) -> dict[str, Any]:
    from app.firstlook.service import refine_from_charter

    if is_local():
        from app.chat.site_actions import _local_page_setup
        from app.core.local_owner import current_owner
        if setup := await asyncio.to_thread(_local_page_setup, current_owner().id):
            return setup
    context = await asyncio.to_thread(_refine_site_context, venture_id)
    if isinstance(context, dict):
        return context
    wid, idea, charter, name = context
    if is_local():
        from app.firstlook import versions
        from app.firstlook.service import FirstLookError, prepare_local_page
        try:
            order = await asyncio.to_thread(prepare_local_page, wid, venture_id,
                {"idea": idea, "name": name, "charter": charter, "request": "Refine your page from your saved requirements."},
                source=versions.get_latest(wid, venture_id=venture_id))
        except FirstLookError as exc:
            return {"kind": "needs_you", "body_md": exc.message,
                "actions": [{"label": "Review your saved page", "href": f"/v/{venture_id}/site"}], "citations": []}
        return {"kind": "text", "body_md": "Your coding tool is working on your page. Choose Watch your page edit below to watch or stop it, then review its checked result. Nothing is published.",
            "actions": [{"label": "Watch your page edit", "href": order["review_path"], "style": "primary"}],
            "citations": [], "device_order": order}
    view = await refine_from_charter(
        wid,
        idea,
        charter,
        venture_id=venture_id,
        venture_name=name,
    )
    if is_local() and isinstance(view, dict):
        return {"kind": "text", "body_md": "Your coding tool is working on your page. Choose Watch your tool below to check its progress.",
                "actions": [{"label": "Watch your tool", "href": view["review_path"], "style": "primary"}],
                "citations": [], "device_order": view}
    url = getattr(view, "url", None) or f"/w/{wid}/site/"
    return {
        "kind": "text",
        "body_md": f"Rebuilt the page from your charter (stage **{getattr(view, 'stage', 'refined')}**).",
        "actions": [{"label": "Open site", "href": url, "style": "primary"}],
        "citations": [],
    }


_SEARCH_ANSWER_RULES = (
    "Answer the founder's request using ONLY the search results and page text provided. Be direct and specific: "
    "list the concrete names, details and contact information the pages actually show, each with its source link "
    "as a markdown link. Never invent people, numbers, emails or phone numbers; say plainly what was not found. "
    "Do not paste page text. The report is for the founder: address them as you, never greet the manager or a teammate. Never mention \"provided information\", "
    "\"search results\" or \"page text\". Under 250 words.")


async def _answer_from_search(query: str, look: dict[str, Any], *, venture_id: str | None = None) -> str:
    """Turn search hits and read pages into the answer the founder asked for (raw list only if the model fails)."""
    from llm_gateway import sole
    from app.credits import metered
    evidence = {"request": query,
                "results": [{"title": h.get("title"), "url": h.get("url"), "snippet": h.get("snippet")} for h in look.get("hits") or []],
                "pages": [{"url": p.get("url"), "text": " ".join(str(p.get("excerpt") or "").split())[:5000]}
                          for p in look.get("pages") or [] if p.get("excerpt")]}
    try:
        resp = await metered.llm(
            [{"role": "system", "content": _SEARCH_ANSWER_RULES},
             {"role": "user", "content": json.dumps(evidence, ensure_ascii=False)[:30000]}],
            purpose="venture.chat.assistant", workspace_id=sole.get_active_workspace_id(), tier="flash", max_tokens=900)
        choices = getattr(resp, "choices", None) or []
        return str(getattr(getattr(choices[0], "message", None), "content", "") or "").strip() if choices else ""
    except Exception as exc:  # noqa: BLE001 - the plain result list is still an honest reply
        log_founder_exception(exc, venture_id or getattr(current_or_none(), "venture_id", None), "search.summarize")
        return ""


async def _openworker_search(query: str, *, venture_id: str | None = None, public_only: bool = False) -> dict[str, Any]:
    if blocked := local_capability_reply("research"):
        return blocked
    from app.chat.search_recovery import search_forbidden, manual_discovery_reply, discovery_query, search_failure_reply
    if search_forbidden(query):
        return await asyncio.to_thread(manual_discovery_reply, query, venture_id=venture_id)
    if not query.strip():
        return {
            "kind": "text",
            "body_md": "Tell me what to search for (e.g. “search competitors for X”).",
            "actions": [],
            "citations": [],
        }
    from app.research.explore import explore_web, render_explore

    people_search = re.match(r"^\s*site:(?:www\.)?linkedin\.com/in/?\s+(.+)", query, re.IGNORECASE)
    public_only = public_only or bool(people_search)
    official = None if public_only else _official_named_topics(query)
    if official:
        providers, topic = official
        looks = await asyncio.gather(*(
            explore_web(f"{provider} {topic} official documentation", max_results=8, extract=3)
            for provider in providers
        ))
        lines = ["I searched each named provider separately for its own documentation. "
                 "These links are leads for review, not an approved integration or reuse right."]
        citations = []
        for provider, look in zip(providers, looks, strict=True):
            slug = provider.lower()
            hits = [hit for hit in look.get("hits", [])
                    if _provider_controls_url(str(hit.get("url") or ""), slug)]
            if not hits:
                lines.append(f"- **{provider}:** No provider-controlled source was found in this search. "
                             "Do not rely on third-party summaries as its terms.")
                continue
            hit = hits[0]
            title, url = str(hit.get("title") or provider), str(hit["url"])
            lines.append(f"- **{provider}:** [{title}]({url})")
            citations.append({"label": title, "href": url})
        lines.append("**Still unverified:** whether each provider permits this venture to aggregate, "
                     "cache, or republish postings across employers; whether an employer's "
                     "consent or a separate agreement is needed. No postings were scraped or "
                     "source approved by this search.")
        return {"kind": "text", "body_md": "\n\n".join(lines),
                "actions": [], "citations": citations}

    look = await explore_web(query.strip()[:600] if public_only else discovery_query(query), max_results=5,
                             extract=0 if public_only else 3, **({"public_only": True} if public_only else {}))
    if people_search and look.get("mode") != "error":
        from core.copy import no_dashes

        from app.gtm.linkedin_people import profile_url

        audience = no_dashes(people_search[1].strip())
        profiles = {}
        for hit in look.get("hits") or []:
            if url := profile_url(hit.get("url")):
                profiles.setdefault(url, hit)
        if not profiles:
            body = (f"I found no public LinkedIn profiles for {audience} in this search. "
                    "You can broaden your audience or add someone you know. Nothing was sent.")
        else:
            body = f"Public LinkedIn profiles returned for {audience}:\n" + "\n".join(
                f"- [{hit.get('title') or 'Public profile'}]({url}): {hit.get('snippet') or 'No public snippet was returned.'}"
                for url, hit in profiles.items())
            body += ("\n\nYou can review these public snippets to check who fits your audience. "
                     "I have not opened LinkedIn or verified contact details. Nothing was sent.")
        return {"kind": "text", "body_md": no_dashes(body), "actions": [],
                "citations": [{"label": no_dashes(str(hit.get("title") or "Public profile")), "href": url}
                              for url, hit in profiles.items()]}
    if look.get("mode") in {"empty", "no_hits", "error"}:
        if look.get("error_code") == "page_read_unavailable":
            return {"kind": "text", "body_md": "I could not read your page. Paste the relevant page text here so I can summarize it.",
                    "actions": [], "citations": []}
        return search_failure_reply(query, str(look.get("error_code") or ""))
    return {
        "kind": "text",
        "body_md": await _answer_from_search(query, look, venture_id=venture_id) or render_explore(look, pages=False),
        "actions": [],
        "citations": [{"label": p.get("title"), "href": p.get("url")} for p in (look.get("hits") or [])[:5]],
    }


def _official_named_topics(query: str) -> tuple[tuple[str, str], str] | None:
    """Split an explicit two-provider official-source question without an LLM guess."""
    first = query.split(".", 1)[0]
    match = re.search(r"\bofficial\s+([A-Z][A-Za-z0-9_-]{1,30})\s+and\s+"
                      r"([A-Z][A-Za-z0-9_-]{1,30})\s+(.+)", first)
    if not match:
        return None
    topic = re.split(r"\s+for\s+[A-Z][A-Za-z0-9_-]+\b", match.group(3), maxsplit=1)[0]
    topic = topic.strip()[:120]
    if not topic:
        return None
    return (match.group(1), match.group(2)), topic


def _provider_controls_url(url: str, provider_slug: str) -> bool:
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or not host:
            return False
        if host == "github.com":
            return parsed.path.lower().startswith("/" + provider_slug + "/")
        labels = host.split(".")
        return len(labels) >= 2 and labels[-2] == provider_slug
    except ValueError:
        return False


async def _pause(venture_id: str) -> dict[str, Any]:
    return await asyncio.to_thread(_pause_work, venture_id, stop_everything=True)


def _pause_work(venture_id: str, *, stop_everything: bool = False) -> dict[str, Any]:
    from app.ventures.lifecycle import VentureLifecycleError, pause

    cancelled = []
    try:
        if stop_everything:
            from app.planning.store import cancel_all_work

            cancelled = cancel_all_work(venture_id)
        from app.autopilot.service import update_settings

        update_settings(venture_id, god_mode=False)
        pause(venture_id, reason="Paused from chat", actor_id="chat")
        body = "Paused. Your saved tasks are kept. Nothing new will start. Say resume when you want to carry on."
    except VentureLifecycleError as exc:
        if exc.code != "already_paused":
            raise
        body = "Already paused. Nothing new will start."
    if stop_everything:
        body = (f"Stopped everything: cancelled {len(cancelled)} queued or running tasks and turned God mode off. "
                "Your saved results are kept. Nothing new will start. Say resume when you want to carry on.")
    return {
        "kind": "text",
        "body_md": body,
        "actions": [{"label": "Resume", "action": "resume", "style": "primary"}],
        "citations": [],
    }


async def _resume(venture_id: str) -> dict[str, Any]:
    from app.ventures.lifecycle import VentureLifecycleError, resume

    try:
        await resume(venture_id, actor_id="chat")
        body = "Resumed. Work can start again; nothing was approved or sent by resuming."
    except VentureLifecycleError as exc:
        if exc.code != "not_paused":
            raise
        body = "This venture is not paused, so there is nothing to resume."
    return {"kind": "text", "body_md": body,
            "actions": [{"label": "Open Now", "href": f"/v/{venture_id}/now", "style": "primary"}],
            "citations": []}


def _god_mode(venture_id: str, on: bool) -> dict[str, Any]:
    from app.autopilot import service

    settings = service.update_settings(venture_id, god_mode=on)
    if is_local():
        from app.chat.navigation_map import billing_hint

        sending = (f"You allow at most {settings['send_max_per_day']} emails a day. " if settings['allow_send']
                   else "Sending stays off until you allow it. ")
        body = ("Your requested background work is on. Your saved spending limit and send permissions still apply. "
                + sending + "Say \"turn off god mode\" or pause your venture to stop it. " + billing_hint() if on else
                "Your background work is off. Nothing new starts without your request. Ask for the next work you want in chat.")
        return {"kind": "text", "body_md": body, "citations": [],
                "actions": [{"label": "Review your spending limit", "href": "/settings/local#spend-title",
                             "kind": "recovery_link", "style": "primary"}]}
    if not on:
        body = "God mode is off. Autopilot has stopped; nothing runs without you from here."
    else:
        body = ("God mode is on (beta). I will keep moving the next steps myself while work is available, "
                "with no daily or lifetime spending cap. I never enter passwords or payment details, never send to real people, never spend "
                "real money or delete anything. Say \"turn off god mode\" or pause to stop me at once.")
    return {"kind": "text", "body_md": body, "citations": [],
            "actions": [{"label": "Autopilot timeline", "href": f"/v/{venture_id}/settings#god-mode", "style": "primary"}]}


def _cancel(venture_id: str, payload: dict[str, Any] | None) -> dict[str, Any]:
    from app.planning.store import cancel_matching_work

    query = _pending_query(venture_id, payload)
    cancelled = cancel_matching_work(venture_id, query)
    if not cancelled:
        return {
            "kind": "text",
            "body_md": "Nothing matching was open to park.",
            "actions": [{"label": "Open cockpit", "href": f"/v/{venture_id}"}],
            "citations": [],
        }
    lines = [f"Parked {len(cancelled)} item(s). That slice will not refill."]
    for row in cancelled[:6]:
        lines.append(f"- **{row.get('title')}**")
    return {
        "kind": "text",
        "body_md": "\n".join(lines),
        "actions": [{"label": "Open cockpit", "href": f"/v/{venture_id}", "style": "primary"}],
        "citations": [],
    }


def _office_hours(venture_id: str) -> dict[str, Any]:
    """Run gstack office-hours diagnostic against the current venture (read-only)."""
    from textwrap import shorten

    from app.gstack.methodology import FORCING_QUESTIONS, founder_topic
    from app.ventures.current_idea import idea_summary

    factory = db()
    with factory() as session:
        ven = session.get(Venture, venture_id)
        idea = (idea_summary(ven, session=session) if ven else "") or "this idea"
        stage = ven.stage if ven else "draft"
    brief = MEM.get(f"brief:{venture_id}") or {}
    research = brief.get("research") if isinstance(brief.get("research"), dict) else {}
    demand = research.get("demand") or "unknown"
    excerpt = "\n".join(shorten(line, width=180, placeholder="…") for line in idea.splitlines())
    lines = [
        "Let's test real demand for your idea.",
        "",
        f"Idea: {excerpt}",
        f"Stage: {stage}. Demand read: {demand}.",
        "",
        "Six questions to answer (use Clarify if onboarding is still open):",
    ]
    for spec in FORCING_QUESTIONS.values():
        prompt = str(spec["prompt"]).format(product="this product")
        lines.append(f"- **{founder_topic(spec['name'])}.** {prompt}")
    lines.append("")
    return {
        "kind": "text",
        "body_md": "\n".join(lines),
        "actions": [
            {"label": "Clarify", "href": f"/v/{venture_id}#clarify", "style": "primary"},
            {"label": "Research", "href": f"/v/{venture_id}/research"},
        ],
        "citations": [],
    }


def _ceo_review_context(venture_id: str) -> tuple[str, dict]:
    from sqlalchemy import select
    from app.db.supymem_models import ClarifierTurn, IdeaSession
    from app.ventures.current_idea import current_revision, revision_fields

    with db()() as session:
        venture = session.get(Venture, venture_id)
        if venture is None:
            raise KeyError("venture")
        idea_session = session.scalar(select(IdeaSession).where(
            IdeaSession.venture_id == venture_id, IdeaSession.workspace_id == venture.workspace_id,
            IdeaSession.state != "abandoned").order_by(IdeaSession.updated_at.desc()).limit(1))
        latest = session.scalar(select(ClarifierTurn).where(ClarifierTurn.session_id == idea_session.id)
            .order_by(ClarifierTurn.turn.desc()).limit(1)) if idea_session else None
        if idea_session and (idea_session.state in {"open", "recon", "clarifying", "validating", "proposing"}
                or idea_session.state == "reviewing" and latest and latest.kind == "question"):
            raise ValueError("idea_intake_pending")
        idea = current_idea(venture, session=session)
        if not idea.strip():
            raise ValueError("idea_intake_pending")
        revision = current_revision(venture, session)
        fields = revision_fields(revision)
        source = {"id": revision.id if revision else "saved_idea", "text": json.dumps({
            **fields, "idea": idea, "project_intent": fields.get("project_intent", "startup")},
            ensure_ascii=False)}
        return venture.workspace_id, source


async def _ceo_review(venture_id: str, payload: dict | None = None) -> dict[str, Any]:
    from app.chat.confirm import ConfirmationCard
    from app.chat.reply_safety import contains_internal_prompt, failed_reply
    from app.credits.errors import CreditError, InsufficientCredits
    from app.idea.validation import pressure_test_quote, pressure_test_review
    from core.copy import founder_copy
    from core.ids import new_id

    payload = payload or {}
    try:
        workspace_id, source = await asyncio.to_thread(_ceo_review_context, venture_id)
        quote = await asyncio.to_thread(pressure_test_quote, workspace_id, source)
        if not payload or set(payload) == {"query"} and isinstance(payload["query"], str):
            review_id = new_id("pressure")
            MEM[f"ceo_review:{venture_id}:{review_id}"] = {
                "workspace_id": workspace_id, "source": source, "quote": quote, "state": "ready"}
            card = ConfirmationCard.for_action(get_action("ceo_review"),
                interpretation="Pressure-test your saved idea and suggest one concrete next test.",
                consequences=["Uses your connected model account." if quote["byok"] else "Uses your available review budget.",
                              "No more questions. Nothing is built, sent or changed."],
                resulting_state={"review_id": review_id, "ceiling_micro": quote["ceiling_micro"]})
            return {"kind": "confirmation", "body_md": "I can review your saved idea for its biggest risks "
                    "and give you one concrete next test. Confirm to start.", "actions": [card.as_action()], "citations": []}
        review_id = payload.get("review_id")
        if (set(payload) != {"review_id", "ceiling_micro"} or not isinstance(review_id, str)
                or not review_id.startswith("pressure_") or len(review_id) > 100):
            raise ValueError("review_confirmation_changed")
        key = f"ceo_review:{venture_id}:{review_id}"

        def claim(saved):
            if (not saved or saved["workspace_id"] != workspace_id
                    or type(payload["ceiling_micro"]) is not int or payload["ceiling_micro"] != saved["quote"]["ceiling_micro"]):
                raise ValueError("review_confirmation_changed")
            if saved["state"] == "complete":
                return saved
            if saved["state"] != "ready":
                raise ValueError("review_already_attempted")
            if source != saved["source"] or quote != saved["quote"]:
                raise ValueError("review_context_changed")
            return {**saved, "state": "running"}

        saved = await asyncio.to_thread(MEM.update_value, key, claim)
        if saved["state"] == "complete":
            return saved["reply"]
        try:
            report = await pressure_test_review(workspace_id, venture_id, source, quote, review_id)
            lines = ["**Biggest risks**", "These are risks to test, not facts proved by research."]
            for risk in report.risks:
                lines.extend(["", "- " + founder_copy(risk.risk),
                              "  What would support this risk: " + founder_copy(risk.supports),
                              "  What would disprove it: " + founder_copy(risk.disproves)])
            test = report.proposed_tests[0]
            lines.extend(["", "**One concrete next test**", founder_copy(test.hypothesis),
                          founder_copy(test.action), "Pass or fail: " + founder_copy(test.success_measure)])
            reply = {"kind": "text", "body_md": "\n".join(lines), "actions": [], "citations": []}
            if contains_internal_prompt(reply):
                raise ValueError("review_internal_prompt")
            await asyncio.to_thread(MEM.update_value, key, lambda row: {**row, "state": "complete", "reply": reply})
            return reply
        except Exception:
            await asyncio.to_thread(MEM.update_value, key, lambda row: {**row, "state": "interrupted"})
            raise
    except Exception as exc:
        logging.getLogger(__name__).exception("ceo_review_failed", extra={"venture_id": venture_id})
        if isinstance(exc, ValueError) and str(exc) == "idea_intake_pending":
            return {"kind": "needs_you", "body_md": "Finish saving your idea here in chat first. "
                    "Then I can pressure-test it without more questions. No review has started.",
                    "actions": [{"label": "Continue idea review", "action": "idea.review", "style": "primary", "retry": True}], "citations": []}
        failure = failed_reply(action="ceo_review", payload={})
        if isinstance(exc, CreditError):
            from app.credits.errors import LIMIT_MESSAGE
            failure["body_md"] = (LIMIT_MESSAGE if isinstance(exc, InsufficientCredits)
                                  else "I could not start your review right now. Please try again.")
            failure["actions"].append({"label": "Buy more" if isinstance(exc, InsufficientCredits) else "Open settings",
                                      "href": f"/v/{venture_id}/settings#settings-credits" if isinstance(exc, InsufficientCredits) else f"/v/{venture_id}/settings"})
        return failure


def _brain_name(query: str) -> str:
    import re

    raw = (query or "").strip()
    match = re.search(
        r"(?:know about|brief me (?:on|about)|prepare me for|works at|founded|invested in)\s+(.+?)[\?.!]*$",
        raw,
        re.I,
    )
    if match:
        return match.group(1).strip(" .")
    match = re.search(r"remember (?:that|this)\s+(.+)", raw, re.I)
    if match:
        return match.group(1).strip()
    return raw


async def _brain_brief(venture_id: str, query: str) -> dict[str, Any]:
    from app.chat.ground import answer_from_memory
    from app.ventures.founder_memory import MemoryWriteError, infer_remember_kind, remember
    from app.ventures.state_brief import invalidate_venture_state_brief

    raw = (query or "").strip()
    if raw.lower().startswith("remember "):
        fact = _brain_name(raw)
        kind = infer_remember_kind(fact)
        from app.chat.store import list_messages
        from app.chat.store import _clean
        messages = await asyncio.to_thread(list_messages, venture_id, limit=30)
        source = next((row for row in reversed(messages)
                       if row.get("role") in {"user", "founder"}
                       and str(row.get("body_md") or "").strip() == _clean(raw)), None)
        if source is None:
            return {"kind": "text", "body_md": "I could not bind that memory to a saved founder message. Please send the complete remember instruction again.", "citations": []}
        fact = _brain_name(source["body_md"])
        try:
            memory_id = await remember(
                venture_id=venture_id,
                fact=fact,
                provenance="chat:" + source["id"],
                kind=kind,
            )
        except MemoryWriteError as exc:
            return {"kind": "text", "body_md": exc.message, "citations": []}
        await asyncio.to_thread(invalidate_venture_state_brief, venture_id)
        confirm = _remember_confirm_copy(fact, kind)
        return {
            "kind": "text",
            "body_md": confirm,
            "citations": [{"label": fact, "href": f"#memory:{memory_id}"}],
        }
    name = _brain_name(raw)
    if not name:
        return await answer_from_memory(venture_id, "What venture context is saved?")
    try:
        from app.gbrain.briefing import brief_entity
    except ModuleNotFoundError as exc:
        if exc.name != "app.gbrain":
            raise
        return await answer_from_memory(venture_id, raw)
    return await brief_entity(venture_id=venture_id, name=name, question=raw)


def _remember_confirm_copy(fact: str, kind: str) -> str:
    """Confirm in the assistant's voice; never echo the founder's first person."""
    lower = (fact or "").lower()
    if kind == "preference" or "short" in lower:
        if "short" in lower:
            return "Got it, I'll keep my answers short."
        return "Got it, I'll keep that preference in mind."
    if kind == "decision":
        return "Got it, I've saved that decision."
    return "Got it, I've saved that."


def _pending_query(venture_id: str, payload: dict[str, Any] | None) -> str:
    payload = payload or {}
    query = str(payload.get("query") or "").strip()
    if query:
        return query
    pending = MEM.get(pending_key(venture_id))
    if pending:
        return str(pending)
    from app.chat.store import list_messages

    for message in reversed(list_messages(venture_id, limit=30)):
        if message.get("role") in {"user", "founder"}:
            return str(message.get("body_md") or "")
    return ""


async def _approve(venture_id: str, payload: dict[str, Any] | None, actor: str | None = None) -> dict[str, Any]:
    return await asyncio.to_thread(_approve_decision, venture_id, payload, actor)


def _approve_decision(venture_id: str, payload: dict[str, Any] | None, actor: str | None = None) -> dict[str, Any]:
    from app.chat import decisions
    from app.chat.intent import classify_intent

    query = _pending_query(venture_id, payload)
    # Old confirmation cards may remain in chat history after intent routing
    # is repaired. Recheck the founder's exact utterance at execution time so
    # a card created from "do not build or approve" cannot approve anything.
    if classify_intent(query) != "approve":
        return {
            "kind": "text",
            "body_md": "No approval decision was recorded. Ask me what needs review, or explicitly say approve or reject when you are ready.",
            "actions": [{"label": "Open cockpit", "href": f"/v/{venture_id}"}],
            "citations": [],
        }
    out = decisions.decide_one(venture_id, query, str((payload or {}).get("approval_id") or ""), actor)
    MEM.pop(pending_key(venture_id), None)
    return out


def _rename_manager(venture_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    from app.org.manager import set_manager_name

    name = str(payload.get("name") or "").strip()
    try:
        saved = set_manager_name(venture_id, name)
    except ValueError:
        return {"kind": "text", "body_md": "That name won't work. Use letters, spaces and . ' - only, up to 40 "
                "characters. My name was not changed.", "actions": [], "citations": []}
    return {"kind": "text", "body_md": f"Done. I'm {saved} now, in the chat header and in everything I say.",
            "actions": [], "citations": []}



def _slate_override(venture_id: str, payload: dict[str, Any] | None) -> dict[str, Any]:
    from app.planning.store import save_tasks
    from app.will.override import parse_override
    from app.will.slate import generate_slate, latest_slate
    from app.state import MEM as mem

    query = _pending_query(venture_id, payload)
    factory = db()
    with factory() as session:
        ven = session.get(Venture, venture_id)
        idea = (current_idea(ven, session=session) if ven else "") or None
    slate = latest_slate(venture_id) or generate_slate(
        venture_id, because="chat override", idea=idea
    )
    parsed = parse_override(query, slate)
    if parsed.unclear and not parsed.edits:
        return {
            "kind": "text",
            "body_md": (parsed.interpretation + (
                " " + " ".join(parsed.unclear) if parsed.unclear else "")).replace("slate", "task list"),
            "actions": [{"label": "Open tasks", "href": f"/v/{venture_id}/tasks"}],
            "citations": [],
        }
    result = parsed.resulting_slate.model_copy(
        update={"founder_edited": True, "founder_edit_transcript": query[:500]}
    )
    dumped = result.model_dump(mode="json")
    mem[f"slate:{result.id}"] = dumped
    mem[f"slate_latest:{venture_id}"] = dumped
    tasks = [
        {
            "title": item.title,
            "detail": item.expected_outcome,
            "why_now": item.why_now,
            "tags": [item.origin, item.capability_id],
            "est_minutes": {"minutes": 20, "hours": 60, "days": 240}.get(item.effort, 60),
            "runnable": not item.blocked_by,
            "rank": item.rank,
        }
        for item in result.items
    ]
    saved = save_tasks(venture_id, tasks, source="override")
    mem.pop(pending_key(venture_id), None)
    lines = [parsed.interpretation.replace("slate", "task list"), "", "**Updated next five**"]
    for i, task in enumerate(saved[:5], 1):
        owner = f", {task['owner_name']}" if task.get("owner_name") else ""
        lines.append(f"{i}. {task['title']}{owner}")
    if parsed.autopilot_on:
        lines.append("")
        lines.append("Autopilot flag noted, I still will not act without a Run.")
    return {
        "kind": "text",
        "body_md": "\n".join(lines),
        "actions": [{"label": "Open tasks", "href": f"/v/{venture_id}/tasks", "style": "primary"}],
        "citations": [],
    }


async def _social_post(venture_id: str, payload: dict[str, Any] | None, actor_subject: str | None = None) -> dict[str, Any]:
    from app.chat import posts

    payload = payload or {}
    query = await asyncio.to_thread(_pending_query, venture_id, payload)
    # The founder's exact words or the draft they clicked; never the instruction itself.
    content = str(payload.get("content") or "").strip()
    platforms = [str(p) for p in payload.get("platforms") or [] if isinstance(p, str)]
    if not content:
        content, platforms = posts.post_request(query)
    await asyncio.to_thread(MEM.pop, pending_key(venture_id), None)
    if not content:
        return {"kind": "text", "body_md": "There was no post text to publish, so nothing was posted. Tell me the "
                "exact text, for example: post to LinkedIn: your text.", "actions": [], "citations": []}
    subreddit = posts._SUBREDDIT.search(query or "")
    return await posts.publish(venture_id, content, platforms, actor_subject, target=subreddit[1] if subreddit else None)


def _social_guide(venture_id: str, payload: dict[str, Any] | None) -> dict[str, Any]:
    from app.chat import posts

    return posts.guide_reply()


async def _social_status(venture_id: str) -> dict[str, Any]:
    from app.chat import posts

    return await asyncio.to_thread(posts.status_reply, venture_id)


def _social_connect(venture_id: str, payload: dict[str, Any] | None) -> dict[str, Any]:
    from app.chat import posts

    _ = payload
    MEM.pop(pending_key(venture_id), None)
    return posts.status_reply(venture_id)
