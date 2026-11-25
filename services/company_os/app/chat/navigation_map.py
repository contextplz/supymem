"""Single source of truth for the console names chat may tell a founder to click.

Chat must never invent UI ("Integrations area"). Every route and label here is checked against
apps/console/src by tests/unit/chat/test_navigation_map.py, so a rename in the console fails CI.
"""
from __future__ import annotations

import re

# screen key -> (sidebar/page label, console route template, page file under apps/console/src/app)
SCREENS: dict[str, tuple[str, str, str]] = {
    "connections": ("Connections", "/connections?venture={venture_id}", "connections/page.tsx"),
    "settings": ("Settings", "/v/{venture_id}/settings", "v/[ventureId]/settings/page.tsx"),
    "now": ("Right now", "/v/{venture_id}/now", "v/[ventureId]/now/page.tsx"),
}
# UI text that appears on those pages; {x} marks text the console fills in.
LABELS: dict[str, str] = {
    "all_ventures": "Ventures",
    "connect": "Connect",
    "refresh_all": "Refresh",
    "open_auth": "Open {name} authorization",
    "coding_tools": "Coding tools on this Mac",
    "open_tool": "Open {tool} app",
    "mac_setup": "Open Mac setup window",
    "request_credits": "Request credits",
    "download_venture": "Download my venture",
    "download_report": "I'm stuck, download report",
    "buy_more": "Buy more",
}
HINT = ("Real navigation only. Sidebar: " + ", ".join(v[0] for v in SCREENS.values())
        + ". The Connections page has one card per service with a Connect button. There is no 'Integrations area'; "
          "Settings > Your data > Export has Download my venture and I'm stuck, download report. "
          "Download my venture includes your venture details, plan, tasks, website details, saved conversations, "
          "up to 100 current saved memories and available document text. Files on this machine are not bundled; "
          "passwords and keys are excluded. The report includes sanitized troubleshooting details. "
          "Use Download my venture for your data, or the report when you need help. "
          "Buy more is in your account. Settings also shows the available purchase controls. "
          "LinkedIn people shows saved public-web profiles and contacts, with notes you copy and send yourself. "
          "You do not need to connect LinkedIn to review public-web results. New profiles require your configured "
          "search or Mac setup; Supymem never opens LinkedIn. An explanation request starts no search. "
          "Never name a page, tab or button that is not listed here.")


def billing_hint() -> str:
    from app.core.local_mode import is_local

    if is_local():
        from app.credits.errors import CreditsUnavailable
        from app.credits.local_meter import monthly_cap_usd_micro

        try:
            cap = monthly_cap_usd_micro()
        except CreditsUnavailable:
            return ("Open Settings > Keys and connections > Your monthly spend cap and save a non-negative "
                    "Monthly limit (USD), or clear it to remove the limit. Then retry.")
        limit = f"Your monthly spending limit is ${cap / 1_000_000:.2f}" if cap is not None else "You have no monthly spending limit"
        return (limit + ". Open Settings > Keys and connections > Your monthly spend cap to change it, "
                "or Your AI provider to check your model connection. If your provider reports a quota limit, "
                "review your provider account, then retry.")
    from app.payments.flags import payments_accepting

    availability = ("Purchases are open. Choose Buy more in your account to see the available packs and checkout."
                    if payments_accepting() else
                    "Purchases are closed. Choose Request credits in Settings to save a request for manual review.")
    return availability + " A manual review request does not add usage immediately. No plan upgrade is required to request more usage."


def purchase_help(venture_id: str, text: str) -> dict | None:
    from app.chat.idea_bridge import informational_request, written_request
    from app.chat.connect import wants_connect

    if written_request(text) or wants_connect(text) or not informational_request(text) or not re.search(r"\b(?:buy\s+more(?=\s*[,?.!]|$)|request\s+(?:credits|usage))\b", text, re.I):
        return None
    return {"kind": "navigation", "body_md": billing_hint(), "citations": [],
            "actions": [{"label": "Open Settings", "href": path("settings", venture_id), "kind": "recovery_link"}]}


def label(screen: str) -> str:
    return SCREENS[screen][0]


def path(screen: str, venture_id: str) -> str:
    from app.core.local_mode import is_local

    if screen == "settings" and is_local():
        return "/settings/local"
    return SCREENS[screen][1].format(venture_id=venture_id)


def ui(key: str, **kw: str) -> str:
    return LABELS[key].format(**kw)


def connections_steps(venture_id: str, button: str = "Connect") -> list[str]:
    """How to reach a service card by hand (the deep link button does steps 1 and 2 for them)."""
    return [f"Click {label('connections')} in the left sidebar (under {ui('all_ventures')}) and choose your company.",
            f"Find the card for the service and click {button}."]


# Pages the chat-first app hides (apps/console/src/lib/chatVisibility.ts chatFirstHidden). A chat button never
# sends the founder there; it runs the same view in chat. Order matters: the first matching pattern wins.
_HIDDEN = (
    (r"^/v/[^/]+/idea/?(?:[?#]|$)", {"label": "Show my idea review here", "action": "idea.review"}),
    (r"^/gtm(?:[?/#]|$)|^/v/[^/]+/(?:gtm|outreach)(?:[?/#]|$)|^/ops/outreach(?:[?/]|$)", {"label": "Show customers and drafts", "action": "gtm.status"}),
    (r"^/v/[^/]+/research(?:[?/#]|$)", {"label": "Show research here", "action": "research"}),
    (r"^/v/[^/]+/connections(?:[?/#]|$)|^/connections(?:[?/#]|$)", {"label": "Show connections here", "action": "connections.status"}),
    (r"^/v/[^/]+/site\?(?:[^#]*&)?build=1(?:[&#]|$)", {"label": "Build my landing page", "action": "pagebuilder.build", "payload": {"prepare": True}}),
    (r"^/v/[^/]+/site(?:[?/#]|$)", {"label": "Show my website here", "action": "site.preview"}),
    (r"^/v/[^/]+/experiments(?:[?/#]|$)", {"label": "Show experiments here", "action": "experiment.list"}),
    (r"^/v/[^/]+/context(?:[?/#]|$)", {"label": "Review my answers here", "action": "startup.review"}),
    (r"^/v/[^/]+/suggestions(?:[?/#]|$)", {"label": "What should I do next?", "action": "whats_next"}),
    (r"^/v/[^/]+/(?:org|people)(?:[?/#]|$)", {"label": "Show my team here", "action": "agents"}),
    (r"^/v/[^/]+/(?:now|work|tasks|plan|execution|charter|builds|analytics|market|feedback|evolution|import|simulate)"
     r"(?:[?/#]|$)|^/(?:tasks|execution)(?:[?/#]|$)", {"label": "Show status here", "action": "status"}),
)

IN_CHAT_VIEWS = frozenset(target["action"] for _, target in _HIDDEN)


def hidden_href(href: object) -> dict | None:
    """The in-chat action that replaces a link to a hidden page, else None."""
    if not isinstance(href, str):
        return None
    path = re.sub(r"^https://app\.supymem\.com(?=/)", "", href.strip())
    if re.search(_HIDDEN[0][0], path):
        from app.credits.policy import get_policy
        flags = get_policy("feature_flags")
        if isinstance(flags, dict) and flags.get("founder_ui") == "classic":
            return None
    target = next((target for pattern, target in _HIDDEN if re.search(pattern, path)), None)
    local_work_page = re.match(r"^/v/[^/?#]+/(?:builds|experiments|research)/?(?:[?#]|$)", path) and not re.match(
        r"^/v/[^/]+/experiments/?\?(?:[^#]*&)?experiment_id=", path)
    if target and (target["action"] == "connections.status" or local_work_page):
        from app.core.local_mode import is_local

        if is_local():
            return None
    return target


def in_chat(actions: list | None, *, check_readiness: bool = True) -> list | None:
    """Replace links to hidden pages with the in-chat action that shows the same thing; other actions are unchanged."""
    if not actions:
        return actions
    out = []
    seen = {action.get("action") for action in actions if isinstance(action, dict) and not action.get("href")}
    for action in actions:
        if (isinstance(action, dict) and not action.get("kind") and isinstance(action.get("href"), str)
                and re.fullmatch(r"(?:https://app\.supymem\.com)?/v/[^/?#]+/settings/?(?:[?#].*)?", action["href"])):
            action = {**action, "kind": "recovery_link"}
        # Secure forms (kind set, e.g. secret_form) keep their link.
        swap = hidden_href(action.get("href")) if isinstance(action, dict) and not action.get("kind") else None
        if swap is None:
            out.append(action)
        elif swap["action"] not in seen:
            seen.add(swap["action"])
            from app.tasks import goals
            out.append({**swap, "style": action.get("style") or "ghost",
                        **({"task_id": action["task_id"]} if goals.enabled() and isinstance(action.get("task_id"), str) else {})})
    if not check_readiness:
        return out
    from app.chat.actions import disabled_reason

    for index, action in enumerate(out):
        if not isinstance(action, dict):
            continue
        confirmation = action.get("confirmation") if isinstance(action.get("confirmation"), dict) else {}
        if action.get("href") and not confirmation:
            continue
        name = confirmation.get("action") or action.get("action") or action.get("name")
        if reason := disabled_reason(name):
            out[index] = {**action, "disabled_reason": reason}
    return out


# ponytail: console's small Markdown subset, use a Markdown parser if the renderer gains richer links.
_BODY_LINK = re.compile(
    r"```[^\n`]*\n[\s\S]*?(?:\n?```|$)|`[^`\n]+`"
    r"|\[(?P<label>[^\]\n]+)\]\((?P<href>[^)\s]+)\)"
    r"|(?P<bare>https?://[^\s<>()\]`\"']+|(?<![\w/:.\\-])/(?!/)[^\s<>()\]`\"']+)",
    re.I,
)


def _in_chat_body(body: str, links: list, preserved_hrefs: set[str]) -> str:
    from app.core.local_mode import is_local

    if is_local():
        failure = re.compile(r"\bReport a problem\b[^.!?\n]*\bcode\b[^.!?\n]*[.!?]?", re.I)
        if failure.search(body):
            body = failure.sub("Choose Show status here below to check your saved work before trying again.", body)
            links.append({"label": "Show status here", "href": "/tasks", "style": "primary"})

    def replace(match):
        href = match["href"] or (match["bare"] or "").rstrip(".,;:!?")
        if href in preserved_hrefs:
            return match[0]
        swap = hidden_href(href)
        if swap is None:
            return match[0]
        links.append({"label": swap["label"], "href": href, "style": "ghost"})
        return match["label"] or swap["label"] + match[0][len(href):]

    return _BODY_LINK.sub(replace, body)


def in_chat_reply(reply):
    """Normalize saved and direct assistant replies, including links inside their text."""
    if not isinstance(reply, dict):
        return reply
    from app.gtm.progress import normalize_search_reply
    reply = normalize_search_reply(reply) if reply.get("role") not in {"user", "founder"} else reply
    out = {**reply}
    if out.get("role") in {"user", "founder"}:
        return out
    from app.core.local_mode import is_local
    saved_local_reply = is_local() and bool(out.get("id")) and out.get("role") == "assistant"
    if out.get("kind") == "memory_list":
        if isinstance(out.get("actions"), list):
            out["actions"] = in_chat(out["actions"], check_readiness=not saved_local_reply)
        return out
    links = []
    actions = list(out["actions"]) if isinstance(out.get("actions"), list) else []
    for index, action in enumerate(actions):
        if (isinstance(action, dict) and action.get("label") == "Choose your next step"
                and not any(action.get(key) for key in ("kind", "confirmation", "action", "name"))):
            href = action.get("href")
            if isinstance(href, str) and re.fullmatch(r"(?:https://app\.supymem\.com)?/v/[^/?#]+/?", href):
                actions[index] = {"label": action["label"], "action": "next_moves", "style": action.get("style") or "ghost"}
    preserved_hrefs = {action["href"] for action in actions if isinstance(action, dict)
                       and action.get("kind") == "recovery_link" and isinstance(action.get("href"), str)}
    for key in ("body_md", "content"):
        if isinstance(out.get(key), str):
            out[key] = _in_chat_body(out[key], links, preserved_hrefs)
    if isinstance(out.get("parts"), list):
        out["parts"] = [{**part, "text": _in_chat_body(part["text"], links, preserved_hrefs)}
                        if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str)
                        else part for part in out["parts"]]
    if isinstance(out.get("actions"), list) or links:
        out["actions"] = in_chat([*actions, *links], check_readiness=not saved_local_reply)
    return out
