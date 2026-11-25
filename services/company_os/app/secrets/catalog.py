"""Fixed secret-slot catalog. AI names a slot; it never invents a value field."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from app.config import REPO_ROOT

ENV_KEY_RE = r"^[A-Z][A-Z0-9_]{1,63}$"


@dataclass(frozen=True)
class SlotSpec:
    id: str
    group: str
    provider: str
    label: str
    env_keys: tuple[str, ...]
    why: str
    help_url: str = ""
    prefix: tuple[str, ...] = ()

    def form(self) -> dict[str, Any]:
        return {
            "slot_id": self.id,
            "label": self.label,
            "provider": self.provider,
            "why": self.why,
            "help_url": self.help_url,
            "fields": [
                {
                    "env_key": key,
                    "label": key.replace("_", " ").title(),
                    "input": "password",
                }
                for key in self.env_keys
            ],
        }

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "group": self.group,
            "provider": self.provider,
            "label": self.label,
            "env_refs": list(self.env_keys),
            "why": self.why,
            "help_url": self.help_url,
        }


@lru_cache(maxsize=1)
def _raw() -> dict[str, SlotSpec]:
    path = REPO_ROOT / "config" / "secret_slots.yaml"
    data = yaml.safe_load(path.read_text()) if path.exists() else {}
    out: dict[str, SlotSpec] = {}
    for row in data.get("slots") or []:
        keys = tuple(str(k) for k in (row.get("env_keys") or []) if str(k).strip())
        if not keys:
            continue
        spec = SlotSpec(
            id=str(row["id"]),
            group=str(row.get("group") or "other"),
            provider=str(row.get("provider") or "other"),
            label=str(row.get("label") or row["id"]),
            env_keys=keys,
            why=str(row.get("why") or ""),
            help_url=str(row.get("help_url") or ""),
            prefix=tuple(str(p) for p in (row.get("prefix") or [])),
        )
        out[spec.id] = spec
    return out


def all_slots() -> list[SlotSpec]:
    return list(_raw().values())


def get_slot(slot_id: str) -> SlotSpec | None:
    return _raw().get((slot_id or "").strip())


def slot_for_provider(provider: str) -> SlotSpec | None:
    name = (provider or "").strip().lower()
    if not name:
        return None
    for spec in _raw().values():
        if spec.provider == name or spec.id == name:
            return spec
    aliases = {
        "openai": "openai.api_key",
        "anthropic": "anthropic.api_key",
        "google": "google.api_key",
        "deepseek_direct": "deepseek.api_key",
        "deepseek": "deepseek.api_key",
        "xai": "xai.api_key",
        "openrouter": "openrouter.api_key",
        "azure_foundry": "azure.api_key",
        "zernio": "zernio.api_key",
        "social": "zernio.api_key",
        "github": "github.pat",
        "linear": "linear.api_key",
        "stripe": "stripe.secret_key",
        "razorpay": "razorpay.key_secret",
        "aws": "aws.credentials",
        "payments": "stripe.secret_key",
    }
    return get_slot(aliases.get(name, ""))


def slot_for_env_key(env_key: str) -> SlotSpec | None:
    key = (env_key or "").strip()
    for spec in _raw().values():
        if key in spec.env_keys:
            return spec
    return None


def guess_slot_from_prefix(text: str) -> SlotSpec | None:
    blob = (text or "").strip()
    if not blob:
        return None
    hits: list[SlotSpec] = []
    for spec in _raw().values():
        for prefix in spec.prefix:
            if blob.startswith(prefix):
                hits.append(spec)
                break
    if len(hits) == 1:
        return hits[0]
    # sk_ is shared by OpenAI, Stripe live, Zernio — prefer stripe/openai by next token.
    if blob.startswith("sk-ant-"):
        return get_slot("anthropic.api_key")
    if blob.startswith("sk_test_") or blob.startswith("sk_live_"):
        return get_slot("stripe.secret_key")
    if blob.startswith("sk-"):
        return get_slot("openai.api_key")
    if blob.startswith("sk_") or blob.startswith("zn_"):
        return get_slot("zernio.api_key")
    return hits[0] if hits else None


def reset_catalog() -> None:
    _raw.cache_clear()
