"""Migrated PG: assistant receipts settle known usage, unknown outcomes stay held."""
import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from tests.integration.test_postgres_auth_security import pg_database as pg_database  # noqa: F401
from tests.integration.test_postgres_journey_jobs import journey_db as journey_db  # noqa: F401


@pytest.fixture
def metered_assistant(journey_db, monkeypatch):
    from app.chat import assistant
    from app.credits.accounts import ensure_account, get_account_by_id
    from app.db.models import CreditLedgerEntry, CreditReservation
    from app.llm import founder_provider
    from app.tenancy import bind
    from llm_gateway import sole

    d = journey_db
    monkeypatch.setenv("LLM_OFFLINE", "0")
    monkeypatch.setenv("LLM_CREDITS_REQUIRE_GATE", "1")
    monkeypatch.setattr(founder_provider, "register_workspace_llm_resolver", lambda: None)
    monkeypatch.setattr(founder_provider, "resolve_llm_config", lambda wid: SimpleNamespace(
        model="DeepSeek-V4-Pro", provider="azure_foundry", source="platform_default"))
    account = ensure_account(d["vid"], workspace_id=d["wid"], owner_person_id=d["actor"])
    before = get_account_by_id(account.id)
    calls = []

    def run(*, error=None, usage=None, outcomes=None, router=False):
        async def transport(messages, **kwargs):
            calls.append(kwargs)
            # The actual charge facade must persist protection before this seam.
            with d["factory"]() as session:
                reservation = session.scalars(select(CreditReservation).where(
                    CreditReservation.account_id == account.id,
                    CreditReservation.purpose == kwargs["purpose"],
                    CreditReservation.status == "reconciling")).one()
                assert reservation.status == "reconciling"
            if outcomes is not None:
                result = outcomes[len(calls) - 1]
                if isinstance(result, BaseException):
                    raise result
                return result
            if error is not None:
                raise error
            return SimpleNamespace(usage=usage, choices=[SimpleNamespace(
                finish_reason="stop", message=SimpleNamespace(content="A grounded reply."))])

        monkeypatch.setattr(sole, "transmit", transport)
        with bind(workspace_id=d["wid"], venture_id=d["vid"], actor=d["actor"]):
            if router:
                from app.chat import llm_router
                return asyncio.run(llm_router._ask_model(d["vid"], "Explain the saved purpose",
                    SimpleNamespace(venture={"workspace_id": d["wid"]})))
            return asyncio.run(assistant._complete(
                [{"role": "user", "content": "What purpose is recorded?"}], workspace_id=d["wid"]))

    def snapshot():
        with d["factory"]() as session:
            row = session.scalars(select(CreditReservation).where(
                CreditReservation.account_id == account.id,
                CreditReservation.purpose == assistant.ASSISTANT_PURPOSE)).one()
            spends = session.scalars(select(CreditLedgerEntry).where(
                CreditLedgerEntry.account_id == account.id, CreditLedgerEntry.kind == "spend")).all()
            return dict(status=row.status, amount=row.amount_micro, settled=row.settled_micro,
                        reservation_id=row.id, spend_count=len(spends))

    return d, run, snapshot, before, lambda: get_account_by_id(account.id), calls


def assert_held(fixture):
    d, _, snapshot, before, current, calls = fixture
    from app.credits.reserve import sweep_expired
    from core.clock import utcnow

    row = snapshot()
    account = current()
    assert row["status"] == "reconciling" and row["settled"] is None
    assert account.reserved_micro == before.reserved_micro + row["amount"]
    assert account.spent_micro == before.spent_micro and row["spend_count"] == 0
    # Expiry/reload cannot silently turn the protected unknown outcome into free spend.
    sweep_expired(utcnow() + timedelta(days=2))
    assert snapshot()["status"] == "reconciling"
    assert current().reserved_micro == account.reserved_micro
    assert len(calls) == 1 and calls[0]["single_attempt"] is True
    assert calls[0]["max_tokens"] == 700


def test_timeout_keeps_actual_protected_ledger_hold(metered_assistant):
    from llm_gateway.errors import classify_provider_error

    raw = type("APITimeoutError", (RuntimeError,), {})("Synthetic timeout")
    from llm_gateway.errors import ProviderCallError
    with pytest.raises(ProviderCallError):
        metered_assistant[1](error=classify_provider_error(raw))
    assert_held(metered_assistant)


def test_empty_usage_keeps_actual_protected_ledger_hold(metered_assistant):
    from llm_gateway.errors import ProviderCallError
    with pytest.raises(ProviderCallError):
        metered_assistant[1](usage={})
    assert_held(metered_assistant)


@pytest.mark.parametrize("code,status", [
    ("provider_down", None), ("provider_down", 500), ("provider_down", 502),
    ("provider_down", 503), ("provider_error", None), ("bad_api_key", None),
    ("rate_limited", None), ("provider_error", 429),
])
def test_ambiguous_provider_errors_do_not_release(metered_assistant, code, status):
    from llm_gateway.errors import ProviderCallError

    with pytest.raises(ProviderCallError):
        metered_assistant[1](error=ProviderCallError(code, "Synthetic error", status=status))
    assert_held(metered_assistant)


@pytest.mark.parametrize("usage", [
    None, {"prompt_tokens": 10}, {"prompt_tokens": True, "completion_tokens": 1, "total_tokens": 2},
    {"prompt_tokens": -1, "completion_tokens": 1, "total_tokens": 0},
    {"prompt_tokens": 10, "completion_tokens": 1, "total_tokens": 12},
    {"prompt_tokens": 10, "completion_tokens": 1},
    {"input_tokens": 10, "output_tokens": False},
    {"input_tokens": 10, "output_tokens": 1, "cache_read_input_tokens": -1},
])
def test_unknown_usage_never_settles_as_zero(metered_assistant, usage):
    from llm_gateway.errors import ProviderCallError
    with pytest.raises(ProviderCallError):
        metered_assistant[1](usage=usage)
    assert_held(metered_assistant)


def test_cancellation_keeps_actual_protected_ledger_hold(metered_assistant):
    with pytest.raises(asyncio.CancelledError):
        metered_assistant[1](error=asyncio.CancelledError())
    assert_held(metered_assistant)


@pytest.mark.parametrize("code,status", [
    ("bad_api_key", 401), ("bad_api_key", 403), ("rate_limited", 429), ("out_of_credits", 402),
])
def test_explicit_provider_rejection_releases_protected_hold(metered_assistant, code, status):
    from llm_gateway.errors import ProviderCallError

    _, run, snapshot, before, current, calls = metered_assistant
    with pytest.raises(ProviderCallError) as error:
        run(error=ProviderCallError(code, "Synthetic rejection", status=status))
    assert error.value.unspent_confirmed is True
    row = snapshot()
    assert row["status"] == "released" and row["settled"] is None
    assert current().reserved_micro == before.reserved_micro
    assert current().spent_micro == before.spent_micro and row["spend_count"] == 0
    assert len(calls) == 1


def retry_response(content="A grounded reply.", usage=None):
    return SimpleNamespace(usage=usage if usage is not None else {
        "prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


def reservation_rows(fixture, purpose):
    from app.db.models import CreditReservation
    d, _, _, _, current, _ = fixture
    with d["factory"]() as session:
        return [dict(id=r.id, status=r.status, amount=r.amount_micro, settled=r.settled_micro,
                     workspace=r.workspace_id, account=r.account_id)
                for r in session.scalars(select(CreditReservation).where(
                    CreditReservation.account_id == current().id,
                    CreditReservation.purpose == purpose))]


@pytest.mark.parametrize("successful", [False, True])
@pytest.mark.parametrize("router", [False, True])
def test_actual_three_attempt_ledgers_release_before_fresh_reserve(metered_assistant, monkeypatch, successful, router):
    from app.chat import assistant, llm_router
    from app.llm.provider_errors import confirmed_provider_rejection
    from llm_gateway.errors import ProviderCallError

    sleeps = []
    purpose = llm_router.PURPOSE if router else assistant.ASSISTANT_PURPOSE
    async def sleep(seconds):
        # A new attempt must not overlap the previous reservation's release.
        rows = reservation_rows(metered_assistant, purpose)
        assert all(r["status"] == "released" for r in rows)
        sleeps.append(seconds)
    monkeypatch.setattr(assistant.asyncio, "sleep", sleep)
    def reject():
        return ProviderCallError("rate_limited", "Synthetic rejection", status=429, retryable=True)
    outcomes = [reject(), reject(), retry_response("{}" if router else "A grounded reply.") if successful else reject()]
    if successful:
        result = metered_assistant[1](outcomes=outcomes, router=router)
        # An empty router decision falls back to the plain assistant for the founder's text.
        assert ([intent for intent, _ in result.acts] == ["assistant"]) if router else result == "A grounded reply."
    else:
        with pytest.raises(ProviderCallError) as error:
            metered_assistant[1](outcomes=outcomes, router=router)
        assert confirmed_provider_rejection(error.value)
    rows = reservation_rows(metered_assistant, purpose)
    assert len(rows) == len({r["id"] for r in rows}) == 3
    assert sum(r["status"] == "released" for r in rows) == (2 if successful else 3)
    assert sum(r["status"] == "settled" for r in rows) == int(successful)
    assert len(metered_assistant[5]) == 3 and sleeps == [1, 2]
    assert metered_assistant[4]().reserved_micro == metered_assistant[3].reserved_micro


@pytest.mark.parametrize("code,status", [("rate_limited", 429), ("bad_api_key", 401), ("out_of_credits", 402)])
def test_actual_release_failure_retains_hold_and_refuses_retry(metered_assistant, monkeypatch, code, status):
    from app.credits import gate
    from llm_gateway.errors import ProviderCallError
    def failed_release(*args, **kwargs):
        raise RuntimeError("Synthetic release unavailable")
    monkeypatch.setattr(gate, "release", failed_release)
    with pytest.raises(ProviderCallError) as error:
        metered_assistant[1](error=ProviderCallError(code, "Synthetic rejection", status=status, retryable=True))
    assert error.value.code == "provider_outcome_unknown"
    assert_held(metered_assistant)


@pytest.mark.parametrize("outcome", ["timeout", "unknown_usage", "known_invalid_json", "rejection"])
def test_actual_router_single_call_ledger_and_fallback_boundary(metered_assistant, outcome):
    from app.chat import llm_router
    from llm_gateway.errors import ProviderCallError
    if outcome == "known_invalid_json":
        # Settled but unusable routing output falls back to the plain assistant for the founder's own text;
        # the paid router call is still charged once and never replayed.
        decision = metered_assistant[1](router=True, outcomes=[retry_response("invalid JSON")])
        assert [intent for intent, _ in decision.acts] == ["assistant"]
    else:
        with pytest.raises(ProviderCallError) as error:
            metered_assistant[1](router=True,
                error=(TimeoutError("Synthetic timeout") if outcome == "timeout" else
                       ProviderCallError("rate_limited", "Synthetic rejection", status=429))
                      if outcome != "unknown_usage" else None,
                usage={} if outcome == "unknown_usage" else None)
        assert getattr(error.value, "unspent_confirmed", False) is (outcome == "rejection")
    rows = reservation_rows(metered_assistant, llm_router.PURPOSE)
    assert len(rows) == 1 and len(metered_assistant[5]) == 1
    expected = {"known_invalid_json": "settled", "rejection": "released"}.get(outcome, "reconciling")
    assert rows[0]["status"] == expected


def test_actual_router_release_failure_keeps_ledger_and_has_no_second_attempt(metered_assistant, monkeypatch):
    from app.chat import llm_router
    from app.credits import gate
    from llm_gateway.errors import ProviderCallError
    def failed_release(*args, **kwargs):
        raise RuntimeError("Synthetic release unavailable")
    monkeypatch.setattr(gate, "release", failed_release)
    with pytest.raises(ProviderCallError) as error:
        metered_assistant[1](router=True,
            error=ProviderCallError("rate_limited", "Synthetic rejection", status=429, retryable=True))
    assert error.value.code == "provider_outcome_unknown"
    rows = reservation_rows(metered_assistant, llm_router.PURPOSE)
    assert len(rows) == len(metered_assistant[5]) == 1
    assert rows[0]["status"] == "reconciling" and rows[0]["settled"] is None
    assert metered_assistant[4]().reserved_micro == metered_assistant[3].reserved_micro + rows[0]["amount"]
    assert metered_assistant[4]().spent_micro == metered_assistant[3].spent_micro


@pytest.mark.parametrize("usage,prompt,completion", [
    ({"prompt_tokens": 120, "completion_tokens": 40, "total_tokens": 160,
      "prompt_tokens_details": {"cached_tokens": 30},
      "completion_tokens_details": {"reasoning_tokens": 8}}, 120, 40),
    ({"input_tokens": 50, "output_tokens": 20, "cache_read_input_tokens": 100,
      "cache_creation_input_tokens": 10}, 160, 20),
    ({"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}, 0, 0),
])
def test_complete_usage_settles_exact_counts(metered_assistant, usage, prompt, completion):
    from app.credits.estimate import actual_llm_micro

    d, run, snapshot, before, current, calls = metered_assistant
    from app.db.models import CreditLedgerEntry

    assert run(usage=usage) == "A grounded reply."
    row = snapshot()
    actual = actual_llm_micro("DeepSeek-V4-Pro", prompt_tokens=prompt, completion_tokens=completion)
    assert row["status"] == "settled" and row["settled"] == actual
    assert current().reserved_micro == before.reserved_micro
    assert current().spent_micro == before.spent_micro + actual and row["spend_count"] == 1
    with d["factory"]() as session:
        entry = session.scalars(select(CreditLedgerEntry).where(
            CreditLedgerEntry.account_id == current().id, CreditLedgerEntry.kind == "spend")).one()
        assert (entry.prompt_tokens, entry.completion_tokens) == (prompt, completion)
    assert len(calls) == 1
