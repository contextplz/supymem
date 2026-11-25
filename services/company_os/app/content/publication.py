"""Prepare immutable company-owned publication intents; no approval or send I/O.

This first checkpoint deliberately has no worker, HTTP route or provider call.
The later canonical external-approval executor must consume this exact target.
"""
from __future__ import annotations

import json
import os
import re
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select

from app.content.generation import authority, digest, dump
from app.content import generation_schema
from app.db.models import ContentAccount, ContentBrief, ContentGenerationRun, ContentPost, ContentPublicationRun, CreditAccount, Venture
from app.state import db
from core.clock import utcnow
from core.ids import new_id

POLICY_VERSION = "content-publication-v1"
OPEN_STATES = ("waiting_review", "waiting_approval", "approved", "queued", "running", "receipt_saved", "blocked", "needs_reconciliation")


class PublicationError(ValueError):
    pass


class Selection(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    account_id: str = Field(min_length=1, max_length=160)
    expected_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    target: str = Field(default="", max_length=100)


def selection(value):
    try:
        return Selection.model_validate(value).model_dump()
    except ValidationError as exc:
        raise PublicationError("content_publication_input_invalid") from exc


def revision(post):
    return digest([post.body, post.title, post.state, post.updated_at.isoformat()])


def _post(session, wid, vid, pid, *, lock=False):
    statement = select(ContentPost, ContentGenerationRun).join(ContentGenerationRun,
        ContentPost.generation_run_id == ContentGenerationRun.id).where(ContentPost.id == pid,
        ContentPost.workspace_id == wid, ContentGenerationRun.workspace_id == wid, ContentGenerationRun.venture_id == vid)
    if lock:
        statement = statement.with_for_update()
    row = session.execute(statement).one_or_none()
    if not row:
        raise PublicationError("content_publication_not_found")
    return row


def _account(session, wid, platform, aid, *, lock=False):
    statement = select(ContentAccount).where(ContentAccount.id == aid, ContentAccount.workspace_id == wid)
    if lock:
        statement = statement.with_for_update()
    account = session.scalar(statement)
    if not account:
        raise PublicationError("content_publication_not_found")
    if account.platform != platform:
        raise PublicationError("content_publication_account_platform_mismatch")
    if account.status != "active":
        raise PublicationError("content_publication_account_inactive")
    if platform not in {"x", "linkedin", "reddit"}:
        raise PublicationError("content_publication_platform_unsupported")
    try:
        extra, scopes = json.loads(account.extra_json or "{}"), json.loads(account.scopes_json or "[]")
        if not isinstance(extra, dict) or not isinstance(scopes, list) or not all(isinstance(v, str) for v in scopes):
            raise ValueError()
    except ValueError as exc:
        raise PublicationError("content_publication_account_unverified") from exc
    managed = extra.get("auth_via") == "composio"
    if managed:
        from app.content.composio_social import user_id_for, COMPOSIO_BASE, POST_TOOL, _toolkit_version
        if not extra.get("connected_account_id") or extra.get("composio_user_id") != user_id_for(wid):
            raise PublicationError("content_publication_account_unverified")
        version = _toolkit_version(platform)
        if not version or version == "latest" or not re.fullmatch(r"[A-Za-z0-9._-]{1,100}", version):
            raise PublicationError("content_publication_adapter_version_required")
        if not os.environ.get("COMPOSIO_API_KEY", "").strip():
            raise PublicationError("content_publication_provider_unavailable")
        contract = {"endpoint": COMPOSIO_BASE.rstrip("/") + "/tools/execute/" + POST_TOOL[platform], "version": version}
    else:
        required = {"x": "tweet.write", "linkedin": "w_member_social", "reddit": "submit"}[platform]
        if not account.access_secret_ref or required not in scopes:
            raise PublicationError("content_publication_account_unverified")
        version = "native-text-v1"
        from app.content.platforms import x, linkedin, reddit
        contract = {"endpoint": {"x": x.TWEETS_URL, "linkedin": linkedin.POSTS_URL, "reddit": reddit.SUBMIT_URL}[platform],
            "version": linkedin._version() if platform == "linkedin" else version}
    endpoint = urlparse(contract["endpoint"])
    if endpoint.scheme != "https" or not endpoint.hostname or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment:
        raise PublicationError("content_publication_provider_contract_invalid")
    if not account.external_account or (platform == "linkedin" and not account.author_urn):
        raise PublicationError("content_publication_account_identity_required")
    identity = {"workspace_id": wid, "account_id": account.id, "platform": platform, "auth_generation": account.auth_generation,
        "source": "composio" if managed else "native", "external_account": account.external_account,
        "author_urn": account.author_urn or "", "scopes": sorted(scopes),
        "connection": extra.get("connected_account_id") if managed else None,
        "provider_user": extra.get("composio_user_id") if managed else None}
    return account, f"{'composio' if managed else 'native'}_{platform}", contract, digest(identity)


def _review(session, post, source, target):
    """Reuse only the durable critic of the exact original unchanged draft."""
    from app.content.safety.linter import lint
    try:
        doc = json.loads(source.document_json)
        runtime = doc["_runtime"]
        data = runtime["input"]
        attempt = doc["result"]["attempts"]
        if type(attempt) is not int or not 1 <= attempt <= 3:
            raise ValueError()
        written = runtime["operations"][f"draft_{attempt}"]
        reviewed = runtime["operations"][f"critic_{attempt}"]
        draft = written["response"]
        critic = generation_schema.parse("critic", json.dumps(reviewed["response"]), data, draft=draft)
        same = (draft["body"] == post.body and (draft.get("title") or "") == (post.title or "")
            and draft.get("visual") in (None, {}) and not json.loads(post.visual_json or "{}")
            and json.loads(post.critic_json) == {**critic, "attempts": attempt})
        # Original writer/critic input contains no subreddit context. A new
        # destination therefore needs the separately quoted review workflow.
        if (source.state != "completed" or source.kind != "draft" or doc["result"]["post_id"] != post.id
            or data["platform"] != post.platform or source.topic_id != post.topic_id or data["topic_id"] != post.topic_id
            or data["topic"]["intent"] != post.intent or target or post.target or not same
            or any(op.get("state") != "completed" or op.get("error_code") or not op.get("usage", {}).get("settled") for op in [written, reviewed])
            or not generation_schema.critic_passes(critic, data)):
            raise ValueError()
        brief = session.get(ContentBrief, data["brief_id"])
        if (not brief or brief.workspace_id != post.workspace_id or brief.venture_id != source.venture_id
                or brief.status != "active" or brief.version != data["brief_version"]):
            raise ValueError()
        linted = lint(post.body, platform=post.platform, intent=post.intent, brief=data["brief"]).to_dict()
        if not linted["ok"]:
            raise PublicationError("content_publication_lint_failed")
        return {"run_id": source.id, "revision_digest": revision(post), "brief_id": brief.id,
            "brief_version": brief.version, "policy_version": POLICY_VERSION,
            "critic_digest": digest(critic), "critic": critic, "lint": linted}
    except PublicationError:
        raise
    except (ValueError, TypeError, KeyError) as exc:
        raise PublicationError("content_publication_fresh_review_required") from exc


def _resolve(session, wid, vid, pid, actor, chosen, *, lock=False):
    from app.approvals.external import OutboundExecutionError, outbound_rate
    post, source = _post(session, wid, vid, pid, lock=lock)
    if post.state not in {"draft", "refused"}:
        raise PublicationError("content_publication_post_not_editable")
    if chosen["expected_digest"] != revision(post):
        raise PublicationError("content_publication_revision_changed")
    try:
        visual = json.loads(post.visual_json or "{}")
    except ValueError as exc:
        raise PublicationError("content_publication_assets_unsupported") from exc
    if post.visual_path or visual:
        raise PublicationError("content_publication_assets_unsupported")
    account, provider, adapter, account_digest = _account(session, wid, post.platform, chosen["account_id"], lock=lock)
    target = chosen["target"]
    if post.platform == "reddit":
        target = target.removeprefix("r/")
        if not re.fullmatch(r"[A-Za-z0-9_]{2,50}", target):
            raise PublicationError("content_publication_destination_required")
    elif target:
        raise PublicationError("content_publication_destination_unsupported")
    review = _review(session, post, source, target)
    from app.credits.accounts import account_for_venture
    credit = account_for_venture(vid, acting_person_id=actor, session=session)
    if not credit or credit.status != "active":
        raise PublicationError("content_publication_credit_account_unavailable")
    try:
        rate = outbound_rate("content_publish", provider)
    except OutboundExecutionError as exc:
        raise PublicationError(exc.code) from exc
    price = {"credit_account_id": credit.id, "payer": "platform_credits", "amount_micro": rate.amount_micro,
        "rate_version": rate.version, "rate_source": rate.source}
    rendered = {"version": 1, "workspace_id": wid, "venture_id": vid, "post_id": pid, "revision_digest": revision(post),
        "account_id": account.id, "account_identity_digest": account_digest, "platform": post.platform,
        "provider": provider, "provider_contract": adapter, "external_actor": account.author_urn or account.external_account,
        "target": target, "intent": post.intent, "title": post.title or "", "body": post.body, "assets": [],
        "editorial_review_run_id": review["run_id"], "editorial_review_digest": digest(review), "lint_policy_version": POLICY_VERSION,
        **price}
    from app.approvals.external import action_digest
    target_digest = action_digest("content_publish", rendered)
    return {"target": rendered, "target_digest": target_digest, "quote_digest": digest([actor, target_digest]),
        "review": review, "price_digest": digest(price), "account_label": account.handle or account.external_account,
        "source_run_id": source.id, "ceiling_micro": rate.amount_micro,
        "available_micro": credit.granted_micro - credit.spent_micro - credit.reserved_micro,
        "sufficient_now": credit.granted_micro - credit.spent_micro - credit.reserved_micro >= rate.amount_micro,
        "execution_available": False, "execution_blocker": "content_publication_executor_not_integrated"}


def quote(wid, vid, pid, requested):
    actor = authority(wid, vid, write=True)
    chosen = selection(requested)
    with db()() as session:
        return _resolve(session, wid, vid, pid, actor, chosen)


def _view(row):
    spec = json.loads(row.spec_json)
    return {"publication_id": row.id, "workspace_id": row.workspace_id, "venture_id": row.venture_id, "post_id": row.post_id,
        "state": row.state, "approval_id": row.approval_id, "job_id": row.job_id,
        "target_digest": row.target_digest, "ceiling_micro": row.ceiling_micro, "review": spec["review"],
        "target": spec["target"], "account_label": spec["account_label"], "result": json.loads(row.result_json),
        "execution_available": False, "execution_blocker": "content_publication_executor_not_integrated"}


def prepare(wid, vid, pid, requested, *, idempotency_key, quote_digest, ceiling_micro):
    """Store/reuse a reviewed target only; create neither Approval nor Job."""
    actor = authority(wid, vid, write=True)
    chosen = selection(requested)
    if not isinstance(idempotency_key, str) or not 8 <= len(idempotency_key) <= 160:
        raise PublicationError("content_publication_idempotency_required")
    intent = digest(idempotency_key)
    request_digest = digest([pid, chosen, quote_digest, ceiling_micro])
    with db()() as session:
        venture = session.get(Venture, vid, with_for_update=True)
        if not venture or venture.workspace_id != wid:
            raise PublicationError("content_publication_not_found")
        old = session.scalar(select(ContentPublicationRun).where(ContentPublicationRun.workspace_id == wid,
            ContentPublicationRun.venture_id == vid, ContentPublicationRun.actor_subject == actor,
            ContentPublicationRun.idempotency_hash == intent))
        if old:
            if old.request_digest != request_digest:
                raise PublicationError("content_publication_idempotency_conflict")
            return _view(old)
        value = _resolve(session, wid, vid, pid, actor, chosen, lock=True)
        if quote_digest != value["quote_digest"] or type(ceiling_micro) is not int or ceiling_micro != value["ceiling_micro"]:
            raise PublicationError("content_publication_quote_changed")
        if not value["sufficient_now"]:
            raise PublicationError("content_publication_insufficient_credits")
        if session.scalar(select(ContentPublicationRun.id).where(ContentPublicationRun.post_id == pid,
                ContentPublicationRun.state.in_(OPEN_STATES))):
            raise PublicationError("content_publication_unresolved_intent")
        target, now = value["target"], utcnow()
        row = ContentPublicationRun(id=new_id("cpr"), workspace_id=wid, venture_id=vid, post_id=pid,
            source_run_id=value["source_run_id"], review_run_id=value["review"]["run_id"], actor_subject=actor,
            account_id=chosen["account_id"], platform=target["platform"], credit_account_id=target["credit_account_id"],
            idempotency_hash=intent, request_digest=request_digest, revision_digest=target["revision_digest"],
            target_digest=value["target_digest"], account_identity_digest=target["account_identity_digest"],
            price_digest=value["price_digest"], ceiling_micro=ceiling_micro, state="waiting_approval",
            spec_json=dump({key: value[key] for key in ("target", "review", "account_label")}), result_json="{}",
            created_at=now, updated_at=now)
        session.add(row)
        session.flush()
        result = _view(row)
        session.commit()
        from app.audit.writer import audit
        from app.authz.workspace import subject_person_id
        audit("content.publication_prepared", workspace_id=wid, venture_id=vid,
            actor_id=subject_person_id(actor), actor_kind="person", surface="api", target_ref=row.id,
            payload={"post_id": pid, "account_id": row.account_id, "target_digest": row.target_digest,
                "ceiling_micro": ceiling_micro, "external_approval_created": False, "job_created": False})
        return result


def get(wid, vid, publication_id):
    authority(wid, vid)
    with db()() as session:
        row = session.scalar(select(ContentPublicationRun).where(ContentPublicationRun.id == publication_id,
            ContentPublicationRun.workspace_id == wid, ContentPublicationRun.venture_id == vid))
        if not row:
            raise PublicationError("content_publication_not_found")
        return _view(row)
