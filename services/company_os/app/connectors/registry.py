"""Provider lookup. Fake adapters are the default in local mode (39 §7)."""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Any

from app.connectors.base import ConnectorBase

KNOWN = frozenset({"github", "linear"})


def connectors_mode() -> str:
    return (os.environ.get("CONNECTORS_MODE") or "local").strip().lower()


def catalog() -> list[dict[str, Any]]:
    """Founder-facing provider list: names, scopes, reasons. Never secrets."""
    items: list[dict[str, Any]] = []
    for name in sorted(KNOWN):
        man = _manifest(name)
        auth = man.get("auth") if isinstance(man.get("auth"), dict) else {}
        scopes_raw = man.get("scopes") if isinstance(man.get("scopes"), dict) else {}
        scopes = []
        for scope, spec in scopes_raw.items():
            row = spec if isinstance(spec, dict) else {}
            scopes.append(
                {
                    "scope": str(scope),
                    "reason": str(row.get("reason") or ""),
                    "allows": str(row.get("allows") or ""),
                    "excludes": str(row.get("excludes") or ""),
                }
            )
        items.append(
            {
                "provider": name,
                "auth_url": str(auth.get("url") or ""),
                "scopes": scopes,
            }
        )
    return items


@lru_cache(maxsize=8)
def get_connector(provider: str, *, simulated: bool | None = None) -> ConnectorBase:
    """Return the adapter for ``provider``. Local mode always uses fakes."""
    name = provider.strip().lower()
    if name not in KNOWN:
        raise KeyError(f"unknown provider: {provider}")
    use_fake = connectors_mode() == "local" if simulated is None else simulated
    if name == "github":
        if use_fake:
            from app.connectors.github.adapter_fake import GitHubFakeConnector

            return GitHubFakeConnector(_manifest(name))
        from app.connectors.github.adapter import GitHubConnector

        return GitHubConnector(_manifest(name), simulated=False)
    if name == "linear":
        if use_fake:
            from app.connectors.linear.adapter_fake import LinearFakeConnector

            return LinearFakeConnector(_manifest(name))
        from app.connectors.linear.adapter import LinearConnector

        return LinearConnector(_manifest(name), simulated=False)
    raise KeyError(f"unknown provider: {provider}")


def reset_registry() -> None:
    get_connector.cache_clear()


def _manifest(provider: str) -> dict[str, Any]:
    from pathlib import Path

    import yaml

    root = Path(__file__).resolve().parents[4]
    path = root / "config" / "connectors" / f"{provider}.yaml"
    if not path.exists():
        return {}
    with path.open() as f:
        return yaml.safe_load(f) or {}
