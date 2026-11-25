import ast
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import sysconfig
from tempfile import TemporaryDirectory
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
CORE_RELEASE_FILES = ("services/company_os/app/main.py", "apps/console/src/app/setup/LocalSetup.tsx", "src-tauri/Cargo.toml")


@pytest.mark.parametrize("entrypoint", ["app/main.py", "app/worker/main.py"])
def test_docker_installs_first_party_entrypoint_imports(entrypoint):
    service = ROOT / "services/company_os"
    packages = ROOT / "packages"
    dockerfile = (service / "Dockerfile").read_text()
    tree = ast.parse((service / entrypoint).read_text())
    imports = {alias.name.partition(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import)
               for alias in node.names}
    imports.update(node.module.partition(".")[0] for node in ast.walk(tree)
                   if isinstance(node, ast.ImportFrom) and not node.level and node.module)
    first_party = {name for name in imports if (packages / name / "pyproject.toml").is_file()}
    assert first_party
    install = re.search(r"(?m)^    /venv/bin/pip install .* --target /firstparty.*(?:\n[^\n]*\\)*", dockerfile)
    assert install
    assert first_party <= set(re.findall(r"/src/packages/(\w+)\b", install[0]))
    assert first_party <= set(re.findall(r"(?m)^COPY packages/(\w+) packages/\1$", dockerfile))
    ignored = (ROOT / ".dockerignore").read_text().splitlines()
    assert not {f"packages/{name}/" for name in first_party}.intersection(ignored)


@pytest.mark.parametrize("domain,marker", [("wholesale.example", "beta-" "tester"), ("gardening.example", "beta " "feedback")])
def test_release_guard_rejects_private_attribution(tmp_path, monkeypatch, domain, marker):
    from hashlib import sha256

    from scripts import check_public

    scanner = tmp_path / "gitleaks"
    scanner.write_text('#!/bin/sh\nwhile [ "$#" -gt 0 ]; do\n'
                       'if [ "$1" = --report-path ]; then printf "[]" > "$2"; exit; fi\nshift\ndone\n')
    scanner.chmod(0o755)
    tree = tmp_path / domain
    tree.mkdir()
    (tree / ".gitleaks.toml").write_text("rules = []\n")
    fixture = tree / "sample.txt"
    fixture.write_text(f"Reported by a {marker} in {domain}\n")
    command = ["bash", str(ROOT / "scripts/check_public.sh"), str(tree)]
    environment = {**os.environ, "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
                   "PUBLIC_CHECK_PYTHON": sys.executable}
    result = subprocess.run(command, env=environment, capture_output=True, text=True)
    assert result.returncode == 1 and "public-private-sample: sample.txt:1" in result.stdout
    fixture.write_text(f"Fictional regression scenario for {domain}\n")
    assert subprocess.run(command, env=environment, capture_output=True).returncode == 0
    filename = f"{marker.replace(' ', '_')}.txt"
    fixture.rename(tree / filename)
    result = subprocess.run(command, env=environment, capture_output=True, text=True)
    assert result.returncode == 1 and f"public-private-sample: {filename}:1" in result.stdout
    alias = f"{domain.partition('.')[0]} ledger"
    monkeypatch.setattr(check_public, "_PRIVATE_SAMPLE_DIGESTS", {2: {sha256(alias.encode()).hexdigest()}})
    assert check_public.scan_file(fixture.with_name(filename), alias.replace(" ", "_") + ".txt", {"rules": []})
    fixture.write_text(f"Saved quote: {alias.swapcase()}.")
    assert check_public.scan_file(fixture, fixture.name, {"rules": []}) == [(fixture.name, 1, "public-private-sample")]


@pytest.mark.parametrize("domain", ["wholesale.example", "gardening.example"])
def test_public_tree_gate_checks_imports_and_assets(tmp_path, domain):
    tree = tmp_path / domain
    test_file = tree / "tests/unit/test_fixture.py"
    test_file.parent.mkdir(parents=True)
    source = tree / "apps/console/src/fixture.tsx"
    source.parent.mkdir(parents=True)
    asset = tree / "apps/console/public" / f"{domain}.svg"
    asset.parent.mkdir()
    source.write_text(f'export const picture = <img src="/{asset.name}" alt="" />;\n')
    command = ["bash", str(ROOT / "scripts/check_public.sh"), "--check-tree", str(tree)]
    environment = {**os.environ, "PUBLIC_CHECK_PYTHON": sys.executable}

    def check():
        return subprocess.run(command, env=environment, capture_output=True, text=True)

    test_file.write_text("from scripts.infra import omitted\n\ndef test_example(): pass\n")
    result = check()
    assert result.returncode and "test_fixture.py" in result.stdout + result.stderr
    asset.write_text("<svg/>")
    test_file.write_text("def test_example():\n    from scripts.infra import omitted\n")
    result = check()
    assert result.returncode and "scripts.infra" in result.stdout + result.stderr
    fixture = tree / "tests/conftest.py"
    fixture.write_text("import pytest\n\n@pytest.fixture\ndef private_input():\n    from scripts.infra import omitted\n")
    test_file.write_text("def test_example(private_input): pass\n")
    result = check()
    assert result.returncode and "tests/conftest.py:5: scripts.infra" in result.stdout + result.stderr
    test_file.write_text("import pytest\n\n@pytest.mark.hosted\ndef test_private(private_input): pass\n"
                         "\ndef test_portable(): pass\n")
    result = check()
    assert result.returncode == 0, result.stdout + result.stderr
    test_file.write_text("def test_example():\n    raise AssertionError('must not run test bodies')\n")
    asset.unlink()
    result = check()
    assert result.returncode and asset.name in result.stdout + result.stderr
    asset.write_text("<svg/>")
    result = check()
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(("domain", "entrypoint", "mode"), [
    ("wholesale.example", "main.py", "local"),
    ("gardening.example", "worker/main.py", "hosted"),
])
def test_public_tree_gate_checks_app_entrypoints(tmp_path, domain, entrypoint, mode):
    tree = tmp_path / domain
    app = tree / "services/company_os/app"
    (app / "worker").mkdir(parents=True)
    (app / "main.py").touch()
    (app / "worker/main.py").touch()
    broken = app / entrypoint
    broken.write_text(f"import os\nif os.environ.get('SUPYMEM_MODE', 'hosted') == {mode!r}:\n    import app.omitted\n")
    command = [sys.executable, str(ROOT / "scripts/check_public.py"), "--check-tree", str(tree)]
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode != 0
    assert mode in result.stderr and "No module named 'app.omitted'" in result.stderr

    broken.write_text("")
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("domain,nested", [("wholesale.example", True), ("gardening.example", False)])
@pytest.mark.skipif(not (ROOT / ".git").exists(), reason="Run the archive reproduction from a Git checkout.")
def test_export_rejects_git_archives_without_their_own_checkout(tmp_path, domain, nested):
    scratch = ROOT / ".tmp" if nested else tmp_path
    scratch.mkdir(exist_ok=True)
    with TemporaryDirectory(prefix=f"{domain}-", dir=scratch) as temporary:
        source = Path(temporary) / "archive"
        source.mkdir()
        tracked = ("README.md", "LICENSE", "NOTICE", "CONTRIBUTING.md", "SECURITY.md", "CODE_OF_CONDUCT.md", "CHANGELOG.md",
                   ".env.example", "Makefile", ".gitleaks.toml", ".github/workflows/public-ci.yml", ".github/ISSUE_TEMPLATE",
                   ".github/PULL_REQUEST_TEMPLATE.md", "apps/desktop/package.json", "apps/desktop/pnpm-lock.yaml",
                   "scripts/make_public_tree.sh", "scripts/check_public.sh", "scripts/check_public.py")
        archive = subprocess.check_output(["git", "-C", str(ROOT), "archive", "HEAD", *tracked])
        subprocess.run(["tar", "-x", "-C", str(source)], input=archive, check=True)
        shutil.copyfile(ROOT / "scripts/make_public_tree.sh", source / "scripts/make_public_tree.sh")
        destination = Path(temporary) / "public"
        result = subprocess.run(["bash", "scripts/make_public_tree.sh", str(destination)], cwd=source,
                                capture_output=True, text=True)
        assert result.returncode == 2, result.stdout + result.stderr
        assert "tracked checkout" in result.stderr
        assert not destination.exists()


@pytest.mark.parametrize("domain", ["wholesale.example", "gardening.example"])
@pytest.mark.skipif(not (ROOT / ".git").exists(), reason="Run the export acceptance test from a Git checkout.")
@pytest.mark.skipif(shutil.which("gitleaks") is None, reason="Install Gitleaks to run the export acceptance test.")
def test_real_public_export_passes_release_gate(tmp_path, domain):
    destination = tmp_path / domain
    result = subprocess.run(["bash", str(ROOT / "scripts/make_public_tree.sh"), str(destination)],
                            env={**os.environ, "PUBLIC_CHECK_PYTHON": sys.executable}, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Public release scan passed: 0 findings" in result.stdout
    workflows = destination / ".github/workflows"
    assert sorted(path.name for path in workflows.iterdir()) == ["public-ci.yml"]
    assert yaml.safe_load((workflows / "public-ci.yml").read_text())["jobs"]
    dependabot = yaml.safe_load((destination / ".github/dependabot.yml").read_text())["updates"]
    assert {update["package-ecosystem"]: update["directories"] for update in dependabot if "directories" in update} == {
        "pip": ["/services/company_os", "/packages/*"], "docker": ["/services/company_os", "/apps/console"],
        "npm": ["/apps/console", "/apps/desktop"],
    }
    assert {path.name for path in (destination / "scripts/qa").iterdir()} == {"eval_reachable_leads.py"}
    assert not (destination / "tests/unit/chat" / "test_beta_" "feedback_regressions.py").exists()
    assert (destination / "tests/unit/chat/test_plan_gtm_and_summary_routing.py").is_file()
    assert (destination / "tests/unit/chat/test_customer_experiment_regressions.py").is_file()
    channel = (destination / "src-tauri/src/channel.rs").read_text()
    assert ".supymem.com" not in channel and "http://127.0.0.1:3100" in channel and "http://127.0.0.1:3200" in channel
    for name in ("src-tauri/src/native_security.rs", "apps/console/src/lib/desktopVentureLink.ts",
                 "apps/console/src/lib/desktopVentureLink.test.ts",
                 "apps/console/src/components/desktop/DesktopProductBoundary.test.cjs"):
        exported = (destination / name).read_text()
        assert "app.example.invalid" in exported and "supymem.com" not in exported, name
    for name in ("src-tauri/src/auth.rs", "src-tauri/src/lib.rs"):
        assert "app.supymem.com" not in (destination / name).read_text(), name
    assert '"127.0.0.1" | "localhost" | "[::1]"' in (destination / "src-tauri/src/native_security.rs").read_text()
    console_config = (destination / "apps/console/next.config.ts").read_text()
    assert ".supymem.com" not in console_config and "report-uri /api/csp-report" in console_config
    assert 'NEXT_PUBLIC_SUPYMEM_MODE: "local"' in console_config
    ci_inputs = (
        *CORE_RELEASE_FILES,
        "services/company_os/requirements.runtime.lock", "services/company_os/requirements.build.lock",
        "packages/desktop_runtime/requirements.local.lock", "services/company_os/pyproject.toml",
        "packages/core/pyproject.toml", "packages/coding_agents/pyproject.toml", "packages/huginn_watch/pyproject.toml",
        "packages/llm_gateway/pyproject.toml", "packages/guardrails/pyproject.toml", "packages/connectors/pyproject.toml",
        "packages/desktop_runtime/pyproject.toml", "scripts/ci_install_playwright.py", "scripts/ci_pytest_shard.py",
        "scripts/generate_chat_contract.py", "apps/console/src/test-fixtures/chat-contract/messages.json",
        "scripts/local_value_journey.py", "scripts/local_build_journey.py", "scripts/desktop/offline_value.py",
        "scripts/desktop/build_runtime.py",
        "scripts/local_journeys/enterprise_health.json", "scripts/local_journeys/consumer_fintech.json",
        "tests/loopback_model.py", "tests/unit/pagebuilder/fixtures/local_tool_pages/1.json",
        "tests/unit/pagebuilder/fixtures/local_tool_pages/2.json",
        "pytest.ini", "tests/unit/infra/test_make_public_tree.py", "apps/console/pnpm-lock.yaml",
        "apps/console/scripts/test.cjs", "apps/console/tsconfig.json", "src-tauri/Cargo.lock",
        "docker-compose.yml", "docker-compose.local.yml",
    )
    assert not (missing := [name for name in ci_inputs if not (destination / name).is_file()]), missing
    console_scripts = json.loads((destination / "apps/console/package.json").read_text())["scripts"]
    assert console_scripts["test"] == "node scripts/test.cjs"
    assert console_scripts["typecheck"] == "tsc --noEmit"
    desktop_scripts = json.loads((destination / "apps/desktop/package.json").read_text())["scripts"]
    assert desktop_scripts["build"] == "../../start --build"
    assert not {"dev:hosted", "build:hosted"} & desktop_scripts.keys()
    assert not (destination / "services/qm").exists()
    assert not (destination / "tests/qm").exists()
    if shutil.which("make"):
        help_text = subprocess.check_output(["make", "help"], cwd=destination, text=True)
        assert not {"llm-check", "oracle", "agent-smoke", "sync-vendors", "openworker-server"} & set(help_text.split())
    for private in (
        "scripts/check_llm_keys.py", "scripts/sync_vendors.sh", "scripts/openworker_server.sh",
        "scripts/desktop/cu_browser_installed_acceptance.py", "scripts/desktop/cu_browser_qualification_controller.py",
        "tests/unit/test_cu_browser_installed_acceptance.py", "tests/unit/test_cu_browser_qualification_controller.py",
        "scripts/ci_release_guard.py", "tests/unit/infra/test_ci_release_guard.py",
        "scripts/llm_adapter_dev.py", "services/llm_adapter",
    ):
        assert not (destination / private).exists(), private
    artwork = destination / "apps/console/public"
    cards = [path.read_bytes() for path in (artwork / "venture-cards").glob("*.png")]
    assert len(cards) == len(set(cards)) == 8
    assert all(content.startswith(b"\x89PNG\r\n\x1a\n") for content in cards)
    assert (artwork / "connections/gmail.svg").read_bytes().startswith(b"<svg ")
    for path in destination.rglob("*"):
        assert not {"data-backups", "vault"}.intersection(path.relative_to(destination).parts)
        assert not path.name.endswith(".tar.gz") and ".sqlite" not in path.name

    def check(command, **kwargs):
        result = subprocess.run(command, cwd=destination, capture_output=True, text=True, timeout=60, **kwargs)
        assert result.returncode == 0, result.stdout + result.stderr

    check(["bash", "-n", "start"])
    site_packages = sorted({sysconfig.get_path(kind) for kind in ("purelib", "platlib")})
    check([sys.executable, "-B", "-S", "-c", """
from pathlib import Path
import sys
sys.path.extend(sys.argv[1:-1])
from app import cli_local, main
from app.email.providers import current_provider, log_only
from app.worker import main as worker
from core.local_security import owner_secret_path
from fastapi.testclient import TestClient
for module in (cli_local, main, worker):
    assert Path(module.__file__).resolve().is_relative_to(Path.cwd())
routes = main.app.openapi()['paths']
assert {'/entitlement', '/email/preferences', '/public/email/unsubscribe', '/public/unsubscribe'} <= routes.keys()
assert not {'/beta/referrals', '/internal/tick', '/internal/builds/{build_id}/intent/prepare',
            '/internal/builds/{build_id}/model-invocations', '/api/v1/finance/stripe/webhook',
            '/webhooks/email/resend', '/webhooks/email/ses', '/openai/v1/chat/completions',
            '/ventures/{venture_id}/hermes/bootstrap'} & routes.keys()
assert current_provider() is log_only
with TestClient(main.app, base_url="http://127.0.0.1", client=("127.0.0.1", 40000)) as client:
    client.headers["X-Local-Proxy-Secret"] = owner_secret_path().read_text().strip()
    client.get("/health").raise_for_status()
    venture = client.post("/ventures", json={"name": sys.argv[-1], "start": False})
    venture.raise_for_status()
    chat = client.get(f"/ventures/{venture.json()['id']}/chat")
    chat.raise_for_status()
    assert isinstance(chat.json()["items"], list)
""", *site_packages, domain], env={
        "PATH": os.defpath, "HOME": str(tmp_path), "SUPYMEM_MODE": "local",
        "LLM_OFFLINE": "1", "GUARDRAILS_MODE": "deterministic_only", "EMAIL_PROVIDER": "resend",
        "COMPANY_OS_DATA_DIR": str(tmp_path / "app-data"),
        "PYTHONPATH": os.pathsep.join(str(destination / name) for name in ("packages", "services/company_os")),
    })

    tauri = destination / "src-tauri"
    paths = []
    for source in tauri.rglob("*.rs"):
        paths.extend(source.parent / path for path in re.findall(r'include_bytes!\(\s*"([^"]+)"', source.read_text()))
    for config in ("tauri.conf.json", "tauri.local.json"):
        settings = json.loads((tauri / config).read_text())
        bundle = settings["bundle"]
        paths.extend(tauri / path for path in (*bundle.get("icon", ()), *bundle.get("resources", ())))
        if frontend := settings["build"].get("frontendDist"):
            paths.append(tauri / frontend)
        for target in bundle.get("linux", {}).values():
            paths.extend(tauri / path for path in target.get("files", {}).values())
            if template := target.get("desktopTemplate"):
                paths.append(tauri / template)
    assert not (missing := sorted({str(path.resolve().relative_to(destination)) for path in paths if not path.exists()})), missing
    assert (tauri / "icons/tray-template@2x.rgba").stat().st_size == 44 * 44 * 4

    cargo = Path.home() / ".cargo/bin/cargo"
    if cargo.is_file():
        check([str(cargo), "metadata", "--offline", "--no-deps", "--format-version", "1",
               "--manifest-path", "src-tauri/Cargo.toml"])


@pytest.mark.parametrize("domain", ["wholesale.example", "gardening.example"])
def test_snapshot_scan_blocks_private_artifacts(tmp_path, domain):
    tree = tmp_path / domain
    tree.mkdir()
    shutil.copyfile(ROOT / ".gitleaks.toml", tree / ".gitleaks.toml")
    artifacts = ("services/data-backups", "services/demo/vault", "services/demo/backup.tar.gz",
                 "services/demo/store.sqlite3-journal", "services/demo/master.key", "docs/archive",
                 "docs/supymem", "scripts/infra", "services/demo/qa-evidence", "config/cognito.env",
                 "services/demo/cloud-inventory.json", ".github/workflows/deploy-staging.yml", "infra", "config/scaffold",
                 "tests/unit/infra/test_auth_custom_domain.py",
                 "services/qm/cli/templates/aws", "services/qm/plugins/admin/public")
    for name in artifacts:
        path = tree / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.suffix:
            path.write_text("Synthetic artifact\n")
        else:
            path.mkdir()
    (tree / "linked-file").symlink_to(tree / "missing-target")
    (tree / "config/hosts.json").write_text(json.dumps({"local": {}, "production": {"core": domain}}))
    result = subprocess.run([sys.executable, str(ROOT / "scripts/check_public.py"), str(tree)],
                            capture_output=True, text=True)
    assert result.returncode == 1
    assert set(result.stdout.splitlines()) == {
        *(f"public-private-artifact: {name}:1" for name in artifacts), "public-symlink: linked-file:1",
        "public-nonlocal-host-config: config/hosts.json:1",
    }


@pytest.mark.parametrize("domain", ["wholesale.example", "gardening.example"])
def test_snapshot_scan_requires_gitleaks_default_rules(tmp_path, domain):
    tree = tmp_path / "tree"
    tree.mkdir()
    shutil.copyfile(ROOT / ".gitleaks.toml", tree / ".gitleaks.toml")
    (tree / "input.py").write_text(f'contact = "owner@{domain}"\n')
    tools = tmp_path / "tools"
    tools.mkdir()
    command = [sys.executable, str(ROOT / "scripts/check_public.py"), str(tree)]
    environment = {**os.environ, "PATH": str(tools)}
    missing = subprocess.run(command, env=environment, capture_output=True, text=True, check=False)
    assert missing.returncode == 2
    assert "Install Gitleaks" in missing.stderr
    scanner = tools / "gitleaks"
    scanner.write_text(
        f"#!{sys.executable}\nimport json, sys\nfrom pathlib import Path\n"
        "assert '--redact=100' in sys.argv\n"
        "Path(sys.argv[sys.argv.index('--report-path') + 1]).write_text(json.dumps("
        "[{'File': 'input.py', 'StartLine': 1, 'RuleID': 'generic-api-key'}]))\n"
        "raise SystemExit(1)\n"
    )
    scanner.chmod(0o755)
    blocked = subprocess.run(command, env=environment, capture_output=True, text=True, check=False)
    assert blocked.returncode == 1
    assert "generic-api-key: input.py:1" in blocked.stdout


@pytest.mark.parametrize("domain", ["wholesale.example", "gardening.example"])
def test_gitleaks_document_and_fixture_paths_are_scoped(tmp_path, domain):
    import tomllib

    from scripts.check_public import _allowed, _gitleaks, scan_file

    config_path = ROOT / ".gitleaks.toml"
    config = tomllib.loads(config_path.read_text())
    document = next(rule for rule in config["rules"] if rule["id"] == "public-internal-document")
    assert "path" in document and "paths" not in document
    reviewed = [(rule, allowlist) for rule in config["rules"] for allowlist in rule.get("allowlists", [])
                if allowlist.get("description", "").startswith("Reviewed release-scan false positive:")]
    assert reviewed
    for rule, allowlist in reviewed:
        assert allowlist["condition"] == "AND"
        assert allowlist["regexTarget"] == "secret"
        assert all(pattern.startswith(r"\A") and pattern.endswith("$") for pattern in allowlist["paths"])
        assert all(pattern.startswith(r"\A") and pattern.endswith("$") for pattern in allowlist["regexes"])
        name = re.sub(r"\\(.)", r"\1", allowlist["paths"][0][2:-1])
        source_file = ROOT / name
        if not source_file.is_file():
            continue
        for pattern in allowlist["regexes"]:
            text = source_file.read_text()
            # gitleaks decodes \uXXXX escapes, so allowlisted values may only exist decoded
            match = re.search(pattern[2:-1], text) or re.search(pattern[2:-1], text.encode().decode("unicode_escape"))
            assert match is not None
            value = match.group()
            assert _allowed({"allowlists": [allowlist]}, name, value, "")
            assert not _allowed({"allowlists": [allowlist]}, name, value + domain, "")
            assert not _allowed({"allowlists": [allowlist]}, f"{domain}/{name}", value, "")
    source = tmp_path / "source"
    source.mkdir()
    input_path = source / "input.py"
    input_path.write_text(f'contact = "owner@{domain}"\n')
    assert scan_file(input_path, "services/input.py", config) == []
    assert scan_file(input_path, "docs/archive/input.md", config) == [
        ("docs/archive/input.md", 1, "public-internal-document")
    ]
    assert scan_file(input_path, "docs/RELEASING.md", config) == []
    assert scan_file(input_path, "docs/internal.md", config) == [
        ("docs/internal.md", 1, "public-internal-document")
    ]
    input_path.write_text("npm_" + "x" * 36)
    assert scan_file(input_path, "docs/RELEASING.md", config) == [
        ("docs/RELEASING.md", 1, "public-package-token")
    ]
    scanner = tmp_path / "gitleaks"
    scanner.write_text(
        f"#!{sys.executable}\nimport sys\nfrom pathlib import Path\n"
        "assert sys.argv[2] == '.'\n"
        "assert Path('input.py').is_file()\n"
        "Path(sys.argv[sys.argv.index('--report-path') + 1]).write_text('[]')\n"
    )
    scanner.chmod(0o755)
    assert _gitleaks(str(scanner), "dir", source, config_path, tmp_path / "report.json") == set()


@pytest.mark.parametrize("domain", ["wholesale.example", "gardening.example"])
@pytest.mark.skipif(not (ROOT / "docs/site").is_dir(), reason="Your public export excludes the site until its media rights are cleared.")
def test_project_site_assets_and_animation_controls(tmp_path, domain):
    site = ROOT / "docs/site"
    html = (site / "index.html").read_text()
    assert 'lang="en"' in html
    assert "—" not in html
    assert 'class="skip-link"' in html
    for resource in re.findall(r'(?:src|href|poster)="([^"]+)"', html):
        if resource.startswith("https://"):
            assert resource.startswith("https://github.com/contextplz/company_os_mvp")
        elif resource.startswith("#"):
            assert resource == "#" or f'id="{resource[1:]}"' in html
        else:
            assert (site / resource).is_file(), resource
    for name in ("poster.png", "mascot_animations.glb"):
        assert (site / "assets" / name).read_bytes() == (ROOT / "apps/console/public/charlie" / name).read_bytes()
    assert (site / "assets/mascot-player.js").read_bytes() == (ROOT / "apps/console/src/lib/mascotPlayer.js").read_bytes()
    assert '<video controls preload="none"' in html
    assert 'poster="assets/demo-poster.png"' in html
    assert 'src="assets/demo.mp4" type="video/mp4"' in html
    assert "sample mode with sample data" in html
    assert 'autoplay' not in html
    readme = (ROOT / "README.md").read_text()
    assert "docs/site/assets/" not in readme
    assert "apps/console/public/charlie/" not in readme
    for name, limit in (("demo.mp4", 8_000_000), ("demo.gif", 5_000_000)):
        assert 0 < (site / "assets" / name).stat().st_size <= limit
    assert (site / "assets/demo.mp4").read_bytes()[4:8] == b"ftyp"
    assert (site / "assets/demo.gif").read_bytes().startswith((b"GIF87a", b"GIF89a"))
    workflow_text = (ROOT / ".github/workflows/pages.yml").read_text()
    workflow = yaml.safe_load(workflow_text)
    assert "push" in workflow[True]
    assert workflow["permissions"] == {"contents": "read", "pages": "write", "id-token": "write"}
    deploy = workflow["jobs"]["deploy"]
    assert deploy["if"] == "github.ref == format('refs/heads/{0}', github.event.repository.default_branch)"
    assert deploy["runs-on"] == "ubuntu-latest"
    assert any(step.get("with", {}).get("path") == "docs/site" for step in deploy["steps"])
    assert not re.search(r"secrets\.|pull_request_target", workflow_text)
    harness = tmp_path / "animation.mjs"
    harness.write_text('''
import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";

const source = fs.readFileSync(process.argv[2], "utf8");
const origin = `https://${process.argv[3]}/project/`;
async function mount(reduced = false, broken = false) {
  const element = () => ({ hidden: true, listeners: {}, attributes: {},
    addEventListener(name, handler) { this.listeners[name] = handler; },
    setAttribute(name, value) { this.attributes[name] = value; } });
  const button = element(), canvas = element(), poster = element();
  poster.hidden = false;
  const motion = { ...element(), matches: reduced };
  const document = { ...element(), hidden: false,
    querySelector(name) { return { "#charlie-toggle": button, "#charlie-canvas": canvas, "#charlie-poster": poster }[name]; } };
  const calls = [];
  const player = { playing: false, disposed: false,
    play() { this.playing = true; }, pause() { this.playing = false; }, dispose() { this.disposed = true; } };
  const context = vm.createContext({ document, window: { matchMedia: () => motion }, URL });
  const dependency = new vm.SyntheticModule(["createMascotPlayer"], function () {
    this.setExport("createMascotPlayer", async (target, options) => {
      calls.push(options);
      assert.equal(target, canvas);
      if (broken) throw new Error("Unavailable");
      return player;
    });
  }, { context });
  await dependency.link(() => {});
  await dependency.evaluate();
  const module = new vm.SourceTextModule(source, { context,
    initializeImportMeta(meta) { meta.url = `${origin}charlie.js`; },
    importModuleDynamically: () => dependency });
  await module.link(() => {});
  await module.evaluate();
  return { button, canvas, poster, motion, document, calls, player };
}

const reduced = await mount(true);
assert.equal(reduced.button.hidden, true);
await reduced.button.listeners.click();
assert.equal(reduced.calls.length, 0);
assert.equal(reduced.poster.hidden, false);

const normal = await mount();
assert.equal(normal.button.hidden, false);
assert.equal(normal.calls.length, 0);
await normal.button.listeners.click();
assert.equal(normal.calls.length, 1);
assert.equal(normal.calls[0].src, `${origin}assets/mascot_animations.glb`);
assert.equal(normal.calls[0].clip, "Swing_Joy");
assert.equal(normal.calls[0].autoplay, false);
assert.equal(normal.calls[0].loop, true);
assert.equal(normal.player.playing, true);
assert.equal(normal.canvas.hidden, false);
assert.equal(normal.poster.hidden, true);
assert.equal(normal.button.attributes["aria-pressed"], "true");
await normal.button.listeners.click();
assert.equal(normal.player.playing, false);
assert.equal(normal.poster.hidden, false);
assert.equal(normal.button.attributes["aria-pressed"], "false");
await normal.button.listeners.click();
assert.equal(normal.calls.length, 1);
normal.document.hidden = true;
normal.document.listeners.visibilitychange();
assert.equal(normal.player.playing, false);
normal.document.hidden = false;
normal.document.listeners.visibilitychange();
assert.equal(normal.player.playing, true);
normal.motion.matches = true;
normal.motion.listeners.change();
assert.equal(normal.player.playing, false);
assert.equal(normal.poster.hidden, false);
assert.equal(normal.button.hidden, true);
normal.motion.matches = false;
normal.motion.listeners.change();
assert.equal(normal.button.hidden, false);
assert.equal(normal.player.playing, false);
let prevented = false;
normal.canvas.listeners.webglcontextlost({ preventDefault() { prevented = true; } });
assert.equal(prevented, true);
assert.equal(normal.player.disposed, true);
assert.equal(normal.poster.hidden, false);
assert.equal(normal.button.hidden, true);

const broken = await mount(false, true);
await broken.button.listeners.click();
assert.equal(broken.poster.hidden, false);
assert.equal(broken.canvas.hidden, true);
assert.equal(broken.button.hidden, true);
assert.equal(broken.button.disabled, false);
''')
    result = subprocess.run(["node", "--experimental-vm-modules", str(harness), str(site / "charlie.js"), domain],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("domain", ["wholesale.example", "gardening.example"])
@pytest.mark.parametrize(("retained", "excluded"), [
    pytest.param(("src-tauri/icons/tray-template@2x.rgba", "src-tauri/icons/icon.icns",
                  "src-tauri/icons/icon.ico", "src-tauri/icons/32x32.png"),
                 ("src-tauri/icons/supymem-mark.png", "src-tauri/icons/icon-1024.png"), id="desktop-icons"),
    pytest.param(("apps/console/public/charlie/charlie-mark.png", "apps/console/public/supymem-logo.svg"),
                 ("apps/console/public/staging-placeholder.html",), id="console-assets"),
    pytest.param(("THIRD_PARTY_INVENTORY.md", "src-tauri/patches/mac-usernotifications/LICENSE-APACHE",
                  "src-tauri/patches/mac-usernotifications/PATCH.md", "third_party/gstack/LICENSE",
                  "packages/core/research_web/LICENSE"),
                 ("services/company_os/app/gbrain/links.py", "packages/harness/loop.py",
                  ".agents/skills/gtm-cofounder/SKILL.md", "vendor/research/openleads/index.ts"),
                 id="third-party-provenance"),
    pytest.param((), ("data/knowledge/startup-pmf-2026-09-19/original.xlsx",
                     "data/knowledge/startup-pmf-2026-09-19/records.jsonl",
                     "data/knowledge/yc-video-advice-1dfdaf90/manifest.json",
                     "services/company_os/app/knowledge/startup_pmf.json.gz",
                     "services/company_os/app/knowledge/yc_video_advice.json.gz",
                     "apps/console/public/charlie/mascot_animations.glb",
                     "apps/console/public/charlie/poster.png",
                     "src-tauri/splashscreen/swing/mascot_animations.glb",
                     "src-tauri/splashscreen/swing/poster.png",
                     "src-tauri/splashscreen/swing/poster.svg",
                     "src-tauri/splashscreen/charlie-mark.png",
                     "docs/site/assets/mascot_animations.glb", "docs/site/assets/poster.png",
                     "docs/site/assets/demo.mp4", "docs/site/assets/demo.gif",
                     "docs/site/assets/demo-poster.png", "data/lead_library/purchased.csv"),
                 id="redistribution-rights"),
    pytest.param(("CHANGELOG.md", "docs/RELEASING.md", ".github/ISSUE_TEMPLATE/release_checklist.md"), (), id="release-documents"),
    pytest.param((), ("docs/supymem/qa-2026-10/B2B_OUTREACH_EVIDENCE.json",
                     "docs/supymem/qa-2026-10/CONSUMER_EVIDENCE_TRIAL.json",
                     "infra/terraform/envs/production/terraform.tfvars"), id="release-custody"),
    pytest.param((".github/workflows/public-ci.yml",), (".github/workflows/deploy-api.yml", ".github/workflows/ci.yml",
                     ".github/workflows/desktop-release.yml", ".github/workflows/desktop-local-release.yml",
                     ".github/workflows/native-dictation.yml", ".github/workflows/native-manifest.yml",
                     ".github/workflows/pages.yml"),
                 id="public-workflows"),
    pytest.param(("scripts/generate_chat_contract.py", "tests/unit/chat/test_chat_contract.py",
                  "apps/console/src/test-fixtures/chat-contract/messages.json"), (), id="chat-contract-inputs"),
    pytest.param(("scripts/email/prove_lifecycle.py",), ("scripts/email/preview_templates.py",
                  "scripts/email/send_test.py", "scripts/email/verify_gallery.py"), id="mail-operator-tools"),
    pytest.param((), ("services/qm/package.json", "services/qm/plugins/admin/package.json",
                     "scripts/qm_dev.py", "scripts/qm_ensure_crons.py"), id="optional-qm-sidecar"),
    pytest.param((), ("tests/unit/suggestions/test_suggestion_outreach_review.py",
                     "tests/unit/chat/test_negative_mac_task_list.py", "scripts/cu_browser_trials.py",
                     "tests/unit/research/test_founder_presentation.py"), id="personal-fixture"),
    pytest.param((), ("docs/site/index.html", "docs/site/styles.css", "docs/site/charlie.js",
                     "docs/site/assets/mascot-player.js"), id="unlicensed-site"),
    pytest.param(("services/company_os/app/consumer/cognito.py",),
                 ("scripts/ci_base_images.py", "tests/unit/infra/test_ci_base_images.py",
                  "scripts/infra/deploy_core.py", "tests/unit/infra/test_deploy_core.py",
                  "tests/unit/test_landing_copy_lock.py", "tests/unit/infra/test_verified_hotfix_dispatch.py"),
                 id="hosted-deployment"),
    pytest.param(("scripts/local_browser_journey.py", "scripts/local_build_journey.py", "scripts/local_mail_journey.py",
                  "tests/unit/test_local_browser_journey.py",
                  "tests/unit/test_desktop_version.py", "tests/unit/test_local_build_journey.py",
                  "tests/unit/test_build_scope_journey.py"),
                 ("tests/unit/builder/test_egress_policy.py", "tests/unit/builder/test_product_visual_diagnostics.py",
                  "tests/unit/infra/test_release_batch.py", "tests/unit/infra/test_staging_parity.py",
                  "tests/unit/infra/test_staging_smoke.py", "tests/unit/product/test_public_product_outer_runner.py",
                  "tests/unit/test_harness_loop.py",
                  "scripts/publish_desktop_downloads.py"), id="public-ci-test-inputs"),
    pytest.param(("services/company_os/app/tasks/scheduler.py", "services/company_os/app/gtm/local_mail.py",
                  "services/company_os/app/provisioning/runtime_intents.py", "services/company_os/app/admin/authn.py",
                  "services/qa_runner/requirements.lock", "docker-compose.local.yml", "scripts/desktop/build_runtime.py"),
                 ("services/company_os/app/build/subscription_product_diagnostic_admission.py",
                  "services/company_os/app/provisioning/database_controller.py",
                  "services/company_os/app/hosted_routes.py",
                  "services/company_os/app/api/internal_tick.py", "services/company_os/app/admin/router.py",
                  "services/company_os/app/payments/stripe.py", "services/company_os/app/email/webhooks.py",
                  "services/company_os/app/api/openai_api.py", "services/company_os/app/hermes/bootstrap.py",
                  "apps/console/src/app/admin/page.tsx", "apps/console/src/components/credits/TopUpCard.tsx",
                  "services/gateway/app/main.py", "services/qa_runner/app/main.py", "services/product_qa/runner.py",
                  "services/product_qa/app_boot.py", "services/product_qa/assessor.py",
                  "docker-compose.companyos.yml", "scripts/desktop/prepare_release.py"), id="local-runtime-cut"),
    pytest.param(("tests/unit/infra/test_local_docs_contract.py",),
                 ("tests/unit/infra/test_auth_custom_domain.py", "tests/unit/infra/test_b5_build_runtime_terraform.py",
                  "tests/unit/infra/test_production_build_runtime_terraform.py"), id="private-terraform-tests"),
    pytest.param((), ("config/scaffold/README.md", "config/scaffold/app/auth.py", "tests/integration/test_scaffold_auth.py"),
                 id="hosted-scaffold"),
    pytest.param(("scripts/desktop/smoke_local_stack.py", "scripts/local_smoke.sh"),
                 ("scripts/smoke.py", "scripts/desktop/collect_macos.py", "scripts/desktop/release_gate_integration.diff",
                  "tests/unit/local_flows/CI_customers.diff"), id="hosted-operator-material"),
    pytest.param((), ("services/qm/LICENSE", "services/qm/src/index.ts", "services/qm/local/Dockerfile",
                  "services/qm/cli/src/config.ts", "services/qm/cli/templates/slack-manifest.json",
                  "services/qm/cli/templates/aws/main.tf", "services/qm/plugins/admin/public/index.html",
                  "services/qm/cli/test/terraform.test.ts", "services/qm/test/cache-observability.test.ts",
                  "services/qm/cli/src/backends/aws.ts", "services/qm/cli/bin/qm.ts",
                  "services/qm/deploy/core/fly.toml", "services/qm/test/live-slack/scenarios.ts",
                  "services/qm/scripts/aws-deploy-smoke.ts"),
                 id="qm-hosted-assets"),
])
def test_public_export_release_boundaries(tmp_path, domain, retained, excluded):
    source = tmp_path / "source"
    required = (
        *CORE_RELEASE_FILES,
        "README.md", "LICENSE", "NOTICE", "CONTRIBUTING.md", "SECURITY.md", "CODE_OF_CONDUCT.md",
        "CHANGELOG.md", ".env.example", "Makefile", ".github/ISSUE_TEMPLATE/release_checklist.md",
        ".github/workflows/public-ci.yml", ".github/ISSUE_TEMPLATE/bug_report.md",
        ".github/ISSUE_TEMPLATE/feature_request.md", ".github/ISSUE_TEMPLATE/self_hosting_help.md",
        ".github/PULL_REQUEST_TEMPLATE.md", "apps/desktop/package.json", "apps/desktop/pnpm-lock.yaml",
    )
    names = set((*required, *retained, *excluded))
    content = f"Release fixture for {domain}\n"
    contents = {name: "def test_export_fixture(): pass\n" if Path(name).name.startswith("test_") and name.endswith(".py")
                else '{}\n' if name == "apps/desktop/package.json" else content for name in names}
    for name in names:
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents[name])
    scripts = source / "scripts"
    scripts.mkdir(exist_ok=True)
    for name in ("make_public_tree.sh", "check_public.sh", "check_public.py"):
        shutil.copyfile(ROOT / "scripts" / name, scripts / name)
    shutil.copyfile(ROOT / ".gitleaks.toml", source / ".gitleaks.toml")
    tools = tmp_path / "tools"
    tools.mkdir()
    git = tools / "git"
    git.write_text(
        f"#!{sys.executable}\nimport sys\n"
        "if 'rev-parse' in sys.argv:\n"
        "    print(sys.argv[2]); sys.exit(0)\n"
        f"sys.stdout.buffer.write({bytes(chr(0).join(sorted(names)) + chr(0), 'utf-8')!r})\n"
    )
    git.chmod(0o755)
    scanner = tools / "gitleaks"
    scanner.write_text(
        f"#!{sys.executable}\nimport sys\nfrom pathlib import Path\n"
        "Path(sys.argv[sys.argv.index('--report-path') + 1]).write_text('[]')\n"
    )
    scanner.chmod(0o755)
    destination = tmp_path / "public"
    result = subprocess.run(["bash", str(scripts / "make_public_tree.sh"), str(destination)],
                            env={**os.environ, "PATH": f"{tools}:{os.environ['PATH']}", "PUBLIC_CHECK_PYTHON": sys.executable},
                            capture_output=True, text=True)
    for name in retained:
        assert (destination / name).is_file(), name
        if name.startswith("src-tauri/icons/") or name in {
            "apps/console/public/charlie/charlie-mark.png", "apps/console/public/supymem-logo.svg",
        }:
            assert (destination / name).read_bytes() != content.encode(), name
        else:
            assert (destination / name).read_text() == contents[name]
    for name in excluded:
        assert not (destination / name).exists(), name
    assert result.returncode == 0, result.stdout + result.stderr
    assert sorted(path.name for path in (destination / ".github/workflows").iterdir()) == ["public-ci.yml"]


@pytest.mark.parametrize("domain", ["wholesale.example", "gardening.example"])
def test_public_export_is_allowlisted_non_destructive_and_scanned(tmp_path, domain):
    exporter = ROOT / "scripts/make_public_tree.sh"
    assert exporter.is_file()
    source = tmp_path / "private"
    source.mkdir()
    files = {
        **dict.fromkeys(CORE_RELEASE_FILES, f"Public fixture for {domain}\n"),
        "services/demo/app.py": f'contact = "owner@{domain}"\n',
        "packages/demo/LICENSE": "MIT\n",
        "apps/console/public/charlie/mascot_animations.glb": "unverified rights\n",
        "src-tauri/splashscreen/swing/mascot_animations.glb": "unverified rights\n",
        "data/knowledge/reviewed/README.md": "unverified rights\n",
        "docs/self-hosting/install.md": "Install locally.\n",
        "docs/archive/plan.md": "private\n",
        "docs/supymem/qa-collection/report.json": "private\n",
        "config/hosts.json": json.dumps({"local": {"core": "http://127.0.0.1:3300"},
                                         "staging": {"core": f"https://staging.{domain}"},
                                         "production": {"core": f"https://{domain}"}}),
        "config/scaffold/cognito-dev.json": "private\n",
        "services/demo/docs/plan.md": "private\n",
        "scripts/infra/README.md": "private\n",
        "scripts/infra/example.py": "private\n",
        "scripts/evals/research_prior_art/README.md": "private\n",
        "scripts/infra/create_staging_db.sql": "private\n",
        "scripts/bench/results/benchmark.json": "private\n",
        "data/knowledge/reviewed/.aws/credentials": "private\n",
        "services/demo/.env": "private\n",
        "services/demo/data/customer.json": "private\n",
        "services/demo/data-backups/snapshot.json": "private\n",
        "services/demo/vault/master.key": "private\n",
        "services/demo/qa-evidence/output.json": "private\n",
        "services/demo/cloud-inventory.json": "private\n",
        "services/demo/settings.enc": "private\n",
        "services/demo/private.db": "private\n",
        "services/demo/private.sqlite3-wal": "private\n",
        "services/demo/backup.tar.gz": "private\n",
        "services/demo/.gitleaksignore": "private\n",
        "services/demo/AGENTS.md": "private\n",
        "services/qm/skills-seed/unreviewed/SKILL.md": "unknown license\n",
        "services/qm/skills-seed/taste-skill/LICENSE": "MIT\n",
        "services/qm/skills-seed/taste-skill/SKILL.md": "Licensed skill\n",
        "services/company_os/app/knowledge/yc_video_advice.json.gz": "unlicensed\n",
        "apps/console/public/connections/logo.png": "unverified rights\n",
        "apps/console/src/app/icon.svg": "unverified rights\n",
        "vendor/postiz/LICENSE": "AGPL\n",
        "vendor/research/openleads/LICENSE": "noncommercial\n",
        "vendor/research/email-supymem/source.py": "unknown license\n",
        "third_party/anthropic-skills/docx/SKILL.md": "restricted\n",
        ".github/workflows/deploy-production.yml": "private deployment\n",
        ".github/workflows/ci.yml": "private deployment\n",
        "tests/unit/test_founder_go_live_doc.py": "private\n",
        ".github/workflows/public-ci.yml": "name: Public CI\n",
        ".github/workflows/desktop-release.yml": "name: desktop-local-release\n",
        ".github/workflows/desktop-local-release.yml": "name: Local desktop release\n",
        ".github/ISSUE_TEMPLATE/bug_report.md": f"Report a bug for {domain}.\n",
        ".github/ISSUE_TEMPLATE/feature_request.md": f"Request a feature for {domain}.\n",
        ".github/ISSUE_TEMPLATE/self_hosting_help.md": f"Get local desktop help for {domain}.\n",
        ".github/ISSUE_TEMPLATE/release_checklist.md": f"Release checks for {domain}.\n",
        ".github/PULL_REQUEST_TEMPLATE.md": "Describe your focused checks.\n",
        "apps/desktop/package.json": '{"private": true}\n',
        "apps/desktop/pnpm-lock.yaml": "lockfileVersion: '9.0'\n",
        ".git/history": "private history\n",
        "README.md": "Read the self-hosting guide.\n",
        "LICENSE": "Apache-2.0\n",
        "NOTICE": "Project notices\n",
        "CONTRIBUTING.md": "Contribute.\n",
        "SECURITY.md": "Report privately.\n",
        "CODE_OF_CONDUCT.md": "Be kind.\n",
        "CHANGELOG.md": "Release changes.\n",
        ".env.example": "EXAMPLE=\n",
        "Makefile": "help:\n\t@echo help\n",
    }
    for name, content in files.items():
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    for name in ("make_public_tree.sh", "check_public.sh", "check_public.py"):
        target = source / "scripts" / name
        target.parent.mkdir(exist_ok=True)
        shutil.copyfile(ROOT / "scripts" / name, target)
    shutil.copyfile(ROOT / ".gitleaks.toml", source / ".gitleaks.toml")
    external = tmp_path / "outside.txt"
    external.write_text("private outside file\n")
    (source / "services/demo/link.py").symlink_to(external)
    (source / "services/demo/untracked.py").write_text("not approved\n")
    tools = tmp_path / "tools"
    tools.mkdir()
    git = tools / "git"
    git.write_text(
        f"#!{sys.executable}\nimport sys\n"
        "if 'rev-parse' in sys.argv:\n"
        "    print(sys.argv[2]); sys.exit(0)\n"
        "if 'check-ignore' in sys.argv:\n"
        "    sys.exit(0 if sys.argv[-1] == '.tmp/public' else 1)\n"
        f"sys.stdout.buffer.write({bytes(chr(0).join([*files, 'services/demo/link.py']) + chr(0), 'utf-8')!r})\n"
    )
    git.chmod(0o755)
    scanner = tools / "gitleaks"
    scanner.write_text(
        f"#!{sys.executable}\nimport sys\nfrom pathlib import Path\n"
        "Path(sys.argv[sys.argv.index('--report-path') + 1]).write_text('[]')\n"
    )
    scanner.chmod(0o755)
    destination = tmp_path / "public"
    environment = {**os.environ, "PATH": f"{tools}:{os.environ['PATH']}"}
    result = subprocess.run(["bash", str(source / "scripts/make_public_tree.sh"), str(destination)],
                            env=environment, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (destination / "services/demo/app.py").read_text() == files["services/demo/app.py"]
    assert (destination / "scripts/make_public_tree.sh").is_file()
    assert (destination / ".github/workflows/public-ci.yml").is_file()
    assert not (destination / "services/qm/skills-seed/taste-skill").exists()
    assert json.loads((destination / "config/hosts.json").read_text()) == {"local": {"core": "http://127.0.0.1:3300"}}
    assert "Public release scan passed: 0 findings" in result.stdout
    for name in files:
        if files[name] in {"private\n", "private deployment\n", "private history\n", "AGPL\n",
                           "noncommercial\n", "unknown license\n", "restricted\n", "unlicensed\n",
                           "unverified rights\n"}:
            assert not (destination / name).exists(), name
        assert (source / name).read_text() == files[name]
    assert not (destination / "services/demo/link.py").exists()
    assert not (destination / "services/demo/untracked.py").exists()
    before = sorted(path.relative_to(destination) for path in destination.rglob("*"))
    again = subprocess.run(["bash", str(source / "scripts/make_public_tree.sh"), str(destination)],
                           env=environment, capture_output=True, text=True)
    assert again.returncode == 2
    assert "empty" in again.stderr
    assert before == sorted(path.relative_to(destination) for path in destination.rglob("*"))
    inside = source / "export"
    refused = subprocess.run(["bash", str(source / "scripts/make_public_tree.sh"), str(inside)],
                             env=environment, capture_output=True, text=True)
    assert refused.returncode == 2
    assert not inside.exists()
    scratch = source / ".tmp/public"
    result = subprocess.run(["bash", str(source / "scripts/make_public_tree.sh"), str(scratch)],
                            env=environment, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (scratch / "services/demo/app.py").read_text() == files["services/demo/app.py"]
    unignored = source / ".tmp/unignored"
    result = subprocess.run(["bash", str(source / "scripts/make_public_tree.sh"), str(unignored)],
                            env=environment, capture_output=True, text=True)
    assert result.returncode == 2
    assert not unignored.exists()
    alias = tmp_path / "alias"
    alias.symlink_to(destination, target_is_directory=True)
    refused_alias = subprocess.run(["bash", str(source / "scripts/make_public_tree.sh"), str(alias)],
                                   env=environment, capture_output=True, text=True)
    assert refused_alias.returncode == 2
    assert before == sorted(path.relative_to(destination) for path in destination.rglob("*"))


@pytest.mark.parametrize("domain", ["wholesale.example", "gardening.example"])
def test_public_community_export_is_complete_and_fork_safe(tmp_path, domain):
    workflow_text = (ROOT / ".github/workflows/public-ci.yml").read_text()
    workflow = yaml.safe_load(workflow_text)
    assert workflow["permissions"] == {"contents": "read"}
    assert all(job["runs-on"] == "ubuntu-latest" for job in workflow["jobs"].values())
    assert workflow["jobs"]["python"]["env"]["SUPYMEM_MODE"] == "local"
    assert workflow["jobs"]["console"]["env"]["SUPYMEM_MODE"] == "local"
    assert not re.search(r"secrets\.|\b(?:aws|ecs|self-hosted|pull_request_target)\b", workflow_text, re.IGNORECASE)
    assert workflow["jobs"]["console"]["env"]["NODE_USE_SYSTEM_CA"] == "0"
    assert workflow["jobs"]["console"]["defaults"]["run"]["working-directory"] == "apps/console"
    for command in ("ruff==0.16.10", "scripts/check_public.sh --check-tree", "-m pytest", '-m "not hosted"', '"$PWD/tests/unit"',
                    "pnpm run typecheck", "pnpm test", "config --quiet", "cargo check --features local-stack"):
        assert command in workflow_text
    source = tmp_path / "source"
    public_files = (
        *CORE_RELEASE_FILES,
        "README.md", "LICENSE", "NOTICE", "CONTRIBUTING.md", "SECURITY.md", "CODE_OF_CONDUCT.md", "CHANGELOG.md",
        ".env.example", ".env.local.example", "start", "start.ps1", "scripts/run_local.sh", "scripts/run_local.ps1",
        "Makefile", ".github/workflows/public-ci.yml",
        ".github/ISSUE_TEMPLATE/bug_report.md", ".github/ISSUE_TEMPLATE/feature_request.md",
        ".github/ISSUE_TEMPLATE/self_hosting_help.md", ".github/PULL_REQUEST_TEMPLATE.md",
        ".github/ISSUE_TEMPLATE/release_checklist.md",
        "apps/desktop/package.json", "apps/desktop/pnpm-lock.yaml",
    )
    private_files = (".github/workflows/ci.yml", ".github/workflows/desktop-release.yml",
                     ".github/workflows/desktop-local-release.yml", "docs/site/index.html", "docs/site/styles.css",
                     "docs/site/charlie.js", "docs/site/assets/mascot-player.js",
                     ".env.local", "docs/self-hosting/.env.local.example", "docs/site/.env", "docs/site/.aws/credentials",
                     "docs/site/data/customer.json", "docs/archive/customer.md")
    for name in (*public_files, *private_files, "docs/site/untracked.html"):
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{}\n' if name == "apps/desktop/package.json" else f"Public fixture for {domain}\n")
    scripts = source / "scripts"
    for name in ("make_public_tree.sh", "check_public.sh", "check_public.py"):
        shutil.copyfile(ROOT / "scripts" / name, scripts / name)
    shutil.copyfile(ROOT / ".gitleaks.toml", source / ".gitleaks.toml")
    tools = tmp_path / "tools"
    tools.mkdir()
    git = tools / "git"
    git.write_text(
        f"#!{sys.executable}\nimport sys\nfrom pathlib import Path\n"
        "root = Path(sys.argv[2])\n"
        "if 'rev-parse' in sys.argv:\n"
        "    print(root); sys.exit(0)\n"
        f"for name in {(*private_files, *CORE_RELEASE_FILES)!r}:\n"
        "    if (root / name).is_file():\n"
        "        sys.stdout.buffer.write(name.encode() + b'\\0')\n"
    )
    git.chmod(0o755)
    scanner = tools / "gitleaks"
    scanner.write_text(
        f"#!{sys.executable}\nimport sys\nfrom pathlib import Path\n"
        "Path(sys.argv[sys.argv.index('--report-path') + 1]).write_text('[]')\n"
    )
    scanner.chmod(0o755)
    environment = {**os.environ, "PATH": f"{tools}:{os.environ['PATH']}"}
    destination = tmp_path / "public"
    command = ["bash", str(scripts / "make_public_tree.sh")]
    result = subprocess.run([*command, str(destination)], env=environment, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    for name in public_files:
        assert (destination / name).read_text() == (source / name).read_text()
    for name in (*private_files, "docs/site/untracked.html"):
        assert not (destination / name).exists(), name
    for name in (*CORE_RELEASE_FILES, ".github/workflows/public-ci.yml", "CHANGELOG.md", ".github/ISSUE_TEMPLATE/release_checklist.md"):
        required_path = source / name
        required_path.unlink()
        incomplete = tmp_path / ("missing-" + Path(name).name)
        refused = subprocess.run([*command, str(incomplete)], env=environment, capture_output=True, text=True, check=False)
        assert refused.returncode == 2
        assert "Required release files are missing" in refused.stderr
        assert not incomplete.exists()
        required_path.write_text(f"Public fixture for {domain}\n")


@pytest.mark.parametrize("domain", ["wholesale.example", "gardening.example"])
def test_public_make_setup_and_hosted_guard(tmp_path, domain):
    tree = tmp_path / domain
    for directory in ("apps/console", "apps/desktop", "scripts/desktop"):
        (tree / directory).mkdir(parents=True)
    shutil.copyfile(ROOT / "Makefile", tree / "Makefile")
    (tree / "scripts/desktop/build_runtime.py").touch()
    make = ["make", "PYTHON=true", "PY=true", "PIP=true", "PNPM=true"]
    setup = subprocess.run([*make, "setup"], cwd=tree, capture_output=True, text=True)
    assert setup.returncode == 0 and not setup.stderr
    build = subprocess.run([*make, "desktop-build-hosted"], cwd=tree, capture_output=True, text=True)
    assert build.returncode != 0
    assert build.stdout.strip() == "Your public source does not include the hosted desktop build. Build your local installer with ./start --build."
    help_result = subprocess.run([*make, "help"], cwd=tree, capture_output=True, text=True, check=True)
    assert "llm-adapter" not in help_result.stdout
    assert "skills-fetch" not in help_result.stdout
    assert "qm-dev" not in help_result.stdout
    qm = subprocess.run([*make, "qm-dev"], cwd=tree, capture_output=True, text=True)
    assert qm.returncode != 0 and "Run ./start" in qm.stderr
    (tree / "tests/unit").mkdir(parents=True)
    (tree / "tests/unit/test_ids.py").touch()
    local_tests = subprocess.run([*make, "-n", "test"], cwd=tree, capture_output=True, text=True, check=True)
    assert re.findall(r"tests/unit/\S+", local_tests.stdout) == ["tests/unit/test_ids.py"]
    for target, next_step in (("skills-fetch", "private checkout"), ("canary-test", "make test"),
                              ("smoke", "scripts/desktop/smoke_local_stack.py")):
        missing = subprocess.run([*make, target], cwd=tree, capture_output=True, text=True)
        assert missing.returncode != 0
        assert next_step in missing.stdout


@pytest.mark.parametrize("domain", ["wholesale.example", "gardening.example"])
@pytest.mark.parametrize(("file", "private_test", "private_input", "fixture"), [
    pytest.param("analytics/test_feature_catalog.py", "test_every_inventory_feature_maps_to_a_slug",
                 "docs/supymem/feature-audit-2026-09-30/INVENTORY.md", None, id="inventory"),
    pytest.param("test_layering.py", "test_windows_release_smokes_the_installed_nsis_payload",
                 ".github/workflows/desktop-release.yml", None, id="desktop-workflow"),
    pytest.param("test_ui_config.py", "test_admin_flags_preserve_boolean_contract_and_validate_layout",
                 "services/company_os/app/admin/router.py", None, id="lazy-import"),
    pytest.param("billing/test_subscription_lifecycle.py", "test_paid_period_expiry",
                 "services/company_os/app/admin/internal.py", "billing_env", id="hosted-fixture"),
    pytest.param("lead_library/test_reclassify.py", "test_only_ambiguous_row_evidence_uses_metered_classifier",
                 "scripts/leads/reclassify.py", "reclassifier", id="private-maintenance-tool"),
    pytest.param("test_pmf_knowledge.py", "test_chat_workbook_answer_cites_a_relevant_row_or_abstains",
                 "services/company_os/app/knowledge/startup_pmf.json.gz", None, id="private-coaching-pack"),
    pytest.param("tasks/test_progress.py", "test_actual_twenty_source_goal_uses_bounded_remaining_tool_budget",
                 "packages/harness/loop.py", None, id="private-harness"),
])
def test_public_ci_selects_portable_checks_without_private_inputs(tmp_path, domain, file, private_test, private_input, fixture):
    tree = tmp_path / domain
    test_file = tree / "tests/unit" / file
    test_file.parent.mkdir(parents=True)
    shutil.copyfile(ROOT / "tests/conftest.py", tree / "tests/conftest.py")
    shutil.copyfile(ROOT / "pytest.ini", tree / "pytest.ini")
    setup = f"import pytest\n@pytest.fixture\ndef {fixture}(): pass\n" if fixture else ""
    test_file.write_text(f"{setup}def {private_test}({fixture or ''}): pass\ndef test_portable(): pass\n")
    workflow = yaml.safe_load((ROOT / ".github/workflows/public-ci.yml").read_text())
    shard = next(step for step in workflow["jobs"]["python"]["steps"] if step.get("name") == "Local SQLite unit shard")
    selector = re.search(r'-m "([^"]+)"', shard["run"])[1]
    command = [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider", "-m", selector,
               str(test_file)]
    result = subprocess.run(command, cwd=tree, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "::test_portable" in result.stdout
    assert f"::{private_test}" not in result.stdout
    required = tree / private_input
    required.parent.mkdir(parents=True)
    required.write_text(f"Private fixture for {domain}\n")
    private = subprocess.run(command, cwd=tree, capture_output=True, text=True, check=True)
    assert f"::{private_test}" in private.stdout


@pytest.mark.parametrize("domain", ["freight dispatch", "family gardening"])
def test_public_shard_cases_isolate_desktop_shutdown_and_redaction(tmp_path, domain):
    test_file = tmp_path / "tests/unit/test_runtime_isolation.py"
    test_file.parent.mkdir(parents=True)
    shutil.copyfile(ROOT / "tests/conftest.py", tmp_path / "tests/conftest.py")
    shutil.copyfile(ROOT / "pytest.ini", tmp_path / "pytest.ini")
    test_file.write_text(f'''\
from core import logging
from desktop_runtime import agent_status

def test_shut_down():
    logging.register_local_secret({domain!r})
    agent_status.stop_probes()
    assert agent_status._PROBES_CLOSED

def test_start_clean():
    assert not agent_status._PROBES_CLOSED
    assert {domain!r} not in logging._local_secret_values()
''')
    result = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(test_file)],
                            cwd=tmp_path, env={**os.environ, "SUPYMEM_MODE": "local"}, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("service", [pytest.param("core", id="wholesale.example"),
                                    pytest.param("console", id="gardening.example")])
def test_local_compose_images_include_release_notices(service):
    compose = yaml.safe_load((ROOT / "docker-compose.local.yml").read_text())
    build = compose["services"][service]["build"]
    recipe = (ROOT / build["context"] / build.get("dockerfile", "Dockerfile")).read_text()
    if service == "core":
        runtime = recipe.split(" AS runtime\n", 1)[1].split("\nFROM ", 1)[0]
        assert "COPY LICENSE NOTICE /workspace/" in runtime
        assert f"FROM runtime AS {build['target']}\n" in recipe
    else:
        context, = build["additional_contexts"]
        assert (ROOT / build["additional_contexts"][context]).resolve() == ROOT
        stage = recipe.split(f" AS {build['target']}\n", 1)[1].split("\nFROM ", 1)[0]
        assert f"COPY --from={context} LICENSE NOTICE /app/" in stage


@pytest.mark.parametrize(("domain", "declared", "installed"), [
    ("wholesale.example", "2.3.4-rc.5", "2.3.4rc5"),
    ("gardening.example", "3.2.1", "3.2.1"),
])
def test_company_os_image_checks_installed_metadata_against_manifest(tmp_path, domain, declared, installed):
    recipe = (ROOT / "services/company_os/Dockerfile").read_text()
    assertion = next(line for line in recipe.splitlines() if "m.version('company-os')" in line)
    _, _, _, option, code = shlex.split(assertion.removesuffix("\\"))
    tree = tmp_path / domain
    manifest = tree / "services/company_os/pyproject.toml"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(f'[project]\nname = "company-os"\nversion = "{declared}"\n')
    for name, version in (("company_os", installed), ("company_os_core", "0.1.0"), ("llm_gateway", "0.1.0")):
        metadata = tree / f"{name}-{version}.dist-info" / "METADATA"
        metadata.parent.mkdir()
        metadata.write_text(f"Name: {name.replace('_', '-')}\nVersion: {version}\n")
    command = [sys.executable, option, code]
    match = subprocess.run(command, cwd=tree, capture_output=True, text=True)
    assert match.returncode == 0, match.stderr
    manifest.write_text('[project]\nname = "company-os"\nversion = "9.9.9"\n')
    mismatch = subprocess.run(command, cwd=tree, capture_output=True, text=True)
    assert mismatch.returncode != 0 and "AssertionError" in mismatch.stderr


@pytest.mark.parametrize("domain", ["wholesale.example", "gardening.example"])
def test_public_source_rights_are_distinct_from_installer_payload(tmp_path, domain):
    release = tmp_path / domain
    release.mkdir()
    notice = release / "THIRD_PARTY.md"
    shutil.copyfile(ROOT / "docs/self-hosting/THIRD_PARTY.md", notice)
    rows = notice.read_text().splitlines()
    for item in ("GBrain algorithms", "DeepSeek Harness Python", "PMF workbook", "YC video advice",
                 "Charlie mascot", "Brand and connection artwork"):
        assert "Excluded from the public source" in next(row for row in rows if row.startswith(f"| {item}"))
    checks = notice.read_text().split("## Release checks\n", 1)[1]
    assert "exact installer payload" in checks
    assert "Hold publication of both source and installers" not in checks


@pytest.mark.parametrize("domain", ["wholesale.example", "gardening.example"])
def test_snapshot_scan_reports_categories_without_git_or_secret_values(tmp_path, domain):
    tree = tmp_path / "tree"
    tree.mkdir()
    shutil.copyfile(ROOT / ".gitleaks.toml", tree / ".gitleaks.toml")
    credential = "sk-" + "proj-" + "A1b2C3d4" * 8
    (tree / "ignored.py").write_text(f'contact = "owner@{domain}"\ncredential = "{credential}"\n')
    (tree / ".gitignore").write_text("ignored.py\n")
    tools = tmp_path / "tools"
    tools.mkdir()
    scanner = tools / "gitleaks"
    scanner.write_text(
        f"#!{sys.executable}\nimport json, sys\nfrom pathlib import Path\n"
        "assert sys.argv[1] == 'dir'\n"
        "assert '--redact=100' in sys.argv\n"
        "assert '--ignore-gitleaks-allow' in sys.argv\n"
        "Path(sys.argv[sys.argv.index('--report-path') + 1]).write_text(json.dumps([]))\n"
    )
    scanner.chmod(0o755)
    git = tools / "git"
    git.write_text(f"#!{sys.executable}\nraise SystemExit(99)\n")
    git.chmod(0o755)
    result = subprocess.run(["bash", str(ROOT / "scripts/check_public.sh"), str(tree)],
                            env={**os.environ, "PATH": f"{tools}:{os.environ['PATH']}"},
                            capture_output=True, text=True)
    assert result.returncode == 1
    assert "public-openai-key: ignored.py:2" in result.stdout
    assert credential not in result.stdout + result.stderr
    (tree / "ignored.py").write_text(f'contact = "owner@{domain}"\n')
    clean = subprocess.run(["bash", str(ROOT / "scripts/check_public.sh"), str(tree)],
                           env={**os.environ, "PATH": f"{tools}:{os.environ['PATH']}"},
                           capture_output=True, text=True)
    assert clean.returncode == 0, clean.stderr
    protected = tree / "protected"
    protected.mkdir()
    (protected / "input.py").write_text(f'contact = "owner@{domain}"\n')
    protected.chmod(0)
    try:
        unreadable = subprocess.run(["bash", str(ROOT / "scripts/check_public.sh"), str(tree)],
                                    env={**os.environ, "PATH": f"{tools}:{os.environ['PATH']}"},
                                    capture_output=True, text=True)
        assert unreadable.returncode == 2
        assert "Scan failed" in unreadable.stderr
    finally:
        protected.chmod(0o755)
