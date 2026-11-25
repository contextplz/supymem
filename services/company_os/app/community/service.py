"""Community Department service — thesis → plan → Discord connect/provision."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core.clock import utcnow
from core.ids import new_id

from app.community import SCHEMA_VERSION
from app.community import composio_discord as discord
from app.community import store
from app.community.identity import generate_identity, objective_mix
from app.community.plan import CommunityPlan, research_from_payload
from app.community.program import generate_program_30d
from app.community.thesis import evaluate_thesis

_LAB_LOG = (
    Path(__file__).resolve().parents[4] / ".agents" / "verify_marketing" / "community_lab_log.jsonl"
)


def append_lab_log(entry: dict[str, Any]) -> None:
    try:
        _LAB_LOG.parent.mkdir(parents=True, exist_ok=True)
        payload = {"ts": utcnow().isoformat(), **entry}
        with _LAB_LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, default=str) + "\n")
    except Exception:
        pass


def status(workspace_id: str) -> dict[str, Any]:
    plan = store.latest_plan(workspace_id)
    discord_st = discord.connection_status(workspace_id)
    servers = store.list_servers(workspace_id)
    funnel = store.funnel_stub(workspace_id)
    return {
        "department": "community",
        "positioning": "Community Department (Loop 2) — not a Discord bot",
        "schema_version": SCHEMA_VERSION,
        "plan": plan,
        "discord": discord_st,
        "servers": servers,
        "funnel": funnel,
        "reddit_note": (
            "Reddit find/draft remains marketing/adapters/community_adapter.py "
            "(watering holes). Owned Discord is this package."
        ),
        "phases": {
            "0_thesis": bool(plan and plan.get("thesis")),
            "1_identity": bool(plan and plan.get("identity")),
            "2_connect": bool(discord_st.get("connected")),
            "3_provision": any(
                (s.get("provision") or {}).get("ok") for s in servers
            ),
            "4_operate": False,
            "5_gtm_handoff": False,
        },
    }


def run_thesis(
    workspace_id: str,
    payload: dict[str, Any] | None = None,
    *,
    persist: bool = True,
) -> dict[str, Any]:
    thesis = evaluate_thesis(payload)
    result = {
        "ok": True,
        "workspace_id": workspace_id,
        "thesis": thesis.to_dict(),
        "research": research_from_payload(payload),
    }
    if persist:
        plan = CommunityPlan(
            schema_version=SCHEMA_VERSION,
            workspace_id=workspace_id,
            venture_id=(payload or {}).get("venture_id"),
            research_bundle_id=(payload or {}).get("research_bundle_id"),
            thesis=thesis.to_dict(),
            objectives=None,
            identity=None,
            program_30d=None,
            work_orders=[
                {
                    "id": new_id("wo"),
                    "kind": "community.evaluate_thesis",
                    "status": "done",
                    "title": "Score community thesis",
                }
            ],
            status="draft",
        )
        saved = store.save_plan(workspace_id=workspace_id, plan=plan.to_dict())
        result["plan"] = saved
    append_lab_log(
        {
            "level": "info",
            "action": "community.thesis",
            "workspace_id": workspace_id,
            "verdict": thesis.verdict,
            "score": thesis.thesis_score,
        }
    )
    return result


def create_plan(
    workspace_id: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = dict(payload or {})
    thesis_obj = evaluate_thesis(payload.get("research") or payload)
    if isinstance(payload.get("thesis"), dict) and payload["thesis"].get("verdict"):
        # Prefer an already-evaluated thesis when the lab posts one back.
        t = payload["thesis"]
        thesis_obj.thesis_score = int(t.get("thesis_score") or thesis_obj.thesis_score)
        thesis_obj.verdict = str(t.get("verdict") or thesis_obj.verdict)
        thesis_obj.because = str(t.get("because") or thesis_obj.because)
        if t.get("motion"):
            thesis_obj.motion = str(t["motion"])

    research = research_from_payload(payload.get("research") or payload)
    work_orders: list[dict[str, Any]] = [
        {
            "id": new_id("wo"),
            "kind": "community.evaluate_thesis",
            "status": "done",
            "title": "Score community thesis",
        }
    ]

    objectives = None
    identity = None
    program = None
    if thesis_obj.verdict == "build_owned":
        objectives = objective_mix(thesis_obj, research)
        identity = generate_identity(thesis_obj, research, objectives)
        program = generate_program_30d(thesis_obj, research, identity, objectives)
        work_orders.extend(
            [
                {
                    "id": new_id("wo"),
                    "kind": "community.generate_identity",
                    "status": "done",
                    "title": "Generate community identity",
                },
                {
                    "id": new_id("wo"),
                    "kind": "community.generate_program_30d",
                    "status": "done",
                    "title": "Compile first 30 days",
                },
                {
                    "id": new_id("wo"),
                    "kind": "community.provision_discord",
                    "status": "pending",
                    "title": "Provision Discord skeleton (after connect)",
                },
            ]
        )
    else:
        work_orders.append(
            {
                "id": new_id("wo"),
                "kind": "community.pre_community_plan",
                "status": "done",
                "title": f"Pre-community motion: {thesis_obj.motion}",
                "motion": thesis_obj.motion,
            }
        )

    plan = CommunityPlan(
        schema_version=SCHEMA_VERSION,
        workspace_id=workspace_id,
        venture_id=research.get("venture_id") or payload.get("venture_id"),
        research_bundle_id=payload.get("research_bundle_id"),
        thesis=thesis_obj.to_dict(),
        objectives=objectives.to_dict() if objectives else None,
        identity=identity.to_dict() if identity else None,
        program_30d=program.to_dict() if program else None,
        work_orders=work_orders,
        status="founder_review" if thesis_obj.verdict == "build_owned" else "draft",
    )
    saved = store.save_plan(workspace_id=workspace_id, plan=plan.to_dict())
    append_lab_log(
        {
            "level": "success",
            "action": "community.plan",
            "workspace_id": workspace_id,
            "plan_id": saved["id"],
            "verdict": thesis_obj.verdict,
        }
    )
    return {"ok": True, "plan": saved}


def connect_discord(workspace_id: str) -> dict[str, Any]:
    try:
        result = discord.start_connect(workspace_id)
        append_lab_log(
            {
                "level": "info",
                "action": "community.discord.connect",
                "workspace_id": workspace_id,
                "has_redirect": bool(result.get("redirect_url")),
            }
        )
        return result
    except discord.DiscordBotError as exc:
        out = {
            "ok": False,
            "status": "blocked",
            "block_reason": exc.message,
            "error": exc.code,
            "needs_you": True,
        }
        append_lab_log(
            {
                "level": "error",
                "action": "community.discord.connect",
                "workspace_id": workspace_id,
                "error": exc.code,
            }
        )
        return out


def discord_status(workspace_id: str) -> dict[str, Any]:
    return discord.connection_status(workspace_id)


def provision_discord(
    workspace_id: str,
    *,
    guild_id: str | None = None,
    plan_id: str | None = None,
    approval_id: str | None = None,
) -> dict[str, Any]:
    from app.approvals.external import consume_external_approval

    allowed, reason = consume_external_approval(
        workspace_id=workspace_id,
        approval_id=approval_id,
        action="community_discord_provision",
        target={"guild_id": guild_id or "", "plan_id": plan_id or ""},
    )
    if not allowed:
        return {
            "ok": False,
            "status": "blocked",
            "block_reason": "NeedsYou: approve this exact Discord provisioning action before it can run.",
            "error": reason,
        }
    st = discord.connection_status(workspace_id)
    if not st.get("connected"):
        return {
            "ok": False,
            "status": "blocked",
            "block_reason": st.get("needs_you_reason")
            or "NeedsYou: Discord bot not connected / no guild.",
            "error": "not_connected",
            "discord": st,
        }
    if not guild_id:
        # Prefer a previously *successful* stored server; never reuse failed/placeholder ids.
        servers = store.list_servers(workspace_id)
        for s in servers or []:
            gid = str(s.get("guild_id") or "").strip()
            if not gid or set(gid) <= {"0"}:
                continue
            if gid.isdigit() and set(gid) <= {"0", "1", "2", "3", "4", "5", "6", "7", "8", "9"}:
                # Reject obvious placeholders used in lab tests.
                if gid in {"000000000000000000", "123456789012345678"} or len(gid) < 17:
                    continue
            prov = s.get("provision") or {}
            if s.get("status") == "provisioned" or prov.get("ok"):
                guild_id = gid
                break
        if not guild_id:
            hint = discord.discover_installed_guild(workspace_id)
            if hint.get("ok") and hint.get("guild_id"):
                guild_id = str(hint["guild_id"])
            else:
                return {
                    "ok": False,
                    "status": "blocked",
                    "block_reason": hint.get("block_reason")
                    or (
                        "NeedsYou: guild_id required. Create the Discord server yourself, "
                        "invite the bot, then pass guild_id (Composio redacts guild ids "
                        "from connection payloads — paste Server ID manually)."
                    ),
                    "error": "guild_id_required",
                    "discord": st,
                    "installed_guild": hint,
                    "founder_steps": hint.get("founder_steps"),
                }

    plan = store.get_plan(plan_id) if plan_id else store.latest_plan(workspace_id)
    identity = (plan or {}).get("identity") or {}
    channel_specs = identity.get("channels") or [
        {"slug": "welcome"},
        {"slug": "intros"},
        {"slug": "help"},
        {"slug": "product"},
    ]
    role_specs = identity.get("roles") or [
        {"slug": "champion"},
        {"slug": "community_host"},
    ]

    created_channels: list[dict[str, Any]] = []
    created_roles: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    existing_names: set[str] = set()

    # List existing channels for idempotency
    try:
        listed = discord.list_guild_channels(workspace_id, guild_id)
        data = listed.get("data") if isinstance(listed.get("data"), dict) else listed
        items = []
        if isinstance(data, dict):
            items = data.get("channels") or data.get("items") or data.get("data") or []
            if isinstance(items, dict):
                items = items.get("items") or []
        if isinstance(items, list):
            for ch in items:
                if isinstance(ch, dict):
                    name = str(ch.get("name") or "").lower()
                    if name:
                        existing_names.add(name)
    except Exception as exc:  # noqa: BLE001
        errors.append({"step": "list_channels", "error": str(exc)})

    for spec in channel_specs[:7]:
        slug = str(spec.get("slug") or "").strip().lower().replace(" ", "-")
        if not slug:
            continue
        if slug in existing_names:
            created_channels.append({"slug": slug, "status": "exists"})
            continue
        try:
            raw = discord.create_guild_channel(
                workspace_id, guild_id=guild_id, name=slug
            )
            created_channels.append({"slug": slug, "status": "created", "raw": raw})
            existing_names.add(slug)
        except Exception as exc:  # noqa: BLE001
            errors.append({"step": "create_channel", "slug": slug, "error": str(exc)})

    existing_roles: set[str] = set()
    try:
        roles_raw = discord.list_guild_roles(workspace_id, guild_id)
        data = roles_raw.get("data") if isinstance(roles_raw.get("data"), dict) else roles_raw
        items = []
        if isinstance(data, dict):
            items = data.get("roles") or data.get("items") or data.get("data") or []
        if isinstance(items, list):
            for r in items:
                if isinstance(r, dict) and r.get("name"):
                    existing_roles.add(str(r["name"]).lower())
    except Exception as exc:  # noqa: BLE001
        errors.append({"step": "list_roles", "error": str(exc)})

    for spec in role_specs:
        name = str(spec.get("slug") or "").strip()
        if not name:
            continue
        if name.lower() in existing_roles:
            created_roles.append({"name": name, "status": "exists"})
            continue
        try:
            raw = discord.create_guild_role(
                workspace_id, guild_id=guild_id, name=name
            )
            created_roles.append({"name": name, "status": "created", "raw": raw})
        except Exception as exc:  # noqa: BLE001
            errors.append({"step": "create_role", "name": name, "error": str(exc)})

    ok = bool(created_channels) and not any(
        e.get("step") == "list_channels" for e in errors
    )
    # If we created or confirmed any channel, treat as partial success
    if created_channels and all(c.get("status") in {"exists", "created"} for c in created_channels):
        ok = True
    if errors and not created_channels:
        ok = False

    provision = {
        "ok": ok,
        "guild_id": guild_id,
        "channels": created_channels,
        "roles": created_roles,
        "errors": errors,
        "automod": {
            "status": "skipped",
            "note": "AutoMod rule creation deferred — enable when Manage Guild confirmed",
        },
        "idempotent": True,
    }
    if not ok:
        provision["block_reason"] = (
            errors[0].get("error")
            if errors
            else "NeedsYou: provision failed — check bot permissions / guild_id"
        )

    saved = store.upsert_server(
        workspace_id=workspace_id,
        guild_id=guild_id,
        plan_id=(plan or {}).get("id"),
        connected_account_id=st.get("active_account_id"),
        status="provisioned" if ok else "needs_you",
        provision=provision,
    )
    append_lab_log(
        {
            "level": "success" if ok else "error",
            "action": "community.discord.provision",
            "workspace_id": workspace_id,
            "guild_id": guild_id,
            "ok": ok,
        }
    )
    return {
        "ok": ok,
        "status": "done" if ok else "blocked",
        "block_reason": None if ok else provision.get("block_reason"),
        "provision": provision,
        "server": saved,
    }


def discord_action(
    workspace_id: str,
    *,
    action: str,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    params = dict(params or {})
    # Reads remain available. Mutations are fail-closed and each requires an
    # approval bound to its exact target/content, not a bare confirm boolean.
    if action in {"send_message", "create_invite"}:
        if action == "send_message" and not bool(params.get("confirm")):
            return {
                "ok": False,
                "status": "blocked",
                "block_reason": "NeedsYou: review the message and request approval before sending.",
                "error": "confirm_required",
            }
        from app.approvals.external import consume_external_approval

        target = {
            "action": action,
            "channel_id": str(params.get("channel_id") or ""),
            "content": str(params.get("content") or params.get("message") or ""),
        }
        allowed, reason = consume_external_approval(
            workspace_id=workspace_id,
            approval_id=str(params.get("approval_id") or "") or None,
            action="community_discord_action",
            target=target,
        )
        if not allowed:
            return {
                "ok": False,
                "status": "blocked",
                "block_reason": "NeedsYou: approve this exact Discord action before it can run.",
                "error": reason,
            }
    result = discord.run_lab_action(workspace_id, action, params)
    append_lab_log(
        {
            "level": "success" if result.get("ok") else "error",
            "action": f"community.discord.action.{action}",
            "workspace_id": workspace_id,
            "ok": result.get("ok"),
            "error": result.get("error"),
        }
    )
    return result
