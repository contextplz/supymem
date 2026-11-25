"""Layer 6 — vault reference check (43 §5.6).

Catches secrets we minted ourselves, whose shape no pattern knows. The vault
adapter differs per tenancy mode, so values arrive through a registered provider
rather than an import: this layer never reaches into a keychain itself.
"""

from __future__ import annotations

from typing import Callable, Iterable

from app.boundary import DataClass, Finding, Segment

DETECTOR = "vault"
# Shorter values would match inside ordinary prose and turn every prompt into a
# false positive without protecting anything a pattern misses.
MIN_LENGTH = 8

ValueProvider = Callable[[str | None], Iterable[str]]

_providers: list[ValueProvider] = []


def register_value_provider(provider: ValueProvider) -> None:
    _providers.append(provider)


def clear_value_providers() -> None:
    _providers.clear()


def known_values(workspace_id: str | None = None) -> tuple[str, ...]:
    values: list[str] = []
    for provider in _providers:
        try:
            values.extend(provider(workspace_id))
        except Exception:  # noqa: BLE001 — an unreadable vault must not open the boundary
            continue
    return tuple(sorted({v for v in values if v and len(v) >= MIN_LENGTH}))


def scan(segment: Segment, *, workspace_id: str | None = None) -> list[Finding]:
    text = segment.text
    if not text:
        return []
    return [
        Finding(
            detector=DETECTOR,
            rule="vault_reference",
            data_class=DataClass.SECRET,
            confidence=1.0,
            value=value,
            label=segment.display(),
        )
        for value in known_values(workspace_id)
        if value in text
    ]
