"""Opt-in QA-only, process-shared pre-request call and estimated-USD allowance.

No request/response text is retained. The allowance is an estimate, not a
provider invoice or account billing limit. Unknown outcomes keep their entire
reservation, and calls are never refunded. One instance must cover all workers
and phases; starting a new process/instance creates a separate allowance.

Text-only bounded Responses requests use one token per serialized UTF-8 byte,
including JSON/schema bytes, plus 2048 request and 256 per-message tokens. This
is deliberately more conservative than ordinary text tokenization. Images,
tools, previous responses and unbound context are refused rather than estimated.
GPT-6 Sol official pricing (checked 2026-10-04): $2/$10 per million input/output;
cache writes $2.50. We reserve at $5/$30, refuse priority/nondefault tiers and
keep this bounded profile below the 272K long-input pricing threshold.
https://developers.openai.com/api/docs/models/gpt-6-sol
"""
from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

INPUT_RATE = Decimal("5")
OUTPUT_RATE = Decimal("30")
MILLION = Decimal("1000000")
MAX_BODY_BYTES = 128_000
MAX_INPUT_BOUND = 150_000
MAX_OUTPUT = 4096
SUPPORTED_MODELS = frozenset({"gpt-6-sol", "gpt-6-luna"})
_BODY_KEYS = frozenset({"model", "store", "input", "max_output_tokens", "reasoning", "text", "service_tier"})


class ProviderBudgetError(RuntimeError):
    """Closed content-free refusal; never includes provider text or arguments."""

    CODES = frozenset({"qa_provider_budget_configuration_invalid", "qa_provider_budget_request_unsupported",
                       "qa_provider_budget_usage_exceeded_bound", "qa_provider_call_limit",
                       "qa_provider_estimated_usd_limit", "qa_provider_budget_receipt_invalid"})

    def __init__(self, code: str):
        self.code = code if code in self.CODES else "qa_provider_budget_receipt_invalid"
        super().__init__(self.code)


@dataclass(frozen=True)
class Reservation:
    sequence: int
    input_token_bound: int
    output_token_bound: int
    estimated_usd: Decimal


@dataclass
class _Record:
    ticket: Reservation
    state: str = "in_flight"
    charged_usd: Decimal | None = None


def _usd(input_tokens: int, output_tokens: int) -> Decimal:
    return (Decimal(input_tokens) * INPUT_RATE + Decimal(output_tokens) * OUTPUT_RATE) / MILLION


def _positive_decimal(value: str | Decimal | int) -> Decimal:
    try:
        if isinstance(value, bool) or not isinstance(value, (str, Decimal, int)):
            raise ValueError
        result = Decimal(value)
        if not result.is_finite() or result <= 0:
            raise ValueError
        return result
    except (ValueError, InvalidOperation, TypeError):
        raise ProviderBudgetError("qa_provider_budget_configuration_invalid") from None


def _request_bounds(body: Any, model: str) -> tuple[int, int]:
    if (not isinstance(body, dict) or set(body) - _BODY_KEYS or body.get("model") != model
            or body.get("store") is not False or body.get("reasoning") != {"effort": "none"}
            or body.get("service_tier") != "default"):
        raise ProviderBudgetError("qa_provider_budget_request_unsupported")
    max_output = body.get("max_output_tokens")
    messages = body.get("input")
    if (type(max_output) is not int or not 1 <= max_output <= MAX_OUTPUT
            or not isinstance(messages, list) or not 1 <= len(messages) <= 16):
        raise ProviderBudgetError("qa_provider_budget_request_unsupported")
    for message in messages:
        if (not isinstance(message, dict) or set(message) != {"role", "content"}
                or message["role"] not in {"system", "developer", "user"}):
            raise ProviderBudgetError("qa_provider_budget_request_unsupported")
        content = message["content"]
        if isinstance(content, str):
            continue
        if not isinstance(content, list) or not 1 <= len(content) <= 32:
            raise ProviderBudgetError("qa_provider_budget_request_unsupported")
        if any(not isinstance(part, dict) or set(part) != {"type", "text"}
               or part["type"] != "input_text" or not isinstance(part["text"], str) for part in content):
            raise ProviderBudgetError("qa_provider_budget_request_unsupported")
    try:
        size = len(json.dumps(body, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8"))
    except (TypeError, ValueError, UnicodeError):
        raise ProviderBudgetError("qa_provider_budget_request_unsupported") from None
    input_bound = size + 2048 + 256 * len(messages)
    if size > MAX_BODY_BYTES or input_bound > MAX_INPUT_BOUND:
        raise ProviderBudgetError("qa_provider_budget_request_unsupported")
    return input_bound, max_output


class SharedProviderBudget:
    """Thread-safe reservations covering one explicitly coordinated QA process."""

    def __init__(self, *, model: str = "gpt-6-sol", max_calls: int = 1000,
                 max_estimated_usd: str | Decimal | int = "10"):
        if model not in SUPPORTED_MODELS or type(max_calls) is not int or not 1 <= max_calls <= 1000:
            raise ProviderBudgetError("qa_provider_budget_configuration_invalid")
        limit = _positive_decimal(max_estimated_usd)
        if limit > Decimal("10"):
            raise ProviderBudgetError("qa_provider_budget_configuration_invalid")
        self.model, self.max_calls, self.max_estimated_usd = model, max_calls, limit
        self._lock = threading.Lock()
        self._records: dict[int, _Record] = {}
        self._committed = Decimal(0)
        self._blocked = False

    def reserve(self, body: dict) -> Reservation:
        """Atomically admit before network I/O; no release exists for unknown calls."""
        input_bound, output_bound = _request_bounds(body, self.model)
        estimate = _usd(input_bound, output_bound)
        with self._lock:
            if self._blocked:
                raise ProviderBudgetError("qa_provider_budget_usage_exceeded_bound")
            if len(self._records) >= self.max_calls:
                raise ProviderBudgetError("qa_provider_call_limit")
            if self._committed + estimate > self.max_estimated_usd:
                raise ProviderBudgetError("qa_provider_estimated_usd_limit")
            ticket = Reservation(len(self._records) + 1, input_bound, output_bound, estimate)
            self._records[ticket.sequence] = _Record(ticket)
            self._committed += estimate
            return ticket

    def _record(self, ticket: Reservation) -> _Record:
        record = self._records.get(getattr(ticket, "sequence", None))
        if record is None or record.ticket is not ticket:
            raise ProviderBudgetError("qa_provider_budget_receipt_invalid")
        return record

    def hold(self, ticket: Reservation) -> None:
        """Mark uncertain response without refunding either USD reservation or call."""
        with self._lock:
            record = self._record(ticket)
            if record.state != "confirmed":
                record.state = "unknown"

    def settle(self, ticket: Reservation, response: dict) -> bool:
        """Reconcile only a complete, exact-model usage receipt within both bounds."""
        with self._lock:
            record = self._record(ticket)
            if record.state != "in_flight":
                return False
            usage = response.get("usage") if isinstance(response, dict) else None
            actual_model = response.get("model") if isinstance(response, dict) else None
            model_matches = isinstance(actual_model, str) and (actual_model == self.model or re.fullmatch(
                re.escape(self.model) + r"-\d{4}-\d{2}-\d{2}", actual_model) is not None)
            if (not isinstance(response, dict) or response.get("status") != "completed" or not model_matches
                    or not isinstance(usage, dict)):
                record.state = "unknown"
                return False
            inp, out, total = (usage.get(k) for k in ("input_tokens", "output_tokens", "total_tokens"))
            if (any(type(x) is not int for x in (inp, out, total)) or inp <= 0 or out < 0
                    or total != inp + out):
                record.state = "unknown"
                return False
            if inp > ticket.input_token_bound or out > ticket.output_token_bound:
                # Fail future admission if the asserted conservative envelope
                # was violated. Do not erase the already consumed reservation.
                record.state = "unknown"
                observed_hold = max(ticket.estimated_usd, _usd(inp, out))
                self._committed += observed_hold - ticket.estimated_usd
                record.charged_usd = observed_hold
                self._blocked = True
                return False
            cost = _usd(inp, out)
            self._committed -= ticket.estimated_usd - cost
            record.state, record.charged_usd = "confirmed", cost
            return True

    def snapshot(self) -> dict:
        """Fresh content-free receipt; caller changes cannot alter the allowance."""
        with self._lock:
            counts = {state: sum(r.state == state for r in self._records.values())
                      for state in ("in_flight", "unknown", "confirmed")}
            return {"schema": 1, "scope": "qa_single_process_shared_workers_and_phases",
                    "model": self.model, "call_limit": self.max_calls, "attempted_calls": len(self._records),
                    "max_estimated_usd": str(self.max_estimated_usd), "committed_estimated_usd": str(self._committed),
                    "remaining_estimated_usd": str(self.max_estimated_usd - self._committed),
                    "input_usd_per_million_bound": str(INPUT_RATE), "output_usd_per_million_bound": str(OUTPUT_RATE),
                    "usage_bound_breached": self._blocked, **counts,
                    "provider_invoice_verified": False}
