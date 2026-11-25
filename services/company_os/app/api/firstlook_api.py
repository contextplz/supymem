"""First-look HTTP surface (45 §9). Parse args, call service, shape response."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qs

from core.artifact_policy import ArtifactRejected, inspect_blob, read_file
from core.logging import _local_secret_values
from core.request_id import from_request
from fastapi import APIRouter, Header, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.responses import Response as FastResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.authz.workspace import assert_venture_access, assert_workspace_access
from app.core.local_mode import is_local
from app.credits.errors import CreditError, InsufficientCredits
from app.firstlook import interest as interest_mod
from app.firstlook import serve
from app.firstlook import service as fl_service
from app.firstlook import versions as fl_versions
from app.firstlook.service import FirstLookError
from app.llm.provider_errors import provider_error_for_user
from app.surfaces.ratelimit import Limiter
from llm_gateway.errors import ProviderCallError

log = logging.getLogger(__name__)
router = APIRouter(tags=["firstlook"])
_INTEREST = Limiter(capacity=30, per_seconds=60.0)
_LOGO_HEADERS = {"X-Content-Type-Options": "nosniff", "Content-Security-Policy": "sandbox; default-src 'none'; style-src 'unsafe-inline'", "Cache-Control": "no-cache"}


def _first_form_value(parsed: dict[str, list[str]], key: str) -> str | None:
    vals = parsed.get(key) or []
    if not vals:
        return None
    return vals[0]


def _parse_version(raw: object) -> int | None:
    if raw in (None, ""):
        return None
    try:
        return int(str(raw))
    except (TypeError, ValueError):
        return None


async def _interest_fields(request: Request) -> tuple[str | None, str | None, str, int | None]:
    """JSON, urlencoded, or multipart — never require a coding-agent Content-Type."""
    import json

    content_type = (request.headers.get("content-type") or "").lower()
    raw = await request.body()
    email: str | None = None
    note: str | None = None
    source = "direct"
    site_version: int | None = None

    stripped = raw.lstrip()
    as_json = "application/json" in content_type or stripped.startswith(b"{") or stripped.startswith(b"[")
    if as_json and raw:
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HTTPException(422, detail="Invalid interest payload") from exc
        if not isinstance(payload, dict):
            raise HTTPException(422, detail="Invalid interest payload")
        if payload:
            email = payload.get("email")
            note = payload.get("note")
            source = str(payload.get("source") or "direct")
            site_version = _parse_version(payload.get("site_version"))
            return str(email) if email is not None else None, str(note) if note else None, source, site_version

    if raw and "multipart/" not in content_type:
        parsed = parse_qs(raw.decode("utf-8", errors="replace"), keep_blank_values=True)
        if parsed:
            email = _first_form_value(parsed, "email")
            note = _first_form_value(parsed, "note")
            source = _first_form_value(parsed, "source") or "direct"
            site_version = _parse_version(_first_form_value(parsed, "site_version"))
            return email, note, source, site_version

    if "multipart/" in content_type:
        form = await request.form()
        email = form.get("email")  # type: ignore[assignment]
        note = form.get("note")  # type: ignore[assignment]
        source = str(form.get("source") or "direct")
        site_version = _parse_version(form.get("site_version"))
    return email, note, source, site_version


class GenerateBody(BaseModel):
    idea_text: str = Field(min_length=3)
    venture_id: str | None = None
    venture_name: str | None = None
    archetype: str | None = None
    audience: str | None = None


class TryAnother(BaseModel):
    idea_text: str | None = None
    venture_id: str | None = None
    venture_name: str | None = None
    archetype: str | None = None


def _fail(exc: FirstLookError) -> HTTPException:
    status = {
        "idea_too_short": 422,
        "no_site": 404,
        "variant_cap": 409,
        "firstlook_retired": 410,
        "coding_tool_required": 409,
        "page_coding_tool_required": 409,
    }.get(exc.code, 422)
    return HTTPException(
        status, detail={"error": {"code": exc.code, "message": exc.message}}
    )


def _paid_call_failure(exc: ProviderCallError | CreditError) -> HTTPException:
    if exc.code == "local_spend_limit":
        return HTTPException(402, detail={"error": {"code": exc.code, "message": str(exc), "retryable": False}})
    if isinstance(exc, ProviderCallError):
        error = provider_error_for_user(exc)
        message = error["message"] + " " + error["hint"]
    else:
        message = ("Page generation paused because the credit check did not pass. "
                   "Open Credits to review your allowance or request credits.")
    return HTTPException(402 if isinstance(exc, InsufficientCredits) else 409,
        detail={"error": {"code": exc.code, "message": message, "retryable": False}})


def _assert_site_writer(workspace_id: str, venture_id: str | None, subject: str | None) -> None:
    """HTTP writes retain authority even if tenancy middleware did not bind a scope."""
    from app.authz.workspace import subject_person_id
    from app.tenancy import current_or_none
    from app.workspaces.service import role_of
    scope = current_or_none()
    actor = scope.actor if scope else subject
    if scope and (scope.workspace_id != workspace_id or
                  (scope.venture_id and scope.venture_id != venture_id)):
        raise HTTPException(404, detail={"error": {"code": "not_found"}})
    assert_workspace_access(workspace_id, actor)
    if role_of(workspace_id, subject_person_id(actor)) not in {"owner", "admin", "maintainer", "contributor"}:
        raise HTTPException(403, detail={"error": {"code": "site_write_forbidden"}})
    if venture_id and assert_venture_access(venture_id, actor)[1] != workspace_id:
        raise HTTPException(404, detail={"error": {"code": "not_found"}})


def _assert_site_reader(workspace_id: str, venture_id: str, subject: str | None) -> None:
    from app.tenancy import current_or_none
    scope = current_or_none()
    if not venture_id or (scope and (scope.workspace_id != workspace_id or
                         (scope.venture_id and scope.venture_id != venture_id))):
        raise HTTPException(404, detail={"error": {"code": "not_found"}})
    if assert_venture_access(venture_id, scope.actor if scope else subject)[1] != workspace_id:
        raise HTTPException(404, detail={"error": {"code": "not_found"}})


@router.get("/workspaces/{workspace_id}/site")
def get_site(workspace_id: str, venture_id: str | None = None,
             x_subject: str | None = Header(default=None, alias="X-Subject")):
    if venture_id is not None:
        _assert_site_reader(workspace_id, venture_id, x_subject)
    site = fl_service.current_site(workspace_id, venture_id=venture_id)
    if site is None:
        raise HTTPException(
            404,
            detail={
                "error": {
                    "code": "no_site",
                    "message": "No page yet for this workspace.",
                }
            },
        )
    return site


@router.get("/workspaces/{workspace_id}/site/preview")
def get_private_site_preview(
    workspace_id: str,
    x_subject: str | None = Header(default=None, alias="X-Subject"),
    venture_id: str | None = None,
    v: int | None = None,
):
    """Return a static, authenticated draft preview with local assets inlined."""
    import base64

    from app.firstlook.brand_assets import custom_logo_path
    from app.firstlook.brandkit import build_brandkit, svg_mark_data_uri
    from app.firstlook.serve import inline_stylesheets

    if venture_id is not None:
        _assert_site_reader(workspace_id, venture_id, x_subject)
    else:
        assert_workspace_access(workspace_id, x_subject)
    site = fl_service.current_site(workspace_id, version=v, venture_id=venture_id)
    if site is None:
        raise HTTPException(404, detail={"error": {"code": "no_site"}})
    copy = site.get("copy") if isinstance(site.get("copy"), dict) else {}
    snapshot = copy.get("_render_snapshot") if isinstance(copy.get("_render_snapshot"), dict) else {}
    html = str(snapshot.get("html") or copy.get("page_html") or "")
    if is_local() and len(html) <= 40:
        html = _owned_site_html(workspace_id, site)
    if len(html) <= 40:
        raise HTTPException(404, detail={"error": {"code": "no_preview"}})
    extras = snapshot.get("files") if isinstance(snapshot.get("files"), dict) else {}
    extras = extras or fl_service.page_files_from_copy(copy)
    html = inline_stylesheets(html, extras)
    logo = custom_logo_path(workspace_id, site.get("venture_id"))
    saved_logo = snapshot.get("logo") or {}
    if saved_logo.get("data") and saved_logo.get("suffix") in {".svg", ".png", ".jpg", ".jpeg", ".webp", ".gif"}:
        media = {".svg": "image/svg+xml", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                 ".webp": "image/webp", ".gif": "image/gif"}[saved_logo["suffix"]]
        logo_uri = f"data:{media};base64," + saved_logo["data"]
    elif logo is not None:
        media = {
            ".svg": "image/svg+xml",
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".webp": "image/webp",
            ".gif": "image/gif",
        }.get(logo.suffix.lower(), "application/octet-stream")
        logo_uri = f"data:{media};base64," + base64.b64encode(logo.read_bytes()).decode("ascii")
    else:
        brand = site.get("brandkit") or build_brandkit(workspace_id).to_dict()
        logo_uri = svg_mark_data_uri(brand)
    import re
    html = re.sub(rf"/w/{re.escape(workspace_id)}/brand/logo(?:\?[^\"'\s<>]*)?", lambda match: logo_uri, html)
    csp = (
        '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; '
        "img-src data:; style-src 'unsafe-inline' data:; script-src 'none'; "
        "base-uri 'none'; form-action 'none'; frame-ancestors 'none'\">"
    )
    return JSONResponse(
        {"html": csp + html, "version": site.get("version"), "published": bool(site.get("published")),
         "venture_id": site.get("venture_id"), "site_id": site.get("id")},
        headers=_SITE_NO_STORE,
    )


@router.post("/workspaces/{workspace_id}/site", status_code=201)
@router.post("/workspaces/{workspace_id}/firstlook", status_code=201)
async def generate_site(workspace_id: str, body: GenerateBody,
                        x_subject: str | None = Header(default=None, alias="X-Subject")):
    _assert_site_writer(workspace_id, body.venture_id, x_subject)
    try:
        if not is_local():
            fl_service.assert_existing_page(workspace_id, body.venture_id)  # no new template pages
        view = await fl_service.generate_first_look(
            workspace_id,
            body.idea_text,
            venture_id=body.venture_id,
            venture_name=body.venture_name,
            archetype=body.archetype,
            audience=body.audience,
        )
    except FirstLookError as exc:
        raise _fail(exc) from exc
    except (ProviderCallError, CreditError) as exc:
        raise _paid_call_failure(exc) from None
    return view if is_local() and isinstance(view, dict) else view.to_dict()


@router.post("/workspaces/{workspace_id}/site/try-another")
@router.post("/workspaces/{workspace_id}/firstlook/try-another")
@router.post("/workspaces/{workspace_id}/firstlook/variant")
async def try_another(workspace_id: str, body: TryAnother | None = None,
                      x_subject: str | None = Header(default=None, alias="X-Subject")):
    body = body or TryAnother()
    _assert_site_writer(workspace_id, body.venture_id, x_subject)
    existing = fl_service.current_site(workspace_id, venture_id=body.venture_id)
    if not existing:
        if is_local():
            raise _fail(FirstLookError("no_site", "Ask Charlie to build your landing page first; connect your coding tool in Settings > Keys and connections > Coding tools, then retry."))
        raise _fail(fl_service.RETIRED)  # no new template pages
    idea = (body.idea_text or "").strip()
    if not idea and existing:
        idea = str((existing.get("copy") or {}).get("headline") or "")
    try:
        view = await fl_service.try_another_look(
            workspace_id,
            idea,
            venture_id=body.venture_id,
            venture_name=body.venture_name,
            archetype=body.archetype or (existing or {}).get("archetype"),
        )
    except FirstLookError as exc:
        raise _fail(exc) from exc
    except (ProviderCallError, CreditError) as exc:
        raise _paid_call_failure(exc) from None
    return view if is_local() and isinstance(view, dict) else view.to_dict()


_BUILDING_HTML = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Landing page in progress</title>
<style>
  body { margin: 0; min-height: 100vh; display: grid; place-items: center; font: 16px/1.5 ui-sans-serif, system-ui;
    background: #f6f3ee; color: #1b1a17; }
  main { max-width: 28rem; padding: 2rem; }
  p { margin: 0 0 0.75rem; }
  .kicker { letter-spacing: 0.08em; text-transform: uppercase; font-size: 0.75rem; color: #6b665c; }
</style></head>
<body><main>
  <p class="kicker">Not ready</p>
  <p>Lina is planning this page. Cole builds it after that. Nothing is public yet.</p>
</main></body></html>
"""


_SITE_NO_STORE = {"Cache-Control": "no-store, max-age=0", "Pragma": "no-cache"}


def _public_not_found() -> FastResponse:
    return FastResponse(
        b"Not found",
        status_code=404,
        media_type="text/plain; charset=utf-8",
        headers=_SITE_NO_STORE,
    )


def _public_site_headers(workspace_id: str) -> dict[str, str]:
    from app.firstlook.waitlist import public_interest_url
    from app.product_analytics.beacon import public_events_url

    events = public_events_url() or "'none'"
    return {
        **_SITE_NO_STORE,
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": (
            "sandbox allow-scripts allow-forms; default-src 'none'; "
            "script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; "
            f"connect-src {events}; base-uri 'none'; object-src 'none'; frame-src 'none'; "
            f"form-action {public_interest_url(workspace_id)}"
        ),
    }


def _owned_site_asset(workspace_id, version, filename):
    root = serve._data_root()
    name = (serve.site_dir(workspace_id=workspace_id, version=version) / filename).relative_to(root).as_posix()
    return read_file(root, name, exact_secrets=tuple(_local_secret_values() or ()))


def _owned_site_html(workspace_id, site):
    try:
        source = _owned_site_asset(workspace_id, int(site.get("version") or 1), "index.html")
        if hashlib.sha256(source).hexdigest() == site.get("sha256"):
            return source.decode("utf-8")
    except (ValueError, OSError):
        pass
    return ""


@router.get("/w/{workspace_id}/site/")
def serve_site_html(workspace_id: str, v: int | None = Query(default=None)):
    site = fl_service.public_site(workspace_id, version=v)
    if site is None:
        return HTMLResponse(_BUILDING_HTML, status_code=404, headers=_SITE_NO_STORE)
    copy = site.get("copy") if isinstance(site.get("copy"), dict) else {}
    snapshot = copy.get("_render_snapshot") if isinstance(copy.get("_render_snapshot"), dict) else {}
    extras = fl_service.page_files_from_copy(copy)
    stored = str(snapshot.get("html") or (copy or {}).get("page_html") or "")
    # Legacy rows may only reference a file. Serve it while its bytes still
    # match the immutable database hash; never trust a later disk replacement
    # or a previously materialized copy after the source changed.
    if is_local():
        if len(stored) <= 40:
            return HTMLResponse(_BUILDING_HTML, status_code=404, headers=_SITE_NO_STORE)
        path = None
    elif len(stored) <= 40:
        recorded = Path(str(site.get("path") or ""))
        if not recorded.is_file():
            return HTMLResponse(_BUILDING_HTML, status_code=404, headers=_SITE_NO_STORE)
        raw = recorded.read_bytes()
        if not site.get("sha256") or hashlib.sha256(raw).hexdigest() != site.get("sha256"):
            return HTMLResponse(_BUILDING_HTML, status_code=404, headers=_SITE_NO_STORE)
        body = raw.decode("utf-8")
        stored = body
        path = None
    else:
        path = fl_service.materialize_site_files(site)
    if path is not None and Path(str(path)).is_file():
        body = Path(str(path)).read_text(encoding="utf-8")
    elif len(stored) > 40:
        body = stored
    else:
        return HTMLResponse(_BUILDING_HTML, status_code=404, headers=_SITE_NO_STORE)
    from app.firstlook.serve import inline_stylesheets, version_stylesheet_hrefs
    from app.firstlook.waitlist import rewrite_waitlist_html

    html = rewrite_waitlist_html(body, workspace_id, native_submit=True)
    html = inline_stylesheets(html, extras)
    html = version_stylesheet_hrefs(html, int(site.get("version") or 1))
    from app.firstlook.asset_versions import AssetVersions

    html = AssetVersions(
        workspace_id,
        int(site.get("version") or 1),
        extras,
    ).html(html)
    # Browser beacon owns landing_view; no server-side pageview on GET.
    return HTMLResponse(html, headers=_public_site_headers(workspace_id))


@router.get("/w/{workspace_id}/site/{filename}")
def serve_site_asset(workspace_id: str, filename: str, v: int | None = Query(default=None)):
    if filename in {"", "index.html"}:
        return serve_site_html(workspace_id, v=v)
    if "/" in filename or filename.startswith(".") or not filename.replace(".", "", 1).replace("-", "").replace("_", "").isalnum():
        return _public_not_found()
    site = fl_service.public_site(workspace_id, version=v)
    if site is None:
        return _public_not_found()
    version = int(v or site.get("version") or 1)
    path = serve.site_dir(workspace_id=workspace_id, version=version) / filename
    copy = site.get("copy") if isinstance(site.get("copy"), dict) else {}
    extras = fl_service.page_files_from_copy(copy)
    if is_local():
        snapshot = copy.get("_render_snapshot")
        if isinstance(snapshot, dict) and isinstance(snapshot.get("files"), dict) and snapshot["files"]:
            extras = fl_service.page_files_from_copy({"page_files": snapshot["files"]})
    body = extras.get(filename)
    if is_local():
        if not body:
            return _public_not_found()
        try:
            inspect_blob(body.encode("utf-8"), exact_secrets=tuple(_local_secret_values() or ()))
            try:
                _owned_site_asset(workspace_id, version, filename)
            except (ArtifactRejected, FileNotFoundError) as error:
                if not isinstance(error, FileNotFoundError) and not isinstance(error.__cause__, FileNotFoundError):
                    raise
        except (ValueError, OSError):
            return _public_not_found()
    elif not path.is_file():
        recorded = Path(str(site.get("path") or ""))
        candidate = recorded.parent / filename
        if candidate.is_file():
            path = candidate
        elif isinstance(body, str) and body:
            fl_service.materialize_site_files(site)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body, encoding="utf-8")
        else:
            fl_service.materialize_site_files(site)
            if not path.is_file():
                return _public_not_found()
    media = {
        ".css": "text/css; charset=utf-8",
        ".js": "text/javascript; charset=utf-8",
        ".svg": "image/svg+xml",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".gif": "image/gif",
        ".html": "text/html; charset=utf-8",
    }.get(path.suffix.lower(), "application/octet-stream")
    if isinstance(body, str) and body and filename in extras:
        from app.firstlook.asset_versions import AssetVersions

        pinned = AssetVersions(workspace_id, version, extras)
        if path.suffix.lower() == ".css":
            body = pinned.css(body)
        elif path.suffix.lower() == ".html":
            body = pinned.html(body)
        return FastResponse(body.encode("utf-8"), media_type=media, headers=_public_site_headers(workspace_id))
    return FileResponse(path, media_type=media, headers=_public_site_headers(workspace_id))


def _brand_logo_response(workspace_id: str, site: dict):
    import base64

    from app.firstlook.brand_assets import custom_logo_path
    from app.firstlook.brandkit import build_brandkit, svg_mark

    path = custom_logo_path(workspace_id, site.get("venture_id"))
    if path and path.is_file():
        media = {
            ".svg": "image/svg+xml",
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".webp": "image/webp",
            ".gif": "image/gif",
        }.get(path.suffix.lower(), "application/octet-stream")
        return FileResponse(path, media_type=media, headers=_LOGO_HEADERS)
    saved = (site.get("copy") or {}).get("_render_snapshot", {}).get("logo", {})
    media = {".svg": "image/svg+xml", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
             ".webp": "image/webp", ".gif": "image/gif"}.get(saved.get("suffix"))
    if media and saved.get("data"):
        try:
            return FastResponse(base64.b64decode(saved["data"], validate=True), media_type=media, headers=_LOGO_HEADERS)
        except ValueError:
            pass
    # Fallback: generated SVG mark from latest site brandkit / idea hash
    brand = site.get("brandkit")
    if not brand:
        brand = build_brandkit(str(site.get("idea_text") or workspace_id)).to_dict()
    return FastResponse(svg_mark(brand), media_type="image/svg+xml", headers=_LOGO_HEADERS)


@router.get("/w/{workspace_id}/brand/logo")
def serve_public_brand_logo(
    workspace_id: str,
    v: int | None = Query(default=None),
    venture_id: str | None = Query(default=None),
):
    import base64

    site = fl_service.public_site(workspace_id, version=v)
    if site is None or (venture_id is not None and site.get("venture_id") != venture_id):
        return _public_not_found()
    copy = site.get("copy") if isinstance(site.get("copy"), dict) else {}
    snapshot = copy.get("_render_snapshot") if isinstance(copy.get("_render_snapshot"), dict) else {}
    logo = snapshot.get("logo") if isinstance(snapshot.get("logo"), dict) else {}
    encoded = logo.get("data")
    suffix = str(logo.get("suffix") or "").lower()
    if isinstance(encoded, str) and encoded:
        try:
            data = base64.b64decode(encoded, validate=True)
        except ValueError:
            return _public_not_found()
        media = {
            ".svg": "image/svg+xml",
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".webp": "image/webp",
            ".gif": "image/gif",
        }.get(suffix)
        if media:
            return FastResponse(data, media_type=media, headers=_LOGO_HEADERS)
    from app.firstlook.brandkit import build_brandkit, svg_mark

    brand = site.get("brandkit") or build_brandkit(str(site.get("idea_text") or workspace_id)).to_dict()
    return FastResponse(svg_mark(brand), media_type="image/svg+xml", headers=_LOGO_HEADERS)


@router.get("/workspaces/{workspace_id}/brand/logo")
def serve_private_brand_logo(
    workspace_id: str,
    x_subject: str | None = Header(default=None, alias="X-Subject"),
    venture_id: str | None = None,
):
    if venture_id:
        _assert_site_reader(workspace_id, venture_id, x_subject)
    else:
        assert_workspace_access(workspace_id, x_subject)
    return _brand_logo_response(workspace_id, fl_service.current_site(workspace_id, venture_id=venture_id) or {"venture_id": venture_id})


@router.post("/ventures/{venture_id}/brand/logo")
async def upload_venture_logo(venture_id: str, request: Request):
    """Founder logo upload (PNG/SVG/WebP). Regenerates site if one exists."""
    from app.firstlook.brand_assets import custom_logo_url, save_logo

    wid, idea, name = fl_service.brand_logo_context(venture_id)
    if is_local():
        _assert_site_writer(wid, venture_id, request.headers.get("X-Subject"))

    form = await request.form()
    file = form.get("file") or form.get("logo")
    if file is None or not hasattr(file, "read"):
        raise HTTPException(422, detail={"error": {"code": "file_required"}})
    data = await file.read(2_000_001)  # type: ignore[misc]
    filename = getattr(file, "filename", None) or "logo.png"
    try:
        save_logo(wid, data, str(filename), venture_id)
    except ValueError as exc:
        raise HTTPException(422, detail={"error": {"code": "bad_logo", "message": str(exc)}}) from exc

    # A saved logo and a regenerated preview are distinct outcomes.
    site_refreshed = False
    current = fl_service.current_site(wid, venture_id=venture_id) or {}
    if is_local():
        result = {"ok": True, "logo_url": custom_logo_url(wid, venture_id), "site_refreshed": False}
        if not current:
            result["next_step"] = "Your logo is saved. Ask Charlie to build your landing page when you are ready."
            return result
        source = fl_versions.get_latest(wid, venture_id=venture_id)
        if source is None or source.id != current["id"]:
            result["next_step"] = "Your logo is saved and your page has changed. Open Website to review your latest page, then ask Charlie to retry the logo change."
            return result
        try:
            view = await fl_service.generate_first_look(
                wid, idea, venture_id=venture_id, venture_name=name,
                stage="refined" if current.get("stage") == "refined" else "first_look",
                reuse_copy=source.copy, edit_source=source,
                edit_instruction="Replace only the logo with your uploaded logo. Preserve your copy, layout, forms and links.",
            )
        except FirstLookError as exc:
            result["next_step"] = exc.message
        except HTTPException as exc:
            detail = exc.detail
            result["next_step"] = (detail.get("message") or (detail.get("error") or {}).get("message")) if isinstance(detail, dict) else detail
            if not result["next_step"]:
                result["next_step"] = "Your logo is saved. Open Builds to check your page edit, then ask Charlie to retry the logo change."
        except (ProviderCallError, CreditError) as exc:
            raise _paid_call_failure(exc) from None
        else:
            if isinstance(view, dict):
                result["device_order"] = view
                result["next_step"] = "Your logo is saved. Open Builds to check your coding tool's page edit, then ask Charlie to retry if needed."
            else:
                result["site_refreshed"] = True
        return result
    try:
        await fl_service.generate_first_look(
            wid,
            idea,
            venture_id=venture_id,
            venture_name=name,
            stage="refined" if current.get("stage") == "refined" else "first_look",
            allow_model=False,
            reuse_copy=current.get("copy"),
        )
        site_refreshed = True
    except FirstLookError:
        pass

    return {
        "ok": True,
        "logo_url": custom_logo_url(wid, venture_id),
        "site_refreshed": site_refreshed,
    }


_INTEREST_HEADERS = {
    "Cache-Control": "no-store, max-age=0",
    "Vary": "Accept",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "sandbox; default-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
}


def _interest_html_requested(request: Request) -> bool:
    # Require explicit HTML acceptance: absent headers and generic API */*
    # retain JSON. Honor quality and prefer JSON for equal explicit preferences.
    accepted = {}
    for part in request.headers.get("accept", "").lower().split(","):
        media, *parameters = part.strip().split(";")
        quality = 1.0
        for parameter in parameters:
            key, _, value = parameter.strip().partition("=")
            if key == "q":
                try:
                    quality = float(value)
                except ValueError:
                    quality = 0.0
        if 0 <= quality <= 1:
            accepted[media] = quality
    return accepted.get("text/html", 0) > accepted.get("application/json", 0)


def _interest_reply(request: Request, *, status: int, payload: dict, headers: dict):
    if not _interest_html_requested(request):
        return JSONResponse(payload, status_code=status, headers=headers)
    # Only fixed application prose enters HTML. Never interpolate workspace,
    # email, note, referrer, exception text or the JSON response's personal data.
    if status == 201:
        title, message = "Thank you", "Your interest has been recorded. You can close this page."
    elif status == 429:
        title, message = "Please wait", "Too many submissions were received. Wait before trying again."
    elif status in {400, 422}:
        title, message = "Check your submission", "Your submission could not be accepted. Go back and check the form before trying again."
    else:
        title, message = "Submission not confirmed", "We could not confirm your submission. It may have been saved. Please avoid submitting it again immediately."
    return HTMLResponse(
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<title>{title}</title></head><body><main><h1>{title}</h1><p>{message}</p></main></body></html>',
        status_code=status, headers=headers)


@router.post("/w/{workspace_id}/interest", status_code=201)
@router.post("/api/w/{workspace_id}/interest", status_code=201)
async def capture_interest(workspace_id: str, request: Request):
    client = request.client.host if request.client else "local"
    limit = _INTEREST.check(f"interest:{workspace_id}:{client}")
    headers = {**_INTEREST_HEADERS, "X-RateLimit-Limit": str(limit.limit),
               "X-RateLimit-Remaining": str(limit.remaining)}
    if not limit.allowed:
        headers["Retry-After"] = str(limit.retry_after_s)
        return _interest_reply(request, status=429, headers=headers, payload={"detail": {"error": {
            "code": "rate_limited", "message": "Too many interest signals, try again shortly."}}})
    try:
        email, note, source, site_version = await _interest_fields(request)
        result = fl_service.record_interest(
            workspace_id,
            email=str(email) if email else None,
            note=str(note) if note else None,
            site_version=site_version,
            source=source,
            referrer=request.headers.get("referer"),
        )
    except StarletteHTTPException as exc:
        return _interest_reply(request, status=exc.status_code, headers=headers,
            payload={"detail": {"error": {"code": "interest_invalid", "message": "Submission could not be accepted."}}})
    except Exception as exc:
        log.error("interest_submission_unconfirmed", extra={
            "error_type": type(exc).__name__, "ws": workspace_id, "request_id": from_request(request),
        })
        # Persistence or post-write audit may have failed. Never announce success
        # or promise no record exists. Do not include exception/entered prose.
        return _interest_reply(request, status=500, headers=headers,
            payload={"detail": {"error": {"code": "interest_unconfirmed", "message": "Submission could not be confirmed."}}})
    # First-look interest has no consent checkbox. Do not emit signup here.
    # Browser beacon owns cta_click; managed waitlist owns consented signup.
    return _interest_reply(request, status=201, headers=headers, payload={**result, "received": True})


@router.get("/workspaces/{workspace_id}/interest")
def get_interest(
    workspace_id: str,
    detail: int = 0,
    x_subject: str | None = Header(default=None, alias="X-Subject"),
):
    assert_workspace_access(workspace_id, x_subject)
    agg = interest_mod.aggregate(workspace_id)
    if detail:
        # Authenticated gateway session only — emails for founder inbox, never for models.
        agg["leads"] = interest_mod.list_leads(workspace_id)
    return agg


@router.get("/ventures/{venture_id}/leads")
def get_venture_leads(
    venture_id: str,
    x_subject: str | None = Header(default=None, alias="X-Subject"),
):
    """Venture-specific consented interest; legacy workspace records stay unassigned."""
    _, workspace_id = assert_venture_access(venture_id, x_subject)
    from app.conversion import waitlist
    try:
        result = waitlist.status(venture_id, workspace_id, include_leads=True)
    except waitlist.WaitlistError as exc:
        raise HTTPException(exc.status, detail={"code": exc.code}) from None
    return {**result, "workspace_id": workspace_id, "by_version": {}, "source": "managed_venture_waitlist"}


def _lead_scope(venture_id: str, subject: str | None, *, write: bool = False) -> str:
    return fl_service.lead_scope(venture_id, subject, write=write)


def _addressless_web_lead(lead: dict) -> bool:
    return lead.get("source") == "web" and not lead.get("email")


_LEAD_OUTREACH_RESTRICTED = "This website asks not to receive outreach. Choose another lead in Leads."


def _lead_website(lead: dict) -> str:
    from app.gtm.reachable import reach_url

    if lead.get("status") in {"opted out", "bounced"}:
        raise HTTPException(409, detail={"message": "You cannot contact this lead. Choose another lead in Leads."})
    if lead.get("outreach_restricted"):
        raise HTTPException(409, detail={"message": _LEAD_OUTREACH_RESTRICTED})
    if not (website := reach_url(lead.get("website") or "")):
        raise HTTPException(422, detail={"message": "Open the saved source in Leads to look for contact details, or choose another lead."})
    return website


def _with_local_lead_actions(venture_id: str, view: dict) -> dict:
    if is_local() and (leads := [lead for lead in view["items"] if lead.get("source") == "web"]):
        from app.gtm.contact_work import _notes
        from app.gtm.reachable import reach_url

        notes = {(note.get("contact_id"), note.get("type")): note for note in reversed(_notes(venture_id))}
        for lead in leads:
            saved = lead.get("saved", {})
            if "work_notes" in saved:
                saved["work_notes"] = [note for note in saved["work_notes"] if note.get("type") != "lead_message_draft"]
            page = notes.get((lead["id"], "lead_contact_page"), {})
            if (lead.get("outreach_restricted") or page.get("outreach_restricted") or
                    any(route.get("rejected_by") == "no_solicitation" for route in lead.get("routes", []))):
                lead.update(outreach_restricted=True, can_draft=False)
                continue
            if lead.get("email") or lead.get("status") in {"opted out", "bounced"}:
                continue
            website = reach_url(lead.get("website") or "")
            if website and page.get("checked_website") == website and (contact_page := reach_url(page.get("contact_page") or "")):
                lead["contact_page"] = contact_page
                draft = notes.get((lead["id"], "lead_message_draft"), {})
                if draft.get("checked_website") == website:
                    lead["message_draft"] = draft["message_draft"]
    return view


@router.get("/ventures/{venture_id}/leads/contacts")
def saved_contacts(venture_id: str, response: Response, x_subject: str | None = Header(default=None, alias="X-Subject")):
    from app.gtm.leads import saved_leads

    workspace_id = _lead_scope(venture_id, x_subject)
    response.headers["Cache-Control"] = "private, no-store"
    return _with_local_lead_actions(venture_id, saved_leads(workspace_id, venture_id))


@router.get("/ventures/{venture_id}/leads/contacts/export")
def export_contacts(venture_id: str, x_subject: str | None = Header(default=None, alias="X-Subject")):
    import csv
    import io
    from app.gtm.leads import saved_leads

    workspace_id = _lead_scope(venture_id, x_subject)
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(["Name", "Role", "Company", "Email", "Source", "Source URL", "Status", "LinkedIn", "Notes"])
    for lead in saved_leads(workspace_id, venture_id)["items"]:
        values = [str(lead.get(field) or "") for field in
            ("name", "role", "company", "email", "source", "source_url", "status", "linkedin_url", "notes")]
        writer.writerow(["'" + value if value.lstrip().startswith(("=", "+", "-", "@")) or value.startswith(("\t", "\r", "\n")) else value for value in values])
    return FastResponse(output.getvalue(), media_type="text/csv; charset=utf-8", headers={
        "Content-Disposition": 'attachment; filename="leads.csv"', "Cache-Control": "private, no-store"})


class FindLeadContactBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


@router.post("/ventures/{venture_id}/leads/contacts/{contact_id}/find-contact")
async def find_lead_contact(venture_id: str, contact_id: str, body: FindLeadContactBody | None = None,
                            x_subject: str | None = Header(default=None, alias="X-Subject")):
    from app.authz.workspace import subject_person_id
    from app.core.local_capabilities import blocked_message, enabled
    from app.gtm import contact_work, enrich, reachable
    from app.gtm.leads import saved_leads
    from app.signals.research import FirecrawlLimiterUnavailable
    from app.tenancy import bind

    workspace_id = _lead_scope(venture_id, x_subject, write=True)
    if not is_local():
        raise HTTPException(404, detail={"message": "Open Leads in your desktop app to find a contact."})
    view = _with_local_lead_actions(venture_id, saved_leads(workspace_id, venture_id))
    lead = next((item for item in view["items"] if item["id"] == contact_id), None)
    if not lead or not _addressless_web_lead(lead):
        raise HTTPException(404, detail={"message": "Refresh Leads and select a saved web lead that still needs a contact."})
    website = _lead_website(lead)
    if not enabled("research"):
        raise HTTPException(409, detail={"message": blocked_message("research")})
    with bind(workspace_id=workspace_id, venture_id=venture_id, actor=subject_person_id(x_subject)):
        try:
            # shortcut: 12 seconds inside the desktop's 15-second request, use a job if slow sites need more time.
            async with asyncio.timeout(12):
                facts = await enrich.enrich_site(None, website, workspace_id, venture_id, published_mailboxes=True)
        except (TimeoutError, FirecrawlLimiterUnavailable):
            raise HTTPException(504, detail={"message": "The website took too long to check. Open Company website in Leads to look for a contact page, or select Find a contact to retry."}) from None
        except CreditError:
            raise HTTPException(409, detail={"message": "Open Settings > Keys and connections > Spending and review your monthly spend cap, then select Find a contact in Leads again."}) from None

    if facts.get("no_solicitation"):
        contact_work._put(venture_id, contact_id, "lead_contact_page", "", {"outreach_restricted": True})
        lead.update(outreach_restricted=True, contact_page=None, message_draft=None)
        message = _LEAD_OUTREACH_RESTRICTED
    else:
        candidates = [(facts.get("contact_form") or {}).get("url"), facts.get("contact_page"),
            *(route.get("source_url") for route in facts.get("contacts", []))]
        readable = {reachable.reach_url(url) for url in facts.get("readable_sources", [])}
        page = next((url for candidate in candidates if (url := reachable.reach_url(candidate or ""))
            and url in readable and enrich._host(url) == enrich._host(website)), None)
        if not page:
            raise HTTPException(422, detail={"message": "No public contact page could be checked. Open Company website in Leads to check, or choose another lead."})
        contact_work._put(venture_id, contact_id, "lead_contact_page", "", {"contact_page": page, "checked_website": website})
        lead["contact_page"] = page
        message = "Your contact page is saved. Open it to see how to get in touch."
    return {**view, "receipt": {"body_md": message}}


@router.post("/ventures/{venture_id}/leads/contacts/{contact_id}/draft")
def draft_contact(venture_id: str, contact_id: str, x_subject: str | None = Header(default=None, alias="X-Subject")):
    from app.authz.workspace import subject_person_id
    from app.gtm import journey
    from app.gtm.leads import saved_leads
    from app.tenancy import bind

    workspace_id = _lead_scope(venture_id, x_subject, write=True)
    view = _with_local_lead_actions(venture_id, saved_leads(workspace_id, venture_id))
    lead = next((lead for lead in view["items"] if lead["id"] == contact_id), None)
    if not lead:
        raise HTTPException(404, detail={"message": "Refresh Leads and select a saved contact."})
    with bind(workspace_id=workspace_id, venture_id=venture_id, actor=subject_person_id(x_subject)):
        if is_local() and _addressless_web_lead(lead):
            return _draft_lead_message(workspace_id, venture_id, lead, view)
        if is_local() and lead.get("outreach_restricted"):
            raise HTTPException(409, detail={"message": _LEAD_OUTREACH_RESTRICTED})
        try:
            receipt = journey.draft_for_review(venture_id, contact_id=contact_id)
        except ValueError:
            from app.core.local_capabilities import capability, blocked_message

            if is_local() and capability("send_email")["missing"]:
                raise HTTPException(409, detail={"message": blocked_message("send_email")}) from None
            raise HTTPException(422, detail={"message": "Open this contact in Leads and check its email source and permission before drafting."}) from None
        except ProviderCallError:
            raise HTTPException(502, detail={"message": "Open Settings > Keys and connections > Your AI provider, check your saved API key and model, then select Draft email in Leads again."}) from None
        except CreditError:
            raise HTTPException(409, detail={"message": "Open Settings > Keys and connections > Spending and review your monthly spend cap, then select Draft email in Leads again."}) from None
    return {**_with_local_lead_actions(venture_id, saved_leads(workspace_id, venture_id)), "receipt": receipt}


def _draft_lead_message(workspace_id: str, venture_id: str, lead: dict, view: dict) -> dict:
    from app.gtm import leads, reachable

    website = _lead_website(lead)
    if not lead.get("contact_page"):
        raise HTTPException(409, detail={"message": "Select Find a contact in Leads to check the website's contact page before drafting a message."})
    source = reachable.reach_url(lead.get("source_url") or "") or website
    lead["message_draft"] = leads.draft_public_contact_message(workspace_id, venture_id, lead, source, checked_website=website)
    return {**view, "receipt": {"body_md": "Your message draft is saved. Review it before using it on a public contact page. Nothing was sent."}}


class LeadHistoryBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["replied", "booked"]


@router.patch("/ventures/{venture_id}/leads/contacts/{contact_id}/history")
def record_contact_history(venture_id: str, contact_id: str, body: LeadHistoryBody,
                           x_subject: str | None = Header(default=None, alias="X-Subject")):
    from app.gtm import contact_work

    workspace_id = _lead_scope(venture_id, x_subject, write=True)
    try:
        return _with_local_lead_actions(venture_id, contact_work.record_contact_history(workspace_id, venture_id, contact_id, body.status, x_subject))
    except KeyError:
        raise HTTPException(404, detail={"message": "Refresh Leads and select a saved contact."}) from None
    except ValueError as exc:
        if str(exc) != "Wait for your current email send to finish, then mark this contact again in Leads.":
            raise
        raise HTTPException(409, detail={"message": str(exc)}) from None


class FirstLookPublishBody(BaseModel):
    site_version_id: str = Field(min_length=1, max_length=80)
    public_sha256: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")


@router.get("/ventures/{venture_id}/site/publication")
def firstlook_publication_state(
    venture_id: str,
    preview: bool = Query(default=False),
    x_subject: str | None = Header(default=None, alias="X-Subject"),
):
    """Live / publishable / blocked state of the concept page; ``preview`` adds the exact HTML that would be published."""
    assert_venture_access(venture_id, x_subject)
    from app.hosting import firstlook_publish as fp

    out = fp.describe(venture_id)
    if preview and out["draft"]:
        try:
            out["preview_html"] = fp.public_page(venture_id, version_id=out["draft"]["site_version_id"])["html"]
        except fp.HostingError:
            pass
    return out


@router.post("/ventures/{venture_id}/site/publication", status_code=202)
def firstlook_publish(
    venture_id: str,
    body: FirstLookPublishBody,
    x_subject: str | None = Header(default=None, alias="X-Subject"),
):
    """Founder-confirmed publication of the exact reviewed page (bound by its sha256)."""
    from app.authz.workspace import subject_person_id
    from app.hosting import firstlook_publish as fp

    assert_venture_access(venture_id, x_subject)
    try:
        return fp.queue_publication(venture_id, actor=subject_person_id(x_subject),
                                    site_version_id=body.site_version_id, public_sha256=body.public_sha256)
    except fp.HostingError as exc:
        raise HTTPException(exc.status, detail={"code": exc.code, "message": fp.plain(exc.code)}) from exc


_ADDRESS = Limiter(capacity=60, per_seconds=60.0)


class SiteAddressBody(BaseModel):
    slug: str = Field(min_length=1, max_length=120)
    target: str = Field(default="firstlook", pattern="^(firstlook|pagebuilder)$")


@router.get("/ventures/{venture_id}/site-address/check")
def site_address_check(
    venture_id: str,
    slug: str = Query(default="", max_length=120),
    target: str = Query(default="firstlook", pattern="^(firstlook|pagebuilder)$"),
    x_subject: str | None = Header(default=None, alias="X-Subject"),
):
    """Is <slug>.supymem.com free for this venture? Light, rate-limited, read-only."""
    from app.hosting import site_address

    assert_venture_access(venture_id, x_subject)
    limit = _ADDRESS.check(f"address:{venture_id}:{x_subject or 'local'}")
    if not limit.allowed:
        raise HTTPException(429, detail={"code": "rate_limited", "message": "Slow down a little and try again."},
                            headers={"Retry-After": str(limit.retry_after_s)})
    return site_address.check(venture_id, slug, target)


@router.put("/ventures/{venture_id}/site-address")
def site_address_set(
    venture_id: str,
    body: SiteAddressBody,
    x_subject: str | None = Header(default=None, alias="X-Subject"),
):
    """Choose the page address. Re-validated here; the unique constraint settles races (409 + a free suggestion)."""
    from app.authz.workspace import subject_person_id
    from app.hosting import firstlook_publish as fp
    from app.hosting import site_address

    _, workspace_id = assert_venture_access(venture_id, x_subject)
    try:
        return site_address.set_address(venture_id, workspace_id, subject_person_id(x_subject), body.slug, body.target)
    except fp.HostingError as exc:
        reason = exc.code.removeprefix("site_address_")
        detail = {"code": exc.code, "message": site_address.REASONS.get(reason) or fp.plain(exc.code)}
        if getattr(exc, "suggestion", None):
            detail["suggestion"] = exc.suggestion
            detail["reason"] = reason
        raise HTTPException(exc.status, detail=detail) from exc
