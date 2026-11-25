"""Error taxonomy for Company OS (gateway error contract + failure semantics)."""

from __future__ import annotations

from typing import Any


class CompanyOSError(Exception):
    """Base error. Maps 1:1 to stable machine ``code`` values."""

    code: str = "internal_error"
    http_status: int = 500
    retryable: bool = False

    def __init__(
        self,
        message: str,
        *,
        detail: dict[str, Any] | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail or {}
        self.request_id = request_id

    def to_dict(self) -> dict[str, Any]:
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "detail": self.detail,
                "request_id": self.request_id,
                "retryable": self.retryable,
            }
        }


class NotFoundError(CompanyOSError):
    code = "not_found"
    http_status = 404


class ValidationFailed(CompanyOSError):
    code = "validation_failed"
    http_status = 400


class ConflictError(CompanyOSError):
    code = "conflict"
    http_status = 409


class IllegalTransition(CompanyOSError):
    code = "illegal_transition"
    http_status = 409


class CharterNotLocked(CompanyOSError):
    code = "charter_not_locked"
    http_status = 409


class Denied(CompanyOSError):
    code = "denied"
    http_status = 403


class WardenUnavailable(CompanyOSError):
    code = "warden_unavailable"
    http_status = 503
    retryable = True


class BudgetExhausted(CompanyOSError):
    code = "budget_exhausted"
    http_status = 429
    retryable = False


class KillSwitchActive(CompanyOSError):
    code = "kill_switch_active"
    http_status = 503


class JobFailed(CompanyOSError):
    code = "job_failed"
    http_status = 500
    retryable = True


class TimeoutExceeded(CompanyOSError):
    code = "timeout_exceeded"
    http_status = 504
    retryable = True


class InternalError(CompanyOSError):
    code = "internal_error"
    http_status = 500
