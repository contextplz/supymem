"""Email preferences (signed-in), one-click unsubscribe and provider webhooks (public, signature-checked)."""
from __future__ import annotations

import json
from html import escape
from urllib.parse import parse_qs, urlencode

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, ConfigDict

from app.authz.workspace import subject_person_id
from app.email import service, templates

router = APIRouter(tags=["email"])
hosted_router = APIRouter(tags=["email"])


class PrefsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    product_updates: bool | None = None
    engagement_reminders: bool | None = None
    digests: bool | None = None
    suggestions: bool | None = None


@router.get("/email/preferences")
def get_preferences(x_subject: str | None = Header(default=None, alias="X-Subject")):
    return {"preferences": service.get_prefs(subject_person_id(x_subject)), "always_on": ["transactional"]}


@router.put("/email/preferences")
def put_preferences(body: PrefsBody, x_subject: str | None = Header(default=None, alias="X-Subject")):
    return {"preferences": service.set_prefs(subject_person_id(x_subject), body.model_dump(exclude_none=True)),
            "always_on": ["transactional"]}


def _page(message: str, form: str = "") -> HTMLResponse:
    return HTMLResponse(
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        "<meta name=color-scheme content='light dark'><title>Supymem email settings</title>"
        "<style>body{margin:0;padding:48px 20px;background:#f4f1f7;color:#2a2530;font-family:system-ui}"
        "main{max-width:460px;margin:auto;padding:32px;background:#fff;border-radius:20px}"
        ".brand{color:#6f36a5;font-weight:800;font-size:22px}h1{font-size:26px;line-height:1.25}"
        "p,label{line-height:1.6}label{display:flex;gap:12px;margin:20px 0}input{margin-top:6px;accent-color:#6f36a5}"
        "small{display:block;color:#6e6874}button{background:#6f36a5;color:#fff;border:0;border-radius:24px;"
        "padding:14px 24px;font:600 16px system-ui;cursor:pointer}a{color:#6f36a5}"
        "@media(prefers-color-scheme:dark){body{background:#121016;color:#f1edf6}main{background:#1c1922}"
        ".brand,a{color:#c39ae8}small{color:#b5adbf}}@media(max-width:480px){main{padding:24px}}</style>"
        f"</head><body><main><div class=brand>supymem.</div><h1>{escape(message)}</h1>{form}</main></body></html>",
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff",
                 "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'"})


_LABELS = {
    "product_updates": ("Progress updates", "Research, pages and builds that are ready to review."),
    "engagement_reminders": ("Gentle reminders", "Help with your next step when you have been away."),
    "digests": ("Daily and weekly digests", "Opt in to a recap when there is progress to share."),
    "suggestions": ("Suggestions", "Ideas waiting for your yes or no."),
}


def _preferences_page(person_id: str, token: str, *, saved: bool = False) -> HTMLResponse:
    prefs = service.get_prefs(person_id)
    action = "/public/email/unsubscribe?" + urlencode({"t": token, "view": "preferences"})
    controls = "".join(
        f'<label><input type="checkbox" name="{cat}" value="on"{" checked" if prefs[cat] else ""}>'
        f'<span><strong>{escape(_LABELS[cat][0])}</strong><small>{escape(_LABELS[cat][1])}</small></span></label>'
        for cat in templates.CATEGORIES)
    return _page("Your email settings", ("<p role=status>Your preferences are saved.</p>" if saved else "")
                 + "<p>Choose what Charlie sends. Essential account and approval emails remain enabled.</p>"
                 + f'<form method="post" action="{escape(action)}">{controls}<button type="submit">Save preferences</button></form>')


@router.get("/public/email/unsubscribe")
def unsubscribe_page(t: str = "", view: str = ""):
    # GET never changes anything: mail scanners prefetch links. The button (or a mail client's one-click POST) does.
    parsed = service.read_token(t)
    if parsed is None or view not in {"", "preferences"}:
        return _page("This unsubscribe link is not valid.")
    if view == "preferences":
        return _preferences_page(parsed[0], t) if parsed[1] == "all" else _page("This preferences link is not valid.")
    return _page("Unsubscribe from these Supymem emails?", "<form method=post><button style='padding:10px 18px'>Unsubscribe</button></form>"
                 f"<p><a href='{escape(service.preferences_url(parsed[0]))}'>Choose what you get in settings</a></p>")


@router.post("/public/email/unsubscribe")
async def unsubscribe_post(request: Request, t: str = "", view: str = ""):
    parsed = service.read_token(t)
    if view == "preferences":
        if parsed is None or parsed[1] != "all":
            return _page("This preferences link is not valid.")
        raw = await request.body()
        if len(raw) > 4096 or request.headers.get("content-type", "").split(";", 1)[0] != "application/x-www-form-urlencoded":
            raise HTTPException(400, detail="invalid_preferences_form")
        try:
            changes = parse_qs(raw.decode("utf-8"), keep_blank_values=True, max_num_fields=len(templates.CATEGORIES))
        except (ValueError, UnicodeDecodeError):
            raise HTTPException(400, detail="invalid_preferences_form") from None
        if any(cat not in templates.CATEGORIES or value != ["on"] for cat, value in changes.items()):
            raise HTTPException(400, detail="invalid_preferences_form")
        service.set_prefs(parsed[0], {cat: cat in changes for cat in templates.CATEGORIES})
        return _preferences_page(parsed[0], t, saved=True)
    if view:
        return _page("This unsubscribe link is not valid.")
    if not service.unsubscribe(t):
        return _page("This unsubscribe link is not valid.")
    return _page("You're unsubscribed. Important account emails may still arrive.",
                 f"<p><a href='{escape(service.preferences_url(parsed[0]))}'>Manage email preferences</a></p>")


@hosted_router.post("/webhooks/email/resend")
async def resend_webhook(request: Request):
    from app.email import webhooks

    raw = await request.body()
    try:
        webhooks.verify_resend(raw, request.headers)
    except webhooks.BadSignature:
        return JSONResponse({}, status_code=401)
    try:
        return {"status": webhooks.handle_resend(raw, event_id=request.headers["svix-id"])}
    except webhooks.UnmatchedMessage:
        # The provider may beat the send response's commit. Retry the callback,
        # never invent a delivery receipt or suppress an unbound address.
        request.state.email_callback_unmatched = True
        return JSONResponse({"status": "unmatched"}, status_code=503)
    except (ValueError, KeyError):
        return JSONResponse({"status": "ignored"}, status_code=200)


@hosted_router.post("/webhooks/email/ses")
async def ses_webhook(request: Request):
    from app.email import webhooks

    raw = await request.body()  # SNS posts JSON as text/plain
    try:
        body = json.loads(raw)
        if not isinstance(body, dict):
            raise ValueError
    except ValueError:
        raise HTTPException(400, detail="bad_body") from None
    try:
        webhooks.verify_sns(body)
    except webhooks.BadSignature:
        return JSONResponse({}, status_code=401)
    try:
        return {"status": webhooks.handle_ses(body)}
    except (ValueError, KeyError):
        return {"status": "ignored"}
