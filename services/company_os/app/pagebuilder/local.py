"""Local page readiness and provider routing without hosted credit accounts."""
from __future__ import annotations

from dataclasses import replace
import json
import re

from app.pagebuilder import api

CODING_TOOL_REVIEW = "coding_tool_review"


def route(workspace_id):
    from app.llm.founder_provider import resolve_llm_config

    resolved = resolve_llm_config(workspace_id)
    return replace(resolved, model=resolved.local_config.deployment_pro)


def enabled():
    from app.workspaces.resolve import local_person
    from app.pagebuilder.tool import connected_provider
    if connected_provider(local_person().id) is None:
        return False
    try:
        from app.core.local_capabilities import enabled as capability_enabled
    except ImportError:
        from app.core.providers_local import LocalProviderError, load_local_config

        try:
            load_local_config()
        except LocalProviderError:
            return False
        return True
    return capability_enabled("landing_pages")


def resources(workspace_id, venture_id):
    from app.conversion.waitlist import VERSION
    from app.credits.estimate import estimate_llm_micro
    from app.db.supymem_models import VentureWaitlist
    from app.llm.founder_provider import FounderLLMConfigError, resolved_provider_identity
    from app.state import db

    try:
        config = route(workspace_id)
    except FounderLLMConfigError:
        config = None
    with db()() as session:
        waitlist = session.get(VentureWaitlist, venture_id)
        waitlist_error = ("pagebuilder_waitlist_scope_invalid" if waitlist and waitlist.workspace_id != workspace_id
            else "pagebuilder_waitlist_disabled" if waitlist and not waitlist.enabled
            else "pagebuilder_waitlist_consent_upgrade_required" if waitlist and waitlist.consent_version != VERSION else None)
    provider, model = (config.provider, config.model) if config else (CODING_TOOL_REVIEW, "Your coding tool")
    identity = resolved_provider_identity(config) if config else CODING_TOOL_REVIEW
    ceiling = estimate_llm_micro(model, prompt_tokens=api.MAX_PROMPT_TOKENS, max_tokens=api.MAX_OUTPUT_TOKENS) if config else 0
    image = image_quote(config) if config else None
    return {"provider": provider, "model": model, "payer": "founder",
        "provider_identity": identity, "account_id": None, "call_timeout_seconds": api.CALL_TIMEOUT_SECONDS,
        "max_output_tokens": api.MAX_OUTPUT_TOKENS, "request_policy": api.REQUEST_POLICY,
        "reasoning_policy": api.reasoning_policy(provider, model),
        "call_ceiling_micro": ceiling, "ceiling_micro": ceiling * api.CALLS_PER_VARIANT
            + (image["call_ceiling_micro"] * image["calls"] if image else 0),
        "image": image, "resource_digest": api.digest([identity, VERSION, waitlist_error, image]),
        "waitlist_error": waitlist_error, "provider_error": None, "ready": waitlist_error is None,
        "available_micro": 0}


def image_quote(resolved):
    from app.credits.estimate import _lookup_model_row, estimate_llm_micro
    from app.llm.founder_provider import resolved_provider_identity

    model = "gpt-image-2.5-flare"
    pricing = _lookup_model_row(model)
    if resolved.provider != "openai" or not pricing:
        return None
    return {"provider": "openai", "model": model,
        "provider_identity": resolved_provider_identity(resolved) + ":images",
        "calls": api.IMAGE_CALLS_PER_VARIANT,
        "call_ceiling_micro": estimate_llm_micro(model, prompt_tokens=api.IMAGE_MAX_PROMPT_TOKENS,
            max_tokens=api.IMAGE_MAX_OUTPUT_TOKENS),
        "max_prompt_tokens": api.IMAGE_MAX_PROMPT_TOKENS, "max_output_tokens": api.IMAGE_MAX_OUTPUT_TOKENS,
        "pricing": pricing}


def page_check_names(failures):
    names = []
    for failure in failures:
        name, _, detail = failure.partition(":")
        if name == "Missing required page file or field":
            name = "required_page_file_missing"
        elif detail.startswith(" Value error, ") or not re.fullmatch(r"[a-z][a-z0-9_]{0,79}", name):
            name = "page_plan_validation"
        if name not in names:
            names.append(name)
    return names


def page_check_message(failures):
    from core.logging import _local_secret_values
    from core.redact import redact_string

    reasons = []
    needs_corrected_heading = False
    for failure in failures:
        heading = re.search(r"sections(?:\.\d+|\[\d+\])\.headline: String should have at most (\d+) characters", str(failure))
        if heading:
            needs_corrected_heading = True
            reason = f"Your page heading is too long. Shorten it to {heading[1]} characters or fewer"
        elif failure.startswith("approved_action_missing_or_ambiguous:") and failure.partition(":")[2] in {"hero_cta", "waitlist_cta"}:
            reason = "Your page is missing its signup button or has more than one in the same section"
        elif failure.startswith("Missing required page file or field:"):
            reason = "Your tool did not save all the files your page needs"
        elif failure == "local_signup_changed":
            if any(item.startswith("local_signup_changed: button:") for item in failures):
                return "Your signup button is not inside the form or cannot be used. In your venture chat, ask Charlie to build a new page version with a visible, enabled signup button inside the form."
            reason = ("Your page is missing its signup form" if any(item.startswith("local_signup_changed: waitlist.html:") for item in failures)
                      else "Your signup form does not match your saved consent and destination")
        elif failure.startswith("local_signup_changed:"):
            continue
        elif failure.startswith("pagebuilder_preview_too_large:"):
            return "Your page preview is too large. In your venture chat, ask Charlie to build a new version with smaller or fewer repeated images."
        elif "reviewed_body_missing" in str(failure) or "required_text_missing" in str(failure):
            reason = "Your page is missing text from your reviewed plan"
        elif "honest_copy" in str(failure) or "pagebuilder_plan_copy_rejected" in str(failure):
            quoted = re.findall(r'"reason"\s*:\s*("(?:[^"\\]|\\.)*")', str(failure))
            try:
                reason = "; ".join(json.loads(item) for item in quoted) or "Your page text does not match your reviewed offer"
            except ValueError:
                reason = "Your page text does not match your reviewed offer"
        elif str(failure).startswith("pagebuilder_") and not str(failure).startswith("pagebuilder_qa_failed"):
            return failure_reason(str(failure))
        else:
            continue
        reason = redact_string(reason, exact_values=_local_secret_values()).replace("\u2014", ", ")
        if re.search(r"\b\w+_\w+\b|\b(?:brief|sections|controller|facts)[.\[]|[{}]", reason):
            reason = "Your page text does not match your reviewed offer"
        if reason not in reasons:
            reasons.append(reason.rstrip(". "))
    if not reasons and (names := [name for name in page_check_names(failures) if name != "page_plan_validation"]):
        return failure_reason("pagebuilder_qa_failed:" + ",".join(names))
    next_step = ("In your venture chat, ask Charlie to build a new version with the corrected heading."
                 if needs_corrected_heading else "On Website, request this fix, then choose Retry. Open Build activity to review the checks.")
    return "; ".join(reasons or ["Your page did not pass its checks"]) + ". " + next_step


def saved_page_check_message(body):
    if body.get("task_snapshot", {}).get("kind") != "landing_page_build":
        return None
    last = (body.get("iterations") or [{}])[-1]
    failures = (last.get("diagnosis") or {}).get("failures") or []
    return page_check_message(failures) if failures else None


def failure_reason(code):
    if code in {"pagebuilder_outcome_uncertain", "pagebuilder_prompt_too_large", "pagebuilder_invalid_output",
                "pagebuilder_output_truncated", "pagebuilder_output_incomplete", "pagebuilder_check_failed"}:
        from app.core.providers_local import LocalProviderError, load_local_config
        from llm_gateway.provider_foundry import loopback_provider

        try:
            local_model = loopback_provider(load_local_config())
        except LocalProviderError:
            local_model = False
        if local_model:
            if code == "pagebuilder_prompt_too_large":
                return ("Your model's context window is too small to check this page. Increase it on your model server and in "
                        "Settings > Keys and connections > Your AI provider > Context window, then choose Retry on Website. Nothing was published.")
            return ("Your page check did not finish. Open Settings > Keys and connections > Your AI provider. "
                    "Check that your model server is running. If the request does not fit, increase the context window on the server and on this screen, "
                    "then choose Retry on Website. Nothing was published.")
        if code == "pagebuilder_prompt_too_large":
            return "Your page check was too large. On Website, ask for a shorter page and choose Retry. Nothing was published."
    if re.search(r"sections(?:\.\d+|\[\d+\])\.headline: String should have at most \d+ characters|reviewed_body_missing|pagebuilder_plan_copy_rejected", code):
        return page_check_message([code])
    if code == "pagebuilder_retry_limit":
        return "Your page build stopped after several tries. Open Build activity to check your coding tool. Your saved pages are unchanged."
    if code == "pagebuilder_coding_tool_required":
        from app.pagebuilder.tool import CONNECT_STEP
        return CONNECT_STEP
    if code == "pagebuilder_tool_build_stopped":
        return "Your coding tool stopped before I could save a checked page. Open Build activity to review its work, then choose Retry on Website. Your previous page is unchanged."
    if code == "pagebuilder_check_timeout":
        return "Your page check reached the build time limit. Open Website, choose Retry, and ask for a smaller page or edit. Your saved page is unchanged."
    if code == "pagebuilder_grounding_review_invalid":
        return "Your coding tool couldn't check the claims on your page. Open Build activity to review its work, then choose Retry on Website. Nothing was published."
    if code == "pagebuilder_check_failed":
        return "Your page check could not finish. Open Settings > Keys and connections > Your AI provider to check your planning model and connection, then choose Retry on Website."
    code, _, detail = code.partition(":")
    retry = " Return to your landing page and choose Retry."
    if code == "pagebuilder_customer_copy_rejected":
        return "Your page needs a customer-focused headline, a specific signup action and complete sections. On Website, ask Charlie to rebuild with your customer's problem, three confirmed workflow steps or What it does when the workflow is unconfirmed, the intended audience and a clearly unconfirmed FAQ, then choose Retry. Nothing was published."
    if code in {"pagebuilder_plan_rejected", "pagebuilder_plan_json_invalid", "pagebuilder_output_invalid",
                "pagebuilder_invalid_output", "pagebuilder_output_truncated", "pagebuilder_plan_invalid"}:
        return "Your model did not return a usable page design. Check your planning model and connection in Settings > Keys and connections > Your AI provider." + retry
    if code in {"pagebuilder_provider_rejected", "pagebuilder_byok_unavailable", "pagebuilder_resources_unavailable",
                "pagebuilder_resources_changed", "pagebuilder_provider_offline"}:
        return "Check your model API key, service URL and chat model in Settings > Keys and connections > Your AI provider." + retry
    if code == "pagebuilder_spend_limit":
        return "Your monthly spending limit stopped this build. Increase Monthly limit (USD) under Your monthly spend cap on Keys and connections." + retry
    if code == "pagebuilder_provider_rate_limited":
        return "Your AI service is busy. Wait a minute, then choose Retry on Website."
    if code == "pagebuilder_outcome_uncertain":
        return "I couldn't confirm that your page check finished. Review your AI account's request history before choosing Retry on Website."
    if code == "pagebuilder_qa_failed":
        checks = {
            "customer_copy": "customer-facing copy and complete page sections",
            "section_missing": "required sections", "required_text_missing": "required text",
            "honest_copy": "reviewed copy", "unsupported_waitlist_claim": "signup promises",
            "active_content": "active content", "semantic_landmarks": "page headings and landmarks",
            "document_language": "document language", "viewport": "mobile layout settings",
            "duplicate_dom_id": "unique page controls", "size_budget": "page size",
            "concept_disclosure_missing": "early-access disclosure", "broken_anchor": "page links",
            "resource_not_pinned": "saved page assets", "css_resource_not_pinned": "saved style assets",
            "css_external_import": "external styles", "unexpected_link_resource": "external resources",
            "contextual_image_missing": "required illustrations", "image_wrong_section": "illustration placement",
            "image_not_pinned_or_alt_invalid": "illustration descriptions", "image_dimensions_missing": "image dimensions",
            "image_not_eager": "visible illustrations", "action_override": "safe form controls",
            "unapproved_controls": "approved page controls", "approved_action_id_missing": "named page controls",
            "approved_action_missing_or_ambiguous": "working page controls", "hosted_waitlist_unwired": "signup destination",
            "waitlist_cta_missing": "signup link text", "inline_form_backend_unsupported_use_hosted_consent": "signup form wiring",
            "waitlist_preview_navigation_unsafe": "safe preview links", "complete_html_required": "complete page markup",
            "render_contrast": "text contrast", "render_overflow": "page width", "render_heading": "visible page heading",
            "render_images": "image loading and descriptions", "render_image_load": "image loading",
            "render_image_scale": "image sizes", "render_image_clipping": "unclipped images",
            "render_hero_cta_initial": "visible signup action", "render_touch_targets": "touch target sizes",
            "render_main": "main page landmark", "render_anchors": "working page links",
            "render_footer": "footer content", "render_hierarchy": "readable heading and text sizes",
            "render_text": "visible page text", "render_page_height": "page length", "render_http": "page loading",
            "render_action": "working page actions", "render_required_text_hidden": "visible required text",
            "render_action_missing_or_ambiguous": "visible page actions", "render_browser_error": "page rendering",
            "render_external_request": "external page requests", "render_evidence_size_budget": "preview size",
            "local_signup_changed": "saved signup settings and consent",
            "render_failed": "page rendering", "motion_not_reduced_motion_guarded": "reduced-motion support",
            "motion_scroll_driven": "safe scrolling", "motion_too_long": "animation duration",
            "motion_unbounded": "bounded animations", "page_test_light_mode_required": "light page theme",
            "page_test_variant_render_failed": "test page appearance", "page_test_headline_not_visible": "visible test headlines",
            "page_test_variant_action_missing": "working test page actions",
        }
        rejected = []
        for failure in detail.split(","):
            name = failure.partition(":")[0]
            label = checks.get(name)
            if label and label not in rejected:
                rejected.append(label)
        summary = "; ".join(rejected) or "content or accessibility"
        return "Your page failed these checks: " + summary + ". On Website, ask for a simpler layout with your reviewed text and signup link, then choose Retry. Nothing was published."
    if code in {"pagebuilder_call_budget_exhausted", "pagebuilder_ceiling_exhausted"}:
        return "Your model could not finish within this build's attempt limit. Make your requested page or edit more specific on the Website screen." + retry
    if code in {"pagebuilder_inputs_changed", "pagebuilder_confirmed_revision_required"}:
        return "Your saved idea changed. Confirm the current version on the Idea screen." + retry
    if code == "pagebuilder_waitlist_disabled":
        return "Enable your waitlist in Website > Waitlist." + retry
    if code in {"pagebuilder_waitlist_scope_invalid", "pagebuilder_waitlist_consent_upgrade_required"}:
        return "Review and save your waitlist consent in Website > Waitlist." + retry
    if code in {"pagebuilder_reference_unavailable", "pagebuilder_artifact_write_failed", "pagebuilder_artifact_read_failed"}:
        return "Your page could not be saved. Free space in your app's data folder and check that you can write to it." + retry
    if code == "pagebuilder_interrupted":
        return "Your landing page build was interrupted when your app stopped. Your saved pages are kept. On Website, choose Retry to build it again."
    if code == "pagebuilder_edit_unchanged":
        return "Your requested edit was not applied. Make the change more specific in your landing page editor, then choose Retry."
    return "Your page could not be completed." + retry
