"""What a founder's "post this on LinkedIn" actually asks to publish.

The instruction is never the post. The text is what follows the first colon, or a quoted passage;
with neither there is no text, and chat asks for it instead of publishing the command itself.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor

from app.actuation.social import wanted_platforms
from app.chat.intent import _CONTENT_OTHER_RE

MAX_CHARS = {"x": 280, "twitter": 280, "linkedin": 3000, "reddit": 3000}
_QUOTED = re.compile(r"[\"“”](.{3,3000}?)[\"“”]", re.S)
LABELS = {"twitter": "X (Twitter)", "linkedin": "LinkedIn", "instagram": "Instagram", "facebook": "Facebook",
          "tiktok": "TikTok", "threads": "Threads"}


def post_request(text: str) -> tuple[str, list[str]]:
    """(exact post text, platforms named). Text is '' when the founder gave none."""
    raw = (text or "").strip()
    body, instruction = "", raw
    head, sep, rest = raw.partition(":")
    if sep and rest.strip():
        body, instruction = rest.strip(), head
    elif (m := _QUOTED.search(raw)):
        body, instruction = m.group(1).strip(), raw.replace(m.group(0), " ")
    platforms = wanted_platforms(chat=instruction)
    if re.search(r"\btweet", instruction, re.I) and "twitter" not in platforms:
        platforms.append("twitter")
    if re.search(r"\breddit\b|\br/\w", instruction, re.I):
        platforms.append("reddit")
    return body[:min((MAX_CHARS.get(p, 280) for p in platforms), default=280)], platforms


def where(platforms: list[str]) -> str:
    return " and ".join(LABELS.get(p, p.title()) for p in platforms) if platforms else "every connected account"


_FAILURES = (
    ("empty_content", "There was no text to post."),
    ("no_connected_accounts", "No social account is connected yet."),
    ("account_inactive", "That account needs reconnecting."),
)


def failure_copy(reason: str | None) -> str:
    code = reason or ""
    return next((msg for prefix, msg in _FAILURES if code.startswith(prefix)),
                "The site did not accept it.")


_DISCONNECT = re.compile(r"^\s*(?:(?:please|can you|could you)[,\s]+)*(?:disconnect|unlink|remove)\b", re.I)
_REPORT = re.compile(r"^\s*(?:i|we)(?:'ve| have)?\s+(?:just\s+)?(?:posted|shared|tweeted|published)\b", re.I)
_UNSUPPORTED = re.compile(r"\b(?:youtube|github)\b", re.I)


def content_reply(venture_id: str, text: str) -> dict | None:
    """Disconnect and report requests, and sites I cannot post to. None: this asks me to write, so write it."""
    if _DISCONNECT.match(text) and _CONTENT_OTHER_RE.match(text):
        body = ("I can't disconnect an account from chat. Open Connections to disconnect it there. "
                "Nothing was changed.")
        actions = [{"label": "Open connections", "href": f"/v/{venture_id}/connections", "style": "primary"}]
    elif _REPORT.match(text):
        body = ("Thanks for telling me. I can't see posts made outside Supymem, so I did not record anything or "
                "search for it. If it was a test, tell me how it went and I can log the result.")
        actions = []
    elif _UNSUPPORTED.search(text):
        body = ("I can post to LinkedIn and Reddit from a connected account, not to that site. Nothing was posted. "
                "I can write the text for you to paste there yourself.")
        actions = []
    else:
        return None
    return {"kind": "text", "body_md": body, "actions": actions, "citations": []}


WRITE_GUIDANCE = ("Write the post itself in the founder's voice, ready to publish, using what you know about this venture. "
                  "Reply with only the post text: no preface, no options, no diagram, no notes about tools or posting.")


class DraftReply(dict):
    """Completed authored material, never a status or failed completion."""


def draft_actions(text: str, draft: str) -> list[dict]:
    """One click publishes the post I just wrote; nothing was published yet."""
    from app.chat.actions import get_action, social_post_enabled
    from app.chat.confirm import ConfirmationCard

    _, platforms = post_request(text)
    platforms = [SLUGS.get(p, p) for p in platforms if SLUGS.get(p, p) in POSTABLE] or ["linkedin"]
    if not social_post_enabled() or not draft.strip():
        return []
    content = draft.strip()
    if any(len(content) > MAX_CHARS[platform] for platform in platforms):
        return []
    card = ConfirmationCard.for_action(get_action("social_post"), interpretation=f"Post this on {where(platforms)}",
        consequences=[f"This exact text goes public on {where(platforms)}."],
        resulting_state={"query": text, "content": content, "platforms": platforms})
    return [card.as_action()]


# --------------------------------------------------------------- publishing
SLUGS = {"twitter": "x"}
POSTABLE = ("linkedin", "reddit", "x")
_SUBREDDIT = re.compile(r"\br/([A-Za-z0-9_]{2,50})\b")


def _connect(name: str) -> dict:
    return {"label": f"Connect {name}", "action": "connections.connect", "payload": {"query": f"connect {name}"}, "style": "primary"}


def _x_available() -> bool:
    from app.connections import local_config

    return bool(local_config.auth_config_id("x"))


def _accounts(workspace_id: str) -> dict:
    from sqlalchemy import select

    from app.db.models import ContentAccount
    from app.state import db

    with db()() as session:
        rows = session.scalars(select(ContentAccount).where(ContentAccount.workspace_id == workspace_id,
            ContentAccount.status == "active").order_by(ContentAccount.created_at)).all()
        return {row.platform: row.id for row in reversed(rows) if row.platform in POSTABLE}


def _workspace(venture_id: str) -> str:
    from app.db.models import Venture
    from app.state import db

    with db()() as session:
        return session.get(Venture, venture_id).workspace_id


def live(platform: str = "linkedin") -> bool:
    """Real config, the same checks the publisher enforces: never a guess about what would go out."""
    from app.approvals.external import OutboundExecutionError, execution_enabled, outbound_rate
    from app.chat.actions import social_post_enabled
    from app.content.platforms import dry_run_enabled
    from app.core.local_mode import is_local
    from app.core.local_capabilities import enabled

    live_enabled = enabled("connected_apps") if is_local() else social_post_enabled() and execution_enabled()
    if not (live_enabled and not dry_run_enabled()):
        return False
    try:
        outbound_rate("content_publish", f"composio_{platform}")
    except OutboundExecutionError:
        return False
    return True


def status_reply(venture_id: str) -> dict:
    try:
        connected = [LABELS.get(p, "X") for p in _accounts(_workspace(venture_id))]
        posting_enabled = live()
    except Exception as error:
        from app.chat.recovery import founder_failure
        return founder_failure(error, venture_id, "your connected social accounts",
            {"action": "social_status", "payload": {}},
            safety="I could not check your posting accounts. Open Settings > Keys and connections > Your connected apps to review your saved connections, then retry.")
    names = " and ".join(connected)
    if not posting_enabled:
        body = ("Posting from Supymem is not switched on yet" + (f", though {names} is connected" if connected else "")
                + ". I can write your post here for you to paste.")
        return {"kind": "text", "body_md": body, "actions": [], "citations": []}
    if connected:
        return {"kind": "text", "body_md": f"You can post to {names} from here. Tell me what to post.", "actions": [], "citations": []}
    return guide_reply()


def guide_reply() -> dict:
    x_note = "" if _x_available() else " X is not available yet."
    return {"kind": "needs_you", "body_md": f"Connect an account and I can post for you. You sign in on the site itself.{x_note}",
            "actions": [_connect("LinkedIn"), {**_connect("Reddit"), "style": "secondary"}], "citations": []}


def _pending_id(venture_id: str, platform: str) -> str:
    return f"post_pending:{venture_id}:{platform}"


def _save_pending(venture_id, platform, content, target, actor_subject) -> None:
    from app.db.models import VentureDocument
    from app.state import db
    from core.clock import utcnow

    now, doc_id = utcnow(), _pending_id(venture_id, platform)
    with db()() as session:
        doc = session.get(VentureDocument, doc_id)
        if doc is None:
            doc = VentureDocument(id=doc_id, venture_id=venture_id, kind="post_pending",
                                  title=f"Post waiting for {platform}", created_at=now, updated_at=now)
            session.add(doc)
        doc.body_md, doc.updated_at = json.dumps({"content": content, "target": target, "actor": actor_subject}), now
        session.commit()


def finish_pending(venture_id: str, platform: str) -> dict | None:
    """The account the founder connected for a post is ready: publish exactly that post once."""
    from app.db.models import VentureDocument
    from app.state import db

    with db()() as session:
        doc = session.get(VentureDocument, _pending_id(venture_id, platform))
        if doc is None:
            return None
        saved = json.loads(doc.body_md or "{}")
        session.delete(doc)
        session.commit()
    # Runs off any caller's event loop: connection checks are synchronous and may sit under one.
    with ThreadPoolExecutor(1) as pool:
        reply = pool.submit(asyncio.run, publish(venture_id, saved["content"], [platform], saved.get("actor"), target=saved.get("target"))).result()
    from app.chat.store import append_message
    append_message(venture_id, role="assistant", kind=reply["kind"], body_md=reply["body_md"], actions=reply["actions"],
                   citations=reply["citations"], idempotency_key=f"post_pending:{hashlib.sha256(saved['content'].encode()).hexdigest()}:{platform}")
    return reply


def _create_post(workspace_id, venture_id, account_id, platform, content, target, *, authored_draft=False) -> str:
    from datetime import timedelta

    from sqlalchemy import select

    from app.content.platforms import dry_run_enabled
    from app.db.models import ContentBrief, ContentPost, ContentTopic
    from app.state import db
    from core.clock import utcnow
    from core.ids import new_id

    now = utcnow()
    with db()() as session:
        # The same text just published (a repeated request or double click) is never posted twice.
        repeated = select(ContentPost.id).where(ContentPost.workspace_id == workspace_id, ContentPost.platform == platform,
            ContentPost.body == content, ContentPost.state.in_(("draft",) if authored_draft else ("approved", "published")),
            ContentPost.created_at >= now - timedelta(minutes=10))
        if authored_draft:
            repeated = repeated.join(ContentTopic).join(ContentBrief).where(
                ContentPost.workspace_id == ContentTopic.workspace_id, ContentTopic.workspace_id == ContentBrief.workspace_id,
                ContentBrief.venture_id == venture_id)
        repeat = session.scalar(repeated)
        if repeat:
            return repeat
        brief = session.scalar(select(ContentBrief.id).where(ContentBrief.workspace_id == workspace_id,
            ContentBrief.venture_id == venture_id).order_by(ContentBrief.version.desc()).limit(1))
        if brief is None:  # Posts need a brief row; an archived one is never picked up as the venture's active brief.
            brief = new_id("cbr")
            session.add(ContentBrief(id=brief, workspace_id=workspace_id, venture_id=venture_id, version=1, status="archived", created_at=now))
            session.flush()
        topic = new_id("ctp")
        from app.org.manager import manager_name

        session.add(ContentTopic(id=topic, workspace_id=workspace_id, brief_id=brief,
            angle="Your post from chat" if authored_draft else "Founder's post from chat", hook=content[:160],
            rationale=f"Written by {manager_name(venture_id)} at your request" if authored_draft else "Founder's exact words",
            intent="value", evidence_json="[]", score=1.0, status="drafted", created_at=now))
        session.flush()
        post = new_id("cps")
        session.add(ContentPost(id=post, workspace_id=workspace_id, topic_id=topic, account_id=account_id, platform=platform, intent="value",
            body=content, title=content.split("\n", 1)[0][:100] if platform == "reddit" else None, target=target,
            state="draft" if authored_draft else "approved", lint_json="{}", critic_json="{}", visual_json="{}",
            dry_run=authored_draft or bool(dry_run_enabled()), created_at=now, updated_at=now))
        session.commit()
        return post


def _read_back(venture_id: str, platform: str, external_id: str) -> bool:
    """True only when the site itself returned the published post."""
    from app.connections import crm
    from app.experiments import tools

    if not tools.enabled() or platform not in crm.POST_READS:
        return False

    def collect(read, _workspace_id):
        identifier = external_id if platform != "linkedin" or external_id.startswith("urn:li:") else "urn:li:share:" + external_id
        read("post_replies", {"post_id": identifier.removeprefix("t3_")})
        return {}
    try:
        return (crm._reader(venture_id, platform, collect) or {}).get("status") == "connected"
    except Exception as exc:  # noqa: BLE001 - the post is live either way; the reply only omits the check
        from app.chat.recovery import log_founder_exception
        log_founder_exception(exc, venture_id, "social.read_back")
        return False


async def publish(venture_id: str, content: str, platforms: list[str], actor_subject: str | None, *, target: str | None = None) -> dict:
    """Publish the founder's exact words from their own connected account, then check the site has them."""
    from app.approvals import service
    from app.approvals.external import content_post_review, propose_external_approval
    from app.authz.workspace import subject_person_id
    from app.content.engine import execute_approved_post

    def reply(body, actions=(), kind="text"):
        return {"kind": kind, "body_md": body, "actions": list(actions), "citations": []}

    kept = f"Nothing went public. Your text:\n\n{content}"
    workspace_id = await asyncio.to_thread(_workspace, venture_id)
    accounts = await asyncio.to_thread(_accounts, workspace_id)
    wanted = list(dict.fromkeys(SLUGS.get(p, p) for p in platforms))
    if any(p not in POSTABLE for p in wanted):
        return reply(f"I can post to LinkedIn and Reddit from a connected account, not to {where([p for p in wanted if p not in POSTABLE])}. {kept}")
    if not wanted and len(accounts) != 1:
        return reply(f"Where should this go, LinkedIn or Reddit? Say \"post to LinkedIn: your text\". {kept}",
                     [] if accounts else guide_reply()["actions"], "needs_you")
    platform = wanted[0] if wanted else next(iter(accounts))
    name = LABELS.get(platform, "X")
    if platform == "x" and platform not in accounts and not _x_available():
        return reply(f"X is not available yet. LinkedIn and Reddit are. {kept}", guide_reply()["actions"], "needs_you")
    if not live(platform):
        return reply(f"Posting from Supymem is not switched on yet, so you can paste this yourself. {kept}")
    if platform == "reddit" and not target:
        return reply(f"Which subreddit? Say \"post to Reddit r/name: your text\". {kept}", kind="needs_you")
    if platform not in accounts:
        await asyncio.to_thread(_save_pending, venture_id, platform, content, target, actor_subject)
        return reply(f"Connect {name} and I will post this as soon as you are back. Nothing has been posted yet:\n\n{content}",
                     [_connect(name)], "needs_you")

    post_id = await asyncio.to_thread(_create_post, workspace_id, venture_id, accounts[platform], platform, content, target)
    try:
        review_target, review = await asyncio.to_thread(content_post_review, workspace_id, post_id)
    except ValueError:  # Already published moments ago: the repeat is not sent again.
        return reply(f"That is already posted on {name}.")
    approval = await asyncio.to_thread(lambda: propose_external_approval(workspace_id=workspace_id, subject_ref=post_id, action="content_publish",
        target=review_target, summary=f"Publish the founder's post to {name}", review=review))
    if approval["status"] == "pending":
        try:
            # The founder asked for exactly this text in chat; the approval records that request.
            await asyncio.to_thread(lambda: service.decide(approval["id"], decision="approved", decided_by=subject_person_id(actor_subject),
                note="Founder asked for this exact post in chat", surface="chat"))
        except service.ApprovalError:
            return reply(f"Only someone who can approve work in this workspace can post for it. {kept}")
    result = await execute_approved_post(workspace_id, post_id, approval_id=approval["id"])
    if result.get("ok") and result.get("mode") == "live":
        checked = bool(result.get("external_id")) and await asyncio.to_thread(_read_back, venture_id, platform, result["external_id"])
        url = result.get("external_url")
        body = f"Posted to {name}" + (f": {url}" if url else ".") + (" I checked and it is live." if checked else "")
        return reply(f"{body}\n\n{content}", [{"label": "Open post", "href": url, "style": "primary"}] if url else [])
    if result.get("reason") == "insufficient_credits":
        from app.chat.blockers import explain
        return explain(venture_id, {"code": "insufficient_credits"})
    return reply(f"I could not post it. {failure_copy(result.get('reason'))} {kept}")
