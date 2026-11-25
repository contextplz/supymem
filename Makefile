.PHONY: help install seed verify test canary-test lint typecheck run up down logs \
	setup doctor migrate types acceptance redteam company-os-dev desktop-dev desktop-build desktop-runtime desktop-dev-hosted desktop-build-hosted desktop-runtime-hosted desktop-build-fast-hosted desktop-build-review-hosted desktop-build-staging-hosted desktop-update-test qm-dev qm-dev-stop qm-install qm-ensure-crons \
	skills-fetch skills-ingest worker tick smoke record-demo

PYTHON ?= python3.12
PNPM ?= pnpm
VENV ?= .venv
PY := $(VENV)/bin/python
PIP := $(VENV)/bin/pip

# Company OS tests through Phases 2–4 (offline / deterministic)
COS_TESTS := \
	tests/unit/test_ids.py tests/unit/test_redact.py tests/unit/test_schema.py \
	tests/unit/test_eventbus.py tests/unit/test_jobs.py \
	tests/unit/fsm/test_machines.py tests/unit/db/test_supymem_migrations.py \
	tests/unit/jobs/test_catalog.py tests/unit/jobs/test_lease.py tests/unit/jobs/test_tick.py \
	tests/unit/tenancy/test_scope_required.py tests/unit/credits/test_allowance_and_estimate.py \
	tests/integration/test_cookie_flags.py tests/integration/test_pkce_device.py \
	tests/unit/llm/test_routing_completeness.py tests/unit/llm/test_bedrock_fallback.py \
	tests/unit/llm/test_transmit_stream.py tests/unit/llm/test_json_schema.py \
	tests/unit/guardrails/test_corpus.py tests/unit/guardrails/test_fence.py \
	tests/unit/infra/test_stage6_surfaces.py \
	tests/unit/idea/test_case_a_and_b.py \
	tests/unit/research/test_categorize_and_board.py \
	tests/unit/planning/test_compile_and_scope.py \
	tests/unit/sandbox/test_build_landing_infra.py \
	tests/unit/product/test_stages_15_19.py \
	tests/unit/product/test_stages_20_23.py \
	tests/chaos/test_worker_kill.py \
	tests/unit/test_warden_policy.py tests/unit/test_warden_paths.py \
	tests/integration/test_warden_boundary.py tests/integration/test_warden_faildown.py \
	tests/unit/test_llm_routing.py tests/unit/test_reasoning_roundtrip.py tests/unit/test_tool_args.py \
	tests/unit/test_sole_llm_provider.py \
	tests/integration/test_gateway_auth.py tests/integration/test_sse_resume.py tests/integration/test_no_secret_egress.py \
	tests/unit/test_stage_machine.py tests/unit/test_complexity.py tests/unit/test_question_budget.py \
	tests/unit/test_question_invariants.py tests/unit/test_other_verbatim.py tests/unit/test_charter_provenance.py \
	tests/integration/test_charter_lock.py tests/integration/test_intake.py tests/integration/test_clarify_round2.py \
	tests/unit/test_composer.py tests/integration/test_agent_creation_speed.py tests/integration/test_agent_runtime.py \
	tests/integration/test_atomic_org_swap.py \
	tests/unit/test_detect.py tests/unit/test_no_credential_read.py tests/unit/test_child_env.py \
	tests/unit/test_child_env_scrubs_company_keys.py \
	tests/unit/test_cursor_argv_includes_trust_and_sandbox.py \
	tests/unit/test_select_infra_failure_falls_through.py \
	tests/unit/test_select_openhands_builder.py \
	tests/integration/test_openhands_builder_adapter_roundtrip.py \
	tests/unit/builder/test_builder_api.py \
	tests/unit/builder/test_llm_config.py \
	tests/unit/builder/test_event_map.py \
	tests/unit/builder/test_cleanup.py \
	tests/unit/preview/test_preview.py \
	tests/unit/test_desktop_data_paths.py \
	tests/unit/test_desktop_bundle_no_llm_keys.py \
	tests/unit/test_hosted_llm_auth.py \
	tests/unit/test_sole_hosted_default.py \
	tests/unit/test_landing_copy_lock.py \
	tests/unit/test_credit_lease_local_halt.py \
	tests/unit/test_consumer_identity.py \
	tests/unit/test_consumer_trial_expiry.py \
	tests/unit/test_founder_analytics_acl.py \
	tests/unit/test_desktop_update_policy.py \
	tests/unit/test_forced_update_halts_hosted.py \
	tests/unit/test_desktop_version.py tests/unit/test_agent_cursor_events.py \
	tests/unit/test_desktop_update_go_live.py \
	tests/unit/test_bug_reports.py \
	tests/unit/test_founder_go_live_doc.py \
	tests/unit/test_db_url_resolution.py \
	tests/unit/test_session_subject_is_real_user_id.py \
	tests/unit/test_adapters.py tests/integration/test_no_op_detection.py \
	tests/integration/test_consent_persists_across_restart.py \
	tests/integration/test_dispatcher_creates_worktree.py \
	tests/integration/test_signup_login_password_reset_flow.py \
	tests/integration/test_build_step_prefers_byo_falls_back_to_stub.py \
	tests/integration/test_local_llm_provider_health_probe.py \
	tests/redteam/test_inhouse_hosted_never_runs_byo_cli_on_our_infra.py \
	tests/unit/test_component_budget.py tests/integration/test_adr_before_code.py \
	tests/integration/test_dispatch.py tests/integration/test_scaffold.py tests/integration/test_gates.py \
	tests/unit/test_directions.py tests/integration/test_concierge_chat.py \
	tests/unit/test_channel_manifests.py tests/integration/test_dry_run_default.py \
	tests/unit/test_campaign_hypothesis.py tests/integration/test_gtm_prereqs.py \
	tests/unit/test_capability_floor.py tests/unit/test_fitness_registry.py \
	tests/unit/test_worldmodel_layers.py tests/unit/test_intervals.py tests/unit/test_sim_cost.py \
	tests/integration/test_healthy_cycle_is_free.py tests/unit/test_proposal_shape.py \
	tests/unit/test_archive_dedup.py tests/unit/test_peer_review_author.py \
	tests/integration/test_validation_chain.py tests/integration/test_rollback_all_kinds.py \
	tests/integration/test_auto_rollback.py tests/integration/test_cycle_budget.py \
	tests/integration/test_night_cost.py \
	tests/redteam/test_injection.py tests/redteam/test_drift.py \
	tests/redteam/test_sandbox_escape.py tests/redteam/test_secret_exfiltration.py \
	tests/redteam/test_rubber_stamp.py \
	tests/unit/test_narration.py tests/unit/test_onboarding_orchestrator.py \
	tests/acceptance/test_a8_guided_onboarding.py \
	tests/acceptance/test_a9_auto_mode.py \
	tests/unit/firstlook/test_serve_git_isolation.py \
	tests/unit/gtm/test_loop1_spine.py \
	tests/unit/test_outreach_discover_retired.py \
	tests/unit/test_outreach_first_five_retired.py \
	tests/unit/test_outreach_sweep_denied.py \
	tests/unit/test_outreach_no_linkedin_send.py \
	tests/unit/test_outreach_require_workspace.py \
	tests/unit/test_outreach_api_key_failclosed.py \
	tests/unit/test_outreach_stripe_webhook.py \
	tests/unit/test_outreach_isolation_idor.py \
	tests/unit/test_ai_gtm_status_workspace.py \
	tests/unit/test_outreach_browser_denylist.py \
	tests/unit/test_outreach_geo_gate.py \
	tests/unit/test_outreach_gates.py \
	tests/unit/test_outreach_score.py \
	tests/unit/test_outreach_network_distance.py \
	tests/unit/test_outreach_refuse_personalize.py \
	tests/unit/test_outreach_harvest_quarantine.py \
	tests/unit/test_outreach_ladder.py \
	tests/unit/test_outreach_honesty.py \
	tests/unit/test_outreach_gtm_get.py \
	tests/unit/test_outreach_ingest.py \
	tests/unit/test_outreach_service.py \
	tests/unit/test_outreach_api.py \
	tests/unit/test_outreach_icp_seed.py \
	tests/unit/test_outreach_icp_per_workspace.py \
	tests/unit/test_outreach_onboarding.py \
	tests/unit/test_outreach_labelled_examples.py \
	tests/unit/test_outreach_isolation_list.py \
	tests/unit/test_outreach_isolation_touch_child.py \
	tests/unit/test_outreach_isolation_composio_user.py \
	tests/unit/test_outreach_funnel.py \
	tests/unit/test_outreach_audit.py \
	tests/unit/test_outreach_aup_gate.py \
	tests/unit/test_outreach_cognito_membership.py \
	tests/unit/test_outreach_abuse.py \
	tests/unit/test_outreach_unsubscribe_token.py \
	tests/unit/test_outreach_dpa_gate.py \
	tests/unit/test_outreach_gmail_isolation.py \
	tests/unit/test_outreach_network_edges.py \
	tests/unit/test_outreach_evidence.py \
	tests/unit/test_outreach_assets.py \
	tests/unit/test_outreach_interviews.py \
	tests/unit/test_outreach_call_15.py \
	tests/unit/test_outreach_cadence.py \
	tests/unit/test_outreach_pilots.py \
	tests/unit/test_outreach_retention.py \
	tests/unit/test_outreach_layer_a.py \
	tests/unit/test_gtm_god.py \
	tests/unit/test_gtm_god_api.py \
	tests/unit/marketing/test_product_demo_recorder.py \
	tests/unit/marketing/test_ads_automation.py

P0_TESTS := $(COS_TESTS)
P1_WARDEN_TESTS :=

help:
	@echo "Targets:"
	@echo "  install / setup  Install Python + Node deps (incl. Company OS)"
	@echo "  doctor           Check your local desktop setup without showing keys"
	@if [ -f scripts/check_llm_keys.py ]; then echo "  llm-check        Probe Anthropic / Foundry DeepSeek / Groq / Gemini keys"; fi
	@echo "  seed             Ingest Amazon Reviews subset and generate tasks"
	@echo "  up               docker compose up --build"
	@echo "  down             docker compose down"
	@echo "  verify           lint + typecheck + Company OS P0 tests (excludes canary/)"
	@echo "  test             Company OS P0 unit + RL package tests"
	@echo "  lint typecheck   Ruff / mypy"
	@echo "  run              Start the local development stack"
	@echo "  migrate          Alembic upgrade head (company_os)"
	@echo "  types            Generate TypeScript types (stub until P1)"
	@echo "  acceptance       Company OS acceptance scenarios"
	@echo "  redteam          Company OS safety suite"
	@echo "  company-os-dev   Open your local app (legacy stack in the private checkout)"
	@echo "  desktop-dev      Open your local desktop (same as ./start)"
	@echo "  desktop-build    Build your local installer (same as ./start --build)"
	@if [ -f scripts/desktop/prepare_release.py ]; then echo "  desktop-build-hosted  Build the legacy hosted Mac app"; fi
	@if [ -f services/qm/package.json ]; then printf '%s\n' \
	  '  qm-install       npm install for vendored QM (services/qm)' \
	  '  qm-dev           Start QM core + worker on :3500 (optional sidecar)' \
	  '  qm-dev-stop      Stop QM core + worker' \
	  '  qm-ensure-crons  Register Company OS ops cadence crons on running QM'; fi
ifneq ($(wildcard scripts/llm_adapter_dev.py),)
	@echo "  llm-adapter-dev  Anthropic façade → LLM_* (Foundry/DeepSeek) on :3600"
	@echo "  llm-adapter-stop Stop the LLM adapter"
endif
	@if [ -d canary/services/environment ]; then printf '%s\n' \
	  '  oracle           Deterministic oracle browser smoke (10 tasks)' \
	  '  agent-smoke      Foundry-powered agent on 10 tasks (needs LLM_* env)'; fi
	@if [ -f vendor/skills/MANIFEST.yaml ]; then echo "  skills-fetch     Fetch pinned external skill packs (private checkout)"; fi
	@echo "  skills-ingest    Normalize installed packs into SQLite"
	@if [ -f scripts/sync_vendors.sh ]; then printf '%s\n' \
	  '  sync-vendors     Clone/update vendor engines (agent-reach, gpt-researcher, openworker, …)' \
	  '  openworker-server  Launch vendored OpenWorker agent server (:8765)'; fi
	@echo "  record-demo      Record a product URL to MP4 (Playwright+ffmpeg; optional Recordly ingest)"

ifneq ($(wildcard scripts/sync_vendors.sh),)
.PHONY: sync-vendors openworker-server
sync-vendors:
	bash scripts/sync_vendors.sh

openworker-server:
	bash scripts/openworker_server.sh
endif

worker:
	APP_ROLE=worker PYTHONPATH=services/company_os $(PY) -m app.worker.main

tick:
	@test -f docker-compose.companyos.yml || { echo 'Your local worker schedules tasks automatically. Start your app with ./start.'; exit 1; }
	@secret="$${COS_TICK_HMAC_SECRET:-$${COS_INTERNAL_ASSERTION_SECRET}}"; \
	body='{"tick":"5m"}'; \
	sig=$$($(PY) -c "import hashlib,hmac,os; print(hmac.new(os.environ.get('COS_TICK_HMAC_SECRET', os.environ.get('COS_INTERNAL_ASSERTION_SECRET','')).encode(), b'$(BODY)', hashlib.sha256).hexdigest())" 2>/dev/null || true); \
	BODY="$$body" COS_TICK_HMAC_SECRET="$$secret" $(PY) -c "import hashlib,hmac,os,urllib.request; b=os.environ['BODY'].encode(); s=hmac.new(os.environ['COS_TICK_HMAC_SECRET'].encode(), b, hashlib.sha256).hexdigest(); req=urllib.request.Request('http://127.0.0.1:3300/internal/tick', data=b, method='POST', headers={'Content-Type':'application/json','X-Tick-Signature':s}); print(urllib.request.urlopen(req).read().decode())"

# Product demo: Playwright walkthrough of URL → MP4 in data/marketing_media.
# Examples:
#   make record-demo URL=http://127.0.0.1:3100/content
#   make record-demo URL=http://127.0.0.1:3100/media HEADED=1
#   make record-demo INGEST="$$HOME/Downloads/demo.mp4"
#   make record-demo LAUNCH_RECORDLY=1
record-demo:
	$(PY) scripts/record_product_demo.py \
		--url "$(if $(URL),$(URL),http://127.0.0.1:3100/content)" \
		$(if $(HEADED),--headed,) \
		$(if $(INGEST),--ingest "$(INGEST)",) \
		$(if $(LAUNCH_RECORDLY),--launch-recordly,)

install setup:
	$(PYTHON) -m venv $(VENV)
	$(PIP) install -U pip
	$(PIP) install -e "packages/core" -e "packages/llm_gateway" -e "packages/guardrails" -e "packages/coding_agents" -e "packages/connectors" \
		-e "packages/desktop_runtime" -e "packages/preview" \
		-e "services/company_os[dev]" \
		$(foreach path,$(wildcard packages/harness/pyproject.toml),-e "$(dir $(path))") \
		$(foreach path,$(wildcard services/warden/pyproject.toml services/gateway/pyproject.toml services/builder/pyproject.toml),-e "$(dir $(path))[dev]") \
		$(foreach path,$(wildcard services/llm_adapter/pyproject.toml),-e "$(dir $(path))[dev]")
	$(PIP) install alembic aiosqlite ruff mypy pytest pytest-asyncio pyyaml httpx jsonschema argon2-cffi itsdangerous cryptography pyinstaller
	cd apps/console && $(PNPM) install || true
	cd apps/desktop && $(PNPM) install || true
	@if [ -d apps/marketing ]; then cd apps/marketing && $(PNPM) install; fi

seed:
	$(PY) scripts/ingest_amazon_reviews.py
	$(PY) scripts/generate_tasks.py

skills-fetch:
	@test -f vendor/skills/MANIFEST.yaml || { echo 'The pinned skills manifest is not in this export. Use your private checkout to fetch those packs.'; exit 1; }
	$(PY) scripts/skills_fetch.py

skills-ingest:
	PYTHONPATH=services/company_os $(PY) -m app.skills.ingest

up:
	docker compose up --build -d

down:
	docker compose down

logs:
	docker compose logs -f --tail=200

doctor:
	@test -x "$(PY)" || (echo "Your local Python is missing. Run make setup, then make doctor."; exit 1)
	@$(PY) scripts/doctor.py $(if $(DATA_DIR),--data-dir "$(DATA_DIR)")

ifneq ($(wildcard scripts/check_llm_keys.py),)
.PHONY: llm-check
llm-check:
	$(PY) scripts/check_llm_keys.py
endif

migrate:
	cd services/company_os && COMPANY_OS_DB_URL=$${COMPANY_OS_DB_URL:-sqlite:///../../data/company_os.db} \
		../../$(VENV)/bin/alembic upgrade head

types:
	$(PY) scripts/gen_types.py || echo "types generator not ready yet (P1)"

acceptance:
	$(PY) -m pytest -q tests/acceptance -m acceptance

redteam:
	$(PY) -m pytest -q tests/redteam

test lint run: export SUPYMEM_MODE = local
test: export GUARDRAILS_MODE = deterministic_only
test: export LLM_OFFLINE = 1
test: export LLM_CASSETTE_MODE = replay
test: export PYTHONPATH := $(CURDIR)/packages:$(CURDIR)/services/company_os

run: company-os-dev

company-os-dev:
	@if test -f docker-compose.companyos.yml; then $(PY) scripts/dev.py; else ./start; fi

# Desktop dogfood: Tauri window + supervisor. Attaches to an already-running
# `make company-os-dev` stack when ports are healthy; otherwise starts sidecars.
desktop-dev:
	./start

desktop-build:
	./start --build

desktop-runtime:
	./start --build

desktop-build-fast desktop-build-review: desktop-build

.PHONY: require-hosted-desktop require-hosted-compose
require-hosted-desktop:
	@test -f scripts/desktop/build_runtime.py && test -f scripts/desktop/prepare_release.py || { echo 'Your public source does not include the hosted desktop build. Build your local installer with ./start --build.'; exit 1; }

require-hosted-compose:
	@test -f docker-compose.companyos.yml || { echo 'Start your local services with docker compose up --build.'; exit 1; }

desktop-dev-hosted: require-hosted-desktop
	@test -x "$(VENV)/bin/python" || (echo "Run make setup first"; exit 1)
	@mkdir -p src-tauri/binaries
	@TRIPLE=$$(rustc -vV 2>/dev/null | awk '/host:/{print $$2}'); \
	 STUB="src-tauri/binaries/companyos-runtime-$$TRIPLE"; \
	 if [ ! -x "$$STUB" ]; then \
	   printf '%s\n' '#!/bin/sh' \
	     'ROOT="$$(cd "$$(dirname "$$0")/../.." && pwd)"' \
	     'exec "$$ROOT/.venv/bin/python" -m desktop_runtime "$$@"' > "$$STUB"; \
	   chmod +x "$$STUB"; \
	   echo "[desktop-dev] wrote stub $$STUB"; \
	 fi
	@# The mascot page is served from :3117 (the :3100 console server has no /swing).
	@curl -sf -o /dev/null http://127.0.0.1:3117/swing/index.html || \
	  { (cd src-tauri/splashscreen && $(PYTHON) -m http.server 3117 --bind 127.0.0.1 >/dev/null 2>&1 &); sleep 1; }
	pnpm --dir apps/desktop install --frozen-lockfile 2>/dev/null || pnpm --dir apps/desktop install
	cd src-tauri && ../apps/desktop/node_modules/.bin/tauri dev

# Build a hosted Mac app with an embedded native bridge, never a repo/Python stub.
desktop-runtime-hosted: require-hosted-desktop
	@test -x "$(VENV)/bin/python" || (echo "Run make setup first"; exit 1)
	$(PY) scripts/desktop/build_runtime.py

desktop-build-hosted: desktop-runtime-hosted
	$(PNPM) --dir apps/desktop install --frozen-lockfile
	$(PY) scripts/desktop/prepare_release.py --version "$$( $(PY) -c 'import json; print(json.load(open("src-tauri/tauri.conf.json"))["version"])' )"
	cd src-tauri && ../apps/desktop/node_modules/.bin/tauri build --config tauri.release.json --bundles app,dmg
	$(PY) scripts/desktop/collect_macos.py

# Fast production-backed dogfood loop. Reuses the frozen browser runtime and
# Rust cache, builds only the app bundle, and deliberately skips DMG/updater
# artifacts and Apple notarization. APPLE_SIGNING_IDENTITY may name an installed
# Developer ID identity so macOS keeps the same team identity during replacement.
.PHONY: desktop-build-fast
desktop-build-fast-hosted: require-hosted-desktop
	$(PY) scripts/desktop/build_runtime.py --if-stale
	$(PNPM) --dir apps/desktop install --frozen-lockfile
	$(PY) scripts/desktop/prepare_fast_local.py
	cd src-tauri && ../apps/desktop/node_modules/.bin/tauri build --config tauri.release.json --bundles app

# Use the bundled splash documents and a real .app identity for native UI/CUA
# acceptance. Never create an updater archive, DMG or release collection here.
.PHONY: desktop-build-review
desktop-build-review-hosted: require-hosted-desktop
	$(PY) scripts/desktop/build_runtime.py --if-stale
	$(PNPM) --dir apps/desktop install --frozen-lockfile
	$(PY) scripts/desktop/prepare_local_review.py
	cd src-tauri && ../apps/desktop/node_modules/.bin/tauri build --config tauri.local-review.json --bundles app

desktop-build-staging-hosted:
	SUPYMEM_DESKTOP_CHANNEL=staging $(MAKE) desktop-build-hosted


# Updater JSON contract + policy. Does not Apple-sign a DMG (impossible on Linux).
DESKTOP_UPDATE_TESTS := \
	tests/unit/test_desktop_update_policy.py \
	tests/unit/test_forced_update_halts_hosted.py \
	tests/unit/test_desktop_version.py tests/unit/test_agent_cursor_events.py \
	tests/unit/test_desktop_update_go_live.py

desktop-update-test:
	@test -x "$(PY)" || (echo "Run make setup first (needs .venv)"; exit 1)
	$(PY) -m pytest -q $(DESKTOP_UPDATE_TESTS)
	$(PY) scripts/desktop_update_dryrun.py --old-version 0.1.0 --new-version 0.2.0

ifneq ($(wildcard services/qm/package.json),)
qm-install:
	cd services/qm && npm install

qm-dev:
	$(PYTHON) scripts/qm_dev.py up

qm-dev-stop:
	$(PYTHON) scripts/qm_dev.py stop

qm-ensure-crons:
	$(PY) scripts/qm_ensure_crons.py
else
qm-install qm-dev qm-dev-stop qm-ensure-crons:
	@echo 'The optional QM sidecar is not in this export. Run ./start to open Supymem Local.' >&2; exit 2
endif

ifneq ($(wildcard scripts/llm_adapter_dev.py),)
.PHONY: llm-adapter-dev llm-adapter-stop
llm-adapter-dev:
	$(PYTHON) scripts/llm_adapter_dev.py up

llm-adapter-stop:
	$(PYTHON) scripts/llm_adapter_dev.py stop
endif

test:
	$(PY) -m pytest -q -m 'not hosted' $(if $(wildcard .github/workflows/ci.yml),$(COS_TESTS),$(wildcard $(COS_TESTS)))

test-cov:
	$(PY) -m pytest -q tests/unit/product tests/unit/guardrails packages/core \
		--cov=packages/core --cov=packages/desktop_runtime --cov-fail-under=0

chaos:
	$(PY) -m pytest -q tests/chaos

load-k6:
	@command -v k6 >/dev/null && k6 run tests/load/ai_proxy.js || echo "k6 not installed; skipped"

canary-test:
	@test -d canary/services/environment/tests && test -d canary/apps/web || { echo 'The canary suite is not in this export. Run make test for the local unit suite.'; exit 1; }
	-PYTHONPATH=canary/services/environment $(PY) -m pytest -q canary/services/environment/tests packages/agent_runner/tests canary/packages/browsergym_shopping/tests
	-cd canary/apps/web && $(PNPM) test
	-$(PY) scripts/openenv_smoke.py || true

adaptive-demo:
	$(PY) scripts/adaptive_e2e_demo.py

lint:
	$(VENV)/bin/ruff check packages/core services/company_os/app/db services/company_os/app/core scripts/doctor.py
	-$(VENV)/bin/ruff check services packages scripts --exclude canary

typecheck:
	$(VENV)/bin/mypy --strict packages/core

ifneq ($(wildcard canary/services/environment),)
.PHONY: oracle agent-smoke
oracle:
	$(PY) -m agent_runner.cli oracle --limit 10

agent-smoke:
	$(PY) -m agent_runner.cli agent --limit 10
endif

verify: lint typecheck test
	@echo "verify: lint + typecheck + pytest OK"
	cd apps/console && $(PNPM) run typecheck
	cd apps/console && $(PNPM) test
	@echo "Run 'make company-os-dev' for the founder stack."

# ---------------------------------------------------------------------------
# Local Docker Compose stack (P1). Does not touch the RL shopping compose file.
# ---------------------------------------------------------------------------
COMPOSE_COS := docker compose $(if $(wildcard .env.local),--env-file .env.local) -f docker-compose.companyos.yml
# One-shot migrate/minio_init exit 0; --wait on the whole project treats that as failure.
COMPOSE_COS_WAIT := postgres redis minio warden company_os worker gateway console builder ai-proxy

.PHONY: local-build local-up local-down local-logs local-ps local-seed local-reset \
	local-smoke local-test local-shell
local-build local-up local-down local-logs local-ps local-seed local-reset local-smoke local-test local-shell: require-hosted-compose

# Build the Company OS images (warden, company_os, gateway, console, migrate).
local-build:
	$(COMPOSE_COS) build

# Start postgres/redis/minio + migrate + the four app services.
local-up:
	$(COMPOSE_COS) up -d
	$(COMPOSE_COS) up -d --wait --wait-timeout 180 $(COMPOSE_COS_WAIT)

local-down:
	$(COMPOSE_COS) down

local-logs:
	$(COMPOSE_COS) logs -f --tail=200

local-ps:
	$(COMPOSE_COS) ps

# Idempotent demo tenant inside the running company_os container.
local-seed:
	$(COMPOSE_COS) exec -T company_os python /workspace/scripts/seed_local.py

# Wipe named volumes and bring the stack back with a fresh seed.
local-reset:
	$(COMPOSE_COS) down -v
	$(COMPOSE_COS) up -d
	$(COMPOSE_COS) up -d --wait --wait-timeout 180 $(COMPOSE_COS_WAIT)
	$(MAKE) local-seed

local-smoke:
	GATEWAY_URL=http://127.0.0.1:$${GATEWAY_PORT:-3200} \
	CONSOLE_URL=http://127.0.0.1:$${CONSOLE_PORT:-3100} \
	bash scripts/local_smoke.sh

smoke:
	@test -f scripts/smoke.py || { echo 'The hosted smoke script is not in this export. Build your local app and use scripts/desktop/smoke_local_stack.py (see docs/self-hosting/DESKTOP.md).'; exit 1; }
	ENV=$(ENV) $(PY) scripts/smoke.py --env $${ENV:-local} --require

# Full pytest suite inside a company_os image with test extras. Mounts host tests/.
local-test:
	docker build -f services/company_os/Dockerfile --target test -t companyos-local-test .
	docker run --rm \
	  --network companyos_default \
	  -e COMPANY_OS_DATA_DIR=/tmp/cos-data \
	  -e COMPANY_OS_DB_URL=sqlite+aiosqlite:////tmp/cos-data/company_os.db \
	  -e LLM_OFFLINE=1 \
	  -e APP_ENV=local \
	  -v "$(CURDIR)/tests:/workspace/tests:ro" \
	  companyos-local-test \
	  python -m pytest /workspace/tests -q

local-shell:
	$(COMPOSE_COS) exec company_os sh
