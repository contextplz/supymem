"""JSON logger with secret redaction (docs/supymem/21 §1)."""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from typing import Any

from core.redact import redact, redact_string

_CONFIGURED: set[str] = set()
_LOCAL_SECRETS: set[str] = set()


def register_local_secret(value: str) -> None:
    if value and os.environ.get("SUPYMEM_MODE", "").strip().lower() == "local":
        _LOCAL_SECRETS.add(value)


def _local_secret_values() -> list[str] | None:
    if os.environ.get("SUPYMEM_MODE", "").strip().lower() != "local":
        return None
    return sorted(_LOCAL_SECRETS | {value for name, value in os.environ.items() if value
        and re.search(r"(?:^|_)(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|PASSPHRASE|CREDENTIAL)(?:_ES256)?$", name)}, key=len, reverse=True)


def _local_message(record: logging.LogRecord) -> str:
    exact_values = _local_secret_values()
    for name, value in list(record.__dict__.items()):
        if name != "getMessage":
            record.__dict__[name] = redact(value, exact_values=exact_values)
    return redact_string(logging.LogRecord.getMessage(record), exact_values=exact_values)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        if hasattr(record, "emf"):
            values = _local_secret_values()
            return json.dumps(redact(record.emf, exact_values=values) if values is not None else record.emf, separators=(",", ":"))
        from core.request_id import current_request_id

        extra = getattr(record, "extra", None)
        if extra is None:
            extra = {
                k: v
                for k, v in record.__dict__.items()
                if k
                not in {
                    "name",
                    "msg",
                    "args",
                    "levelname",
                    "levelno",
                    "pathname",
                    "filename",
                    "module",
                    "exc_info",
                    "exc_text",
                    "stack_info",
                    "lineno",
                    "funcName",
                    "created",
                    "msecs",
                    "relativeCreated",
                    "thread",
                    "threadName",
                    "processName",
                    "process",
                    "message",
                    "taskName",
                    "getMessage",
                }
            }
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "service": getattr(record, "service", None) or os.environ.get("SERVICE_NAME") or record.name,
            "env": os.environ.get("APP_ENV", "local"),
            "request_id": getattr(record, "request_id", None) or current_request_id(),
            "trace_id": getattr(record, "trace_id", None) or current_request_id(),
            "subject_hash": getattr(record, "subject_hash", None),
            "ws": getattr(record, "ws", None),
            "ven": getattr(record, "ven", None),
            "app": getattr(record, "app", None),
            "run_id": getattr(record, "run_id", None),
            "job_id": getattr(record, "job_id", None),
            "msg": redact_string(record.getMessage(), exact_values=_local_secret_values()),
            "extra": redact(extra, exact_values=_local_secret_values()) if extra else {},
        }
        if record.exc_info:
            payload["exc"] = redact_string(self.formatException(record.exc_info), exact_values=_local_secret_values())
        elif record.exc_text and _local_secret_values() is not None:
            payload["exc"] = record.exc_text
        return json.dumps(payload, default=str, separators=(",", ":"))


def _redact_arg(value: Any) -> Any:
    if isinstance(value, str):
        return redact_string(value)
    if isinstance(value, BaseException):  # str(exc) often carries the failing URL / key
        return redact_string(str(value))
    return redact(value)


def _redact_record(record: logging.LogRecord) -> None:
    """Redact msg and each %-arg in place (keeps arg shape for RequestQueryFilter)."""
    exact_values = _local_secret_values()
    if exact_values is not None:
        RequestQueryFilter().filter(record)
        record.msg = redact(record.msg, exact_values=exact_values)
        record.args = redact(record.args, exact_values=exact_values)
        if record.exc_info:
            record.exc_text = redact_string(logging.Formatter().formatException(record.exc_info), exact_values=exact_values)
            record.exc_info = None
        elif record.exc_text:
            record.exc_text = redact_string(record.exc_text, exact_values=exact_values)
        return
    if isinstance(record.msg, str):
        record.msg = redact_string(record.msg)
    args = record.args
    if isinstance(args, tuple):
        record.args = tuple(_redact_arg(a) for a in args)
    elif isinstance(args, dict):
        record.args = redact(args)


class RedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        _redact_record(record)
        return True


_BASE_FACTORY = logging.getLogRecordFactory()


def _redacting_factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
    record = _BASE_FACTORY(*args, **kwargs)
    _redact_record(record)
    if _local_secret_values() is not None:
        record.getMessage = _local_message.__get__(record)
    return record


def install_log_redaction() -> None:
    """Redact every log record at creation, whatever handler or format is in use."""
    if logging.getLogRecordFactory() is not _redacting_factory:
        logging.setLogRecordFactory(_redacting_factory)


class RequestQueryFilter(logging.Filter):
    """OAuth codes and email links must not enter HTTP/access logs."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            args = list(record.args)
            if record.name == "uvicorn.access" and len(args) >= 3:
                args[2] = str(args[2]).split("?", 1)[0]
            elif record.name == "httpx":
                args = [
                    str(value).split("?", 1)[0]
                    if str(value).startswith(("https://", "http://")) else value
                    for value in args
                ]
            record.args = tuple(args)
        return True


def install_sensitive_url_filters() -> None:
    install_log_redaction()
    for name in ("uvicorn.access", "httpx"):
        target = logging.getLogger(name)
        if not any(isinstance(item, RequestQueryFilter) for item in target.filters):
            target.addFilter(RequestQueryFilter())


def configure_json_logging(service: str, *, stream: Any | None = None) -> logging.Logger:
    """Idempotent: attach a JSON handler to the root logger once per service name."""
    install_sensitive_url_filters()
    os.environ.setdefault("SERVICE_NAME", service)
    key = f"{service}:{id(stream)}"
    log = logging.getLogger(service)
    if key in _CONFIGURED:
        return log
    handler = logging.StreamHandler(stream or sys.stdout)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(RedactFilter())
    root = logging.getLogger()
    if not any(isinstance(h, logging.StreamHandler) and isinstance(h.formatter, JsonFormatter) for h in root.handlers):
        root.handlers.clear()
        root.addHandler(handler)
        root.setLevel(logging.INFO)
    log.setLevel(logging.INFO)
    _CONFIGURED.add(key)
    return log
