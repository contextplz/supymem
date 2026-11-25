"""connections.connect: "connect/link/sign in to <service>" gets a real link plus exact steps, then is watched.

For Composio-backed services (Gmail, LinkedIn, Reddit, X, Discord) the authorization link is created by the same
code the Connections page uses (``connections.hub.connect_provider``). Everything else gets exact in-app steps from
``navigation_map``. A connected check runs every ``CONNECT_POLL_SECONDS`` in the API process and again on the worker's
5-minute tick; when it passes chat posts "connected", and if it is still not done after ``CONNECT_ESCALATE_MINUTES``
a persistent needs_you reminder takes over until the founder finishes.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from datetime import timedelta, timezone
from typing import Any

from sqlalchemy import select

from app.chat import navigation_map as nav
from app.connections.composio_pacing import RateLimited
from app.core.local_mode import is_local
from app.db.models import Person, Venture, VentureDocument, Workspace
from app.state import db
from core.clock import utcnow

class ConnectionStatusUnavailable(RuntimeError):
    def __init__(self, error: Exception):
        self.error = error
        super().__init__("connection_status_unconfirmed")


def gmail_status(brief: dict[str, Any]) -> str:
    mail = (brief.get("connections") or {}).get("gmail") or {}
    workspace_id = brief.get("workspace_id")
    if workspace_id:
        try:
            from app.gtm.supymem.pipeline import mail_readiness
            mail = mail_readiness(workspace_id)
        except Exception as exc:
            from app.chat.recovery import log_founder_exception
            log_founder_exception(exc, brief.get("venture_id"), "connections.gmail_status")
            raise ConnectionStatusUnavailable(exc) from None
    if not mail.get("connected"):
        return "Gmail is not connected. Connect your account under Connections. Nothing is sent without your approval."
    address = mail.get("account_email")
    text = f"Gmail is connected as {address}." if address else "Gmail is connected. Your email address is not confirmed yet."
    text += " Nothing is sent without your approval."
    if mail.get("ready") and mail.get("send_enabled"):
        from app.gtm.supymem.pipeline import DEFAULT_DAILY_CAP
        text += f" Approved sending is capped at {DEFAULT_DAILY_CAP} emails per day."
    return text


def gmail_status_reply(venture_id: str, brief: dict[str, Any]) -> dict[str, Any]:
    try:
        body = gmail_status(brief)
    except ConnectionStatusUnavailable as exc:
        from app.chat.recovery import founder_failure
        return founder_failure(exc.error, venture_id, "your Gmail connection status",
            {"action": "connections.status", "payload": {}},
            safety="I could not check your Gmail connection. Nothing was sent.")
    return {"kind": "text", "body_md": body, "actions": [], "citations": []}


# slug -> (display name, text pattern)
SERVICES: dict[str, tuple[str, str]] = {
    "google_analytics": ("Google Analytics", r"google\s*analytics|\bga4?\b"),
    "google_search_console": ("Google Search Console", r"google\s*search\s*console|\bgsc\b"),
    "google_calendar": ("Google Calendar", r"(?:google\s*)?calendar"),
    "cal": ("Cal.com", r"\bcal(?:\.com)?\b"),
    "calendly": ("Calendly", r"\bcalendly\b"),
    "gmail": ("Gmail", r"g\s*-?\s*mail|gmial|gmai\b|google\s*mail|my\s+(?:e-?mail|inbox)|e-?mail\s+account|inbox|mailbox"),
    "linkedin": ("LinkedIn", r"linked\s*-?\s*in|linkdin|linkedln|lnkdin"),
    "discord": ("Discord", r"discord|discrod"),
    "stripe": ("Stripe", r"\bstripe\b"),
    "reddit": ("Reddit", r"reddit|redit"),
    "x": ("X", r"twitter|twiter|twittr|\bx\s+(?:account|profile)|\bx\.com|(?:connect|link)\s+(?:my\s+|our\s+|the\s+)?x\b"),
    "unipile": ("Unipile", r"unipile"),
    "hunter": ("Hunter", r"hunter(?:\.io)?"),
    "google_ads": ("Google Ads", r"google\s*ads?|adwords"),
    "meta_ads": ("Meta Ads", r"meta\s*ads?|facebook\s*ads?|fb\s*ads?|instagram\s*ads?"),
    "claude": ("Claude Code", r"claude(?:\s*code)?"),
    "codex": ("Codex", r"codex"),
    "cursor": ("Cursor", r"cursor"),
    "opencode": ("OpenCode", r"\bopencode\b"),
}
COMPOSIO = ("gmail", "google_calendar", "cal", "calendly", "linkedin", "reddit", "x", "discord", "stripe")
CODING_TOOLS = ("claude", "codex", "cursor", "opencode")
ACCOUNT = {"gmail": "Google", "google_calendar": "Google", "cal": "Cal.com", "calendly": "Calendly",
           "linkedin": "LinkedIn", "reddit": "Reddit", "x": "X", "discord": "Discord", "stripe": "Stripe"}
_VERB = re.compile(
    r"\b(?:reconnect|(?:con+ect|conn+et|conn+cet|cnnect|cone?ct)(?!ed|ion|ivity)\w*|link(?:ing)?\b|hook\s*(?:it\s*|them\s*)?up|hookup|"
    r"sign\s*(?:in|into|up\s+(?:to|for|with))|log\s*(?:in|into|on)|login|integrat\w+|set\s*up|setup|"
    r"authori[sz]e|jod\w*|lagao)\b", re.I)
_NEG = re.compile(r"\b(?:don'?t|dont|do\s+not|never|not\s+now|mat\s+\w+|nahi|nahin|no\s+need)\b", re.I)
_ZERNIO = re.compile(r"zernio", re.I)
_DISCONNECT = re.compile(r"\b(?:disconnect|unlink|remove|revoke)\b", re.I)


def _services():
    from app.connections.toolkits import TOOLKITS, configured_toolkits
    slugs = TOOLKITS if is_local() else configured_toolkits()
    return {**{slug: (entry["name"], re.escape(entry["name"]) + "|" + re.escape(slug))
        for slug in slugs for entry in [TOOLKITS[slug]]}, **SERVICES}


def _composio():
    from app.connections.toolkits import TOOLKITS, configured_toolkits
    return (*COMPOSIO, *(TOOLKITS if is_local() else configured_toolkits()))


def service_from(text: str) -> str | None:
    for slug, (_, pat) in _services().items():
        if re.search(rf"(?:{pat})", text or "", re.I):
            return slug
    return None


def wants_connect(text: str) -> bool:
    """A connect request (any phrasing the regex knows) that names a service; never a status question or a refusal."""
    raw = text or ""
    if not _VERB.search(raw) or _NEG.search(raw) or _ZERNIO.search(raw) or _DISCONNECT.search(raw):
        return False
    from app.chat.idea_bridge import informational_request
    from app.chat.intent import _split_clauses

    clauses = _split_clauses(raw, question_reply=True)
    if clauses and informational_request(clauses[0]):
        return False
    if service_from(raw) == "x" and not is_local():
        return False  # X needs the founder's own app keys; the existing posting guide (social_guide) is its real path
    return service_from(raw) is not None


def wants_disconnect(text: str) -> bool:
    return bool(_DISCONNECT.search(text or "") and not _NEG.search(text or "") and service_from(text) in _composio())


# ---------------------------------------------------------------- state
def _scope(venture_id: str) -> tuple[str, str]:
    """(workspace id, founder email or '')."""
    with db()() as s:
        v = s.get(Venture, venture_id)
        ws = s.get(Workspace, v.workspace_id) if v and v.workspace_id else None
        person = s.get(Person, ws.owner_id) if ws and ws.owner_id else None
        return (v.workspace_id if v else ""), ((person.email if person else "") or "")


def _is_connected(workspace_id: str, slug: str) -> bool:
    try:
        from app.connections import hub

        if slug == "gmail":
            if is_local():
                return bool(hub._smtp_card()["ready"])
            from app.gtm.supymem.pipeline import mail_readiness

            return bool(mail_readiness(workspace_id).get("connected"))
        if slug in {"linkedin", "reddit", "x"}:
            from app.content.oauth import flow

            return bool(hub._account_slice(flow.list_accounts(workspace_id), slug).get("ok"))
        return bool((hub.hub_status(workspace_id)["connections"].get(slug) or {}).get("connected"))
    except Exception as exc:  # noqa: BLE001
        from app.chat.recovery import log_founder_exception
        log_founder_exception(exc, None, "connections.check")
        raise ConnectionStatusUnavailable(exc) from None


def _watch_id(venture_id: str, slug: str) -> str:
    return f"connect-watch:{venture_id}:{slug}"


def _save_watch(venture_id: str, slug: str, workspace_id: str, *, oauth_state: str | None = None) -> None:
    now = utcnow()
    body = json.dumps({"slug": slug, "workspace_id": workspace_id, "started": now.isoformat(), "escalated": False,
                       **({"oauth_state": oauth_state} if oauth_state else {})})
    with db()() as s:
        doc = s.get(VentureDocument, _watch_id(venture_id, slug))
        if doc is None:
            doc = VentureDocument(id=_watch_id(venture_id, slug), venture_id=venture_id, kind="connect_watch",
                                  title=f"Waiting for {slug} connection", created_at=now, updated_at=now)
            s.add(doc)
        doc.body_md, doc.updated_at = body, now
        s.commit()


def _poll_seconds() -> float:
    try:
        return float(os.environ.get("CONNECT_POLL_SECONDS", "10"))
    except ValueError:
        return 10.0


def check(venture_id: str, slug: str) -> str:
    """One status check for a watched connection: 'connected' | 'waiting' | 'gone'. Posts chat/needs_you as needed."""
    if slug not in _services():
        return "waiting"
    with db()() as s:
        doc = s.get(VentureDocument, _watch_id(venture_id, slug))
        data = json.loads(doc.body_md or "{}") if doc else None
    if not data:
        return "gone"
    name = _services()[slug][0]
    wid = data["workspace_id"]
    started = utcnow().fromisoformat(data["started"]) if isinstance(data.get("started"), str) else utcnow()
    from app.connections.mailboxes import enabled
    targeted = slug == "gmail" and enabled() and bool(data.get("oauth_state"))
    try:
        if slug in _composio() and (targeted or not _is_connected(wid, slug)):
            from app.connections import hub

            hub.sync_provider(wid, slug)  # pulls a freshly authorized Composio account into this workspace
    except RateLimited:
        raise
    except ConnectionStatusUnavailable:
        return "waiting"
    except Exception as exc:  # noqa: BLE001
        from app.chat.recovery import log_founder_exception
        log_founder_exception(exc, venture_id, "connections.sync")
        return "waiting"
    try:
        connected = _gmail_watch_connected(wid, data["oauth_state"]) if targeted else _is_connected(wid, slug)
    except ConnectionStatusUnavailable:
        return "waiting"
    except Exception as exc:  # noqa: BLE001
        from app.chat.recovery import log_founder_exception
        log_founder_exception(exc, venture_id, "connections.watch_status")
        return "waiting"
    if connected:
        from app.chat.milestones import post_milestone
        from app.chat.posts import POSTABLE, finish_pending
        from app.needs_you import service

        with db()() as s:
            doc = s.get(VentureDocument, _watch_id(venture_id, slug))
            if doc is not None:
                s.delete(doc)
                s.commit()
        post_milestone(venture_id, key=f"connect_done:{slug}:{data['started']}", kind="connection_ready",
                       body_md=f"**{name} connected ✓** You are all set; I can use {name} from now on.",
                       actions=[{"label": "Open Connections", "href": nav.path("connections", venture_id), "style": "ghost"}])
        service.resolve_ref(venture_id, f"connect:{slug}")
        service.resolve_ref(venture_id, f"setup:{slug}")  # readiness card for the same service
        if slug in POSTABLE:
            finish_pending(venture_id, slug)
        return "connected"
    after = timedelta(minutes=float(os.environ.get("CONNECT_ESCALATE_MINUTES", "10")))
    if not data.get("escalated") and utcnow() - started >= after:
        _escalate(venture_id, slug, name, data)
    return "waiting"


def _gmail_watch_connected(workspace_id: str, oauth_state: str) -> bool:
    from app.content.oauth.attempts import digest
    from app.db.models import ContentOAuthState, GtmEmailAccount
    from app.gtm.supymem.pipeline import _mailboxes_query
    with db()() as session:
        attempt = session.get(ContentOAuthState, digest(oauth_state))
        if attempt is None or attempt.workspace_id != workspace_id or attempt.outcome != "active":
            return False
        return session.scalar(_mailboxes_query(session, workspace_id).where(
            GtmEmailAccount.connected_account_id == attempt.connected_account_id,
            GtmEmailAccount.status == "active")) is not None


def _escalate(venture_id: str, slug: str, name: str, data: dict[str, Any]) -> None:
    from app.needs_you.service import request_human

    request_human(
        venture_id, reason_code="gmail_connect" if slug == "gmail" else "login",
        title=f"Finish connecting {name}", blocking_ref=f"connect:{slug}", severity="blocking", chat_card=False,
        what_we_need=f"{name} is not connected yet, so I cannot use it.",
        how_to_resume="\n".join(f"{i}. {t}" for i, t in enumerate(_steps(slug, name, "", venture_id)[:-1], 1)))
    data["escalated"] = True
    with db()() as s:
        doc = s.get(VentureDocument, _watch_id(venture_id, slug))
        if doc is not None:
            doc.body_md = json.dumps(data)
            s.commit()


async def poll(venture_id: str, slug: str, *, max_minutes: float = 15) -> None:
    """Fast path: check every few seconds while the founder is on the provider's page. The 5-minute tick is the backstop."""
    every = _poll_seconds()
    for _ in range(int(max_minutes * 60 / every) if every > 0 else 0):
        await asyncio.sleep(every)
        try:
            if await asyncio.to_thread(check, venture_id, slug) != "waiting":
                return
        except Exception as exc:  # noqa: BLE001
            from app.chat.recovery import log_founder_exception
            log_founder_exception(exc, venture_id, "connections.poll")


def check_pending(*, founder_id=None) -> int:
    """Worker tick: check every watched connection; drop watches older than a day. Returns how many connected."""
    done = 0
    with db()() as s:
        query = select(VentureDocument).where(VentureDocument.kind == "connect_watch")
        if founder_id:
            from app.core.scheduled import founder_ventures
            query = query.where(VentureDocument.venture_id.in_(founder_ventures(founder_id)))
        docs = s.scalars(query).all()
        rows = [(d.venture_id, d.id.rsplit(":", 1)[-1], d.updated_at) for d in docs]
    for vid, slug, updated in rows:
        if slug not in SERVICES:
            continue
        if utcnow() - (updated if updated.tzinfo else updated.replace(tzinfo=timezone.utc)) > timedelta(hours=24):
            with db()() as s:
                doc = s.get(VentureDocument, _watch_id(vid, slug))
                if doc is not None:
                    s.delete(doc)
                    s.commit()
            continue
        try:
            done += check(vid, slug) == "connected"
        except RateLimited:
            raise
        except Exception as exc:  # noqa: BLE001
            from app.chat.recovery import log_founder_exception
            log_founder_exception(exc, vid, "connections.tick")
    return done


# ---------------------------------------------------------------- the card
def _steps(slug: str, name: str, email: str, venture_id: str) -> list[str]:
    """Numbered steps; the last item is always the 'come back' line."""
    if slug in _composio():
        who = ACCOUNT.get(slug, name)
        pick = f"Pick the {who} account you want Supymem to use" + (f" (you are signed in to Supymem as {email})" if email else "")
        return [f"Click **Connect {name}** below. It opens the {name} authorization page in your browser",
                pick, "Click Allow",
                "Come back here. I will check automatically and tell you the moment it is connected"]
    if slug in CODING_TOOLS:
        if slug == "opencode":
            return ["In Terminal, run `opencode auth login` and choose your subscription login",
                    "Open Settings > Keys and connections > Coding tools, select Check connection and turn on Allow Charlie"]
        return [*nav.connections_steps(venture_id, "Connect")[:1],
                f"Scroll to {nav.ui('coding_tools')} (this needs the Supymem Mac app)",
                f"Click {nav.ui('open_tool', tool=name)} and sign in inside that app or its CLI yourself (Supymem never types your password)",
                "Come back here and tell me when you are signed in"]
    return [*nav.connections_steps(venture_id), "Follow the note on the card, then click " + nav.ui("refresh_all"),
            "Come back here and tell me when it says connected"]


def _numbered(steps: list[str]) -> str:
    return "\n".join(f"{i}. {s}." for i, s in enumerate(steps, 1))


def handle(venture_id: str, text: str) -> dict[str, Any]:
    slug = service_from(text)
    from app.core.local_capabilities import surface_visible, unavailable_message

    if reason := unavailable_message("connections", "smtp" if is_local() and slug == "gmail" else slug or ""):
        return {"kind": "text", "body_md": reason, "actions": [], "citations": []}
    open_page = {"label": f"Open {nav.label('connections')}", "href": nav.path("connections", venture_id), "style": "ghost"}
    if slug is None:
        names = ", ".join(n for k, (n, _) in _services().items()
                          if k not in CODING_TOOLS and surface_visible("connections", k))
        return {"kind": "navigation", "citations": [{"question": True}], "actions": [{**open_page, "style": "primary"}],
                "body_md": f"Which service? I can connect {names}, or help you sign in to Claude Code, Codex, Cursor or OpenCode. "
                           f"Or open {nav.label('connections')} in the left sidebar and use the Connect button on its card."}
    name = _services()[slug][0]
    wid, email = _scope(venture_id)
    from app.chat.idea_bridge import informational_request
    if informational_request(text) and not wants_connect(text):
        return {"kind": "navigation", "body_md": f"To connect {name}, " + " ".join(nav.connections_steps(venture_id))
                + f" Choose {name}, complete its authorization, then choose Refresh. Nothing is sent.",
                "actions": [open_page], "citations": []}
    from app.connections.mailboxes import enabled
    multi = slug == "gmail" and enabled()
    adding = multi and bool(re.search(r"\b(?:add|another|additional|second)\b", text, re.I))
    options = {}
    if multi and re.search(r"\breconnect\b", text, re.I):
        from app.gtm.supymem.pipeline import list_mailboxes
        accounts = [account for account in list_mailboxes(wid) if account["status"] != "revoked"]
        selected = re.search(r"\binbox\s+([A-Za-z0-9_-]+)\b", text, re.I)
        addresses = re.findall(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+", text)
        candidates = [account for account in accounts if (account.get("email") or "").casefold() in
                      {address.casefold() for address in addresses}] if addresses else [
                          account for account in accounts if account["status"] == "needs_reconnect"] or accounts
        if selected:
            candidates = [account for account in accounts if account["id"] == selected.group(1)]
        if len(candidates) != 1:
            return {"kind": "navigation", "body_md": "Choose the Gmail inbox you want to reconnect in Connections.",
                    "actions": [{**open_page, "style": "primary"}], "citations": [{"question": True}]}
        options = {"intent": "reconnect", "mailbox_id": candidates[0]["id"]}
    elif adding:
        options = {"intent": "add"}
    if slug in _composio() and not adding and not re.search(r"\breconnect\b", text, re.I):
        try:
            connected = _is_connected(wid, slug)
        except ConnectionStatusUnavailable as exc:
            from app.chat.recovery import founder_failure
            return founder_failure(exc.error, venture_id, "your connection status",
                {"action": "connections.status", "payload": {}},
                safety="I could not confirm whether this account is connected. Check Connections before reconnecting.")
        if connected:
            shared = " Your Gmail connection works across all ventures you own, with one daily limit of 15 emails including follow-ups." if slug == "gmail" else ""
            if is_local() and slug == "gmail":
                shared = " Your email uses your saved SMTP settings. Open Find customers to request outreach."
            if multi:
                shared = " You can manage your inboxes and default sender in Connections."
            return {"kind": "text", "citations": [], "actions": [{**open_page, "style": "ghost"},
                    *([{"label": "Add another Gmail", "action": "connections.connect", "payload": {"query": "add another gmail"}, "style": "primary"}] if multi else [])],
                    "body_md": f"**{name} is already connected ✓** Nothing to do.{shared}"}
    if slug in CODING_TOOLS:
        steps = _steps(slug, name, email, venture_id)
        return {"kind": "navigation", "citations": [], "actions": [{**open_page, "style": "primary"}],
                "body_md": f"Here is exactly how to sign in to {name}:\n\n{_numbered(steps)}"}
    from app.connections import hub

    started = hub.connect_provider(wid, slug, **options)
    url = started.get("redirect_url") or started.get("authorize_url")
    if is_local() and slug == "gmail" and not url:
        message = ("Your email is connected through your saved SMTP settings. Open Find customers and ask me to send your reviewed outreach."
                   if started.get("ok") else "Your email could not be connected. Open Settings > Keys and connections > Your mail account, check your SMTP host, sender email and mail app password, then retry.")
        return {"kind": "text" if started.get("ok") else "navigation", "citations": [],
                "actions": [{"label": "Open Keys and connections", "href": "/settings/local", "style": "ghost"}],
                "body_md": message}
    if is_local() and (started.get("ok") is False or not started.get("ok") and not url):
        from app.connections import local_config
        from app.secrets.detect import looks_like_secret
        message = started.get("message")
        if not isinstance(message, str) or not message.strip() or looks_like_secret(message) or re.search(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b", message):
            message = local_config.setup_message(slug)
        else:
            next_step = local_config.setup_message(slug)
            if next_step not in message:
                message += " " + next_step
        return {"kind": "navigation", "citations": [], "actions": [{**open_page, "style": "primary"}],
                "body_md": re.sub(r"\s*\u2014\s*", ", ", message.strip())}
    if slug == "x" and not url:
        from app.chat.posts import guide_reply  # X has no managed app here; say so and offer the ones that work

        return guide_reply()
    steps = _steps(slug, name, email, venture_id)
    if slug in _composio() and url:
        _save_watch(venture_id, slug, wid, **({"oauth_state": started.get("oauth_state")} if multi else {}))
        manual = (f"Prefer the page? Click {nav.label('connections')} in the left sidebar, choose your company, find the {name} card, "
                  f"click {nav.ui('connect')}, then {nav.ui('open_auth', name=name)}; afterwards click {nav.ui('refresh_all')}. "
                  "The link expires after a few minutes; ask me again for a fresh one.")
        return {"kind": "navigation", "citations": [], "watch": slug,
                "body_md": f"Here is how to connect {name} (about a minute):\n\n{_numbered(steps)}\n\n{manual}",
                "actions": [{"label": f"Connect {name}", "href": url, "style": "primary"}, open_page]}
    if not started.get("ok"):
        return {"kind": "navigation", "citations": [], "actions": [{**open_page, "style": "primary"}],
                "body_md": f"{name} could not be connected right now. You can keep working without it, try again from Connections, or contact support."}
    why = started.get("message") or started.get("note") or ""
    if started.get("ok") and not url:
        lead = f"**{name} is set up on this installation ✓**"
    else:
        lead = f"I could not create a {name} link myself right now. {why}".strip()
    return {"kind": "navigation", "citations": [], "actions": [{**open_page, "style": "primary"}],
            "body_md": f"{lead}\n\nHere is what to do:\n\n{_numbered(steps)}"}


def status(venture_id: str) -> dict[str, Any]:
    from app.connections import hub

    try:
        workspace_id, _ = _scope(venture_id)
        snapshot = hub.hub_status(workspace_id)
        connections = snapshot["connections"]
    except Exception as exc:  # noqa: BLE001
        from app.chat.recovery import founder_failure
        return founder_failure(exc, venture_id, "your connection status", {"action": "connections.status", "payload": {}},
                               safety="I could not confirm which accounts are connected. Nothing was changed.")
    lines, actions = [], []
    services = _services()
    for slug in snapshot["order"]:
        if slug not in services and not is_local():
            continue
        connection = connections.get(slug) or {}
        name = services.get(slug, (connection.get("name") or slug.title(), ""))[0]
        if is_local() and (slug == "smtp" or connection.get("connectable") is False
                and (not connection.get("connected") or not connection.get("configured", True))):
            ready = bool(connection.get("ready") or connection.get("ok"))
            if slug == "smtp":
                next_step = ("Your email settings are saved. Use Send test email on Keys and connections to check delivery." if ready
                    else connection.get("hint") or "Add your email server, sender and password on Keys and connections, then use Send test email.")
            elif ready:
                next_step = "Your settings are saved. Review them on Keys and connections before connecting your apps."
            else:
                from app.connections import local_config

                missing = local_config.missing(slug)
                fields = ["Composio API key" if field == "composio.api_key" else f"{name} authorization ID"
                    for field in missing if field != "connected_apps"]
                next_step = (f"Add your {' and '.join(fields)} on Keys and connections, then connect {name} on Connections." if fields
                    else local_config.blocked_message("connected_apps"))
                if fields and "connected_apps" in missing:
                    next_step += " " + local_config.blocked_message("connected_apps")
            lines.append(f"- **{name}**: {'Configured' if ready else 'Needs setup'}. {next_step}")
            actions.append({"label": f"{'Review' if ready else 'Set up'} {name}", "href": "/settings/local", "style": "ghost"})
            continue
        if slug == "gmail" and connection.get("multi_gmail_v1"):
            for account in connection.get("accounts") or []:
                address = account.get("email") or "Gmail inbox"
                state = "Needs reconnect" if account.get("needs_reconnect") else account.get("status", "unavailable")
                lines.append(f"- **Gmail {address}**: {state}" + (" (default)" if account.get("is_default") else ""))
                verb = "Reconnect" if account.get("needs_reconnect") else "Disconnect"
                if account.get("status") != "revoked":
                    actions.append({"label": f"{verb} {address}", "action": "connections.connect" if verb == "Reconnect" else "connections.disconnect",
                                    "payload": {"query": f"{verb.lower()} gmail {address} inbox {account['id']}", "mailbox_id": account["id"]}, "style": "ghost"})
            actions.append({"label": "Add another Gmail", "action": "connections.connect", "payload": {"query": "add another gmail"}, "style": "primary"})
            continue
        connected = bool(connection.get("connected"))
        reconnect = bool(connection.get("needs_reconnect") or (slug != "gmail" and connected and not connection.get("ready", connection.get("ok"))))
        address = connection.get("account_email") or connection.get("handle")
        # shortcut: Calendar has no profile lookup, add verified identity when its connector exposes it.
        if slug == "google_calendar" and address and "@" not in address:
            address = None
        state = "Needs reconnect" if reconnect else (f"Connected as {address}" if address else "Connected") if connected else "Not connected"
        lines.append(f"- **{name}**: {state}")
        verb = "Reconnect" if reconnect else "Disconnect" if connected else "Connect"
        actions.append({"label": f"{verb} {name}", "action": "connections.disconnect" if verb == "Disconnect" else "connections.connect",
                        "payload": {"query": f"{verb.lower()} {name}"}, "style": "ghost"})
    return {"kind": "connections_status", "body_md": "Your connections\n\n" + "\n".join(lines), "actions": actions, "citations": []}


def disconnect(venture_id: str, payload: dict[str, Any], confirmation_message_id: str | None = None) -> dict[str, Any]:
    from app.connections import hub

    slug = payload.get("provider") or service_from(str(payload.get("query") or ""))
    if slug not in _composio():
        raise ValueError("connection_provider_required")
    workspace_id, _ = _scope(venture_id)
    name = _services()[slug][0]
    if confirmation_message_id:
        from app.chat.confirmations import card
        from app.connections.mailboxes import enabled

        card(venture_id, confirmation_message_id, "connections.disconnect", payload)
        if slug == "gmail" and enabled():
            if not payload.get("mailbox_id") or not payload.get("expected_updated_at"):
                raise ValueError("choose_one_mailbox")
            hub.disconnect_mailbox(workspace_id, payload["mailbox_id"], confirmed=True,
                                   expected_updated_at=payload["expected_updated_at"])
        else:
            hub.disconnect_provider(workspace_id, slug, accounts=payload.get("accounts") or {})
        from app.ventures.state_brief import invalidate_venture_state_brief

        invalidate_venture_state_brief(venture_id)
        multi = slug == "gmail" and enabled()
        return {"kind": "text", "body_md": "You disconnected this Gmail inbox. Your other inboxes are unchanged." if multi else f"You disconnected {name}. You can connect it again whenever you need it.",
                "actions": [{"label": "Add another Gmail" if multi else f"Connect {name}", "action": "connections.connect",
                             "payload": {"query": "add another gmail" if multi else f"connect {name}"}, "style": "primary"}],
                "citations": [], "action_started": True}
    accounts = {account_id: connection_id for account_id, connection_id in hub.disconnect_accounts(workspace_id, slug).items()
                if connection_id is not None}
    from app.connections.mailboxes import enabled
    multi = slug == "gmail" and enabled()
    if multi:
        mailbox_id = payload.get("mailbox_id")
        if mailbox_id is None:
            from app.gtm.supymem.pipeline import list_mailboxes
            query = str(payload.get("query") or "").casefold()
            candidates = [account["id"] for account in list_mailboxes(workspace_id)
                          if account["id"] in accounts and account.get("email") and account["email"].casefold() in query]
            mailbox_id = candidates[0] if len(candidates) == 1 else None
        if mailbox_id is None:
            if len(accounts) > 1:
                return {"kind": "navigation", "body_md": "Choose the Gmail inbox you want to disconnect in Connections.",
                        "actions": [{"label": "Open Connections", "href": nav.path("connections", venture_id), "style": "primary"}], "citations": [{"question": True}]}
        elif mailbox_id not in accounts:
            raise ValueError("mailbox_unavailable")
        else:
            accounts = {mailbox_id: accounts[mailbox_id]}
    if not accounts:
        return {"kind": "text", "body_md": f"{name} is not connected. You can connect it whenever you need it.",
                "actions": [{"label": f"Connect {name}", "action": "connections.connect", "payload": {"query": f"connect {name}"}}], "citations": []}
    if multi:
        from app.gtm.supymem.pipeline import list_mailboxes
        mailbox_id = next(iter(accounts))
        account = next(account for account in list_mailboxes(workspace_id) if account["id"] == mailbox_id)
        hub.disconnect_mailbox(workspace_id, mailbox_id, confirmed=True,
                               expected_updated_at=payload.get("expected_updated_at") or account["updated_at"])
    else:
        hub.disconnect_provider(workspace_id, slug, accounts=accounts)
    from app.ventures.state_brief import invalidate_venture_state_brief
    invalidate_venture_state_brief(venture_id)
    return {"kind": "text", "body_md": f"You disconnected {name}. You can connect it again whenever you need it.",
            "actions": [{"label": f"Connect {name}", "action": "connections.connect", "payload": {"query": f"connect {name}"}, "style": "primary"}],
            "citations": [], "action_started": True}
