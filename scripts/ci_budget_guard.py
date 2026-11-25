#!/usr/bin/env python3
"""Read Actions gross billing for the approved budget window."""

import argparse
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import re
import subprocess
import sys


DAYS = (date(2026, 10, 29), date(2026, 10, 30), date(2026, 10, 31))
BASE = Decimal("53.78")
CAP = Decimal("366")
STANDARD_RUNNER_TARIFF_CEILING = Decimal("0.008")
NEXT_BATCH_RESERVATION = Decimal("20")


class BudgetError(RuntimeError):
    pass


def actions_gross(pages, day):
    if not isinstance(pages, list) or not pages:
        raise BudgetError(f"Missing billing pages for {day}")
    amounts = []
    for page in pages:
        if not isinstance(page, dict) or not isinstance(page.get("usageItems"), list):
            raise BudgetError(f"Missing usageItems for {day}")
        for item in page["usageItems"]:
            if not isinstance(item, dict) or not isinstance(item.get("product"), str) or not item["product"]:
                raise BudgetError(f"Invalid billing product for {day}")
            if item["product"] != "actions":
                continue
            value = item.get("grossAmount")
            if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
                raise BudgetError(f"Missing or nonnumeric Actions grossAmount for {day}")
            amount = Decimal(str(value))
            if not amount.is_finite() or amount < 0:
                raise BudgetError(f"Negative or nonfinite Actions grossAmount for {day}")
            try:
                timestamp = datetime.fromisoformat(item["date"].replace("Z", "+00:00"))
                if timestamp.tzinfo is None or timestamp.astimezone(timezone.utc).date() != day:
                    raise ValueError("Wrong billing date")
            except (KeyError, AttributeError, TypeError, ValueError) as error:
                raise BudgetError(f"Missing or wrong Actions billing date for {day}") from error
            amounts.append(amount)
    if not amounts:
        raise BudgetError(f"No Actions billing data for {day}; not evidence of zero spend")
    return sum(amounts, Decimal(0))


def reject_json_constant(value):
    raise ValueError(f"Invalid JSON constant: {value}")


def read_day(org, day):
    endpoint = f"/organizations/{org}/settings/billing/usage?year={day.year}&month={day.month}&day={day.day}"
    try:
        result = subprocess.run(["gh", "api", endpoint, "--paginate", "--slurp"],
                                capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise BudgetError(f"Billing CLI unavailable or timed out for {day}: {error}") from error
    if result.returncode != 0:
        raise BudgetError(f"Billing CLI failed for {day} (exit {result.returncode}): {result.stderr.strip()[:500]}")
    if not result.stdout.strip():
        raise BudgetError(f"Empty billing JSON for {day}")
    try:
        pages = json.loads(result.stdout, parse_float=Decimal, parse_constant=reject_json_constant)
    except ValueError as error:
        raise BudgetError(f"Invalid billing JSON for {day}") from error
    return actions_gross(pages, day)


def budget_report(daily_gross, *, in_flight_batches=None, unreported_spend_bound=None):
    if set(daily_gross) != set(DAYS):
        raise BudgetError(f"All approved Actions billing totals are required: {', '.join(day.isoformat() for day in DAYS)}")
    for amount in daily_gross.values():
        if not isinstance(amount, Decimal) or not amount.is_finite() or amount < 0:
            raise BudgetError("Invalid daily Actions billing total")
    gross = sum(daily_gross.values(), Decimal(0))
    spent = gross - BASE
    if spent < 0:
        raise BudgetError("Actions gross is below the established baseline; billing is incomplete")
    if in_flight_batches is not None and (type(in_flight_batches) is not int or in_flight_batches < 0):
        raise BudgetError("In-flight batch count must be a known nonnegative integer")
    if unreported_spend_bound is not None and (
            not isinstance(unreported_spend_bound, Decimal) or not unreported_spend_bound.is_finite()
            or unreported_spend_bound < 0):
        raise BudgetError("Unreported/external spend needs a finite nonnegative upper bound")
    in_flight_reservation = None if in_flight_batches is None else NEXT_BATCH_RESERVATION * in_flight_batches
    accounted = None
    reason = "In-flight work or unreported/external spend is unbounded; admission requires serialized, bounded work"
    if in_flight_reservation is not None and unreported_spend_bound is not None:
        accounted = spent + NEXT_BATCH_RESERVATION + in_flight_reservation + unreported_spend_bound
        reason = None if accounted <= CAP else "Reported spend plus reservations and unreported spend exceeds the cap"
    return {"actions_gross": str(gross), "baseline": str(BASE), "spent": str(spent),
            "cap": str(CAP), "next_batch_reservation": str(NEXT_BATCH_RESERVATION),
            "in_flight_batches": in_flight_batches,
            "in_flight_reservation": None if in_flight_reservation is None else str(in_flight_reservation),
            "unreported_spend_bound": None if unreported_spend_bound is None else str(unreported_spend_bound),
            "accounted_spend_bound": None if accounted is None else str(accounted),
            "ci_admission_allowed": reason is None, "admission_blocked_reason": reason}


def parse_spend_bound(value):
    try:
        amount = Decimal(value)
    except InvalidOperation as error:
        raise argparse.ArgumentTypeError("Expected a nonnegative monetary upper bound") from error
    if not amount.is_finite() or amount < 0:
        raise argparse.ArgumentTypeError("Expected a finite nonnegative monetary upper bound")
    return amount


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--org", required=True)
    parser.add_argument("--report-only", action="store_true", help="Report valid billing without authorizing admission")
    parser.add_argument("--in-flight-batches", type=int, help="Known outstanding standard batches, including queued work")
    parser.add_argument("--unreported-spend-bound", type=parse_spend_bound,
                        help="Upper bound for billing lag and all other unreported/external Actions spend")
    args = parser.parse_args(argv)
    try:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*", args.org):
            raise BudgetError("Invalid billing organization")
        if date.today() != DAYS[-1]:
            raise BudgetError(f"Budget checkpoint is only valid on {DAYS[-1].isoformat()}; you must approve a new window")
        report = budget_report({day: read_day(args.org, day) for day in DAYS},
                               in_flight_batches=args.in_flight_batches,
                               unreported_spend_bound=args.unreported_spend_bound)
        if args.report_only:
            report["ci_admission_allowed"] = False
            report["admission_blocked_reason"] = "Report only; no admission authorized"
        print(json.dumps(report))
        if args.report_only:
            return 0
        if not report["ci_admission_allowed"]:
            raise BudgetError(report["admission_blocked_reason"])
        return 0
    except BudgetError as error:
        print(f"BUDGET BLOCKED: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
