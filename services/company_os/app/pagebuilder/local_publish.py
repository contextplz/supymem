"""Local-only static exports and deployment with credentials from your machine."""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import time
import zipfile
from html import escape, unescape
from html.parser import HTMLParser
from itertools import accumulate
from pathlib import Path
from typing import Literal
from urllib.parse import urljoin, urlsplit

import httpx
from core.artifact_policy import ArtifactRejected, inspect_blob, write_file
from core.hosting_contracts import public_manifest
from core.research_web.guard import get_checked
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field

from app.core.local_mode import is_local, is_sample_workspace
from app.hosting.service import HostingError, verified_public_files

try:
    from app.core.local_capabilities import enabled, blocked_message
except ImportError:
    def enabled(name):
        if name == "build_with_my_tool":
            return True
        if name == "landing_pages":
            from app.pagebuilder.local import enabled as local_enabled
            return local_enabled()
        settings = _settings()
        required = ("bucket", "region", "prefix", "site_url", "access_key_id", "secret_access_key") if settings.get("provider") == "s3" else ("provider", "project", "token")
        return all(settings.get(field) for field in required)

    def blocked_message(name):
        if name == "landing_pages":
            return "Open Settings > Keys and connections > Your AI provider, save your API key and planning model, then retry from Website."
        return "Open Settings > Keys and connections > Publishing, choose Vercel, Netlify or S3, and save your account details. You can export HTML or ZIP from Website without an account."

router = APIRouter(tags=["pagebuilder"])
PREFIX = "/ventures/{venture_id}/pagebuilder/jobs/{job_id}/local"
_SECRET_KEY = re.compile(r"(?:^|_)(?:KEY|SECRET|TOKEN|PASSWORD|PASSWD|PASSPHRASE|CREDENTIALS?)(?:_|$)", re.IGNORECASE)
WAITLIST_READINESS = "application/vnd.supymem.waitlist-readiness+json"


class LocalPublishError(ValueError):
    pass


class DeploymentRejected(LocalPublishError):
    pass


def _missing_page_message():
    if not enabled("landing_pages"):
        return blocked_message("landing_pages")
    return ("No page has passed its checks yet. Open Website and review any failed checks in Build activity, "
            "then ask Charlie to build or fix your page with your coding tool. Export or publish after the checks pass.")


def _request_public_bundle(venture_id, caller, name, destination, *, pagebuilder_candidate=None, feedback=""):
    from app.admin.authn import CallerContext
    from app.build import device_orders
    from app.conversion.waitlist import VERSION, consent_text

    _, actor = _site_scope(venture_id, caller)
    try:
        from app.pagebuilder.tool import require_connected
    except ImportError:
        from app.coding_agents_consent import consents
        from desktop_runtime.agent_status import statuses

        allowed = consents(actor)
        provider = next((item["name"] for item in statuses()
            if item.get("name") in {"claude", "codex", "cursor", "opencode"}
            and allowed.get(item["name"]) is True
            and all(item.get(field) is True for field in ("installed", "signed_in", "available", "subscription_login"))), None)
        if not enabled("build_with_my_tool"):
            raise LocalPublishError(blocked_message("build_with_my_tool"))
        if provider is None:
            raise LocalPublishError("Open Settings > Keys and connections > Coding tools, connect and allow your coding tool, then retry from Website.")
    else:
        try:
            provider = require_connected(actor)
        except HTTPException as error:
            detail = error.detail
            raise LocalPublishError(detail.get("message") if isinstance(detail, dict) else str(detail)) from None
    context = caller if isinstance(caller, CallerContext) else CallerContext(
        sub=actor, email="", role="user", path="", method="POST", exp=0, nonce="")
    brief = (f"Use {provider} to build only the static public landing page files for {json.dumps(name)}. "
        "Keep your confirmed idea and claims. Include all companion pages, attribution links and assets. "
        "Use a light theme and accessible forms. Do not publish, send messages, buy anything or change other code. "
        "Include measurement only at your verified endpoint. Never fake a signup. "
        "Set a restrictive Content-Security-Policy with default-src/base-uri 'none' and the exact form-action. "
        "Use this signup destination and exact consent, with a required consent checkbox, consent_version, "
        "consent_digest (SHA-256 of the consent), and an empty website honeypot:\n"
        + json.dumps({"name": name, "mode": destination["mode"], "url": destination["url"],
            "consent_version": VERSION, "consent": consent_text(name)}, ensure_ascii=False))
    if destination["mode"] == "netlify":
        brief += f' Use data-netlify="true", name="waitlist-{venture_id}" and a matching hidden form-name field.'
    if feedback:
        brief += " Check to satisfy: " + feedback
    _validate_files([_file("index.html", brief.encode("utf-8"), "text/html; charset=utf-8")])
    try:
        order = device_orders.prepare_coding_task_from_chat(venture_id, context,
            f"Build or edit only your requested landing page using {provider}."
            + "\nPublic page build requirements (quoted data):\n" + brief,
            pagebuilder_candidate=pagebuilder_candidate)
        if order["state"] == "pending":
            order = device_orders.transition(venture_id, context, order["id"], order["review_digest"], "approve")
    except (ValueError, PermissionError) as error:
        raise LocalPublishError("Your coding tool could not start safely. Open Website and confirm your current idea and build scope, then retry; check Settings > Keys and connections > Coding tools if your connection changed.") from error
    return {"kind": "device_work_order", "body_md": "Your coding tool is preparing your page files. Open Website to follow the build, then export or publish the checked result.",
        "actions": [{"label": "Follow your build", "href": f"/v/{venture_id}/device-work-orders/{order['id']}"}],
        "citations": [], "device_work_order": order}


def build_test_preview(venture_id: str, actor: str | None) -> dict:
    from app.core.local_mode import is_test_mode
    if not is_test_mode():
        raise LocalPublishError("Start your disposable installation in test mode, then retry your preview.")
    workspace_id, actor = _site_scope(venture_id, actor)
    from app.conversion.waitlist import _venture
    from app.state import db

    with db()() as session:
        name = _venture(session, venture_id, workspace_id).name
    try:
        return _request_public_bundle(venture_id, actor, name, waitlist_destination(venture_id, name, deployed=False))
    except LocalPublishError as error:
        return {"kind": "needs_you", "body_md": str(error),
            "actions": [{"label": "Open your connections", "href": "/settings/local"}], "citations": []}


@router.get("/workspaces/{workspace_id}/site/export")
def export_html(workspace_id: str, venture_id: str | None = None,
                x_subject: str | None = Header(default=None, alias="X-Subject")):
    if not is_local():
        raise HTTPException(404, "Open your hosted Website to download your page.")
    from app.api.firstlook_api import _assert_site_reader

    if venture_id:
        _assert_site_reader(workspace_id, venture_id, x_subject)
        from app.db.models import Job
        from app.pagebuilder import api
        from app.state import db
        from sqlalchemy import select

        with db()() as session:
            job = session.scalar(select(Job).where(Job.kind == api.KIND, Job.venture_id == venture_id,
                Job.workspace_id == workspace_id, Job.status == "succeeded").order_by(Job.created_at.desc()))
            job_id = job.id if job else None
        if job_id:
            response = export_current_html(venture_id, job_id=job_id, variant=0, caller=x_subject)
            response.headers["Content-Disposition"] = 'attachment; filename="landing.html"'
            return response

    from app.firstlook import versions
    from app.hosting.firstlook_publish import _local_files

    _assert_site_reader(workspace_id, venture_id, x_subject)
    version = versions.get_latest(workspace_id, venture_id=venture_id)
    if version is None:
        raise HTTPException(404, _missing_page_message())
    try:
        body = _local_files(version)[0]["body"]
    except (HostingError, LocalPublishError) as exc:
        from app.hosting.firstlook_publish import plain
        raise HTTPException(422, plain(str(exc)) if isinstance(exc, HostingError) else str(exc)) from None
    return Response(body, media_type="text/html", headers={"Cache-Control": "no-store",
        "Content-Disposition": 'attachment; filename="landing.html"', "X-Content-Type-Options": "nosniff"})


def _require_local():
    if not is_local():
        raise LocalPublishError("Run locally in your desktop app to export or deploy your page.")


def _settings():
    _require_local()
    from app.core.local_secrets import get_secret, read_settings

    try:
        settings = dict(read_settings("publishing")["values"])
        for field in ("token", "access_key_id", "secret_access_key", "session_token"):
            settings[field] = get_secret("publishing", field) or ""
    except (ValueError, OSError):
        raise LocalPublishError("Open Settings > Keys and connections > Your data folder and backup, restore your encrypted settings from a backup, then retry from Website.") from None
    return settings


def _required(settings, key):
    raw = settings.get(key, "")
    value = raw.strip() if isinstance(raw, str) else ""
    if not value:
        labels = {"token": "your provider token", "project": "your project or site ID", "bucket": "your bucket",
            "region": "your region", "prefix": "a new folder prefix", "site_url": "your public site URL",
            "access_key_id": "your access key ID", "secret_access_key": "your secret access key", "waitlist_email": "your waitlist email"}
        raise LocalPublishError(f"Open Settings > Keys and connections > Publishing, add {labels.get(key, key)}, and save before trying again.")
    return value


def _https_url(value, *, origin=False):
    if not isinstance(value, str):
        raise LocalPublishError("Open Settings > Keys and connections > Publishing, save your public HTTPS site URL, and try again.")
    try:
        parts = urlsplit(value)
        valid = (parts.scheme == "https" and parts.hostname and not parts.username and not parts.password
            and not parts.query and not parts.fragment and (not origin or parts.path in {"", "/"}))
        valid = valid and (parts.port is None or 1 <= parts.port <= 65535)
        valid = valid and not (parts.hostname == "supymem.com" or (parts.hostname or "").endswith(".supymem.com"))
    except ValueError:
        valid = False
    if not valid:
        raise LocalPublishError("Open Settings > Keys and connections > Publishing and save your own public HTTPS site URL without login details or a query, then try again.")
    return value.rstrip("/")


def waitlist_destination(venture_id, name, *, deployed=True, verify=True):
    from app.conversion.waitlist import clean_email, consent_text
    from app.core.local_mode import is_sample_mode

    if is_sample_mode():
        return {"mode": "setup", "url": "", "notice": "Your sample page stays private and collects no signups. Open Settings > Keys and connections > Your AI provider, save your model connection, then create a real venture to collect signups."}
    settings = _settings()
    if not deployed and not settings.get("site_url"):
        from app.conversion.waitlist import public_url

        return {"mode": "endpoint", "url": public_url(venture_id),
            "notice": "Your signup form posts to your local app. Keep your app running while serving this folder on your laptop. If your laptop is not reachable, open Experiments and record your page views and signups by hand."}
    if settings.get("site_url"):
        candidate = _https_url(settings["site_url"]) + f"/public/waitlists/{venture_id}"
        if not verify or _reachable(candidate, consent_text(name)):
            return {"mode": "endpoint", "url": candidate,
                "notice": "Your signup form posts to your own running instance. Keep your site URL reachable to receive signups."}
    if settings.get("provider") == "netlify" and (deployed or verify):
        return {"mode": "netlify", "url": "/", "notice": "Your signups go to Netlify Forms. Open your site in Netlify > Forms to read them, then import consented signups into Website > Collect signups."}
    email = settings.get("waitlist_email", "").strip()
    if not email:
        reason = "Your saved site URL did not respond to the signup check. " if settings.get("site_url") else ""
        return {"mode": "setup", "url": "", "notice": reason + "Open Settings > Keys and connections > Publishing, save a Waitlist email or a reachable site URL, then return to Website. You can still export your private preview."}
    try:
        email = clean_email(email, is_test=True)
    except ValueError:
        raise LocalPublishError("Open Settings > Keys and connections > Publishing and correct your waitlist email, then save and try again.") from None
    return {"mode": "mailto", "url": "mailto:" + email, "notice": "Your signup form opens the visitor's email app. Signups are not stored automatically. Import consented signups in Website > Collect signups, or save your reachable site URL in Settings > Keys and connections > Publishing and publish again."}


def _reachable(url, consent):
    try:
        with httpx.Client(timeout=10, follow_redirects=False, trust_env=False, headers={"Accept": WAITLIST_READINESS}) as client:
            response = get_checked(client, url, max_redirects=0, https_only=True)
        digest = hashlib.sha256(consent.encode()).hexdigest()
        if response.status_code == 204:
            return response.headers.get("X-Waitlist-Consent") == digest
        return response.status_code == 200 and digest in response.text and 'name="consent"' in response.text
    except (httpx.HTTPError, ValueError, OSError):
        return False


def _events_reachable(url):
    from core.research_web.guard import pin_request

    try:
        with httpx.Client(timeout=5, follow_redirects=False, trust_env=False, event_hooks={"request": [pin_request]}) as client:
            response = client.head(url)
        return response.status_code == 204 and response.headers.get("X-Page-Measurement") == "local"
    except (httpx.HTTPError, ValueError, OSError):
        return False


def _file(path, body, content_type):
    return {"path": path, "body": body, "size_bytes": len(body),
        "sha256": hashlib.sha256(body).hexdigest(), "content_type": content_type}


def _public_html(source, endpoint, *, measurement_url=""):
    action = "mailto:" if endpoint.startswith("mailto:") else "'self'"
    if endpoint.startswith("https://"):
        parsed = urlsplit(endpoint)
        action = parsed.scheme + "://" + parsed.netloc
    script_policy = "'none'"
    connection_policy = ""
    if measurement_url and (script := re.search(r'<script data-supymem-analytics="1">([\s\S]*?)</script>', source)):
        digest = base64.b64encode(hashlib.sha256(script.group(1).encode()).digest()).decode()
        script_policy = f"'sha256-{digest}'"
        connection_policy = "; connect-src " + measurement_url
    policy = "default-src 'none'; img-src 'self' data:; style-src 'self' 'unsafe-inline' data:; font-src 'self' data:; script-src " + script_policy + connection_policy + "; base-uri 'none'; form-action " + action
    source = re.sub(r'<meta\b[^>]*http-equiv=["\']Content-Security-Policy["\'][^>]*>', '', source, flags=re.I)
    tag = '<meta http-equiv="Content-Security-Policy" content="' + escape(policy, quote=True) + '">'
    return re.sub(r'<head\b[^>]*>', lambda match: match.group() + tag, source, count=1, flags=re.I) if re.search(r'<head\b', source, flags=re.I) else tag + source


def _validate_files(files):
    _require_local()
    manifest = [{key: item[key] for key in ("path", "size_bytes", "sha256")} for item in files]
    validated = public_manifest(manifest)
    settings = _settings()
    from app.connectors.secrets import store, vault_secret
    from app.core import providers_local
    from app.secrets.dotenv_file import read_env_file
    from app.core.local_secrets import get_secret

    secrets = tuple(value.strip() for source in (read_env_file(), os.environ, settings)
        for key, value in source.items() if _SECRET_KEY.search(key) and value
        and isinstance(value, str)
        and key != "LLM_DAILY_TOKEN_BUDGET" and not re.fullmatch(r"RATE_LIMIT_[A-Z_]+_[0-9]+", key))
    secrets += tuple(get_secret(section, field) or "" for section, fields in {
        "search": ("api_key", "firecrawl_key"), "smtp": ("password",), "composio": ("api_key",),
    }.items() for field in fields)
    try:
        secrets += tuple(store().values()) + (vault_secret(),)
        document = providers_local._read(providers_local._root())
        secrets += tuple((provider.get("api_key") or "").strip() for provider in document["providers"].values())
    except (AttributeError, OSError, RuntimeError, providers_local.LocalProviderError):
        raise LocalPublishError("Your credentials could not be checked. Restore your local credential storage and export again.") from None
    for item, expected in zip(files, validated["files"]):
        body = item["body"]
        if len(body) != item["size_bytes"] or hashlib.sha256(body).hexdigest() != item["sha256"]:
            raise LocalPublishError("Your page changed. Export the latest preview and try again.")
        try:
            inspect_blob(body, exact_secrets=secrets)
        except ArtifactRejected as error:
            if str(error) != "artifact_secret_detected":
                raise
            raise LocalPublishError("Your page contains a private credential. Remove it from your page and export again.") from None
        if item["content_type"] != expected["content_type"]:
            raise LocalPublishError("Your file type changed. Export the latest preview and try again.")


class _ExportLinks(HTMLParser):
    def __init__(self, source, replace, *, netlify=False, form_name="waitlist", post_forms=False, waitlist_url=None):
        super().__init__(convert_charrefs=False)
        self.source, self.replace, self.changes = source, replace, []
        self.netlify, self.form_name = netlify, form_name
        self.post_forms = post_forms
        self.waitlist_url = waitlist_url
        self.offsets = [0, *accumulate(len(line) for line in source.splitlines(keepends=True))]
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        if self.netlify and tag == "input" and dict(attrs).get("name") == "form-name":
            line, column = self.getpos()
            start = self.offsets[line - 1] + column
            self.changes.append((start, start + len(self.get_starttag_text()), ""))
            return
        updated = [(name, self.replace(name, value) if name in {"href", "src", "action", "formaction"} and value else value)
            for name, value in attrs if not name.startswith("on")]
        if tag == "a" and self.waitlist_url and dict(attrs).get("id") == "waitlist_cta":
            updated = [(name, self.replace("href", self.waitlist_url) if name == "href" else value) for name, value in updated]
        mailto_form = tag == "form" and any(name == "action" and value and value.startswith("mailto:") for name, value in updated)
        if mailto_form:
            updated = [(name, value) for name, value in updated if name != "enctype"] + [("enctype", "text/plain")]
        if tag == "form" and (mailto_form or self.post_forms):
            updated = [(name, value) for name, value in updated if name != "method"] + [("method", "post")]
        if tag == "form" and self.post_forms and not any(name == "action" for name, value in updated):
            updated.append(("action", self.replace("action", "")))
        if self.post_forms:
            updated = [(name, value) for name, value in updated if name not in {"formmethod", "formtarget", "formenctype"}]
        netlify_form = self.netlify and tag == "form" and any(key == "action" and value and value.startswith("/") and urlsplit(value).path == "/" for key, value in updated)
        if netlify_form:
            updated = [(key, value) for key, value in updated if key not in {"name", "method", "enctype", "data-netlify", "netlify-honeypot"}]
            updated += [("name", self.form_name), ("method", "post"), ("data-netlify", "true"), ("netlify-honeypot", "website")]
        if updated == attrs and not netlify_form:
            return
        raw = self.get_starttag_text()
        rewritten = "<" + tag + "".join(" " + name + (f'="{escape(value, quote=True)}"' if value is not None else "")
            for name, value in updated) + ("/>" if raw.endswith("/>") else ">")
        if netlify_form:
            rewritten += f'<input type="hidden" name="form-name" value="{escape(self.form_name, quote=True)}">'
        line, column = self.getpos()
        start = self.offsets[line - 1] + column
        self.changes.append((start, start + len(raw), rewritten))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def html(self):
        result = self.source
        for start, end, replacement in reversed(self.changes):
            result = result[:start] + replacement + result[end:]
        return result


def _consent_form_fields(form, name):
    from app.conversion.waitlist import VERSION, consent_text

    if not re.search(r'name=["\']email["\']|interest|waitlist', form, flags=re.I):
        return form
    consent = consent_text(name)
    fields = ""
    if not re.search(r'name=["\']consent["\']', form, flags=re.I):
        fields += '<label><input type="checkbox" name="consent" value="yes" required> ' + escape(consent) + '</label>'
    for key, value in (("consent_version", VERSION), ("consent_digest", hashlib.sha256(consent.encode()).hexdigest())):
        if not re.search(r'name=["\']' + key + '["\']', form, flags=re.I):
            fields += f'<input type="hidden" name="{key}" value="{value}">'
    if not re.search(r'name=["\']website["\']', form, flags=re.I):
        fields += '<label hidden aria-hidden="true">Website<input name="website" pattern="^$" tabindex="-1" autocomplete="off"></label>'
    return re.sub(r'</form\s*>', lambda match: fields + match.group(), form, count=1, flags=re.I)


def prepare(archive, capture, *, venture_id, name, waitlist_url, deployed, analytics_beacon=None, local_signup=None,
            caller=None, pagebuilder_candidate=None):
    """Reverify and copy the coding tool's public files without changing their bytes."""
    _require_local()

    files = verified_public_files(archive, capture, public_manifest(capture["manifest"]))
    return prepare_files(files, venture_id=venture_id, name=name, waitlist_url=waitlist_url,
        deployed=deployed, analytics_beacon=analytics_beacon, local_signup=local_signup,
        caller=caller, pagebuilder_candidate=pagebuilder_candidate)


def signup_page(venture_id, name, destination):
    """Reference markup for checking the coding tool's signup forms."""
    from app.conversion.waitlist import VERSION, consent_text

    consent = consent_text(name)
    endpoint, notice = destination["url"], destination["notice"]
    heading = (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<title>Early access to {escape(name)}</title></head><body><main><h1>Early access to {escape(name)}</h1>')
    if destination["mode"] == "setup":
        return heading + '<p>Preview only. This page does not collect signups.</p>' + f'<p>{escape(notice)}</p></main></body></html>'
    digest = hashlib.sha256(consent.encode()).hexdigest()
    form = (heading + f'<form method="post" action="{escape(endpoint, quote=True)}"'
        + (' enctype="text/plain"' if endpoint.startswith("mailto:") else '') + '>'
        '<label>Email address<input name="email" type="email" autocomplete="email" maxlength="254" required></label>'
        f'<input type="hidden" name="consent_version" value="{VERSION}">'
        f'<input type="hidden" name="consent_digest" value="{digest}">'
        '<label hidden aria-hidden="true">Website<input name="website" pattern="^$" tabindex="-1" autocomplete="off"></label>'
        f'<label><input type="checkbox" name="consent" value="yes" required> {escape(consent, quote=False)}</label>'
        '<button type="submit">Join early access</button></form>'
        f'<p>{escape(notice)}</p></main></body></html>')
    if destination["mode"] == "netlify":
        form = _ExportLinks(form, lambda key, value: value, netlify=True, form_name="waitlist-" + venture_id).html()
    return form


def _check_public_files(files, name, destination, waitlist_url, *, deployed):
    from app.conversion.waitlist import VERSION, consent_text
    from app.pagebuilder.qa import _EditSource, has_signup_submit
    from core.public_copy import public_copy_observation, public_html_text
    from posixpath import dirname, join, normpath
    from urllib.parse import unquote

    paths = {item["path"] for item in files}
    consent = consent_text(name)
    digest = hashlib.sha256(consent.encode()).hexdigest()
    if "index.html" not in paths:
        raise ValueError("public_index_required")
    for item in files:
        if not item["path"].lower().endswith(".html"):
            continue
        source = item["body"].decode("utf-8")
        parsed = _EditSource(source)
        signup_companion = item["path"] == "waitlist.html" and destination["mode"] != "setup"
        if signup_companion and not any(node[0] == "form" for node in parsed.nodes):
            raise ValueError("public_consent_required")
        if deployed and public_copy_observation(public_html_text(source))[0]:
            raise ValueError("public_copy_unreviewed")
        policies = [attrs.get("content", "") for tag, _, _, attrs, parents in parsed.nodes
            if tag == "meta" and attrs.get("http-equiv", "").lower() == "content-security-policy"
            and any(parent[0] == "head" for parent in parents)]
        if deployed:
            if len(policies) != 1:
                raise ValueError("public_content_policy_required")
            policy = {parts[0]: parts[1:] for directive in policies[0].split(";") if (parts := directive.split())}
            endpoint = urlsplit(destination["url"])
            form_origin = endpoint.scheme + "://" + endpoint.netloc if endpoint.netloc else "mailto:"
            form_policies = [[form_origin]] if destination["mode"] == "mailto" else [[form_origin], ["'self'"]]
            if (policy.get("default-src") != ["'none'"] or policy.get("base-uri") != ["'none'"]
                    or not policy.get("script-src") or any(value not in {"'self'", "'none'"}
                        and not re.fullmatch(r"'sha256-[A-Za-z0-9+/]{43}='", value) for value in policy["script-src"])
                    or policy.get("form-action") not in form_policies):
                raise ValueError("public_content_policy_required")
        for tag, start, end, attrs, parents in parsed.nodes:
            if deployed and tag == "script":
                if attrs.get("src"):
                    if "'self'" not in policy["script-src"] or urlsplit(attrs["src"]).scheme or urlsplit(attrs["src"]).netloc:
                        raise ValueError("public_script_source_required")
                else:
                    content = re.fullmatch(r'<script\b[^>]*>([\s\S]*?)</script\s*>', source[start:end], flags=re.I)
                    digest_script = base64.b64encode(hashlib.sha256(content.group(1).encode()).digest()).decode() if content else ""
                    if "'sha256-" + digest_script + "'" not in policy["script-src"]:
                        raise ValueError("public_script_hash_required")
                if "data-supymem-analytics" in attrs:
                    from app.product_analytics.beacon import public_events_url

                    measurement_url = public_events_url()
                    if not enabled("page_measurement"):
                        raise LocalPublishError(blocked_message("page_measurement"))
                    if not measurement_url or not _events_reachable(measurement_url):
                        raise LocalPublishError("Open Settings > Keys and connections > Publishing, save your reachable public site URL and keep your instance running, then retry your page export or publication.")
                    if measurement_url not in source[start:end]:
                        raise ValueError("public_measurement_binding_changed")
            for attribute in ("href", "src", "action", "formaction"):
                target = attrs.get(attribute)
                if not target:
                    continue
                url = urlsplit(target)
                if url.hostname and (url.hostname in {"localhost", "127.0.0.1", "::1", "supymem.com"}
                        or url.hostname.endswith(".supymem.com")):
                    if deployed or target != destination["url"]:
                        raise ValueError("public_destination_required")
                if target.startswith(waitlist_url) and not target.startswith(destination["url"] or "#"):
                    raise ValueError("public_signup_destination_changed")
                if attribute in {"href", "src"} and url.path and not url.scheme and not url.netloc:
                    path = normpath(unquote(url.path).lstrip("/") if url.path.startswith("/")
                        else join(dirname(item["path"]), unquote(url.path)))
                    if url.path.endswith("/"):
                        path = join(path, "index.html")
                    if path not in paths:
                        raise ValueError("public_companion_required")
            if tag != "form":
                continue
            controls = [node[3] for node in parsed.nodes if node[0] in {"input", "button", "select", "textarea"}
                and (any(parent[0] == "form" and parent[1] == start for parent in node[4])
                    or attrs.get("id") and node[3].get("form") == attrs["id"])]
            fields = {control.get("name"): control for control in controls if control.get("name")}
            if not signup_companion and "email" not in fields and not re.search(r"waitlist|interest|request", " ".join(str(value or "") for value in attrs.values()), re.I):
                continue
            if ("email" not in fields or attrs.get("action") != destination["url"] or attrs.get("method", "get").lower() != "post"
                    or "onsubmit" in attrs or any(any(key in control for key in (
                        "onclick", "formaction", "formmethod", "formtarget", "formenctype")) for control in controls)
                    or fields.get("consent_version", {}).get("value") != VERSION
                    or fields.get("consent_digest", {}).get("value") != digest
                    or fields.get("consent", {}).get("type", "").lower() != "checkbox"
                    or fields.get("consent", {}).get("value") != "yes"
                    or "required" not in fields.get("consent", {}) or "checked" in fields.get("consent", {})
                    or fields.get("website", {}).get("pattern") != "^$" or fields.get("website", {}).get("value", "")
                    or consent not in public_html_text(source[start:end])):
                raise ValueError("public_consent_required")
            if not has_signup_submit(parsed, start):
                raise ValueError("public_submit_button_required_inside_form")
            if destination["mode"] == "mailto" and attrs.get("enctype") != "text/plain":
                raise ValueError("public_mailto_encoding_required")
            if destination["mode"] == "netlify" and (attrs.get("data-netlify") != "true"
                    or not attrs.get("name") or fields.get("form-name", {}).get("value") != attrs["name"]):
                raise ValueError("public_netlify_form_required")


def prepare_files(files, *, venture_id, name, waitlist_url, deployed, analytics_beacon=None, local_signup=None,
                  caller=None, pagebuilder_candidate=None):
    _require_local()

    source_url = urlsplit(waitlist_url)
    if (source_url.scheme not in {"http", "https"} or not source_url.hostname or source_url.username
            or source_url.password or source_url.query or source_url.fragment):
        raise LocalPublishError("Your signup link is invalid. Build the latest landing page and export again.")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", venture_id):
        raise LocalPublishError("Open your project again, then export the latest preview.")
    destination = local_signup if not deployed and local_signup else waitlist_destination(venture_id, name, deployed=deployed)
    notice = destination["notice"]
    if destination["mode"] == "setup":
        if deployed:
            raise LocalPublishError(notice)
    _validate_files(files)
    try:
        _check_public_files(files, name, destination, waitlist_url, deployed=deployed)
        if deployed:
            from app.product_analytics.beacon import _app_for_venture, inject_into_html, public_events_url

            measurement_url = public_events_url()
            if measurement_url and _events_reachable(measurement_url):
                binding = _app_for_venture(venture_id)
                if binding:
                    source = next(item["body"].decode() for item in files if item["path"] == "index.html")
                    try:
                        inject_into_html(source, key=binding[1], endpoint=measurement_url)
                    except ValueError as error:
                        raise LocalPublishError(str(error)) from None
    except LocalPublishError:
        raise
    except (ValueError, UnicodeError) as error:
        if not deployed:
            raise LocalPublishError("Your saved page does not match its signup settings. In your venture chat, ask Charlie to build a new landing page with your current signup destination, then export it on Website.") from None
        if caller is not None and pagebuilder_candidate is not None:
            result = _request_public_bundle(venture_id, caller, name, destination,
                pagebuilder_candidate=pagebuilder_candidate, feedback=str(error))
            raise LocalPublishError(result["body_md"]) from error
        raise LocalPublishError("Your page needs updated public files. Open Settings > Keys and connections > Coding tools, connect and allow your coding tool, then ask for the current signup destination and consent on Website before exporting or publishing.") from error
    return files, notice


def export_zip(files):
    """Export only public static files, never local settings."""
    _validate_files(files)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for item in files:
            archive.writestr(item["path"], item["body"])
    return output.getvalue()


def export_folder(files, destination):
    """Create a new static folder without replacing an existing path."""
    _validate_files(files)
    destination = Path(destination).expanduser()
    parent = destination.parent.resolve(strict=True)
    destination = parent / destination.name
    if destination.exists() or destination.is_symlink():
        raise LocalPublishError("Choose a new folder by exporting again from Website. Your existing files are unchanged.")
    try:
        destination.mkdir()
        for item in files:
            write_file(destination, item["path"], item["body"])
    except (OSError, ValueError):
        raise LocalPublishError("Your folder export did not finish. Open Settings > Data, check your data folder permissions and free space, then export again from Website.") from None
    return str(destination)


def _request(method, url, **kwargs):
    from app.credits import metered

    try:
        def send():
            with httpx.Client(timeout=30, follow_redirects=False, trust_env=False) as client:
                return client.request(method, url, **kwargs)
        response = metered.call_sync(send, purpose="site.publish", estimate_micro=0)
        if method == "DELETE" and response.status_code == 404:
            return {}
        response.raise_for_status()
        if not response.content:
            return {}
        result = response.json()
        if not isinstance(result, dict):
            raise TypeError("unexpected_response")
        return result
    except (httpx.HTTPError, TypeError, ValueError):
        raise LocalPublishError("Your hosting provider did not confirm deployment. Open Settings > Keys and connections > Publishing, check your token or access keys and your project, then retry from Website.") from None


def _s3_client(service, **kwargs):
    import boto3
    from botocore.config import Config

    return boto3.client(service, config=Config(connect_timeout=5, read_timeout=30,
        retries={"total_max_attempts": 2}, ignore_configured_endpoint_urls=True, proxies={}), **kwargs)


def _verify_public(url, sha256):
    url = _https_url(url) + ("/" if url.endswith("/") else "")
    try:
        with httpx.Client(timeout=10, follow_redirects=False, trust_env=False) as client:
            response = get_checked(client, url, max_redirects=0, https_only=True)
        if response.status_code == 200 and hashlib.sha256(response.content).hexdigest() == sha256:
            return
    except (httpx.HTTPError, ValueError, OSError):
        pass
    raise LocalPublishError("Your upload finished, but your public page could not be verified. Open your hosting account and check the deployment. In Settings > Keys and connections > Publishing, check your site URL, then choose Check public URL in Website before publishing again.")


def _poll_deployment(provider, identity, token, result=None):
    if not isinstance(identity, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", identity):
        raise LocalPublishError("Your host did not return a deployment ID. Check Deploys in your hosting account before retrying from Website.")
    vercel = provider == "vercel"
    state_key, ready = ("readyState", "READY") if vercel else ("state", "ready")
    poll_base = "https://api.vercel.com/v13/deployments/" if vercel else "https://api.netlify.com/api/v1/deploys/"
    deadline = time.monotonic() + 120
    while True:
        if result is None:
            try:
                result = _request("GET", poll_base + identity, headers={"Authorization": "Bearer " + token})
            except LocalPublishError:
                raise LocalPublishError("Your host accepted your page but its status could not be checked. On Website, choose Check public URL. If it still fails, check your token in Settings > Keys and connections > Publishing.") from None
        if result.get("id") != identity:
            raise LocalPublishError("Your host did not confirm the saved deployment. Check Deploys in your hosting account, then choose Check public URL on Website.")
        if result.get(state_key) == ready:
            value = result.get("url", "") if vercel else result.get("ssl_url", "")
            return _https_url(("https://" + value) if vercel else value) + "/"
        if result.get(state_key) in {"ERROR", "CANCELED", "error", "failed"}:
            raise DeploymentRejected("Your host stopped this deployment. Check Deploys in your hosting account, then choose Publish again on Website.")
        if time.monotonic() >= deadline:
            raise LocalPublishError("Your deployment is not ready. Check Deploys in your hosting account, then choose Check public URL on Website.")
        result = None
        time.sleep(2)


def deploy(files, provider, *, receipt=None, save_accepted=None):
    """Deploy a static page using your saved publishing account."""
    _validate_files(files)
    if not enabled("publish_pages"):
        raise LocalPublishError(blocked_message("publish_pages"))
    settings = _settings()
    if provider != settings.get("provider"):
        raise LocalPublishError("Open Settings > Keys and connections > Publishing, select your provider, and save before publishing.")
    index = next(item for item in files if item["path"] == "index.html")
    if receipt is not None:
        receipt.update({"provider": provider, "sha256": index["sha256"],
            "files": [{key: item[key] for key in ("path", "sha256")} for item in files],
            **{key: settings.get(key, "") for key in ("project", "bucket", "region", "prefix")}})
    if provider == "s3":
        from botocore.exceptions import BotoCoreError, ClientError

        bucket = _required(settings, "bucket")
        prefix = _required(settings, "prefix").strip("/")
        if not re.fullmatch(r"[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*", prefix):
            raise LocalPublishError("Open Settings > Keys and connections > Publishing, set a new folder prefix using letters, numbers, hyphens or underscores, and publish again.")
        address = _https_url(_required(settings, "site_url"))
        url = address if urlsplit(address).path.endswith(".html") else address + "/"
        try:
            client = _s3_client("s3", region_name=_required(settings, "region"),
                aws_access_key_id=_required(settings, "access_key_id"),
                aws_secret_access_key=_required(settings, "secret_access_key"),
                aws_session_token=settings.get("session_token") or None)
            for item in files:
                from app.credits import metered
                metered.call_sync(lambda: client.put_object(Bucket=bucket, Key=prefix + "/" + item["path"], Body=item["body"],
                    ContentType=item["content_type"], Metadata={"sha256": item["sha256"]}, IfNoneMatch="*"), purpose="site.publish", estimate_micro=0)
                head = metered.call_sync(lambda: client.head_object(Bucket=bucket, Key=prefix + "/" + item["path"]), purpose="site.publish", estimate_micro=0)
                if head.get("ContentLength") != len(item["body"]) or head.get("Metadata", {}).get("sha256") != item["sha256"]:
                    raise LocalPublishError("Your storage upload could not be verified. Open Settings > Keys and connections > Publishing, check your bucket and choose a new folder prefix before retrying.")
        except (BotoCoreError, ClientError):
            raise LocalPublishError("Your storage upload did not finish. Open Settings > Keys and connections > Publishing, check your access keys and choose a new folder prefix before retrying.") from None
    elif provider in {"vercel", "netlify"}:
        token = _required(settings, "token")
        if provider == "vercel":
            headers = {"Authorization": "Bearer " + token}
            project = _required(settings, "project")
            if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,99}", project):
                raise LocalPublishError("Open Settings > Keys and connections > Publishing, set your Vercel project name, and save before trying again.")
            result = _request("POST", "https://api.vercel.com/v13/deployments", headers=headers,
                json={"name": project, "project": project, "target": "production", "projectSettings": {"framework": None},
                    "files": [{"file": item["path"], "data": base64.b64encode(item["body"]).decode(), "encoding": "base64"} for item in files]})
        else:
            headers = {"Authorization": "Bearer " + token, "Content-Type": "application/zip"}
            site = _required(settings, "project")
            if not re.fullmatch(r"[A-Za-z0-9_-]+", site):
                raise LocalPublishError("Open Settings > Keys and connections > Publishing, set your Netlify site ID, and save before trying again.")
            result = _request("POST", f"https://api.netlify.com/api/v1/sites/{site}/deploys", headers=headers, content=export_zip(files))
        identity = result.get("id", "")
        if receipt is not None and isinstance(identity, str) and re.fullmatch(r"[A-Za-z0-9_-]+", identity):
            receipt.update(deployment_id=identity, state="verification_pending")
            if save_accepted is not None:
                save_accepted(receipt)
        try:
            url = _poll_deployment(provider, identity, token, result)
        except DeploymentRejected:
            if receipt is not None and receipt.get("deployment_id"):
                receipt["state"] = "failed"
                if save_accepted is not None:
                    save_accepted(receipt)
            raise
    else:
        raise LocalPublishError("Choose Vercel, Netlify or S3, then deploy again.")
    if receipt is not None:
        receipt.update(url=url, state="verification_pending")
        if save_accepted is not None:
            save_accepted(receipt)
        _verify_receipt(receipt)
    else:
        _verify_public(url, index["sha256"])
    return url


class PublishBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: Literal["vercel", "netlify", "s3"]


def caller(request: Request):
    if not is_local():
        raise HTTPException(404, detail="Run locally to use this page. Open your hosted Website instead.")
    from app.admin.authn import verify_caller

    return verify_caller(request)


_CALLER = Depends(caller)


def _load(venture_id, job_id, variant, caller, *, deployed, receipt=None):
    from app.conversion import waitlist
    from app.db.supymem_models import VentureWaitlist
    from app.pagebuilder import api, native_reference
    from app.state import db
    from app.db.models import Job

    _require_local()
    wid, _actor = _site_scope(venture_id, caller)
    with db()() as session:
        row = session.get(Job, job_id)
        if not row or (row.kind, row.venture_id, row.workspace_id) != (api.KIND, venture_id, wid):
            raise HTTPException(404, "Open your own Website and choose your saved page before exporting or publishing.")
        doc = native_reference._completed_candidate(session, row)
        venture = waitlist._venture(session, venture_id, wid)
        configured = session.get(VentureWaitlist, venture_id)
        if configured and configured.workspace_id != wid or deployed and (not configured or not configured.enabled or configured.consent_version != waitlist.VERSION):
            raise LocalPublishError("Turn on your waitlist with the current consent text, then export again.")
        frozen = doc["_runtime"]["input"]
        revision_id, _current = api.snapshot(session, venture_id, wid)
        if doc["revision_id"] != revision_id or frozen["name"] != venture.name or frozen["waitlist_consent_text"] != waitlist.consent_text(venture.name):
            raise LocalPublishError("Your project changed. Build its latest landing page before exporting.")
    bundles = doc["result"]["bundles"]
    if not 0 <= variant < len(bundles):
        raise LocalPublishError("Choose an available page preview, then try again.")
    bundle = bundles[variant]
    if receipt is not None and (test := frozen.get("page_test")) and (test.get("headlines") or test.get("prices")):
        receipt.update(experiment_id=test["experiment_id"], app_id=doc["_runtime"]["analytics_app_id"],
            source_sha256=bundle["manifest"]["artifact_capture"]["sha256"])
    return prepare(base64.b64decode(bundle["source_zip"], validate=True), bundle["manifest"]["artifact_capture"],
        venture_id=venture_id, name=frozen["name"], waitlist_url=frozen["waitlist_url"], deployed=deployed,
        analytics_beacon=bundle["manifest"].get("analytics_beacon"),
        local_signup=frozen.get("research", {}).get("local_signup"), caller=caller,
        pagebuilder_candidate={"job_id": job_id, "variant": variant})


def _error(error):
    if isinstance(error, LocalPublishError):
        message = str(error)
    else:
        message = "Your page could not be verified. Open Website, build your latest landing page, and try again."
    raise HTTPException(409, detail=message) from None


@router.get(PREFIX + "/preview")
def preview(venture_id: str, job_id: str, variant: int = Query(0, ge=0, le=2), caller=_CALLER):
    _require_local()
    from app.api.pagebuilder_api import preview as inert_preview

    return inert_preview(venture_id, job_id, variant, caller)


@router.get(PREFIX + "/export.zip")
def download(venture_id: str, job_id: str, variant: int = Query(0, ge=0, le=2), caller=_CALLER):
    try:
        files, notice = _load(venture_id, job_id, variant, caller, deployed=False)
        return Response(export_zip(files), media_type="application/zip", headers={"Cache-Control": "no-store",
            "Content-Disposition": 'attachment; filename="landing-page.zip"', "X-Content-Type-Options": "nosniff",
            "X-Page-Notice": notice})
    except (HostingError, ValueError, KeyError, OSError) as error:
        _error(error)


@router.post(PREFIX + "/export-folder")
def folder(venture_id: str, job_id: str, variant: int = Query(0, ge=0, le=2), caller=_CALLER):
    try:
        files, notice = _load(venture_id, job_id, variant, caller, deployed=False)
        from app.config import data_root
        from core.ids import new_id
        parent = data_root() / "exports"
        parent.mkdir(parents=True, exist_ok=True)
        destination = parent / new_id("page")
        return {"folder": export_folder(files, destination), "notice": notice}
    except (HostingError, ValueError, KeyError, OSError) as error:
        _error(error)


@router.post(PREFIX + "/deploy")
def publish(venture_id: str, job_id: str, body: PublishBody, variant: int = Query(0, ge=0, le=2), caller=_CALLER):
    try:
        return publish_site(venture_id, caller, job_id=job_id, variant=variant, provider=body.provider)
    except (HostingError, ValueError, KeyError, OSError) as error:
        _error(error)


def _site_scope(venture_id, caller):
    _require_local()
    from app.authz.workspace import assert_venture_access
    from app.api.firstlook_api import _assert_site_writer
    actor = caller if isinstance(caller, str) else getattr(caller, "sub", None)
    _, workspace_id = assert_venture_access(venture_id, actor)
    _assert_site_writer(workspace_id, venture_id, actor)
    return workspace_id, actor


def _publication(venture_id, workspace_id):
    from app.db.models import VentureDocument
    from app.state import db
    with db()() as session:
        row = session.get(VentureDocument, "local-publication:" + venture_id)
        if not row or row.venture_id != venture_id:
            return None
        receipt = json.loads(row.provenance_json)
        if receipt.get("workspace_id") != workspace_id:
            raise LocalPublishError("Open your own Website to manage this publication.")
        return receipt


def _publication_attempt(receipt):
    return (receipt or {}).get("pending_attempt") or receipt or {}


def _publication_key(receipt):
    if receipt.get("provider") == "s3":
        return ("s3", receipt.get("bucket"), (receipt.get("prefix") or "").strip("/"))
    return (receipt.get("provider"), receipt.get("deployment_id") or receipt.get("local_deployment_id") or receipt.get("url"))


def _recorded_publications(receipt, *, include_prior=True):
    if receipt:
        if pending := receipt.get("pending_attempt"):
            yield pending
        yield {key: value for key, value in receipt.items() if key not in {"pending_attempt", "prior_publications"}}
        if include_prior:
            yield from receipt.get("prior_publications", [])


def _accepted_publications(receipt):
    return (item for item in _recorded_publications(receipt) if item.get("state") in {"live", "verification_pending"})


def live_publications(venture_id, workspace_id, *, include_prior):
    seen = set()
    for receipt in _recorded_publications(_publication(venture_id, workspace_id), include_prior=include_prior):
        url = receipt.get("url")
        if url and url not in seen and receipt.get("state") == "live":
            yield receipt
        if url:
            seen.add(url)


def _verify_receipt(receipt):
    url = receipt["url"]
    _verify_public(url, receipt["sha256"])
    if receipt.get("experiment_id"):
        for item in receipt["files"]:
            if item["path"] != "index.html" and item["path"].endswith(".html"):
                _verify_public(urljoin(url, item["path"]), item["sha256"])


def publish_accepted(app_id, build_id, *, actor, effect_fence=None, deployment_id=None):
    from app.db.models import Job
    from app.db.supymem_models import Deployment, FounderApp
    from app.db.session import serialize_writes
    from app.hosting.service import _accepted_source, _serialize
    from app.pagebuilder import api, native_reference
    from app.conversion import waitlist
    from app.state import db
    from core.clock import utcnow
    from core.ids import new_id
    from sqlalchemy import func, select

    _require_local()
    if not enabled("publish_pages"):
        raise HostingError(blocked_message("publish_pages"))
    with db()() as session:
        serialize_writes(session, key="static-publish:" + app_id)
        app = session.get(FounderApp, app_id)
        if not app or app.deleted_at is not None:
            raise HostingError("hosting_app_not_found", status=404)
        workspace_id, venture_id = app.workspace_id, app.venture_id
        _site_scope(venture_id, actor)
        if is_sample_workspace(workspace_id):
            raise LocalPublishError("Your sample page stays private. Open Website to preview or export it. To publish, open Settings > Keys and connections > Your AI provider, save your model and API key, then create a real venture to publish your own page.")
        if effect_fence is not None:
            effect_fence(session)
        run, artifact, qa, capture, manifest = _accepted_source(session, app, build_id)
        from app.hosting.unpublish import assert_no_takedown
        from app.hosting.revisions import assert_publication_baseline
        assert_no_takedown(session, app)
        assert_publication_baseline(session, app, run, artifact)
        archive = None
        for job in session.scalars(select(Job).where(Job.kind == api.KIND, Job.venture_id == venture_id,
                Job.workspace_id == workspace_id, Job.status == "succeeded")):
            doc = native_reference._completed_candidate(session, job)
            for bundle in doc["result"]["bundles"]:
                if bundle["manifest"]["artifact_capture"]["sha256"] == artifact.sha256:
                    archive = base64.b64decode(bundle["source_zip"], validate=True)
                    break
            if archive is not None:
                break
        if archive is None:
            raise HostingError("Your accepted source is not saved on this laptop. Open Website and build a local landing page, then publish that saved page.")
        name = waitlist._venture(session, venture_id, workspace_id).name
        deployment = session.get(Deployment, deployment_id) if deployment_id else session.scalar(select(Deployment).where(
            Deployment.app_id == app_id, Deployment.build_artifact_id == artifact.id, Deployment.target == "local_static").order_by(Deployment.version.desc()))
        if deployment and (deployment.app_id, deployment.build_artifact_id, deployment.target) != (app_id, artifact.id, "local_static"):
            raise HostingError("hosting_source_changed")
        if deployment and deployment.state == "live":
            return _serialize(deployment)
        if not deployment:
            version = (session.scalar(select(func.max(Deployment.version)).where(Deployment.app_id == app_id)) or 0) + 1
            deployment = Deployment(id=deployment_id or new_id("dep"), workspace_id=workspace_id, venture_id=venture_id,
                app_id=app_id, build_artifact_id=artifact.id, target="local_static", state="pending", version=version, started_at=utcnow())
            session.add(deployment)
        identity = deployment.id
        session.commit()
    receipt = {"state": "publishing", "local_deployment_id": identity, "app_id": app_id}
    try:
        pending = _publication_attempt(_publication(venture_id, workspace_id))
        if pending.get("state") == "verification_pending":
            if pending.get("local_deployment_id") != identity:
                raise LocalPublishError("Choose Check public URL on Website before starting another publication.")
            checked = verify_current(venture_id, actor)
            if checked["state"] != "live":
                raise LocalPublishError(checked["notice"])
            with db()() as session:
                return _serialize(session.get(Deployment, identity))
        waitlist.configure(venture_id, workspace_id, enabled=True, actor=actor)
        files, notice = prepare(archive, capture, venture_id=venture_id, name=name,
            waitlist_url=waitlist.public_url(venture_id), deployed=True)
        receipt["notice"] = notice
        url = deploy(files, _required(_settings(), "provider"), receipt=receipt,
            save_accepted=lambda pending: _save_publication(venture_id, workspace_id, pending))
        receipt["state"] = "live"
        _save_publication(venture_id, workspace_id, receipt)
        with db()() as session:
            deployment = session.get(Deployment, identity)
            deployment.state, deployment.url, deployment.error = "live", url, None
            deployment.live_at = deployment.live_at or utcnow()
            session.commit()
            return _serialize(deployment)
    except (ValueError, OSError) as error:
        if receipt.get("url"):
            receipt["state"] = "verification_pending"
            _save_publication(venture_id, workspace_id, receipt)
        with db()() as session:
            deployment = session.get(Deployment, identity)
            deployment.state = "failed"
            deployment.error = "Open Website and check your publishing account in Settings > Keys and connections > Publishing."
            session.commit()
        raise HostingError(str(error) if isinstance(error, LocalPublishError) else "Open Website and build a fresh local landing page, then publish again.") from None


def _save_publication(venture_id, workspace_id, receipt):
    from app.db.models import VentureDocument
    from app.db.session import serialize_writes
    from app.state import db
    from core.clock import utcnow
    with db()() as session:
        serialize_writes(session, key="local-publication:" + venture_id)
        row = session.get(VentureDocument, "local-publication:" + venture_id)
        previous = json.loads(row.provenance_json) if row else {}
        if row and (row.venture_id != venture_id or previous.get("workspace_id") != workspace_id):
            raise LocalPublishError("Open your own Website and try again. Your page belongs to another workspace.")
        if row is None:
            row = VentureDocument(id="local-publication:" + venture_id, venture_id=venture_id,
                kind="local_publication", title="Your published landing page", created_at=utcnow(), updated_at=utcnow())
            session.add(row)
        saved = {**receipt, "workspace_id": workspace_id}
        saved.pop("pending_attempt", None)
        saved.pop("prior_publications", None)
        key = _publication_key(saved)
        history = previous.pop("prior_publications", [])
        pending = previous.get("pending_attempt")
        if saved["state"] == "offline" and previous:
            history = [item for item in history if _publication_key(item) != key]
            if pending and _publication_key(pending) == key:
                previous.pop("pending_attempt")
            if _publication_key(previous) != key:
                saved = previous
            elif "pending_attempt" in previous:
                saved["pending_attempt"] = previous["pending_attempt"]
        elif saved["state"] == "verification_pending" and any(_publication_key(item) == key for item in history):
            history = [saved if _publication_key(item) == key else item for item in history]
            saved = previous
        elif saved["state"] in {"verification_pending", "failed"} and previous.get("state") in {"live", "offline"}:
            if saved["state"] == "verification_pending":
                if pending and _publication_key(pending) != key:
                    history.insert(0, pending)
                previous["pending_attempt"] = saved
            elif pending and _publication_key(pending) == key:
                previous.pop("pending_attempt", None)
            saved = previous
        elif saved["state"] == "live":
            history = [item for item in _accepted_publications({**previous, "prior_publications": history}) if _publication_key(item) != key]
        elif saved["state"] == "verification_pending" and previous.get("state") == "verification_pending" and _publication_key(previous) != key:
            history.insert(0, previous)
        if saved["state"] == "live" and saved.get("experiment_id"):
            saved.setdefault("live_at", utcnow().isoformat())
        if history:
            saved["prior_publications"] = history
        row.provenance_json = json.dumps(saved)
        row.body_md = "Open Website to manage your published landing page."
        row.updated_at = utcnow()
        session.commit()


def _preview_files(venture_id, workspace_id, actor, *, deployed, variant=0):
    from app.conversion import waitlist
    from app.db.models import Job
    from app.firstlook import versions
    from app.hosting.firstlook_publish import _local_files
    from app.pagebuilder import api
    from app.state import db
    from sqlalchemy import select

    with db()() as session:
        name = waitlist._venture(session, venture_id, workspace_id).name
        job = session.scalar(select(Job).where(Job.kind == api.KIND, Job.venture_id == venture_id,
            Job.workspace_id == workspace_id, Job.status == "succeeded").order_by(Job.created_at.desc()))
        job_id = job.id if job else None
    if job_id:
        return _load(venture_id, job_id, variant, actor, deployed=deployed)
    version = versions.get_latest(workspace_id, venture_id=venture_id)
    if version is None:
        raise LocalPublishError(_missing_page_message())
    return prepare_files(_local_files(version),
        venture_id=venture_id, name=name, waitlist_url=waitlist.public_url(venture_id), deployed=deployed, caller=actor)


def publish_site(venture_id, caller, *, job_id=None, variant=0, provider=None, app_id=None, build_id=None,
                 site_version_id=None, public_sha256=None):
    _require_local()
    workspace_id, actor = _site_scope(venture_id, caller)
    if is_sample_workspace(workspace_id):
        raise LocalPublishError("Your sample page stays private. Open Website to preview or export it. To publish, open Settings > Keys and connections > Your AI provider, save your model and API key, then create a real venture to publish your own page.")
    if not enabled("publish_pages"):
        raise LocalPublishError(blocked_message("publish_pages"))
    saved = _publication(venture_id, workspace_id)
    attempt = _publication_attempt(saved)
    if attempt.get("state") == "verification_pending":
        raise LocalPublishError("Your host accepted your last publication. On Website, choose Check public URL before publishing again.")
    from app.conversion import waitlist
    if site_version_id or public_sha256:
        from app.hosting.firstlook_publish import public_page
        if not site_version_id or not public_sha256:
            raise HostingError("hosting_firstlook_draft_changed")
        page = public_page(venture_id, version_id=site_version_id)
        if page["workspace_id"] != workspace_id or page["sha256"] != public_sha256:
            raise HostingError("hosting_firstlook_draft_changed")
        files, notice = prepare_files(page.get("files") or [_file("index.html", page["body"], "text/html; charset=utf-8")],
            venture_id=venture_id, name=page["name"], waitlist_url=page["waitlist_url"], deployed=True, caller=caller)
    waitlist.configure(venture_id, workspace_id, enabled=True, actor=actor)
    if not job_id and not site_version_id:
        from app.pagebuilder import api
        from app.db.models import Job
        from app.state import db
        from sqlalchemy import select
        with db()() as session:
            job = session.scalar(select(Job).where(Job.kind == api.KIND, Job.venture_id == venture_id,
                Job.workspace_id == workspace_id, Job.status == "succeeded").order_by(Job.created_at.desc()))
            job_id = job.id if job else None
    if not job_id and not site_version_id and app_id and build_id:
        from app.db.supymem_models import FounderApp
        from app.state import db
        with db()() as session:
            app = session.get(FounderApp, app_id)
            if not app or (app.venture_id, app.workspace_id) != (venture_id, workspace_id):
                raise HTTPException(404, "Open your own Website and choose your saved build before publishing.")
        return publish_accepted(app_id, build_id, actor=actor)
    receipt = {"state": "publishing", "job_id": job_id, "variant": variant}
    if not site_version_id:
        files, notice = (_load(venture_id, job_id, variant, caller, deployed=True, receipt=receipt) if job_id
            else _preview_files(venture_id, workspace_id, actor, deployed=True, variant=variant))
    settings = _settings()
    chosen_provider = provider or _required(settings, "provider")
    if saved and saved.get("state") == "publishing":
        from app.hosting.firstlook_publish import plain
        raise LocalPublishError(plain("hosting_publication_in_progress"))
    manifest = [{key: item[key] for key in ("path", "sha256")} for item in files]
    fields = ("bucket", "region", "prefix") if chosen_provider == "s3" else ("project",)
    if (saved and saved.get("state") == "live" and saved.get("provider") == chosen_provider and saved.get("files") == manifest
            and all(saved.get(key) == settings.get(key, "") for key in fields)
            and all(saved.get(key) == receipt.get(key) for key in ("experiment_id", "source_sha256"))):
        _verify_receipt(saved)
        return {"url": saved["url"], "notice": saved["notice"], "state": "live"}
    receipt["notice"] = notice
    if site_version_id:
        receipt.update(site_version_id=site_version_id, public_sha256=public_sha256)
    try:
        url = deploy(files, chosen_provider, receipt=receipt,
            save_accepted=lambda pending: _save_publication(venture_id, workspace_id, pending))
    except LocalPublishError:
        if receipt.get("url"):
            receipt["state"] = "verification_pending"
            _save_publication(venture_id, workspace_id, receipt)
        raise
    receipt["state"] = "live"
    _save_publication(venture_id, workspace_id, receipt)
    return {"url": url, "notice": notice, "state": "live"}


class SitePublishBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_id: str | None = None
    variant: int = Field(default=0, ge=0, le=2)
    app_id: str | None = None
    build_id: str | None = None


@router.get("/ventures/{venture_id}/site/local")
def local_status(venture_id: str, caller=_CALLER):
    try:
        workspace_id, actor = _site_scope(venture_id, caller)
        from app.conversion import waitlist
        from app.state import db
        with db()() as session:
            name = waitlist._venture(session, venture_id, workspace_id).name
        publication = _publication(venture_id, workspace_id)
        can_take_offline = any(_accepted_publications(publication))
        if publication:
            publication.pop("prior_publications", None)
            if can_take_offline and publication.get("state") == "offline":
                publication["state"] = "takedown_pending"
        ready = enabled("publish_pages")
        try:
            destination = waitlist_destination(venture_id, name)
        except LocalPublishError as error:
            destination = {"mode": "setup", "url": "", "notice": str(error)}
        next_step = (blocked_message("publish_pages") if not ready else destination["notice"]
                     if destination["mode"] == "setup" else "Open Website and publish your page.")
        return {"publication": publication, "ready": ready and destination["mode"] != "setup", "can_take_offline": can_take_offline,
            "verification_pending": _publication_attempt(publication).get("state") == "verification_pending",
            "next_step": next_step, "destination": destination}
    except (HostingError, ValueError, KeyError, OSError) as error:
        _error(error)


@router.post("/ventures/{venture_id}/site/local/publish")
def publish_current(venture_id: str, body: SitePublishBody, caller=_CALLER):
    try:
        return publish_site(venture_id, caller, job_id=body.job_id, variant=body.variant, app_id=body.app_id, build_id=body.build_id)
    except (HostingError, ValueError, KeyError, OSError) as error:
        _error(error)


@router.get("/ventures/{venture_id}/site/local/export.zip")
def export_current(venture_id: str, job_id: str | None = None, variant: int = Query(0, ge=0, le=2), caller=_CALLER):
    try:
        workspace_id, actor = _site_scope(venture_id, caller)
        files, notice = (_load(venture_id, job_id, variant, caller, deployed=False) if job_id
            else _preview_files(venture_id, workspace_id, actor, deployed=False, variant=variant))
        return Response(export_zip(files), media_type="application/zip", headers={"Cache-Control": "no-store",
            "Content-Disposition": 'attachment; filename="landing-page.zip"', "X-Page-Notice": notice})
    except (HostingError, ValueError, KeyError, OSError) as error:
        _error(error)


@router.get("/ventures/{venture_id}/site/local/export.html")
def export_current_html(venture_id: str, job_id: str | None = None, variant: int = Query(0, ge=0, le=2), caller=_CALLER):
    try:
        workspace_id, actor = _site_scope(venture_id, caller)
        if job_id:
            files, notice = _load(venture_id, job_id, variant, caller, deployed=False)
        else:
            files, notice = _preview_files(venture_id, workspace_id, actor, deployed=False, variant=variant)
        index = next(item for item in files if item["path"] == "index.html")
        body = index["body"]
        _validate_files([_file("index.html", body, "text/html; charset=utf-8")])
        # shortcut: HTML downloads keep companion links; export ZIP when you need a portable bundle.
        return Response(body, media_type="text/html", headers={"Cache-Control": "no-store",
            "Content-Disposition": 'attachment; filename="landing.html"',
            "X-Page-Notice": notice + " Export ZIP from Website to include your companion pages and assets."})
    except (HostingError, ValueError, KeyError, OSError) as error:
        _error(error)


def _pending_public_url(receipt):
    settings = _settings()
    if any(receipt.get(key) != settings.get(key) for key in ("provider", "project")):
        raise LocalPublishError("Open Settings > Keys and connections > Publishing, select the provider and project you used to publish, then retry from Website.")
    return _poll_deployment(receipt["provider"], receipt["deployment_id"], _required(settings, "token"))


def unpublish(receipt):
    _require_local()
    url = receipt["url"]
    _https_url(url)
    settings = _settings()
    provider = receipt["provider"]
    fields = ("bucket", "region", "prefix") if provider == "s3" else ("project",)
    if provider != settings.get("provider") or any(receipt.get(key) != settings.get(key) for key in fields):
        raise LocalPublishError("Open Settings > Keys and connections > Publishing and restore the account and destination used for this page before taking it offline.")
    identity = receipt.get("deployment_id", "")
    if provider in {"vercel", "netlify"}:
        if not isinstance(identity, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", identity):
            raise LocalPublishError("Open your hosting account and take this deployment offline. Its saved deployment ID is missing.")
        headers = {"Authorization": "Bearer " + _required(settings, "token")}
        if provider == "vercel":
            _request("DELETE", "https://api.vercel.com/v13/deployments/" + identity, headers=headers)
        else:
            site = _required(settings, "project")
            if not re.fullmatch(r"[A-Za-z0-9_-]+", site):
                raise LocalPublishError("Open Settings > Keys and connections > Publishing and correct your Netlify site ID.")
            current = _request("GET", "https://api.netlify.com/api/v1/sites/" + site, headers=headers)
            current_deployment = current.get("published_deploy")
            if current_deployment and current_deployment.get("id") != identity:
                raise LocalPublishError("Your Netlify site now serves another deployment. Open Netlify > Deploys and review it before taking that page offline.")
            if current_deployment:
                _request("POST", "https://api.netlify.com/api/v1/sites/" + site + "/unpublish", headers=headers)
    elif provider == "s3":
        from botocore.exceptions import BotoCoreError, ClientError
        client = _s3_client("s3", region_name=_required(settings, "region"),
            aws_access_key_id=_required(settings, "access_key_id"), aws_secret_access_key=_required(settings, "secret_access_key"),
            aws_session_token=settings.get("session_token") or None)
        try:
            objects = []
            for item in receipt["files"]:
                key = receipt["prefix"].strip("/") + "/" + item["path"]
                try:
                    from app.credits import metered
                    head = metered.call_sync(lambda: client.head_object(Bucket=receipt["bucket"], Key=key), purpose="site.publish", estimate_micro=0)
                except ClientError as error:
                    if error.response.get("Error", {}).get("Code") in {"404", "NoSuchKey", "NotFound"}:
                        continue
                    raise
                if head.get("Metadata", {}).get("sha256") != item["sha256"]:
                    raise LocalPublishError("Your bucket now contains a changed page. Open your S3 bucket and review that folder before taking it offline.")
                objects.append((key, head["ETag"]))
            for key, etag in objects:
                from app.credits import metered
                metered.call_sync(lambda: client.delete_object(Bucket=receipt["bucket"], Key=key, IfMatch=etag), purpose="site.publish", estimate_micro=0)
        except (BotoCoreError, ClientError, KeyError):
            raise LocalPublishError("Your S3 page could not be taken offline. Open your bucket, check this folder and your delete permission, then retry from Website.") from None
    else:
        raise LocalPublishError("Open Settings > Keys and connections > Publishing and restore your publishing provider.")
    try:
        with httpx.Client(timeout=10, follow_redirects=False, trust_env=False) as client:
            response = get_checked(client, url, max_redirects=0, https_only=True)
        if response.status_code not in {403, 404, 410}:
            raise ValueError("still_served")
    except (httpx.HTTPError, ValueError, OSError):
        raise LocalPublishError("Your provider received the offline request, but the public URL is not confirmed offline. Open your hosting account, check its status, then retry from Website.") from None


@router.post("/ventures/{venture_id}/site/local/unpublish")
def unpublish_current(venture_id: str, caller=_CALLER):
    try:
        workspace_id, _actor = _site_scope(venture_id, caller)
        publication = _publication(venture_id, workspace_id)
        error = None
        for receipt in _accepted_publications(publication):
            try:
                if not receipt.get("url"):
                    if receipt.get("state") != "verification_pending" or receipt.get("provider") not in {"vercel", "netlify"}:
                        raise LocalPublishError("Choose Check public URL on Website, or take the deployment offline in your hosting account.")
                    receipt["url"] = _pending_public_url(receipt)
                    _save_publication(venture_id, workspace_id, receipt)
                unpublish(receipt)
            except DeploymentRejected:
                pass
            except LocalPublishError as failure:
                error = error or failure
                continue
            _save_publication(venture_id, workspace_id, {**receipt, "state": "offline"})
            if receipt.get("local_deployment_id"):
                from app.db.supymem_models import Deployment
                from app.state import db
                with db()() as session:
                    deployment = session.get(Deployment, receipt["local_deployment_id"])
                    if deployment and (deployment.venture_id, deployment.workspace_id) == (venture_id, workspace_id):
                        deployment.state = "offline"
                        session.commit()
        if error:
            raise error
        if any(_accepted_publications(_publication(venture_id, workspace_id))):
            raise LocalPublishError("Another publication was accepted. Reload Website and choose Take offline again.")
        url = (publication or {}).get("url")
        return {"state": "offline", **({"url": url} if url else {})}
    except (HostingError, ValueError, KeyError, OSError) as error:
        _error(error)


@router.post("/ventures/{venture_id}/site/local/verify")
def verify_current(venture_id: str, caller=_CALLER):
    try:
        workspace_id, actor = _site_scope(venture_id, caller)
        receipt = _publication_attempt(_publication(venture_id, workspace_id))
        if not receipt or receipt.get("state") in {"offline", "failed"}:
            raise LocalPublishError("Open Website and publish your page before checking its public URL.")
        if not receipt.get("url") and receipt.get("state") == "verification_pending" and receipt.get("provider") in {"vercel", "netlify"}:
            try:
                receipt["url"] = _pending_public_url(receipt)
            except DeploymentRejected as error:
                receipt["state"] = "failed"
                _save_publication(venture_id, workspace_id, receipt)
                return {"state": "failed", "notice": str(error)}
            _save_publication(venture_id, workspace_id, receipt)
        _verify_receipt(receipt)
        receipt["state"] = "live"
        _save_publication(venture_id, workspace_id, receipt)
        if receipt.get("local_deployment_id"):
            from app.db.supymem_models import Deployment
            from app.state import db
            from core.clock import utcnow
            with db()() as session:
                deployment = session.get(Deployment, receipt["local_deployment_id"])
                if deployment and (deployment.venture_id, deployment.workspace_id) == (venture_id, workspace_id):
                    deployment.state, deployment.url, deployment.error = "live", receipt["url"], None
                    deployment.live_at = deployment.live_at or utcnow()
                    session.commit()
        return {"state": "live", "url": receipt["url"]}
    except (HostingError, ValueError, KeyError, OSError) as error:
        _error(error)


class ImportSignupsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    emails: list[str]
    consent: Literal[True]


@router.post("/ventures/{venture_id}/site/local/signups")
def import_signups(venture_id: str, body: ImportSignupsBody, caller=_CALLER):
    from app.conversion import waitlist
    from app.db.supymem_models import VentureWaitlist
    from app.state import db
    try:
        workspace_id, actor = _site_scope(venture_id, caller)
        if not 1 <= len(body.emails) <= 100:
            raise LocalPublishError("In Website > Collect signups, enter between 1 and 100 consented email addresses per import.")
        emails = [waitlist.clean_email(email) for email in body.emails]
        with db()() as session:
            name = waitlist._venture(session, venture_id, workspace_id).name
            configured = session.get(VentureWaitlist, venture_id)
            waitlist_enabled = bool(configured and configured.enabled)
        waitlist.configure(venture_id, workspace_id, enabled=waitlist_enabled, actor=actor)
        imported = 0
        for email in emails:
            _, created = waitlist.capture(venture_id, {"email": email, "consent": True, "consent_version": waitlist.VERSION,
                "consent_digest": hashlib.sha256(waitlist.consent_text(name).encode()).hexdigest()}, with_new=True, founder_import=True)
            imported += int(created)
        return {"imported": imported, "note": "Your consented signups are saved locally. Open Website > Collect signups to review them."}
    except (ValueError, KeyError, OSError) as error:
        if isinstance(error, waitlist.WaitlistError):
            error = LocalPublishError("Your import stopped. In Website > Collect signups, correct the email addresses and confirm that each person consented to early-access updates, then retry. Previously saved signups are kept.")
        _error(error)
