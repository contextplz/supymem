"""Hard rails for Relationships send paths (doc 130 OT-04)."""

from __future__ import annotations

LINKEDIN_SEND_MODES = frozenset({"human_clipboard", "simulated"})


class ComplianceError(Exception):
    """A send or automation path is forbidden for this product."""


def assert_linkedin_send_allowed(send_mode: str) -> None:
    """LinkedIn may only be human-copy or an honest simulated queue — never Unipile."""
    mode = (send_mode or "").strip()
    if mode not in LINKEDIN_SEND_MODES:
        raise ComplianceError(
            f"linkedin_send_forbidden:{mode or 'empty'}"
        )


def assert_unipile_outreach_forbidden() -> None:
    """Unipile chat/invite is never a Relationships send path."""
    raise ComplianceError("unipile_outreach_forbidden")
