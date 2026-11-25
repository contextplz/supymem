"""Flagged, authenticated page-builder API and inert preview artifacts."""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import mimetypes
import re

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from core.artifact_policy import DEFAULT_LIMITS
from app.admin.authn import verify_caller
from app.core.local_mode import is_local
from app.pagebuilder import api
from app.pagebuilder.preview_assets import (
    PREVIEW_DOCUMENT_MAX_BYTES, PREVIEW_POLICY, PreviewAssets, PreviewTooLarge, signup_preview_notice, srcdoc_document,
)

router = APIRouter(tags=["pagebuilder"])
PREFIX = "/ventures/{venture_id}/pagebuilder"
TOOL_RESULT_MAX_BASE64 = ((DEFAULT_LIMITS.max_archive_bytes + 2) // 3) * 4


def _preview_too_large():
    return HTTPException(409, detail={"code": "pagebuilder_preview_too_large",
        "message": "Your page is saved, but its preview is too large. In your venture chat, ask Charlie to build a new version with smaller or fewer repeated images."})

if is_local():
    from app.pagebuilder.local_publish import router as local_router

    ROUTERS = (local_router,)


class StartBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    variants: int = Field(default=1, ge=1, le=3, strict=True)
    quote_fingerprint: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


class SourceRetryBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_attempt: int = Field(ge=0, strict=True)


class BuildBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    new_version_of: str | None = Field(default=None, min_length=1, max_length=100, strict=True)


class ToolResultBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    order_id: str = Field(pattern=r"^dwo_[A-Za-z0-9]+$", max_length=128)
    execution_id: str = Field(pattern=r"^dex_[A-Za-z0-9]+$", max_length=128)
    source_zip: str = Field(min_length=1, max_length=TOOL_RESULT_MAX_BASE64)


def caller(request: Request):
    if not is_local():
        api.require_enabled()
    return verify_caller(request)


@router.get(PREFIX + "/profile")
def profile(venture_id: str, caller=Depends(caller), job_id: str | None = None):
    return api.profile(venture_id, caller, job_id=job_id)

@router.post(PREFIX + "/estimate")
def estimate(venture_id: str, body: StartBody = StartBody(), caller=Depends(caller)):
    return api._estimate(venture_id, caller, variants=body.variants)


@router.post(PREFIX + "/start", status_code=202)
def start(venture_id: str, body: StartBody = StartBody(), caller=Depends(caller)):
    return api._start(venture_id, caller, variants=body.variants, expected_quote=body.quote_fingerprint, require_quote=True)


@router.post(PREFIX + "/build", status_code=202)
def build(venture_id: str, body: BuildBody | None = None, caller=Depends(caller)):
    """One click: quote and approval in the same request, within the internal per-page cap."""
    try:
        return api.build(venture_id, caller, new_version_of=body.new_version_of if body else None)
    except HTTPException as exc:
        code = exc.detail.get("code") if isinstance(exc.detail, dict) else None
        if isinstance(code, str) and re.fullmatch(r"[a-z_]{1,100}", code):
            logging.getLogger(__name__).warning("pagebuilder_build_request_failed", extra={"error_code": code, "venture_id": venture_id})
        raise


@router.get(PREFIX + "/jobs/{job_id}")
def status(venture_id: str, job_id: str, caller=Depends(caller)):
    return api.status(venture_id, caller, job_id)


@router.post(PREFIX + "/jobs/{job_id}/retry-source", status_code=202)
def retry_source(venture_id: str, job_id: str, body: SourceRetryBody, caller=Depends(caller)):
    """Explicitly requeue saved ready bytes; this cannot grant model generation."""
    from app.pagebuilder.native_reference import retry_source as requeue
    wid, actor = api.authority(venture_id, caller, write=True)
    try:
        result = requeue(job_id, person_id=actor, venture_id=venture_id, workspace_id=wid,
            expected_attempt=body.expected_attempt)
    except KeyError as exc:
        raise HTTPException(404, detail={"code": "not_found"}) from exc
    except ValueError as exc:
        raise HTTPException(409, detail={"code": "pagebuilder_source_retry_unavailable"}) from exc
    api.publish_wake()
    return result


@router.get(PREFIX + "/jobs/{job_id}/result")
def result(venture_id: str, job_id: str, caller=Depends(caller)):
    return api.result(venture_id, caller, job_id)


@router.post(PREFIX + "/jobs/{job_id}/tool-result")
async def tool_result(venture_id: str, job_id: str, request: Request):
    if not is_local():
        raise HTTPException(404, detail={"code": "not_found"})
    from pydantic import ValidationError
    from app.pagebuilder.tool import finish
    owner = verify_caller(request, required=True)
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > TOOL_RESULT_MAX_BASE64 + 4096:
            raise HTTPException(413, detail={"code": "pagebuilder_input_too_large",
                "message": "Ask your coding tool to reduce the page assets on Build activity, then retry saving your page."})
        chunks.append(chunk)
    try:
        body = ToolResultBody.model_validate_json(b"".join(chunks))
        blob = base64.b64decode(body.source_zip, validate=True)
    except (ValidationError, ValueError):
        raise HTTPException(409, detail={"code": "pagebuilder_tool_result_invalid",
            "message": "Open Build activity and ask your coding tool to fix the page checks, then retry saving your page."}) from None
    return _save_tool_result(finish, venture_id, owner, job_id, body.order_id, body.execution_id, blob)


@router.post(PREFIX + "/jobs/{job_id}/retry-tool-result")
def retry_tool_result(venture_id: str, job_id: str, request: Request):
    if not is_local():
        raise HTTPException(404, detail={"code": "not_found"})
    from app.pagebuilder.tool import retry_result

    return _save_tool_result(retry_result, venture_id, verify_caller(request, required=True), job_id)


def _save_tool_result(save, *args):
    from app.core.jobs import FatalError, RetryableError

    try:
        return save(*args)
    except ValueError:
        raise HTTPException(409, detail={"code": "pagebuilder_tool_result_invalid",
            "message": "Open Build activity and check the saved page result before retrying the save on Website."}) from None
    except FatalError as exc:
        code = str(exc).partition(":")[0]
        if not re.fullmatch(r"pagebuilder_[a-z_]{1,80}", code):
            code = "pagebuilder_generation_failed"
        raise HTTPException(409, detail={"code": code, "message": api._error_reason(code)}) from None
    except (RetryableError, OSError):
        raise HTTPException(503, detail={"code": "pagebuilder_artifact_write_failed",
            "message": "Free space in the data folder shown in Settings > Keys and connections > Your data folder and backup, then choose Retry saving on Website."}) from None


def bundle(venture_id, caller, job_id, variant):
    row = api._authorized_job(venture_id, caller, job_id)
    view = api._view(row)
    if view["state"] != "ready":
        raise HTTPException(409, detail={"code": view.get("error", "pagebuilder_not_ready")})
    bundles = json.loads(row.payload_json)["result"]["bundles"]
    if not 0 <= variant < len(bundles):
        raise HTTPException(404, detail={"code": "not_found"})
    return bundles[variant]


def file_bytes(data, path):
    from core.artifact_policy import safe_name, ArtifactRejected
    try:
        path = safe_name(path)
    except ArtifactRejected:
        raise HTTPException(404, detail={"code": "not_found"}) from None
    value = data["files"].get(path)
    if value is None:
        raise HTTPException(404, detail={"code": "not_found"})
    body = base64.b64decode(value, validate=True)
    manifest = data["manifest"]["artifact_capture"]["manifest"]
    item = next((item for item in manifest if item["path"] == path), None)
    if not item or item["sha256"] != hashlib.sha256(body).hexdigest() or item["size_bytes"] != len(body):
        raise HTTPException(409, detail={"code": "pagebuilder_artifact_changed"})
    return body


@router.get(PREFIX + "/jobs/{job_id}/preview")
def preview(venture_id: str, job_id: str, variant: int = Query(0, ge=0, le=2), caller=Depends(caller), page_name: str = "index.html"):
    data = bundle(venture_id, caller, job_id, variant)
    if not re.fullmatch(r"index\.html|variant_[a-z][a-z0-9_]{0,19}\.html", page_name):
        raise HTTPException(404, detail={"code": "not_found"})
    content = file_bytes(data, page_name).decode("utf-8")
    local_signup = data["manifest"].get("waitlist") if is_local() and "waitlist.html" in data["files"] else None
    if local_signup:
        try:
            content = signup_preview_notice(content, local_signup.get("notice", ""))
        except ValueError:
            raise HTTPException(409, detail={"code": "pagebuilder_artifact_changed",
                "message": "Your saved signup preview could not be found. On Website, build a new version."}) from None
    def page_link(value):
        path = value.removeprefix("./").removeprefix("/")
        if path not in data["files"]:
            return None
        if path == "waitlist.html" and local_signup:
            return "#waitlist"
        if re.fullmatch(r"variant_[a-z][a-z0-9_]{0,19}\.html", path):
            return f"/api/ventures/{venture_id}/pagebuilder/jobs/{job_id}/preview?variant={variant}&page_name={path}"
    # Policies intersect: the original page policy can block data images even with a preview policy.
    try:
        content = PreviewAssets(data["files"], lambda path: file_bytes(data, path), page_link).document(content, PREVIEW_POLICY)
    except PreviewTooLarge:
        raise _preview_too_large() from None
    return HTMLResponse(content, headers={
        "Content-Security-Policy": "sandbox allow-popups allow-popups-to-escape-sandbox; default-src 'none'; img-src data:; style-src 'unsafe-inline'; font-src data:; script-src 'none'; connect-src 'none'; form-action 'none'; base-uri 'none'; frame-ancestors 'self' https://*.supymem.com",
        "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer"})


@router.get(PREFIX + "/jobs/{job_id}/preview-document")
def preview_document(venture_id: str, job_id: str, variant: int = Query(0, ge=0, le=2), caller=Depends(caller), page_name: str = "index.html"):
    # The native API bridge signs JSON requests; an iframe URL cannot use that bridge.
    document = preview(venture_id, job_id, variant, caller, page_name)
    try:
        content = document.body.decode("utf-8")
        if len(document.body) > PREVIEW_DOCUMENT_MAX_BYTES:
            raise PreviewTooLarge("index.html")
        return {"html": srcdoc_document(content)}
    except PreviewTooLarge:
        raise _preview_too_large() from None


@router.get(PREFIX + "/jobs/{job_id}/assets/{path:path}")
def asset(venture_id: str, job_id: str, path: str, variant: int = Query(0, ge=0, le=2), caller=Depends(caller)):
    data = bundle(venture_id, caller, job_id, variant)
    mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
    return Response(file_bytes(data, path), media_type=mime, headers={"Cache-Control": "private, no-store",
        "Content-Security-Policy": "sandbox; default-src 'none'", "X-Content-Type-Options": "nosniff"})


@router.get(PREFIX + "/jobs/{job_id}/source")
def source(venture_id: str, job_id: str, variant: int = Query(0, ge=0, le=2), caller=Depends(caller)):
    data = bundle(venture_id, caller, job_id, variant)
    body = base64.b64decode(data["source_zip"], validate=True)
    capture = data["manifest"]["artifact_capture"]
    if len(body) != capture["size_bytes"] or hashlib.sha256(body).hexdigest() != capture["sha256"]:
        raise HTTPException(409, detail={"code": "pagebuilder_artifact_changed"})
    return Response(body, media_type="application/zip", headers={"Cache-Control": "no-store",
        "Content-Disposition": f'attachment; filename="landing-{job_id}-{variant}.zip"', "X-Content-Type-Options": "nosniff"})


@router.get(PREFIX + "/jobs/{job_id}/screenshots/{name}")
def screenshot(venture_id: str, job_id: str, name: str, variant: int = Query(0, ge=0, le=2), caller=Depends(caller)):
    data = bundle(venture_id, caller, job_id, variant)
    if name not in {"desktop.png", "mobile.png"} or name not in data["screenshots"]:
        raise HTTPException(404, detail={"code": "not_found"})
    body = base64.b64decode(data["screenshots"][name], validate=True)
    metadata = data["screenshot_manifest"][name]
    if len(body) != metadata["size_bytes"] or hashlib.sha256(body).hexdigest() != metadata["sha256"]:
        raise HTTPException(409, detail={"code": "pagebuilder_artifact_changed"})
    return Response(body, media_type="image/png", headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})


@router.get(PREFIX + "/spec")
def spec(venture_id: str, job_id: str | None = None, variant: int = Query(0, ge=0, le=2), caller=Depends(caller)):
    content = bundle(venture_id, caller, job_id, variant)["spec_markdown"] if job_id else api.build_spec_for_coding_tool(venture_id, caller)
    return {"markdown": content}
