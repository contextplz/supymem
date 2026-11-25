"""Authenticated, bounded client usage events; never financial evidence."""
import hashlib
import json
import logging

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError

from app.authz.workspace import assert_venture_access, assert_workspace_access
from app.authz.workspace import subject_person_id
from app.core.local_mode import is_local
from app.db.models import AnalyticsEvent, VentureDocument
from app.state import db
from core.clock import utcnow


def record_process_heartbeat(subject, device_id, app_version):
    from app.db.supymem_models import DesktopDevice, BetaCodeRedemption
    from app.db.session import serialize_writes

    with db()() as session:
        serialize_writes(session, key=f"referral-person:{subject_person_id(subject)}")
        device = session.get(DesktopDevice, device_id, with_for_update=True)
        if device is None or device.person_id != subject_person_id(subject) or device.revoked_at is not None:
            raise HTTPException(403, detail={"error": {"code": "authenticated_device_required"}})
        if app_version is not None:
            device.app_version = app_version
        device.last_seen_at = utcnow()
        receipt = session.get(BetaCodeRedemption, device.person_id, with_for_update=True)
        if receipt is not None and receipt.installed_at is None:
            receipt.installed_at = device.last_seen_at
        session.commit()


def capture(body, actor):
    if body.get("context_subject") is not None and body["context_subject"] != actor:
        raise HTTPException(409, detail={"error": {"code": "usage_session_changed"}})
    person_id = subject_person_id(actor)
    stored_person = None if body["event"] in {"ui.dead_end", "journey.step_entered", "journey.step_completed"} else person_id
    wid, vid = body.get("workspace_id"), body.get("venture_id")
    if vid:
        _, actual = assert_venture_access(vid, actor)
        if wid is not None and actual != wid:
            raise HTTPException(404, detail={"error": {"code": "not_found"}})
        wid = actual
    elif wid:
        assert_workspace_access(wid, actor)
    # A saved job can be observed again after a full app restart. Give its
    # closed failure label one durable identity per person and job, while other
    # client events continue to use a nonce only for network retries.
    event_props = dict(body["props"])
    if body["event"] in {"ui.dead_end", "journey.step_entered", "journey.step_completed"}:
        event_props = {key: value for key, value in event_props.items() if value is not None}
    # Preserve the serialized shape of older queued events across rollout.
    if event_props.get("job_id") is None:
        event_props.pop("job_id", None)
    if event_props.get("source") is None:
        event_props.pop("source", None)
    for key in ("error_route", "error_status", "error_server_code", "outcome", "error_source", "error_intent",
                "error_page", "error_occurred_at", "error_request_id", "runtime_diagnostic", "reason"):
        if event_props.get(key) is None:
            event_props.pop(key, None)
    job_id = event_props.get("job_id") if body["event"] == "ui.error" else None
    identity = [person_id, "job_error", job_id] if job_id else [person_id, body["event_id"]]
    eid = "usage_" + hashlib.sha256(json.dumps(identity).encode()).hexdigest()
    if is_local():
        error_order_id = event_props.get("error_order_id")
        if error_order_id:
            with db()() as session:
                order = session.get(VentureDocument, error_order_id)
                if not order or order.venture_id != vid or order.kind != "device_work_order_v1":
                    raise HTTPException(404, detail={"error": {"code": "not_found",
                        "message": "Open one of your tasks and try again."}})
        return {"id": eid}
    props = json.dumps(event_props, sort_keys=True)
    def same(row):
        if row.person_id != stored_person or row.workspace_id != wid or row.venture_id != vid or row.event_name != body["event"]:
            raise HTTPException(409, detail={"error": {"code": "usage_event_conflict"}})
        if job_id:
            previous = json.loads(row.props_json)
            if any(previous.get(key) != event_props.get(key) for key in
                   ("job_id", "error_code", "error_context", "error_text")):
                raise HTTPException(409, detail={"error": {"code": "usage_event_conflict"}})
        elif row.props_json != props:
            raise HTTPException(409, detail={"error": {"code": "usage_event_conflict"}})
        return {"id": row.id}
    with db()() as session:
        existing = session.get(AnalyticsEvent, eid)
        if existing:
            return same(existing)
        error_order_id = event_props.get("error_order_id")
        if error_order_id:
            order = session.get(VentureDocument, error_order_id)
            if not order or order.venture_id != vid or order.kind != "device_work_order_v1":
                raise HTTPException(404, detail={"error": {"code": "not_found"}})
        session.add(AnalyticsEvent(id=eid, person_id=stored_person, workspace_id=wid, venture_id=vid,
            event_name=body["event"], props_json=props, occurred_at=utcnow()))
        try:
            session.commit()
            if body["event"] == "ui.dead_end":
                from app.analytics.error_events import log_error_event
                from core.error_event import observability_metric

                observability_metric("dead_end", event_props["feature"])
                log_error_event("client", feature=event_props["feature"], code=event_props["code"],
                                venture_id=vid, workspace_id=wid, error_route=event_props["route"])
            if body["event"] == "ui.error":
                from app.analytics.error_events import log_error_event

                log_error_event("client", feature=event_props.get("feature"), code=event_props.get("error_code"),
                                venture_id=vid, person_id=person_id, workspace_id=wid,
                                level=logging.INFO if event_props.get("error_severity") == "transient" else logging.WARNING,
                                component=event_props.get("error_source"),
                                intent=event_props.get("error_intent"),
                                error_status=event_props.get("error_status"),
                                error_route=event_props.get("error_route"),
                                error_origin=event_props.get("error_origin"),
                                error_occurred_at=event_props.get("error_occurred_at"),
                                reason=event_props.get("reason"),
                                **({"runtime_diagnostic": event_props["runtime_diagnostic"]}
                                   if event_props.get("runtime_diagnostic") is not None else {}))
        except IntegrityError:
            session.rollback()
            existing = session.get(AnalyticsEvent, eid)
            if existing is None:
                raise
            return same(existing)
    return {"id": eid}
