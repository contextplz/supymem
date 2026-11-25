"""Import externally built code (docs/supymem/13 §10). No fake success without a report."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from app.config import data_root
from core.ids import new_id

_SECRET_HINTS = re.compile(
    r"(aws_secret_access_key|sk_live_|ghp_[A-Za-z0-9]|BEGIN (RSA |OPENSSH )?PRIVATE KEY)",
    re.I,
)

_UNSUPPORTED = {
    "ruby": "Ruby on Rails isn't supported for hosting yet; we can deploy it to your own AWS account.",
    "rails": "Ruby on Rails isn't supported for hosting yet; we can deploy it to your own AWS account.",
    "php": "PHP apps aren't hosted on Supymem in v1; import as a reference or use external_aws.",
    "java": "JVM apps aren't hosted on Supymem in v1; we can keep the repo as an existing asset.",
}


def _walk(root: Path) -> list[Path]:
    skip = {".git", "node_modules", ".next", "dist", "__pycache__", ".venv"}
    out: list[Path] = []
    if not root.is_dir():
        return out
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip]
        for name in filenames:
            out.append(Path(dirpath) / name)
    return out


def _detect_stack(files: list[Path], names: set[str]) -> dict[str, Any]:
    stack = "unknown"
    package_manager = None
    build = None
    run = None
    if "package.json" in names:
        stack = "nextjs" if any("next" in n for n in names) or _file_contains(files, "next") else "node"
        package_manager = "pnpm" if "pnpm-lock.yaml" in names else "npm"
        build = f"{package_manager} run build"
        run = f"{package_manager} start"
    if "pyproject.toml" in names or "requirements.txt" in names:
        stack = "fastapi" if _file_contains(files, "fastapi") else "python"
        package_manager = "pip"
        build = "pip install -e ."
        run = "uvicorn app.main:app --host 0.0.0.0 --port $PORT"
    if "Gemfile" in names:
        stack = "rails"
        package_manager = "bundler"
        build = "bundle install"
        run = "rails server"
    if "go.mod" in names:
        stack = "go"
        package_manager = "go"
        build = "go build ./..."
        run = "./app"
    dockerfile = "Dockerfile" in names
    tests = any(n.startswith("test") or n.endswith("_test.py") or n == "pytest.ini" for n in names)
    env_example = ".env.example" in names
    return {
        "stack": stack,
        "package_manager": package_manager,
        "build": build,
        "run": run,
        "dockerfile": dockerfile,
        "tests_present": tests,
        "env_example": env_example,
    }


def _file_contains(files: list[Path], needle: str) -> bool:
    for path in files:
        if path.suffix not in {".json", ".toml", ".js", ".ts", ".py", ".md", ""}:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")[:8000]
        except OSError:
            continue
        if needle.lower() in text.lower():
            return True
    return False


def analyze_tree(root: Path) -> dict[str, Any]:
    files = _walk(root)
    names = {p.name for p in files}
    detected = _detect_stack(files, names)
    secrets: list[str] = []
    env_needed: list[str] = []
    size = 0
    license_name = None
    for path in files:
        try:
            data = path.read_bytes()
        except OSError:
            continue
        size += len(data)
        if len(data) > 200_000:
            continue
        text = data.decode("utf-8", errors="ignore")
        if _SECRET_HINTS.search(text) and path.name != ".env.example":
            secrets.append(str(path.relative_to(root)))
        if path.name in {".env.example", ".env.sample"}:
            for line in text.splitlines():
                if "=" in line and not line.strip().startswith("#"):
                    env_needed.append(line.split("=", 1)[0].strip())
        if path.name.upper().startswith("LICENSE"):
            license_name = path.name
    unsupported = None
    for key, msg in _UNSUPPORTED.items():
        if key in detected["stack"]:
            unsupported = msg
            break
    ports = [3000] if detected["stack"] in {"nextjs", "node"} else [8000]
    return {
        "stack": detected["stack"],
        "package_manager": detected["package_manager"],
        "build": detected["build"],
        "run": detected["run"],
        "ports": ports,
        "env_vars_needed": sorted(set(env_needed)),
        "db_usage": "postgres" if _file_contains(files, "postgres") or _file_contains(files, "prisma") else None,
        "external_services": [],
        "tests_present": detected["tests_present"],
        "dockerfile": detected["dockerfile"],
        "license": license_name,
        "secrets_committed": secrets,
        "size_bytes": size,
        "file_count": len(files),
        "unsupported_message": unsupported,
        "blocker": bool(secrets),
        "proposed_plan": [
            {
                "milestone": "import",
                "goal": "Make it build in the sandbox, add SUPYMEM.md and a health endpoint.",
            },
            {
                "milestone": "deploy",
                "goal": "Recommend a micro tier unless the report says otherwise.",
            },
        ],
    }


def presign_upload(filename: str, size_bytes: int) -> dict[str, Any]:
    if size_bytes > 200 * 1024 * 1024:
        raise ValueError("upload_too_large")
    upload_id = new_id("upl")
    dest = data_root() / "uploads" / upload_id
    dest.mkdir(parents=True, exist_ok=True)
    target = dest / (Path(filename).name or "bundle")
    target.write_bytes(b"")
    return {
        "upload_id": upload_id,
        "method": "PUT",
        "url": str(target),
        "adapter": "local",
        "expires_in": 3600,
    }
