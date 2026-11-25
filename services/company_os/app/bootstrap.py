"""Process bootstrap hooks."""

from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)


def _mark_owned_auth_from_env() -> None:
    """Load `.env` into coding_agents config so BYO children never inherit platform keys."""
    try:
        from coding_agents.env import (
            COMPANY_OS_OWNED_AUTH_VARS,
            load_dotenv_file,
            mark_company_os_owned,
            set_company_os_config,
        )

        root = Path(__file__).resolve().parents[3]
        file_cfg = load_dotenv_file(root / ".env")
        # Prefer process env (already sourced by scripts/dev.py) over file for auth vars.
        merged = dict(file_cfg)
        for key in COMPANY_OS_OWNED_AUTH_VARS:
            if os.environ.get(key):
                merged[key] = os.environ[key]
        if merged:
            set_company_os_config(merged, mark_auth=True)
        else:
            # Still mark any auth keys already in the process environment.
            present = [k for k in COMPANY_OS_OWNED_AUTH_VARS if os.environ.get(k)]
            if present:
                mark_company_os_owned(present)
                existing = set(filter(None, os.environ.get("_COS_OWNED_AUTH", "").split(",")))
                existing |= set(present)
                os.environ["_COS_OWNED_AUTH"] = ",".join(sorted(existing))
    except Exception:  # noqa: BLE001 — never block boot
        log.exception("owned-auth bootstrap failed")


def bootstrap(*, recover_onboarding: bool = True) -> list[str]:
    _mark_owned_auth_from_env()
    try:
        from app.admin.authn import validate_security_settings

        validate_security_settings()
    except Exception:
        # SecurityConfigError must not be swallowed; other import failures too.
        raise
    try:
        from app.secrets.boundary import register_vault_boundary

        register_vault_boundary()
    except Exception:  # noqa: BLE001 — never block boot
        log.exception("vault boundary provider bootstrap failed")
    try:
        from app.llm.founder_provider import register_workspace_llm_resolver

        register_workspace_llm_resolver()
    except Exception:  # noqa: BLE001 — never block boot
        log.exception("workspace LLM resolver bootstrap failed")
    try:
        from app.runtime.handlers import install_handlers

        install_handlers()
    except Exception:  # noqa: BLE001 — never block boot
        log.exception("toolbus handler install failed")
    if not recover_onboarding:
        return []
    try:
        from app.idea.service import repair_saved_audiences

        repair_saved_audiences()
    except Exception:  # noqa: BLE001 — never block boot
        log.warning("Saved audience repair did not finish; it will retry at startup.")
    touched: list[str] = []
    try:
        from app.onboarding.resume import crash_sweep

        # Boot has no inherited asyncio workers: every "running" run is orphaned.
        touched = crash_sweep(stale_after_s=0)
        if touched:
            log.info("crash_sweep paused runs: %s", touched)
    except Exception:  # noqa: BLE001 — never block boot
        log.exception("crash_sweep failed")
    return touched
