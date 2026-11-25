"""Bearer-token authentication for every non-console surface (41 §8).

A surface without an actor breaks the audit trail, which is the thing that
makes any of this acceptable to a real company. So the principal is resolved
once, here, and carries the actor into `app.audit`.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.audit.writer import AuditActor
from app.authz.effective import TOKEN_SCOPES, EffectivePermission, effective
from app.core.local_mode import is_local
from app.surfaces.errors import SurfaceError
from app.surfaces.tokens import TokenError, VerifiedToken, verify_token


@dataclass(frozen=True)
class Principal:
    person_id: str
    token_id: str
    token_label: str
    surface: str
    scopes: frozenset[str]
    workspace_id: str | None

    @property
    def actor(self) -> AuditActor:
        return AuditActor(
            actor_id=f"{self.surface}:{self.token_label}",
            actor_kind="token",
            # The human whose grant this token acts under (42 §1).
            on_behalf_of=self.person_id,
            surface=self.surface if self.surface in {"mcp", "cli", "widget"} else "api",
        )

    def may(self, capability: str, *, workspace_id: str) -> EffectivePermission:
        from app.workspaces.service import get_workspace, role_of

        ws = get_workspace(workspace_id)
        if ws is None:
            return EffectivePermission(
                False, "workspace not found", limited_by="workspace"
            )
        if self.workspace_id and self.workspace_id != workspace_id:
            return EffectivePermission(
                False,
                "this token is scoped to a different workspace",
                limited_by="token",
            )
        if ws.settings.get("blocked"):
            return EffectivePermission(False, "workspace is blocked", limited_by="workspace")
        return effective(
            capability,
            role=role_of(workspace_id, self.person_id),
            autonomy_level=ws.autonomy_level,
            observe_level=ws.observe_level,
            token_scopes=self.scopes,
        )


def resolve_principal(authorization: str | None) -> Principal:
    if is_local():
        from app.core.local_owner import current_owner

        owner = current_owner()
        return Principal(person_id=owner.id, token_id=owner.id, token_label="local",
                         surface="api", scopes=TOKEN_SCOPES, workspace_id=None)
    if not authorization or not authorization.lower().startswith("bearer "):
        raise SurfaceError(
            "unauthenticated",
            "This endpoint needs a token.",
            status=401,
            hint="Send Authorization: Bearer cos_…",
        )
    raw = authorization.split(" ", 1)[1].strip()
    try:
        verified: VerifiedToken = verify_token(raw)
    except TokenError as exc:
        status = 401 if exc.code != "not_found" else 404
        raise SurfaceError(
            exc.code, exc.message, status=status, hint="Mint a new token and retry."
        ) from exc
    return Principal(
        person_id=verified.person_id,
        token_id=verified.id,
        token_label=verified.label,
        surface=verified.surface,
        scopes=verified.scopes,
        workspace_id=verified.workspace_id,
    )
