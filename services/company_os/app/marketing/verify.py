"""Verification — jobs are not done until proof exists."""

from __future__ import annotations

from typing import Any

from app.content.platforms.base import dry_run_enabled


BLOCK_HINTS = (
    "captcha",
    "challenge",
    "verification required",
    "login expired",
    "session expired",
    "reconnect",
    "unauthorized",
    "401",
    "403",
    "2fa",
    "two-factor",
)


def is_block_error(message: str | None) -> bool:
    text = (message or "").lower()
    return any(h in text for h in BLOCK_HINTS)


def publish_proof_ok(proof: dict[str, Any] | None, *, require_live_id: bool | None = None) -> tuple[bool, str]:
    """Live publishes need an external_id. Simulated/dry-run need a dryrun URL or sim id."""
    if not proof:
        return False, "missing_proof"
    mode = str(proof.get("mode") or "")
    external_id = str(proof.get("external_id") or "").strip()
    external_url = str(proof.get("external_url") or "").strip()

    live_required = require_live_id
    if live_required is None:
        live_required = not dry_run_enabled()

    if mode == "simulated" or not live_required:
        if external_id or external_url.startswith("dryrun://"):
            return True, "simulated_ok"
        return False, "simulated_missing_id"

    if not external_id:
        return False, "live_missing_external_id"
    if external_id.startswith("sim_") or external_url.startswith("dryrun://"):
        return False, "live_got_simulated_id"
    return True, "live_ok"


def content_create_proof_ok(proof: dict[str, Any] | None) -> tuple[bool, str]:
    if not proof:
        return False, "missing_proof"
    if not proof.get("content_post_id"):
        return False, "missing_content_post_id"
    return True, "ok"


def visual_proof_ok(proof: dict[str, Any] | None, *, allow_skip: bool = True) -> tuple[bool, str]:
    if not proof:
        return False, "missing_proof"
    if proof.get("skipped"):
        return (True, "skipped") if allow_skip else (False, "visual_skipped")
    if not proof.get("visual_path"):
        return False, "missing_visual_path"
    return True, "ok"
