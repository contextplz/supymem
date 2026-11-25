import pytest

from tests.company_os_harness import load_company_os_app


@pytest.mark.parametrize("audience,scale", [("Freight dispatch teams", 100), ("Home aquarium keepers", 1)])
def test_price_band_math_and_real_answer_validation(audience, scale):
    load_company_os_app()
    from app.experiments.price_band import PRICE_FIELDS, calculate_price_band

    answers = [dict(zip(PRICE_FIELDS, [scale * (value + offset) for value in (1, 2, 3, 4)]), audience=audience)
               for offset in range(5)]
    invalid = [[4, 3, 2, 1], [1, 2, 3], [True, 2, 3, 4], [1, 2, 3, float("nan")],
               [1, 2, 3, float("inf")], [0, 2, 3, 4], [None, 2, 3, 4], "not an interview"]
    result = calculate_price_band(answers + invalid)
    assert result["status"] == "ok"
    assert result["count_used"] == 5 and result["count_dropped"] == len(invalid)
    assert result["count_out_of_order"] == 1
    assert result["pmc"] == pytest.approx(4 * scale)
    assert result["pme"] == pytest.approx(5 * scale)
    assert result["optimal"] == result["indifference"] == pytest.approx(4.5 * scale)
    for size in range(5):
        refused = calculate_price_band(answers[:size] + invalid)
        assert refused["status"] == "needs_answers" and refused["count_used"] == size
        assert "at least 5 valid real answers" in refused["message"]
        assert "pmc" not in refused
    tied = calculate_price_band([[scale] * 4] * 5)
    assert all(tied[field] == scale for field in ("pmc", "pme", "optimal", "indifference"))
    homogeneous = calculate_price_band([[scale * value for value in (1, 2, 3, 4)]] * 5)
    assert homogeneous["pmc"] == 2 * scale and homogeneous["pme"] == 3 * scale
    assert homogeneous["optimal"] == homogeneous["indifference"] == 2.5 * scale
    with pytest.raises(ValueError, match="real interview or survey"):
        calculate_price_band("invent answers")
