"""House-style hints extracted deterministically from README/files (38 §6)."""

from __future__ import annotations

import re
from typing import Any


def extract_house_style(files: list[dict[str, Any]]) -> dict[str, Any]:
    """Derive simple, frozen style hints. No model calls."""
    texts = {
        str(f.get("path") or "").lower(): str(f.get("content") or "") for f in files
    }
    joined = "\n".join(texts.values()).lower()
    style: dict[str, Any] = {
        "test_framework": _guess_tests(joined, texts),
        "naming": _guess_naming(joined),
        "commit_format": _guess_commits(joined),
        "docstrings": "unknown",
        "forbidden": [],
        "evidence": [p for p in texts if texts[p]],
        "confidence": 0.55,
    }
    if "pytest" in style["test_framework"]:
        style["confidence"] = 0.75
    if style["commit_format"] == "conventional":
        style["confidence"] = max(float(style["confidence"]), 0.7)
    return style


def _guess_tests(joined: str, texts: dict[str, str]) -> str:
    if "pytest" in joined or "tests/" in joined:
        return "pytest"
    if "jest" in joined or "vitest" in joined:
        return "jest"
    if "go test" in joined:
        return "go_test"
    pyproject = texts.get("pyproject.toml", "")
    if "pytest" in pyproject:
        return "pytest"
    return "unknown"


def _guess_naming(joined: str) -> str:
    if "snake_case" in joined or "python" in joined:
        return "snake_case functions, PascalCase classes"
    if "camelcase" in joined or "typescript" in joined or "javascript" in joined:
        return "camelCase functions, PascalCase classes"
    return "unknown"


def _guess_commits(joined: str) -> str:
    if re.search(r"\b(feat|fix|chore|docs)!:", joined) or "conventional commit" in joined:
        return "conventional"
    if "conventional" in joined:
        return "conventional"
    return "unknown"
