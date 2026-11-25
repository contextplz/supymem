"""One-click Hermes install for a non-technical founder.

We do not install Cursor, Claude Code, Codex, or OpenCode.
After an explicit button, we put Nous Hermes (MIT, ``hermes acp``) on this Mac
and drive it from the desktop app.
"""

from __future__ import annotations

from pathlib import Path
import os
import shutil


def hermes_venv_dir(data_root: Path | None = None) -> Path:
    if data_root is not None:
        return Path(data_root) / "hermes-venv"
    override = os.environ.get("COMPANY_OS_DATA_DIR", "").strip()
    if override:
        return Path(override) / "hermes-venv"
    return Path.home() / "Library" / "Application Support" / "Supymem" / "hermes-venv"


def hermes_bin(data_root: Path | None = None) -> Path:
    return hermes_venv_dir(data_root) / "bin" / "hermes"


def already_installed(path_env: str | None = None, data_root: Path | None = None) -> str | None:
    loc = shutil.which("hermes", path=path_env)
    if loc:
        return loc
    bundled = hermes_bin(data_root)
    if bundled.is_file():
        return str(bundled)
    return None


def pip_packages() -> list[str]:
    return ["hermes-agent[acp]"]


def consent_copy() -> dict[str, str]:
    return {
        "title": "Install Supymem Computer Use on this Mac",
        "body": (
            "This downloads Supymem Computer Use onto this computer "
            "and lets Supymem run it for your venture. You do not need Cursor, Claude, "
            "or a terminal. Nothing is installed until you click Install."
        ),
        "installs": "Supymem Computer Use into Supymem’s folder on this Mac",
        "does_not": "Does not install Cursor, Claude Code, Codex, or OpenCode",
    }


def hermes_config_yaml(*, base_url: str, model: str, api_key: str) -> str:
    """Hermes reads ``~/.hermes/config.yaml``. Key is ours (``sk_app_``), not the founder's."""
    try:
        from app.hermes.messaging import messaging_gateway_yaml

        gateway = messaging_gateway_yaml()
    except Exception:
        gateway = (
            "gateway:\n"
            "  platforms:\n"
            "    telegram:\n"
            "      enabled: false\n"
            "    discord:\n"
            "      enabled: false\n"
            "    slack:\n"
            "      enabled: false\n"
            "    whatsapp_cloud:\n"
            "      enabled: false\n"
        )
    return (
        "# Written by Supymem after you clicked Install. Uses workspace credits.\n"
        "model:\n"
        "  provider: custom\n"
        f"  default: {model}\n"
        f"  base_url: {base_url}\n"
        f"  api_key: {api_key}\n"
        "providers:\n"
        "  supymem:\n"
        f"    api: {base_url}\n"
        f"    base_url: {base_url}\n"
        "    key_env: SUPYMEM_AI_API_KEY\n"
        "    api_mode: chat_completions\n"
        "    transport: chat_completions\n"
        "    type: openai\n"
        f"    default_model: {model}\n"
        "    models:\n"
        "      - supymem-flash\n"
        "      - supymem-pro\n"
        f"{gateway}"
    )


def hermes_env_file(*, base_url: str, api_key: str) -> str:
    # Only the metered proxy key. Platform Azure / messaging secrets stay on the server.
    return (
        f"SUPYMEM_AI_API_KEY={api_key}\n"
        f"OPENAI_API_KEY={api_key}\n"
        f"OPENAI_BASE_URL={base_url}\n"
    )
