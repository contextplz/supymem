"""Map provider HTTP failures to founder-readable errors (plan BYOK §3)."""

from __future__ import annotations

from typing import Any


class ProviderCallError(RuntimeError):
    """Classified LLM provider failure."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.retryable = retryable

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "status": self.status,
            "retryable": self.retryable,
        }


def classify_provider_error(exc: BaseException) -> ProviderCallError:
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(exc, "status", None)
    try:
        status_i = int(status) if status is not None else None
    except (TypeError, ValueError):
        status_i = None

    name = type(exc).__name__.lower()
    if status_i in {401, 403} or "authentication" in name:
        return ProviderCallError(
            "bad_api_key",
            "API key rejected by the provider — check your key in Settings.",
            status=status_i,
        )
    if status_i == 429 or "ratelimit" in name:
        return ProviderCallError(
            "rate_limited",
            "Provider rate limited — wait a moment and retry.",
            status=status_i,
            retryable=True,
        )
    if status_i == 402:
        return ProviderCallError(
            "out_of_credits",
            "Your provider reported insufficient funds.",
            status=status_i,
        )
    if status_i and status_i >= 500:
        return ProviderCallError(
            "provider_down",
            "Provider is unavailable right now.",
            status=status_i,
            retryable=True,
        )
    if "timeout" in name or "connection" in name:
        return ProviderCallError(
            "provider_down",
            "Could not reach the provider — check network or base URL.",
            status=status_i,
            retryable=True,
        )
    return ProviderCallError(
        "provider_error",
        str(exc)[:240] or "Provider call failed.",
        status=status_i,
        retryable=False,
    )


__all__ = ["ProviderCallError", "classify_provider_error"]
