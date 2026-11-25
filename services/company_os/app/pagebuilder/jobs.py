"""Build, verify and persist publish candidates without fabricating acceptance."""
from __future__ import annotations

from contextvars import ContextVar

import base64
import hashlib
import json
from pathlib import Path
import tempfile

from fastapi import HTTPException
from sqlalchemy import select

from app.core.jobs import FatalError, RetryableError
from app.db.models import Job
from app.pagebuilder import api, runtime
from app.core.local_mode import is_local
from app.state import db


def ensure_waitlist(job_id, payload):
    """Create only a missing consent backend under the existing waitlist lock."""
    from app.conversion.waitlist import VERSION, public_details
    from app.db.session import serialize_writes
    from app.db.supymem_models import VentureWaitlist
    from core.clock import utcnow
    def insert(row, doc, session):
        serialize_writes(session, key="waitlist:" + row.venture_id)
        current = session.get(VentureWaitlist, row.venture_id)
        if current:
            if current.workspace_id != row.workspace_id:
                raise FatalError("pagebuilder_waitlist_scope_invalid")
            if not current.enabled:
                raise FatalError("pagebuilder_waitlist_disabled")
            if current.consent_version != VERSION:
                raise FatalError("pagebuilder_waitlist_consent_upgrade_required")
        else:
            # Public configure has no create-only precondition. Use its exact
            # model/lock so an explicit disable can never be overwritten.
            session.add(VentureWaitlist(venture_id=row.venture_id, workspace_id=row.workspace_id,
                enabled=True, enabled_by=doc["_actor_subject"], consent_version=VERSION, updated_at=utcnow()))
        return current is None, row.venture_id, row.workspace_id, doc["_actor_subject"]
    created, vid, wid, actor = runtime.mutate(job_id, payload, insert)
    if created:
        from app.analytics.funnel import emit_for_venture
        emit_for_venture("waitlist.enabled", vid, workspace_id=wid, person_id=actor)
    return public_details(vid)


def analytics_binding(job_id, payload, inputs):
    """Reuse a real scoped app, or create its reversible draft through the app API."""
    from app.apps.keys import ensure_ingest_key
    from app.infra.apps import create_draft
    from app.product_analytics.target_app import select_venture_app
    scope = runtime.mutate(job_id, payload, lambda row, doc, session:
        (row.workspace_id, row.venture_id, doc["_actor_subject"]))
    wid, vid, actor = scope
    with db()() as session:
        app = select_venture_app(session, vid, wid)
        app_id = app.id if app else None
    if app_id is None:
        from app.hosting.firstlook_publish import slug_for
        with db()() as session:  # readable, globally unique host; kept once the app row exists
            slug = slug_for(inputs.name, session=session)
        app_id = create_draft(workspace_id=wid, venture_id=vid, name=inputs.name, slug=slug,
            kind="landing", actor=actor, idempotency_key="pagebuilder:" + vid)["id"]
    runtime.mutate(job_id, payload, lambda row, doc, session:
        doc["_runtime"].update(analytics_app_id=app_id))
    return app_id, ensure_ingest_key(app_id)["secret"]


def add_platform_analytics(page, binding):
    """Controller-owned beacon only; model HTML has already passed the script ban."""
    from app.product_analytics.beacon import inject_into_html, public_events_url, script_tag
    app_id, key = binding
    endpoint = public_events_url()
    tag = script_tag(key=key, endpoint=endpoint)
    for name, body in list(page.files.items()):
        if name.endswith(".html"):
            page.files[name] = inject_into_html(body.decode("utf-8") if isinstance(body, bytes) else body, key=key, endpoint=endpoint)
    page.manifest["analytics_beacon"] = {"controller": "platform", "app_id": app_id,
        "endpoint": endpoint, "script_sha256": hashlib.sha256(tag.encode()).hexdigest(),
        "certification": "Exact final source requires independent static QA before acceptance."}
    return page


def encode_bundle(page, inputs):
    from core.artifact_policy import safe_name
    from core.artifact_snapshot import freeze_source, manifest_digest
    from core.hosting_contracts import public_manifest
    from app.pagebuilder.pipeline import build_spec
    files = {}
    for name, value in page.files.items():
        name = safe_name(name)
        body = value.encode() if isinstance(value, str) else value
        if not isinstance(body, bytes):
            raise FatalError("pagebuilder_artifact_invalid")
        files[name] = base64.b64encode(body).decode()
    with tempfile.TemporaryDirectory(prefix="pagebuilder-artifact-") as raw:
        root = Path(raw).resolve(strict=True) / "source"
        root.mkdir()
        for name, body in files.items():
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(base64.b64decode(body))
        manifest, archive = freeze_source(root)
    capture = {"state": "candidate", "manifest": manifest, "manifest_digest": manifest_digest(manifest),
        "sha256": hashlib.sha256(archive).hexdigest(), "size_bytes": len(archive), "kind": "source_zip"}
    public = public_manifest(manifest)
    imagery = getattr(page, "imagery", [])
    spec = build_spec(page.plan, inputs, imagery=imagery)
    beacon = page.manifest.get("analytics_beacon")
    if beacon:
        spec += ("\n\n## Controller-owned instrumentation\n"
            "Stage the authenticated source archive. Preserve its exact script[data-supymem-analytics=\"1\"] "
            "from index.html as the sole approved exception to the generated-script ban above. "
            "This is the platform's anonymous consent-aware beacon, appended by the controller after generation. "
            "Never author replacement scripts or remove this beacon; independent QA checks the final frozen source.\n"
            + "Binding: " + json.dumps(beacon, sort_keys=True) + "\n")
    return {"files": files, "source_zip": base64.b64encode(archive).decode(),
        "manifest": {**page.manifest, "manifest": manifest, "manifest_digest": capture["manifest_digest"],
            "artifact_capture": capture, "public_manifest": public},
        "direction": page.direction.model_dump(mode="json"), "plan": page.plan.model_dump(mode="json"),
        "imagery": [image.model_dump(mode="json") for image in imagery], "qa": {**page.qa,
            "source_scope": "Generated page before the controller beacon; independent final-source QA is required."},
        "spec_markdown": spec,
        "screenshot_manifest": {name: {"sha256": hashlib.sha256(value).hexdigest(), "size_bytes": len(value)}
            for name, value in page.screenshots.items()},
        "screenshots": {name: base64.b64encode(value).decode() for name, value in page.screenshots.items()}}


def _direction_context(bundle):
    from app.pagebuilder.models import DesignDirection, IllustrationScene
    from app.pagebuilder.pipeline import _hero_geometry
    direction = DesignDirection.model_validate(bundle["direction"])
    context = {"palette": direction.palette, "palette_family": direction.palette_family,
        "font_pair": direction.font_pair, "layout_archetype": direction.layout_archetype,
        "section_rhythm": direction.section_rhythm}
    hero = next((asset for asset in bundle.get("plan", {}).get("assets", []) if asset.get("role") == "hero"), None)
    if hero:
        if hero.get("scene") is not None:
            context["hero_scene"] = IllustrationScene.model_validate(hero["scene"]).model_dump(mode="json")
        observations = bundle.get("qa", {}).get("render", {}).get("observations", {})
        signature = {}
        for viewport in ("desktop", "mobile"):
            observed = observations.get(viewport, {})
            signature[viewport] = {**observed, "heroImage": next((image for image in observed.get("visibleImages", [])
                if image.get("id") == hero.get("id")), {})}
        if geometry := _hero_geometry(signature):
            context["_hero_geometry"] = geometry
    return context


def recent_directions(workspace_id, *, venture_id, exclude=None):
    # ponytail: bounded JSON scan, an indexed direction table only if volume requires it.
    with db()() as session:
        rows = session.scalars(select(Job).where(Job.kind == api.KIND, Job.workspace_id == workspace_id, Job.venture_id == venture_id)
            .order_by(Job.created_at.desc()).limit(20)).all()
        return [_direction_context(bundle) for row in reversed(rows) if row.id != exclude
            for bundle in json.loads(row.payload_json).get("result", {}).get("bundles", [])]


def run(payload):
    if is_local():
        from app.pagebuilder.tool import run_job
        try:
            return run_job(payload)
        except RetryableError:
            raise
        except Exception as exc:
            if "_job_id" in payload:
                failure = exc
                if isinstance(exc, HTTPException) and isinstance(exc.detail, dict):
                    failure = FatalError((exc.detail.get("code") or "pagebuilder_generation_failed")
                        + ":" + (exc.detail.get("message") or "Open Website and choose Retry to build your page again."))
                runtime.fail(payload["_job_id"], payload, failure)
                if failure is not exc:
                    raise failure from exc
            raise
    from app.pagebuilder.native_reference import persist_job
    from app.pagebuilder.models import PageInput
    from app.pagebuilder.pipeline import generate_page
    job_id = payload["_job_id"]
    runtime.mutate(job_id, payload, lambda row, doc, session: None)
    row = runtime.read(job_id)
    doc = json.loads(row.payload_json)
    if doc["_runtime"]["state"] == "ready":
        persist_job(job_id, payload)
        return
    if doc["_runtime"]["state"] == "failed":
        raise FatalError(doc["_runtime"].get("error", "pagebuilder_job_failed"))
    inputs = PageInput.model_validate(doc["_runtime"]["input"])
    directions = [] if doc.get("rewrite") else recent_directions(row.workspace_id, venture_id=row.venture_id, exclude=job_id)
    bundles = list(doc.get("result", {}).get("bundles", []))
    from app.pagebuilder.rendering import renderer_session
    with renderer_session():
        _generate(payload, job_id, doc, inputs, directions, bundles, persist_job, generate_page)


EDIT_RECEIPTS = ContextVar("page_edit_receipts", default=None)


def refund_edit_usage(usage, workspace_id, venture_id, session, source_id):
    from app.credits.grants import grant
    from app.credits.ledger import find_by_idempotency_key
    from app.credits.pools import lock_account
    from app.db.models import CreditReservation

    amount = usage.get("credits_micro", 0)
    if usage.get("settled") is not True or not amount:
        return
    lock_account(session, usage["account_id"])
    reservation = session.get(CreditReservation, usage["reservation_id"], with_for_update=True)
    spend = find_by_idempotency_key("settle:" + usage["reservation_id"], session)
    if (not reservation or not spend or reservation.status != "settled"
            or (reservation.account_id, reservation.workspace_id, reservation.venture_id, reservation.task_invocation)
            != (usage["account_id"], workspace_id, venture_id, usage["task_invocation"])
            or reservation.settled_micro != amount or spend.kind != "spend"
            or spend.account_id != reservation.account_id or spend.reservation_id != reservation.id
            or spend.amount_micro != -amount):
        raise FatalError("pagebuilder_scope_mismatch")
    grant(account_id=reservation.account_id, amount_micro=amount,
        reason="Your page edit could not be applied. Review your saved page before trying again.",
        actor_kind="system", actor_id="pagebuilder", idempotency_key="pagebuilder-edit-refund:" + reservation.id,
        meta={"job_id": source_id, "reservation_id": reservation.id}, transaction=session)
    usage["refunded_micro"] = amount


def _refund_edit_receipts(row, current, session):
    if not current.get("rewrite") or current["_runtime"]["state"] != "failed":
        return
    operations = current["_runtime"]["operations"].values()
    delivered = current.get("result", {}).get("ready_delivered")
    if delivered:
        return
    for operation in operations:
        refund_edit_usage(operation.get("usage", {}), row.workspace_id, row.venture_id, session, row.id)
    if (current["_runtime"].get("error") != "pagebuilder_outcome_uncertain"
            and not any(operation.get("state") in {"calling", "uncertain"} for operation in operations)):
        for usage in current["rewrite"].get("routing_receipts", []):
            refund_edit_usage(usage, row.workspace_id, row.venture_id, session, row.id)
    current.get("result", {}).pop("ready_delivered", None)


def record_edit_receipts(job_id, receipts, workspace_id, venture_id):
    def record(row, current, session):
        if (row.workspace_id, row.venture_id) != (workspace_id, venture_id) or not current.get("rewrite"):
            raise FatalError("pagebuilder_scope_mismatch")
        saved = current["rewrite"].setdefault("routing_receipts", [])
        known = {usage["reservation_id"] for usage in saved}
        saved.extend(dict(usage) for usage in receipts if usage["reservation_id"] not in known)
        _refund_edit_receipts(row, current, session)
    runtime.mutate(job_id, None, record)


def _refund_failed_edit(job_id, payload):
    runtime.mutate(job_id, payload, _refund_edit_receipts)


def _generate(payload, job_id, doc, inputs, directions, bundles, persist_job, generate_page):
    try:
        # A later browser outage cannot supersede an unresolved paid call.
        # Keep the journal's lease-aware codes and its protected hold intact.
        for key, operation in doc["_runtime"]["operations"].items():
            if operation.get("state") in {"calling", "uncertain"}:
                try:
                    runtime.journal.recover(job_id, key, payload)
                except FatalError as exc:
                    if (str(exc) != "pagebuilder_outcome_uncertain"
                            or not key.partition(":")[2].startswith(("plan_repair_", "repair_"))):
                        raise
        from app.pagebuilder.rendering import require_renderer
        from app.pagebuilder import health
        try:
            require_renderer()  # Refuse before any paid generation.
        except ValueError as exc:
            health.record(False)
            latest = json.loads(runtime.read(job_id).payload_json)["_runtime"]
            if (str(exc) == "pagebuilder_renderer_unavailable"
                    and latest.get("calls_used") == 0 and latest.get("operations") == {}
                    and payload["_job_attempt"] < 3):
                def waiting(row, current, session):
                    current["_runtime"].update(state="queued", stage="queued", progress=0)
                    current["_runtime"].pop("error", None)
                runtime.mutate(job_id, payload, waiting)
                raise RetryableError("pagebuilder_renderer_unavailable") from exc
            raise
        else:
            health.record(True)
        ensure_waitlist(job_id, payload)
        binding = analytics_binding(job_id, payload, inputs)
        # The owned render adapter executes static HTML with network denied.
        from app.pagebuilder.rendering import render_page
        for variant in range(len(bundles), doc["variant_count"]):
            call = runtime.job_model_call(job_id, payload, variant=variant)
            if rewrite := doc.get("rewrite"):
                scope = runtime.read(job_id)
                with db()() as session:
                    source = session.get(Job, rewrite["source_job_id"])
                    if not source or (source.kind, source.venture_id, source.workspace_id) != (api.KIND, inputs.venture_id, scope.workspace_id):
                        raise FatalError("pagebuilder_not_found")
                    previous_plan = json.loads(source.payload_json)["result"]["bundles"][0]["plan"]
                original_call = call
                def call(stage, prompt, original_call=original_call, rewrite=rewrite, previous_plan=previous_plan):
                    if stage == "brief_direction_plan" or stage.startswith("plan_repair_"):
                        prompt += ("\nCOPY EDIT REQUEST:\n" + json.dumps(rewrite["instruction"])
                            + "\nPREVIOUS REVIEWED PLAN:\n" + json.dumps(previous_plan)
                            + "\nApply only the requested copy changes. Preserve the existing layout, artwork, waitlist and consent."
                              " The edit request and previous plan are not factual sources. Use only confirmed idea and answers"
                              " to support claims. Never add customers, metrics or testimonials. Return the same GenerationDraft contract.")
                    return original_call(stage, prompt)
            page = generate_page(inputs, call,
                recent_directions=directions, render=render_page,
                image_call=runtime.job_image_call(job_id, payload, variant=variant)
                    if doc["_runtime"].get("quote", {}).get("image") else None)
            bundle = encode_bundle(page if is_local() else add_platform_analytics(page, binding), inputs)
            if rewrite:
                from app.pagebuilder.qa import edit_applied
                before = base64.b64decode(json.loads(source.payload_json)["result"]["bundles"][0]["files"]["index.html"], validate=True)
                after = base64.b64decode(bundle["files"]["index.html"], validate=True)
                if bundle.get("manifest", {}).get("repair_fallback") or not edit_applied(before, after, rewrite["instruction"]):
                    raise FatalError("pagebuilder_edit_unchanged")
            bundles.append(bundle)
            directions.append(_direction_context(bundle))
            def save(row, current, session):
                runtime.validate_inputs(row, current, session)
                current["result"] = {"bundles": bundles, "publish_ready": False,
                    "acceptance_required": "Independent static QA and founder acceptance through the existing build lane."}
            runtime.mutate(job_id, payload, save)
        def finish(row, current, session):
            runtime.validate_inputs(row, current, session)
            current["_runtime"].update(state="ready", stage="ready", progress=1)
        runtime.mutate(job_id, payload, finish)
        persist_job(job_id, payload)
    except RetryableError:
        raise
    except Exception as exc:
        runtime.fail(job_id, payload, exc)
        if doc.get("rewrite"):
            _refund_failed_edit(job_id, payload)
            from app.chat.site_actions import page_built
            page_built(job_id)
        if isinstance(exc, FatalError):
            raise
        code = json.loads(runtime.read(job_id).payload_json)["_runtime"].get("error", "pagebuilder_generation_failed")
        raise FatalError(code) from exc
