"""Qualified browser-only pending reviews; no approval, claim, model or effect."""
from contextlib import contextmanager
from contextvars import ContextVar
import os
import re

from app.authz.workspace import subject_person_id
from app.chat.conversations import current_conversation, resolve

_CALLER = ContextVar("qualified_browser_prepare_caller", default=None)
FLAG = "SUPYMEM_CHAT_BROWSER_PREP_ENABLED"
PIN = "SUPYMEM_CHAT_BROWSER_PREP_PROFILE_DIGEST"
BLOCKED = "error: browser_preparation_unavailable"


def enabled():
    return os.getenv(FLAG) == "1" and re.fullmatch(r"[a-f0-9]{64}", os.getenv(PIN, "")) is not None


def explicit_browser_request(text):
    """A leading browser request, never quoted/negated text or a native command."""
    from app.build.device_orders import browser_task_request
    return browser_task_request(text)


def prepare_founder_review(goal, ctx):
    from app.build import device_orders as orders
    from app.chat.link_context import _safe_host
    from app.secrets.detect import looks_like_secret
    from urllib.parse import urlsplit

    caller, conversation = _CALLER.get(), current_conversation()
    settings = {"label": "Open browser tasks", "href": f"/v/{ctx.venture_id}/settings#settings-computer-use", "style": "primary"}
    if caller is None or caller.legacy or caller.role != "user" or not caller.device_id:
        ctx.actions.append(settings)
        return "Browser tasks run in the Supymem Mac app. Open this venture there to prepare and review the task."
    if (conversation is None or conversation.visibility != "private"
            or (conversation.venture_id, conversation.workspace_id, conversation.actor_id)
                != (ctx.venture_id, ctx.workspace_id, subject_person_id(caller.sub))):
        ctx.actions.append(settings)
        return "Open a private conversation in the Mac app or use Browser tasks in Settings to review this request."
    urls = [url.rstrip(".,)") for url in re.findall(r'https://[^\s<>"\x27]+', goal)]
    if not urls:
        return "Which website should I use? Send its HTTPS URL with the task."
    try:
        unsafe = any(urlsplit(url).username or urlsplit(url).password or not _safe_host(urlsplit(url).hostname or "") for url in urls)
    except ValueError:
        unsafe = True
    if looks_like_secret(goal) or len(urls) > 4 or unsafe:
        return "Use a public HTTPS website and leave out passwords, payment details and codes. Send the site and task again."
    try:
        prepared = orders.prepare_browser_task(ctx.venture_id, caller, goal, conversation_id=conversation.id)
        from app.chat.store import append_message
        append_message(ctx.venture_id, role="user", body_md=goal, conversation_id=conversation.id,
                       idempotency_key=f"mac-review:{prepared['id']}:user")
    except orders.ActiveDeviceOrder:
        ctx.actions.append(settings)
        return "A browser task already has a saved review. Open Browser tasks in Settings to finish or cancel it first."
    except Exception as exc:
        from app.chat.recovery import founder_failure
        reply = founder_failure(exc, ctx.venture_id, "your browser task review",
            {"action": "chat.reply", "text": "What happened in the latest Mac browser task?"},
            safety="Check saved Mac tasks in Settings before another request. Nothing was approved or started.")
        ctx.actions.extend([*reply["actions"], settings])
        return reply["body_md"]
    if resolve(ctx.venture_id, conversation.id) != conversation:
        ctx.actions.append(settings)
        return "The conversation changed. Check saved Mac tasks in Settings before another request."
    ctx.actions.append({"label": "Review browser task", "href": prepared["review_path"], "style": "primary"})
    return "Your browser task is ready for review. It runs in the Mac app only, after you review the websites and permissions, approve, and press Start."


@contextmanager
def bind_caller(caller):
    token = _CALLER.set(caller)
    try:
        yield
    finally:
        _CALLER.reset(token)


def prepare(goal, ctx):
    prior_actions = len(ctx.actions)
    try:
        return _prepare(goal, ctx)
    except Exception as exc:
        from app.chat.recovery import log_founder_exception
        log_founder_exception(exc, ctx.venture_id, "browser.prepare")
        # A database write may precede a lost acknowledgment. Never promise that
        # no review exists or create a new unrelated request as a fallback.
        del ctx.actions[prior_actions:]
        return "error: browser_preparation_unconfirmed"


def _prepare(goal, ctx):
    from app.build import device_browser_ai as browser, device_orders as orders
    from app.chat.link_context import _safe_host
    from app.secrets.detect import looks_like_secret
    from urllib.parse import urlsplit

    caller, conversation = _CALLER.get(), current_conversation()
    if (not enabled() or caller is None or caller.legacy or caller.role != "user" or not caller.device_id
            or conversation is None or conversation.visibility != "private"
            or (conversation.venture_id, conversation.workspace_id, conversation.actor_id)
                != (ctx.venture_id, ctx.workspace_id, subject_person_id(caller.sub))):
        return BLOCKED
    # Existing general browser policy grants read+interact. Never call it read-only
    # or use this helper to escape the deliberately unsupported no-mutation request.
    if (orders.readonly_browser_task_request(goal) or re.search(r"\bread[ -]?only\b|\bno (?:mutation|changes?|edits?)\b", goal, re.I)
            or looks_like_secret(goal) or orders.classify_device_chat_intent(goal) in {
                "readonly_browser", "desktop", "coding", "inspection", "terminal", "app_navigation", "public_site"}):
        return "error: browser_request_scope_unsupported"
    urls = [u.rstrip(".,)") for u in re.findall(r'https://[^\s<>"\x27]+', goal)]
    if not urls or len(urls) > 4:
        return "error: browser_explicit_https_url_required"
    for url in urls:
        parsed = urlsplit(url)
        if parsed.username or parsed.password or not parsed.hostname or not _safe_host(parsed.hostname):
            return "error: browser_url_not_allowed"
    offered = browser.available_profile(ctx.venture_id, caller)
    if not offered.get("available") or offered.get("execution_profile_digest") != os.getenv(PIN):
        return BLOCKED
    profile = orders.BrowserAIProfile.model_validate(offered["execution_profile"])
    try:
        prepared = orders.prepare_general_browser_from_chat(ctx.venture_id, caller, goal, profile,
                                                            conversation_id=conversation.id)
    except orders.ActiveDeviceOrder:
        return "error: browser_review_exists_in_another_scope"
    manifest = prepared["manifest"]
    if (manifest["applications"] != ["com.google.Chrome"] or manifest["actions"] != ["browser.read", "browser.interact"]
            or not manifest.get("general_browser_verification") or any(manifest.get(k) is not None for k in (
                "coding_verification", "desktop_verification", "inspection_verification", "terminal_verification", "app_navigation_verification"))
            or browser.profile_digest(manifest["general_browser_verification"]["execution_profile"]) != offered["execution_profile_digest"]):
        return BLOCKED
    # Conversation custody was saved atomically with the review digest. Confirm
    # the request still has that private destination before exposing the action.
    current = resolve(ctx.venture_id, conversation.id)
    if current != conversation:
        return BLOCKED
    action = {"label": "Review browser task", "href": f"/v/{ctx.venture_id}/device-work-orders/{prepared['id']}", "style": "primary"}
    if action not in ctx.actions:
        ctx.actions.append(action)
    if prepared["state"] != "pending":
        return ("This browser request already has a saved review. Open it to inspect its current status. "
                "I did not approve or start a new task.")
    return ("Saved a pending browser review in this private conversation. Its scope includes browser read and interact, "
            "after you review its permissions. "
            "It is not a no-mutation contract. Nothing was approved, executed or charged. Click the saved review to inspect and approve it.")
