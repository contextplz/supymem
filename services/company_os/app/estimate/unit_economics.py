"""Customer-month economics and cash projections using your stated inputs."""
from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from math import ceil, isfinite

REQUIRED_INPUTS = ("price_per_month", "cost_to_serve_per_month", "monthly_churn", "cac",
                   "fixed_monthly_costs", "customer_ramp")
INPUT_QUESTIONS = {
    "price_per_month": "What price do you charge per customer per month?",
    "cost_to_serve_per_month": "What does it cost you to serve one customer per month? Include direct delivery costs.",
    "monthly_churn": "What is your monthly customer churn? Use a percentage or a fraction from 0 to 1.",
    "cac": "What does it cost you to acquire one customer?",
    "fixed_monthly_costs": "What are your fixed monthly costs?",
    "customer_ramp": "What is your customer ramp? List active customers for each month, starting with month 1. This model starts from zero customers.",
    "starting_cash": "What is your starting cash? This is optional; reply skip if you do not want to include it.",
}


def validate_inputs(inputs: Mapping) -> dict:
    """Validate supplied fields without substituting numbers for missing ones."""
    if not isinstance(inputs, Mapping):
        raise ValueError("Supply your numbers as an object with named inputs.")
    result = {}
    for field in (*REQUIRED_INPUTS, "starting_cash"):
        if field not in inputs:
            continue
        raw = inputs[field]
        if field == "starting_cash" and raw is None:
            result[field] = None
            continue
        if field == "customer_ramp":
            if not isinstance(raw, (list, tuple)) or not raw:
                raise ValueError("Your customer ramp needs at least one monthly active customer count.")
            values = raw
        else:
            values = [raw]
        numbers = []
        for value in values:
            try:
                number = float(value) if not isinstance(value, bool) else float("nan")
            except (TypeError, ValueError, OverflowError):
                number = float("nan")
            if not isfinite(number) or number < 0:
                raise ValueError(f"Your {field.replace('_', ' ')} must contain finite, non-negative numbers.")
            if field == "monthly_churn" and number > 1:
                raise ValueError("Your monthly churn must be between 0 and 100%.")
            if field == "customer_ramp" and not number.is_integer():
                raise ValueError("Your customer ramp must contain whole customer counts.")
            numbers.append(number)
        result[field] = numbers if field == "customer_ramp" else numbers[0]
    return result


def _projection(inputs: dict) -> dict:
    precise_contribution = Decimal(str(inputs["price_per_month"])) - Decimal(str(inputs["cost_to_serve_per_month"]))
    contribution = float(precise_contribution)
    churn, cac, fixed = inputs["monthly_churn"], inputs["cac"], inputs["fixed_monthly_costs"]
    payback = cac / contribution if contribution > 0 else None
    ltv = contribution / churn if churn > 0 else None
    ratio = ltv / cac if ltv is not None and cac > 0 else None
    if any(value is not None and not isfinite(value) for value in (contribution, payback, ltv, ratio)):
        raise ValueError("Your numbers are too large to calculate safely. Check your inputs and units.")
    threshold = Decimal(str(fixed)) / precise_contribution if contribution > 0 else None
    if threshold is not None and not isfinite(threshold):
        raise ValueError("Your break-even requires numbers too large to calculate safely. Check your inputs and units.")
    break_even = ceil(threshold) if threshold is not None else (0 if fixed == 0 and contribution == 0 else None)
    cumulative, lowest, previous, break_even_month = 0.0, 0.0, 0.0, None
    months = []
    for month, customers in enumerate(inputs["customer_ramp"], 1):
        acquired = max(0.0, customers - previous * (1 - churn))
        profit = customers * contribution - fixed - acquired * cac
        cumulative += profit
        if not isfinite(cumulative):
            raise ValueError("Your ramp produces numbers too large to calculate safely. Check your inputs and units.")
        lowest = min(lowest, cumulative)
        if profit >= 0 and break_even_month is None:
            break_even_month = month
        months.append({"month": month, "customers": customers, "acquired": acquired,
                       "net_cash": profit, "cumulative_cash": cumulative})
        previous = customers
    flags = []
    if contribution < 0:
        flags.append("You have negative contribution: each customer costs more to serve than you charge.")
    elif contribution == 0:
        flags.append("Your contribution is zero, so customers cannot cover fixed costs or acquisition.")
    if payback is not None and payback > 12:
        flags.append("Your acquisition payback is over 12 months.")
    if ratio is not None and ratio < 3:
        flags.append("Your LTV/CAC is below 3.")
    cash_needed = max(0.0, -lowest)
    cash = inputs.get("starting_cash")
    return {"contribution": contribution, "payback_months": payback, "ltv": ltv, "ltv_cac": ratio,
            "break_even_customers": break_even, "months_to_break_even": break_even_month,
            "cash_needed": cash_needed, "additional_cash_needed": max(0.0, cash_needed - cash) if cash is not None else None,
            "months": months, "red_flags": flags}


def calculate_unit_economics(inputs: Mapping) -> dict:
    values = validate_inputs(inputs)
    missing = [field for field in REQUIRED_INPUTS if field not in values]
    if missing:
        raise ValueError(INPUT_QUESTIONS[missing[0]])
    try:
        result = _projection(values)
    except OverflowError as error:
        raise ValueError("Your numbers are too large to calculate safely. Check your inputs and units.") from error
    scenarios = {
        "Price -10%": {**values, "price_per_month": float(Decimal(str(values["price_per_month"])) * Decimal("0.9"))},
        "Churn +50%": {**values, "monthly_churn": min(1.0, values["monthly_churn"] * 1.5)},
        "CAC +25%": {**values, "cac": float(Decimal(str(values["cac"])) * Decimal("1.25"))},
    }
    return {**result, "inputs": values, "what_ifs": {label: _projection(spec) for label, spec in scenarios.items()}}
