#!/usr/bin/env bash
set -euo pipefail

if [[ $# != 1 ]]; then
  echo 'Usage: bash scripts/make_public_tree.sh /path/to/new-or-empty-public-directory' >&2
  exit 2
fi

root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
python3 -B - "$root" "$1" <<'PY'
import json
import os
from math import hypot
from pathlib import Path
import re
import shutil
from struct import pack
import subprocess
import sys
import zlib

root = Path(sys.argv[1])
git_env = {name: value for name, value in os.environ.items() if not name.startswith("GIT_")}
try:
    toplevel = subprocess.check_output(["git", "-C", str(root), "rev-parse", "--show-toplevel"],
                                      env=git_env, stderr=subprocess.DEVNULL)
    if not toplevel or Path(os.fsdecode(toplevel.removesuffix(b"\n"))).resolve() != root:
        raise ValueError("Source is not a checkout root")
except (OSError, RuntimeError, ValueError, subprocess.CalledProcessError):
    print("Run scripts/make_public_tree.sh from your tracked checkout, not an extracted archive, then retry.", file=sys.stderr)
    sys.exit(2)

sys.path.insert(0, str(root / "scripts"))
try:
    from check_public import artwork_paths, private_artifact, private_sample
except ImportError:
    print("Restore scripts/check_public.py and use Python 3.11 or later, then rerun the export.", file=sys.stderr)
    sys.exit(2)

requested = Path(sys.argv[2]).absolute()
try:
    destination = requested.resolve()
    scratch = root / ".tmp"
    inside = requested.is_relative_to(root) or destination.is_relative_to(root)
    ignored_scratch = (requested == destination and destination != scratch and destination.is_relative_to(scratch)
                       and subprocess.run(["git", "-C", str(root), "check-ignore", "--quiet", "--",
                                           str(destination.relative_to(root))], env=git_env, capture_output=True).returncode == 0)
    if requested.is_symlink() or (inside and not ignored_scratch):
        print("Choose a directory outside your checkout or under its Git-ignored .tmp directory, without symlinks, and rerun the export.", file=sys.stderr)
        sys.exit(2)
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        print("Choose a new or empty directory and rerun the export. Your existing files were not changed.", file=sys.stderr)
        sys.exit(2)
except (OSError, RuntimeError):
    print("Cannot inspect your destination. Choose an accessible new or empty directory and rerun the export.", file=sys.stderr)
    sys.exit(2)

directories = (
    "services/", "packages/", "apps/console/", "src-tauri/", "tests/",
    "docs/self-hosting/", "docs/site/",
    "config/concierge_catalog/", "config/connectors/", "config/firstlook/",
    "config/genomes/", "config/narration.d/", "config/worldmodel/",
    "third_party/gstack/",
    "scripts/analytics/", "scripts/bench/", "scripts/composio/", "scripts/desktop/",
    "scripts/email/", "scripts/evals/research_prior_art/", "scripts/gtm/",
    "scripts/intake/", "scripts/memory/", "scripts/pagebuilder/",
)
files = set("""
README.md LICENSE NOTICE CONTRIBUTING.md SECURITY.md CODE_OF_CONDUCT.md CHANGELOG.md start start.ps1
docs/RELEASING.md
THIRD_PARTY_INVENTORY.md src-tauri/patches/mac-usernotifications/PATCH.md
src-tauri/patches/mac-usernotifications/LICENSE-APACHE
.env.example .env.local.example .dockerignore .gitignore .gitattributes .gitleaks.toml pytest.ini Makefile
docker-compose.yml docker-compose.local.yml docker/entrypoint.sh docker/python-healthcheck.py
.github/workflows/public-ci.yml
docs/site/index.html docs/site/styles.css docs/site/charlie.js
docs/site/assets/mascot-player.js
.github/dependabot.yml
.github/ISSUE_TEMPLATE/bug_report.md .github/ISSUE_TEMPLATE/feature_request.md
.github/ISSUE_TEMPLATE/self_hosting_help.md .github/PULL_REQUEST_TEMPLATE.md
.github/ISSUE_TEMPLATE/release_checklist.md
apps/desktop/package.json apps/desktop/pnpm-lock.yaml
scripts/make_public_tree.sh scripts/check_public.sh scripts/check_public.py
tests/unit/infra/test_make_public_tree.py
tests/unit/chat/test_plan_gtm_and_summary_routing.py tests/unit/chat/test_customer_experiment_regressions.py
scripts/qa/eval_reachable_leads.py
scripts/adaptive_e2e_demo.py scripts/check_no_shell_true.py
scripts/check_provider_builder_controls.py scripts/ci_budget_guard.py
scripts/ci_install_playwright.py scripts/ci_pytest_shard.py
scripts/content_smoke.py scripts/desktop_update_dryrun.py scripts/desktop_version.py
scripts/dev.py scripts/doctor.py scripts/eval_chat_router.py scripts/eval_chat_steering.py
scripts/gen_types.py scripts/generate_chat_contract.py scripts/generate_tasks.py scripts/ingest_amazon_reviews.py
scripts/local_acceptance.py scripts/local_ci.sh scripts/local_smoke.sh scripts/marketing_smoke.py
scripts/local_value_journey.py scripts/local_page_signup_journey.py scripts/local_browser_journey.py scripts/local_gate.sh
scripts/local_mail_journey.py
scripts/local_build_journey.py
scripts/local_journeys/enterprise_health.json scripts/local_journeys/consumer_fintech.json
scripts/openenv_smoke.py scripts/outreach/seed_test_contact.py
scripts/record_product_demo.py
scripts/run_local.ps1 scripts/run_local.sh scripts/seed_fixtures.py scripts/seed_local.py
scripts/skills_fetch.py
config/agent_catalog.yaml config/ai_proxy_models.yaml config/autonomy_profiles.yaml
config/blockers.yaml config/boundary.yaml config/browser_cuai_profile.json config/capabilities.yaml
config/channel_manifests.yaml config/copy_lint.yaml config/credit_meters.yaml config/deployment.example.json
config/estimation.yaml config/fitness_registry.yaml config/gate.yaml config/gtm_icp.yaml
config/guardrails.yaml config/hosting.yaml config/hosts.json config/idealab_banned.yaml config/infra_tiers.yaml
config/job_kinds.yaml config/llm_pricing.yaml config/memory.yaml config/model_routing.yaml config/money.yaml
config/narration.yaml config/plan_templates.yaml config/platform_capabilities.yaml config/policy.baseline.yaml
config/pricing.yaml config/providers.yaml config/secret_slots.yaml config/trigger_policy.yaml config/will_playbook.yaml
services/company_os/app/planning/diagnostics.py
scripts/desktop/vendor/cua-modal-0.29.1/LICENSE.upstream.md
scripts/desktop/vendor/cua-modal-0.29.1/exact-modal.patch scripts/desktop/vendor/cua-modal-0.29.1/manifest.json
""".split())

excluded_directories = (
    "services/gateway/", "services/warden/", "services/ai_proxy/", "services/builder/",
    "services/agent_server/", "services/egress_proxy/", "services/sandbox/", "services/llm_adapter/",
    "apps/console/src/app/admin/", "apps/console/src/app/auth/", "apps/console/src/app/invite/",
    "apps/console/src/app/welcome/", "apps/console/src/app/download/", "apps/console/src/app/lab/diagrams/",
    "apps/console/src/app/pagebuilder-lab/", "apps/console/src/components/admin/",
    "tests/unit/admin/", "tests/unit/payments/", "tests/qa_runner/", "tests/qm/",
    "scripts/bench/results/",
    "docs/site/",
    "services/company_os/data/", "services/qm/",
    "apps/console/public/charlie/", "apps/console/public/connections/", "apps/console/public/toolkit/",
    "apps/console/public/marketing/", "apps/console/public/mac-beta/",
    "apps/console/public/email/", "apps/console/public/guide/", "apps/console/public/venture-cards/",
    "src-tauri/binaries/", "src-tauri/resources/console/", "src-tauri/resources/local/", "src-tauri/icons/",
    "services/company_os/app/gbrain/", "packages/harness/",
)
excluded_files = {
    "services/company_os/app/hosted_routes.py",
    "services/company_os/app/build/subscription_product_diagnostic_policy.py",
    "services/company_os/app/build/subscription_product_diagnostic_inputs.py",
    "services/company_os/app/build/subscription_product_diagnostic_admission.py",
    "services/company_os/app/build/subscription_product_diagnostic_controller.py",
    "services/company_os/app/build/subscription_product_diagnostic_provider.py",
    "services/company_os/app/build/coding_source_qa_provider.py",
    "services/company_os/app/build/native_product_private_review.py",
    "services/company_os/app/build/native_product_source_staging.py",
    "services/company_os/app/build/public_product_diagnostic_policy.py",
    *(f"services/company_os/app/provisioning/{name}.py" for name in (
        "cognito_auth", "database_controller", "fargate_database", "founder_image", "founder_migration",
        "founder_service", "postgres_apps", "run_database", "run_private_qa_database",
    )),
    *(f"services/company_os/app/api/{name}.py" for name in (
        "consumer_api", "hosted_llm_api", "beta_api", "build_runtime_api", "build_intent_api",
        "internal_ops", "internal_tick", "diagram_lab_api", "payments_api", "hermes_api", "openai_api",
    )),
    *(f"services/company_os/app/admin/{name}.py" for name in (
        "router", "internal", "server_diagnostics", "diagram_lab", "catalog", "grants", "sessions",
    )),
    *(f"services/company_os/app/payments/{name}.py" for name in (
        "base", "connect", "razorpay", "registry", "service", "stripe", "stub",
    )),
    "services/company_os/app/money/checkout.py", "services/company_os/app/money/stripe_client.py",
    "services/company_os/app/email/webhooks.py", "services/company_os/app/db/app_key_models.py",
    "services/company_os/app/hermes/bootstrap.py", "apps/console/src/components/desktop/InstallLocalBuilder.tsx",
    "apps/console/src/lib/adminApi.ts", "apps/console/src/lib/adminSessionRequestGate.ts",
    "apps/console/src/lib/adminSessionRequestGate.test.ts", "apps/console/src/lib/payments.ts",
    "apps/console/src/components/credits/TopUpCard.tsx", "apps/console/src/components/credits/TopUpCard.module.css",
    "apps/console/src/components/credits/ReferralCard.tsx", "apps/console/src/components/credits/ReferralCard.test.cjs",
    "apps/console/src/components/credits/CompanyCreditSummary.test.cjs",
    "apps/console/src/lib/creditRequestReceipt.test.ts",
    "packages/desktop_runtime/companyos-runtime.spec", "packages/desktop_runtime/frozen_main.py",
    "scripts/desktop/prepare_release.py",
    "scripts/desktop/prepare_staging_update.py", "scripts/desktop/smoke_runtime.py",
    "scripts/email/preview_templates.py", "scripts/email/send_test.py", "scripts/email/verify_gallery.py",
    "tests/browser/test_founder_live_journeys.py",
    "tests/unit/test_founder_live_journey_harness.py",
    "tests/unit/product/test_local_product_journey.py", "tests/unit/infra/test_static_qa_image_contract.py",
    "tests/builder_harness.py", "tests/gateway_harness.py", "tests/warden_harness.py",
    "tests/integration/test_scaffold_auth.py",
    *(f"tests/integration/test_{name}.py" for name in (  # need hosted services/builder
        "managed_artifact_export", "managed_openhands_sdk", "managed_process_api",
        "postgres_final_sdk_receipts", "postgres_managed_sdk",
    )),
    *(f"tests/unit/provisioning/test_{name}.py" for name in (
        "cognito_auth", "fargate_database", "founder_image", "founder_migration", "founder_service",
        "founder_runtime_worker", "database_controller", "database_effect_reconciliation",
    )),
    *(f"tests/unit/builder/test_{name}.py" for name in (
        "artifact_sync", "builder_api", "cleanup", "egress_policy", "event_map", "llm_config",
        "product_visual_diagnostics", "sandbox_policy",
    )),
    "tests/unit/test_gateway_cors.py", "tests/unit/test_ci_image_plan.py",
    "tests/unit/test_desktop_build_channels.py", "tests/unit/test_staging_update_artifacts.py",
    "tests/unit/test_warden_policy.py", "tests/unit/test_warden_paths.py",
    "tests/unit/email/test_callback_retry.py", "tests/unit/email/test_delivery_safety.py",
    "tests/unit/test_email_go_live.py", "tests/unit/ops/test_finance_webhook.py",
    "tests/security/test_openai_proxy_financial_capture.py",
    "tests/unit/product/test_hermes_platform_credits.py",
    *(f"tests/unit/product/test_{name}.py" for name in (
        "offline_product_bootstrap", "product_diagnostic_launch", "product_diagnostic_policy",
        "product_qa_host", "subscription_product_custody", "coding_source_qa_provider",
        "public_product_diagnostic_policy", "public_product_outer_runner",
    )),
    *(f"tests/integration/test_postgres_{name}.py" for name in (
        "app_databases", "native_private_review", "native_source_staging",
        "product_diagnostic_lifecycle",
    )),
    "tests/unit/infra/test_ai_proxy_completion_contract.py", "tests/unit/infra/test_builder_sdk_layer_contract.py",
    "tests/unit/infra/test_frozen_publisher_contract.py", "tests/unit/infra/test_managed_runtime_image_contract.py",
    "tests/unit/infra/test_supply_chain_contract.py", "tests/unit/infra/test_image_workflow_cache_contract.py",
    "apps/console/public/supymem-logo.svg", "apps/console/public/staging-placeholder.html",
    "apps/console/src/app/icon.svg", "src-tauri/splashscreen/logo.png",
    "src-tauri/splashscreen/charlie-mark.png", "src-tauri/splashscreen/swing/mascot_animations.glb",
    "src-tauri/splashscreen/swing/poster.png", "src-tauri/splashscreen/swing/poster.svg",
    "docs/site/assets/poster.png", "docs/site/assets/mascot_animations.glb",
    "docs/site/assets/demo.mp4", "docs/site/assets/demo.gif", "docs/site/assets/demo-poster.png",
    "services/company_os/app/knowledge/startup_pmf.json.gz",
    "services/company_os/app/knowledge/yc_video_advice.json.gz",
    "scripts/evals/research_prior_art/README.md",
    "tests/unit/infra/test_deploy_core.py", "tests/unit/infra/test_bootstrap_staging.py",
    "tests/unit/infra/test_migrate_staging.py", "tests/unit/infra/test_run_staging_bootstrap.py",
    "tests/unit/infra/test_scale_staging_services.py", "tests/unit/infra/test_staging_proof.py",
    "tests/unit/infra/test_start_frozen_image_build.py", "tests/unit/infra/test_tag_frozen_images_for_production.py",
    "tests/unit/infra/test_zero_downtime_deploy.py", "tests/unit/test_console_digest_deploy.py",
    "tests/unit/test_gateway_digest_deploy.py", "tests/unit/test_scale_profile.py",
    "tests/unit/test_founder_go_live_doc.py",
    "tests/unit/infra/test_ci_base_images.py", "tests/unit/infra/test_verified_hotfix_dispatch.py",
    "tests/unit/test_landing_copy_lock.py", "tests/unit/suggestions/test_suggestion_outreach_review.py",
    "tests/unit/chat/test_negative_mac_task_list.py", "tests/unit/research/test_founder_presentation.py",
    "tests/unit/infra/test_wallet_cutover_workflow.py",
    "tests/unit/infra/test_release_batch.py", "tests/unit/infra/test_staging_parity.py",
    "tests/unit/infra/test_staging_smoke.py", "tests/unit/test_harness_loop.py",
    "tests/unit/infra/test_ci_release_guard.py",
    "tests/unit/test_cu_browser_installed_acceptance.py", "tests/unit/test_cu_browser_qualification_controller.py",
    "tests/unit/test_cu_qualification_payment_stop.py",
    "tests/unit/test_gateway_credential_epoch.py", "tests/unit/test_gateway_restart.py",
    "tests/unit/test_gateway_session_recovery.py",
    "tests/security/test_gateway_retry_boundary.py", "tests/security/test_admin_ui_diagnostics.py",
    "tests/redteam/test_attestation_gates_autonomy.py", "tests/redteam/test_drift.py", "tests/redteam/test_sandbox_escape.py",
    *(f"tests/integration/test_{name}.py" for name in (
        "agent_runtime", "scaffold_migrations", "staging_bootstrap_fargate",
        "postgres_payment_readiness", "signup_password_policy",
    )),
}
private_parts = {
    ".git", ".agents", ".codex", ".aws", ".claude", "node_modules", "__pycache__",
    ".pytest_cache", ".next", "target", "docs", "adrs", "audits", "evidence", "plans",
    "handoff", "backups", "vendor", "data",
}
private_names = {"AGENTS.md", "CLAUDE.md", ".gitleaksignore"}
omitted_package_scripts = {
    "apps/desktop/package.json": {"dev:hosted", "build:hosted"},
}


def local_console_config(text):
    text = re.sub(r"(?m)^const local = .*\n  \|\| .*\n", "", text)
    text = re.sub(r"(?m)^const (?:gateway(?:Socket)?Origin|hostedConnect) = .*\n", "", text)
    text = text.replace("${hostedConnect}", "")
    text = re.sub(r'''(?m)^  `form-action 'self'.*,$''', '''  "form-action 'self'",''', text)
    text = re.sub(r"(?m)^  `report-uri .*,$", '  "report-uri /api/csp-report",', text)
    return re.sub(r'NEXT_PUBLIC_SUPYMEM_MODE: local \? .*? \},', 'NEXT_PUBLIC_SUPYMEM_MODE: "local" },', text)


def local_dependabot(text):
    directories = {"/services/*": "/services/company_os"}
    omitted = {"/config/scaffold", "/infra/bootstrap/staging", "/apps/marketing", "/canary/apps/web"}
    return "".join(line.replace(path, directories.get(path, path)) for line in text.splitlines(keepends=True)
                   if (path := line.removeprefix("      - ").strip()) not in omitted)


def included(name):
    if name not in files and not name.startswith(directories):
        return False
    if (name.startswith(("services/qa_runner/", "services/product_qa/"))
            and name != "services/qa_runner/requirements.lock"):
        return False
    if name in excluded_files or name.startswith(excluded_directories):
        return False
    path = Path(name)
    if path.is_absolute() or ".." in path.parts or path.name in private_names or private_artifact(path):
        return False
    approved = name == "docs/RELEASING.md" or name.startswith(("docs/self-hosting/", "docs/site/"))
    parts = path.parts[2:] if approved else path.parts
    if any(part in private_parts for part in parts) and not name.startswith("scripts/desktop/vendor/cua-modal-0.29.1/"):
        return False
    if path.name.startswith(".env") and path.name != ".env.example" and name != ".env.local.example":
        return False
    if path.suffix == ".dylib":
        return False
    if path.suffix == ".md" and not approved and name not in files:
        return path.name in {"README.md", "LICENSE.md", "LICENSE.upstream.md", "NOTICE.md", "PROVENANCE.md"} or name.startswith("third_party/gstack/")
    return True


def rgba_png(width, height, pixels):
    def chunk(kind, data):
        return pack(">I", len(data)) + kind + data + pack(">I", zlib.crc32(kind + data))

    stride = width * 4
    rows = b"".join(b"\0" + pixels[start:start + stride] for start in range(0, len(pixels), stride))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


def public_desktop_assets():
    def glyph(size, color):
        offsets = [max(0, abs(pixel + 0.5 - size / 2) - size * 0.22) for pixel in range(size)]
        return b"".join(bytes((*color, round(255 * max(0, min(1, size * 0.16 + 0.5 - hypot(x, y))))))
                        for y in offsets for x in offsets)

    icons = {size: rgba_png(size, size, glyph(size, (30, 59, 84))) for size in (32, 64, 128, 256, 512)}
    icns = b"".join(pack(">4sI", kind, len(icons[size]) + 8) + icons[size] for kind, size in
                    zip((b"icp5", b"icp6", b"ic07", b"ic08", b"ic09"), icons))
    ico = pack("<HHHBBBBHHII", 0, 1, 1, 0, 0, 0, 0, 1, 32, len(icons[256]), 22) + icons[256]
    return {
        "32x32.png": icons[32], "64x64.png": icons[64], "128x128.png": icons[128],
        "128x128@2x.png": icons[256], "icon.png": icons[512], "icon.ico": ico,
        "icon.icns": pack(">4sI", b"icns", len(icns) + 8) + icns,
        "tray-template@2x.rgba": glyph(44, (0, 0, 0)),
    }


def public_console_artwork(source):
    palettes = ((194, 222, 242), (195, 228, 201), (237, 202, 217), (250, 219, 187),
                (212, 205, 239), (192, 229, 226), (241, 225, 182), (209, 217, 240))
    cards = "venture-cards/${art}.png"
    if (source / "components/ventures/ventureCards.ts").is_file():
        for index, path in enumerate(sorted(artwork_paths(source, cards))):
            color = palettes[index % len(palettes)]
            width, height = 192, 112
            pixels = bytes(channel for row in range(height) for column in range(width)
                           for channel in (*[min(255, value + 6 * ((column + row * (index + 1)) // 24 % 4))
                                             for value in color], 255))
            yield path, rgba_png(width, height, pixels)
    icon = (b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 36 36">'
            b'<rect width="36" height="36" rx="9" fill="#e9f1f7"/>'
            b'<path d="M10 18h16m-8-8v16" stroke="#1e3b54" stroke-width="2" stroke-linecap="round"/></svg>\n')
    for file, template in (("components/connections/ConnectionsHub.tsx", "connections/${CONNECTION_LOGOS[id]}.svg"),
                           ("components/office/CompanyHome.tsx", "toolkit/${id}.svg")):
        if (source / file).is_file():
            for path in sorted(artwork_paths(source, template)):
                yield path, icon


try:
    result = subprocess.run(["git", "-C", str(root), "ls-files", "-z", "--cached"],
                            env=git_env, capture_output=True, check=True)
    names = {os.fsdecode(name) for name in result.stdout.split(b"\0") if name}
    names.update(name for name in files if (root / name).is_file())
    selected = []
    for name in sorted(names):
        if not included(name):
            continue
        source = root / name
        if source.is_symlink() or any(parent.is_symlink() for parent in source.parents if parent != root):
            print(f"Excluded symlink: {name}. Review its target before any separate export.", file=sys.stderr)
            continue
        if not source.exists() and private_sample(source.name.replace("_", " ")):
            continue
        if not source.is_file():
            raise OSError("Missing tracked source")
        selected.append(name)
    required = {"README.md", "LICENSE", "NOTICE", "CONTRIBUTING.md", "SECURITY.md", "CODE_OF_CONDUCT.md", "CHANGELOG.md",
                ".env.example", "Makefile", ".gitleaks.toml", "scripts/check_public.sh", "scripts/check_public.py",
                ".github/workflows/public-ci.yml", ".github/ISSUE_TEMPLATE/bug_report.md",
                ".github/ISSUE_TEMPLATE/release_checklist.md",
                ".github/ISSUE_TEMPLATE/feature_request.md", ".github/ISSUE_TEMPLATE/self_hosting_help.md",
                ".github/PULL_REQUEST_TEMPLATE.md", "apps/desktop/package.json", "apps/desktop/pnpm-lock.yaml",
                "services/company_os/app/main.py", "apps/console/src/app/setup/LocalSetup.tsx", "src-tauri/Cargo.toml"}
    if not required.issubset(selected):
        print("Required release files are missing. Restore them in your checkout and rerun the export.", file=sys.stderr)
        sys.exit(2)
    destination.mkdir(parents=True, exist_ok=True)
    for name in selected:
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as exported, (root / name).open("rb") as source:
            if name == "config/hosts.json":
                hosts = json.load(source)
                if not isinstance(hosts, dict) or not isinstance(hosts.get("local"), dict):
                    raise ValueError("Missing local host configuration")
                exported.write((json.dumps({"local": hosts["local"]}, indent=2) + "\n").encode())
            elif name in omitted_package_scripts:
                package = json.load(source)
                if not isinstance(package, dict) or not isinstance(package.get("scripts", {}), dict):
                    raise ValueError("Invalid package scripts")
                for script in omitted_package_scripts[name]:
                    package.get("scripts", {}).pop(script, None)
                exported.write((json.dumps(package, indent=2, ensure_ascii=False) + "\n").encode())
            elif name == ".github/dependabot.yml":
                exported.write(local_dependabot(source.read().decode()).encode())
            elif name == "apps/console/next.config.ts":
                exported.write(local_console_config(source.read().decode()).encode())
            elif name == "src-tauri/src/channel.rs":
                ports = {"app": 3100, "gateway": 3200}
                exported.write(re.sub(rb"https://(app|gateway)(?:\.[a-z]+)?\.supymem\.com",
                                      lambda match: f"http://127.0.0.1:{ports[match[1].decode()]}".encode(),
                                      source.read()))
            elif name in {"src-tauri/src/native_security.rs", "src-tauri/src/auth.rs", "src-tauri/src/lib.rs",
                          "apps/console/src/lib/desktopVentureLink.ts", "apps/console/src/lib/desktopVentureLink.test.ts",
                          "apps/console/src/components/desktop/DesktopProductBoundary.test.cjs"}:
                exported.write(re.sub(rb"((?:app(?:\.staging)?|venture-preview)\.)supymem\.com"
                                      rb"|supymem\.com(?=/downloads/Supymem\.dmg)",
                                      rb"\1example.invalid", source.read()))
            else:
                shutil.copyfileobj(source, exported)
        shutil.copystat(root / name, target)
    # Keep the configured paths while replacing private artwork and leaving build payloads for the build hook.
    generated = {f"src-tauri/icons/{name}": data for name, data in public_desktop_assets().items()}
    generated["apps/console/public/charlie/charlie-mark.png"] = generated["src-tauri/icons/64x64.png"]
    generated["apps/console/public/supymem-logo.svg"] = (
        b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
        b'<rect x="4" y="4" width="24" height="24" rx="5" fill="#1e3b54"/></svg>\n'
    )
    generated.update((f"apps/console/public/{path}", data)
                     for path, data in public_console_artwork(destination / "apps/console/src"))
    generated.update({f"src-tauri/resources/{name}/.gitkeep": b"" for name in ("console", "local")})
    for name, data in generated.items():
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as exported:
            exported.write(data)
    selected.extend(generated)
    size = sum((destination / name).stat().st_size for name in selected)
    print(f"Public tree: {destination}; {len(selected)} files; {size} bytes.", flush=True)
except (OSError, ValueError, subprocess.CalledProcessError):
    print("Export failed. Check Git, source permissions and your destination, then rerun into a new empty directory.", file=sys.stderr)
    sys.exit(2)
sys.exit(subprocess.run(["bash", str(destination / "scripts/check_public.sh"), str(destination)]).returncode)
PY
