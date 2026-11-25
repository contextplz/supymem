"""Thin, authenticated, audited ways in (doc 41).

No surface holds business logic. If one surface can do something another
cannot, logic leaked into the surface and must be pushed into a service.
"""

from app.surfaces.auth import Principal, resolve_principal
from app.surfaces.errors import SurfaceError, error_body
from app.surfaces.idempotency import replay_or_reserve, store_response
from app.surfaces.ratelimit import RateLimit, check_rate
from app.surfaces.tokens import mint_token, revoke_token, verify_token

__all__ = [
    "Principal",
    "RateLimit",
    "SurfaceError",
    "check_rate",
    "error_body",
    "mint_token",
    "replay_or_reserve",
    "resolve_principal",
    "revoke_token",
    "store_response",
    "verify_token",
]
