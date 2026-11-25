"""Actual closed request/streamed receipt budget; no provider/task/GUI effects."""

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
from cu_jev_provider_budget import (  # noqa: E402
    FULL_RESERVATION_USD,
    INPUT_LIMIT,
    MAX_BODY_BYTES,
    MAX_RESPONSE_BYTES,
    QUESTION_OVERHEAD,
    REQUEST_OVERHEAD,
    JevBudgetError,
    SharedJevBudget,
)
from coding_agents.jev_decision import Control, Observation, request  # noqa: E402


def body(context="PRIVATE PAGE 🌿"):
    return request(
        Observation(
            revision="owned_revision",
            application="com.google.Chrome",
            location="https://owned.example/",
            goal="Read the approved page and its waitlist link.",
            context=context,
            controls=[
                Control(
                    id="c1",
                    label="PRIVATE LINK 🌿",
                    operations=["click"],
                    visible=True,
                    enabled=True,
                )
            ],
            permitted=True,
            confirmation_pending=False,
            prior_effect_uncertain=False,
        )
    )


def receipt(*, inp=1000, out=84, **extra):
    return json.dumps(
        {
            "model": "jev-1.13.0",
            "answers": {},
            "usage": {"input_tokens": inp, "output_tokens": out},
            **extra,
        }
    ).encode()


def settle(budget, ticket, value=None, **kw):
    return budget.settle(
        ticket,
        value if value is not None else receipt(),
        http_status=kw.get("http_status", 200),
        transport_complete=kw.get("transport_complete", True),
    )


def test_actual_request_full_provider_ceiling_conservative_utf8_bounds_and_no_private_content():
    b = SharedJevBudget()
    value = body()
    ticket = b.reserve(value)
    raw = json.dumps(value, ensure_ascii=True, allow_nan=False).encode()
    assert ticket.estimated_usd == FULL_RESERVATION_USD == Decimal("0.002688")
    assert ticket.input_token_bound == INPUT_LIMIT == 64000
    assert ticket.request_utf8_token_bound == len(
        raw
    ) + REQUEST_OVERHEAD + QUESTION_OVERHEAD * len(value["questions"])
    assert ticket.state_question_utf8_token_bound <= 32000
    assert ticket.request_utf8_token_bound <= 64000 and len(raw) <= MAX_BODY_BYTES
    private_free = repr(ticket) + repr(b._records) + json.dumps(b.snapshot())
    assert "PRIVATE PAGE" not in private_free and "PRIVATE LINK" not in private_free
    assert b.snapshot()["in_flight"] == 1


def test_exact_full_streamed_usage_releases_difference_not_call_or_output_price():
    b = SharedJevBudget(max_calls=1)
    t = b.reserve(body())
    assert settle(b, t) is True
    final = b.snapshot()
    assert final["confirmed"] == 1 and final["attempted_calls"] == 1
    assert Decimal(final["committed_estimated_usd"]) == Decimal("0.000042")
    assert (
        final["provider_invoice_verified"] is False
        and final["provider_account_cap_verified"] is False
    )
    assert settle(b, t, receipt(inp=1, out=0)) is False
    b.hold(t)
    assert final == b.snapshot()
    with pytest.raises(JevBudgetError, match="qa_jev_call_limit"):
        b.reserve(body())


@pytest.mark.parametrize("retire", ["hold", "bad_usage", "partial"])
def test_unknown_never_refunds_or_accepts_late_complete_receipt(retire):
    b = SharedJevBudget(max_estimated_usd="0.002688")
    t = b.reserve(body())
    if retire == "hold":
        b.hold(t)
    else:
        assert (
            settle(b, t, receipt(inp="100"), transport_complete=retire != "partial")
            is False
        )
    snap = b.snapshot()
    assert (
        snap["unknown"] == 1
        and Decimal(snap["committed_estimated_usd"]) == FULL_RESERVATION_USD
    )
    assert settle(b, t) is False
    assert b.snapshot() == snap
    with pytest.raises(JevBudgetError, match="qa_jev_estimated_usd_limit"):
        b.reserve(body())


@pytest.mark.parametrize(
    "max_calls,usd",
    [
        (0, "1"),
        (201, "1"),
        (True, "1"),
        (1, "0"),
        (1, "1.00001"),
        (1, "NaN"),
        (1, "Infinity"),
        (1, True),
        (1, 0.1),
    ],
)
def test_configuration_never_widens_root_authorized_allowance(max_calls, usd):
    with pytest.raises(JevBudgetError, match="qa_jev_budget_configuration_invalid"):
        SharedJevBudget(max_calls=max_calls, max_estimated_usd=usd)


def test_configuration_public_properties_cannot_expand_after_admission():
    b = SharedJevBudget(max_calls=1)
    for name, value in (
        ("max_calls", 201),
        ("max_estimated_usd", Decimal(2)),
        ("model", "jev-latest"),
    ):
        with pytest.raises(AttributeError):
            setattr(b, name, value)


@pytest.mark.parametrize(
    "mutation",
    [
        "model_alias",
        "foreign_model",
        "extra_top",
        "wrong_app",
        "unknown_state",
        "question_prompt",
        "question_type",
        "question_criteria",
        "unicode_bound",
        "hidden_control",
        "unknown_control_field",
        "unbounded_goal",
    ],
)
def test_unsupported_request_never_reserves_or_reaches_transport(mutation):
    value = body()
    if mutation == "model_alias":
        value["model"] = "jev-latest"
    elif mutation == "foreign_model":
        value["model"] = "gpt-6-sol"
    elif mutation == "extra_top":
        value["tools"] = []
    elif mutation == "wrong_app":
        value["state"]["application"] = "com.apple.Mail"
    elif mutation == "unknown_state":
        value["state"]["authority"] = True
    elif mutation == "question_prompt":
        value["questions"]["operation"]["instructions"] += " Changed."
    elif mutation == "question_type":
        value["questions"]["operation"]["type"] = "score"
    elif mutation == "question_criteria":
        value["questions"]["operation"]["criteria"]["new"] = "New action"
    elif mutation == "unicode_bound":
        value["state"]["context"] = "🌿" * 4000
    elif mutation == "hidden_control":
        value["state"]["controls"][0]["visible"] = False
    elif mutation == "unknown_control_field":
        value["state"]["controls"][0]["secret"] = "private"
    elif mutation == "unbounded_goal":
        value["state"]["goal"] = "x" * 12001
    b = SharedJevBudget()
    before = copy.deepcopy(value)
    with pytest.raises(JevBudgetError, match="qa_jev_request_unsupported"):
        b.reserve(value)
    assert value == before
    assert b.snapshot()["attempted_calls"] == 0


@pytest.mark.parametrize(
    "value,kw",
    [
        (b"", {}),
        ("not bytes", {}),
        (b"{}", {}),
        (b"\xff", {}),
        (b"{} trailing", {}),
        (receipt(), {"http_status": 429}),
        (receipt(), {"http_status": True}),
        (receipt(), {"transport_complete": False}),
        (receipt(), {"transport_complete": 1}),
        (receipt(model="jev-latest"), {}),
        (receipt(model="gpt-6-sol"), {}),
        (receipt(inp=True), {}),
        (receipt(out=False), {}),
        (receipt(inp=-1), {}),
        (receipt(out=-1), {}),
        (receipt(inp=1.5), {}),
        (receipt(inp=None), {}),
        (
            b'{"model":"jev-1.13.0","usage":{"input_tokens":10,"input_tokens":1,"output_tokens":2}}',
            {},
        ),
        (b'{"model":"jev-1.13.0","usage":{"input_tokens":NaN,"output_tokens":2}}', {}),
        (
            b'{"model":"jev-1.13.0","usage":{"input_tokens":10,"output_tokens":2,"total_tokens":99}}',
            {},
        ),
        (b" " * (MAX_RESPONSE_BYTES + 1), {}),
    ],
)
def test_unconfirmed_or_malformed_whole_stream_permanently_holds(value, kw):
    b = SharedJevBudget()
    t = b.reserve(body())
    assert settle(b, t, value, **kw) is False
    assert b.snapshot()["unknown"] == 1
    assert Decimal(b.snapshot()["committed_estimated_usd"]) == FULL_RESERVATION_USD
    assert settle(b, t) is False


@pytest.mark.parametrize("inp,out", [(64001, 0), (1, 65537), (30_000_000, 1)])
def test_known_bound_breach_retains_max_observed_charge_and_closes_admission(inp, out):
    b = SharedJevBudget()
    t = b.reserve(body())
    assert settle(b, t, receipt(inp=inp, out=out)) is False
    snap = b.snapshot()
    expected = max(FULL_RESERVATION_USD, Decimal(inp) * Decimal("0.042") / 1_000_000)
    assert Decimal(snap["committed_estimated_usd"]) == expected
    assert snap["usage_bound_breached"] is True and snap["unknown"] == 1
    assert settle(b, t, receipt(inp=1, out=0)) is False
    assert b.snapshot() == snap
    if inp == 30_000_000:
        assert Decimal(snap["remaining_estimated_usd"]) < 0
    with pytest.raises(JevBudgetError, match="qa_jev_usage_bound_breached"):
        b.reserve(body())


def test_usage_confirmation_does_not_claim_candidate_or_task_or_invoice_acceptance():
    b = SharedJevBudget()
    t = b.reserve(body())
    # Paid but unusable answers are still known usage, matching Core's ordering.
    assert settle(b, t, receipt(answers={"not_the_expected_head": "wrong"})) is True
    assert b.snapshot()["confirmed"] == 1
    assert "task_verified" not in b.snapshot()


def test_zero_usage_is_known_cost_only_and_never_refunds_attempted_call():
    b = SharedJevBudget(max_calls=1)
    t = b.reserve(body())
    assert settle(b, t, receipt(inp=0, out=0)) is True
    assert Decimal(b.snapshot()["committed_estimated_usd"]) == 0
    with pytest.raises(JevBudgetError, match="qa_jev_call_limit"):
        b.reserve(body())


def test_foreign_or_copied_ticket_cannot_release_this_instance_reservation():
    a, b = SharedJevBudget(), SharedJevBudget()
    ta, tb = a.reserve(body()), b.reserve(body())
    before = b.snapshot()
    for ticket in (ta, replace(tb), object()):
        with pytest.raises(JevBudgetError, match="qa_jev_receipt_invalid"):
            settle(b, ticket)
        with pytest.raises(JevBudgetError, match="qa_jev_receipt_invalid"):
            b.hold(ticket)
    assert before == b.snapshot()


@pytest.mark.parametrize("limit_kind", ["calls", "usd"])
def test_three_concurrent_workers_share_admission_before_any_fake_post(limit_kind):
    b = SharedJevBudget(
        max_calls=1 if limit_kind == "calls" else 200,
        max_estimated_usd="1" if limit_kind == "calls" else "0.002688",
    )
    start = threading.Barrier(3)
    admitted = threading.Event()
    release = threading.Event()
    delivered = []

    def worker(index):
        start.wait()
        try:
            ticket = b.reserve(body())
        except JevBudgetError as error:
            return error.code
        # This is the only effect seam in the test and comes after admission.
        delivered.append(index)
        admitted.set()
        assert release.wait(3)
        b.hold(ticket)
        return "dispatched_unknown"

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(worker, i) for i in range(3)]
        assert admitted.wait(3)
        assert b.snapshot()["in_flight"] == 1 and b.snapshot()["attempted_calls"] == 1
        release.set()
        results = [f.result() for f in futures]
    assert len(delivered) == 1 and results.count("dispatched_unknown") == 1
    assert b.snapshot()["unknown"] == 1
    assert Decimal(b.snapshot()["committed_estimated_usd"]) == FULL_RESERVATION_USD


def test_exact_root_two_hundred_attempt_ceiling_includes_all_unknowns():
    b = SharedJevBudget()
    for _ in range(200):
        b.hold(b.reserve(body()))
    snap = b.snapshot()
    assert snap["unknown"] == 200 and snap["attempted_calls"] == 200
    assert Decimal(snap["committed_estimated_usd"]) == Decimal("0.537600")
    with pytest.raises(JevBudgetError, match="qa_jev_call_limit"):
        b.reserve(body())


def test_stream_eof_and_status_must_be_explicitly_supplied_not_assumed():
    b = SharedJevBudget()
    t = b.reserve(body())
    with pytest.raises(TypeError):
        b.settle(t, receipt())
    assert b.snapshot()["in_flight"] == 1
