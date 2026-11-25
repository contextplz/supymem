"""Scoped, declarative founder resources and truthful planning availability.

Only rows with source='founder_inventory' are managed here. Updating inventory
replaces those declarations for the exact venture and preserves imported assets.
A marker row uses kind='resource_inventory', name='Founder resource inventory',
and detail={complete, completed_at}; it is never exposed as an asset. Its stable
ID and the venture row lock serialize writes without a schema change.

Declarations do not prove access, compatibility, consent, usability, or savings.
Capability 'implemented' describes code support for a metered review workflow,
not provider health or execution authorization. Budget totals remain unknown.
No credential values are read, no probes run, and credit accounts are not created.
The caller must authenticate the actor; every operation checks workspace/venture
ownership independently before reading or changing scoped records.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date
from typing import Annotated, Any, Literal
from urllib.parse import unquote, urlsplit

from core.clock import utcnow
from core.ids import new_id
from guardrails.secrets import detect_secrets
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator
from sqlalchemy import select

from app.db.models import CreditAccount, Venture, Workspace
from app.db.supymem_models import ExistingAsset
from app.core.local_mode import is_local
from app.llm.founder_provider import resolve_llm_config
from app.secrets.detect import looks_like_secret
from app.state import db
from app.worker.handlers import UNAVAILABLE_HOSTED

MAX_ASSETS = 40
MAX_VISIBLE_ASSETS = 200
SOURCE = "founder_inventory"
MARKER_KIND = "resource_inventory"
MARKER_NAME = "Founder resource inventory"
SUPPORTED_CAPABILITIES = (
    "idea.clarify_turn",
    "idea.validate",
    "research.deep",
    "plan.compile",
)
UNAVAILABLE_CAPABILITIES = (
    "deploy.run",
    "infra.provision",
    "app.iterate",
)
AssetKind = Literal[
    "coding_agent",
    "ai_subscription",
    "aws_account",
    "aws_credits",
    "cloud_account",
    "cloud_credits",
    "api_subscription",
    "llm_key",
    "repo",
    "domain",
    "email_provider",
    "analytics",
    "brand_asset",
    "saas_subscription",
    "engineering_time",
]
Identifier = Annotated[
    str,
    StringConstraints(
        min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$"
    ),
]
Label = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)
]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class AssetDetail(StrictModel):
    provider: Label | None = None
    plan: Label | None = None
    region: Annotated[str, StringConstraints(max_length=40)] | None = None
    expires_on: (
        Annotated[str, StringConstraints(pattern=r"^\d{4}-\d{2}-\d{2}$")] | None
    ) = None
    currency: Literal["USD", "INR", "EUR", "GBP", "CAD", "AUD", "SGD", "JPY"] | None = (
        None
    )
    credit_amount: (
        Annotated[str, StringConstraints(pattern=r"^\d{1,9}(?:\.\d{1,2})?$")] | None
    ) = None
    hours_per_week: int | None = Field(default=None, ge=0, le=168)
    url: Annotated[str, StringConstraints(max_length=500)] | None = None
    notes: Annotated[str, StringConstraints(max_length=500)] | None = None

    @model_validator(mode="after")
    def safe_metadata(self):
        if self.expires_on:
            date.fromisoformat(self.expires_on)
        if self.url:
            try:
                parsed = urlsplit(self.url)
                if (
                    parsed.scheme not in {"http", "https"}
                    or not parsed.hostname
                    or parsed.username is not None
                    or parsed.password is not None
                    or parsed.query
                    or parsed.fragment
                    or parsed.port is not None
                    or any(ord(char) < 33 for char in self.url)
                    or "\\" in self.url
                    or _secret_text(unquote(parsed.path))
                ):
                    raise ValueError(
                        "resource URL must be a plain HTTP(S) URL without credentials"
                    )
            except ValueError:
                raise ValueError(
                    "resource URL must be a plain HTTP(S) URL without credentials"
                ) from None
        for value in self.model_dump(exclude_none=True).values():
            if isinstance(value, str) and _secret_text(value):
                raise ValueError(
                    "resource inventory accepts metadata only; use the secure credential form"
                )
        return self


class AssetDeclaration(StrictModel):
    id: Identifier | None = None
    kind: AssetKind
    name: Label
    exists: bool = True
    detail: AssetDetail = Field(default_factory=AssetDetail)

    @model_validator(mode="after")
    def safe_name(self):
        if _secret_text(self.name):
            raise ValueError(
                "resource inventory accepts metadata only; use the secure credential form"
            )
        return self


def _secret_text(value: str) -> bool:
    return (
        looks_like_secret(value)
        or bool(detect_secrets(value))
        or "PRIVATE KEY-----" in value
    )


def _identifier(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", value
    ):
        raise ValueError("resource scope id is invalid")
    return value


def _venture(
    session, workspace_id: str, venture_id: str, *, lock: bool = False
) -> Venture:
    workspace_id, venture_id = _identifier(workspace_id), _identifier(venture_id)
    workspace = session.get(Workspace, workspace_id)
    query = select(Venture).where(
        Venture.id == venture_id, Venture.workspace_id == workspace_id
    )
    if lock:
        query = query.with_for_update()
    venture = session.scalar(query)
    if (
        workspace is None
        or workspace.archived_at
        or venture is None
        or venture.archived_at
    ):
        raise ValueError("resource scope not found")
    return venture


def _marker_id(workspace_id: str, venture_id: str) -> str:
    digest = hashlib.sha256(f"{workspace_id}\0{venture_id}".encode()).hexdigest()[:32]
    return f"rin_{digest}"


def _json_object(raw: str) -> dict[str, Any]:
    if not isinstance(raw, str) or len(raw) > 30_000:
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _public_detail(raw: str) -> dict[str, Any]:
    # Imported reports may contain paths, raw scans, or arbitrary metadata. Expose
    # only the same safe declarative fields accepted by the inventory form.
    data = _json_object(raw)
    selected = {
        key: value for key, value in data.items() if key in AssetDetail.model_fields
    }
    try:
        return AssetDetail.model_validate(selected).model_dump(exclude_none=True)
    except ValueError:
        return {}


def _inventory(session, workspace_id: str, venture_id: str) -> dict[str, Any]:
    rows = session.scalars(
        select(ExistingAsset)
        .where(
            ExistingAsset.workspace_id == workspace_id,
            ExistingAsset.venture_id == venture_id,
        )
        .order_by(ExistingAsset.created_at, ExistingAsset.id)
        .limit(MAX_VISIBLE_ASSETS + 2)
    ).all()
    if len(rows) > MAX_VISIBLE_ASSETS + 1:
        raise ValueError("resource inventory exceeds the readable asset limit")
    complete = False
    assets = []
    for row in rows:
        if row.kind == MARKER_KIND:
            if row.id == _marker_id(workspace_id, venture_id) and row.source == SOURCE:
                complete = _json_object(row.detail).get("complete") is True
            continue
        declared = row.source == SOURCE
        tasks = _json_object('{"tasks":' + (row.usable_tasks or "[]") + "}").get(
            "tasks"
        )
        tasks = (
            [
                task
                for task in tasks
                if isinstance(task, str)
                and re.fullmatch(r"[a-z][a-z0-9_.-]{0,100}", task)
            ][:30]
            if isinstance(tasks, list)
            else []
        )
        assets.append(
            {
                "id": row.id,
                "kind": row.kind,
                "name": "Resource" if _secret_text(row.name) else row.name[:120],
                "exists": bool(row.exists),
                "detail": _public_detail(row.detail),
                "source": row.source,
                "accessible": None if declared else row.accessible,
                "compatible": None if declared else row.compatible,
                "usable_tasks": [] if declared else tasks,
                "usable": None if declared else row.usable,
                "credits_saved_estimate": None
                if declared
                else row.credits_saved_estimate,
                "verified_at": None
                if declared or row.verified_at is None
                else row.verified_at.isoformat(),
            }
        )
    if len(assets) > MAX_VISIBLE_ASSETS:
        raise ValueError("resource inventory exceeds the readable asset limit")
    return {"complete": complete, "assets": assets}


def _provider_metadata(workspace_id: str) -> dict[str, Any]:
    try:
        configuration = resolve_llm_config(workspace_id)
        provider, model = configuration.provider, configuration.model
        if any(
            not isinstance(value, str)
            or not value.strip()
            or len(value) > 128
            or _secret_text(value)
            for value in (provider, model)
        ):
            raise ValueError("invalid configuration metadata")
        return {
            "provider": provider,
            "model": model,
            "credential_source": "workspace"
            if configuration.source == "founder_override"
            else "platform",
            "credential_state": "reference_present"
            if configuration.api_key_ref
            else "not_checked",
            "health": "unverified",
        }
    except Exception as exc:
        from app.chat.recovery import log_founder_exception

        log_founder_exception(exc, None, "planning.resources.model_metadata")
        # Configuration exceptions can include sensitive endpoint/key details.
        return {
            "provider": None,
            "model": None,
            "credential_source": "unknown",
            "credential_state": "not_checked",
            "health": "unverified",
        }


def publication_contract(venture_id: str) -> dict[str, Any]:
    """Describe the reviewed Website workflow, never authorize a deployment."""
    if is_local():
        from app.core.local_capabilities import capability
        publication = capability("publish_pages")
        configured = publication["state"] == "ready"
        return {"kind": "local_publication", "configured": configured,
            "provider_health": "unverified", "review_path": f"/v/{venture_id}/site",
            "steps": ["Open Website and build your page.", "Review your page and test its contact link.",
                "Return to Website and choose Publish." if configured else publication["next_step"]],
            "required_evidence": ["reviewed page", "working contact link", "your saved publishing settings"],
            "limitations": "Building and previewing your page needs only your model key. Publishing uses your own hosting account; provider health is checked when you publish."}
    from app.hosting.service import HostingConfig, HostingError

    try:
        HostingConfig.from_environment()
        configured = True
    except HostingError:
        configured = False
    return {
        "kind": "reviewed_static_publication",
        "configured": configured,
        "provider_health": "unverified",
        "review_path": f"/v/{venture_id}/site",
        "steps": [
            "Open Website and review the exact accepted private build, including its images and conversion link.",
            "Enable the venture waitlist if closed, then use Test public submission if verification is missing or expired.",
            "Choose Publish accepted build, review the confirmation, then choose Publish now.",
            "Wait for a verified live deployment and public URL; a queued job is not publication evidence.",
        ],
        "required_evidence": [
            "accepted immutable build with independent QA",
            "reviewed landing contract containing this venture's exact conversion link",
            "current public waitlist submission and storage verification",
            "founder publication confirmation",
        ],
        "limitations": "Static sites only. This is a separate founder-confirmed Website workflow, not general application deployment, database provisioning or permission to enqueue a legacy deploy.run task. Configuration does not prove provider health or that this venture has published.",
    }


def snapshot(workspace_id: str, venture_id: str, *, include_builder_runtime=True) -> dict[str, Any]:
    with db()() as session:
        _venture(session, workspace_id, venture_id)
        inventory = _inventory(session, workspace_id, venture_id)
        if is_local():
            provider = _provider_metadata(workspace_id)
            ready = provider["provider"] is not None
            try:
                from app.core.local_capabilities import enabled, blocked_message
            except ImportError:
                # shortcut: only saved setup is checked, use capability switches when the shared module lands.
                research_ready = landing_ready = ready
                research_reason = landing_reason = "Open Keys and connections and save your model-provider key, then retry."
            else:
                research_ready, landing_ready = enabled("research"), enabled("landing_pages")
                research_reason, landing_reason = blocked_message("research"), blocked_message("landing_pages")
            capabilities = [{"id": name, "state": "implemented" if ready else "unavailable", "metered": True,
                "reason": "Uses your saved model provider." if ready else "Open Keys and connections and save your model-provider key, then retry.",
                "provider_configuration": dict(provider)} for name in SUPPORTED_CAPABILITIES]
            research = next(item for item in capabilities if item["id"] == "research.deep")
            research.update(state="implemented" if research_ready else "unavailable", reason=research_reason)
            capabilities.append({"id": "firstlook.landing", "state": "implemented" if landing_ready else "unavailable",
                "metered": False, "supported_milestone_kinds": ["landing"],
                "reason": "Build and preview your page with your connected coding tool. Publishing is a separate action you request." if landing_ready else landing_reason,
                "provider_configuration": None})
            return {"inventory": inventory, "capabilities": capabilities,
                "managed_publication": publication_contract(venture_id),
                "credit_account": {"state": "local", "available_micro": None, "reserved_micro": None, "spent_micro": None},
                "execution_budget": {"status": "local", "upfront_credits": None, "monthly_credits": None,
                    "gaps": ["Your model provider bills your own account. Set your monthly spend cap in Keys and connections.",
                        "Open Plan and record your available tools and time before running work that needs them."]}}
        from app.credits.accounts import account_can_spend, account_for_venture
        account = account_for_venture(venture_id, session=session)
        if account and not account_can_spend(session, account, workspace_id, venture_id):
            account = None
        credit_account = {
            "state": account.status if account else "missing",
            "available_micro": account.granted_micro
            - account.spent_micro
            - account.reserved_micro
            if account
            else None,
            "reserved_micro": account.reserved_micro if account else None,
            "spent_micro": account.spent_micro if account else None,
        }
    provider = (_provider_metadata(workspace_id) if include_builder_runtime else {
        "provider": None, "model": None, "credential_source": "outside_source_qa_scope",
        "credential_state": "not_checked", "health": "unverified"})
    from app.builder.budgets import runtime_quote

    build_runtime = (runtime_quote(workspace_id) if include_builder_runtime else
                     {"ready": False, "reasons": ["Managed builder is outside source-QA scope."]})
    capabilities = [
        {
            "id": capability,
            "state": "implemented",
            "metered": True,
            "reason": "Implemented metered review workflow; this inventory does not verify provider health or authorize a call.",
            "provider_configuration": dict(provider),
        }
        for capability in SUPPORTED_CAPABILITIES
    ]
    capabilities.append({
        "id": "coding.subscription_static",
        "state": "implemented", "metered": False,
        "supported_milestone_kinds": ["landing"],
        "reason": "Prepare a founder-reviewed native Codex or Claude subscription task for a static landing page. Local authentication, supported sandbox, exact approved copy contract and source custody are checked when it runs. No paid Supymem builder or model is launched. Captured source is unverified: independent QA, founder acceptance and publication require separate reviewed admissions.",
        "included_work": ["reviewed native static source preparation", "immutable source custody"],
        "excluded_work": ["backend or product implementation", "independent QA before its own admission", "public publication", "payment processing"],
        "provider_configuration": None,
    })
    capabilities.append({
        "id": "coding.subscription_product_source", "state": "unavailable", "metered": False,
        "supported_milestone_kinds": ["core"],
        "reason": "Native product-source preparation is not executable yet: reviewed product source import, independent runtime QA, and managed authentication/database/hosting gates are not connected. CLI installation or saved service choices do not make these gates ready.",
        "included_work": [],
        "excluded_work": ["native execution", "verified product functionality", "runtime QA", "public hosting", "external integrations", "payments"],
        "provider_configuration": None,
    })
    capabilities += [
        {
            "id": "build.run",
            "state": "implemented" if build_runtime["ready"] else "unavailable",
            "metered": bool(build_runtime["ready"]),
            "reason": (
                "Reviewed isolated landing or core-product implementation, profile-specific independent QA, immutable artifact capture, and private founder review are one bounded workflow. Core products continue through app-scoped database, authentication, immutable image, migration and staged service provisioning after acceptance. Use build.run only for landing or core milestones; do not add a separate qa.run task. Iteration, unverified third-party integrations, public publication, and payment work are not included."
                if build_runtime["ready"]
                else UNAVAILABLE_HOSTED["build.run"]
            ),
            "included_work": (
                [
                    "isolated static landing or authenticated core-product implementation",
                    "profile-specific independent QA",
                    "immutable artifact capture",
                    "private founder review",
                    "accepted core-product staging through isolated managed runtime",
                ]
                if build_runtime["ready"]
                else []
            ),
            "supported_milestone_kinds": ["landing", "core"] if build_runtime["ready"] else [],
            "excluded_work": [
                "product iteration",
                "unverified third-party integration",
                "public publication before runtime QA and founder review",
                "payment implementation",
            ],
            "provider_configuration": dict(provider) if build_runtime["ready"] else None,
        }
    ]
    capabilities += [
        {
            "id": capability,
            "state": "unavailable",
            "metered": False,
            "reason": (
                "The general deployment executor is unavailable. Consult managed_publication for the separate founder-confirmed static Website workflow."
                if capability == "deploy.run" else UNAVAILABLE_HOSTED.get(
                    capability, "Hosted execution is not integrated."
                )
            ),
            "provider_configuration": None,
        }
        for capability in UNAVAILABLE_CAPABILITIES
    ]
    # No firstlook.landing capability: landing milestones are always built (coding.subscription_static / build.run).
    gaps = [
        "Monthly infrastructure, app AI usage, and third-party costs require a scoped workload and observed pricing.",
        "Declared assets do not establish access, compatibility or consent.",
    ]
    if not inventory["complete"]:
        gaps.insert(0, "Founder resource inventory is incomplete.")
    if not build_runtime["ready"]:
        gaps.insert(
            0,
            "Build and hosted execution costs are not established for an available executor.",
        )
    return {
        "inventory": inventory,
        "capabilities": capabilities,
        "managed_publication": publication_contract(venture_id),
        "credit_account": credit_account,
        "execution_budget": {
            "status": "bounded" if build_runtime["ready"] else "unknown",
            "upfront_credits": (
                build_runtime["ceiling_micro"] / 10_000
                if build_runtime["ready"]
                else None
            ),
            "monthly_credits": None,
            "gaps": gaps,
        },
    }


def update_inventory(
    workspace_id: str, venture_id: str, assets: list[dict[str, Any]], complete: bool
) -> dict[str, Any]:
    """Atomically replace this venture's declarations; never probe or store keys."""
    if (
        type(complete) is not bool
        or not isinstance(assets, list)
        or len(assets) > MAX_ASSETS
    ):
        raise ValueError(
            "resource inventory requires at most 40 assets and a boolean completion state"
        )
    try:
        declared = [AssetDeclaration.model_validate(asset) for asset in assets]
    except ValueError:
        raise ValueError(
            "resource inventory declaration is invalid; provide metadata without credentials"
        ) from None
    identifiers = [asset.id for asset in declared if asset.id]
    names = [(asset.kind, asset.name.casefold()) for asset in declared]
    if len(set(identifiers)) != len(identifiers) or len(set(names)) != len(names):
        raise ValueError("resource inventory assets must be unique")
    with db()() as session:
        _venture(session, workspace_id, venture_id, lock=True)
        rows = session.scalars(
            select(ExistingAsset).where(
                ExistingAsset.workspace_id == workspace_id,
                ExistingAsset.venture_id == venture_id,
                ExistingAsset.source == SOURCE,
                ExistingAsset.kind != MARKER_KIND,
            )
        ).all()
        owned = {row.id: row for row in rows}
        by_name = {(row.kind, row.name.casefold()): row for row in rows}
        if any(identifier not in owned for identifier in identifiers):
            raise ValueError(
                "resource inventory asset not found in this venture's declarations"
            )
        now = utcnow()
        retained = set()
        for asset in declared:
            row = (
                owned.get(asset.id)
                if asset.id
                else by_name.get((asset.kind, asset.name.casefold()))
            )
            if row is None:
                row = ExistingAsset(
                    id=new_id("ast"),
                    workspace_id=workspace_id,
                    venture_id=venture_id,
                    source=SOURCE,
                    created_at=now,
                )
                session.add(row)
            if row.id in retained:
                raise ValueError(
                    "resource inventory assets resolve to a duplicate record"
                )
            retained.add(row.id)
            row.kind, row.name, row.exists = asset.kind, asset.name, asset.exists
            row.detail = json.dumps(asset.detail.model_dump(exclude_none=True))
            row.accessible = row.compatible = row.usable = None
            row.usable_tasks = "[]"
            row.credits_saved_estimate = None
            row.verified_at = None
        for identifier, row in owned.items():
            if identifier not in retained:
                session.delete(row)
        marker_id = _marker_id(workspace_id, venture_id)
        marker = session.get(ExistingAsset, marker_id)
        if marker is not None and (
            marker.workspace_id != workspace_id
            or marker.venture_id != venture_id
            or marker.source != SOURCE
            or marker.kind != MARKER_KIND
        ):
            raise ValueError("resource inventory marker scope conflict")
        if marker is None:
            marker = ExistingAsset(
                id=marker_id,
                workspace_id=workspace_id,
                venture_id=venture_id,
                kind=MARKER_KIND,
                name=MARKER_NAME,
                source=SOURCE,
                exists=True,
                created_at=now,
            )
            session.add(marker)
        marker.detail = json.dumps(
            {
                "complete": complete,
                "completed_at": now.isoformat() if complete else None,
            }
        )
        session.flush()
        inventory = _inventory(session, workspace_id, venture_id)
        session.commit()
        return inventory
