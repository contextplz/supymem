import pytest

from tests.company_os_harness import load_company_os_app


@pytest.mark.parametrize("audience,price,cost,churn,cac,fixed,cash,needed,ltv,ratio", [
    ("Wholesale inventory teams", 100, 20, .05, 160, 800, 1000, 3440, 1600, 10),
    ("People learning a language at home", 30, 6, .1, 48, 240, 100, 1104, 240, 5),
])
def test_customer_month_economics_ramp_sensitivity_and_validation(audience, price, cost, churn, cac, fixed, cash, needed, ltv, ratio):
    load_company_os_app()
    from app.estimate.unit_economics import INPUT_QUESTIONS, REQUIRED_INPUTS, calculate_unit_economics

    inputs = {"price_per_month": price, "cost_to_serve_per_month": cost, "monthly_churn": churn,
              "cac": cac, "fixed_monthly_costs": fixed, "customer_ramp": [0, 10, 20, 30, 40], "starting_cash": cash}
    result = calculate_unit_economics({**inputs, "audience": audience})
    assert result["contribution"] == price - cost
    assert result["payback_months"] == 2
    assert result["ltv"] == pytest.approx(ltv) and result["ltv_cac"] == pytest.approx(ratio)
    assert result["break_even_customers"] == 10 and result["months_to_break_even"] == 5
    assert result["cash_needed"] == pytest.approx(needed)
    assert result["additional_cash_needed"] == pytest.approx(needed - cash)
    assert result["months"][2]["acquired"] == pytest.approx(10 + 10 * churn)
    assert not result["red_flags"]
    worse = result["what_ifs"]
    assert set(worse) == {"Price -10%", "Churn +50%", "CAC +25%"}
    assert worse["Price -10%"]["contribution"] == pytest.approx(price * .9 - cost)
    assert worse["Churn +50%"]["ltv"] == pytest.approx(ltv / 1.5)
    assert worse["CAC +25%"]["payback_months"] == pytest.approx(2.5)
    assert all(scenario["cash_needed"] > needed for scenario in worse.values())
    if churn == .05:
        assert [worse[label]["cash_needed"] for label in worse] == pytest.approx([4040, 3560, 4700])
    without_cash = calculate_unit_economics({field: value for field, value in inputs.items() if field != "starting_cash"})
    assert without_cash["additional_cash_needed"] is None and without_cash["cash_needed"] == needed
    for field in REQUIRED_INPUTS:
        with pytest.raises(ValueError, match="What") as missing:
            calculate_unit_economics({key: value for key, value in inputs.items() if key != field})
        assert str(missing.value) == INPUT_QUESTIONS[field]
    for field, value in [("price_per_month", -1), ("cost_to_serve_per_month", None), ("cac", True),
                         ("fixed_monthly_costs", float("inf")), ("starting_cash", float("nan")),
                         ("monthly_churn", 1.01), ("customer_ramp", []), ("customer_ramp", [1.5]),
                         ("customer_ramp", [1, -1]), ("customer_ramp", "1,2,3")]:
        with pytest.raises(ValueError):
            calculate_unit_economics({**inputs, field: value})
    free = calculate_unit_economics({**inputs, "cac": 0, "monthly_churn": 0})
    assert free["payback_months"] == 0 and free["ltv"] is None and free["ltv_cac"] is None
    capped = calculate_unit_economics({**inputs, "monthly_churn": 1})
    assert capped["what_ifs"]["Churn +50%"]["ltv"] == price - cost
    with pytest.raises(ValueError, match="too large"):
        calculate_unit_economics({**inputs, "price_per_month": 1e308, "monthly_churn": 1e-308})
    pennies = calculate_unit_economics({**inputs, "price_per_month": .3, "cost_to_serve_per_month": .2, "fixed_monthly_costs": 7})
    assert pennies["contribution"] == .1 and pennies["break_even_customers"] == 70
    negative = calculate_unit_economics({**inputs, "price_per_month": cost - 1})
    assert negative["break_even_customers"] is None and negative["payback_months"] is None
    assert any("negative contribution" in flag for flag in negative["red_flags"])
    flagged = calculate_unit_economics({**inputs, "cac": 13 * (price - cost), "monthly_churn": .5})
    assert any("over 12 months" in flag for flag in flagged["red_flags"])
    assert any("below 3" in flag for flag in flagged["red_flags"])
