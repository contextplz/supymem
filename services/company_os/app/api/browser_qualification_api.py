"""Signed current-device trial status; creation/approval stays controller-owned."""
import json
from urllib.parse import parse_qsl

from fastapi import APIRouter, HTTPException, Request, Response
from starlette.background import BackgroundTask

from app.admin.authn import verify_caller
from app.build import browser_qualification as service
from core.bounded_body import BodyRejected, events_body
from core.canonical import sha256_hex

router = APIRouter(tags=["browser-qualification"])


def invoke(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except KeyError as exc:
        raise HTTPException(404, detail={"code": "browser_qualification_not_found"}) from exc
    except PermissionError as exc:
        raise HTTPException(403, detail={"code": "browser_qualification_access_denied"}) from exc
    except ValueError as exc:
        code = str(exc) if str(exc).startswith("browser_qualification_") and len(str(exc)) < 100 else service.INVALID
        raise HTTPException(409, detail={"code": code}) from exc


@router.get("/ventures/{venture_id}/browser-qualification/{trial_id}")
def status(venture_id: str, trial_id: str, request: Request):
    return invoke(service.status, venture_id, verify_caller(request, required=True), trial_id)


@router.get("/ventures/{venture_id}/browser-qualification/{trial_id}/requests/{request_id}")
def request_status(venture_id: str, trial_id: str, request_id: str, request: Request):
    return invoke(service.request_status, venture_id, verify_caller(request, required=True), trial_id, request_id)


@router.get("/ventures/{venture_id}/browser-qualification/{trial_id}/orders/{order_id}")
def order_status(venture_id: str, trial_id: str, order_id: str, request: Request):
    return invoke(service.order_status, venture_id, verify_caller(request, required=True), trial_id, order_id)


@router.get("/public/browser-qualification/{nonce}/{fixture_path:path}", operation_id="browser_qualification_fixture_get")
@router.head("/public/browser-qualification/{nonce}/{fixture_path:path}", operation_id="browser_qualification_fixture_head")
@router.post("/public/browser-qualification/{nonce}/{fixture_path:path}", operation_id="browser_qualification_fixture_post")
async def fixture(nonce: str, fixture_path: str, request: Request):
    try:
        raw = await events_body(request, maximum=8192, timeout=10)
    except BodyRejected as exc:
        raise HTTPException(exc.status, detail={"code": "browser_qualification_body_invalid"}) from exc
    pairs = []
    body_kind = None
    if request.method == "POST":
        try:
            media_type = request.headers.get("content-type", "").split(";", 1)[0]
            if media_type == "application/x-www-form-urlencoded":
                body_kind = "form"
                pairs = parse_qsl(raw.decode("utf-8"), keep_blank_values=True, strict_parsing=True)
            elif media_type == "application/json":
                body_kind = "json"
                def unique(items):
                    if len({key for key, _ in items}) != len(items):
                        raise ValueError(service.INVALID)
                    return dict(items)
                value = json.loads(raw, object_pairs_hook=unique, parse_constant=lambda _: (_ for _ in ()).throw(ValueError(service.INVALID)))
                if not isinstance(value, dict):
                    raise ValueError(service.INVALID)
                pairs = list(value.items())
            else:
                raise ValueError(service.INVALID)
            if len({name for name, _ in pairs}) != len(pairs):
                raise ValueError(service.INVALID)
        except (UnicodeError, ValueError, RecursionError):
            raise HTTPException(400, detail={"code": service.INVALID})
    elif raw:
        raise HTTPException(400, detail={"code": service.INVALID})
    result, request_id = invoke(service.receive_fixture_request, nonce, path=request.url.path, query=request.url.query,
        method=request.method, submitted_fields=pairs, body_kind=body_kind, raw_body_digest=sha256_hex(raw),
        request_id=request.headers.get("x-supymem-qualification-request"),
        invocation_id=request.headers.get("x-supymem-qualification-invocation"),
        effect_digest=request.headers.get("x-supymem-qualification-effect"),
        execution_id=request.headers.get("x-supymem-qualification-execution"))
    return Response(content=b"" if request.method == "HEAD" else result.body, status_code=result.status,
        media_type=result.media_type, headers=dict(result.headers) | {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
            "X-Supymem-Qualification-Request": request_id},
        background=BackgroundTask(service.record_response_delivery, nonce, request_id))
