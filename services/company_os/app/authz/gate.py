"""The one gate that loads both dials before any guarded action (35 §8).

Feature code calls :func:`require` and nothing else. The autonomy dial and the
observe-level dial are ANDed — the stricter always wins — and every denial is
audited naming both, so "why was this refused" is answerable from the log.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.authz.effective import EffectivePermission, effective
from app.core.warden_client import warden
from app.workspaces.resolve import LOCAL_PERSON_ID


class Denied(PermissionError):
    """A guarded action was refused. Message is founder-readable."""

    def __init__(self, decision: "GateDecision") -> None:
        super().__init__(decision.reason)
        self.decision = decision


@dataclass(frozen=True)
class GateDecision:
    allowed: bool
    reason: str = ""
    limited_by: str | None = None
    autonomy_level: str = "L1"
    observe_level: str = "O0"
    warden_decision: str = "allow"


async def check(
    capability: str,
    *,
    workspace_id: str,
    target: str | None = None,
    target_class: str | None = None,
    actor_id: str = LOCAL_PERSON_ID,
    actor_kind: str = "person",
    token_scopes: frozenset[str] | set[str] | None = None,
    risk: str = "low",
    cost_usd: float | None = None,
    consult_warden: bool = True,
    surface: str = "internal",
) -> GateDecision:
    """Evaluate role ∧ token ∧ autonomy ∧ observe, then the out-of-process warden."""
    from app.audit.writer import audit
    from app.workspaces.service import get_workspace, role_of

    ws = get_workspace(workspace_id)
    if ws is None:
        decision = GateDecision(False, "workspace not found", limited_by="workspace")
        audit(
            "governance.denied",
            workspace_id=workspace_id,
            actor_id=actor_id,
            actor_kind=actor_kind,
            surface=surface,
            target_ref=target,
            outcome="denied",
            denied_reason=decision.reason,
            payload={"capability": capability},
        )
        return decision

    role = role_of(workspace_id, actor_id) if actor_kind != "agent" else "maintainer"
    perm: EffectivePermission = effective(
        capability,
        role=role,
        autonomy_level=ws.autonomy_level,
        observe_level=ws.observe_level,
        token_scopes=token_scopes,
        target_class=target_class,
    )
    if not perm.allowed:
        decision = GateDecision(
            False,
            perm.reason,
            limited_by=perm.limited_by,
            autonomy_level=ws.autonomy_level,
            observe_level=ws.observe_level,
        )
        audit(
            "governance.denied",
            workspace_id=workspace_id,
            actor_id=actor_id,
            actor_kind=actor_kind,
            surface=surface,
            target_ref=target,
            outcome="denied",
            # Both dials are always named, whichever one bound.
            denied_reason=(
                f"{perm.audit_reason()} "
                f"autonomy={ws.autonomy_level} observe={ws.observe_level}"
            ),
            payload={"capability": capability, "target_class": target_class},
        )
        return decision

    if not consult_warden:
        return GateDecision(
            True,
            autonomy_level=ws.autonomy_level,
            observe_level=ws.observe_level,
        )

    resp = await warden.check(
        capability=capability,
        venture_id=workspace_id,
        target=target,
        risk=risk,
        cost_usd=cost_usd,
    )
    if resp.decision != "allow":
        decision = GateDecision(
            False,
            resp.reason or "denied by governance",
            limited_by="warden",
            autonomy_level=ws.autonomy_level,
            observe_level=ws.observe_level,
            warden_decision=resp.decision,
        )
        audit(
            "governance.denied",
            workspace_id=workspace_id,
            actor_id=actor_id,
            actor_kind=actor_kind,
            surface=surface,
            target_ref=target,
            outcome="denied",
            denied_reason=(
                f"warden:{resp.decision} {resp.reason} "
                f"autonomy={ws.autonomy_level} observe={ws.observe_level}"
            ),
            payload={"capability": capability},
        )
        return decision

    return GateDecision(
        True, autonomy_level=ws.autonomy_level, observe_level=ws.observe_level
    )


async def require(capability: str, **kwargs: object) -> GateDecision:
    """:func:`check`, but raises :class:`Denied` instead of returning a refusal."""
    decision = await check(capability, **kwargs)  # type: ignore[arg-type]
    if not decision.allowed:
        raise Denied(decision)
    return decision
