import { usageLimitMessage } from "./credits.ts";
import { isLocalConsole } from "./localMode.ts";
/** Plain copy for machine error codes (desktop_..., native_..., cu_..., mac_...) that can leak into chat lines. */
const COPY: Record<string, string> = {
  no_customer_conversation: "No customer conversation recorded yet",
  desktop_app_launch_unconfirmed: "Supymem opened the app on your Mac but could not confirm it came up",
  desktop_app_launch_unavailable: "Supymem could not open that app on your Mac",
  desktop_app_launch_failed: "The app did not open on your Mac",
  desktop_app_window_unavailable: "The app has no window Supymem can work with right now",
  desktop_app_identity_changed: "A different app came to the front, so Supymem stopped",
  desktop_window_identity_changed: "The reviewed window changed or closed, so Supymem stopped",
  desktop_fresh_observation_required: "Supymem lost sight of the screen and stopped instead of guessing",
  desktop_cua_session_unavailable: "The Mac session could not start",
  desktop_driver_session_unavailable: "The Mac session could not start",
  desktop_driver_missing: "The Mac helper Supymem needs is not installed",
  desktop_driver_not_installed: "The Mac helper Supymem needs is not installed",
};
const TOKEN = /\b(?:no_customer_conversation|(?:desktop|native|cu|mac)_[a-z0-9]+(?:_[a-z0-9]+)*)\b/gi;

export function humanizeErrorCodes(text: string): string {
  return text.replace(TOKEN, (code) => COPY[code.toLowerCase()] ?? code.replace(/_/g, " "));
}

/** Founder-facing sentences for server error codes. Exact match first, then a per-family fallback, so a raw code never reaches the screen. */
const CODE_COPY: Record<string, string> = {
  device_coding_loop_unavailable: "Local product builds are not enabled for this account yet. Your work is saved.",
  device_product_build_unavailable: "This idea needs a supported software scope before a local product build can start.",
  device_product_build_parser_unavailable: "Update the Supymem Mac app before approving this product build. Your review is saved.",
  device_coding_loop_scope_invalid: "The coding limits or approved plan changed. Ask Supymem for a new coding review.",
  device_coding_plan_invalid: "The product plan needs a valid task list and acceptance checks before coding can start.",
  device_coding_plan_unavailable: "The product plan is not ready yet. Wait for the local planning task to stop, then refresh.",
  device_coding_source_unverified: "The saved app has not passed its local checks. Review the coding result before deploying.",
  device_deploy_source_unavailable: "The complete app has not passed its local checks. Finish the coding run before preparing deployment.",
  device_deploy_source_changed: "The saved source no longer matches the deployment review. Prepare a new deployment review.",
  device_deploy_receipt_invalid: "The deployment result could not be verified. Check your Vercel account before trying another deployment.",
  coding_sign_in_required: "Sign in to your coding CLI in Terminal, then re-detect it in Settings.",
  coding_git_scope_invalid: "Supymem could not verify this venture's local Git workspace, so the build stopped. Your saved files are retained; ask Supymem for a new coding review.",
  deploy_cli_not_installed: "Install the Vercel CLI in Terminal or use the deployment checklist.",
  deploy_sign_in_required: "Sign in to Vercel in Terminal by running vercel login. Supymem has not started a deployment.",
  deploy_source_changed: "Your app files changed after deployment was reviewed. Prepare a new deployment review.",
  deploy_result_unconfirmed: "The deployment result is uncertain. Check your Vercel account before starting another deployment.",
  deploy_secret_detected: "A private detail was found in the app files. Nothing was uploaded; remove it before preparing a new review.",
  deploy_project_link_invalid: "The saved Vercel project link could not be verified. Review the local project before deploying.",
  deploy_project_link_changed: "The linked Vercel project changed after review. Nothing was deployed by this task; prepare a new review for the saved preview project.",
  deploy_scope_unsupported: "This deployment request does not match the supported Vercel review. Prepare a new review.",
  deploy_authority_unconfirmed: "Deployment permission could not be confirmed. Check the saved task and your Vercel account before retrying.",
  deploy_output_bounds: "The deployment tool returned more output than Supymem could verify. Check your Vercel account before retrying.",
  coding_loop_scope_unsupported: "The local coding request changed or is unsupported. Prepare a new coding review.",
  coding_agent_auth_required: "Sign in to your coding CLI in Terminal, then re-detect it in Settings.",
  coding_checks_unavailable: "Install Node in Terminal before running this app's local checks.",
  coding_deploy_auth_required: "Sign in to Vercel in Terminal by running vercel login, then prepare a new deployment review.",
  coding_deploy_cli_missing: "Install the Vercel CLI in Terminal or use the deployment checklist.",
  coding_deploy_source_changed: "The saved files changed after deployment was reviewed. Prepare a new deployment review.",
  handler_failed: "Something went wrong while running this step. Your saved work is safe. Try again.",
  pagebuilder_renderer_unavailable: "The landing page preview could not start. Your idea is saved. Try building again.",
  pagebuilder_generation_failed: "The landing page could not be built. Your idea is saved. Try building again.",
  pagebuilder_job_failed: "The landing page build stopped. Your idea is saved. Try building again.",
  public_search_unavailable: "Public search is busy. Retry search in a moment.",
  public_search_blocked: "Public search is unavailable from this workspace. Add a contact you know.",
  no_public_candidates: "No public company pages matched this audience. Refine the audience or add a contact you know.",
  research_boundary_blocked: "Some source material could not be used safely. Review the saved findings and run research again.",
  research_provider_rejected: "The research service could not complete the request. Review the saved findings and run research again.",
  research_provider_rate_limited: "The research service is busy. Try again in a moment.",
  device_browser_ai_configuration_unavailable: "This saved browser AI profile is unavailable. Its exact models, pricing and execution configuration must be supported before preparation or execution can continue. No other model is substituted; your saved work is retained. Cancel an unstarted task, then rerun it and approve a fresh review with the current profile. Stop started work and review its saved result before rerunning.",
  subscription_static_app_ambiguous: "This venture has multiple possible website targets. Review the intended product before preparing another coding task.",
  subscription_static_publication_baseline_ambiguous: "The current website publication is ambiguous. Open Website and reconcile it before reviewing replacement source.",
  subscription_static_publication_baseline_changed: "The published website changed after this review. Prepare a fresh coding review before QA or replacement.",
  subscription_static_publication_baseline_unverified: "The previous website source or QA evidence could not be verified. Open Website to review its saved evidence.",
  subscription_static_qa_policy_unavailable: "Independent static QA is unavailable. Your saved source is preserved; try reviewing its QA budget later.",
  subscription_static_qa_policy_invalid: "This QA configuration can no longer be used. Review a fresh QA budget.",
  subscription_static_approved_inputs_changed: "The approved landing task changed. Prepare a fresh coding review for its current contract.",
  subscription_static_quote_changed: "The source or QA budget changed. Review a fresh QA budget before starting.",
  subscription_static_budget_approval_changed: "The approved QA ceiling does not match this quote. Review the budget again.",
  subscription_static_app_has_open_run: "This product already has an active run. Open Builds to finish or cancel it first.",
  subscription_static_scope_unavailable: "This saved source is not eligible for static QA. Review the current approved landing task.",
  subscription_static_qa_budget_unavailable: "The QA reservation could not be confirmed. Open Builds to check saved progress before retrying.",
  research_insufficient_credits: "There are not enough credits to run research again. Request more in Settings.",
  research_inputs_changed: "Your idea or sources changed. Review them before running research again.",
  research_saved_sources_unavailable: "The saved sources are no longer available. Run research again to collect fresh ones.",
  research_outcome_uncertain: "Research was interrupted. Run research again, or skip for now.",
  research_interrupted: "Research was interrupted. Run research again, or skip for now.",
  validation_requires_revalidation: "Your idea changed. Validate the current version before running research.",
  import_upload_capacity: "Too many uploads are running right now. Try again in a minute.",
  import_upload_in_progress: "An upload is already running for this project. Wait for it to finish.",
  import_quota_exceeded: "This company has reached its import limit.",
  import_byte_quota_exceeded: "This company has reached its import storage limit.",
  import_archive_size_invalid: "That archive is too large or empty. Upload a smaller ZIP.",
  import_archive_invalid: "That file is not a readable ZIP archive.",
  import_empty_source: "That project has no files to import.",
  import_repository_unavailable: "That repository could not be reached. Check the address and access.",
  import_github_rate_limited: "GitHub is limiting requests right now. Try again in a few minutes.",
  import_retry_not_available: "This import cannot be retried. Start a new import.",
  import_write_forbidden: "Your account cannot change imports for this company.",
  state_version_conflict: "This import changed while you were looking at it. Refresh and try again.",
  import_state_changed: "This import changed while you were looking at it. Refresh and try again.",
  hosting_revision_baseline_changed: "The published landing page changed while you were editing. Reload and try again.",
  hosting_revision_source_changed: "The published landing page changed while you were editing. Reload and try again.",
  hosting_revision_instruction_invalid: "Describe the change in a little more detail (at least a short sentence).",
  hosting_revision_not_approved: "Approve this revision request before starting it.",
  hosting_revision_request_conflict: "A revision request is already saved for this landing page. Refresh to see it.",
  hosting_revision_request_not_found: "That revision request no longer exists. Refresh and try again.",
  hosting_live_static_site_required: "Publish your landing page first, then you can request revisions.",
  hosting_deployment_not_live: "Your landing page is not live right now. Publish it, then try again.",
  gtm_invalid_input: "Some of the details were not accepted. Check what you entered and try again.",
  gtm_write_forbidden: "Your account cannot run this diagnosis for this company.",
  gtm_not_found: "That company or run could not be found. Refresh and try again.",
  gtm_venture_unavailable: "This company is not available right now. Refresh and try again.",
  gtm_insufficient_credits: "Not enough credits to run this diagnosis. Request more in Settings.",
  gtm_quote_changed: "The input changed. Review it again before approving.",
  gtm_operation_in_progress: "A diagnosis is already running. Wait for it to finish.",
  gtm_provider_unavailable: "Open Keys and connections and save your model-provider key, then run your diagnosis again.",
  gtm_generation_failed: "Open Go to market and refresh your diagnosis status before trying again.",
  content_brief_required: "Save your company brief first, then try again.",
  content_company_topic_required: "Plan new post ideas from your current brief, then draft your post.",
  content_scope_mismatch: "This content belongs to a different company. Switch companies and try again.",
  content_actor_required: "Sign in again to continue.",
  content_write_forbidden: "Your account cannot change content for this company.",
  content_venture_unavailable: "This company is not available right now. Refresh and try again.",
  content_pricing_unavailable: "The price for this could not be worked out right now. Try again shortly.",
  content_provider_unavailable: "The writing service is unavailable right now. Try again in a few minutes.",
  content_insufficient_credits: "Not enough credits for this. Request more in Settings.",
  content_publication_insufficient_credits: "Not enough credits to publish this. Request more in Settings.",
  content_invalid_output: "The draft did not come out right. Try again.",
  content_authoring_contract_unsupported: "This content type is not supported yet.",
  device_order_load_unconfirmed: "Could not load this Mac task. Refresh to check its saved state.",
  device_order_not_found: "This Mac task could not be found.",
  device_order_forbidden: "Your account cannot open this Mac task.",
  native_action_unconfirmed: "Could not confirm that action on your Mac. Refresh the task before retrying.",
  native_desktop_click_reconciliation_unavailable: "Could not confirm that click on your Mac. Refresh the task before retrying.",
};
const FAMILY_COPY: Array<[string, string]> = [
  ["research_", "Research could not continue. Refresh and try again."],
  ["import_", "That import step could not finish. Refresh and try again."],
  ["hosting_", "That website step could not finish. Refresh and try again."],
  ["gtm_", "The go-to-market step could not finish. Refresh and try again."],
  ["content_", "That content step could not finish. Refresh and try again."],
  ["device_order_", "Could not complete that Mac task step. Refresh and try again."],
  ["native_", "Could not complete that step on your Mac. Refresh and try again."],
];

/** Plain sentence for a server error code, or undefined when it is not a code we know (even by family). */
export function errorCodeCopy(code?: string | null): string | undefined {
  if (code === "insufficient_credits" || code?.endsWith("_insufficient_credits")) return usageLimitMessage();
  if (!code) return undefined;
  const key = code.split(":")[0].toLowerCase();
  if (isLocalConsole()) {
    if (key === "content_disabled") return "Turn on Content in Capabilities. If setup is needed, save your model provider, key and chat model in Keys and connections > Your AI provider, then request your draft again.";
    if (["content_provider_unavailable", "content_provider_offline", "gtm_provider_unavailable", "gtm_llm_unavailable"].includes(key)) return "Save your model provider, key and chat model in Keys and connections > Your AI provider, then retry your request.";
    if (["unauthorized", "content_actor_required", "signed_out"].includes(key)) return "Your owner session could not be verified. Restart your local app to reconnect your session, then retry your request.";
    if (key === "account_changed_during_request") return "Your owner session changed while this step finished. Check its saved status before trying again. If access still fails, restart your local app to reconnect your owner session.";
    if (["forbidden", "content_write_forbidden", "gtm_write_forbidden", "import_write_forbidden"].includes(key)) return "Open your venture from All ventures, then retry your request. If access still fails, restart your local app to reconnect your owner session.";
    if (key === "external_execution_disabled") return "Turn on Connected apps in Capabilities. If setup is needed, save your API key in Keys and connections > Connected apps and connect your publishing account in Connections, then retry publishing your post. Nothing was published.";
    if (key === "device_product_build_parser_unavailable") return "Your local app needs an update before this build can continue. Your work is saved. Update your app, then try again.";
  }
  return CODE_COPY[key] ?? COPY[key] ?? FAMILY_COPY.find(([prefix]) => code.startsWith(prefix))?.[1];
}

/** Known code -> copy, bare unknown code -> fallback, anything else is already readable text. */
export function plainErrorText(text: string, fallback: string): string {
  const body = text.trim();
  return errorCodeCopy(body) ?? (/^[a-z][a-z0-9_]*$|\bjob_[a-z0-9]+\b|\b\w*(?:Error|Exception)\b|\b[a-z][a-z0-9_]+:[A-Za-z0-9_]+\b/.test(body) ? fallback : body);
}

/** Founder sentence for an error thrown by opsApi ("API 409: content_scope_mismatch"). */
export function opsErrorMessage(err: unknown, fallback = "Something went wrong. Try again."): string {
  if (!(err instanceof Error)) return fallback;
  return plainErrorText(/^API \d+: ([\s\S]*)$/.exec(err.message)?.[1] ?? err.message, fallback);
}
