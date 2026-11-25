"""Marketing Lab — consolidated readiness, append-only problem log, manual job enqueue.

Used by the Console Marketing Lab GUI so founders can manually trigger and inspect
every marketing surface without hunting across Content / GTM / Media pages.
Never returns secret values — only presence / missing env names.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import timedelta
from hashlib import sha256
from pathlib import Path
from typing import Any

from core.clock import utcnow
from core.ids import new_id

from app.config import data_root
from app.marketing import store as marketing_store
from app.marketing.media.service import ensure_media_plan
from app.marketing.schema import default_constraints, empty_strategy

logger = logging.getLogger(__name__)


def _repo_root() -> Path:
    # services/company_os/app/marketing/lab.py → repo root is parents[4]
    return Path(__file__).resolve().parents[4]


def lab_log_path(workspace_id: str) -> Path:
    if not workspace_id:
        raise ValueError("workspace_required")
    override = (os.environ.get("MARKETING_LAB_LOG") or "").strip()
    base = Path(override) if override else data_root() / "marketing" / "lab_log.jsonl"
    return base.parent / "workspaces" / sha256(workspace_id.encode()).hexdigest() / base.name


def append_lab_log(entry: dict[str, Any], *, workspace_id: str) -> dict[str, Any]:
    """Append one structured log line without failing the action being logged."""
    path = lab_log_path(workspace_id)
    row = {
        "ts": utcnow().isoformat(),
        "level": str(entry.get("level") or "info"),
        "action": str(entry.get("action") or "unknown"),
        "http_status": entry.get("http_status"),
        "request": entry.get("request"),
        "response": entry.get("response"),
        "error": entry.get("error"),
        "category": str(entry.get("category") or entry.get("level") or "info"),
        "workspace_id": workspace_id,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    except OSError:
        logger.warning("Could not write marketing activity log", exc_info=True)
    return row


def read_lab_log(*, workspace_id: str, limit: int = 200) -> list[dict[str, Any]]:
    path = lab_log_path(workspace_id)
    if not path.is_file():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    out: list[dict[str, Any]] = []
    for line in lines[-max(1, min(limit, 500)) :]:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            out.append({"ts": None, "level": "error", "action": "parse", "error": line[:300]})
    return out


def clear_lab_log(*, workspace_id: str) -> dict[str, Any]:
    path = lab_log_path(workspace_id)
    if path.is_file():
        path.write_text("", encoding="utf-8")
    return {"ok": True, "path": str(path)}


def _flag(name: str, *, default: str = "true") -> bool:
    raw = os.environ.get(name, default)
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _env_present(*names: str) -> dict[str, Any]:
    missing = [n for n in names if not (os.environ.get(n) or "").strip()]
    return {"ok": not missing, "missing": missing, "configured": not missing}


def _account_ready(accounts: list[dict[str, Any]], platform: str) -> dict[str, Any]:
    matches = [
        a
        for a in accounts
        if str(a.get("platform") or "").lower() == platform
        and str(a.get("status") or "") != "revoked"
    ]
    active = [a for a in matches if str(a.get("status") or "") == "active"]
    paused = [a for a in matches if a.get("paused_reason")]
    return {
        "ok": bool(active),
        "connected": bool(matches),
        "active": len(active),
        "paused_reason": (paused[0].get("paused_reason") if paused else None),
        "last_error": (matches[0].get("last_error") if matches else None),
        "handle": (active[0].get("handle") if active else None),
        "auth_via": (active[0].get("auth_via") if active else None),
    }


def ensure_lab_plan(workspace_id: str) -> dict[str, Any]:
    """Prefer the active marketing plan; otherwise a draft lab plan for manual jobs."""
    plan = marketing_store.active_plan(workspace_id)
    if plan is not None:
        return plan

    from sqlalchemy import select

    from app.db.models import MarketingPlan
    from app.state import db

    factory = db()
    with factory() as session:
        row = session.scalars(
            select(MarketingPlan)
            .where(
                MarketingPlan.workspace_id == workspace_id,
                MarketingPlan.source == "marketing_lab",
            )
            .order_by(MarketingPlan.created_at.desc())
        ).first()
        if row is not None:
            return marketing_store.serialize_plan(row)

    strategy = empty_strategy()
    now = utcnow()
    with factory() as session:
        row = MarketingPlan(
            id=new_id("mpl"),
            workspace_id=workspace_id,
            brief_id=None,
            horizon_days=7,
            status="draft",
            strategy_json=json.dumps(strategy, ensure_ascii=False),
            constraints_json=json.dumps(default_constraints(), ensure_ascii=False),
            source="marketing_lab",
            starts_at=now,
            ends_at=now + timedelta(days=7),
            created_at=now,
            updated_at=now,
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        return marketing_store.serialize_plan(row)


def lab_status(workspace_id: str) -> dict[str, Any]:
    """Aggregate readiness + flags + blockers for the Marketing Lab GUI."""
    from app.content import brief as brief_store
    from app.content.oauth import flow, provider_status
    from app.content.platforms import dry_run_enabled as content_dry_run
    from app.marketing.executor import status as execution_status
    from app.marketing.media.capabilities import capabilities
    from app.marketing.media.probes import google_ads_available, meta_ads_available

    blockers: list[dict[str, Any]] = []
    content_accounts = flow.list_accounts(workspace_id)
    providers = provider_status()

    linkedin = _account_ready(content_accounts, "linkedin")
    x_acc = _account_ready(content_accounts, "x")
    reddit = _account_ready(content_accounts, "reddit")

    composio = {
        "ok": bool((os.environ.get("COMPOSIO_API_KEY") or "").strip()),
        "missing": []
        if (os.environ.get("COMPOSIO_API_KEY") or "").strip()
        else ["COMPOSIO_API_KEY"],
    }
    hunter = _env_present("HUNTER_API_KEY")
    unipile = _env_present("UNIPILE_API_KEY")
    gmail = {
        "ok": False,
        "configured": False,
        "connected": False,
        "missing": [],
        "hint": None,
        "account_email": None,
        "accounts": [],
    }
    try:
        from app.gtm.supymem import composio_gmail
        from app.gtm.supymem.pipeline import mail_readiness

        gmail_ok = composio_gmail.configured()
        mail = mail_readiness(workspace_id)
        hint = None
        if not gmail_ok:
            hint = "Supymem's Gmail connection service needs operator setup. This version does not accept a personal Composio API key."
        elif not mail.get("connected"):
            hint = "Click Connect mail to authorize Gmail via Composio."
        gmail = {
            "ok": bool(mail.get("connected") and gmail_ok),
            "configured": gmail_ok,
            "connected": bool(mail.get("connected")),
            "ready": bool(mail.get("ready")),
            "missing": []
            if gmail_ok
            else ["COMPOSIO_API_KEY", "COMPOSIO_GMAIL_AUTH_CONFIG_ID"],
            "hint": hint,
            "account_email": mail.get("account_email"),
            "accounts": mail.get("accounts") or [],
            "dry_run": mail.get("dry_run"),
            "send_enabled": mail.get("send_enabled"),
        }
    except Exception as exc:
        gmail["hint"] = str(exc)[:160]

    google_ads = google_ads_available()
    meta_ads = meta_ads_available()
    caps = capabilities()

    channels_dry = _flag("CHANNELS_DRY_RUN", default="true")
    outbound = _flag("MARKETING_OUTBOUND_ENABLED", default="false")
    execution = _flag("MARKETING_EXECUTION_ENABLED", default="true")
    content_dry = bool(content_dry_run())

    if not linkedin["ok"]:
        blockers.append(
            {
                "code": "linkedin_not_connected",
                "severity": "NeedsYou",
                "message": linkedin.get("paused_reason")
                or "Connect LinkedIn via Composio on Content Studio.",
            }
        )
    if not composio["ok"]:
        blockers.append(
            {
                "code": "composio_missing",
                "severity": "NeedsOperator",
                "message": "Supymem's connection service needs operator setup. This version does not accept a personal Composio API key.",
            }
        )
    if not unipile["ok"]:
        blockers.append(
            {
                "code": "unipile_missing",
                "severity": "warning",
                "message": (
                    "UNIPILE_API_KEY missing, LinkedIn DMs use human_queue (not live Unipile)."
                ),
            }
        )
    if not gmail.get("connected"):
        blockers.append(
            {
                "code": "gmail_not_ready",
                "severity": "NeedsYou",
                "message": gmail.get("hint") or "Gmail not connected, use Connect mail.",
            }
        )
    if not google_ads.get("ok"):
        blockers.append(
            {
                "code": "google_ads_env",
                "severity": "NeedsYou",
                "message": "Missing: " + ", ".join(google_ads.get("missing") or []),
            }
        )
    if not meta_ads.get("ok"):
        blockers.append(
            {
                "code": "meta_ads_env",
                "severity": "NeedsYou",
                "message": "Missing: " + ", ".join(meta_ads.get("missing") or []),
            }
        )
    if content_dry:
        blockers.append(
            {
                "code": "content_dry_run",
                "severity": "warning",
                "message": "CONTENT_DRY_RUN is on, publishes are simulated.",
            }
        )
    if channels_dry:
        blockers.append(
            {
                "code": "channels_dry_run",
                "severity": "warning",
                "message": "CHANNELS_DRY_RUN is on, cold email / channel sends are simulated.",
            }
        )
    if not outbound:
        blockers.append(
            {
                "code": "outbound_disabled",
                "severity": "info",
                "message": "MARKETING_OUTBOUND_ENABLED is off, plan compiler skips outreach jobs.",
            }
        )
    if not execution:
        blockers.append(
            {
                "code": "execution_disabled",
                "severity": "blocked",
                "message": "MARKETING_EXECUTION_ENABLED is off, marketing tick will no-op.",
            }
        )

    gtm: dict[str, Any] = {}
    try:
        from app.gtm.lead_pipeline import status as pipeline_status
        from app.gtm.supymem.factory import funnel, get_or_create_state
        from app.gtm.supymem.pipeline import enrich_status

        gtm = {
            **pipeline_status(),
            **{k: v for k, v in enrich_status().items()},
            "engine": get_or_create_state(workspace_id),
            "funnel": funnel(workspace_id),
        }
    except Exception as exc:
        gtm = {"error": str(exc)[:240]}

    marketing = execution_status(workspace_id)
    brief = brief_store.active(workspace_id)
    blocked_jobs = marketing_store.list_jobs(workspace_id, status="blocked", limit=40)

    linkedin_dm_mode = "human_queue"
    if unipile.get("ok"):
        linkedin_dm_mode = "unipile"
    enrolled_li = 0
    try:
        engine = gtm.get("engine") or {}
        enrolled_li = int(engine.get("linkedin_enrolled") or 0)
    except Exception:
        enrolled_li = 0
    if isinstance(gtm.get("funnel"), dict):
        enrolled_li = enrolled_li or int(
            (gtm["funnel"].get("linkedin") or gtm["funnel"].get("linkedin_queued") or 0)
            or 0
        )

    out: dict[str, Any] = {
        "workspace_id": workspace_id,
        "flags": {
            "CONTENT_DRY_RUN": content_dry,
            "CHANNELS_DRY_RUN": channels_dry,
            "MARKETING_OUTBOUND_ENABLED": outbound,
            "MARKETING_EXECUTION_ENABLED": execution,
            "MARKETING_OWNS_CONTENT": _flag("MARKETING_OWNS_CONTENT", default="true"),
        },
        "connectors": {
            "linkedin": {**linkedin, "composio": composio},
            "gmail": gmail,
            "hunter": hunter,
            "reddit": reddit,
            "x": x_acc,
            "unipile": {
                **unipile,
                "mode": linkedin_dm_mode,
                "note": (
                    "Live Unipile DMs when UNIPILE_API_KEY is set; otherwise human_queue."
                ),
            },
            "google_ads": google_ads,
            "meta_ads": meta_ads,
            "composio": composio,
        },
        "providers": providers,
        "accounts": content_accounts,
        "blockers": blockers,
        "has_brief": brief is not None,
        "brief_id": (brief or {}).get("id") if isinstance(brief, dict) else None,
        "marketing": marketing,
        "gtm": gtm,
        "linkedin_dm": {
            "mode": linkedin_dm_mode,
            "live": linkedin_dm_mode == "unipile",
            "enrolled": enrolled_li,
            "banner": None
            if linkedin_dm_mode == "unipile"
            else "LinkedIn DM is not live, Unipile missing; messages stay on the human queue.",
        },
        "media_capabilities": caps,
        "blocked_jobs": blocked_jobs,
        "lab_log_path": str(lab_log_path(workspace_id)),
        "note": (
            "Presence-only credentials. Secrets never returned. "
            "If a route 404s, restart company_os ops so new lab routes load."
        ),
    }

    try:
        from app.content import linkedin_actions

        out["linkedin_actions"] = linkedin_actions.readiness(workspace_id)
    except Exception as exc:
        out["linkedin_actions"] = {"error": str(exc)[:240]}
    return out


def schedule_manual_content(
    *,
    workspace_id: str,
    platform: str,
    body: str,
    image_path: str | None = None,
    also_publish_job: bool = True,
    also_check_job: bool = True,
) -> dict[str, Any]:
    """Create a scheduled ContentPost from founder text and optional publish/check jobs."""
    from app.content import brief as brief_store
    from app.content.oauth import flow
    from app.content.platforms import dry_run_enabled
    from app.db.models import ContentPost, ContentTopic
    from app.state import db

    platform = (platform or "linkedin").strip().lower()
    text = (body or "").strip()
    if not text:
        return {"ok": False, "error": "body_required"}

    brief = brief_store.active(workspace_id)
    if brief is None:
        return {
            "ok": False,
            "status": "blocked",
            "error": "no_brief",
            "block_reason": "NeedsYou: upload a content brief (or run research → plan) first.",
        }

    accounts = [
        a
        for a in flow.list_accounts(workspace_id)
        if a.get("platform") == platform and a.get("status") == "active"
    ]
    account_id = accounts[0]["id"] if accounts else None
    now = utcnow()
    plan = ensure_lab_plan(workspace_id)

    factory = db()
    with factory() as session:
        topic = ContentTopic(
            id=new_id("ctp"),
            workspace_id=workspace_id,
            brief_id=str(brief["id"]),
            angle="Marketing lab manual post",
            hook=text[:160],
            rationale="Manual lab harness",
            icp_segment=None,
            intent="value",
            evidence_json="[]",
            score=1.0,
            status="drafted",
            created_at=now,
        )
        session.add(topic)
        post = ContentPost(
            id=new_id("cps"),
            workspace_id=workspace_id,
            topic_id=topic.id,
            account_id=account_id,
            platform=platform,
            intent="value",
            body=text,
            title=None,
            target=None,
            state="scheduled",
            simhash=None,
            lint_json="{}",
            critic_json="{}",
            refusal_reason=None,
            scheduled_for=now - timedelta(seconds=5),
            published_at=None,
            external_id=None,
            external_url=None,
            visual_json="{}",
            visual_path=(image_path or None),
            dry_run=bool(dry_run_enabled()),
            created_at=now,
            updated_at=now,
        )
        session.add(post)
        session.commit()
        post_id = post.id

    jobs: list[dict[str, Any]] = []
    publish_id = None
    if also_publish_job:
        publish_id = new_id("mjb")
        jobs.append(
            {
                "id": publish_id,
                "workspace_id": workspace_id,
                "plan_id": plan["id"],
                "kind": "publish_content",
                "platform": platform,
                "title": f"Lab publish · {platform}",
                "payload": {
                    "platform": platform,
                    "content_post_id": post_id,
                    "require_proof": True,
                    "lab": True,
                },
                "depends_on": [],
                "status": "pending",
                "priority": 5,
                "scheduled_for": now - timedelta(seconds=2),
            }
        )
    if also_check_job and publish_id:
        jobs.append(
            {
                "id": new_id("mjb"),
                "workspace_id": workspace_id,
                "plan_id": plan["id"],
                "kind": "check_result",
                "platform": platform,
                "title": f"Lab verify · {platform}",
                "payload": {
                    "platform": platform,
                    "content_post_id": post_id,
                    "verify_browser": True,
                    "lab": True,
                },
                "depends_on": [publish_id],
                "status": "pending",
                "priority": 8,
                "scheduled_for": now,
            }
        )

    inserted = marketing_store.insert_jobs(jobs) if jobs else []
    return {
        "ok": True,
        "content_post_id": post_id,
        "platform": platform,
        "dry_run": bool(dry_run_enabled()),
        "account_id": account_id,
        "plan_id": plan["id"],
        "jobs": inserted,
        "note": (
            None
            if account_id
            else f"No active {platform} account, publish will block until connected."
        ),
    }


def enqueue_lab_jobs(
    *,
    workspace_id: str,
    kinds: list[str],
    platform: str | None = None,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Enqueue one or more marketing jobs for immediate tick (lab harness)."""
    payload = dict(payload or {})
    platform = (platform or payload.get("platform") or "linkedin")
    if isinstance(platform, str):
        platform = platform.strip().lower()
    else:
        platform = "linkedin"
    payload.setdefault("platform", platform)
    payload["lab"] = True

    plan = marketing_store.active_plan(workspace_id) or ensure_lab_plan(workspace_id)
    now = utcnow()
    specs: list[dict[str, Any]] = []
    prev_id: str | None = None
    for kind in kinds:
        kind = str(kind).strip()
        if not kind:
            continue
        jid = new_id("mjb")
        specs.append(
            {
                "id": jid,
                "workspace_id": workspace_id,
                "plan_id": plan["id"],
                "kind": kind,
                "platform": platform if kind not in {"enqueue_outreach", "send_email_touch"} else None,
                "title": f"Lab · {kind}" + (f" · {platform}" if platform else ""),
                "payload": {**payload, "kind": kind},
                "depends_on": [prev_id] if prev_id else [],
                "status": "pending",
                "priority": 5,
                "scheduled_for": now - timedelta(seconds=2),
            }
        )
        prev_id = jid

    if not specs:
        return {"ok": False, "error": "no_kinds"}
    inserted = marketing_store.insert_jobs(specs)
    return {
        "ok": True,
        "plan_id": plan["id"],
        "jobs": inserted,
        "note": "Run POST /marketing/tick to execute due jobs.",
    }


def enqueue_ads_enable_job(
    *,
    workspace_id: str,
    platform: str,
    resource_id: str,
    approval_id: str | None = None,
) -> dict[str, Any]:
    """Enqueue enable_ad (spend_money) or call probe immediately for NeedsYou visibility."""
    from app.marketing.media import probes

    outcome = probes.enable_ad(
        platform=platform,
        resource_id=resource_id,
        approval_id=approval_id,
    )
    plan = ensure_media_plan(workspace_id)
    now = utcnow()
    status = str(outcome.get("status") or ("done" if outcome.get("ok") else "blocked"))
    inserted = marketing_store.insert_jobs(
        [
            {
                "id": new_id("mjb"),
                "workspace_id": workspace_id,
                "plan_id": plan["id"],
                "kind": "enable_ad",
                "platform": platform,
                "title": f"Lab enable ad · {platform}",
                "payload": {
                    "platform": platform,
                    "resource_id": resource_id,
                    "approval_id": approval_id,
                    "lab": True,
                },
                "depends_on": [],
                "status": status if status in {"done", "failed", "blocked"} else "blocked",
                "priority": 5,
                "scheduled_for": now,
            }
        ]
    )
    job = inserted[0]
    marketing_store.mark_job(
        job["id"],
        status=status if status in {"done", "failed", "blocked"} else "blocked",
        result=outcome.get("result") or outcome,
        proof=outcome.get("proof") or {},
        error_message=outcome.get("error"),
        block_reason=outcome.get("block_reason"),
        executor=f"ads_{platform}",
    )
    try:
        append_lab_log(
            {
                "level": "blocked" if not outcome.get("ok") else "success",
                "category": "NeedsYou" if outcome.get("block_reason") else "info",
                "action": "ads.enable",
                "workspace_id": workspace_id,
                "request": {"platform": platform, "resource_id": resource_id},
                "response": outcome,
                "error": outcome.get("error") or outcome.get("block_reason"),
            },
            workspace_id=workspace_id,
        )
    except Exception:
        pass
    return {"ok": bool(outcome.get("ok")), "outcome": outcome, "job_id": job["id"]}


def start_mail_connect(
    workspace_id: str,
    app_user_id: str | None = None,
) -> dict[str, Any]:
    """One-click Composio Gmail connect — returns redirect_url for the founder."""
    from app.gtm.supymem.pipeline import start_gmail_connect

    _ = app_user_id
    result = start_gmail_connect(workspace_id)
    append_lab_log(
        {
            "level": "info",
            "category": "NeedsYou" if result.get("redirect_url") else "info",
            "action": "mail.connect.start",
            "request": {"app_user_id": result.get("app_user_id") or "namespaced"},
            "response": {
                "connection_id": result.get("connection_id"),
                "has_redirect": bool(result.get("redirect_url")),
                "app_user_id": result.get("app_user_id"),
            },
        },
        workspace_id=workspace_id,
    )
    return result


def mail_status(workspace_id: str) -> dict[str, Any]:
    from app.gtm.supymem.pipeline import mail_readiness, sync_gmail_accounts

    try:
        synced = sync_gmail_accounts(workspace_id)
    except Exception as exc:  # noqa: BLE001
        synced = {"error": str(exc)[:240], "accounts": [], "synced": 0}
    ready = mail_readiness(workspace_id)
    return {**ready, "sync": synced}


def send_lab_test_email(
    *,
    workspace_id: str,
    to: str,
    subject: str,
    body: str,
    confirm: bool = False,
    approval_id: str | None = None,
    mailbox_id: str | None = None,
) -> dict[str, Any]:
    """Lab harness for a single real (or dry-run) Gmail send — no prospect pipeline."""
    from app.gtm.supymem.pipeline import send_test_email
    from app.connections.mailboxes import enabled

    outcome = send_test_email(
        workspace_id,
        to=to,
        subject=subject,
        body=body,
        confirm=confirm,
        approval_id=approval_id,
        **({"mailbox_id": mailbox_id} if enabled() else {}),
    )
    level = "success" if outcome.get("ok") else (
        "NeedsYou" if "NeedsYou" in str(outcome.get("block_reason") or "") else "error"
    )
    append_lab_log(
        {
            "level": level if level != "NeedsYou" else "warning",
            "category": level,
            "action": "mail.test_send",
            "workspace_id": workspace_id,
            "request": {
                "to": to,
                "subject": subject,
                "body_len": len(body or ""),
                "confirm": confirm,
            },
            "response": {
                k: v
                for k, v in outcome.items()
                if k not in {"raw"}
            },
            "error": outcome.get("error") or outcome.get("block_reason"),
        },
        workspace_id=workspace_id,
    )
    # Also write a compact verify artifact (no secrets).
    try:
        verify = _repo_root() / ".agents" / "verify_marketing" / "gmail_test_send.jsonl"
        verify.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "ts": utcnow().isoformat(),
            "workspace_id": workspace_id,
            "ok": bool(outcome.get("ok")),
            "to": to,
            "subject": subject,
            "from": outcome.get("from"),
            "provider_message_id": outcome.get("provider_message_id"),
            "dry_run": outcome.get("dry_run"),
            "error": outcome.get("error") or outcome.get("block_reason"),
        }
        with verify.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass
    return outcome
