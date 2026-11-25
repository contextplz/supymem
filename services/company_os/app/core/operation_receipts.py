"""Shared durable single-attempt receipt state machine; storage and fences injected."""
from __future__ import annotations

import logging
import uuid
from copy import deepcopy
from datetime import datetime, timedelta

from core.clock import as_utc, utcnow
from core.redact import redact_string

from app.core.jobs import FatalError, RetryableError
from app.core.local_mode import is_local

log = logging.getLogger(__name__)


class OperationJournal:
    def __init__(self, *, read, mutate, document, before_call, prefix, validate_usage=None, retry_reads=False):
        self.read = read
        self.mutate = mutate
        self.document = document
        self.before_call = before_call
        self.prefix = prefix
        self.validate_usage = validate_usage
        self.retry_reads = retry_reads


    def operation(self, did: str, key: str) -> dict:
        return self.document(did)["_runtime"]["operations"].get(key, {})


    def save_operation(self, did: str, key: str, values: dict, *, payload=None):
        def update(row, doc, session):
            old = doc["_runtime"]["operations"].setdefault(key, {})
            if old.get("billing_resolution") == "platform_absorbed":
                old["late_receipt"] = deepcopy(values)
            else:
                old.update(values)
        self.mutate(did, payload, update)


    def prepare(self, did: str, key: str, payload: dict) -> str:
        """Claim before reserving credits so duplicate deliveries cannot release
        the active caller's idempotent hold. A new lease may reclaim pre-I/O work.
        """
        token = uuid.uuid4().hex
        def update(row, doc, session):
            operations = doc["_runtime"]["operations"]
            old = operations.get(key, {})
            if old.get("state") not in {None, "reserving", "retryable"}:
                raise FatalError(self.prefix + "_outcome_uncertain")
            if old.get("state") == "reserving" and old.get("attempt", 0) >= payload["_job_attempt"] and (
                not self.retry_reads or old.get("attempt") != payload["_job_attempt"] or old.get("worker_id") not in {None, payload["_worker_id"]}
            ):
                raise FatalError(self.prefix + "_operation_in_progress")
            operations[key] = {"state": "reserving", "attempt": payload["_job_attempt"],
                "worker_id": payload["_worker_id"], "token": token,
                **({"interrupted_reads": old["interrupted_reads"]} if old.get("interrupted_reads") else {})}
        self.mutate(did, payload, update)
        return token


    def begin(self, did: str, key: str, payload: dict, usage: dict, token: str):
        def update(row, doc, session):
            operations = doc["_runtime"]["operations"]
            old = operations.get(key, {})
            if old.get("state") != "reserving" or old.get("token") != token:
                raise FatalError(self.prefix + "_operation_in_progress")
            if self.validate_usage is not None:
                self.validate_usage(row, doc, session, usage)
            self.before_call(row, doc, session)
            operations[key] = {"state": "calling", "attempt": payload["_job_attempt"], "worker_id": payload["_worker_id"],
                               "usage": usage, "started_at": utcnow().isoformat(),
                               **({"interrupted_reads": old["interrupted_reads"]} if old.get("interrupted_reads") else {})}
        self.mutate(did, payload, update)


    def checkpoint_model_receipt(self, did: str, key: str, usage: dict, *, response=None, error_code=None):
        """CAS the known SDK evidence before fallible pricing or settlement."""
        from app.credits.gate import _primitive_copy
        usage = _primitive_copy(usage)
        def update(row, doc, session):
            old = doc["_runtime"]["operations"].get(key, {})
            prior = old.get("usage", {})
            if old.get("billing_resolution") == "platform_absorbed":
                if any(name in prior and usage.get(name) != prior[name] for name in (
                        "account_id", "reservation_id", "workspace_id", "venture_id", "request_id", "source",
                        "purpose", "provider", "model", "estimate_micro", "task_invocation", "model_binding", "request_digest")):
                    raise RetryableError(self.prefix + "_settlement_pending")
                old["late_receipt"] = {"usage": usage, "response": response, "error_code": error_code}
                return
            if old.get("state") not in {"calling", "provider_responded"} or any(
                name not in usage or usage[name] != value for name, value in prior.items()
            ):
                raise RetryableError(self.prefix + "_settlement_pending")
            if self.validate_usage is not None:
                self.validate_usage(row, doc, session, usage)
            old.update(state="provider_responded", usage=usage, response=response, error_code=error_code)
        # Saving paid evidence survives a lost lease; applying output does not.
        self.mutate(did, None, update)


    def reconcile_interrupted(self, did: str, key: str, resolve, *, now=None, started_at=None) -> bool:
        """Resolve saved evidence or close stale custody; never authorize another dial."""
        saved = deepcopy(self.operation(did, key))
        if saved.get("state") not in {"calling", "uncertain", "provider_responded"}:
            return False
        started = saved.get("started_at") or started_at
        try:
            expired = as_utc(now or utcnow()) - as_utc(datetime.fromisoformat(started)) >= timedelta(minutes=10)
        except (TypeError, ValueError):
            return False
        resolutions = []
        def finish(row, doc, session):
            if doc["_runtime"]["operations"].get(key) != saved:
                raise RetryableError(self.prefix + "_settlement_pending")
            resolution = resolve(saved, expired, row, doc, session)
            if resolution is not None:
                doc["_runtime"]["operations"][key].update(resolution)
                resolutions.append(resolution)
        self.mutate(did, None, finish)
        if not resolutions:
            return False
        resolution = resolutions[0]
        if resolution["state"] in {"provider_responded", "responded"}:
            self.recover(did, key)
        return True


    def recover(self, did: str, key: str, payload: dict | None = None) -> dict | None:
        saved = deepcopy(self.operation(did, key))
        state = saved.get("state")
        if saved and (state is None or state in {"provider_responded", "responded", "completed", "release_pending"} and (
            type(saved.get("attempt")) is not int or saved["attempt"] < 1 or not isinstance(saved.get("usage"), dict)
            or state != "release_pending" and "response" not in saved
        )):
            raise FatalError(self.prefix + "_outcome_uncertain")
        usage = saved.get("usage", {})
        if not isinstance(usage, dict) or (state in {"calling", "uncertain"} and "usage" not in saved):
            raise FatalError(self.prefix + "_outcome_uncertain")
        if state == "reserving" and payload and self.retry_reads:
            def retry_reservation(row, doc, session):
                if doc["_runtime"]["operations"].get(key) != saved:
                    raise RetryableError(self.prefix + "_settlement_pending")
                doc["_runtime"]["operations"][key]["state"] = "retryable"
            self.mutate(did, payload, retry_reservation)
            return None
        if state in {"calling", "uncertain"} and payload and self.retry_reads and type(saved.get("attempt")) is int and saved["attempt"] >= 1 and not usage.get("reservation_id") and (
            not usage or usage.get("source") in {"byok", "free"} and (usage.get("model") is None or is_local()) and str(usage.get("purpose", "")).startswith("gtm.discovery.")
        ) and (
            not is_local() and not usage.get("local_call_id")
            or is_local() and not usage.get("local_call_id") and usage.get("model") is None
            or type(usage.get("estimate_micro")) is int and usage["estimate_micro"] == 0
            or is_local() and usage.get("local_call_id")
                and type(usage.get("estimate_micro")) is int and usage["estimate_micro"] > 0
                and (state == "uncertain" or saved["attempt"] < payload["_job_attempt"])
        ):
            def retry_read(row, doc, session):
                if doc["_runtime"]["operations"].get(key) != saved:
                    raise RetryableError(self.prefix + "_settlement_pending")
                previous = {name: value for name, value in saved.items() if name != "interrupted_reads"}
                doc["_runtime"]["operations"][key] = {"state": "retryable", "attempt": saved["attempt"],
                    "usage": {}, "interrupted_reads": [*saved.get("interrupted_reads", []), previous]}
            self.mutate(did, payload, retry_read)
            return None
        if state == "calling" and payload and saved.get("attempt") == payload["_job_attempt"] and saved.get("worker_id") == payload["_worker_id"]:
            raise FatalError(self.prefix + "_operation_in_progress")
        if state in {"calling", "uncertain"}:
            raise FatalError(self.prefix + "_outcome_uncertain")
        # Older callers published retryable before their gate confirmed release.
        # Retain that hold until the same custody has been closed successfully.
        if state in {"retryable", "failed"} and not saved.get("usage", {}).get("settled") and (
            saved.get("usage", {}).get("reservation_id") or saved.get("usage", {}).get("task_invocation")
        ):
            previous = deepcopy(saved)
            saved["usage"]["definitive_unspent"] = True
            saved.update(state="release_pending", failure_state=state)
            def upgrade(row, doc, session):
                if doc["_runtime"]["operations"].get(key) != previous:
                    raise RetryableError(self.prefix + "_settlement_pending")
                doc["_runtime"]["operations"][key] = deepcopy(saved)
            self.mutate(did, None, upgrade)
            state = "release_pending"
        if state == "failed":
            raise FatalError(saved.get("error_code", self.prefix + "_failed"))
        if state not in {"provider_responded", "responded", "completed", "release_pending"}:
            return None
        usage = deepcopy(saved.get("usage", {}))
        next_state = saved.get("failure_state") if state == "release_pending" else "completed"
        if next_state not in {"completed", "retryable", "failed"} or (
            state == "release_pending" and usage.get("definitive_unspent") is not True
        ):
            raise FatalError(self.prefix + "_outcome_uncertain")
        binding = {}
        def inspect(row, doc, session):
            if doc["_runtime"]["operations"].get(key) != saved:
                raise RetryableError(self.prefix + "_settlement_pending")
            if self.validate_usage is not None:
                self.validate_usage(row, doc, session, usage)
            binding.update(workspace_id=row.workspace_id, venture_id=row.venture_id,
                request_id=f"{did}:{key}:{saved['attempt']}",
                **{name: usage[name] for name in ("purpose", "provider", "model", "source", "task_invocation", "model_binding", "request_digest") if name in usage})
        # Funded receipt cleanup survives retirement or a lost lease. Current
        # authority/source fences still run before returning an applicable body.
        self.mutate(did, None, inspect)
        if state == "provider_responded":
            from app.credits.gate import project_model_receipt
            try:
                usage = project_model_receipt(usage)
            except Exception as exc:
                log.warning("model_receipt_recovery_failed", extra={"exception_type": type(exc).__name__,
                    "exception_message": redact_string(str(exc))[:512], "operation_id": did,
                    "operation_key": key, "reservation_id": usage.get("reservation_id"),
                    "model_receipt_error": usage.get("model_receipt_error")})
                raise RetryableError(self.prefix + "_settlement_pending") from exc
            error = saved.get("error_code")
            if usage["billing_output_error"]:
                error = self.prefix + "_provider_receipt_output_unusable"
            previous = deepcopy(saved)
            def project(row, doc, session):
                if doc["_runtime"]["operations"].get(key) != previous:
                    raise RetryableError(self.prefix + "_settlement_pending")
                doc["_runtime"]["operations"][key].update(state="responded", usage=usage, error_code=error)
            self.mutate(did, None, project)
            saved.update(state="responded", usage=deepcopy(usage), error_code=error)
        from app.credits.gate import reconcile_receipt
        try:
            charged = reconcile_receipt(usage, binding)
            if type(charged) is not int or charged < 0:
                raise ValueError("receipt charge is unconfirmed")
        except Exception as exc:
            log.warning("model_receipt_recovery_failed", extra={"exception_type": type(exc).__name__,
                "exception_message": redact_string(str(exc))[:512], "operation_id": did,
                "operation_key": key, "reservation_id": usage.get("reservation_id"),
                "model_receipt_error": usage.get("model_receipt_error")})
            raise RetryableError(self.prefix + "_settlement_pending") from exc
        usage.update(credits_micro=charged, settled=True)
        def finish(row, doc, session):
            if doc["_runtime"]["operations"].get(key) != saved:
                raise RetryableError(self.prefix + "_settlement_pending")
            doc["_runtime"]["operations"][key].update(state=next_state, usage=usage)
        self.mutate(did, None, finish)
        if next_state == "retryable":
            return None
        if saved.get("error_code"):
            raise FatalError(saved["error_code"])
        if saved.get("response", {}) is None:
            raise FatalError(self.prefix + "_invalid_output")
        if payload is not None:
            self.mutate(did, payload, lambda row, doc, session: None)
        return saved.get("response", {})
