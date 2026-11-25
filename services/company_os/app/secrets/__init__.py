"""Founder secrets never enter model context.

The engine requests a slot. The UI writes the value to ``.env`` and the vault.
Tools and chat only ever see ``env_ref`` + armed/fingerprint.
"""

from app.secrets.need import request_for_provider, require_secret
from app.secrets.service import (
    pending_requests,
    request_secret,
    reset_secret_slots,
    save_secret,
    slot_status,
)

__all__ = [
    "pending_requests",
    "request_for_provider",
    "request_secret",
    "require_secret",
    "reset_secret_slots",
    "save_secret",
    "slot_status",
]
