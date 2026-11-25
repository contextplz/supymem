"""Publish a firstlook concept page with your saved local account or managed static hosting.

Local requests use the saved publisher and preserve the exact source and signup consent checks.
A hosted firstlook draft is not a QA-accepted build, so it cannot use ``publish_accepted_build``. This module keeps every
other gate of that pipeline and replaces only the source authority:

* the public page is recomposed deterministically from the stored copy and brand kit (never the stored draft bytes,
  never founder-supplied HTML): one index.html, no form, no external asset, one link to the hosted waitlist consent page;
* the copy must pass the first-look linter and the public-copy review (``public_copy_observation``);
* the venture waitlist must be on and have a passing submission test in the last 24 hours (``waitlist.verified_receipt``);
* the founder's confirmation binds the exact sha256 of the page that will be published (audit record
  ``hosting.firstlook_publication_accepted``); any change before the upload refuses;
* objects are written with ``AwsStaticPublisher.put_files`` (immutable prefix, checksum, head verification), the hostname
  switches only after that, and ``verify_public`` reads the landing page back over HTTPS and compares its hash;
* only then are the deployment, the domain and the ``SiteVersion`` marked live/published. Any failure restores the
  previous mapping and leaves the version unpublished.

The deployment row records ``build_artifact_id = "firstlook:<site_version_id>"``; no build, artifact or QA record is faked.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from typing import Any

from core.hosting_contracts import HostingRejected, host_mapping, public_hostname
from core.ids import new_id
from core.public_copy import public_copy_observation, public_html_text
from core.static_qa_contracts import STATIC_MEDIA
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.audit.writer import audit
from app.core.local_mode import is_local
from app.credits._txn import naive_utc
from app.db.models import SiteVersion
from app.db.session import serialize_writes
from app.db.supymem_models import AppDomain, Deployment, FounderApp
from app.hosting.service import (
    AwsStaticPublisher,
    HostingConfig,
    HostingError,
    _record_site_published,
    _serialize,
)
from app.state import db

SOURCE_PREFIX = "firstlook:"
BUILD_MODE = "firstlook"
_BUSY = ("pending", "packaging", "pushing", "creating_infra", "health_checking")


def source_id(site_version_id: str) -> str:
    return SOURCE_PREFIX + site_version_id


# ---------------------------------------------------------------- the page that would be published

def _local_files(version):
    from app.pagebuilder.local_publish import _file, _validate_files
    from pathlib import PurePosixPath

    snapshot = (version.copy or {}).get("_render_snapshot") or {}
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("files", {}), dict):
        raise HostingError("Your saved page files could not be read safely. Open Settings > Keys and connections > Your data folder and backup, restore your backup, then reopen Website.")
    html = snapshot.get("html")
    if not isinstance(html, str) or not html:
        raise HostingError("Open Settings > Keys and connections > Coding tools, connect and allow your coding tool, then build your page from Website before exporting or publishing.")
    body = html.encode("utf-8")
    if hashlib.sha256(body).hexdigest() != version.sha256:
        raise HostingError("hosting_firstlook_draft_changed")
    if len(body) > 512 * 1024:
        raise HostingError("Open Website and ask your coding tool to reduce your public page size, then export or publish the checked result.")
    files = [_file("index.html", body, "text/html; charset=utf-8")]
    for path, content in (snapshot.get("files") or {}).items():
        suffix = PurePosixPath(path).suffix.lower()
        if path == "index.html" or suffix not in STATIC_MEDIA or not isinstance(content, str):
            raise HostingError("hosting_firstlook_page_rejected")
        files.append(_file(path, content.encode("utf-8"), STATIC_MEDIA[suffix]))
    _validate_files(files)
    return files


def public_page(venture_id: str, *, version_id: str | None = None) -> dict[str, Any]:
    """Recompose the public variant of the venture's newest firstlook draft; raise HostingError when it cannot be published."""
    from app.conversion import waitlist
    from app.firstlook import versions
    from app.firstlook.compose import _EXTERNAL, compose_html, lint_html
    from app.firstlook.lint import lint_copy
    from app.firstlook.service import _workspace_and_idea

    wid, idea, _name = _workspace_and_idea(venture_id)
    if not wid:
        raise HostingError("hosting_app_not_found", status=404)
    sv = versions.get_latest(wid, venture_id=venture_id)
    if sv is None or sv.venture_id != venture_id or (version_id and sv.id != version_id):
        raise HostingError("hosting_firstlook_draft_changed" if version_id else "hosting_firstlook_draft_required")
    if is_local():
        files = _local_files(sv)
        body = files[0]["body"]
        html = body.decode("utf-8")
        if public_copy_observation(public_html_text(html))[0]:
            raise HostingError("Open Website and ask your coding tool to correct your public page copy and size, then publish the checked result.")
        return {"site_version_id": sv.id, "version": sv.version, "sha256": sv.sha256,
            "html": html, "body": body, "files": files, "headline": (sv.copy or {}).get("headline"),
            "subhead": (sv.copy or {}).get("subhead"), "waitlist_url": waitlist.public_url(venture_id),
            "workspace_id": wid, "name": _name}
    copy = {k: v for k, v in (sv.copy or {}).items() if k != "_render_snapshot"}
    if sv.template == "local_builder" or not copy.get("headline") or not isinstance(copy.get("points"), list):
        raise HostingError("hosting_firstlook_draft_unsupported")  # hand-made or ingested HTML is never republished
    verdict = lint_copy(copy, idea_text=idea)
    if not verdict.ok:
        raise HostingError("hosting_firstlook_copy_rejected")
    try:
        link = waitlist.public_url(venture_id)
    except waitlist.WaitlistError as exc:
        raise HostingError("hosting_conversion_verification_required") from exc
    html, _template, _plan = compose_html(
        brand=sv.brandkit, copy=copy, idea_text=idea, version=sv.version, stage=sv.stage,
        variant_index=sv.variant_index, archetype=sv.archetype, logo_url=None,
        generated_on=str(sv.created_at)[:10], public_waitlist_url=link,
    )
    # The single permitted external reference is the hosted waitlist consent page.
    if html.count(f'href="{link}"') != 1 or "<form" in html.lower():
        raise HostingError("hosting_conversion_link_required")
    if lint_html(html.replace(f'href="{link}"', 'href="#"')) or _EXTERNAL.search(html.replace(f'href="{link}"', 'href="#"')):
        raise HostingError("hosting_firstlook_page_rejected")
    findings, _ = public_copy_observation(public_html_text(html))
    if findings:
        raise HostingError("hosting_public_copy_unreviewed")
    body = html.encode("utf-8")
    if len(body) > 512 * 1024:
        raise HostingError("hosting_firstlook_page_rejected")
    return {"site_version_id": sv.id, "version": sv.version, "sha256": hashlib.sha256(body).hexdigest(),
            "html": html, "body": body, "headline": copy.get("headline"), "subhead": copy.get("subhead"),
            "waitlist_url": link, "workspace_id": wid, "name": _name}


def _waitlist_receipt(venture_id: str, workspace_id: str) -> dict[str, Any]:
    from app.conversion import waitlist

    with db()() as session:
        try:
            return waitlist.verified_receipt(session, venture_id, workspace_id)
        except waitlist.WaitlistError as exc:
            raise HostingError("hosting_conversion_verification_required") from exc


# ---------------------------------------------------------------- app / intent

def slug_for(name: str, *, session: Session) -> str:
    from app.infra.apps import free_slug

    base = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")[:40].strip("-")
    for candidate in (base, f"{base}-site" if base else "", f"site-{new_id('s')[-8:]}"):
        if not candidate:
            continue
        try:
            public_hostname(candidate, "supymem.com")
            return free_slug(candidate, session=session)
        except HostingRejected:
            continue
    raise HostingError("hosting_slug_unavailable")


def _generated_slug_conflict(exc: IntegrityError) -> bool:
    """Retry only the global address constraint, never an unrelated failed insert."""
    original = exc.orig
    if getattr(getattr(original, "diag", None), "constraint_name", None) == "apps_slug_key":
        return True
    return isinstance(original, sqlite3.IntegrityError) and str(original) == "UNIQUE constraint failed: apps.slug"


def _ensure_app(venture_id: str, workspace_id: str, actor: str) -> str:
    from app.builder.access import authorize

    with db()() as session:
        serialize_writes(session, key="firstlook-app:" + venture_id)
        try:
            venture = authorize(session, workspace_id=workspace_id, venture_id=venture_id, actor=actor, mutate=True)
        except KeyError as exc:
            raise HostingError("hosting_app_not_found", status=404) from exc
        except PermissionError as exc:
            raise HostingError(str(exc), status=403) from exc
        if venture.archived_at is not None:
            raise HostingError("hosting_app_not_found", status=404)
        app = session.scalar(select(FounderApp).where(
            FounderApp.venture_id == venture_id, FounderApp.workspace_id == workspace_id,
            FounderApp.build_mode == BUILD_MODE, FounderApp.deleted_at.is_(None)))
        if app is None:
            now = naive_utc()
            for _attempt in range(100):
                try:
                    # A different venture can select the same generated address. Keep
                    # authorization in this transaction, and let the unique constraint
                    # decide the winner without aborting the outer transaction.
                    with session.begin_nested():
                        app = FounderApp(id=new_id("app"), workspace_id=workspace_id, venture_id=venture_id,
                                         slug=slug_for(venture.name, session=session),
                                         name=(venture.name or "Landing page")[:120], kind="static_site",
                                         state="draft", build_mode=BUILD_MODE, deploy_target="supymem",
                                         created_at=now, updated_at=now)
                        session.add(app)
                        session.flush()
                except IntegrityError as exc:
                    if not _generated_slug_conflict(exc):
                        raise
                else:
                    session.commit()
                    break
            else:
                raise HostingError("hosting_slug_unavailable")
        return app.id


def queue_publication(venture_id: str, *, actor: str, site_version_id: str, public_sha256: str) -> dict[str, Any]:
    """Publish the requested local page or persist and enqueue a managed-publication intent."""
    if is_local():
        from app.pagebuilder.local_publish import LocalPublishError, publish_site

        try:
            return {**publish_site(venture_id, actor, site_version_id=site_version_id, public_sha256=public_sha256), "job_id": None}
        except LocalPublishError as exc:
            raise HostingError(str(exc), status=409) from None
    from app.core.jobs import enqueue
    from app.hosting.unpublish import assert_no_takedown

    config = HostingConfig.from_environment()
    page = public_page(venture_id, version_id=site_version_id)
    if page["sha256"] != public_sha256:
        raise HostingError("hosting_firstlook_draft_changed")
    workspace_id = page["workspace_id"]
    _waitlist_receipt(venture_id, workspace_id)
    app_id = _ensure_app(venture_id, workspace_id, actor)
    source = source_id(site_version_id)
    with db()() as session:
        serialize_writes(session, key="static-publish:" + app_id)
        app = session.get(FounderApp, app_id)
        if not app or app.deleted_at is not None or app.build_mode != BUILD_MODE:
            raise HostingError("hosting_app_not_found", status=404)
        assert_no_takedown(session, app)
        hostname = public_hostname(app.slug, config.public_domain)
        domain = session.scalar(select(AppDomain).where(AppDomain.app_id == app.id, AppDomain.kind == "supymem"))
        if domain and domain.hostname != hostname:
            raise HostingError("hosting_domain_conflict")
        rows = session.scalars(select(Deployment).where(Deployment.app_id == app.id)
                               .order_by(Deployment.version.desc())).all()
        # A page live on a previous address is not "already published" at the new one.
        same = next((row for row in rows if row.build_artifact_id == source
                     and ((row.state == "live" and row.url == f"https://{hostname}/") or row.state in _BUSY
                          or row.error == "hosting_public_state_uncertain")), None)
        if same is not None and same.state == "live":
            return {**_serialize(same), "job_id": None}
        if any(row.error == "hosting_public_state_uncertain" for row in rows):
            raise HostingError("hosting_public_state_uncertain")
        if any(row.state in _BUSY and row is not same for row in rows):
            raise HostingError("hosting_publication_in_progress")
        now = naive_utc()
        if same is None:
            previous = next((row for row in rows if row.state == "live"), None)
            same = Deployment(id=new_id("dep"), app_id=app.id, workspace_id=app.workspace_id, venture_id=app.venture_id,
                              build_artifact_id=source, target="supymem_static", state="pending",
                              version=(rows[0].version if rows else 0) + 1,
                              previous_deployment_id=previous.id if previous else None, started_at=now)
            session.add(same)
        if not domain:
            session.add(AppDomain(id=new_id("dom"), app_id=app.id, workspace_id=app.workspace_id, hostname=hostname,
                                  kind="supymem", state="pending", created_at=now))
        session.flush()
        job_id = enqueue(
            session, "deploy.run",
            {"schema_version": 1, "kind": "firstlook", "deployment_id": same.id, "app_id": app.id,
             "venture_id": venture_id, "site_version_id": site_version_id, "public_sha256": public_sha256, "actor": actor},
            workspace_id=app.workspace_id, venture_id=app.venture_id,
            dedupe_key="static-publish:" + same.id, effect_key="static-publish:" + same.id, priority=20, max_attempts=3)
        session.commit()
        result = {**_serialize(same), "job_id": job_id}
        deployment_id = same.id
    audit("hosting.firstlook_publication_accepted", workspace_id=workspace_id, venture_id=venture_id, actor_id=actor,
          actor_kind="person", surface="api", target_ref=deployment_id,
          payload={"app_id": app_id, "site_version_id": site_version_id, "public_sha256": public_sha256,
                   "hostname": hostname, "waitlist_url": page["waitlist_url"]})
    return result


# ---------------------------------------------------------------- the publisher (job handler)

def publish(payload: dict[str, Any], *, provider: AwsStaticPublisher | None = None) -> dict[str, Any]:
    from app.builder.access import authorize
    from app.hosting.unpublish import assert_no_takedown

    deployment_id, app_id = payload["deployment_id"], payload["app_id"]
    venture_id, version_id, sha = payload["venture_id"], payload["site_version_id"], payload["public_sha256"]
    actor = payload["actor"]
    provider = provider or AwsStaticPublisher.configured()
    source = source_id(version_id)
    alias_done: list[tuple[str, str | None]] = []  # retired hosts re-pointed so far, for rollback

    def _intent(session):
        deployment = session.get(Deployment, deployment_id)
        app = session.get(FounderApp, app_id)
        if (not deployment or not app or app.deleted_at is not None or app.build_mode != BUILD_MODE
                or (deployment.app_id, deployment.workspace_id, deployment.venture_id)
                != (app.id, app.workspace_id, venture_id) or app.venture_id != venture_id
                or deployment.build_artifact_id != source or deployment.target != "supymem_static"
                or deployment.state not in (*_BUSY, "failed", "live")):
            raise HostingError("hosting_deployment_intent_invalid")
        try:
            venture = authorize(session, workspace_id=app.workspace_id, venture_id=venture_id,
                                actor=actor, mutate=True)
        except KeyError as exc:
            raise HostingError("hosting_app_not_found", status=404) from exc
        except PermissionError as exc:
            raise HostingError(str(exc), status=403) from exc
        if venture.archived_at is not None:
            raise HostingError("hosting_app_not_found", status=404)
        return deployment, app

    with db()() as session:
        serialize_writes(session, key="static-publish:" + app_id)
        try:
            deployment, app = _intent(session)
        except HostingError as exc:
            denied = session.get(Deployment, deployment_id)
            denied_app = session.get(FounderApp, app_id)
            if (denied and denied_app and denied.state != "live"
                    and denied.error != "hosting_public_state_uncertain"
                    and (denied.app_id, denied.workspace_id, denied.venture_id, denied.build_artifact_id)
                    == (denied_app.id, denied_app.workspace_id, venture_id, source)
                    and denied_app.venture_id == venture_id):
                denied.state, denied.error = "failed", exc.code[:200]
                session.commit()
            raise
        if deployment.state == "live":
            return _serialize(deployment)
        if deployment.error == "hosting_public_state_uncertain":
            raise HostingError("hosting_public_state_uncertain")
        assert_no_takedown(session, app)
        workspace_id, hostname = app.workspace_id, public_hostname(app.slug, provider.config.public_domain)
        version = deployment.version
        deployment.state, deployment.error = "packaging", None
        session.commit()

    try:
        page = public_page(venture_id, version_id=version_id)
        if page["sha256"] != sha:
            raise HostingError("hosting_firstlook_draft_changed")
        _waitlist_receipt(venture_id, workspace_id)
        files = [{"path": "index.html", "sha256": sha, "size_bytes": len(page["body"]), "body": page["body"],
                  "content_type": STATIC_MEDIA[".html"]}]
        with db()() as session:
            session.get(Deployment, deployment_id).state = "pushing"
            session.commit()
        provider.put_files(app_id=app_id, deployment_id=deployment_id, artifact_id=source.replace(":", "_"), files=files)
        with db()() as session:
            session.get(Deployment, deployment_id).state = "creating_infra"
            session.commit()
        mapping = host_mapping(app_id=app_id, deployment_id=deployment_id, generation=version, enabled=True)
        with db()() as session:  # recheck authority right before the public switch
            _, current_app = _intent(session)
            assert_no_takedown(session, current_app)
            previous_live = session.scalar(select(Deployment).where(
                Deployment.app_id == app_id, Deployment.state == "live").order_by(Deployment.version.desc()))
            domain_row = session.scalar(select(AppDomain).where(AppDomain.app_id == app_id, AppDomain.kind == "supymem"))
            # A new address has no mapping yet; a served one (same host, or a retired one we return to) does.
            served = previous_live is not None and (previous_live.url == f"https://{hostname}/"
                                                    or (domain_row is not None and domain_row.active_at is not None))
            previous_id = previous_live.id if served else None
        current_page = public_page(venture_id, version_id=version_id)
        if current_page["sha256"] != sha:
            raise HostingError("hosting_firstlook_draft_changed")
        _waitlist_receipt(venture_id, workspace_id)
        switch_attempted = True
        previous_mapping = (provider.switch(hostname, mapping, expected_previous_deployment_id=previous_id)
                            if previous_id else provider.switch(hostname, mapping, expected_absent=True))
        switch_confirmed = True
        with db()() as session:
            row = session.get(Deployment, deployment_id)
            row.state, row.url, row.health_url = "health_checking", f"https://{hostname}/", f"https://{hostname}/"
            session.commit()
        provider.verify_public(hostname, sha)  # HTTPS readback of the exact bytes
        from app.hosting.site_address import aliases

        for alias_host in aliases(app_id):  # retired addresses keep serving the current page
            alias_done.append((alias_host, provider.switch(alias_host, mapping)))
        final = public_page(venture_id, version_id=version_id)
        if final["sha256"] != sha:
            raise HostingError("hosting_source_changed")
        _waitlist_receipt(venture_id, workspace_id)
        with db()() as session:
            serialize_writes(session, key="static-publish:" + app_id)
            _, current_app = _intent(session)
            assert_no_takedown(session, current_app)
            row = session.get(Deployment, deployment_id)
            old = session.get(Deployment, row.previous_deployment_id) if row.previous_deployment_id else None
            if old and old.state == "live":
                old.state = "superseded"
            domain = session.scalar(select(AppDomain).where(AppDomain.app_id == app_id, AppDomain.kind == "supymem"))
            domain.state, domain.active_at = "active", naive_utc()
            row.state, row.live_at = "live", naive_utc()
            site = session.get(SiteVersion, version_id)
            if site is None or (site.workspace_id, site.venture_id) != (workspace_id, venture_id):
                raise HostingError("hosting_source_changed")
            site.published = 1  # only after the public readback verified
            session.commit()
            session.refresh(row)
            result = _serialize(row)
            publication_venture_id, publication_deployment_id = row.venture_id, row.id
        try:
            from app.ventures.stage import after_evidence
            after_evidence(publication_venture_id, "publication:" + publication_deployment_id)
        except Exception:  # stage projection must never roll back a durable live publication
            import logging
            logging.getLogger(__name__).warning("founder_stage_publication_pending")
        audit("hosting.publication_live", workspace_id=workspace_id, venture_id=venture_id, actor_id=actor,
              actor_kind="person", surface="internal", target_ref=deployment_id,
              payload={"app_id": app_id, "source": source, "url": result["url"], "public_sha256": sha})
        _record_site_published(deployment_id, actor, workspace_id, venture_id)
        from app.chat.milestone_hooks import deployment_live

        deployment_live(venture_id, deployment_id, result["url"])
        return result
    except Exception as exc:
        rollback_error: Exception | None = None
        if "mapping" in locals() and "previous_mapping" in locals():
            try:
                provider.restore(hostname, mapping, previous_mapping)
            except Exception as restore_exc:  # noqa: BLE001
                rollback_error = restore_exc
            for alias_host, alias_previous in alias_done:
                try:
                    provider.restore(alias_host, mapping, alias_previous)
                except Exception as restore_exc:  # noqa: BLE001
                    rollback_error = restore_exc
        code = exc.code if isinstance(exc, HostingError) else str(exc) if isinstance(exc, HostingRejected) else "hosting_provider_failed"
        if rollback_error or (locals().get("switch_attempted") and not locals().get("switch_confirmed")
                              and (code == "hosting_mapping_write_failed" or not isinstance(exc, HostingError))):
            code = "hosting_public_state_uncertain"
        with db()() as session:
            row = session.get(Deployment, deployment_id)
            if row and row.state != "live":
                row.state, row.error = "failed", code[:200]
                session.commit()
        audit("hosting.publication_failed", workspace_id=workspace_id, venture_id=venture_id, actor_id=actor,
              actor_kind="person", surface="internal", target_ref=deployment_id, outcome="failed", denied_reason=code,
              payload={"app_id": app_id, "source": source})
        from app.chat.milestone_hooks import deployment_failed

        deployment_failed(venture_id, deployment_id, code)
        if isinstance(exc, HostingError) and code == exc.code:
            raise
        raise HostingError(code, status=503) from exc


# ---------------------------------------------------------------- read side

def publishable_draft(venture_id: str) -> dict[str, Any] | None:
    """The firstlook draft a founder could publish now (summary only), else None. Never raises."""
    try:
        page = public_page(venture_id)
    except (HostingError, KeyError, ValueError):
        return None
    return {k: page[k] for k in ("site_version_id", "version", "sha256", "headline", "subhead", "waitlist_url")}


def live_hostname_for(venture_id: str) -> str | None:
    with db()() as session:
        row = session.scalar(select(Deployment).join(FounderApp, Deployment.app_id == FounderApp.id).where(
            Deployment.venture_id == venture_id, Deployment.state == "live",
            Deployment.build_artifact_id.like(SOURCE_PREFIX + "%"), FounderApp.build_mode == BUILD_MODE))
        return row.url if row else None


MESSAGES = {
    "hosting_firstlook_draft_required": "There is no landing page draft yet. Build one first.",
    "hosting_firstlook_draft_changed": "The draft changed after you reviewed it. Review the latest landing page and confirm again.",
    "hosting_firstlook_draft_unsupported": "This landing page was not generated by Supymem, so it cannot be published from here.",
    "hosting_firstlook_copy_rejected": "The landing page copy did not pass the honesty checks (it may claim things that do not exist yet). Rewrite it first.",
    "hosting_firstlook_page_rejected": "The landing page did not pass the public-page checks. Rewrite it first.",
    "hosting_public_copy_unreviewed": "The landing page copy still has placeholder or draft wording. Rewrite it first.",
    "hosting_conversion_verification_required": "The waitlist is off or has no passing test in the last 24 hours. Turn it on and run the test first.",
    "hosting_conversion_link_required": "The landing page could not link to your waitlist form.",
    "hosting_provider_unavailable": "Supymem hosting is temporarily unavailable. Your draft is private and safe.",
    "hosting_provider_configuration_invalid": "Supymem hosting is temporarily unavailable. Your draft is private and safe.",
    "hosting_publication_in_progress": "A publication is already in progress.",
    "hosting_unpublish_in_progress": "The landing page is being taken offline right now.",
    "hosting_public_state_uncertain": "I could not confirm whether the public address was restored. Open Website before publishing again.",
}


def plain(code: str) -> str:
    if is_local():
        from app.pagebuilder.local_publish import LocalPublishError, _settings, blocked_message, enabled

        if code.startswith(("Open ", "Your ", "In Settings > ")):
            return code
        if code in {"hosting_provider_unavailable", "hosting_provider_configuration_invalid"}:
            if not enabled("publish_pages"):
                return blocked_message("publish_pages")
            return "Your draft is private and safe. Open Website and publish it with your saved publishing account."
        if code == "hosting_conversion_link_required":
            try:
                settings = _settings()
            except LocalPublishError as exc:
                return str(exc)
            if settings.get("provider") != "netlify" and not settings.get("waitlist_email"):
                return "Open Settings > Keys and connections > Publishing and save your waitlist email, or choose Netlify for built-in forms. Then open Website and publish again."
            return "Open Website and rebuild your latest landing page to attach its consented signup form, then publish again."
        if code == "hosting_public_state_uncertain":
            return "Your upload is not confirmed public. Open Website and choose Check public URL. Check the deployment in your publishing account before publishing again."
        if code == "hosting_firstlook_draft_changed":
            return "Your saved page changed. Open Website, review your latest landing page, then publish again."
        if code == "hosting_publication_in_progress":
            return "Your page is being published. Open Website to check its progress before publishing again."
    return MESSAGES.get(code, "Publishing did not complete. Your draft is saved. Open Website to check the current public state.")


def describe(venture_id: str) -> dict[str, Any]:
    """Founder-facing state of the firstlook publication: live, publishable, or blocked with plain reasons."""
    blockers: list[dict[str, str]] = []
    draft = None
    try:
        page = public_page(venture_id)
        draft = {k: page[k] for k in ("site_version_id", "version", "sha256", "headline", "subhead", "waitlist_url")}
        if not is_local():
            try:
                _waitlist_receipt(venture_id, page["workspace_id"])
            except HostingError as exc:
                blockers.append({"code": exc.code, "message": plain(exc.code)})
    except HostingError as exc:
        blockers.append({"code": exc.code, "message": plain(exc.code)})
    latest = None
    if is_local():
        from app.firstlook.service import _workspace_and_idea
        from app.pagebuilder.local_publish import LocalPublishError, _publication, blocked_message, enabled, waitlist_destination

        try:
            if not enabled("publish_pages"):
                blockers.append({"code": "hosting_provider_unavailable", "message": blocked_message("publish_pages")})
            workspace_id, _idea, name = _workspace_and_idea(venture_id)
            if draft:
                destination = waitlist_destination(venture_id, name)
                if destination["mode"] == "setup":
                    blockers.append({"code": "hosting_conversion_link_required", "message": destination["notice"]})
            receipt = _publication(venture_id, workspace_id) if workspace_id else None
            if receipt:
                source = source_id(receipt["site_version_id"]) if (draft and receipt.get("site_version_id")
                    and receipt.get("public_sha256") == draft["sha256"]) else None
                latest = {**receipt, "source": source, "error": "hosting_public_state_uncertain"
                          if receipt["state"] == "verification_pending" else None}
        except LocalPublishError as exc:
            blockers.append({"code": "hosting_provider_configuration_invalid", "message": str(exc)})
    else:
        try:
            HostingConfig.from_environment()
        except HostingError as exc:
            blockers.append({"code": exc.code, "message": plain(exc.code)})
        with db()() as session:
            row = session.scalar(select(Deployment).join(FounderApp, Deployment.app_id == FounderApp.id).where(
                Deployment.venture_id == venture_id, Deployment.build_artifact_id.like(SOURCE_PREFIX + "%"),
                FounderApp.build_mode == BUILD_MODE).order_by(Deployment.version.desc()))
            if row:
                latest = {"id": row.id, "app_id": row.app_id, "state": row.state, "url": row.url, "error": row.error,
                          "source": row.build_artifact_id}
    live = latest["url"] if latest and latest["state"] == "live" else None
    if latest and latest["state"] in (*_BUSY, "publishing"):
        blockers.append({"code": "hosting_publication_in_progress", "message": plain("hosting_publication_in_progress")})
    if latest and latest["error"] == "hosting_public_state_uncertain":
        blockers.append({"code": "hosting_public_state_uncertain", "message": plain("hosting_public_state_uncertain")})
    state = "live" if live and draft and latest["source"] == source_id(draft["site_version_id"]) else \
        "publishable" if draft and not blockers else "blocked"
    return {"state": state, "live_url": live, "draft": draft, "blockers": blockers, "deployment": latest}


__all__ = ["BUILD_MODE", "MESSAGES", "describe", "plain", "SOURCE_PREFIX", "live_hostname_for", "public_page", "publish", "publishable_draft",
           "queue_publication", "source_id"]
