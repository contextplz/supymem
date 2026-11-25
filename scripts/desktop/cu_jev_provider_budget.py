"""QA-only shared admission for the actual closed Jev browser protocol.

One instance covers every worker/phase/attempt in one explicitly authorized
process. Calls never refund; unknown and in-flight calls retain their full
reservation. This is a finite list-price estimate, not an invoice/account cap.
No credentials, provider I/O, request text or response text are retained here.

Official terms checked 2026-10-04: https://docs.typesafe.ai/models
Pinned jev-1.13.0: $0.042/M input, output free; total64K request and32K
state+longest question. We reserve the full64K for every attempted POST.
Serialized UTF-8 plus fixed schema/question overhead is an additional
conservative validation bound, not a claim of exact provider tokenization.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

MODEL = "jev-1.13.0"
INPUT_RATE = Decimal("0.042")
INPUT_LIMIT = 64_000
STATE_QUESTION_LIMIT = 32_000
MAX_BODY_BYTES = 24_000
MAX_RESPONSE_BYTES = 65_536
OUTPUT_USAGE_LIMIT = 65_536
REQUEST_OVERHEAD = 2048
QUESTION_OVERHEAD = 256
FULL_RESERVATION_USD = Decimal(INPUT_LIMIT) * INPUT_RATE / Decimal(1_000_000)


class JevBudgetError(RuntimeError):
    """Closed refusal with no provider/body/credential data."""

    CODES = frozenset(
        {
            "qa_jev_budget_configuration_invalid",
            "qa_jev_request_unsupported",
            "qa_jev_call_limit",
            "qa_jev_estimated_usd_limit",
            "qa_jev_usage_bound_breached",
            "qa_jev_receipt_invalid",
        }
    )

    def __init__(self, code: str):
        self.code = code if code in self.CODES else "qa_jev_receipt_invalid"
        super().__init__(self.code)


@dataclass(frozen=True)
class Reservation:
    sequence: int
    input_token_bound: int
    output_token_bound: int
    estimated_usd: Decimal
    request_utf8_token_bound: int
    state_question_utf8_token_bound: int


@dataclass
class _Record:
    ticket: Reservation
    state: str = "in_flight"
    charged_usd: Decimal | None = None


def _cost(tokens: int) -> Decimal:
    return Decimal(tokens) * INPUT_RATE / Decimal(1_000_000)


def _serialized(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=True, allow_nan=False).encode("utf-8")


def _request_bounds(body: Any) -> tuple[int, int]:
    """Admit only an unchanged production browser-decision request shape."""
    try:
        if (
            type(body) is not dict
            or set(body) != {"model", "state", "questions"}
            or body["model"] != MODEL
        ):
            raise ValueError
        size = len(_serialized(body))
        state, questions = body["state"], body["questions"]
        if (
            size > MAX_BODY_BYTES
            or type(state) is not dict
            or type(questions) is not dict
            or not 1 <= len(questions) <= 6
            or set(state)
            not in (
                {"goal", "context", "application", "location", "controls"},
                {
                    "goal",
                    "context",
                    "application",
                    "location",
                    "controls",
                    "reviewed_text",
                },
            )
            or state["application"] != "com.google.Chrome"
        ):
            raise ValueError
        # Reuse the unchanged source contract, including control visibility,
        # closed operation/value questions and exact model/policy instructions.
        from coding_agents import jev_decision

        obs = jev_decision.Observation.model_validate(
            {
                **state,
                "revision": "qa_guard",
                "permitted": True,
                "confirmation_pending": False,
                "prior_effect_uncertain": False,
            }
        )
        if jev_decision.request(obs) != body:
            raise ValueError
        whole_bound = size + REQUEST_OVERHEAD + QUESTION_OVERHEAD * len(questions)
        state_question_bound = (
            len(_serialized(state))
            + max(len(_serialized(q)) for q in questions.values())
            + REQUEST_OVERHEAD
            + QUESTION_OVERHEAD
        )
        if whole_bound > INPUT_LIMIT or state_question_bound > STATE_QUESTION_LIMIT:
            raise ValueError
        return whole_bound, state_question_bound
    except (TypeError, ValueError, KeyError, UnicodeError, RecursionError):
        raise JevBudgetError("qa_jev_request_unsupported") from None


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _no_nonfinite(_value):
    raise ValueError


class SharedJevBudget:
    """Atomic process allowance; constructor cannot exceed200 calls/$1."""

    __slots__ = ("_max_calls", "_limit", "_lock", "_records", "_committed", "_blocked")

    def __init__(
        self, *, max_calls: int = 200, max_estimated_usd: str | Decimal | int = "1"
    ):
        try:
            if (
                type(max_calls) is not int
                or not 1 <= max_calls <= 200
                or isinstance(max_estimated_usd, bool)
                or not isinstance(max_estimated_usd, (str, Decimal, int))
            ):
                raise ValueError
            limit = Decimal(max_estimated_usd)
            if not limit.is_finite() or not 0 < limit <= 1:
                raise ValueError
        except (TypeError, ValueError, InvalidOperation):
            raise JevBudgetError("qa_jev_budget_configuration_invalid") from None
        self._max_calls, self._limit = max_calls, limit
        self._lock = threading.Lock()
        self._records: dict[int, _Record] = {}
        self._committed = Decimal(0)
        self._blocked = False

    @property
    def model(self):
        return MODEL

    @property
    def max_calls(self):
        return self._max_calls

    @property
    def max_estimated_usd(self):
        return self._limit

    def reserve(self, body: dict) -> Reservation:
        whole_bound, state_bound = _request_bounds(body)
        with self._lock:
            if self._blocked:
                raise JevBudgetError("qa_jev_usage_bound_breached")
            if len(self._records) >= self._max_calls:
                raise JevBudgetError("qa_jev_call_limit")
            if self._committed + FULL_RESERVATION_USD > self._limit:
                raise JevBudgetError("qa_jev_estimated_usd_limit")
            ticket = Reservation(
                len(self._records) + 1,
                INPUT_LIMIT,
                OUTPUT_USAGE_LIMIT,
                FULL_RESERVATION_USD,
                whole_bound,
                state_bound,
            )
            self._records[ticket.sequence] = _Record(ticket)
            self._committed += ticket.estimated_usd
            return ticket

    def _record(self, ticket: Reservation) -> _Record:
        record = self._records.get(getattr(ticket, "sequence", None))
        if record is None or record.ticket is not ticket:
            raise JevBudgetError("qa_jev_receipt_invalid")
        return record

    def hold(self, ticket: Reservation) -> None:
        """Never refund an attempted request, including late valid receipts."""
        with self._lock:
            record = self._record(ticket)
            if record.state == "in_flight":
                record.state = "unknown"

    def settle(
        self,
        ticket: Reservation,
        response_bytes: bytes,
        *,
        http_status: int,
        transport_complete: bool,
    ) -> bool:
        """Reconcile full streamed bytes; caller must capture actual transport EOF.

        Usage confirmation is distinct from parsing a useful candidate or an
        independent task oracle. Malformed/unusable decisions can still cost.
        """
        with self._lock:
            record = self._record(ticket)
            if record.state != "in_flight":
                return False
            try:
                if (
                    type(http_status) is not int
                    or http_status != 200
                    or transport_complete is not True
                    or type(response_bytes) is not bytes
                    or not 0 < len(response_bytes) <= MAX_RESPONSE_BYTES
                ):
                    raise ValueError
                value = json.loads(
                    response_bytes.decode("utf-8"),
                    object_pairs_hook=_unique_object,
                    parse_constant=_no_nonfinite,
                )
                if (
                    type(value) is not dict
                    or value.get("model") != MODEL
                    or type(value.get("usage")) is not dict
                ):
                    raise ValueError
                usage = value["usage"]
                if set(usage) != {"input_tokens", "output_tokens"}:
                    raise ValueError
                inp, out = usage.get("input_tokens"), usage.get("output_tokens")
                if any(type(v) is not int or v < 0 for v in (inp, out)):
                    raise ValueError
            except (TypeError, ValueError, UnicodeError, RecursionError):
                record.state = "unknown"
                return False
            if inp > INPUT_LIMIT or out > OUTPUT_USAGE_LIMIT:
                record.state = "unknown"
                observed_hold = max(ticket.estimated_usd, _cost(inp))
                self._committed += observed_hold - ticket.estimated_usd
                record.charged_usd = observed_hold
                self._blocked = True
                return False
            cost = _cost(inp)
            self._committed -= ticket.estimated_usd - cost
            record.state, record.charged_usd = "confirmed", cost
            return True

    def snapshot(self) -> dict:
        with self._lock:
            counts = {
                s: sum(r.state == s for r in self._records.values())
                for s in ("in_flight", "unknown", "confirmed")
            }
            return {
                "schema": 1,
                "scope": "qa_single_process_shared_jev_workers_and_attempts",
                "model": MODEL,
                "call_limit": self._max_calls,
                "attempted_calls": len(self._records),
                "max_estimated_usd": str(self._limit),
                "committed_estimated_usd": str(self._committed),
                "remaining_estimated_usd": str(self._limit - self._committed),
                "full_request_input_token_reservation": INPUT_LIMIT,
                "input_usd_per_million_bound": str(INPUT_RATE),
                "output_usd_per_million_bound": "0",
                "usage_bound_breached": self._blocked,
                **counts,
                "provider_invoice_verified": False,
                "provider_account_cap_verified": False,
            }
