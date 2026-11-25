"""Mount all Company OS API routers.

Phase 10 modules are discovered from :data:`ROUTER_MODULES` so a new feature
adds one name here rather than editing an import list in three places.
"""

from __future__ import annotations

import importlib
import logging

from fastapi import APIRouter, FastAPI

log = logging.getLogger(__name__)

# Order matters only where paths overlap; more specific routers first.
ROUTER_MODULES: tuple[str, ...] = (
    "ui_config_api",
    "company_home_api",
    "idea_page_api",
    "next_step_api",
    # Phases 0-9
    "ventures",
    "startup_review_api",
    "venture_assets",
    "intake",
    "clarify",
    "gstack",
    "builder",
    "validation",
    "charter",
    "org",
    "agents",
    "build",
    "concierge",
    "gtm",
    "feedback",
    "experiments_api",
    "exploration_api",
    "evolution",
    "worldmodel",
    "governance",
    "events",
    "events_ingest",
    "onboarding",
    "activity",
    "dashboard",
    "chat_api",
    "voice_api",
    "greeting_api",
    "tasks_api",
    "routines_api",
    "handoffs_api",
    "idea_api",
    "plans_api",
    "apps_api",
    "imports_api",
    "build_runtime_api",
    "build_intent_api",
    "jobs_api",
    "credits_api",
    "spend_api",
    "billing_api",
    # Phase 10 (docs 35-47)
    "workspaces_api",
    "entry_api",
    "engagements_api",
    "firstlook_api",
    "pagebuilder_api",
    "pagebuilder_admission_api",
    "simulation_api",
    "idealab_api",
    "connections_api",
    "llm_settings_api",
    "local_tools_api",
    "watch_api",
    "coding_api",
    "desktop_api",
    "computer_use_api",
    "device_orders_api",
    "linkedin_api",
    "browser_qualification_api",
    "dossiers_api",
    "research_api",
    "knowledge_api",
    "waitlist_api",
    "beta_api",
    "email_api",
    "error_reports_api",
    "autopilot_api",
    "suggestions_api",
    "needs_you_api",
    "workflows_api",
    "approvals_api",
    "members_api",
    "audit_api",
    "boundary_api",
    "company_api",
    "v1",
    "ops_api",
    "outreach_ops",
    "gtm_god_ops",
    "internal_ops",
    "internal_tick",
    "payments_api",
    "hermes_api",
    "openai_api",
    # Legacy consumer_api/hosted_llm_api use a separate account-token identity.
    # They remain unmounted until billing and account records are explicitly
    # reconciled with gateway founder sessions and the desktop PKCE contract.
)


HOSTED_ONLY_MODULES = frozenset({
    "beta_api", "build_runtime_api", "build_intent_api", "internal_ops", "internal_tick", "payments_api",
    "hermes_api", "openai_api",
})


def mount_routers(app: FastAPI) -> None:
    from app.core.local_mode import is_local

    local = is_local()
    modules = ROUTER_MODULES + (("local_settings", "local_capabilities_api") if local else ())
    for name in modules:
        if local and name in HOSTED_ONLY_MODULES:
            continue
        mod_name = f"app.api.{name}"
        try:
            mod = importlib.import_module(mod_name)
        except ModuleNotFoundError as exc:
            missing = exc.name or ""
            # Optional: the router file has not landed yet (`app.api.<name>`).
            # Nested misses (`app.build`, a third-party package, …) are broken
            # code and must not be skipped via `exc.name.endswith(name)`.
            if missing in {mod_name, name}:
                log.debug("router not present yet: %s", name)
                continue
            log.error(
                "router %s failed to import: missing dependency %r (not an optional router)",
                name,
                missing,
            )
            raise
        router = getattr(mod, "local_router" if local and name == "billing_api" else "router", None)
        if isinstance(router, APIRouter):
            app.include_router(router)
        extra = getattr(mod, "ROUTERS", None)
        if isinstance(extra, (list, tuple)):
            for item in extra:
                if isinstance(item, APIRouter):
                    app.include_router(item)
        hosted_router = getattr(mod, "hosted_router", None)
        if not local and isinstance(hosted_router, APIRouter):
            app.include_router(hosted_router)
