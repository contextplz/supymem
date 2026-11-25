"""Estimate LLM spend from token usage + config/llm_pricing.yaml.

Every live call is recorded — platform key *and* founder BYOK. BYOK does not
skip the meter: we still store tokens and an estimated USD cost so the console
can show what their key consumed. The founder pays the provider; we do not
invent a second invoice.
"""

from __future__ import annotations

import calendar
import json
import logging
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.config import REPO_ROOT, data_root

log = logging.getLogger(__name__)
_pg_engine: Engine | None = None
_pg_ready = False

_lock = threading.Lock()
_pricing_cache: dict[str, Any] | None = None


def _pricing_path() -> Path:
    return REPO_ROOT / "config" / "llm_pricing.yaml"


def _usage_path() -> Path:
    root = data_root()
    root.mkdir(parents=True, exist_ok=True)
    return root / "llm_usage.json"


def _source_of(purpose: str, source: str | None) -> str:
    raw = (source or "").strip().lower()
    if raw in {"byok", "platform"}:
        return raw
    return "byok" if "byok" in (purpose or "").lower() else "platform"


def _pg_url() -> str:
    from app.db.session import resolve_database_url

    return resolve_database_url()


def _postgres_engine() -> Engine | None:
    """Hosted: durable ledger. Local SQLite tests keep using llm_usage.json."""
    global _pg_engine, _pg_ready
    url = _pg_url()
    if "postgres" not in url.lower():
        return None
    from app.state import db

    try:
        engine = db().kw["bind"]
    except Exception:
        log.exception("llm usage table unavailable; JSON ledger only")
        return None
    if _pg_engine is not engine:
        _pg_engine = engine
        _pg_ready = False
    if not _pg_ready:
        try:
            with _pg_engine.begin() as conn:
                conn.execute(
                    text(
                        """
                        CREATE TABLE IF NOT EXISTS llm_usage_events (
                            id TEXT PRIMARY KEY,
                            workspace_id TEXT NOT NULL,
                            at TIMESTAMPTZ NOT NULL,
                            model TEXT,
                            purpose TEXT,
                            source TEXT NOT NULL DEFAULT 'platform',
                            prompt_tokens INTEGER NOT NULL DEFAULT 0,
                            completion_tokens INTEGER NOT NULL DEFAULT 0,
                            cached_tokens INTEGER NOT NULL DEFAULT 0,
                            reasoning_tokens INTEGER NOT NULL DEFAULT 0,
                            estimate_usd DOUBLE PRECISION NOT NULL DEFAULT 0
                        )
                        """
                    )
                )
                conn.execute(
                    text(
                        "CREATE INDEX IF NOT EXISTS llm_usage_events_ws_at "
                        "ON llm_usage_events (workspace_id, at)"
                    )
                )
            _pg_ready = True
        except Exception:
            log.exception("llm usage table unavailable; JSON ledger only")
            return None
    return _pg_engine


def _record_postgres(run: dict[str, Any], workspace_id: str) -> None:
    engine = _postgres_engine()
    if engine is None:
        return
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO llm_usage_events (
                        id, workspace_id, at, model, purpose, source,
                        prompt_tokens, completion_tokens, cached_tokens,
                        reasoning_tokens, estimate_usd
                    ) VALUES (
                        :id, :workspace_id, :at, :model, :purpose, :source,
                        :prompt_tokens, :completion_tokens, :cached_tokens,
                        :reasoning_tokens, :estimate_usd
                    )
                    """
                ),
                {
                    "id": str(uuid4()),
                    "workspace_id": workspace_id,
                    "at": run["at"],
                    "model": run.get("model"),
                    "purpose": run.get("purpose") or "",
                    "source": run.get("source") or "platform",
                    "prompt_tokens": int(run.get("prompt_tokens") or 0),
                    "completion_tokens": int(run.get("completion_tokens") or 0),
                    "cached_tokens": int(run.get("cached_tokens") or 0),
                    "reasoning_tokens": int(run.get("reasoning_tokens") or 0),
                    "estimate_usd": float(run.get("estimate_usd") or 0),
                },
            )
    except Exception:
        log.exception("failed to persist llm usage event")


def _pg_buckets(workspace_id: str | None) -> dict[str, Any] | None:
    engine = _postgres_engine()
    if engine is None:
        return None
    wid = (workspace_id or "").strip() or None
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    try:
        with engine.connect() as conn:
            if wid:
                rows = conn.execute(
                    text(
                        """
                        SELECT source,
                               SUM(prompt_tokens), SUM(completion_tokens),
                               SUM(cached_tokens), SUM(reasoning_tokens),
                               SUM(estimate_usd), COUNT(*),
                               SUM(CASE WHEN to_char(at AT TIME ZONE 'UTC', 'YYYY-MM') = :month
                                        THEN prompt_tokens ELSE 0 END),
                               SUM(CASE WHEN to_char(at AT TIME ZONE 'UTC', 'YYYY-MM') = :month
                                        THEN completion_tokens ELSE 0 END),
                               SUM(CASE WHEN to_char(at AT TIME ZONE 'UTC', 'YYYY-MM') = :month
                                        THEN estimate_usd ELSE 0 END),
                               SUM(CASE WHEN to_char(at AT TIME ZONE 'UTC', 'YYYY-MM') = :month
                                        THEN 1 ELSE 0 END)
                        FROM llm_usage_events
                        WHERE workspace_id = :wid
                        GROUP BY source
                        """
                    ),
                    {"wid": wid, "month": month},
                ).all()
                last = conn.execute(
                    text(
                        """
                        SELECT at, estimate_usd, model, purpose, source
                        FROM llm_usage_events
                        WHERE workspace_id = :wid
                        ORDER BY at DESC LIMIT 1
                        """
                    ),
                    {"wid": wid},
                ).first()
                run_rows = conn.execute(
                    text(
                        """
                        SELECT at, model, purpose, source, prompt_tokens,
                               completion_tokens, estimate_usd
                        FROM llm_usage_events
                        WHERE workspace_id = :wid
                        ORDER BY at DESC LIMIT 200
                        """
                    ),
                    {"wid": wid},
                ).all()
            else:
                rows = conn.execute(
                    text(
                        """
                        SELECT source,
                               SUM(prompt_tokens), SUM(completion_tokens),
                               SUM(cached_tokens), SUM(reasoning_tokens),
                               SUM(estimate_usd), COUNT(*),
                               SUM(CASE WHEN to_char(at AT TIME ZONE 'UTC', 'YYYY-MM') = :month
                                        THEN prompt_tokens ELSE 0 END),
                               SUM(CASE WHEN to_char(at AT TIME ZONE 'UTC', 'YYYY-MM') = :month
                                        THEN completion_tokens ELSE 0 END),
                               SUM(CASE WHEN to_char(at AT TIME ZONE 'UTC', 'YYYY-MM') = :month
                                        THEN estimate_usd ELSE 0 END),
                               SUM(CASE WHEN to_char(at AT TIME ZONE 'UTC', 'YYYY-MM') = :month
                                        THEN 1 ELSE 0 END)
                        FROM llm_usage_events
                        GROUP BY source
                        """
                    ),
                    {"month": month},
                ).all()
                last = conn.execute(
                    text(
                        """
                        SELECT at, estimate_usd, model, purpose, source
                        FROM llm_usage_events
                        ORDER BY at DESC LIMIT 1
                        """
                    )
                ).first()
                run_rows = conn.execute(
                    text(
                        """
                        SELECT at, model, purpose, source, prompt_tokens,
                               completion_tokens, estimate_usd
                        FROM llm_usage_events
                        ORDER BY at DESC LIMIT 200
                        """
                    )
                ).all()
    except Exception:
        log.exception("llm usage read failed")
        return None
    if not rows and last is None:
        return None
    lifetime = _empty_bucket()
    month_b = _empty_bucket()
    byok = _empty_bucket()
    platform = _empty_bucket()
    for (
        source,
        p,
        c,
        cached,
        reasoning,
        usd,
        calls,
        mp,
        mc,
        mu,
        mcalls,
    ) in rows:
        bucket = byok if source == "byok" else platform
        _add(
            lifetime,
            prompt=int(p or 0),
            completion=int(c or 0),
            cached=int(cached or 0),
            reasoning=int(reasoning or 0),
            cost=float(usd or 0),
            calls=int(calls or 0),
        )
        _add(
            bucket,
            prompt=int(p or 0),
            completion=int(c or 0),
            cached=int(cached or 0),
            reasoning=int(reasoning or 0),
            cost=float(usd or 0),
            calls=int(calls or 0),
        )
        _add(
            month_b,
            prompt=int(mp or 0),
            completion=int(mc or 0),
            cached=0,
            reasoning=0,
            cost=float(mu or 0),
            calls=int(mcalls or 0),
        )
    runs = [
        {
            "at": (r[0].isoformat() if hasattr(r[0], "isoformat") else str(r[0])),
            "model": r[1],
            "purpose": r[2],
            "source": r[3],
            "prompt_tokens": int(r[4] or 0),
            "completion_tokens": int(r[5] or 0),
            "estimate_usd": float(r[6] or 0),
        }
        for r in reversed(run_rows)
    ]
    last_at = None
    last_usd = None
    last_source = None
    if last is not None:
        last_at = last[0].isoformat() if hasattr(last[0], "isoformat") else str(last[0])
        last_usd = float(last[1] or 0)
        last_source = last[4]
    return {
        "lifetime": lifetime,
        "month": month_b,
        "byok": byok,
        "platform": platform,
        "runs": runs,
        "last_at": last_at,
        "last_usd": last_usd,
        "last_source": last_source,
    }


def load_pricing() -> dict[str, Any]:
    global _pricing_cache
    if _pricing_cache is not None:
        return _pricing_cache
    path = _pricing_path()
    if not path.exists():
        _pricing_cache = {"models": {"default": {"input_per_mtok": 1.0, "output_per_mtok": 3.0}}}
        return _pricing_cache
    text = path.read_text()
    try:
        import yaml  # type: ignore

        _pricing_cache = yaml.safe_load(text) or {}
    except Exception:
        # Minimal fallback if PyYAML is unavailable in a lean env.
        _pricing_cache = {"models": {"default": {"input_per_mtok": 1.0, "output_per_mtok": 3.0}}}
    return _pricing_cache


def pricing_table() -> dict[str, Any]:
    return load_pricing().get("models") or {}


def rates_as_of() -> str:
    return str(load_pricing().get("as_of") or "2026-08")


def _model_rates(model: str) -> dict[str, Any]:
    models = pricing_table()
    key = (model or "").strip()
    if key in models:
        return models[key]
    aliases = load_pricing().get("aliases") or {}
    mapped = aliases.get(key) or aliases.get(key.lower())
    if mapped and mapped in models:
        return models[mapped]
    lower = {str(k).lower(): v for k, v in models.items()}
    if key.lower() in lower:
        return lower[key.lower()]
    return models.get("default") or {"input_per_mtok": 1.0, "output_per_mtok": 3.0}


def estimate_usd(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    row = _model_rates(model)
    inp = float(row.get("input_per_mtok") or 1.0)
    out = float(row.get("output_per_mtok") or 3.0)
    return (prompt_tokens / 1_000_000.0) * inp + (completion_tokens / 1_000_000.0) * out


def _read_ledger() -> dict[str, Any]:
    path = _usage_path()
    if not path.exists():
        return {"workspaces": {}}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {"workspaces": {}}


def _write_ledger(data: dict[str, Any]) -> None:
    path = _usage_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True))


def _empty_bucket() -> dict[str, Any]:
    return {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "cached_tokens": 0,
        "reasoning_tokens": 0,
        "total_tokens": 0,
        "estimate_usd": 0.0,
        "calls": 0,
    }


def _add(
    bucket: dict[str, Any],
    *,
    prompt: int,
    completion: int,
    cached: int,
    reasoning: int,
    cost: float,
    calls: int = 1,
) -> None:
    bucket["prompt_tokens"] = int(bucket.get("prompt_tokens") or 0) + int(prompt or 0)
    bucket["completion_tokens"] = int(bucket.get("completion_tokens") or 0) + int(completion or 0)
    bucket["cached_tokens"] = int(bucket.get("cached_tokens") or 0) + int(cached or 0)
    bucket["reasoning_tokens"] = int(bucket.get("reasoning_tokens") or 0) + int(reasoning or 0)
    bucket["total_tokens"] = int(bucket.get("prompt_tokens") or 0) + int(bucket.get("completion_tokens") or 0)
    bucket["estimate_usd"] = round(float(bucket.get("estimate_usd") or 0) + float(cost or 0), 6)
    bucket["calls"] = int(bucket.get("calls") or 0) + int(calls or 0)


def _snapshot(bucket: dict[str, Any] | None) -> dict[str, Any]:
    b = bucket or _empty_bucket()
    prompt = int(b.get("prompt_tokens") or 0)
    completion = int(b.get("completion_tokens") or 0)
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "cached_tokens": int(b.get("cached_tokens") or 0),
        "reasoning_tokens": int(b.get("reasoning_tokens") or 0),
        "total_tokens": int(b.get("total_tokens") or (prompt + completion)),
        "estimate_usd": round(float(b.get("estimate_usd") or 0), 4),
        "calls": int(b.get("calls") or 0),
    }


def record_usage(
    workspace_id: str,
    *,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    purpose: str = "",
    cached_tokens: int = 0,
    reasoning_tokens: int = 0,
    source: str | None = None,
) -> dict[str, Any]:
    cost = estimate_usd(model, prompt_tokens, completion_tokens)
    billed_from = _source_of(purpose, source)
    now = datetime.now(timezone.utc)
    month = now.strftime("%Y-%m")
    wid = (workspace_id or "").strip() or "platform"
    with _lock:
        data = _read_ledger()
        totals = data.setdefault("totals", _empty_bucket())
        ws = data.setdefault("workspaces", {}).setdefault(
            wid,
            {"months": {}, "runs": [], "totals": _empty_bucket()},
        )
        ws.setdefault("totals", _empty_bucket())
        ws.setdefault("byok", _empty_bucket())
        ws.setdefault("platform", _empty_bucket())
        m = ws["months"].setdefault(month, _empty_bucket())
        _add(
            totals,
            prompt=prompt_tokens,
            completion=completion_tokens,
            cached=cached_tokens,
            reasoning=reasoning_tokens,
            cost=cost,
        )
        _add(
            ws["totals"],
            prompt=prompt_tokens,
            completion=completion_tokens,
            cached=cached_tokens,
            reasoning=reasoning_tokens,
            cost=cost,
        )
        _add(
            ws[billed_from],
            prompt=prompt_tokens,
            completion=completion_tokens,
            cached=cached_tokens,
            reasoning=reasoning_tokens,
            cost=cost,
        )
        _add(
            m,
            prompt=prompt_tokens,
            completion=completion_tokens,
            cached=cached_tokens,
            reasoning=reasoning_tokens,
            cost=cost,
        )
        run = {
            "at": now.isoformat(),
            "model": model,
            "prompt_tokens": int(prompt_tokens or 0),
            "completion_tokens": int(completion_tokens or 0),
            "cached_tokens": int(cached_tokens or 0),
            "reasoning_tokens": int(reasoning_tokens or 0),
            "estimate_usd": round(cost, 6),
            "purpose": purpose,
            "source": billed_from,
        }
        runs = ws.setdefault("runs", [])
        runs.append(run)
        ws["runs"] = runs[-200:]
        ws["last_run_usd"] = round(cost, 6)
        ws["last_run_source"] = billed_from
        _write_ledger(data)
    _record_postgres(run, wid)
    return run


def _lifetime_for_workspace(ws: dict[str, Any]) -> dict[str, Any]:
    totals = ws.get("totals")
    if totals and (
        int(totals.get("calls") or 0)
        or int(totals.get("prompt_tokens") or 0)
        or int(totals.get("completion_tokens") or 0)
    ):
        return totals
    summed = _empty_bucket()
    for row in (ws.get("months") or {}).values():
        _add(
            summed,
            prompt=int(row.get("prompt_tokens") or 0),
            completion=int(row.get("completion_tokens") or 0),
            cached=int(row.get("cached_tokens") or 0),
            reasoning=int(row.get("reasoning_tokens") or 0),
            cost=float(row.get("estimate_usd") or 0),
            calls=int(row.get("calls") or 0),
        )
    return summed


def _parse_run_at(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


def forecast_usage(
    *,
    runs: list[dict[str, Any]] | None,
    month_usd: float,
    month_prompt: int = 0,
    month_completion: int = 0,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Project remaining-month and 30-day spend from recent token pace.

    This is not an invoice. Output length, cache hits, model mix, and list-price
    changes all move the real bill. With little history the numbers stay marked thin.
    """
    now = now or datetime.now(timezone.utc)
    days_in_month = calendar.monthrange(now.year, now.month)[1]
    days_elapsed = max(1, now.day)
    remaining = max(0, days_in_month - now.day)
    rows = list(runs or [])
    note = (
        "Projection from recent token pace × pinned list prices. "
        "Not your provider invoice, output length and price changes will move it."
    )
    empty = {
        "possible": True,
        "kind": "projection",
        "confidence": "none",
        "basis": "none",
        "window_days": 0,
        "calls_in_window": 0,
        "daily_usd": 0.0,
        "month_end_usd": 0.0,
        "next_30d_usd": 0.0,
        "days_remaining_in_month": remaining,
        "rates_as_of": rates_as_of(),
        "current_model": None,
        "current_rates": None,
        "what_if": [],
        "note": note,
    }
    if month_usd <= 0 and not rows:
        return empty

    calendar_daily = float(month_usd or 0) / days_elapsed
    cutoff = now - timedelta(days=7)
    recent: list[dict[str, Any]] = []
    for row in rows:
        ts = _parse_run_at(row.get("at"))
        if ts is not None and ts >= cutoff:
            recent.append(row)

    if recent:
        window_days = 7
        pace_usd = sum(float(r.get("estimate_usd") or 0) for r in recent) / window_days
        pace_prompt = sum(int(r.get("prompt_tokens") or 0) for r in recent) / window_days
        pace_completion = sum(int(r.get("completion_tokens") or 0) for r in recent) / window_days
        basis = "last_7_days"
        confidence = "steady" if len(recent) >= 5 else "thin"
        calls_in_window = len(recent)
    else:
        window_days = days_elapsed
        pace_usd = calendar_daily
        pace_prompt = int(month_prompt or 0) / days_elapsed
        pace_completion = int(month_completion or 0) / days_elapsed
        basis = "this_month"
        confidence = "steady" if days_elapsed >= 5 and month_usd > 0 else "thin"
        calls_in_window = 0

    current_model = str((rows[-1].get("model") if rows else "") or "") or None
    current_rates = None
    if current_model:
        row = _model_rates(current_model)
        current_rates = {
            "model": current_model,
            "input_per_mtok": float(row.get("input_per_mtok") or 0),
            "output_per_mtok": float(row.get("output_per_mtok") or 0),
        }

    mix_prompt = int(round(pace_prompt * 30))
    mix_completion = int(round(pace_completion * 30))
    what_if: list[dict[str, Any]] = []
    seen: set[str] = set()
    for name in [current_model, "deepseek-v4-flash", "luna", "claude-sonnet-5", "gpt-5.6-sol"]:
        key = (name or "").strip()
        if not key or key.lower() in seen:
            continue
        seen.add(key.lower())
        what_if.append(
            {
                "model": key,
                "next_30d_usd": round(estimate_usd(key, mix_prompt, mix_completion), 4),
                "current": bool(current_model and key.lower() == current_model.lower()),
            }
        )

    return {
        "possible": True,
        "kind": "projection",
        "confidence": confidence,
        "basis": basis,
        "window_days": window_days,
        "calls_in_window": calls_in_window,
        "daily_usd": round(pace_usd, 6),
        "month_end_usd": round(float(month_usd or 0) + pace_usd * remaining, 4),
        "next_30d_usd": round(pace_usd * 30, 4),
        "days_remaining_in_month": remaining,
        "rates_as_of": rates_as_of(),
        "current_model": current_model,
        "current_rates": current_rates,
        "what_if": what_if,
        "note": note,
    }


def _attach_source_views(
    payload: dict[str, Any],
    *,
    byok: dict[str, Any] | None,
    platform: dict[str, Any] | None,
    last_source: str | None,
) -> dict[str, Any]:
    payload["byok"] = _snapshot(byok)
    payload["platform"] = _snapshot(platform)
    payload["last_run_source"] = last_source
    return payload


def workspace_usage_summary(workspace_id: str) -> dict[str, Any]:
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    wid = (workspace_id or "").strip() or "platform"
    pg = _pg_buckets(wid)
    if pg is not None:
        lifetime = _snapshot(pg["lifetime"])
        month_snap = _snapshot(pg["month"])
        forecast = forecast_usage(
            runs=pg["runs"],
            month_usd=month_snap["estimate_usd"],
            month_prompt=month_snap["prompt_tokens"],
            month_completion=month_snap["completion_tokens"],
        )
        return _attach_source_views(
            {
                "workspace_id": wid,
                "month": month,
                "all_time": lifetime,
                "this_month": month_snap,
                "forecast": forecast,
                "estimate_usd_month": month_snap["estimate_usd"],
                "last_run_usd": pg["last_usd"],
                "last_run_at": pg["last_at"],
                "prompt_tokens_month": month_snap["prompt_tokens"],
                "completion_tokens_month": month_snap["completion_tokens"],
                "prompt_tokens_total": lifetime["prompt_tokens"],
                "completion_tokens_total": lifetime["completion_tokens"],
                "total_tokens": lifetime["total_tokens"],
                "estimate_usd_total": lifetime["estimate_usd"],
                "calls": lifetime["calls"],
                "cached_tokens_total": lifetime["cached_tokens"],
                "reasoning_tokens_total": lifetime["reasoning_tokens"],
            },
            byok=pg["byok"],
            platform=pg["platform"],
            last_source=pg["last_source"],
        )
    with _lock:
        data = _read_ledger()
        ws = (data.get("workspaces") or {}).get(wid) or {}
        m = (ws.get("months") or {}).get(month) or _empty_bucket()
        lifetime = _snapshot(_lifetime_for_workspace(ws))
        month_snap = _snapshot(m)
        runs = list(ws.get("runs") or [])
        last_at = runs[-1].get("at") if runs else None
        last_source = (runs[-1].get("source") if runs else None) or ws.get("last_run_source")
        forecast = forecast_usage(
            runs=runs,
            month_usd=month_snap["estimate_usd"],
            month_prompt=month_snap["prompt_tokens"],
            month_completion=month_snap["completion_tokens"],
        )
        return _attach_source_views(
            {
                "workspace_id": wid,
                "month": month,
                "all_time": lifetime,
                "this_month": month_snap,
                "forecast": forecast,
                "estimate_usd_month": month_snap["estimate_usd"],
                "last_run_usd": ws.get("last_run_usd"),
                "last_run_at": last_at,
                "prompt_tokens_month": month_snap["prompt_tokens"],
                "completion_tokens_month": month_snap["completion_tokens"],
                "prompt_tokens_total": lifetime["prompt_tokens"],
                "completion_tokens_total": lifetime["completion_tokens"],
                "total_tokens": lifetime["total_tokens"],
                "estimate_usd_total": lifetime["estimate_usd"],
                "calls": lifetime["calls"],
                "cached_tokens_total": lifetime["cached_tokens"],
                "reasoning_tokens_total": lifetime["reasoning_tokens"],
            },
            byok=ws.get("byok"),
            platform=ws.get("platform"),
            last_source=last_source,
        )


def global_usage_summary() -> dict[str, Any]:
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    pg = _pg_buckets(None)
    if pg is not None:
        lifetime = _snapshot(pg["lifetime"])
        month_snap = _snapshot(pg["month"])
        forecast = forecast_usage(
            runs=pg["runs"][-200:],
            month_usd=month_snap["estimate_usd"],
            month_prompt=month_snap["prompt_tokens"],
            month_completion=month_snap["completion_tokens"],
        )
        return _attach_source_views(
            {
                "month": month,
                "all_time": lifetime,
                "this_month": month_snap,
                "forecast": forecast,
                "last_run_usd": pg["last_usd"],
                "last_run_at": pg["last_at"],
                "prompt_tokens_total": lifetime["prompt_tokens"],
                "completion_tokens_total": lifetime["completion_tokens"],
                "total_tokens": lifetime["total_tokens"],
                "estimate_usd_total": lifetime["estimate_usd"],
                "calls": lifetime["calls"],
            },
            byok=pg["byok"],
            platform=pg["platform"],
            last_source=pg["last_source"],
        )
    with _lock:
        data = _read_ledger()
        stored = data.get("totals")
        if stored and (
            int(stored.get("calls") or 0)
            or int(stored.get("prompt_tokens") or 0)
            or int(stored.get("completion_tokens") or 0)
        ):
            lifetime = _snapshot(stored)
        else:
            summed = _empty_bucket()
            for ws in (data.get("workspaces") or {}).values():
                row = _lifetime_for_workspace(ws)
                _add(
                    summed,
                    prompt=int(row.get("prompt_tokens") or 0),
                    completion=int(row.get("completion_tokens") or 0),
                    cached=int(row.get("cached_tokens") or 0),
                    reasoning=int(row.get("reasoning_tokens") or 0),
                    cost=float(row.get("estimate_usd") or 0),
                    calls=int(row.get("calls") or 0),
                )
            lifetime = _snapshot(summed)
        month_prompt = 0
        month_completion = 0
        month_usd = 0.0
        month_calls = 0
        runs: list[dict[str, Any]] = []
        for ws in (data.get("workspaces") or {}).values():
            m = (ws.get("months") or {}).get(month) or {}
            month_prompt += int(m.get("prompt_tokens") or 0)
            month_completion += int(m.get("completion_tokens") or 0)
            month_usd += float(m.get("estimate_usd") or 0)
            month_calls += int(m.get("calls") or 0)
            runs.extend(list(ws.get("runs") or []))
        runs.sort(key=lambda r: str(r.get("at") or ""))
        last_at = runs[-1].get("at") if runs else None
        last_usd = float(runs[-1].get("estimate_usd") or 0) if runs else None
        last_source = runs[-1].get("source") if runs else None
        byok = _empty_bucket()
        platform = _empty_bucket()
        for ws in (data.get("workspaces") or {}).values():
            for key, bucket in (("byok", byok), ("platform", platform)):
                row = ws.get(key) or {}
                _add(
                    bucket,
                    prompt=int(row.get("prompt_tokens") or 0),
                    completion=int(row.get("completion_tokens") or 0),
                    cached=int(row.get("cached_tokens") or 0),
                    reasoning=int(row.get("reasoning_tokens") or 0),
                    cost=float(row.get("estimate_usd") or 0),
                    calls=int(row.get("calls") or 0),
                )
        forecast = forecast_usage(
            runs=runs[-200:],
            month_usd=month_usd,
            month_prompt=month_prompt,
            month_completion=month_completion,
        )
        return _attach_source_views(
            {
            "month": month,
            "all_time": lifetime,
            "this_month": {
                "prompt_tokens": month_prompt,
                "completion_tokens": month_completion,
                "total_tokens": month_prompt + month_completion,
                "estimate_usd": round(month_usd, 4),
                "calls": month_calls,
            },
            "forecast": forecast,
            "last_run_usd": last_usd,
            "last_run_at": last_at,
            "prompt_tokens_total": lifetime["prompt_tokens"],
            "completion_tokens_total": lifetime["completion_tokens"],
            "total_tokens": lifetime["total_tokens"],
            "estimate_usd_total": lifetime["estimate_usd"],
            "calls": lifetime["calls"],
            },
            byok=byok,
            platform=platform,
            last_source=last_source,
        )


__all__ = [
    "estimate_usd",
    "forecast_usage",
    "global_usage_summary",
    "load_pricing",
    "pricing_table",
    "rates_as_of",
    "record_usage",
    "workspace_usage_summary",
]
