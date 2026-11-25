"""Founder-scoped asset import previews and explicit commits."""
from __future__ import annotations

import asyncio
import json
from email import policy
from email.parser import BytesParser
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.authz.workspace import assert_venture_access
from app.gtm.god.generation import authority
from app.intake import contacts
from core.bounded_body import BodyRejected, events_body

router = APIRouter(tags=["venture assets"])


def read_scope(venture_id: str, x_subject: str = Header(alias="X-Subject")):
    _, wid = assert_venture_access(venture_id, x_subject)
    call(authority, wid, venture_id, actor=x_subject)
    return wid, x_subject


def scope(venture_id: str, bound=Depends(read_scope)):
    call(authority, bound[0], venture_id, write=True, actor=bound[1])
    return bound


def call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except contacts.ContactImportError as exc:
        raise HTTPException(exc.status, detail={"error": {"message": exc.message}}) from None
    except ValueError:
        raise HTTPException(403, detail={"error": {"message": "This action is unavailable for this venture."}}) from None


class PreviewBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=contacts.MAX_BYTES)
    format: Literal["csv", "paste"] = "csv"
    mapping: dict[str, str] | None = None


class CommitBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    preview_id: str = Field(min_length=1, max_length=100)
    lawful_basis_attested: bool = False
    lawful_basis: Literal["consent", "legitimate_interest"] = "legitimate_interest"
    lawful_basis_note: str = Field(default="", max_length=1000)


@router.get("/ventures/{venture_id}/gtm/contacts")
@router.get("/api/v1/ventures/{venture_id}/gtm/contacts", include_in_schema=False)
def contacts_list(venture_id: str, bound=Depends(read_scope)):
    from app.gtm.leads import saved_leads

    return saved_leads(bound[0], venture_id)


async def preview_body(request: Request) -> PreviewBody:
    try:
        raw = await events_body(request, maximum=contacts.MAX_BYTES + 64 * 1024)
        media = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if media == "application/json":
            data = json.loads(raw)
        elif media == "multipart/form-data":
            header = "Content-Type: " + request.headers.get("content-type", "") + "\r\nMIME-Version: 1.0\r\n\r\n"
            message = BytesParser(policy=policy.default).parsebytes(header.encode() + raw)
            if not message.is_multipart():
                raise contacts.ContactImportError("Choose a CSV file to preview.")
            data, files = {"format": "csv"}, 0
            for part in message.iter_parts():
                name = part.get_param("name", header="content-disposition")
                if name == "file":
                    files += 1
                    filename = part.get_filename() or ""
                    if files > 1 or not filename.lower().endswith(".csv"):
                        raise contacts.ContactImportError("Choose one CSV file to preview.")
                    data["text"] = part.get_payload(decode=True).decode("utf-8-sig")
                elif name in {"text", "format", "mapping"}:
                    value = part.get_payload(decode=True).decode("utf-8-sig")
                    data[name] = json.loads(value) if name == "mapping" else value
                else:
                    raise contacts.ContactImportError("Choose a CSV file or paste contacts.")
        else:
            raise contacts.ContactImportError("Choose a CSV file or paste contacts.", 415)
        return PreviewBody.model_validate(data)
    except BodyRejected as exc:
        raise HTTPException(exc.status, detail={"error": {"message": "Keep the import under 2 MB and try again."}}) from None
    except (UnicodeError, ValueError, ValidationError) as exc:
        message = exc.message if isinstance(exc, contacts.ContactImportError) else "Check the import format and try again."
        raise HTTPException(getattr(exc, "status", 422), detail={"error": {"message": message}}) from None


@router.post("/ventures/{venture_id}/gtm/contacts/import/preview")
@router.post("/api/v1/ventures/{venture_id}/gtm/contacts/import/preview", include_in_schema=False)
async def contacts_preview(venture_id: str, request: Request, bound=Depends(scope)):
    body = await preview_body(request)
    try:
        return await contacts.preview(bound[0], venture_id, body.text, format=body.format, mapping=body.mapping, actor=bound[1])
    except contacts.ContactImportError as exc:
        raise HTTPException(exc.status, detail={"error": {"message": exc.message}}) from None
    except ValueError:
        raise HTTPException(403, detail={"error": {"message": "This action is unavailable for this venture."}}) from None


@router.post("/ventures/{venture_id}/gtm/contacts/import")
@router.post("/api/v1/ventures/{venture_id}/gtm/contacts/import", include_in_schema=False)
def contacts_commit(venture_id: str, body: CommitBody, bound=Depends(scope)):
    return call(contacts.commit, bound[0], venture_id, body.preview_id, actor=bound[1],
        lawful_basis_attested=body.lawful_basis_attested, lawful_basis=body.lawful_basis, lawful_basis_note=body.lawful_basis_note)


class HistoryBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["new", "contacted", "replied", "interested"]
    last_contacted: str | None = Field(default=None, max_length=64)


@router.patch("/ventures/{venture_id}/gtm/contacts/{contact_id}/history")
@router.patch("/api/v1/ventures/{venture_id}/gtm/contacts/{contact_id}/history", include_in_schema=False)
def contacts_history(venture_id: str, contact_id: str, body: HistoryBody, bound=Depends(scope)):
    return call(contacts.update_history, bound[0], venture_id, contact_id, actor=bound[1], status=body.status, last_contacted=body.last_contacted)


@router.post("/ventures/{venture_id}/gtm/contacts/{contact_id}/verify-email")
@router.post("/api/v1/ventures/{venture_id}/gtm/contacts/{contact_id}/verify-email", include_in_schema=False)
async def contacts_verify_email(venture_id: str, contact_id: str, bound=Depends(scope)):
    try:
        return await contacts.verify_email(bound[0], venture_id, contact_id, actor=bound[1])
    except contacts.ContactImportError as exc:
        raise HTTPException(exc.status, detail={"error": {"message": exc.message}}) from None
    except ValueError:
        raise HTTPException(403, detail={"error": {"message": "This action is unavailable for this venture."}}) from None


class SiteBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(min_length=8, max_length=2048)


@router.post("/ventures/{venture_id}/assets/site")
async def site_import(venture_id: str, body: SiteBody, bound=Depends(scope)):
    from app.intake.assets import import_site
    try:
        return await asyncio.to_thread(import_site, venture_id, body.url)
    except ValueError:
        raise HTTPException(422, detail={"error": {"message": "Use a public website address starting with https://."}}) from None


@router.get("/ventures/{venture_id}/assets/site")
def site_get(venture_id: str, bound=Depends(read_scope)):
    from app.intake.assets import get_site
    return get_site(venture_id)
