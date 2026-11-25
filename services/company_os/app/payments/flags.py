"""House payments are off until we flip PAYMENTS_ACCEPTING.

First rollout: leave this unset. Stripe is not a blocker for Hermes,
desktop updates, or walking the console. Code stays in the tree; a later
ECS update that sets the env turns Checkout / Connect / top-up on.
"""

from __future__ import annotations

import os

_TRUE = frozenset({"1", "true", "yes", "on"})


def payments_accepting() -> bool:
    return (os.environ.get("PAYMENTS_ACCEPTING") or "").strip().lower() in _TRUE


def payments_closed_detail() -> dict[str, dict[str, str]]:
    return {
        "error": {
            "code": "payments_closed",
            "message": "Supymem is not accepting payments yet.",
        }
    }
