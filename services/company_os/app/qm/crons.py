"""Register Company OS ops cadence crons on QM (doc 56 QM-P3).

Finding (doc 58 §3): nothing in the repo currently auto-triggers
``app.ops.schedule`` — these QM crons are the first real timer wiring, not a
like-for-like replacement of an existing scheduler.
"""

from __future__ import annotations

import logging
from typing import Any

from app.qm.client import QmClientError, _http_json
from app.qm import settings as qm_settings

log = logging.getLogger(__name__)

# Intended cadences (UTC) matching Polsia-style morning/evening/sweeps (doc 49).
OPS_CRONS: tuple[dict[str, Any], ...] = (
    {
        "title": "Company OS morning cycle",
        "schedule": {"cron": "0 9 * * *", "timezone": "UTC"},
        "cycle": "morning",
    },
    {
        "title": "Company OS evening cycle",
        "schedule": {"cron": "0 18 * * *", "timezone": "UTC"},
        "cycle": "evening",
    },
    {
        "title": "Company OS social sweep",
        "schedule": {"cron": "0 */4 * * *", "timezone": "UTC"},
        "cycle": "social_sweep",
    },
    {
        "title": "Company OS email sweep",
        "schedule": {"cron": "30 */2 * * *", "timezone": "UTC"},
        "cycle": "email_sweep",
    },
    {
        "title": "Company OS GTM signal sweep",
        "schedule": {"cron": "0 */3 * * *", "timezone": "UTC"},
        "cycle": "signal_sweep",
    },
    {
        "title": "Company OS GTM nurture tick",
        "schedule": {"cron": "*/15 * * * *", "timezone": "UTC"},
        "cycle": "nurture_tick",
    },
    {
        "title": "Company OS GTM inbox sweep",
        "schedule": {"cron": "*/10 * * * *", "timezone": "UTC"},
        "cycle": "inbox_sweep",
    },
    {
        "title": "Company OS ads/stripe sync",
        "schedule": {"cron": "15 */6 * * *", "timezone": "UTC"},
        "cycle": "ads_stripe_sync",
    },
)


def _actor_id() -> str:
    return qm_settings.qm_actor_external_id()


def ensure_ops_crons(
    *,
    base_url: str | None = None,
    secret: str | None = None,
) -> list[dict[str, Any]]:
    """Idempotently create the five ops cadence crons on a running QM instance."""
    base = (base_url or qm_settings.qm_base_url()).rstrip("/")
    signing = (secret if secret is not None else qm_settings.qm_source_signing_secret()).strip()
    if not signing:
        raise QmClientError("QM_SOURCE_SIGNING_SECRET is not set")

    actor = _actor_id()
    owner_scope = f"personal:{actor}"
    created: list[dict[str, Any]] = []

    for spec in OPS_CRONS:
        cycle = spec["cycle"]
        action = (
            "Use the skill at skills/companyos-run-ops-cycle/SKILL.md to complete this task.\n\n"
            f"Run the Company OS ops cycle named `{cycle}` for all venture workspaces "
            "(POST /internal/ops/run-cycle with that cycle). Report success or failure plainly."
        )
        body = {
            "ownerScopeId": owner_scope,
            "owner": actor,
            "createdBy": actor,
            "schedule": spec["schedule"],
            "title": spec["title"],
            "action": action,
            "destination": {"type": "principal", "target": actor},
        }
        try:
            _code, parsed = _http_json(
                "POST",
                "/v1/crons",
                body_obj=body,
                secret=signing,
                base_url=base,
                timeout_s=30.0,
            )
            created.append({"cycle": cycle, "ok": True, "response": parsed})
            log.info("QM cron ensured for cycle=%s", cycle)
        except QmClientError as exc:
            created.append(
                {
                    "cycle": cycle,
                    "ok": False,
                    "error": str(exc),
                    "status_code": exc.status_code,
                }
            )
            log.warning("QM cron create failed for cycle=%s: %s", cycle, exc)
    return created
