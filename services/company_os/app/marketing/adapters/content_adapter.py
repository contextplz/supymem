"""Content-engine adapter: create + publish posts via the existing pipeline."""

from __future__ import annotations

from typing import Any

from core.clock import utcnow

from app.content import engine as content_engine
from app.content.platforms.base import dry_run_enabled
from app.db.models import ContentPost
from app.marketing.schema import proof_shape
from app.marketing.verify import is_block_error, publish_proof_ok
from app.state import db


class ContentAdapter:
    name = "content_engine"

    def supports(self, kind: str) -> bool:
        return kind in {"create_content", "publish_content", "check_result"}

    async def run(self, job: dict[str, Any]) -> dict[str, Any]:
        kind = job["kind"]
        if kind == "create_content":
            return await self._create(job)
        if kind == "publish_content":
            return await self._publish(job)
        if kind == "check_result":
            return await self._check(job)
        return {
            "ok": False,
            "status": "failed",
            "error": f"unsupported_kind:{kind}",
            "result": {},
            "proof": {},
        }

    async def _create(self, job: dict[str, Any]) -> dict[str, Any]:
        platform = job.get("platform") or (job.get("payload") or {}).get("platform")
        if not platform:
            return {
                "ok": False,
                "status": "failed",
                "error": "missing_platform",
                "result": {},
                "proof": {},
            }
        from app.content import director

        if director.queued_count(job["workspace_id"]) < 4:
            await director.refill(workspace_id=job["workspace_id"], count=10)

        outcome = await content_engine.draft_for_platform(
            workspace_id=job["workspace_id"],
            platform=str(platform),
        )
        if not outcome.get("ok"):
            reason = str(outcome.get("reason") or "draft_failed")
            if reason in {"not_connected", "no_brief"} or is_block_error(reason):
                return {
                    "ok": False,
                    "status": "blocked",
                    "block_reason": reason,
                    "error": reason,
                    "result": outcome,
                    "proof": {},
                }
            return {
                "ok": False,
                "status": "failed",
                "error": reason,
                "result": outcome,
                "proof": {},
            }
        post_id = str(outcome["post_id"])
        proof = proof_shape(
            content_post_id=post_id,
            mode="simulated" if dry_run_enabled() else "live",
            timestamp=utcnow().isoformat(),
            account_id=None,
        )
        return {
            "ok": True,
            "status": "done",
            "result": {"content_post_id": post_id, "platform": platform, **outcome},
            "proof": proof,
        }

    async def _publish(self, job: dict[str, Any]) -> dict[str, Any]:
        payload = job.get("payload") or {}
        post_id = str(payload.get("content_post_id") or "")
        # Recover post id from an upstream dependency proof if propagation missed.
        if not post_id:
            for dep_id in job.get("depends_on") or []:
                from app.marketing import store as marketing_store

                dep = next(
                    (
                        j
                        for j in marketing_store.list_jobs(
                            job["workspace_id"], limit=200
                        )
                        if j["id"] == dep_id
                    ),
                    None,
                )
                if not dep:
                    continue
                post_id = str(
                    (dep.get("result") or {}).get("content_post_id")
                    or (dep.get("proof") or {}).get("content_post_id")
                    or (dep.get("payload") or {}).get("content_post_id")
                    or ""
                )
                if post_id:
                    break
        if not post_id:
            return {
                "ok": False,
                "status": "failed",
                "error": "missing_content_post_id",
                "result": {},
                "proof": {},
            }

        factory = db()
        with factory() as session:
            row = session.get(ContentPost, post_id)
            if row is None or row.workspace_id != job["workspace_id"]:
                return {
                    "ok": False,
                    "status": "failed",
                    "error": "content_post_not_found",
                    "result": {},
                    "proof": {},
                }
            if row.state == "published" and row.external_id:
                proof = proof_shape(
                    external_id=row.external_id,
                    external_url=row.external_url,
                    account_id=row.account_id,
                    mode="simulated" if row.dry_run else "live",
                    timestamp=(row.published_at or utcnow()).isoformat(),
                    content_post_id=post_id,
                    visual_path=row.visual_path,
                )
                ok, reason = publish_proof_ok(proof)
                return {
                    "ok": ok,
                    "status": "done" if ok else "failed",
                    "error": None if ok else reason,
                    "result": {"already_published": True, "content_post_id": post_id},
                    "proof": proof,
                }
            # Force due: publish_due only picks scheduled_for <= now. Bump if needed.
            if row.state == "scheduled":
                row.scheduled_for = utcnow()
                session.commit()
            post_view = {
                "id": row.id,
                "platform": row.platform,
                "account_id": row.account_id,
                "body": row.body,
                "title": row.title,
                "target": row.target,
                "visual_path": row.visual_path,
            }

        outcome = await content_engine._publish_one(  # noqa: SLF001 — intentional reuse
            workspace_id=job["workspace_id"],
            post=post_view,
            approval_id=str(payload.get("approval_id") or "") or None,
        )
        # Re-read proof from DB — source of truth after publish.
        with factory() as session:
            row = session.get(ContentPost, post_id)
            if row is None:
                return {
                    "ok": False,
                    "status": "failed",
                    "error": "post_vanished",
                    "result": outcome,
                    "proof": {},
                }
            proof = proof_shape(
                external_id=row.external_id,
                external_url=row.external_url,
                account_id=row.account_id,
                mode="simulated" if row.dry_run or dry_run_enabled() else "live",
                timestamp=(row.published_at or utcnow()).isoformat(),
                content_post_id=post_id,
                visual_path=row.visual_path,
            )

        if not outcome.get("ok"):
            reason = str(outcome.get("reason") or outcome.get("error") or "publish_failed")
            # Composio connection gaps are NeedsYou, not hard failures.
            if is_block_error(reason) or reason.startswith("warden:") or any(
                t in reason.lower()
                for t in (
                    "no connected account",
                    "reconnect",
                    "expired",
                    "author urn missing",
                )
            ):
                return {
                    "ok": False,
                    "status": "blocked",
                    "block_reason": reason,
                    "error": reason,
                    "result": outcome,
                    "proof": proof,
                }
            return {
                "ok": False,
                "status": "failed",
                "error": reason,
                "result": outcome,
                "proof": proof,
            }

        ok, reason = publish_proof_ok(proof)
        if not ok:
            return {
                "ok": False,
                "status": "failed",
                "error": f"unverified_publish:{reason}",
                "result": outcome,
                "proof": proof,
            }
        return {
            "ok": True,
            "status": "done",
            "result": outcome,
            "proof": proof,
        }

    async def _check(self, job: dict[str, Any]) -> dict[str, Any]:
        payload = job.get("payload") or {}
        post_id = str(payload.get("content_post_id") or "")
        if not post_id:
            for dep_id in job.get("depends_on") or []:
                from app.marketing import store as marketing_store

                dep = next(
                    (
                        j
                        for j in marketing_store.list_jobs(
                            job["workspace_id"], limit=200
                        )
                        if j["id"] == dep_id
                    ),
                    None,
                )
                if not dep:
                    continue
                post_id = str(
                    (dep.get("result") or {}).get("content_post_id")
                    or (dep.get("proof") or {}).get("content_post_id")
                    or (dep.get("payload") or {}).get("content_post_id")
                    or ""
                )
                if post_id:
                    break
        factory = db()
        with factory() as session:
            row = session.get(ContentPost, post_id) if post_id else None
            if row is None:
                return {
                    "ok": False,
                    "status": "failed",
                    "error": "not_found",
                    "result": {},
                    "proof": {},
                }
            proof = proof_shape(
                external_id=row.external_id,
                external_url=row.external_url,
                account_id=row.account_id,
                mode="simulated" if row.dry_run else "live",
                timestamp=(row.published_at or utcnow()).isoformat(),
                content_post_id=post_id,
                visual_path=row.visual_path,
            )
        ok, reason = publish_proof_ok(proof)
        return {
            "ok": ok,
            "status": "done" if ok else "failed",
            "error": None if ok else reason,
            "result": {"state": "checked", "content_post_id": post_id},
            "proof": proof,
        }
