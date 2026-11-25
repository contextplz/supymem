"""One estimator per meter."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from app.estimate.model import EstimateLine, ResourceEstimate

_REPO = Path(__file__).resolve().parents[4]


@lru_cache(maxsize=1)
def load_meters() -> dict[str, Any]:
    return yaml.safe_load((_REPO / "config" / "credit_meters.yaml").read_text()) or {}


@lru_cache(maxsize=1)
def load_estimation() -> dict[str, Any]:
    return yaml.safe_load((_REPO / "config" / "estimation.yaml").read_text()) or {}


def meter_micro(name: str) -> int:
    meters = load_meters().get("meters") or {}
    spec = meters.get(name) or {}
    return int(spec.get("micro") or 0)


def build_estimate(plan: dict[str, Any] | None = None) -> list[EstimateLine]:
    cfg = load_estimation()
    tasks = list((plan or {}).get("tasks") or [{"kind": "default", "complexity": "moderate"}])
    seconds_map = cfg.get("task_kind_seconds") or {}
    complexity = cfg.get("complexity") or {}
    total_s = 0.0
    for task in tasks:
        base = float(seconds_map.get(task.get("kind") or "default") or seconds_map.get("default") or 120)
        factor = float(complexity.get(task.get("complexity") or "moderate") or 1.0)
        total_s += base * factor
    micro = int(round(total_s * meter_micro("build_seconds")))
    return [
        EstimateLine(
            meter="build_seconds",
            quantity=total_s,
            micro=micro,
            confidence=0.4,
            basis="task_kind × complexity (estimation.yaml)",
        )
    ]


def research_estimate(dossier_kind: str = "recon", *, budget_calls: int = 8) -> list[EstimateLine]:
    calls = 3 if dossier_kind == "recon" else budget_calls
    micro = calls * meter_micro("research_call")
    return [
        EstimateLine(
            meter="research_call",
            quantity=calls,
            micro=micro,
            confidence=0.7,
            basis=f"{dossier_kind} budget_calls={calls}",
        )
    ]


def infra_estimate(tier: str = "micro", months: int = 1) -> list[EstimateLine]:
    from pathlib import Path

    raw = yaml.safe_load((Path(_REPO) / "config" / "infra_tiers.yaml").read_text()) or {}
    row = next((t for t in (raw.get("tiers") or []) if t.get("name") == tier or t.get("id") == tier), None)
    monthly = int((row or {}).get("monthly_credits") or 900)
    micro = monthly * 10_000 * months
    return [
        EstimateLine(
            meter="compute_seconds",
            quantity=months,
            micro=micro,
            confidence=0.6,
            basis=f"infra_tiers.{tier} monthly_credits",
        )
    ]


def ai_proxy_estimate(plan: dict[str, Any] | None = None) -> list[EstimateLine]:
    reqs = int((plan or {}).get("requests_per_day") or 50)
    micro = int(reqs * 30 * meter_micro("requests") / 10000)
    return [
        EstimateLine(
            meter="requests",
            quantity=reqs * 30,
            micro=max(micro, meter_micro("requests")),
            confidence=0.3,
            basis="first-user target × 30 days",
        )
    ]


def third_party_estimate(dossier: dict[str, Any] | None = None) -> list[EstimateLine]:
    services = list((dossier or {}).get("services") or [])
    if not services:
        return []
    return [
        EstimateLine(
            meter="third_party",
            quantity=len(services),
            micro=0,
            confidence=0.5,
            basis="paid to provider, not credits",
        )
    ]


def offsets_estimate(existing_assets: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    offsets: list[dict[str, Any]] = []
    for asset in existing_assets or []:
        kind = str(asset.get("kind") or asset.get("type") or "")
        if kind in {"llm_key", "byok"}:
            offsets.append(
                {
                    "asset": kind,
                    "credits_saved": 0,
                    "note": "Founder LLM key → ai_proxy line is 0",
                    "applies_to": "ai_proxy",
                }
            )
        elif kind in {"cursor", "coding_agent", "founder_owned"}:
            offsets.append(
                {
                    "asset": kind,
                    "credits_saved": 0,
                    "note": "Founder-owned coding agent saves ~15% of build credits",
                    "applies_to": "build_seconds",
                    "factor": 0.15,
                }
            )
        elif kind in {"aws_credits", "free_tier"}:
            offsets.append(
                {
                    "asset": kind,
                    "credits_saved": 0,
                    "note": asset.get("note") or "Covered by founder AWS credits / free tier",
                    "applies_to": "compute_seconds",
                }
            )
    return offsets


def compose_estimate(
    *,
    plan: dict[str, Any] | None = None,
    dossier_kind: str = "deep",
    tier: str = "micro",
    existing_assets: list[dict[str, Any]] | None = None,
) -> ResourceEstimate:
    lines = [
        *build_estimate(plan),
        *research_estimate(dossier_kind),
        *infra_estimate(tier),
        *ai_proxy_estimate(plan),
        *third_party_estimate(plan),
    ]
    offsets = offsets_estimate(existing_assets)
    gross = sum(l.micro for l in lines if l.meter != "third_party")
    saved = 0
    for off in offsets:
        factor = float(off.get("factor") or 0)
        applies = off.get("applies_to")
        if factor and applies:
            part = sum(l.micro for l in lines if l.meter == applies)
            cut = int(part * factor)
            off["credits_saved"] = cut // 10_000
            saved += cut
        elif applies == "ai_proxy" or applies == "requests":
            part = sum(l.micro for l in lines if l.meter == "requests")
            off["credits_saved"] = part // 10_000
            saved += part
    monthly = sum(l.micro for l in lines if l.meter in {"compute_seconds", "requests", "db_gb_hours"})
    return ResourceEstimate(
        lines=lines,
        offsets=offsets,
        gross_upfront_micro=gross,
        total_upfront_micro=max(0, gross - saved),
        first_month_run_micro=monthly,
        monthly_run_micro=monthly,
    )
