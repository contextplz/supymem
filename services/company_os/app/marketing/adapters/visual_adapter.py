"""Visual adapter — render a branded card for a content post."""

from __future__ import annotations

from typing import Any

from core.clock import utcnow

from app.content.visuals import render as visual_render
from app.content.visuals import spec as visual_spec
from app.db.models import ContentPost
from app.marketing.schema import proof_shape
from app.marketing.verify import visual_proof_ok
from app.state import db


class VisualAdapter:
    name = "visual_renderer"

    def supports(self, kind: str) -> bool:
        return kind == "create_visual"

    async def run(self, job: dict[str, Any]) -> dict[str, Any]:
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
            body = row.body or ""
            existing_visual = {}
            try:
                import json

                existing_visual = json.loads(row.visual_json or "{}")
            except Exception:
                existing_visual = {}

        brand = payload.get("brand") if isinstance(payload.get("brand"), dict) else {}
        template = str(payload.get("template") or "insight_card")
        built = visual_spec.build_spec(
            body=body,
            template=template,
            brand=brand,
            existing=existing_visual if isinstance(existing_visual, dict) else {},
        )
        rendered = visual_render.render_to_workspace(
            workspace_id=job["workspace_id"],
            post_id=post_id,
            spec=built,
        )
        if not rendered.get("ok"):
            # Fail closed to text-only: mark skipped so the publish chain continues.
            proof = proof_shape(
                content_post_id=post_id,
                mode="simulated",
                timestamp=utcnow().isoformat(),
                extra={"skipped": True, "reason": rendered.get("error") or "render_failed"},
            )
            ok, reason = visual_proof_ok(proof, allow_skip=True)
            return {
                "ok": ok,
                "status": "done" if ok else "failed",
                "error": None if ok else reason,
                "result": {"skipped": True, "reason": rendered.get("error"), "content_post_id": post_id},
                "proof": proof,
            }

        path = str(rendered["path"])
        import json

        with factory() as session:
            row = session.get(ContentPost, post_id)
            if row is not None:
                row.visual_json = json.dumps(built, ensure_ascii=False)
                row.visual_path = path
                row.updated_at = utcnow()
                session.commit()

        proof = proof_shape(
            content_post_id=post_id,
            visual_path=path,
            mode="simulated",
            timestamp=utcnow().isoformat(),
        )
        ok, reason = visual_proof_ok(proof)
        return {
            "ok": ok,
            "status": "done" if ok else "failed",
            "error": None if ok else reason,
            "result": {"visual_path": path, "spec": built, "content_post_id": post_id},
            "proof": proof,
        }
