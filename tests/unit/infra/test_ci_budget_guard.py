from datetime import date, timedelta
from decimal import Decimal
import importlib.util
import json
from pathlib import Path
import subprocess

import pytest
import yaml


ROOT = Path(__file__).parents[3]
SPEC = importlib.util.spec_from_file_location("ci_budget_guard", ROOT / "scripts/ci_budget_guard.py")
guard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(guard)


def usage(day, amount, product="actions"):
    return {"date": f"{day}T00:00:00Z", "product": product, "grossAmount": amount}


def billing_amounts(first, last):
    return [first, *[0 for day in guard.DAYS[1:-1]], last]


@pytest.fixture(params=["cobalt", "maple"], ids=["b2b", "consumer"])
def fake_billing(request, monkeypatch):
    state = {"org": request.param, "calls": [], "amounts": billing_amounts(60, 10)}

    class CheckpointDate(date):
        @classmethod
        def today(cls):
            return state.get("today", guard.DAYS[-1])

    def fake_subprocess(arguments, **kwargs):
        state["calls"].append(arguments)
        assert arguments[:2] == ["gh", "api"]
        assert arguments[-2:] == ["--paginate", "--slurp"]
        assert kwargs["timeout"] == 30 and kwargs["check"] is False
        if state.get("exception"):
            raise state["exception"]
        position = len(state["calls"]) - 1
        day = guard.DAYS[position]
        assert arguments[2] == (f"/organizations/{state['org']}/settings/billing/usage"
                                f"?year={day.year}&month={day.month}&day={day.day}")
        output = json.dumps([{"usageItems": [usage(day, state["amounts"][position]),
                                            usage(day, 99999, "storage")]}])
        return subprocess.CompletedProcess(arguments, state.get("exit_code", 0),
                                           state.get("output", output), "billing unavailable")

    monkeypatch.setattr(guard, "date", CheckpointDate)
    monkeypatch.setattr(guard.subprocess, "run", fake_subprocess)
    return state


def invoke(state, *extra):
    return guard.main(["--org", state["org"], "--in-flight-batches", "0", "--unreported-spend-bound", "0", *extra])


def test_real_billing_counts_only_actions_across_approved_days_and_baseline(fake_billing, capsys):
    assert invoke(fake_billing) == 0
    report = json.loads(capsys.readouterr().out)
    assert report == {"actions_gross": "70", "baseline": "53.78", "spent": "16.22",
                      "cap": str(guard.CAP), "next_batch_reservation": "20", "in_flight_batches": 0,
                      "in_flight_reservation": "0", "unreported_spend_bound": "0",
                      "accounted_spend_bound": "36.22", "ci_admission_allowed": True,
                      "admission_blocked_reason": None}
    assert usage(guard.DAYS[-1], 100) == {"date": f"{guard.DAYS[-1]}T00:00:00Z", "product": "actions", "grossAmount": 100}
    assert len(fake_billing["calls"]) == len(guard.DAYS)
    assert all("PATCH" not in call and "DELETE" not in call for call in fake_billing["calls"])


@pytest.mark.parametrize("offset,allowed", [(Decimal("-0.001"), True), (Decimal(0), True), (Decimal("0.001"), False),
                                          (guard.NEXT_BATCH_RESERVATION - Decimal("0.01"), False),
                                          (guard.NEXT_BATCH_RESERVATION, False)])
def test_exact_cap_blocks_default_runner_ci_admissions_without_rounding_or_api_changes(fake_billing, offset, allowed, capsys):
    fake_billing["amounts"] = billing_amounts(float(guard.BASE), float(guard.CAP - guard.NEXT_BATCH_RESERVATION + offset))
    assert invoke(fake_billing) == (0 if allowed else 1)
    report = json.loads(capsys.readouterr().out)
    assert report["ci_admission_allowed"] is allowed
    assert Decimal(report["accounted_spend_bound"]) == guard.CAP + offset
    assert len(fake_billing["calls"]) == len(guard.DAYS)


@pytest.mark.parametrize("exit_code,output", [(1, ""), (1, "[]"), (0, ""), (0, "not JSON"),
                                            (0, "null"), (0, "[{}]"), (0, '{"message":"denied"}')])
def test_billing_errors_or_invalid_json_block_instead_of_claiming_zero(fake_billing, exit_code, output, capsys):
    fake_billing.update(exit_code=exit_code, output=output)
    assert invoke(fake_billing) == 1
    result = capsys.readouterr()
    assert "BUDGET BLOCKED" in result.err and result.out == ""


@pytest.mark.parametrize("exception", [OSError("gh missing"), subprocess.TimeoutExpired("gh", 30)])
def test_billing_cli_unavailable_or_timeout_blocks(fake_billing, exception):
    fake_billing["exception"] = exception
    assert invoke(fake_billing) == 1


def test_json_nonfinite_constants_cannot_hide_in_other_products(fake_billing):
    day = guard.DAYS[0]
    fake_billing["output"] = json.dumps([{"usageItems": [usage(day, 60), usage(day, float("nan"), "storage")]}])
    with pytest.raises(guard.BudgetError, match="Invalid billing JSON"):
        guard.read_day(fake_billing["org"], day)


@pytest.mark.parametrize("items", [[], [usage(guard.DAYS[0], 100, "storage")]])
def test_missing_actions_data_is_not_zero(fake_billing, items):
    with pytest.raises(guard.BudgetError, match="No Actions billing data"):
        guard.actions_gross([{"usageItems": items}], guard.DAYS[0])


@pytest.mark.parametrize("value", [None, "10.00", True, -1, float("nan"), float("inf"), Decimal("-0.01")])
def test_missing_nonnumeric_negative_or_nonfinite_billing_is_rejected(fake_billing, value):
    with pytest.raises(guard.BudgetError, match="grossAmount"):
        guard.actions_gross([{"usageItems": [usage(guard.DAYS[0], value)]}], guard.DAYS[0])


def test_missing_amount_field_does_not_default_to_zero(fake_billing):
    item = usage(guard.DAYS[0], 10)
    del item["grossAmount"]
    with pytest.raises(guard.BudgetError, match="grossAmount"):
        guard.actions_gross([{"usageItems": [item]}], guard.DAYS[0])


@pytest.mark.parametrize("timestamp", [None, "bad date", f"{guard.DAYS[-1]}T00:00:00Z", f"{guard.DAYS[0]}T00:00:00"])
def test_wrong_or_missing_billing_day_is_rejected(fake_billing, timestamp):
    item = usage(guard.DAYS[0], 10)
    item["date"] = timestamp
    with pytest.raises(guard.BudgetError, match="billing date"):
        guard.actions_gross([{"usageItems": [item]}], guard.DAYS[0])


def test_actual_date_field_is_required_without_obsolete_schema_fallback(fake_billing):
    item = usage(guard.DAYS[0], 10)
    item["dateTime"] = item.pop("date")
    with pytest.raises(guard.BudgetError, match="billing date"):
        guard.actions_gross([{"usageItems": [item]}], guard.DAYS[0])


@pytest.mark.parametrize("missing_day", guard.DAYS)
def test_budget_below_baseline_or_missing_day_is_not_negative_or_zero_spend(fake_billing, missing_day):
    with pytest.raises(guard.BudgetError, match="baseline"):
        guard.budget_report({day: Decimal("1") for day in guard.DAYS})
    with pytest.raises(guard.BudgetError, match="All approved"):
        guard.budget_report({day: Decimal("60") for day in guard.DAYS if day != missing_day})


def test_all_billing_pages_and_real_zero_rows_are_counted(fake_billing):
    day = guard.DAYS[0]
    pages = [{"usageItems": [usage(day, 0)]}, {"usageItems": [usage(day, Decimal("53.78"))]}]
    assert guard.actions_gross(pages, day) == Decimal("53.78")
    pages.append({"message": "API error"})
    with pytest.raises(guard.BudgetError, match="usageItems"):
        guard.actions_gross(pages, day)


@pytest.mark.parametrize("today", [guard.DAYS[0], guard.DAYS[-1] + timedelta(days=1)])
def test_budget_checkpoint_cannot_silently_ignore_a_new_day(fake_billing, today, capsys):
    fake_billing["today"] = today
    assert invoke(fake_billing) == 1
    assert "approve a new window" in capsys.readouterr().err
    assert fake_billing["calls"] == []


def test_reservation_covers_workflow_timeout_cost_bound():
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    minutes = sum(job["timeout-minutes"] * len(job.get("strategy", {}).get("matrix", {}).get("shard", [1]))
                  for job in workflow["jobs"].values())
    assert minutes == 1410
    assert guard.STANDARD_RUNNER_TARIFF_CEILING == Decimal("0.008")
    assert guard.STANDARD_RUNNER_TARIFF_CEILING > Decimal("0.006")
    bounded_cost = Decimal(minutes + 3 * 360) * guard.STANDARD_RUNNER_TARIFF_CEILING
    assert bounded_cost == Decimal("19.920")
    assert guard.NEXT_BATCH_RESERVATION == Decimal("20")
    assert guard.NEXT_BATCH_RESERVATION >= bounded_cost
    assert all(after - before == timedelta(days=1) for before, after in zip(guard.DAYS, guard.DAYS[1:]))
    assert guard.BASE == Decimal("53.78") and guard.CAP > guard.NEXT_BATCH_RESERVATION


def test_supplied_live_spend_is_not_rounded_and_in_flight_batch_blocks(fake_billing, capsys):
    fake_billing["amounts"] = billing_amounts(float(guard.BASE), 76.844799102)
    assert invoke(fake_billing) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["spent"] == "76.844799102"
    assert report["accounted_spend_bound"] == "96.844799102"
    batches = int((guard.CAP - Decimal(report["accounted_spend_bound"])) // guard.NEXT_BATCH_RESERVATION) + 1
    fake_billing["calls"].clear()
    assert invoke(fake_billing, "--in-flight-batches", str(batches)) == 1
    report = json.loads(capsys.readouterr().out)
    assert Decimal(report["in_flight_reservation"]) == guard.NEXT_BATCH_RESERVATION * batches
    assert Decimal(report["accounted_spend_bound"]) == Decimal("96.844799102") + guard.NEXT_BATCH_RESERVATION * batches
    assert Decimal(report["accounted_spend_bound"]) > guard.CAP


def test_known_in_flight_batch_changes_an_otherwise_safe_admission(fake_billing, capsys):
    spent = guard.CAP - guard.NEXT_BATCH_RESERVATION - Decimal("10")
    fake_billing["amounts"] = billing_amounts(float(guard.BASE), float(spent))
    assert invoke(fake_billing) == 0
    assert json.loads(capsys.readouterr().out)["ci_admission_allowed"] is True
    fake_billing["calls"].clear()
    assert invoke(fake_billing, "--in-flight-batches", "1") == 1
    report = json.loads(capsys.readouterr().out)
    assert Decimal(report["accounted_spend_bound"]) == spent + guard.NEXT_BATCH_RESERVATION * 2
    assert report["ci_admission_allowed"] is False


def test_default_cli_cannot_admit_near_cap_and_can_still_report(fake_billing, capsys):
    spent = guard.CAP - Decimal("0.01")
    fake_billing["amounts"] = billing_amounts(float(guard.BASE), float(spent))
    assert guard.main(["--org", fake_billing["org"]]) == 1
    report = json.loads(capsys.readouterr().out)
    assert Decimal(report["spent"]) == spent
    assert report["next_batch_reservation"] == "20"
    assert report["ci_admission_allowed"] is False
    fake_billing["calls"].clear()
    assert guard.main(["--org", fake_billing["org"], "--report-only"]) == 0
    assert json.loads(capsys.readouterr().out)["ci_admission_allowed"] is False


@pytest.mark.parametrize("extra", [[], ["--in-flight-batches", "0"], ["--unreported-spend-bound", "0"]])
def test_established_cli_reports_but_refuses_unknown_outstanding_spend(fake_billing, capsys, extra):
    assert guard.main(["--org", fake_billing["org"], *extra]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["next_batch_reservation"] == "20"
    assert report["accounted_spend_bound"] is None
    assert report["ci_admission_allowed"] is False
    assert "unbounded" in report["admission_blocked_reason"]


@pytest.mark.parametrize("spent", [guard.CAP / 2, guard.CAP - Decimal("0.01"), guard.CAP + 100])
def test_safe_report_only_never_authorizes_admission(fake_billing, capsys, spent):
    fake_billing["amounts"] = billing_amounts(float(guard.BASE), float(spent))
    assert invoke(fake_billing, "--report-only") == 0
    report = json.loads(capsys.readouterr().out)
    assert report["ci_admission_allowed"] is False
    assert report["admission_blocked_reason"] == "Report only; no admission authorized"


@pytest.mark.parametrize("offset,allowed", [(Decimal(0), True), (Decimal("0.000000001"), False), (Decimal("0.22"), False)])
def test_billing_lag_and_other_external_spend_are_reserved(fake_billing, capsys, offset, allowed):
    spent = sum((Decimal(str(amount)) for amount in fake_billing["amounts"]), Decimal(0)) - guard.BASE
    bound = str(guard.CAP - spent - guard.NEXT_BATCH_RESERVATION + offset)
    assert invoke(fake_billing, "--unreported-spend-bound", bound) == (0 if allowed else 1)
    report = json.loads(capsys.readouterr().out)
    assert report["unreported_spend_bound"] == bound
    assert report["ci_admission_allowed"] is allowed


@pytest.mark.parametrize("rate,minutes", [(Decimal("0.022"), 2000), (Decimal("0.062"), 400)],
                         ids=["old-linux-8-core", "macos-3-core"])
def test_nonstandard_outstanding_work_uses_its_explicit_cost_bound(fake_billing, capsys, rate, minutes):
    spent = guard.CAP - guard.NEXT_BATCH_RESERVATION - Decimal("10")
    fake_billing["amounts"] = billing_amounts(float(guard.BASE), float(spent))
    bound = rate * minutes
    assert invoke(fake_billing, "--unreported-spend-bound", str(bound)) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["next_batch_reservation"] == "20"
    assert Decimal(report["unreported_spend_bound"]) == bound
    assert Decimal(report["accounted_spend_bound"]) == spent + guard.NEXT_BATCH_RESERVATION + bound
    assert report["ci_admission_allowed"] is False


@pytest.mark.parametrize("extra", [["--in-flight-batches", "-1"], ["--unreported-spend-bound", "-1"],
                                   ["--unreported-spend-bound", "NaN"], ["--unreported-spend-bound", "Infinity"],
                                   ["--unreported-spend-bound", "bad"]])
def test_invalid_outstanding_spend_cannot_admit(fake_billing, extra):
    if extra[0] == "--in-flight-batches":
        assert invoke(fake_billing, *extra) == 1
    else:
        with pytest.raises(SystemExit) as rejected:
            invoke(fake_billing, *extra)
        assert rejected.value.code == 2


@pytest.mark.parametrize("count", [-1, True, "1", 0.5])
def test_in_flight_api_requires_a_nonnegative_integer(fake_billing, count):
    with pytest.raises(guard.BudgetError, match="batch count"):
        guard.budget_report({day: Decimal("60") for day in guard.DAYS},
                            in_flight_batches=count, unreported_spend_bound=Decimal(0))


@pytest.mark.parametrize("bound", [Decimal("-1"), Decimal("NaN"), Decimal("Infinity"), True, "0"])
def test_unreported_api_requires_a_finite_nonnegative_upper_bound(fake_billing, bound):
    with pytest.raises(guard.BudgetError, match="upper bound"):
        guard.budget_report({day: Decimal("60") for day in guard.DAYS},
                            in_flight_batches=0, unreported_spend_bound=bound)
