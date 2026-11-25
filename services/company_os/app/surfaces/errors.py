"""The one error shape every surface returns (41 §4).

``code`` is machine-stable, ``message`` is for a human, ``hint`` is actionable,
``retryable`` tells a client whether to bother. Internals never reach
``message``.
"""

from __future__ import annotations

from typing import Any


class SurfaceError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int = 400,
        hint: str = "",
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.hint = hint
        self.retryable = retryable

    def body(self) -> dict[str, Any]:
        return error_body(
            self.code, self.message, hint=self.hint, retryable=self.retryable
        )


def error_body(
    code: str, message: str, *, hint: str = "", retryable: bool = False
) -> dict[str, Any]:
    return {
        "error": {
            "code": code,
            "message": message,
            "hint": hint,
            "retryable": retryable,
        }
    }
