"""Packaged desktop trees must not ship platform LLM_* secrets."""

from __future__ import annotations

import ast
from pathlib import Path
import runpy
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
requires_frozen_spec = pytest.mark.skipif(
    not (ROOT / "packages/desktop_runtime/companyos-runtime.spec").is_file(),
    reason="Hosted frozen build spec is not in the public local release",
)

BUNDLE_TREES = (
    ROOT / "src-tauri",
    ROOT / "packages" / "desktop_runtime",
    ROOT / "apps" / "desktop",
)

ENV_FILENAMES = {".env", ".env.local", ".env.production", ".env.development"}
ASSIGNMENT = ("LLM_API_KEY=", "AZURE_API_KEY=")
DOTENV_EXCLUDES = ["**/.env", "**/.env.*"]


def _trusted_playwright_data(data):
    if (not isinstance(data, ast.Call) or not isinstance(data.func, ast.Name)
            or data.func.id != "collect_data_files" or len(data.args) != 1
            or not isinstance(data.args[0], ast.Constant) or data.args[0].value != "playwright"
            or len(data.keywords) != 1 or data.keywords[0].arg != "excludes"):
        return False
    try:
        return ast.literal_eval(data.keywords[0].value) == DOTENV_EXCLUDES
    except (ValueError, TypeError):
        return False


def _final_data_filter(tree):
    return next(node for node in tree.body if isinstance(node, ast.Assign)
                and len(node.targets) == 1 and isinstance(node.targets[0], ast.Attribute)
                and isinstance(node.targets[0].value, ast.Name) and node.targets[0].value.id == "a"
                and node.targets[0].attr == "datas")


def test_no_dotenv_files_in_desktop_packaging_trees() -> None:
    found: list[str] = []
    for tree in BUNDLE_TREES:
        if not tree.exists():
            continue
        for path in tree.rglob("*"):
            if not path.is_file():
                continue
            if path.name in ENV_FILENAMES:
                found.append(str(path.relative_to(ROOT)))
    assert not found, f"dotenv files must not ship in the desktop bundle: {found}"


def test_tauri_resources_do_not_embed_platform_keys() -> None:
    resources = ROOT / "src-tauri" / "resources"
    if not resources.exists():
        return
    offenders: list[str] = []
    for path in resources.rglob("*"):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for needle in ASSIGNMENT:
            if needle in text:
                offenders.append(str(path.relative_to(ROOT)))
                break
    assert not offenders, f"LLM_* assignment in tauri resources: {offenders}"


@requires_frozen_spec
def test_pyinstaller_spec_does_not_collect_dotenv() -> None:
    spec = (ROOT / "packages" / "desktop_runtime" / "companyos-runtime.spec").read_text(encoding="utf-8")
    tree = ast.parse(spec)
    analysis = next(node for node in ast.walk(tree)
                    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "Analysis")
    data = next(keyword.value for keyword in analysis.keywords if keyword.arg == "datas")
    assert _trusted_playwright_data(data)
    approved_exclusion_nodes = {id(node) for node in ast.walk(data.keywords[0].value)}
    final_filter = _final_data_filter(tree)
    assert isinstance(final_filter.value, ast.ListComp)
    exe = next(node for node in ast.walk(tree) if isinstance(node, ast.Call)
               and isinstance(node.func, ast.Name) and node.func.id == "EXE")
    assert analysis.end_lineno < final_filter.lineno < exe.lineno
    approved_exclusion_nodes.update(id(node) for node in ast.walk(final_filter))
    # Reading signing configuration from os.environ is not a dotenv file read.
    assert not any(isinstance(node, ast.Constant) and isinstance(node.value, str)
                   and Path(node.value).name in ENV_FILENAMES and id(node) not in approved_exclusion_nodes
                   for node in ast.walk(tree))


@pytest.mark.parametrize("expression", [
    'collect_data_files("playwright")',
    'collect_data_files("playwright", excludes=[".env", ".env.*"])',
    'collect_data_files("playwright", excludes=["**/.env"])',
    'collect_data_files("other", excludes=["**/.env", "**/.env.*"])',
    'collect_data_files("playwright", excludes=["**/.env", "**/.env.*"], include_py_files=True)',
    'collect_data_files("playwright", excludes=["**/.env", "**/.env.*"]) + [(".env", ".")]',
])
def test_packaging_assertion_rejects_broader_or_extra_data_collections(expression):
    assert not _trusted_playwright_data(ast.parse(expression, mode="eval").body)


@requires_frozen_spec
def test_pinned_pyinstaller_excludes_root_and_nested_dotenv_but_keeps_driver_data(tmp_path, monkeypatch):
    hooks = pytest.importorskip("PyInstaller.utils.hooks")  # Existing desktop build tool, absent from generic CI venvs.
    playwright = pytest.importorskip("playwright")
    package = tmp_path / "playwright"
    package.mkdir()
    (package / "__init__.py").write_text("")
    kept = ["driver/node", "driver/package/cli.js", "driver/package/browsers.json"]
    excluded = [".env", ".env.local", "driver/.env", "driver/deep/.env.credentials",
                "driver/.env.private/secret.txt"]
    for name in kept + excluded:
        path = package / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("synthetic fixture only")
    monkeypatch.setattr(hooks, "get_all_package_paths", lambda name: [str(package)])
    datas = hooks.collect_data_files("playwright", excludes=DOTENV_EXCLUDES)
    assert {Path(source).relative_to(package).as_posix() for source, _destination in datas} == set(kept)
    # Execute both actual pinned SDK hooks with synthetic package data, then
    # the actual spec's final guard. No Analysis, freeze or SDK files change.
    spec = ast.parse((ROOT / "packages/desktop_runtime/companyos-runtime.spec").read_text())
    guard = ast.Module(body=[_final_data_filter(spec)], type_ignores=[])
    for name in ("sync_api", "async_api"):
        hook = Path(playwright.__file__).parent / "_impl/__pyinstaller" / f"hook-playwright.{name}.py"
        hook_data = runpy.run_path(str(hook))["datas"]
        assert len(hook_data) > len(kept)
        toc = [(str(Path(destination) / Path(source).name), source, "DATA") for source, destination in hook_data]
        scope = {"a": SimpleNamespace(datas=toc), "Path": Path}
        exec(compile(guard, "actual-spec-data-filter", "exec"), scope)
        assert {Path(source).relative_to(package).as_posix() for _dest, source, _kind in scope["a"].datas} == set(kept)


def test_packaged_supervisor_uses_isolated_hosted_bridge_environment() -> None:
    src = (ROOT / "src-tauri" / "src" / "supervisor.rs").read_text(encoding="utf-8")
    bridge_env = src.split("fn apply_bridge_env(", 1)[1].split("fn apply_child_env(", 1)[0]
    assert "cmd.env_clear()" in bridge_env
    assert "dotenv" not in bridge_env.split("// Clear inherited", 1)[-1].split("cmd.env_clear()", 1)[-1]
    assert 'cmd.env("BRIDGE_TOKEN"' in bridge_env
    assert 'cmd.env("SUPYMEM_MODE", "hosted")' in bridge_env
    assert "is_packaged_macos_app" in src
    # The Rust process-based environment test verifies the actual child state.
    assert "hosted_bridge_does_not_inherit_secrets_or_repo_pythonpath" in src
