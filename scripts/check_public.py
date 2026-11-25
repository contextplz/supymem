"""Fail closed before publishing a worktree or any reachable Git history."""

from __future__ import annotations

import argparse
import ast
from functools import cache
from hashlib import sha256
import importlib.machinery
import inspect
import json
import os
import re
import shutil
import subprocess
import sys
import sysconfig
import tempfile
from textwrap import dedent
import tomllib
from pathlib import Path


_PRIVATE_SAMPLE_DIGESTS = {
    1: {
        "24981f0abb915fe5e3168e59a9eeb6cb29babfa4d1ed9c974223a81e79f0a196",
        "29fb6dc6093170355888bdc0bf94237b119f5b7731c587f430c39c6ab9e8bd7b",
        "4114e87cc540895bf803ee8536169b1494f31242d2cf49f26d025fa38793f445",
        "594c6b4a1bdc19d9bf5410a4020cd75d68ecafe3848e8196a1c4b58d21c2a376",
        "5c59f5d68884fe54836d10615152394305ab6d1c90621f00f2202a204eac23ae",
        "6a25e85b97647f967d9ceb714bdc696eba24e2383d7d3caa6f2802a18e46ff10",
        "6a723efe5e1b6b6b1fe6598d98e83b76a27e0471c2f32fde8888c64f1323b7fb",
        "6ce2a56426dcb6ddd2ff66b234a177edb29d2554613192d7d21266ba6c0f5783",
        "7c95e8fdd317157cebe85b4320b879c1c0990086105f6dff919450688594956f",
        "9ebe38af168709f6796c968d3153f720ca7fad4d7498b474f5421e003953b2f1",
        "be36b63dae719ab44d7b32bd4a074f1ed163aaa8e88c51be6e4365ebfb515946",
        "d2015702d31aa92e2ff9f22cb894107a42967f5c1a2e8c6a7e089962e2570512",
        "dd84735ca1d720372d23b78ac1dc7d9b6d24ec35cf60738315ad4612cc132d61",
    },
    2: {
        "1ee975403c0de5982467c3b38c60da29c62ad54c0c4edea9c991ea3a123dbf01",
        "31676a074946de058c16fd1dee8ed8dfceff61ebfe31da0766be8ddf49291d06",
        "4662e8fd5f527ae9870da77b9162319f9ffb640364eb9c53711d142695ef56f7",
        "481d74fef16d8d204b48fe9b1c731ed264e223c224d510c63b56e52ae8e731bf",
        "4e01d49fc809a20dcfe50518e13f4515057c7b9016a9e089cff1753dee9647b7",
        "6435b0a421e1c79f728f378be95be5cccbb8815a2ccffbe16fde3a59a6849cc2",
        "86df0fbe61c497a5489920b818574f6dac614fd32c87644a5784cff4e168458c",
        "87bc6ec93bb6d9c2f1be9f29464be664cfd21e466f3c221f6c92e3298f658d9a",
        "ccfe539e0afe761859e9c3384240036da3aa1478dab85e45ce6b4ea1b7542391",
        "e2f5f38231af7d04655ce224be5b4ddf1729e46f88c70115fe782709a58122ad",
    },
}
_SAMPLE_WORD = re.compile(r"[a-z0-9]+")
_SAMPLE_ATTRIBUTION = re.compile(r"\b(?:beta[ -]+(?:user[ -]+)?feedback|beta[ _-]+(?:testers?|conversations?)|"
                                 r"dogfood[ -]+(?:conversations?|transcripts?|ventures?)|founder[ -]+(?:quotes?|transcripts?))\b")


def private_sample(text: str) -> bool:
    text = text.lower()
    if _SAMPLE_ATTRIBUTION.search(text):
        return True
    words = _SAMPLE_WORD.findall(re.sub(r"\\[nrt]", " ", text))
    return any(sha256(" ".join(words[index:index + size]).encode()).hexdigest() in digests
               for size, digests in _PRIVATE_SAMPLE_DIGESTS.items() for index in range(len(words) - size + 1))


def private_artifact(path: Path) -> bool:
    parts = tuple(part.casefold() for part in path.parts)
    name = parts[-1]
    return (
        bool({".git", "data-backups", "vault", "evidence", "qa-evidence", "qa_evidence"}.intersection(parts))
        or parts[:2] in {("docs", "archive"), ("docs", "supymem"), ("scripts", "infra"), ("config", "scaffold")}
        or name.endswith(".diff")
        or parts in {("scripts", "smoke.py"), ("scripts", "desktop", "collect_macos.py"),
                     ("scripts", "desktop", "cu_browser_installed_acceptance.py"),
                     ("scripts", "desktop", "cu_browser_qualification_controller.py")}
        or parts[:3] == ("services", "qm", "deploy")
        or parts[:4] == ("services", "qm", "test", "live-slack")
        or parts[:5] in {("services", "qm", "cli", "templates", "aws"), ("services", "qm", "plugins", "admin", "public")}
        or (parts[:3] == ("tests", "unit", "infra") and name in {
            "test_auth_custom_domain.py", "test_b5_build_runtime_terraform.py", "test_production_build_runtime_terraform.py",
        })
        or parts[0] in {"infra", "deploy"}
        or (parts[:2] == (".github", "workflows") and name.startswith("deploy-"))
        or (parts[0] == "config" and "cognito" in name)
        or name.startswith(("cloud-inventory", "cloud_inventory"))
        or name in {"master.key", "settings.enc", "machine.secret", "local-settings.json", "local-key.bin", "owner-session.secret"}
        or ".sqlite" in name
        or name.endswith((".tar.gz", ".tar.bz2", ".tar.xz", ".tar", ".tgz", ".zip", ".7z", ".rar",
                          ".db", ".db-wal", ".db-shm", ".enc"))
    )


def _matches(patterns: list[str], value: str) -> bool:
    return any(re.search(pattern, value) is not None for pattern in patterns)


def _allowed(rule: dict, name: str, secret: str, line: str) -> bool:
    for allowlist in rule.get("allowlists", []):
        checks = []
        if "paths" in allowlist:
            checks.append(_matches(allowlist["paths"], name))
        if "regexes" in allowlist:
            target = line if allowlist.get("regexTarget") == "line" else secret
            checks.append(_matches(allowlist["regexes"], target))
        if checks and (all(checks) if allowlist.get("condition") == "AND" else any(checks)):
            return True
    return False


def scan_file(path: Path, name: str, config: dict) -> list[tuple[str, int, str]]:
    rules = [rule for rule in config["rules"] if rule["id"].startswith("public-")]
    for rule in rules:
        if (rule["id"] == "public-internal-document" and _matches([rule["path"]], name)
                and not _allowed(rule, name, "", "")):
            return [(name, 1, rule["id"])]
    data = str(path.readlink()).encode() if path.is_symlink() else path.read_bytes()
    text = data.decode("utf-8", errors="replace")
    findings = []
    if private_sample(name.replace("_", " ")):
        findings.append((name, 1, "public-private-sample"))
    if b"\0" not in data:
        findings.extend((name, number, "public-private-sample") for number, line in enumerate(text.splitlines(), 1)
                        if private_sample(line))
    if name in {"apps/console/next.config.ts", "src-tauri/src/channel.rs"} and ".supymem.com" in text:
        findings.append((name, 1, "public-hosted-routing"))
    if name == "config/hosts.json":
        try:
            hosts = json.loads(text)
        except ValueError:
            hosts = None
        if not isinstance(hosts, dict) or hosts.keys() != {"local"} or not isinstance(hosts["local"], dict):
            findings.append((name, 1, "public-nonlocal-host-config"))
    for rule in rules:
        if "path" in rule and not _matches([rule["path"]], name):
            continue
        for match in re.finditer(rule["regex"], text):
            line_start = text.rfind("\n", 0, match.start()) + 1
            line_end = text.find("\n", match.end())
            line = text[line_start:line_end if line_end >= 0 else len(text)]
            if not _allowed(rule, name, match.group(), line):
                findings.append((name, text.count("\n", 0, match.start()) + 1, rule["id"]))
    return findings


def _git(root: Path, *arguments: str) -> bytes:
    return subprocess.run(["git", "-C", str(root), *arguments], check=True,
                          capture_output=True).stdout


def _gitleaks(scanner: str, mode: str, source: Path, config: Path, report: Path) -> set[tuple[str, int, str]]:
    command = [scanner, mode, ".", "--config", str(config), "--no-banner", "--redact=100",
               "--report-format=json", "--report-path", str(report),
               "--gitleaks-ignore-path=/dev/null", "--ignore-gitleaks-allow"]
    if mode == "git":
        command.append("--log-opts=--all --full-history")
    result = subprocess.run(command, cwd=source, capture_output=True, check=False)
    if result.returncode not in {0, 1}:
        raise RuntimeError("Gitleaks failed. Install Gitleaks 8.30 or later and rerun scripts/check_public.sh.")
    if not report.exists():
        raise RuntimeError("Gitleaks produced no report. Repair the scanner and rerun scripts/check_public.sh.")
    records = json.loads(report.read_text())
    if not isinstance(records, list) or (result.returncode == 1 and not records):
        raise RuntimeError("Gitleaks produced an invalid report. Repair the scanner and rerun scripts/check_public.sh.")
    return {(str(record["File"]), int(record["StartLine"]), str(record["RuleID"])) for record in records}


# shortcut: local asset URLs are literals or finite artwork lists; use a build manifest if URLs become data-driven.
_ASSET = re.compile(
    r"(?:[\"'`]|url\(\s*)/(?P<path>(?:[\w./@-]|\$\{[^}\n]+\})+\."
    r"(?:avif|gif|glb|html|ico|jpe?g|json|mp3|mp4|ogg|otf|pdf|png|svg|ttf|txt|wasm|wav|webm|webmanifest|webp|woff2?|xml))(?=[\"'`?#)])"
)
_ARTWORK_LISTS = {
    "venture-cards/${art}.png": ("components/ventures/ventureCards.ts", r"CARD_ART = \[([^\]]+)\]"),
    "connections/${CONNECTION_LOGOS[id]}.svg": ("components/connections/ConnectionsHub.tsx", r"CONNECTION_LOGOS:.*?= \{([^}]+)\}"),
    "toolkit/${id}.svg": ("components/office/CompanyHome.tsx", r"styles\.toolkits\}>\{\[([^\]]+)\]"),
}
# These rights-withheld assets already have no-image or still-image paths.
_OPTIONAL_ART = {
    "components/guide/CapabilityGuide.tsx": {"guide/hero.webp", "guide/${step.image}.webp", "guide/${story.image}.webp", "guide/${card.image}.webp"},
    "lib/mascotPlayer.js": {"charlie/mascot_animations.glb"},
    "components/desktop/CharlieIntro.tsx": {"charlie/poster.png"},
    "components/chat/CharlieDock.tsx": {"charlie/poster.png"},
}


def artwork_paths(source: Path, template: str) -> set[str]:
    file, pattern = _ARTWORK_LISTS[template]
    values = re.search(pattern, (source / file).read_text(encoding="utf-8"))
    if not values or not (names := re.findall(r'"([\w-]+)"', values[1])):
        raise ValueError(f"Cannot resolve artwork paths in {file}.")
    return {re.sub(r"\$\{[^}]+\}", name, template) for name in names}


def check_assets(root: Path) -> int:
    source = root / "apps/console/src"
    public = root / "apps/console/public"
    missing = set()
    for file in sorted(source.rglob("*")):
        if file.suffix not in {".ts", ".tsx", ".js", ".jsx", ".css", ".html"} or ".test." in file.name:
            continue
        for number, line in enumerate(file.read_text(encoding="utf-8").splitlines(), 1):
            if line.lstrip().startswith(("//", "*")):
                continue
            for match in _ASSET.finditer(line):
                reference = match["path"]
                if reference in _OPTIONAL_ART.get(file.relative_to(source).as_posix(), ()):
                    continue
                paths = artwork_paths(source, reference) if reference in _ARTWORK_LISTS else {reference}
                missing.update((file.relative_to(root), number, asset) for asset in paths
                               if ".." in Path(asset).parts or not (public / asset).is_file())
    for file, number, asset in sorted(missing):
        print(f"Missing public asset: {file}:{number}: {asset}. Ship the asset or use a fallback.", file=sys.stderr)
    if source.is_dir() and not missing:
        print("Public console assets passed.", flush=True)
    return int(bool(missing))


def _check_imports(root: Path, function: str, label: str, *, mode: str = "local") -> int:
    site_packages = sorted({sysconfig.get_path(kind) for kind in ("purelib", "platlib")})
    try:
        with tempfile.TemporaryDirectory(prefix="public-test-imports-") as temporary:
            result = subprocess.run(
                [sys.executable, "-I", "-S", "-B", "-c", """
import runpy
import sys
sys.path[:0] = sys.argv[4:]
script, root, function = sys.argv[1:4]
check = runpy.run_path(script)
raise SystemExit(check[function](root))
""", __file__, str(root), function, *site_packages],
                cwd=root, capture_output=True, text=True, timeout=180,
                env={"PATH": os.environ.get("PATH", os.defpath), "HOME": temporary, "TMPDIR": temporary,
                     "APP_ENV": "local", "SUPYMEM_MODE": mode, "COMPANY_OS_DATA_DIR": temporary,
                     "COS_TEST_APP_REUSE": "1", "GUARDRAILS_MODE": "deterministic_only", "LLM_OFFLINE": "1",
                     "LLM_CASSETTE_MODE": "replay"},
            )
    except subprocess.TimeoutExpired:
        print(f"{label} timed out. Move blocking work out of imports, then rerun scripts/check_public.sh.", file=sys.stderr)
        return 1
    if result.returncode:
        print(result.stdout + result.stderr, file=sys.stderr, end="")
        print(f"{label} failed. Install the Public CI Python dependencies and fix the missing imports, then rerun scripts/check_public.sh.", file=sys.stderr)
    else:
        for line in result.stdout.splitlines():
            if line.startswith(f"{label} passed:"):
                print(line)
    return result.returncode


def check_app_imports(root: Path) -> int:
    if not (root / "services/company_os").is_dir():
        return 0
    return (_check_imports(root, "import_app_modules", "Public local app imports")
            | _check_imports(root, "import_app_modules", "Public hosted app imports", mode="hosted"))


def import_app_modules(directory: str) -> int:
    root = Path(directory)
    sys.path[:0] = [str(root / "services/company_os"), str(root / "packages"), directory]
    for module in ("app.main", "app.worker.main"):
        importlib.import_module(module)
    print(f"Public {os.environ['SUPYMEM_MODE']} app imports passed: app.main and app.worker.main.")
    return 0


def check_test_imports(root: Path) -> int:
    if not any((root / "tests").rglob("test_*.py")):
        return 0
    return _check_imports(root, "import_test_modules", "Public test imports")


def import_test_modules(directory: str) -> int:
    import pytest

    root = Path(directory)
    tests = root / "tests"
    packages = root / "packages"
    module_roots = {"app": root / "services/company_os", "scripts": root, "services": root, "tests": root}
    if packages.is_dir():
        module_roots.update({path.name: packages for path in packages.iterdir() if path.is_dir()})

    @cache
    def module_spec(name):
        parent, _, leaf = name.rpartition(".")
        if not parent:
            paths = [str(module_roots[leaf])]
        else:
            spec = module_spec(parent)
            paths = spec.submodule_search_locations if spec else None
        return importlib.machinery.PathFinder.find_spec(leaf, paths) if paths else None

    @cache
    def test_tree(path):
        return ast.parse(path.read_bytes(), filename=str(path))

    def empty_package(path):
        body = test_tree(path).body
        return not body or (len(body) == 1 and ast.get_docstring(test_tree(path)) is not None)

    @cache
    def runtime_imports(function):
        function = inspect.unwrap(function)
        if not inspect.isfunction(function):
            return set()
        path = Path(function.__code__.co_filename).resolve()
        if not path.is_relative_to(tests):
            return set()
        lines, start = inspect.getsourcelines(function)
        missing = set()
        for node in ast.walk(ast.parse(dedent("".join(lines)), filename=str(path))):
            if isinstance(node, ast.Import):
                names = [name.name for name in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                names = [node.module]
                if node.module.partition(".")[0] in module_roots:
                    spec = module_spec(node.module)
                    if spec and (spec.origin is None or Path(spec.origin).name == "__init__.py"
                                 and empty_package(Path(spec.origin))):
                        names.extend(f"{node.module}.{name.name}" for name in node.names if name.name != "*")
            elif (isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant)
                  and isinstance(node.args[0].value, str)
                  and ((isinstance(node.func, ast.Attribute) and node.func.attr == "import_module")
                       or (isinstance(node.func, ast.Name) and node.func.id in {"import_module", "__import__"}))):
                names = [node.args[0].value]
            else:
                continue
            missing.update((path.relative_to(root), start + node.lineno - 1, name) for name in names
                           if name.partition(".")[0] in module_roots and module_spec(name) is None)
        return missing

    # Pytest really imports test modules before marking/deselecting items. Inspect only
    # the selected bodies as well, without invoking tests or their fixtures.
    class ImportCheck:
        def pytest_collection_finish(self, session):
            missing = set()
            selected = set()
            for item in session.items:
                skipped = item.get_closest_marker("skip") or any(
                    marker.args and marker.args[0] is True for marker in item.iter_markers("skipif")
                )
                if isinstance(item, pytest.Function) and not skipped:
                    selected.add(item.path)
                    missing.update(runtime_imports(getattr(item.obj, "__func__", item.obj)))
                    for name in item.fixturenames:
                        if definitions := item._fixtureinfo.name2fixturedefs.get(name):
                            missing.update(runtime_imports(definitions[-1].func))
            if missing:
                pytest.exit("\n".join(f"Missing test import: {path}:{line}: {name}" for path, line, name in sorted(missing)), returncode=1)
            print(f"Public test imports passed: {len(selected)} selected modules, {len(session.items)} test cases checked.")

    result = pytest.main(["--collect-only", "--assert=plain", "-qq", "-p", "no:cacheprovider", "-m", "not hosted", str(tests)],
                         plugins=[ImportCheck()])
    return int(result)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-tree", action="store_true", help="Check app and test imports and console assets without scanning secrets.")
    parser.add_argument("directory", nargs="?", type=Path,
                        help="Scan every file in a history-free export; omit to also scan all Git refs.")
    arguments = parser.parse_args()
    snapshot_only = arguments.directory is not None
    root = arguments.directory.resolve() if snapshot_only else Path(__file__).resolve().parents[1]
    if arguments.check_tree:
        if not root.is_dir():
            parser.error("Choose an existing public source directory.")
        try:
            return check_assets(root) | check_app_imports(root) | check_test_imports(root)
        except (OSError, ValueError) as error:
            print(f"Cannot verify the public tree: {error} Fix the affected source and retry.", file=sys.stderr)
            return 1
    config_path = root / ".gitleaks.toml"
    findings: set[tuple[str, int, str]] = set()
    scanner_error = None
    try:
        config = tomllib.loads(config_path.read_text())
        if snapshot_only:
            if not root.is_dir() or (root / ".git").exists() or (root / ".git").is_symlink():
                raise RuntimeError("Choose a history-free export directory and rerun scripts/check_public.sh with its path.")
            names = set()
            directories = [root]
            while directories:
                for path in directories.pop().iterdir():
                    relative = path.relative_to(root)
                    if private_artifact(relative):
                        findings.add((str(relative), 1, "public-private-artifact"))
                    elif path.is_symlink():
                        findings.add((str(relative), 1, "public-symlink"))
                    elif path.is_dir():
                        directories.append(path)
                    elif path.is_file():
                        names.add(str(relative))
                    else:
                        findings.add((str(relative), 1, "public-special-file"))
        else:
            if _git(root, "rev-parse", "--is-shallow-repository").strip() != b"false":
                raise RuntimeError("History is incomplete. Fetch every release ref and rerun scripts/check_public.sh.")
            names = {os.fsdecode(name) for name in _git(root, "ls-files", "-z", "--cached", "--others",
                                                      "--exclude-standard").split(b"\0") if name}
            private_names = {name for name in names if private_artifact(Path(name))}
            findings.update((name, 1, "public-private-artifact") for name in private_names)
            names.difference_update(private_names)
        for name in sorted(names):
            path = root / name
            if path.exists() or path.is_symlink():
                findings.update(scan_file(path, name, config))
        scanner = shutil.which("gitleaks")
        if scanner is None:
            scanner_error = "Install Gitleaks 8.30 or later, then rerun scripts/check_public.sh. Default rules were not checked."
        else:
            with tempfile.TemporaryDirectory(prefix="public-release-") as temporary:
                directory = Path(temporary)
                snapshot = directory / "worktree"
                snapshot.mkdir()
                for name in names:
                    source = root / name
                    if not source.exists() and not source.is_symlink():
                        continue
                    target = snapshot / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if source.is_symlink():
                        target.write_text(str(source.readlink()))
                    else:
                        shutil.copyfile(source, target)
                modes = [("dir", snapshot)] if snapshot_only else [("dir", snapshot), ("git", root)]
                for mode, source in modes:
                    findings.update(_gitleaks(scanner, mode, source, config_path, directory / f"{mode}.json"))
    except (OSError, ValueError, KeyError, re.error, subprocess.CalledProcessError):
        scanner_error = "Scan failed. Check file permissions, Git and .gitleaks.toml, then rerun scripts/check_public.sh."
    except RuntimeError as error:
        scanner_error = str(error)
    for name, line, rule in sorted(findings):
        print(f"{rule}: {name}:{line}")
    if findings:
        print(f"Blocked: {len(findings)} findings. Resolve them in the export and all public history, then rerun.", file=sys.stderr)
    if scanner_error:
        print(scanner_error, file=sys.stderr)
    if findings or scanner_error:
        return 1 if findings else 2
    print("Public release scan passed: 0 findings. Review the checklist before publishing.", flush=True)
    return check_assets(root) | check_test_imports(root)


if __name__ == "__main__":
    raise SystemExit(main())
