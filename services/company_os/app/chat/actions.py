"""Closed, declarative chat action registry.

Chat parses requests into these references; it does not gain a second path to
perform the operation. New actions require a code change here.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping

from app.authz.roles import Capability
from app.core.local_capabilities import UNAVAILABLE
from app.core.local_mode import is_local


def question_reply(body: str, actions: list | None = None, kind: str = "text") -> dict:
    return {"kind": kind, "body_md": body, "actions": actions or [], "citations": [{"question": True}]}


@dataclass(frozen=True)
class ChatAction:
    name: str
    governed_capability: str
    endpoint: str
    consequential: bool = False
    required_permission: Capability = field(kw_only=True)


_ACTIONS = (
    ChatAction("routines.create", "cap.write.work_start", "routines.action", required_permission="request_work"),
    ChatAction("routines.list", "cap.read.venture_status", "routines.action", required_permission="read"),
    ChatAction("routines.pause", "cap.autopilot.pause", "routines.action", required_permission="request_work"),
    ChatAction("routines.delete", "cap.autopilot.pause", "routines.action", required_permission="request_work"),
    ChatAction("routines.approve", "cap.write.work_start", "routines.action", True, required_permission="request_work"),
    ChatAction("approvals.revoke", "cap.write.approval_revoke", "approvals.service.decide", required_permission="read"),
    ChatAction("inbox.briefing.consent", "cap.write.connection_start", "inbox.briefing.action", True, required_permission="connect_systems"),
    ChatAction("inbox.briefing.settings", "cap.write.connection_start", "inbox.briefing.action", required_permission="connect_systems"),
    ChatAction("inbox.item.feedback", "cap.write.connection_start", "inbox.briefing.action", required_permission="connect_systems"),
    ChatAction("gtm.resume_sending", "cap.autopilot.send", "journey.resume_sending", True, required_permission="change_autonomy"),
    ChatAction("journey.skip_step", "cap.write.idea_turn", "journey_overrides.apply", True, required_permission="request_work"),
    ChatAction("journey.jump_to", "cap.write.idea_turn", "journey_overrides.apply", True, required_permission="request_work"),
    ChatAction("journey.restore_step", "cap.write.idea_turn", "journey_overrides.apply", True, required_permission="request_work"),
    ChatAction("venture.set_website", "cap.write.idea_turn", "journey_overrides.apply", True, required_permission="request_work"),
    ChatAction("venture.rename", "cap.write.idea_turn", "PATCH /ventures/{id}", required_permission="request_work"),
    ChatAction("status", "cap.read.venture_status", "GET /ventures/{id}/dashboard", required_permission="read"),
    ChatAction("why", "cap.read.memory_answer", "MemoryPort.answer", required_permission="read"),
    ChatAction("money", "cap.read.money_summary", "GET /money/summary", required_permission="read"),
    ChatAction("leads", "cap.read.interest_leads", "GET /ventures/{id}/leads", required_permission="read"),
    ChatAction("research", "cap.read.research", "research.service / dossier", required_permission="read"),
    ChatAction("browse_web", "cap.read.web_search", "tools.browse.start", required_permission="request_work"),
    ChatAction("research.competitors", "cap.read.research", "research.service / competitor grid", required_permission="read"),
    ChatAction("idea.pressure_test.prepare", "cap.read.research", "validation.board.prepare", required_permission="request_work"),
    ChatAction("idea.pressure_test", "cap.write.idea_turn", "validation.board.run", True, required_permission="request_work"),
    ChatAction("idea.pressure_test.accept", "cap.write.idea_turn", "validation.board.accept", True, required_permission="request_work"),
    ChatAction("research.start", "cap.write.research", "research.service.start", True, required_permission="request_work"),
    ChatAction("research.update_and_rerun", "cap.write.research", "chat.research_revision.confirm", True, required_permission="request_work"),
    ChatAction("agents", "cap.read.org_agents", "GET /org/agents", required_permission="read"),
    ChatAction("jobs.status", "cap.read.venture_status", "core.job_status.list_jobs", required_permission="read"),
    ChatAction("whats_next", "cap.read.will_slate", "GET /will/slate", required_permission="read"),
    ChatAction("next_moves", "cap.read.will_slate", "chat.next_moves.compose", required_permission="read"),
    ChatAction("refine_site", "cap.write.site_refine", "firstlook.refine_from_charter", True, required_permission="request_work"),
    ChatAction("site.preview", "cap.read.site", "firstlook.current_site", required_permission="read"),
    ChatAction("site.rewrite", "cap.write.site_rewrite", "firstlook.generate_first_look", True, required_permission="request_work"),
    ChatAction("waitlist.enable", "cap.write.waitlist", "PUT /ventures/{id}/waitlist", True, required_permission="request_work"),
    ChatAction("waitlist.close", "cap.write.waitlist", "PUT /ventures/{id}/waitlist", True, required_permission="request_work"),
    ChatAction("waitlist.test", "cap.write.waitlist_test", "POST /ventures/{id}/waitlist/test", required_permission="request_work"),
    ChatAction("site.unpublish", "cap.write.site_publish", "POST /apps/{a}/deployments/{d}/unpublish", True, required_permission="manage_apps"),
    ChatAction("site.publish", "cap.write.site_publish", "POST /apps/{a}/deployments", True, required_permission="manage_apps"),
    ChatAction("openworker_search", "cap.read.web_search", "openworker_bridge.search_web", required_permission="read"),
    ChatAction("agent_reach", "cap.read.web_search", "agent_reach_bridge.read_url / search_platform", required_permission="read"),
    ChatAction("slate_override", "cap.will.override", "app.will.override.parse_override", True, required_permission="request_work"),
    # Stopping or reducing autonomy only makes things safer, so any contributor may do it.
    ChatAction("pause", "cap.autopilot.pause", "POST /autopilot/pause", True, required_permission="request_work"),
    ChatAction("resume", "cap.autopilot.resume", "POST /ventures/{id}/resume", required_permission="request_work"),
    ChatAction("autopilot.god_on", "cap.autopilot.god_mode", "PUT /ventures/{id}/autopilot", True, required_permission="change_autonomy"),
    ChatAction("autopilot.allow_send", "cap.autopilot.send", "PUT /ventures/{id}/autopilot", True, required_permission="change_autonomy"),
    ChatAction("autopilot.send_off", "cap.autopilot.send", "PUT /ventures/{id}/autopilot", required_permission="request_work"),
    ChatAction("autopilot.god_off", "cap.autopilot.god_mode", "PUT /ventures/{id}/autopilot", required_permission="request_work"),
    ChatAction("venture.start_new", "cap.write.venture_create", "POST /entry/chat/confirm", True, required_permission="request_work"),
    ChatAction("autopilot.set_cap", "cap.autopilot.cap", "PUT /ventures/{id}/autopilot", True, required_permission="change_autonomy"),
    ChatAction("autopilot.send_cap", "cap.autopilot.send", "PUT /ventures/{id}/autopilot", True, required_permission="change_autonomy"),
    ChatAction("build.accept", "cap.write.build_review", "POST /builds/{id}/review", True, required_permission="manage_apps"),
    # Early-access interest only: records the request (idempotent), never turns God mode on.
    ChatAction("god_mode.request", "cap.write.god_mode_request", "POST /ventures/{id}/god-mode/requests", required_permission="request_work"),
    ChatAction("credits.request", "cap.write.credit_request", "POST /ventures/{id}/credits/request", True, required_permission="request_work"),
    ChatAction("cancel", "cap.will.override", "planning.store.cancel_matching_work", True, required_permission="request_work"),
    ChatAction("build.cancel", "cap.write.build_cancel", "POST /builds/{id}/cancel", True, required_permission="manage_apps"),
    ChatAction("manager.rename", "cap.write.manager_name", "PATCH /ventures/{id}/settings", True, required_permission="request_work"),
    ChatAction("approve", "cap.approvals.decide", "POST /approvals/{id}", True, required_permission="approve"),
    ChatAction("office_hours", "cap.read.venture_status", "gstack.office_hours", required_permission="read"),
    ChatAction("ceo_review", "cap.read.will_slate", "gstack.ceo_review", True, required_permission="request_work"),
    ChatAction("brain_brief", "cap.read.memory_answer", "gbrain.brief_entity", required_permission="read"),
    ChatAction("memory.list", "cap.read.memory_answer", "MemoryPort.recall", required_permission="read"),
    ChatAction("memory.remember", "cap.write.memory", "MemoryPort.remember", True, required_permission="request_work"),
    ChatAction("memory.correct", "cap.write.memory", "MemoryPort.supersede", True, required_permission="request_work"),
    ChatAction("memory.forget", "cap.write.memory", "MemoryPort.forget", True, required_permission="request_work"),
    ChatAction("social_post", "cap.write.social_post", "actuation.social_post", True, required_permission="request_work"),
    ChatAction("social_guide", "cap.read.social_guide", "concierge.social_guide", required_permission="read"),
    ChatAction("social_status", "cap.read.social_status", "local_tools.social_health", required_permission="read"),
    ChatAction("social_connect", "cap.write.social_connect", "concierge.connect.social", True, required_permission="connect_systems"),
    ChatAction("gtm.diagnosis", "cap.read.gtm_diagnosis", "gtm.god.plan", required_permission="read"),
    ChatAction("credits.balance", "cap.read.credits", "credits.accounts", required_permission="read"),
    ChatAction("connections.status", "cap.read.connections", "connections.gmail", required_permission="read"),
    ChatAction("connections.context.select_documents", "cap.write.connection_start", "connections.context_docs.select_documents", True, required_permission="connect_systems"),
    ChatAction("connections.feedback.select_sources", "cap.write.connection_start", "connections.feedback_mining.select_sources", True, required_permission="connect_systems"),
    ChatAction("connections.connect", "cap.write.connection_start", "POST /connections/{provider}/connect", required_permission="connect_systems"),
    ChatAction("connections.disconnect", "cap.write.connection_revoke", "connections.hub.disconnect_provider", True, required_permission="connect_systems"),
    ChatAction("content.drafts", "cap.read.content_drafts", "content.store", required_permission="read"),
    ChatAction("idea.answer", "cap.write.idea_turn", "POST /ventures/{id}/idea/sessions/{s}/messages", True, required_permission="request_work"),
    ChatAction("idea.review", "cap.read.venture_status", "chat.idea_proposal.review", required_permission="read"),
    ChatAction("idea.page_refresh", "cap.write.idea_turn", "ventures.idea_page.request_refresh", required_permission="request_work"),
    ChatAction("startup.review", "cap.read.venture_status", "chat.startup_review", required_permission="read"),
    ChatAction("research.prepare", "cap.read.venture_status", "chat.build_path.research_card", required_permission="read"),
    ChatAction("idea.confirm", "cap.write.idea_confirm", "POST /ventures/{id}/idea/sessions/{s}/confirm", True, required_permission="request_work"),
    ChatAction("idea.explore", "cap.write.idea_case", "POST /ventures/{id}/idea/sessions/{s}/case", required_permission="request_work"),
    ChatAction("idea.proposal.approve", "cap.write.idea_validate", "chat.idea_proposal.approve", True, required_permission="request_work"),
    ChatAction("idea.proposal.edit", "cap.write.idea_turn", "chat.idea_proposal.edit", required_permission="request_work"),
    ChatAction("plan.resources_none", "cap.write.plan_resources", "PUT /ventures/{id}/resources", required_permission="request_work"),
    ChatAction("plan.quote", "cap.read.plan_quote", "POST /ventures/{id}/plans/estimate-generation", required_permission="read"),
    ChatAction("plan.generate", "cap.write.plan_generate", "POST /ventures/{id}/plans/compile", True, required_permission="request_work"),
    ChatAction("plan.approve", "cap.write.plan_approve", "POST /ventures/{id}/plans/{p}/approve", True, required_permission="approve"),
    ChatAction("build.start", "cap.write.build_start", "app.builder.execution.start", True, required_permission="manage_apps"),
    ChatAction("work.start", "cap.write.work_start", "POST /ventures/{id}/tasks/{t}/run", True, required_permission="request_work"),
    ChatAction("build.prepare", "cap.read.plan_quote", "chat.build_chain.prepare", required_permission="read"),
    ChatAction("job.retry", "cap.write.job_retry", "idea.validation.start_validation", True, required_permission="request_work"),
    ChatAction("experiment.run_review", "cap.write.experiment_start", "experiments.ledger.start", True, required_permission="request_work"),
    ChatAction("outreach.draft", "cap.write.work_start", "planning.execute.execute_planned_task", True, required_permission="request_work"),
    ChatAction("pagebuilder.build", "cap.write.work_start", "pagebuilder.api._start", True, required_permission="request_work"),
    ChatAction("outreach.drafts", "cap.read.content_drafts", "autopilot.outreach.drafts", required_permission="read"),
    ChatAction("outreach.targets", "cap.write.work_start", "autopilot.outreach.build_targets", required_permission="request_work"),
    ChatAction("outreach.send", "cap.write.social_post", "autopilot.outreach.send_batch", True, required_permission="approve"),
    ChatAction("gtm.find", "cap.write.work_start", "gtm.journey.prepare", True, required_permission="request_work"),
    ChatAction("gtm.status", "cap.read.interest_leads", "gtm.journey.status", required_permission="read"),
    ChatAction("gtm.eligibility", "cap.write.work_start", "gtm.journey.patch_prospect", required_permission="request_work"),
    ChatAction("gtm.library_claim", "cap.write.work_start", "lead_library.claim", required_permission="request_work"),
    ChatAction("gtm.library_more", "cap.read.interest_leads", "lead_library.match", required_permission="read"),
    ChatAction("gtm.library_notes", "cap.read.interest_leads", "gtm.playbooks.messages", required_permission="read"),
    ChatAction("gtm.approve", "cap.write.work_start", "gtm.journey.patch_draft", required_permission="request_work"),
    ChatAction("gtm.draft_edit", "cap.write.work_start", "gtm.journey.patch_draft", required_permission="request_work"),
    ChatAction("gtm.draft_rewrite", "cap.write.work_start", "gtm.journey.rewrite_drafts", required_permission="request_work"),
    ChatAction("gtm.send", "cap.write.social_post", "gtm.journey.send", True, required_permission="approve"),
    ChatAction("gtm.auto_first", "cap.autopilot.send", "gtm.journey.update_settings", True, required_permission="change_autonomy"),
    ChatAction("gtm.automate", "cap.autopilot.send", "gtm.journey.update_settings", True, required_permission="change_autonomy"),
    ChatAction("gtm.auto_search", "cap.autopilot.send", "gtm.journey.update_settings", True, required_permission="change_autonomy"),
    ChatAction("gtm.accept_terms", "cap.autopilot.send", "gtm.journey.update_settings", True, required_permission="change_autonomy"),
    ChatAction("outreach.delivery_resolve", "cap.write.recovery_result", "autopilot.outreach.resolve_delivery", True, required_permission="approve"),
    ChatAction("build.app.create", "cap.write.app_create", "app.infra.apps.create_draft", True, required_permission="manage_apps"),
    ChatAction("scope.request", "cap.write.scope_request", "POST /ventures/{id}/plans/{p}/requests", required_permission="request_work"),
    ChatAction("app.iterate", "cap.write.iterate", "POST /apps/{a}/iterate", True, required_permission="manage_apps"),
    ChatAction("app.debug", "cap.write.debug", "POST /incidents/{i}/debug", True, required_permission="manage_apps"),
    ChatAction("blocker.explain", "cap.read.blocker_guide", "chat.blockers.explain", required_permission="read"),
    ChatAction("chat.model_retry", "cap.read.memory_answer", "chat.assistant.retry_reply", required_permission="request_work"),
    ChatAction("mac.task.prepare", "cap.write.mac_review", "POST /ventures/{id}/device-work-orders/from-chat", required_permission="request_work"),
    ChatAction("build.tool.start", "cap.write.work_start", "device_orders.prepare_product_build_from_chat", required_permission="request_work"),
    ChatAction("experiment.list", "cap.read.experiments", "experiments.ledger.list_for_venture", required_permission="read"),
    ChatAction("experiment.review", "cap.read.experiments", "chat.experiments.review_reply", required_permission="read"),
    ChatAction("experiment.export_sheet", "cap.write.experiment_start", "connections.sheets_export.export", True, required_permission="request_work"),
    ChatAction("experiment.exploration.start", "cap.write.experiment_start", "experiments.exploration.act", required_permission="request_work"),
    ChatAction("experiment.exploration.retry", "cap.write.experiment_start", "experiments.exploration.act", required_permission="request_work"),
    ChatAction("experiment.exploration.continue", "cap.write.experiment_start", "experiments.exploration.act", required_permission="request_work"),
    ChatAction("experiment.exploration.select", "cap.write.experiment_start", "experiments.exploration.act", required_permission="request_work"),
    ChatAction("experiment.exploration.next", "cap.write.experiment_start", "experiments.exploration.act", required_permission="request_work"),
    ChatAction("experiment.exploration.next_round", "cap.write.experiment_start", "experiments.exploration.act", required_permission="request_work"),
    ChatAction("experiment.beliefs.prepare", "cap.read.experiments", "chat.market_beliefs.prepare", required_permission="request_work"),
    ChatAction("experiment.audience.change", "cap.read.experiments", "chat.experiments.audience_question", required_permission="request_work"),
    ChatAction("experiment.beliefs.approve", "cap.write.experiment_start", "experiments.belief_autopilot.approve", True, required_permission="change_autonomy"),
    ChatAction("experiment.beliefs.stop", "cap.write.experiment_start", "experiments.belief_autopilot.stop", required_permission="change_autonomy"),
    ChatAction("experiment.prepare", "cap.read.experiments", "experiments.autonomous.prepare", required_permission="request_work"),
    ChatAction("experiment.recommendation.prepare", "cap.read.experiments", "chat.experiments.review_test_prepare", required_permission="request_work"),
    ChatAction("experiment.run", "cap.write.experiment_start", "experiments.autonomous.run", True, required_permission="request_work"),
    ChatAction("experiment.spend_cap", "cap.write.experiment_start", "credits.spend.set_cap", True, required_permission="change_autonomy"),
    ChatAction("experiment.page.guide", "cap.read.experiments", "chat.experiments.page_guide_reply", required_permission="read"),
    ChatAction("experiment.booking.configure", "cap.write.experiment_start", "experiments.outreach_tests.configure_booking", required_permission="request_work"),
    ChatAction("experiment.booking.mark", "cap.write.experiment_record", "experiments.outreach_tests.mark_booking", required_permission="request_work"),
    ChatAction("experiment.booking.readout", "cap.read.experiments", "experiments.outreach_tests.booking_readout", required_permission="read"),
    ChatAction("experiment.page.prepare", "cap.read.experiments", "experiments.page_tests.prepare", required_permission="request_work"),
    ChatAction("experiment.page.configure", "cap.write.experiment_start", "experiments.page_tests.configure", True, required_permission="approve"),
    ChatAction("experiment.page.readout", "cap.read.experiments", "experiments.page_tests.readout", required_permission="read"),
    ChatAction("experiment.measurement.record", "cap.write.experiment_record", "product_analytics.service.record_counts", True, required_permission="request_work"),
    ChatAction("experiment.request.prepare", "cap.read.experiments", "experiments.wizard_tests.setup", required_permission="request_work"),
    ChatAction("experiment.request.list", "cap.read.experiments", "experiments.wizard_tests.list_reply", required_permission="read"),
    ChatAction("experiment.request.publish", "cap.write.experiment_start", "experiments.wizard_tests.publish", True, required_permission="approve"),
    ChatAction("experiment.request.reply", "cap.read.experiments", "experiments.wizard_tests.prepare_reply", required_permission="request_work"),
    ChatAction("experiment.request.fulfill", "cap.write.experiment_start", "experiments.wizard_tests.fulfill", True, required_permission="approve"),
    ChatAction("experiment.channel.prepare", "cap.read.experiments", "experiments.channel_tests.prepare", required_permission="request_work"),
    ChatAction("experiment.survey.link", "cap.write.experiment_start", "connections.surveys.link_form", required_permission="request_work"),
    ChatAction("experiment.tools.link", "cap.write.experiment_start", "experiments.tools.link", required_permission="request_work"),
    ChatAction("experiment.tools.readout", "cap.read.experiments", "experiments.tools.readout", required_permission="read"),
    ChatAction("experiment.channel.configure", "cap.write.experiment_start", "experiments.channel_tests.configure", True, required_permission="approve"),
    ChatAction("experiment.channel.readout", "cap.read.experiments", "experiments.channel_tests.readout", required_permission="read"),
    ChatAction("experiment.channel.log", "cap.write.experiment_record", "experiments.channel_tests.log_effort", True, required_permission="approve"),
    ChatAction("experiment.start", "cap.write.experiment_start", "experiments.ledger.start", required_permission="request_work"),
    ChatAction("experiment.record", "cap.write.experiment_record", "experiments.ledger.record_result", True, required_permission="request_work"),
    ChatAction("experiment.revise", "cap.write.idea_revise", "idea.service.start_direction_correction", True, required_permission="request_work"),
    ChatAction("experiment.persevere", "cap.write.experiment_record", "chat.experiments.decision_run", True, required_permission="approve"),
    ChatAction("experiment.pivot", "cap.write.idea_revise", "chat.experiments.decision_run", True, required_permission="approve"),
    ChatAction("experiment.spinoff", "cap.write.venture_create", "entry.service.commit", True, required_permission="approve"),
    ChatAction("experiment.kill", "cap.write.venture_pause", "chat.experiments.decision_run", True, required_permission="approve"),
    ChatAction("service.guide", "cap.read.recovery", "chat.service_guides.guide", required_permission="read"),
    ChatAction("service.select", "cap.write.recovery_result", "chat.service_guides.select_service", required_permission="request_work"),
    ChatAction("product.record", "cap.write.recovery_result", "chat.service_guides.record_product", required_permission="request_work"),
    ChatAction("product.status", "cap.read.recovery", "chat.service_guides.status", required_permission="read"),
    ChatAction("host.choose", "cap.write.recovery_result", "chat.hosting.choose", required_permission="request_work"),
    ChatAction("host.plan", "cap.read.recovery", "chat.hosting.plan", required_permission="read"),
    ChatAction("host.verify", "cap.write.recovery_result", "chat.hosting.verify", required_permission="request_work"),
    ChatAction("needs_you.resolve", "cap.write.recovery_result", "needs_you.service.resolve", required_permission="request_work"),
    ChatAction("recovery.alternative", "cap.read.recovery", "chat.recovery.alternative", required_permission="read"),
    ChatAction("recovery.submit", "cap.write.recovery_result", "chat.recovery.submit", required_permission="request_work"),
    ChatAction("recovery.plan_template", "cap.write.plan_generate", "chat.recovery.create_plan", True, required_permission="request_work"),
    ChatAction("contacts.preview", "cap.write.contact_import", "POST /ventures/{id}/gtm/contacts/import/preview", required_permission="request_work"),
    ChatAction("contacts.import", "cap.write.contact_import", "POST /ventures/{id}/gtm/contacts/import", True, required_permission="request_work"),
    ChatAction("website.review", "cap.write.site_import", "intake.assets.import_site", required_permission="request_work"),
    ChatAction("import.start", "cap.write.import", "POST /ventures/{id}/imports", True, required_permission="request_work"),
)
REGISTRY: Mapping[str, ChatAction] = MappingProxyType({action.name: action for action in _ACTIONS})


_LOCAL_CAPABILITIES = {
    action: capability_name for capability_name, actions in {
        "research": ("research", "browse_web", "research.competitors", "research.prepare", "research.start",
                     "research.update_and_rerun", "openworker_search", "agent_reach", "idea.pressure_test.prepare",
                     "idea.pressure_test", "idea.pressure_test.accept"),
        "find_customers": ("gtm.find", "outreach.targets"),
        "send_email": ("gtm.send", "outreach.send", "gtm.resume_sending", "autopilot.allow_send", "gtm.auto_first",
                       "gtm.automate", "gtm.accept_terms"),
        "read_replies": ("inbox.briefing.consent", "inbox.briefing.settings"),
        "landing_pages": ("pagebuilder.build", "refine_site", "site.rewrite", "experiment.page.configure", "experiment.page.prepare"),
        "publish_pages": ("site.publish", "experiment.request.publish"),
        "page_measurement": ("experiment.page.readout", "experiment.measurement.record"),
        "background_work": ("autopilot.god_on", "god_mode.request", "work.start", "gtm.auto_search"),
        "build_with_my_tool": ("build.tool.start",),
        "run_for_me_experiments": ("experiment.beliefs.approve",),
        "routines": ("routines.create", "routines.approve"),
        "connected_apps": ("social_guide", "social_status", "experiment.export_sheet",
                           "experiment.survey.link", "experiment.tools.link"),
        "content": ("content.drafts", "outreach.draft", "outreach.drafts", "gtm.draft_rewrite"),
    }.items() for action in actions
}
_LOCAL_CAPABILITIES.update({action: name for name, item in UNAVAILABLE.items()
                            for action in item["surfaces"].get("actions", ())})

def _local_blocked_reason(name: str | None) -> str | None:
    if not isinstance(name, str):
        return None
    capability_name = _LOCAL_CAPABILITIES.get(name)
    if capability_name is None:
        return None
    try:
        from app.core.local_capabilities import enabled, blocked_message
    except ImportError:
        # shortcut: owner switches await the shared capability module, remove when that module lands.
        from app.core.local_secrets import get_secret, read_settings

        if capability_name in UNAVAILABLE:
            return UNAVAILABLE[capability_name]["next_step"]

        if capability_name == "build_with_my_tool":
            from app.chat.execute import _local_settings_ready

            return None if _local_settings_ready(capability_name) else (
                "Open Settings > Keys and connections > Coding tools, connect Codex or Claude Code, "
                "sign in with your own subscription, allow Charlie to use it, then retry your build.")

        requirements = {
            "send_email": ("smtp", ("host", "user", "sender_name", "postal_address"), ("password",),
                           "save your SMTP host, username, password, sender name, and postal address"),
            "read_replies": ("smtp", ("imap_host", "user"), ("password",),
                             "save your IMAP host, SMTP username, and password"),
            "connected_apps": ("composio", (), ("api_key",), "save your Composio API key"),
        }
        if capability_name == "publish_pages":
            values = read_settings("publishing")["values"]
            provider = values.get("provider")
            fields, secrets = ((("bucket",), ("access_key_id", "secret_access_key")) if provider == "s3"
                               else (("project",), ("token",)))
            if (provider in {"vercel", "netlify", "s3"} and all(values.get(field) for field in fields)
                    and all(get_secret("publishing", field) for field in secrets)):
                return None
            if provider == "s3":
                return "Open Settings > Keys and connections and save your S3 bucket, access key ID, and secret access key."
            if provider in {"vercel", "netlify"}:
                label = "Vercel" if provider == "vercel" else "Netlify"
                return f"Open Settings > Keys and connections and save your {label} token and project."
            return ("Open Settings > Keys and connections and choose your publishing provider, then save its token "
                    "and project or its S3 bucket, access key ID, and secret access key.")
        if capability_name not in requirements:
            return None
        section, fields, secrets, next_step = requirements[capability_name]
        values = read_settings(section)["values"]
        if all(values.get(field) for field in fields) and all(get_secret(section, field) for field in secrets):
            return None
        return f"Open Settings > Keys and connections and {next_step}."
    return None if enabled(capability_name) else blocked_message(capability_name)


# ponytail: legacy registry includes dormant actions, drop exceptions when dispatch covers the full registry.
def disabled_reason(name: str | None) -> str | None:
    if is_local():
        reason = _local_blocked_reason(name)
        if reason:
            return reason
    client_actions = {"chat.reply", "chat.prefill", "chat.stop", "chat.startFresh", "chat.new_conversation",
                      "referrals.show", "task.run", "mac.task.prepare", "gtm.route.confirm", "gtm.diagnosis.start",
                      "entry.direction.confirm", "entry.direction.back"}
    unimplemented = {"app.debug", "app.iterate", "import.start", "idea.answer", "idea.explore",
                     "memory.correct", "memory.forget", "memory.list", "memory.remember", "scope.request"}
    if is_local():
        unimplemented -= {"memory.correct", "memory.forget", "memory.list", "memory.remember"}
        if name == "idea.answer":
            return "Open your venture home, then answer your latest idea question in chat."
        if name == "idea.explore":
            return "Tell me your idea in chat and ask to explore it. You can review the saved direction on your venture home."
    if isinstance(name, str) and (name in client_actions or name in REGISTRY and name not in unimplemented):
        return None
    if is_local():
        return "Your request was not recognized. Describe the work you want in a new chat message."
    return "You cannot use this action in chat yet. Ask me for your next step."


def social_post_enabled() -> bool:
    """Your local capability controls posting; hosted posting uses its flag."""
    import os

    return _local_blocked_reason("social_post") is None if is_local() else os.environ.get("SUPYMEM_SOCIAL_POST_ENABLED") == "1"


SOCIAL_POST_COMING_SOON = {
    "kind": "text",
    "body_md": "You need a connected social account before posting. Open Connections, connect your account, then ask me to post your exact text.",
    "actions": [], "citations": [],
}


# Beta: actions outside the limited-beta scope. Blocked in execute (even from a stale confirmation card)
# and not offered. SUPYMEM_BETA_SCOPE=0 lifts the gate (tests, non-beta servers).
BETA_BLOCKED_ACTIONS = frozenset({
    "autopilot.god_on", "build.start", "build.app.create",
    "app.iterate", "app.debug", "import.start", "mac.task.prepare", "social_connect",
})


def beta_scope_enforced() -> bool:
    import os

    return os.environ.get("SUPYMEM_BETA_SCOPE", "1") != "0"


def beta_blocked(name: str, venture_id: str | None = None) -> dict | None:
    """Use your local capability settings, or the hosted beta gate, before dispatch.

    God mode defers to the early-access gate: a venture an admin approved may turn it on."""
    if is_local():
        reason = _local_blocked_reason(name)
        return {"kind": "text", "actions": [], "citations": [], "body_md": reason} if reason else None
    if name not in BETA_BLOCKED_ACTIONS or not beta_scope_enforced():
        return None
    if name == "autopilot.god_on" and venture_id:
        from app.autopilot import god_requests

        if god_requests.beta_gate_open(venture_id):
            return None
    return {"kind": "text", "actions": [], "citations": [],
            "body_md": "That is coming soon in the beta, so I did not do it and nothing changed. In the beta I can "
                       "research your idea, build an honest concept landing page, publish it when you confirm, collect "
                       "waitlist signups, remember things for you, and save outreach drafts for you to review and "
                       "send yourself."}


def get_action(name: str) -> ChatAction:
    try:
        return REGISTRY[name]
    except KeyError as exc:
        raise ValueError(f"unknown chat action: {name}") from exc


def all_actions() -> tuple[ChatAction, ...]:
    return _ACTIONS
