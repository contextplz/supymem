"""Every live LLM dial must go through llm_gateway and LLM_API_KEY from .env."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

# Packages / trees allowed to construct AsyncOpenAI or call chat.completions.create
ALLOWED_ASYNC_OPENAI = {
    ROOT / "packages" / "llm_gateway" / "provider_foundry.py",
    ROOT / "packages" / "llm_gateway" / "provider_openai.py",
    # agent_runner still type-hints AsyncOpenAI but must not construct it
}

ALLOWED_COMPLETIONS_CREATE = {
    ROOT / "packages" / "llm_gateway" / "client.py",
    ROOT / "packages" / "llm_gateway" / "local_transport.py",
    ROOT / "packages" / "llm_gateway" / "sole.py",
    ROOT / "packages" / "llm_gateway" / "provider_openai.py",
    # Anthropic→OpenAI façade used by QM; still dials via env LLM_* only.
    ROOT / "services" / "llm_adapter" / "llm_adapter" / "upstream.py",
}


def _py_files() -> list[Path]:
    out: list[Path] = []
    for base in (
        ROOT / "packages",
        ROOT / "services",
    ):
        if not base.exists():
            continue
        for p in base.rglob("*.py"):
            if "/.venv/" in str(p) or "__pycache__" in str(p):
                continue
            if "/tests/" in str(p) or p.name.startswith("test_"):
                continue
            out.append(p)
    return out


def test_async_openai_only_in_llm_gateway() -> None:
    offenders: list[str] = []
    for path in _py_files():
        src = path.read_text(encoding="utf-8")
        if "AsyncOpenAI(" not in src:
            continue
        if path.resolve() in {p.resolve() for p in ALLOWED_ASYNC_OPENAI}:
            continue
        # Import-only / annotation mentions are fine; construction is not.
        try:
            tree = ast.parse(src)
        except SyntaxError:
            offenders.append(f"{path}: unparseable")
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = ""
                if isinstance(node.func, ast.Name):
                    name = node.func.id
                elif isinstance(node.func, ast.Attribute):
                    name = node.func.attr
                if name == "AsyncOpenAI":
                    offenders.append(str(path.relative_to(ROOT)))
                    break
    assert not offenders, f"AsyncOpenAI() outside llm_gateway: {offenders}"


def test_chat_completions_create_only_in_llm_gateway() -> None:
    offenders: list[str] = []
    needle = "chat.completions.create"
    allowed = {p.resolve() for p in ALLOWED_COMPLETIONS_CREATE}
    for path in _py_files():
        if needle not in path.read_text(encoding="utf-8"):
            continue
        if path.resolve() in allowed:
            continue
        offenders.append(str(path.relative_to(ROOT)))
    assert not offenders, f"chat.completions.create outside llm_gateway: {offenders}"


def test_require_env_api_key_rejects_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    from llm_gateway.provider_foundry import ProviderConfig
    from llm_gateway.sole import require_env_api_key
    from pydantic import SecretStr

    monkeypatch.setenv("LLM_API_KEY", "env-key-abc")
    cfg = ProviderConfig(api_key=SecretStr("other-key-xyz"))
    with pytest.raises(RuntimeError, match="does not match LLM_API_KEY"):
        require_env_api_key(cfg)


def test_require_env_api_key_ok_when_matches(monkeypatch: pytest.MonkeyPatch) -> None:
    from llm_gateway.provider_foundry import ProviderConfig
    from llm_gateway.sole import require_env_api_key
    from pydantic import SecretStr

    monkeypatch.setenv("LLM_API_KEY", "env-key-abc")
    cfg = ProviderConfig(api_key=SecretStr("env-key-abc"))
    assert require_env_api_key(cfg) == "env-key-abc"


def test_agent_runner_delegates_to_gateway() -> None:
    src = (ROOT / "packages" / "agent_runner" / "agent_runner" / "provider.py").read_text()
    assert "llm_gateway.sole" in src or "from llm_gateway" in src
    assert "AsyncOpenAI(" not in src
