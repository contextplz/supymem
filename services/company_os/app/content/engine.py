"""The loop: refill topics, draft against gates, publish what is due.

Nothing here waits on a person. Every refusal is written to content_events so
the founder can read why the machine declined rather than approve each post.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import timedelta
from typing import Any

from core.clock import utcnow
from core.ids import new_id
from sqlalchemy import select

from app.content import brief as brief_store
from app.content import director
from app.content.oauth import flow as oauth_flow
from app.content.platforms import PUBLISHERS, dry_run_enabled
from app.content.platforms.reddit import fetch_rules
from app.content.safety import cadence, originality
from app.content.writer import compose
from app.db.models import ContentAccount, ContentEvent, ContentPost, ContentTopic
from app.state import db

log = logging.getLogger(__name__)

#: Keep this many drafts scheduled per platform before drafting more.
TARGET_SCHEDULED = 2
TOPIC_LOW_WATER = 4


def record_event(
    *,
    workspace_id: str,
    kind: str,
    platform: str | None = None,
    post_id: str | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    factory = db()
    with factory() as session:
        session.add(
            ContentEvent(
                id=new_id("cev"),
                workspace_id=workspace_id,
                kind=kind,
                platform=platform,
                post_id=post_id,
                payload_json=json.dumps(payload or {}, default=str)[:8000],
                created_at=utcnow(),
            )
        )
        session.commit()


async def _warden_allows(workspace_id: str, platform: str) -> tuple[bool, str]:
    """Ask Warden directly.

    ``post_public`` is an autonomy dimension owned by Warden and defined in
    config/autonomy_profiles.yaml, not one of the seven workspace role
    capabilities, so this does not go through the role gate. Warden fails
    closed, and the kill switch lives on the same call.
    """
    from app.core.warden_client import warden

    resp = await warden.check(
        capability="post_public",
        venture_id=workspace_id,
        target=platform,
        risk="medium",
    )
    return resp.decision == "allow", resp.reason or resp.decision


def _published_hashes(workspace_id: str, limit: int = 400) -> list[str]:
    """Fingerprints of posts that went out or are about to.

    Refused drafts are excluded: they were never seen by anyone, so they must
    not block a later draft that happens to resemble them.
    """
    factory = db()
    with factory() as session:
        rows = session.scalars(
            select(ContentPost.simhash)
            .where(
                ContentPost.workspace_id == workspace_id,
                ContentPost.simhash.is_not(None),
                ContentPost.state.in_(("published", "scheduled", "approved")),
            )
            .order_by(ContentPost.created_at.desc())
            .limit(limit)
        ).all()
        return [r for r in rows if r]


def _scheduled_count(workspace_id: str, platform: str) -> int:
    factory = db()
    with factory() as session:
        rows = session.scalars(
            select(ContentPost.id).where(
                ContentPost.workspace_id == workspace_id,
                ContentPost.platform == platform,
                ContentPost.state.in_(("scheduled", "approved")),
            )
        ).all()
        return len(rows)


def _pick_target(brief: dict[str, Any], topic: dict[str, Any]) -> str | None:
    """Choose the subreddit for a Reddit post from the ICP's watering holes."""
    segments = (brief.get("icp") or {}).get("segments") or []
    wanted = topic.get("icp_segment")
    ordered = [s for s in segments if s.get("name") == wanted] + segments
    for seg in ordered:
        for hole in seg.get("watering_holes") or []:
            text = str(hole).strip()
            if text.lower().startswith(("r/", "/r/")):
                return text.lstrip("/").removeprefix("r/")
    return None


async def draft_for_platform(
    *, workspace_id: str, platform: str
) -> dict[str, Any]:
    """Take one topic, compose it for a platform, and schedule or refuse it."""
    current = brief_store.active(workspace_id)
    if current is None:
        return {"ok": False, "reason": "no_brief"}

    account = oauth_flow.active_account(workspace_id, platform)
    if account is None:
        return {"ok": False, "reason": "not_connected"}
    account_id = account["id"]
    daily_cap = account["daily_cap"]
    author_urn = account["author_urn"]
    last_post_at = account["last_post_at"]

    ratio = int((current.get("cadence") or {}).get("value_to_promo") or 4)
    intent_filter = None
    promo = cadence.promo_allowed(
        workspace_id=workspace_id, platform=platform, ratio=ratio
    )
    if not promo.ok:
        intent_filter = "value"

    topic = director.take_topic(workspace_id, intent=intent_filter)
    if topic is None:
        return {"ok": False, "reason": "no_topics"}

    subreddit = _pick_target(current, topic) if platform == "reddit" else None
    live_rules: list[str] = []
    if platform == "reddit" and subreddit and not dry_run_enabled():
        try:
            token = oauth_flow.access_token(account_id)
            fetched = fetch_rules(subreddit=subreddit, access_token=token)
            # Feed the community's own words to the linter, not our snapshot.
            live_rules = [
                r["short_name"]
                for r in fetched.get("rules") or []
                if "no self" in r.get("description", "").lower()
                or "no promo" in r.get("description", "").lower()
            ]
            record_event(
                workspace_id=workspace_id,
                kind="reddit.rules_fetched",
                platform="reddit",
                payload=fetched,
            )
        except Exception as exc:
            log.info("reddit rules fetch failed for r/%s: %s", subreddit, exc)

    draft = await compose(
        topic=topic,
        brief=current,
        platform=platform,
        workspace_id=workspace_id,
        subreddit_rules=live_rules,
    )

    fingerprint = originality.simhash(draft.body) if draft.body else None
    duplicate = False
    skip_dedup = os.environ.get("CONTENT_SKIP_NEAR_DUPLICATE", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    if draft.ok and fingerprint and not skip_dedup:
        duplicate, distance = originality.is_duplicate(
            draft.body, _published_hashes(workspace_id)
        )
        if duplicate:
            draft.ok = False
            draft.refusal_reason = f"near_duplicate:hamming={distance}"

    now = utcnow()
    slot = cadence.next_slot(platform=platform, after=last_post_at or now)
    post_id = new_id("cps")
    factory = db()
    with factory() as session:
        session.add(
            ContentPost(
                id=post_id,
                workspace_id=workspace_id,
                topic_id=topic["id"],
                account_id=account_id,
                platform=platform,
                intent=topic.get("intent") or "value",
                body=draft.body,
                title=draft.title,
                target=subreddit,
                state="scheduled" if draft.ok else "refused",
                simhash=fingerprint,
                lint_json=json.dumps(draft.lint.to_dict() if draft.lint else {}),
                critic_json=json.dumps(draft.critic, default=str),
                visual_json=json.dumps(draft.visual or {}, default=str),
                refusal_reason=draft.refusal_reason or None,
                scheduled_for=slot if draft.ok else None,
                dry_run=dry_run_enabled(),
                created_at=now,
                updated_at=now,
            )
        )
        topic_row = session.get(ContentTopic, topic["id"])
        if topic_row is not None and not draft.ok:
            # A refused topic goes back so a different platform can try it.
            topic_row.status = "queued" if "near_duplicate" not in (
                draft.refusal_reason or ""
            ) else "dead"
        session.commit()

    record_event(
        workspace_id=workspace_id,
        kind="post.scheduled" if draft.ok else "post.refused",
        platform=platform,
        post_id=post_id,
        payload={
            "angle": topic["angle"],
            "intent": topic.get("intent"),
            "attempts": draft.attempts,
            "refusal_reason": draft.refusal_reason,
            "lint": draft.lint.to_dict() if draft.lint else {},
            "critic": draft.critic,
            "scheduled_for": slot.isoformat() if draft.ok else None,
        },
    )
    return {
        "ok": draft.ok,
        "post_id": post_id,
        "platform": platform,
        "reason": draft.refusal_reason or "",
        "scheduled_for": slot.isoformat() if draft.ok else None,
        "daily_cap": daily_cap,
        "author_urn": bool(author_urn),
    }


async def publish_due(
    *, workspace_id: str, limit: int = 5, approval_id: str | None = None
) -> list[dict[str, Any]]:
    """Publish due drafts only when each live post has an exact approval."""
    now = utcnow()
    factory = db()
    with factory() as session:
        due = session.scalars(
            select(ContentPost)
            .where(
                ContentPost.workspace_id == workspace_id,
                ContentPost.state == "scheduled",
                ContentPost.scheduled_for.is_not(None),
                ContentPost.scheduled_for <= now,
            )
            .order_by(ContentPost.scheduled_for)
            .limit(limit)
        ).all()
        pending = [
            {
                "id": p.id,
                "platform": p.platform,
                "account_id": p.account_id,
                "body": p.body,
                "title": p.title,
                "target": p.target,
                "visual_path": p.visual_path,
            }
            for p in due
        ]

    results: list[dict[str, Any]] = []
    for item in pending:
        results.append(
            await _publish_one(
                workspace_id=workspace_id, post=item, approval_id=approval_id
            )
        )
    return results


async def _publish_one(
    *, workspace_id: str, post: dict[str, Any], approval_id: str | None = None
) -> dict[str, Any]:
    platform = post["platform"]
    post_id = post["id"]

    allowed, reason = await _warden_allows(workspace_id, platform)
    if not allowed:
        _mark(post_id, state="scheduled", refusal_reason=f"warden:{reason}"[:400])
        record_event(
            workspace_id=workspace_id,
            kind="post.blocked",
            platform=platform,
            post_id=post_id,
            payload={"reason": reason},
        )
        return {"post_id": post_id, "ok": False, "reason": f"warden:{reason}"}

    factory = db()
    with factory() as session:
        account = session.get(ContentAccount, post["account_id"] or "")
        if account is None or account.status != "active":
            _mark(post_id, state="failed", refusal_reason="account_inactive")
            return {"post_id": post_id, "ok": False, "reason": "account_inactive"}
        cap_verdict = cadence.check_cadence(
            platform=platform,
            daily_cap=account.daily_cap,
            posts_today=(
                account.posts_today
                if account.day_key == cadence.day_key()
                else 0
            ),
            last_post_at=account.last_post_at,
        )
        handle = account.handle
        author_urn = account.author_urn or ""
        account_id = account.id
        try:
            account_extra = json.loads(account.extra_json or "{}")
        except Exception:
            account_extra = {}

    if not cap_verdict.ok:
        # Not a failure: slide the slot and try on a later tick.
        later = cadence.next_slot(platform=platform)
        _reschedule(post_id, later)
        record_event(
            workspace_id=workspace_id,
            kind="post.deferred",
            platform=platform,
            post_id=post_id,
            payload={"reason": cap_verdict.reason, "next": later.isoformat()},
        )
        return {"post_id": post_id, "ok": False, "reason": cap_verdict.reason}

    publisher = PUBLISHERS.get(platform)
    if publisher is None:
        _mark(post_id, state="failed", refusal_reason="no_publisher")
        return {"post_id": post_id, "ok": False, "reason": "no_publisher"}

    composio_ca = ""
    composio_uid = ""
    if isinstance(account_extra, dict) and account_extra.get("auth_via") == "composio":
        composio_ca = str(account_extra.get("connected_account_id") or "")
        composio_uid = str(account_extra.get("composio_user_id") or "")

    # Composio is a real provider. Do not let a connected account bypass the
    # rate/approval/reservation boundary that native publishers observe.
    if dry_run_enabled():
        from app.content.platforms.base import simulated

        result = simulated(platform)
    else:
        from app.approvals.external import (
            DefinitiveProviderFailure,
            OutboundExecutionError,
            execute_metered_external_action,
        )

        provider = f"composio_{platform}" if composio_ca and composio_uid else f"native_{platform}"
        token = ""
        if composio_ca and composio_uid:
            from app.content import composio_social

            if not composio_social.configured():
                _mark(post_id, state="scheduled", refusal_reason="outbound_provider_credentials_unavailable")
                return {"post_id": post_id, "ok": False, "reason": "outbound_provider_credentials_unavailable"}
        else:
            try:
                token = oauth_flow.access_token(account_id)
            except oauth_flow.OAuthError as exc:
                _mark(post_id, state="failed", refusal_reason=f"token:{exc.code}")
                record_event(
                    workspace_id=workspace_id,
                    kind="post.failed",
                    platform=platform,
                    post_id=post_id,
                    payload={"reason": exc.message},
                )
                return {"post_id": post_id, "ok": False, "reason": exc.code}

        def invoke():
            if composio_ca and composio_uid:
                candidate = composio_social.publish(
                    platform=platform,
                    body=post["body"],
                    connected_account_id=composio_ca,
                    composio_user_id=composio_uid,
                    author_urn=author_urn,
                    title=post.get("title"),
                    target=post.get("target"),
                    image_paths=[post["visual_path"]] if post.get("visual_path") else None,
                )
            else:
                kwargs: dict[str, Any] = {
                    "body": post["body"],
                    "access_token": token,
                    "author_urn": author_urn,
                    "title": post.get("title"),
                    "target": post.get("target"),
                }
                if platform == "x":
                    kwargs["handle"] = handle
                candidate = publisher(**kwargs)
            if candidate.ok:
                return candidate
            if candidate.fatal or candidate.status_code in {400, 401, 403, 404, 422}:
                raise DefinitiveProviderFailure(candidate.error or "provider rejected publication")
            raise RuntimeError(candidate.error or "provider outcome unknown")

        try:
            outcome = execute_metered_external_action(
                workspace_id=workspace_id,
                approval_id=approval_id,
                action="content_publish",
                provider=provider,
                target={
                    "post_id": post_id,
                    "platform": platform,
                    "target": post.get("target") or "",
                    "title": post.get("title") or "",
                    "body": post.get("body") or "",
                    "visual_path": post.get("visual_path") or "",
                },
                invoke=invoke,
            )
        except OutboundExecutionError as exc:
            _mark(post_id, state="scheduled", refusal_reason=exc.code)
            record_event(
                workspace_id=workspace_id,
                kind="post.blocked",
                platform=platform,
                post_id=post_id,
                payload={"reason": exc.code},
            )
            return {"post_id": post_id, "ok": False, "reason": exc.code}
        if outcome.status != "succeeded":
            # Timeouts are terminal until a human reconciles the platform
            # history. Retrying could duplicate a post.
            _mark(post_id, state="failed", refusal_reason=outcome.code)
            return {"post_id": post_id, "ok": False, "reason": outcome.code}
        result = outcome.provider_result

    now = utcnow()
    with factory() as session:
        row = session.get(ContentPost, post_id)
        if row is not None:
            row.state = "published" if result.ok else "failed"
            row.published_at = now if result.ok else None
            row.external_id = result.external_id
            row.external_url = result.external_url
            row.refusal_reason = None if result.ok else (result.error or "")[:400]
            row.updated_at = now
            topic_row = session.get(ContentTopic, row.topic_id)
            if topic_row is not None and result.ok:
                topic_row.status = "published"
        account = session.get(ContentAccount, account_id)
        if account is not None and result.ok:
            today = cadence.day_key(now)
            account.posts_today = (
                account.posts_today + 1 if account.day_key == today else 1
            )
            account.day_key = today
            account.last_post_at = now
            account.health = "ok"
            account.last_error = None
            account.updated_at = now
        elif account is not None and not result.ok:
            account.last_error = (result.error or "")[:500]
            if result.fatal and result.status_code in {401, 403}:
                # The platform is telling us to stop, so stop this account.
                account.status = "error"
                account.health = "down"
                account.paused_reason = f"{platform} returned {result.status_code}"
            account.updated_at = now
        session.commit()

    record_event(
        workspace_id=workspace_id,
        kind="post.published" if result.ok else "post.failed",
        platform=platform,
        post_id=post_id,
        payload=result.to_dict(),
    )
    return {"post_id": post_id, **result.to_dict()}


async def execute_approved_post(
    workspace_id: str, post_id: str, *, approval_id: str | None
) -> dict[str, Any]:
    """Publish one explicitly reviewed post, never a due-post batch."""

    from app.approvals.external import execution_enabled
    from app.core.local_mode import is_local

    if not (execution_enabled("content_publish") if is_local() else execution_enabled()):
        result = {"post_id": post_id, "ok": False, "reason": "external_execution_disabled"}
        if is_local():
            from app.core.local_capabilities import blocked_message

            result["message"] = blocked_message("connected_apps")
        return result
    if dry_run_enabled():
        return {"post_id": post_id, "ok": False, "reason": "outbound_channel_dry_run"}
    with db()() as session:
        row = session.get(ContentPost, post_id)
        if row is None or row.workspace_id != workspace_id:
            return {"post_id": post_id, "ok": False, "reason": "content_post_not_found"}
        if row.state not in {"scheduled", "approved"}:
            return {"post_id": post_id, "ok": False, "reason": "content_post_not_reviewable"}
        post = {
            "id": row.id,
            "platform": row.platform,
            "account_id": row.account_id,
            "body": row.body,
            "title": row.title,
            "target": row.target,
            "visual_path": row.visual_path,
        }
    return await _publish_one(workspace_id=workspace_id, post=post, approval_id=approval_id)


def _mark(post_id: str, *, state: str, refusal_reason: str | None) -> None:
    factory = db()
    with factory() as session:
        row = session.get(ContentPost, post_id)
        if row is None:
            return
        row.state = state
        row.refusal_reason = refusal_reason
        row.updated_at = utcnow()
        session.commit()


def _reschedule(post_id: str, when) -> None:
    factory = db()
    with factory() as session:
        row = session.get(ContentPost, post_id)
        if row is None:
            return
        row.scheduled_for = when
        row.updated_at = utcnow()
        session.commit()


async def tick(*, workspace_id: str) -> dict[str, Any]:
    """One full pass. Safe to call every few minutes."""
    import os

    summary: dict[str, Any] = {
        "workspace_id": workspace_id,
        "refilled": 0,
        "drafted": [],
        "published": [],
        "dry_run": dry_run_enabled(),
    }
    if brief_store.active(workspace_id) is None:
        summary["reason"] = "no_brief"
        return summary

    # When the marketing plan owns create_content jobs, do not also freestyle-draft
    # here — that would double the queue and ignore the strategist's cadence.
    marketing_owns = os.environ.get("MARKETING_OWNS_CONTENT", "true").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    if marketing_owns:
        try:
            from app.marketing import store as marketing_store

            if marketing_store.active_plan(workspace_id) is not None:
                summary["draft_owner"] = "marketing_plan"
                summary["published"] = await publish_due(workspace_id=workspace_id)
                return summary
        except Exception:
            pass

    if director.queued_count(workspace_id) < TOPIC_LOW_WATER:
        refill = await director.refill(workspace_id=workspace_id)
        summary["refilled"] = refill.get("created", 0)
        summary["planner"] = refill.get("source")

    for account in oauth_flow.list_accounts(workspace_id):
        if account["status"] != "active":
            continue
        platform = account["platform"]
        if _scheduled_count(workspace_id, platform) >= TARGET_SCHEDULED:
            continue
        try:
            summary["drafted"].append(
                await draft_for_platform(workspace_id=workspace_id, platform=platform)
            )
        except Exception as exc:
            log.exception("content draft failed for %s", platform)
            summary["drafted"].append(
                {"ok": False, "platform": platform, "reason": str(exc)[:200]}
            )

    summary["published"] = await publish_due(workspace_id=workspace_id)
    return summary


def recent_posts(workspace_id: str, limit: int = 40) -> list[dict[str, Any]]:
    factory = db()
    with factory() as session:
        rows = session.scalars(
            select(ContentPost)
            .where(ContentPost.workspace_id == workspace_id)
            .order_by(ContentPost.created_at.desc())
            .limit(limit)
        ).all()
        return [_post_view(r) for r in rows]


def _post_view(row: ContentPost) -> dict[str, Any]:
    def _loads(raw: str) -> Any:
        try:
            return json.loads(raw or "{}")
        except Exception:
            return {}

    result = {
        "id": row.id,
        "platform": row.platform,
        "intent": row.intent,
        "state": row.state,
        "title": row.title,
        "body": row.body,
        "target": row.target,
        "lint": _loads(row.lint_json),
        "critic": _loads(row.critic_json),
        "visual": _loads(getattr(row, "visual_json", None) or "{}"),
        "visual_path": getattr(row, "visual_path", None),
        "refusal_reason": row.refusal_reason,
        "scheduled_for": row.scheduled_for.isoformat() if row.scheduled_for else None,
        "published_at": row.published_at.isoformat() if row.published_at else None,
        "external_id": row.external_id,
        "external_url": row.external_url,
        "dry_run": bool(row.dry_run),
        "created_at": row.created_at.isoformat(),
    }
    from app.core.local_mode import is_local
    if is_local():
        from app.marketing.media.images import image_setup_message

        text = f"{row.title}\n\n{row.body}" if row.title else row.body
        result["handoff"] = {
            "text": text,
            "markdown": f"# {row.title}\n\n{row.body}\n" if row.title else row.body + "\n",
            "filename": row.id + ".md",
            "next_step": "Copy your draft or download Markdown in Content, then publish it yourself on your chosen channel.",
            "image_next_step": image_setup_message() if not result["visual_path"] else "",
        }
    return result


def recent_events(workspace_id: str, limit: int = 50) -> list[dict[str, Any]]:
    factory = db()
    with factory() as session:
        rows = session.scalars(
            select(ContentEvent)
            .where(ContentEvent.workspace_id == workspace_id)
            .order_by(ContentEvent.created_at.desc())
            .limit(limit)
        ).all()
        out = []
        for row in rows:
            try:
                payload = json.loads(row.payload_json or "{}")
            except Exception:
                payload = {}
            out.append(
                {
                    "id": row.id,
                    "kind": row.kind,
                    "platform": row.platform,
                    "post_id": row.post_id,
                    "payload": payload,
                    "created_at": row.created_at.isoformat(),
                }
            )
        return out


def next_run_hint(workspace_id: str) -> str | None:
    factory = db()
    with factory() as session:
        row = session.scalars(
            select(ContentPost)
            .where(
                ContentPost.workspace_id == workspace_id,
                ContentPost.state == "scheduled",
                ContentPost.scheduled_for.is_not(None),
            )
            .order_by(ContentPost.scheduled_for)
        ).first()
        if row is None or row.scheduled_for is None:
            return None
        return row.scheduled_for.isoformat()


def purge_expired_states() -> int:
    """Housekeeping for abandoned OAuth attempts."""
    from app.db.models import ContentOAuthState

    cutoff = utcnow() - timedelta(hours=6)
    factory = db()
    with factory() as session:
        rows = session.scalars(
            select(ContentOAuthState).where(ContentOAuthState.expires_at < cutoff)
        ).all()
        for row in rows:
            session.delete(row)
        session.commit()
        return len(rows)
