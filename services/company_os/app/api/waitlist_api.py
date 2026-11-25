"""Managed public signup and authenticated founder controls."""
from __future__ import annotations

import hashlib
from html import escape
import json
import re
from urllib.parse import parse_qs, urlencode

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, ConfigDict
from core.bounded_body import BodyRejected, waitlist_body
from app.authz.workspace import assert_venture_access, subject_person_id
from app.conversion import waitlist
from app.core.local_mode import is_local
from app.api.firstlook_api import _interest_html_requested

router = APIRouter(tags=["venture-waitlist"])
_BASE_HEADERS = {"Cache-Control": "no-store", "Vary": "Accept", "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY"}
STYLE = "body{margin:0;background:#f1f5fb;color:#142238;font:18px/1.6 system-ui}main{max-width:580px;margin:8vh auto;padding:32px;background:white;border:1px solid #ccd8eb;border-radius:20px}h1{font-size:2rem;line-height:1.2}input[type=email]{box-sizing:border-box;width:100%;padding:14px;border:1px solid #8a9cba;border-radius:8px;font:inherit}button{font:inherit;padding:12px 22px;background:#194ba5;color:white;border:0;border-radius:8px}label{display:block;margin:18px 0}input[type=checkbox]{width:20px;height:20px}a{color:#194ba5}.trap{position:absolute;left:-10000px}small{color:#52627c} @media(max-width:640px){main{margin:20px 12px;padding:24px}}"


def _page_headers(*, allow_beacon: bool = False) -> dict[str, str]:
    headers = dict(_BASE_HEADERS)
    if allow_beacon:
        from app.product_analytics.beacon import public_events_url
        events = public_events_url() or "'none'"
        headers["Content-Security-Policy"] = (
            "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
            f"connect-src {events}; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
        )
    else:
        headers["Content-Security-Policy"] = (
            "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
            "base-uri 'none'; frame-ancestors 'none'"
        )
    return headers


HEADERS = _page_headers(allow_beacon=False)


def page(title, content, status=200, *, body_html: str | None = None, allow_beacon: bool = False):
    body = body_html if body_html is not None else f"<main><h1>{escape(title)}</h1>{content}</main>"
    return HTMLResponse(
        f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<title>{escape(title)}</title><style>{STYLE}</style></head><body>{body}</body></html>',
        status_code=status,
        headers=_page_headers(allow_beacon=allow_beacon),
    )


def _waitlist_body_with_beacon(venture_id: str, title: str, content: str) -> tuple[str, bool]:
    """Return (body_inner_html, beacon_enabled). Beacon is the sole landing_view/cta source."""
    main = f"<main><h1>{escape(title)}</h1>{content}</main>"
    try:
        from app.product_analytics.beacon import script_tag

        key = waitlist.beacon_key(venture_id)
        if key is None:
            return main, False
        tag = script_tag(key=key)
        return main + tag, bool(tag)
    except Exception:
        return main, False


class ConfigureBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    enabled: bool


def error(exc):
    from app.core.local_mode import is_local

    if is_local():
        raise HTTPException(exc.status, detail="Open Website > Collect signups, turn on your waitlist, then retry. Check your destination in Settings > Keys and connections > Publishing.") from None
    raise HTTPException(exc.status, detail={"code": exc.code}) from None


@router.get("/ventures/{venture_id}/waitlist")
def founder_waitlist(venture_id: str, x_subject: str | None = Header(default=None, alias="X-Subject")):
    _, workspace = assert_venture_access(venture_id, x_subject)
    try:
        return waitlist.status(venture_id, workspace, include_leads=True)
    except waitlist.WaitlistError as exc:
        error(exc)


@router.put("/ventures/{venture_id}/waitlist")
def configure_waitlist(venture_id: str, body: ConfigureBody, x_subject: str | None = Header(default=None, alias="X-Subject")):
    _, workspace = assert_venture_access(venture_id, x_subject)
    try:
        out = waitlist.configure(venture_id, workspace, enabled=body.enabled, actor=subject_person_id(x_subject))
        if body.enabled:
            from app.chat.milestone_hooks import waitlist_enabled

            waitlist_enabled(venture_id)
        return out
    except waitlist.WaitlistError as exc:
        error(exc)


@router.post("/ventures/{venture_id}/waitlist/test")
def test_waitlist(venture_id: str, x_subject: str | None = Header(default=None, alias="X-Subject")):
    _, workspace = assert_venture_access(venture_id, x_subject)
    try:
        return waitlist.probe(venture_id, workspace)
    except waitlist.WaitlistError as exc:
        error(exc)


@router.get("/public/waitlists/{venture_id}")
def public_waitlist(venture_id: str, variant: str = "", utm_campaign: str = "", experiment_id: str = "", kind: str = "", return_token: str = "",
                    utm_source: str = "", utm_medium: str = "", ref: str = "",
                    accept: str = Header(default="", alias="Accept")):
    if kind == "request":
        from app.experiments import wizard_tests
        try:
            if return_token:
                return RedirectResponse(wizard_tests.return_visit(venture_id, experiment_id, return_token), status_code=303, headers=HEADERS)
            title, content = wizard_tests.public_form(venture_id, experiment_id)
            if is_local():
                return RedirectResponse(content, status_code=303, headers=HEADERS)
            return page(title, content)
        except ValueError:
            return page("Request form unavailable", "<p>Ask Charlie to build and publish your request form from Website after connecting your tool in Settings &gt; Keys and connections &gt; Coding tools.</p>" if is_local() else "<p>This request form is not open.</p>", 404)
    try:
        details = waitlist.public_details(venture_id)
    except waitlist.WaitlistError:
        return page("Waitlist unavailable", "<p>Open Website &gt; Collect signups, turn on your waitlist, then ask Charlie to build it after connecting your tool in Settings &gt; Keys and connections &gt; Coding tools.</p>" if is_local() else "<p>This venture is not accepting early-access requests here.</p>", 404)
    if is_local():
        from app.pagebuilder.local_publish import WAITLIST_READINESS

        if accept == WAITLIST_READINESS:
            digest = hashlib.sha256(details["consent"].encode()).hexdigest()
            return Response(status_code=204, headers={**_BASE_HEADERS, "X-Waitlist-Consent": digest})
    title = f"Join {details['name']}"
    attribution = {key: value for key, value in {"variant": variant, "utm_campaign": utm_campaign,
        "utm_source": utm_source, "utm_medium": utm_medium, "experiment_id": experiment_id, "ref": ref}.items() if value}
    if any(not re.fullmatch(r"[A-Za-z0-9_-]{1,80}" if key == "ref" else r"[A-Za-z0-9_.-]{1,80}", value)
            for key, value in attribution.items()):
        return page("Invalid link", "<p>Check your waitlist link.</p>", 422)
    if is_local():
        return _checked_page_redirect(venture_id, experiment_id, "waitlist.html", attribution)
    content = (
        '<p>Leave your email to request early-access updates from this venture.</p>'
        f'<form method="post" action="{escape(details["url"], quote=True)}">'
        '<label>Email address<input name="email" type="email" autocomplete="email" maxlength="254" required></label>'
        f'<input type="hidden" name="consent_version" value="{waitlist.VERSION}">'
        f'<input type="hidden" name="consent_digest" value="{hashlib.sha256(details["consent"].encode()).hexdigest()}">'
        + "".join(f'<input type="hidden" name="{key}" value="{escape(value, quote=True)}">' for key, value in attribution.items()) +
        '<label class="trap" aria-hidden="true">Website<input name="website" tabindex="-1" autocomplete="off"></label>'
        f'<label><input type="checkbox" name="consent" value="yes" required> {escape(details["consent"])}</label>'
        '<button type="submit">Request early access</button></form>'
        '<p><small>Powered by Supymem. Anonymous page views (no identifiers; honors GPC/DNT) may be recorded for the founder. Your email is stored only after you consent.</small></p>'
    )
    body, beacon = _waitlist_body_with_beacon(venture_id, title, content)
    return page(title, content, body_html=body, allow_beacon=beacon)


@router.post("/public/waitlists/{venture_id}")
async def submit_waitlist(venture_id: str, request: Request):
    wants_html = _interest_html_requested(request)
    try:
        raw = await waitlist_body(request)
        content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
        if content_type == "application/json":
            payload = json.loads(raw)
        elif content_type == "application/x-www-form-urlencoded":
            fields = parse_qs(raw.decode("utf-8"), keep_blank_values=True, max_num_fields=11)
            if any(len(values) != 1 for values in fields.values()):
                raise ValueError("duplicate_fields")
            payload = {key: values[0] for key, values in fields.items()}
        else:
            raise waitlist.WaitlistError("waitlist_content_type", 415)
        if isinstance(payload, dict) and payload.get("kind") == "request":
            from app.experiments import wizard_tests
            result = wizard_tests.capture(venture_id, payload)
        else:
            result = waitlist.capture(venture_id, payload)
    except (waitlist.WaitlistError, BodyRejected) as exc:
        if wants_html:
            return page("Submission not recorded", "<p>Check your email and consent, and make sure this waitlist is still open.</p>", exc.status)
        return JSONResponse({"code": exc.code}, status_code=exc.status, headers=HEADERS)
    except (ValueError, UnicodeDecodeError):
        return page("Invalid submission", "<p>Go back and check the form.</p>", 422) if wants_html else JSONResponse({"code": "waitlist_input_invalid"}, status_code=422, headers=HEADERS)
    except Exception:
        # An interrupted response can follow a committed write; don't promise absence.
        return page("Submission not confirmed", "<p>We could not confirm the result. Your request may have been saved. Please avoid an immediate repeat.</p>", 503) if wants_html else JSONResponse({"code": "waitlist_submission_unconfirmed"}, status_code=503, headers=HEADERS)
    # Consent already enforced inside waitlist.capture before this line runs.
    # A newly stored non-test request and its app signup event commit together.
    # Replays and QA probes get the same public response without a new event.
    if wants_html and result.get("return_url"):
        return page("Request recorded", '<p>Your request is saved. A person reviews every request during early access.</p>'
            f'<p>Save this link to come back tomorrow: <a href="{escape(result["return_url"], quote=True)}">Return to the page</a>.</p>', 201)
    return page("Request recorded", "<p>Your request is saved. The founder can contact you about early access.</p>", 201) if wants_html else JSONResponse(result, status_code=201, headers=HEADERS)


@router.get("/public/waitlists/{venture_id}/pricing/{experiment_id}/{price_key}")
def pricing_disclosure(venture_id: str, experiment_id: str, price_key: str):
    from app.experiments import page_tests
    try:
        price = page_tests.public_price(venture_id, experiment_id, price_key)
        target = waitlist.public_url(venture_id) + "?" + urlencode({"experiment_id": experiment_id})
    except (ValueError, waitlist.WaitlistError):
        return page("Pricing unavailable", "<p>Ask Charlie to build your saved pricing test after connecting your tool in Settings &gt; Keys and connections &gt; Coding tools, then publish its checked pages from Website and retry this link.</p>" if is_local() else "<p>This proposed price is not open for interest.</p>", 404)
    if is_local():
        return _checked_page_redirect(venture_id, experiment_id, f"price_{price_key}.html")
    return page("Proposed pricing", f'<p>{escape(price["label"])}: {escape(price["price"])}</p>'
        f'<p role="status">{page_tests.DISCLOSURE}</p><p><a href="{escape(target, quote=True)}">Join the waitlist</a></p>')


@router.get("/public/waitlists/{venture_id}/surveys/{experiment_id}/{token}")
def public_survey(venture_id: str, experiment_id: str, token: str):
    from app.experiments import page_tests
    try:
        details = page_tests.survey(venture_id, experiment_id, token)
    except ValueError:
        return page("Survey unavailable", "<p>Ask Charlie to build and publish your survey from Website after connecting your tool in Settings &gt; Keys and connections &gt; Coding tools, then request a current survey invitation.</p>" if is_local() else "<p>This survey link is not available.</p>", 404)
    if is_local():
        return _checked_page_redirect(venture_id, experiment_id, "survey.html", {"experiment_id": experiment_id, "token": token})
    choices = "".join(f'<label><input type="radio" name="choice" value="{key}" required> {escape(label)}</label>'
        for key, label in details["choices"].items())
    content = f'<form method="post"><fieldset><legend>{escape(details["question"])}</legend>{choices}</fieldset>'
    content += '<label>Main benefit (optional)<textarea name="benefit" maxlength="500"></textarea></label><button type="submit">Send your response</button></form>'
    return page(details["name"], content)


def _checked_page_redirect(venture_id, experiment_id, filename, attribution=None):
    from app.experiments import page_tests
    try:
        return RedirectResponse(page_tests.checked_public_url(venture_id, experiment_id, filename, attribution),
            status_code=303, headers=HEADERS)
    except ValueError:
        return page("Page not published", "<p>Ask Charlie to build and publish your checked page from Website after connecting your tool in Settings &gt; Keys and connections &gt; Coding tools.</p>", 404)


@router.post("/public/waitlists/{venture_id}/surveys/{experiment_id}/{token}")
async def submit_survey(venture_id: str, experiment_id: str, token: str, request: Request):
    from app.experiments import page_tests
    try:
        raw = await waitlist_body(request)
        content_type = request.headers.get("content-type", "").split(";")[0]
        if content_type == "application/json":
            payload = json.loads(raw)
        elif content_type == "application/x-www-form-urlencoded":
            fields = parse_qs(raw.decode(), keep_blank_values=True, max_num_fields=2)
            if any(len(values) != 1 for values in fields.values()):
                raise ValueError("duplicate_fields")
            payload = {key: values[0] for key, values in fields.items()}
        else:
            return page("Invalid response", "<p>Use the survey form.</p>", 415)
        page_tests.survey(venture_id, experiment_id, token, payload)
    except (ValueError, UnicodeError):
        return page("Response not recorded", "<p>Check your choice and survey link.</p>", 422)
    except BodyRejected as exc:
        return page("Response not recorded", "<p>Your response is too large.</p>", exc.status)
    return page("Response recorded", "<p>Thank you for your feedback.</p>", 201)
