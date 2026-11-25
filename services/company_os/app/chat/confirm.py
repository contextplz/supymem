"""Inline confirmation cards for consequential chat actions."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
import re

from app.chat.actions import ChatAction


# shortcut: legacy English billing copy only, extend when localized usage copy is observed.
_USAGE_COPY = re.compile(
    r"^\s*credits?\s*$|\bcredits\b[^.!?\n]*\d|\bcredit\s*[:=]\s*\d|\b(?:\d[\d,.]*\s+credits?|(?:no|new|zero|any|free|extra|additional|all|total|reserved|remaining|spent|unused|available|enough|more|model|platform|generation|email|top\s*up|buy|get|add|quote)\s+credits|"
    r"credits\s+(?:are|were|remain|stay|ran|run|used|before|not|still|quoted|set|will)|"
    r"credit\s+(?:cost|spend|usage|fee|charge|ceiling|balance|maximum|cap|quote|reservation|estimate|admission|price|status|account|reconciliation))\b|"
    r"\b(?:model|platform|compute|generation)\s+(?:usage|cost|spend|charge|calls?)\b[^\n]*\$\d",
    re.I,
)
_LIMIT_REASON = re.compile(r"\b(?:[\w]+_)?insufficient_credits\b|\b(?:(?:insufficient|not enough)\s+credits?|available\s+credits?\s+(?:do not|cannot|can't|don't)\s+(?:cover|fund))\b", re.I)


def without_usage_copy(text: str) -> str:
    text = re.sub(r"\([^()]*\)", lambda match: "" if _USAGE_COPY.search(match[0]) else match[0], text)
    clean = "".join(part for part in re.split(r"((?<=[.!?])\s+|\n)", text)
                    if not _USAGE_COPY.search(part)).strip()
    return clean


def visible_action(action: dict) -> dict:
    result = dict(action)
    for key in ("label", "disabled_reason"):
        if isinstance(result.get(key), str):
            if key == "disabled_reason" and _LIMIT_REASON.search(result[key]):
                from app.credits.errors import LIMIT_MESSAGE
                result[key] = LIMIT_MESSAGE
                continue
            if key == "disabled_reason" and result[key] == "":
                continue
            result[key] = without_usage_copy(result[key]) or {
                "label": "Continue", "disabled_reason": "This action is unavailable right now. Try again, or open Settings for help.",
            }[key]
    if isinstance(result.get("confirmation"), dict):
        card = dict(result["confirmation"])
        card["interpretation"] = without_usage_copy(card.get("interpretation") or "")
        if isinstance(card.get("cancel_label"), str):
            card["cancel_label"] = without_usage_copy(card["cancel_label"]) or "Not now"
        card["consequences"] = [clean for item in card.get("consequences") or []
                                if (clean := without_usage_copy(item))]
        if card.get("action") == "experiment.spend_cap":
            state = card.get("resulting_state")
            stop = isinstance(state, dict) and state.get("stop") is True
            result["label"] = "Stop it" if stop else "Change experiment limit"
            card["interpretation"] = "Stop this experiment." if stop else "Change your saved experiment limit."
            card["consequences"] = (["No new runs will start. Work already in progress may still finish. Your saved results are kept."]
                                    if stop else ["Your experiment setting changes. Ask to run it again when you are ready."])
        result["confirmation"] = card
    return result


@dataclass(frozen=True)
class ConfirmationCard:
    action: str
    capability: str
    interpretation: str
    consequences: list[str] = field(default_factory=list)
    resulting_state: dict[str, Any] = field(default_factory=dict)
    confirm_label: str = "Do it"
    cancel_label: str = "Not quite"

    @classmethod
    def for_action(
        cls,
        action: ChatAction,
        *,
        interpretation: str,
        consequences: list[str] | None = None,
        resulting_state: dict[str, Any] | None = None,
    ) -> "ConfirmationCard":
        if not action.consequential:
            raise ValueError(f"{action.name} does not require confirmation")
        return cls(
            action=action.name,
            capability=action.governed_capability,
            interpretation=interpretation,
            consequences=consequences or [],
            resulting_state=resulting_state or {},
        )

    def as_action(self) -> dict[str, Any]:
        result = visible_action({
            "name": "chat.confirm",
            "action": self.action,
            "label": self.confirm_label,
            "style": "primary",
            "confirmation": {
                "action": self.action,
                "capability": self.capability,
                "interpretation": self.interpretation,
                "consequences": self.consequences,
                "resulting_state": self.resulting_state,
                "cancel_label": self.cancel_label,
            },
        })
        from app.approvals.standing import card_action, enabled
        if not enabled():
            return result
        from app.chat.conversations import current_conversation
        from app.tenancy import current_or_none

        tenant = current_or_none()
        conversation = current_conversation()
        if (tenant and conversation and conversation.visibility == "private"
                and conversation.venture_id == tenant.venture_id and conversation.workspace_id == tenant.workspace_id):
            result["confirmation"]["approval_policy"] = {
                "eligible": card_action(self.action, self.resulting_state).eligible(),
                "scope_label": "these saved inputs",
                "venture_id": tenant.venture_id, "workspace_id": tenant.workspace_id,
            }
        return result
