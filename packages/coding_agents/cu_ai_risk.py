"""Semantic risk gate for web/app controls: task intent versus control role, label and context.

A control is risky when it commits the founder to something (consent, signup, trial, cancel, delete, send, pay,
submit a form that was only to be filled). It is allowed when the original task explicitly authorizes that KIND of
act (synonyms count: "remove" authorizes a Delete button, "sign me up" a Subscribe button). Legal consent and
cancelling a paid subscription are never authorized by task wording alone: the founder decides. Pure logic, no I/O.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_I = re.IGNORECASE


def _rx(pattern):
    return re.compile(pattern, _I)


# kind -> (control label pattern, goal pattern that authorizes it; None = never authorized by wording)
_KINDS = {
    "consent": (_rx(r"\b(terms|conditions|privacy|eula|agree|i accept|accept|consent|gdpr)\b"), None),
    "marketing": (_rx(r"\b(marketing|partners?|promotion\w*|third.?party|offers)\b"), _rx(r"\b(marketing|partners?|promotion\w*|third.?party|offers)\b")),
    "trial": (_rx(r"\b(start|begin|activate|get|try)\b.{0,20}\btrial\b|\bfree trial\b|\bstart (for )?free\b|\btry\b.{0,12}\bfree\b"), _rx(r"\btrial\b")),
    "cancel_subscription": (_rx(r"\b(cancel|end|terminate|close|deactivate|delete)\b.{0,20}\b(subscription|membership|plan|account|service)\b|"
                                r"\bconfirm cancell?ation\b|\bunsubscribe\b"), None),
    "signup": (_rx(r"\b(sign ?up|register|create (an |your |my )?account|join( now)?|subscribe|enrol+)\b"),
               _rx(r"\b(sign (me |us )?up|signup|registe\w*|subscrib\w*|create (an |my |a )?account|enrol\w*|join|newsletter)\b")),
    "delete": (_rx(r"\b(delete|remove|erase|discard|empty trash|move to trash|clear all|destroy|wipe)\b"),
               _rx(r"\b(delete|remove|erase|discard|trash|clear|get rid|drop|destroy|wipe|throw away)\b")),
    "send": (_rx(r"\b(send|publish|post|share|email|reply|forward|notify)\b"),
             _rx(r"\b(send|publish|post|share|e-?mail|mail|message|reply|forward|notify|tell|inform|contact|let .{1,30} know|submit)\b")),
    "payment": (_rx(r"\b(buy|purchase|pay|place order|checkout|check out|confirm (purchase|order)|transfer)\b"),
                _rx(r"\b(buy|purchase|pay|order|checkout|check out|transfer|get me)\b")),
}
_ORDER = ("consent", "cancel_subscription", "trial", "marketing", "signup", "delete", "payment", "send")
_TOGGLE = {"checkbox", "radio", "switch"}
_SUBMIT_LABEL = _rx(r"^\s*(submit|save|send|register|sign ?up|apply|confirm|book|post|create|finish|complete|continue|next)\b")
_SUBMIT_GOAL = _rx(r"\b(submit|send|save|post|book|register|sign (me |us )?up|apply|confirm|subscribe|buy|order|request|finish|create|add|update|"
                   r"change|set|turn|enable|disable|enter|pay|cancel|delete|remove|download|upload|log ?in|and (hit|press|click|go))\b")
_FILL_GOAL = _rx(r"\b(fill|pre-?fill|populate|draft)\b")
_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_SYNTHETIC_HOST = re.compile(r"@[^@\s]*\.(invalid|test|example)$|@example\.(com|org|net)$", _I)
_DESCRIPTION = {"consent": "agree to terms or consent", "marketing": "opt in to marketing or partner messages",
                "trial": "start a free trial", "cancel_subscription": "cancel a subscription or account",
                "signup": "sign up or register", "delete": "delete something", "send": "send or publish something",
                "payment": "buy or pay for something", "fill_only": "submit a form you only asked me to fill in",
                "provide_value": "enter an email address you did not give me", "upload": "upload a file you did not name"}


@dataclass(frozen=True)
class Risk:
    kind: str
    label: str
    approvable: bool          # False: the founder must do it themselves (legal consent)
    message: str              # founder-facing, never empty
    key: tuple = ()           # what a founder Continue approves


def _label_of(element: dict | None, fallback: str) -> str:
    element = element or {}
    text = fallback or " ".join(str(element.get(k) or "") for k in ("label", "title", "description", "name"))
    return " ".join(text.replace("_", " ").replace("-", " ").split())[:80]


def fill_only(goal: str) -> bool:
    return bool(_FILL_GOAL.search(goal)) and not _SUBMIT_GOAL.search(goal)


def _message(kind: str, label: str, approvable: bool) -> str:
    what = _DESCRIPTION[kind]
    target = f" ('{label}')" if label else ""
    if kind == "provide_value":
        return "Which email address should I use? You did not give me one, so I stopped instead of making one up."
    if kind == "fill_only":
        return (f"I filled in the form as asked but stopped before submitting{target}. "
                "Should I submit it, or will you review it first? Please confirm.")
    if approvable:
        return (f"I stopped before this control{target}: it would {what}, and your task did not clearly ask for that. "
                "Should I go ahead? Please confirm and press Continue, or tell me what to do instead.")
    return (f"I stopped before this control{target}: it would {what}, which only you can decide. "
            "Please do that step yourself in the browser, then press Continue, or tell me what to do instead.")


def assess_risk(op: str, goal: str, *, element: dict | None = None, label: str = "", text: str = "", chord: str = "",
                approved=frozenset()) -> Risk | None:
    """None when the action may run; otherwise the Risk the founder must decide on."""
    element = element or {}
    kind_of_control = str(element.get("type") or element.get("ax_role") or element.get("role") or "").lower()
    if op == "type" and text and element.get("type") != "password":
        for email in _EMAIL.findall(text):
            if email.lower() not in goal.lower() and ("provide_value", email.lower()) not in approved:
                label_ = _label_of(element, label)
                return Risk("provide_value", label_, True, _message("provide_value", label_, True), ("provide_value", email.lower()))
    if op == "upload":
        path = text.strip()
        name = path.replace("\\", "/").rsplit("/", 1)[-1]
        if path and path.lower() not in goal.lower() and name.lower() not in goal.lower() and ("upload", path) not in approved:
            return Risk("upload", name, True, "Which file should I upload? Your task did not name one, so I stopped instead of picking a file. "
                        "Please tell me the file (full path).", ("upload", path))
        return None
    clicking = op in {"click", "double_click", "right_click"}
    enter = op == "key" and chord.lower().replace(" ", "") in {"return", "enter"}
    if not (clicking or enter):
        return None
    label_ = _label_of(element, label)
    if enter or element.get("submits_form") or (clicking and _SUBMIT_LABEL.search(label_) and kind_of_control in {"button", "submit", "image"}):
        if fill_only(goal) and ("fill_only", label_.lower()) not in approved:
            return Risk("fill_only", label_, True, _message("fill_only", label_, True), ("fill_only", label_.lower()))
    if not clicking or not label_:
        return None
    if ("*", label_.lower()) in approved:
        return None   # the founder already okayed this exact control (any kind except legal consent / cancel, see engine)
    toggle = kind_of_control in _TOGGLE or str(element.get("ax_role") or "").lower() in _TOGGLE
    link = str(element.get("ax_role") or "").lower() in {"a", "link"} or str(element.get("role") or "").lower() == "link"
    for kind in _ORDER:
        label_rx, goal_rx = _KINDS[kind]
        if kind == "send" and link:
            continue   # "Email us" / "Share" links are navigation, not a delivered message
        if kind in {"consent", "marketing"} and not toggle and not re.match(r"^\s*(i )?(agree|accept|consent)\b", label_, _I):
            continue   # a legal/marketing phrase only gates ticking a box or an explicit Agree/Accept button
        if not label_rx.search(label_):
            continue
        if (kind, label_.lower()) in approved and kind not in {"consent", "cancel_subscription"}:
            return None
        if goal_rx is not None and goal_rx.search(goal):
            if kind == "delete" and re.search(r"\b(all|everything)\b", label_, _I) and not re.search(r"\b(all|every|everything)\b", goal, _I):
                pass   # "Delete all" needs the task to say all
            else:
                continue
        approvable = kind not in {"consent", "cancel_subscription"}
        return Risk(kind, label_, approvable, _message(kind, label_, approvable), (kind, label_.lower()))
    return None
