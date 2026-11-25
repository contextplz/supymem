"""Compatibility entry point: venture creation has no count limit."""

from __future__ import annotations

LIMIT_CODE = "venture_limit_reached"
LIMIT_MESSAGE = "Venture creation has no count limit."


class VentureLimitReached(ValueError):
    code = LIMIT_CODE
    message = LIMIT_MESSAGE

    def __init__(self) -> None:
        super().__init__(LIMIT_MESSAGE)


def assert_can_create_venture(owner_id: str) -> None:
    """Count does not gate creation; caller access, rate and budget checks still do."""
    return None
