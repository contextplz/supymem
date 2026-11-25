"""Media generation + desktop/browser/ads adapters.

Adapters execute an *existing* job. The Media Studio GUI records jobs via
``media.service`` instead.
"""

from __future__ import annotations

from typing import Any

from app.marketing.adapters import openclaw_bridge, recordly_bridge
from app.marketing.media import images as image_gen
from app.marketing.media.hosted import blocked_outcome
from app.marketing.media import probes
from app.marketing.media import video as video_gen


class ImageStudioAdapter:
    name = "image_studio"

    def supports(self, kind: str) -> bool:
        return kind == "generate_image"

    async def run(self, job: dict[str, Any]) -> dict[str, Any]:
        blocked = blocked_outcome()
        if blocked:
            return blocked
        payload = job.get("payload") or {}
        return image_gen.generate_image(
            workspace_id=job["workspace_id"],
            headline=str(payload.get("headline") or payload.get("title") or "Still"),
            scene=str(payload.get("scene") or payload.get("caption") or ""),
            want_person=bool(payload.get("want_person")),
            width=int(payload.get("width") or 1080),
            height=int(payload.get("height") or 1350),
            accent=str(payload.get("accent") or "#6B5A4A"),
            allow_remote=bool(payload.get("allow_remote")),
        )


class VideoStudioAdapter:
    name = "video_studio"

    def supports(self, kind: str) -> bool:
        return kind == "generate_video"

    async def run(self, job: dict[str, Any]) -> dict[str, Any]:
        blocked = blocked_outcome()
        if blocked:
            return blocked
        payload = job.get("payload") or {}
        engine = str(payload.get("engine") or "").strip().lower()
        if (
            engine in {"product_demo", "recordly", "demo"}
            or payload.get("record_demo")
            or payload.get("ingest_path")
            or payload.get("launch_recordly")
        ):
            from app.marketing.media.demo_recorder import (
                record_product_demo,
                steps_from_payload,
            )

            return record_product_demo(
                workspace_id=job["workspace_id"],
                url=str(payload.get("url") or "") or None,
                headline=str(payload.get("headline") or payload.get("title") or "Product demo"),
                steps=steps_from_payload(payload),
                seconds=int(payload.get("seconds") or 20),
                headed=payload.get("headed"),
                ingest_path=str(payload.get("ingest_path") or "").strip() or None,
                launch_recordly=bool(payload.get("launch_recordly")),
                short_clip=payload.get("short_clip", True) is not False,
            )
        return video_gen.generate_video(
            workspace_id=job["workspace_id"],
            headline=str(payload.get("headline") or payload.get("title") or "Clip"),
            scene=str(payload.get("scene") or ""),
            seconds=int(payload.get("seconds") or 15),
            want_person=bool(payload.get("want_person")),
        )


class DesktopOperatorAdapter:
    name = "desktop_operator"

    def supports(self, kind: str) -> bool:
        return kind == "operate_app"

    async def run(self, job: dict[str, Any]) -> dict[str, Any]:
        blocked = blocked_outcome()
        if blocked:
            return blocked
        payload = job.get("payload") or {}
        app_name = str(payload.get("app") or "desktop")
        if app_name.lower() in {"recordly"}:
            return recordly_bridge.launch_gui()
        if app_name.lower() in {"resolve", "davinci", "davinci resolve"}:
            info = probes.resolve_available()
            return {
                "ok": False,
                "status": "blocked",
                "error": "resolve_studio_required",
                "block_reason": info.get("note"),
                "result": info,
            }
        probe = probes.desktop_probe()
        if probe.get("ok"):
            return {
                "ok": True,
                "status": "done",
                "result": probe,
                "proof": {"mode": "desktop_probe", "engine": probe.get("engine")},
            }
        return {
            "ok": False,
            "status": "blocked",
            "error": "desktop_driver_missing",
            "block_reason": probe.get("block_reason"),
            "result": probe,
        }


class BrowserOperatorAdapter:
    name = "browser_operator"

    def supports(self, kind: str) -> bool:
        return kind == "operate_browser"

    async def run(self, job: dict[str, Any]) -> dict[str, Any]:
        blocked = blocked_outcome()
        if blocked:
            return blocked
        payload = job.get("payload") or {}
        attach = bool(payload.get("attach"))
        probe = probes.browser_probe(attach=attach)
        if not probe.get("ok"):
            return {
                "ok": False,
                "status": "blocked",
                "error": probe.get("error") or "browser_unavailable",
                "block_reason": probe.get("block_reason"),
                "result": probe,
            }
        url = str(payload.get("url") or "")
        from app.gtm.relationships.denylist import linkedin_host_blocked

        if linkedin_host_blocked(url):
            return {
                "ok": False,
                "status": "blocked",
                "error": "linkedin_host_blocked",
                "block_reason": "linkedin_host_blocked",
                "result": {"url": url},
            }
        if not url or "example.com" in url.lower():
            url = "http://127.0.0.1:3100/content"
        verify = await openclaw_bridge.verify_public_url_async(
            url, workspace_id=job["workspace_id"], post_id=None
        )
        return {
            "ok": bool(verify.get("ok")),
            "status": "done" if verify.get("ok") else "blocked",
            "error": verify.get("error"),
            "block_reason": verify.get("block_reason"),
            "result": {**probe, **(verify.get("result") or {}), "url": url},
            "proof": verify.get("proof") or {"mode": "browser", "url": url},
        }


class AdsAdapter:
    name = "ads"

    def supports(self, kind: str) -> bool:
        return kind in {"create_ad", "enable_ad"}

    async def run(self, job: dict[str, Any]) -> dict[str, Any]:
        blocked = blocked_outcome()
        if blocked:
            return blocked
        payload = job.get("payload") or {}
        platform = str(payload.get("platform") or job.get("platform") or "meta")
        if job["kind"] == "enable_ad":
            return probes.enable_ad(
                platform=platform,
                resource_id=str(
                    payload.get("resource_id")
                    or payload.get("campaign_id")
                    or payload.get("resource_name")
                    or ""
                ),
                approval_id=str(payload.get("approval_id") or "") or None,
            )
        return probes.create_paused_ad(
            platform=platform,
            name=str(payload.get("name") or "Paused test campaign"),
            daily_budget=payload.get("daily_budget"),
        )


class AdsGoogleAdapter(AdsAdapter):
    name = "ads_google"

    def supports(self, kind: str) -> bool:
        return kind in {"create_ad", "enable_ad"}

    async def run(self, job: dict[str, Any]) -> dict[str, Any]:
        blocked = blocked_outcome()
        if blocked:
            return blocked
        payload = dict(job.get("payload") or {})
        payload["platform"] = "google"
        return await AdsAdapter.run(self, {**job, "payload": payload})


class AdsMetaAdapter(AdsAdapter):
    name = "ads_meta"

    def supports(self, kind: str) -> bool:
        return kind in {"create_ad", "enable_ad"}

    async def run(self, job: dict[str, Any]) -> dict[str, Any]:
        blocked = blocked_outcome()
        if blocked:
            return blocked
        payload = dict(job.get("payload") or {})
        payload["platform"] = "meta"
        return await AdsAdapter.run(self, {**job, "payload": payload})
