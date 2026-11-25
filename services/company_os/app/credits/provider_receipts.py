"""Pure pricing/count evidence for server-built native provider receipts.

Labels and output_usable establish no caller, Task, device or effect permission.
The owner must persist the original binding/digest before HTTP and validate
payload/current authority separately. Unknown evidence retains its funded hold.
"""
from __future__ import annotations

import math
import os
import re
from copy import deepcopy
from decimal import Decimal, InvalidOperation, ROUND_CEILING, localcontext

from app.credits.errors import CreditsUnavailable
from app.credits.units import MICRO_PER_USD, MIN_RESERVE_MICRO, usd_to_micro
from app.secrets.detect import looks_like_secret

CONFIRMED_PROVIDER_AUTHORITIES = frozenset({"proposal_only", "observation_only"})
_MAX_COUNT = 2**63 - 1
_NAME = re.compile(r"[A-Za-z0-9._:/-]{1,256}")
_TIER = re.compile(r"[a-z][a-z0-9_-]{0,31}")
_DIGEST = re.compile(r"[a-f0-9]{64}")
_RATE_FIELDS = frozenset({"input_per_mtok", "output_per_mtok", "cached_input_per_mtok", "cache_write_per_mtok",
    "long_input_per_mtok", "long_output_per_mtok", "long_cached_input_per_mtok", "long_cache_write_per_mtok"})
_LONG_ALIASES = {"long_context_threshold_tokens": "long_context_threshold",
    "long_context_input_per_mtok": "long_input_per_mtok",
    "long_context_output_per_mtok": "long_output_per_mtok",
    "long_context_cached_input_per_mtok": "long_cached_input_per_mtok"}
_BINDING_FIELDS = frozenset({"schema_version", "purpose", "provider", "deployment", "catalog_model", "api_mode",
    "endpoint_host", "input_token_limit", "max_output_tokens", "requested_service_tier", "pricing_snapshot"})
_ROLE_FIELDS = frozenset({"purpose", "provider", "model", "catalog_model", "api_mode", "max_output_tokens", "endpoint_host"})
_RECEIPT_FIELDS = frozenset({"schema_version", "authority", "requires_proposal_apply", "request_digest", "provider",
    "deployment", "catalog_model", "model", "api_mode", "input_tokens", "output_tokens", "total_tokens",
    "cached_tokens", "reasoning_tokens", "service_tier", "role_metadata"})
_SNAPSHOT_FIELDS = frozenset({"schema_version", "currency", "micro_per_usd", "source", "models", "aliases", "service_tiers"})


def _unknown(reason: str):
    raise CreditsUnavailable("provider_receipt_" + reason)


def _plain(value, reason: str) -> dict:
    if type(value) is not dict or any(type(key) is not str for key in value):
        _unknown(reason)
    return value


def _version(value) -> None:
    if type(value) is not int or value != 1:
        _unknown("schema_unknown")


def _name(value, reason: str) -> str:
    if type(value) is not str or _NAME.fullmatch(value) is None or "://" in value or looks_like_secret(value):
        _unknown(reason)
    return value


def _count(value, *, positive=False) -> int:
    if type(value) is not int or not (1 if positive else 0) <= value <= _MAX_COUNT:
        _unknown("counts_unknown")
    return value


def _host(value) -> str:
    if type(value) is not str or not 1 <= len(value) <= 253 or value != value.lower() or looks_like_secret(value):
        _unknown("binding_unknown")
    labels = value.split(".")
    if any(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) is None for label in labels):
        _unknown("binding_unknown")
    return value


def _decimal(value, *, captured=False) -> Decimal:
    if captured and type(value) is not str:
        _unknown("pricing_unknown")
    if type(value) not in {str, int, float, Decimal} or type(value) is float and not math.isfinite(value):
        _unknown("pricing_unknown")
    if isinstance(value, str) and not 1 <= len(value) <= 64:
        _unknown("pricing_unknown")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        _unknown("pricing_unknown")
    if not result.is_finite() or not 0 <= result <= Decimal("1000000") or result.as_tuple().exponent < -18:
        _unknown("pricing_unknown")
    return result


def _decimal_text(value: Decimal) -> str:
    if value == 0:
        return "0"
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _row(raw: dict, *, captured=False) -> dict:
    raw = _plain(raw, "pricing_unknown")
    normalized = dict(raw)
    for old, current in _LONG_ALIASES.items():
        if old not in raw:
            continue
        if current in raw:
            same = raw[old] == raw[current] if current == "long_context_threshold" else _decimal(
                raw[old], captured=captured) == _decimal(raw[current], captured=captured)
            if not same:
                _unknown("pricing_unknown")
        normalized[current] = raw[old]
    allowed = _RATE_FIELDS | {"long_context_threshold"}
    if set(normalized) - allowed - set(_LONG_ALIASES):
        _unknown("pricing_unknown")
    if not {"input_per_mtok", "output_per_mtok"} <= normalized.keys():
        _unknown("pricing_unknown")
    result = {name: _decimal_text(_decimal(normalized[name], captured=captured))
              for name in _RATE_FIELDS if name in normalized}
    if "long_context_threshold" in normalized:
        result["long_context_threshold"] = _count(normalized["long_context_threshold"])
        effective_input = _decimal(result.get("long_input_per_mtok", result["input_per_mtok"]), captured=True)
        effective_output = _decimal(result.get("long_output_per_mtok", result["output_per_mtok"]), captured=True)
        if effective_input + effective_output == 0:
            _unknown("pricing_unknown")
    elif any(name.startswith("long_") for name in result):
        _unknown("pricing_unknown")
    return result


def _known(models: dict, aliases: dict, model: str) -> str:
    # Exactly one explicit captured alias hop, no date/family regex or inferred
    # deployment equivalence. Fallback and zero-price local rows are not evidence.
    if model.casefold() in {"local", "default"}:
        _unknown("model_unknown")
    canonical = aliases.get(model, model)
    if canonical.casefold() in {"local", "default"} or canonical not in models:
        _unknown("model_unknown")
    row = models[canonical]
    if _decimal(row["input_per_mtok"], captured=True) + _decimal(
            row["output_per_mtok"], captured=True) == 0:
        _unknown("model_unknown")
    return canonical


def capture_pricing_snapshot(catalog_model: str) -> dict:
    """Copy bounded configured pricing/aliases/tier multipliers before HTTP."""
    from app.llm.pricing import load_pricing
    catalog_model = _name(catalog_model, "model_unknown")
    config = _plain(load_pricing(), "pricing_unknown")
    models = _plain(config.get("models"), "pricing_unknown")
    aliases = _plain(config.get("aliases", {}), "pricing_unknown")
    if not 1 <= len(models) <= 64 or len(aliases) > 128:
        _unknown("pricing_unknown")
    copied = {}
    for model, raw in models.items():
        _name(model, "pricing_unknown")
        row = _row(raw)
        if model.casefold() not in {"local", "default"} and _decimal(row["input_per_mtok"], captured=True) + _decimal(
                row["output_per_mtok"], captured=True) > 0:
            copied[model] = row
    captured_aliases = {}
    for alias, target in aliases.items():
        _name(alias, "pricing_unknown")
        _name(target, "pricing_unknown")
        if target not in models:
            _unknown("pricing_unknown")
        if alias in copied and alias != target and copied[alias] != copied.get(target):
            _unknown("pricing_unknown")
        if alias.casefold() not in {"local", "default"} and target in copied:
            captured_aliases[alias] = target
    tiers = _plain(config.get("service_tiers", {}), "pricing_unknown")
    if len(tiers) > 8:
        _unknown("pricing_unknown")
    captured_tiers = {}
    for name, value in tiers.items():
        if type(name) is not str or _TIER.fullmatch(name) is None or looks_like_secret(name):
            _unknown("pricing_unknown")
        multiplier = _decimal(value)
        if multiplier <= 0:
            _unknown("pricing_unknown")
        captured_tiers[name] = _decimal_text(multiplier)
    if "default" in captured_tiers and _decimal(captured_tiers["default"], captured=True) != 1:
        _unknown("pricing_unknown")
    captured_tiers["default"] = "1"
    if "priority" not in captured_tiers:
        priority = _decimal(os.environ.get("SUPYMEM_CU_PRIORITY_MULTIPLIER", "2"))
        if not 1 <= priority <= 10:
            _unknown("pricing_unknown")
        captured_tiers["priority"] = _decimal_text(priority)
    if len(captured_tiers) > 8:
        _unknown("pricing_unknown")
    _known(copied, captured_aliases, catalog_model)
    return {"schema_version": 1, "currency": "USD", "micro_per_usd": MICRO_PER_USD,
        "source": "configured_model_rate_table", "models": copied, "aliases": captured_aliases,
        "service_tiers": captured_tiers}


def _snapshot(value) -> dict:
    value = _plain(value, "pricing_unknown")
    if set(value) != _SNAPSHOT_FIELDS:
        _unknown("pricing_unknown")
    _version(value["schema_version"])
    if (type(value["currency"]) is not str or value["currency"] != "USD"
            or type(value["micro_per_usd"]) is not int or value["micro_per_usd"] != MICRO_PER_USD
            or type(value["source"]) is not str or value["source"] != "configured_model_rate_table"):
        _unknown("pricing_unknown")
    models, aliases, tiers = (_plain(value[key], "pricing_unknown") for key in ("models", "aliases", "service_tiers"))
    if not 1 <= len(models) <= 64 or len(aliases) > 128 or not 1 <= len(tiers) <= 8:
        _unknown("pricing_unknown")
    normalized = {}
    for name, raw in models.items():
        _name(name, "pricing_unknown")
        if name.casefold() in {"local", "default"}:
            _unknown("pricing_unknown")
        normalized[name] = _row(raw, captured=True)
    for alias, target in aliases.items():
        _name(alias, "pricing_unknown")
        _name(target, "pricing_unknown")
        if alias.casefold() in {"local", "default"} or target.casefold() in {"local", "default"} or target not in normalized:
            _unknown("pricing_unknown")
        if alias in normalized and alias != target and normalized[alias] != normalized[target]:
            _unknown("pricing_unknown")
    for name, raw in tiers.items():
        if (type(name) is not str or _TIER.fullmatch(name) is None or looks_like_secret(name)
                or _decimal(raw, captured=True) <= 0):
            _unknown("pricing_unknown")
    if "default" not in tiers or _decimal(tiers["default"], captured=True) != 1:
        _unknown("pricing_unknown")
    return {**value, "models": normalized}


def validate_model_binding(model_binding: dict) -> dict:
    """Validate captured admission evidence before HTTP; detach the original."""
    binding = _plain(model_binding, "binding_unknown")
    if set(binding) != _BINDING_FIELDS:
        _unknown("binding_unknown")
    _version(binding["schema_version"])
    for key in ("purpose", "provider", "deployment", "catalog_model"):
        _name(binding[key], "binding_unknown")
    _host(binding["endpoint_host"])
    if type(binding["api_mode"]) is not str or binding["api_mode"] not in {"responses", "chat_completions"}:
        _unknown("binding_unknown")
    _count(binding["input_token_limit"], positive=True)
    _count(binding["max_output_tokens"], positive=True)
    snapshot = _snapshot(binding["pricing_snapshot"])
    _known(snapshot["models"], snapshot["aliases"], binding["catalog_model"])
    tier = binding["requested_service_tier"]
    if tier is not None and (type(tier) is not str or _TIER.fullmatch(tier) is None
            or looks_like_secret(tier) or tier not in snapshot["service_tiers"]):
        _unknown("tier_unknown")
    return deepcopy(binding)


def estimate_model_binding_micro(model_binding: dict) -> int:
    """Conservative original captured model/tier quote; no permission or I/O.

    Maximize each admissible context domain, including a cached rate above the
    full-input rate. Returned model/tier mismatches are separate liabilities.
    """
    binding = validate_model_binding(model_binding)
    tier = binding["requested_service_tier"]
    if tier is None:
        _unknown("tier_unknown")
    snapshot = _snapshot(binding["pricing_snapshot"])
    canonical = _known(snapshot["models"], snapshot["aliases"], binding["catalog_model"])
    row = snapshot["models"][canonical]
    input_limit, output_limit = binding["input_token_limit"], binding["max_output_tokens"]
    threshold = row.get("long_context_threshold", input_limit)
    domains = [("", min(input_limit, threshold))]
    if input_limit > threshold:
        domains.append(("long_", input_limit))
    with localcontext() as context:
        # Counts have <=19 digits; rate/tier fractions have <=18 digits each.
        # Their maximum product needs <=68 significant digits, including tails
        # that can change an integer ceiling. Never use the process context28.
        context.prec = 96
        costs = []
        for prefix, inputs in domains:
            inp = _decimal(row.get(prefix + "input_per_mtok", row["input_per_mtok"]), captured=True)
            out = _decimal(row.get(prefix + "output_per_mtok", row["output_per_mtok"]), captured=True)
            cached = _decimal(row.get(prefix + "cached_input_per_mtok", _decimal_text(inp)), captured=True)
            costs.append(Decimal(inputs) * max(inp, cached) + Decimal(output_limit) * out)
        usd = max(costs) / Decimal(1000000) * _decimal(snapshot["service_tiers"][tier], captured=True)
        micro = int((usd * MICRO_PER_USD).to_integral_value(rounding=ROUND_CEILING))
    return max(MIN_RESERVE_MICRO, micro)


def validate_confirmed_provider_receipt(confirmed_response: dict, *, model_binding: dict,
                                        expected_request_digest: str) -> dict:
    """Return known pricing evidence; unknown evidence raises without mutation.

    output_usable covers only these binding/count/tier checks. The native owner
    separately validates provider success, allowlisted payload and current authority.
    """
    binding = validate_model_binding(model_binding)
    receipt = _plain(confirmed_response, "dto_unknown")
    if not _RECEIPT_FIELDS <= receipt.keys() or set(receipt) - _RECEIPT_FIELDS - {"result", "error_code"}:
        _unknown("dto_unknown")
    _version(receipt["schema_version"])
    if (type(receipt["authority"]) is not str or receipt["authority"] not in CONFIRMED_PROVIDER_AUTHORITIES
            or type(receipt["requires_proposal_apply"]) is not bool):
        _unknown("dto_unknown")
    if (type(expected_request_digest) is not str or _DIGEST.fullmatch(expected_request_digest) is None
            or type(receipt["request_digest"]) is not str or receipt["request_digest"] != expected_request_digest):
        _unknown("binding_unknown")
    input_limit, output_limit = binding["input_token_limit"], binding["max_output_tokens"]
    role = _plain(receipt["role_metadata"], "binding_unknown")
    if set(role) != _ROLE_FIELDS:
        _unknown("binding_unknown")
    expected_role = {key: binding["deployment"] if key == "model" else binding[key] for key in _ROLE_FIELDS}
    if (type(role["max_output_tokens"]) is not int
            or any(type(role[key]) is not str for key in _ROLE_FIELDS - {"max_output_tokens"})
            or role != expected_role or any(type(receipt[key]) is not str or receipt[key] != binding[key]
            for key in ("provider", "deployment", "catalog_model", "api_mode"))):
        _unknown("binding_unknown")
    counts = {key: _count(receipt[key]) for key in ("input_tokens", "output_tokens", "total_tokens", "cached_tokens", "reasoning_tokens")}
    if (counts["total_tokens"] != counts["input_tokens"] + counts["output_tokens"]
            or counts["cached_tokens"] > counts["input_tokens"] or counts["reasoning_tokens"] > counts["output_tokens"]):
        _unknown("counts_unknown")
    snapshot = _snapshot(binding["pricing_snapshot"])
    models, aliases, tiers = (snapshot[key] for key in ("models", "aliases", "service_tiers"))
    requested_model = _known(models, aliases, binding["catalog_model"])
    actual_model = _name(receipt["model"], "model_unknown")
    actual_catalog = _known(models, aliases, actual_model)
    requested_tier, actual_tier = binding["requested_service_tier"], receipt["service_tier"]
    if actual_tier is None:
        if requested_tier != "default":
            _unknown("tier_unknown")
        priced_tier = "default"  # Explicit ordinary request, actual remains unreported.
    else:
        if type(actual_tier) is not str or actual_tier not in tiers:
            _unknown("tier_unknown")
        priced_tier = actual_tier
    reasons = []
    if actual_catalog != requested_model:
        reasons.append("model_mismatch")
    if actual_tier is not None and actual_tier != requested_tier:
        reasons.append("service_tier_mismatch")
    if counts["input_tokens"] > input_limit:
        reasons.append("input_limit_exceeded")
    if counts["output_tokens"] > output_limit:
        reasons.append("output_limit_exceeded")
    row = models[actual_catalog]
    long_context = "long_context_threshold" in row and counts["input_tokens"] > row["long_context_threshold"]
    prefix = "long_" if long_context else ""
    inp = _decimal(row.get(prefix + "input_per_mtok", row["input_per_mtok"]), captured=True)
    out = _decimal(row.get(prefix + "output_per_mtok", row["output_per_mtok"]), captured=True)
    cached = _decimal(row.get(prefix + "cached_input_per_mtok", _decimal_text(inp)), captured=True)
    with localcontext() as context:
        context.prec = 64
        usd = (Decimal(counts["input_tokens"] - counts["cached_tokens"]) * inp
               + Decimal(counts["cached_tokens"]) * cached + Decimal(counts["output_tokens"]) * out) / Decimal(1000000)
        usd *= _decimal(tiers[priced_tier], captured=True)
        micro = usd_to_micro(usd)
    return {key: receipt[key] for key in ("schema_version", "authority", "requires_proposal_apply", "request_digest",
        "provider", "deployment", "catalog_model", "model", "api_mode", "service_tier")} | {
        **counts, "actual_catalog_model": actual_catalog, "priced_service_tier": priced_tier,
        "service_tier_reported": actual_tier is not None, "cost_usd": _decimal_text(usd),
        "provider_actual_micro": micro, "cost_basis": "captured_catalog_usage", "long_context": long_context,
        "cache_write_cost_applied": False, "output_usable": not reasons, "output_errors": reasons}
