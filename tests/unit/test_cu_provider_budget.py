"""Actual shared pre-request QA admission; zero provider, GUI or task effects."""
from concurrent.futures import ThreadPoolExecutor
import copy
from dataclasses import replace
from decimal import Decimal
import json
from pathlib import Path
import sys
import threading

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts/desktop"))
from cu_provider_budget import ProviderBudgetError, SharedProviderBudget  # noqa: E402


def body(text="PRIVATE PAGE CONTENT", *, max_output=100):
    return {"model": "gpt-6-sol", "store": False, "service_tier": "default", "max_output_tokens": max_output,
            "reasoning": {"effort": "none"},
            "text": {"format": {"type": "json_schema", "name": "step", "schema": {"type": "object"}}},
            "input": [{"role": "system", "content": "PRIVATE POLICY"},
                      {"role": "user", "content": [{"type": "input_text", "text": text}]}]}


def response(*, inp=100, out=20):
    return {"status": "completed", "model": "gpt-6-sol", "output": "PRIVATE RESULT",
            "usage": {"input_tokens": inp, "output_tokens": out, "total_tokens": inp + out}}


def test_bounds_include_exact_unicode_json_schema_role_and_output_ceiling_without_storing_content():
    budget = SharedProviderBudget()
    request = body("UNICODE_PRIVATE" + "🍃" * 40, max_output=700)
    request["text"]["format"]["schema"]["description"] = "SCHEMA_PRIVATE" * 100
    ticket = budget.reserve(request)
    serialized_bytes = len(json.dumps(request, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode())
    assert ticket.input_token_bound == serialized_bytes + 2048 + 256 * 2
    assert ticket.output_token_bound == 700
    assert ticket.estimated_usd == (Decimal(ticket.input_token_bound) * 5 + Decimal(700) * 30) / 1_000_000
    assert budget.snapshot()["in_flight"] == 1
    combined = repr(vars(budget)) + repr(ticket) + json.dumps(budget.snapshot())
    for private in ("UNICODE_PRIVATE", "PRIVATE POLICY", "SCHEMA_PRIVATE"):
        assert private not in combined


def test_pending_and_unknown_requests_consume_all_allowance_before_any_next_request():
    budget = SharedProviderBudget(max_estimated_usd="0.025")
    ticket = budget.reserve(body())
    before = budget.snapshot()
    assert Decimal(before["committed_estimated_usd"]) == ticket.estimated_usd
    with pytest.raises(ProviderBudgetError, match="qa_provider_estimated_usd_limit"):
        budget.reserve(body())
    budget.hold(ticket)
    assert budget.snapshot()["unknown"] == 1
    assert budget.snapshot()["committed_estimated_usd"] == before["committed_estimated_usd"]
    with pytest.raises(ProviderBudgetError, match="qa_provider_estimated_usd_limit"):
        budget.reserve(body())
    assert budget.snapshot()["attempted_calls"] == 1


def test_completed_provider_receipt_releases_only_difference_but_never_refunds_calls():
    budget = SharedProviderBudget(max_calls=1)
    ticket = budget.reserve(body())
    assert budget.settle(ticket, response()) is True
    result = budget.snapshot()
    assert result["confirmed"] == 1 and result["attempted_calls"] == 1
    assert Decimal(result["committed_estimated_usd"]) == Decimal("0.0011")
    assert budget.settle(ticket, response(inp=1, out=0)) is False
    budget.hold(ticket)
    assert budget.snapshot() == result
    with pytest.raises(ProviderBudgetError, match="qa_provider_call_limit"):
        budget.reserve(body())


def test_luna_uses_same_conservative_pre_request_allowance():
    budget = SharedProviderBudget(model="gpt-6-luna", max_calls=1)
    ticket = budget.reserve({**body(), "model": "gpt-6-luna"})
    assert budget.settle(ticket, {**response(), "model": "gpt-6-luna"}) is True
    assert budget.snapshot()["confirmed"] == 1 and ticket.estimated_usd > Decimal("0.0011")


@pytest.mark.parametrize("retire", ["hold", "malformed_receipt"])
def test_unknown_reservation_is_permanent_even_if_a_later_valid_receipt_is_supplied(retire):
    budget = SharedProviderBudget()
    ticket = budget.reserve(body())
    if retire == "hold":
        budget.hold(ticket)
    else:
        assert not budget.settle(ticket, {"status": "completed", "model": "gpt-6-sol"})
    snapshot = budget.snapshot()
    assert snapshot["unknown"] == 1 and Decimal(snapshot["committed_estimated_usd"]) == ticket.estimated_usd
    assert not budget.settle(ticket, response())
    assert budget.snapshot() == snapshot


@pytest.mark.parametrize("change", [
    {"status": "incomplete"}, {"status": "failed"}, {"status": None}, {"model": "another-model"},
    {"usage": None}, {"usage": {}}, {"usage": {"input_tokens": True, "output_tokens": 0, "total_tokens": 1}},
    {"usage": {"input_tokens": 100, "output_tokens": 20.0, "total_tokens": 120}},
    {"usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}},
    {"usage": {"input_tokens": 100, "output_tokens": 20, "total_tokens": 121}},
])
def test_incomplete_missing_invalid_or_wrong_model_usage_never_releases_unknown_hold(change):
    budget = SharedProviderBudget()
    ticket = budget.reserve(body())
    before = budget.snapshot()["committed_estimated_usd"]
    assert budget.settle(ticket, {**response(), **change}) is False
    assert budget.snapshot()["unknown"] == 1
    assert budget.snapshot()["committed_estimated_usd"] == before


@pytest.mark.parametrize("which", ["input", "output"])
def test_usage_exceeding_reserved_envelope_stops_all_further_admission(which):
    budget = SharedProviderBudget()
    ticket = budget.reserve(body())
    receipt = response(inp=ticket.input_token_bound + 1) if which == "input" else response(out=101)
    assert budget.settle(ticket, receipt) is False
    assert budget.snapshot()["usage_bound_breached"] is True
    assert Decimal(budget.snapshot()["committed_estimated_usd"]) == ticket.estimated_usd
    with pytest.raises(ProviderBudgetError, match="qa_provider_budget_usage_exceeded_bound"):
        budget.reserve(body())


@pytest.mark.parametrize("which", ["input", "output"])
def test_known_above_bound_cost_keeps_larger_hold_and_truthful_negative_remaining(which):
    budget = SharedProviderBudget(max_estimated_usd="0.1")
    ticket = budget.reserve(body())
    receipt = response(inp=100_000) if which == "input" else response(out=10_000)
    assert budget.settle(ticket, receipt) is False
    observed = (Decimal(receipt["usage"]["input_tokens"]) * 5
                + Decimal(receipt["usage"]["output_tokens"]) * 30) / 1_000_000
    snapshot = budget.snapshot()
    assert Decimal(snapshot["committed_estimated_usd"]) == observed > ticket.estimated_usd
    assert Decimal(snapshot["remaining_estimated_usd"]) < 0
    assert snapshot["usage_bound_breached"] and snapshot["provider_invoice_verified"] is False
    assert budget.settle(ticket, response(inp=1, out=0)) is False
    budget.hold(ticket)
    assert budget.snapshot() == snapshot
    with pytest.raises(ProviderBudgetError, match="qa_provider_budget_usage_exceeded_bound"):
        budget.reserve(body())


def test_only_exact_source_ticket_can_settle_or_hold_not_copy_foreign_or_reused_sequence():
    budget, other = SharedProviderBudget(), SharedProviderBudget()
    ticket = budget.reserve(body())
    for invalid in (replace(ticket), other.reserve(body()), None):
        with pytest.raises(ProviderBudgetError, match="qa_provider_budget_receipt_invalid"):
            budget.settle(invalid, response())
        with pytest.raises(ProviderBudgetError, match="qa_provider_budget_receipt_invalid"):
            budget.hold(invalid)
    assert budget.snapshot()["in_flight"] == 1


def test_dated_exact_model_alias_usage_and_detached_snapshot_are_safe():
    budget = SharedProviderBudget()
    ticket = budget.reserve(body())
    detached = budget.snapshot()
    detached["committed_estimated_usd"] = "0"
    receipt = {**response(), "model": "gpt-6-sol-2026-05-03"}
    assert budget.settle(ticket, receipt)
    assert budget.snapshot()["committed_estimated_usd"] != "0"


def test_three_concurrent_workers_share_global_call_and_usd_holds_across_phases():
    budget = SharedProviderBudget(max_calls=7)
    calls, lock = [], threading.Lock()
    def run(_):
        try:
            ticket = budget.reserve(body())
        except ProviderBudgetError as error:
            assert error.code == "qa_provider_call_limit"
            return
        # This represents a network boundary reached only after reservation.
        with lock:
            calls.append(ticket)
        budget.hold(ticket)
    with ThreadPoolExecutor(3) as pool:
        list(pool.map(run, range(40)))
    assert len(calls) == budget.snapshot()["attempted_calls"] == 7
    assert budget.snapshot()["unknown"] == 7
    assert Decimal(budget.snapshot()["committed_estimated_usd"]) == sum(t.estimated_usd for t in calls)
    # A later controlled/public/held-out phase uses the SAME object; no reset.
    with pytest.raises(ProviderBudgetError, match="qa_provider_call_limit"):
        budget.reserve(body("LATER PHASE"))


def test_three_concurrent_workers_never_overreserve_money_even_with_all_requests_in_flight():
    budget = SharedProviderBudget(max_estimated_usd="0.05")
    barrier, admitted = threading.Barrier(3), []
    def reserve(_):
        barrier.wait()
        try:
            return budget.reserve(body())
        except ProviderBudgetError as error:
            assert error.code == "qa_provider_estimated_usd_limit"
    with ThreadPoolExecutor(3) as pool:
        admitted = [x for x in pool.map(reserve, range(3)) if x is not None]
    assert len(admitted) == 2
    assert budget.snapshot()["in_flight"] == 2
    assert Decimal(budget.snapshot()["committed_estimated_usd"]) <= Decimal("0.05")


@pytest.mark.parametrize("changes", [
    {"service_tier": "priority"}, {"service_tier": "auto"}, {"model": "gpt-6-astra"},
    {"store": True}, {"reasoning": {"effort": "high"}}, {"max_output_tokens": True},
    {"max_output_tokens": 0}, {"max_output_tokens": 4097}, {"tools": []},
    {"previous_response_id": "PRIVATE"}, {"input": "UNBOUND"},
    {"input": [{"role": "user", "content": [{"type": "input_image", "image_url": "PRIVATE"}]}]},
    {"input": [{"role": "assistant", "content": "HIDDEN CONTEXT"}]},
])
def test_unsupported_modalities_context_model_tier_or_output_refuse_before_attempt_count(changes):
    budget = SharedProviderBudget()
    with pytest.raises(ProviderBudgetError, match="qa_provider_budget_request_unsupported"):
        budget.reserve({**body(), **changes})
    assert budget.snapshot()["attempted_calls"] == 0
    assert budget.snapshot()["committed_estimated_usd"] == "0"


def test_oversize_and_nonserializable_body_refuse_without_retaining_private_values():
    budget = SharedProviderBudget()
    for invalid in (body("PRIVATE" * 20_000), {**body(), "text": {"format": float("nan")}}):
        with pytest.raises(ProviderBudgetError) as error:
            budget.reserve(invalid)
        assert error.value.code == "qa_provider_budget_request_unsupported"
    assert budget.snapshot()["attempted_calls"] == 0


def test_missing_tier_is_not_proof_of_default_pricing_and_refuses_before_io():
    request = body()
    del request["service_tier"]
    budget = SharedProviderBudget()
    with pytest.raises(ProviderBudgetError, match="qa_provider_budget_request_unsupported"):
        budget.reserve(request)
    assert budget.snapshot()["attempted_calls"] == 0


@pytest.mark.parametrize("config", [
    {"model": "unknown"}, {"max_calls": True}, {"max_calls": 0}, {"max_calls": 1001},
    {"max_estimated_usd": "0"}, {"max_estimated_usd": "10.01"}, {"max_estimated_usd": "NaN"},
    {"max_estimated_usd": "Infinity"}, {"max_estimated_usd": 10.0},
])
def test_allowance_cannot_exceed_authorized_finite_limits(config):
    with pytest.raises(ProviderBudgetError, match="qa_provider_budget_configuration_invalid"):
        SharedProviderBudget(**config)


def test_actual_actor_planner_verifier_builders_all_get_independent_before_request_reservations():
    from coding_agents.cu_ai_loop import BROWSER_MARKER, build_request_body, build_text_request_body
    budget = SharedProviderBudget()
    requests = [build_request_body("gpt-6-sol", BROWSER_MARKER + " OWNED TASK", ""),
                build_text_request_body("gpt-6-sol", "SYSTEM", "TASK", "plan", 600),
                build_text_request_body("gpt-6-sol", "SYSTEM", "FACTS", "verify", 300)]
    # The opt-in QA caller pins default tier; production builders stay unchanged.
    requests = [{**request, "service_tier": "default"} for request in requests]
    original = copy.deepcopy(requests)
    tickets = [budget.reserve(request) for request in requests]
    assert [t.output_token_bound for t in tickets] == [700, 600, 300]
    assert requests == original
    assert budget.snapshot()["attempted_calls"] == 3
