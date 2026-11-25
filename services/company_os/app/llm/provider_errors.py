"""Founder-facing copy for LLM provider failures (BYOK + platform)."""

from __future__ import annotations

import logging
import re
from typing import Any


def log_chat_provider_failure(*, phase: str, reason: str, attempt: int = 0,
                              exc: BaseException | None = None,
                              usage_state: str = "not_observed",
                              content_length: int | None = None) -> None:
    """Closed diagnostic fields only; logging cannot replace the held outcome."""
    try:
        phase = phase if phase in {"router", "assistant"} else "unknown"
        if reason.startswith("unknown_intent:"):
            from app.secrets.detect import looks_like_secret

            name = reason.partition(":")[2]
            reason = "unknown_intent:" + (name if re.fullmatch(r"[a-z][a-z_.]{0,47}", name)
                                         and not looks_like_secret(name) else "unknown")
        elif reason.startswith("finish_reason:"):
            finish = reason.partition(":")[2]
            reason = "finish_reason:" + (finish if finish in {"stop", "length", "tool_calls", "content_filter", "provider_error"} else "unknown")
        else:
            reason = reason if reason in {"transport_failed", "deadline_expired", "cancelled",
            "usage_missing", "usage_invalid", "usage_read_failed", "rejection_release_unconfirmed",
            "rejection_confirmed", "local_failed", "json_parse", "not_object", "acts_not_list",
            "empty_content", "schema_invalid"} else "unknown"
        content_length = content_length if type(content_length) is int and content_length >= 0 else None
        usage_state = usage_state if usage_state in {"not_observed", "missing", "invalid", "confirmed"} else "unknown"
        attempt = attempt if type(attempt) is int and 0 <= attempt <= 3 else 0
        code = getattr(exc, "code", None)
        code = code if code in {"bad_api_key", "rate_limited", "out_of_credits",
            "provider_down", "provider_error", "provider_outcome_unknown", "provider_output_invalid"} else "unknown"
        status = getattr(exc, "status", None)
        status = status if type(status) is int and 100 <= status <= 599 else None
        exception_class = "none" if exc is None else "other"
        # The gateway wraps SDK exceptions. Retain their class, never their text.
        for _ in range(4):
            if exc is None:
                break
            name = type(exc).__name__
            if name in {"TimeoutError", "APITimeoutError", "ReadTimeout", "ConnectTimeout",
                "WriteTimeout", "PoolTimeout", "APIConnectionError", "ConnectError",
                "CancelledError", "RateLimitError", "AuthenticationError", "APIStatusError"}:
                exception_class = name
                break
            exc = exc.__cause__ or exc.__context__
        logging.getLogger("app.chat.provider").warning(
            "chat_provider_failure phase=%s reason=%s attempt=%d status=%s "
            "exception_class=%s provider_code=%s usage_state=%s content_length=%s",
            phase, reason, attempt, status, exception_class, code, usage_state, content_length,
            extra={"provider_phase": phase, "provider_failure_reason": reason,
                   "provider_attempt": attempt, "provider_http_status": status,
                   "provider_exception_class": exception_class, "provider_error_code": code,
                   "provider_usage_state": usage_state, "provider_content_length": content_length},
        )
    except Exception:
        # A broken handler or hostile exception property must not affect accounting.
        pass


def definitive_provider_rejection(exc: BaseException) -> bool:
    from llm_gateway.errors import ProviderCallError
    from llm_gateway.sole import LocalProviderConnectError

    if isinstance(exc, LocalProviderConnectError):
        return True
    return isinstance(exc, ProviderCallError) and (exc.code, exc.status) in {
        ("bad_api_key", 401), ("bad_api_key", 403),
        ("rate_limited", 429), ("out_of_credits", 402),
    }


def safe_rate_limit_retry(exc: BaseException) -> bool:
    from llm_gateway.errors import ProviderCallError

    return (isinstance(exc, ProviderCallError)
            and (exc.code, exc.status) == ("rate_limited", 429)
            and exc.retryable is True)


def confirmed_provider_rejection(exc: BaseException) -> bool:
    """Only the metered caller can qualify its completed release/readback."""
    return definitive_provider_rejection(exc) and getattr(exc, "unspent_confirmed", False) is True


def hosted_chat_timeout(exc: BaseException) -> bool:
    from llm_gateway.errors import ProviderCallError

    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if isinstance(exc, ProviderCallError) and exc.code == "provider_outcome_unknown":
            return True
        if isinstance(exc, TimeoutError) or type(exc).__name__ in {
            "APITimeoutError", "ReadTimeout", "ConnectTimeout", "WriteTimeout", "PoolTimeout",
            "ReadTimeoutError", "ConnectTimeoutError",
        }:
            return True
        exc = exc.__cause__ or exc.__context__
    return False


HOSTED_CHAT_RETRY_COPY = "Charlie is taking longer than usual. Retrying..."
LOCAL_PROVIDER_SCREEN = "Settings > Keys and connections > Your AI provider"


def local_provider_next_step(code: str = "") -> str:
    if code == "bad_api_key":
        return f"Open {LOCAL_PROVIDER_SCREEN}, replace your API key, then test your connection."
    if code == "not_configured":
        return f"Open {LOCAL_PROVIDER_SCREEN}, choose your provider and chat model, and save your API key if required."
    return f"Open {LOCAL_PROVIDER_SCREEN}, check your API key, service address and chat model, then test your connection."


def provider_error_for_user(exc: BaseException) -> dict[str, Any]:
    """Map any provider failure to a safe UI payload (never includes the key)."""
    try:
        from llm_gateway.errors import ProviderCallError
        from app.core.local_mode import is_local

        if isinstance(exc, ProviderCallError):
            if is_local():
                from llm_gateway.sole import LocalProviderConnectError

                if isinstance(exc, LocalProviderConnectError):
                    next_step = exc.message if exc.loopback else (
                        f"Check your internet connection and Service URL in {LOCAL_PROVIDER_SCREEN}, then tap Try again.")
                    return {"code": exc.code, "retryable": True, "hint": "",
                        "message": "Charlie could not reach your AI provider. Nothing was sent. " + next_step}
                return {"code": exc.code, "message": {
                    "bad_api_key": "Your AI provider rejected your API key.",
                    "provider_output_invalid": "Your AI provider did not return an answer I could use.",
                }.get(exc.code, "Your AI provider did not return a confirmed result."),
                    "retryable": False, "hint": local_provider_next_step(exc.code)}
            if exc.code == "provider_output_invalid":
                return {
                    "code": exc.code,
                    "message": "The provider completed this request, but its reply could not be used.",
                    "retryable": False,
                    "hint": "No automatic retry was made. Review the saved request before continuing.",
                }
            if getattr(exc, "hosted_chat_failure", False):
                return {"code": exc.code, "message": "Charlie is taking longer than usual.",
                    "retryable": getattr(exc, "platform_absorbed", False) or confirmed_provider_rejection(exc),
                    "hint": ("You can try your reply again. Any uncertain model charge is covered by us."
                        if getattr(exc, "platform_absorbed", False) else
                        "Your saved reply needs to be checked before you try again.")}
            known_rejection = confirmed_provider_rejection(exc)
            return {
                "code": exc.code,
                "message": ({
                    "bad_api_key": "This request paused because the provider rejected the API key.",
                    "rate_limited": "This request paused because the provider rejected it with a rate or quota limit.",
                    "out_of_credits": "This request paused because the provider reported insufficient credits.",
                }.get(exc.code) if known_rejection else
                    "This request paused because the provider outcome is not confirmed."),
                "retryable": safe_rate_limit_retry(exc) if known_rejection else False,
                "hint": (_hint(exc.code) if known_rejection else
                         "Check the saved request before trying again. Any uncertain charge remains held for reconciliation; nothing is automatically replayed."),
            }
    except Exception:
        pass
    return {
        "code": "provider_error",
        "message": "This request paused because the provider outcome is not confirmed.",
        "retryable": False,
        "hint": "Check the saved request before trying again. Any uncertain charge remains held for reconciliation; nothing is automatically replayed.",
    }


def _hint(code: str) -> str:
    return {
        "bad_api_key": "Open Settings → Your LLM key, paste a valid key, then Test connection.",
        "rate_limited": "Wait briefly, check your provider quota, then explicitly continue the saved request when ready.",
        "out_of_credits": "Top up credits with your provider, or switch to another key.",
    }.get(code, "Open Settings → Your LLM key to verify provider, model, and key.")


__all__ = ["provider_error_for_user", "definitive_provider_rejection", "safe_rate_limit_retry",
           "confirmed_provider_rejection", "log_chat_provider_failure", "hosted_chat_timeout",
           "HOSTED_CHAT_RETRY_COPY"]
