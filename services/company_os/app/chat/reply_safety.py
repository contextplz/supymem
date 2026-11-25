"""Keep model instructions out of founder replies."""

import logging
import re

from app.gstack.methodology import (
    AUTOPLAN_PRINCIPLES, CEO_PRIME_DIRECTIVES, DESIGN_SLOP, INVESTIGATE_IRON_LAW, _ROLE_SPECIALISTS,
)

log = logging.getLogger(__name__)


def _normalized(text: str) -> str:
    return " ".join(re.sub(r"[*`_#]", "", text).casefold().split())


PROMPT_MARKERS = (
    *CEO_PRIME_DIRECTIVES, *AUTOPLAN_PRINCIPLES.values(), INVESTIGATE_IRON_LAW,
    *DESIGN_SLOP, *_ROLE_SPECIALISTS.values(),
    "Prime directive", "Autoplan principles", "CEO review mode:", "GSTACK METHODOLOGY",
    "CEO_PRIME_DIRECTIVES", "AUTOPLAN_PRINCIPLES",
    "System prompt:", "Developer instructions:", "BEGIN SYSTEM PROMPT",
    "<system>", "<developer>", "<|im_start|>system", "<|im_start|>developer",
    "<|start|>system", "<|start|>developer", "<|system|>", "<<SYS>>", "[SYSTEM]", "[DEVELOPER]",
    "[INST]", "<system_prompt>", "<developer_instructions>",
)
_MARKERS = tuple(_normalized(marker) for marker in PROMPT_MARKERS)


def contains_internal_prompt(value) -> bool:
    if isinstance(value, str):
        text = _normalized(value)
        return any(marker in text for marker in _MARKERS)
    if isinstance(value, dict):
        return any(contains_internal_prompt(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(contains_internal_prompt(item) for item in value)
    return False


def failed_reply(*, action: str = "chat.model_retry", payload: dict | None = None) -> dict:
    return {"kind": "action_failed", "action_started": False,
            "body_md": "I could not finish that reply because of a problem on our side. "
                       "Your saved work is kept. Please try again.",
            "actions": [{"label": "Retry", "action": action, "style": "primary",
                         "payload": payload if payload is not None else {
                             "text": "Please answer my last request using my saved idea and notes."}}],
            "citations": []}


def guard_reply(venture_id: str, reply: dict) -> dict:
    visible = {key: value for key, value in reply.items() if key not in (
        "id", "conversation_id", "role", "created_at", "client_request_id", "reply_to_message_id")}
    if not contains_internal_prompt(visible):
        from app.chat.idea_proposal import guard_confirmation
        return guard_confirmation(venture_id, reply)
    log.error("founder_reply_internal_prompt_blocked", extra={"venture_id": venture_id})
    failure = failed_reply()
    return {**{key: reply[key] for key in (
        "id", "conversation_id", "role", "created_at", "client_request_id", "reply_to_message_id") if key in reply},
        **failure, "content": failure["body_md"], "parts": [{"type": "text", "text": failure["body_md"]}]}
