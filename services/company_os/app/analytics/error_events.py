"""Closed error receipts for operators: a structured ``error_event`` log line plus
worker and model analytics rows. Ids, closed labels and status ints only; never
request or model text, exception messages, prompts or PII.

CloudWatch: every line is ``error_event {"acct":...,"code":...,"feature":...,"source":...,"venture":...}``.
A metric filter with the plain pattern ``"error_event"`` counts them (works with
or without JSON logging); in JSON mode use ``{ $.message = "error_event*" }``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import Any
from app.core.local_mode import is_local

from sqlalchemy import event

from app.analytics.events import emit_in_session, emit_once
from app.analytics.feature_catalog import job_feature
from core.runtime_diagnostics import closed_runtime_diagnostic

log = logging.getLogger("app.error_event")
_CODE = re.compile(r"[A-Za-z0-9_.:-]{1,64}\Z")
_LABEL = re.compile(r"[a-z][a-z0-9_.]{0,47}\Z")
_ROUTE = re.compile(r"/[a-z0-9_{}./-]{1,80}\Z")
_SYSTEM_ACTORS = {"system", "anonymous", "local", "founder", ""}


def closed_code(value: object, fallback: str = "other") -> str:
    """A product-safe code (``handler_failed:ValueError``, ``plan_invalid``) or ``other``."""
    return value if isinstance(value, str) and _CODE.fullmatch(value) else fallback


def closed_label(value: object, fallback: str = "other") -> str:
    return value if isinstance(value, str) and _LABEL.fullmatch(value) else fallback


def log_error_event(source: str, *, feature: str, code: str, venture_id: str | None = None,
                    person_id: str | None = None, status: int | None = None, level: int = logging.WARNING,
                    place: str = "", frame: str = "", workspace_id: str | None = None,
                    runtime_diagnostic: dict | None = None, error_route: str | None = None,
                    reason: str | None = None,
                    **closed: Any) -> None:
    """One greppable line per error. ``acct`` is a short hash so CloudWatch can count accounts."""
    if is_local():
        return
    body = {"source": closed_label(source), "feature": closed_label(feature), "code": closed_code(code),
            "venture": venture_id if isinstance(venture_id, str) and _CODE.fullmatch(venture_id) else None,
            "acct": hashlib.sha256(person_id.encode()).hexdigest()[:12] if person_id else None}
    if isinstance(workspace_id, str) and _CODE.fullmatch(workspace_id):
        body["workspace"] = workspace_id
    if status is not None:
        body["status"] = int(status)
    if isinstance(error_route, str) and _ROUTE.fullmatch(error_route):
        body["error_route"] = error_route
    body.update({k: v for k, v in closed.items() if isinstance(v, (int, bool)) or closed_code(v, "") == v})
    diagnostic = closed_runtime_diagnostic(runtime_diagnostic)
    reasons = {"unknown", "panic", "unclean_exit", "supervisor_start_failed", "console_navigation_failed",
               "console_unreachable", "update_failed", "sidecar_spawn_failed", "sidecar_port_conflict",
               "sidecar_binary_missing", "sidecar_permission_denied", "sidecar_startup_exited",
               "sidecar_health_timeout", "sidecar_health_unavailable", "sidecar_protocol_mismatch", "sidecar_exited"}
    if isinstance(reason, str) and reason in reasons:
        body["reason"] = reason
    elif diagnostic and diagnostic["reason"] in reasons:
        body["reason"] = diagnostic["reason"]
    elif source == "client" and body["code"] in {"network_error", "request_error", "server_error"}:
        body["reason"] = "fetch_failed" if closed.get("error_status") == 0 else "service_unavailable" if body["code"] in {"network_error", "server_error"} else "request_rejected"
    if diagnostic:
        body["runtime_diagnostic"] = diagnostic
    try:
        log.log(level, "error_event %s", json.dumps({k: v for k, v in body.items() if v is not None}, sort_keys=True))
    except Exception:  # noqa: BLE001 - logging never breaks the caller
        pass
    # Durable fingerprint + alerting (docs/supymem/handoff-2026-10-05/ERROR_PIPELINE.md); best effort, never raises.
    from app.error_reports import service as reports

    severity = reports.severity_for(body["source"], body["code"], level)
    if severity:
        where = place or closed.get("kind") or closed.get("intent") or closed.get("component") or ""
        reports.record(source=body["source"], feature=body["feature"], code=body["code"],
                       place=closed_code(where, "") if not place else place, frame=frame, severity=severity,
                       person_id=person_id, workspace_id=workspace_id, venture_id=body["venture"], status=status,
                       **({"runtime_diagnostic": diagnostic} if diagnostic else {}))


def actor_person_id(actor: str | None) -> str | None:
    """Person id for a bound actor subject; system actors have none."""
    if not actor or actor in _SYSTEM_ACTORS:
        return None
    try:
        from app.authz.workspace import subject_person_id

        return subject_person_id(actor)
    except Exception:  # noqa: BLE001
        return None


def record_job_failure(session: Any, job: Any, *, code: str, status: str, person_id: str | None = None) -> None:
    """Stage ``job.failed`` in the failing transaction and log the alarm line.

    Every failed attempt is recorded (``final`` marks the ``failed_dead`` one the
    founder actually experiences); reports keep the latest row per ``job_id``.
    """
    feature, code = job_feature(job.kind), closed_code(code)
    props = {"job_id": job.id, "kind": closed_label(job.kind), "feature": feature, "error_code": code,
             "attempt": int(job.attempts or 0), "max_attempts": int(job.max_attempts or 0),
             "final": status == "failed_dead", "terminal_status": closed_label(status)}
    emit_in_session(session, "job.failed", event_key=f"{job.id}:{job.attempts}", person_id=person_id,
                    workspace_id=job.workspace_id, venture_id=job.venture_id, props=props)
    if props["final"]:
        from app.analytics.funnel import stage_job_outcome

        stage_job_outcome(session, job, "failed", person_id=person_id)
    pending = {"feature": feature, "code": code, "venture_id": job.venture_id, "person_id": person_id,
               "level": logging.ERROR if props["final"] else logging.WARNING,
               "kind": props["kind"], "final": props["final"]}

    def log_after_transaction(_session, transaction):
        if transaction.parent is None and pending:
            alarm = dict(pending)
            pending.clear()
            log_error_event("worker", **alarm)

    event.listen(session, "after_transaction_end", log_after_transaction)


def record_model_rejected(*, operation_kind: str, rule: str, feature: str, operation_id: str,
                          venture_id: str | None, workspace_id: str | None, job_id: str | None = None) -> None:
    """A paid model answer failed validation. Closed labels only; the text is never kept."""
    actor = None
    try:
        from app.tenancy import current_or_none

        scope = current_or_none()
        actor = scope.actor if scope else None
    except Exception:  # noqa: BLE001
        pass
    person_id = actor_person_id(actor)
    props = {"operation_kind": closed_label(operation_kind), "rule": closed_code(rule), "feature": closed_label(feature)}
    if isinstance(job_id, str) and _CODE.fullmatch(job_id):
        props["job_id"] = job_id
    key = re.sub(r"[^a-z0-9_:-]", "", f"{operation_id}:{rule}".lower())[:160] or "unknown"
    emit_once("model.output_rejected", event_key=key, person_id=person_id, workspace_id=workspace_id,
              venture_id=venture_id, props=props)
    log_error_event("model", feature=props["feature"], code=props["rule"], venture_id=venture_id, person_id=person_id,
                    operation_kind=props["operation_kind"])
