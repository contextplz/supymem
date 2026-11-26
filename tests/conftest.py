"""Pytest isolation: keep company_os / gateway / warden / environment `app` packages from colliding.

Company OS tests must use `tests.company_os_harness.load_company_os_app()` (or warden_harness)
rather than relying on a global `services/company_os` entry on sys.path.

Environment package tests live under `canary/services/environment/tests` and manage their own path.
"""

from __future__ import annotations

import os
import socket
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("COMPANY_OS_DEPLOYMENT_CONFIG", str(ROOT / "config/deployment.example.json"))

# Never leave CHANNELS_DRY_RUN off in the test process.
os.environ.setdefault("CHANNELS_DRY_RUN", "true")
# Legacy-path tests are the default; new-user-path tests enable NEW_USER_FLOW explicitly.
os.environ.setdefault("NEW_USER_FLOW", "off")
# Background Idea page refreshes would add a housekeeping job to every "no work queued" assertion;
# tests/unit/test_idea_page.py turns them on explicitly.
os.environ.setdefault("IDEA_PAGE_REFRESH", "off")

_HOSTED_ONLY = {
    "tests/unit/intake/test_entry_first_minutes.py": {
        "test_setup_review_keeps_the_saved_answers",
        "test_ready_setup_side_question_preserves_the_known_direction",
        "test_setup_side_question_answers_without_consuming_progress",
    },
    "tests/unit/intake/test_smart.py": {
        "test_unsure_founder_gets_interest_grounded_directions_without_an_invented_idea",
    },
    "tests/unit/llm/test_bedrock_fallback.py": {
        "test_foundry_503_falls_back_to_bedrock",
        "test_non_retryable_does_not_fallback",
    },
    "tests/unit/llm/test_chat_sol_cutover.py": {
        "test_route_row_moves_only_the_router",
        "test_both_chat_rows_and_removal_falls_back_to_deepseek",
        "test_chat_api_sol_capture_and_deepseek_rollback",
    },
    "tests/unit/product/test_next_step.py": {
        "test_guided_answer_returns_confirmation_card_for_chat_action",
    },
    "tests/unit/research/test_loot_drop_index.py": {
        "test_weekly_index_job_is_registered_and_deduplicated",
    },
    "tests/unit/research/test_paid_search_paths.py": {
        "test_real_gate_without_account_refuses_before_search_io",
        "test_sync_real_gate_no_account_precedes_provider_io",
        "test_hosted_limiter_non_pg_or_down_refuses_before_http",
    },
}
_HOSTED_INFRA = {
    "tests/unit/infra/test_auth_custom_domain.py",
    "tests/unit/infra/test_b5_build_runtime_terraform.py",
    "tests/unit/infra/test_production_build_runtime_terraform.py",
}
_PLATFORM_TEST_MODULES = {
    f"tests/unit/llm/test_{name}.py" for name in (
        "client_catalog_ledger", "platform_credential_capture", "reasoning_effort", "single_attempt",
        "sol_dispatch", "time_context", "tool_round_budget",
    )
} | {"tests/unit/simulation/test_provider_mode.py"}
_PLATFORM_TESTS = {
    "tests/unit/chat/test_customers_in_chat.py": {
        "test_empty_customer_search_offers_public_routes_and_still_accepts_explicit_refinements",
        "test_hidden_page_links_become_in_chat_actions",
    },
    "tests/unit/llm/test_provider_limiter.py": {"test_limiter_is_on_real_sdk_chat_and_responses_requests"},
    "tests/unit/test_forced_update_halts_hosted.py": {"test_cloud_provider_ignores_desktop_update_snapshot"},
    "tests/unit/test_sole_llm_provider.py": {
        "test_require_env_api_key_ok_when_matches", "test_require_env_api_key_rejects_mismatch",
    },
}
_PRIVATE_WORKFLOW = ".github/workflows/ci.yml"
_PRIVATE_HOSTED_CORE = ("services/company_os/app/admin/internal.py",)
_PRIVATE_CONSUMER_API = ("services/company_os/app/api/consumer_api.py",)
_PRIVATE_GATEWAY = ("services/gateway/app/main.py",)
_PRIVATE_PAGE_ARCHIVES = tuple(f"tests/unit/pagebuilder/fixtures/{name}_real_codex_output.tgz" for name in ("macpage", "macpage2"))
_PRIVATE_PMF_PACK = ("services/company_os/app/knowledge/startup_pmf.json.gz",)
_PRIVATE_COACHING_PACKS = (*_PRIVATE_PMF_PACK, "services/company_os/app/knowledge/yc_video_advice.json.gz")
_PRIVATE_HARNESS = ("packages/harness/loop.py",)
_PRIVATE_TICK = ("services/company_os/app/api/internal_tick.py",)
_PRIVATE_STRIPE = ("services/company_os/app/payments/stripe.py",)
_PRIVATE_FIXTURE_INPUTS = {
    "tests/integration/test_gateway_auth.py": {"client": _PRIVATE_GATEWAY},
    "tests/integration/test_no_secret_egress.py": {"client": _PRIVATE_GATEWAY},
    "tests/integration/test_pkce_device.py": {"sqlite_device": ("services/gateway/app/device_auth.py",)},
    "tests/integration/test_postgres_auth_security.py": {"gateway": ("services/gateway/app/device_auth.py",)},
    "tests/integration/test_sse_resume.py": {"client": _PRIVATE_GATEWAY},
    "tests/security/test_admin_boundary.py": {"gw": _PRIVATE_GATEWAY},
    "tests/security/test_legacy_execution_quarantine.py": {"legacy_builder": ("tests/builder_harness.py", "tests/warden_harness.py")},
    "tests/security/test_warden_assertion_boundary.py": {"warden": ("tests/warden_harness.py",)},
    "tests/unit/billing/test_subscription_lifecycle.py": {"billing_env": _PRIVATE_HOSTED_CORE},
    "tests/unit/credits/conftest.py": {"credits_env": _PRIVATE_HOSTED_CORE},
    "tests/unit/firstlook/test_copy_paid_outcome.py": {"legacy_model": _PRIVATE_HOSTED_CORE},
    "tests/unit/lead_library/test_reclassify.py": {"reclassifier": ("scripts/leads/reclassify.py",)},
    "tests/unit/llm/test_build_provider_pin.py": {"current_app_modules": _PRIVATE_HOSTED_CORE},
    "tests/unit/pagebuilder/test_honesty.py": {"tool_output[b2b]": _PRIVATE_PAGE_ARCHIVES},
    "tests/unit/pagebuilder/test_tool_contract.py": {"page[b2b]": _PRIVATE_PAGE_ARCHIVES},
    "tests/unit/test_bug_reports.py": {"client": _PRIVATE_CONSUMER_API},
    "tests/unit/test_consumer_identity.py": {"client": _PRIVATE_CONSUMER_API, "consumer_env": _PRIVATE_HOSTED_CORE},
    "tests/unit/test_consumer_trial_expiry.py": {"app_client": (*_PRIVATE_CONSUMER_API, "services/company_os/app/api/hosted_llm_api.py")},
    "tests/unit/test_email_engine.py": {"env": _PRIVATE_HOSTED_CORE},
    "tests/unit/test_founder_analytics_acl.py": {"client": _PRIVATE_CONSUMER_API},
    "tests/unit/test_password_policy.py": {"users_mod": ("services/gateway/app/users.py",)},
    "tests/unit/test_venture_soft_delete.py": {"client": _PRIVATE_HOSTED_CORE},
}
_PRIVATE_INPUTS = {
    "tests/unit/billing/test_stripe_checkout.py": {
        "test_checkout_closed_until_payments_accepting": ("services/company_os/app/payments/registry.py",),
        "test_checkout_uses_stripe_when_configured": _PRIVATE_STRIPE,
    },
    "tests/unit/chat/test_live_harness_steer.py": {"test_live_instruction_does_not_enqueue": _PRIVATE_HARNESS},
    "tests/unit/credits/test_metering_guard.py": {
        "test_converted_feature_modules_keep_shared_facade[hosted-think]": ("services/company_os/app/gbrain/think.py",),
    },
    "tests/unit/idea/test_gstack_office_hours.py": {
        "test_summary_and_looks_right_show_only_relevant_offline_references[relevant-pack]": _PRIVATE_COACHING_PACKS,
    },
    "tests/unit/planning/test_harness_research.py": {"test_harness_research_uses_explore_then_writes_note": _PRIVATE_HARNESS},
    "tests/unit/test_linear_connector.py": {
        "test_linear_fake_lists_teams": ("data/fixtures/connectors/linear/resources.json",),
        "test_linear_webhook_rejects_tamper_and_stale": ("data/fixtures/connectors/linear/webhook.json",),
    },
    "tests/unit/test_outreach_stripe_webhook.py": {"*": _PRIVATE_STRIPE},
    "tests/unit/test_referral_rewards.py": {
        "test_claim_replay_self_legacy_usage_and_private_summary": ("services/company_os/app/api/beta_api.py",),
    },
    "tests/unit/test_teardown_connections.py": {
        "test_teardown_refuses_connected_workspace": ("data/fixtures/connectors/linear/account.json",),
    },
    "tests/unit/chat/test_yc_coaching.py": {
        "test_local_advice_prompt_uses_bundled_coaching_and_names_missing_files": _PRIVATE_COACHING_PACKS,
        "test_relevance_and_frequency": _PRIVATE_COACHING_PACKS[1:],
    },
    "tests/unit/product/test_stages_20_23.py": {"test_admin_security_lists_events": _PRIVATE_HOSTED_CORE},
    "tests/unit/tasks/test_scheduler.py": {
        "test_tick_transaction_belongs_to_scheduler_service": _PRIVATE_TICK,
        "test_signed_tick_and_scheduler_share_dedupe_window": _PRIVATE_TICK,
        "test_signed_tick_respects_specialist_worker_kinds": _PRIVATE_TICK,
    },
    "tests/unit/test_pmf_knowledge.py": {
        **dict.fromkeys((
            "test_complete_content_and_original_preserved",
            "test_every_explicit_case_reference_resolves_without_alias_collision",
        ), (*_PRIVATE_PMF_PACK, "data/knowledge/startup-pmf-2026-09-19/manifest.json")),
        **dict.fromkeys((
            "test_secondary_unheaded_source_table_and_dates_are_interpreted",
            "test_retrieval_preserves_provenance_and_does_not_fabricate_matches",
            "test_theories_stay_theories_and_urls_are_never_executed",
            "test_planning_context_is_reproducible_and_versioned",
            "test_contextual_cases_favor_relevant_customer_model_and_problem",
            "test_job_seeker_context_does_not_become_security_or_developer_tooling",
            "test_sales_problem_can_retrieve_playbook_before_a_case",
            "test_chat_workbook_answer_cites_a_relevant_row_or_abstains",
        ), _PRIVATE_PMF_PACK),
    },
    "tests/unit/planning/test_research_completion.py": dict.fromkeys((
        "test_actual_harness_only_named_successful_page_read_can_qualify",
        "test_actual_harness_failure_preserves_brief_and_does_not_request_fallback_search",
        "test_actual_harness_analyst_cannot_adopt_researcher_search_authority",
    ), _PRIVATE_HARNESS),
    "tests/unit/tasks/test_progress.py": dict.fromkeys((
        "test_actual_harness_turn_reflects_from_real_pages_then_requires_new_query",
        "test_actual_executor_unavailable_tool_stops_with_no_reflection_or_fallback_call",
        "test_actual_twenty_source_goal_uses_bounded_remaining_tool_budget",
    ), _PRIVATE_HARNESS),
    "tests/integration/test_pkce_device.py": {
        "test_pkce_and_bearer": ("services/gateway/app/device_auth.py",),
        "test_sqlite_existing_device_schema_upgrades_without_losing_token": ("services/gateway/app/users.py",),
    },
    "tests/security/test_connector_oauth_boundary.py": {"*": _PRIVATE_HOSTED_CORE},
    "tests/security/test_core_signed_boundary.py": {"*": _PRIVATE_HOSTED_CORE},
    "tests/unit/builder/test_static_lifecycle.py": {
        "test_local_managed_watch_preserves_hosted_artifacts_without_running_hosted_checks": (
            "services/company_os/app/build/subscription_product_diagnostic_controller.py",
        ),
    },
    "tests/unit/chat/test_landing_lane.py": dict.fromkeys((
        "test_unconfirmed_landing_page_request_is_friendly_and_keeps_idea_action",
        "test_build_my_landing_page_never_offers_a_template_page",
    ), _PRIVATE_HOSTED_CORE),
    "tests/unit/knowledge/test_founder_coaching.py": {
        "test_idea_references_abstain_on_failure_and_never_pad_a_single_case": _PRIVATE_PMF_PACK,
        "test_references_require_customer_industry_or_decision_overlap[relevant-pack]": _PRIVATE_COACHING_PACKS,
        "test_real_cards_have_why_do_avoid_metric_sources_and_refresh": _PRIVATE_PMF_PACK,
        "test_yc_mode_switch_and_real_excerpt": _PRIVATE_COACHING_PACKS[1:],
        "test_compact_runtime_manifest_and_planning_context": _PRIVATE_COACHING_PACKS[1:],
        "test_idea_references_mode_switch_personal_and_incidental_requirements": _PRIVATE_COACHING_PACKS,
        "test_local_data_directory_preserves_retrieval_and_hosted_loading": _PRIVATE_COACHING_PACKS,
        "test_local_integrity_lists_pinned_files_and_detects_damage": _PRIVATE_COACHING_PACKS,
        "test_local_founder_questions_return_cited_coaching": _PRIVATE_COACHING_PACKS,
        "test_local_relevance_review_uses_cited_ranking_without_a_provider": _PRIVATE_PMF_PACK,
    },
    "tests/unit/local_flows/test_knowledge_flow.py": {
        "test_pmf_retrieval_and_source_record_work_without_a_search_key": _PRIVATE_COACHING_PACKS,
        "test_yc_and_pmf_coaching_reply_contains_retrievable_citations": _PRIVATE_COACHING_PACKS,
    },
    "tests/unit/pagebuilder/test_honesty.py": {
        "test_real_tool_output_reaches_independent_html_checks": _PRIVATE_PAGE_ARCHIVES[:1],
    },
    "tests/unit/test_cu_browser_round1.py": {"*": tuple(
        f"docs/supymem/beta-2026-10-04/browser-tests/{kind}_tasks.yaml" for kind in ("short", "long", "ambiguous")
    )},
    "tests/unit/test_session_subject_is_real_user_id.py": {"*": ("services/gateway/app/session.py",)},
    **{f"tests/integration/test_gateway_{name}.py": {"*": ("services/gateway/app/main.py",)} for name in (
        "b1_security", "edge_hardening", "native_auth", "ratelimit", "venture_waitlist",
        "accept_headers", "beta_download", "binary_transport", "import_transport", "interest_transport", "oauth_callbacks",
    )},
    "tests/integration/test_cookie_flags.py": dict.fromkeys((
        "test_hosted_cookie_deletion_keeps_host_prefix_requirements",
        "test_hosted_cookie_name_is_host_prefix",
    ), ("services/gateway/app/auth.py",)),
    "tests/integration/test_no_secret_egress.py": {
        "test_proxy_redaction_is_authoritatively_tenant_scoped": ("services/gateway/app/proxy.py",),
    },
    "tests/integration/test_postgres_app_runtime_intents.py": {
        "test_isolated_database_runner_uses_injected_values_without_exposing_password": (
            "services/company_os/app/provisioning/run_database.py",
        ),
    },
    "tests/integration/test_postgres_auth_security.py": {
        "test_federated_gateway_profile_is_readable_by_core_without_password_or_email_collision": (
            "services/company_os/app/admin/catalog.py",
        ),
        "test_gateway_accepts_bare_postgres_urls": ("services/gateway/app/users_pg.py",),
        "test_gateway_http_device_exchange_refresh_and_revocation": ("services/gateway/app/auth.py",),
    },
    "tests/integration/test_postgres_charter_working_memory.py": {
        "test_legacy_canonical_charter_projects_without_cache_import": (
            "services/company_os/app/gbrain/__init__.py",
        ),
    },
    "tests/integration/test_postgres_chat_memory.py": {
        "test_complete_dedupe_and_context_never_clip_constraint": ("services/company_os/app/gbrain/verbs.py",),
        "test_source_bound_internal_and_portfolio_scope": ("services/company_os/app/gbrain/cycle.py",),
    },
    "tests/integration/test_postgres_native_failure_attribution.py": {
        "test_queued_browser_failure_reaches_signed_telemetry_and_error_pipeline": (
            "services/company_os/app/admin/router.py",
        ),
    },
    "tests/integration/test_postgres_public_memory_commands.py": {
        "test_public_memory_target_cannot_cross_workspace_or_venture": (
            "services/company_os/app/gbrain/verbs.py",
        ),
    },
    "tests/integration/test_postgres_wallet_bridge_rollout.py": {
        "test_delayed_stub_checkout_refund_and_pending_admin_grant_follow_wallet": (
            "services/company_os/app/admin/grants.py",
            "services/company_os/app/payments/service.py",
        ),
    },
    "tests/security/test_admin_boundary.py": {
        "test_admin_login_error_does_not_leak_email_existence": ("services/gateway/app/users.py",),
        "test_admin_login_requires_totp": ("services/gateway/app/users.py",),
        "test_revoked_admin_session_is_rejected": (
            "services/gateway/app/admin_auth.py",
            "services/gateway/app/users.py",
        ),
    },
    "tests/security/test_gateway_users_engine_pool.py": {
        "test_pg_engine_is_built_once_per_url": ("services/gateway/app/users.py", "services/gateway/app/users_pg.py"),
    },
    "tests/security/test_beta_funnel.py": {
        "test_job_outcomes_and_feedback_and_admin_route": ("services/company_os/app/admin/router.py",),
    },
    "tests/security/test_founder_credential_readers.py": dict.fromkeys((
        "test_hosted_venture_without_stripe_key_never_calls_platform",
        "test_vault_backend_that_cannot_release_credentials_never_falls_back_on_hosted",
        "test_venture_stripe_uses_per_request_own_key_without_global_sdk_mutation",
    ), ("services/company_os/app/money/stripe_client.py",)),
    "tests/security/test_hosted_execution_boundary.py": dict.fromkeys((
        "test_hermes_uses_trusted_gateway",
        "test_signed_local_qa_keeps_its_configured_gateway",
        "test_bad_hermes_origin_cannot_revoke_or_mint",
    ), ("services/company_os/app/hermes/bootstrap.py",)),
    "tests/security/test_idea_validation_boundary.py": {
        "test_gbrain_uses_actual_scoped_memory_and_excludes_foreign_venture": (
            "services/company_os/app/gbrain/__init__.py",
        ),
    },
    "tests/security/test_payments_boundary.py": {
        "test_complete_stub_is_refused_while_payments_are_closed": (
            "services/company_os/app/api/consumer_api.py",
        ),
    },
    "tests/security/test_strix_core_api.py": {
        "test_hosted_live_checkout_does_not_activate_until_payment": (
            "services/company_os/app/payments/base.py",
        ),
    },
    "tests/unit/chat/test_durable_memory.py": dict.fromkeys((
        "test_memory_api_boundary_and_forget",
        "test_preference_never_truncated_in_context_pack",
    ), ("services/company_os/app/gbrain/verbs.py",)),
    "tests/unit/chat/test_idea_review.py": {
        "test_rejected_review_saves_confirmable_summary": ("services/company_os/app/gbrain/__init__.py",),
    },
    "tests/unit/chat/test_production_batch_handoffs.py": {
        "test_credit_request_does_not_claim_exhaustion_or_purchase": (
            "services/company_os/app/api/payments_api.py",
        ),
    },
    "tests/unit/credits/test_paid_paths.py": {
        "test_gbrain_and_localization_reach_metered_llm": ("services/company_os/app/gbrain/__init__.py",),
    },
    "tests/unit/memory/test_brief_availability.py": {
        "test_explicit_remember_dedupe_binds_actor_and_source_not_just_statement": (
            "services/company_os/app/gbrain/verbs.py",
        ),
    },
    "tests/unit/memory/test_consumer_scope.py": {
        "test_scheduled_jobs_project_two_workspaces": ("services/company_os/app/gbrain/__init__.py",),
    },
    "tests/unit/product/test_journey_overrides.py": {
        "test_every_skip_to_later_step_has_usable_downstream_responses": (
            "services/company_os/app/gbrain/__init__.py",
        ),
    },
    "tests/unit/product/test_native_failure_attribution.py": {
        "test_mac_failure_summary_retains_installed_version_cause_and_feature": (
            "services/company_os/app/admin/router.py",
        ),
    },
    "tests/unit/product/test_stages_15_19.py": {
        "test_ai_proxy_v1_with_key": ("tests/unit/product/test_hermes_platform_credits.py",),
    },
    "tests/unit/test_desktop_parent_watch.py": dict.fromkeys((
        "test_no_watch_without_parent_pid",
        "test_sidecar_exits_when_parent_pid_is_gone",
    ), ("packages/desktop_runtime/frozen_main.py",)),
    "tests/unit/test_health_ready.py": dict.fromkeys((
        "test_gateway_health_ok_ready_503_when_db_unavailable",
        "test_gateway_ready_200_when_deps_healthy",
        "test_warden_health_ok_ready_503_when_db_unavailable",
        "test_warden_ready_200_when_db_healthy",
    ), ("tests/warden_harness.py",)),
    "tests/unit/test_local_mode.py": {
        "test_local_sibling_services_enforce_browser_boundary": ("tests/warden_harness.py",),
    },
    "tests/unit/test_shared_ratelimit.py": {
        "test_three_same_ip_signup_and_founder_journeys_fit_default_limits": (
            "services/gateway/app/ratelimit.py",
        ),
    },
    "tests/unit/test_ui_config.py": dict.fromkeys((
        "test_admin_flags_preserve_boolean_contract_and_validate_layout",
        "test_founder_config_is_read_only_and_cannot_change_global_flags",
    ), ("services/company_os/app/admin/router.py",)),
    "tests/unit/analytics/test_feature_catalog.py": {
        "test_every_inventory_feature_maps_to_a_slug": ("docs/supymem/feature-audit-2026-09-30/INVENTORY.md",),
    },
    "tests/unit/chat/test_jarvis_control.py": {
        "test_account_questions_and_requests_run_without_cards[hosted]": ("services/company_os/app/api/payments_api.py",),
        "test_every_founder_page_is_reachable_by_name": (
            "apps/console/src/app/pagebuilder-lab/layout.tsx", "apps/console/src/app/lab/diagrams/layout.tsx",
        ),
    },
    "tests/unit/console/test_no_dollar_founder_credits.py": {
        "test_founder_credit_surfaces_have_no_dollar_copy": ("apps/console/src/components/credits/TopUpCard.tsx",),
    },
    "tests/unit/db/test_scale_pools.py": dict.fromkeys((
        "test_default_pools_and_nested_send_fences_release_connections",
        "test_main_ledger_pricing_share_pool_and_configure_proxy",
    ), ("services/gateway/app/users_pg.py",)),
    "tests/unit/email/test_theme.py": {
        "test_email_tokens_match_console": ("apps/console/public/email/supymem-logo.png",),
        "test_gateway_adds_branded_html_without_changing_plain_text": ("services/gateway/app/mail.py",),
    },
    "tests/unit/infra/test_ci_budget_guard.py": {
        "test_reservation_covers_workflow_timeout_cost_bound": (_PRIVATE_WORKFLOW,),
    },
    "tests/integration/test_postgres_planning.py": {
        "test_operator_probe_reads_the_failed_context_without_changing_its_hold_or_result": ("scripts/infra/probe_preserved_plan.py",),
    },
    "tests/integration/test_postgres_browser_bounded_reads_controller.py": dict.fromkeys((
        "test_actual_controller_stages_one_pending_order_and_advances_only_after_real_stop_settlement",
        "test_controller_retains_expired_and_cancelled_seeds_without_reset",
        "test_controller_waits_for_real_metered_result_and_settlement_after_stop",
    ), ("scripts/desktop/cu_browser_qualification_controller.py",)),
    "tests/security/test_public_error_and_ingest_security.py": {
        "test_adapter_errors_never_echo_provider_details": ("services/llm_adapter/llm_adapter/main.py",),
        "test_internal_ops_refuses_missing_or_short_secret": ("services/company_os/app/api/internal_ops.py",),
        "test_internal_ops_uses_constant_time_bytes_comparison": ("services/company_os/app/api/internal_ops.py",),
    },
    "tests/unit/infra/test_hotfix_console_cache_contract.py": dict.fromkeys((
        "test_manual_console_rolls_serialize_without_canceling_active_or_pending_runs",
        "test_cache_export_is_bounded_nonfatal_but_cannot_skip_build_or_rollout",
        "test_runtime_chunks_do_not_invalidate_builder_inputs_or_override_current_chunks",
    ), (".github/workflows/hotfix-console.yml",)),
    "tests/unit/infra/test_stage6_surfaces.py": {
        "test_ai_proxy_health": ("services/ai_proxy/app/main.py",),
        "test_workflows_parse": (_PRIVATE_WORKFLOW, ".github/workflows/images.yml", ".github/workflows/deploy-staging.yml"),
    },
    "tests/unit/intake/test_upload_gateway.py": {
        "test_gateway_upload_envelope_and_normal_limits": ("services/gateway/app/body_limit.py",),
    },
    "tests/unit/planning/test_social_result_completion.py": {
        "test_social_regressions_remain_selected_in_ci": (_PRIVATE_WORKFLOW,),
    },
    "tests/unit/test_browser_qualification_catalog.py": {
        "test_catalog_retains_exact_26_plus_six_plus_four_goals_and_oracles": (
            "docs/supymem/beta-2026-10-04/browser-tests/long_tasks.yaml",
        ),
    },
    "tests/unit/test_desktop_local_review.py": {
        "test_local_review_is_refused_by_release_collector_and_publisher": (
            "scripts/desktop/collect_macos.py", "scripts/publish_desktop_downloads.py",
        ),
    },
    "tests/unit/test_error_capture_coverage.py": {
        "test_gateway_warden_and_console_install_the_standard_reporter": (
            "services/gateway/app/main.py", "services/warden/app/main.py",
        ),
    },
    "tests/unit/test_layering.py": {
        "test_local_flow_files_are_registered_once_in_ci": (_PRIVATE_WORKFLOW,),
        "test_windows_release_smokes_the_installed_nsis_payload": (".github/workflows/desktop-release.yml",),
    },
    "tests/unit/test_local_startup.py": {
        "test_ci_keeps_hosted_boundaries_out_of_local_mode": (_PRIVATE_WORKFLOW,),
        "test_hosted_setup_keeps_provider_environment_and_native_update_help": ("docker-compose.companyos.yml",),
    },
    "tests/unit/test_scale_loadtest.py": {
        "test_stub_seed_is_scoped_and_unknown_providers_fail_closed": (
            "scripts/loadtest/k6/seed.py", "scripts/loadtest/k6/provider_stubs.py", "scripts/loadtest/k6/scenarios.js",
        ),
        "test_staging_requires_exact_unexpired_review_and_blocks_production": ("scripts/loadtest/k6/targets.js",),
        "test_soak_receipts_require_matching_measured_cost_and_recovery_evidence": ("scripts/loadtest/k6/receipts.py",),
    },
}


def pytest_configure(config: pytest.Config) -> None:
    # Drop service app roots from sys.path so `import app` is unambiguous.
    blocked = {
        (ROOT / "services" / "company_os").resolve(),
        (ROOT / "services" / "gateway").resolve(),
        (ROOT / "services" / "warden").resolve(),
        (ROOT / "services" / "environment").resolve(),
        (ROOT / "services" / "builder").resolve(),
        (ROOT / "config" / "scaffold").resolve(),
    }
    cleaned = []
    for p in sys.path:
        try:
            if Path(p).resolve() in blocked:
                continue
        except OSError:
            pass
        cleaned.append(p)
    sys.path[:] = cleaned


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    local = os.environ.get("SUPYMEM_MODE", "").strip().lower() == "local"
    for item in items:
        path = item.path.relative_to(ROOT).as_posix() if item.path.is_relative_to(ROOT) else ""
        name = getattr(item, "originalname", None)
        if path in _HOSTED_INFRA or name in _HOSTED_ONLY.get(path, ()):
            item.add_marker(pytest.mark.hosted_only)
        hosted_only = item.get_closest_marker("hosted_only")
        if hosted_only:
            item.add_marker(pytest.mark.hosted)
        if local and (hosted_only or item.get_closest_marker("hosted")):
            item.add_marker(pytest.mark.skip(reason="Hosted service contract; run with SUPYMEM_MODE unset."))
        private = _PRIVATE_INPUTS.get(path, {})
        inputs = set(private.get(getattr(item, "name", name), private.get(name, private.get("*", ()))))
        if isinstance(item, pytest.Function):
            parameters = item.callspec.params if hasattr(item, "callspec") else {}
            for fixture_name in item.fixturenames:
                if not (definitions := item._fixtureinfo.name2fixturedefs.get(fixture_name)):
                    continue
                function = definitions[-1].func
                code = getattr(function, "__code__", None)
                fixture_path = Path(code.co_filename) if code else None
                if fixture_path is None or not fixture_path.is_relative_to(ROOT):
                    continue
                requirements = _PRIVATE_FIXTURE_INPUTS.get(fixture_path.relative_to(ROOT).as_posix(), {})
                inputs.update(requirements.get(function.__name__, ()))
                if requirements and isinstance(parameter := parameters.get(fixture_name), str):
                    inputs.update(requirements.get(f"{function.__name__}[{parameter}]", ()))
        if missing := sorted(entry for entry in inputs if not (ROOT / entry).is_file()):
            item.add_marker(pytest.mark.hosted)
            item.add_marker(pytest.mark.skip(reason=f"Private checkout required: missing {', '.join(missing)}."))


@pytest.fixture(scope="module", autouse=True)
def _unit_data_defaults(request, tmp_path_factory):
    path = Path(getattr(request.module, "__file__", "")).resolve()
    if not path.is_relative_to(ROOT / "tests/unit") or path.parent == ROOT / "tests/unit/local_flows":
        yield None
        return
    directory = tmp_path_factory.mktemp("unit-data")
    patch = pytest.MonkeyPatch()
    for name, value in {
        "COMPANY_OS_DATA_DIR": str(directory),
        "COMPANY_OS_DB_URL": f"sqlite:///{directory}/owner.db",
        "COMPANY_OS_WORKING_MEMORY_PATH": str(directory / "memory.db"),
        "COMPANY_OS_SECRETS_ENV": str(directory / "import.env"),
        "SUPYMEM_LOCAL_PROXY_SECRET_FILE": str(directory / "owner-session.secret"),
    }.items():
        patch.setenv(name, value)
    try:
        yield directory
    finally:
        patch.undo()


@pytest.fixture(autouse=True)
def _unit_data_paths(_unit_data_defaults, tmp_path, monkeypatch):
    if _unit_data_defaults is None or os.environ.get("COMPANY_OS_DATA_DIR") != str(_unit_data_defaults):
        return
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/owner.db")
    monkeypatch.setenv("COMPANY_OS_WORKING_MEMORY_PATH", str(tmp_path / "memory.db"))
    monkeypatch.setenv("COMPANY_OS_SECRETS_ENV", str(tmp_path / "import.env"))


@pytest.fixture(autouse=True)
def _platform_provider_mode(request, monkeypatch):
    node = request.node
    path = node.path.relative_to(ROOT).as_posix() if node.path.is_relative_to(ROOT) else ""
    if path in _PLATFORM_TEST_MODULES or getattr(node, "originalname", None) in _PLATFORM_TESTS.get(path, ()):
        monkeypatch.delenv("SUPYMEM_MODE", raising=False)


@pytest.fixture(autouse=True)
def _isolate_local_flows(request, tmp_path, monkeypatch):
    path = Path(getattr(request.module, "__file__", "")).resolve()
    if path.parent != ROOT / "tests/unit/local_flows":
        yield
        return
    from tests.company_os_harness import load_company_os_app

    for name, value in {
        "SUPYMEM_MODE": "local", "APP_ENV": "local", "COS_TEST_APP_REUSE": "1",
        "COMPANY_OS_DATA_DIR": str(tmp_path), "COMPANY_OS_DB_URL": f"sqlite:///{tmp_path}/owner.db",
        "COMPANY_OS_WORKING_MEMORY_PATH": str(tmp_path / "memory.db"),
        "SUPYMEM_LOCAL_PROXY_SECRET_FILE": str(tmp_path / "owner-session.secret"),
        "COMPANY_OS_SECRETS_ENV": str(tmp_path / "import.env"),
        "SUPYMEM_DESKTOP_INSTANCE_ID": "local-flow", "SUPYMEM_SINGLE_PROCESS": "0",
        "LLM_OFFLINE": "1", "LLM_CASSETTE_MODE": "replay", "EMAIL_PROVIDER": "log",
    }.items():
        monkeypatch.setenv(name, value)
    for name in ("SUPYMEM_LOCAL_KEY_PIPE", "SUPYMEM_LOCAL_ALLOW_REMOTE", "DATABASE_URL", "DB_URL",
                 "REDIS_URL", "RDS_PROXY_ENDPOINT"):
        monkeypatch.delenv(name, raising=False)

    attempts = []

    def forbidden(*args, **kwargs):
        attempts.append(True)
        raise AssertionError("Your offline flow attempted a real network connection. Mock its transport before retrying.")

    for name in ("connect", "connect_ex", "sendto"):
        original = getattr(socket.socket, name)

        def guarded(connection, *args, _original=original, **kwargs):
            if connection.family in {socket.AF_INET, socket.AF_INET6}:
                forbidden()
            return _original(connection, *args, **kwargs)

        monkeypatch.setattr(socket.socket, name, guarded)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    import primp

    monkeypatch.setattr(primp.Client, "request", forbidden)
    load_company_os_app()
    from app.core import local_env_import, providers_local

    names = set(local_env_import._FIELDS) | {"SUPYMEM_LLM_PROVIDER"}
    names.update(name for fields in providers_local.PROVIDER_ENV.values() for name in fields)
    names.update(f"SUPYMEM_MODEL_{role.upper()}" for role in providers_local.ROLES)
    for name in names - {"LLM_OFFLINE", "LLM_CASSETTE_MODE"}:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(local_env_import, "repo_root", lambda: tmp_path)
    yield
    assert not attempts, "Your offline flow attempted a real network connection. Mock its transport before retrying."


@pytest.fixture(autouse=True)
def _local_entry_client_origin(request, monkeypatch):
    fixture = getattr(request.module, "client", None)
    if os.environ.get("SUPYMEM_MODE", "").strip().lower() != "local" or getattr(fixture, "__module__", "") != "tests.integration.test_entry_chat_api":
        return
    if "_isolated_local_setup" not in request.fixturenames:
        return
    request.getfixturevalue("_isolated_local_setup")
    client = request.getfixturevalue("client")
    monkeypatch.setitem(client.headers, "Origin", str(client.base_url).rstrip("/"))


@pytest.fixture(autouse=True)
def _wire_usage_boundary(request, tmp_path, monkeypatch):
    if Path(getattr(request.module, "__file__", "")).resolve() != ROOT / "tests/unit/llm/test_responses_wire_usage.py":
        return
    from tests.company_os_harness import load_company_os_app

    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COMPANY_OS_WORKING_MEMORY_PATH", str(tmp_path / "memory.db"))
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/wire.db")
    load_company_os_app()


@pytest.fixture(autouse=True)
def _local_paid_search_provider(request, monkeypatch):
    if os.environ.get("SUPYMEM_MODE", "").strip().lower() != "local" or Path(getattr(request.module, "__file__", "")).resolve() != ROOT / "tests/unit/research/test_paid_search_paths.py":
        return
    paths = request.getfixturevalue("paths")
    from app.research import local_providers

    monkeypatch.setattr(local_providers, "search_provider", lambda **kwargs: paths.explore._provider_from_env())


@pytest.fixture
def isolate_app_imports():
    saved_modules = {name: module for name, module in sys.modules.items()
                     if name == "app" or name.startswith("app.")}
    saved_path = list(sys.path)
    try:
        yield
    finally:
        for name in list(sys.modules):
            if name == "app" or name.startswith("app."):
                del sys.modules[name]
        sys.modules.update(saved_modules)
        sys.path[:] = saved_path


@pytest.fixture(autouse=True)
def _drain_background_threads():
    # Background work lazily imports `app`; a thread outliving its test can import across a later
    # sys.modules swap (company_os <-> gateway) and see a wrong or half-initialized package.
    yield
    import threading
    for thread in threading.enumerate():
        if thread.name in {"since-you-left", "research-graveyard"}:
            thread.join(timeout=30)


@pytest.fixture(autouse=True)
def _ensure_dry_run_channels(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHANNELS_DRY_RUN", "true")


@pytest.fixture(autouse=True)
def _beta_scope_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPYMEM_BETA_SCOPE", "0")  # beta gate has its own test; legacy tests exercise the full registry


@pytest.fixture(autouse=True)
def _isolate_desktop_diagnostics(tmp_path, monkeypatch):
    """Fixture failures must not write into the installed desktop's diagnostics."""
    from desktop_runtime import crash_report

    monkeypatch.setenv("COMPANY_OS_RUNTIME_LOG_DIR", str(tmp_path / "desktop_logs"))
    original_data_dir = crash_report._data_dir
    monkeypatch.setattr(crash_report, "_data_dir", lambda: original_data_dir()
        if os.environ.get("COMPANY_OS_DATA_DIR", "").strip() else tmp_path / "desktop_data")
    original_handlers = list(crash_report.log.handlers)
    for handler in original_handlers:
        crash_report.log.removeHandler(handler)
    monkeypatch.setattr(crash_report, "_log_ready", False)
    yield
    for handler in list(crash_report.log.handlers):
        crash_report.log.removeHandler(handler)
        if handler not in original_handlers:
            handler.close()
    for handler in original_handlers:
        crash_report.log.addHandler(handler)


@pytest.fixture(autouse=True)
def _isolate_unit_runtime_state(_unit_data_defaults, request, monkeypatch):
    if not request.path.is_relative_to(ROOT / "tests/unit"):
        return
    from core import logging as core_logging

    monkeypatch.setattr(core_logging, "_LOCAL_SECRETS", set(core_logging._LOCAL_SECRETS))
    if probe_module := sys.modules.get("desktop_runtime.agent_status"):
        probe_module.start_probes()
