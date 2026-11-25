"""Provider data-retention positions from config/providers.yaml (43 §6).

`no_train_asserted` is read, never inferred. An unlisted provider resolves to the
honest fallback — false, with a string that says we have not checked — because
asserting a guarantee we cannot substantiate is the one failure this whole
surface cannot survive.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
CONFIG_PATH = REPO_ROOT / "config" / "providers.yaml"


@dataclass(frozen=True)
class ProviderPosition:
    key: str
    label_prefix: str
    display_name: str
    no_train_asserted: bool
    retention: str
    source_url: str | None
    date_checked: str | None
    ui_string: str

    def to_dict(self) -> dict[str, object]:
        return {
            "key": self.key,
            "display_name": self.display_name,
            "no_train_asserted": self.no_train_asserted,
            "retention": self.retention,
            "source_url": self.source_url,
            "date_checked": self.date_checked,
            "ui_string": self.ui_string,
        }


def _coerce(key: str, raw: dict) -> ProviderPosition:
    asserted = bool(raw.get("no_train_asserted", False))
    source = raw.get("source_url") or None
    checked = raw.get("date_checked") or None
    # A claim without a link and a date is not a claim we get to make.
    if asserted and not (source and checked):
        asserted = False
    return ProviderPosition(
        key=key,
        label_prefix=str(raw.get("label_prefix") or key),
        display_name=str(raw.get("display_name") or key),
        no_train_asserted=asserted,
        retention=str(raw.get("retention") or "undocumented"),
        source_url=str(source) if source else None,
        date_checked=str(checked) if checked else None,
        ui_string=" ".join(str(raw.get("ui_string") or "").split()),
    )


@functools.lru_cache(maxsize=1)
def load() -> dict[str, ProviderPosition]:
    import yaml

    with CONFIG_PATH.open() as handle:
        raw = yaml.safe_load(handle) or {}
    out = {
        key: _coerce(key, value or {})
        for key, value in (raw.get("providers") or {}).items()
    }
    out["__unknown__"] = _coerce("__unknown__", raw.get("unknown_provider") or {})
    return out


def fallback() -> ProviderPosition:
    return load()["__unknown__"]


def position_for(provider_label: str) -> ProviderPosition:
    """Resolve a `<prefix>:<deployment>` ledger label to a recorded position."""
    prefix = (provider_label or "").partition(":")[0]
    for key, position in load().items():
        if key == "__unknown__":
            continue
        if prefix in {key, position.label_prefix}:
            return position
    return fallback()


def no_train_asserted(provider_label: str) -> bool:
    return position_for(provider_label).no_train_asserted


__all__ = [
    "ProviderPosition",
    "fallback",
    "load",
    "no_train_asserted",
    "position_for",
]
