"""Founder-blocker guides: what is blocked, and the exact clicks that clear it.

Every needs_you reply carries an "Explain in detail" button (``with_explain``);
clicking it runs the non-consequential ``blocker.explain`` action, which posts
the guide below as numbered steps plus deep-link buttons. Labels here must
match the console UI; ``{vid}`` is the venture id.
"""
from __future__ import annotations

import re
from typing import Any

_SCREEN = "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture"
_AX = "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"
_AUTO = "x-apple.systempreferences:com.apple.preference.security?Privacy_Automation"


def _g(title: str, why: str, steps: list[str], actions: list[tuple[str, str]]) -> dict[str, Any]:
    return {"title": title, "why": why, "steps": steps,
            "actions": [{"label": a, "href": h} for a, h in actions]}


GUIDES: dict[str, dict[str, Any]] = {
    "idea_review_failed": _g(
        "Your independent idea review did not finish",
        "Your saved idea is unchanged. No complete independent review was accepted.",
        ["Retry your independent review from chat.",
         "Or choose Do it another way to ask three people to review your saved idea separately.",
         "Bring their separate notes back to chat; these are your notes, not a completed AI review."],
        [("Review your idea", "/v/{vid}/idea")]),
    "insufficient_credits": _g(
        "You have reached your usage limit",
        "Open Settings to get more usage, then retry. Your saved work is kept.",
        ["Open Settings (button below).",
         "Use the account usage section to get more.",
         "If purchases are not open, request more usage for the team to review.",
         "After more usage is available, come back to chat and retry."],
        [("Open Settings", "/v/{vid}/settings"), ("Open account usage", "/account")]),
    "computer_use_permissions": _g(
        "Mac permissions are missing",
        "Supymem can only see and drive an app on your Mac after you turn on macOS privacy switches. Only you can do this.",
        ["Open **Mac settings** (button below) and click **Request Screen Recording**, then **Request Accessibility**.",
         "If macOS shows no prompt, click **Open settings**, or use the System Settings buttons below.",
         "In **Privacy & Security → Screen & System Audio Recording**, switch on Supymem. Do the same under **Accessibility**.",
         "For local builds in Cursor, also switch on Supymem under **Automation**.",
         "macOS may ask you to quit and reopen Supymem. Do that.",
         "Back in Supymem, click **Check again**. Each row should read Ready."],
        [("Open Mac settings", "/v/{vid}/settings#settings-mac"), ("Screen Recording", _SCREEN),
         ("Accessibility", _AX), ("Automation", _AUTO)]),
    "mac_not_connected": _g(
        "This Mac is not connected",
        "Mac tasks and local builds run through the Supymem app on your Mac, signed in to your account.",
        ["If Supymem just opened, wait about 30 seconds for its local helper to start, then retry.",
         "Open the Supymem app on your Mac (not the website).",
         "Open **Account** (button below) and find **Desktop connection**.",
         "Click **Sign in**. Your browser opens; sign in and approve this Mac. Opening the browser alone does not finish it.",
         "If it says a new sign-in is needed, click **Reconnect this account** instead.",
         "When it reads connected, come back and retry."],
        [("Open Account", "/account")]),
    "no_hosted_app": _g(
        "There is no app or build yet",
        "Nothing has been built for this venture, so there is nothing to preview or publish.",
        ["Open Builds (button below).",
         "If there is no product yet, type a name and slug and click **Save product draft**.",
         "Make sure the execution plan is approved (see the plan guide).",
         "Click **Review build estimate**, check the milestone, then **Start reviewed build**."],
        [("Open Builds", "/v/{vid}/builds"), ("Open plan", "/v/{vid}/plan")]),
    "build_not_accepted": _g(
        "The build is not accepted yet",
        "A landing page or app can only be published after a build has finished and independent QA passed.",
        ["Open Builds (button below) and pick the run under **Saved build runs**.",
         "Read the QA line. Anything other than passed means it is not publishable.",
         "If it failed, read the saved evidence, then click **Review build estimate** and start a new reviewed build.",
         "Once QA passes, open Site and click **Publish accepted build**, then **Publish landing page**."],
        [("Open Builds", "/v/{vid}/builds"), ("Open Site", "/v/{vid}/site")]),
    "waitlist_off": _g(
        "The waitlist is off",
        "Visitors have no way to sign up until the waitlist is enabled.",
        ["Open Site (button below).",
         "Find **Give visitors a next step**.",
         "Click **Enable waitlist**.",
         "Click **Test public submission** and wait for it to report verified."],
        [("Open Site", "/v/{vid}/site")]),
    "waitlist_test_failed": _g(
        "The waitlist test has not passed",
        "Publishing is held until a public signup is proven to reach storage.",
        ["Open Site (button below) and find **Give visitors a next step**.",
         "Click **Test public submission**. If it is greyed out, wait for the countdown.",
         "If it fails, click **Refresh waitlist status**, then test again.",
         "When it reads verified, publish from the same page."],
        [("Open Site", "/v/{vid}/site")]),
    "verified_contact_missing": _g(
        "A verified contact is missing",
        "Outreach and notifications need an email address you have verified.",
        ["Check your inbox for the verification email from Supymem.",
         "Click the link in it. If you cannot find it, sign in and use the **Verify your email** screen to resend it.",
         "Come back and retry."],
        [("Open Account", "/account")]),
    "connection_missing": _g(
        "A connection is missing",
        "This step needs an outside account (Gmail, posting, GitHub, payments) that is not connected.",
        ["Open Connections (button below).",
         "Under **Connect a system**, choose the provider and sign in on its own site.",
         "Confirm it shows connected, then retry."],
        [("Open Connections", "/v/{vid}/connections")]),
    "coding_provider_missing": _g(
        "No coding provider on this Mac",
        "Local builds run Claude Code or Codex on your Mac. Neither was found.",
        ["Install Claude Code or Codex on this Mac.",
         "Open Settings (button below) and check the local coding tools section.",
         "Retry the build."],
        [("Open Settings", "/v/{vid}/settings")]),
    "validation_incomplete": _g(
        "Idea validation is not finished",
        "Research only starts once the idea is validated and confirmed.",
        ["Open Idea (button below).",
         "Answer any open clarification questions.",
         "Wait for validation to finish, and read the findings.",
         "Accept them, then click **Continue to research**."],
        [("Open Idea", "/v/{vid}/idea")]),
    "research_incomplete": _g(
        "Research is not finished",
        "Planning needs research findings first.",
        ["Open Research (button below).",
         "If it failed, read the message and click **Run research again**.",
         "If it is running, wait; a chat message appears when it is done."],
        [("Open Research", "/v/{vid}/research")]),
    "plan_needs_approval": _g(
        "The plan needs your approval",
        "Nothing is built or run from a plan until you approve it.",
        ["Ask me **what is the plan?** here to read it, including its open questions and limits.",
         "Say **approve the plan** to continue.",
         "To change it first, tell me what to change, for example **change the plan to focus on waitlist signups**."],
        [("Open Plan", "/v/{vid}/plan")]),
    "founder_only_work": _g(
        "This task is yours to do",
        "Some work (customer interviews, calls, decisions) cannot be done by an agent.",
        ["Open Tasks (button below) and open the task waiting on you.",
         "Do it yourself.",
         "Record what you learned and mark it done so planning can continue."],
        [("Open Tasks", "/v/{vid}/tasks")]),
    "llm_unavailable": _g(
        "The AI model is unavailable",
        "The model provider rejected or failed the call.",
        ["Open Settings (button below) and find **How this company thinks**.",
         "Re-enter your own key and test the connection.",
         "Wait a minute if it was rate limited, then retry."],
        [("Open Settings", "/v/{vid}/settings")]),
    "session_expired": _g(
        "You need to sign in again",
        "This Mac's sign-in expired or does not match your account.",
        ["Open Account (button below).",
         "Under **Desktop connection**, click **Reconnect this account** (or **Sign in**).",
         "Approve in the browser that opens.",
         "If it says the account does not match, click **Sign out this Mac** first, then connect the right account."],
        [("Open Account", "/account")]),
    "mac_session_unavailable": _g(
        "The Mac session could not start",
        "Supymem could not start its computer-use session on this Mac. Nothing was done.",
        ["Quit Supymem from the menu bar and reopen it.",
         "Wait about 30 seconds, then open Account and check **Desktop connection** reads connected.",
         "Prepare the Mac task again."],
        [("Open Account", "/account"), ("Open Mac settings", "/v/{vid}/settings#settings-mac")]),
    "mac_field_mismatch": _g(
        "A text field did not take what was typed",
        "Supymem could not clear the field, or what it typed did not match, so it stopped instead of typing into old text.",
        ["Open the app on your Mac and look at the field.",
         "Clear or fix the text yourself.",
         "Prepare a new Mac task with a simpler, more specific goal."],
        [("Review Mac tasks", "/v/{vid}/device-work-orders")]),
    "mac_repeated_action": _g(
        "The Mac task was going in circles",
        "Supymem repeated the same step without progress, so it stopped.",
        ["Open the app on your Mac and check where it got stuck.",
         "Do the stuck step yourself if it is quick.",
         "Prepare a new Mac task with a simpler, more specific goal."],
        [("Review Mac tasks", "/v/{vid}/device-work-orders")]),
    "mac_task_stopped": _g(
        "The Mac task stopped",
        "The reviewed window changed or closed, an action could not be confirmed, or the task was stopped. Nothing more was done.",
        ["Open the app on your Mac and check its current state.",
         "Open the saved task to read exactly what was recorded.",
         "Prepare a fresh Mac task for the current window."],
        [("Review Mac tasks", "/v/{vid}/device-work-orders")]),
    "browser_decision_unavailable": _g(
        "The browser task could not get a usable decision",
        "Supymem stopped because its browser decision was unavailable. The saved result and final usage still need review.",
        ["Open the saved browser task to read its result and status.",
         "A stopped task is not confirmation that the requested work completed.",
         "If usage or an action is unconfirmed, reconcile that outcome before preparing another task."],
        [("Review Mac tasks", "/v/{vid}/device-work-orders")]),
    "mac_unresponsive": _g(
        "Your Mac stopped responding",
        "The connection timed out. Your saved progress is kept, but the expired task cannot act.",
        ["Open your Mac app on the same Mac and review the saved task.",
         "Check any unconfirmed action or usage before retrying.",
         "Resume saved progress if the task is still within its time limit, or prepare a fresh review."],
        [("Review Mac tasks", "/v/{vid}/device-work-orders")]),
    "mac_blocked": _g(
        "Your Mac task was blocked",
        "A safety check or required permission prevented the task from continuing.",
        ["Open the saved task to see which permission or reviewed limit blocked it.",
         "Grant only the access you intend, or choose a task within the reviewed limits.",
         "Review any unconfirmed action or usage before preparing another task."],
        [("Review Mac tasks", "/v/{vid}/device-work-orders")]),
    "mac_timeout": _g(
        "Your Mac task reached its time limit",
        "The allowed time or step limit ran out before the result was confirmed.",
        ["Review the saved result to see how far your task got.",
         "Check any unconfirmed action or usage before retrying.",
         "Prepare a fresh task with a smaller, more specific goal."],
        [("Review Mac tasks", "/v/{vid}/device-work-orders")]),
    "mac_auth_lost": _g(
        "You need to sign in again",
        "Your browser session or account changed, so the task could not continue.",
        ["Sign in to the intended account on your Mac without sharing your password here.",
         "Review the saved task and any unconfirmed action or usage.",
         "Prepare a fresh task for the account you are signed in to."],
        [("Review Mac tasks", "/v/{vid}/device-work-orders")]),
    "mac_model_unavailable": _g(
        "Your Mac task could not reach the AI model",
        "The AI model could not provide a usable decision, so the task could not continue.",
        ["Review the saved result and status before retrying.",
         "Check any unconfirmed action or usage first.",
         "Try again after the model is available, or do the task yourself."],
        [("Review Mac tasks", "/v/{vid}/device-work-orders")]),
    "mac_needs_founder": _g(
        "Your Mac task needs your review",
        "The result could not be confirmed. A closed task is not proof that your goal was completed.",
        ["Open the saved result and check what happened on your Mac.",
         "Check any unconfirmed action or usage before retrying.",
         "Prepare a fresh reviewed task, or finish the remaining steps yourself."],
        [("Review Mac tasks", "/v/{vid}/device-work-orders")]),
    "browser_reply_unusable": _g(
        "Your browser model returned an incomplete or invalid reply",
        "Your task stopped after one compact retry without authorizing an action from an unusable reply. Saved results are kept.",
        ["Review your saved browser task and status before preparing another task.",
         "A stopped task does not confirm completion. Reconcile any unconfirmed usage or action first.",
         "Check your saved model settings before preparing a fresh task."],
        [("Review Mac tasks", "/v/{vid}/device-work-orders")]),
    "stuck": _g(
        "Getting unstuck",
        "Every venture stalls sometimes. There is always one step that unblocks the rest.",
        ["Open Now (button below) and read the one highlighted step.",
         "If it says it is waiting on you, do that step. If it says work is running, wait for the chat message.",
         "If nothing is highlighted, ask me **what's next?**",
         "If a step keeps failing, check your AI settings, then retry it."],
        [("Open Now", "/v/{vid}/now"), ("Open Settings", "/v/{vid}/settings")]),
    "job_failed": _g(
        "Your work did not finish",
        "Your saved work is unchanged.",
        ["Open the work from the earlier message to try again."],
        [("Open Settings", "/v/{vid}/settings")]),
}


# One vocabulary: registry codes are canonical; mac_offers and #616 cu_* codes alias into them.
ALIASES: dict[str, str] = {
    "cu_permission_missing": "computer_use_permissions",
    "mac_screen_recording": "computer_use_permissions",
    "mac_accessibility": "computer_use_permissions",
    "cu_bridge_not_ready": "mac_not_connected",
    "cu_session_unavailable": "mac_session_unavailable",
    "cu_browser_decision_unavailable": "browser_decision_unavailable",
    "cu_browser_reply_unusable": "browser_reply_unusable",
    "cu_field_not_cleared": "mac_field_mismatch",
    "cu_field_value_mismatch": "mac_field_mismatch",
    "cu_repeated_action": "mac_repeated_action",
    "cu_window_changed": "mac_task_stopped",
    "cu_effect_unverified": "mac_task_stopped",
    "cu_reconcile_unavailable": "mac_task_stopped",
    "cu_stopped": "mac_task_stopped",
    "cu_mac_unresponsive": "mac_unresponsive",
    "cu_blocked": "mac_blocked",
    "cu_timeout": "mac_timeout",
    "cu_auth_lost": "mac_auth_lost",
    "cu_model_unavailable": "mac_model_unavailable",
    "cu_needs_founder": "mac_needs_founder",
    "password_required": "founder_only_work",
    "payment_required": "founder_only_work",
    "two_factor_code": "founder_only_work",
}


_ERROR_COPY = {
    "exploration_direction_changed": "Your idea changed while this round was being prepared. Review your latest idea before starting a new round.",
    "provider_down": "Your AI service did not respond. Your saved work is unchanged. Try again in a moment.",
    "research_provider_timed_out": "The research service took too long to respond. Your saved work is kept.",
    "qa_failed": "The build did not pass its final checks.",
    "handler_failed": "Your work didn't finish. Your saved work is kept. Ask me to try again here.",
    "pagebuilder_renderer_unavailable": "The landing page preview could not start. Your idea is saved. Try building again.",
    "pagebuilder_generation_failed": "The landing page could not be built. Your idea is saved. Try building again.",
    "pagebuilder_model_exceeds_limit": "Your selected model exceeds the limit for a page build. Your idea is saved. Open Settings and choose a supported model for your page, then try building again.",
    "pagebuilder_provider_or_credits_unavailable": "Your page build setup is not ready. Your idea is saved. Open Settings and check your model connection before trying again.",
    "public_search_unavailable": "Public search is busy. Retry search in a moment.",
    "no_public_candidates": "No public company pages matched this audience. Refine the audience or add a contact you know.",
    "clarification_invalid_output": "The step that turns your answers into the next question returned something "
                                    "I could not use, so it stopped. Your answers are saved.",
    "clarification_insufficient_credits": "You have reached your usage limit. Your answers are saved. Open Settings to get more, then continue.",
    "clarification_credits_unavailable": "The next question could not start within your current usage limit. Your answers are saved.",
    "clarification_credit_reservation_unavailable": "The next question could not start within your current usage limit. Your answers are saved.",
    "clarification_byok_unavailable": "Your selected AI connection is unavailable. Check it in Settings. Your answers are saved.",
    "clarification_account_suspended": "Your account can no longer continue this work. Check company access. Your answers are saved.",
    "clarification_outcome_uncertain": "The AI request ended without a confirmed result, so nothing retried on its own. Your answers are saved.",
    "job_outcome_uncertain": "I couldn't confirm what finished. Check your saved work before trying again. Nothing was retried.",
    "clarification_provider_offline": "AI requests are paused in this environment. Your answers are saved.",
    "clarification_context_limit": "The conversation is too long for the next question. Shorten the idea into a new revision.",
    "research_credits_unavailable": "Research could not start within your current usage limit. Your saved work is kept.",
    "research_byok_unavailable": "Your selected AI connection is unavailable. Check it in Settings before researching again.",
    "research_provider_offline": "AI requests are paused in this environment. Your saved reports are kept.",
    "research_invalid_output": "The research step returned a result I could not use, so it stopped. "
                               "Nothing was saved as a finding.",
    "research_provider_rejected": "The AI service turned down the research request, so it stopped.",
    "research_boundary_blocked": "A source contained data we cannot send for review. Your saved sources are kept. Run research again.",
    "research_provider_rate_limited": "The AI service was too busy to finish the research. Waiting a minute helps.",
    "research_insufficient_credits": "You have reached your usage limit. Open Settings to get more, then retry.",
    "research_credentials_unavailable": "Research could not reach the AI service with your current settings.",
    "research_call_budget_exhausted": "Research used up its call budget before it could finish.",
    "research_lease_lost": "The research run was interrupted before it finished.",
    "research_outcome_uncertain": "The research run ended without a result I can trust.",
    "research_saved_sources_unavailable": "The sources saved for this research could not be read back.",
    "plan_invalid_output": "The AI's plan failed our checks, so nothing was saved. Your answers and research are kept; "
                           "Retry plan asks again with the exact fields to fix.",
    "plan_outcome_uncertain": "The planning run ended without a confirmed result, usually because the AI took too long. "
                              "Your answers and research are kept; nothing retries on its own.",
    "plan_inputs_changed": "Your idea or research changed while the plan was being written, so it was dropped.",
    "plan_lease_lost": "The planning run was interrupted before it finished.",
    "plan_research_required": "A plan needs finished research first.",
    "validation_revision_superseded": "Your idea changed while validation was running, so that run was dropped.",
    "validation_in_progress": "A validation run is already in progress.",
    "desktop_app_launch_unconfirmed": "Supymem opened the app on your Mac but could not confirm it came up, so it stopped.",
    "desktop_app_launch_unavailable": "Supymem could not open that app on your Mac.",
    "desktop_app_launch_failed": "The app did not open on your Mac.",
    "desktop_app_window_unavailable": "The app has no window Supymem can work with right now.",
    "desktop_app_identity_changed": "A different app came to the front, so Supymem stopped.",
    "desktop_window_identity_changed": "The reviewed window changed or closed, so Supymem stopped.",
    "desktop_fresh_observation_required": "Supymem lost sight of the screen and stopped instead of guessing.",
    "desktop_element_not_actionable": "Supymem could not click the item it was aiming at, so it stopped.",
    "desktop_ocr_unavailable": "Supymem could not read the text on the screen.",
    "desktop_driver_missing": "The Mac helper Supymem needs is not installed.",
    "desktop_driver_not_installed": "The Mac helper Supymem needs is not installed.",
    "desktop_cua_session_unavailable": "The Mac session could not start. Nothing was done.",
    "desktop_driver_session_unavailable": "The Mac session could not start. Nothing was done.",
    "desktop_cua_transport_unavailable": "Supymem lost contact with the Mac helper. Nothing more was done.",
    "desktop_driver_transport_unavailable": "Supymem lost contact with the Mac helper. Nothing more was done.",
    "desktop_cua_effect_uncertain": "Supymem could not confirm its last step worked, so it stopped.",
    "desktop_foreground_handoff_unconfirmed": "Supymem could not confirm the app came to the front, so it stopped.",
    "desktop_snapshot_changed": "The screen changed under Supymem, so it stopped.",
    "desktop_verification_required": "Supymem needs to check the screen again before it can continue.",
}
_CODE_TOKEN = re.compile(r"\b(?:desktop|native|cu|mac)_[a-z0-9]+(?:_[a-z0-9]+)*\b")


def error_copy(code: object, *, credits_restored: bool = False) -> str | None:
    """Plain founder copy for a known stored error code, else None."""
    from app.core.jobs import LOCAL_MANAGED_BUILD_CODE, LOCAL_MANAGED_BUILD_MESSAGE

    text = " ".join(str(code or "").split()).partition(":")[0]
    from app.core.local_mode import is_local

    if is_local():
        from app.llm.provider_errors import local_provider_next_step

        if text == "clarification_provider_bad_api_key":
            return "Your answers are saved. " + local_provider_next_step("bad_api_key")
        if text in {"clarification_provider_offline", "clarification_byok_unavailable", "research_provider_offline", "research_byok_unavailable"}:
            return "Your work is saved. " + local_provider_next_step("not_configured")
        if text in {"clarification_outcome_uncertain", "clarification_provider_provider_error", "discovery_outcome_uncertain", "research_outcome_uncertain"}:
            return "You do not have a confirmed result. Your work is saved. " + local_provider_next_step() + " Check your saved progress before trying again."
        if text in {"discovery_provider_rejected", "research_provider_rejected"}:
            return "Your AI provider turned down this request. Your work is saved. " + local_provider_next_step()
    if text == LOCAL_MANAGED_BUILD_CODE:
        return LOCAL_MANAGED_BUILD_MESSAGE
    if credits_restored and text.endswith("_insufficient_credits"):
        return "You can continue now. Continue from your saved work."
    if text == "insufficient_credits" or text.endswith("_insufficient_credits"):
        from app.credits.errors import LIMIT_MESSAGE

        return LIMIT_MESSAGE
    # Older rows have these exact plain summaries followed by diagnostics.
    text = {"provider down": "provider_down", "research provider timed out": "research_provider_timed_out"}.get(
        text.partition(";")[0].strip(), text)
    text = ALIASES.get(text, text)
    if text.startswith("handler_failed"):
        return _ERROR_COPY["handler_failed"]
    if text in _ERROR_COPY:
        return _ERROR_COPY[text]
    if re.fullmatch(r"(?:pagebuilder|qa|subscription_static)_[a-z_]{1,80}", text):
        from app.tasks.contracts import blocker
        problem = blocker(text)
        return problem["reason"] + " " + problem["unblock_action"]
    if re.fullmatch(r"desktop_cua_effect_uncertain_[a-z]+", text):
        return _ERROR_COPY["desktop_cua_effect_uncertain"]
    if (m := re.fullmatch(r"([a-z]+(?:_[a-z]+)*)_invalid_output", text)):
        return f"The {m.group(1).replace('_', ' ')} step returned something I could not use, so it stopped."
    return None


def humanize_error(raw: object) -> str:
    """Founder-facing copy for a stored error code; a raw snake_case code never reaches the chat."""
    text = " ".join(str(raw or "").split())
    code = ALIASES.get(text.partition(":")[0], text.partition(":")[0])
    if (known := error_copy(code)) is not None:
        return known
    if code in GUIDES:
        return f"{GUIDES[code]['title']}. {GUIDES[code]['why']}"
    # Unrecognized exception text is untrusted and can contain IDs or values.
    return "This step did not finish. Your saved work is kept. Review the result and try again."


def humanize_codes(text: str, *, include_failures: bool = False) -> str:
    """Replace machine error tokens (desktop_*, native_*, cu_*, mac_*) inside a chat line with plain copy."""
    def swap(m: re.Match[str]) -> str:
        known = error_copy(m.group(0))
        if known is None and (guide := guide_code(m.group(0))):
            known = GUIDES[guide]["title"]
        return known.rstrip(".") if known else m.group(0).replace("_", " ")
    text = _CODE_TOKEN.sub(swap, text or "")
    if include_failures:
        # Old assistant rows may predate mapped worker errors. Never apply this
        # to founder messages or exact saved-memory quotations.
        text = re.sub(r"\bhandler[ _]failed(?:[ _][a-z]+)*(?::[A-Za-z][A-Za-z0-9_]*)?",
                      _ERROR_COPY["handler_failed"].rstrip("."), text, flags=re.I)
        text = re.sub(r"\bjob_[A-Za-z0-9_]+\b", "saved work", text)
        text = re.sub(r"\b[A-Za-z][A-Za-z0-9_]*(?:Error|Exception|Violation)\b", "an unexpected problem", text)
    return text


def guide_code(code: str | None) -> str | None:
    code = ALIASES.get(str(code or ""), str(code or ""))
    return code if code in GUIDES else None


def with_explain(reply: dict[str, Any], code: str, **extra: str) -> dict[str, Any]:
    """Append the always-present 'Explain in detail' button to a needs_you reply.

    ``extra`` (for example ``job_kind`` and ``job_code`` of a failed job) rides in the button
    payload so the explanation can name the real error instead of a generic one.
    """
    code = guide_code(code) or code
    actions = list(reply.get("actions") or [])
    if not any(a.get("action") == "blocker.explain" for a in actions):
        actions.append({"label": "Explain in detail", "action": "blocker.explain",
                        "payload": {"code": code, **extra}, "style": "ghost"})
    return {**reply, "actions": actions}


def explain(venture_id: str, payload: dict[str, Any] | None) -> dict[str, Any]:
    from app.core.local_mode import is_local

    if is_local() and guide_code((payload or {}).get("code")) == "mac_not_connected":
        return {"kind": "text", "citations": [],
                "body_md": "Your app could not reach its local helper. Quit and restart your app, wait for it to finish starting, "
                    "then retry. If it still cannot connect, check that your local data folder is writable and has free space, then restart "
                    "your app again. Open Settings > Keys and connections to check your saved settings after it reconnects.",
                "actions": [{"label": "Open Keys and connections", "href": "/settings/local",
                             "style": "primary", "kind": "recovery_link"}]}
    if (payload or {}).get("step") in {"idea.board", "ceo_review"}:
        from app.chat.recovery import review_failure_reply
        return review_failure_reply()
    if guide_code((payload or {}).get("code")) == "insufficient_credits":
        from app.credits.errors import LIMIT_MESSAGE

        return {"kind": "text", "citations": [], "action_started": False,
                "body_md": LIMIT_MESSAGE,
                "actions": [{"label": "Buy more", "href": f"/v/{venture_id}/settings#settings-credits",
                             "style": "primary", "kind": "recovery_link"}]}
    if (payload or {}).get("job_kind"):
        from app.chat.failed_jobs import explain_failure

        return explain_failure(venture_id, str(payload["job_kind"]), str(payload.get("job_code") or ""))
    code = guide_code((payload or {}).get("code")) or ""
    if code == "llm_unavailable":
        from app.llm import founder_provider
        from app.db.models import Venture
        from app.state import db

        with db()() as session:
            venture = session.get(Venture, venture_id)
            workspace_id = venture.workspace_id if venture else None
        if not workspace_id or founder_provider.resolve_llm_config(workspace_id).source != "founder_override":
            return {"kind": "text", "citations": [], "actions": [],
                "body_md": "Charlie is taking longer than usual. You can use Try again on your failed reply. "
                    "Any uncertain model charge is covered by us."}
    raw_code = (payload or {}).get("code")
    if isinstance(raw_code, str) and raw_code.startswith("pagebuilder_"):
        if known := error_copy(raw_code):
            if raw_code in {"pagebuilder_model_exceeds_limit", "pagebuilder_provider_or_credits_unavailable",
                            "pagebuilder_provider_offline", "pagebuilder_byok_unavailable", "pagebuilder_pricing_unavailable",
                            "pagebuilder_credit_reservation_unavailable", "pagebuilder_provider_rejected"}:
                return {"kind": "text", "citations": [],
                    "body_md": known if raw_code in _ERROR_COPY else "Your page build setup is not ready. Your idea is saved. Open Settings and check your model connection before trying again.",
                    "actions": [{"label": "Review your page setup", "href": f"/v/{venture_id}/settings",
                                 "style": "primary", "kind": "recovery_link"}]}
            return {"kind": "text", "citations": [], "body_md": known,
                "actions": [{"label": "Review your landing page status", "action": "site.preview", "style": "primary"}]}
    guide = GUIDES.get(code)
    if guide is None:
        return {"kind": "text", "citations": [], "actions": [],
                "body_md": "I do not have a step-by-step guide for that yet. Ask me what is blocking us."}
    steps = "\n".join(f"{i}. {s}" for i, s in enumerate(guide["steps"], 1))
    actions = [{**a, "href": a["href"].replace("{vid}", venture_id),
                "style": "primary" if i == 0 else "secondary"}
               for i, a in enumerate(guide["actions"])]
    from app.chat.navigation_map import path

    settings_hrefs = {path("settings", venture_id), f"/v/{venture_id}/settings"}
    for action in actions:
        if re.split(r"[?#]", action["href"], maxsplit=1)[0].rstrip("/") in settings_hrefs:
            action["kind"] = "recovery_link"
    return {"kind": "text", "citations": [], "actions": actions,
            "body_md": f"**{guide['title']}.** {guide['why']}\n\n{steps}"}
