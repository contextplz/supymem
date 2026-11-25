"""Run media jobs immediately and persist them on a side plan."""

from __future__ import annotations

import io
import json
from datetime import timedelta
from pathlib import Path
from typing import Any

from app.marketing.media.hosted import blocked_outcome
from core.clock import utcnow
from core.ids import new_id

from app.marketing import store as job_store
from app.marketing.media import compositor, probes
from app.marketing.media import images as image_gen
from app.marketing.media import store as media_store
from app.marketing.media import video as video_gen
from app.marketing.schema import default_constraints, empty_strategy, proof_shape

MAX_UPLOAD_BYTES = 15 * 1024 * 1024


def _brief_copy(workspace_id: str) -> dict[str, str]:
    from app.content.brief import active

    row = active(workspace_id) or {}
    product = row.get("product") if isinstance(row.get("product"), dict) else {}
    headline = str(product.get("one_liner") or "").strip()
    diffs = product.get("differentiators") or []
    category = str(product.get("category") or "").strip()
    scene = category
    if diffs:
        scene = f"{category}. {diffs[0]}".strip(". ")
    url = str(product.get("url") or "").strip()
    if not url or "example.com" in url.lower() or "example.local" in url.lower():
        url = "http://127.0.0.1:3100/content"
    return {"headline": headline, "scene": scene, "url": url}


def ensure_media_plan(workspace_id: str) -> dict[str, Any]:
    """A side plan that does not archive the active GTM plan."""
    from sqlalchemy import select

    from app.db.models import MarketingPlan
    from app.state import db

    factory = db()
    with factory() as session:
        row = session.scalars(
            select(MarketingPlan)
            .where(
                MarketingPlan.workspace_id == workspace_id,
                MarketingPlan.source == "media_studio",
            )
            .order_by(MarketingPlan.created_at.desc())
        ).first()
        if row is not None:
            return job_store.serialize_plan(row)

    strategy = empty_strategy()
    strategy["content"]["formats"] = ["photo", "short_video", "branded_card"]
    strategy["content"]["media"] = {
        "images": {"enabled": True},
        "video": {"enabled": True, "seconds": 15},
    }
    now = utcnow()
    with factory() as session:
        row = MarketingPlan(
            id=new_id("mpl"),
            workspace_id=workspace_id,
            brief_id=None,
            horizon_days=30,
            status="draft",
            strategy_json=json.dumps(strategy, ensure_ascii=False),
            constraints_json=json.dumps(default_constraints(), ensure_ascii=False),
            source="media_studio",
            starts_at=now,
            ends_at=now + timedelta(days=30),
            created_at=now,
            updated_at=now,
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        return job_store.serialize_plan(row)


def _record_job(
    *,
    workspace_id: str,
    kind: str,
    title: str,
    payload: dict[str, Any],
    outcome: dict[str, Any],
    platform: str | None = None,
) -> dict[str, Any]:
    plan = ensure_media_plan(workspace_id)
    now = utcnow()
    status = str(outcome.get("status") or ("done" if outcome.get("ok") else "failed"))
    if status not in {"done", "failed", "blocked"}:
        status = "failed"
    inserted = job_store.insert_jobs(
        [
            {
                "id": new_id("mjb"),
                "workspace_id": workspace_id,
                "plan_id": plan["id"],
                "kind": kind,
                "platform": platform,
                "title": title,
                "payload": payload,
                "depends_on": [],
                "status": "running",
                "priority": 15,
                "scheduled_for": now - timedelta(seconds=1),
            }
        ]
    )
    job = inserted[0]
    proof = outcome.get("proof") or {}
    if not proof and outcome.get("ok"):
        proof = proof_shape(
            mode="local",
            timestamp=now.isoformat(),
            extra=outcome.get("result") or {},
        )
    recorded = job_store.mark_job(
        job["id"],
        status=status,
        result=outcome.get("result") or {},
        proof=proof,
        error_message=outcome.get("error"),
        block_reason=outcome.get("block_reason"),
        executor=kind,
    )
    return {**outcome, "job": recorded or {**job, "status": status}}


def run_generate_image(workspace_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    blocked = blocked_outcome()
    if blocked:
        return blocked
    copy = _brief_copy(workspace_id)
    headline = str(payload.get("headline") or payload.get("title") or copy["headline"] or "").strip()
    scene = str(payload.get("scene") or payload.get("caption") or copy["scene"] or "").strip()
    if not headline:
        return _record_job(
            workspace_id=workspace_id,
            kind="generate_image",
            title="Generate image",
            payload=payload,
            outcome={
                "ok": False,
                "status": "blocked",
                "error": "headline_required",
                "block_reason": "Save a content brief with a one-liner, or type a real headline.",
            },
            platform=payload.get("platform") or "linkedin",
        )
    outcome = image_gen.generate_image(
        workspace_id=workspace_id,
        headline=headline,
        scene=scene,
        want_person=bool(payload.get("want_person")),
        width=int(payload.get("width") or 1080),
        height=int(payload.get("height") or 1350),
        accent=str(payload.get("accent") or "#6B5A4A"),
        allow_remote=bool(payload.get("allow_remote")),
    )
    return _record_job(
        workspace_id=workspace_id,
        kind="generate_image",
        title=f"Generate image · {headline}",
        payload=payload,
        outcome=outcome,
        platform=payload.get("platform") or "linkedin",
    )


def _wants_product_demo(payload: dict[str, Any]) -> bool:
    engine = str(payload.get("engine") or "").strip().lower()
    return engine in {"product_demo", "recordly", "demo"} or bool(
        payload.get("record_demo") or payload.get("ingest_path") or payload.get("launch_recordly")
    )


def run_record_product_demo(workspace_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    blocked = blocked_outcome()
    if blocked:
        return blocked
    from app.marketing.media.demo_recorder import (
        record_product_demo,
        steps_from_payload,
    )

    copy = _brief_copy(workspace_id)
    headline = str(
        payload.get("headline") or payload.get("title") or copy["headline"] or "Product demo"
    ).strip()
    url = str(payload.get("url") or copy.get("url") or "").strip()
    outcome = record_product_demo(
        workspace_id=workspace_id,
        url=url or None,
        headline=headline,
        steps=steps_from_payload(payload),
        seconds=int(payload.get("seconds") or 20),
        headed=payload.get("headed"),
        ingest_path=str(payload.get("ingest_path") or "").strip() or None,
        launch_recordly=bool(payload.get("launch_recordly")),
        short_clip=payload.get("short_clip", True) is not False,
    )
    return _record_job(
        workspace_id=workspace_id,
        kind="generate_video",
        title=f"Product demo · {headline}",
        payload={**payload, "engine": "product_demo"},
        outcome=outcome,
        platform=payload.get("platform") or "linkedin",
    )


def run_generate_video(workspace_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    blocked = blocked_outcome()
    if blocked:
        return blocked
    if _wants_product_demo(payload):
        return run_record_product_demo(workspace_id, payload)
    copy = _brief_copy(workspace_id)
    headline = str(payload.get("headline") or payload.get("title") or copy["headline"] or "Clip").strip()
    outcome = video_gen.generate_video(
        workspace_id=workspace_id,
        headline=headline,
        scene=str(payload.get("scene") or copy["scene"] or ""),
        seconds=int(payload.get("seconds") or 15),
        want_person=bool(payload.get("want_person")),
    )
    return _record_job(
        workspace_id=workspace_id,
        kind="generate_video",
        title=f"Generate video · {headline}",
        payload=payload,
        outcome=outcome,
        platform=payload.get("platform") or "linkedin",
    )


def run_operate_browser(workspace_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    blocked = blocked_outcome()
    if blocked:
        return blocked
    from app.marketing.adapters import openclaw_bridge

    attach = bool(payload.get("attach"))
    probe = probes.browser_probe(attach=attach)
    if not probe.get("ok"):
        outcome = {
            "ok": False,
            "status": "blocked",
            "error": probe.get("error") or "browser_unavailable",
            "block_reason": probe.get("block_reason"),
            "result": probe,
        }
    else:
        url = str(payload.get("url") or _brief_copy(workspace_id)["url"])
        from app.gtm.relationships.denylist import linkedin_host_blocked

        if linkedin_host_blocked(url):
            outcome = {
                "ok": False,
                "status": "blocked",
                "error": "linkedin_host_blocked",
                "block_reason": "linkedin_host_blocked",
                "result": {**probe, "url": url},
            }
        else:
            verify = openclaw_bridge.verify_public_url(
                url, workspace_id=workspace_id, post_id=None
            )
            outcome = {
                "ok": bool(verify.get("ok")),
                "status": "done" if verify.get("ok") else "blocked",
                "error": verify.get("error"),
                "block_reason": verify.get("block_reason"),
                "result": {**probe, **(verify.get("result") or {}), "url": url},
                "proof": verify.get("proof") or {"mode": "browser", "url": url},
            }
    return _record_job(
        workspace_id=workspace_id,
        kind="operate_browser",
        title="Browser probe",
        payload=payload,
        outcome=outcome,
    )


def run_operate_app(workspace_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    blocked = blocked_outcome()
    if blocked:
        return blocked
    app_name = str(payload.get("app") or "desktop")
    if app_name.lower() in {"recordly"}:
        from app.marketing.adapters import recordly_bridge

        return _record_job(
            workspace_id=workspace_id,
            kind="operate_app",
            title="Launch Recordly GUI",
            payload=payload,
            outcome=recordly_bridge.launch_gui(),
        )
    if app_name.lower() in {"resolve", "davinci", "davinci resolve"}:
        info = probes.resolve_available()
        outcome = {
            "ok": False,
            "status": "blocked",
            "error": "resolve_studio_required",
            "block_reason": info.get("note"),
            "result": info,
        }
        return _record_job(
            workspace_id=workspace_id,
            kind="operate_app",
            title="DaVinci Resolve scripting",
            payload=payload,
            outcome=outcome,
        )
    probe = probes.desktop_probe()
    if probe.get("ok"):
        outcome = {
            "ok": True,
            "status": "done",
            "result": probe,
            "proof": {"mode": "desktop_probe", "engine": probe.get("engine")},
        }
    else:
        outcome = {
            "ok": False,
            "status": "blocked",
            "error": "desktop_driver_missing",
            "block_reason": probe.get("block_reason"),
            "result": probe,
        }
    return _record_job(
        workspace_id=workspace_id,
        kind="operate_app",
        title=f"Desktop operate · {app_name}",
        payload=payload,
        outcome=outcome,
    )


def run_create_ad(workspace_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    blocked = blocked_outcome()
    if blocked:
        return blocked
    outcome = probes.create_paused_ad(
        platform=str(payload.get("platform") or "meta"),
        name=str(payload.get("name") or "Paused test campaign"),
        daily_budget=payload.get("daily_budget"),
    )
    return _record_job(
        workspace_id=workspace_id,
        kind="create_ad",
        title=f"Create paused ad · {payload.get('platform') or 'meta'}",
        payload=payload,
        outcome=outcome,
        platform=str(payload.get("platform") or "meta"),
    )


def ingest_upload(
    *,
    workspace_id: str,
    kind: str,
    filename: str,
    data: bytes,
    title: str | None = None,
) -> dict[str, Any]:
    kind = kind if kind in {"logo", "founder", "product", "other"} else "other"
    if not data:
        return {"ok": False, "error": "empty_file"}
    if len(data) > MAX_UPLOAD_BYTES:
        return {"ok": False, "error": "file_too_large"}
    suffix = Path(filename).suffix.lower() or ".bin"
    raster = {".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG", ".webp": "WEBP", ".gif": "GIF"}
    allowed = {*raster, ".svg"} if kind != "other" else {*raster, ".svg", ".pdf", ".mp4"}
    if suffix not in allowed:
        return {"ok": False, "error": "unsupported_image_type"}
    from PIL import Image

    try:
        if suffix in raster:
            with Image.open(io.BytesIO(data)) as image:
                if image.format != raster[suffix]:
                    return {"ok": False, "error": "invalid_file_type"}
                image.verify()
        elif suffix == ".svg":
            from xml.etree import ElementTree

            if ElementTree.fromstring(data).tag not in {"svg", "{http://www.w3.org/2000/svg}svg"}:
                return {"ok": False, "error": "invalid_file_type"}
        elif suffix == ".pdf" and not data.startswith(b"%PDF-"):
            return {"ok": False, "error": "invalid_file_type"}
        elif suffix == ".mp4" and not (len(data) >= 16 and data[4:8] == b"ftyp"
                                       and 16 <= int.from_bytes(data[:4], "big") <= len(data)
                                       and data[8:12] in {b"isom", b"iso2", b"iso3", b"iso4", b"iso5", b"iso6",
                                                           b"mp41", b"mp42", b"avc1", b"M4V ", b"dash"}):
            return {"ok": False, "error": "invalid_file_type"}
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError):
        return {"ok": False, "error": "invalid_file_type"}
    dest_dir = media_store.media_dir(workspace_id, "intake", kind)
    dest = dest_dir / f"{new_id('upl')}{suffix}"
    dest.write_bytes(data)
    from app.marketing.media.quality import reject_reason

    blocked = reject_reason(kind, dest)
    if blocked:
        dest.unlink(missing_ok=True)
        return {"ok": False, "status": "blocked", "error": "placeholder_asset", "block_reason": blocked}
    width = height = None
    if suffix != ".svg":
        size = compositor.jpeg_size(str(dest))
        if size:
            width, height = size
    review = "approved" if kind in {"logo", "founder", "product"} else "pending"
    asset = media_store.insert_asset(
        workspace_id=workspace_id,
        kind=kind,
        path=str(dest),
        source="upload",
        mime=_mime(suffix),
        title=title or filename,
        width=width,
        height=height,
        license_info={"origin": "founder_upload"},
        meta={"filename": filename},
        review_status=review,
    )
    return {"ok": True, "asset": asset}


def _mime(suffix: str) -> str:
    return {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".gif": "image/gif",
        ".svg": "image/svg+xml",
        ".pdf": "application/pdf",
        ".mp4": "video/mp4",
    }.get(suffix, "application/octet-stream")
