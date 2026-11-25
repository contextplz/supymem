"""Token ledger + cost accounting from pricing.yaml."""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PRICING = REPO_ROOT / "config" / "pricing.yaml"
MODEL_PRICING = Path("/workspace/config/llm_pricing.yaml")
if not MODEL_PRICING.is_file():
    MODEL_PRICING = REPO_ROOT / "config" / "llm_pricing.yaml"


@lru_cache(maxsize=2)
def load_pricing(path: str | None = None) -> dict[str, Any]:
    p = Path(path) if path else DEFAULT_PRICING
    with p.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def cost_usd(
    tier: str,
    tokens_in: int,
    tokens_out: int,
    *,
    reasoning_tokens: int = 0,
    catalog_model: str | None = None,
    cached_tokens: int = 0,
    cache_write_tokens: int = 0,
    path: str | None = None,
) -> float:
    """Live output includes reasoning; legacy tier fixtures report it separately."""
    if catalog_model is not None:
        counts = (tokens_in, tokens_out, reasoning_tokens, cached_tokens, cache_write_tokens)
        if (any(type(value) is not int or value < 0 for value in counts)
                or reasoning_tokens > tokens_out or cached_tokens + cache_write_tokens > tokens_in):
            raise ValueError("Model cost requires confirmed token subsets")
        pricing = load_pricing(path or str(MODEL_PRICING))
        models = pricing.get("models") or {}
        canonical = (pricing.get("aliases") or {}).get(catalog_model, catalog_model)
        rates = models.get(canonical)
        if rates is None:
            # Unknown catalogs reserve the maximum known rate without cache discounts.
            rates = {"input_per_mtok": max(float(row.get(key, 0)) for row in models.values()
                for key in ("input_per_mtok", "long_input_per_mtok", "cache_write_per_mtok", "long_cache_write_per_mtok")),
                "output_per_mtok": max(float(row.get(key, 0)) for row in models.values()
                    for key in ("output_per_mtok", "long_output_per_mtok"))}
        long_context = tokens_in > int(rates.get("long_context_threshold", tokens_in))
        prefix = "long_" if long_context else ""
        inp = float(rates.get(prefix + "input_per_mtok", rates.get("input_per_mtok", 0)))
        out = float(rates.get(prefix + "output_per_mtok", rates.get("output_per_mtok", 0)))
        cached = float(rates.get(prefix + "cached_input_per_mtok", inp))
        written = float(rates.get(prefix + "cache_write_per_mtok", inp))
        return ((tokens_in - cached_tokens - cache_write_tokens) * inp + cached_tokens * cached
            + cache_write_tokens * written + tokens_out * out) / 1_000_000
    tiers = (load_pricing(path).get("tiers") or {})
    rates = tiers.get(tier) or tiers.get("flash") or {"input": 0.0, "output": 0.0}
    inp = float(rates.get("input", 0)) / 1_000_000
    out = float(rates.get("output", 0)) / 1_000_000
    return tokens_in * inp + (tokens_out + reasoning_tokens) * out


@dataclass
class LedgerEntry:
    purpose: str
    tier: str
    tokens_in: int
    tokens_out: int
    reasoning_tokens: int = 0
    catalog_model: str | None = None
    cached_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0
    venture_id: str | None = None
    agent_id: str | None = None
    run_id: str | None = None


@dataclass
class TokenLedger:
    entries: list[LedgerEntry] = field(default_factory=list)

    def record(
        self,
        *,
        purpose: str,
        tier: str,
        tokens_in: int,
        tokens_out: int,
        reasoning_tokens: int = 0,
        catalog_model: str | None = None,
        cached_tokens: int = 0,
        cache_write_tokens: int = 0,
        venture_id: str | None = None,
        agent_id: str | None = None,
        run_id: str | None = None,
    ) -> LedgerEntry:
        c = cost_usd(tier, tokens_in, tokens_out, reasoning_tokens=reasoning_tokens,
            catalog_model=catalog_model, cached_tokens=cached_tokens, cache_write_tokens=cache_write_tokens)
        entry = LedgerEntry(
            purpose=purpose,
            tier=tier,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            reasoning_tokens=reasoning_tokens,
            catalog_model=catalog_model,
            cached_tokens=cached_tokens,
            cache_write_tokens=cache_write_tokens,
            cost_usd=c,
            venture_id=venture_id,
            agent_id=agent_id,
            run_id=run_id,
        )
        self.entries.append(entry)
        return entry

    def today_spend(self, venture_id: str | None = None) -> float:
        return sum(
            e.cost_usd
            for e in self.entries
            if venture_id is None or e.venture_id == venture_id
        )
