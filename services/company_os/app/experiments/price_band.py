"""Empirical price sensitivity from supplied interview or survey answers only."""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections.abc import Mapping, Sequence
from math import isfinite

PRICE_FIELDS = ("too_cheap", "bargain", "expensive", "too_expensive")
PRICE_QUESTIONS = (
    "At what price would this be too cheap for you to trust its quality?",
    "At what price would you consider this a bargain?",
    "At what price would this start getting expensive, but you would still consider it?",
    "At what price would this be too expensive for you to buy?",
)


def calculate_price_band(answers: Sequence) -> dict:
    """Interpolate empirical curves at observed prices, using plateau midpoints.

    PMC crosses too-cheap with not-a-bargain; PME crosses not-expensive
    with too-expensive. OPP crosses the two extremes; IPP the middle curves.
    Use one currency and billing period for every respondent.
    """
    if not isinstance(answers, Sequence) or isinstance(answers, (str, bytes)):
        raise ValueError("Paste a list of real interview or survey answers.")
    valid, out_of_order = [], 0
    for answer in answers:
        raw = [answer.get(field) for field in PRICE_FIELDS] if isinstance(answer, Mapping) else answer
        if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)) or len(raw) != 4:
            continue
        try:
            if any(isinstance(value, bool) for value in raw):
                continue
            prices = tuple(float(value) for value in raw)
        except (TypeError, ValueError, OverflowError):
            continue
        if not all(isfinite(value) and value > 0 for value in prices):
            continue
        if list(prices) != sorted(prices):
            out_of_order += 1
            continue
        valid.append(prices)
    counts = {"count_used": len(valid), "count_dropped": len(answers) - len(valid),
              "count_out_of_order": out_of_order}
    if len(valid) < 5:
        return {**counts, "status": "needs_answers", "message":
                f"You need at least 5 valid real answers to work out a price range. You have {len(valid)}; "
                f"{counts['count_dropped']} were dropped, including {out_of_order} out of order."}
    columns = [sorted(row[index] for row in valid) for index in range(4)]
    grid = sorted({price for row in valid for price in row})
    size = len(valid)
    curves = [(size - bisect_left(columns[0], price), size - bisect_left(columns[1], price),
               bisect_right(columns[2], price), bisect_right(columns[3], price)) for price in grid]
    curves = [(size, size, 0, 0), *curves, (0, 0, size, size)]
    grid = [grid[0], *grid, grid[-1]]

    def intersection(differences: list[int]) -> float:
        zeros = [price for price, difference in zip(grid, differences) if difference == 0]
        if zeros:
            return zeros[0] / 2 + zeros[-1] / 2
        for index in range(1, len(grid)):
            before, after = differences[index - 1:index + 1]
            if before > 0 > after:
                fraction = before / (before - after)
                return grid[index - 1] * (1 - fraction) + grid[index] * fraction
        raise ValueError("These answers do not produce a usable price crossing.")

    pmc = intersection([cheap_limit - (size - bargain) for cheap_limit, bargain, _, _ in curves])
    pme = intersection([size - expensive - limit for _, _, expensive, limit in curves])
    optimal = intersection([cheap_limit - limit for cheap_limit, _, _, limit in curves])
    indifference = intersection([bargain - expensive for _, bargain, expensive, _ in curves])
    return {**counts, "status": "ok", "pmc": pmc, "pme": pme,
            "optimal": optimal, "indifference": indifference}
