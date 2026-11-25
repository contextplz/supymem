"""Single source of truth for feature labels used by usage and error analytics.

One edit here adds a feature everywhere: the ingest label list, control and chat
action mappings, job kind and route attribution, and the reference tables the
report views join to (see ``app.analytics.rollup.sync_reference_tables``).

A slug is the canonical label (``FEATURES`` key). ``group`` is the coarse legacy
label that older app versions still send; history is never rewritten, the views
map old rows to slugs through ``control``/``tool``/``label`` entries below.
Everything here is a closed, content-free label: no ids, text or PII.
"""

from __future__ import annotations

import re

from app.analytics.control_catalog import CONTROL_FEATURE

# Labels older clients already send. All of them stay accepted by ingest.
LEGACY_LABELS = (
    "ventures", "onboarding", "idea", "research", "cases", "planning", "builds", "qa", "website",
    "analytics", "gtm", "content", "community", "chat", "computer_use", "credits", "payments",
    "settings", "notifications", "import", "execution", "team", "feedback", "shell",
)

# slug -> (legacy group, title)
FEATURES: dict[str, tuple[str, str]] = {
    "ventures": ("ventures", "Ventures hub, leads, run status"),
    "onboarding": ("onboarding", "Chat entry and onboarding"),
    "idea": ("idea", "Idea clarification and validation"),
    "research": ("research", "Research and verdict"),
    "browse": ("research", "Read-only public web browsing"),
    "idea_board": ("idea", "AI idea pressure test"),
    "competitor_grid": ("research", "Competitor grid and cited review themes"),
    "cases": ("cases", "Case studies"),
    "planning": ("planning", "Charter, plan, slate"),
    "builds": ("builds", "Builds and app iterate"),
    "qa": ("qa", "Build evidence and QA"),
    "website": ("website", "Website preview and revision"),
    "analytics": ("analytics", "Analytics page"),
    "gtm": ("gtm", "Go to market"),
    "content": ("content", "Content studio and social"),
    "community": ("community", "Community"),
    "chat": ("chat", "Chat with Charlie"),
    "computer_use": ("computer_use", "Mac tasks (computer use)"),
    "credits": ("credits", "Credits summary and token meter"),
    "payments": ("payments", "Payments and finance ops"),
    "settings": ("settings", "Settings, connections, autonomy"),
    "notifications": ("notifications", "Needs you and approvals"),
    "import": ("import", "Import existing repo"),
    "execution": ("execution", "Now and Work views, agents"),
    "team": ("team", "Team, people, invites"),
    "feedback": ("feedback", "Feedback and bug report"),
    "shell": ("shell", "Desktop shell and boot (errors only)"),
    # Finer labels. Clients send them only after the server accepts them.
    "experiments": ("idea", "Experiments ledger and chat"),
    "price_band": ("idea", "Price range from real answers"),
    "unit_economics": ("planning", "Customer-month unit economics"),
    "waitlist": ("website", "Waitlist"),
    "publish": ("website", "Publish review and confirm"),
    "hosting": ("builds", "Host my product (free hosting, backend choice, URL check)"),
    "outreach": ("gtm", "Outreach drafts and ops"),
    "gtm_diagnosis": ("gtm", "GTM diagnosis"),
    "voice": ("chat", "Voice chat"),
    "greeting": ("shell", "Greeting and first name"),
    "tray": ("shell", "Tray menu and mascot"),
    "updater": ("shell", "Auto-updater"),
    "permissions": ("settings", "Set up this Mac permissions"),
    "local_tools": ("settings", "Local tools and Docker"),
    "account": ("credits", "Account sign-in and reconnect"),
    "venture_admin": ("ventures", "Rename, pause, delete, restore"),
    "simulate": ("planning", "Simulate"),
    "billing": ("credits", "Top up and plans"),
    "llm_settings": ("settings", "Bring your own model key"),
    "guards": ("chat", "Chat guards and claims refusals"),
    "recovery": ("chat", "Never-stuck recovery: failures, other ways, founder-provided results"),
    "god_mode": ("settings", "God mode early-access request"),
    "suggestions": ("notifications", "Suggestions: tasks Supymem offers to take over"),
    "email": ("notifications", "Email to users: sends, bounces, preferences"),
}
# Stored or sent under another name; reports show the canonical slug.
LABEL_ALIAS = {"voice_chat": "voice", "mac_tasks": "computer_use"}
# Every label the ingest endpoint accepts.
INGEST_FEATURES = (*FEATURES, *LABEL_ALIAS)

# ---------------------------------------------------------------- inventory
# INVENTORY.md feature id -> slug. tests/unit/analytics/test_feature_catalog.py
# fails when a new id appears in the inventory without an entry here.
_AREAS = {
    "A": "shell account account account greeting greeting tray tray tray tray permissions local_tools "
         "updater settings settings settings feedback shell",
    "B": "onboarding ventures idea idea idea idea idea cases research research research research import simulate",
    "C": "planning planning planning planning planning planning planning planning execution execution builds "
         "builds qa execution notifications execution builds",
    "D": "website website website website publish website waitlist waitlist ventures website analytics credits "
         "guards guards",
    "E": "gtm_diagnosis gtm_diagnosis gtm_diagnosis gtm_diagnosis gtm_diagnosis gtm outreach content content content "
         "settings content content settings content outreach",
    "F": "experiments experiments experiments experiments experiments experiments experiments experiments planning",
    "G": "ventures chat credits research execution planning research planning execution planning planning planning "
         "content gtm_diagnosis settings chat chat chat idea planning website experiments execution chat computer_use "
         "chat guards chat notifications voice",
    "H": "computer_use " * 11,
    "I": "venture_admin venture_admin venture_admin settings settings venture_admin venture_admin credits billing "
         "billing credits llm_settings settings team team notifications ventures",
}
INVENTORY_SLUG: dict[str, str] = {
    f"{area}{i}": slug for area, row in _AREAS.items() for i, slug in enumerate(row.split(), 1)
}

# ------------------------------------------------------------------ controls
# Fine slug for controls whose legacy group was too coarse.
_CONTROL_OVERRIDE = {
    **{c: "waitlist" for c in CONTROL_FEATURE if c.startswith("waitlist_")},
    **{c: "publish" for c in CONTROL_FEATURE if c.startswith("website_publish_")},
    **{c: "gtm_diagnosis" for c in CONTROL_FEATURE if c.startswith(("gtm_diagnosis_", "gtm_next_"))
       or c in {"gtm_companies_refresh", "gtm_company_select", "gtm_run_select"}},
    **{c: "experiments" for c in CONTROL_FEATURE if c.startswith("experiment_")},
    "settings_permission_screen_request": "permissions", "settings_accessibility_request": "permissions",
    "settings_permissions_refresh": "permissions", "settings_local_tools_redetect": "local_tools",
    "settings_account_reconnect": "account",
}
# Controls the clients will tag in later releases (tray/native verbs, settings
# writes, voice, greeting, verdict buttons). Accepted now so the server always
# deploys before the client that sends them.
NEW_CONTROL_SLUG = {
    "charlie_role_preset": "settings", "charlie_role_preset_save": "settings",
    "inbox_briefing_consent": "settings", "inbox_briefing_settings_save": "settings",
    "inbox_briefing_window": "settings", "inbox_briefing_window_add": "settings", "inbox_briefing_window_remove": "settings",
    "inbox_briefing_quiet_start": "settings", "inbox_briefing_quiet_end": "settings", "inbox_briefing_email_copy": "settings",
    "inbox_briefing_feedback": "feedback", "inbox_briefing_action": "chat", "inbox_briefing_open_gmail": "chat",
    "cos_brief_action": "chat", "cos_brief_open": "chat",
    "approval_always": "notifications", "approval_deny": "notifications", "approval_revoke": "settings",
    "chat_question_detail": "idea",
    "referrals_show": "credits", "referrals_copy_link": "credits", "referrals_copy_code": "credits",
    "venture_switch": "venture_admin", "venture_rename": "venture_admin", "venture_delete": "venture_admin",
    "venture_restore": "venture_admin", "venture_pause": "venture_admin", "venture_resume": "venture_admin",
    "settings_emergency_stop": "settings", "settings_autonomy_change": "settings", "settings_memory_delete": "settings",
    "settings_god_mode_toggle": "settings", "settings_god_mode_save": "settings", "settings_god_mode_request": "god_mode",
    "team_org_activate": "team", "team_invite_send": "team", "team_invite_accept": "team",
    "llm_settings_test": "llm_settings", "llm_settings_save": "llm_settings", "llm_settings_use_ours": "llm_settings",
    "experiments_start_suggested": "experiments", "experiments_open": "experiments", "experiments_learned_open": "experiments",
    "market_beliefs_prepare": "experiments",
    "linkedin_set_audience": "gtm",
    "voice_mic_press": "voice", "voice_end": "voice",
    "reasoning_deep_answer": "chat", "reasoning_continue": "chat",
    "greeting_name_submit": "greeting", "greeting_tap": "greeting",
    "mac_result_looks_right": "computer_use", "mac_result_something_wrong": "computer_use",
    "permissions_coach_open": "permissions", "permissions_do_later": "permissions",
    "local_tools_docker_install": "local_tools", "local_tools_builder_install": "local_tools",
    "billing_topup": "billing", "billing_plan_subscribe": "billing",
    "website_logo_upload": "website", "website_export_html": "website", "feedback_bug_report": "feedback",
    "tray_show": "tray", "tray_new_venture": "tray", "tray_setup_mac": "tray", "tray_pause": "tray",
    "tray_resume": "tray", "tray_check_updates": "tray", "tray_reveal_data": "tray", "tray_quit": "tray",
    "mascot_show": "tray", "mascot_menu_action": "tray",
    "emergency_stop": "computer_use", "emergency_stop_menu": "computer_use",
    "emergency_stop_shortcut": "computer_use", "emergency_stop_cursor": "computer_use",
    "updater_check": "updater", "updater_offered": "updater", "updater_installed": "updater",
    "updater_failed": "updater", "autostart_toggle": "settings", "mac_notice_test": "settings",
    "account_sign_in_start": "account", "account_sign_in_code": "account", "account_sign_out": "account",
    # Added with the console/native client coverage. Several were already tagged in the console but unknown here.
    "next_step_action": "ventures", "research_next_step": "research", "idea_gstack_skip": "idea",
    "settings_manager_rename": "settings", "credits_request": "credits", "mac_cancel": "computer_use",
    "permissions_background_input_request": "permissions",
    "greeting_shown": "greeting", "greeting_replayed": "greeting",
    "greeting_audio_failed": "greeting", "greeting_voice_fallback": "greeting",
    "greeting_ctx_suspended": "greeting", "greeting_decode_failed": "greeting",
    "voice_session_start": "voice", "permissions_granted": "permissions", "tray_help": "tray",
    "tray_setup_permissions": "tray",
}
CONTROL_SLUG = {**{c: _CONTROL_OVERRIDE.get(c, g) for c, g in CONTROL_FEATURE.items()}, **NEW_CONTROL_SLUG}


def control_features(control_id: str) -> frozenset[str]:
    """Labels an event for this control may carry: the legacy group or the fine slug."""
    slug = CONTROL_SLUG.get(control_id)
    if slug is None:
        return frozenset()
    legacy = CONTROL_FEATURE.get(control_id)
    return frozenset({slug, legacy} if legacy else {slug})


# --------------------------------------------------------------- chat actions
# Chat tool (action name, read intent or reviewed route) -> slug. Every
# registered chat action must appear (enforced by test).
ACTION_SLUG = {
    "routines.create": "settings", "routines.list": "settings", "routines.pause": "settings",
    "routines.delete": "settings", "routines.approve": "settings",
    "approvals.revoke": "settings",
    "inbox.briefing.consent": "settings", "inbox.briefing.settings": "settings", "inbox.item.feedback": "feedback",
    "connections.context.select_documents": "settings", "connections.feedback.select_sources": "settings",
    "idea.pressure_test.prepare": "idea_board", "idea.pressure_test": "idea_board",
    "idea.pressure_test.accept": "idea_board", "research.competitors": "competitor_grid",
    "price_band.calculate": "price_band", "unit_economics.calculate": "unit_economics",
    "entry.direction.confirm": "onboarding", "entry.direction.back": "onboarding", "entry.ideas.discover": "idea",
    "referrals.show": "credits",
    "chat.reply": "chat", "chat.startFresh": "chat",
    "chat.prefill": "chat", "idea.review": "idea", "startup.review": "idea", "research.prepare": "research",
    "idea.page_refresh": "idea",
    "status": "ventures", "why": "chat", "money": "credits", "leads": "ventures", "research": "research",
    "research.start": "research", "research.update_and_rerun": "research", "agents": "execution", "whats_next": "planning", "next_moves": "planning", "refine_site": "website",
    "site.preview": "website", "site.rewrite": "website", "pagebuilder.build": "website", "site.publish": "publish", "site.unpublish": "publish",
    "waitlist.enable": "waitlist", "waitlist.close": "waitlist", "waitlist.test": "waitlist",
    "openworker_search": "research", "agent_reach": "research", "slate_override": "planning",
    "browse_web": "browse",
    "pause": "venture_admin", "resume": "venture_admin", "autopilot.god_on": "settings", "autopilot.god_off": "settings", "god_mode.request": "god_mode", "autopilot.allow_send": "settings", "autopilot.send_off": "settings", "venture.start_new": "ventures", "autopilot.set_cap": "settings", "autopilot.send_cap": "settings", "build.accept": "builds", "credits.request": "credits", "cancel": "planning", "build.cancel": "builds",
    "manager.rename": "venture_admin", "venture.rename": "venture_admin", "approve": "planning", "office_hours": "planning",
    "ceo_review": "planning", "brain_brief": "chat", "social_post": "content", "social_guide": "content",
    "memory.list": "chat", "memory.remember": "chat", "memory.correct": "chat", "memory.forget": "chat",
    "social_status": "content", "social_connect": "settings", "gtm.diagnosis": "gtm_diagnosis",
    "credits.balance": "credits", "connections.status": "settings", "connections.connect": "settings", "connections.disconnect": "settings", "content.drafts": "content",
    "idea.answer": "idea", "idea.confirm": "idea", "idea.explore": "idea", "idea.proposal.approve": "idea", "idea.proposal.edit": "idea",
    "plan.resources_none": "planning", "plan.quote": "planning", "plan.generate": "planning",
    "plan.approve": "planning", "build.start": "builds", "work.start": "execution", "build.prepare": "builds",
    "jobs.status": "execution", "job.retry": "execution", "experiment.run_review": "experiments", "outreach.draft": "outreach", "outreach.drafts": "outreach", "outreach.targets": "outreach", "outreach.send": "outreach", "outreach.delivery_resolve": "outreach", "gtm.find": "gtm", "gtm.status": "gtm", "gtm.approve": "outreach", "gtm.draft_address": "outreach", "gtm.draft_edit": "outreach", "gtm.draft_rewrite": "outreach", "gtm.send": "outreach", "contacts.preview": "gtm", "contacts.import": "gtm", "website.review": "website",
    "build.app.create": "builds", "scope.request": "planning", "app.iterate": "builds", "app.debug": "builds",
    "service.guide": "builds", "service.select": "builds", "product.record": "builds", "product.status": "builds",
    "host.choose": "hosting", "host.plan": "hosting", "host.verify": "hosting",
    "blocker.explain": "execution", "chat.model_retry": "chat", "mac.task.prepare": "computer_use", "build.tool.start": "builds", "experiment.list": "experiments",
    "experiment.start": "experiments", "experiment.record": "experiments", "experiment.revise": "experiments",
    "experiment.export_sheet": "experiments", "experiment.review": "experiments",
    "experiment.persevere": "experiments", "experiment.pivot": "experiments",
    "experiment.spinoff": "experiments", "experiment.kill": "experiments",
    "experiment.prepare": "experiments", "experiment.run": "experiments",
    "experiment.exploration.start": "experiments", "experiment.exploration.retry": "experiments",
    "experiment.exploration.continue": "experiments", "experiment.exploration.next": "experiments",
    "experiment.exploration.next_round": "experiments",
    "experiment.exploration.select": "experiments",
    "experiment.recommendation.prepare": "experiments", "experiment.run_review": "experiments",
    "experiment.beliefs.prepare": "experiments", "experiment.beliefs.approve": "experiments", "experiment.beliefs.stop": "experiments", "experiment.spend_cap": "credits",
    "experiment.audience.change": "experiments",
    "experiment.page.guide": "experiments", "experiment.page.prepare": "experiments", "experiment.page.configure": "experiments", "experiment.page.readout": "experiments",
    "experiment.measurement.record": "experiments",
    "experiment.request.prepare": "experiments", "experiment.request.list": "experiments",
    "experiment.request.publish": "experiments",
    "experiment.request.reply": "experiments", "experiment.request.fulfill": "experiments",
    "experiment.channel.prepare": "experiments", "experiment.channel.configure": "experiments",
    "experiment.survey.link": "experiments",
    "experiment.tools.link": "experiments",
    "experiment.tools.readout": "experiments",
    "experiment.channel.readout": "experiments", "experiment.channel.log": "experiments",
    "experiment.booking.configure": "experiments", "experiment.booking.mark": "experiments", "experiment.booking.readout": "experiments",
    "import.start": "import", "recovery.alternative": "recovery", "recovery.submit": "recovery", "needs_you.resolve": "recovery",
    "recovery.plan_template": "recovery",
    "journey.skip_step": "planning", "journey.jump_to": "planning", "journey.restore_step": "planning", "venture.set_website": "website",
    # router read intents and the reviewed GTM route
    "gtm_review": "gtm", "gtm.route.confirm": "gtm", "open_screen.analytics_read": "analytics", "open_screen.builds_read": "builds",
    "gtm.diagnosis.start": "gtm_diagnosis", "gtm.resume_sending": "gtm", "gtm.auto_first": "settings", "gtm.automate": "settings", "gtm.auto_search": "settings", "gtm.accept_terms": "settings", "gtm.eligibility": "gtm", "gtm.library_claim": "gtm", "gtm.library_more": "gtm", "gtm.library_notes": "gtm",
}

# ------------------------------------------------------------------ jobs
# Legacy prefix -> group, unchanged from the operator snapshot.
JOB_FEATURES = {
    "routines.fire": "settings", "routines.report": "settings",
    "inbox.": "chat",
    "memory.extract": "chat",
    "tasks.execute": "execution", "tasks.verify": "execution",
    "idea.": "idea", "research.": "research", "plan.": "planning", "build.": "builds",
    "qa.": "qa", "deploy.": "website", "content.": "content", "gtm.": "gtm",
    "import.": "import", "app.": "builds",
}
_JOB_SLUG_PREFIX = {
    "routines.": "settings",
    "browse.": "browse",
    "inbox.": "chat",
    "memory.extract": "chat",
    "tasks.": "execution", "task.run": "execution",
    "idea.": "idea", "research.": "research", "plan.": "planning", "build.": "builds", "qa.": "qa",
    "deploy.": "publish", "content.": "content", "gtm.": "gtm_diagnosis", "import.": "import",
    "app.": "builds", "experiment.": "experiments", "email.": "email", "scope.": "planning", "estimate.": "planning",
}


def job_feature(kind: str) -> str:
    """Fine slug for a job kind; background housekeeping is ``other``."""
    return next((slug for prefix, slug in _JOB_SLUG_PREFIX.items() if (kind or "").startswith(prefix)), "other")


# ------------------------------------------------------------------ routes
# Route template prefix (ids normalised to ``{}``) -> slug; longest prefix wins.
_ROUTE_SLUG = {
    "/ventures/{}/gtm/contacts": "gtm",
    "/ventures/{}/device-work-orders": "computer_use", "/ventures/{}/computer-use": "computer_use",
    "/ventures/{}/idea": "idea", "/ventures/{}/idea-page": "idea", "/ventures/{}/clarify": "idea", "/ventures/{}/clarification": "idea", "/workspaces/{}/idealab": "idea",
    "/ventures/{}/research": "research", "/ventures/{}/validation": "research", "/ventures/{}/knowledge": "research",
    "/ventures/{}/plans": "planning", "/ventures/{}/charter": "planning", "/ventures/{}/plan-tasks": "planning",
    "/ventures/{}/resources": "planning", "/ventures/{}/slate": "planning", "/ventures/{}/stage": "planning",
    "/ventures/{}/tasks": "execution", "/ventures/{}/handoffs": "execution", "/ventures/{}/task-preferences": "settings",
    "/ventures/{}/routines": "settings",
    "/ventures/{}/human-tasks": "execution", "/ventures/{}/agents": "execution",
    "/ventures/{}/activity": "execution", "/ventures/{}/jobs": "execution",
    "/ventures/{}/chat/gtm": "gtm_diagnosis", "/ventures/{}/chat": "chat", "/ventures/{}/voice": "voice",
    "/ventures/{}/gtm": "gtm_diagnosis", "/ventures/{}/waitlist": "waitlist", "/public/waitlists": "waitlist",
    "/ventures/{}/experiments": "experiments", "/ventures/{}/credits/topup": "billing",
    "/ventures/{}/market-map": "experiments",
    "/ventures/{}/credits/request": "billing", "/ventures/{}/credits": "credits",
    "/ventures/{}/billing": "billing", "/workspaces/{}/billing": "billing", "/workspaces/{}/credits": "credits",
    "/webhooks/payments": "billing", "/payments": "billing",
    "/workspaces/{}/llm-settings": "llm_settings", "/workspaces/{}/llm-catalog": "llm_settings",
    "/workspaces/{}/site": "website", "/workspaces/{}/firstlook": "website", "/workspaces/{}/brand": "website",
    "/ventures/{}/brand": "website", "/apps/{}/site": "website", "/w/{}": "website",
    "/apps/{}/deployments": "publish", "/deployments": "publish",
    "/apps/{}/analytics": "analytics", "/analytics": "analytics", "/ventures/{}/dashboard": "analytics",
    "/ventures/{}/money": "credits", "/ventures/{}/leads": "ventures",
    "/ventures/{}/builder": "builds", "/ventures/{}/build": "builds", "/ventures/{}/apps": "builds",
    "/apps": "builds", "/builds": "qa", "/internal/builds": "builds",
    "/content": "content", "/outreach": "outreach", "/ai-gtm/outreach": "outreach", "/ai-gtm": "gtm",
    "/ads": "gtm", "/marketing": "gtm", "/ops": "outreach",
    "/ventures/{}/connect": "settings", "/workspaces/{}/connections": "settings", "/connections": "settings",
    "/ventures/{}/settings": "settings", "/ventures/{}/autonomy": "settings", "/ventures/{}/autopilot": "settings", "/ventures/{}/god-mode": "god_mode", "/workspaces/{}/autonomy": "settings",
    "/ventures/{}/memories": "settings", "/ventures/{}/hermes": "settings",
    "/ventures/{}/pause": "venture_admin", "/ventures/{}/resume": "venture_admin",
    "/ventures/{}/delete": "venture_admin", "/ventures/{}/restore": "venture_admin",
    "/ventures/deleted": "venture_admin", "/ventures/delete-phrase": "venture_admin",
    "/workspaces/{}/members": "team", "/workspaces/{}/invites": "team", "/members": "team",
    "/ventures/{}/org": "team", "/workspaces/{}/presence": "team",
    "/ventures/{}/feedback": "feedback", "/desktop/bug-report": "feedback",
    "/me/greeting": "greeting", "/me/profile": "greeting", "/greeting": "greeting", "/local-tools": "local_tools", "/desktop": "shell",
    "/ventures/{}/import": "import", "/imports": "import", "/ventures/{}/intake": "import",
    "/suggestions": "suggestions", "/ventures/{}/suggestions": "suggestions", "/ventures/{}/approvals": "notifications", "/workspaces/{}/approvals": "notifications",
    "/approvals": "notifications", "/ventures/{}/onboard": "onboarding", "/ventures/{}/onboarding": "onboarding",
    "/entry": "onboarding", "/ventures": "ventures", "/workspaces": "ventures",
    "/invites": "team", "/billing": "billing", "/entitlement": "billing", "/governance": "settings",
    "/me": "account", "/engagements": "execution", "/workflows": "execution", "/jobs": "execution",
    "/tasks": "execution", "/agents": "execution", "/ingest": "analytics", "/e": "analytics", "/api/w": "website",
    "/coding": "builds", "/incidents": "builds", "/connectors": "settings", "/llm": "credits", "/idealab": "idea",
    "/social": "content", "/public/unsubscribe": "outreach", "/events": "execution",
    "/community": "community", "/finance": "payments", "/dashboard": "ventures", "/gtm": "gtm_diagnosis",
    "/memory": "settings", "/config": "other",
    # operator, internal and protocol routes are not founder features
    "/admin": "other", "/internal": "other", "/mcp": "other", "/openai": "other", "/health": "other",
    "/ready": "other", "/capabilities": "other", "/boundary": "other", "/trace": "other",
}
_ROUTE_ORDER = sorted(_ROUTE_SLUG.items(), key=lambda kv: -len(kv[0]))
_PARAM = re.compile(r"\{[^}]*\}")
_API_PREFIX = re.compile(r"^/api(?:/v1)?(?=/)")


def route_feature(template: str | None) -> str:
    """Slug for a FastAPI route template (never a raw path). Founder routes map to a feature,
    operator/protocol routes to ``other``, anything unmapped to ``shell``."""
    path = _PARAM.sub("{}", _API_PREFIX.sub("", template or "")) if not (template or "").startswith("/api/w/") \
        else "/api/w"
    return next((slug for prefix, slug in _ROUTE_ORDER if path == prefix or path.startswith(prefix + "/")), "shell")


def console_route(path: str) -> str:
    path = path.split("?", 1)[0].split("#", 1)[0]
    template = re.sub(r"^/v/[^/]+(?=/|$)", "/v/{id}", path)
    shared = {"/", "/other", "/ventures", "/new", "/welcome", "/lab", "/gtm", "/gtm/lab", "/connections",
              "/settings", "/account", "/auth", "/content", "/community", "/marketing", "/media", "/ads",
              "/ops", "/ops/agents", "/ops/tasks", "/ops/settings", "/ops/ads", "/ops/ai-gtm", "/ops/outreach",
              "/ops/social", "/ops/finance", "/ops/memory", "/setup/permissions", "/download", "/guide",
              "/operator", "/pagebuilder-lab", "/suggestions/overlay"}
    venture = {"idea", "context", "research", "market", "plan", "execution", "builds", "build", "website",
               "site", "analytics", "charter", "tasks", "org", "people", "connections", "connect", "evolution",
               "simulate", "go-to-market", "gtm", "content", "community", "settings", "team", "feedback",
               "import", "work", "now", "device-work-orders", "experiments", "market-map", "suggestions", "guide", "idea/current", "leads"}
    if template in shared or template == "/v/{id}" or template in {"/v/{id}/" + suffix for suffix in venture}:
        return template
    return "/other"


# ---------------------------------------------------------------- reference rows
def reference_rows() -> tuple[list[dict], list[dict]]:
    """(catalog rows, map rows) written to the reference tables the views join."""
    by_slug: dict[str, list[str]] = {}
    for inv, slug in sorted(INVENTORY_SLUG.items(), key=lambda kv: (kv[0][0], int(kv[0][1:]))):
        by_slug.setdefault(slug, []).append(inv)
    catalog = [{"slug": slug, "feature_group": group, "title": title, "inventory_ids": ",".join(by_slug.get(slug, []))}
               for slug, (group, title) in FEATURES.items()]
    mapping = [{"kind": "control", "key": k, "slug": v} for k, v in sorted(CONTROL_SLUG.items())]
    mapping += [{"kind": "tool", "key": k, "slug": v} for k, v in sorted(ACTION_SLUG.items())]
    mapping += [{"kind": "label", "key": k, "slug": v} for k, v in sorted(LABEL_ALIAS.items())]
    return catalog, mapping
