"""AST scan: nothing under services/company_os may import services.warden."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
COMPANY_OS = ROOT / "services" / "company_os"


def _imports_warden(tree: ast.AST) -> list[str]:
    hits: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.name
                if name == "warden" or name.startswith("warden.") or "services.warden" in name:
                    hits.append(name)
                if name.startswith("app.") and "warden" in name.split("."):
                    # allow app.core.warden_client — that's the HTTP client, not the service
                    if name != "app.core.warden_client" and "warden_client" not in name:
                        if "services.warden" in name or name.startswith("warden"):
                            hits.append(name)
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod.startswith("services.warden") or mod == "warden" or mod.startswith("warden."):
                hits.append(mod)
            # Absolute import of warden app package from company_os is also forbidden
            if mod.startswith("services.warden"):
                hits.append(mod)
    return hits


def test_company_os_never_imports_warden() -> None:
    violations: list[str] = []
    for path in COMPANY_OS.rglob("*.py"):
        src = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(src, filename=str(path))
        except SyntaxError:
            continue
        for hit in _imports_warden(tree):
            violations.append(f"{path.relative_to(ROOT)}: import {hit}")
        # Ban runtime imports of the warden service package (not docstrings).
        for i, line in enumerate(src.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith('"""') or stripped.startswith("'''"):
                continue
            if stripped.startswith(("from ", "import ")):
                if "services.warden" in stripped or (
                    "import warden" in stripped and "warden_client" not in stripped
                ):
                    violations.append(f"{path.relative_to(ROOT)}:{i}: {stripped}")

    assert not violations, "company_os must not import warden:\n" + "\n".join(violations)
